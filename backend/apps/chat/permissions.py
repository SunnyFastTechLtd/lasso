"""D7 会话内逐次授权：待决请求落库 + 轮询裁决

授权等待与 SSE 流仍在同一个进程/事件循环内，但待决状态不再保存在进程内
Future 里：`open` 写入数据库，`wait` 按固定间隔轮询，决定接口只需改一行。
这样决定可以从任意线程/进程提交；进程重启后 instance_id 失配，残留的
待决请求一律失效，前端提示重新发起。
"""
import asyncio
import uuid
from datetime import timedelta
from typing import Any, Callable, Dict, Optional

from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone

from backend.apps.chat.models import PendingPermission

ASK_TIMEOUT_SECONDS = 60

# 轮询间隔：权衡决定延迟与数据库压力（授权卡片本身有 60 秒窗口）
POLL_INTERVAL_SECONDS = 0.2

# 本进程标识：服务重启即变化，用于让重启前的待决请求失效
INSTANCE_ID = uuid.uuid4().hex

# 已结束记录保留一天，供排查；更早的在下一次 open 时顺手清理
RETENTION = timedelta(days=1)


def _create(request_id: str, fields: Dict[str, Any], expires_at) -> None:
    PendingPermission.objects.filter(
        created_at__lt=timezone.now() - RETENTION).delete()
    PendingPermission.objects.create(
        request_id=request_id,
        expires_at=expires_at,
        instance_id=INSTANCE_ID,
        **fields,
    )


def _snapshot(request_id: str) -> Optional[Dict[str, Any]]:
    return PendingPermission.objects.filter(request_id=request_id).values(
        'status', 'decision', 'expires_at', 'instance_id').first()


def _expire(request_id: str) -> None:
    PendingPermission.objects.filter(
        request_id=request_id, status=PendingPermission.STATUS_PENDING,
    ).update(status=PendingPermission.STATUS_EXPIRED, resolved_at=timezone.now())


def _delete(request_id: str) -> None:
    PendingPermission.objects.filter(request_id=request_id).delete()


class PermissionSession:
    """一次流式回答内的授权会话；open → wait → close 与桥接层配对使用"""

    def __init__(
        self,
        *,
        user_id: int,
        conversation_id: Optional[int] = None,
        agent_id: Optional[int] = None,
        message_id: Optional[int] = None,
        timeout: int = ASK_TIMEOUT_SECONDS,
        on_grant: Optional[Callable[[str], None]] = None,
    ):
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.agent_id = agent_id
        self.message_id = message_id
        self.timeout = timeout
        self._on_grant = on_grant

    async def open(self, payload: Dict[str, Any]) -> str:
        request_id = uuid.uuid4().hex
        fields = {
            'user_id': self.user_id,
            'conversation_id': self.conversation_id,
            'agent_id': self.agent_id,
            'message_id': self.message_id,
            'call_id': str(payload.get('call_id') or '')[:64],
            'tool_name': str(payload.get('tool_name') or '')[:160],
            'display': str(payload.get('display') or '')[:200],
        }
        expires_at = timezone.now() + timedelta(seconds=self.timeout)
        await sync_to_async(_create, thread_sensitive=True)(
            request_id, fields, expires_at)
        return request_id

    async def wait(self, request_id: str) -> str:
        """轮询等待决定；超时按拒绝收口，重启前遗留的请求返回 restarted"""
        while True:
            snapshot = await sync_to_async(_snapshot, thread_sensitive=False)(
                request_id)
            if snapshot is None:
                return 'deny'
            if snapshot['instance_id'] != INSTANCE_ID:
                return 'restarted'
            if snapshot['status'] != PendingPermission.STATUS_PENDING:
                return snapshot['decision'] or 'deny'
            remaining = (snapshot['expires_at'] - timezone.now()).total_seconds()
            if remaining <= 0:
                await sync_to_async(_expire, thread_sensitive=False)(request_id)
                return 'timeout'
            await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))

    async def remember(self, tool_name: str) -> None:
        if self._on_grant is None:
            return
        await sync_to_async(self._on_grant, thread_sensitive=True)(tool_name)

    async def close(self, request_id: str) -> None:
        await sync_to_async(_delete, thread_sensitive=False)(request_id)


def resolve_permission(request_id: str, user_id: int, decision: str) -> str:
    """由决定接口（同步线程）调用；返回 ok / not_found / already_resolved / expired / restarted"""
    now = timezone.now()
    with transaction.atomic():
        record = PendingPermission.objects.select_for_update().filter(
            request_id=request_id).first()
        if record is None or record.user_id != user_id:
            # 不向他人泄漏请求是否存在
            return 'not_found'
        if record.instance_id != INSTANCE_ID:
            return 'restarted'
        if record.status == PendingPermission.STATUS_EXPIRED or now >= record.expires_at:
            if record.status == PendingPermission.STATUS_PENDING:
                record.status = PendingPermission.STATUS_EXPIRED
                record.resolved_at = now
                record.save(update_fields=['status', 'resolved_at'])
            return 'expired'
        if record.status != PendingPermission.STATUS_PENDING:
            return 'already_resolved'
        record.status = PendingPermission.STATUS_RESOLVED
        record.decision = decision
        record.resolved_at = now
        record.save(update_fields=['status', 'decision', 'resolved_at'])
    return 'ok'
