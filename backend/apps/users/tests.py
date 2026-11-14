import hashlib
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import authenticate
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate
from rest_framework_simplejwt.tokens import RefreshToken

from backend.apps.users.models import AuditLog, Department, User, log_audit
from backend.apps.users.views import AuthViewSet


def _make_user(username, is_staff=False, department=''):
    return User.objects.create_user(
        username=username, password='demo-pass-9x2',
        is_staff=is_staff, department=department,
    )


class LogoutBlacklistTests(TestCase):
    """登出：refresh token 加入黑名单后不可再刷新"""

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='demo-pass-9x2')
        self.client = APIClient()

    def test_logout_blacklists_refresh_token(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        refresh = RefreshToken.for_user(self.user)
        self.client.force_authenticate(user=self.user)
        resp = self.client.post('/api/auth/logout/', {'refresh': str(refresh)}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        # 黑名单中的 refresh token 再刷新应 401
        resp = self.client.post('/api/auth/refresh/', {'refresh': str(refresh)}, format='json')
        self.assertEqual(resp.status_code, 401)


class RegisterAPITests(TestCase):
    """注册接口：默认关闭，ALLOW_REGISTER=True 时开放"""

    def setUp(self):
        self.client = APIClient()

    def test_register_closed_by_default(self):
        resp = self.client.post('/api/auth/register/', {
            'username': 'hacker', 'password': 'demo-pass-9x2',
        }, format='json')
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertFalse(User.objects.filter(username='hacker').exists())

    @override_settings(ALLOW_REGISTER=True)
    def test_register_open_when_enabled(self):
        resp = self.client.post('/api/auth/register/', {
            'username': 'newbie', 'password': 'demo-pass-9x2', 'confirm_password': 'demo-pass-9x2',
        }, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(User.objects.filter(username='newbie').exists())
        self.assertIn('access', resp.data)


class LoginAPITests(TestCase):
    """登录：口令校验必须真实生效，DEBUG 下也不放行、不自动建号"""

    URL = '/api/auth/login/'

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='alice', password='demo-pass-9x2')
        # 登录失败计数在缓存里跨用例保留，先清掉避免相互影响
        cache.clear()

    def _login(self, username, password='whatever'):
        return self.client.post(
            self.URL, {'username': username, 'password': password}, format='json')

    def test_correct_password_returns_tokens(self):
        resp = self._login('alice', 'demo-pass-9x2')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn('access', resp.data)
        self.assertIn('refresh', resp.data)
        self.assertEqual(resp.data['user']['username'], 'alice')

    def test_wrong_password_rejected(self):
        resp = self._login('alice', 'wrong-pass')
        self.assertEqual(resp.status_code, 401, resp.content)
        self.assertNotIn('access', resp.data)
        self.assertNotIn('refresh', resp.data)

    def test_unknown_username_rejected_without_creating_user(self):
        before = User.objects.count()
        resp = self._login('ghost', 'demo-pass-9x2')
        self.assertEqual(resp.status_code, 401, resp.content)
        self.assertEqual(User.objects.count(), before)
        self.assertFalse(User.objects.filter(username='ghost').exists())

    def test_inactive_user_rejected(self):
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        resp = self._login('alice', 'demo-pass-9x2')
        self.assertEqual(resp.status_code, 401, resp.content)
        self.assertNotIn('access', resp.data)

    @override_settings(DEBUG=True)
    def test_debug_true_does_not_bypass_password_check(self):
        resp = self._login('alice', 'wrong-pass')
        self.assertEqual(resp.status_code, 401, resp.content)
        self.assertNotIn('access', resp.data)

    @override_settings(DEBUG=True)
    def test_debug_true_does_not_auto_create_or_grant_staff(self):
        before = User.objects.count()
        resp = self._login('admin', 'anything')
        self.assertEqual(resp.status_code, 401, resp.content)
        self.assertEqual(User.objects.count(), before)
        self.assertFalse(User.objects.filter(username='admin').exists())


class LoginAuditTests(TestCase):
    """登录成功/失败留痕：用户名、结果、IP、User-Agent；成功更新 last_login"""

    URL = '/api/auth/login/'

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create_user(username='alice', password='demo-pass-9x2')

    def _post(self, username, password, **extra):
        extra.setdefault('HTTP_USER_AGENT', 'LassoTest/1.0')
        return self.client.post(
            self.URL, {'username': username, 'password': password},
            format='json', **extra)

    def test_login_success_audit_and_last_login(self):
        self.assertIsNone(self.user.last_login)
        resp = self._post('alice', 'demo-pass-9x2')
        self.assertEqual(resp.status_code, 200, resp.content)

        log = AuditLog.objects.get(action='login_success')
        self.assertEqual(log.actor, self.user)
        self.assertEqual(log.target, 'alice')
        self.assertEqual(log.ip, '127.0.0.1')
        self.assertEqual(log.user_agent, 'LassoTest/1.0')
        self.assertIn('success', log.detail)

        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.last_login)

    def test_login_failure_audit_fields(self):
        resp = self._post('alice', 'wrong-pass')
        self.assertEqual(resp.status_code, 401, resp.content)

        log = AuditLog.objects.get(action='login_failed')
        self.assertEqual(log.actor, self.user)
        self.assertEqual(log.target, 'alice')
        self.assertEqual(log.ip, '127.0.0.1')
        self.assertEqual(log.user_agent, 'LassoTest/1.0')
        self.assertIn('bad_credentials', log.detail)

        self.user.refresh_from_db()
        self.assertIsNone(self.user.last_login)

    def test_unknown_username_failure_has_no_actor(self):
        resp = self._post('ghost', 'whatever')
        self.assertEqual(resp.status_code, 401, resp.content)

        log = AuditLog.objects.get(action='login_failed')
        self.assertIsNone(log.actor)
        self.assertEqual(log.target, 'ghost')

    def test_forged_xff_ignored_without_trusted_proxy(self):
        resp = self._post(
            'alice', 'demo-pass-9x2', HTTP_X_FORWARDED_FOR='203.0.113.9')
        self.assertEqual(resp.status_code, 200, resp.content)

        log = AuditLog.objects.get(action='login_success')
        # 未配置可信代理时，客户端自带的 XFF 不参与取值
        self.assertEqual(log.ip, '127.0.0.1')

    @override_settings(AUTH_TRUSTED_PROXIES=['127.0.0.1'])
    def test_xff_parsed_only_for_trusted_proxy(self):
        resp = self._post(
            'alice', 'demo-pass-9x2',
            HTTP_X_FORWARDED_FOR='198.51.100.7, 203.0.113.9')
        self.assertEqual(resp.status_code, 200, resp.content)

        log = AuditLog.objects.get(action='login_success')
        # 从右向左剔除可信代理，取第一个不可信跳数；
        # 客户端预置在最左侧的 198.51.100.7 不能冒充真实来源
        self.assertEqual(log.ip, '203.0.113.9')


class LoginThrottleTests(TestCase):
    """登录失败按「用户名 + IP」节流；锁定期内正确口令也被拒绝"""

    URL = '/api/auth/login/'

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.alice = User.objects.create_user(username='alice', password='demo-pass-9x2')
        User.objects.create_user(username='bob', password='demo-pass-9x2')

    def _login(self, username, password):
        return self.client.post(
            self.URL, {'username': username, 'password': password}, format='json')

    @override_settings(LOGIN_FAILURE_LIMIT=3, LOGIN_FAILURE_WINDOW_SECONDS=60)
    def test_failures_lock_and_block_correct_password(self):
        for _ in range(2):
            self.assertEqual(self._login('alice', 'bad').status_code, 401)

        # 第 3 次失败达到阈值，明确返回 429 而不是静默 401
        resp = self._login('alice', 'bad')
        self.assertEqual(resp.status_code, 429, resp.content)
        self.assertEqual(resp.data['detail'], '尝试次数过多，请稍后再试')

        # 锁定期内即使是正确口令也进不去
        resp = self._login('alice', 'demo-pass-9x2')
        self.assertEqual(resp.status_code, 429, resp.content)
        self.assertTrue(AuditLog.objects.filter(
            action='login_failed', detail__contains='locked').exists())

    @override_settings(LOGIN_FAILURE_LIMIT=2, LOGIN_FAILURE_WINDOW_SECONDS=1)
    def test_lock_expires_and_login_recovers(self):
        self.assertEqual(self._login('alice', 'bad').status_code, 401)
        self.assertEqual(self._login('alice', 'bad').status_code, 429)
        self.assertEqual(self._login('alice', 'demo-pass-9x2').status_code, 429)

        time.sleep(1.2)
        self.assertEqual(self._login('alice', 'demo-pass-9x2').status_code, 200)

    @override_settings(LOGIN_FAILURE_LIMIT=2, LOGIN_FAILURE_WINDOW_SECONDS=60)
    def test_lock_is_scoped_to_username_and_ip(self):
        self.assertEqual(self._login('alice', 'bad').status_code, 401)
        self.assertEqual(self._login('alice', 'bad').status_code, 429)

        # 其它用户名不受同一 IP 的锁定影响
        self.assertEqual(self._login('bob', 'demo-pass-9x2').status_code, 200)


class AuthActionAuditTests(TestCase):
    """登出 / 改密 / 管理员重置密码 / 启停用的动作留痕"""

    def setUp(self):
        self.admin = _make_user('admin', is_staff=True)
        self.alice = _make_user('alice')
        self.client = APIClient()

    def test_logout_audit(self):
        self.client.force_authenticate(user=self.alice)
        resp = self.client.post('/api/auth/logout/', {}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)

        log = AuditLog.objects.get(action='logout')
        self.assertEqual(log.actor, self.alice)
        self.assertEqual(log.target, 'alice')
        self.assertEqual(log.ip, '127.0.0.1')

    def test_change_password_audit(self):
        self.client.force_authenticate(user=self.alice)
        resp = self.client.post('/api/auth/change-password/', {
            'old_password': 'demo-pass-9x2',
            'new_password': 'newpass66',
            'confirm_password': 'newpass66',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)

        log = AuditLog.objects.get(action='password_change')
        self.assertEqual(log.actor, self.alice)
        self.assertEqual(log.target, 'alice')

    def test_admin_reset_password_audits_and_forces_logout(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.patch(f'/api/auth/manage/{self.alice.id}/', {
            'password': 'Str0ngPass!',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)

        actions = set(AuditLog.objects.filter(target='alice')
                      .values_list('action', flat=True))
        self.assertIn('password_reset', actions)
        self.assertIn('force_logout', actions)

    def test_disable_user_audits_and_revokes_refresh(self):
        old_refresh = str(RefreshToken.for_user(self.alice))
        self.client.force_authenticate(user=self.admin)
        resp = self.client.patch(f'/api/auth/manage/{self.alice.id}/', {
            'is_active': False,
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)

        actions = set(AuditLog.objects.filter(target='alice')
                      .values_list('action', flat=True))
        self.assertIn('user_disable', actions)
        self.assertIn('force_logout', actions)

        resp = APIClient().post(
            '/api/auth/refresh/', {'refresh': old_refresh}, format='json')
        self.assertEqual(resp.status_code, 401, resp.content)

    def test_enable_user_audit(self):
        self.alice.is_active = False
        self.alice.save(update_fields=['is_active'])
        self.client.force_authenticate(user=self.admin)
        resp = self.client.patch(f'/api/auth/manage/{self.alice.id}/', {
            'is_active': True,
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)

        self.assertTrue(AuditLog.objects.filter(
            action='user_enable', target='alice').exists())


class AuthProtectedEndpointTests(TestCase):
    """受保护接口：匿名必须 401（不能 200，也不能因 AnonymousUser 落到 500）"""

    def setUp(self):
        self.client = APIClient()

    def test_anonymous_profile_rejected(self):
        resp = self.client.get('/api/auth/profile/')
        self.assertEqual(resp.status_code, 401, resp.content)

    def test_anonymous_profile_update_rejected(self):
        resp = self.client.patch('/api/auth/profile/', {'nickname': 'x'}, format='json')
        self.assertEqual(resp.status_code, 401, resp.content)

    def test_anonymous_change_password_rejected(self):
        resp = self.client.post('/api/auth/change-password/', {
            'old_password': 'demo-pass-9x2',
            'new_password': 'newpass66',
            'confirm_password': 'newpass66',
        }, format='json')
        self.assertEqual(resp.status_code, 401, resp.content)

    def test_authenticated_profile_ok(self):
        user = User.objects.create_user(username='alice', password='demo-pass-9x2')
        self.client.force_authenticate(user=user)
        resp = self.client.get('/api/auth/profile/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['username'], 'alice')

    def test_authenticated_can_change_password(self):
        user = User.objects.create_user(username='alice', password='demo-pass-9x2')
        self.client.force_authenticate(user=user)
        resp = self.client.post('/api/auth/change-password/', {
            'old_password': 'demo-pass-9x2',
            'new_password': 'newpass66',
            'confirm_password': 'newpass66',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        user.refresh_from_db()
        self.assertTrue(user.check_password('newpass66'))


class ProfileDepartmentTests(TestCase):
    """资料接口不允许普通用户改部门；部门调整只走管理端且要存在性校验"""

    def setUp(self):
        self.department = Department.objects.create(name='研发部')
        self.user = _make_user('alice', department='研发部')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_profile_update_cannot_change_department(self):
        resp = self.client.patch('/api/auth/profile/', {
            'nickname': '爱丽丝', 'department': '市场部',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.user.refresh_from_db()
        self.assertEqual(self.user.nickname, '爱丽丝')
        self.assertEqual(self.user.department, '研发部')

    def test_profile_update_cannot_set_nonexistent_department(self):
        resp = self.client.patch('/api/auth/profile/', {
            'department': '不存在的部门',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.user.refresh_from_db()
        self.assertEqual(self.user.department, '研发部')

    def test_profile_get_still_exposes_department(self):
        resp = self.client.get('/api/auth/profile/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['department'], '研发部')


class AvatarRetiredTests(TestCase):
    """头像功能已停用：资料接口不再读写 avatar，字段本身保留在模型里"""

    def setUp(self):
        self.user = _make_user('alice')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_profile_does_not_expose_avatar(self):
        resp = self.client.get('/api/auth/profile/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertNotIn('avatar', resp.data)

    def test_profile_update_ignores_avatar(self):
        upload = SimpleUploadedFile(
            'avatar.png', b'\x89PNG\r\n\x1a\n', content_type='image/png')
        resp = self.client.patch(
            '/api/auth/profile/',
            {'nickname': '爱丽丝', 'avatar': upload},
            format='multipart',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertNotIn('avatar', resp.data)
        self.user.refresh_from_db()
        self.assertFalse(self.user.avatar)
        self.assertEqual(self.user.nickname, '爱丽丝')


class ChangePasswordHardeningTests(TestCase):
    """修改密码：强度校验走 Django 统一口径；成功后吊销该用户全部 refresh token"""

    URL = '/api/auth/change-password/'

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='alice', password='demo-pass-9x2')
        self.client.force_authenticate(user=self.user)

    def _change(self, old='demo-pass-9x2', new='newpass66', confirm=None):
        return self.client.post(self.URL, {
            'old_password': old,
            'new_password': new,
            'confirm_password': new if confirm is None else confirm,
        }, format='json')

    def test_weak_numeric_password_rejected(self):
        resp = self._change(new='12345678')
        self.assertEqual(resp.status_code, 400, resp.content)
        # 项目的异常层会把字段级错误拼进 detail，这里只校验确实是 new_password 被拒
        self.assertIn('new_password', str(resp.data))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('demo-pass-9x2'))

    def test_common_password_rejected(self):
        resp = self._change(new='password')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('demo-pass-9x2'))

    def test_password_too_similar_to_username_rejected(self):
        # UserAttributeSimilarityValidator：与用户名相似度达到 0.7 即拒绝
        resp = self._change(new='alice1234')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('demo-pass-9x2'))

    def test_rejected_change_does_not_revoke_tokens(self):
        refresh = RefreshToken.for_user(self.user)
        resp = self._change(new='12345678')
        self.assertEqual(resp.status_code, 400, resp.content)
        probe = self.client.post('/api/auth/refresh/', {'refresh': str(refresh)}, format='json')
        self.assertEqual(probe.status_code, 200, probe.content)

    def test_change_password_revokes_all_refresh_tokens(self):
        device_a = RefreshToken.for_user(self.user)
        device_b = RefreshToken.for_user(self.user)
        resp = self._change()
        self.assertEqual(resp.status_code, 200, resp.content)

        for token in (device_a, device_b):
            probe = self.client.post('/api/auth/refresh/', {'refresh': str(token)}, format='json')
            self.assertEqual(probe.status_code, 401, probe.content)

    def test_change_password_reissues_current_session(self):
        """改密后当前设备拿到新 token：其它设备下线，但自己不会被踢掉"""
        old_access = str(RefreshToken.for_user(self.user).access_token)
        resp = self._change()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn('access', resp.data)
        self.assertIn('refresh', resp.data)

        # 新 refresh 可续签，新 access 可访问受保护接口
        probe = APIClient()
        renewed = probe.post(
            '/api/auth/refresh/', {'refresh': resp.data['refresh']}, format='json')
        self.assertEqual(renewed.status_code, 200, renewed.content)
        probe.credentials(HTTP_AUTHORIZATION='Bearer ' + renewed.data['access'])
        self.assertEqual(probe.get('/api/auth/profile/').status_code, 200)

        # 改密前签发的 access 仍是短时效无状态 token，不在本测试断言范围内；
        # 这里只确认改密响应没有把旧 refresh 原样回显
        self.assertNotEqual(resp.data['access'], old_access)

    def test_new_password_can_login(self):
        self.assertEqual(self._change().status_code, 200)
        self.client.force_authenticate(user=None)
        resp = self.client.post('/api/auth/login/', {
            'username': 'alice', 'password': 'newpass66',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)


class RefreshTokenSerializerTests(TestCase):
    """刷新 token 走 TokenRefreshSerializer：轮换与黑名单是真实生效而非只写在配置里"""

    URL = '/api/auth/refresh/'

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='alice', password='demo-pass-9x2')

    def test_rotation_config_is_enabled(self):
        self.assertTrue(settings.SIMPLE_JWT['ROTATE_REFRESH_TOKENS'])
        self.assertTrue(settings.SIMPLE_JWT['BLACKLIST_AFTER_ROTATION'])

    def test_refresh_rotates_and_blacklists_old_token(self):
        old = RefreshToken.for_user(self.user)
        resp = self.client.post(self.URL, {'refresh': str(old)}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn('access', resp.data)
        self.assertIn('refresh', resp.data)
        # 轮换：返回的是新 refresh，不是原样回显
        self.assertNotEqual(resp.data['refresh'], str(old))

        # 新 access 可用
        probe = APIClient()
        probe.credentials(HTTP_AUTHORIZATION='Bearer ' + resp.data['access'])
        self.assertEqual(probe.get('/api/auth/profile/').status_code, 200)

        # 旧 refresh 已被拉黑，不能重复使用
        again = self.client.post(self.URL, {'refresh': str(old)}, format='json')
        self.assertEqual(again.status_code, 401, again.content)

        # 新 refresh 可继续轮换
        nxt = self.client.post(self.URL, {'refresh': resp.data['refresh']}, format='json')
        self.assertEqual(nxt.status_code, 200, nxt.content)

    def test_invalid_refresh_rejected(self):
        resp = self.client.post(self.URL, {'refresh': 'not-a-token'}, format='json')
        self.assertEqual(resp.status_code, 401, resp.content)

    def test_blacklisted_refresh_rejected(self):
        token = RefreshToken.for_user(self.user)
        token.blacklist()
        resp = self.client.post(self.URL, {'refresh': str(token)}, format='json')
        self.assertEqual(resp.status_code, 401, resp.content)

    def test_missing_refresh_field_rejected(self):
        resp = self.client.post(self.URL, {}, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)


class _FutureAuthViewSet(AuthViewSet):
    """模拟后续新增的 action：不声明 permission_classes"""

    @action(detail=False, methods=['get'])
    def future_action(self, request):
        return Response({'ok': True})


class AuthDefaultPermissionTests(TestCase):
    """AuthViewSet 默认受保护：新增 action 漏声明权限也不会匿名放行"""

    def test_class_default_is_authenticated(self):
        self.assertEqual(AuthViewSet.permission_classes, [IsAuthenticated])

    def test_new_action_without_declaration_rejects_anonymous(self):
        view = _FutureAuthViewSet.as_view({'get': 'future_action'})
        response = view(APIRequestFactory().get('/api/auth/future-action/'))
        self.assertEqual(response.status_code, 401, response.data)

    def test_new_action_without_declaration_allows_authenticated(self):
        user = User.objects.create_user(username='alice', password='demo-pass-9x2')
        request = APIRequestFactory().get('/api/auth/future-action/')
        force_authenticate(request, user=user)
        response = _FutureAuthViewSet.as_view({'get': 'future_action'})(request)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, {'ok': True})


class AnonymousEntryPointTests(TestCase):
    """收口不能收过头：确需匿名的入口仍可访问"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='alice', password='demo-pass-9x2')

    def test_anonymous_login_still_allowed(self):
        resp = self.client.post(
            '/api/auth/login/', {'username': 'alice', 'password': 'demo-pass-9x2'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_anonymous_refresh_still_allowed(self):
        refresh = RefreshToken.for_user(self.user)
        resp = self.client.post('/api/auth/refresh/', {'refresh': str(refresh)}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn('access', resp.data)
        # 刷新出的 access 确实可用
        probe = APIClient()
        probe.credentials(HTTP_AUTHORIZATION='Bearer ' + resp.data['access'])
        self.assertEqual(probe.get('/api/auth/profile/').status_code, 200)

    def test_anonymous_logout_now_requires_auth(self):
        resp = self.client.post('/api/auth/logout/', {'refresh': 'x'}, format='json')
        self.assertEqual(resp.status_code, 401, resp.content)


class JwtSigningKeyTests(TestCase):
    """JWT 签名密钥：JWT_SIGNING_KEY 生效时独立于 SECRET_KEY，未设置时回落到 SECRET_KEY"""

    # 在独立进程中重新加载 settings，才能真实覆盖「环境变量 → SIMPLE_JWT」这一层；
    # 进程内 override_settings 改不到模块导入时已求值的 SIMPLE_JWT。
    CHILD_SCRIPT = textwrap.dedent('''
        import json
        import os

        import django
        django.setup()

        import jwt
        from django.conf import settings
        from rest_framework_simplejwt.tokens import AccessToken

        signing_key = settings.SIMPLE_JWT['SIGNING_KEY']
        main_key = settings.SECRET_KEY
        algorithm = settings.SIMPLE_JWT['ALGORITHM']
        env_key = os.environ.get('JWT_SIGNING_KEY')
        token = str(AccessToken())

        def can_verify(key):
            try:
                jwt.decode(token, key, algorithms=[algorithm])
                return True
            except jwt.InvalidSignatureError:
                return False

        print('RESULT:' + json.dumps({
            'signing_key': signing_key,
            'main_key': main_key,
            'env_key': env_key,
            'verify_env': can_verify(env_key) if env_key else None,
            'verify_main': can_verify(main_key),
        }))
    ''')

    def _resolve(self, jwt_signing_key):
        env = {k: v for k, v in os.environ.items() if k != 'JWT_SIGNING_KEY'}
        env['DJANGO_SETTINGS_MODULE'] = 'backend.settings'
        env['PYTHONIOENCODING'] = 'utf-8'
        if jwt_signing_key is not None:
            env['JWT_SIGNING_KEY'] = jwt_signing_key
        proc = subprocess.run(
            [sys.executable, '-c', self.CHILD_SCRIPT],
            cwd=str(settings.BASE_DIR), env=env,
            capture_output=True, encoding='utf-8', timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [line for line in proc.stdout.splitlines() if line.startswith('RESULT:')]
        self.assertTrue(lines, proc.stdout + proc.stderr)
        return json.loads(lines[-1][len('RESULT:'):])

    def test_jwt_signing_key_from_env_signs_and_verifies(self):
        env_key = 'unit-test-jwt-signing-key-' + 'x' * 40
        result = self._resolve(env_key)
        # 设置了独立密钥后，签名密钥即为该值，且与主 SECRET_KEY 不同
        self.assertEqual(result['signing_key'], env_key)
        self.assertNotEqual(result['signing_key'], result['main_key'])
        # 该密钥可验签，主 SECRET_KEY 验签失败
        self.assertTrue(result['verify_env'])
        self.assertFalse(result['verify_main'])

    def test_unset_jwt_signing_key_falls_back_to_secret_key(self):
        result = self._resolve(None)
        self.assertIsNone(result['env_key'])
        self.assertEqual(result['signing_key'], result['main_key'])
        self.assertTrue(result['verify_main'])


class UserManageAPITests(TestCase):
    """用户管理（仅管理员）：CRUD + 自保护 + 搜索"""

    def setUp(self):
        self.admin = _make_user('admin', is_staff=True)
        self.alice = _make_user('alice')
        self.bob = _make_user('bob')
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def test_admin_can_list_users(self):
        resp = self.client.get('/api/auth/manage/')
        self.assertEqual(resp.status_code, 200, resp.content)
        items = resp.data.get('results', resp.data)
        usernames = [u['username'] for u in items]
        self.assertIn('alice', usernames)
        self.assertIn('bob', usernames)

    def test_normal_user_forbidden(self):
        self.client.force_authenticate(user=self.alice)
        self.assertEqual(self.client.get('/api/auth/manage/').status_code, 403)

    def test_unauthenticated_rejected(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get('/api/auth/manage/').status_code, 401)

    def test_admin_can_create_user_with_password(self):
        resp = self.client.post('/api/auth/manage/', {
            'username': 'newbie', 'password': 'Devpass123', 'nickname': '新人',
        }, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        user = User.objects.get(username='newbie')
        self.assertTrue(user.check_password('Devpass123'))
        self.assertFalse(user.is_staff)
        # 密码不出现在响应中
        self.assertNotIn('password', resp.data)

    def test_admin_can_update_user_and_reset_password(self):
        resp = self.client.patch(f'/api/auth/manage/{self.alice.id}/', {
            'nickname': '爱丽丝', 'password': 'newpass66', 'is_staff': True,
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.nickname, '爱丽丝')
        self.assertTrue(self.alice.is_staff)
        self.assertTrue(self.alice.check_password('newpass66'))

    def test_admin_can_deactivate_other_user(self):
        resp = self.client.patch(f'/api/auth/manage/{self.alice.id}/', {
            'is_active': False,
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.alice.refresh_from_db()
        self.assertFalse(self.alice.is_active)

    def test_admin_can_delete_other_user(self):
        """删除是软删除：标记 is_deleted 并停用，行与会话/审计都保留"""
        resp = self.client.delete(f'/api/auth/manage/{self.alice.id}/')
        self.assertEqual(resp.status_code, 204)
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.is_deleted)
        self.assertFalse(self.alice.is_active)
        self.assertIsNotNone(self.alice.deleted_at)
        self.assertEqual(self.alice.deleted_by, self.admin)

    def test_admin_cannot_delete_self(self):
        resp = self.client.delete(f'/api/auth/manage/{self.admin.id}/')
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_admin_cannot_demote_self(self):
        resp = self.client.patch(f'/api/auth/manage/{self.admin.id}/', {
            'is_staff': False,
        }, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_admin_cannot_deactivate_self(self):
        resp = self.client.patch(f'/api/auth/manage/{self.admin.id}/', {
            'is_active': False,
        }, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_search_by_username(self):
        resp = self.client.get('/api/auth/manage/?search=alice')
        items = resp.data.get('results', resp.data)
        usernames = [u['username'] for u in items]
        self.assertIn('alice', usernames)
        self.assertNotIn('bob', usernames)

    def test_search_matches_email_and_phone(self):
        self.alice.email = 'alice@example.com'
        self.alice.phone = '13800001111'
        self.alice.save(update_fields=['email', 'phone'])

        for query in ('example.com', '1380000'):
            resp = self.client.get(f'/api/auth/manage/?search={query}')
            self.assertEqual(resp.status_code, 200, resp.content)
            usernames = [u['username'] for u in resp.data.get('results', resp.data)]
            self.assertEqual(usernames, ['alice'], query)

    def test_list_exposes_last_admin_flag(self):
        """最后一个管理员由后端下发 is_last_admin，供界面直接禁用危险操作"""
        resp = self.client.get('/api/auth/manage/')
        self.assertEqual(resp.status_code, 200, resp.content)
        rows = {u['username']: u for u in resp.data.get('results', resp.data)}
        self.assertTrue(rows['admin']['is_last_admin'])
        self.assertFalse(rows['alice']['is_last_admin'])

        # 再有一个管理员后，原管理员不再是「最后一个」
        _make_user('admin2', is_staff=True)
        resp = self.client.get('/api/auth/manage/')
        rows = {u['username']: u for u in resp.data.get('results', resp.data)}
        self.assertFalse(rows['admin']['is_last_admin'])
        self.assertFalse(rows['admin2']['is_last_admin'])

    def test_single_object_response_exposes_last_admin_flag(self):
        resp = self.client.patch(f'/api/auth/manage/{self.admin.id}/', {
            'nickname': '平台管理员',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data['is_last_admin'])
        self.assertFalse(resp.data['is_superuser'])

    def test_create_user_requires_existing_department(self):
        Department.objects.create(name='研发部')
        resp = self.client.post('/api/auth/manage/', {
            'username': 'dev1', 'password': 'demo-pass-9x2', 'department': '不存在',
        }, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(User.objects.filter(username='dev1').exists())

    def test_create_user_with_valid_department(self):
        Department.objects.create(name='研发部')
        resp = self.client.post('/api/auth/manage/', {
            'username': 'dev1', 'password': 'Devpass123', 'department': '研发部',
        }, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data['department'], '研发部')

    def test_create_user_rejects_weak_password(self):
        resp = self.client.post('/api/auth/manage/', {
            'username': 'weak1', 'password': '12345678',
        }, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(User.objects.filter(username='weak1').exists())

    def test_reset_password_rejects_weak_password(self):
        resp = self.client.patch(f'/api/auth/manage/{self.alice.id}/', {
            'password': '12345678',
        }, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.check_password('demo-pass-9x2'))

    def test_reset_password_rejects_password_similar_to_username(self):
        resp = self.client.patch(f'/api/auth/manage/{self.alice.id}/', {
            'password': 'alice1234',
        }, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.check_password('demo-pass-9x2'))

    def test_reset_password_accepts_strong_password(self):
        resp = self.client.patch(f'/api/auth/manage/{self.alice.id}/', {
            'password': 'Str0ngPass!',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.check_password('Str0ngPass!'))

    def test_user_delete_creates_audit_log(self):
        resp = self.client.delete(f'/api/auth/manage/{self.alice.id}/')
        self.assertEqual(resp.status_code, 204)
        log = AuditLog.objects.filter(action='user_delete', target='alice').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.actor, self.admin)

    def test_user_update_creates_audit_log(self):
        resp = self.client.patch(f'/api/auth/manage/{self.alice.id}/', {
            'is_active': False,
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        log = AuditLog.objects.filter(action='user_update', target='alice').first()
        self.assertIsNotNone(log)
        self.assertIn('状态变更', log.detail)

    def test_admin_reset_password_revokes_old_refresh_tokens(self):
        """管理员重置密码后，旧 refresh 必须不可再续签（与自助改密同口径）"""
        old_refresh = str(RefreshToken.for_user(self.alice))

        resp = self.client.patch(f'/api/auth/manage/{self.alice.id}/', {
            'password': 'Str0ngPass!',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)

        resp = APIClient().post(
            '/api/auth/refresh/', {'refresh': old_refresh}, format='json')
        self.assertEqual(resp.status_code, 401, resp.content)
        log = AuditLog.objects.filter(action='user_update', target='alice').first()
        self.assertIsNotNone(log)
        self.assertIn('已吊销', log.detail)


class LastAdminGuardTests(TestCase):
    """管理权限不变量：任何更新都不能让系统失去最后一个有效管理员"""

    def setUp(self):
        self.admin = _make_user('admin', is_staff=True)
        self.alice = _make_user('alice')
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def _patch(self, user, payload):
        return self.client.patch(f'/api/auth/manage/{user.id}/', payload, format='json')

    def test_last_admin_cannot_demote_self(self):
        resp = self._patch(self.admin, {'is_staff': False})
        self.assertEqual(resp.status_code, 400, resp.content)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_staff)

    def test_last_admin_cannot_deactivate_self(self):
        resp = self._patch(self.admin, {'is_active': False})
        self.assertEqual(resp.status_code, 400, resp.content)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_demoting_one_of_two_admins_allowed(self):
        other = _make_user('other_admin', is_staff=True)
        resp = self._patch(other, {'is_staff': False})
        self.assertEqual(resp.status_code, 200, resp.content)
        other.refresh_from_db()
        self.assertFalse(other.is_staff)

    def test_last_admin_cannot_be_demoted_after_other_admin_removed(self):
        other = _make_user('other_admin', is_staff=True)
        self.assertEqual(self._patch(other, {'is_staff': False}).status_code, 200)
        # 此时 admin 是最后一个具备管理权限的账号，降级与停用都必须拒绝
        self.assertEqual(self._patch(self.admin, {'is_staff': False}).status_code, 400)
        self.assertEqual(self._patch(self.admin, {'is_active': False}).status_code, 400)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_staff)
        self.assertTrue(self.admin.is_active)

    def test_self_demote_allowed_when_another_admin_exists(self):
        _make_user('other_admin', is_staff=True)
        resp = self._patch(self.admin, {'is_staff': False})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.is_staff)

    def test_inactive_admin_does_not_count_as_fallback(self):
        dormant = _make_user('dormant_admin', is_staff=True)
        dormant.is_active = False
        dormant.save(update_fields=['is_active'])
        resp = self._patch(self.admin, {'is_staff': False})
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_superuser_counts_as_admin(self):
        # 超级管理员即使 is_staff=False 也算具备管理权限，此时 admin 不是最后一个
        guard = User.objects.create_user(username='root', password='demo-pass-9x2')
        guard.is_superuser = True
        guard.save(update_fields=['is_superuser'])
        resp = self._patch(self.admin, {'is_staff': False})
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_non_admin_update_not_blocked(self):
        resp = self._patch(self.alice, {'nickname': '爱丽丝', 'is_staff': False})
        self.assertEqual(resp.status_code, 200, resp.content)


class DepartmentAPITests(TestCase):
    """部门管理（仅管理员）：CRUD + 重名校验 + 改名同步 User.department"""

    def setUp(self):
        self.admin = _make_user('admin', is_staff=True)
        self.user = _make_user('alice')
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def test_admin_can_list_departments(self):
        Department.objects.create(name='研发部')
        resp = self.client.get('/api/auth/departments/')
        self.assertEqual(resp.status_code, 200, resp.content)
        names = [d['name'] for d in resp.data.get('results', resp.data)]
        self.assertIn('研发部', names)

    def test_normal_user_forbidden(self):
        self.client.force_authenticate(user=self.user)
        self.assertEqual(self.client.get('/api/auth/departments/').status_code, 403)

    def test_unauthenticated_rejected(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get('/api/auth/departments/').status_code, 401)

    def test_admin_can_create_department(self):
        resp = self.client.post('/api/auth/departments/', {
            'name': '研发部', 'description': '技术研发',
        }, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(Department.objects.filter(name='研发部').exists())
        log = AuditLog.objects.filter(action='department_create', target='研发部').first()
        self.assertIsNotNone(log)

    def test_department_name_unique(self):
        Department.objects.create(name='研发部')
        resp = self.client.post('/api/auth/departments/', {
            'name': '研发部',
        }, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_admin_can_update_department(self):
        dept = Department.objects.create(name='研发部')
        resp = self.client.patch(f'/api/auth/departments/{dept.id}/', {
            'description': '核心技术研发',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        dept.refresh_from_db()
        self.assertEqual(dept.description, '核心技术研发')

    def test_rename_department_syncs_user_department(self):
        dept = Department.objects.create(name='研发部')
        user = _make_user('dev1', department='研发部')
        resp = self.client.patch(f'/api/auth/departments/{dept.id}/', {
            'name': '研发中心',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        user.refresh_from_db()
        self.assertEqual(user.department, '研发中心')
        log = AuditLog.objects.filter(action='department_update', target='研发中心').first()
        self.assertIsNotNone(log)

    def test_admin_can_delete_department(self):
        dept = Department.objects.create(name='研发部')
        resp = self.client.delete(f'/api/auth/departments/{dept.id}/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Department.objects.filter(id=dept.id).exists())
        log = AuditLog.objects.filter(action='department_delete', target='研发部').first()
        self.assertIsNotNone(log)


class AuditLogAPITests(TestCase):
    """操作日志（仅管理员）：只读列表，按时间倒序"""

    def setUp(self):
        self.admin = _make_user('admin', is_staff=True)
        self.user = _make_user('alice')
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)
        AuditLog.objects.create(actor=self.admin, action='user_delete', target='bob', detail='删除用户 bob')
        AuditLog.objects.create(actor=self.admin, action='llm_update', target='LLM配置', detail='更新模型')

    def test_admin_can_list_audit_logs(self):
        resp = self.client.get('/api/auth/audit-logs/')
        self.assertEqual(resp.status_code, 200, resp.content)
        results = resp.data.get('results', resp.data)
        self.assertEqual(len(results), 2)
        # 默认倒序：后创建的 llm_update 在前
        self.assertEqual(results[0]['action'], 'llm_update')

    def test_normal_user_forbidden(self):
        self.client.force_authenticate(user=self.user)
        self.assertEqual(self.client.get('/api/auth/audit-logs/').status_code, 403)

    def test_auth_action_filter_and_source_fields(self):
        """新增认证动作可被 action 精确筛选，IP/UA 正常返回"""
        AuditLog.objects.create(
            actor=self.admin, action='login_failed', target='alice',
            detail='{"result": "failed"}', ip='127.0.0.1', user_agent='LassoTest/1.0',
        )
        resp = self.client.get('/api/auth/audit-logs/?action=login_failed')
        self.assertEqual(resp.status_code, 200, resp.content)
        results = resp.data.get('results', resp.data)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['action_display'], '登录失败')
        self.assertEqual(results[0]['ip'], '127.0.0.1')
        self.assertEqual(results[0]['user_agent'], 'LassoTest/1.0')

    def test_unauthenticated_rejected(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get('/api/auth/audit-logs/').status_code, 401)


class DepartmentMigrationTests(TestCase):
    """数据迁移：现有用户/知识库部门名去重后生成 Department"""

    def test_migration_seeds_departments(self):
        from backend.apps.knowledge.models import KnowledgeBase
        # 清理并新建
        Department.objects.all().delete()
        User.objects.all().delete()
        KnowledgeBase.objects.all().delete()

        _make_user('u1', department='  财务部  ')
        _make_user('u2', department='财务部')
        _make_user('u3', department='')
        KnowledgeBase.objects.create(name='kb1', department='市场部')

        # 重新执行迁移中的 seed 逻辑
        from importlib import import_module
        from django.apps import apps
        migration_module = import_module('backend.apps.users.migrations.0002_department_auditlog')
        migration_module.seed_departments_from_existing_data(apps, None)

        names = set(Department.objects.values_list('name', flat=True))
        self.assertEqual(names, {'财务部', '市场部'})


class ToolBodyViewTests(TestCase):
    """PRD 9.2 #4：查看工具调用全文（管理员、查看即留痕、无正文可区分原因）"""

    URL = '/api/auth/audit-logs/{}/body/'
    LIST_URL = '/api/auth/audit-logs/'

    def setUp(self):
        from backend.apps.chat.models import ToolCallBody  # noqa: F401
        self.admin = _make_user('admin', is_staff=True)
        self.user = _make_user('alice')
        self.client = APIClient()

    def _log(self, days_ago=0):
        log = AuditLog.objects.create(actor=self.user, action='tool_call',
                                      target='demo', detail='{"call_id": "c1"}')
        if days_ago:
            AuditLog.objects.filter(pk=log.pk).update(
                created_at=timezone.now() - timedelta(days=days_ago))
            log.refresh_from_db()
        return log

    def _with_body(self, days_ago=0):
        from backend.apps.chat.models import ToolCallBody
        log = self._log(days_ago)
        raw = '完整返回\n第二行'
        ToolCallBody.objects.create(
            audit=log, call_id='c1', tool='mcp__demo__ping', actor=self.user,
            state='success', result=raw, result_chars=len(raw),
            result_sha256=hashlib.sha256(raw.encode('utf-8')).hexdigest())
        return log, raw

    def test_admin_can_read_full_body(self):
        from backend.apps.chat.models import ToolCallBody
        log, raw = self._with_body()
        self.client.force_authenticate(user=self.admin)
        resp = self.client.get(self.URL.format(log.pk))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['result'], raw)
        for field in ('id', 'conversation_id', 'call_id', 'tool', 'state', 'egress',
                      'created_at', 'result', 'result_chars', 'result_sha256',
                      'storage_truncated'):
            self.assertIn(field, resp.data)
        body = ToolCallBody.objects.get(audit=log)
        self.assertEqual(resp.data['result_sha256'], body.result_sha256)
        self.assertFalse(resp.data['storage_truncated'])

    def test_non_admin_forbidden(self):
        log, _ = self._with_body()
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(self.URL.format(log.pk))
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertFalse(AuditLog.objects.filter(action='tool_body_view').exists())

    def test_history_without_body_is_404(self):
        log = self._log()
        self.client.force_authenticate(user=self.admin)
        resp = self.client.get(self.URL.format(log.pk))
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(resp.data['code'], 'tool_body_unavailable')
        self.assertIn('未留存全文', resp.data['detail'])
        self.assertFalse(AuditLog.objects.filter(action='tool_body_view').exists())

    def test_expired_body_is_404_with_distinct_message(self):
        log = self._log(days_ago=120)
        self.client.force_authenticate(user=self.admin)
        resp = self.client.get(self.URL.format(log.pk))
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(resp.data['code'], 'tool_body_unavailable')
        self.assertIn('到期清理', resp.data['detail'])
        self.assertFalse(AuditLog.objects.filter(action='tool_body_view').exists())

    def test_each_view_adds_exactly_one_audit_without_body(self):
        log, raw = self._with_body()
        self.client.force_authenticate(user=self.admin)
        before = AuditLog.objects.filter(action='tool_body_view').count()
        self.client.get(self.URL.format(log.pk))
        after = AuditLog.objects.filter(action='tool_body_view').count()
        self.assertEqual(after - before, 1)
        row = AuditLog.objects.filter(action='tool_body_view').latest('id')
        self.assertNotIn(raw, row.detail)
        detail = json.loads(row.detail)
        self.assertEqual(detail['audit_id'], log.pk)
        self.assertEqual(detail['call_id'], 'c1')
        self.assertEqual(detail['tool'], 'mcp__demo__ping')

    def test_audit_write_failure_still_returns_body(self):
        log, raw = self._with_body()
        self.client.force_authenticate(user=self.admin)
        with patch('backend.apps.users.views.log_audit',
                   side_effect=RuntimeError('boom')):
            resp = self.client.get(self.URL.format(log.pk))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['result'], raw)

    def test_list_has_body_without_per_row_query(self):
        from backend.apps.chat.models import ToolCallBody
        with_body, _ = self._with_body()
        plain = self._log(days_ago=1)
        self.client.force_authenticate(user=self.admin)
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(self.LIST_URL)
        self.assertEqual(resp.status_code, 200, resp.content)
        rows = {row['id']: row for row in resp.data['results']} \
            if 'results' in resp.data else {row['id']: row for row in resp.data}
        self.assertTrue(rows[with_body.pk]['has_body'])
        self.assertFalse(rows[plain.pk]['has_body'])
        # 一次 Exists 子查询即可，若逐行查全文表会出现 N 条
        body_queries = [q for q in ctx.captured_queries
                        if 'ask_tool_call_bodies' in q['sql']]
        self.assertLessEqual(len(body_queries), 1)
        self.assertTrue(ToolCallBody.objects.filter(audit=with_body).exists())


class SessionManagementTests(TestCase):
    """查看活跃会话 + 强制下线（与停用账号互相独立）"""

    LIST_URL = '/api/auth/manage/{}/sessions/'
    REVOKE_URL = '/api/auth/manage/{}/revoke-sessions/'
    REFRESH_URL = '/api/auth/refresh/'

    def setUp(self):
        self.admin = _make_user('admin', is_staff=True)
        self.alice = _make_user('alice')
        self.bob = _make_user('bob')
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def test_admin_lists_active_sessions_with_real_timestamps(self):
        RefreshToken.for_user(self.alice)
        RefreshToken.for_user(self.alice)

        resp = self.client.get(self.LIST_URL.format(self.alice.id))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['count'], 2)
        self.assertEqual(len(resp.data['results']), 2)
        # 最后使用时间确实没有数据源，返回 None 并附带说明，不伪造
        self.assertFalse(resp.data['last_used_supported'])
        self.assertIn('未记录', resp.data['last_used_note'])
        for row in resp.data['results']:
            self.assertIsNone(row['last_used'])
            self.assertIsNotNone(row['created_at'])
            self.assertIsNotNone(row['expires_at'])
            self.assertIn('source_note', row)

    def test_blacklisted_session_is_not_listed(self):
        token = RefreshToken.for_user(self.alice)
        token.blacklist()
        resp = self.client.get(self.LIST_URL.format(self.alice.id))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['count'], 0)

    def test_sessions_correlate_ip_and_user_agent_from_auth_audit(self):
        log_audit(
            self.alice, 'login_success', self.alice.username,
            detail='{"result": "success"}', ip='198.51.100.10', user_agent='LassoTest/2.0',
        )
        RefreshToken.for_user(self.alice)
        resp = self.client.get(self.LIST_URL.format(self.alice.id))
        row = resp.data['results'][0]
        self.assertEqual(row['ip'], '198.51.100.10')
        self.assertEqual(row['user_agent'], 'LassoTest/2.0')
        self.assertEqual(row['source_action'], 'login_success')
        self.assertIn('关联', row['source_note'])

    def test_uncorrelated_session_says_so_instead_of_faking(self):
        RefreshToken.for_user(self.alice)
        resp = self.client.get(self.LIST_URL.format(self.alice.id))
        row = resp.data['results'][0]
        self.assertIsNone(row['ip'])
        self.assertEqual(row['user_agent'], '')
        self.assertIsNone(row['source_action'])
        self.assertIn('未关联', row['source_note'])

    def test_revoke_kills_target_refresh_and_leaves_others_untouched(self):
        target_refresh = str(RefreshToken.for_user(self.alice))
        other_refresh = str(RefreshToken.for_user(self.bob))

        resp = self.client.post(self.REVOKE_URL.format(self.alice.id), {}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertGreaterEqual(resp.data['revoked'], 1)

        anon = APIClient()
        killed = anon.post(self.REFRESH_URL, {'refresh': target_refresh}, format='json')
        self.assertEqual(killed.status_code, 401, killed.content)
        survived = anon.post(self.REFRESH_URL, {'refresh': other_refresh}, format='json')
        self.assertEqual(survived.status_code, 200, survived.content)

        # 强制下线不改变启用状态：停用是另一个独立动作
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.is_active)
        self.assertFalse(self.alice.is_deleted)

        log = AuditLog.objects.filter(
            action='force_logout', target='alice').latest('created_at')
        self.assertEqual(log.actor, self.admin)
        self.assertIn('账号启用状态未改变', log.detail)

    def test_revoke_empties_session_list(self):
        RefreshToken.for_user(self.alice)
        self.client.post(self.REVOKE_URL.format(self.alice.id), {}, format='json')
        resp = self.client.get(self.LIST_URL.format(self.alice.id))
        self.assertEqual(resp.data['count'], 0)

    def test_normal_user_cannot_view_or_revoke_sessions(self):
        self.client.force_authenticate(user=self.alice)
        self.assertEqual(
            self.client.get(self.LIST_URL.format(self.bob.id)).status_code, 403)
        self.assertEqual(
            self.client.post(self.REVOKE_URL.format(self.bob.id), {}, format='json').status_code,
            403,
        )


class SoftDeleteUserTests(TestCase):
    """删除账号是软删除：不毁会话与追责链，且保留既有管理员保护"""

    def setUp(self):
        self.admin = _make_user('admin', is_staff=True)
        self.victim = _make_user('victim')
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def test_delete_keeps_conversation_and_audit_actor(self):
        from backend.apps.chat.models import Conversation

        conversation = Conversation.objects.create(user=self.victim, title='历史会话')
        own_audit = log_audit(self.victim, 'login_success', self.victim.username)

        resp = self.client.delete(f'/api/auth/manage/{self.victim.id}/')
        self.assertEqual(resp.status_code, 204, resp.content)

        self.victim.refresh_from_db()
        self.assertTrue(self.victim.is_deleted)
        self.assertFalse(self.victim.is_active)
        self.assertEqual(self.victim.deleted_by, self.admin)
        # 会话正文与操作人的历史留痕都还在
        self.assertTrue(Conversation.objects.filter(pk=conversation.pk).exists())
        own_audit.refresh_from_db()
        self.assertEqual(own_audit.actor_id, self.victim.id)
        self.assertEqual(own_audit.actor_name, 'victim')

        delete_log = AuditLog.objects.filter(action='user_delete', target='victim').latest('id')
        self.assertEqual(delete_log.actor_name, 'admin')
        self.assertIn('软删除', delete_log.detail)

    def test_deleted_user_cannot_login_and_refresh_is_revoked(self):
        old_refresh = str(RefreshToken.for_user(self.victim))
        self.client.delete(f'/api/auth/manage/{self.victim.id}/')
        self.assertIsNone(authenticate(username='victim', password='demo-pass-9x2'))
        resp = APIClient().post(
            '/api/auth/refresh/', {'refresh': old_refresh}, format='json')
        self.assertEqual(resp.status_code, 401, resp.content)

    def test_deleted_user_hidden_from_default_list_but_visible_in_archive(self):
        self.client.delete(f'/api/auth/manage/{self.victim.id}/')

        resp = self.client.get('/api/auth/manage/')
        names = [u['username'] for u in resp.data.get('results', resp.data)]
        self.assertNotIn('victim', names)

        resp = self.client.get('/api/auth/manage/?deleted=1')
        rows = {u['username']: u for u in resp.data.get('results', resp.data)}
        self.assertIn('victim', rows)
        self.assertTrue(rows['victim']['is_deleted'])
        self.assertEqual(rows['victim']['deleted_by_name'], 'admin')

    def test_actor_snapshot_survives_physical_row_removal(self):
        """即使将来物理清理用户行（actor 被 SET_NULL），审计仍显示是谁做的"""
        own_audit = log_audit(self.victim, 'login_success', self.victim.username)
        User.objects.filter(pk=self.victim.pk).delete()

        own_audit.refresh_from_db()
        self.assertIsNone(own_audit.actor_id)
        self.assertEqual(own_audit.actor_name, 'victim')
        self.assertEqual(str(own_audit), f'victim {own_audit.get_action_display()} victim')

    def test_cannot_delete_self_still_guarded(self):
        resp = self.client.delete(f'/api/auth/manage/{self.admin.id}/')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.is_deleted)


class RoleChangeAuditTests(TestCase):
    """角色/状态变更留痕必须带前后值，且不得写入密码"""

    def setUp(self):
        self.admin = _make_user('admin', is_staff=True)
        self.alice = _make_user('alice')
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def _latest_user_update(self, username='alice'):
        return AuditLog.objects.filter(
            action='user_update', target=username).latest('created_at')

    def test_promotion_records_false_to_true(self):
        resp = self.client.patch(
            f'/api/auth/manage/{self.alice.id}/', {'is_staff': True}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        detail = self._latest_user_update().detail
        self.assertIn('is_staff: false → true', detail)

    def test_demotion_records_true_to_false(self):
        staff = _make_user('staff1', is_staff=True)
        resp = self.client.patch(
            f'/api/auth/manage/{staff.id}/', {'is_staff': False}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        detail = self._latest_user_update('staff1').detail
        self.assertIn('is_staff: true → false', detail)

    def test_status_change_records_before_after(self):
        resp = self.client.patch(
            f'/api/auth/manage/{self.alice.id}/', {'is_active': False}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        detail = self._latest_user_update().detail
        self.assertIn('is_active: true → false', detail)
        self.assertIn('状态变更', detail)

    def test_password_reset_audit_never_contains_password(self):
        secret = 'Str0ngPass!42'
        resp = self.client.patch(
            f'/api/auth/manage/{self.alice.id}/', {'password': secret}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertNotIn(secret, self._latest_user_update().detail)
        reset_log = AuditLog.objects.filter(
            action='password_reset', target='alice').latest('created_at')
        self.assertNotIn(secret, reset_log.detail)
        self.assertIn('已吊销', reset_log.detail)


class SensitiveRevealTests(TestCase):
    """展示层脱敏的展开留痕：记录谁在何时看了哪条，且不把正文写进审计"""

    URL = '/api/auth/audit-logs/sensitive-reveal/'

    def setUp(self):
        self.admin = _make_user('reveal-admin', is_staff=True)
        self.alice = _make_user('reveal-alice')
        self.bob = _make_user('reveal-bob')
        self.client = APIClient()
        self.log = AuditLog.objects.create(
            actor=self.alice, action='tool_call', target='工具调用:c1',
            detail='{"api_key": "sk-live-1234567890abcd"}')

    def test_requires_login(self):
        resp = APIClient().post(
            self.URL, {'scope': 'audit', 'audit_id': self.log.pk, 'field': 'detail'},
            format='json')
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(AuditLog.objects.filter(action='sensitive_reveal').exists())

    def test_audit_detail_reveal_is_recorded(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(
            self.URL, {'scope': 'audit', 'audit_id': self.log.pk, 'field': 'detail',
                       'masked_fields': 1}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        rec = AuditLog.objects.get(action='sensitive_reveal')
        self.assertEqual(rec.actor, self.admin)
        self.assertEqual(rec.target, f'审计详情:{self.log.pk}')
        detail = json.loads(rec.detail)
        self.assertEqual(detail['field'], 'detail')
        self.assertEqual(detail['audit_id'], self.log.pk)
        # 只记对象标识与字段，不落被展开的正文
        self.assertNotIn('sk-live', rec.detail)

    def test_audit_detail_reveal_forbidden_for_normal_user(self):
        self.client.force_authenticate(user=self.alice)
        resp = self.client.post(
            self.URL, {'scope': 'audit', 'audit_id': self.log.pk, 'field': 'detail'},
            format='json')
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertFalse(AuditLog.objects.filter(action='sensitive_reveal').exists())

    def test_audit_detail_reveal_missing_record_is_404(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(
            self.URL, {'scope': 'audit', 'audit_id': 999999, 'field': 'detail'},
            format='json')
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertFalse(AuditLog.objects.filter(action='sensitive_reveal').exists())

    def test_chat_reveal_in_own_conversation_is_recorded(self):
        from backend.apps.chat.models import Conversation
        conv = Conversation.objects.create(user=self.alice, title='演示')
        self.client.force_authenticate(user=self.alice)
        resp = self.client.post(
            self.URL, {'scope': 'chat', 'conversation_id': conv.pk, 'call_id': 'c1',
                       'field': 'args', 'masked_fields': 2}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        rec = AuditLog.objects.get(action='sensitive_reveal')
        self.assertEqual(rec.actor, self.alice)
        self.assertEqual(rec.target, '工具调用:c1')
        detail = json.loads(rec.detail)
        self.assertEqual(detail['conversation_id'], conv.pk)
        self.assertEqual(detail['field'], 'args')

    def test_chat_reveal_in_foreign_conversation_is_404(self):
        from backend.apps.chat.models import Conversation
        conv = Conversation.objects.create(user=self.alice, title='私有')
        self.client.force_authenticate(user=self.bob)
        resp = self.client.post(
            self.URL, {'scope': 'chat', 'conversation_id': conv.pk, 'call_id': 'c1',
                       'field': 'args'}, format='json')
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertFalse(AuditLog.objects.filter(action='sensitive_reveal').exists())

    def test_trial_reveal_without_conversation_is_recorded(self):
        self.client.force_authenticate(user=self.alice)
        resp = self.client.post(
            self.URL, {'scope': 'chat', 'call_id': 'c9', 'field': 'result'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        rec = AuditLog.objects.get(action='sensitive_reveal')
        self.assertEqual(rec.target, '试聊工具调用:c9')

    def test_rejects_unknown_scope(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(
            self.URL, {'scope': 'all', 'audit_id': self.log.pk, 'field': 'detail'},
            format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(AuditLog.objects.filter(action='sensitive_reveal').exists())
