"""
异步事件桥接层

AgentScope 的 Agent 运行在 asyncio 事件循环中（reply_stream 是
异步生成器），而 Django 的 StreamingHttpResponse 需要同步生成器。
桥接层在后台守护线程中启动独立事件循环消费 AgentScope 事件，
通过线程安全队列转发给同步迭代器，供 SSE 输出使用。

产出的事件字典格式：
- {'type': 'chunk', 'content': str}   文本分片（打字机输出）
- {'type': 'thinking', 'content': str} 思考过程分片（推理模型）
- {'type': 'tool', 'call_id': str, 'name': str, 'display': str, 'args': str}
                                      工具调用开始（D2：来源名·工具名口径）
- {'type': 'tool_result', 'call_id': str, 'name': str, 'display': str,
   'state': str, 'result': str, 'elapsed': float, 'egress': bool}
                                      工具调用结束（返回摘要 + 耗时；F4 出网归属）
- {'type': 'degrade', 'notes': list}  D6 降级条（装配阶段产出，先于正文下发；
                                      A3 触顶时在正文收尾后补发一条「已达本次查询次数上限」）
- {'type': 'error', 'message': str}   错误（已转为用户友好文案）

每个请求使用独立的后台线程 + 事件循环：本系统是离线一体机
部署，并发量小，独立循环可以彻底隔离请求间状态，实现最简单
也最可靠。

按智能体装配的会话（D1）必须"装配—执行—收尾"落在同一个事件循环里：
Toolkit 只校验有状态 MCP 客户端已连接、不负责连接，循环一关连接即失效，
所以这里不复用 create_agent_blocking，而是由 run_profile_stream 在协程内
完成装配、消费事件并在 finally 关闭客户端。
"""
import asyncio
import queue
import threading
import time
from typing import Any, AsyncGenerator, Callable, Dict, Generator, Iterable, List, Optional, Tuple

from asgiref.sync import sync_to_async
from agentscope.agent import Agent
from agentscope.event import (
    ConfirmResult,
    ModelCallEndEvent,
    ReplyEndEvent,
    RequireUserConfirmEvent,
    UserConfirmResultEvent,
    TextBlockDeltaEvent,
    ThinkingBlockDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.message import AssistantMsg, Msg, UserMsg
from agentscope.types import ReplyFinishedReason

# 过程区与审计共用：参数/返回摘要的截断长度（D2 只要摘要，不铺全文）
ARGS_SUMMARY_LEN = 300
RESULT_SUMMARY_LEN = 500

# A3：查询次数上限到顶的显式提示口径（PRD 7.4：过程区标记 + 正文如实说明）
QUERY_LIMIT_TEXT = '已达本次查询次数上限'


def _query_limit_payloads(tracker: '_ToolTracker') -> List[Dict[str, Any]]:
    """触顶收尾：正文追加如实说明 + 补发一条降级条（禁止静默结束）"""
    sources = list(tracker.sources)
    if sources:
        tried = f'；已试过的来源：{"、".join(sources)}'
    else:
        tried = '；本轮未能完成任何外部查询'
    return [
        {'type': 'chunk',
         'content': f'\n\n（{QUERY_LIMIT_TEXT}，本轮停止继续调用，'
                    f'没能查到完整结果{tried}）'},
        {'type': 'degrade',
         'notes': [{'name': '本轮检索',
                    'reason': f'{QUERY_LIMIT_TEXT}，本轮停止继续调用，未能查到完整结果'}]},
    ]


def _summary(text: Any, limit: int) -> str:
    """压成单行并截断，供过程区展示与审计留痕"""
    if text is None:
        return ''
    if not isinstance(text, str):
        try:
            import json

            text = json.dumps(text, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(text)
    flat = ' '.join(text.split())
    return flat if len(flat) <= limit else f'{flat[:limit]}…'


class _ToolTracker:
    """把 AgentScope 的工具事件聚合为过程区可读的单条记录（F3 同源留痕）"""

    def __init__(self, displays: Optional[Dict[str, str]] = None,
                 egress_tools: Optional[Iterable[str]] = None,
                 egress_hosts: Optional[Dict[str, str]] = None):
        self.displays = displays or {}
        # F4：走外网出口的工具名（装配期确定，运行期只做集合判定，无额外查询）
        self.egress_tools = set(egress_tools or ())
        # B8-b1：出网工具 → 目标域名，过程区据此说明"参数摘要发给了谁"
        self.egress_hosts = dict(egress_hosts or {})
        self._args: Dict[str, List[str]] = {}
        self._results: Dict[str, List[str]] = {}
        self._names: Dict[str, str] = {}
        self._clock: Dict[str, float] = {}
        # PRD 9.2 #4：工具返回的原始全文旁路缓存，仅供留痕回调取用；
        # 不进 SSE payload（过程区仍是 500 字摘要），由 pop_full 取走后释放
        self._full: Dict[str, str] = {}
        # A3 触顶说明用：本轮已试过的来源（display 口径、去重、保序）
        self.sources: List[str] = []
        # F4 真实 token 计量：累计本轮所有模型调用的 usage
        self.usage = {
            'input_tokens': 0,
            'output_tokens': 0,
            'cache_input_tokens': 0,
            'cache_creation_input_tokens': 0,
            'model_calls': 0,
        }

    def display_of(self, name: str) -> str:
        return self.displays.get(name) or name.replace('__', '·', 1)

    def pop_full(self, call_id: str) -> str:
        """取走某次调用的原始返回全文（未压平、未截断），取后即释放"""
        return self._full.pop(call_id, '')

    def on(self, ev: Any) -> List[Dict[str, Any]]:
        """返回该事件对应需要下发的字典（多数事件无输出）"""
        if isinstance(ev, ModelCallEndEvent):
            # F4：模型服务返回的真实 usage；流式由 AgentScope 以
            # stream_options.include_usage 汇总后随 ModelCallEndEvent 给出
            payload = {
                'type': 'usage',
                'input_tokens': int(ev.input_tokens or 0),
                'output_tokens': int(ev.output_tokens or 0),
                'cache_input_tokens': int(ev.cache_input_tokens or 0),
                'cache_creation_input_tokens':
                    int(ev.cache_creation_input_tokens or 0),
                'model_calls': 1,
            }
            for key in self.usage:
                self.usage[key] += payload[key]
            return [payload]

        if isinstance(ev, ToolCallStartEvent):
            call_id = ev.tool_call_id
            self._names[call_id] = ev.tool_call_name
            self._args[call_id] = []
            self._clock[call_id] = time.monotonic()
            display = self.display_of(ev.tool_call_name)
            if display not in self.sources:
                self.sources.append(display)
            return [{'type': 'tool', 'call_id': call_id,
                     'name': ev.tool_call_name,
                     'display': display, 'args': '',
                     'egress': ev.tool_call_name in self.egress_tools,
                     'egress_host': self.egress_hosts.get(ev.tool_call_name, '')}]

        if isinstance(ev, ToolCallDeltaEvent):
            self._args.setdefault(ev.tool_call_id, []).append(ev.delta or '')
            return []

        if isinstance(ev, ToolCallEndEvent):
            call_id = ev.tool_call_id
            name = self._names.get(call_id, '')
            args = _summary(''.join(self._args.get(call_id, [])), ARGS_SUMMARY_LEN)
            return [{'type': 'tool_args', 'call_id': call_id, 'name': name,
                     'display': self.display_of(name), 'args': args,
                     'egress': name in self.egress_tools,
                     'egress_host': self.egress_hosts.get(name, '')}]

        if isinstance(ev, ToolResultStartEvent):
            self._names[ev.tool_call_id] = ev.tool_call_name
            self._results[ev.tool_call_id] = []
            self._clock.setdefault(ev.tool_call_id, time.monotonic())
            return []

        if isinstance(ev, ToolResultTextDeltaEvent):
            self._results.setdefault(ev.tool_call_id, []).append(ev.delta or '')
            return []

        if isinstance(ev, ToolResultEndEvent):
            call_id = ev.tool_call_id
            name = self._names.get(call_id, '')
            state = getattr(ev.state, 'value', None) or str(ev.state or '')
            raw_result = ''.join(self._results.get(call_id, []))
            # 全文只放旁路，payload 仍只带 500 字摘要（模型上下文与过程区不变）
            self._full[call_id] = raw_result
            payload = {
                'type': 'tool_result',
                'call_id': call_id,
                'name': name,
                'display': self.display_of(name),
                'state': state,
                'result': _summary(raw_result, RESULT_SUMMARY_LEN),
                'args': _summary(''.join(self._args.get(call_id, [])), ARGS_SUMMARY_LEN),
                'elapsed': round(time.monotonic() - self._clock.get(call_id, time.monotonic()), 2),
                # F4：出网归属（与内网互斥，用量页据此单列计数）
                'egress': name in self.egress_tools,
                'egress_host': self.egress_hosts.get(name, ''),
            }
            self._args.pop(call_id, None)
            self._results.pop(call_id, None)
            return [payload]

        return []


def to_msg(role: str, content: str) -> Msg:
    """把数据库中的消息记录转为 AgentScope 消息"""
    if role == 'user':
        return UserMsg(name='用户', content=content)
    return AssistantMsg(name='助手', content=content)


def build_inputs(
    history: Iterable[Dict[str, str]],
    user_message: str,
) -> List[Msg]:
    """组装完整输入：历史消息 + 本轮用户消息"""
    msgs = [to_msg(m['role'], m['content']) for m in history]
    msgs.append(UserMsg(name='用户', content=user_message))
    return msgs


def _extract_text(msg: Msg) -> str:
    """从 Agent 回复消息中提取纯文本"""
    content = getattr(msg, 'content', None)
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        text = getattr(block, 'text', None)
        if text:
            parts.append(text)
    return ''.join(parts)


def _friendly_error(exc: Exception) -> str:
    """把底层异常转为用户可理解的错误文案"""
    name = type(exc).__name__
    text = str(exc)
    if 'Connection' in name or 'connect' in text.lower():
        return '无法连接大模型服务，请确认模型服务已启动（系统设置中可查看服务地址）'
    if 'Timeout' in name or 'timeout' in text.lower():
        return '大模型响应超时，请稍后重试'
    if 'Authentication' in name or '401' in text:
        return '大模型服务鉴权失败，请检查系统设置中的 API Key'
    return f'大模型调用失败：{text}'


async def _call_tool_hook(on_tool: Callable[..., Any],
                          payload: Dict[str, Any],
                          full_result: str = '') -> None:
    """在协程内安全执行工具收尾回调（F3 审计为同步 ORM 写）

    第二个参数是原始全文，仅供留痕落库；SSE 的 payload 仍只含 500 字摘要。

    Django 禁止在事件循环所在线程直接调用同步 ORM，会抛
    "You cannot call this from an async context"。这里用 sync_to_async
    把同步回调转到工作线程执行；thread_sensitive=True 让同一循环内的
    审计固定落在一根线程上，数据库连接可复用、不随调用次数增长。
    """
    if asyncio.iscoroutinefunction(on_tool):
        await on_tool(payload, full_result)
        return
    result = await sync_to_async(on_tool, thread_sensitive=True)(payload, full_result)
    if asyncio.iscoroutine(result):
        await result


def _run_coro_in_thread(coro_factory) -> 'queue.Queue':
    """
    在后台守护线程中运行协程，返回接收结果的队列

    coro_factory: 接收队列作为唯一参数的协程工厂，
        协程负责向队列 put (kind, payload)，结束时必须 put ('done', None)
    """
    q: 'queue.Queue' = queue.Queue()

    def _thread_main():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(coro_factory(q))
        except Exception as e:  # 协程工厂自身的兜底
            q.put(('error', _friendly_error(e)))
            q.put(('done', None))
        finally:
            # 装配与审计都会在这个线程里用到 Django 连接，线程即弃必须显式回收，
            # 否则每次请求泄漏一条数据库连接
            try:
                from django.db import connections

                connections.close_all()
            except Exception:
                pass
            loop.close()

    threading.Thread(target=_thread_main, daemon=True).start()
    return q


def create_agent_blocking(*args, **kwargs) -> Agent:
    """
    在同步上下文中创建智能体

    registry.create_agent 是协程（Toolkit 注册为异步），
    这里复用桥接层的后台线程事件循环执行，避免污染调用方
    线程的 asyncio 状态，WSGI/ASGI 环境下都安全。
    """
    from .registry import create_agent

    result: List[Agent] = []

    async def _make(q: 'queue.Queue'):
        try:
            result.append(await create_agent(*args, **kwargs))
        finally:
            q.put(('done', None))

    q = _run_coro_in_thread(_make)
    q.get()
    return result[0]


def _permission_payload(
    tool_call: Any, tracker: _ToolTracker, timeout: int
) -> Dict[str, Any]:
    """D7：授权卡片所需的最小字段（工具名、来源名、本次输入摘要）"""
    display = tracker.display_of(tool_call.name)
    if '·' in display:
        source_name, tool_name = display.split('·', 1)
    else:
        source_name, tool_name = '平台内置', display
    return {
        'type': 'permission_request',
        'call_id': tool_call.id,
        'tool_name': tool_name,
        'source_name': source_name,
        'display': display,
        'args': _summary(tool_call.input, ARGS_SUMMARY_LEN),
        'timeout_seconds': timeout,
        'can_always': bool(tool_call.suggested_rules),
    }


def _confirm_result(tool_call: Any, decision: str) -> ConfirmResult:
    confirmed = decision in ('allow_once', 'allow_session')
    return ConfirmResult(
        confirmed=confirmed,
        tool_call=tool_call,
        rules=tool_call.suggested_rules if decision == 'allow_session' else None,
    )


async def _keepalive(emit: Callable[[Dict[str, Any]], Any], interval: float = 5.0) -> None:
    while True:
        await asyncio.sleep(interval)
        await emit({'type': 'keepalive'})


async def _await_decision_with_keepalive(
    session: Any, request_id: str, emit: Callable[[Dict[str, Any]], Any]
) -> str:
    """等待授权决定；期间持续发 keepalive，写失败即视为客户端断开"""
    wait_task = asyncio.create_task(session.wait(request_id))
    ping_task = asyncio.create_task(_keepalive(emit))
    try:
        done, _ = await asyncio.wait(
            {wait_task, ping_task}, return_when=asyncio.FIRST_COMPLETED)
        if wait_task in done:
            return wait_task.result()
        ping_task.result()  # 连接断开时在此抛出
        wait_task.cancel()
        raise ConnectionError('客户端连接已断开')
    finally:
        for task in (wait_task, ping_task):
            if not task.done():
                task.cancel()
        for task in (wait_task, ping_task):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


async def _pump_stream(
    agent: Agent,
    inputs: List[Msg],
    emit: Callable[[Dict[str, Any]], Any],
    *,
    tracker: _ToolTracker,
    on_tool: Optional[Callable[[Dict[str, Any]], Any]] = None,
    permission_session: Any = None,
) -> None:
    """消费 reply_stream 事件并通过 emit 下发（emit 可为同步回调）

    D7：遇到 RequireUserConfirmEvent 时挂起当前流，下发授权卡片并等待决定，
    然后在同一个 Agent / 事件循环上续跑；超时与拒绝都由服务端收口。
    """
    current_inputs: Any = inputs

    async def _emit(payload: Dict[str, Any]) -> None:
        result = emit(payload)
        if asyncio.iscoroutine(result):
            await result

    while True:
        stream = agent.reply_stream(current_inputs)
        confirm_event: Optional[RequireUserConfirmEvent] = None
        try:
            async for ev in stream:
                if isinstance(ev, TextBlockDeltaEvent):
                    if ev.delta:
                        await _emit({'type': 'chunk', 'content': ev.delta})
                elif isinstance(ev, ThinkingBlockDeltaEvent):
                    if ev.delta:
                        await _emit({'type': 'thinking', 'content': ev.delta})
                elif isinstance(ev, ReplyEndEvent):
                    if ev.error:
                        await _emit({'type': 'error', 'message': str(ev.error)})
                    elif ev.finished_reason == ReplyFinishedReason.EXCEED_MAX_ITERS:
                        # A3：查询次数到顶——显式补发如实说明与降级条，不静默收尾
                        for payload in _query_limit_payloads(tracker):
                            await _emit(payload)
                elif isinstance(ev, RequireUserConfirmEvent):
                    confirm_event = ev
                else:
                    for payload in tracker.on(ev):
                        await _emit(payload)
                        if payload.get('type') == 'tool_result':
                            full = tracker.pop_full(payload.get('call_id', ''))
                            if on_tool is not None:
                                await _call_tool_hook(on_tool, payload, full)
        except Exception as exc:
            await _emit({'type': 'error', 'message': _friendly_error(exc)})
            break
        finally:
            # 显式关闭异步生成器，让其清理代码在事件循环存活期内执行，
            # 避免循环关闭后 GC 触发 "Task was destroyed but it is pending"
            try:
                await stream.aclose()
            except Exception:
                pass

        if confirm_event is None:
            break

        confirm_results: List[ConfirmResult] = []
        try:
            for tool_call in confirm_event.tool_calls:
                request_id: Optional[str] = None
                decision = 'deny'
                if permission_session is not None:
                    payload = _permission_payload(
                        tool_call, tracker, int(getattr(permission_session, 'timeout', 60)))
                    request_id = await permission_session.open(payload)
                    payload['request_id'] = request_id
                    await _emit(payload)
                    decision = await _await_decision_with_keepalive(
                        permission_session, request_id, _emit)
                    await permission_session.close(request_id)
                    if decision == 'allow_session':
                        try:
                            await permission_session.remember(tool_call.name)
                        except Exception:
                            # 授权记忆失败不阻断本轮作答，下次仍会再次询问
                            pass
                await _emit({
                    'type': 'permission_result',
                    'request_id': request_id,
                    'call_id': tool_call.id,
                    'display': tracker.display_of(tool_call.name),
                    'decision': decision,
                })
                confirm_results.append(_confirm_result(tool_call, decision))
        except Exception as exc:
            await _emit({'type': 'error', 'message': _friendly_error(exc)})
            break

        current_inputs = UserConfirmResultEvent(
            reply_id=confirm_event.reply_id, confirm_results=confirm_results)


def run_agent_stream(
    agent: Agent,
    history: Iterable[Dict[str, str]],
    user_message: str,
    *,
    tracker: Optional[_ToolTracker] = None,
    on_tool: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> Generator[Dict[str, Any], None, None]:
    """
    流式执行智能体，以同步生成器产出事件字典

    Args:
        agent: 由注册中心创建的 Agent 实例
        history: 历史消息 [{'role': 'user'|'assistant', 'content': str}]
        user_message: 本轮用户输入
        on_tool: 每次工具调用结束时的回调（用于 F3 审计留痕）
    """
    inputs = build_inputs(history, user_message)
    tool_tracker = tracker or _ToolTracker()

    async def _consume(q: 'queue.Queue'):
        await _pump_stream(
            agent, inputs, lambda payload: q.put(('evt', payload)),
            tracker=tool_tracker, on_tool=on_tool)
        q.put(('done', None))

    q = _run_coro_in_thread(_consume)

    while True:
        kind, payload = q.get()
        if kind == 'done':
            break
        if kind == 'error':
            yield {'type': 'error', 'message': payload}
        else:
            yield payload


async def run_agent_stream_async(
    agent: Agent,
    history: Iterable[Dict[str, str]],
    user_message: str,
    *,
    tracker: Optional[_ToolTracker] = None,
    on_tool: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    原生异步流式执行智能体（供 ASGI 下的 StreamingHttpResponse 直接消费）

    与 run_agent_stream 产出相同格式的事件字典，但不经过后台线程 + 队列
    桥接：AgentScope 的 reply_stream 本身就是异步生成器，在事件循环内
    直接迭代即可，避免 Django 对同步迭代器的兼容告警与断流问题。
    """
    inputs = build_inputs(history, user_message)
    queue_: 'asyncio.Queue' = asyncio.Queue()

    async def _emit(payload: Dict[str, Any]) -> None:
        await queue_.put(payload)

    async def _run() -> None:
        try:
            await _pump_stream(agent, inputs, _emit,
                               tracker=tracker or _ToolTracker(), on_tool=on_tool)
        finally:
            await queue_.put(None)

    pump = asyncio.create_task(_run())
    try:
        while True:
            payload = await queue_.get()
            if payload is None:
                break
            yield payload
    finally:
        # 客户端断开时取消消费任务，显式 aclose 让清理逻辑在循环存活期内执行
        if not pump.done():
            pump.cancel()
        try:
            await pump
        except (asyncio.CancelledError, Exception):
            pass


async def _iter_async(
    agent: Agent,
    inputs: List[Msg],
    tracker: _ToolTracker,
    on_tool: Optional[Callable[[Dict[str, Any]], Any]],
    permission_session: Any = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """在调用方事件循环内直接产出事件字典（装配会话用，不经队列）"""
    current_inputs: Any = inputs
    while True:
        stream = agent.reply_stream(current_inputs)
        confirm_event: Optional[RequireUserConfirmEvent] = None
        try:
            async for ev in stream:
                if isinstance(ev, TextBlockDeltaEvent):
                    if ev.delta:
                        yield {'type': 'chunk', 'content': ev.delta}
                elif isinstance(ev, ThinkingBlockDeltaEvent):
                    if ev.delta:
                        yield {'type': 'thinking', 'content': ev.delta}
                elif isinstance(ev, ReplyEndEvent):
                    if ev.error:
                        yield {'type': 'error', 'message': _friendly_error(RuntimeError(str(ev.error)))}
                    elif ev.finished_reason == ReplyFinishedReason.EXCEED_MAX_ITERS:
                        # A3：查询次数到顶——显式补发如实说明与降级条，不静默收尾
                        for payload in _query_limit_payloads(tracker):
                            yield payload
                elif isinstance(ev, RequireUserConfirmEvent):
                    confirm_event = ev
                else:
                    for payload in tracker.on(ev):
                        yield payload
                        if payload.get('type') == 'tool_result':
                            full = tracker.pop_full(payload.get('call_id', ''))
                            if on_tool is not None:
                                await _call_tool_hook(on_tool, payload, full)
        except Exception as exc:
            yield {'type': 'error', 'message': _friendly_error(exc)}
            break
        finally:
            try:
                await stream.aclose()
            except Exception:
                pass

        if confirm_event is None:
            break

        confirm_results: List[ConfirmResult] = []
        for tool_call in confirm_event.tool_calls:
            request_id: Optional[str] = None
            decision = 'deny'
            if permission_session is not None:
                payload = _permission_payload(
                    tool_call, tracker, int(getattr(permission_session, 'timeout', 60)))
                request_id = await permission_session.open(payload)
                payload['request_id'] = request_id
                yield payload
                decision = await permission_session.wait(request_id)
                await permission_session.close(request_id)
                if decision == 'allow_session':
                    try:
                        await permission_session.remember(tool_call.name)
                    except Exception:
                        pass
            yield {
                'type': 'permission_result',
                'request_id': request_id,
                'call_id': tool_call.id,
                'display': tracker.display_of(tool_call.name),
                'decision': decision,
            }
            confirm_results.append(_confirm_result(tool_call, decision))

        current_inputs = UserConfirmResultEvent(
            reply_id=confirm_event.reply_id, confirm_results=confirm_results)


async def run_profile_stream_async(
    agent_id: int,
    history: Iterable[Dict[str, str]],
    user_message: str,
    *,
    user=None,
    user_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    on_tool: Optional[Callable[[Dict[str, Any]], Any]] = None,
    agent_version: Optional[int] = None,
    permission_session: Any = None,
    session_grants: Optional[List[str]] = None,
    on_knowledge_sources: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    preference_prompt: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """按智能体定义装配并流式执行（D1–D6），协程版

    装配 → 连接 → 执行 → 关闭全在同一条事件循环内，
    第一个事件固定是降级条（哪怕说明为空），让界面一致渲染。
    agent_version 把会话钉在开场版本（7.3）。
    """
    from .runtime import create_profile_runtime

    bundle, notes = await create_profile_runtime(
        agent_id, user=user, user_id=user_id, conversation_id=conversation_id,
        agent_version=agent_version, session_grants=session_grants,
        interactive=permission_session is not None,
        on_knowledge_sources=on_knowledge_sources,
        preference_prompt=preference_prompt)
    if bundle is None:
        yield {'type': 'degrade', 'notes': [n.as_dict() for n in notes]}
        yield {'type': 'error', 'message': notes[0].reason if notes else '智能体不可用'}
        return

    yield {'type': 'degrade', 'notes': [n.as_dict() for n in bundle.notes]}
    inputs = build_inputs(history, user_message)
    tracker = _ToolTracker(bundle.displays, bundle.egress_tools,
                           bundle.egress_hosts)
    try:
        async for payload in _iter_async(
                bundle.agent, inputs, tracker, on_tool, permission_session):
            yield payload
    finally:
        await bundle.aclose()


def run_profile_stream(
    agent_id: int,
    history: Iterable[Dict[str, str]],
    user_message: str,
    *,
    user=None,
    user_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    on_tool: Optional[Callable[[Dict[str, Any]], Any]] = None,
    draft: bool = False,
    agent_version: Optional[int] = None,
    permission_session: Any = None,
    session_grants: Optional[List[str]] = None,
    on_knowledge_sources: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    preference_prompt: Optional[str] = None,
) -> Generator[Dict[str, Any], None, None]:
    """按智能体定义装配并流式执行的同步生成器版（WSGI runserver 下可用）

    draft=True（A3 试聊）按最新保存的草稿配置装配，不带「尚未发布」降级条。
    agent_version 把会话钉在开场版本（7.3）。
    """
    inputs = build_inputs(history, user_message)

    async def _consume(q: 'queue.Queue'):
        from .runtime import create_profile_runtime

        try:
            bundle, notes = await create_profile_runtime(
                agent_id, user=user, user_id=user_id,
                conversation_id=conversation_id, draft=draft,
                agent_version=agent_version, session_grants=session_grants,
                interactive=permission_session is not None,
                on_knowledge_sources=on_knowledge_sources,
                preference_prompt=preference_prompt)
        except Exception as exc:
            q.put(('error', _friendly_error(exc)))
            q.put(('done', None))
            return

        if bundle is None:
            q.put(('evt', {'type': 'degrade',
                           'notes': [n.as_dict() for n in notes]}))
            q.put(('error', notes[0].reason if notes else '智能体不可用'))
            q.put(('done', None))
            return

        q.put(('evt', {'type': 'degrade',
                       'notes': [n.as_dict() for n in bundle.notes]}))
        tracker = _ToolTracker(bundle.displays, bundle.egress_tools,
                           bundle.egress_hosts)
        try:
            await _pump_stream(bundle.agent, inputs,
                               lambda payload: q.put(('evt', payload)),
                               tracker=tracker, on_tool=on_tool,
                               permission_session=permission_session)
        finally:
            await bundle.aclose()
            q.put(('done', None))

    q = _run_coro_in_thread(_consume)

    while True:
        kind, payload = q.get()
        if kind == 'done':
            break
        if kind == 'error':
            yield {'type': 'error', 'message': payload}
        else:
            yield payload


def run_profile_reply(
    agent_id: int,
    history: Iterable[Dict[str, str]],
    user_message: str,
    *,
    user=None,
    user_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    on_tool: Optional[Callable[[Dict[str, Any]], Any]] = None,
    agent_version: Optional[int] = None,
    on_knowledge_sources: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    preference_prompt: Optional[str] = None,
    return_usage: bool = False,
) -> Tuple[str, List[Dict[str, Any]]]:
    """按智能体定义非流式执行，返回 (完整回答, 降级说明)

    return_usage=True 时返回 (完整回答, 降级说明, usage)；
    usage 取自本轮所有模型调用（非流式同样保留真实 token 数）。
    """
    text: List[str] = []
    notes: List[Dict[str, Any]] = []
    usage = {
        'input_tokens': 0,
        'output_tokens': 0,
        'cache_input_tokens': 0,
        'cache_creation_input_tokens': 0,
        'model_calls': 0,
    }
    for payload in run_profile_stream(
        agent_id, history, user_message, user=user, user_id=user_id,
        conversation_id=conversation_id, on_tool=on_tool,
        agent_version=agent_version, permission_session=None,
        session_grants=[], on_knowledge_sources=on_knowledge_sources,
        preference_prompt=preference_prompt):
        if payload.get('type') == 'chunk':
            text.append(payload['content'])
        elif payload.get('type') == 'degrade':
            # 累加而非替换：装配期降级条与 A3 触顶补发条要同时保留
            notes.extend(payload.get('notes') or [])
        elif payload.get('type') == 'usage':
            for key in usage:
                usage[key] += int(payload.get(key) or 0)
        elif payload.get('type') == 'error':
            raise RuntimeError(payload.get('message') or '智能体调用失败')
    if return_usage:
        return ''.join(text), notes, usage
    return ''.join(text), notes


def run_agent(
    agent: Agent,
    history: Iterable[Dict[str, str]],
    user_message: str,
) -> str:
    """
    非流式执行智能体，返回完整回答文本

    Raises:
        RuntimeError: 模型调用失败（已转为友好文案）
    """
    inputs = build_inputs(history, user_message)
    result: List[Tuple[str, Any]] = []

    async def _call(q: 'queue.Queue'):
        try:
            reply = await agent.reply(inputs)
            result.append(('ok', _extract_text(reply)))
        except Exception as e:
            result.append(('error', _friendly_error(e)))
        finally:
            q.put(('done', None))

    q = _run_coro_in_thread(_call)
    q.get()  # 阻塞直至完成

    kind, payload = result[0]
    if kind == 'error':
        raise RuntimeError(payload)
    return payload
