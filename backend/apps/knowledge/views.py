from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.views import APIView
from rest_framework.exceptions import PermissionDenied
from django.db import models, transaction
from django.utils import timezone
from django.http import StreamingHttpResponse
import logging

from .models import (
    KnowledgeBase, Document, TiangongSource, TiangongSourceProposal,
    TiangongSearchBinding, can_manage_kb, visible_kb_query,
)
from .services import search_chunks
from .tasks import process_document_task, tiangong_run_task
from . import vectorstore, tiangong, discovery, searxng, mcp_search, bing


logger = logging.getLogger(__name__)


from .serializers import (
    KnowledgeBaseSerializer, DocumentSerializer,
    DocumentChunkSerializer,
    DocumentUploadSerializer, ChatRequestSerializer,
    TiangongRunSerializer, TiangongSourceSerializer, TiangongSourceProposalSerializer,
    TiangongSearchBindingSerializer,
)
from backend.apps.common.pagination import StandardPagination
from backend.apps.common.llm import format_sse_event
# 保留 single_embedding 名字以兼容既有调用/测试 patch，实际为本地 fastembed
from backend.apps.common.embedding import embed_query as single_embedding
from backend.apps.users.models import log_audit
from backend.apps.agents.core import (
    AGENT_TYPE_KNOWLEDGE, create_agent_blocking,
    run_agent, run_agent_stream,
)


def _cleanup_vectors(kb_id: int, doc_id: int = None, drop_kb: bool = False) -> None:
    """Qdrant 向量清理（失败不阻塞 DB 删除，孤儿向量检索时自动跳过）"""
    if not vectorstore.is_enabled():
        return
    try:
        if drop_kb:
            vectorstore.drop_collection(kb_id)
        elif doc_id is not None:
            vectorstore.delete_document_vectors(kb_id, doc_id)
    except Exception as e:
        logger.warning('Qdrant 向量清理失败（kb=%s doc=%s）：%s', kb_id, doc_id, e)


def _delete_document_with_stats(doc) -> None:
    """删除文档并同步知识库冗余计数（document/chunk/size 三项，均下限为 0）"""
    kb = doc.knowledge_base
    with transaction.atomic():
        file_size = doc.file_size
        chunk_count = doc.chunk_count
        doc.delete()
        kb.document_count = max(0, kb.document_count - 1)
        kb.chunk_count = max(0, kb.chunk_count - chunk_count)
        kb.size_bytes = max(0, kb.size_bytes - file_size)
        kb.save()


class KnowledgeBaseViewSet(viewsets.ModelViewSet):
    """知识库 CRUD"""

    queryset = KnowledgeBase.objects.all()
    serializer_class = KnowledgeBaseSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = StandardPagination

    def get_queryset(self):
        # 管理员全量；普通用户：公开 + 自己创建 + 本部门共享
        if self.request.user.is_staff:
            return KnowledgeBase.objects.all().order_by('-updated_at')
        return KnowledgeBase.objects.filter(
            visible_kb_query(self.request.user)
        ).order_by('-updated_at')

    def perform_create(self, serializer):
        kb = serializer.save(created_by=self.request.user)
        if kb.tiangong_enabled:
            run = tiangong.create_run(kb, trigger='create', actor=self.request.user)
            tiangong_run_task.delay(run.id)
            log_audit(actor=self.request.user, action='tiangong_run_started',
                      target=kb.name, detail=f'建库自动采集 #{run.pk}')
            kb.refresh_from_db()

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if not can_manage_kb(request.user, instance):
            raise PermissionDenied('只有知识库创建者或平台管理员可以修改')
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        if not can_manage_kb(request.user, instance):
            raise PermissionDenied('只有知识库创建者或平台管理员可以修改')
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if not can_manage_kb(request.user, instance):
            raise PermissionDenied('只有知识库创建者或平台管理员可以删除')
        return super().destroy(request, *args, **kwargs)

    def perform_destroy(self, instance):
        # 删除知识库时连同 Qdrant collection 一起清理
        kb_id = instance.id
        kb_name = instance.name
        _cleanup_vectors(kb_id, drop_kb=True)
        instance.delete()
        log_audit(
            actor=self.request.user,
            action='kb_delete',
            target=kb_name,
            detail=f"删除知识库 {kb_name}（ID={kb_id}）",
        )

    @action(detail=True, methods=['post'], url_path='tiangong/run')
    def tiangong_run(self, request, pk=None):
        """手动补充采集：仅库属主/管理员；同一库同时只允许一个任务"""
        kb = self.get_object()
        if not can_manage_kb(request.user, kb):
            raise PermissionDenied('只有知识库创建者或平台管理员可以触发采集')
        if not kb.tiangong_enabled:
            return Response({'detail': '该知识库未开启天工模式，请先打开开关。'},
                            status=status.HTTP_400_BAD_REQUEST)
        if not tiangong.enabled():
            return Response({'detail': '管理员已关闭天工模式，请联系平台管理员。'},
                            status=status.HTTP_400_BAD_REQUEST)
        if kb.tiangong_runs.filter(status__in=('queued', 'running')).exists():
            return Response({'detail': '已有采集任务进行中，请稍后再试。'},
                            status=status.HTTP_409_CONFLICT)
        run = tiangong.create_run(kb, trigger='manual', actor=request.user)
        tiangong_run_task.delay(run.id)
        log_audit(actor=request.user, action='tiangong_run_started',
                  target=kb.name, detail=f'手动补充采集 #{run.pk}')
        run.refresh_from_db()
        return Response(TiangongRunSerializer(run).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'], url_path='tiangong/runs')
    def tiangong_runs(self, request, pk=None):
        """最近 20 条采集记录：谁触发、采到什么、为什么降级"""
        kb = self.get_object()
        runs = kb.tiangong_runs.select_related('actor')[:20]
        return Response(TiangongRunSerializer(runs, many=True).data)

    @action(detail=True, methods=['get'])
    def stats(self, request, pk=None):
        """知识库统计信息"""
        kb = self.get_object()
        return Response({
            'id': kb.id,
            'name': kb.name,
            'document_count': kb.document_count,
            'chunk_count': kb.chunk_count,
            'size_bytes': kb.size_bytes,
            'created_at': kb.created_at,
        })


class TiangongSwitchesView(APIView):
    """天工开关（管理员）：与女娲两个出网开关、智能体工具出网互不影响"""

    permission_classes = [IsAdminUser]

    def get(self, request):
        return Response(tiangong.switches())

    def post(self, request):
        enabled = request.data.get('enabled')
        online = request.data.get('online')
        return Response(tiangong.set_switches(
            enabled_value=enabled if isinstance(enabled, bool) else None,
            online_value=online if isinstance(online, bool) else None,
            actor=request.user,
        ))


class TiangongSourceViewSet(viewsets.ModelViewSet):
    """天工来源库：读对所有登录用户开放，写操作仅管理员"""

    queryset = TiangongSource.objects.all()
    serializer_class = TiangongSourceSerializer
    pagination_class = StandardPagination

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAuthenticated()]
        return [IsAdminUser()]

    def get_queryset(self):
        qs = TiangongSource.objects.select_related('created_by')
        keyword = (self.request.query_params.get('search') or '').strip()
        if keyword:
            qs = qs.filter(models.Q(name__icontains=keyword)
                           | models.Q(domain__icontains=keyword)
                           | models.Q(topic_tags__icontains=keyword))
        enabled = self.request.query_params.get('enabled')
        if enabled in ('true', 'false'):
            qs = qs.filter(enabled=enabled == 'true')
        return qs.order_by('priority', 'name')

    def perform_create(self, serializer):
        source = serializer.save(created_by=self.request.user)
        log_audit(actor=self.request.user, action='tiangong_source_create',
                  target=source.name,
                  detail=f'{source.domain}；启用={source.enabled}；优先级={source.priority}')

    def perform_update(self, serializer):
        source = serializer.save()
        log_audit(actor=self.request.user, action='tiangong_source_update',
                  target=source.name,
                  detail=f'{source.domain}；启用={source.enabled}；优先级={source.priority}')

    def perform_destroy(self, instance):
        name, domain = instance.name, instance.domain
        instance.delete()
        log_audit(actor=self.request.user, action='tiangong_source_delete',
                  target=name, detail=f'删除来源 {domain}')

    @action(detail=True, methods=['post'], url_path='check')
    def check(self, request, pk=None):
        """只读检查 robots / Crawl-delay / sitemap，不抓正文"""
        source = self.get_object()
        result = discovery.probe_source(source)
        source.last_checked_at = timezone.now()
        source.last_status = 'ok' if result['ok'] else 'failed'
        source.last_error = (result.get('error') or '')[:300]
        source.save(update_fields=['last_checked_at', 'last_status', 'last_error',
                                   'updated_at'])
        log_audit(actor=request.user, action='tiangong_source_check',
                  target=source.name,
                  detail=f'{source.domain}；robots={"允许" if result["robots_ok"] else "禁止"}；'
                         f'sitemap {result["sitemap_count"]} 条')
        return Response({**result, 'source': self.get_serializer(source).data})


class TiangongSourceProposalViewSet(viewsets.ReadOnlyModelViewSet):
    """候选来源：列表按可见知识库过滤，批准/拒绝仅管理员

    批准后创建或合并到来源库；未批准候选不会进入任何抓取链路。
    """

    serializer_class = TiangongSourceProposalSerializer
    pagination_class = StandardPagination

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAuthenticated()]
        return [IsAdminUser()]

    def get_queryset(self):
        user = self.request.user
        qs = TiangongSourceProposal.objects.select_related('knowledge_base', 'reviewed_by')
        if not user.is_staff:
            qs = qs.filter(knowledge_base__in=KnowledgeBase.objects.filter(
                visible_kb_query(user)))
        status_value = self.request.query_params.get('status')
        if status_value in ('pending', 'approved', 'rejected'):
            qs = qs.filter(status=status_value)
        discovered = self.request.query_params.get('discovered_by')
        if discovered in dict(TiangongSourceProposal.DISCOVERED_BY_CHOICES):
            qs = qs.filter(discovered_by=discovered)
        domain = (self.request.query_params.get('domain') or '').strip()
        if domain:
            qs = qs.filter(domain__icontains=domain)
        return qs.order_by('-created_at')

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        proposal = self.get_object()
        if proposal.status != 'pending':
            return Response({'detail': '该候选已处理过，不能重复审批'},
                            status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            source, created = TiangongSource.objects.get_or_create(
                domain=proposal.domain,
                defaults={
                    'name': (proposal.title or proposal.domain)[:200],
                    'entry_urls': proposal.url,
                    'topic_tags': proposal.topics,
                    'description': '由候选来源批准',
                    'created_by': request.user,
                },
            )
            if not created:
                urls = [line.strip() for line in (source.entry_urls or '').splitlines()
                        if line.strip()]
                if proposal.url not in urls:
                    urls.append(proposal.url)
                    source.entry_urls = '\n'.join(urls[:10])
                    source.save(update_fields=['entry_urls', 'updated_at'])
            proposal.status = 'approved'
            proposal.reviewed_by = request.user
            proposal.reviewed_at = timezone.now()
            proposal.reject_reason = ''
            proposal.save(update_fields=['status', 'reviewed_by', 'reviewed_at',
                                         'reject_reason'])
        log_audit(actor=request.user, action='tiangong_source_proposal_approve',
                  target=proposal.domain,
                  detail=f'批准候选 {proposal.url[:180]}；'
                         f'{"新建来源" if created else "合并入口"}')
        return Response({
            'proposal': self.get_serializer(proposal).data,
            'source': TiangongSourceSerializer(source).data,
            'created': created,
        })

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        proposal = self.get_object()
        if proposal.status != 'pending':
            return Response({'detail': '该候选已处理过，不能重复审批'},
                            status=status.HTTP_400_BAD_REQUEST)
        reason = (request.data.get('reason') or '').strip()[:300]
        proposal.status = 'rejected'
        proposal.reviewed_by = request.user
        proposal.reviewed_at = timezone.now()
        proposal.reject_reason = reason
        proposal.save(update_fields=['status', 'reviewed_by', 'reviewed_at',
                                     'reject_reason'])
        log_audit(actor=request.user, action='tiangong_source_proposal_reject',
                  target=proposal.domain,
                  detail=f'拒绝 {proposal.url[:180]}；原因：{reason or "未填写"}')
        return Response(self.get_serializer(proposal).data)


class TiangongSearchBindingViewSet(viewsets.ModelViewSet):
    """L1 MCP 搜索绑定：管理员从扫描候选中确认工具并配置映射

    绑定只保存 query/limit 与结果字段映射，不保存任何凭据；创建绑定不会自动
    启用对应的 MCP 工具（工具开关仍由 MCP 管理端显式控制）。
    """

    queryset = TiangongSearchBinding.objects.select_related('server', 'tool', 'created_by')
    serializer_class = TiangongSearchBindingSerializer
    permission_classes = [IsAdminUser]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = super().get_queryset()
        enabled = self.request.query_params.get('enabled')
        if enabled in ('true', 'false'):
            qs = qs.filter(enabled=enabled == 'true')
        return qs.order_by('priority', 'id')

    def perform_create(self, serializer):
        binding = serializer.save(created_by=self.request.user)
        log_audit(actor=self.request.user, action='tiangong_search_binding_create',
                  target=str(binding)[:100],
                  detail=(f'query_field={binding.query_field}；'
                          f'limit_field={binding.limit_field or "-"}；'
                          f'priority={binding.priority}；工具启用={binding.tool.enabled}'))

    def perform_update(self, serializer):
        binding = serializer.save()
        log_audit(actor=self.request.user, action='tiangong_search_binding_update',
                  target=str(binding)[:100],
                  detail=(f'启用={binding.enabled}；priority={binding.priority}；'
                          f'query_field={binding.query_field}；'
                          f'limit_field={binding.limit_field or "-"}'))

    def perform_destroy(self, instance):
        name = str(instance)[:100]
        instance.delete()
        log_audit(actor=self.request.user, action='tiangong_search_binding_delete',
                  target=name, detail='删除天工 MCP 搜索绑定')

    @action(detail=True, methods=['post'])
    def test(self, request, pk=None):
        """用一条样例查询验证映射；只更新绑定状态，不改工具开关"""
        binding = self.get_object()
        query = (request.data.get('query') or '测试').strip()[:60]
        outcome = mcp_search.test_binding(binding, query=query, actor=request.user)
        return Response({
            **outcome,
            'binding': self.get_serializer(binding).data,
        })


class TiangongSearchCandidatesView(APIView):
    """扫描已接入 MCP 工具，按名称/描述/入参打分给出搜索工具候选（只读）"""

    permission_classes = [IsAdminUser]

    def get(self, request):
        candidates = mcp_search.scan_candidates()
        return Response({'candidates': candidates, 'total': len(candidates)})


class TiangongDiscoverySettingsView(APIView):
    """搜索层开关、SearXNG 地址/超时、抓取页数上限（仅管理员）"""

    permission_classes = [IsAdminUser]

    def get(self, request):
        return Response(tiangong.discovery_settings())

    def post(self, request):
        try:
            data = tiangong.set_discovery_settings(request.data, actor=request.user)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(data)

    def put(self, request):
        return self.post(request)


class TiangongSearxngCheckView(APIView):
    """检查本机 SearXNG：/healthz → 退化为一次 JSON 检索"""

    permission_classes = [IsAdminUser]

    def post(self, request):
        return Response(searxng.health_check())


class TiangongBingCheckView(APIView):
    """检查 Playwright + Chromium 是否可用（真正启动一次 headless 浏览器）"""

    permission_classes = [IsAdminUser]

    def post(self, request):
        return Response(bing.health_check())


class DocumentViewSet(viewsets.ModelViewSet):
    """文档管理"""

    queryset = Document.objects.all()
    serializer_class = DocumentSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = StandardPagination

    @staticmethod
    def _visible_kb_or_none(user, kb_id):
        """按可见性取知识库（管理员全量），不存在/不可见返回 None"""
        qs = KnowledgeBase.objects.all() if user.is_staff else KnowledgeBase.objects.filter(
            visible_kb_query(user)
        )
        return qs.filter(id=kb_id).first()

    def _assert_can_manage(self, doc):
        """文档写权限跟所属知识库的属主走（可见不等于可写）"""
        if not can_manage_kb(self.request.user, doc.knowledge_base):
            raise PermissionDenied('只有知识库创建者或平台管理员可以管理其文档')

    def get_queryset(self):
        # 仅可见知识库内的文档（管理员全量）
        user = self.request.user
        if user.is_staff:
            queryset = Document.objects.all()
        else:
            visible_kbs = KnowledgeBase.objects.filter(visible_kb_query(user))
            queryset = Document.objects.filter(knowledge_base__in=visible_kbs)
        kb_id = self.request.query_params.get('knowledge_base_id')
        if kb_id:
            queryset = queryset.filter(knowledge_base_id=kb_id)
        return queryset.order_by('-created_at')

    @action(detail=False, methods=['post'], url_path='upload')
    def upload(self, request):
        """上传文档"""
        serializer = DocumentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        kb_id = serializer.validated_data['knowledge_base_id']
        title = serializer.validated_data.get('title')

        kb = self._visible_kb_or_none(request.user, kb_id)
        if kb is None:
            return Response(
                {'detail': '知识库不存在'},
                status=status.HTTP_404_NOT_FOUND
            )
        if not can_manage_kb(request.user, kb):
            raise PermissionDenied('只有知识库创建者或平台管理员可以上传文档')

        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response(
                {'detail': '请上传文件'},
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            doc = Document.objects.create(
                knowledge_base=kb,
                title=title or file_obj.name,
                file_name=file_obj.name,
                file_type=file_obj.name.split('.')[-1].lower() if '.' in file_obj.name else '',
                file_size=file_obj.size,
                file_path=file_obj,
                uploaded_by=request.user,
                status='pending',
            )

            # 更新知识库统计
            kb.document_count += 1
            kb.size_bytes += file_obj.size
            kb.save()

        # 向量化丢给 Celery 异步执行（eager 模式下同步完成，行为同旧实现）
        process_document_task.delay(doc.id)
        doc.refresh_from_db()

        return Response(
            DocumentSerializer(doc, context={'request': request}).data,
            status=status.HTTP_201_CREATED
        )

    def update(self, request, *args, **kwargs):
        self._assert_can_manage(self.get_object())
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        self._assert_can_manage(self.get_object())
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        self._assert_can_manage(self.get_object())
        return super().destroy(request, *args, **kwargs)

    def perform_destroy(self, instance):
        # 默认 DELETE /documents/{id}/ 路径也要清理 Qdrant 向量并同步计数
        _cleanup_vectors(instance.knowledge_base_id, instance.id)
        _delete_document_with_stats(instance)

    @action(detail=True, methods=['delete'], url_path='delete')
    def delete_document(self, request, pk=None):
        """删除文档"""
        doc = self.get_object()
        self._assert_can_manage(doc)
        _cleanup_vectors(doc.knowledge_base_id, doc.id)
        _delete_document_with_stats(doc)

        return Response({'detail': '删除成功'})

    @action(detail=True, methods=['post'], url_path='reprocess')
    def reprocess(self, request, pk=None):
        """重新解析文档：重置状态并重新排队处理流水线（提取/分块/向量化）"""
        doc = self.get_object()
        self._assert_can_manage(doc)
        if doc.status == 'processing':
            return Response(
                {'detail': '文档正在处理中，请稍后再试'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        doc.status = 'pending'
        doc.error_msg = ''
        doc.save(update_fields=['status', 'error_msg'])
        process_document_task.delay(doc.id)
        doc.refresh_from_db()
        return Response(DocumentSerializer(doc, context={'request': request}).data)

    @action(detail=True, methods=['get'], url_path='chunks')
    def chunks(self, request, pk=None):
        """文档分块预览：块号/内容/向量状态/禁检标记，按块号升序"""
        doc = self.get_object()
        qs = doc.chunks.all().order_by('chunk_index')
        return Response(DocumentChunkSerializer(qs, many=True).data)

    @action(detail=True, methods=['patch'], url_path=r'chunks/(?P<chunk_id>\d+)')
    def chunk_toggle(self, request, pk=None, chunk_id=None):
        """分块禁检/恢复：{is_active: bool}

        禁检时同步从 Qdrant 移除向量点（检索不再命中）；
        恢复时用 DB 中的 embedding 重新 upsert。
        """
        doc = self.get_object()
        self._assert_can_manage(doc)
        chunk = doc.chunks.filter(id=chunk_id).first()
        if chunk is None:
            return Response({'detail': '分块不存在'}, status=status.HTTP_404_NOT_FOUND)

        is_active = request.data.get('is_active')
        if not isinstance(is_active, bool):
            return Response({'detail': 'is_active 必须是布尔值'}, status=status.HTTP_400_BAD_REQUEST)

        if chunk.is_active != is_active:
            chunk.is_active = is_active
            chunk.save(update_fields=['is_active'])
            if vectorstore.is_enabled():
                try:
                    if is_active:
                        if chunk.embedding:
                            vectorstore.upsert_chunks(
                                doc.knowledge_base_id,
                                [(chunk.id, chunk.embedding, doc.id)],
                            )
                    else:
                        vectorstore.delete_points(doc.knowledge_base_id, [chunk.id])
                except Exception as e:
                    logger.warning('分块禁检状态同步 Qdrant 失败（chunk=%s）：%s', chunk.id, e)

        return Response(DocumentChunkSerializer(chunk).data)


class ChatViewSet(viewsets.ViewSet):
    """知识库对话"""

    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['post'])
    def ask(self, request):
        """
        向知识库提问

        支持 stream=true 时返回 SSE 流式响应
        """
        serializer = ChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        question = serializer.validated_data['question']
        kb_id = serializer.validated_data['knowledge_base_id']
        stream = serializer.validated_data.get('stream', False)

        kb = DocumentViewSet._visible_kb_or_none(request.user, kb_id)
        if kb is None:
            return Response(
                {'detail': '知识库不存在'},
                status=status.HTTP_404_NOT_FOUND
            )

        # 语义检索真实命中分块，用于前端 sources 展示与引用定位；
        # 回答由知识库智能体生成 —— 它会自主调用
        # search_knowledge_base 工具检索并基于结果作答
        hits = search_chunks(kb.id, single_embedding(question), top_k=3)
        sources = [
            {
                'title': hit['doc_title'],
                'score': round(hit['score'], 2),
                'chunk_id': hit['chunk_id'],
                'doc_id': hit['doc_id'],
                'chunk_index': hit['chunk_index'],
                'content': hit['content'],
                'meta': hit.get('meta') or {},
            }
            for hit in hits
        ]

        if stream:
            # 流式响应
            response = StreamingHttpResponse(
                self._stream_answer(question=question, sources=sources, kb=kb),
                content_type='text/event-stream',
            )
            response['Cache-Control'] = 'no-cache'
            response['X-Accel-Buffering'] = 'no'
            return response

        # 非流式
        try:
            agent = create_agent_blocking(
                agent_type=AGENT_TYPE_KNOWLEDGE,
                resource_id=kb.id,
                stream=False,
            )
            answer = run_agent(agent, [], question)
        except Exception as e:
            return Response(
                {'detail': str(e)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )

        return Response({
            'answer': answer,
            'sources': sources,
            'knowledge_base': {
                'id': kb.id,
                'name': kb.name,
            },
        })

    def _stream_answer(self, question, sources, kb):
        """SSE 流式生成知识库回答（AgentScope 事件 → SSE 事件）"""
        yield format_sse_event('start', {
            'knowledge_base': {'id': kb.id, 'name': kb.name},
            'sources': sources,
        })

        try:
            agent = create_agent_blocking(
                agent_type=AGENT_TYPE_KNOWLEDGE,
                resource_id=kb.id,
                stream=True,
            )

            answer_text = ''
            for ev in run_agent_stream(agent, [], question):
                if ev['type'] == 'chunk':
                    answer_text += ev['content']
                    yield format_sse_event('chunk', {'content': ev['content']})
                elif ev['type'] == 'error':
                    yield format_sse_event('error', ev['message'])
                    yield format_sse_event('end', '')
                    return

            yield format_sse_event('end', {
                'answer_length': len(answer_text),
            })
        except Exception as e:
            yield format_sse_event('error', str(e))
            yield format_sse_event('end', '')
