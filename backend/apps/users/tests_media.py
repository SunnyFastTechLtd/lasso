"""`/media/` 鉴权测试

等价于 nginx 的 auth_request 子请求：
    GET /api/media/auth/ + Cookie: media_token + X-Original-URI: /media/...
"""

import os
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from backend.apps.chat.models import ChatGeneratedFile
from backend.apps.common.media_auth import (
    MEDIA_TOKEN_COOKIE, can_access_media, make_media_token, normalize_media_path,
)
from backend.apps.knowledge.models import Document, KnowledgeBase

User = get_user_model()


class NormalizeMediaPathTests(TestCase):
    """路径规范化：只放行 MEDIA_ROOT 下的普通文件"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.media_dir = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.media_dir, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.override = override_settings(MEDIA_ROOT=self.media_dir)
        self.override.enable()
        os.makedirs(os.path.join(self.media_dir, 'knowledge'), exist_ok=True)

    def tearDown(self):
        self.override.disable()

    def test_accepts_normal_file(self):
        self.assertEqual(
            normalize_media_path('/media/chat_attachments/3/a.txt'),
            'chat_attachments/3/a.txt',
        )

    def test_rejects_traversal_variants(self):
        for raw in (
            '/media/../.env',
            '/media/%2e%2e/.env',
            '/media/%252e%252e/.env',          # 双重编码
            '/media/knowledge/..%2f.env',
            '/media/chat_attachments\\1\\a.txt',  # 反斜杠
            '/media//etc/passwd',
            '/media/knowledge/./a.txt',
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_media_path(raw))

    def test_rejects_hidden_directory_and_dir(self):
        os.makedirs(os.path.join(self.media_dir, '.git'), exist_ok=True)
        self.assertIsNone(normalize_media_path('/media/.git/config'))
        self.assertIsNone(normalize_media_path('/media/knowledge'))
        self.assertIsNone(normalize_media_path('/media/knowledge/'))

    def test_rejects_other_prefix_and_empty(self):
        self.assertIsNone(normalize_media_path('/static/a.txt'))
        self.assertIsNone(normalize_media_path('media/a.txt'))
        self.assertIsNone(normalize_media_path(''))

    def test_rejects_symlink_escape(self):
        outside = tempfile.mkdtemp()
        try:
            link = os.path.join(self.media_dir, 'knowledge', 'escape')
            os.symlink(outside, link)
            self.assertIsNone(normalize_media_path('/media/knowledge/escape/x.txt'))
        finally:
            shutil.rmtree(outside, ignore_errors=True)


class MediaAuthViewTests(TestCase):
    """auth_request 入口的状态码语义：401 未登录 / 403 无权或路径不安全 / 200 放行"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.media_dir = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.media_dir, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.override = override_settings(MEDIA_ROOT=self.media_dir)
        self.override.enable()
        os.makedirs(os.path.join(self.media_dir, 'chat_attachments', '1'), exist_ok=True)
        os.makedirs(os.path.join(self.media_dir, 'branding'), exist_ok=True)
        self.user = User.objects.create_user(username='media_owner', password='pw123456')
        self.other = User.objects.create_user(username='media_other', password='pw123456')
        self.client = APIClient()

    def tearDown(self):
        self.override.disable()

    def _check(self, uri, user=None):
        client = APIClient()
        if user is not None:
            client.cookies[MEDIA_TOKEN_COOKIE] = make_media_token(user.id)
        return client.get('/api/media/auth/', HTTP_X_ORIGINAL_URI=uri)

    def test_anonymous_private_file_is_401(self):
        self.assertEqual(self._check('/media/chat_attachments/1/a.txt').status_code, 401)

    def test_branding_is_public(self):
        # 登录页 LOGO 的唯一公开例外
        self.assertEqual(self._check('/media/branding/logo.png').status_code, 200)

    def test_invalid_cookie_is_401(self):
        client = APIClient()
        client.cookies[MEDIA_TOKEN_COOKIE] = 'tampered'
        resp = client.get(
            '/api/media/auth/', HTTP_X_ORIGINAL_URI='/media/chat_attachments/1/a.txt')
        self.assertEqual(resp.status_code, 401)

    def test_owner_can_read_own_attachment(self):
        resp = self._check(f'/media/chat_attachments/{self.user.id}/a.txt', self.user)
        self.assertEqual(resp.status_code, 200)

    def test_other_user_forbidden(self):
        resp = self._check(f'/media/chat_attachments/{self.user.id}/a.txt', self.other)
        self.assertEqual(resp.status_code, 403)

    def test_traversal_forbidden_even_when_logged_in(self):
        resp = self._check('/media/../.env', self.user)
        self.assertEqual(resp.status_code, 403)

    def test_chat_generated_checks_owner(self):
        ChatGeneratedFile.objects.create(
            owner=self.user,
            file='chat_generated/2026/11/report.docx',
            file_name='report.docx',
            file_type='docx',
        )
        self.assertEqual(
            self._check('/media/chat_generated/2026/11/report.docx', self.user).status_code, 200)
        self.assertEqual(
            self._check('/media/chat_generated/2026/11/report.docx', self.other).status_code, 403)

    def test_skills_mirror_is_denied(self):
        self.assertFalse(can_access_media(self.user, 'skills/demo/SKILL.md'))

    def test_avatars_directory_is_denied(self):
        # 头像功能已停用：avatars/ 不再是可访问目录，按未登记目录处理
        self.assertEqual(self._check('/media/avatars/a.png', self.user).status_code, 403)
        self.assertEqual(self._check('/media/avatars/a.png').status_code, 401)

    def test_post_not_allowed(self):
        client = APIClient()
        client.cookies[MEDIA_TOKEN_COOKIE] = make_media_token(self.user.id)
        resp = client.post(
            '/api/media/auth/', HTTP_X_ORIGINAL_URI='/media/chat_attachments/1/a.txt')
        self.assertEqual(resp.status_code, 405)

    def test_inactive_user_is_401(self):
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        resp = self._check('/media/chat_attachments/1/a.txt', self.user)
        self.assertEqual(resp.status_code, 401)


class KnowledgeMediaPermissionTests(TestCase):
    """知识库文档：管理员全量，其他人按库的可见范围"""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='kb_owner', password='pw123456', department='研发部')
        self.same_dept = User.objects.create_user(
            username='kb_same', password='pw123456', department='研发部')
        self.outsider = User.objects.create_user(
            username='kb_out', password='pw123456', department='市场部')
        self.admin = User.objects.create_user(
            username='kb_admin', password='pw123456', is_staff=True)

        self.public_kb = KnowledgeBase.objects.create(
            name='公开库', scope='public', created_by=self.owner)
        self.dept_kb = KnowledgeBase.objects.create(
            name='部门库', scope='department', department='研发部', created_by=self.owner)
        self.private_kb = KnowledgeBase.objects.create(
            name='私有库', scope='private', created_by=self.owner)

        self.public_rel = 'knowledge/2026/11/public.txt'
        self.dept_rel = 'knowledge/2026/11/dept.txt'
        self.private_rel = 'knowledge/2026/11/private.txt'
        for kb, rel in (
            (self.public_kb, self.public_rel),
            (self.dept_kb, self.dept_rel),
            (self.private_kb, self.private_rel),
        ):
            Document.objects.create(
                knowledge_base=kb, title=rel, file_name=os.path.basename(rel),
                file_type='txt', file_size=1, file_path=rel, uploaded_by=self.owner,
            )

    def test_public_kb_visible_to_any_logged_in_user(self):
        self.assertTrue(can_access_media(self.outsider, self.public_rel))

    def test_department_kb_only_same_department(self):
        self.assertTrue(can_access_media(self.same_dept, self.dept_rel))
        self.assertFalse(can_access_media(self.outsider, self.dept_rel))

    def test_private_kb_only_owner(self):
        self.assertTrue(can_access_media(self.owner, self.private_rel))
        self.assertFalse(can_access_media(self.same_dept, self.private_rel))

    def test_staff_can_read_everything(self):
        for rel in (self.public_rel, self.dept_rel, self.private_rel):
            self.assertTrue(can_access_media(self.admin, rel))

    def test_unknown_knowledge_file_denied(self):
        self.assertFalse(can_access_media(self.admin, 'knowledge/2026/11/none.txt'))


class MediaCookieTests(TestCase):
    """Cookie 下发与清理：登录/刷新下发，带 Bearer 的接口请求补发，登出清除"""

    def setUp(self):
        self.user = User.objects.create_user(username='cookie_user', password='pw123456')
        self.client = APIClient()

    def test_login_sets_media_cookie(self):
        resp = self.client.post(
            '/api/auth/login/',
            {'username': 'cookie_user', 'password': 'pw123456'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(MEDIA_TOKEN_COOKIE, resp.cookies)

    def test_refresh_sets_media_cookie(self):
        refresh = str(RefreshToken.for_user(self.user))
        resp = self.client.post('/api/auth/refresh/', {'refresh': refresh}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(MEDIA_TOKEN_COOKIE, resp.cookies)

    def test_middleware_mints_cookie_for_bearer_request(self):
        access = str(RefreshToken.for_user(self.user).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        resp = self.client.get('/api/auth/profile/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(MEDIA_TOKEN_COOKIE, resp.cookies)

    def test_logout_clears_media_cookie(self):
        access = str(RefreshToken.for_user(self.user).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        self.client.cookies[MEDIA_TOKEN_COOKIE] = make_media_token(self.user.id)
        resp = self.client.post('/api/auth/logout/', {}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.cookies[MEDIA_TOKEN_COOKIE].value, '')
