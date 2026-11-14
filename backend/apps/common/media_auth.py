"""媒体文件访问控制（/media/ 鉴权）

生产环境 nginx 用 ``auth_request`` 把每个 /media/ 请求先转发到
``media_auth_view``：未登录 401、无权 403，返回 200 后仍由 nginx 直出文件。
这样既不会把 MEDIA_ROOT 整个目录裸挂在公网路径上，也不必让 Django 参与
传文件（Range 请求由 nginx 原生处理）。

校验必须回答「这个用户能不能看这个文件」，而不是「这个用户登录了没有」：

    路径规范化 → 数据库记录 → 所有者 / 知识库可见范围

浏览器原生请求（``<img>``/``<audio>``/``<a download>``）带不上 Authorization 头，
登录态只能靠 HttpOnly Cookie ``media_token`` 携带：登录/注册/刷新 token 时下发，
存量会话由 ``MediaTokenCookieMiddleware`` 在带 Bearer 的 API 请求上补发。
Cookie 里只放签名过的用户 ID，不参与 API 认证。
"""

import os
import re
from urllib.parse import unquote, urlsplit

from django.conf import settings
from django.core import signing
from django.http import HttpResponse, HttpResponseNotAllowed

MEDIA_TOKEN_COOKIE = 'media_token'
MEDIA_TOKEN_SALT = 'lasso.media.auth'
MEDIA_TOKEN_MAX_AGE = 7 * 24 * 60 * 60

MEDIA_PREFIX = '/media/'
BRANDING_DIR = 'branding'

# 连续解码后仍残留的百分号转义（如双重编码）一律拒绝
_PCT_ESCAPE_RE = re.compile(r'%[0-9a-fA-F]{2}')


# --------------------------------------------------------------------------
# 登录态 Cookie
# --------------------------------------------------------------------------
def make_media_token(user_id: int) -> str:
    return signing.dumps({'uid': int(user_id)}, salt=MEDIA_TOKEN_SALT, compress=True)


def read_media_token(token: str):
    """解析并校验 Media Cookie，返回 user_id；无效/过期返回 None"""
    if not token:
        return None
    try:
        data = signing.loads(token, salt=MEDIA_TOKEN_SALT, max_age=MEDIA_TOKEN_MAX_AGE)
    except (signing.BadSignature, signing.SignatureExpired):
        return None
    uid = data.get('uid') if isinstance(data, dict) else None
    return uid if isinstance(uid, int) else None


def set_media_cookie(response, user_id: int):
    """下发 /media/ 专用鉴权 Cookie

    Path=/ 让浏览器在 /media/ 与 /api/ 下都带上；HttpOnly 防脚本读取；
    SameSite=Lax 防跨站携带。它只用于 /media/ 鉴权，不参与 API 认证。
    """
    response.set_cookie(
        MEDIA_TOKEN_COOKIE,
        make_media_token(user_id),
        max_age=MEDIA_TOKEN_MAX_AGE,
        path='/',
        httponly=True,
        samesite='Lax',
    )
    return response


def clear_media_cookie(response):
    response.delete_cookie(MEDIA_TOKEN_COOKIE, path='/')
    return response


class MediaTokenCookieMiddleware:
    """为存量登录会话补发 Media Cookie，并在登出时清理

    只在 Cookie 缺失/失效时尝试解析 Authorization: Bearer，正常请求零额外
    数据库开销；登出路径不再补发，否则刚清掉的 Cookie 会被同请求重新种上。
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request._media_mint_user_id = None
        is_api = request.path.startswith('/api/')
        if is_api and read_media_token(request.COOKIES.get(MEDIA_TOKEN_COOKIE)) is None:
            if request.path != '/api/auth/logout/':
                user = self._bearer_user(request)
                if user is not None and user.is_active:
                    request._media_mint_user_id = user.id

        response = self.get_response(request)

        if request.path == '/api/auth/logout/':
            clear_media_cookie(response)
        elif request._media_mint_user_id:
            set_media_cookie(response, request._media_mint_user_id)
        return response

    @staticmethod
    def _bearer_user(request):
        """从 Authorization: Bearer 解析用户；任何异常都按未登录处理"""
        from rest_framework_simplejwt.authentication import JWTAuthentication

        try:
            result = JWTAuthentication().authenticate(request)
        except Exception:
            return None
        return result[0] if result else None


# --------------------------------------------------------------------------
# 路径规范化
# --------------------------------------------------------------------------
def normalize_media_path(raw_uri: str):
    """把原始请求 URI 规范成 MEDIA_ROOT 下的相对路径

    拒绝：非 /media/ 前缀、目录（尾斜杠）、空段、``.``/``..``、隐藏文件、
    反斜杠、NUL、双重编码残留，以及 realpath 后逃出 MEDIA_ROOT 的路径
    （符号链接同样会在这里被拦下）。返回 None 表示不安全。
    """
    if not raw_uri:
        return None
    path = urlsplit(str(raw_uri)).path
    # nginx 传的是原始编码，这里解码两次以识别双重编码的穿越
    for _ in range(2):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded

    if _PCT_ESCAPE_RE.search(path):
        return None
    if not path.startswith(MEDIA_PREFIX):
        return None

    rel = path[len(MEDIA_PREFIX):]
    if not rel or rel.endswith('/'):
        return None
    if '\\' in rel or '\x00' in rel:
        return None

    segments = rel.split('/')
    if any(seg in ('', '.', '..') for seg in segments):
        return None
    if any(seg.startswith('.') for seg in segments):
        return None

    media_root = os.path.realpath(str(settings.MEDIA_ROOT))
    target = os.path.realpath(os.path.join(media_root, *segments))
    if target != media_root and not target.startswith(media_root + os.sep):
        return None
    # 真实目录一律拒绝，避免目录枚举
    if os.path.isdir(target):
        return None
    return rel


# --------------------------------------------------------------------------
# 归属判定
# --------------------------------------------------------------------------
def _can_view_knowledge(user, rel: str) -> bool:
    """知识库文档：管理员全量；其余按库的可见范围（公开/本人/本部门）"""
    from backend.apps.knowledge.models import Document

    doc = Document.objects.filter(file_path=rel).select_related('knowledge_base').first()
    if doc is None:
        return False
    if getattr(user, 'is_staff', False):
        return True
    kb = doc.knowledge_base
    if kb.created_by_id is not None and kb.created_by_id == user.id:
        return True
    if kb.scope == 'public':
        return True
    if kb.scope == 'department' and user.department and kb.department == user.department:
        return True
    return False


def _can_view_chat_generated(user, rel: str) -> bool:
    from backend.apps.chat.models import ChatGeneratedFile

    return ChatGeneratedFile.objects.filter(file=rel, owner=user).exists()


def can_access_media(user, rel: str) -> bool:
    """按「路径 → 记录 → 所有者/可见范围」判定，未登记目录一律拒绝"""
    parts = rel.split('/')
    head = parts[0]

    # 登录页 LOGO 必须未登录可见；该目录仅管理员可写（品牌设置接口）
    if head == BRANDING_DIR:
        return True
    if user is None:
        return False

    # 对话临时附件：chat_attachments/<user_id>/<单层文件名>，严格属主隔离
    if head == 'chat_attachments':
        return len(parts) == 3 and parts[1] == str(user.id)

    # 对话产物：以数据库属主为准，不按目录猜
    if head == 'chat_generated':
        return _can_view_chat_generated(user, rel)

    if head == 'knowledge':
        return _can_view_knowledge(user, rel)

    # skills 等内部镜像目录一律拒绝（内容可枚举，且不是给终端用户看的）
    return False


# --------------------------------------------------------------------------
# nginx auth_request 入口
# --------------------------------------------------------------------------
def media_auth_view(request):
    """nginx 子请求：仅返回状态码，正文为空

    - 401：未登录 / Cookie 无效 / 账号已停用
    - 403：路径不安全（穿越、目录枚举、隐藏文件）或无权访问
    - 200：允许，nginx 继续直出文件
    """
    if request.method not in ('GET', 'HEAD'):
        return HttpResponseNotAllowed(['GET', 'HEAD'])

    raw_uri = request.META.get('HTTP_X_ORIGINAL_URI') or request.GET.get('uri', '')
    rel = normalize_media_path(raw_uri)
    if rel is None:
        return HttpResponse(status=403)

    # branding 公开，先判路径再取用户，未登录也能拿到 LOGO
    if rel.split('/')[0] == BRANDING_DIR:
        return HttpResponse(status=200)

    user_id = read_media_token(request.COOKIES.get(MEDIA_TOKEN_COOKIE))
    if user_id is None:
        return HttpResponse(status=401)

    from django.contrib.auth import get_user_model

    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        return HttpResponse(status=401)

    if not can_access_media(user, rel):
        return HttpResponse(status=403)
    return HttpResponse(status=200)
