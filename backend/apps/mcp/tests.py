"""MCP 工具来源接口测试（B2/B3/B4/B5/B6/B8 的口径落到断言上）

约定：所有用例都把 services._run_in_thread 打成假探测，避免测试真的拉起 MCP 会话；
需要验证失败归类时用 side_effect 抛特定异常。
"""
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from backend.apps.agenthub.models import AgentMCPServer, AgentProfile
from backend.apps.mcp.models import MCPServer, MCPTool
from backend.apps.settings.models import SystemSetting
from backend.apps.users.models import AuditLog, Department, User

BASE = '/api/mcp/servers/'

FAKE_TOOLS = [
    {'name': 'list_offline_devices', 'description': '查询离线设备列表',
     'input_schema': {'type': 'object', 'properties': {
         'dept': {'type': 'string', 'description': '部门名称'}}}, 'params': [
         {'name': 'dept', 'type': 'string', 'description': '部门名称'}]},
    {'name': 'device_detail', 'description': '查询单台设备详情',
     'input_schema': {'type': 'object', 'properties': {
         'device_id': {'type': 'string'}}}, 'params': []},
]


def _user(username, is_staff=False):
    return User.objects.create_user(username=username, password='demo-pass-9x2', is_staff=is_staff)


def _http_server(**kwargs):
    data = {'name': '内网工单系统', 'transport': 'http_mcp',
            'url': 'http://198.51.100.10:8931/mcp'}
    data.update(kwargs)
    return MCPServer.objects.create(**data)


class McpTestCase(TestCase):
    """公共夹具：管理员客户端 + 假探测"""

    def setUp(self):
        self.admin = _user('admin', is_staff=True)
        self.client = APIClientAuth(self.admin)
        patcher = patch('backend.apps.mcp.services._run_in_thread', return_value=FAKE_TOOLS)
        patcher.start()
        self.addCleanup(patcher.stop)


class APIClientAuth:
    """极简封装：APIClient + force_authenticate"""

    def __init__(self, user):
        from rest_framework.test import APIClient
        self.raw = APIClient()
        self.raw.force_authenticate(user=user)

    def get(self, url, **kw):
        return self.raw.get(url, **kw)

    def post(self, url, data=None, format='json', **kw):
        return self.raw.post(url, data=data, format=format, **kw)

    def patch(self, url, data=None, format='json', **kw):
        return self.raw.patch(url, data=data, format=format, **kw)

    def put(self, url, data=None, format='json', **kw):
        return self.raw.put(url, data=data, format=format, **kw)

    def delete(self, url, **kw):
        return self.raw.delete(url, **kw)


class PermissionTests(McpTestCase):
    """读接口登录即可，写接口与闸门仅管理员（PRD 7.6）"""

    def test_anonymous_cannot_read(self):
        from rest_framework.test import APIClient
        resp = APIClient().get(BASE)
        self.assertIn(resp.status_code, (401, 403))

    def test_normal_user_can_read_but_not_write(self):
        user = _user('bob')
        client = APIClientAuth(user)
        _http_server()
        resp = client.get(BASE)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(client.post(BASE, {'name': 'x'}).status_code, 403)
        self.assertEqual(client.get(f'{BASE}command-prefixes/').status_code, 403)
        self.assertEqual(client.get(f'{BASE}egress/').status_code, 403)


class CreateAndTestTests(McpTestCase):
    """B1/B3：注册即自动测一次，新工具默认关闭"""

    def test_create_returns_connectivity_result(self):
        resp = self.client.post(BASE, {'name': '内网工单', 'transport': 'http_mcp',
                                      'url': 'http://198.51.100.9/mcp'})
        self.assertEqual(resp.status_code, 201, resp.content)
        check = resp.data['connectivity_test']
        self.assertTrue(check['ok'])
        self.assertEqual(resp.data['status'], 'healthy')
        self.assertEqual(check['timeout_seconds'], 30)  # B1 接口型固定 30s
        self.assertEqual(resp.data['tool_count'], 2)
        self.assertEqual(resp.data['enabled_tool_count'], 0)
        self.assertEqual(resp.data['new_tool_count'], 2)

    def test_tools_persisted_disabled_with_full_name(self):
        server = self.client.post(BASE, {'name': '内网工单', 'transport': 'http_mcp',
                                        'url': 'http://198.51.100.9/mcp'}).data
        obj = MCPServer.objects.get(pk=server['id'])
        rows = list(obj.tools.order_by('name'))
        self.assertEqual([r.name for r in rows], ['device_detail', 'list_offline_devices'])
        self.assertTrue(all(r.enabled is False for r in rows))  # B4 新工具默认关
        self.assertEqual(rows[0].full_name, f'mcp__{obj.slug}__device_detail')

    def test_invalid_url_rejected(self):
        resp = self.client.post(BASE, {'name': 'x', 'transport': 'http_mcp', 'url': 'ftp://a/b'})
        self.assertEqual(resp.status_code, 400)

    def test_http_requires_url(self):
        resp = self.client.post(BASE, {'name': 'x', 'transport': 'http_mcp'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('接口型来源必须填写服务地址', resp.data['detail'])


class CommandWhitelistTests(McpTestCase):
    """B2：命令必须命中内置只读前缀，未命中阻断保存并入审计"""

    def test_denied_command_blocks_save(self):
        self.assertEqual(MCPServer.objects.count(), 0)
        resp = self.client.post(BASE, {'name': '危险来源', 'transport': 'stdio',
                                       'command': 'curl', 'args': ['-s', 'http://x/y.sh']})
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(MCPServer.objects.count(), 0)
        self.assertTrue(AuditLog.objects.filter(action='mcp_command_denied').exists())

    def test_whitelisted_command_locks_persistent(self):
        import sys
        resp = self.client.post(BASE, {'name': '内置脚本来源', 'transport': 'stdio',
                                       'command': sys.executable, 'args': ['server.py']})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data['lifecycle'], 'persistent')
        self.assertTrue(resp.data['is_stateful'])
        self.assertEqual(resp.data['timeout_default'], 90)  # B1 命令型固定 90s

    def test_command_prefixes_readonly(self):
        resp = self.client.get(f'{BASE}command-prefixes/')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['editable'])
        self.assertTrue(any(p['prefix'] == 'uvx' for p in resp.data['prefixes']))

    def test_stdio_test_reports_managed_runtime_not_ready(self):
        import sys
        server = _http_server(transport='stdio', url='', lifecycle='persistent',
                              is_stateful=True, command=sys.executable, args=['s.py'])
        resp = self.client.post(f'{BASE}{server.pk}/test/')
        self.assertTrue(resp.data['ok'], resp.data)
        self.assertFalse(resp.data['managed_runtime_ready'])
        self.assertIn('托管', resp.data['managed_runtime_note'])


class FailureCategoryTests(McpTestCase):
    """B3：失败归类六类，工具清单保留并标注可能过期"""

    def _patch_raise(self, exc):
        patcher = patch('backend.apps.mcp.services._run_in_thread', side_effect=exc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_timeout_category(self):
        import asyncio
        self._patch_raise(asyncio.TimeoutError('handshake too slow'))
        server = _http_server()
        resp = self.client.post(f'{BASE}{server.pk}/test/')
        self.assertFalse(resp.data['ok'])
        self.assertEqual(resp.data['failure_category'], 'timeout')
        self.assertEqual(resp.data['status'], 'error')
        server.refresh_from_db()
        self.assertEqual(server.status, 'error')

    def test_auth_failed_category(self):
        self._patch_raise(RuntimeError('HTTP 401 Unauthorized'))
        server = _http_server()
        resp = self.client.post(f'{BASE}{server.pk}/test/')
        self.assertEqual(resp.data['failure_category'], 'auth_failed')
        self.assertEqual(resp.data['failure_label'], '认证失败')

    def test_stale_tools_kept_on_failure(self):
        server = _http_server()
        self.client.post(f'{BASE}{server.pk}/test/')
        self.assertEqual(MCPTool.objects.filter(server=server).count(), 2)
        self._patch_raise(OSError('[Errno 111] Connection refused'))
        resp = self.client.post(f'{BASE}{server.pk}/test/')
        self.assertTrue(resp.data['tools_stale'])
        self.assertEqual(len(resp.data['tools']), 2)
        self.assertEqual(MCPTool.objects.filter(server=server, state='present').count(), 2)

    def test_missing_tools_marked_stale(self):
        server = _http_server()
        self.client.post(f'{BASE}{server.pk}/test/')
        with patch('backend.apps.mcp.services._run_in_thread', return_value=FAKE_TOOLS[:1]):
            resp = self.client.post(f'{BASE}{server.pk}/test/')
        self.assertEqual(resp.data['missing_tools'], ['device_detail'])
        self.assertEqual(MCPTool.objects.get(server=server, name='device_detail').state,
                         'missing')


class SecretTests(McpTestCase):
    """B5：加密存储 + 掩码回显 + 整值替换 + 掩码提交视为未修改"""

    def setUp(self):
        super().setUp()
        self.server = self.client.post(BASE, {
            'name': '内网工单', 'transport': 'http_mcp', 'url': 'http://198.51.100.9/mcp',
            'secrets': {'headers': {'Authorization': 'Bearer token-XYZW9876'}},
        }).data

    def _obj(self):
        return MCPServer.objects.get(pk=self.server['id'])

    def test_never_returns_plaintext(self):
        resp = self.client.get(f"{BASE}{self.server['id']}/")
        self.assertNotIn('secrets', resp.data)
        self.assertEqual(resp.data['secrets_masked']['headers']['Authorization'], '******9876')
        self.assertTrue(resp.data['has_secrets'])
        self.assertNotIn('XYZW9876', str(resp.data))

    def test_ciphertext_in_db(self):
        raw = self._obj().secrets_enc
        self.assertNotIn('XYZW9876', raw)
        self.assertEqual(self._obj().get_secrets()['headers']['Authorization'],
                         'Bearer token-XYZW9876')

    def test_mask_submit_means_unchanged(self):
        self.client.patch(
            f"{BASE}{self.server['id']}/",
            {'secrets': {'headers': {'Authorization': self.server['secrets_masked']
                                                 ['headers']['Authorization']}}})
        self.assertEqual(self._obj().get_secrets()['headers']['Authorization'],
                         'Bearer token-XYZW9876')

    def test_new_value_replaces_whole(self):
        self.client.patch(f"{BASE}{self.server['id']}/",
                          {'secrets': {'headers': {'Authorization': 'Bearer other-1234'}}})
        self.assertEqual(self._obj().get_secrets()['headers']['Authorization'],
                         'Bearer other-1234')
        self.assertTrue(AuditLog.objects.filter(action='mcp_secret_update').exists())

    def test_secret_like_header_value_rejected(self):
        resp = self.client.post(BASE, {'name': '另一个', 'transport': 'http_mcp',
                                       'url': 'http://198.51.100.8/mcp',
                                       'headers': {'X-Key': '******abcd'}})
        self.assertEqual(resp.status_code, 400)

    def test_unknown_secret_group_rejected(self):
        resp = self.client.patch(f"{BASE}{self.server['id']}/",
                                 {'secrets': {'query': {'a': 'b'}}})
        self.assertEqual(resp.status_code, 400)


class EgressGateTests(McpTestCase):
    """B8：出网总开关出厂为关，出网标记由平台判定"""

    def test_egress_flag_auto_judged(self):
        resp = self.client.post(BASE, {'name': '公有云 MCP', 'transport': 'http_mcp',
                                       'url': 'https://mcp.demo.com/mcp',
                                       'egress_domains': ['mcp.demo.com']})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(resp.data['requires_egress'])
        self.assertEqual(resp.data['status'], 'egress_blocked')
        self.assertFalse(resp.data['connectivity_test']['ok'])
        self.assertEqual(resp.data['connectivity_test']['failure_category'], 'egress_blocked')
        self.assertEqual(MCPTool.objects.filter(server_id=resp.data['id']).count(), 0)

    def test_intranet_source_not_flagged(self):
        resp = self.client.post(BASE, {'name': '内网', 'transport': 'http_mcp',
                                       'url': 'http://198.51.100.9/mcp'})
        self.assertFalse(resp.data['requires_egress'])
        self.assertTrue(resp.data['connectivity_test']['ok'])

    def test_switch_defaults_off_and_blocks(self):
        self.assertFalse(self.client.get(f'{BASE}egress/').data['enabled'])
        server = _http_server(name='公有云', requires_egress=True, status='healthy')
        resp = self.client.post(f'{BASE}{server.pk}/test/')
        self.assertEqual(resp.data['status'], 'egress_blocked')

    def test_enable_switch_unblocks(self):
        _http_server(name='公有云', requires_egress=True, status='egress_blocked')
        resp = self.client.post(f'{BASE}egress/', {'enabled': True})
        self.assertTrue(resp.data['enabled'])
        self.assertEqual(SystemSetting.get_value('mcp_egress_enabled'), 'true')
        server = MCPServer.objects.get(name='公有云')
        self.assertEqual(server.status, 'unknown')
        self.assertTrue(self.client.post(f'{BASE}{server.pk}/test/').data['ok'])

    def test_disable_switch_reblocks(self):
        self.client.post(f'{BASE}egress/', {'enabled': True})
        _http_server(name='公有云', requires_egress=True, status='healthy')
        self.client.post(f'{BASE}egress/', {'enabled': False})
        server = MCPServer.objects.get(name='公有云')
        self.assertEqual(server.status, 'egress_blocked')
        self.assertTrue(AuditLog.objects.filter(action='egress_toggle').exists())

    def test_mountable_hides_blocked(self):
        _http_server(name='公有云', requires_egress=True, status='egress_blocked')
        ok = _http_server(name='内网可用')
        self.client.post(f'{BASE}{ok.pk}/test/')
        data = self.client.get(f'{BASE}mountable/').data
        self.assertEqual([d['name'] for d in data], ['内网可用'])


class ToolToggleTests(McpTestCase):
    """B4：启停与失效工具不可启用"""

    def setUp(self):
        super().setUp()
        self.server = _http_server()
        self.client.post(f'{BASE}{self.server.pk}/test/')

    def test_enable_then_disable(self):
        resp = self.client.post(f'{BASE}{self.server.pk}/tools/toggle/',
                                {'names': ['device_detail'], 'enabled': True})
        self.assertEqual(resp.data['enabled_tools'], ['device_detail'])
        self.assertTrue(MCPTool.objects.get(server=self.server,
                                            name='device_detail').enabled)
        resp = self.client.post(f'{BASE}{self.server.pk}/tools/toggle/',
                                {'names': ['device_detail'], 'enabled': False})
        self.assertEqual(resp.data['enabled_tools'], [])

    def test_select_all(self):
        resp = self.client.post(f'{BASE}{self.server.pk}/tools/toggle/', {'select_all': True})
        self.assertEqual(sorted(resp.data['enabled_tools']),
                         ['device_detail', 'list_offline_devices'])

    def test_missing_tool_cannot_enable(self):
        with patch('backend.apps.mcp.services._run_in_thread', return_value=FAKE_TOOLS[:1]):
            self.client.post(f'{BASE}{self.server.pk}/test/')
        resp = self.client.post(f'{BASE}{self.server.pk}/tools/toggle/',
                                {'names': ['device_detail'], 'enabled': True})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('工具已失效', str(resp.content, 'utf-8'))

    def test_unknown_tool_name(self):
        resp = self.client.post(f'{BASE}{self.server.pk}/tools/toggle/', {'names': ['nope']})
        self.assertEqual(resp.status_code, 400)

    def test_tools_listed_with_state(self):
        resp = self.client.get(f'{BASE}{self.server.pk}/tools/?state=present')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['count'], 2)
        by_name = {r['name']: r for r in resp.data['results']}
        self.assertEqual(by_name['list_offline_devices']['params'][0]['name'], 'dept')
        self.assertEqual(by_name['device_detail']['full_name'],
                         f'mcp__{self.server.slug}__device_detail')
        self.assertEqual(by_name['device_detail']['enabled'], False)

    def test_tools_search(self):
        resp = self.client.get(f'{BASE}{self.server.pk}/tools/?search=offline')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['count'], 1)


class ReferenceAndDeleteTests(McpTestCase):
    """B6：删除前列引用，默认阻断，两条出路"""

    def setUp(self):
        super().setUp()
        self.server = _http_server()
        self.agent = AgentProfile.objects.create(name='工单助手')
        AgentMCPServer.objects.create(agent=self.agent, server=self.server,
                                      tool_names=['device_detail'])

    def test_references_reported(self):
        resp = self.client.get(f'{BASE}{self.server.pk}/references/')
        self.assertEqual(resp.data['count'], 1)
        self.assertEqual(resp.data['references'][0]['name'], '工单助手')
        self.assertEqual(resp.data['references'][0]['tool_names'], ['device_detail'])

    def test_delete_blocked_with_options(self):
        resp = self.client.delete(f'{BASE}{self.server.pk}/')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'mcp_server_referenced')
        self.assertEqual(len(resp.data['options']), 2)
        self.assertTrue(MCPServer.objects.filter(pk=self.server.pk).exists())

    def test_delete_with_confirm_unmounts(self):
        resp = self.client.delete(f'{BASE}{self.server.pk}/?confirm_unmount=true')
        self.assertEqual(resp.status_code, 204, resp.content)
        self.assertFalse(AgentMCPServer.objects.filter(server_id=self.server.pk).exists())
        self.assertTrue(AuditLog.objects.filter(action='mcp_unmount').exists())
        self.assertTrue(AuditLog.objects.filter(action='mcp_delete').exists())

    def test_disable_is_alternative(self):
        resp = self.client.post(f'{BASE}{self.server.pk}/disable/')
        self.assertEqual(resp.data['status'], 'disabled')
        self.assertFalse(resp.data['is_active'])
        self.assertEqual([d['name'] for d in self.client.get(f'{BASE}mountable/').data], [])
        resp = self.client.post(f'{BASE}{self.server.pk}/enable/')
        self.assertEqual(resp.data['status'], 'unknown')


class BuiltinProtectionTests(McpTestCase):
    """B7：内置演示来源不可删、不可改地址与命令、排在自定义之前"""

    def setUp(self):
        super().setUp()
        self.builtin = _http_server(name='演示设备状态查询', is_builtin=True,
                                    slug='demo-device-status',
                                    url='http://127.0.0.1:8931/mcp')
        self.custom = _http_server(name='自定义来源')

    def test_builtin_listed_first(self):
        data = self.client.get(BASE).data['results']
        self.assertEqual([d['name'] for d in data], ['演示设备状态查询', '自定义来源'])

    def test_builtin_url_locked(self):
        resp = self.client.patch(f'{BASE}{self.builtin.pk}/',
                                 {'url': 'http://evil.example.com/mcp'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('不可编辑', str(resp.content, 'utf-8'))

    def test_builtin_cannot_delete(self):
        resp = self.client.delete(f'{BASE}{self.builtin.pk}/')
        self.assertEqual(resp.status_code, 400)

    def test_builtin_description_editable(self):
        resp = self.client.patch(f'{BASE}{self.builtin.pk}/', {'description': '演示用'})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['description'], '演示用')

    def test_duplicate_name_rejected(self):
        resp = self.client.post(BASE, {'name': '演示设备状态查询', 'transport': 'http_mcp',
                                       'url': 'http://198.51.100.6/mcp'})
        self.assertEqual(resp.status_code, 400)
        # 全局 custom_exception_handler 把字段级错误压进 detail 字符串
        detail = resp.data['detail']
        self.assertIn('name', detail)
        self.assertIn('已存在', detail)


class VisibilityFilterTests(McpTestCase):
    """A6 可见性在 MCP 侧的口径：公开 / 自建 / 指定部门（含上级授予）"""

    def setUp(self):
        super().setUp()
        self.dept = Department.objects.create(name='设备运维部')
        self.child = Department.objects.create(name='一线班组', parent=self.dept)
        self.user = _user('carol')
        self.client = APIClientAuth(self.user)

    def _names(self):
        resp = self.client.get(BASE)
        self.assertEqual(resp.status_code, 200, resp.content)
        return sorted(d['name'] for d in resp.data['results'])

    def test_public_visible_to_all(self):
        _http_server(name='公开来源', visibility='public')
        self.assertEqual(self._names(), ['公开来源'])

    def test_private_only_creator(self):
        _http_server(name='私有来源', visibility='private', created_by=self.user)
        other = _user('dave', is_staff=False)
        _http_server(name='他人私有', visibility='private', created_by=other)
        self.assertEqual(self._names(), ['私有来源'])

    def test_department_visible_to_member_and_child(self):
        server = _http_server(name='部门来源', visibility='department')
        server.allowed_departments.set([self.dept])
        self.user.department = self.dept.name
        self.user.save()
        self.assertEqual(self._names(), ['部门来源'])

        self.user.department = self.child.name
        self.user.save()
        self.assertEqual(self._names(), ['部门来源'])

    def test_department_not_visible_to_outsider(self):
        server = _http_server(name='部门来源', visibility='department')
        server.allowed_departments.set([self.dept])
        self.user.department = '市场部'
        self.user.save()
        self.assertEqual(self._names(), [])

    def test_department_visibility_requires_departments(self):
        self.client = APIClientAuth(_user('eve', is_staff=True))
        resp = self.client.post(BASE, {'name': '无部门', 'transport': 'http_mcp',
                                       'url': 'http://198.51.100.5/mcp', 'visibility': 'department'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('必须至少选择一个部门', resp.data['detail'])


class SearchAndFilterTests(McpTestCase):
    """列表检索：search / status / transport"""

    def setUp(self):
        super().setUp()
        _http_server(name='设备状态查询', status='healthy')
        _http_server(name='CRM 接口', transport='http_mcp', url='http://198.51.100.4/mcp',
                     status='unknown')

    def test_search(self):
        data = self.client.get(f'{BASE}?search=设备').data
        self.assertEqual(data['count'], 1)

    def test_filter_status(self):
        data = self.client.get(f'{BASE}?status=healthy').data
        self.assertEqual([d['name'] for d in data['results']], ['设备状态查询'])

    def test_pagination_shape(self):
        data = self.client.get(f'{BASE}?page_size=1').data
        self.assertEqual(set(data), {'count', 'page', 'page_size', 'total_pages', 'results'})


class ExportImportTests(McpTestCase):
    """跨环境迁移：导出不含密钥，导入按名称去重、部门按名称映射"""

    def setUp(self):
        super().setUp()
        self.dept = Department.objects.create(name='设备运维部')

    def _server_with_secret(self, **kwargs):
        server = _http_server(**kwargs)
        server.set_secrets({'headers': {'Authorization': 'Bearer top-secret-token'}})
        server.save(update_fields=['secrets_enc'])
        return server

    def test_export_never_leaks_secrets(self):
        server = self._server_with_secret(name='带密钥来源')
        server.allowed_departments.set([self.dept])
        resp = self.client.get(f'{BASE}export/')
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.content.decode('utf-8')
        self.assertNotIn('top-secret-token', body)
        self.assertNotIn('secrets_enc', body)
        self.assertNotIn('******', body)
        row = resp.data['servers'][0]
        self.assertEqual(row['name'], '带密钥来源')
        self.assertTrue(row['has_secrets'])
        self.assertEqual(row['allowed_departments'], ['设备运维部'])
        self.assertEqual(resp.data['version'], 1)

    def test_export_requires_admin(self):
        user = _user('bob')
        self.assertEqual(APIClientAuth(user).get(f'{BASE}export/').status_code, 403)

    def test_import_requires_admin(self):
        user = _user('bob')
        resp = APIClientAuth(user).post(f'{BASE}import/', {'servers': [
            {'name': '来源A', 'transport': 'http_mcp', 'url': 'http://198.51.100.8/mcp'}]})
        self.assertEqual(resp.status_code, 403)

    def test_import_creates_and_ignores_secret_fields(self):
        resp = self.client.post(f'{BASE}import/', {
            'version': 1,
            'servers': [{
                'name': '来源A', 'transport': 'http_mcp', 'url': 'http://198.51.100.8/mcp',
                'description': '导入来源', 'visibility': 'department',
                'allowed_departments': ['设备运维部'],
                # 手工改过的导出文件也不允许把密钥带进来
                'secrets': {'headers': {'Authorization': 'Bearer leaked'}},
                'secrets_enc': 'whatever', 'has_secrets': True,
            }],
        }, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data['created'], ['来源A'])
        server = MCPServer.objects.get(name='来源A')
        self.assertEqual(server.secrets_enc, '')
        self.assertEqual(server.status, 'unknown')  # 导入不自动探测
        self.assertEqual(list(server.allowed_departments.values_list('name', flat=True)),
                         ['设备运维部'])
        self.assertTrue(AuditLog.objects.filter(action='mcp_import').exists())

    def test_import_skips_existing_name(self):
        _http_server(name='来源A')
        resp = self.client.post(f'{BASE}import/', {'servers': [
            {'name': '来源A', 'transport': 'http_mcp', 'url': 'http://198.51.100.1/mcp'}]})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['skipped'], ['来源A'])
        self.assertEqual(MCPServer.objects.filter(name='来源A').count(), 1)

    def test_import_reports_missing_department(self):
        resp = self.client.post(f'{BASE}import/', {'servers': [{
            'name': '来源B', 'transport': 'http_mcp', 'url': 'http://198.51.100.2/mcp',
            'visibility': 'department', 'allowed_departments': ['不存在的部门'],
        }]})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn('不存在的部门', resp.data['errors'][0]['detail'])
        self.assertFalse(MCPServer.objects.filter(name='来源B').exists())

    def test_import_rejects_unknown_version_and_empty(self):
        self.assertEqual(self.client.post(f'{BASE}import/', {'version': 99, 'servers': [
            {'name': 'x', 'transport': 'http_mcp', 'url': 'http://198.51.100.3/mcp'}]
        }).status_code, 400)
        self.assertEqual(self.client.post(f'{BASE}import/', {'servers': []}).status_code, 400)


class EgressDomainTests(McpTestCase):
    """B8-a：出网来源必须声明允许域名；严格模式在连接前复验域名与解析结果"""

    def setUp(self):
        super().setUp()
        SystemSetting.set_value('mcp_egress_enabled', 'true', '出网总开关')
        SystemSetting.set_value('mcp_egress_strict_domains', 'false', '严格域名白名单')

    def _payload(self, **over):
        data = {
            'name': '外部来源', 'transport': 'http_mcp',
            'url': 'https://api.vendor.com/mcp',
            'egress_domains': ['api.vendor.com'],
        }
        data.update(over)
        return data

    def _existing(self, **over):
        data = {'name': '外部来源', 'transport': 'http_mcp',
                'url': 'https://api.vendor.com/mcp', 'requires_egress': True,
                'egress_domains': ['api.vendor.com']}
        data.update(over)
        return _http_server(**data)

    def test_create_requires_covering_domain(self):
        no_domain = self.client.post(BASE, self._payload(egress_domains=[]))
        self.assertEqual(no_domain.status_code, 400, no_domain.content)
        self.assertIn('egress_domains', str(no_domain.data))

        covered = self.client.post(BASE, self._payload())
        self.assertEqual(covered.status_code, 201, covered.content)
        self.assertTrue(covered.data['requires_egress'])
        self.assertEqual(covered.data['egress_domains'], ['api.vendor.com'])

    def test_wildcard_covers_subdomain_but_not_apex(self):
        sub = self.client.post(BASE, self._payload(
            name='子域来源', url='https://sub.vendor.com/mcp',
            egress_domains=['*.vendor.com']))
        self.assertEqual(sub.status_code, 201, sub.content)

        apex = self.client.post(BASE, self._payload(
            name='裸域来源', url='https://vendor.com/mcp',
            egress_domains=['*.vendor.com']))
        self.assertEqual(apex.status_code, 400, apex.content)
        self.assertIn('未覆盖', str(apex.data))

    def test_rejects_malformed_domain(self):
        resp = self.client.post(BASE, self._payload(egress_domains=['https://api.vendor.com']))
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn('只填主机名', str(resp.data))

    def test_strict_off_keeps_legacy_behavior(self):
        """严格白名单关闭时只看总开关：存量来源即使没登记域名也不被拦"""
        server = self._existing(egress_domains=[])
        resp = self.client.post(f'{BASE}{server.pk}/test/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['status'], 'healthy')
        self.assertFalse(resp.data['egress']['strict'])

    def test_strict_blocks_undeclared_host(self):
        SystemSetting.set_value('mcp_egress_strict_domains', 'true', '严格域名白名单')
        server = self._existing(egress_domains=['other.vendor.com'])
        resp = self.client.post(f'{BASE}{server.pk}/test/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['status'], 'egress_blocked')
        self.assertEqual(resp.data['egress']['reason_code'], 'egress_domain_not_allowed')
        self.assertIn('api.vendor.com', resp.data['egress']['admin_message'])
        server.refresh_from_db()
        self.assertIn('不在该来源的允许域名内', server.last_error)
        self.assertTrue(AuditLog.objects.filter(action='mcp_egress_denied').exists())

    def test_strict_allows_declared_host_and_audits_success(self):
        SystemSetting.set_value('mcp_egress_strict_domains', 'true', '严格域名白名单')
        server = self._existing()
        with patch('backend.apps.mcp.services.resolved_addresses',
                   return_value=['93.184.216.34']):
            resp = self.client.post(f'{BASE}{server.pk}/test/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['status'], 'healthy')
        self.assertTrue(resp.data['egress']['strict'])
        self.assertTrue(AuditLog.objects.filter(action='mcp_egress_allowed').exists())

    def test_strict_blocks_dns_rebinding_to_private(self):
        SystemSetting.set_value('mcp_egress_strict_domains', 'true', '严格域名白名单')
        server = self._existing()
        with patch('backend.apps.mcp.services.resolved_addresses',
                   return_value=['172.16.0.9']):
            resp = self.client.post(f'{BASE}{server.pk}/test/')
        self.assertEqual(resp.data['status'], 'egress_blocked')
        self.assertEqual(resp.data['egress']['reason_code'], 'egress_private_resolved')
        self.assertEqual(resp.data['egress']['resolved_address'], '172.16.0.9')
        self.assertIn('私网', resp.data['egress']['admin_message'])

    def test_switch_off_wins_over_strict(self):
        SystemSetting.set_value('mcp_egress_enabled', 'false', '出网总开关')
        SystemSetting.set_value('mcp_egress_strict_domains', 'true', '严格域名白名单')
        server = self._existing()
        resp = self.client.post(f'{BASE}{server.pk}/test/')
        self.assertEqual(resp.data['egress']['reason_code'], 'egress_switch_off')

    def test_egress_policy_endpoint_toggles_strict(self):
        state = self.client.get(f'{BASE}egress/')
        self.assertIn('strict_domains', state.data)
        self.assertFalse(state.data['strict_domains'])

        resp = self.client.post(f'{BASE}egress/', {'strict_domains': True})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data['strict_domains'])
        self.assertTrue(AuditLog.objects.filter(action='egress_strict_toggle').exists())

        normal = _user('normal')
        self.assertEqual(APIClientAuth(normal).post(
            f'{BASE}egress/', {'strict_domains': False}).status_code, 403)

    def test_export_carries_egress_domains(self):
        self._existing()
        resp = self.client.get(f'{BASE}export/')
        row = next(r for r in resp.data['servers'] if r['name'] == '外部来源')
        self.assertEqual(row['egress_domains'], ['api.vendor.com'])


class BuiltinSeedCommandTests(TestCase):
    """B7：内置演示来源的出厂注册命令（幂等、不覆盖现场勾选、交付时可停用）"""

    def test_seed_registers_builtin_as_unverified(self):
        call_command('init_builtin_mcp')
        server = MCPServer.objects.get(slug='demo-devices')
        self.assertTrue(server.is_builtin)
        self.assertEqual(server.name, '演示设备状态查询')
        self.assertEqual(server.transport, 'http_mcp')
        self.assertEqual(server.url, 'http://127.0.0.1:8931/mcp')
        self.assertFalse(server.requires_egress)
        # 不因为内置就跳过连通性测试这一步
        self.assertEqual(server.status, 'unknown')
        self.assertEqual(server.enabled_tools, [])
        self.assertTrue(AuditLog.objects.filter(action='mcp_create',
                                                target='演示设备状态查询').exists())

    def test_seed_is_idempotent_and_preserves_runtime_state(self):
        call_command('init_builtin_mcp')
        server = MCPServer.objects.get(slug='demo-devices')
        server.status = 'healthy'
        server.enabled_tools = ['device_detail']
        server.save()

        call_command('init_builtin_mcp')
        server.refresh_from_db()
        self.assertEqual(MCPServer.objects.filter(is_builtin=True).count(), 1)
        self.assertEqual(server.status, 'healthy')
        self.assertEqual(server.enabled_tools, ['device_detail'])

    def test_disable_hides_builtin_source(self):
        call_command('init_builtin_mcp')
        call_command('init_builtin_mcp', '--disable')
        server = MCPServer.objects.get(slug='demo-devices')
        self.assertFalse(server.is_active)
        self.assertEqual(server.status, 'disabled')
        self.assertTrue(AuditLog.objects.filter(action='mcp_disable',
                                                target='演示设备状态查询').exists())
