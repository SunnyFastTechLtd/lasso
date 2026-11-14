"""MCP 工具来源接口（B1–B8）

权限口径（PRD 第 3 章 + 7.6）：读接口登录即可（用于装配页选择器），
写接口、命令前缀清单、出网开关一律仅平台管理员。
"""
import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response

from backend.apps.common.choices import Visibility
from backend.apps.common.pagination import StandardPagination
from backend.apps.mcp import services
from backend.apps.mcp.models import MCPServer
from backend.apps.mcp.serializers import (
    MCPServerSerializer, MCPToolSerializer,
)
from backend.apps.users.models import log_audit

logger = logging.getLogger(__name__)

WRITE_ACTIONS = {'create', 'update', 'partial_update', 'destroy', 'test',
                 'toggle_tools', 'disable', 'enable', 'command_prefixes',
                 'egress', 'export', 'import_servers'}

# 导出格式版本：字段口径变更时递增，导入端按版本提示
EXPORT_VERSION = 1
# 导出字段：secrets_enc / 掩码值一律不出现在导出体中（B5 / PRD 6.5）
EXPORT_FIELDS = ('name', 'description', 'transport', 'url', 'headers',
                 'egress_domains', 'command', 'args', 'cwd', 'is_stateful',
                 'lifecycle', 'timeout_seconds', 'visibility')


def _error_summary(errors) -> str:
    """把 DRF 校验错误压成一行，便于导入结果逐条回显"""
    if isinstance(errors, dict):
        parts = []
        for field, value in errors.items():
            detail = _error_summary(value)
            parts.append(detail if field == 'non_field_errors' else f'{field}: {detail}')
        return '；'.join(parts)
    if isinstance(errors, (list, tuple)):
        return '；'.join(_error_summary(v) for v in errors)
    return str(errors)


def visible_server_query(user):
    """非管理员的来源可见性过滤：公开 + 自己创建 + 命中可见部门

    部门维度含子部门（A6）：用户所在部门的任一上级被授予可见性，
    该用户同样可见，因此比对集合取"用户部门 + 其全部祖先部门"。
    """
    from backend.apps.users.models import Department
    q = Q(visibility=Visibility.PUBLIC) | Q(created_by=user)
    dept = Department.objects.filter(name=user.department).first() if user.department else None
    if dept:
        related = {dept.pk}
        while dept.parent_id:
            dept = Department.objects.filter(pk=dept.parent_id).first()
            if dept is None:
                break
            related.add(dept.pk)
        q |= Q(visibility=Visibility.DEPARTMENT, allowed_departments__in=related)
    return q


class MCPServerViewSet(viewsets.ModelViewSet):
    """工具来源：CRUD / 连通性测试 / 工具启停 / 引用检查 / 出网闸门"""

    serializer_class = MCPServerSerializer
    pagination_class = StandardPagination
    permission_classes = [IsAuthenticated]
    lookup_field = 'pk'

    def get_permissions(self):
        if self.action in WRITE_ACTIONS:
            return [IsAdminUser()]
        return [IsAuthenticated()]

    def get_queryset(self):
        user = self.request.user
        qs = MCPServer.objects.all()
        if not (user.is_staff or user.is_superuser):
            qs = qs.filter(visible_server_query(user)).distinct()
        # 列表类查询参数只对 list 生效：详情动作（如 tools/?search=）自带同名参数，
        # get_object() 会走 filter_queryset，若在此处过滤会导致 404
        if self.action == 'list':
            keyword = (self.request.query_params.get('search') or '').strip()
            if keyword:
                qs = qs.filter(Q(name__icontains=keyword) | Q(description__icontains=keyword))
            state = (self.request.query_params.get('status') or '').strip()
            if state:
                qs = qs.filter(status=state)
            transport = (self.request.query_params.get('transport') or '').strip()
            if transport:
                qs = qs.filter(transport=transport)
        return qs.order_by('-is_builtin', 'name')

    # ─── CRUD ───

    def create(self, request, *args, **kwargs):
        """B1/B2 注册；B3 保存后自动执行一次连通性测试（出网被闸门拦截时除外）"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        server = serializer.save()
        log_audit(actor=request.user, action='mcp_create',
                  target=f'工具来源:{server.name}',
                  detail=f'连接类型 {server.get_transport_display()}，'
                         f'出网标记 {server.requires_egress}')
        check = services.test_server(server, actor=request.user)
        server.refresh_from_db()
        response = self.get_serializer(server).data
        response['connectivity_test'] = check
        return Response(response, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        server = self.get_object()
        before_status = server.status
        serializer = self.get_serializer(server, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        had_secrets = bool(server.secrets_enc)
        server = serializer.save()
        if bool(server.secrets_enc) != had_secrets or 'secrets' in request.data:
            log_audit(actor=request.user, action='mcp_secret_update',
                      target=f'工具来源:{server.name}',
                      detail='密钥整值替换（值不落审计）' if server.secrets_enc else '密钥已清空')
        if not partial:
            # 关键连接参数变更会使既有验证失效，状态回退未验证，需重新测试
            touched = {'transport', 'url', 'command', 'args', 'cwd'} & set(request.data)
            if touched and before_status == 'healthy':
                server.status = 'unknown'
                server.last_error = '连接参数已变更，需重新测试'
                server.save(update_fields=['status', 'last_error'])
        log_audit(actor=request.user, action='mcp_update',
                  target=f'工具来源:{server.name}', detail=f'更新字段 {sorted(request.data)}')
        server.refresh_from_db()
        return Response(self.get_serializer(server).data)

    def destroy(self, request, *args, **kwargs):
        """B6：存在引用时默认阻断；显式 confirm_unmount 才删除并逐项摘除挂载"""
        server = self.get_object()
        if server.is_builtin:
            raise ValidationError({'detail': '内置演示来源不可删除，正式交付请停用隐藏'})
        refs = services.server_references(server)
        confirmed = str(request.query_params.get('confirm_unmount', '')).lower() in ('1', 'true')
        if refs and not confirmed:
            return Response({
                'detail': f'该来源被 {len(refs)} 个智能体引用，默认阻断删除',
                'code': 'mcp_server_referenced',
                'references': refs,
                'options': ['只停用不删除（POST …/disable/）',
                            '确认删除并摘除挂载（DELETE …?confirm_unmount=true）'],
            }, status=status.HTTP_409_CONFLICT)

        with transaction.atomic():
            unmounted = services.unmount_server(server, actor=request.user) if refs else 0
            name = server.name
            server.delete()
        log_audit(actor=request.user, action='mcp_delete', target=f'工具来源:{name}',
                  detail=f'已删除；从 {unmounted} 个智能体摘除挂载，历史会话调用记录保留')
        return Response(status=status.HTTP_204_NO_CONTENT)

    # ─── B3 连通性测试与工具发现 ───

    @action(detail=True, methods=['post'], url_path='test')
    def test(self, request, pk=None):
        server = self.get_object()
        result = services.test_server(server, actor=request.user)
        server.refresh_from_db()
        result['server'] = self.get_serializer(server).data
        return Response(result)

    # ─── B4 工具清单与启停 ───

    @action(detail=True, methods=['get'], url_path='tools')
    def tools(self, request, pk=None):
        server = self.get_object()
        qs = server.tools.all()
        keyword = (request.query_params.get('search') or '').strip()
        if keyword:
            qs = qs.filter(name__icontains=keyword)
        state = (request.query_params.get('state') or '').strip()
        if state:
            qs = qs.filter(state=state)
        page = self.paginate_queryset(qs)
        data = MCPToolSerializer(page, many=True).data
        if page is not None:
            return self.get_paginated_response(data)
        return Response(data)

    @action(detail=True, methods=['post'], url_path='tools/toggle')
    def toggle_tools(self, request, pk=None):
        """按工具名启停；支持 names 批量与 select_all/invert 全选举反选"""
        server = self.get_object()
        names = request.data.get('names')
        if names is None and request.data.get('tool_name'):
            names = [request.data['tool_name']]
        if not isinstance(names, list) or not names:
            if request.data.get('select_all') is not None or request.data.get('invert'):
                present = list(server.tools.filter(state='present').values_list('name', flat=True))
                current = set(server.enabled_tools or [])
                if request.data.get('invert'):
                    names = [n for n in present if n not in current]
                else:
                    names = [] if request.data['select_all'] is False else present
            else:
                raise ValidationError({'names': '请提供要变更的工具名列表'})
        enabled = bool(request.data.get('enabled', True))

        rows = list(server.tools.filter(name__in=names))
        found = {r.name for r in rows}
        unknown = [n for n in names if n not in found]
        if unknown:
            raise ValidationError({'names': f'来源内不存在这些工具：{"、".join(unknown)}'})
        stale = [r.name for r in rows if r.state == 'missing']
        if stale and enabled:
            raise ValidationError({'detail': f'工具已失效，不可启用：{"、".join(stale)}'})

        with transaction.atomic():
            for row in rows:
                row.enabled = enabled
                row.save(update_fields=['enabled'])
            merged = set(server.enabled_tools or [])
            if enabled:
                merged.update(names)
            else:
                merged.difference_update(names)
            server.enabled_tools = sorted(merged)
            server.save(update_fields=['enabled_tools'])
        server.refresh_from_db()
        log_audit(actor=request.user, action='mcp_tool_toggle',
                  target=f'工具来源:{server.name}',
                  detail=f'{"启用" if enabled else "关闭"} {len(names)} 个工具：'
                         f'{"、".join(names)[:300]}')
        return Response({'enabled_tools': server.enabled_tools,
                         'changed': names, 'enabled': enabled,
                         'server': self.get_serializer(server).data})

    # ─── 停用 / 恢复 ───

    @action(detail=True, methods=['post'])
    def disable(self, request, pk=None):
        server = self.get_object()
        server.status = 'disabled'
        server.is_active = False
        server.last_error = '管理员已停用该来源'
        server.save(update_fields=['status', 'is_active', 'last_error'])
        log_audit(actor=request.user, action='mcp_disable',
                  target=f'工具来源:{server.name}', detail='已停用，视为未挂载')
        return Response(self.get_serializer(server).data)

    @action(detail=True, methods=['post'])
    def enable(self, request, pk=None):
        server = self.get_object()
        server.status = 'unknown'
        server.is_active = True
        server.last_error = ''
        server.save(update_fields=['status', 'is_active', 'last_error'])
        log_audit(actor=request.user, action='mcp_disable',
                  target=f'工具来源:{server.name}', detail='恢复为未验证，需重新测试')
        return Response(self.get_serializer(server).data)

    # ─── B6 引用检查 / B2 白名单 / B8 开关 ───

    @action(detail=True, methods=['get'], url_path='references')
    def references(self, request, pk=None):
        refs = services.server_references(self.get_object())
        return Response({'count': len(refs), 'references': refs})

    # ─── 跨环境导出 / 导入（密钥不参与导出） ───

    @action(detail=False, methods=['get'], url_path='export')
    def export(self, request):
        """导出工具来源配置：只含非敏感字段，secrets_enc 永不出现

        这是"换环境重建来源"的迁移手段，不是备份：密钥必须在目标环境重新填写。
        """
        servers = self.get_queryset().order_by('name')
        rows = []
        for server in servers:
            row = {field: getattr(server, field) for field in EXPORT_FIELDS}
            row['allowed_departments'] = list(
                server.allowed_departments.values_list('name', flat=True))
            row['has_secrets'] = bool(server.secrets_enc)
            row['is_builtin'] = server.is_builtin
            rows.append(row)
        with_secrets = sum(1 for r in rows if r['has_secrets'])
        return Response({
            'version': EXPORT_VERSION,
            'exported_at': timezone.now().isoformat(),
            'count': len(rows),
            'servers': rows,
            'note': ('密钥不参与导出，导入后需在目标环境重新填写；'
                     f'本次有 {with_secrets} 个来源原本配置了密钥'),
        })

    @action(detail=False, methods=['post'], url_path='import')
    def import_servers(self, request):
        """按名称去重导入：已存在的来源跳过，不覆盖目标环境的现有配置

        导入不自动发起连通性测试，来源落库后状态为"未验证"，
        由管理员在目标环境手动测试并重新勾选工具。
        """
        payload = request.data
        if isinstance(payload, dict):
            version = payload.get('version')
            rows = payload.get('servers')
        else:
            version, rows = None, payload
        if not isinstance(rows, list) or not rows:
            raise ValidationError({'servers': '请提供 servers 数组'})
        if version is not None and version != EXPORT_VERSION:
            raise ValidationError({
                'version': f'不支持的导出格式版本 {version}，当前仅支持 {EXPORT_VERSION}'})

        created, skipped, errors = [], [], []
        for index, raw in enumerate(rows):
            if not isinstance(raw, dict):
                errors.append({'index': index, 'name': '', 'detail': '单条配置需为对象'})
                continue
            name = str(raw.get('name') or '').strip()
            if not name:
                errors.append({'index': index, 'name': '', 'detail': '缺少名称'})
                continue
            if MCPServer.objects.filter(name=name).exists():
                skipped.append(name)
                continue

            data = {field: raw[field] for field in EXPORT_FIELDS if field in raw}
            data['name'] = name
            # 只接受部门名称：按名称映射到目标环境，名称缺失时整条拒绝，
            # 避免"指定部门"可见范围被静默改成空集合
            dept_names = raw.get('allowed_departments') or []
            if dept_names:
                from backend.apps.users.models import Department

                ids = list(Department.objects.filter(
                    name__in=dept_names).values_list('pk', flat=True))
                missing = [n for n in dept_names
                           if n not in set(Department.objects.filter(
                               pk__in=ids).values_list('name', flat=True))]
                if missing:
                    errors.append({'index': index, 'name': name,
                                   'detail': f'目标环境不存在这些部门：{"、".join(missing)}'})
                    continue
                data['allowed_department_ids'] = ids

            serializer = MCPServerSerializer(
                data=data, context=self.get_serializer_context())
            if not serializer.is_valid():
                errors.append({'index': index, 'name': name,
                               'detail': _error_summary(serializer.errors)})
                continue
            server = serializer.save()
            created.append(server.name)

        if created:
            log_audit(actor=request.user, action='mcp_import',
                      target='工具来源导入',
                      detail=f'新建 {len(created)} 个：{"、".join(created)[:200]}；'
                             f'跳过 {len(skipped)} 个；失败 {len(errors)} 个；密钥需重新填写')
        result = {'created': created, 'skipped': skipped, 'errors': errors,
                  'note': '导入来源未做连通性测试，请在目标环境逐个测试并重新勾选工具'}
        return Response(result, status=status.HTTP_201_CREATED if created
                        else status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='command-prefixes')
    def command_prefixes(self, request):
        """内置只读清单：管理端可见，界面不提供新增入口"""
        return Response({
            'editable': False,
            'policy': 'P0 为平台内置只读清单，新增前缀＝走版本发布；每次命中失败都入审计',
            'prefixes': services.command_prefix_whitelist(),
        })

    @action(detail=False, methods=['get', 'post'], url_path='egress')
    def egress(self, request):
        """全平台出网总开关 + 严格域名白名单开关（出厂均为关，B8 红线）"""
        if request.method == 'GET':
            return Response({
                'enabled': services.egress_allowed(),
                'strict_domains': services.egress_strict_enabled(),
                'blocked_count': MCPServer.objects.filter(
                    requires_egress=True, status='egress_blocked').count(),
                'note': ('严格白名单关闭时沿用原有行为（只看总开关）；'
                         '开启后出网来源只允许访问其声明的域名，且解析结果不得落私网段'),
            })
        changed = False
        if 'enabled' in request.data:
            services.set_egress_allowed(bool(request.data.get('enabled')), actor=request.user)
            changed = True
        if 'strict_domains' in request.data:
            services.set_egress_strict(bool(request.data.get('strict_domains')), actor=request.user)
            changed = True
        if not changed:
            raise ValidationError({'detail': '请提供 enabled 或 strict_domains'})
        return Response({
            'enabled': services.egress_allowed(),
            'strict_domains': services.egress_strict_enabled(),
            'detail': '出网策略已更新；严格白名单即时生效于下一次连通性测试与新会话',
        })

    # ─── 选择器（装配页用，只回可挂载来源） ───

    @action(detail=False, methods=['get'], url_path='mountable')
    def mountable(self, request):
        qs = self.get_queryset().filter(is_active=True).exclude(
            status__in=['disabled', 'egress_blocked'])
        data = [{
            'id': s.pk, 'name': s.name, 'slug': s.slug, 'status': s.status,
            'is_builtin': s.is_builtin, 'requires_egress': s.requires_egress,
            'warning': '' if s.status != 'unknown' else '存在未验证工具来源',
            'tools': [{'name': t.name, 'full_name': t.full_name,
                       'description': t.description}
                      for t in s.tools.filter(enabled=True, state='present')],
        } for s in qs.prefetch_related('tools')]
        return Response(data)
