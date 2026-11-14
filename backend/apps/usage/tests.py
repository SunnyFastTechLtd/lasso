from datetime import date, timedelta

from django.test import TestCase
from rest_framework.test import APIClient

from backend.apps.chat.models import Conversation, Message
from backend.apps.users.models import AuditLog, User
from backend.apps.usage.services import METRIC_KEYS, build_overview


def _make_user(username, staff=False, department=''):
    user = User.objects.create_user(username=username, password='demo-pass-9x2')
    user.is_staff = staff
    user.department = department
    user.save(update_fields=['is_staff', 'department'])
    return user


def _audit(actor, action, egress, agent='', source='', elapsed_ms=None):
    return AuditLog.objects.create(
        actor=actor, action=action, target='工具调用', egress=egress,
        agent_name=agent, source_name=source, elapsed_ms=elapsed_ms,
    )


class UsageEgressMetricTests(TestCase):
    """F4：出网调用单列，且与内网工具调用互斥归属、总量不重复计数"""

    def setUp(self):
        self.today = date.today()
        self.admin = _make_user('admin', staff=True)
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def _overview(self, **kwargs):
        return build_overview(
            granularity='day', start=self.today, end=self.today,
            scope='all', department=None, **kwargs)

    def test_egress_and_internal_are_mutually_exclusive(self):
        # 内网：2 次成功 + 1 次失败；出网：1 次成功 + 1 次失败
        _audit(self.admin, 'tool_call', egress=False)
        _audit(self.admin, 'tool_call', egress=False)
        _audit(self.admin, 'tool_failed', egress=False)
        _audit(self.admin, 'tool_call', egress=True)
        _audit(self.admin, 'tool_failed', egress=True)

        data = self._overview()
        metrics = data['metrics']
        self.assertEqual(metrics['tool_calls'], 3)
        self.assertEqual(metrics['egress_calls'], 2)
        # 互斥：两项相加恰好等于全部工具调用留痕，不重复计
        self.assertEqual(metrics['tool_calls'] + metrics['egress_calls'], 5)
        # 失败次数含两类来源，不受归属拆分影响
        self.assertEqual(metrics['failures'], 2)

    def test_series_carries_egress_key_every_bucket(self):
        _audit(self.admin, 'tool_call', egress=True)
        data = self._overview()
        self.assertEqual(len(data['series']), 1)
        bucket = data['series'][0]
        self.assertEqual(bucket['egress_calls'], 1)
        self.assertEqual(bucket['tool_calls'], 0)
        for key in METRIC_KEYS:
            self.assertIn(key, bucket)

    def test_api_exposes_egress_metric_and_definition(self):
        resp = self.client.get('/api/usage/overview/', {
            'granularity': 'day',
            'start': self.today.isoformat(),
            'end': self.today.isoformat(),
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn('egress_calls', resp.data['metrics'])
        self.assertIn('egress_calls', resp.data['definitions'])
        self.assertIn('出网', resp.data['definitions']['egress_calls'])

    def test_zero_egress_is_normal_zero_not_missing(self):
        """P0 出网总开关为关：出网指标必须是 0，而不是字段缺失让前端显示「—」"""
        _audit(self.admin, 'tool_call', egress=False)
        data = self._overview()
        self.assertEqual(data['metrics']['egress_calls'], 0)
        self.assertEqual(data['series'][0]['egress_calls'], 0)

    def test_dept_scope_filters_egress_by_actor_department(self):
        other = _make_user('other', department='研发部')
        _audit(other, 'tool_call', egress=True)
        data = build_overview(
            granularity='day', start=self.today, end=self.today,
            scope='dept', department='研发部')
        self.assertEqual(data['metrics']['egress_calls'], 1)

        data_again = build_overview(
            granularity='day', start=self.today, end=self.today,
            scope='dept', department='行政部')
        self.assertEqual(data_again['metrics']['egress_calls'], 0)


class UsageBreakdownTests(TestCase):
    """F4 完整版：按智能体 / 工具来源 / 部门统计调用次数、失败率与平均耗时"""

    def setUp(self):
        self.today = date.today()
        self.admin = _make_user('admin', staff=True)
        self.dev = _make_user('dev', department='研发部')
        self.hr = _make_user('hr', department='行政部')
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def _breakdowns(self, department=None):
        return build_overview(
            granularity='day', start=self.today, end=self.today,
            scope='all' if department is None else 'dept', department=department,
        )['breakdowns']

    def test_agent_dimension_calls_failure_rate_and_avg(self):
        _audit(self.dev, 'tool_call', False, agent='工单助手', source='工单系统', elapsed_ms=100)
        _audit(self.dev, 'tool_call', False, agent='工单助手', source='工单系统', elapsed_ms=300)
        _audit(self.dev, 'tool_failed', False, agent='工单助手', source='工单系统', elapsed_ms=200)
        _audit(self.dev, 'tool_call', False, agent='制度问答', source='平台内置')

        rows = {r['key']: r for r in self._breakdowns()['agent']}
        self.assertEqual(rows['工单助手']['calls'], 3)
        self.assertEqual(rows['工单助手']['failures'], 1)
        self.assertAlmostEqual(rows['工单助手']['failure_rate'], 0.3333)
        self.assertEqual(rows['工单助手']['avg_elapsed_ms'], 200)
        # 平均耗时只统计有耗时记录的调用，缺失时留空而不是 0
        self.assertEqual(rows['制度问答']['calls'], 1)
        self.assertIsNone(rows['制度问答']['avg_elapsed_ms'])

    def test_source_dimension_includes_internal_and_egress_calls(self):
        # 失败率按来源统计；出网与内网都计入（出网已单列，但三维度看总数）
        _audit(self.dev, 'tool_call', False, agent='助手', source='工单系统', elapsed_ms=50)
        _audit(self.dev, 'tool_failed', False, agent='助手', source='工单系统', elapsed_ms=150)
        _audit(self.dev, 'tool_call', True, agent='助手', source='天气服务', elapsed_ms=80)

        rows = {r['key']: r for r in self._breakdowns()['source']}
        self.assertEqual(rows['工单系统']['calls'], 2)
        self.assertEqual(rows['工单系统']['failures'], 1)
        self.assertEqual(rows['工单系统']['avg_elapsed_ms'], 100)
        self.assertEqual(rows['天气服务']['calls'], 1)
        self.assertAlmostEqual(rows['天气服务']['failure_rate'], 0.0)

    def test_department_dimension_uses_actor_department(self):
        _audit(self.dev, 'tool_call', False, agent='助手', source='工单系统')
        _audit(self.hr, 'tool_failed', False, agent='助手', source='工单系统')

        rows = {r['key']: r for r in self._breakdowns()['department']}
        self.assertEqual(rows['研发部']['calls'], 1)
        self.assertEqual(rows['行政部']['failures'], 1)
        self.assertAlmostEqual(rows['行政部']['failure_rate'], 1.0)

    def test_dept_scope_filters_all_dimensions(self):
        _audit(self.dev, 'tool_call', False, agent='助手', source='工单系统')
        _audit(self.hr, 'tool_call', False, agent='助手', source='工单系统')

        data = build_overview(
            granularity='day', start=self.today, end=self.today,
            scope='dept', department='研发部')
        breakdowns = data['breakdowns']
        self.assertEqual([r['key'] for r in breakdowns['department']], ['研发部'])
        self.assertEqual(sum(r['calls'] for r in breakdowns['agent']), 1)
        self.assertEqual(sum(r['calls'] for r in breakdowns['source']), 1)

    def test_unrecorded_label_for_rows_without_snapshot(self):
        _audit(self.dev, 'tool_call', False)  # 升级前历史行：无快照
        rows = self._breakdowns()
        self.assertEqual(rows['agent'][0]['key'], '未记录')
        self.assertEqual(rows['source'][0]['key'], '未记录')

    def test_overview_api_exposes_breakdowns_and_definitions(self):
        _audit(self.dev, 'tool_call', False, agent='助手', source='工单系统', elapsed_ms=120)
        resp = self.client.get('/api/usage/overview/', {
            'granularity': 'day',
            'start': self.today.isoformat(),
            'end': self.today.isoformat(),
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(set(resp.data['breakdowns']), {'agent', 'source', 'department'})
        self.assertIn('agent', resp.data['breakdown_definitions'])
        self.assertEqual(resp.data['breakdowns']['agent'][0]['avg_elapsed_ms'], 120)


class UsageTokenMetricTests(TestCase):
    """真实 token 计量：按助手消息聚合，用户消息不重复计"""

    def setUp(self):
        self.today = date.today()
        self.admin = _make_user('admin', staff=True)
        self.dev = _make_user('dev', department='研发部')
        self.hr = _make_user('hr', department='行政部')
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def _answer(self, user, input_tokens, output_tokens):
        conv = Conversation.objects.create(user=user, title='计量')
        Message.objects.create(conversation=conv, role='assistant',
                               content='答', input_tokens=input_tokens,
                               output_tokens=output_tokens)
        Message.objects.create(conversation=conv, role='user', content='问',
                               input_tokens=999, output_tokens=999)

    def test_token_metrics_sum_only_assistant_messages(self):
        self._answer(self.dev, 120, 30)
        self._answer(self.dev, 80, 20)

        data = build_overview(
            granularity='day', start=self.today, end=self.today,
            scope='all', department=None)
        self.assertEqual(data['metrics']['input_tokens'], 200)
        self.assertEqual(data['metrics']['output_tokens'], 50)
        self.assertEqual(data['series'][0]['input_tokens'], 200)
        self.assertIn('input_tokens', data['definitions'])

    def test_token_metrics_respect_department_scope(self):
        self._answer(self.dev, 120, 30)
        self._answer(self.hr, 80, 20)

        data = build_overview(
            granularity='day', start=self.today, end=self.today,
            scope='dept', department='研发部')
        self.assertEqual(data['metrics']['input_tokens'], 120)
        self.assertEqual(data['metrics']['output_tokens'], 30)

    def test_normalize_usage_maps_provider_fields(self):
        from types import SimpleNamespace

        from backend.apps.common.llm import normalize_usage

        self.assertEqual(normalize_usage(SimpleNamespace(
            prompt_tokens=12, completion_tokens=5, total_tokens=17)), {
            'input_tokens': 12, 'output_tokens': 5, 'total_tokens': 17,
        })
        self.assertEqual(normalize_usage(None), {
            'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0,
        })


class UsageExportTests(TestCase):
    """F4 完整版：三维度 CSV 导出，范围与概览页一致，不导出正文与个人明细"""

    def setUp(self):
        self.today = date.today()
        self.admin = _make_user('admin', staff=True)
        self.dev = _make_user('dev', department='研发部')
        self.other = _make_user('other', department='行政部')
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def _params(self, **overrides):
        params = {
            'start': self.today.isoformat(),
            'end': self.today.isoformat(),
            'scope': 'all',
        }
        params.update(overrides)
        return params

    def test_export_csv_has_bom_header_and_breakdown_rows(self):
        _audit(self.dev, 'tool_call', False, agent='工单助手', source='工单系统', elapsed_ms=120)
        _audit(self.dev, 'tool_failed', False, agent='工单助手', source='工单系统', elapsed_ms=80)

        resp = self.client.get('/api/usage/export/', self._params())
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn('text/csv', resp['Content-Type'])
        self.assertIn('attachment;', resp['Content-Disposition'])
        text = resp.content.decode('utf-8-sig')
        self.assertTrue(text.startswith('维度,名称'))
        self.assertIn('汇总', text)
        self.assertIn('按智能体,工单助手,2,1,0.5000,100', text)
        self.assertIn('按工具来源,工单系统,2,1,0.5000,100', text)
        self.assertIn('按部门,研发部,2,1,0.5000,100', text)

    def test_export_non_admin_cannot_export_whole_company(self):
        self.client.force_authenticate(user=self.dev)
        resp = self.client.get('/api/usage/export/', self._params(scope='all'))
        self.assertEqual(resp.status_code, 403)

    def test_export_dept_scope_only_contains_own_department(self):
        _audit(self.dev, 'tool_call', False, agent='助手', source='工单系统')
        _audit(self.other, 'tool_call', False, agent='助手', source='工单系统')
        self.client.force_authenticate(user=self.dev)

        resp = self.client.get('/api/usage/export/', self._params(scope='dept'))
        self.assertEqual(resp.status_code, 200)
        text = resp.content.decode('utf-8-sig')
        self.assertIn('研发部', text)
        self.assertNotIn('行政部', text)

    def test_export_rejects_malformed_and_overlong_range(self):
        resp = self.client.get('/api/usage/export/', {'start': '2026/01/01', 'end': self.today.isoformat()})
        self.assertEqual(resp.status_code, 400)
        long_ago = self.today - timedelta(days=400)
        resp = self.client.get('/api/usage/export/', {
            'start': long_ago.isoformat(), 'end': self.today.isoformat()})
        self.assertEqual(resp.status_code, 400)
