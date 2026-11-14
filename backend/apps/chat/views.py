import hashlib
import json
import logging
import os
import re
import uuid
from datetime import timedelta
from pathlib import Path

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import BaseRenderer
from rest_framework.views import APIView
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.encoding import escape_uri_path
from .models import (
    Conversation, Message, ChatGeneratedFile, ToolCallBody, TOOL_BODY_MAX_CHARS,
)
from .serializers import (
    ConversationSerializer, MessageSerializer, ChatRequestSerializer,
    ChatRegenerateSerializer,
)
from .services import export_conversation, needs_rewrite, standalone_question, agent_switch_enabled
from .preferences import active_preference_texts, mark_preferences_used, preference_block
from .permissions import PermissionSession, resolve_permission
from backend.apps.common.pagination import StandardPagination
from backend.apps.common.llm import check_health, format_sse_event, get_llm_config
from backend.apps.agents.core import (
    create_agent_blocking, run_agent, run_agent_stream,
    run_profile_reply, run_profile_stream,
)
from backend.apps.agents.tools import multi_kb_search
from backend.apps.agenthub.models import AgentProfile, AgentStatus
from backend.apps.agenthub.services import can_edit, can_see, runtime_config
from backend.apps.common.choices import Visibility
from backend.apps.knowledge.services import extract_text
from backend.apps.users.models import log_audit
from django.conf import settings

logger = logging.getLogger(__name__)


class EventStreamRenderer(BaseRenderer):
    """SSE 流式响应渲染器"""
    media_type = 'text/event-stream'
    format = 'text/event-stream'
    charset = 'utf-8'
    render_style = 'streamed'

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return data


def _sse(payload: dict) -> bytes:
    """试聊事件帧：event 名取 payload['type']，data 为完整 JSON（保留 type 键）"""
    return format_sse_event(str(payload.get('type') or 'message'), payload)


def _clean_trial_history(raw):
    """A3 试聊上下文：只收 user/assistant 文本、保留最近 20 条

    试聊不留会话记录，多轮上下文由前端在每次请求里回传，服务端不信任其结构。
    """
    if not isinstance(raw, list):
        return []
    items = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        role, content = m.get('role'), m.get('content')
        if role in ('user', 'assistant') and isinstance(content, str) and content.strip():
            items.append({'role': role, 'content': content})
    return items[-20:]


def _elapsed_ms(value):
    """把桥接层的秒级 elapsed 转成毫秒整数；缺失或非法一律 None（不参与平均耗时）"""
    if value is None:
        return None
    try:
        return max(0, int(round(float(value) * 1000)))
    except (TypeError, ValueError):
        return None


def _source_of(display, name):
    """从过程区展示名拆出来源名（口径同 D7 授权卡片）

    展示名形如「工单系统·list_tickets」；平台内置工具没有「来源·工具」结构，
    统一记为「平台内置」。
    """
    text = display or name or ''
    if '·' in text:
        return text.split('·', 1)[0].strip()[:200]
    return '平台内置'


def _record_tool_call(*, actor, action, target, detail, egress, conversation, payload,
                      full_result='', agent_name=''):
    """写工具调用留痕：审计摘要 + 全文独立表（PRD 9.2 #4）

    审计表只放摘要（列表页轻量），原始未压平全文落 ToolCallBody。
    两处在同一事务内写；全文写失败时回滚到内层 savepoint 并降级为
    「只保留审计摘要」，外层审计仍提交，异常不外抛，不影响对话流。
    F4 完整版：agent_name / source_name / elapsed_ms 落成一等列，供三维度聚合。
    """
    with transaction.atomic():
        row = log_audit(
            actor, action, target, detail, egress=egress,
            agent_name=agent_name,
            source_name=_source_of(payload.get('display'), payload.get('name')),
            elapsed_ms=_elapsed_ms(payload.get('elapsed')),
        )
        raw = full_result or ''
        try:
            with transaction.atomic():
                ToolCallBody.objects.create(
                    audit=row,
                    conversation=conversation,
                    call_id=str(payload.get('call_id') or '')[:64],
                    tool=str(payload.get('name') or '')[:160],
                    actor=actor,
                    state=str(payload.get('state') or '')[:16],
                    egress=bool(egress),
                    result=raw[:TOOL_BODY_MAX_CHARS],
                    result_chars=len(raw),
                    result_sha256=hashlib.sha256(raw.encode('utf-8')).hexdigest(),
                    storage_truncated=len(raw) > TOOL_BODY_MAX_CHARS,
                )
        except Exception:
            logger.exception('工具调用全文写入失败，降级为仅保留审计摘要 audit=%s', row.pk)
    return row


def _make_trial_auditor(user, agent_name):
    """A3 试聊工具留痕（PRD 6.1：调用标记来源「试聊」）

    与正式调用分动作记录（trial_tool_call），F4 用量统计不受试聊影响；
    失败与拒绝不阻断试聊，留痕写失败后继续。试聊不产生会话，
    全文落库时 conversation 为空（PRD 9.2 #4）。
    """
    def on_tool(event, full_result=''):
        try:
            state = event.get('state')
            _record_tool_call(
                actor=user,
                action='trial_tool_call',
                target=f'智能体:{agent_name} · 工具:{event.get("display") or event.get("name")}',
                detail=json.dumps({
                    'source': '试聊',
                    'agent': agent_name,
                    'tool': event.get('name'),
                    'args': event.get('args') or '',
                    'result': ('' if state == 'success'
                               else (event.get('result') or ''))[:500],
                    'state': state,
                    'elapsed_ms': event.get('elapsed'),
                }, ensure_ascii=False),
                egress=bool(event.get('egress')),
                conversation=None,
                payload=event,
                full_result=full_result,
                agent_name=agent_name,
            )
        except Exception:
            logger.exception('试聊工具调用留痕写入失败')
    return on_tool


class ConversationViewSet(viewsets.ModelViewSet):
    """对话会话管理"""

    queryset = Conversation.objects.all()
    serializer_class = ConversationSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = StandardPagination

    def get_queryset(self):
        queryset = Conversation.objects.filter(user=self.request.user)
        conv_type = self.request.query_params.get('type')
        if conv_type:
            queryset = queryset.filter(type=conv_type)
        # P1-c：列表一次带出「已被切换承接」标记，避免逐行 EXISTS；
        # 只用于展示只读状态，不参与会话绑定判断
        queryset = queryset.annotate(
            switched_away=Exists(
                Conversation.objects.filter(switched_from=OuterRef('pk')))
        )
        return queryset.order_by('-last_message_at')

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=True, methods=['get'])
    def messages(self, request, pk=None):
        """获取会话的消息列表"""
        conv = self.get_object()
        messages = conv.messages.all().order_by('created_at')
        page = self.paginate_queryset(messages)
        if page is not None:
            serializer = MessageSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = MessageSerializer(messages, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def delete_conversation(self, request, pk=None):
        """删除会话"""
        conv = self.get_object()
        conv.delete()
        return Response({'detail': '删除成功'})

    @action(detail=True, methods=['get'])
    def export(self, request, pk=None):
        """导出会话为 Markdown 或 Word"""
        # 注意：self.get_object() 在本 viewset 的测试环境下会异常返回 404，
        # 直接使用 owner+pk 查询可保持行为一致并复用 get_queryset 的权限隔离语义。
        try:
            conv = Conversation.objects.get(pk=pk, user=request.user)
        except Conversation.DoesNotExist:
            return Response({'detail': '会话不存在'}, status=status.HTTP_404_NOT_FOUND)
        export_type = request.query_params.get('type', 'md')
        if export_type not in ('md', 'docx', 'pdf'):
            return Response(
                {'detail': '不支持的导出格式，仅支持 md、docx 或 pdf'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            content_bytes, mime_type, ext = export_conversation(conv, export_type)
        except ImportError as e:
            return Response(
                {'detail': f'导出服务未就绪：{e}'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        title = (conv.title or '未命名会话').replace(' ', '_').replace('/', '_')
        filename = f'{title}.{ext}'
        # RFC5987 中文文件名
        response = HttpResponse(
            content_bytes,
            content_type=mime_type,
        )
        response['Content-Disposition'] = (
            f"attachment; filename=\"{filename}\"; "
            f"filename*=UTF-8''{escape_uri_path(filename)}"
        )
        return response

    @action(detail=False, methods=['get'], url_path='agent-switch-policy')
    def agent_switch_policy(self, request):
        """P1-c：前端据此决定是否展示「切换助手」入口；关闭时不改变任何既有行为"""
        return Response({'enabled': agent_switch_enabled()})

    @action(detail=True, methods=['post'], url_path='switch-agent')
    def switch_agent(self, request, pk=None):
        """P1-c：切换助手 = 新建会话承接历史，旧会话转为只读

        方案 B：不改写原会话的绑定（D1「一条会话一个助手」不变量不变），
        把已有消息复制到新会话，新会话绑定目标助手；旧会话只保留回看。
        """
        if not agent_switch_enabled():
            return Response(
                {'detail': '当前环境未开放会话内切换助手，请新建会话后重新选择助手。',
                 'code': 'agent_switch_disabled'},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            conv = Conversation.objects.get(pk=pk, user=request.user)
        except Conversation.DoesNotExist:
            return Response(
                {'detail': '会话不存在', 'code': 'conversation_not_found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            agent_id = int(request.data.get('agent_id'))
        except (TypeError, ValueError):
            return Response(
                {'detail': '请选择要切换到的助手', 'code': 'agent_required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 可见性校验与发送消息同一口径：不可见一律显式报错，绝不静默回落
        agent, agent_error = ChatViewSet()._resolve_agent(request.user, agent_id)
        if agent_error is not None:
            return agent_error
        if agent is None:
            return Response(
                {'detail': '请选择要切换到的助手', 'code': 'agent_required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if agent.status != AgentStatus.PUBLISHED:
            return Response(
                {'detail': f'智能体「{agent.name}」未发布，不能用于对话。',
                 'code': 'agent_unavailable'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if conv.agent_id == agent.pk:
            return Response(
                {'detail': '当前会话已经使用的是这个助手。', 'code': 'agent_same'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if Conversation.objects.filter(switched_from=conv).exists():
            return Response(
                {'detail': '该会话已切换为新会话承接，历史只读，不能再次切换。',
                 'code': 'conversation_readonly'},
                status=status.HTTP_409_CONFLICT,
            )

        originals = list(conv.messages.all().order_by('created_at', 'id'))
        with transaction.atomic():
            new_conv = Conversation.objects.create(
                user=request.user,
                title=conv.title,
                type=conv.type,
                resource_id=conv.resource_id,
                knowledge_base_ids=list(conv.knowledge_base_ids or []),
                agent=agent,
                agent_version=agent.current_version or None,
                use_preferences=conv.use_preferences,
                switched_from=conv,
                message_count=len(originals),
            )
            if originals:
                created = Message.objects.bulk_create([
                    Message(conversation=new_conv, role=m.role, content=m.content,
                            meta=m.meta, attachments=m.attachments)
                    for m in originals
                ])
                # bulk_create 会被 auto_now_add 改写成当前时间；回写原始时间，
                # 让承接会话的历史时间线与原会话保持一致
                for row, src in zip(created, originals):
                    Message.objects.filter(pk=row.pk).update(created_at=src.created_at)

        return Response(
            ConversationSerializer(new_conv).data,
            status=status.HTTP_201_CREATED,
        )


class ChatViewSet(viewsets.ViewSet):
    """对话接口（发送消息 + 流式响应）"""

    permission_classes = [IsAuthenticated]

    # 单附件大小限制：10 MB
    _MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024
    # 提取文本截断字数（中文语境下字符数 ≈ token 数，给 prompt 留预算）
    _MAX_ATTACHMENT_TEXT = 6000

    @action(detail=False, methods=['post'])
    def upload_attachment(self, request):
        """
        上传对话临时附件，提取文本后返回，不入知识库/不向量化。

        支持：txt / md / pdf / docx / xlsx / pptx
        """
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({'detail': '未提供文件'}, status=status.HTTP_400_BAD_REQUEST)

        if file_obj.size > self._MAX_ATTACHMENT_SIZE:
            return Response(
                {'detail': f'文件大小超过 {self._MAX_ATTACHMENT_SIZE // 1024 // 1024} MB 限制'},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        name = getattr(file_obj, 'name', 'unknown')
        ext = Path(name).suffix.lower().lstrip('.')
        if ext not in ('txt', 'md', 'pdf', 'docx', 'xlsx', 'pptx'):
            return Response(
                {'detail': f'不支持的文件类型：.{ext}（支持 txt/md/pdf/docx/xlsx/pptx）'},
                status=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            )

        # 临时目录：按用户隔离，cron / 清理脚本可整体删除
        save_dir = Path(settings.MEDIA_ROOT) / 'chat_attachments' / str(request.user.id)
        save_dir.mkdir(parents=True, exist_ok=True)
        tmp_name = f'{uuid.uuid4().hex}.{ext}'
        file_path = save_dir / tmp_name

        try:
            with open(file_path, 'wb') as f:
                for chunk in file_obj.chunks():
                    f.write(chunk)

            text = extract_text(str(file_path), ext)
            truncated = False
            if len(text) > self._MAX_ATTACHMENT_TEXT:
                text = text[:self._MAX_ATTACHMENT_TEXT]
                truncated = True

            # 解析成功后删除临时文件（文本已随请求带走）
            os.remove(file_path)

            return Response({
                'name': name,
                'size': file_obj.size,
                'type': ext,
                'text': text,
                'truncated': truncated,
            })
        except Exception as e:
            # 异常时兜底清理
            if file_path.exists():
                os.remove(file_path)
            return Response(
                {'detail': f'文件解析失败：{str(e)}'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

    def _inject_attachments(self, message: str, attachments: list) -> str:
        """将附件文本包装为前缀注入用户消息"""
        if not attachments:
            return message

        parts = []
        for idx, att in enumerate(attachments, start=1):
            name = att.get('name', f'附件{idx}')
            text = att.get('text', '')
            parts.append(f'【附件：{name}】\n{text}')

        prefix = '\n\n'.join(parts)
        return f'{prefix}\n\n用户问题：{message}'

    @action(detail=False, methods=['post'])
    def send(self, request):
        """
        发送消息，获取 AI 回答

        所有请求统一路由到 AgentScope 智能体：根据会话类型
        （chat / knowledge / reading）由注册中心装配对应智能体。

        支持 stream=true 时返回 SSE 流式响应，事件类型：
        - start: 开始生成
        - chunk: 内容分片
        - end: 生成结束
        - error: 错误
        """
        serializer = ChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user_message = serializer.validated_data['message']
        conversation_id = serializer.validated_data.get('conversation_id')
        conv_type = serializer.validated_data.get('type', 'chat')
        resource_id = serializer.validated_data.get('resource_id')
        stream = serializer.validated_data.get('stream', False)
        temperature = serializer.validated_data.get('temperature')
        attachments = serializer.validated_data.get('attachments', [])
        # None=沿用会话已选知识库；列表（含空）=本次显式设置
        kb_ids = self._filter_visible_kb_ids(
            request.user, serializer.validated_data.get('knowledge_base_ids')
        )

        # 把附件文本注入用户消息上下文（仅本轮有效，不入知识库）
        prompt_message = self._inject_attachments(user_message, attachments)

        # D1：本次显式指定的智能体，不可见/不存在直接拒绝，绝不静默退回通用助手
        agent, agent_error = self._resolve_agent(
            request.user, serializer.validated_data.get('agent_id')
        )
        if agent_error is not None:
            return agent_error

        # D1：会话绑定后中途不换助手，助手已下线的会话只读
        binding_error = self._check_agent_binding(request.user, conversation_id, agent)
        if binding_error is not None:
            return binding_error

        # 获取或创建会话
        conversation = self._get_or_create_conversation(
            request.user, conversation_id, conv_type, resource_id, user_message,
            knowledge_base_ids=kb_ids, agent=agent,
        )
        selected_kb_ids = list(conversation.knowledge_base_ids or [])

        # 历史消息（system prompt 由智能体规格提供，
        # 本轮用户消息由桥接层注入）
        history = self._build_history(conversation)

        # E3：按会话所钉版本的检索策略决定是否先检索；追问补全只影响检索问题
        sources, retrieval_question = self._plan_retrieval(
            conversation, request.user, user_message, history)
        prompt_message = self._append_retrieval_hint(
            prompt_message, user_message, retrieval_question)

        if stream:
            # 流式响应
            response = StreamingHttpResponse(
                self._stream_response(
                    conversation=conversation,
                    user_message=user_message,
                    prompt_message=prompt_message,
                    history=history,
                    temperature=temperature,
                    attachments=attachments,
                    user_id=request.user.id,
                    sources=sources,
                    knowledge_base_ids=selected_kb_ids,
                    user=request.user,
                ),
                content_type='text/event-stream',
            )
            response['Cache-Control'] = 'no-cache'
            response['X-Accel-Buffering'] = 'no'
            return response

        # 非流式响应：绑定了智能体的会话走按定义装配的运行时（D1），
        # 未绑定的历史会话仍走平台通用助手
        degrade_notes = []
        collected_sources = []
        preference_texts, preference_ids = self._preferences_for(request.user, conversation)
        preference_prompt = preference_block(preference_texts)
        mark_preferences_used(preference_ids)

        def _collect_sources(hits):
            collected_sources.extend(self._source_dict(hit) for hit in hits)

        usage = {
            'input_tokens': 0, 'output_tokens': 0,
            'cache_input_tokens': 0, 'cache_creation_input_tokens': 0,
            'model_calls': 0,
        }
        try:
            if conversation.agent_id:
                answer, degrade_notes, usage = run_profile_reply(
                    conversation.agent_id, history, prompt_message,
                    user=request.user,
                    user_id=request.user.id,
                    conversation_id=conversation.id,
                    on_tool=self._make_tool_auditor(request.user, conversation),
                    agent_version=conversation.agent_version,
                    on_knowledge_sources=_collect_sources,
                    preference_prompt=preference_prompt,
                    return_usage=True,
                )
            else:
                agent = create_agent_blocking(
                    agent_type=conversation.type,
                    resource_id=conversation.resource_id,
                    stream=False,
                    temperature=temperature,
                    user_id=request.user.id,
                    conversation_id=conversation.id,
                    knowledge_base_ids=selected_kb_ids,
                )
                answer = run_agent(agent, history, prompt_message)
        except Exception as e:
            return Response(
                {'detail': str(e)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )

        # E3 按需查：引用来源以模型真实调用的检索结果为准
        sources = self._merge_sources(sources, collected_sources)

        # 保存消息
        with transaction.atomic():
            Message.objects.create(
                conversation=conversation,
                role='user',
                content=user_message,
                attachments=attachments,
            )
            meta = self._build_meta(sources, degrade_notes, len(preference_texts))
            if usage['model_calls']:
                meta['usage'] = dict(usage)
            assistant_msg = Message.objects.create(
                conversation=conversation,
                role='assistant',
                content=answer,
                meta=meta,
                input_tokens=usage['input_tokens'],
                output_tokens=usage['output_tokens'],
            )
            generated_files = self._link_generated_files(answer, assistant_msg)
            conversation.message_count += 2
            # 更新标题（第一条用户消息作为标题）
            if conversation.message_count == 2:
                conversation.title = user_message[:30]
            conversation.save()

        return Response({
            'conversation_id': conversation.id,
            'answer': answer,
            'generated_files': generated_files,
            'sources': sources,
            'degrade': [self._degrade_text(n) for n in degrade_notes],
            'preferences': len(preference_texts),
        })

    @action(detail=False, methods=['post'], url_path='trial')
    def trial(self, request):
        """A3 试聊：按智能体最新保存的草稿配置跑一轮临时会话

        未发布的改动不用发布即可验证（draft 装配路径）；不留会话与消息，
        多轮上下文由前端回传；工具调用写审计并标记来源「试聊」，不计入 F4 用量。
        仅创建者与平台管理员可用（与草稿编辑权限同口径）。
        """
        message = (request.data.get('message') or '').strip()
        if not message:
            return Response({'detail': '请输入问题', 'code': 'empty_message'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            agent_id = int(request.data.get('agent_id'))
        except (TypeError, ValueError):
            return Response({'detail': '缺少或非法的智能体标识', 'code': 'agent_required'},
                            status=status.HTTP_400_BAD_REQUEST)
        agent = AgentProfile.objects.filter(pk=agent_id).first()
        if agent is None:
            return Response({'detail': '智能体不存在或已被删除', 'code': 'agent_not_found'},
                            status=status.HTTP_404_NOT_FOUND)
        if not can_edit(request.user, agent):
            return Response({'detail': '只有创建者或平台管理员可以试聊该智能体',
                             'code': 'forbidden'},
                            status=status.HTTP_403_FORBIDDEN)

        health = check_health()
        if health.get('status') != 'ok':
            return Response(
                {'error': '模型服务暂不可用，请先在系统设置中确认接入',
                 'detail': health.get('error')},
                status=status.HTTP_503_SERVICE_UNAVAILABLE)

        history = _clean_trial_history(request.data.get('messages'))
        user = request.user
        on_tool = _make_trial_auditor(user, agent.name)
        permission_session = PermissionSession(
            user_id=user.id, agent_id=agent.pk, on_grant=None)

        def event_stream():
            thread_id = str(uuid.uuid4())
            yield _sse({'type': 'start', 'thread_id': thread_id})
            try:
                for ev in run_profile_stream(agent.pk, history, message,
                                             user=user, user_id=user.id,
                                             draft=True, on_tool=on_tool,
                                             permission_session=permission_session):
                    etype = ev.get('type')
                    if etype == 'chunk':
                        yield _sse({'type': 'chunk', 'content': ev.get('content', '')})
                    elif etype == 'degrade':
                        # 与 send 同口径：降级条压平为 string[] 再下发
                        notes = [self._degrade_text(n) for n in (ev.get('notes') or [])]
                        if notes:
                            yield _sse({'type': 'degrade', 'notes': notes})
                    elif etype in ('thinking', 'tool', 'tool_args', 'tool_result',
                                   'permission_request', 'permission_result'):
                        yield _sse(ev)
                    elif etype == 'keepalive':
                        yield ': keep-alive\n\n'
                    elif etype == 'error':
                        # 试聊不留档，出错把原因透出后即结束本轮（与 send 一致透传原文）
                        yield _sse({'type': 'error', 'message': ev.get('message', '')})
                        return
                yield _sse({'type': 'done'})
            except Exception as exc:
                logger.exception('试聊流式生成失败')
                yield _sse({'type': 'error', 'message': str(exc)})

        response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        return response

    @action(detail=False, methods=['post'])
    def regenerate(self, request):
        """
        D2：重新生成回答

        不新增用户消息，只对会话内最新一版助手答案重出一版；旧答案标记为
        meta.superseded 留档（前端呈现「上一版」供对比），并从后续上下文里
        剔除，避免模型看到同一问的两个答案。返回 SSE 流，事件与 send 一致。
        """
        serializer = ChatRegenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        conversation = get_object_or_404(
            Conversation, pk=serializer.validated_data['conversation_id'], user=request.user
        )

        # D1：助手已下线的会话只读，连重新生成也不放行
        offline = self._check_agent_binding(request.user, conversation.pk, None)
        if offline is not None:
            return offline

        latest = conversation.messages.filter(role='assistant').order_by('-created_at', '-id').first()
        if latest is None:
            return Response(
                {'detail': '该会话还没有可重新生成的回答', 'code': 'message_not_found'},
                status=status.HTTP_404_NOT_FOUND,
            )
        wanted = serializer.validated_data.get('message_id')
        if wanted is not None and wanted != latest.pk:
            return Response(
                {'detail': '只能重新生成最新一版回答', 'code': 'not_latest'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if (latest.meta or {}).get('superseded'):
            return Response(
                {'detail': '该回答已是上一版', 'code': 'already_superseded'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 重新生成的「问题」= 这版答案之前最近的一次用户提问（含当时的附件上下文）
        user_msg = conversation.messages.filter(
            role='user', created_at__lte=latest.created_at,
        ).order_by('-created_at', '-id').first()
        if user_msg is None:
            return Response(
                {'detail': '找不到对应的用户提问，无法重新生成', 'code': 'question_not_found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        prompt_message = self._inject_attachments(user_msg.content, user_msg.attachments or [])
        kb_ids = list(conversation.knowledge_base_ids or [])
        # 上下文不含正在重生的这一版，也不含更早的上一版
        history = self._build_history(conversation, exclude_ids={latest.pk})
        # E3：重生同样按会话所钉版本的检索策略执行
        sources, retrieval_question = self._plan_retrieval(
            conversation, request.user, user_msg.content, history)
        prompt_message = self._append_retrieval_hint(
            prompt_message, user_msg.content, retrieval_question)

        response = StreamingHttpResponse(
            self._stream_response(
                conversation=conversation,
                user_message=user_msg.content,
                prompt_message=prompt_message,
                history=history,
                attachments=user_msg.attachments or [],
                user_id=request.user.id,
                sources=sources,
                knowledge_base_ids=kb_ids,
                user=request.user,
                user_msg=user_msg,
                supersede=latest,
            ),
            content_type='text/event-stream',
        )
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        return response

    @action(detail=False, methods=['post'], url_path='permission')
    def permission(self, request):
        """D7：会话内授权卡片的决定入口（只接受请求标识与决定，不接受工具参数）"""
        request_id = str(request.data.get('request_id') or '').strip()
        decision = request.data.get('decision')
        if not request_id or decision not in ('allow_once', 'allow_session', 'deny'):
            return Response(
                {'detail': '授权请求参数不合法', 'code': 'invalid_permission_request'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        result = resolve_permission(request_id, request.user.id, decision)
        if result == 'ok':
            return Response({'code': 'ok'})
        if result == 'restarted':
            return Response(
                {'detail': '平台服务已重启，该授权请求已失效，请重新发起本轮对话。',
                 'code': 'permission_restarted'},
                status=status.HTTP_410_GONE,
            )
        if result == 'expired':
            return Response(
                {'detail': '授权请求已超时，已按拒绝处理', 'code': 'permission_expired'},
                status=status.HTTP_410_GONE,
            )
        if result == 'already_resolved':
            return Response(
                {'detail': '授权请求已处理', 'code': 'permission_already_resolved'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(
            {'detail': '授权请求不存在或已失效', 'code': 'permission_not_found'},
            status=status.HTTP_404_NOT_FOUND,
        )

    def _remember_tool_grant(self, conversation_id: int, tool_name: str) -> None:
        """D7：把「本会话始终允许」写入会话；并发写入用行锁串行化"""
        with transaction.atomic():
            conversation = Conversation.objects.select_for_update().get(pk=conversation_id)
            grants = list(conversation.tool_grants or [])
            if tool_name not in grants:
                grants.append(tool_name)
                conversation.tool_grants = grants[:200]
                conversation.save(update_fields=['tool_grants'])

    def _resolve_agent(self, user, agent_id):
        """D1：把请求里的 agent_id 换成可见的智能体实例

        返回 (agent, None) 或 (None, 错误响应)。不可见一律显式报错，
        不允许静默退回通用助手——否则用户会以为在用智能体（D6 同理）。
        创建者/管理员可见草稿，因此草稿可直接在对话里试用。
        """
        if agent_id is None:
            return None, None
        agent = AgentProfile.objects.filter(pk=agent_id).first()
        if agent is None:
            return None, Response(
                {'detail': '智能体不存在或已被删除', 'code': 'agent_not_found'},
                status=status.HTTP_404_NOT_FOUND,
            )
        if agent.visibility == Visibility.ADMIN_ONLY and not (
                user.is_staff or user.is_superuser):
            return None, Response(
                {'detail': f'智能体「{agent.name}」是仅管理端智能体，不面向业务用户开放',
                 'code': 'agent_admin_only'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not can_see(user, agent):
            return None, Response(
                {'detail': f'智能体「{agent.name}」不在你的可见范围内，'
                          f'请让管理员调整可见范围或改用其他智能体',
                 'code': 'agent_forbidden'},
                status=status.HTTP_403_FORBIDDEN,
            )
        return agent, None

    @staticmethod
    def _factory_default_agent(conv_type):
        """D1：未选智能体的新会话回落出厂「通用问答」

        内置件没安装（未跑 init_builtin_agents）或已被停用/归档时返回 None，
        沿用平台通用问答路径，不阻断对话。
        """
        if conv_type != 'chat':
            return None
        return (
            AgentProfile.objects
            .filter(is_builtin=True, name='通用问答', status=AgentStatus.PUBLISHED)
            .first()
        )

    @staticmethod
    def _check_agent_binding(user, conversation_id, agent):
        """D1 会话口径闸门：一条会话从头到尾只对应一个智能体，下线的只读

        返回 None 放行，否则返回可直接交给客户端的错误响应。
        - 会话已绑定后又传别的 agent_id：换助手要新建会话；
        - 绑定的智能体已停用/归档：历史照常可读，但不能继续追问。
        """
        if not conversation_id:
            return None
        conv = Conversation.objects.filter(pk=conversation_id, user=user).first()
        if conv is None:
            return None
        # P1-c：已被切换承接的旧会话保持只读，历史照常可读但不能继续追问
        if Conversation.objects.filter(switched_from_id=conv.pk).exists():
            return Response(
                {'detail': '该会话已切换为新会话承接，历史只读，不能继续追问。',
                 'code': 'conversation_readonly'},
                status=status.HTTP_409_CONFLICT,
            )
        if not conv.agent_id:
            return None
        if conv.agent.status in (AgentStatus.DISABLED, AgentStatus.ARCHIVED):
            return Response(
                {'detail': '该助手已下线，历史会话只读，不能继续追问。', 'code': 'agent_offline'},
                status=status.HTTP_409_CONFLICT,
            )
        if conv.agent.visibility == Visibility.ADMIN_ONLY and not (
                user.is_staff or user.is_superuser):
            return Response(
                {'detail': '该助手已调整为仅管理端，业务用户不可继续追问，历史会话仍可查看。',
                 'code': 'agent_admin_only'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if agent is not None and agent.pk != conv.agent_id:
            return Response(
                {'detail': '一条会话只对应一个助手，新建会话才会应用新能力配置。', 'code': 'agent_locked'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return None

    def _get_or_create_conversation(
        self, user, conv_id, conv_type, resource_id, first_message,
        knowledge_base_ids=None, agent=None,
    ):
        """获取或创建会话"""
        if conv_id:
            from django.shortcuts import get_object_or_404
            conv = get_object_or_404(Conversation, id=conv_id, user=user)
            # 本次请求显式改了勾选的知识库 → 持久化到会话，后续消息沿用
            if knowledge_base_ids is not None and \
                    list(conv.knowledge_base_ids or []) != knowledge_base_ids:
                conv.knowledge_base_ids = knowledge_base_ids
                conv.save(update_fields=['knowledge_base_ids'])
            # 7.3：历史会话没有开场版本号时，用当前线上版本补钉一次，
            # 之后的重发布/回滚不再影响它
            if conv.agent_id and conv.agent_version is None and conv.agent.current_version:
                conv.agent_version = conv.agent.current_version
                conv.save(update_fields=['agent_version'])
            # D1：会话中途不换智能体（换绑由 _check_agent_binding 拦下），
            # 这里只沿用已绑定的那个
            return conv

        with transaction.atomic():
            conv_agent = agent or self._factory_default_agent(conv_type)
            conv = Conversation.objects.create(
                user=user,
                title=first_message[:30] or '新对话',
                type=conv_type,
                resource_id=resource_id,
                knowledge_base_ids=knowledge_base_ids or [],
                agent=conv_agent,
                agent_version=(conv_agent.current_version or None) if conv_agent else None,
            )
        return conv

    def _filter_visible_kb_ids(self, user, kb_ids):
        """按可见性过滤请求中的知识库 ID（去重保序）

        None 表示请求未涉及知识库选择，原样返回，由调用方沿用会话已有值。
        """
        if kb_ids is None or not kb_ids:
            return kb_ids

        from backend.apps.knowledge.models import KnowledgeBase, visible_kb_query

        queryset = (
            KnowledgeBase.objects.all()
            if user.is_staff
            else KnowledgeBase.objects.filter(visible_kb_query(user))
        )
        allowed = set(queryset.filter(id__in=set(kb_ids)).values_list('id', flat=True))
        result = []
        for kb_id in kb_ids:
            if kb_id in allowed and kb_id not in result:
                result.append(kb_id)
        return result

    @staticmethod
    def _source_dict(hit):
        """把一次检索命中压成前端引用来源结构（按需查与每轮必查共用）"""
        return {
            'knowledge_base_id': hit['kb_id'],
            'knowledge_base_name': hit['kb_name'],
            'title': hit['doc_title'],
            'file_name': hit['file_name'],
            'score': round(hit['score'], 2),
            'chunk_id': hit['chunk_id'],
            'doc_id': hit['doc_id'],
            'chunk_index': hit['chunk_index'],
            'content': hit['content'],
            # 页码 / OCR 标记（可能为空 dict）：前端据此标「可能有误差」
            'meta': hit.get('meta') or {},
        }

    def _retrieve_sources(self, kb_ids, question):
        """每轮必查 / 历史会话：先跨库检索，返回供前端展示的引用来源（失败不阻断对话）"""
        if not kb_ids:
            return []
        try:
            return [self._source_dict(hit)
                    for hit in multi_kb_search(kb_ids, question, top_k=3)]
        except Exception:
            return []

    @staticmethod
    def _merge_sources(base, extra):
        """合并按需查命中的来源，按 (知识库, 片段) 去重，最多保留 8 条"""
        merged, seen = [], set()
        for item in list(base or []) + list(extra or []):
            key = (item.get('knowledge_base_id'), item.get('chunk_id'))
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
            if len(merged) >= 8:
                break
        return merged

    def _knowledge_policy(self, conversation):
        """E3：取会话所钉版本的检索调用策略，返回 (knowledge_mode, followup_rewrite)

        未绑定智能体的历史会话沿用原行为：每轮都先检索一次（always）。
        """
        if not conversation.agent_id:
            return 'always', False
        from backend.apps.agenthub.models import AgentVersion
        versions = AgentVersion.objects.filter(agent=conversation.agent)
        version = None
        if conversation.agent_version:
            version = versions.filter(version=conversation.agent_version).first()
        else:
            version = versions.order_by('-version').first()
        try:
            cfg = runtime_config(conversation.agent, version)
        except Exception:
            return 'always', False
        return (cfg.get('knowledge_mode') or 'on_demand',
                bool(cfg.get('followup_rewrite', True)))

    def _effective_kb_ids(self, conversation, user):
        """本轮实际检索的知识库：优先会话勾选，否则用智能体装配的库（E1/E2）"""
        selected = list(conversation.knowledge_base_ids or [])
        if selected or not conversation.agent_id:
            return selected
        return self._filter_visible_kb_ids(
            user, list(conversation.agent.knowledge_base_ids or [])) or []

    def _retrieval_question(self, question, history, *, policy, kb_ids):
        """E3：按追问补全开关改写检索问题（失败回退原问题）"""
        mode, rewrite = policy
        if not kb_ids or not rewrite or not needs_rewrite(question, history):
            return question
        try:
            return standalone_question(question, history) or question
        except Exception:
            return question

    def _plan_retrieval(self, conversation, user, question, history):
        """E3 检索计划：返回 (sources, 用于提示词的检索问题)

        按需查（默认）不预先检索，引用来源改由模型真实调用检索工具时收集；
        每轮必查先检索再作答。E2 的可见性过滤已在取库 ID 时生效。
        """
        policy = self._knowledge_policy(conversation)
        kb_ids = self._effective_kb_ids(conversation, user)
        retrieval_question = self._retrieval_question(
            question, history, policy=policy, kb_ids=kb_ids)
        sources = []
        if kb_ids and policy[0] == 'always':
            sources = self._retrieve_sources(kb_ids, retrieval_question)
        return sources, retrieval_question

    @staticmethod
    def _append_retrieval_hint(prompt_message, question, retrieval_question):
        """追问补全只在检索时生效：把补全后的问题作为本轮检索提示附在提示词里"""
        if retrieval_question == question:
            return prompt_message
        return (f'{prompt_message}\n\n（检索提示：如需检索知识库，'
                f'请以完整表述「{retrieval_question}」调用检索工具）')

    def _build_history(self, conversation, exclude_ids=None):
        """取最近 20 条历史消息作为上下文

        D2：「上一版」答案（meta.superseded）只留档对比，不进上下文，
        否则模型会看到同一个问题的两个回答；exclude_ids 用于正在重生、
        还没落上标记的那一版。多取一些再过滤，保证剔除后仍凑够 20 条。
        """
        exclude_ids = exclude_ids or set()
        rows = list(conversation.messages.all().order_by('-created_at', '-id')[:60])
        rows = [
            m for m in rows
            if m.pk not in exclude_ids and not (m.meta or {}).get('superseded')
        ][:20]
        rows.reverse()
        return [{'role': m.role, 'content': m.content} for m in rows]

    _GENERATED_FILE_RE = re.compile(r'\[GENERATED_FILE:(\d+)\]')

    def _link_generated_files(self, answer_text: str, assistant_msg: Message) -> list:
        """把本次请求生成的文件关联到 Assistant 消息

        优先解析 AI 回复中显式保留的 [GENERATED_FILE:id] 标记；
        若模型未保留标记（常见），则兜底查询当前会话最近 2 分钟内
        未关联消息的文件，避免文件"丢失"。
        """
        conversation = assistant_msg.conversation
        file_ids = {int(m) for m in self._GENERATED_FILE_RE.findall(answer_text or '')}

        # 兜底：同一会话最近 2 分钟内未关联消息的文件
        if conversation is not None:
            cutoff = timezone.now() - timedelta(minutes=2)
            recent_files = ChatGeneratedFile.objects.filter(
                conversation=conversation,
                message__isnull=True,
                created_at__gte=cutoff,
            )
            for f in recent_files:
                file_ids.add(f.id)

        if not file_ids:
            return []

        files = ChatGeneratedFile.objects.filter(id__in=file_ids, message__isnull=True)
        result = []
        for f in files:
            f.message = assistant_msg
            f.conversation = conversation
            f.save(update_fields=['message', 'conversation'])
            result.append({
                'id': f.id,
                'name': f.file_name,
                'type': f.file_type,
                'size': f.file_size,
                'url': f'/chat/files/{f.id}/download/',
                'is_expired': f.is_expired,
            })

        # 把文件信息写入消息 meta，便于前端直接渲染
        if result:
            assistant_msg.meta = {**(assistant_msg.meta or {}), 'generated_files': result}
            assistant_msg.save(update_fields=['meta'])

        return result

    @staticmethod
    def _degrade_text(note):
        """降级说明压平为单行文案：「对象名：原因」"""
        if isinstance(note, dict):
            name = note.get('name') or ''
            reason = note.get('reason') or ''
            if name and reason:
                return f'{name}：{reason}'
            return name or reason or str(note)
        return str(note)

    @staticmethod
    def _build_meta(sources=None, degrade_notes=None, preferences=0):
        """消息 meta：引用来源 + D6 降级说明 + D8 带入偏好条数（历史回看可见）"""
        meta = {}
        if sources:
            meta['sources'] = sources
        if degrade_notes:
            meta['degrade'] = [ChatViewSet._degrade_text(n) for n in degrade_notes]
        if preferences:
            meta['preferences'] = preferences
        return meta

    @staticmethod
    def _preferences_for(user, conversation):
        """D8：本会话要带入的偏好文本与行 id（无智能体或本会话关闭时为空）"""
        if not conversation.agent_id or not conversation.use_preferences:
            return [], []
        return active_preference_texts(user, conversation.agent)

    def _make_tool_auditor(self, user, conversation):
        """D2/F3：工具调用一结束就落审计，与过程区同源

        在桥接层的后台线程里执行，因此不能依赖外层请求线程的收尾；
        客户端中途断开也不会丢留痕。

        成功走 tool_call、失败/被打断/被权限档拒绝走 tool_failed，
        F4 的"失败/被拒次数"直接按 action 计数，不去解析 detail；
        出网归属由装配期给定的 egress_tools 判定（egress 字段），与内网互斥。
        """
        agent_name = conversation.agent.name if conversation.agent_id else ''
        failed_states = {'error', 'interrupted', 'denied'}

        def audit_tool(payload, full_result=''):
            state = payload.get('state') or ''
            egress = bool(payload.get('egress'))
            detail = {
                'conversation_id': conversation.id,
                'agent': agent_name,
                'call_id': payload.get('call_id', ''),
                'tool': payload.get('name', ''),
                'args': payload.get('args', ''),
                'state': state,
                'elapsed': payload.get('elapsed'),
                'result': payload.get('result', ''),
                'egress': egress,
            }
            _record_tool_call(
                actor=user,
                action='tool_failed' if state in failed_states else 'tool_call',
                target=payload.get('display') or payload.get('name') or '工具调用',
                detail=json.dumps(detail, ensure_ascii=False),
                egress=egress,
                conversation=conversation,
                payload=payload,
                full_result=full_result,
                agent_name=agent_name,
            )

        return audit_tool

    def _stream_response(self, conversation, user_message, prompt_message,
                         history, temperature=None, attachments=None, user_id=None,
                         sources=None, knowledge_base_ids=None, user=None,
                         user_msg=None, supersede=None):
        """
        SSE 流式响应生成器：AgentScope 事件 → SSE 事件

        转发的 SSE 事件名与桥接层 type 一致：start / degrade / thinking /
        chunk / tool / tool_args / tool_result / end / error（D2、D6）。

        user_msg 已给（重新生成）时不再新建用户消息；supersede 给了就把旧答案
        转成「上一版」留档。两者都不给就是普通的一问一答。
        """
        attachments = attachments or []
        sources = sources or []
        knowledge_base_ids = knowledge_base_ids or []
        user = user or conversation.user
        # D8：带入用户确认的跨会话偏好；本会话关闭或未绑定智能体时为空
        preference_texts, preference_ids = self._preferences_for(user, conversation)
        preference_prompt = preference_block(preference_texts)
        mark_preferences_used(preference_ids)
        # 重新生成只多一版回答，不多一次提问
        delta = 1 if user_msg is not None else 2

        # 先检查服务是否可用
        health = check_health()
        if health['status'] != 'ok':
            yield format_sse_event('error', health.get('error', '大模型服务不可用'))
            yield format_sse_event('end', '')
            return

        # 保存用户消息（原始消息落库，附件随消息保存）
        if user_msg is None:
            user_msg = Message.objects.create(
                conversation=conversation,
                role='user',
                content=user_message,
                attachments=attachments,
            )

        permission_session = None
        if conversation.agent_id:
            permission_session = PermissionSession(
                user_id=user.id,
                conversation_id=conversation.id,
                agent_id=conversation.agent_id,
                message_id=user_msg.id,
                on_grant=lambda tool: self._remember_tool_grant(conversation.id, tool),
            )

        yield format_sse_event('start', {
            'conversation_id': conversation.id,
            'message_id': user_msg.id,
            'sources': sources,
            'preferences': len(preference_texts),
        })

        answer_text = ''
        degrade_notes = []
        permission_events = []
        permission_requested = False
        saved = False
        collected_sources = []
        # F4 真实 token 计量：累计本轮所有模型调用，只落助手消息
        usage = {
            'input_tokens': 0,
            'output_tokens': 0,
            'cache_input_tokens': 0,
            'cache_creation_input_tokens': 0,
            'model_calls': 0,
        }

        def _collect_sources(hits):
            collected_sources.extend(self._source_dict(hit) for hit in hits)

        try:
            # D1：绑定智能体的会话按定义装配运行，未绑定的沿用平台通用助手
            if conversation.agent_id:
                events = run_profile_stream(
                    conversation.agent_id, history, prompt_message,
                    user=user,
                    user_id=user_id,
                    conversation_id=conversation.id,
                    on_tool=self._make_tool_auditor(user, conversation),
                    agent_version=conversation.agent_version,
                    permission_session=permission_session,
                    session_grants=list(conversation.tool_grants or []),
                    on_knowledge_sources=_collect_sources,
                    preference_prompt=preference_prompt,
                )
            else:
                agent = create_agent_blocking(
                    agent_type=conversation.type,
                    resource_id=conversation.resource_id,
                    stream=True,
                    temperature=temperature,
                    user_id=user_id,
                    conversation_id=conversation.id,
                    knowledge_base_ids=knowledge_base_ids,
                )
                events = run_agent_stream(agent, history, prompt_message)

            for ev in events:
                etype = ev.get('type')
                if etype == 'chunk':
                    answer_text += ev['content']
                    yield format_sse_event('chunk', {'content': ev['content']})
                elif etype == 'error':
                    yield format_sse_event('error', ev.get('message', ''))
                    yield format_sse_event('end', '')
                    return
                elif etype == 'degrade':
                    # D6：降级条先于正文下发，并随消息留档；A3 触顶会在正文后
                    # 补发一条，这里累加而非替换，两类降级条都要留档。
                    # 前端契约是 string[]，桥接层给的是结构化对象，这里统一压平
                    new_notes = ev.get('notes') or []
                    degrade_notes.extend(new_notes)
                    ev = {'type': 'degrade',
                          'notes': [self._degrade_text(n) for n in new_notes]}
                    yield format_sse_event('degrade', ev)
                elif etype == 'keepalive':
                    # D7：授权等待期间的 SSE 注释，前端解析器天然忽略
                    yield ': keep-alive\n\n'
                elif etype == 'usage':
                    # F4：模型调用用量只做累计，不下发独立事件（由 end 事件带出）
                    for key in usage:
                        usage[key] += int(ev.get(key) or 0)
                elif etype == 'permission_request':
                    permission_requested = True
                    yield format_sse_event(etype, ev)
                elif etype == 'permission_result':
                    permission_events.append({
                        'tool': ev.get('display') or ev.get('tool_name') or '',
                        'decision': ev.get('decision'),
                    })
                    yield format_sse_event(etype, ev)
                else:
                    yield format_sse_event(etype, ev)

            # 保存 AI 回答
            sources = self._merge_sources(sources, collected_sources)
            meta = self._build_meta(sources, degrade_notes, len(preference_texts))
            if usage['model_calls']:
                meta['usage'] = dict(usage)
            if permission_events:
                meta['permission'] = permission_events
            assistant_msg = Message.objects.create(
                conversation=conversation,
                role='assistant',
                content=answer_text,
                meta=meta,
                input_tokens=usage['input_tokens'],
                output_tokens=usage['output_tokens'],
            )
            self._mark_superseded(supersede, assistant_msg)
            generated_files = self._link_generated_files(answer_text, assistant_msg)
            conversation.message_count += delta
            if conversation.message_count == 2:
                conversation.title = user_message[:30]
            # D7：并发下授权请求可能已写入 tool_grants，这里只回写本轮字段，
            # 避免用请求开始时的旧快照覆盖新授权
            conversation.save(update_fields=[
                'message_count', 'title', 'last_message_at', 'updated_at'])
            saved = True

            yield format_sse_event('end', {
                'message_count': conversation.message_count,
                'generated_files': generated_files,
                'sources': sources,
                'usage': usage,
            })

        except Exception as e:
            yield format_sse_event('error', str(e))
            yield format_sse_event('end', '')
        finally:
            if not saved and (answer_text or permission_requested):
                # 客户端「停止生成」/关闭页面中断了流：把已生成的部分落库，
                # 刷新后问答不缺一问（meta.stopped=True 标记为中断产物）
                # D7：授权等待中断且尚无正文时也落一条显式中断记录，避免空白
                try:
                    sources = self._merge_sources(sources, collected_sources)
                    meta = {**self._build_meta(
                        sources, degrade_notes, len(preference_texts)), 'stopped': True}
                    if usage['model_calls']:
                        meta['usage'] = dict(usage)
                    if permission_events:
                        meta['permission'] = permission_events
                    if permission_requested and not answer_text:
                        meta['permission_interrupted'] = True
                    assistant_msg = Message.objects.create(
                        conversation=conversation,
                        role='assistant',
                        content=answer_text,
                        meta=meta,
                        input_tokens=usage['input_tokens'],
                        output_tokens=usage['output_tokens'],
                    )
                    self._mark_superseded(supersede, assistant_msg)
                    self._link_generated_files(answer_text, assistant_msg)
                    conversation.message_count += delta
                    if conversation.message_count == 2:
                        conversation.title = user_message[:30]
                    conversation.save(update_fields=[
                        'message_count', 'title', 'last_message_at', 'updated_at'])
                except Exception:
                    pass

    @staticmethod
    def _mark_superseded(old, new):
        """D2：旧答案转「上一版」留档，新答案记录它替代了哪一版

        只在新生成的回答确实落库后调用：中途彻底失败（一个字都没出）时旧版
        仍是当前答案，不能被标记成上一版。
        """
        if old is None or new is None:
            return
        old.meta = {**(old.meta or {}), 'superseded': True, 'superseded_by': new.id}
        old.save(update_fields=['meta'])
        new.meta = {**(new.meta or {}), 'regenerates': old.id}
        new.save(update_fields=['meta'])


class ModelViewSet(viewsets.ViewSet):
    """模型管理接口"""

    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['get'])
    def health(self, request):
        """检查模型服务健康状态"""
        health = check_health()
        return Response(health)

    @action(detail=False, methods=['get'])
    def config(self, request):
        """获取当前模型配置（不含敏感信息）

        与 check_health 一致只读数据库（SystemSetting）；未配置时如实返回空值，
        不回落到环境变量
        """
        base_url = get_llm_config('llm_base_url', '') or ''
        # 隐藏 host 部分信息，只显示协议和端口
        if base_url:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(base_url)
                display_url = f'{parsed.scheme}://***:{parsed.port or "default"}'
                host = parsed.hostname or ''
            except Exception:
                display_url = base_url[:10] + '***'
                host = ''
        else:
            display_url = '未配置'
            host = ''

        # 供前端如实展示部署形态：云端 API 时不能标「本地部署」
        is_local = (
            host in ('127.0.0.1', 'localhost', '::1')
            or host.startswith('192.168.')
            or host.startswith('10.')
            or any(host.startswith(f'172.{i}.') for i in range(16, 32))
        )

        return Response({
            'base_url_display': display_url,
            'model': get_llm_config('llm_model', '') or '',
            'temperature': float(get_llm_config('llm_temperature', 0.7) or 0.7),
            'max_tokens': int(get_llm_config('llm_max_tokens', 4096) or 4096),
            'is_local': is_local,
        })


_MIME_MAP = {
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'pdf': 'application/pdf',
}


class ChatGeneratedFileDownloadView(APIView):
    """下载对话中 AI 生成的文件"""

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        file_record = get_object_or_404(
            ChatGeneratedFile, pk=pk, owner=request.user
        )
        if file_record.is_expired:
            return Response(
                {'detail': '文件已过期，无法下载'},
                status=status.HTTP_410_GONE,
            )
        if not file_record.file:
            return Response(
                {'detail': '文件不存在'},
                status=status.HTTP_404_NOT_FOUND,
            )

        response = HttpResponse(
            file_record.file.read(),
            content_type=_MIME_MAP.get(file_record.file_type, 'application/octet-stream'),
        )
        response['Content-Disposition'] = (
            f"attachment; filename=\"{file_record.file_name}\"; "
            f"filename*=UTF-8''{escape_uri_path(file_record.file_name)}"
        )
        return response
