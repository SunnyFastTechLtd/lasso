import os
import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.core.files.storage import default_storage
from rest_framework.test import APIClient

from backend.apps.users.models import AuditLog, User
from backend.apps.settings.models import SystemSetting


def _make_user(username, is_staff=False):
    return User.objects.create_user(username=username, password='demo-pass-9x2', is_staff=is_staff)


class LLMConfigReadTests(TestCase):
    """读取 LLM 配置：只来自数据库，掩码回显，环境变量不参与"""

    def setUp(self):
        from backend.apps.common import llm

        self.llm = llm
        self.llm.reset_client()
        self.addCleanup(self.llm.reset_client)
        self.user = _make_user('alice')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_get_without_db_config_returns_empty(self):
        """数据库没有配置时返回空值 + 缺失项，不回落到 settings/环境变量"""
        resp = self.client.get('/api/system/settings/llm/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn('api_key_masked', resp.data)
        self.assertNotIn('api_key', resp.data)  # 不泄露原文
        self.assertEqual(resp.data['base_url'], '')
        self.assertEqual(resp.data['model'], '')
        self.assertFalse(resp.data['configured'])
        self.assertEqual(
            resp.data['missing_fields'],
            ['llm_base_url', 'llm_api_key', 'llm_model'],
        )
        self.assertIn('系统设置', resp.data['hint'])
        # 可选推理参数用代码内默认值，不是环境变量
        self.assertEqual(resp.data['temperature'], 0.7)
        self.assertEqual(resp.data['max_tokens'], 4096)
        self.assertEqual(resp.data['timeout'], 120)

    def test_db_values_override_defaults(self):
        SystemSetting.set_value('llm_model', 'qwen-test')
        SystemSetting.set_value('llm_max_tokens', '8192')
        resp = self.client.get('/api/system/settings/llm/')
        self.assertEqual(resp.data['model'], 'qwen-test')
        self.assertEqual(resp.data['max_tokens'], 8192)

    def test_api_key_masked(self):
        SystemSetting.set_value('llm_api_key', 'abcdef123456')
        resp = self.client.get('/api/system/settings/llm/')
        self.assertEqual(resp.data['api_key_masked'], 'abc***456')

    def test_get_lists_verified_models(self):
        """A3 模型下拉候选来源：健康检查通过的模型随配置一起回显"""
        import json

        SystemSetting.set_value('llm_verified_models', json.dumps(['qwen3-32b', 'deepseek-r1']))
        resp = self.client.get('/api/system/settings/llm/')
        self.assertEqual(resp.data['verified_models'], ['qwen3-32b', 'deepseek-r1'])

    def test_get_tolerates_broken_verified_list(self):
        SystemSetting.set_value('llm_verified_models', 'not-json')
        resp = self.client.get('/api/system/settings/llm/')
        self.assertEqual(resp.data['verified_models'], [])

    def test_unauthenticated_rejected(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get('/api/system/settings/llm/').status_code, 401)


class ModelConfigSourceTests(TestCase):
    """模型配置只认数据库：env 不参与、完整档生效、缺字段明确报错"""

    def setUp(self):
        from backend.apps.common import llm

        self.llm = llm
        self.llm.reset_client()
        self.addCleanup(self.llm.reset_client)

    def test_env_is_ignored_when_db_empty(self):
        """env 里即使有完整模型配置，DB 无档也不采用"""
        with patch.dict('os.environ', {
            'LLM_BASE_URL': 'https://env.example.com/v1',
            'LLM_API_KEY': 'env-key',
            'LLM_MODEL': 'env-model',
            'LLM_TEMPERATURE': '1.2',
        }):
            self.assertIsNone(self.llm.get_llm_config('llm_model'))
            with self.assertRaises(self.llm.ModelConfigError) as ctx:
                self.llm.load_model_config()
            self.assertEqual(
                ctx.exception.missing,
                ['llm_base_url', 'llm_api_key', 'llm_model'],
            )
            self.assertIn('系统设置', str(ctx.exception))
            self.assertEqual(self.llm.check_health()['status'], 'not_configured')

    def test_db_complete_config_is_used(self):
        SystemSetting.set_value('llm_base_url', 'https://db.example.com/v1')
        SystemSetting.set_value('llm_api_key', 'db-key')
        SystemSetting.set_value('llm_model', 'db-model')
        SystemSetting.set_value('llm_temperature', '0.3')

        cfg = self.llm.load_model_config()
        self.assertEqual(cfg['base_url'], 'https://db.example.com/v1')
        self.assertEqual(cfg['api_key'], 'db-key')
        self.assertEqual(cfg['model'], 'db-model')
        self.assertEqual(cfg['temperature'], 0.3)
        # 未填的推理参数用代码内默认值
        self.assertEqual(cfg['max_tokens'], 4096)
        self.assertEqual(cfg['timeout'], 120)

    def test_db_missing_required_field_raises(self):
        """激活档缺必填字段时明确报错，不静默回落到别处配置"""
        SystemSetting.set_value('llm_base_url', 'https://db.example.com/v1')
        SystemSetting.set_value('llm_model', 'db-model')
        with self.assertRaises(self.llm.ModelConfigError) as ctx:
            self.llm.load_model_config()
        self.assertEqual(ctx.exception.missing, ['llm_api_key'])
        self.assertIn('API Key', str(ctx.exception))
        self.assertIn('系统设置', str(ctx.exception))


class LLMConfigUpdateTests(TestCase):
    """更新 LLM 配置（F1）：先按候选配置做健康检查，通过才落库并写入可用模型列表"""

    def setUp(self):
        self.admin = _make_user('admin', is_staff=True)
        self.user = _make_user('alice')
        self.client = APIClient()
        self.payload = {
            'base_url': 'http://127.0.0.1:11434/v1',
            'api_key': 'test-key',
            'model': 'qwen3.8:latest',
            'max_tokens': 4096,
        }

    @patch('backend.apps.settings.views.probe_model', return_value={'status': 'ok'})
    @patch('backend.apps.settings.views.reset_client')
    def test_admin_can_update(self, mock_reset, mock_health):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post('/api/system/settings/llm/update/', self.payload, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['health']['status'], 'ok')
        self.assertTrue(resp.data['saved'])

        self.assertEqual(SystemSetting.get_value('llm_model'), 'qwen3.8:latest')
        self.assertEqual(SystemSetting.get_value('llm_max_tokens'), '4096')
        mock_reset.assert_called_once()

    @patch('backend.apps.settings.views.probe_model', return_value={'status': 'ok'})
    @patch('backend.apps.settings.views.reset_client')
    def test_update_creates_audit_log(self, mock_reset, mock_health):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post('/api/system/settings/llm/update/', self.payload, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        log = AuditLog.objects.filter(action='llm_update', target='LLM配置').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.actor, self.admin)
        self.assertIn('qwen3.8:latest', log.detail)

    def test_health_check_uses_candidate_config_before_saving(self):
        """检查用的是候选配置，且此刻库里还是旧值（先检查后落库）"""
        SystemSetting.set_value('llm_model', 'old-model')
        captured = {}

        def fake_health(candidate=None):
            captured['candidate'] = dict(candidate or {})
            captured['model_in_db'] = SystemSetting.get_value('llm_model')
            return {'status': 'ok', 'verified_model': candidate['model']}

        with patch('backend.apps.settings.views.probe_model', side_effect=fake_health), \
                patch('backend.apps.settings.views.reset_client'):
            self.client.force_authenticate(user=self.admin)
            resp = self.client.post('/api/system/settings/llm/update/', self.payload, format='json')
            self.assertEqual(resp.status_code, 200, resp.content)

        self.assertEqual(captured['candidate']['model'], 'qwen3.8:latest')
        self.assertEqual(captured['candidate']['api_key'], 'test-key')
        self.assertEqual(captured['model_in_db'], 'old-model')

    @patch('backend.apps.settings.views.probe_model',
           return_value={'status': 'unauthorized', 'base_url': 'http://x/v1', 'error': 'API Key 无效'})
    @patch('backend.apps.settings.views.reset_client')
    def test_failed_health_check_saves_nothing(self, mock_reset, mock_health):
        """未通过检查：不落库、不重置 client、不写可用列表，审计写明未保存"""
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post('/api/system/settings/llm/update/', self.payload, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.data['saved'])
        self.assertEqual(resp.data['health']['status'], 'unauthorized')

        self.assertEqual(SystemSetting.get_value('llm_model'), '')
        self.assertEqual(SystemSetting.get_value('llm_base_url'), '')
        self.assertEqual(SystemSetting.get_value('llm_verified_models'), '')
        mock_reset.assert_not_called()

        log = AuditLog.objects.filter(action='llm_update', target='LLM配置').first()
        self.assertIn('未保存', log.detail)
        self.assertIn('API Key 无效', log.detail)

    @patch('backend.apps.settings.views.probe_model',
           return_value={'status': 'ok', 'verified_model': 'qwen3-32b'})
    @patch('backend.apps.settings.views.reset_client')
    def test_passed_model_enters_verified_list(self, mock_reset, mock_health):
        """通过检查的模型写入 llm_verified_models，装配页下拉不再恒回落主模型"""
        from backend.apps.agenthub import services

        self.client.force_authenticate(user=self.admin)
        resp = self.client.post('/api/system/settings/llm/update/', self.payload, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn('qwen3-32b', services.verified_models())
        self.assertTrue(services.model_available('qwen3-32b'))

    @patch('backend.apps.settings.views.probe_model',
           return_value={'status': 'ok', 'verified_model': 'qwen3-32b'})
    @patch('backend.apps.settings.views.reset_client')
    def test_verified_list_keeps_history_and_dedupes(self, mock_reset, mock_health):
        import json

        SystemSetting.set_value('llm_verified_models', json.dumps(['deepseek-r1']))
        self.client.force_authenticate(user=self.admin)
        self.client.post('/api/system/settings/llm/update/', self.payload, format='json')
        self.client.post('/api/system/settings/llm/update/', self.payload, format='json')

        names = json.loads(SystemSetting.get_value('llm_verified_models'))
        self.assertEqual(names.count('qwen3-32b'), 1)
        self.assertIn('deepseek-r1', names)

    def test_normal_user_forbidden(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post('/api/system/settings/llm/update/', self.payload, format='json')
        self.assertEqual(resp.status_code, 403)

    def test_max_tokens_too_small_rejected(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post('/api/system/settings/llm/update/', {
            **self.payload, 'max_tokens': 128,
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        # 校验失败不落库
        self.assertEqual(SystemSetting.get_value('llm_model'), '')


class LLMConfigTestResetTests(TestCase):
    """测试连接 + 重置默认配置"""

    def setUp(self):
        self.admin = _make_user('admin', is_staff=True)
        self.user = _make_user('normal')
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    @patch('backend.apps.settings.views.probe_model', return_value={'status': 'offline', 'error': '拒绝连接'})
    def test_test_connection(self, _health):
        resp = self.client.post('/api/system/settings/llm/test/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'offline')
        # 未通过的接入不进可用列表
        self.assertEqual(SystemSetting.get_value('llm_verified_models'), '')

    @patch('backend.apps.settings.views.probe_model',
           return_value={'status': 'ok', 'verified_model': 'qwen-test'})
    def test_test_connection_marks_verified(self, _health):
        resp = self.client.post('/api/system/settings/llm/test/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('qwen-test', SystemSetting.get_value('llm_verified_models'))

    @patch('backend.apps.settings.views.reset_client')
    def test_reset_deletes_llm_keys(self, mock_reset):
        SystemSetting.set_value('llm_model', 'qwen-test')
        SystemSetting.set_value('llm_max_tokens', '8192')
        SystemSetting.set_value('llm_verified_models', '["qwen-test"]')
        SystemSetting.set_value('other_key', 'keep')
        resp = self.client.post('/api/system/settings/llm/reset/')
        self.assertEqual(resp.status_code, 200, resp.content)

        self.assertEqual(SystemSetting.get_value('llm_model'), '')
        self.assertEqual(SystemSetting.get_value('llm_max_tokens'), '')
        # 可用列表随接入一起失效
        self.assertEqual(SystemSetting.get_value('llm_verified_models'), '')
        # 非 llm_ 前缀的键不受影响
        self.assertEqual(SystemSetting.get_value('other_key'), 'keep')
        mock_reset.assert_called_once()

    @patch('backend.apps.settings.views.reset_client')
    def test_reset_creates_audit_log(self, mock_reset):
        resp = self.client.post('/api/system/settings/llm/reset/')
        self.assertEqual(resp.status_code, 200, resp.content)
        log = AuditLog.objects.filter(action='llm_reset', target='LLM配置').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.actor, self.admin)

    def test_normal_user_cannot_probe_model(self):
        """探测会打外部请求并写可用模型列表，普通用户不能触发"""
        self.client.force_authenticate(user=self.user)
        with patch('backend.apps.settings.views.probe_model') as probe:
            resp = self.client.post('/api/system/settings/llm/test/')
        self.assertEqual(resp.status_code, 403)
        probe.assert_not_called()
        self.assertEqual(SystemSetting.get_value('llm_verified_models'), '')

    def test_admin_probe_still_works(self):
        """收紧后管理员探测不受影响（配置页「测试连接」仍可用）"""
        with patch('backend.apps.settings.views.probe_model',
                   return_value={'status': 'ok', 'verified_model': 'qwen-ok'}):
            resp = self.client.post('/api/system/settings/llm/test/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn('qwen-ok', SystemSetting.get_value('llm_verified_models'))


class LLMConfigCacheTTLTests(TestCase):
    """LLM 配置缓存 TTL：多 worker 下其他进程最长 TTL 秒后读到新配置"""

    def setUp(self):
        from backend.apps.common import llm
        self.llm = llm
        self.llm.reset_client()
        self.addCleanup(self.llm.reset_client)

    def test_cache_used_within_ttl(self):
        SystemSetting.set_value('llm_model', 'model-v1')
        self.assertEqual(self.llm.get_llm_config('llm_model'), 'model-v1')

        # TTL 内改 DB：仍返回缓存旧值（不重查库）
        SystemSetting.set_value('llm_model', 'model-v2')
        self.assertEqual(self.llm.get_llm_config('llm_model'), 'model-v1')

    def test_cache_expires_after_ttl(self):
        SystemSetting.set_value('llm_model', 'model-v1')
        self.assertEqual(self.llm.get_llm_config('llm_model'), 'model-v1')

        SystemSetting.set_value('llm_model', 'model-v2')
        # 推进单调时钟越过 TTL：下次读取重新查库拿到新值
        with patch('backend.apps.common.llm.time.monotonic') as mock_clock:
            mock_clock.return_value = self.llm._config_loaded_at + self.llm._CONFIG_CACHE_TTL + 1
            self.assertEqual(self.llm.get_llm_config('llm_model'), 'model-v2')


class LLMProbeModelTests(TestCase):
    """probe_model(candidate)：按候选配置做深度检查，不碰单例客户端与现网配置"""

    def setUp(self):
        from backend.apps.common import llm
        self.llm = llm
        self.llm.reset_client()
        self.addCleanup(self.llm.reset_client)
        SystemSetting.set_value('llm_base_url', 'http://old/v1')
        SystemSetting.set_value('llm_api_key', 'old-key')
        SystemSetting.set_value('llm_model', 'old-model')
        self.llm.reset_client()

    def test_candidate_overrides_saved_config(self):
        seen = {}

        class FakeCompletions:
            def create(self, **kwargs):
                seen['model'] = kwargs['model']
                return object()

        class FakeChat:
            completions = FakeCompletions()

        class FakeModels:
            data = []

        class FakeClient:
            def __init__(self, base_url=None, api_key=None, timeout=None):
                seen['base_url'], seen['api_key'], seen['timeout'] = base_url, api_key, timeout
                self.chat = FakeChat()
                self.models = self

            def list(self):
                return FakeModels()

        with patch.object(self.llm, 'OpenAI', FakeClient):
            health = self.llm.probe_model({
                'base_url': 'http://new/v1', 'api_key': 'new-key', 'model': 'new-model', 'timeout': 60,
            })

        self.assertEqual(health['status'], 'ok')
        self.assertEqual(health['verified_model'], 'new-model')
        self.assertEqual(seen['base_url'], 'http://new/v1')
        self.assertEqual(seen['api_key'], 'new-key')
        self.assertEqual(seen['model'], 'new-model')
        # 探测不改写现网配置
        self.assertEqual(SystemSetting.get_value('llm_model'), 'old-model')

    def test_connection_error_maps_to_offline(self):
        class ConnError(self.llm.APIConnectionError):
            def __init__(self, message):
                Exception.__init__(self, message)

        class Boom:
            def __init__(self, **kwargs):
                self.chat = self
                self.completions = self

            def create(self, **kwargs):
                raise ConnError('拒绝连接')

        with patch.object(self.llm, 'OpenAI', Boom):
            health = self.llm.probe_model({'model': 'new-model'})
        self.assertEqual(health['status'], 'offline')
        self.assertIn('拒绝连接', health['error'])

    def test_missing_model_name_is_error(self):
        """候选+库内都缺模型名：明确报未配置，不回落到任何默认"""
        SystemSetting.set_value('llm_model', '')
        self.llm.reset_client()
        health = self.llm.probe_model({'base_url': 'http://new/v1'})
        self.assertEqual(health['status'], 'not_configured')
        self.assertIn('模型名', health['error'])
        self.assertIn('系统设置', health['error'])
        self.assertIn('llm_model', health['missing_fields'])

    def test_session_health_check_stays_light(self):
        """会话每轮开头都调 check_health，它不能改成真实补全（否则每条消息多付一次推理）"""
        class NoCompletions:
            """对话接口缺失：轻探测只用清单，不应触碰 chat"""

            def __getattr__(self, name):
                raise AssertionError(f'check_health 不应调用 chat.{name}')

        class FakeModels:
            @staticmethod
            def list():
                class R:
                    data = []
                return R()

        class FakeClient:
            chat = type('NoChat', (), {'completions': NoCompletions()})()
            models = FakeModels()

        with patch.object(self.llm, 'get_client', return_value=FakeClient()):
            health = self.llm.check_health()

        self.assertEqual(health['status'], 'ok')
        self.assertNotIn('verified_model', health)


class BrandingTests(TestCase):
    """企业品牌配置：公开读取 + 管理员上传 + 校验"""

    def setUp(self):
        self.admin = _make_user('admin', is_staff=True)
        self.user = _make_user('alice')
        self.client = APIClient()

    def test_public_read_returns_defaults(self):
        """未认证可读取，且只返回 site_name/site_logo"""
        resp = self.client.get('/api/system/branding/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['site_name'], '商客AI一体机')
        self.assertEqual(resp.data['site_logo'], '')
        self.assertEqual(set(resp.data.keys()), {'site_name', 'site_logo'})

    def test_unauthenticated_cannot_update(self):
        resp = self.client.post('/api/system/branding/', {'site_name': '测试'})
        self.assertEqual(resp.status_code, 401)

    def test_normal_user_forbidden(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post('/api/system/branding/', {'site_name': '测试'})
        self.assertEqual(resp.status_code, 403)

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_admin_update_name_and_logo(self):
        self.client.force_authenticate(user=self.admin)
        logo = self._make_uploaded_file('logo.png', b'fake-png', size=1)
        resp = self.client.post(
            '/api/system/branding/',
            {'site_name': ' 测试企业 ', 'site_logo': logo},
            format='multipart',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['site_name'], '测试企业')
        self.assertTrue(resp.data['site_logo'].endswith('/media/branding/logo.png'))

        # 数据库持久化
        self.assertEqual(SystemSetting.get_value('site_name'), '测试企业')
        self.assertTrue(SystemSetting.get_value('site_logo').startswith('/media/branding/'))

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_update_name_without_logo_keeps_existing_logo(self):
        SystemSetting.set_value('site_logo', '/media/branding/old.png', '企业 LOGO')
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post('/api/system/branding/', {'site_name': '新名称'})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['site_name'], '新名称')
        self.assertIn('/media/branding/old.png', resp.data['site_logo'])

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_reject_oversized_logo(self):
        self.client.force_authenticate(user=self.admin)
        logo = self._make_uploaded_file('logo.png', b'x', size=600 * 1024)
        resp = self.client.post(
            '/api/system/branding/',
            {'site_name': '测试', 'site_logo': logo},
            format='multipart',
        )
        self.assertEqual(resp.status_code, 400)

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_reject_invalid_format(self):
        self.client.force_authenticate(user=self.admin)
        logo = self._make_uploaded_file('logo.txt', b'text')
        resp = self.client.post(
            '/api/system/branding/',
            {'site_name': '测试', 'site_logo': logo},
            format='multipart',
        )
        self.assertEqual(resp.status_code, 400)

    def _make_uploaded_file(self, name, content, size=None):
        from django.core.files.uploadedfile import SimpleUploadedFile
        if size is not None:
            content = (content * (size // len(content) + 1))[:size]
        return SimpleUploadedFile(name, content, content_type='image/png')



class ProxyEndpointApiTests(TestCase):
    """代理条目 CRUD / 测试：密码只写不读，写操作触发应用，权限仅管理员"""

    def setUp(self):
        from pathlib import Path

        from django.conf import settings as dj_settings
        from backend.apps.settings import proxies

        self.proxies = proxies
        self.tmpdir = tempfile.mkdtemp(prefix='lasso-proxy-test-')
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmpdir, ignore_errors=True))
        self.config_file = Path(self.tmpdir) / 'settings.yml'
        template = Path(dj_settings.BASE_DIR) / 'deploy' / 'searxng' / 'settings.yml'
        self.template = template
        self.old_config = template.read_text(encoding='utf-8').replace(
            '__SEARXNG_SECRET_KEY__', 'fixedsecret')
        self.config_file.write_text(self.old_config, encoding='utf-8')

        cm = self.settings(
            SEARXNG_SETTINGS_PATH=str(self.config_file),
            SEARXNG_BACKUP_DIR=str(Path(self.tmpdir) / 'backups'),
            SEARXNG_RESTART_CMD='true',
            SEARXNG_VENV_PYTHON='/bin/true',
        )
        cm.enable()
        self.addCleanup(cm.disable)
        proxies.clear_cache()
        self.addCleanup(proxies.clear_cache)

        for target, kwargs in (
            ('backend.apps.settings.proxies._validate', {'return_value': (True, '')}),
            ('backend.apps.settings.proxies._restart', {'return_value': (True, '')}),
            ('backend.apps.settings.proxies._wait_health', {'return_value': True}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.admin = _make_user('proxy-admin', is_staff=True)
        self.user = _make_user('proxy-user')
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def _create_proxy(self, **extra):
        payload = {
            'name': '内网出口', 'scheme': 'http', 'host': '192.0.2.10',
            'port': 7890, 'username': 'demo', 'password': 'p@ss&word',
            'enabled': True, 'note': '测试',
        }
        payload.update(extra)
        return self.client.post('/api/system/settings/proxies/', payload, format='json')

    def test_non_admin_cannot_read_or_write(self):
        self.assertEqual(self.client.get('/api/system/settings/proxies/').status_code, 200)
        self.client.force_authenticate(user=self.user)
        self.assertEqual(self.client.get('/api/system/settings/proxies/').status_code, 403)
        self.assertEqual(self.client.post('/api/system/settings/proxies/', {}, format='json').status_code, 403)
        self.assertEqual(self.client.get('/api/system/settings/proxy-bindings/').status_code, 403)

    def test_create_hides_password_and_marks_applied(self):
        resp = self._create_proxy()
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(resp.data['has_password'])
        self.assertNotIn('password', resp.data)
        self.assertNotIn('p@ss', str(resp.data))
        self.assertEqual(resp.data['display_url'], 'http://demo:***@192.0.2.10:7890')
        self.assertTrue(resp.data['apply']['applied'])
        self.assertTrue(AuditLog.objects.filter(action='proxy_create').exists())

        from backend.apps.settings.models import ProxyEndpoint
        endpoint = ProxyEndpoint.objects.get()
        self.assertEqual(endpoint.get_password(), 'p@ss&word')
        self.assertEqual(endpoint.url(), 'http://demo:p%40ss%26word@192.0.2.10:7890')

    def test_update_blank_password_keeps_old(self):
        created = self._create_proxy().data
        resp = self.client.patch(
            f"/api/system/settings/proxies/{created['id']}/",
            {'name': '改名', 'password': ''}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        from backend.apps.settings.models import ProxyEndpoint
        self.assertEqual(ProxyEndpoint.objects.get().get_password(), 'p@ss&word')
        self.assertTrue(AuditLog.objects.filter(action='proxy_update').exists())

    def test_reject_bad_host_and_password_without_username(self):
        self.assertEqual(self._create_proxy(host='http://x.com').status_code, 400)
        self.assertEqual(self._create_proxy(host='a/b').status_code, 400)
        resp = self._create_proxy(username='')
        self.assertEqual(resp.status_code, 400)

    def test_test_action_records_result_and_audit(self):
        created = self._create_proxy().data
        with patch('backend.apps.settings.proxies._test_http', return_value=(True, 'HTTP 200')):
            resp = self.client.post(f"/api/system/settings/proxies/{created['id']}/test/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data['ok'])
        self.assertEqual(resp.data['endpoint']['last_test_status'], 'ok')
        self.assertTrue(AuditLog.objects.filter(action='proxy_test').exists())

    def test_test_failure_maps_timeout(self):
        created = self._create_proxy().data
        with patch('backend.apps.settings.proxies._test_http', side_effect=TimeoutError('timed out')):
            resp = self.client.post(f"/api/system/settings/proxies/{created['id']}/test/")
        self.assertFalse(resp.data['ok'])
        self.assertEqual(resp.data['reason'], 'timeout')

    def test_delete_bound_proxy_is_rejected(self):
        created = self._create_proxy().data
        from backend.apps.settings.models import ProxyEndpoint, ResourceProxyBinding
        ResourceProxyBinding.objects.create(
            resource_type=ResourceProxyBinding.TYPE_SEARXNG_ENGINE,
            resource_key='duckduckgo', proxy_id=created['id'])
        resp = self.client.delete(f"/api/system/settings/proxies/{created['id']}/")
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertTrue(ProxyEndpoint.objects.filter(pk=created['id']).exists())


class SearxngRenderTests(TestCase):
    """模板渲染 / 应用 / 回滚：不联服务器，纯本地文件与打桩"""

    def setUp(self):
        from pathlib import Path
        from django.conf import settings as dj_settings
        from backend.apps.settings import proxies
        from backend.apps.settings.models import ProxyEndpoint

        self.proxies = proxies
        self.tmpdir = tempfile.mkdtemp(prefix='lasso-render-test-')
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmpdir, ignore_errors=True))
        self.config_file = Path(self.tmpdir) / 'settings.yml'
        template = Path(dj_settings.BASE_DIR) / 'deploy' / 'searxng' / 'settings.yml'
        self.old_config = template.read_text(encoding='utf-8').replace(
            '__SEARXNG_SECRET_KEY__', 'fixedsecret')
        self.config_file.write_text(self.old_config, encoding='utf-8')
        cm = self.settings(
            SEARXNG_SETTINGS_PATH=str(self.config_file),
            SEARXNG_BACKUP_DIR=str(Path(self.tmpdir) / 'backups'),
            SEARXNG_RESTART_CMD='true',
            SEARXNG_VENV_PYTHON='/bin/true',
        )
        cm.enable()
        self.addCleanup(cm.disable)
        proxies.clear_cache()
        self.addCleanup(proxies.clear_cache)
        self.proxy = ProxyEndpoint.objects.create(
            name='出口', scheme='http', host='192.0.2.10', port=3128
        )
        self.admin = _make_user('render-admin', is_staff=True)

    def _patch_apply(self, healthy=True):
        for target, kwargs in (
            ('backend.apps.settings.proxies._validate', {'return_value': (True, '')}),
            ('backend.apps.settings.proxies._restart', {'return_value': (True, '')}),
            ('backend.apps.settings.proxies._wait_health', {'return_value': healthy}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_render_keeps_secret_and_applies_engine_proxy(self):
        from backend.apps.settings.models import ResourceProxyBinding
        ResourceProxyBinding.objects.create(
            resource_type=ResourceProxyBinding.TYPE_SEARXNG_ENGINE,
            resource_key='duckduckgo', proxy=self.proxy)
        text = self.proxies.render_settings(self.old_config)
        self.assertIn('secret_key: "fixedsecret"', text)
        self.assertIn('proxies:\n      all://:\n        - http://192.0.2.10:3128', text)
        self.assertNotIn('        proxies:', text)  # 不能出现 8 空格缩进
        self.assertNotIn('# __PROXY_duckduckgo__', text)
        self.assertNotIn('__PROXY_', text)
        self.assertIn('display_type: ["infobox", "list"]', text)
        self.assertNotIn('- name: wikidata', text)

    def test_apply_writes_file_and_marks_bindings(self):
        from backend.apps.settings.models import ResourceProxyBinding
        self._patch_apply(healthy=True)
        binding = ResourceProxyBinding.objects.create(
            resource_type=ResourceProxyBinding.TYPE_SEARXNG_ENGINE,
            resource_key='wikipedia', proxy=self.proxy)
        result = self.proxies.apply_searxng(self.admin)
        self.assertTrue(result['applied'], result)
        self.assertIn('proxies:', self.config_file.read_text(encoding='utf-8'))
        binding.refresh_from_db()
        self.assertTrue(binding.applied)
        backups = list((self.config_file.parent / 'backups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertTrue(AuditLog.objects.filter(action='proxy_apply').exists())

    def test_apply_failure_rolls_back(self):
        from backend.apps.settings.models import ResourceProxyBinding
        self._patch_apply(healthy=False)
        ResourceProxyBinding.objects.create(
            resource_type=ResourceProxyBinding.TYPE_SEARXNG_ENGINE,
            resource_key='bing', proxy=self.proxy)
        result = self.proxies.apply_searxng(self.admin)
        self.assertFalse(result['applied'])
        self.assertEqual(result['error'], 'searxng_health_failed')
        self.assertEqual(self.config_file.read_text(encoding='utf-8'), self.old_config)
        self.assertEqual(self.proxies.load_apply_state().get('status'), 'failed')
        self.assertFalse(ResourceProxyBinding.objects.get().applied)

    def test_apply_without_config_file(self):
        self.config_file.unlink()
        result = self.proxies.apply_searxng(self.admin)
        self.assertFalse(result['applied'])
        self.assertEqual(result['error'], 'searxng_config_missing')


class TiangongProxyResolveTests(TestCase):
    """天工抓取解析：精确 > 最左通配（最长后缀）> 来源站 > 直连；socks5 不用于天工"""

    def setUp(self):
        from backend.apps.settings import proxies
        from backend.apps.settings.models import ProxyEndpoint, ResourceProxyBinding

        self.proxies = proxies
        proxies.clear_cache()
        self.addCleanup(proxies.clear_cache)
        self.http_proxy = ProxyEndpoint.objects.create(
            name='h', scheme='http', host='192.0.2.10', port=3128)
        self.socks_proxy = ProxyEndpoint.objects.create(
            name='s', scheme='socks5', host='192.0.2.11', port=1080)

    def _bind(self, key, proxy, kind='domain'):
        from backend.apps.settings.models import ResourceProxyBinding
        rtype = (ResourceProxyBinding.TYPE_TIANGONG_DOMAIN if kind == 'domain'
                 else ResourceProxyBinding.TYPE_TIANGONG_SOURCE)
        ResourceProxyBinding.objects.create(resource_type=rtype, resource_key=key, proxy=proxy)

    def test_exact_beats_wildcard_and_longest_wildcard_wins(self):
        self._bind('*.example.com', self.http_proxy)
        self._bind('*.sub.example.com', self.socks_proxy)
        self._bind('api.example.com', self.http_proxy)
        url, reason = self.proxies.tiangong_proxy_for_url('https://api.example.com/a')
        self.assertEqual(reason, 'domain_exact')
        self.assertEqual(url, 'http://192.0.2.10:3128')
        _, reason = self.proxies.tiangong_proxy_for_url('https://x.sub.example.com/a')
        self.assertEqual(reason, 'socks5_unsupported')
        self.assertIsNone(self.proxies.tiangong_proxy_for_url('https://other.com/a')[0])

    def test_wildcard_does_not_match_suffix_only_host(self):
        self._bind('*.example.com', self.http_proxy)
        self.assertIsNone(self.proxies.tiangong_proxy_for_url('https://notexample.com/')[0])
        self.assertEqual(
            self.proxies.tiangong_proxy_for_url('https://example.com/')[0],
            'http://192.0.2.10:3128')

    def test_disabled_proxy_or_binding_falls_back_to_direct(self):
        from backend.apps.settings.models import ResourceProxyBinding
        binding = ResourceProxyBinding.objects.create(
            resource_type=ResourceProxyBinding.TYPE_TIANGONG_DOMAIN,
            resource_key='example.com', proxy=self.http_proxy, enabled=False)
        self.assertIsNone(self.proxies.tiangong_proxy_for_url('https://example.com/')[0])
        binding.enabled = True
        binding.save()
        self.http_proxy.enabled = False
        self.http_proxy.save()
        self.proxies.clear_cache()
        self.assertIsNone(self.proxies.tiangong_proxy_for_url('https://example.com/')[0])


class NetProxyTests(TestCase):
    """http_get 代理参数：构建代理 opener；代理侧失败归类 proxy_error"""

    def test_http_get_uses_proxy_opener(self):
        from unittest.mock import MagicMock
        from backend.apps.nuwa import net

        response = MagicMock()
        response.read.return_value = b'ok'
        response.headers = {}
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch('backend.apps.nuwa.net._opener', return_value=opener) as build:
            raw, _ = net.http_get('https://example.com/', proxy='http://192.0.2.10:3128')
        self.assertEqual(raw, b'ok')
        build.assert_called_once_with('http://192.0.2.10:3128')

    def test_proxy_connection_error_is_proxy_error(self):
        from urllib.error import URLError
        from backend.apps.nuwa import net

        with patch('backend.apps.nuwa.net._opener') as build:
            build.return_value.open.side_effect = URLError('connection refused')
            with self.assertRaises(net.RemoteError) as ctx:
                net.http_get('https://example.com/', proxy='http://192.0.2.10:3128')
        self.assertEqual(ctx.exception.reason, 'proxy_error')
