"""技能规范接口（C1–C6）

权限口径（PRD 第 3 章 + 7.6）：上传、审核、启停、编辑、删除一律仅平台管理员；
读接口登录即可，但非管理员只看到对自己可见的技能（默认 admin_only）。

决策 10：结构性错误（路径穿越、超限、非文本、重名、覆盖 SKILL.md）直接 400，
不给确认入口；命中高危规则返回 422 + risk_fingerprint，带 risk_ack 与匹配的
指纹再次提交才落库。
"""
import logging
from urllib.parse import quote

from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response

from backend.apps.common.pagination import StandardPagination
from backend.apps.skills import services
from backend.apps.skills.models import MAX_BODY_CHARS, Skill, SkillBlob, SkillFile, \
    SkillSource, SkillStatus, SkillVersion, body_fingerprint
from backend.apps.skills.permissions import visible_skill_query
from backend.apps.skills.scanner import RULESET_VERSION
from backend.apps.skills.serializers import SkillReviewSerializer, SkillSerializer, \
    SkillVersionSerializer
from backend.apps.users.models import log_audit

logger = logging.getLogger(__name__)

WRITE_ACTIONS = {'create', 'update', 'partial_update', 'destroy', 'approve',
                 'reject', 'disable', 'enable', 'mirror',
                 'files', 'delete_file', 'rollback'}
MAX_UPLOAD_BYTES = 200 * 1024
SKIPPED_KEYS = ('body_ack', 'body_hash', 'reason', 'risk_ack', 'risk_fingerprint',
                'files', 'paths')
RISK_STATUS = status.HTTP_422_UNPROCESSABLE_ENTITY


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _risk_response(hits, fingerprint, code='risk_ack_required') -> Response:
    """命中高危但未确认（决策 10：422，给规则、位置与确认指纹）"""
    return Response({
        'detail': '检测到高危内容，确认后才能保存',
        'code': code,
        'ack_field': 'risk_ack',
        'fingerprint': fingerprint,
        'ruleset_version': RULESET_VERSION,
        'hits': hits,
    }, status=RISK_STATUS)


def _payload_of(request) -> dict:
    """多值表单（部门多选）与 JSON 都能吃；上传字段单独取"""
    data = request.data
    if hasattr(data, 'getlist'):
        payload = {}
        for key in data.keys():
            if key in ('files', 'paths', 'file'):
                continue
            values = data.getlist(key)
            payload[key] = values if len(values) > 1 else values[0]
        return payload
    return {k: v for k, v in data.items() if k not in ('files', 'paths', 'file')}


class SkillViewSet(viewsets.ModelViewSet):
    """技能：上传建档 / 校验 / 审核启用 / 启停 / 版本 / 附件 / 引用 / 装配选项"""

    serializer_class = SkillSerializer
    pagination_class = StandardPagination
    permission_classes = [IsAuthenticated]
    lookup_field = 'pk'

    def get_permissions(self):
        if self.action in WRITE_ACTIONS:
            return [IsAdminUser()]
        return [IsAuthenticated()]

    def get_queryset(self):
        user = self.request.user
        qs = Skill.objects.all()
        if not (user.is_staff or user.is_superuser):
            # 非管理员只看已启用/内置，以及自己上传的技能（需要看审核结论与驳回原因）；
            # 他人的待审/停用/校验未通过属于管理端过程状态，不暴露
            qs = qs.filter(visible_skill_query(user)).filter(
                Q(status__in=(SkillStatus.ACTIVE, SkillStatus.BUILTIN))
                | Q(created_by=user)).distinct()
        if self.action == 'list':
            keyword = (self.request.query_params.get('search') or '').strip()
            if keyword:
                qs = qs.filter(name__icontains=keyword) | qs.filter(
                    description__icontains=keyword)
            state = (self.request.query_params.get('status') or '').strip()
            if state:
                qs = qs.filter(status=state)
            source = (self.request.query_params.get('source') or '').strip()
            if source:
                qs = qs.filter(source=source)
        return qs.prefetch_related('agent_links', 'reviewed_by')

    def get_serializer_context(self):
        context = super().get_serializer_context()
        # 列表页不返回正文，审核队列点进详情才拖全文
        if self.action == 'list':
            context['compact'] = True
        return context

    # ─── C1 上传与解析 ───

    def create(self, request, *args, **kwargs):
        payload = _payload_of(request)
        uploaded = request.FILES.get('file')
        if uploaded is not None:
            parsed = parse_skill_file(uploaded)
            payload.setdefault('name', parsed['name'])
            payload.setdefault('description', parsed['description'])
            payload['body'] = parsed['body']

        uploads = self._collect_uploads(request)
        checked = services.validate_attachments(uploads)
        if checked['errors']:
            return _invalid_files(checked['errors'])

        serializer = self.get_serializer(data=payload)
        serializer.is_valid(raise_exception=True)

        body = serializer.validated_data.get('body', '') or ''
        body_hits = getattr(serializer, '_high_risk_hits', [])
        scanned = services.scan_package('', checked['files'])
        hits = body_hits + scanned['hits']
        fingerprint = services.package_fingerprint(
            body, [(f['rel_path'], f['sha256']) for f in checked['files']])
        ack_requested = _truthy(request.data.get('risk_ack'))
        ack_fingerprint = (request.data.get('risk_fingerprint') or '').strip()
        if hits:
            ok, reason = services.risk_ack_valid(
                None, fingerprint, ack_requested, ack_fingerprint)
            if not ok:
                return _risk_response(hits, fingerprint, reason)

        with transaction.atomic():
            skill = serializer.save()
            skill.file_scan_warnings = [i['reason'] + '（' + i['file'] + '）'
                                        for i in scanned['warnings']]
            for item in checked['files']:
                SkillFile.objects.create(
                    skill=skill, rel_path=item['rel_path'], entry=False,
                    size=item['size'], sha256=item['sha256'],
                    blob=SkillBlob.put(item['raw']),
                )
            if hits:
                for key, value in services.ack_fields_for(
                        hits, checked['files'], request.user, fingerprint).items():
                    setattr(skill, key, value)
                skill.save()
                services.log_risk_ack(skill, request.user, hits)
            else:
                skill.save(update_fields=['file_scan_warnings', 'updated_at'])
        self._sync(skill)
        log_audit(actor=request.user, action='skill_upload', target=f'技能:{skill.name}',
                  detail=f'正文 {skill.char_count} 字，附件 {checked["count"]} 个，'
                         f'风险提示 {len(skill.scan_warnings)} 条，'
                         f'状态={skill.get_status_display()}')
        response = self.get_serializer(skill).data
        response['validation_errors'] = skill.validation_errors
        return Response(response, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        """C4：编辑后回到待审核，原启用版本继续生效直至新版通过"""
        partial = kwargs.pop('partial', False)
        skill = self.get_object()
        self._reject_builtin_edits(skill, request.data)
        before_status = skill.status
        serializer = self.get_serializer(skill, data=_payload_of(request), partial=partial)
        serializer.is_valid(raise_exception=True)

        attachments = services.prospective_attachments(skill)
        body = serializer.validated_data.get('body', skill.body) or ''
        # 只对「本次真的改了正文」做扫描：仅改可见范围/名称/说明不重扫历史正文，
        # 否则存量的高危已确认技能会在每次编辑时被反复拦下。
        body_changed = 'body' in serializer.validated_data and body != (skill.body or '')
        hits, fingerprint, scanned = [], '', {'warnings': []}
        if body_changed:
            scanned = services.scan_package(body, attachments)
            hits, fingerprint = scanned['hits'], scanned['fingerprint']
        ack_requested = _truthy(request.data.get('risk_ack'))
        ack_fingerprint = (request.data.get('risk_fingerprint') or '').strip()
        already_acked = bool(
            hits and skill.risk_ack_fingerprint == fingerprint
            and skill.risk_ack_ruleset_version == RULESET_VERSION)
        newly_acked = False
        if hits and not already_acked:
            ok, reason = services.risk_ack_valid(
                skill, fingerprint, ack_requested, ack_fingerprint)
            if not ok:
                return _risk_response(hits, fingerprint, reason)
            newly_acked = True

        with transaction.atomic():
            skill = serializer.save()
            if newly_acked:
                for key, value in services.ack_fields_for(
                        hits, attachments, request.user, fingerprint).items():
                    setattr(skill, key, value)
                skill.save(update_fields=['risk_ack_at', 'risk_ack_by',
                                          'risk_ack_fingerprint', 'risk_ack_rules',
                                          'risk_ack_files', 'risk_ack_ruleset_version',
                                          'updated_at'])
                services.log_risk_ack(skill, request.user, hits)
        self._sync(skill)
        scope_note = ''
        if {'scope_mode', 'rollout_percent', 'applicable_department_ids',
                'applicable_user_ids', 'scope_include_sub_departments'} & set(request.data):
            dept_names = list(skill.applicable_departments.values_list('name', flat=True))
            scope_note = (f'；生效范围 {skill.get_scope_mode_display()}'
                          f'{"（" + "、".join(dept_names[:6]) + "）" if dept_names else ""}'
                          f'，灰度 {skill.rollout_percent}%'
                          f'，定向用户 {skill.applicable_users.count()} 人')
        log_audit(actor=request.user, action='skill_update', target=f'技能:{skill.name}',
                  detail=f'更新字段 {sorted(k for k in request.data if k not in SKIPPED_KEYS)}，'
                         f'状态 {before_status} → {skill.status}{scope_note}')
        return Response(self.get_serializer(skill).data)

    def destroy(self, request, *args, **kwargs):
        """C6：被挂载的技能不可直接删除；内置技能不可删除、可停用"""
        skill = self.get_object()
        if skill.source == SkillSource.BUILTIN:
            raise ValidationError({'detail': '内置技能不可删除，如需下线请使用停用'})
        refs = services.skill_references(skill)
        if refs:
            return Response({
                'detail': f'该技能被 {len(refs)} 个智能体挂载，请先从智能体摘除后再删除',
                'code': 'skill_referenced',
                'references': refs,
                'options': [f'先从 {len(refs)} 个智能体摘除', '只停用不删除（POST …/disable/）'],
            }, status=status.HTTP_409_CONFLICT)
        name = skill.name
        with transaction.atomic():
            skill.disable()
            services.remove_mirror(skill)
            skill.delete()
        log_audit(actor=request.user, action='skill_delete', target=f'技能:{name}',
                  detail=f'已删除，正文指纹 {skill.body_hash[:12]}，无挂载引用')
        return Response(status=status.HTTP_204_NO_CONTENT)

    # ─── C1 技能包附件 ───

    @action(detail=True, methods=['get', 'post'], url_path='files')
    def files(self, request, pk=None):
        """GET 附件清单；POST 批量上传（files[] + paths[]），高危走 422 确认"""
        skill = self.get_object()
        serializer = self.get_serializer(skill)
        if request.method == 'GET':
            return Response({
                'files': serializer.get_files(skill),
                'file_count': serializer.get_file_count(skill),
                'package_bytes': serializer.get_package_bytes(skill),
                'scan_warnings': skill.file_scan_warnings,
                'risk_ack': serializer.get_risk_ack(skill),
            })

        uploads = self._collect_uploads(request, allow_single=True)
        existing = [f.rel_path for f in skill.files.all()
                    if not f.entry and f.rel_path != skill.entry_file]
        checked = services.validate_attachments(
            uploads, existing_paths=existing,
            existing_bytes=serializer.get_package_bytes(skill),
            existing_count=len(existing))
        if checked['errors']:
            return _invalid_files(checked['errors'])

        attachments = services.prospective_attachments(skill, extra=checked['files'])
        scanned = services.scan_package(skill.body or '', attachments)
        hits, fingerprint = scanned['hits'], scanned['fingerprint']
        ack_requested = _truthy(request.data.get('risk_ack'))
        ack_fingerprint = (request.data.get('risk_fingerprint') or '').strip()
        if hits:
            ok, reason = services.risk_ack_valid(
                skill, fingerprint, ack_requested, ack_fingerprint)
            if not ok:
                return _risk_response(hits, fingerprint, reason)

        with transaction.atomic():
            for item in checked['files']:
                SkillFile.objects.create(
                    skill=skill, rel_path=item['rel_path'], entry=False,
                    size=item['size'], sha256=item['sha256'],
                    blob=SkillBlob.put(item['raw']),
                )
            skill.file_scan_warnings = [f'{i["reason"]}（{i["file"]}）'
                                        for i in scanned['warnings']]
            skill.clear_risk_ack()
            if hits:
                for key, value in services.ack_fields_for(
                        hits, attachments, request.user, fingerprint).items():
                    setattr(skill, key, value)
                services.log_risk_ack(skill, request.user, hits)
            skill.save()
            if skill.status not in (SkillStatus.DISABLED, SkillStatus.INVALID):
                skill.submit()
        log_audit(actor=request.user, action='skill_update', target=f'技能:{skill.name}',
                  detail=f'上传附件 {checked["count"]} 个：'
                         f'{",".join(f["rel_path"] for f in checked["files"])}')
        return Response(self.get_serializer(skill).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['delete'],
            url_path=r'files/(?P<file_id>\d+)')
    def delete_file(self, request, pk=None, file_id=None):
        skill = self.get_object()
        item = get_object_or_404(SkillFile, pk=file_id, skill=skill)
        if item.entry or item.rel_path == skill.entry_file:
            raise ValidationError({'detail': 'SKILL.md 是技能主文件，请通过正文编辑更新'})
        rel_path = item.rel_path
        item.delete()
        skill.clear_risk_ack()
        skill.save(update_fields=['risk_ack_at', 'risk_ack_by', 'risk_ack_fingerprint',
                                  'risk_ack_rules', 'risk_ack_files',
                                  'risk_ack_ruleset_version', 'updated_at'])
        if skill.status not in (SkillStatus.DISABLED, SkillStatus.INVALID):
            skill.submit()
        log_audit(actor=request.user, action='skill_update', target=f'技能:{skill.name}',
                  detail=f'删除附件 {rel_path}')
        return Response(self.get_serializer(skill).data)

    @action(detail=True, methods=['get'],
            url_path=r'files/(?P<file_id>\d+)/download')
    def download_file(self, request, pk=None, file_id=None):
        skill = self.get_object()
        item = get_object_or_404(SkillFile.objects.select_related('blob'),
                                 pk=file_id, skill=skill)
        if item.blob is None:
            raise ValidationError({'detail': '文件内容缺失'})
        return _text_response(bytes(item.blob.content), item.rel_path)

    # ─── C4 版本历史 / 差异 / 回滚 ───

    @action(detail=True, methods=['get'], url_path='versions')
    def versions(self, request, pk=None):
        skill = self.get_object()
        rows = skill.versions.select_related('approved_by').prefetch_related('files')
        serializer = SkillVersionSerializer(
            rows, many=True, context={**self.get_serializer_context(), 'compact': True})
        return Response({
            'current_version': skill.current_version,
            'versions': serializer.data,
        })

    @action(detail=True, methods=['get'], url_path=r'versions/(?P<version>\d+)')
    def version_detail(self, request, pk=None, version=None):
        skill = self.get_object()
        target = get_object_or_404(
            SkillVersion.objects.select_related('approved_by').prefetch_related(
                'files__blob'), skill=skill, version=version)
        return Response(SkillVersionSerializer(target).data)

    @action(detail=True, methods=['get'], url_path='versions/compare')
    def version_compare(self, request, pk=None):
        skill = self.get_object()
        left_no = request.query_params.get('left') or ''
        right_no = request.query_params.get('right') or ''
        if not left_no or not right_no:
            raise ValidationError({'detail': '请提供 left 与 right 两个版本号'})
        left = get_object_or_404(SkillVersion, skill=skill, version=left_no)
        right = get_object_or_404(SkillVersion, skill=skill, version=right_no)
        return Response(services.compare_versions(left, right))

    @action(detail=True, methods=['post'], url_path=r'versions/(?P<version>\d+)/rollback')
    def rollback(self, request, pk=None, version=None):
        skill = self.get_object()
        target = get_object_or_404(SkillVersion, skill=skill, version=version)
        if skill.status == SkillStatus.DISABLED:
            raise ValidationError({'detail': '技能已停用，请先恢复启用再回滚'})
        try:
            result = services.rollback_to(
                skill, target, request.user,
                ack_requested=_truthy(request.data.get('risk_ack')),
                ack_fingerprint=(request.data.get('risk_fingerprint') or '').strip())
        except services.RiskConfirmRequired as exc:
            return _risk_response(exc.hits, exc.fingerprint)
        return Response({
            'detail': f'已回滚到 v{target.version}，生成新版本 v{result["new_version"]}',
            **result,
            'skill': self.get_serializer(skill).data,
        })

    @action(detail=True, methods=['get'],
            url_path=r'versions/(?P<version>\d+)/files/(?P<file_id>\d+)/download')
    def version_file_download(self, request, pk=None, version=None, file_id=None):
        skill = self.get_object()
        target = get_object_or_404(SkillVersion, skill=skill, version=version)
        item = get_object_or_404(target.files.select_related('blob'), pk=file_id)
        return _text_response(bytes(item.blob.content), item.rel_path)

    # ─── C3 审核启用 ───

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        skill = self.get_object()
        gate = SkillReviewSerializer(data=request.data)
        gate.is_valid(raise_exception=True)
        current_hash = body_fingerprint(skill.body)
        if gate.validated_data['body_hash'] != current_hash:
            raise ValidationError({'detail': '正文在审核期间已被改动，请重新查看正文后再确认'})

        attachments = services.prospective_attachments(skill)
        scanned = services.scan_package(skill.body or '', attachments)
        hits, fingerprint = scanned['hits'], scanned['fingerprint']
        if hits:
            stored_ok = bool(
                skill.risk_ack_fingerprint == fingerprint
                and skill.risk_ack_ruleset_version == RULESET_VERSION)
            if not stored_ok:
                ok, reason = services.risk_ack_valid(
                    skill, fingerprint,
                    _truthy(request.data.get('risk_ack')),
                    (request.data.get('risk_fingerprint') or '').strip())
                if not ok:
                    return _risk_response(hits, fingerprint, reason)
                for key, value in services.ack_fields_for(
                        hits, attachments, request.user, fingerprint).items():
                    setattr(skill, key, value)
                skill.save(update_fields=['risk_ack_at', 'risk_ack_by',
                                          'risk_ack_fingerprint', 'risk_ack_rules',
                                          'risk_ack_files', 'risk_ack_ruleset_version',
                                          'updated_at'])
                services.log_risk_ack(skill, request.user, hits)

        reviewer_name = request.user.username
        skill.approve(request.user)
        self._sync(skill)
        log_audit(actor=request.user, action='skill_approve', target=f'技能:{skill.name}',
                  detail=f'审核人 {reviewer_name}，正文指纹 {skill.body_hash[:12]}，'
                         f'版本 v{skill.current_version}，已启用并对新会话生效')
        return Response(self.get_serializer(skill).data)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        skill = self.get_object()
        reason = (request.data.get('reason') or '').strip()
        if not reason:
            raise ValidationError({'reason': '驳回必须填写理由，创建者需要看到原因'})
        gate = SkillReviewSerializer(data={**request.data, 'reason': reason})
        gate.is_valid(raise_exception=True)
        if gate.validated_data['body_hash'] != body_fingerprint(skill.body):
            raise ValidationError({'detail': '正文在审核期间已被改动，请重新查看正文后再驳回'})
        before = skill.status
        skill.reject(request.user, reason)
        self._sync(skill)
        log_audit(actor=request.user, action='skill_reject', target=f'技能:{skill.name}',
                  detail=f'驳回理由：{reason}；状态 {before} → {skill.status}')
        return Response(self.get_serializer(skill).data)

    # ─── C4 停用 / 恢复启用 ───

    @action(detail=True, methods=['post'])
    def disable(self, request, pk=None):
        """停用即时对新会话生效：镜像目录移除，挂载关系保留（视为未挂载）"""
        skill = self.get_object()
        skill.disable()
        services.remove_mirror(skill)
        log_audit(actor=request.user, action='skill_disable', target=f'技能:{skill.name}',
                  detail=f'已停用，即时对新会话生效；仍被 '
                         f'{services.reference_count(skill)} 个智能体挂载（视为未挂载）')
        return Response(self.get_serializer(skill).data)

    @action(detail=True, methods=['post'])
    def enable(self, request, pk=None):
        skill = self.get_object()
        if not skill.approved_body:
            raise ValidationError({'detail': '尚无审核通过的生效版本，请编辑正文后提交审核'})
        skill.enable()
        self._sync(skill)
        log_audit(actor=request.user, action='skill_disable', target=f'技能:{skill.name}',
                  detail=f'恢复启用，正文指纹 {skill.body_hash[:12]}')
        return Response(self.get_serializer(skill).data)

    # ─── C6 引用 / A4 装配选项 / C3 队列 ───

    @action(detail=True, methods=['get'], url_path='references')
    def references(self, request, pk=None):
        refs = services.skill_references(self.get_object())
        return Response({'count': len(refs), 'references': refs})

    @action(detail=False, methods=['get'], url_path='options')
    def select_options(self, request):
        """装配页选择器：已启用可选，未启用灰置并给出原因（A4 + C5 生效范围）"""
        data = [{
            'id': s.pk, 'name': s.name, 'slug': s.slug, 'description': s.description,
            'status': s.status, 'status_display': s.get_status_display(),
            'char_count': s.char_count, 'source': s.source,
            'mountable': s.mountable,
            'reason': '' if s.mountable else '技能需管理员审核启用后才能挂载',
            'live': s.live,
            'scope_mode': s.scope_mode,
            'scope_mode_display': s.get_scope_mode_display(),
            'scope_departments': [d.name for d in s.applicable_departments.all()],
            'rollout_percent': s.rollout_percent,
            'scope_users': [u.username for u in s.applicable_users.all()],
            'applies_to_me': s.applies_to(request.user),
            'warning': '存在待审核的编辑稿，当前生效的仍是上一次通过审核的正文'
                       if (s.live and s.body_changed) else '',
        } for s in self.get_queryset().prefetch_related(
            'applicable_departments', 'applicable_users')]
        return Response(data)

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """各状态计数与待审核队列长度（管理端技能页顶部）"""
        stats = services.status_summary()
        return Response({
            'counts': stats,
            'pending': stats.get(SkillStatus.PENDING, 0),
            'body_limit': MAX_BODY_CHARS,
        })

    @action(detail=False, methods=['get', 'post'])
    def mirror(self, request):
        """DB → 磁盘镜像的运维可见性与全量重建（AgentScope 加载器只认目录）"""
        root = services.mirror_root()
        if request.method == 'GET':
            return Response({
                'root': str(root),
                'mirrored': [{
                    'slug': s.slug, 'name': s.name, 'status': s.status,
                    'path': str(s.mirror_dir / (s.entry_file or 'SKILL.md')),
                    'runtime_chars': len(s.runtime_body),
                } for s in Skill.objects.all() if s.mountable and s.runtime_body],
            })
        result = services.rebuild_all_mirror()
        log_audit(actor=request.user, action='skill_update', target='技能镜像',
                  detail=f'全量重建镜像：生效 {result["mirrored"]}，移除 {result["removed"]}')
        return Response(result)

    # ─── 内部 ───

    def _sync(self, skill: Skill):
        """状态机变更后同步磁盘镜像（上传≠生效由 sync_mirror 内部判定）"""
        services.sync_mirror(skill)

    @staticmethod
    def _collect_uploads(request, allow_single=False):
        """收集 files[] 与 paths[]；缺路径时回落上传文件名

        create 里的 file= 是规范主文件（SKILL.md），不能当成附件重复收集。
        """
        files = request.FILES.getlist('files')
        if not files and allow_single:
            single = request.FILES.get('file')
            files = [single] if single is not None else []
        paths = []
        data = request.data
        if hasattr(data, 'getlist'):
            paths = data.getlist('paths') or data.getlist('paths[]')
        if isinstance(paths, str):
            paths = [paths]
        uploads = []
        for index, item in enumerate(files):
            rel_path = paths[index].strip() if index < len(paths) and paths[index] else ''
            uploads.append((rel_path or item.name, item.read()))
        return uploads

    @staticmethod
    def _reject_builtin_edits(skill: Skill, data):
        if skill.source != SkillSource.BUILTIN:
            return
        locked = {'name', 'slug', 'source', 'body', 'entry_file', 'requires'} & set(data)
        if locked:
            raise ValidationError(
                {'detail': f'内置技能不可编辑（4.4/C6）：{sorted(locked)}'})


def _invalid_files(errors) -> Response:
    """结构性错误（决策 10：400，不给确认入口）"""
    return Response({
        'detail': '文件校验未通过，请按提示修正后重试',
        'code': 'invalid_files',
        'files': errors,
    }, status=status.HTTP_400_BAD_REQUEST)


def _text_response(raw: bytes, rel_path: str) -> HttpResponse:
    response = HttpResponse(raw, content_type='text/plain; charset=utf-8')
    response['Content-Disposition'] = \
        f"attachment; filename*=UTF-8''{quote(rel_path, safe='')}"
    return response


def parse_skill_file(uploaded) -> dict:
    """上传的规范文件 → 名称/适用说明/正文。

    优先取 YAML frontmatter 的 name / description，缺失时回落文件名与一级标题，
    正文始终是去掉 frontmatter 后的全文（C1：上传与解析）。
    """
    raw = uploaded.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValidationError({'detail': f'规范文件过大（{len(raw)} 字节，'
                                        f'上限 {MAX_UPLOAD_BYTES} 字节）'})
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValidationError({'detail': '规范文件须为 UTF-8 文本（支持 SKILL.md / .txt / .md）'})

    meta = {}
    lines = text.splitlines()
    body_start = 0
    if lines and lines[0].strip() == '---':
        for idx in range(1, len(lines)):
            if lines[idx].strip() == '---':
                body_start = idx + 1
                for line in lines[1:idx]:
                    if ':' in line:
                        key, _, val = line.partition(':')
                        meta[key.strip().lower()] = val.strip().strip('"\'')
                break
    body = '\n'.join(lines[body_start:]).strip()

    stem = uploaded.name.rsplit('.', 1)[0].strip() or 'skill'
    name = meta.get('name') or _first_heading(body) or stem
    description = meta.get('description') or _first_paragraph(body)
    return {'name': name[:128], 'description': description[:200], 'body': body}


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            return stripped.lstrip('#').strip()
    return ''


def _first_paragraph(text: str) -> str:
    for block in text.split('\n\n'):
        stripped = block.strip()
        if stripped and not stripped.startswith('#'):
            return stripped.replace('\n', ' ')
    return ''
