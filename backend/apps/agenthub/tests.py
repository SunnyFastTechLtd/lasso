"""智能体装配接口测试（PRD 第 6 章 A 组：A1–A8 口径逐条落到断言）

覆盖面：A1 列表与行操作、A2 基本字段、A3 模型与档位、A4 能力挂载、
A5 授权档位、A6 可见范围、A7 发布前检查与差异视图、A8 只读发布历史。
约定：所有用例都不依赖真实大模型，模型可用性用 SystemSetting 里的
llm_verified_models 假列表驱动。
"""
import json

from unittest.mock import patch

from rest_framework.test import APIClient

from django.core.management import call_command
from django.test import TestCase

from backend.apps.agenthub import builtin_agents, services
from backend.apps.agents.core.runtime import resolve_snapshot
from backend.apps.agenthub.models import (
    AgentImportRequest, AgentMCPServer, AgentProfile, AgentSkill, AgentStatus, AgentVersion,
    PermissionTier,
)
from backend.apps.knowledge.models import KnowledgeBase
from backend.apps.mcp.models import MCPServer, MCPTool
from backend.apps.settings.models import SystemSetting
from backend.apps.skills.models import Skill, SkillScope, SkillStatus
from backend.apps.users.models import AuditLog, Department, User

BASE = '/api/agents/'
LONG_PROMPT = ('你是企业工单助手。请先读技能规范再作答，'
               '引用工单编号与状态原文，不得编造不存在的处理进度，'
               '遇到信息不足时明确说明缺哪一项。')


def _user(username, is_staff=False, department=''):
    return User.objects.create_user(username=username, password='demo-pass-9x2',
                                    is_staff=is_staff, department=department)


class _JsonClient(APIClient):
    """请求体默认 JSON 编码。

    APIClient 的默认格式是 multipart（TEST_REQUEST_DEFAULT_FORMAT），
    会把 mcp_mounts 这类嵌套结构压平成字符串，导致服务端收到非法数据。
    """

    default_format = 'json'


def _client(user):
    client = _JsonClient()
    client.force_authenticate(user=user)
    return client


def _server(name='工单系统', slug='ticket', tools=('list_tickets', 'ticket_detail'),
            **kwargs):
    """已验证可用的工具来源（status=healthy），工具默认在来源侧已启用"""
    data = {'transport': 'http_mcp', 'url': 'http://198.51.100.10:8931/mcp',
            'status': 'healthy'}
    data.update(kwargs)
    server = MCPServer.objects.create(name=name, slug=slug, **data)
    names = list(tools)
    for tool in names:
        MCPTool.objects.create(server=server, name=tool,
                               description=f'{tool} 的用途说明', enabled=True)
    server.enabled_tools = names
    server.save(update_fields=['enabled_tools'])
    return server


def _payload(**overrides):
    data = {'name': '工单问答助手', 'description': '回答工单系统的常见问题',
            'model_name': 'qwen-max', 'speech_tier': 'mid', 'retrieval_tier': 'standard',
            'permission_tier': 'whitelist', 'visibility': 'private', 'prompt': LONG_PROMPT}
    data.update(overrides)
    return data


class AgentTestCase(TestCase):
    """公共夹具：管理员 + 普通装配者 + 部门树 + 已验证模型列表"""

    def setUp(self):
        SystemSetting.set_value('llm_verified_models', '["qwen-max", "deepseek-r1"]')
        self.hq = Department.objects.create(name='总部')
        self.rd = Department.objects.create(name='研发中心', parent=self.hq)
        self.rd1 = Department.objects.create(name='研发一组', parent=self.rd)
        self.ops = Department.objects.create(name='运维部', parent=self.hq)

        self.admin = _user('admin', is_staff=True, department='总部')
        self.staff = _client(self.admin)
        self.worker = _user('worker', department='研发中心')
        self.member = _user('member', department='研发一组')
        self.outsider = _user('outsider', department='运维部')
        self.worker_client = _client(self.worker)
        self.member_client = _client(self.member)
        self.outsider_client = _client(self.outsider)

    def _make(self, client=None, **overrides):
        client = client or self.staff
        resp = client.post(BASE, _payload(**overrides))
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.data

    def _make_row(self, **overrides):
        """直接建库（绕过写入校验，用于构造异常态）"""
        data = {'name': '工单问答助手', 'description': '回答工单系统的常见问题',
                'model_name': 'qwen-max', 'prompt': LONG_PROMPT,
                'created_by': self.admin, 'updated_by': self.admin}
        data.update(overrides)
        return AgentProfile.objects.create(**data)

    def _publish(self, agent, **body):
        return self.staff.post(f'{BASE}{agent.pk}/publish/', body or {})


# ─── A1 列表与行操作 ───

class ListA1Tests(AgentTestCase):
    """A1：列表面、草稿可见性、筛选、空状态、删除不影响历史"""

    def test_list_has_no_prompt_and_shows_mount_summary(self):
        server = _server()
        self._make(mcp_mounts=[{'server': server.pk,
                               'tool_names': ['list_tickets']}])
        resp = self.staff.get(BASE)
        row = resp.data['results'][0]
        self.assertNotIn('prompt', row)
        self.assertEqual(row['mount_summary'],
                         {'mcp_servers': 1, 'skills': 0, 'knowledge_bases': 0})
        self.assertEqual(row['updated_by_name'], 'admin')
        for key in ('name', 'description', 'status', 'visibility', 'updated_at'):
            self.assertIn(key, row)

    def test_draft_only_visible_to_creator_and_admin(self):
        created = self.worker_client.post(BASE, _payload(name='我的草稿助手'))
        self.assertEqual(created.status_code, 201, created.content)
        pk = created.data['id']
        self.assertEqual(self.staff.get(BASE + str(pk) + '/').status_code, 200)
        resp = self.outsider_client.get(BASE + str(pk) + '/')
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn(pk, [r['id'] for r in self.outsider_client.get(BASE).data['results']])

    def test_published_public_agent_visible_to_everyone(self):
        agent = self._make_row(name='全员助手', visibility='public', status=AgentStatus.PUBLISHED)
        ids = [r['id'] for r in self.outsider_client.get(BASE).data['results']]
        self.assertIn(agent.pk, ids)

    def test_filters_by_status_visibility_and_keyword(self):
        self._make_row(name='审批助手', description='走审批流程', status=AgentStatus.DRAFT)
        self._make_row(name='巡检助手', description='查设备状态',
                       status=AgentStatus.PUBLISHED, visibility='public')
        rows = self.staff.get(BASE, {'status': 'published'}).data['results']
        self.assertEqual([r['name'] for r in rows], ['巡检助手'])
        rows = self.staff.get(BASE, {'visibility': 'private'}).data['results']
        self.assertEqual([r['name'] for r in rows], ['审批助手'])
        rows = self.staff.get(BASE, {'search': '巡检'}).data['results']
        self.assertEqual(len(rows), 1)
        resp = self.staff.get(BASE, {'job_tag': '行政'})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data['results']), 0)

    def test_archived_hidden_unless_explicitly_filtered(self):
        self._make_row(name='废弃助手', status=AgentStatus.ARCHIVED)
        self.assertEqual(self.staff.get(BASE).data['count'], 0)
        rows = self.staff.get(BASE, {'status': 'archived'}).data['results']
        self.assertEqual(len(rows), 1)

    def test_only_user_side_returns_published(self):
        self._make_row(name='线上助手', status=AgentStatus.PUBLISHED, visibility='public')
        self._make_row(name='草稿助手', status=AgentStatus.DRAFT)
        rows = self.worker_client.get(BASE, {'only_user_side': '1'}).data['results']
        self.assertEqual([r['name'] for r in rows], ['线上助手'])

    def test_summary_counts_and_empty_guide(self):
        data = self.staff.get(BASE + 'summary/').data
        self.assertTrue(data['empty'])
        self.assertIn('内置智能体', data['guide'] + '')
        self._make_row(name='助手甲')
        data = self.staff.get(BASE + 'summary/').data
        self.assertFalse(data['empty'])
        self.assertEqual(data['draft'], 1)
        self.assertEqual(data['total'], 1)

    def test_delete_draft_keeps_audit_trail(self):
        agent = self._make_row(name='待删助手')
        AuditLog.objects.create(actor=self.admin, action='agent_create',
                                target=f'智能体:{agent.name}')
        resp = self.staff.delete(BASE + str(agent.pk) + '/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(AgentProfile.objects.filter(pk=agent.pk).exists())
        self.assertTrue(AuditLog.objects.filter(target=f'智能体:{agent.name}').exists())

    def test_delete_published_is_blocked_until_archived(self):
        agent = self._make_row(name='线上助手', status=AgentStatus.PUBLISHED)
        resp = self.staff.delete(BASE + str(agent.pk) + '/')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'agent_not_archived')
        self.assertEqual(self.staff.post(f'{BASE}{agent.pk}/archive/').status_code, 200)
        self.assertEqual(self.staff.delete(BASE + str(agent.pk) + '/').status_code, 204)

    def test_builtin_agent_cannot_be_deleted(self):
        agent = self._make_row(name='内置通用助手', is_builtin=True)
        resp = self.staff.delete(BASE + str(agent.pk) + '/')
        self.assertEqual(resp.status_code, 400)

    def test_normal_user_cannot_edit_admin_agent(self):
        agent = self._make_row(name='他人助手', visibility='public',
                               status=AgentStatus.PUBLISHED)
        resp = self.worker_client.patch(BASE + str(agent.pk) + '/', {'description': '改一下'})
        self.assertEqual(resp.status_code, 403)
        self.assertIn('复制新建', resp.data['detail'])

    def test_clone_makes_private_draft_with_mounts(self):
        server = _server()
        skill = Skill.objects.create(name='工伤认定规范', status=SkillStatus.ACTIVE,
                                     approved_body='按目录认定')
        agent = self._make_row(name='线上助手', visibility='public',
                               status=AgentStatus.PUBLISHED)
        AgentMCPServer.objects.create(agent=agent, server=server,
                                      tool_names=['list_tickets'])
        AgentSkill.objects.create(agent=agent, skill=skill)
        resp = self.worker_client.post(f'{BASE}{agent.pk}/clone/', {})
        self.assertEqual(resp.status_code, 201, resp.content)
        copy = resp.data
        self.assertEqual(copy['status'], 'draft')
        self.assertEqual(copy['visibility'], 'private')
        self.assertEqual(copy['mount_summary']['mcp_servers'], 1)
        self.assertEqual(copy['mount_summary']['skills'], 1)
        self.assertEqual(copy['created_by_name'], 'worker')
        self.assertNotEqual(copy['slug'], agent.slug)
        self.assertEqual(
            [m['tool_names'] for m in copy['mcp_mounts']], [['list_tickets']])

    def test_clone_name_conflict_reported(self):
        agent = self._make_row(name='线上助手', status=AgentStatus.PUBLISHED,
                               visibility='public')
        self._make_row(name='线上助手 副本')
        resp = self.worker_client.post(f'{BASE}{agent.pk}/clone/', {})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('已被占用', resp.data['detail'])


# ─── A2 基本字段 ───

class BasicFieldsA2Tests(AgentTestCase):
    """A2：名称唯一与占用方、描述长度、slug 不可改、岗位标签"""

    def test_name_too_short_and_too_long(self):
        resp = self.staff.post(BASE, _payload(name='甲'))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('至少 2 个字', resp.data['detail'])
        resp = self.staff.post(BASE, _payload(name='名' * 31))
        self.assertEqual(resp.status_code, 400)

    def test_duplicate_name_tells_occupant(self):
        self.worker_client.post(BASE, _payload(name='工单问答助手'))
        resp = self.staff.post(BASE, _payload(name='工单问答助手'))
        self.assertEqual(resp.status_code, 400)
        message = resp.data['detail']
        self.assertIn('worker', message)
        self.assertIn('草稿', message)

    def test_description_over_200_rejected(self):
        resp = self.staff.post(BASE, _payload(description='说' * 201))
        self.assertEqual(resp.status_code, 400)

    def test_slug_autogenerated_and_immutable(self):
        created = self._make(name='Device Helper')
        self.assertEqual(created['slug'], 'device-helper')
        resp = self.staff.put(BASE + str(created['id']) + '/',
                              _payload(name='Device Helper', slug='hacked'))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(AgentProfile.objects.get(pk=created['id']).slug, 'device-helper')

    def test_job_tags_multi_select(self):
        created = self._make(job_tags=['IT 支持', '行政'])
        self.assertEqual(created['job_tags'], ['IT 支持', '行政'])

    def test_draft_can_be_created_without_prompt_and_model(self):
        resp = self.worker_client.post(BASE, {'name': '先建后配', 'permission_tier': 'whitelist'})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data['status'], 'draft')
        self.assertEqual(resp.data['prompt'], '')


# ─── A3 模型与三档预设 ───

class ModelAndTiersA3Tests(AgentTestCase):
    """A3：模型必须来自已验证列表、档位只能选预设、空提示词不静默"""

    def test_model_not_in_verified_list_rejected(self):
        resp = self.staff.post(BASE, _payload(model_name='gpt-9'))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('已验证可用列表', resp.data['detail'])
        self.assertIn('qwen-max', resp.data['detail'])

    def test_no_verified_model_configured_says_so(self):
        SystemSetting.set_value('llm_verified_models', '')
        SystemSetting.set_value('llm_model', '')
        resp = self.staff.post(BASE, _payload(model_name='qwen-max'))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('系统设置', resp.data['detail'])

    def test_tiers_cannot_be_free_numbers(self):
        for field, bad in (('speech_tier', '0.7'), ('retrieval_tier', '12')):
            resp = self.staff.post(BASE, _payload(**{field: bad}))
            self.assertEqual(resp.status_code, 400, resp.content)
            self.assertIn('三档预设', resp.data['detail'])

    def test_options_exposes_three_tiers_and_placeholders(self):
        data = self.staff.get(BASE + 'options/').data
        self.assertEqual(len(data['speech_tiers']), 3)
        self.assertEqual(len(data['retrieval_tiers']), 3)
        self.assertEqual([t['max_rounds'] for t in data['retrieval_tiers']], [1, 3, 8])
        self.assertIn('qwen-max', data['models'])
        tokens = [p['token'] for p in data['placeholders']]
        self.assertEqual(tokens, ['{current_date}', '{user_name}', '{department}'])
        ask = [t for t in data['permission_tiers'] if t['value'] == 'ask'][0]
        self.assertTrue(ask['selectable'])
        self.assertIn('60 秒', ask['desc'])
        admin_only = [v for v in data['visibilities'] if v['value'] == 'admin_only'][0]
        self.assertTrue(admin_only['selectable'])
        self.assertIn('业务用户不可见', admin_only['desc'])

    def test_enable_thinking_switch_and_presets(self):
        created = self._make(enable_thinking=True, speech_tier='low', retrieval_tier='many')
        self.assertTrue(created['enable_thinking'])
        self.assertEqual(created['speech_params'], {'temperature': 0.1, 'top_p': 0.8})
        self.assertEqual(created['max_tool_rounds'], 8)

    def test_prompt_placeholders_accepted_in_body(self):
        created = self._make(prompt='今天是{current_date}，服务对象{user_name}（{department}）。')
        self.assertIn('{current_date}', created['prompt'])


# ─── E3 检索调用策略 ───

class KnowledgePolicyE3Tests(AgentTestCase):
    """E3：按需查/每轮必查与追问补全，随版本快照固化"""

    def test_defaults_are_on_demand_and_rewrite_on(self):
        created = self._make()
        self.assertEqual(created['knowledge_mode'], 'on_demand')
        self.assertEqual(created['knowledge_mode_display'], '按需查（智能调用）')
        self.assertTrue(created['followup_rewrite'])

    def test_unknown_mode_rejected(self):
        resp = self.staff.post(BASE, _payload(knowledge_mode='sometimes'))
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn('检索调用策略', resp.data['detail'])

    def test_update_policy_persists_and_enters_snapshot(self):
        created = self._make()
        resp = self.staff.patch(
            f"{BASE}{created['id']}/",
            {'knowledge_mode': 'always', 'followup_rewrite': False},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['knowledge_mode'], 'always')
        self.assertFalse(resp.data['followup_rewrite'])

        agent = AgentProfile.objects.get(pk=created['id'])
        snap = agent.snapshot()
        self.assertEqual(snap['knowledge_mode'], 'always')
        self.assertFalse(snap['followup_rewrite'])

    def test_old_snapshot_without_policy_uses_safe_default(self):
        from backend.apps.agenthub.services import runtime_config
        agent = self._make_row()
        cfg = runtime_config(agent, None)
        self.assertEqual(cfg['knowledge_mode'], 'on_demand')
        self.assertTrue(cfg['followup_rewrite'])


# ─── A3 试聊装配路径 ───

class TrialSnapshotA3Tests(AgentTestCase):
    """A3：试聊固定按最新保存的草稿配置装配，不受线上快照影响"""

    def _edit_draft(self, agent, **fields):
        AgentProfile.objects.filter(pk=agent.pk).update(**fields)
        agent.refresh_from_db()

    def test_default_takes_online_snapshot(self):
        agent = self._make_row(prompt='线上口径：只答已知信息',
                               status=AgentStatus.PUBLISHED)
        resp = self._publish(agent)
        self.assertEqual(resp.status_code, 201, resp.content)  # 发布接口固定 201
        agent.refresh_from_db()
        self._edit_draft(agent, prompt='草稿新口径：可以给出建议')
        cfg, _obj, _notes = resolve_snapshot(agent.pk, self.admin)
        self.assertEqual(cfg['system_prompt'], '线上口径：只答已知信息')
        self.assertEqual(cfg['snapshot_version'], agent.current_version)

    def test_draft_flag_takes_latest_saved_draft(self):
        agent = self._make_row(prompt='线上口径：只答已知信息',
                               status=AgentStatus.PUBLISHED)
        self.assertEqual(self._publish(agent).status_code, 201)
        self._edit_draft(agent, prompt='草稿新口径：可以给出建议')
        cfg, _obj, notes = resolve_snapshot(agent.pk, self.admin, draft=True)
        self.assertEqual(cfg['system_prompt'], '草稿新口径：可以给出建议')
        self.assertIsNone(cfg['snapshot_version'])
        self.assertEqual(notes, [])

    def test_draft_flag_on_unpublished_agent(self):
        """从未发布的草稿也能试聊（这正是 A3 的主场景）"""
        agent = self._make_row(prompt='草稿口径', status=AgentStatus.DRAFT)
        cfg, _obj, notes = resolve_snapshot(agent.pk, self.admin, draft=True)
        self.assertEqual(cfg['system_prompt'], '草稿口径')
        self.assertEqual(notes, [])

    def test_draft_flag_still_respects_visibility(self):
        """试聊不越权：他人私有的草稿取不到快照"""
        agent = self._make_row(name='他人私有', visibility='private',
                               created_by=self.worker, updated_by=self.worker)
        cfg, _obj, notes = resolve_snapshot(agent.pk, self.outsider, draft=True)
        self.assertIsNone(cfg)
        self.assertTrue(notes)


# ─── A4 能力挂载 ───

class MountsA4Tests(AgentTestCase):
    """A4：四分区可为空、逐个勾选工具、同名冲突标全称、未启用技能灰置"""

    def test_all_sections_may_be_empty(self):
        created = self._make(mcp_mounts=[], skill_ids=[], knowledge_base_ids=[],
                             builtin_capabilities=[])
        self.assertEqual(created['mount_summary'],
                         {'mcp_servers': 0, 'skills': 0, 'knowledge_bases': 0})
        self.assertEqual(created['builtin_capabilities'], [])

    def test_mount_selected_tools_only(self):
        server = _server()
        created = self._make(mcp_mounts=[{'server': server.pk,
                                         'tool_names': ['ticket_detail']}])
        mount = created['mcp_mounts'][0]
        self.assertEqual(mount['tool_names'], ['ticket_detail'])
        self.assertEqual(mount['tools'][0]['full_name'], 'mcp__ticket__ticket_detail')
        self.assertEqual(mount['tools'][0]['display_name'], '工单系统·ticket_detail')

    def test_unknown_tool_and_disabled_tool_are_reported(self):
        server = _server()
        resp = self.staff.post(BASE, _payload(mcp_mounts=[
            {'server': server.pk, 'tool_names': ['no_such_tool']}]))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('不存在这些工具', resp.data['detail'])

        MCPTool.objects.filter(server=server, name='list_tickets').update(enabled=False)
        resp = self.staff.post(BASE, _payload(name='另一助手', mcp_mounts=[
            {'server': server.pk, 'tool_names': ['list_tickets']}]))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('已关闭', resp.data['detail'])

    def test_disabled_and_egress_blocked_server_cannot_be_mounted(self):
        off = _server(name='停用来源', slug='off', status='disabled', is_active=False)
        resp = self.staff.post(BASE, _payload(mcp_mounts=[
            {'server': off.pk, 'tool_names': ['list_tickets']}]))
        self.assertEqual(resp.status_code, 400)
        blocked = _server(name='出网来源', slug='out', status='egress_blocked')
        resp = self.staff.post(BASE, _payload(name='另一助手', mcp_mounts=[
            {'server': blocked.pk, 'tool_names': ['list_tickets']}]))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('出网', resp.data['detail'])

    def test_unverified_server_can_be_mounted_with_yellow_flag(self):
        server = _server(name='新接来源', slug='newer', status='unknown')
        created = self._make(mcp_mounts=[{'server': server.pk,
                                         'tool_names': ['list_tickets']}])
        self.assertEqual(created['mcp_mounts'][0]['server_status'], 'unknown')
        self.assertTrue(created['mcp_mounts'][0]['warning'])
        check = self.staff.get(f'{BASE}{created["id"]}/publish-check/').data
        self.assertEqual(check['status'], 'ack_required')
        self.assertTrue(check['can_publish'])

    def test_same_tool_name_from_two_servers_keeps_full_names(self):
        a = _server(name='工单系统', slug='ticket', tools=('get_status',))
        b = _server(name='设备系统', slug='device', tools=('get_status',))
        created = self._make(
            mcp_mounts=[{'server': a.pk, 'tool_names': ['get_status']},
                        {'server': b.pk, 'tool_names': ['get_status']}])
        conflicts = created['tool_conflicts']
        self.assertEqual(conflicts[0]['tool'], 'get_status')
        self.assertEqual(sorted(conflicts[0]['owners']), ['工单系统·get_status', '设备系统·get_status'])

    def test_inactive_skill_not_selectable_with_reason(self):
        active = Skill.objects.create(name='生效规范', status=SkillStatus.ACTIVE,
                                      approved_body='正文')
        dead = Skill.objects.create(name='停用规范', status=SkillStatus.DISABLED)
        resp = self.staff.post(BASE, _payload(skill_ids=[dead.pk]))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('已停用', resp.data['detail'])
        created = self._make(skill_ids=[active.pk])
        self.assertEqual(created['mount_summary']['skills'], 1)
        options = self.staff.get(f'{BASE}{created["id"]}/mount-options/').data
        flags = {s['name']: s for s in options['skills']}
        self.assertTrue(flags['生效规范']['selectable'])
        self.assertFalse(flags['停用规范']['selectable'])
        self.assertTrue(flags['停用规范']['reason'])

    def test_skill_needs_pending_without_approved_body(self):
        pending = Skill.objects.create(name='待审核规范', status=SkillStatus.PENDING)
        resp = self.staff.post(BASE, _payload(skill_ids=[pending.pk]))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('待审核', resp.data['detail'])

    def test_builtin_capabilities_validated(self):
        created = self._make(builtin_capabilities=['generate_file', 'knowledge_search'])
        self.assertEqual(created['builtin_capabilities'],
                         ['generate_file', 'knowledge_search'])
        resp = self.staff.post(BASE, _payload(name='另一助手', builtin_capabilities=['ssh']))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('不支持的内置能力', resp.data['detail'])

    def test_unmount_takes_effect_immediately(self):
        server = _server()
        created = self._make(mcp_mounts=[{'server': server.pk,
                                         'tool_names': ['list_tickets']}])
        resp = self.staff.patch(BASE + str(created['id']) + '/', {'mcp_mounts': []})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['mcp_mounts'], [])
        self.assertEqual(resp.data['allow_tool_names'], [])
        self.assertTrue(AuditLog.objects.filter(action='agent_mount').exists())

    def test_knowledge_bases_existence_checked(self):
        kb = KnowledgeBase.objects.create(name='制度汇编', document_count=3)
        created = self._make(knowledge_base_ids=[kb.pk])
        self.assertEqual(created['knowledge_bases'][0]['name'], '制度汇编')
        resp = self.staff.patch(BASE + str(created['id']) + '/',
                                {'knowledge_base_ids': [99999]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('知识库不存在', str(resp.data))

    def test_mount_options_tools_selectable_flags(self):
        server = _server()
        MCPTool.objects.filter(server=server, name='ticket_detail').update(state='missing')
        agent = self._make_row(name='选项助手')
        options = self.staff.get(f'{BASE}{agent.pk}/mount-options/').data
        row = options['mcp_servers'][0]
        self.assertEqual(row['slug'], 'ticket')
        self.assertTrue(row['verified'])
        tools = {t['name']: t for t in row['tools']}
        self.assertTrue(tools['list_tickets']['selectable'])
        self.assertNotIn('ticket_detail', tools)  # 失效工具不进候选


# ─── A5 授权档位 ───

class PermissionA5Tests(AgentTestCase):
    """A5/D7：档位必须显式选定、ask 可写入且发布只提示 60 秒超时"""

    def test_ask_tier_accepted_on_write(self):
        resp = self.staff.post(BASE, _payload(permission_tier='ask'))
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data['permission_tier'], 'ask')

    def test_empty_tier_rejected(self):
        resp = self.staff.post(BASE, _payload(permission_tier=''))
        self.assertEqual(resp.status_code, 400)

    def test_ask_row_publish_only_warns_timeout(self):
        agent = self._make_row(name='逐次询问助手', permission_tier=PermissionTier.ASK)
        check = self.staff.get(f'{BASE}{agent.pk}/publish-check/').data
        codes = [i['code'] for i in check['blocking']]
        self.assertNotIn('permission_ask_unsupported', codes)
        self.assertIn('permission_ask_timeout', [i['code'] for i in check['warnings']])
        resp = self._publish(agent, risk_ack=True)
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data['version']['version'], 1)

    def test_whitelist_syncs_full_names(self):
        server = _server()
        created = self._make(permission_tier='whitelist',
                            mcp_mounts=[{'server': server.pk,
                                         'tool_names': ['list_tickets', 'ticket_detail']}])
        self.assertEqual(created['allow_tool_names'],
                         ['mcp__ticket__list_tickets', 'mcp__ticket__ticket_detail'])

    def test_allow_list_cannot_exceed_mounts(self):
        resp = self.staff.post(BASE, _payload(
            mcp_mounts=[], allow_tool_names=['mcp__ticket__list_tickets']))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('未挂载', resp.data['detail'])

    def test_auto_tier_needs_no_whitelist(self):
        server = _server()
        created = self._make(permission_tier='auto', mcp_mounts=[
            {'server': server.pk, 'tool_names': ['list_tickets']}])
        self.assertEqual(created['permission_tier'], 'auto')
        check = self.staff.get(f'{BASE}{created["id"]}/publish-check/').data
        self.assertEqual(check['status'], 'pass')


# ─── A6 可见范围 ───

class VisibilityA6Tests(AgentTestCase):
    """A6：private/department/public、含子部门、admin_only（仅管理端）、停用即移出用户侧"""

    def test_department_requires_at_least_one(self):
        resp = self.staff.post(BASE, _payload(visibility='department',
                                              allowed_department_ids=[]))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('至少选择一个部门', resp.data['detail'])

    def test_non_staff_cannot_create_admin_only(self):
        resp = self.worker_client.post(BASE, _payload(visibility='admin_only'))
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn('只有平台管理员', resp.data['detail'])

    def test_non_staff_admin_only_option_not_selectable(self):
        data = self.worker_client.get(BASE + 'options/').data
        admin_only = [v for v in data['visibilities'] if v['value'] == 'admin_only'][0]
        self.assertFalse(admin_only['selectable'])
        self.assertIn('管理员', admin_only['reason'])

    def test_staff_can_create_publish_edit_admin_only(self):
        created = self._make(visibility='admin_only')
        agent = AgentProfile.objects.get(pk=created['id'])
        self.assertEqual(agent.visibility, 'admin_only')
        self.assertEqual(self._publish(agent).status_code, 201)
        agent.refresh_from_db()
        self.assertEqual(agent.status, AgentStatus.PUBLISHED)
        resp = self.staff.patch(BASE + str(agent.pk) + '/',
                                {'description': '管理端底座，业务用户不可见'})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['visibility'], 'admin_only')

    def test_admin_only_published_absent_from_business_user_side(self):
        """发布后不进业务用户列表/选择器，详情也 404；管理端仍可见"""
        created = self._make(visibility='admin_only')
        agent = AgentProfile.objects.get(pk=created['id'])
        self._publish(agent)
        for client in (self.worker_client, self.member_client, self.outsider_client):
            ids = [r['id'] for r in client.get(
                BASE, {'only_user_side': '1'}).data['results']]
            self.assertNotIn(agent.pk, ids)
            self.assertNotIn(agent.pk, [r['id'] for r in client.get(BASE).data['results']])
            self.assertEqual(client.get(BASE + str(agent.pk) + '/').status_code, 404)
        self.assertIn(agent.pk, [r['id'] for r in self.staff.get(BASE).data['results']])

    def test_admin_only_does_not_affect_department_and_public(self):
        dept = self._make_row(name='部门助手', visibility='department',
                              status=AgentStatus.PUBLISHED)
        dept.allowed_departments.set([self.rd])
        public = self._make_row(name='全员助手', visibility='public',
                                status=AgentStatus.PUBLISHED)
        admin_only = self._make_row(name='底座助手', visibility='admin_only',
                                    status=AgentStatus.PUBLISHED)
        worker_ids = [r['id'] for r in self.worker_client.get(
            BASE, {'only_user_side': '1'}).data['results']]
        self.assertIn(dept.pk, worker_ids)
        self.assertIn(public.pk, worker_ids)
        self.assertNotIn(admin_only.pk, worker_ids)

    def test_include_sub_departments_expands_to_child_dept(self):
        created = self._make(visibility='department',
                             allowed_department_ids=[self.rd.pk],
                             include_sub_departments=True)
        agent = AgentProfile.objects.get(pk=created['id'])
        agent.status = AgentStatus.PUBLISHED
        agent.save(update_fields=['status'])
        self.assertIn(agent.pk, [r['id'] for r in
                                 self.member_client.get(BASE).data['results']])
        self.assertNotIn(agent.pk, [r['id'] for r in
                                    self.outsider_client.get(BASE).data['results']])

    def test_without_sub_departments_child_cannot_see(self):
        created = self._make(visibility='department',
                             allowed_department_ids=[self.rd.pk],
                             include_sub_departments=False)
        agent = AgentProfile.objects.get(pk=created['id'])
        agent.status = AgentStatus.PUBLISHED
        agent.save(update_fields=['status'])
        self.assertIn(agent.pk, [r['id'] for r in
                                 self.worker_client.get(BASE).data['results']])
        self.assertNotIn(agent.pk, [r['id'] for r in
                                    self.member_client.get(BASE).data['results']])
        self.assertEqual(self.member_client.get(BASE + str(agent.pk) + '/').status_code, 404)

    def test_visibility_change_is_audited_and_immediate(self):
        created = self._make(visibility='private')
        resp = self.staff.patch(BASE + str(created['id']) + '/',
                                {'visibility': 'department',
                                 'allowed_department_ids': [self.rd.pk]})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['allowed_departments'], ['研发中心'])
        self.assertTrue(AuditLog.objects.filter(
            action='agent_visibility', target=f'智能体:{created["name"]}').exists())

    def test_disable_removes_from_user_side(self):
        created = self._make(visibility='public')
        agent = AgentProfile.objects.get(pk=created['id'])
        self._publish(agent)
        self.assertIn(agent.pk, [r['id'] for r in
                                 self.outsider_client.get(BASE, {'only_user_side': '1'}).data['results']])
        self.assertEqual(self.staff.post(f'{BASE}{agent.pk}/disable/').status_code, 200)
        self.assertEqual([r['id'] for r in self.outsider_client.get(
            BASE, {'only_user_side': '1'}).data['results']], [])
        self.assertTrue(AuditLog.objects.filter(action='agent_disable').exists())

    def test_status_flow_errors(self):
        agent = self._make_row(name='草稿流转')
        self.assertEqual(self.staff.post(f'{BASE}{agent.pk}/disable/').status_code, 400)
        self.assertEqual(self.staff.post(f'{BASE}{agent.pk}/enable/').status_code, 400)
        self.assertEqual(self.staff.post(f'{BASE}{agent.pk}/restore/').status_code, 400)
        self.assertEqual(self.staff.post(f'{BASE}{agent.pk}/archive/').status_code, 200)
        self.assertEqual(self.staff.post(f'{BASE}{agent.pk}/archive/').status_code, 400)
        restored = self.staff.post(f'{BASE}{agent.pk}/restore/')
        self.assertEqual(restored.data['status'], 'draft')


# ─── A7 发布前检查 ───

class PrepublishA7Tests(AgentTestCase):
    """A7：逐项清单，阻断/警示/提示三档，不一次性抛错"""

    def _codes(self, check, bucket):
        return [item['code'] for item in check[bucket]]

    def test_missing_fields_are_blocking(self):
        agent = self._make_row(name='缺件助手', description='', prompt='', model_name='')
        check = self.staff.get(f'{BASE}{agent.pk}/publish-check/').data
        codes = self._codes(check, 'blocking')
        self.assertIn('prompt_missing', codes)
        self.assertIn('description_missing', codes)
        self.assertIn('model_missing', codes)
        self.assertEqual(check['status'], 'blocked')
        self.assertFalse(check['can_publish'])
        for item in check['blocking']:
            self.assertIn(item['section'], ('basic', 'model'))
            self.assertTrue(item['section_title'])
            self.assertTrue(item['message'])

    def test_unavailable_model_blocking(self):
        agent = self._make_row(name='模型失效', model_name='qwen-max')
        SystemSetting.set_value('llm_verified_models', '["deepseek-r1"]')
        check = self.staff.get(f'{BASE}{agent.pk}/publish-check/').data
        self.assertIn('model_unavailable', self._codes(check, 'blocking'))
        self.assertIn('deepseek-r1', check['blocking'][0]['message'])

    def test_dangling_knowledge_base_blocking(self):
        agent = self._make_row(name='知识库丢失', knowledge_base_ids=[99999])
        check = self.staff.get(f'{BASE}{agent.pk}/publish-check/').data
        self.assertIn('kb_missing', self._codes(check, 'blocking'))

    def test_warnings_need_risk_ack_and_are_audited(self):
        server = _server(name='未验证来源', slug='unverified', status='unknown')
        created = self._make(mcp_mounts=[{'server': server.pk,
                                         'tool_names': ['list_tickets']}])
        agent = AgentProfile.objects.get(pk=created['id'])
        resp = self._publish(agent)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.data['code'], 'risk_ack_required')
        self.assertEqual(resp.data['ack_field'], 'risk_ack')
        self.assertFalse(AuditLog.objects.filter(action='agent_risk_ack').exists())

        resp = self._publish(agent, risk_ack=True)
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data['version']['version'], 1)
        audit = AuditLog.objects.filter(action='agent_risk_ack').first()
        self.assertIsNotNone(audit)
        self.assertIn('未验证来源', audit.detail)

    def test_error_server_is_warning_not_blocking(self):
        server = _server(name='异常来源', slug='broken', status='error',
                         last_error='连接被拒绝')
        created = self._make(mcp_mounts=[{'server': server.pk,
                                         'tool_names': ['list_tickets']}])
        check = self.staff.get(f'{BASE}{created["id"]}/publish-check/').data
        self.assertEqual(check['counts']['blocking'], 0)
        self.assertEqual(check['status'], 'ack_required')
        self.assertIn('连接被拒绝', check['warnings'][0]['message'])

    def test_notices_for_skill_prompt_kb_and_empty_whitelist(self):
        skill = Skill.objects.create(name='写作规范', status=SkillStatus.ACTIVE,
                                     approved_body='正文')
        empty_kb = KnowledgeBase.objects.create(name='空库', document_count=0)
        created = self._make(prompt='直接回答问题即可。', skill_ids=[skill.pk],
                             knowledge_base_ids=[empty_kb.pk], mcp_mounts=[])
        check = self.staff.get(f'{BASE}{created["id"]}/publish-check/').data
        codes = self._codes(check, 'notices')
        self.assertIn('skill_not_referenced', codes)
        self.assertIn('kb_all_empty', codes)
        self.assertIn('whitelist_empty', codes)
        self.assertEqual(check['status'], 'pass')
        self.assertTrue(check['can_publish'])

    def test_prompt_that_reads_skill_has_no_notice(self):
        skill = Skill.objects.create(name='写作规范', status=SkillStatus.ACTIVE,
                                     approved_body='正文')
        created = self._make(prompt='请先读技能规范，再按规范作答。', skill_ids=[skill.pk])
        check = self.staff.get(f'{BASE}{created["id"]}/publish-check/').data
        self.assertNotIn('skill_not_referenced', self._codes(check, 'notices'))

    def test_skill_requires_missing_notice(self):
        server = _server(slug='device', tools=('get_status',))
        skill = Skill.objects.create(name='巡检规范', status=SkillStatus.ACTIVE,
                                     approved_body='正文',
                                     requires=['mcp__ticket__list_tickets'])
        created = self._make(skill_ids=[skill.pk], mcp_mounts=[
            {'server': server.pk, 'tool_names': ['get_status']}])
        check = self.staff.get(f'{BASE}{created["id"]}/publish-check/').data
        notice = [i for i in check['notices'] if i['code'] == 'skill_requires_missing']
        self.assertTrue(notice)
        self.assertIn('巡检规范', notice[0]['message'])
        self.assertIn('mcp__ticket__list_tickets', notice[0]['message'])

    def test_server_without_selected_tools_is_notice(self):
        server = _server()
        created = self._make(mcp_mounts=[{'server': server.pk, 'tool_names': []}])
        check = self.staff.get(f'{BASE}{created["id"]}/publish-check/').data
        self.assertIn('server_no_tools', self._codes(check, 'notices'))

    def test_publish_blocked_does_not_create_version(self):
        agent = self._make_row(name='发不出去的助手', prompt='')
        resp = self._publish(agent)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.data['code'], 'prepublish_blocked')
        self.assertEqual(AgentVersion.objects.filter(agent=agent).count(), 0)
        agent.refresh_from_db()
        self.assertEqual(agent.status, 'draft')


# ─── A7 差异视图与发布 ───

class PublishDiffA7Tests(AgentTestCase):
    """A7：非首次发布先给"这次改了什么"，提示词只给摘要与字数增减"""

    def setUp(self):
        super().setUp()
        self.server = _server()
        self.created = self._make(
            mcp_mounts=[{'server': self.server.pk,
                         'tool_names': ['list_tickets', 'ticket_detail']}])
        self.agent = AgentProfile.objects.get(pk=self.created['id'])
        self.first = self._publish(self.agent)
        self.assertEqual(self.first.status_code, 201, self.first.content)

    def test_first_publish_diff_has_no_baseline(self):
        self.assertTrue(self.first.data['diff']['first_publish'])
        self.assertEqual(self.first.data['check']['base_version'], 0)
        self.assertEqual(self.first.data['version']['change_summary'], '首次发布')

    def test_second_publish_requires_diff_confirmation(self):
        resp = self.staff.patch(BASE + str(self.agent.pk) + '/',
                                {'prompt': LONG_PROMPT + '补充：涉及金额需二次确认。',
                                 'retrieval_tier': 'many'})
        self.assertEqual(resp.status_code, 200, resp.content)
        resp = self._publish(self.agent)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'diff_review_required')
        sections = {s['key'] for s in resp.data['diff']['sections']}
        self.assertIn('model', sections)

        resp = self._publish(self.agent, confirm_diff=True)
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data['version']['version'], 2)

    def test_prompt_diff_is_summary_not_full_text(self):
        long_new = LONG_PROMPT + '补充要求：' + '逐条列出工单状态与责任人，不得合并陈述。' * 3
        self.staff.patch(BASE + str(self.agent.pk) + '/', {'prompt': long_new})
        check = self.staff.get(f'{BASE}{self.agent.pk}/publish-check/').data
        item = [i for s in check['diff']['sections'] for i in s['items']
                if i['field'] == 'prompt'][0]
        self.assertEqual(item['kind'], 'text')
        self.assertLessEqual(len(item['new_summary']), services.PROMPT_SUMMARY_LEN + 1)
        self.assertEqual(item['delta'], len(long_new) - len(LONG_PROMPT))
        self.assertNotIn('prompt', item)
        self.assertNotIn(long_new, json.dumps(check, ensure_ascii=False))

    def test_tool_level_diff(self):
        other = _server(name='设备系统', slug='device', tools=('get_status',))
        self.staff.patch(BASE + str(self.agent.pk) + '/', {'mcp_mounts': [
            {'server': self.server.pk, 'tool_names': ['list_tickets']},
            {'server': other.pk, 'tool_names': ['get_status']}]})
        check = self.staff.get(f'{BASE}{self.agent.pk}/publish-check/').data
        rows = [i for s in check['diff']['sections'] for i in s['items']
                if i['field'] == 'mcp_servers']
        kinds = {r['kind'] for r in rows}
        self.assertIn('added', kinds)
        self.assertIn('tools', kinds)
        removed_tool = [r for r in rows if r['kind'] == 'tools'][0]
        self.assertEqual(removed_tool['removed'], ['ticket_detail'])
        self.assertEqual(removed_tool['added'], [])

    def test_no_substantial_change_still_publishable(self):
        check = self.staff.get(f'{BASE}{self.agent.pk}/publish-check/').data
        self.assertTrue(check['diff']['no_change'])
        self.assertEqual(check['diff']['summary'], '与线上版本无实质变化')
        resp = self._publish(self.agent)  # 无实质变化不需要确认差异
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data['version']['change_summary'], '与上一版无实质变化')

    def test_pending_changes_flag(self):
        self.assertFalse(self.staff.get(BASE + str(self.agent.pk) + '/').data['pending_changes'])
        self.staff.patch(BASE + str(self.agent.pk) + '/', {'speech_tier': 'high'})
        row = self.staff.get(BASE).data['results'][0]
        self.assertTrue(row['pending_changes'])
        self.assertEqual(row['current_version'], 1)

    def test_runtime_config_uses_published_snapshot(self):
        cfg = services.runtime_config(self.agent,
                                      AgentVersion.objects.filter(agent=self.agent).latest('version'))
        self.assertEqual(cfg['allow_tool_names'],
                         ['mcp__ticket__list_tickets', 'mcp__ticket__ticket_detail'])
        self.assertEqual(cfg['max_tool_rounds'], 3)
        self.assertEqual(cfg['snapshot_version'], 1)
        self.agent.prompt = '改过的草稿提示词'
        self.assertEqual(services.runtime_config(
            self.agent, AgentVersion.objects.get(agent=self.agent, version=1)
        )['system_prompt'], LONG_PROMPT)


# ─── A8 只读发布历史 ───

class VersionsA8Tests(AgentTestCase):
    """A8：版本对比（A8-1）与回滚重发布（A8-2）"""

    def setUp(self):
        super().setUp()
        self.agent = self._make_row(name='版本助手', visibility='public')
        self.agent_orm = self.agent
        self._publish(self.agent)
        self.staff.patch(BASE + str(self.agent.pk) + '/',
                         {'description': '改过用途', 'retrieval_tier': 'many'})
        self._publish(self.agent, confirm_diff=True)

    def _extra_version(self, number, **changes):
        snapshot = self.agent.snapshot()
        snapshot.update(changes)
        record = AgentVersion.objects.create(
            agent=self.agent, version=number, snapshot=snapshot, diff={},
            change_summary=f'手工构造 v{number}', published_by=self.admin)
        # 模拟该版本确实是线上 head，保证回滚时新版本号不冲突
        if number > self.agent.current_version:
            self.agent.current_version = number
            self.agent.save(update_fields=['current_version'])
        return record

    def test_history_lists_versions_readonly(self):
        resp = self.staff.get(f'{BASE}{self.agent.pk}/versions/')
        self.assertEqual(resp.status_code, 200)
        rows = resp.data['results']
        self.assertEqual([r['version'] for r in rows], [2, 1])
        self.assertEqual(rows[0]['published_by_name'], 'admin')
        self.assertIn('基本信息', rows[0]['change_summary'])
        self.assertIn('→', rows[0]['change_summary'])
        self.assertNotIn('snapshot', rows[0])

    def test_versions_list_rejects_write(self):
        self.assertEqual(self.staff.post(f'{BASE}{self.agent.pk}/versions/', {}).status_code,
                         405)
        self.assertEqual(self.staff.delete(f'{BASE}{self.agent.pk}/versions/').status_code, 405)

    def test_version_detail_returns_snapshot(self):
        resp = self.staff.get(f'{BASE}{self.agent.pk}/versions/1/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['snapshot']['description'], '回答工单系统的常见问题')
        self.assertEqual(resp.data['snapshot']['retrieval_tier'], 'standard')
        self.assertEqual(resp.data['version'], 1)
        self.assertTrue(AuditLog.objects.filter(action='agent_version_diff').exists())

    def test_unknown_version_returns_404(self):
        self.assertEqual(self.staff.get(f'{BASE}{self.agent.pk}/versions/9/').status_code, 404)
        self.assertEqual(self.staff.post(
            f'{BASE}{self.agent.pk}/versions/9/rollback/', {}).status_code, 404)
        self.assertEqual(self.staff.get(
            f'{BASE}{self.agent.pk}/versions/compare/?left=1&right=9').status_code, 404)

    def test_diff_of_stored_versions(self):
        """A8 历史里落的是分区差异（形状与 A7 视图不同，按 {分区: {字段: {old,new}}}）"""
        record = AgentVersion.objects.get(agent=self.agent, version=2)
        self.assertIn('basic', record.diff)
        self.assertEqual(record.diff['basic']['description']['old'],
                         '回答工单系统的常见问题')
        self.assertIn('model', record.diff)

    # ─── A8-1 对比 ───

    def test_compare_normalizes_order_and_returns_section_diff(self):
        """任选两版本：反向传入也按旧→新归一化，逐分区给出 old/new"""
        resp = self.staff.get(
            f'{BASE}{self.agent.pk}/versions/compare/?left=2&right=1')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['base_version'], 1)
        self.assertEqual(resp.data['target_version'], 2)
        sections = {s['key']: s for s in resp.data['sections']}
        self.assertIn('basic', sections)
        self.assertIn('model', sections)
        desc = [i for i in sections['basic']['items'] if i['field'] == 'description'][0]
        self.assertEqual(desc['kind'], 'value')
        self.assertEqual(desc['old'], '回答工单系统的常见问题')
        self.assertEqual(desc['new'], '改过用途')
        tier = [i for i in sections['model']['items'] if i['field'] == 'retrieval_tier'][0]
        self.assertEqual((tier['old'], tier['new']),
                         ('允许少量补查', '允许连续多次查询后汇总'))
        self.assertIn('基本信息', resp.data['summary'])

    def test_compare_rejects_same_or_invalid_version(self):
        self.assertEqual(self.staff.get(
            f'{BASE}{self.agent.pk}/versions/compare/?left=1&right=1').status_code, 400)
        self.assertEqual(self.staff.get(
            f'{BASE}{self.agent.pk}/versions/compare/?left=a&right=1').status_code, 400)
        self.assertEqual(self.staff.get(
            f'{BASE}{self.agent.pk}/versions/compare/?left=1').status_code, 400)

    def test_compare_counts_list_changes(self):
        """列表项数量变化也要显示，不能只比字符串"""
        self._extra_version(3, skills=['briefing', 'triage'],
                            job_tags=['工单', 'SLA', '路由'])
        resp = self.staff.get(
            f'{BASE}{self.agent.pk}/versions/compare/?left=2&right=3')
        self.assertEqual(resp.status_code, 200, resp.content)
        sections = {s['key']: s for s in resp.data['sections']}
        skills = [i for i in sections['skills']['items'] if i['field'] == 'skills'][0]
        self.assertEqual(skills['added'], ['briefing', 'triage'])
        self.assertIn('新增 2 项、移除 0 项', skills['detail'])
        tags = [i for i in sections['basic']['items'] if i['field'] == 'job_tags'][0]
        self.assertIn('新增 3', tags['detail'])

    def test_compare_marks_unrecoverable_references(self):
        """部门名/来源/技能/知识库被删后，对比必须显式标出，不能静默"""
        self._extra_version(
            3,
            mcp_servers=[{'server': 'gone', 'server_name': '旧工单来源',
                          'tool_names': ['list_tickets']}],
            skills=['gone-skill'],
            knowledge_base_ids=[9999],
            allowed_departments=['已撤销部门'],
        )
        resp = self.staff.get(
            f'{BASE}{self.agent.pk}/versions/compare/?left=2&right=3')
        self.assertEqual(resp.status_code, 200, resp.content)
        codes = {d['code'] for d in resp.data['dependencies']}
        self.assertIn('ref_server_missing', codes)
        self.assertIn('ref_skill_missing', codes)
        self.assertIn('ref_kb_missing', codes)
        self.assertIn('ref_department_missing', codes)
        self.assertGreaterEqual(resp.data['unavailable_count'], 3)
        self.assertGreaterEqual(resp.data['warning_count'], 1)
        self.assertEqual(
            {d['source'] for d in resp.data['dependencies']
             if d['code'] == 'ref_department_missing'},
            {'snapshot'})

    def test_compare_writes_audit_without_prompt_body(self):
        self.staff.get(f'{BASE}{self.agent.pk}/versions/compare/?left=1&right=2')
        log = AuditLog.objects.filter(action='agent_version_diff').latest('id')
        self.assertIn('对比 v1 ↔ v2', log.detail)
        self.assertIn('基本信息', log.detail)
        self.assertNotIn(LONG_PROMPT, log.detail)
        self.assertLess(len(log.detail), 200)

    # ─── A8-2 回滚 ───

    def test_rollback_creates_new_version_and_keeps_history(self):
        resp = self.staff.post(
            f'{BASE}{self.agent.pk}/versions/1/rollback/', {})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data['source_version'], 1)
        self.assertEqual(resp.data['version']['version'], 3)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.current_version, 3)
        self.assertEqual(self.agent.description, '回答工单系统的常见问题')
        self.assertEqual(self.agent.retrieval_tier, 'standard')
        versions = list(self.agent.versions.order_by('version')
                        .values_list('version', flat=True))
        self.assertEqual(versions, [1, 2, 3])
        self.assertEqual(
            AgentVersion.objects.get(agent=self.agent, version=1).snapshot['description'],
            '回答工单系统的常见问题')
        self.assertEqual(
            AgentVersion.objects.get(agent=self.agent, version=2).snapshot['description'],
            '改过用途')
        log = AuditLog.objects.filter(action='agent_rollback').latest('id')
        self.assertIn('v1', log.detail)
        self.assertIn('v3', log.detail)

    def test_rollback_blocked_when_references_missing(self):
        self._extra_version(
            3,
            mcp_servers=[{'server': 'gone', 'server_name': '旧来源',
                          'tool_names': ['list_tickets']}],
            skills=['gone-skill'],
        )
        resp = self.staff.post(
            f'{BASE}{self.agent.pk}/versions/3/rollback/', {})
        self.assertEqual(resp.status_code, 422, resp.content)
        self.assertEqual(resp.data['code'], 'rollback_blocked')
        codes = {b['code'] for b in resp.data['blocking']}
        self.assertEqual(codes, {'ref_server_missing', 'ref_skill_missing'})
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.current_version, 3)
        self.assertEqual(self.agent.versions.count(), 3)
        self.assertEqual(self.agent.agentmcpserver_set.count(), 0)

    def test_rollback_warns_and_requires_ack_for_disabled_source(self):
        _server(status='disabled')
        self._extra_version(
            3,
            mcp_servers=[{'server': 'ticket', 'server_name': '工单系统',
                          'tool_names': ['list_tickets']}],
        )
        resp = self.staff.post(
            f'{BASE}{self.agent.pk}/versions/3/rollback/', {})
        self.assertEqual(resp.status_code, 422, resp.content)
        self.assertEqual(resp.data['code'], 'risk_ack_required')
        self.assertEqual(resp.data['ack_field'], 'risk_ack')
        self.assertIn('ref_server_disabled',
                      {w['code'] for w in resp.data['warnings']})

        resp = self.staff.post(f'{BASE}{self.agent.pk}/versions/3/rollback/',
                               {'risk_ack': True})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data['version']['version'], 4)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.agentmcpserver_set.count(), 1)
        mount = self.agent.agentmcpserver_set.first()
        self.assertEqual(mount.server.slug, 'ticket')
        self.assertEqual(mount.tool_names, ['list_tickets'])
        self.assertTrue(AuditLog.objects.filter(action='agent_risk_ack').exists())

    def test_rollback_denied_for_non_editor(self):
        resp = self.worker_client.post(
            f'{BASE}{self.agent.pk}/versions/1/rollback/', {})
        self.assertEqual(resp.status_code, 403)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.current_version, 2)

    def test_inflight_conversation_keeps_opening_snapshot(self):
        """7.3：进行中会话钉在开场版本，回滚只影响之后的会话"""
        from backend.apps.chat.models import Conversation

        conv = Conversation.objects.create(
            user=self.worker, agent=self.agent, agent_version=2)
        resp = self.staff.post(
            f'{BASE}{self.agent.pk}/versions/1/rollback/', {})
        self.assertEqual(resp.status_code, 201, resp.content)

        pinned, _, _ = resolve_snapshot(
            self.agent.pk, self.worker, agent_version=conv.agent_version)
        self.assertEqual(pinned['snapshot_version'], 2)
        self.assertEqual(pinned['retrieval_tier'], 'many')

        latest, _, _ = resolve_snapshot(self.agent.pk, self.worker)
        self.assertEqual(latest['snapshot_version'], 3)
        self.assertEqual(latest['retrieval_tier'], 'standard')


# ─── 服务层口径单测（唯一键与档位换算） ───

class ServicesContractTests(AgentTestCase):
    """7.1 全平台工具唯一标识口径 + A3 档位取值"""

    def test_full_name_and_display_name_convention(self):
        self.assertEqual(services.tool_full_name('ticket', 'list_tickets'),
                         'mcp__ticket__list_tickets')
        self.assertEqual(services.tool_display_name('工单系统', 'list_tickets'),
                         '工单系统·list_tickets')

    def test_whitelist_rules_use_full_names(self):
        server = _server()
        created = self._make(mcp_mounts=[{'server': server.pk,
                                         'tool_names': ['list_tickets']}])
        agent = AgentProfile.objects.get(pk=created['id'])
        self.assertEqual(set(services.mounted_full_names(agent)),
                         set(agent.allow_tool_names))
        tool = MCPTool.objects.get(server=server, name='list_tickets')
        self.assertEqual(tool.full_name, 'mcp__ticket__list_tickets')

    def test_scoped_department_ids_expansion(self):
        created = self._make(visibility='department',
                             allowed_department_ids=[self.rd.pk],
                             include_sub_departments=True)
        agent = AgentProfile.objects.get(pk=created['id'])
        self.assertEqual(services.scoped_department_ids(agent),
                         {self.rd.pk, self.rd1.pk})
        self.assertEqual(services.user_department_id(self.member), self.rd1.pk)
        self.assertEqual(set(services.related_department_ids(self.member)),
                         {self.hq.pk, self.rd.pk, self.rd1.pk})

    def test_occupant_of_name_excludes_self(self):
        agent = self._make_row(name='占用测试')
        self.assertIsNone(services.occupant_of_name('占用测试', agent.pk))
        self.assertEqual(services.occupant_of_name('占用测试').pk, agent.pk)

    def test_model_available_falls_back_to_current_model(self):
        SystemSetting.set_value('llm_verified_models', '')
        SystemSetting.set_value('llm_model', 'deepseek-r1')
        self.assertEqual(services.verified_models(), ['deepseek-r1'])
        self.assertTrue(services.model_available('DeepSeek-R1'.lower()))

    def test_verified_models_ignores_env_when_db_empty(self):
        """数据库没有主模型时返回空列表，不读取环境变量"""
        SystemSetting.set_value('llm_verified_models', '')
        SystemSetting.set_value('llm_model', '')
        with patch.dict('os.environ', {'LLM_MODEL': 'env-model'}):
            self.assertEqual(services.verified_models(), [])


# ─── 4.2 出厂内置智能体 ───

class BuiltinAgents42Tests(AgentTestCase):
    """内置智能体：出厂即可用（自动发布）、不可删除、改动置「已自定义」、可恢复出厂"""

    def setUp(self):
        super().setUp()
        SystemSetting.set_value('llm_model', 'qwen-max')

    def _generic(self):
        return AgentProfile.objects.get(name='通用问答')

    def test_register_creates_published_agents_usable_out_of_box(self):
        call_command('init_builtin_agents')
        generic, kb = self._generic(), AgentProfile.objects.get(name='企业知识助手')
        for agent in (generic, kb):
            self.assertTrue(agent.is_builtin)
            self.assertFalse(agent.is_customized)
            self.assertEqual(agent.status, AgentStatus.PUBLISHED)
            self.assertEqual(agent.visibility, 'public')
            self.assertEqual(agent.current_version, 1)
        self.assertEqual(generic.model_name, 'qwen-max')
        self.assertEqual(kb.builtin_capabilities, ['knowledge_search'])

    def test_register_is_idempotent(self):
        call_command('init_builtin_agents')
        call_command('init_builtin_agents')
        self.assertEqual(AgentProfile.objects.filter(is_builtin=True).count(),
                         len(builtin_agents.BUILTIN_AGENTS))
        self.assertEqual(self._generic().current_version, 1)

    def test_check_reports_drift_without_writing(self):
        call_command('init_builtin_agents')
        agent = self._generic()
        agent.prompt = '现场改过的提示词'
        agent.save(update_fields=['prompt'])
        call_command('init_builtin_agents', check=True)
        agent.refresh_from_db()
        self.assertEqual(agent.prompt, '现场改过的提示词')
        self.assertFalse(agent.is_customized)

    def test_edit_marks_customized_and_does_not_revert(self):
        call_command('init_builtin_agents')
        agent = self._generic()
        resp = self.staff.patch(f'{BASE}{agent.pk}/', {'description': '只答财务问题'})
        self.assertEqual(resp.status_code, 200, resp.content)
        agent.refresh_from_db()
        self.assertTrue(agent.is_customized)
        self.assertEqual(agent.description, '只答财务问题')

    def test_customized_flag_cleared_when_back_to_factory(self):
        call_command('init_builtin_agents')
        agent = self._generic()
        factory_desc = builtin_agents.factory_of('通用问答')['description']
        self.staff.patch(f'{BASE}{agent.pk}/', {'description': '改过'})
        resp = self.staff.patch(f'{BASE}{agent.pk}/', {'description': factory_desc})
        self.assertEqual(resp.status_code, 200, resp.content)
        agent.refresh_from_db()
        self.assertFalse(agent.is_customized)

    def test_restore_builtin_wipes_config_mounts_and_flag(self):
        call_command('init_builtin_agents')
        agent = self._generic()
        server = _server()
        resp = self.staff.patch(
            f'{BASE}{agent.pk}/',
            {'prompt': '现场加了工具', 'mcp_mounts': [{'server': server.pk,
                                                      'tool_names': ['list_tickets']}]})
        self.assertEqual(resp.status_code, 200, resp.content)
        agent.refresh_from_db()
        self.assertTrue(agent.is_customized)
        self.assertTrue(agent.agentmcpserver_set.exists())

        resp = self.staff.post(f'{BASE}{agent.pk}/restore-builtin/', {})
        self.assertEqual(resp.status_code, 200, resp.content)
        agent.refresh_from_db()
        self.assertEqual(agent.prompt, builtin_agents.factory_of('通用问答')['prompt'])
        self.assertEqual(agent.agentmcpserver_set.count(), 0)
        self.assertEqual(agent.allow_tool_names, [])
        self.assertFalse(agent.is_customized)
        self.assertEqual(agent.current_version, 1)

    def test_restore_builtin_rejects_non_builtin_and_clean_builtin(self):
        call_command('init_builtin_agents')
        mine = self._make(name='我自己的助手')
        resp = self.staff.post(f'{BASE}{mine["id"]}/restore-builtin/', {})
        self.assertEqual(resp.status_code, 400, resp.content)

        agent = self._generic()
        resp = self.staff.post(f'{BASE}{agent.pk}/restore-builtin/', {})
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_builtin_cannot_be_deleted(self):
        call_command('init_builtin_agents')
        agent = self._generic()
        resp = self.staff.delete(f'{BASE}{agent.pk}/')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertTrue(AgentProfile.objects.filter(pk=agent.pk).exists())

    def test_restore_command_writes_back_factory_baseline(self):
        call_command('init_builtin_agents')
        agent = self._generic()
        agent.speech_tier = 'high'
        agent.save(update_fields=['speech_tier'])
        call_command('init_builtin_agents', restore=True)
        agent.refresh_from_db()
        self.assertEqual(agent.speech_tier, 'mid')
        self.assertFalse(agent.is_customized)


class SkillScopeC5Tests(AgentTestCase):
    """C5：技能生效范围进入装配选项与发布前检查"""

    def _scoped_skill(self, name='按部门技能', departments=None):
        skill = Skill.objects.create(
            name=name, status=SkillStatus.ACTIVE, approved_body='正文',
            scope_mode=SkillScope.DEPARTMENTS)
        if departments:
            skill.applicable_departments.set(departments)
        return skill

    def test_mount_options_expose_scope(self):
        skill = self._scoped_skill(departments=[self.rd])
        agent = self._make_row()
        AgentSkill.objects.create(agent=agent, skill=skill)
        row = self.staff.get(f'{BASE}{agent.pk}/mount-options/').data['skills'][0]
        self.assertEqual(row['scope_mode'], SkillScope.DEPARTMENTS)
        self.assertEqual(row['scope_departments'], ['研发中心'])
        self.assertTrue(row['applies_to_me'])

    def test_publish_check_notices_scope_limited(self):
        skill = self._scoped_skill(departments=[self.rd])
        agent = self._make_row()
        AgentSkill.objects.create(agent=agent, skill=skill)
        result = services.prepublish_check(agent)
        codes = [item['code'] for item in result['notices']]
        self.assertIn('skill_scope_limited', codes)
        self.assertNotIn('skill_scope_empty', codes)

    def test_publish_check_warns_empty_scope(self):
        skill = self._scoped_skill(name='空范围技能')
        agent = self._make_row()
        AgentSkill.objects.create(agent=agent, skill=skill)
        result = services.prepublish_check(agent)
        self.assertIn('skill_scope_empty', [item['code'] for item in result['warnings']])
        self.assertTrue(result['requires_ack'])

    def test_publish_check_notices_rollout_and_targets(self):
        skill = Skill.objects.create(
            name='灰度技能', status=SkillStatus.ACTIVE, approved_body='正文',
            rollout_percent=30)
        skill.applicable_users.set([User.objects.create_user(username='pilot-c5')])
        agent = self._make_row()
        AgentSkill.objects.create(agent=agent, skill=skill)
        result = services.prepublish_check(agent)
        codes = [item['code'] for item in result['notices']]
        self.assertIn('skill_gray_percent', codes)
        self.assertIn('skill_scope_users', codes)


class DefaultPreferencesD8Tests(AgentTestCase):
    """D8：管理员预置偏好复用用户自建项的内容口径，变更留痕、克隆继承"""

    def test_update_and_read_back(self):
        agent = self._make()
        resp = self.staff.patch(f'{BASE}{agent["id"]}/', {
            'default_preferences': ['先给结论再给依据', '用「你」称呼我']})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['default_preferences'],
                         ['先给结论再给依据', '用「你」称呼我'])
        detail = self.staff.get(f'{BASE}{agent["id"]}/').data
        self.assertEqual(detail['default_preferences'],
                         ['先给结论再给依据', '用「你」称呼我'])

    def test_rejects_more_than_eight(self):
        agent = self._make()
        resp = self.staff.patch(f'{BASE}{agent["id"]}/', {
            'default_preferences': [f'偏好{i}' for i in range(9)]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('最多', str(resp.data))

    def test_dedupes_and_rejects_business_data(self):
        agent = self._make()
        resp = self.staff.patch(f'{BASE}{agent["id"]}/', {
            'default_preferences': ['先给结论', '先给结论']})
        self.assertEqual(resp.data['default_preferences'], ['先给结论'])
        bad = self.staff.patch(f'{BASE}{agent["id"]}/', {
            'default_preferences': ['工号 202601']})
        self.assertEqual(bad.status_code, 400)

    def test_change_is_audited(self):
        agent = self._make()
        self.staff.patch(f'{BASE}{agent["id"]}/', {'default_preferences': ['先给结论']})
        log = AuditLog.objects.filter(action='agent_preferences').first()
        self.assertIsNotNone(log)
        self.assertIn('默认偏好', log.detail)
        self.assertIn('先给结论', log.detail)

    def test_clone_copies_preferences(self):
        agent = self._make()
        self.staff.patch(f'{BASE}{agent["id"]}/', {'default_preferences': ['先给结论']})
        resp = self.staff.post(f'{BASE}{agent["id"]}/clone/', {})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data['default_preferences'], ['先给结论'])


# ─── 跨环境导出 / 导入（单级审批） ───

class ImportExportTests(AgentTestCase):
    """导出不含密钥、导入必须另一人审批、落库为草稿、依赖缺失显式告警"""

    def _package(self, **overrides):
        agent = {
            'name': '跨环境助手',
            'description': '由另一个环境导入的助手',
            'job_tags': ['行政'],
            'icon': '',
            'model_name': 'qwen-max',
            'speech_tier': 'mid',
            'retrieval_tier': 'standard',
            'knowledge_mode': 'on_demand',
            'followup_rewrite': True,
            'enable_thinking': False,
            'prompt': LONG_PROMPT,
            'default_preferences': ['先给结论'],
            'builtin_capabilities': ['generate_file'],
            'permission_tier': 'whitelist',
            'allow_tool_names': [],
            'visibility': 'private',
            'include_sub_departments': False,
            'owner_department': '总部',
            'mcp_mounts': [],
            'skills': [],
            'knowledge_bases': [],
            'allowed_departments': [],
        }
        extra = overrides.pop('agent', {})
        agent.update(overrides)
        agent.update(extra)
        return {'format': 'lasso.agent', 'version': 1,
                'source': {'agent': '源环境助手', 'slug': 'source', 'status': 'published',
                           'current_version': 3},
                'agent': agent}

    def _submit(self, package, client=None):
        client = client or self.worker_client
        return client.post(f'{BASE}import-requests/', {'package': package})

    def test_export_uses_portable_refs_and_no_secrets(self):
        server = _server()
        skill = Skill.objects.create(name='工伤认定规范', status=SkillStatus.ACTIVE,
                                     approved_body='按目录认定')
        kb = KnowledgeBase.objects.create(name='制度库')
        agent = self._make_row(name='导出助手', visibility='department',
                               knowledge_base_ids=[kb.pk])
        AgentMCPServer.objects.create(agent=agent, server=server,
                                      tool_names=['list_tickets'])
        AgentSkill.objects.create(agent=agent, skill=skill)
        agent.allowed_departments.set([self.rd])

        resp = self.staff.get(f'{BASE}{agent.pk}/export/')
        self.assertEqual(resp.status_code, 200, resp.content)
        package = resp.data
        self.assertEqual(package['version'], 1)
        payload = package['agent']
        self.assertEqual(payload['mcp_mounts'][0]['server_slug'], 'ticket')
        self.assertEqual(payload['skills'][0]['slug'], skill.slug)
        self.assertEqual(payload['knowledge_bases'][0]['name'], '制度库')
        self.assertEqual(payload['allowed_departments'], ['研发中心'])
        # 密钥类字段一个都不能出现
        dumped = json.dumps(package, ensure_ascii=False)
        self.assertNotIn('secrets_enc', dumped)
        self.assertNotIn('api_key', dumped)
        self.assertTrue(AuditLog.objects.filter(action='agent_export').exists())

    def test_submit_does_not_create_agent_and_other_admin_approves(self):
        resp = self._submit(self._package())
        self.assertEqual(resp.status_code, 201, resp.content)
        request_id = resp.data['request']['id']
        self.assertFalse(AgentProfile.objects.filter(name='跨环境助手').exists())
        self.assertTrue(AuditLog.objects.filter(action='agent_import_request').exists())

        # 普通用户没有审批权限
        self.assertEqual(
            self.worker_client.post(f'{BASE}import-requests/{request_id}/approve/').status_code,
            403)

        resp = self.staff.post(f'{BASE}import-requests/{request_id}/approve/', {'note': '同意'})
        self.assertEqual(resp.status_code, 201, resp.content)
        agent = AgentProfile.objects.get(name='跨环境助手')
        self.assertEqual(agent.status, AgentStatus.DRAFT)
        self.assertEqual(agent.current_version, 0)
        self.assertEqual(agent.prompt, LONG_PROMPT)
        self.assertEqual(resp.data['request']['status'], 'approved')
        self.assertEqual(resp.data['request']['reviewed_by_name'], 'admin')
        self.assertTrue(AuditLog.objects.filter(action='agent_import_approve').exists())

    def test_submitter_cannot_approve_own_request(self):
        resp = self._submit(self._package(name='自审助手'), client=self.staff)
        self.assertEqual(resp.status_code, 201, resp.content)
        request_id = resp.data['request']['id']
        resp = self.staff.post(f'{BASE}import-requests/{request_id}/approve/')
        self.assertEqual(resp.status_code, 403)
        self.assertIn('另一人', str(resp.data))

    def test_reject_keeps_no_agent_and_records_note(self):
        resp = self._submit(self._package(name='驳回助手'))
        request_id = resp.data['request']['id']
        resp = self.staff.post(f'{BASE}import-requests/{request_id}/reject/', {'note': '描述不清'})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['request']['status'], 'rejected')
        self.assertFalse(AgentProfile.objects.filter(name='驳回助手').exists())
        self.assertTrue(AuditLog.objects.filter(action='agent_import_reject').exists())
        # 已驳回不可再次审批
        again = self.staff.post(f'{BASE}import-requests/{request_id}/approve/')
        self.assertEqual(again.status_code, 409)

    def test_duplicate_name_blocks_submission(self):
        self._make_row(name='已存在助手')
        resp = self._submit(self._package(name='已存在助手'))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.data['code'], 'import_blocked')
        self.assertFalse(AgentImportRequest.objects.exists())

    def test_department_visibility_missing_departments_blocks(self):
        resp = self._submit(self._package(name='部门助手', visibility='department',
                                          allowed_departments=['不存在的部门']))
        self.assertEqual(resp.status_code, 422)
        self.assertIn('department_missing',
                      [row['code'] for row in resp.data['blocking']])

    def test_missing_dependencies_are_warned_and_skipped(self):
        resp = self._submit(self._package(
            name='缺依赖助手',
            mcp_mounts=[{'server_slug': 'ghost', 'server_name': '幽灵来源',
                         'tool_names': ['x']}],
            skills=[{'slug': 'ghost-skill', 'name': '幽灵技能'}],
            knowledge_bases=[{'id': 999, 'name': '幽灵知识库'}],
            allow_tool_names=['mcp__ghost__x'],
        ))
        self.assertEqual(resp.status_code, 201, resp.content)
        request_id = resp.data['request']['id']
        self.assertGreaterEqual(len(resp.data['request']['warnings']), 4)

        resp = self.staff.post(f'{BASE}import-requests/{request_id}/approve/')
        self.assertEqual(resp.status_code, 201, resp.content)
        agent = AgentProfile.objects.get(name='缺依赖助手')
        self.assertEqual(agent.agentmcpserver_set.count(), 0)
        self.assertEqual(agent.skills.count(), 0)
        self.assertEqual(agent.knowledge_base_ids, [])
        self.assertEqual(agent.allow_tool_names, [])
        self.assertTrue(AgentImportRequest.objects.get(pk=request_id).result['warnings'])

    def test_mounts_skills_and_departments_are_mapped(self):
        server = _server()
        skill = Skill.objects.create(name='写作规范', status=SkillStatus.ACTIVE,
                                     approved_body='正文')
        kb = KnowledgeBase.objects.create(name='制度库')
        resp = self._submit(self._package(
            name='完整助手',
            mcp_mounts=[{'server_slug': 'ticket', 'server_name': '工单系统',
                         'tool_names': ['list_tickets']}],
            skills=[{'slug': skill.slug, 'name': skill.name}],
            knowledge_bases=[{'id': 1, 'name': '制度库'}],
            allowed_departments=['研发中心'],
            visibility='department',
            allow_tool_names=['mcp__ticket__list_tickets'],
        ))
        self.assertEqual(resp.status_code, 201, resp.content)
        request_id = resp.data['request']['id']
        resp = self.staff.post(f'{BASE}import-requests/{request_id}/approve/')
        self.assertEqual(resp.status_code, 201, resp.content)
        agent = AgentProfile.objects.get(name='完整助手')
        self.assertEqual(agent.agentmcpserver_set.get().server, server)
        self.assertEqual(agent.agentmcpserver_set.get().tool_names, ['list_tickets'])
        self.assertEqual([s.skill_id for s in agent.agentskill_set.all()], [skill.pk])
        self.assertEqual(agent.knowledge_base_ids, [kb.pk])
        self.assertEqual(list(agent.allowed_departments.values_list('name', flat=True)),
                         ['研发中心'])
        self.assertEqual(agent.allow_tool_names, ['mcp__ticket__list_tickets'])
        self.assertEqual(agent.owner_department, self.hq)

    def test_list_scope_and_summary(self):
        other = _user('other', department='运维部')
        other_client = _client(other)
        self._submit(self._package(name='工人申请甲'))
        self._submit(self._package(name='工人申请乙'))
        self._submit(self._package(name='他人申请'), client=other_client)

        worker_rows = self.worker_client.get(f'{BASE}import-requests/').data
        self.assertEqual(worker_rows['count'], 2)
        other_rows = other_client.get(f'{BASE}import-requests/').data
        self.assertEqual(other_rows['count'], 1)
        admin_rows = self.staff.get(f'{BASE}import-requests/').data
        self.assertEqual(admin_rows['count'], 3)

        summary = self.staff.get(f'{BASE}import-requests/summary/').data
        self.assertEqual(summary['pending'], 3)
        self.assertEqual(summary['actionable'], 3)  # 三条都不是管理员本人提交，均可审批
        worker_summary = self.worker_client.get(f'{BASE}import-requests/summary/').data
        self.assertEqual(worker_summary['pending'], 2)
        self.assertEqual(worker_summary['actionable'], 0)

    def test_preview_reports_blocking_without_persisting(self):
        resp = self.worker_client.post(f'{BASE}import-requests/preview/',
                                       {'package': self._package(name='X')})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data['blocking'])
        self.assertFalse(AgentImportRequest.objects.exists())
