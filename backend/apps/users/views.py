import ipaddress
import json
import logging
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.core.cache import cache
from django.db import models
from django.utils import timezone
from rest_framework import status, viewsets, mixins
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from django.contrib.auth import get_user_model, authenticate
from .serializers import (
    AdminUserSerializer, AuditLogSerializer, DepartmentSerializer,
    UserSerializer, LoginSerializer, RegisterSerializer, ChangePasswordSerializer,
)
from .models import AuditLog, Department, log_audit
from backend.apps.chat.models import ToolCallBody
from backend.apps.common.media_auth import set_media_cookie

User = get_user_model()
logger = logging.getLogger(__name__)

# 工具调用全文保留期（PRD 9.2 #4）：到期由 clean_tool_bodies 硬删除，
# 查看接口据此区分「历史未留存」与「已到期清理」
TOOL_BODY_RETENTION_DAYS = 90


def revoke_user_refresh_tokens(user):
    """拉黑该用户全部 refresh token，让其它设备上的登录态失效。

    按 OutstandingToken 全量处理而不按 expires_at 过滤：登录「记住我」只改了 token
    载荷的 exp（30 天），库里的 expires_at 仍是默认 7 天，按库时间过滤会漏掉这类
    仍然有效但库里已过期的 token。已过期的 token 本就不可用，多写一条黑名单不改变行为。

    access token 是短时效无状态 token，无法也无需在此吊销：它最多再存活
    settings.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME']（当前 60 分钟），到期后必须走 refresh，
    而 refresh 此时已全部失效。不为此自建 access 黑名单。
    """
    revoked = 0
    for token in OutstandingToken.objects.filter(user=user):
        _, created = BlacklistedToken.objects.get_or_create(token=token)
        if created:
            revoked += 1
    return revoked


def _trusted_proxy_networks():
    networks = []
    for raw in getattr(settings, 'AUTH_TRUSTED_PROXIES', []):
        try:
            networks.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            logger.warning('AUTH_TRUSTED_PROXIES 中无法解析的条目已忽略: %s', raw)
    return networks


def _is_trusted_proxy(value, networks):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in networks)


def get_client_ip(request):
    """解析客户端 IP：X-Forwarded-For 只在直连对端是可信代理时才采信。

    信任边界（由 AUTH_TRUSTED_PROXIES 控制）：
    - 默认清单为空：完全忽略 XFF，只取 REMOTE_ADDR。客户端自带 X-Forwarded-For
      不能伪造审计来源。
    - 配置了可信代理且 REMOTE_ADDR 命中：要求 nginx 用 $proxy_add_x_forwarded_for
      追加链路；这里从右向左跳过可信代理，取第一个不可信地址，客户端预置在左侧的
      XFF 不会覆盖真实来源。
    - 解析不出合法 IP 时返回 None，审计留空，避免 PostgreSQL inet 字段写入失败。
    """
    remote = (request.META.get('REMOTE_ADDR') or '').strip()
    networks = _trusted_proxy_networks()
    if not networks or not _is_trusted_proxy(remote, networks):
        try:
            ipaddress.ip_address(remote)
        except ValueError:
            return None
        return remote

    hops = [
        hop.strip()
        for hop in (request.META.get('HTTP_X_FORWARDED_FOR') or '').split(',')
        if hop.strip()
    ]
    for hop in reversed(hops + [remote]):
        if _is_trusted_proxy(hop, networks):
            continue
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            continue
        return hop

    # 整条链路都命中可信代理（异常配置）：退回最左的合法地址，仍保证可审计
    for hop in hops:
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            continue
        return hop
    return None


def get_user_agent(request):
    return (request.META.get('HTTP_USER_AGENT') or '')[:512]


def _bool_text(value):
    """审计详情统一用 true/false，便于机器检索，也避免 Python 的 True/False 混入。"""
    return 'true' if value else 'false'


def _login_failure_key(username, ip):
    return f'login_fail:{username.strip().lower()}:{ip or "unknown"}'


def _bump_login_failure(key):
    window = max(int(getattr(settings, 'LOGIN_FAILURE_WINDOW_SECONDS', 300)), 1)
    attempts = int(cache.get(key, 0)) + 1
    cache.set(key, attempts, timeout=window)
    return attempts


class AuthViewSet(viewsets.GenericViewSet):
    """认证相关接口

    默认按受保护处理：新增 action 若未显式声明权限即为登录后可用。
    确需匿名可达的入口（login / register / refresh_token）必须显式声明 AllowAny，
    不依赖类级放开再逐个收口。
    """

    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['post'], permission_classes=[AllowAny])
    def login(self, request):
        """用户登录

        登录成功/失败都写审计（用户名、结果、IP、User-Agent），成功时更新 last_login。
        失败按「用户名（忽略大小写）+ 客户端 IP」节流：锁定期内即使口令正确也返回 429，
        不让调用方把锁定态当成口令是否正确的一种探测信号。
        """
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        username = serializer.validated_data['username']
        password = serializer.validated_data['password']
        ip = get_client_ip(request)
        user_agent = get_user_agent(request)
        # 节流键里的 IP 用原始 REMOTE_ADDR 兜底，保证取不到合法 IP 时仍能限速
        throttle_ip = ip or (request.META.get('REMOTE_ADDR') or '').strip()
        failure_key = _login_failure_key(username, throttle_ip)
        limit = int(getattr(settings, 'LOGIN_FAILURE_LIMIT', 5))

        if limit > 0 and int(cache.get(failure_key, 0)) >= limit:
            log_audit(
                actor=User.objects.filter(username=username).first(),
                action='login_failed',
                target=username,
                detail=json.dumps(
                    {'result': 'locked', 'reason': 'too_many_attempts'},
                    ensure_ascii=False,
                ),
                ip=ip,
                user_agent=user_agent,
            )
            return Response(
                {'detail': '尝试次数过多，请稍后再试'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        user = authenticate(username=username, password=password)

        # 认证失败一律拒绝：不再依据 DEBUG 放行，也不自动建号
        if user is None:
            attempts = _bump_login_failure(failure_key) if limit > 0 else 0
            log_audit(
                actor=User.objects.filter(username=username).first(),
                action='login_failed',
                target=username,
                detail=json.dumps({
                    'result': 'failed',
                    'reason': 'bad_credentials',
                    'attempts': attempts,
                    'limit': limit,
                }, ensure_ascii=False),
                ip=ip,
                user_agent=user_agent,
            )
            if limit > 0 and attempts >= limit:
                return Response(
                    {'detail': '尝试次数过多，请稍后再试'},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )
            return Response(
                {'detail': '用户名或密码错误'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        cache.delete(failure_key)
        user.last_login = timezone.now()
        user.save(update_fields=['last_login'])
        log_audit(
            actor=user,
            action='login_success',
            target=username,
            detail=json.dumps({'result': 'success'}, ensure_ascii=False),
            ip=ip,
            user_agent=user_agent,
        )

        refresh = RefreshToken.for_user(user)

        # 记住我：延长 refresh token 有效期
        if serializer.validated_data.get('remember'):
            from datetime import timedelta
            refresh.set_exp(lifetime=timedelta(days=30))

        return set_media_cookie(Response({
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'user': UserSerializer(user).data,
        }), user.id)

    @action(detail=False, methods=['post'], permission_classes=[AllowAny])
    def register(self, request):
        """用户注册（默认关闭，需环境变量 ALLOW_REGISTER=True 开启）"""
        if not getattr(settings, 'ALLOW_REGISTER', False):
            return Response(
                {'detail': '注册接口未开放，请联系管理员创建账号'},
                status=status.HTTP_403_FORBIDDEN
            )
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        refresh = RefreshToken.for_user(user)

        return set_media_cookie(Response({
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'user': UserSerializer(user).data,
        }, status=status.HTTP_201_CREATED), user.id)

    @action(detail=False, methods=['post'])
    def logout(self, request):
        """用户登出（将 refresh token 加入黑名单）"""
        try:
            refresh = request.data.get('refresh')
            if refresh:
                token = RefreshToken(refresh)
                token.blacklist()
        except Exception:
            pass
        log_audit(
            actor=request.user,
            action='logout',
            target=request.user.username,
            detail=json.dumps({'result': 'success'}, ensure_ascii=False),
            ip=get_client_ip(request),
            user_agent=get_user_agent(request),
        )
        return Response({'detail': '登出成功'})

    @action(detail=False, methods=['post'], permission_classes=[AllowAny])
    def refresh_token(self, request):
        """刷新 token（走标准序列化器）

        TokenRefreshSerializer 负责有效期与黑名单校验，并按 SIMPLE_JWT 配置执行轮换：
        ROTATE_REFRESH_TOKENS=True 时返回新的 refresh；BLACKLIST_AFTER_ROTATION=True
        时把旧 refresh 拉黑。手工取 access_token 会绕开这些逻辑，故不再使用。
        """
        serializer = TokenRefreshSerializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as exc:
            # TokenError 不是 DRF 的 APIException，不会被框架自动转成 401
            return Response(
                {'detail': str(exc)},
                status=status.HTTP_401_UNAUTHORIZED
            )
        response = Response(serializer.validated_data)
        # 轮换后 access 已换新，同步刷新 /media/ 鉴权 Cookie（失败不影响刷新本身）
        try:
            user_id = int(AccessToken(serializer.validated_data['access'])['user_id'])
        except Exception:
            return response
        return set_media_cookie(response, user_id)

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def me(self, request):
        """获取当前用户信息"""
        serializer = UserSerializer(request.user)
        return Response(serializer.data)

    @action(detail=False, methods=['put', 'patch'], permission_classes=[IsAuthenticated])
    def update_profile(self, request):
        """更新个人信息"""
        serializer = UserSerializer(
            request.user,
            data=request.data,
            partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @action(detail=False, methods=['post'], permission_classes=[IsAuthenticated], url_path='change-password')
    def change_password(self, request):
        """修改密码：强度校验走后端统一口径，成功后吊销该用户全部 refresh token"""
        serializer = ChangePasswordSerializer(
            data=request.data, context={'user': request.user},
        )
        serializer.is_valid(raise_exception=True)

        user = request.user
        if not user.check_password(serializer.validated_data['old_password']):
            return Response(
                {'detail': '原密码错误'},
                status=status.HTTP_400_BAD_REQUEST
            )

        user.set_password(serializer.validated_data['new_password'])
        user.save()
        revoked = revoke_user_refresh_tokens(user)
        # 全部 refresh 已拉黑（其它设备下线），再为当前设备签发一对新 token：
        # 否则当前标签页会在 access（最长 60 分钟）到期时被自己的改密操作踢下线。
        refresh = RefreshToken.for_user(user)
        log_audit(
            actor=user,
            action='password_change',
            target=user.username,
            detail=json.dumps(
                {'revoked_refresh_tokens': revoked, 'session_reissued': True},
                ensure_ascii=False),
            ip=get_client_ip(request),
            user_agent=get_user_agent(request),
        )

        return Response({
            'detail': '密码修改成功',
            'access': str(refresh.access_token),
            'refresh': str(refresh),
        })


class UserManageViewSet(viewsets.ModelViewSet):
    """用户管理（仅管理员）：用户的增删改查

    - 不能删除自己
    - 任何更新都不能让系统失去最后一个具备管理权限的账号（无论目标是不是自己）
    """

    serializer_class = AdminUserSerializer
    permission_classes = [IsAdminUser]

    @staticmethod
    def _would_leave_no_admin(target, data):
        """本次更新是否会把系统里最后一个具备管理权限的账号降级或停用

        「具备管理权限」口径：is_active 且（is_staff 或 is_superuser）。
        is_superuser 不经本接口修改，但既存的超级管理员同样计入，避免只盯着 is_staff。
        停用最后一个管理员与降级等价，因此一并拦截。
        """
        if not target.is_active or not (target.is_staff or target.is_superuser):
            return False
        new_staff = data.get('is_staff', target.is_staff)
        new_active = data.get('is_active', target.is_active)
        keeps_admin = new_active and (new_staff or target.is_superuser)
        if keeps_admin:
            return False
        others = User.objects.filter(is_active=True).filter(
            models.Q(is_staff=True) | models.Q(is_superuser=True)
        ).exclude(pk=target.pk)
        return not others.exists()

    def get_queryset(self):
        # 默认只看未删除账号；?deleted=1 查看保留用于追溯的已删除账号。
        # 已删除账号 is_active=False，本就不会被算进「最后一个管理员」。
        show_deleted = (self.request.query_params.get('deleted') or '').strip().lower() in (
            '1', 'true', 'yes')
        # 「是否最后一个管理员」用一次 EXISTS 子查询下发，避免序列化时逐行 count
        qs = User.objects.filter(is_deleted=show_deleted).annotate(has_other_active_admin=models.Exists(
            User.objects.filter(is_active=True)
            .filter(models.Q(is_staff=True) | models.Q(is_superuser=True))
            .exclude(pk=models.OuterRef('pk'))
        ))
        search = (self.request.query_params.get('search') or '').strip()
        if search:
            qs = qs.filter(
                models.Q(username__icontains=search)
                | models.Q(nickname__icontains=search)
                | models.Q(email__icontains=search)
                | models.Q(phone__icontains=search)
            )
        return qs.order_by('id')

    def perform_create(self, serializer):
        return serializer.save()

    def create(self, request, *args, **kwargs):
        resp = super().create(request, *args, **kwargs)
        if resp.status_code == status.HTTP_201_CREATED:
            log_audit(
                actor=request.user,
                action='user_create',
                target=resp.data['username'],
                detail=f"创建用户 {resp.data['username']}",
            )
        return resp

    def destroy(self, request, *args, **kwargs):
        """软删除：标记 is_deleted 并停用，不物理删行。

        会话（Conversation）、生成文件与审计外键都保留，追责链不断。
        与「停用」的区别：停用可随时重新启用；删除后账号从常规列表消失，
        仅在 ?deleted=1 的追溯视图可见，且不再占用任何登录能力。
        """
        target = self.get_object()
        if target.id == request.user.id:
            return Response(
                {'detail': '不能删除当前登录的管理员账号'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if self._would_leave_no_admin(target, {'is_active': False}):
            return Response(
                {'detail': '不能删除系统中最后一个具备管理权限的账号'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        username = target.username
        target.is_deleted = True
        target.is_active = False
        target.deleted_at = timezone.now()
        target.deleted_by = request.user
        target.save(update_fields=['is_deleted', 'is_active', 'deleted_at', 'deleted_by'])
        revoked = revoke_user_refresh_tokens(target)
        log_audit(
            actor=request.user,
            action='user_delete',
            target=username,
            detail=(f'删除用户 {username}（软删除：账号已停用，历史会话与审计保留用于追溯；'
                    f'已吊销 {revoked} 个 refresh token）'),
            ip=get_client_ip(request),
            user_agent=get_user_agent(request),
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['get'], url_path='sessions')
    def sessions(self, request, pk=None):
        """查看某用户当前活跃登录（仅管理员）

        数据源是 SimpleJWT 的 OutstandingToken，不新增会话表：
        - 签发时间 / 过期时间取令牌库与令牌自身 exp（「记住我」的 30 天只有载荷里有）
        - 已拉黑（登出/轮换/强制下线）或已过期的令牌不算活跃
        - 最后使用时间：系统未在每次请求时更新时间戳，明确返回 null，不伪造
        - IP / User-Agent：按签发时间关联该用户最近一次认证审计（登录成功/改密），
          这是尽力关联，不保证精确对应，返回值里带说明
        """
        target = self.get_object()
        blacklisted_ids = set(
            BlacklistedToken.objects.filter(token__user=target)
            .values_list('token_id', flat=True)
        )
        audits = list(
            AuditLog.objects.filter(
                actor=target, action__in=['login_success', 'password_change'],
            ).order_by('created_at').values('created_at', 'action', 'ip', 'user_agent')
        )
        items = []
        for token in OutstandingToken.objects.filter(user=target).order_by('-created_at'):
            if token.id in blacklisted_ids:
                continue
            try:
                decoded = RefreshToken(token.token)
            except TokenError:
                # 载荷已过期（含「记住我」30 天）或签名不可解析，均不属于活跃会话
                continue
            expires_at = token.expires_at
            exp = decoded.get('exp')
            if exp:
                expires_at = datetime.fromtimestamp(exp, tz=datetime_timezone.utc)
            matched = None
            for audit in reversed(audits):
                if audit['created_at'] <= token.created_at:
                    matched = audit
                    break
            items.append({
                'id': token.id,
                'jti': token.jti,
                'created_at': token.created_at,
                'expires_at': expires_at,
                'last_used': None,
                'ip': matched['ip'] if matched else None,
                'user_agent': matched['user_agent'] if matched else '',
                'source_action': matched['action'] if matched else None,
                'source_note': (
                    '按签发时间关联到最近一次认证审计，不保证精确对应' if matched
                    else '未关联到认证审计（可能由刷新增发，或对应登录日志已被清理）'
                ),
            })
        return Response({
            'count': len(items),
            'results': items,
            'last_used_supported': False,
            'last_used_note': '系统未记录令牌的最后使用时间；签发与过期时间来自令牌库。',
            'correlation_note': 'IP / User-Agent 为尽力关联结果，仅供参考。',
        })

    @action(detail=True, methods=['post'], url_path='revoke-sessions')
    def revoke_sessions(self, request, pk=None):
        """强制下线：吊销该用户全部 refresh token，但不改变账号启停状态。

        与「停用账号」是两个独立动作：停用会阻断后续登录并连带吊销令牌，
        这里只清当前登录，账号仍可立即重新登录。管理员操作一律留痕。
        """
        target = self.get_object()
        revoked = revoke_user_refresh_tokens(target)
        log_audit(
            actor=request.user,
            action='force_logout',
            target=target.username,
            detail=(f'管理员强制下线：吊销 {revoked} 个 refresh token；'
                    f'账号启用状态未改变（is_active={_bool_text(target.is_active)}）'),
            ip=get_client_ip(request),
            user_agent=get_user_agent(request),
        )
        return Response({
            'revoked': revoked,
            'detail': f'已强制下线：吊销 {revoked} 个会话令牌，账号状态未改变',
        })

    def update(self, request, *args, **kwargs):
        return self._guarded_update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        kwargs['partial'] = True
        return self._guarded_update(request, *args, **kwargs)

    def _guarded_update(self, request, *args, **kwargs):
        target = self.get_object()
        if self._would_leave_no_admin(target, request.data):
            return Response(
                {'detail': '不能降级或停用系统中最后一个具备管理权限的账号'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        old_department = target.department
        old_active = target.is_active
        old_staff = target.is_staff
        resp = super().update(request, *args, **kwargs)
        if resp.status_code == status.HTTP_200_OK:
            ip = get_client_ip(request)
            user_agent = get_user_agent(request)
            revoked = None
            if 'password' in request.data:
                # 管理员重置密码等同一次强制下线：不吊销的话，攻击者手里的旧 refresh
                # 仍可继续续签（自助改密已吊销，这里必须同口径）。
                # 残余窗口：已签发的 access 是无状态 token，仍会存活到自身过期，
                # 最长 settings.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME']（当前 60 分钟）；
                # 到期后必须走 refresh，而 refresh 已全部失效。
                revoked = revoke_user_refresh_tokens(target)
            new_active = bool(resp.data.get('is_active', old_active))
            new_staff = bool(resp.data.get('is_staff', old_staff))
            deactivated = ('is_active' in request.data
                           and old_active and not new_active)
            disabled_revoked = (revoke_user_refresh_tokens(target)
                                if deactivated else 0)
            detail_parts = []
            if 'password' in request.data:
                # 只记录「重置过密码」，不记录任何密码值
                detail_parts.append(f'重置密码（已吊销 {revoked} 个 refresh token）')
            if 'is_staff' in request.data and new_staff != old_staff:
                detail_parts.append(
                    f'角色变更（is_staff: {_bool_text(old_staff)} → {_bool_text(new_staff)}）')
            if 'is_active' in request.data and new_active != old_active:
                detail_parts.append(
                    f'状态变更（is_active: {_bool_text(old_active)} → {_bool_text(new_active)}）')
            if 'department' in request.data and request.data.get('department') != old_department:
                detail_parts.append(f"部门变更：{old_department or '无'} → {request.data.get('department') or '无'}")
            log_audit(
                actor=request.user,
                action='user_update',
                target=target.username,
                detail='；'.join(detail_parts) or '修改用户信息',
                ip=ip,
                user_agent=user_agent,
            )

            # 认证相关动作单独留痕，便于审计面板按动作精确筛选
            if 'password' in request.data:
                log_audit(
                    actor=request.user,
                    action='password_reset',
                    target=target.username,
                    detail=f'管理员重置密码，已吊销 {revoked} 个 refresh token',
                    ip=ip,
                    user_agent=user_agent,
                )
            if 'is_active' in request.data and new_active != old_active:
                if new_active:
                    log_audit(
                        actor=request.user,
                        action='user_enable',
                        target=target.username,
                        detail=(f'启用账号（is_active: {_bool_text(old_active)} → '
                                f'{_bool_text(new_active)}）'),
                        ip=ip,
                        user_agent=user_agent,
                    )
                else:
                    log_audit(
                        actor=request.user,
                        action='user_disable',
                        target=target.username,
                        detail=(f'停用账号（is_active: {_bool_text(old_active)} → '
                                f'{_bool_text(new_active)}；已吊销 {disabled_revoked} 个 refresh token）'),
                        ip=ip,
                        user_agent=user_agent,
                    )
            force_reasons = []
            if 'password' in request.data:
                force_reasons.append('管理员重置密码')
            if deactivated:
                force_reasons.append('账号停用')
            if force_reasons:
                log_audit(
                    actor=request.user,
                    action='force_logout',
                    target=target.username,
                    detail='；'.join(force_reasons) + '，全部 refresh token 已失效',
                    ip=ip,
                    user_agent=user_agent,
                )
        return resp


class DepartmentViewSet(viewsets.ModelViewSet):
    """部门管理（仅管理员）"""

    queryset = Department.objects.all()
    serializer_class = DepartmentSerializer
    permission_classes = [IsAdminUser]

    def perform_create(self, serializer):
        return serializer.save()

    def create(self, request, *args, **kwargs):
        resp = super().create(request, *args, **kwargs)
        if resp.status_code == status.HTTP_201_CREATED:
            log_audit(
                actor=request.user,
                action='department_create',
                target=resp.data['name'],
                detail=f"创建部门 {resp.data['name']}",
            )
        return resp

    def update(self, request, *args, **kwargs):
        old_name = self.get_object().name
        resp = super().update(request, *args, **kwargs)
        if resp.status_code == status.HTTP_200_OK:
            new_name = resp.data['name']
            detail = f"部门重命名：{old_name} → {new_name}" if old_name != new_name else f"更新部门 {new_name}"
            # 同步本应用内 User.department 的字符串值（知识库侧字符串保持原样，由管理员手动维护）
            if old_name != new_name:
                User.objects.filter(department=old_name).update(department=new_name)
            log_audit(
                actor=request.user,
                action='department_update',
                target=new_name,
                detail=detail,
            )
        return resp

    def destroy(self, request, *args, **kwargs):
        target = self.get_object()
        name = target.name
        resp = super().destroy(request, *args, **kwargs)
        if resp.status_code == status.HTTP_204_NO_CONTENT:
            log_audit(
                actor=request.user,
                action='department_delete',
                target=name,
                detail=f"删除部门 {name}",
            )
        return resp


class AuditLogViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """操作日志（仅管理员）：只读列表，按时间倒序

    F3 检索参数与前端 Settings/AuditPanel 对齐：
    actor（用户名/昵称模糊）、action（精确）、target（对象名模糊）、
    date_start / date_end（YYYY-MM-DD，按发生日期闭区间）。
    """

    serializer_class = AuditLogSerializer
    permission_classes = [IsAdminUser]

    def get_queryset(self):
        # 同一秒内连续写入很常见（如批量操作），只按 created_at 排序会产生并列，
        # 翻页时可能出现重复或漏项，因此加 id 作次级排序
        qs = AuditLog.objects.select_related('actor').order_by('-created_at', '-id')
        # 列表只给「是否有全文」的布尔，不拖正文；Exists 子查询一次判定，避免 N+1
        qs = qs.annotate(has_body=models.Exists(
            ToolCallBody.objects.filter(audit_id=models.OuterRef('pk'))))
        params = self.request.query_params

        actor = (params.get('actor') or '').strip()
        if actor:
            qs = qs.filter(
                models.Q(actor__username__icontains=actor)
                | models.Q(actor__nickname__icontains=actor)
            )

        action = (params.get('action') or '').strip()
        if action:
            qs = qs.filter(action=action)

        target = (params.get('target') or '').strip()
        if target:
            qs = qs.filter(target__icontains=target)

        for key, lookup in (('date_start', 'created_at__date__gte'),
                            ('date_end', 'created_at__date__lte')):
            value = (params.get(key) or '').strip()
            if not value:
                continue
            try:
                qs = qs.filter(**{lookup: datetime.strptime(value, '%Y-%m-%d').date()})
            except ValueError:
                continue

        return qs

    @action(detail=True, methods=['get'])
    def body(self, request, pk=None):
        """查看某条工具调用的返回全文（PRD 9.2 #4：查看即留痕）

        - 仅管理员（与审计列表一致，类级 permission_classes 已生效）
        - 读到正文并准备返回后才写 tool_body_view 留痕，detail 不存正文
        - 留痕写失败只告警，不让查看接口 500
        - 404 区分「历史未留存」与「已到期清理（90 天）」，且不写留痕
        """
        log = self.get_object()
        try:
            record = log.tool_body
        except ToolCallBody.DoesNotExist:
            expired = log.created_at < timezone.now() - timedelta(
                days=TOOL_BODY_RETENTION_DAYS)
            message = ('全文已到期清理（保留 90 天），仅保留审计摘要' if expired
                       else '该记录未留存全文（历史数据或非工具调用）')
            return Response({'detail': message, 'code': 'tool_body_unavailable'},
                            status=status.HTTP_404_NOT_FOUND)

        try:
            log_audit(
                actor=request.user,
                action='tool_body_view',
                target=f'工具调用:{log.pk}',
                detail=json.dumps({
                    'audit_id': log.pk,
                    'conversation_id': record.conversation_id,
                    'call_id': record.call_id,
                    'tool': record.tool,
                    'result_chars': record.result_chars,
                }, ensure_ascii=False),
            )
        except Exception:
            logger.exception('查看全文留痕写入失败 audit=%s', log.pk)

        return Response({
            'id': log.pk,
            'conversation_id': record.conversation_id,
            'call_id': record.call_id,
            'tool': record.tool,
            'state': record.state,
            'egress': record.egress,
            'created_at': record.created_at,
            'result': record.result,
            'result_chars': record.result_chars,
            'result_sha256': record.result_sha256,
            'storage_truncated': record.storage_truncated,
        })

    @action(detail=False, methods=['post'], permission_classes=[IsAuthenticated],
            url_path='sensitive-reveal')
    def sensitive_reveal(self, request):
        """界面展开敏感内容（脱敏掩码后的原文）前先留痕：谁、何时、看了哪条

        存储口径为「原文照常入库，只在展示层掩码」，因此这里只补齐可追溯性：
        detail 只记对象标识与字段名，不记被展开的正文本身。

        - scope=audit：审计详情 / 工具返回全文的展开，沿用审计页的管理员判定
        - scope=chat：会话过程区工具参数与结果的展开，沿用会话归属判定
        """
        scope = (request.data.get('scope') or '').strip()
        field = (request.data.get('field') or '').strip()[:32]
        if scope not in ('audit', 'chat') or not field:
            return Response({'detail': 'scope 必须是 audit 或 chat，且需提供 field'},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            masked_fields = int(request.data.get('masked_fields') or 0)
        except (TypeError, ValueError):
            masked_fields = 0

        if scope == 'audit':
            if not request.user.is_staff:
                return Response({'detail': '仅管理员可展开审计详情'},
                                status=status.HTTP_403_FORBIDDEN)
            try:
                audit_id = int(request.data.get('audit_id'))
            except (TypeError, ValueError):
                return Response({'detail': 'audit_id 不合法'},
                                status=status.HTTP_400_BAD_REQUEST)
            log = AuditLog.objects.filter(pk=audit_id).first()
            if log is None:
                return Response({'detail': '审计记录不存在'},
                                status=status.HTTP_404_NOT_FOUND)
            target = f'审计详情:{log.pk}'
            detail = {'scope': scope, 'audit_id': log.pk, 'field': field,
                      'masked_fields': masked_fields}
        else:
            call_id = (request.data.get('call_id') or '').strip()[:64]
            conversation_id = request.data.get('conversation_id')
            if conversation_id in (None, ''):
                # 试聊不落库，没有会话可校验，只按调用标识留痕
                target = f'试聊工具调用:{call_id or "未知"}'
                detail = {'scope': scope, 'call_id': call_id, 'field': field,
                          'masked_fields': masked_fields}
            else:
                try:
                    conversation_id = int(conversation_id)
                except (TypeError, ValueError):
                    return Response({'detail': 'conversation_id 不合法'},
                                    status=status.HTTP_400_BAD_REQUEST)
                # 与会话列表同一口径：只能看自己的会话，不存在与无权限同样返回 404
                from backend.apps.chat.models import Conversation
                if not Conversation.objects.filter(
                        pk=conversation_id, user=request.user).exists():
                    return Response({'detail': '会话不存在'},
                                    status=status.HTTP_404_NOT_FOUND)
                target = f'工具调用:{call_id or "未知"}'
                detail = {'scope': scope, 'conversation_id': conversation_id,
                          'call_id': call_id, 'field': field,
                          'masked_fields': masked_fields}

        # 留痕失败直接报错：前端据此保持掩码，避免「看了但查不到」
        log_audit(actor=request.user, action='sensitive_reveal', target=target,
                  detail=json.dumps(detail, ensure_ascii=False))
        return Response({'ok': True})
