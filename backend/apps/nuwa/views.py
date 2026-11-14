"""女娲模式接口

会话自身（/api/nuwa/sessions/）归创建人；市场源、开关、审批归管理员；
目录条目对登录用户只读。所有步骤失败都返回可读原因，不抛 500。
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from backend.apps.agenthub.models import (AgentProfile, AgentSkill, AgentStatus,
                                          PermissionTier, RetrievalTier, SpeechTier,
                                          AgentMCPServer)
from backend.apps.common.choices import Visibility
from backend.apps.mcp.models import MCPServer
from backend.apps.nuwa import config, generation, imports, knowledge_fetch, market
from backend.apps.nuwa.models import (MarketItem, MarketSource, NuwaImportRequest,
                                      NuwaSession)
from backend.apps.nuwa.serializers import (MarketItemSerializer, MarketSourceSerializer,
                                           NuwaImportRequestSerializer,
                                           NuwaSessionSerializer)
from backend.apps.skills.models import Skill
from backend.apps.users.models import Department, log_audit

logger = logging.getLogger(__name__)

SESSION_TTL_DAYS = 7


class NuwaSessionViewSet(viewsets.ModelViewSet):
    serializer_class = NuwaSessionSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_queryset(self):
        user = self.request.user
        if user.is_staff:
            return NuwaSession.objects.all()
        return NuwaSession.objects.filter(owner=user)

    # ─── ① 描述 → 骨架 ───

    def create(self, request, *args, **kwargs):
        if not config.nuwa_enabled():
            return Response({'detail': '女娲模式已停用，请联系平台管理员。'},
                            status=status.HTTP_400_BAD_REQUEST)
        brief = str(request.data.get('brief') or '').strip()
        if len(brief) < 10:
            return Response({'detail': '请至少用 10 个字描述想要的智能体。'},
                            status=status.HTTP_400_BAD_REQUEST)
        if len(brief) > generation.BRIEF_MAX:
            return Response({'detail': f'描述过长，上限 {generation.BRIEF_MAX} 字。'},
                            status=status.HTTP_400_BAD_REQUEST)
        allowed, used, limit = generation.allowed_daily_sessions(request.user)
        if not allowed:
            return Response({'detail': f'今日女娲会话已达上限（{limit} 次），明天再试。'},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)

        audience = str(request.data.get('audience') or '').strip()[:120]
        department = str(request.data.get('department') or '').strip()[:100]
        session = NuwaSession.objects.create(
            owner=request.user, brief=brief, audience=audience, department=department,
            brief_hash=generation.brief_hash(brief),
            expires_at=timezone.now() + timedelta(days=SESSION_TTL_DAYS),
        )
        self._generate(session)
        log_audit(actor=request.user, action='nuwa_session_created',
                  target=f'女娲会话#{session.pk}', detail=brief[:80])
        return Response(self.get_serializer(session).data,
                        status=status.HTTP_201_CREATED)

    def _models(self) -> list:
        from backend.apps.agenthub import services as agent_services

        return agent_services.verified_models()

    def _generate(self, session: NuwaSession, *, force: bool = False) -> None:
        departments = list(Department.objects.values_list('name', flat=True)[:30])
        result = generation.generate(
            session.brief, audience=session.audience, models=self._models(),
            departments=departments, force=force,
            previous=(session.generated or None) if not force else None,
        )
        session.generated = result['skeleton']
        session.generation_meta = result['meta']
        session.error = result['error']
        session.step = max(session.step, 2)
        session.status = 'drafting'
        session.save(update_fields=['generated', 'generation_meta', 'error', 'step',
                                    'status', 'updated_at'])

    def partial_update(self, request, *args, **kwargs):
        session = self.get_object()
        if session.expired:
            return Response({'detail': '会话已过期，请另存为新会话。'},
                            status=status.HTTP_400_BAD_REQUEST)
        editable = {'brief', 'audience', 'department', 'step', 'status'}
        for field in editable:
            if field in request.data:
                setattr(session, field, request.data[field])
        if 'generated' in request.data and isinstance(request.data['generated'], dict):
            merged = {**(session.generated or {}), **request.data['generated']}
            session.generated = merged
            edited = set(session.edited_fields or []) | set(request.data['generated'].keys())
            session.edited_fields = sorted(edited)
        session.save()
        return Response(self.get_serializer(session).data)

    @action(detail=True, methods=['post'])
    def regenerate(self, request, pk=None):
        session = self.get_object()
        if session.expired:
            return Response({'detail': '会话已过期。'}, status=status.HTTP_400_BAD_REQUEST)
        if not config.nuwa_enabled():
            return Response({'detail': '女娲模式已停用。'}, status=status.HTTP_400_BAD_REQUEST)
        self._generate(session, force=True)
        return Response(self.get_serializer(session).data)

    # ─── ③ 市场检索与勾选 ───

    @action(detail=True, methods=['post'])
    def matches(self, request, pk=None):
        session = self.get_object()
        kinds = request.data.get('kinds') or ['mcp', 'skill']
        kinds = [kind for kind in kinds if kind in ('mcp', 'skill')] or ['mcp', 'skill']
        query = str(request.data.get('query') or '').strip()
        if not query:
            skeleton = session.generated or {}
            query = ' '.join(filter(None, [skeleton.get('name', ''),
                                           skeleton.get('description', ''),
                                           session.brief[:200]]))[:200]
        payload = market.search(query, kinds=kinds, limit=int(request.data.get('limit') or 12))
        session.candidates = payload
        session.step = max(session.step, 3)
        session.status = 'matching'
        session.save(update_fields=['candidates', 'step', 'status', 'updated_at'])
        log_audit(actor=request.user, action='nuwa_market_fetch',
                  target=f'女娲会话#{session.pk}',
                  detail=f'mode={payload["mode"]} hits={len(payload["items"])} '
                         f'degraded={payload["degraded_reason"]}')
        return Response(payload)

    @action(detail=True, methods=['post'])
    def selections(self, request, pk=None):
        session = self.get_object()
        mcp_ids = [int(item) for item in (request.data.get('mcp') or []) if str(item).isdigit()]
        skill_ids = [int(item) for item in (request.data.get('skills') or [])
                     if str(item).isdigit()]
        items = list(MarketItem.objects.filter(pk__in=set(mcp_ids + skill_ids)))
        submitted, existing_local = [], []
        for item in items:
            name = item.title
            local = (MCPServer.objects.filter(name=name).first() if item.kind == 'mcp'
                     else Skill.objects.filter(name=name).first())
            if local:
                existing_local.append({'kind': item.kind, 'id': local.pk, 'title': name})
                continue
            request_obj = imports.submit(item, request.user)
            submitted.append({'id': request_obj.pk, 'kind': request_obj.kind,
                              'title': name, 'risk_flags': request_obj.risk_flags})
        session.selections = {'mcp': mcp_ids, 'skills': skill_ids,
                              'submitted': submitted, 'local': existing_local}
        session.save(update_fields=['selections', 'updated_at'])
        if submitted:
            log_audit(actor=request.user, action='nuwa_import_submitted',
                      target=f'女娲会话#{session.pk}', detail=f'{len(submitted)} 条待审批')
        return Response(session.selections)

    # ─── ④ 资料 ───

    @action(detail=True, methods=['post'], url_path='knowledge/plan')
    def knowledge_plan(self, request, pk=None):
        session = self.get_object()
        queries = request.data.get('queries')
        if not isinstance(queries, list) or not queries:
            queries = generation.knowledge_queries(session.generated or {}, session.brief)
        plan = {'queries': [str(q)[:60] for q in queries][:6],
                'urls': [str(u).strip() for u in (request.data.get('urls') or []) if str(u).strip()][:knowledge_fetch.MAX_ITEMS],
                'discover': bool(request.data.get('discover'))}
        session.knowledge_plan = plan
        session.save(update_fields=['knowledge_plan', 'updated_at'])
        return Response(plan)

    @action(detail=True, methods=['post'], url_path='knowledge/preview')
    def knowledge_preview(self, request, pk=None):
        session = self.get_object()
        plan = session.knowledge_plan or {}
        urls = request.data.get('urls') or plan.get('urls') or []
        discover = bool(request.data.get('discover', plan.get('discover', False)))
        result = knowledge_fetch.preview(session, urls, discover=discover)
        session.knowledge_items = result.get('items') or []
        session.knowledge_plan = {**plan, 'urls': urls, 'discover': discover,
                                  'last_mode': result.get('mode'),
                                  'last_degraded': result.get('degraded_reason', '')}
        if result.get('items'):
            session.step = max(session.step, 4)
            session.status = 'knowledge'
        session.save(update_fields=['knowledge_items', 'knowledge_plan', 'step',
                                    'status', 'updated_at'])
        return Response(result)

    @action(detail=True, methods=['post'], url_path='knowledge/commit')
    def knowledge_commit(self, request, pk=None):
        session = self.get_object()
        wanted = request.data.get('urls')
        items = session.knowledge_items or []
        if wanted:
            selected = set(str(url) for url in wanted)
            items = [item for item in items if item.get('url') in selected]
        items = [item for item in items if item.get('status') == 'ok' and item.get('file')]
        if not items:
            return Response({'detail': '没有可入库的条目，请先抓取并勾选。'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            result = knowledge_fetch.commit(
                session, items,
                kb_id=request.data.get('kb_id'),
                kb_name=str(request.data.get('kb_name') or ''),
                scope=str(request.data.get('scope') or 'public'),
                user=request.user,
            )
        except PermissionError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        session.knowledge_plan = {**(session.knowledge_plan or {}),
                                  'kb_id': result['knowledge_base_id'],
                                  'committed': len(result['created'])}
        session.save(update_fields=['knowledge_plan', 'updated_at'])
        log_audit(actor=request.user, action='nuwa_knowledge_imported',
                  target=f'知识库#{result["knowledge_base_id"]}',
                  detail=f'女娲会话#{session.pk} 入库 {len(result["created"])} 条，'
                         f'跳过 {len(result["skipped"])} 条')
        return Response(result)

    # ─── ⑤ 建草稿 ───

    @action(detail=True, methods=['post'], url_path='create-agent')
    def create_agent(self, request, pk=None):
        session = self.get_object()
        if session.agent_id:
            return Response({'agent_id': session.agent_id, 'created': False})
        skeleton = session.generated or {}
        if not (skeleton.get('prompt') or '').strip():
            return Response({'detail': '提示词为空，请先在第二步生成或填写提示词。'},
                            status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            agent = AgentProfile.objects.create(
                name=self._unique_agent_name(skeleton.get('name') or session.brief[:12]),
                description=(skeleton.get('description') or '')[:200],
                job_tags=list(skeleton.get('job_tags') or [])[:3],
                icon=(skeleton.get('icon') or 'Bot')[:64],
                model_name=(skeleton.get('model_name') or '')[:128],
                speech_tier=skeleton.get('speech_tier')
                if skeleton.get('speech_tier') in dict(SpeechTier.choices) else SpeechTier.MID,
                retrieval_tier=skeleton.get('retrieval_tier')
                if skeleton.get('retrieval_tier') in dict(RetrievalTier.choices)
                else RetrievalTier.STANDARD,
                prompt=skeleton.get('prompt') or '',
                welcome_message=(skeleton.get('welcome_message') or '')[:200],
                permission_tier=PermissionTier.WHITELIST,
                visibility=self._visibility_of(session),
                knowledge_base_ids=[session.knowledge_plan.get('kb_id')]
                if session.knowledge_plan.get('kb_id') else [],
                status=AgentStatus.DRAFT,
                created_by=request.user,
                updated_by=request.user,
                owner_department=(Department.objects.filter(
                    name=(request.user.department or '')).first()),
            )
            if agent.visibility == Visibility.DEPARTMENT and session.department:
                department = Department.objects.filter(name=session.department).first()
                if department:
                    agent.allowed_departments.add(department)
            mounted, pending = self._mount(session, agent)
        session.agent = agent
        session.step = 5
        session.status = 'created'
        session.save(update_fields=['agent', 'step', 'status', 'updated_at'])
        log_audit(actor=request.user, action='nuwa_agent_drafted',
                  target=f'智能体#{agent.pk}', detail=f'女娲会话#{session.pk} 生成草稿')
        return Response({
            'agent_id': agent.pk,
            'created': True,
            'mounted': mounted,
            'pending_imports': pending,
            'warnings': self._warnings(session, mounted, pending),
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def abandon(self, request, pk=None):
        session = self.get_object()
        session.status = 'abandoned'
        session.save(update_fields=['status', 'updated_at'])
        return Response({'id': session.pk, 'status': session.status})

    # ─── 内部工具 ───

    @staticmethod
    def _unique_agent_name(base: str) -> str:
        name = (base or '女娲智能体')[:30]
        index = 2
        while AgentProfile.objects.filter(name=name).exists():
            suffix = f'-{index}'
            name = f'{(base or "女娲智能体")[:30 - len(suffix)]}{suffix}'
            index += 1
        return name

    @staticmethod
    def _visibility_of(session: NuwaSession) -> str:
        suggested = (session.generated or {}).get('visibility') or 'private'
        if suggested == 'public':
            return Visibility.PUBLIC
        if suggested == 'department' and session.department:
            return Visibility.DEPARTMENT
        return Visibility.PRIVATE

    def _mount(self, session: NuwaSession, agent: AgentProfile) -> tuple:
        """把已在本地存在的来源/技能挂上；其余留给审批结果，不静默丢弃"""
        selections = session.selections or {}
        mounted = {'mcp': [], 'skills': []}
        pending = []
        for item_id in selections.get('mcp') or []:
            item = MarketItem.objects.filter(pk=item_id).first()
            if not item:
                continue
            server = MCPServer.objects.filter(name=item.title).first()
            if server is None:
                continue
            agent.mcp_servers.add(server, through_defaults={
                'tool_names': list(server.enabled_tools or [])})
            mounted['mcp'].append({'id': server.pk, 'name': server.name})
        for item_id in selections.get('skills') or []:
            item = MarketItem.objects.filter(pk=item_id).first()
            if not item:
                continue
            skill = Skill.objects.filter(name=item.title).first()
            if skill is None or not skill.mountable:
                continue
            AgentSkill.objects.get_or_create(agent=agent, skill=skill)
            mounted['skills'].append({'id': skill.pk, 'name': skill.name})
        for request_obj in NuwaImportRequest.objects.filter(
                market_item_id__in=(selections.get('mcp') or []) + (selections.get('skills') or []),
                requested_by_id=session.owner_id, status='pending'):
            pending.append({'id': request_obj.pk, 'kind': request_obj.kind,
                            'title': getattr(request_obj.market_item, 'title', ''),
                            'risk_flags': request_obj.risk_flags})
        return mounted, pending

    @staticmethod
    def _warnings(session, mounted, pending) -> list:
        warnings = []
        if pending:
            warnings.append(f'{len(pending)} 个外部条目待管理员审批，审批通过后需回到装配页手动挂载。')
        if not (session.generated or {}).get('welcome_message'):
            warnings.append('未生成欢迎语，对话页将使用平台默认开场白。')
        return warnings


class MarketSourceViewSet(viewsets.ModelViewSet):
    queryset = MarketSource.objects.all()
    serializer_class = MarketSourceSerializer
    permission_classes = [IsAdminUser]
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def perform_create(self, serializer):
        serializer.save(is_builtin=False)

    @action(detail=True, methods=['post'])
    def sync(self, request, pk=None):
        source = self.get_object()
        if not config.market_online() and source.kind != 'local_catalog':
            return Response({'detail': '女娲在线检索已关闭，无法同步外部源。'},
                            status=status.HTTP_400_BAD_REQUEST)
        run = market.sync_source(source, query=str(request.data.get('query') or ''),
                                 limit=int(request.data.get('limit') or 50))
        log_audit(actor=request.user, action='nuwa_market_sync',
                  target=source.name, detail=f'{run.status} +{run.added}/{run.updated}')
        return Response({'id': run.pk, 'status': run.status, 'added': run.added,
                         'updated': run.updated, 'failed': run.failed,
                         'error': run.error})


class MarketItemViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = MarketItemSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = MarketItem.objects.select_related('source').exclude(status='blocked')
        kind = self.request.query_params.get('kind')
        if kind in ('mcp', 'skill'):
            qs = qs.filter(kind=kind)
        source = self.request.query_params.get('source')
        if str(source or '').isdigit():
            qs = qs.filter(source_id=int(source))
        trust = self.request.query_params.get('trust')
        if trust in ('trusted', 'unverified'):
            qs = qs.filter(trust_level=trust)
        keyword = (self.request.query_params.get('search') or '').strip()
        if keyword:
            qs = qs.filter(Q(title__icontains=keyword) | Q(summary__icontains=keyword))
        return qs

    @action(detail=False, methods=['get'])
    def facets(self, request):
        """来源与数量：市场页筛选器用，不暴露同步凭据等管理员字段"""
        rows = (MarketSource.objects.filter(items__isnull=False).distinct()
                .values('id', 'name', 'kind'))
        counts = dict(MarketItem.objects.exclude(status='blocked')
                      .values_list('source_id').annotate(total=Count('id')))
        return Response([{**row, 'count': counts.get(row['id'], 0)} for row in rows])

    @action(detail=True, methods=['post'], permission_classes=[IsAdminUser])
    def block(self, request, pk=None):
        item = self.get_object()
        item.status = 'blocked'
        item.save(update_fields=['status', 'updated_at'])
        log_audit(actor=request.user, action='market_item_blocked',
                  target=item.title, detail=item.external_id)
        return Response(self.get_serializer(item).data)


class MarketSearchView(APIView):
    """独立市场页与女娲第三步共用同一套检索（market.search）"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return self._respond(request, request.query_params)

    def post(self, request):
        return self._respond(request, request.data)

    @staticmethod
    def _respond(request, payload) -> Response:
        kinds = payload.get('kinds')
        if isinstance(kinds, str):
            kinds = [part.strip() for part in kinds.split(',') if part.strip()]
        if not isinstance(kinds, list):
            kinds = [payload.get('kind')] if payload.get('kind') else ['mcp', 'skill']
        kinds = [kind for kind in kinds if kind in ('mcp', 'skill')] or ['mcp', 'skill']
        query = str(payload.get('query') or '').strip()[:200]
        limit = payload.get('limit')
        source_id = payload.get('source')
        result = market.search(
            query, kinds=kinds,
            limit=max(1, min(int(limit or 20), 50)),
            source_id=int(source_id) if str(source_id or '').isdigit() else None,
        )
        return Response(result)


class NuwaImportRequestViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = NuwaImportRequestSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'post', 'head', 'options']

    def get_queryset(self):
        user = self.request.user
        qs = NuwaImportRequest.objects.select_related('market_item', 'requested_by')
        if user.is_staff:
            return qs
        return qs.filter(requested_by=user)

    def create(self, request, *args, **kwargs):
        """就地提交导入申请：只落待审草稿，不建来源/技能（非管理员同样只能提交）"""
        raw_id = request.data.get('market_item')
        if not str(raw_id or '').isdigit():
            return Response({'detail': '请指定 market_item。'},
                            status=status.HTTP_400_BAD_REQUEST)
        item = MarketItem.objects.filter(pk=int(raw_id)).first()
        if item is None:
            return Response({'detail': '条目不存在，可能已被清理，请重新检索。'},
                            status=status.HTTP_404_NOT_FOUND)
        if item.status == 'blocked':
            return Response({'detail': '该条目已被管理员拦截，不能提交导入。'},
                            status=status.HTTP_400_BAD_REQUEST)
        approved = (NuwaImportRequest.objects.filter(market_item=item, status='approved')
                    .order_by('-reviewed_at').first())
        if approved and approved.result:
            return Response({'detail': '该条目已通过审批并落库，无需重复提交。'},
                            status=status.HTTP_400_BAD_REQUEST)
        existing = NuwaImportRequest.objects.filter(
            market_item=item, requested_by=request.user, status='pending').first()
        target = imports.submit(item, request.user)
        log_audit(actor=request.user, action='nuwa_import_submitted',
                  target=item.title, detail=f'市场页就地提交：{target.get_kind_display()}')
        return Response(
            {**self.get_serializer(target).data, 'created': existing is None},
            status=status.HTTP_201_CREATED if existing is None else status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'], permission_classes=[IsAdminUser])
    def approve(self, request, pk=None):
        target = self.get_object()
        try:
            result = imports.approve(target, request.user,
                                     note=str(request.data.get('note') or ''))
        except PermissionError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'result': result,
                         'request': self.get_serializer(target).data})

    @action(detail=True, methods=['post'], permission_classes=[IsAdminUser])
    def reject(self, request, pk=None):
        target = self.get_object()
        try:
            imports.reject(target, request.user,
                           note=str(request.data.get('note') or ''))
        except PermissionError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(target).data)


class NuwaSwitchesView(APIView):
    """管理员开关：与智能体工具出网（mcp_egress_enabled）完全独立"""

    permission_classes = [IsAdminUser]

    def get(self, request):
        return Response(config.switches())

    def post(self, request):
        payload = request.data or {}
        switches = config.set_flags(
            market_online_value=payload.get('market_online'),
            knowledge_online_value=payload.get('knowledge_online'),
            allowlist=payload.get('knowledge_allowlist')
            if isinstance(payload.get('knowledge_allowlist'), list) else None,
            actor=request.user,
        )
        return Response(switches)
