"""智能体装配接口（A1–A8）

权限口径（PRD 第 3 章）：登录用户可建自己的智能体与查看对自己可见的已发布智能体；
草稿只有创建者与管理员可见（A1）；内置智能体只有管理员能改（普通用户走"复制新建"）。
删除只允许草稿与已归档，已发布必须先停用或归档，历史会话记录不受影响（A1）。
"""
import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response

from backend.apps.agenthub import builtin_agents, services
from backend.apps.agenthub.models import (
    BUILTIN_CAPABILITIES, AgentImportRequest, AgentMCPServer, AgentProfile, AgentSkill, AgentStatus,
    AgentVersion, ImportRequestStatus, RetrievalTier, SpeechTier,
)
from backend.apps.agenthub.serializers import (
    AgentDetailSerializer, AgentImportRequestDetailSerializer, AgentImportRequestSerializer,
    AgentListSerializer, AgentVersionDetailSerializer, AgentVersionSerializer, AgentWriteSerializer,
)
from backend.apps.common.choices import Visibility
from backend.apps.common.pagination import StandardPagination
from backend.apps.users.models import log_audit

logger = logging.getLogger(__name__)


class AgentProfileViewSet(viewsets.ModelViewSet):
    """智能体：装配 CRUD / 发布前检查 / 发布差异 / 只读版本历史"""

    permission_classes = [IsAuthenticated]
    pagination_class = StandardPagination
    lookup_field = 'pk'

    def get_serializer_class(self):
        if self.action == 'list':
            return AgentListSerializer
        if self.action in ('create', 'update', 'partial_update'):
            return AgentWriteSerializer
        if self.action in ('versions',):
            return AgentVersionSerializer
        if self.action == 'version_detail':
            return AgentVersionDetailSerializer
        return AgentDetailSerializer

    def get_queryset(self):
        user = self.request.user
        qs = AgentProfile.objects.select_related('owner_department', 'created_by',
                                                 'updated_by', 'published_by')
        qs = qs.filter(services.visible_agent_query(user)).distinct()
        if self.action == 'list':
            params = self.request.query_params
            keyword = (params.get('search') or '').strip()
            if keyword:
                qs = qs.filter(Q(name__icontains=keyword) | Q(description__icontains=keyword)
                               | Q(slug__icontains=keyword))
            state = (params.get('status') or '').strip()
            if state:
                qs = qs.filter(status__in=[s for s in state.split(',') if s])
            elif AgentStatus.ARCHIVED not in (params.get('search') or ''):
                qs = qs.exclude(status=AgentStatus.ARCHIVED)
            visibility = (params.get('visibility') or '').strip()
            if visibility:
                qs = qs.filter(visibility=visibility)
            tag = (params.get('job_tag') or '').strip()
            if tag:
                # SQLite 不支持 JSONField 的 contains，按数组索引匹配（标签数量有限）
                cond = Q()
                for i in range(10):
                    cond |= Q(**{f'job_tags__{i}': tag})
                qs = qs.filter(cond)
            if params.get('only_user_side') in ('1', 'true'):
                qs = qs.filter(status=AgentStatus.PUBLISHED)  # 对话页选择器（D1）
        return qs.order_by('-updated_at')

    def get_object(self):
        obj = super().get_object()
        if not services.can_see(self.request.user, obj):
            raise NotFound('智能体不存在，或草稿仅创建者与管理员可见')
        return obj

    def _require_edit(self, agent):
        if not services.can_edit(self.request.user, agent):
            raise PermissionDenied('只有创建者或平台管理员能修改该智能体；'
                                   '内置智能体请用"复制新建"得到自己的副本')

    # ─── A1/A2 列表与创建 ───

    def list(self, request, *args, **kwargs):
        """A1：挂载摘要 + 最近修改时间与修改人，列表不含提示词正文"""
        qs = self.get_queryset()
        page = self.paginate_queryset(qs)
        data = AgentListSerializer(page if page is not None else qs, many=True).data
        if page is not None:
            return self.get_paginated_response(data)
        return Response(data)

    def create(self, request, *args, **kwargs):
        serializer = AgentWriteSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            agent = serializer.save()
        log_audit(actor=request.user, action='agent_create',
                  target=f'智能体:{agent.name}',
                  detail=f'标识 {agent.slug}，可见范围 {agent.get_visibility_display()}，'
                         f'授权档位 {agent.get_permission_tier_display()}')
        return Response(AgentDetailSerializer(agent).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        agent = self.get_object()
        self._require_edit(agent)
        return self._apply_update(agent, request, partial)

    def partial_update(self, request, *args, **kwargs):
        agent = self.get_object()
        self._require_edit(agent)
        return self._apply_update(agent, request, True)

    def _apply_update(self, agent, request, partial):
        before = {'visibility': agent.visibility,
                  'department_ids': set(agent.allowed_departments.values_list('pk', flat=True)),
                  'include_sub': agent.include_sub_departments,
                  'preferences': list(agent.default_preferences or [])}
        mounts_before = self._mount_signature(agent)
        serializer = AgentWriteSerializer(agent, data=request.data, partial=partial,
                                         context={'request': request})
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            agent = serializer.save()
            self._sync_builtin_customized(agent)
        changed = sorted(request.data.keys())
        log_audit(actor=request.user, action='agent_update', target=f'智能体:{agent.name}',
                  detail=f'更新字段 {changed}；线上仍为 v{agent.current_version}，'
                         f'重新发布后新会话生效')
        departments_after = set(agent.allowed_departments.values_list('pk', flat=True))
        if before['visibility'] != agent.visibility or \
                departments_after != before['department_ids'] or \
                agent.include_sub_departments != before['include_sub']:
            log_audit(actor=request.user, action='agent_visibility',
                      target=f'智能体:{agent.name}',
                      detail=f'{Visibility(before["visibility"]).label} → '
                             f'{agent.get_visibility_display()}，可见部门 '
                             f'{len(before["department_ids"])} → {len(departments_after)} 个，'
                             f'变更即时生效')
        mounts_after = self._mount_signature(agent)
        if mounts_after != mounts_before:
            log_audit(actor=request.user, action='agent_mount', target=f'智能体:{agent.name}',
                      detail=f'挂载变更：{self._mount_diff_text(mounts_before, mounts_after)}；'
                             f'取消挂载即时对新会话生效，进行中会话保留开场快照')
        # D8：管理员改预置偏好要留痕（用户自己的增删不审计）
        prefs_after = list(agent.default_preferences or [])
        if prefs_after != before['preferences']:
            added = [p for p in prefs_after if p not in before['preferences']]
            removed = [p for p in before['preferences'] if p not in prefs_after]
            log_audit(actor=request.user, action='agent_preferences',
                      target=f'智能体:{agent.name}',
                      detail=f'默认偏好 {len(before["preferences"])} → {len(prefs_after)} 条；'
                             f'新增 {added}；移除 {removed}；对使用者下次新会话生效')
        return Response(AgentDetailSerializer(agent).data)

    @staticmethod
    def _sync_builtin_customized(agent):
        """内置智能体（4.2）：偏离出厂基线就置「已自定义」，回到基线就清"""
        if not agent.is_builtin:
            return
        defn = builtin_agents.factory_of(agent.name)
        if defn is None:
            return
        drifted = bool(builtin_agents.drifted_fields(agent, defn))
        if agent.is_customized != drifted:
            agent.is_customized = drifted
            agent.save(update_fields=['is_customized', 'updated_at'])

    @staticmethod
    def _mount_signature(agent) -> dict:
        return {
            'mcp': sorted((m.server.slug, tuple(sorted(m.tool_names or [])))
                          for m in agent.agentmcpserver_set.select_related('server')),
            'skills': sorted(agent.skills.values_list('slug', flat=True)),
            'kbs': sorted(int(i) for i in (agent.knowledge_base_ids or [])),
            'builtin': sorted(agent.builtin_capabilities or []),
        }

    @staticmethod
    def _mount_diff_text(before: dict, after: dict) -> str:
        parts = []
        for key, label in (('mcp', '工具来源'), ('skills', '技能'),
                           ('kbs', '知识库'), ('builtin', '内置能力')):
            added = set(after[key]) - set(before[key])
            removed = set(before[key]) - set(after[key])
            if added or removed:
                parts.append(f'{label} +{len(added)}/-{len(removed)}')
        return '、'.join(parts) or '无实质变化'

    def destroy(self, request, *args, **kwargs):
        """A1：删除不影响历史会话；已发布/已停用的智能体请先归档"""
        agent = self.get_object()
        self._require_edit(agent)
        if agent.is_builtin:
            raise ValidationError({'detail': '内置智能体不可删除，可归档隐藏'})
        if agent.status in (AgentStatus.PUBLISHED, AgentStatus.DISABLED):
            return Response({
                'detail': f'「{agent.get_status_display()}」的智能体不可直接删除，'
                          f'请先归档（保留历史会话与版本留档）',
                'code': 'agent_not_archived',
                'options': ['POST …/archive/ 归档', 'PATCH 改状态后再删除'],
            }, status=status.HTTP_409_CONFLICT)
        name, version = agent.name, agent.current_version
        with transaction.atomic():
            AgentMCPServer.objects.filter(agent=agent).delete()
            AgentSkill.objects.filter(agent=agent).delete()
            agent.delete()
        log_audit(actor=request.user, action='agent_delete', target=f'智能体:{name}',
                  detail=f'删除（最后版本 v{version}）；历史会话与调用记录保留')
        return Response(status=status.HTTP_204_NO_CONTENT)

    # ─── A7 发布前检查与发布 ───

    @action(detail=True, methods=['get', 'post'], url_path='publish-check')
    def publish_check(self, request, pk=None):
        """逐项三档结论（阻断红/警示黄/提示灰）+ 与线上版本的差异视图，不落库"""
        agent = self.get_object()
        return Response(services.prepublish_check(agent))

    @action(detail=True, methods=['post'])
    def publish(self, request, pk=None):
        """A7 发布：先检查，非首次发布必须先看过差异（diff_review）才允许落库

        警示项需要 risk_ack=true，勾选行为入审计；发布不改变进行中会话（7.3）。
        """
        agent = self.get_object()
        self._require_edit(agent)
        check = services.prepublish_check(agent)
        if not check['can_publish']:
            return Response({'detail': '发布前检查未通过，存在阻断项',
                             'code': 'prepublish_blocked', **check},
                            status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        if check['requires_ack'] and not request.data.get('risk_ack'):
            return Response({'detail': '存在未验证或异常的工具来源，需勾选「我已了解风险」后发布',
                             'code': 'risk_ack_required',
                             'ack_field': 'risk_ack', **check},
                            status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        previous = AgentVersion.objects.filter(agent=agent).order_by('-version').first()
        if previous is not None and not check['diff']['no_change'] and \
                not request.data.get('confirm_diff'):
            return Response({'detail': '请先确认这次改了什么（A7 差异视图），再执行发布',
                             'code': 'diff_review_required',
                             'diff': check['diff'], **check},
                            status=status.HTTP_409_CONFLICT)

        with transaction.atomic():
            version = agent.publish(actor=request.user,
                                     change_summary=services.change_summary_of(check['diff']))
        if request.data.get('risk_ack') and check['requires_ack']:
            log_audit(actor=request.user, action='agent_risk_ack', target=f'智能体:{agent.name}',
                      detail=f'发布 v{version.version} 时确认 {len(check["warnings"])} 项警示：'
                             f'{"；".join(w["message"] for w in check["warnings"])[:300]}')
        log_audit(actor=request.user, action='agent_publish', target=f'智能体:{agent.name}',
                  detail=f'发布 v{version.version}：{version.change_summary or "首次发布"}；'
                         f'进行中会话沿用开场快照，新会话用本次配置')
        return Response({'detail': f'已发布 v{version.version}',
                         'version': AgentVersionSerializer(version).data,
                         'diff': check['diff'], 'check': check,
                         'agent': AgentDetailSerializer(agent).data},
                        status=status.HTTP_201_CREATED)

    # ─── 状态流转（4.3 / A6 / A1） ───

    @action(detail=True, methods=['post'])
    def disable(self, request, pk=None):
        agent = self.get_object()
        self._require_edit(agent)
        if agent.status != AgentStatus.PUBLISHED:
            raise ValidationError({'detail': '只有已发布的智能体需要停用'})
        agent.status = AgentStatus.DISABLED
        agent.updated_by = request.user
        agent.save(update_fields=['status', 'updated_by', 'updated_at'])
        log_audit(actor=request.user, action='agent_disable', target=f'智能体:{agent.name}',
                  detail='用户侧选择器即时移除，历史会话仍可读')
        return Response(AgentDetailSerializer(agent).data)

    @action(detail=True, methods=['post'])
    def enable(self, request, pk=None):
        agent = self.get_object()
        self._require_edit(agent)
        if agent.status != AgentStatus.DISABLED:
            raise ValidationError({'detail': '只有已停用的智能体可恢复'})
        if agent.current_version:
            agent.status = AgentStatus.PUBLISHED
        else:
            agent.status = AgentStatus.DRAFT
        agent.updated_by = request.user
        agent.save(update_fields=['status', 'updated_by', 'updated_at'])
        log_audit(actor=request.user, action='agent_publish', target=f'智能体:{agent.name}',
                  detail=f'恢复为「{agent.get_status_display()}」，即时对新会话生效')
        return Response(AgentDetailSerializer(agent).data)

    @action(detail=True, methods=['post'])
    def archive(self, request, pk=None):
        agent = self.get_object()
        self._require_edit(agent)
        if agent.status == AgentStatus.ARCHIVED:
            raise ValidationError({'detail': '该智能体已归档'})
        before = agent.get_status_display()
        agent.status = AgentStatus.ARCHIVED
        agent.updated_by = request.user
        agent.save(update_fields=['status', 'updated_by', 'updated_at'])
        log_audit(actor=request.user, action='agent_archive', target=f'智能体:{agent.name}',
                  detail=f'由「{before}」归档；配置、版本历史与会话记录全部保留')
        return Response(AgentDetailSerializer(agent).data)

    @action(detail=True, methods=['post'])
    def restore(self, request, pk=None):
        """归档件恢复为草稿继续编辑（线上不再可见，需重新发布才回用户侧）"""
        agent = self.get_object()
        self._require_edit(agent)
        if agent.status != AgentStatus.ARCHIVED:
            raise ValidationError({'detail': '只有已归档的智能体需要恢复为草稿'})
        agent.status = AgentStatus.DRAFT
        agent.updated_by = request.user
        agent.save(update_fields=['status', 'updated_by', 'updated_at'])
        log_audit(actor=request.user, action='agent_restore', target=f'智能体:{agent.name}',
                  detail='归档 → 草稿，需重新发布才回到用户侧')
        return Response(AgentDetailSerializer(agent).data)

    @action(detail=True, methods=['post'], url_path='restore-builtin')
    def restore_builtin(self, request, pk=None):
        """4.2「恢复出厂」：把内置智能体的草稿配置写回出厂基线

        只动草稿，线上快照不变（7.3），需重新发布才影响用户侧；
        同时移除内置件上挂载的工具来源与技能，并清空对应白名单。
        """
        agent = self.get_object()
        self._require_edit(agent)
        if not agent.is_builtin:
            raise ValidationError({'detail': '只有内置智能体提供「恢复出厂」'})
        defn = builtin_agents.factory_of(agent.name)
        if defn is None:
            raise ValidationError({'detail': '当前平台版本中没有该内置智能体的出厂基线'})
        drifted = builtin_agents.drifted_fields(agent, defn)
        if not drifted:
            raise ValidationError({'detail': '该智能体仍是出厂配置，无需恢复'})
        with transaction.atomic():
            written = builtin_agents.apply_factory(agent, defn)
            removed = builtin_agents.clear_builtin_mounts(agent)
            services.sync_allow_list(agent)
            agent.is_customized = False
            agent.updated_by = request.user
            agent.save(update_fields=list(dict.fromkeys(
                written + ['is_customized', 'updated_by', 'updated_at'])))
        log_audit(actor=request.user, action='agent_update', target=f'智能体:{agent.name}',
                  detail=f'恢复出厂配置：改写 {written}，移除挂载 {removed} 条；'
                         f'线上仍为 v{agent.current_version}，重新发布后新会话生效')
        return Response(AgentDetailSerializer(agent).data)

    @action(detail=True, methods=['post'])
    def clone(self, request, pk=None):
        """A1 行操作"复制新建"：连挂载一起复制，产物一律为草稿"""
        source = self.get_object()
        base = (request.data.get('name') or f'{source.name} 副本').strip()
        occupant = services.occupant_of_name(base)
        if occupant is not None:
            raise ValidationError({'name': f'名称已被占用：{base}（'
                                          f'{occupant.get_status_display()}）'})
        if len(base) < 2 or len(base) > 30:
            raise ValidationError({'name': '名称需为 2–30 个字'})
        with transaction.atomic():
            copy = AgentProfile.objects.create(
                name=base, description=source.description, job_tags=list(source.job_tags or []),
                icon=source.icon, model_name=source.model_name,
                speech_tier=source.speech_tier, retrieval_tier=source.retrieval_tier,
                knowledge_mode=source.knowledge_mode,
                followup_rewrite=source.followup_rewrite,
                default_preferences=list(source.default_preferences or []),
                enable_thinking=source.enable_thinking, prompt=source.prompt,
                knowledge_base_ids=list(source.knowledge_base_ids or []),
                builtin_capabilities=list(source.builtin_capabilities or []),
                permission_tier=source.permission_tier,
                visibility=Visibility.PRIVATE, include_sub_departments=False,
                owner_department=source.owner_department,
                created_by=request.user, updated_by=request.user)
            for mount in source.agentmcpserver_set.all():
                AgentMCPServer.objects.create(agent=copy, server=mount.server,
                                              tool_names=list(mount.tool_names or []))
            for link in source.agentskill_set.all():
                AgentSkill.objects.create(agent=copy, skill=link.skill)
            services.sync_allow_list(copy)
        log_audit(actor=request.user, action='agent_create', target=f'智能体:{copy.name}',
                  detail=f'由「{source.name}」复制新建，配置为草稿，线上不受影响')
        return Response(AgentDetailSerializer(copy).data, status=status.HTTP_201_CREATED)

    # ─── 跨环境导出（密钥不参与导出） ───

    @action(detail=True, methods=['get'], url_path='export')
    def export(self, request, pk=None):
        """导出单个智能体的配置包：换环境重建用，不含任何密钥

        引用对象按目标环境可解析的口径导出（来源/技能 slug、知识库与部门名称）；
        导入端必须走单级审批，落库为草稿后再重新发布。
        """
        agent = self.get_object()
        package = services.export_package(agent)
        log_audit(actor=request.user, action='agent_export', target=f'智能体:{agent.name}',
                  detail=f'导出配置（v{agent.current_version}，{agent.get_status_display()}）；'
                         f'不含密钥，导入后需在目标环境重新填写并重新发布')
        return Response(package)

    # ─── A8 只读发布历史 ───

    @action(detail=True, methods=['get'])
    def versions(self, request, pk=None):
        """发布历史（P0 只读）：版本号/发布人/发布时间/变更摘要 + 分区差异"""
        agent = self.get_object()
        qs = agent.versions.select_related('published_by').order_by('-version')
        page = self.paginate_queryset(qs)
        data = AgentVersionSerializer(page if page is not None else qs, many=True).data
        if page is not None:
            return self.get_paginated_response(data)
        return Response(data)

    @action(detail=True, methods=['get'], url_path=r'versions/(?P<version>\d+)')
    def version_detail(self, request, pk=None, version=None):
        """查看某版本的完整配置（只读）"""
        agent = self.get_object()
        record = agent.versions.filter(version=version).select_related('published_by').first()
        if record is None:
            raise NotFound(f'智能体「{agent.name}」没有 v{version}')
        log_audit(actor=request.user, action='agent_version_diff', target=f'智能体:{agent.name}',
                  detail=f'查看 v{record.version} 配置快照')
        return Response(AgentVersionDetailSerializer(record).data)

    @action(detail=True, methods=['get'], url_path='versions/compare')
    def compare_versions(self, request, pk=None):
        """A8-1 任选两版本对比：逐分区差异 + 引用可用性标注（不写正文）"""
        agent = self.get_object()
        raw = {}
        for key in ('left', 'right'):
            value = request.query_params.get(key)
            if not str(value or '').isdigit():
                raise ValidationError({key: '请提供版本号（整数）'})
            raw[key] = int(value)
        if raw['left'] == raw['right']:
            raise ValidationError({'detail': '请选择两个不同的版本进行对比'})
        records = {r.version: r for r in agent.versions.filter(
            version__in=[raw['left'], raw['right']])}
        for label, number in (('left', raw['left']), ('right', raw['right'])):
            if number not in records:
                raise NotFound(f'智能体「{agent.name}」没有 v{number}')
        payload = services.compare_versions(records[raw['left']], records[raw['right']])
        log_audit(actor=request.user, action='agent_version_diff', target=f'智能体:{agent.name}',
                  detail=(f'对比 v{payload["base_version"]} ↔ v{payload["target_version"]}；'
                          f'变更分区：{payload["summary"] or "无"}；'
                          f'引用不可用 {payload["unavailable_count"]} 项、'
                          f'告警 {payload["warning_count"]} 项'))
        return Response({'agent_id': agent.pk, **payload})

    @action(detail=True, methods=['post'], url_path=r'versions/(?P<version>\d+)/rollback')
    def rollback_version(self, request, pk=None, version=None):
        """A8-2 以此版本重新发布：重跑 A7 检查 → 生成新版本号，不覆盖历史"""
        agent = self.get_object()
        self._require_edit(agent)
        record = agent.versions.filter(version=version).select_related('published_by').first()
        if record is None:
            raise NotFound(f'智能体「{agent.name}」没有 v{version}')
        if agent.status == AgentStatus.ARCHIVED:
            return Response({'detail': '已归档的智能体不能回滚，请先恢复为草稿',
                             'code': 'agent_archived'},
                            status=status.HTTP_409_CONFLICT)

        deps = services.snapshot_dependencies(
            record.snapshot or {}, version=record.version, agent=agent)
        blocking = [d for d in deps if d['level'] == 'blocking']
        warnings = [d for d in deps if d['level'] == 'warning']
        if blocking:
            return Response({
                'detail': '该版本引用的对象已不可用，回滚会得到不可运行的配置',
                'code': 'rollback_blocked',
                'version': record.version,
                'blocking': blocking, 'warnings': warnings,
                'dependencies': deps,
            }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        if warnings and not request.data.get('risk_ack'):
            return Response({
                'detail': '该版本引用的对象已降级（停用/改名/工具缺失），'
                          '需勾选「我已了解风险」后回滚',
                'code': 'risk_ack_required', 'ack_field': 'risk_ack',
                'version': record.version,
                'warnings': warnings, 'dependencies': deps,
            }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        with transaction.atomic():
            services.apply_snapshot(agent, record.snapshot or {}, request.user)
            check = services.prepublish_check(agent)
            if not check['can_publish']:
                transaction.set_rollback(True)
                return Response({
                    'detail': '回滚后的配置未通过 A7 发布检查',
                    'code': 'rollback_publish_blocked',
                    'version': record.version,
                    'blocking': blocking + check['blocking'],
                    'warnings': warnings + check['warnings'],
                    'check': check, 'dependencies': deps,
                }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
            if check['requires_ack'] and not request.data.get('risk_ack'):
                transaction.set_rollback(True)
                return Response({
                    'detail': '回滚后的配置存在警示项，需勾选「我已了解风险」',
                    'code': 'risk_ack_required', 'ack_field': 'risk_ack',
                    'version': record.version, **check, 'dependencies': deps,
                }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
            published = agent.publish(
                actor=request.user,
                change_summary=(f'回滚至 v{record.version}：'
                                + services.change_summary_of(check['diff'])))

        log_audit(actor=request.user, action='agent_rollback', target=f'智能体:{agent.name}',
                  detail=(f'以 v{record.version} 重新发布，生成 v{published.version}；'
                          f'勾选依赖告警 {len(warnings)} 项；'
                          f'变更：{published.change_summary or "无"}'))
        if request.data.get('risk_ack') and warnings:
            log_audit(actor=request.user, action='agent_risk_ack', target=f'智能体:{agent.name}',
                      detail=f'回滚 v{record.version} 时确认 {len(warnings)} 项依赖告警：'
                             f'{"；".join(w["message"] for w in warnings)[:300]}')
        return Response({
            'detail': f'已按 v{record.version} 重新发布为 v{published.version}',
            'version': AgentVersionSerializer(published).data,
            'source_version': record.version,
            'diff': check['diff'], 'check': check, 'dependencies': deps,
            'agent': AgentDetailSerializer(agent).data,
        }, status=status.HTTP_201_CREATED)

    # ─── 装配页选择器与候选 ───

    @action(detail=True, methods=['get'], url_path='mount-options')
    def mount_options(self, request, pk=None):
        """A4：四个分区候选（来源可展开逐个勾选工具、未启用技能灰置带原因）"""
        agent = self.get_object()
        payload = services.mount_options_for(agent, request.user)
        payload['conflicts'] = services.tool_conflicts(agent)
        return Response(payload)

    @action(detail=False, methods=['get'])
    def options(self, request):
        """A3/A5 选择器候选：模型（已验证可用）、三档预设、内置能力、占位符"""
        return Response({
            'models': services.verified_models(),
            'speech_tiers': [{'value': v, 'label': l} for v, l in SpeechTier.choices],
            'retrieval_tiers': [{'value': v, 'label': l,
                                 'max_rounds': services.RETRIEVAL_PRESETS[v]}
                                for v, l in RetrievalTier.choices],
            'permission_tiers': services.PERMISSION_TIER_INFO,
            'builtin_capabilities': [{'value': v, 'label': l} for v, l in BUILTIN_CAPABILITIES],
            'placeholders': services.PROMPT_PLACEHOLDERS,
            'visibilities': [{'value': Visibility.PRIVATE, 'label': '仅自己'},
                             {'value': Visibility.DEPARTMENT, 'label': '指定部门'},
                             {'value': Visibility.PUBLIC, 'label': '全企业'},
                             {'value': Visibility.ADMIN_ONLY,
                              'label': Visibility.ADMIN_ONLY.label,
                              'desc': '业务用户不可见，仅管理员在管理端维护',
                              'selectable': bool(request.user.is_staff
                                                 or request.user.is_superuser),
                              'reason': '' if (request.user.is_staff
                                               or request.user.is_superuser)
                                        else '仅平台管理员可选'}],
            'statuses': [{'value': v, 'label': l} for v, l in AgentStatus.choices],
            'prompt_required': '空提示词无法发布（A7 阻断项）',
        })

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """A1 列表页顶部统计与空状态引导（内置智能体 + 新建入口）"""
        return Response(services.status_summary(request.user))


class AgentImportRequestViewSet(viewsets.GenericViewSet,
                                mixins.ListModelMixin, mixins.RetrieveModelMixin):
    """智能体跨环境导入申请（单级审批）

    提交与生效分离：登录用户提交导入包，平台管理员且非提交人审批通过后
    才在目标环境创建草稿智能体；普通用户只能看到自己提交的申请。
    """

    permission_classes = [IsAuthenticated]
    pagination_class = StandardPagination
    lookup_field = 'pk'

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return AgentImportRequestDetailSerializer
        return AgentImportRequestSerializer

    def get_permissions(self):
        if self.action in ('approve', 'reject'):
            return [IsAdminUser()]
        return [IsAuthenticated()]

    def _base_queryset(self):
        user = self.request.user
        qs = AgentImportRequest.objects.select_related('submitted_by', 'reviewed_by')
        if not (user.is_staff or user.is_superuser):
            qs = qs.filter(submitted_by=user)
        return qs

    def get_queryset(self):
        qs = self._base_queryset()
        state = (self.request.query_params.get('status') or '').strip()
        if state:
            qs = qs.filter(status=state)
        return qs.order_by('-submitted_at')

    @staticmethod
    def _package_of(request):
        package = request.data.get('package') if isinstance(request.data, dict) else None
        if not isinstance(package, dict):
            raise ValidationError({'package': '请提供导入包对象（JSON）'})
        return package

    @staticmethod
    def _public_preview(preview: dict) -> dict:
        """预览对外口径：不返回内部解析出的模型对象，只给结论与摘要"""
        return {key: preview[key] for key in ('blocking', 'warnings', 'summary')}

    def create(self, request, *args, **kwargs):
        """提交导入申请：先做依赖映射校验，阻断项直接拒绝，不落库"""
        package = self._package_of(request)
        preview = services.prepare_import(package)
        public = self._public_preview(preview)
        if preview['blocking']:
            return Response({
                'detail': '导入包存在阻断项，请修正后重新提交',
                'code': 'import_blocked', **public,
            }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        counts = preview['summary']['counts']
        record = AgentImportRequest.objects.create(
            agent_name=preview['summary']['agent_name'],
            description=preview['summary']['description'],
            package=package,
            warnings=preview['warnings'],
            submitted_by=request.user,
        )
        log_audit(actor=request.user, action='agent_import_request',
                  target=f'智能体导入申请:{record.agent_name}',
                  detail=f'待审批；工具来源 {counts["mcp_servers"]} 个、技能 {counts["skills"]} 项、'
                         f'知识库 {counts["knowledge_bases"]} 个；依赖告警 '
                         f'{len(preview["warnings"])} 项；提交不生效，需另一名管理员审批')
        return Response({
            'detail': '导入申请已提交，等待另一名管理员审批',
            'preview': public,
            'request': AgentImportRequestSerializer(record).data,
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'])
    def preview(self, request):
        """提交前预览：返回依赖映射结果与阻断/告警，不写库"""
        return Response(self._public_preview(services.prepare_import(self._package_of(request))))

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """审批状态计数：非管理员只看自己的，管理员额外给出可审批数量"""
        qs = self._base_queryset()
        out = {value: qs.filter(status=value).count()
               for value in ImportRequestStatus.values}
        out['total'] = sum(out.values())
        out['actionable'] = (0 if not (request.user.is_staff or request.user.is_superuser)
                             else qs.filter(status=ImportRequestStatus.PENDING)
                             .exclude(submitted_by=request.user).count())
        return Response(out)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """审批通过：重新校验后落库为草稿；提交人不能审批自己的申请"""
        record = self.get_object()
        if record.status != ImportRequestStatus.PENDING:
            return Response({
                'detail': f'该申请已「{record.get_status_display()}」，不能重复审批',
                'code': 'import_not_pending',
            }, status=status.HTTP_409_CONFLICT)
        if record.submitted_by_id == request.user.pk:
            raise PermissionDenied('单级审批要求由另一人复核，不能审批自己提交的导入申请')

        preview = services.prepare_import(record.package)
        if preview['blocking']:
            return Response({
                'detail': '当前目标环境无法完成导入，存在阻断项',
                'code': 'import_blocked', **self._public_preview(preview),
            }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        agent = services.create_agent_from_import(
            preview['resolved'], submitter=record.submitted_by, reviewer=request.user)
        record.status = ImportRequestStatus.APPROVED
        record.reviewed_by = request.user
        record.reviewed_at = timezone.now()
        record.review_note = str(request.data.get('note') or '')[:200]
        record.result = {'agent_id': agent.pk, 'agent_name': agent.name,
                         'agent_slug': agent.slug, 'warnings': preview['warnings']}
        record.save(update_fields=['status', 'reviewed_by', 'reviewed_at',
                                   'review_note', 'result'])
        log_audit(actor=request.user, action='agent_import_approve',
                  target=f'智能体导入申请:{record.agent_name}',
                  detail=f'审批通过，创建草稿智能体「{agent.name}」（{agent.slug}）；'
                         f'依赖告警 {len(preview["warnings"])} 项；需重新发布才上线')
        return Response({
            'detail': f'已导入为草稿「{agent.name}」，需重新发布后才会对使用端生效',
            'agent': AgentDetailSerializer(agent).data,
            'request': AgentImportRequestSerializer(record).data,
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """驳回导入申请：不落库，理由留痕"""
        record = self.get_object()
        if record.status != ImportRequestStatus.PENDING:
            return Response({
                'detail': f'该申请已「{record.get_status_display()}」，不能重复审批',
                'code': 'import_not_pending',
            }, status=status.HTTP_409_CONFLICT)
        if record.submitted_by_id == request.user.pk:
            raise PermissionDenied('单级审批要求由另一人复核，不能审批自己提交的导入申请')

        note = str(request.data.get('note') or '').strip()[:200]
        record.status = ImportRequestStatus.REJECTED
        record.reviewed_by = request.user
        record.reviewed_at = timezone.now()
        record.review_note = note
        record.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_note'])
        log_audit(actor=request.user, action='agent_import_reject',
                  target=f'智能体导入申请:{record.agent_name}',
                  detail=f'已驳回；理由：{note or "未填写"}')
        return Response({'detail': '已驳回该导入申请',
                         'request': AgentImportRequestSerializer(record).data})
