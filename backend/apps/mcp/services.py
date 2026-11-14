"""MCP 工具来源的服务层（B2/B3/B7/B8 的编排逻辑）

职责边界：模型层已承载字段规则与密钥加解密，本模块只做三件事——
1. 命令前缀白名单判定（B2，平台内置只读，现场不可扩充）
2. 出网判定与出网总开关（B8，红线自 P0 生效，闸门出厂为关）
3. 连通性测试与工具发现（B3，一次性握手，失败归类六类，原始堆栈只折叠不外显）

P0 不引入常驻连接池：测试与发现都是"拉起 → 握手 → list_tools → 关闭"的一次性会话，
因此命令型来源的"保持会话/常驻托管"在本版本尚未就绪，接口会如实回传该状态。
"""
import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from urllib.parse import urlparse

from django.utils import timezone

from backend.apps.mcp.models import MCPTool, MCPServer

logger = logging.getLogger(__name__)


# ─── B2 命令前缀白名单（内置只读） ───

def _interpreter_prefixes() -> list:
    """平台自带解释器路径：当前进程解释器 + 可选自定义 venv 路径"""
    out = [os.path.realpath(sys.executable), sys.executable]
    custom = os.getenv('MCP_PYTHON_BIN', '').strip()
    if custom:
        out.append(custom)
    out.append('python')
    out.append('python3')
    return out


def command_prefix_whitelist() -> list:
    """允许的命令前缀清单（含用途说明），管理端只读展示。

    新增一条前缀＝走版本发布，界面上不提供"新增"入口（PRD B2 已决口径）。
    """
    prefixes = []
    for p in _interpreter_prefixes():
        prefixes.append({
            'prefix': p,
            'usage': '平台自带 Python 解释器，用于拉起随版本发布的 MCP 服务脚本',
        })
    prefixes += [
        {'prefix': 'node', 'usage': 'Node.js 运行时，用于拉起随版本发布的 MCP 服务脚本'},
        {'prefix': 'npx -y', 'usage': '拉起平台评审后内置的官方 npm 包形态 MCP 服务'},
        {'prefix': 'uvx', 'usage': '拉起平台评审后内置的官方 PyPI 包形态 MCP 服务'},
    ]
    seen, uniq = set(), []
    for item in prefixes:
        if item['prefix'] and item['prefix'] not in seen:
            seen.add(item['prefix'])
            uniq.append(item)
    return uniq


def match_command_prefix(command: str, args: list | None = None) -> dict | None:
    """命令与参数拼成命令行后，逐条前缀比对；命中返回该条，未命中返回 None"""
    if not command:
        return None
    argv = [command, *[str(a) for a in (args or [])]]
    cmdline = ' '.join(argv).strip()
    for item in command_prefix_whitelist():
        prefix = item['prefix'].strip()
        if cmdline == prefix or cmdline.startswith(prefix + ' '):
            return item
    return None


# ─── B8 出网判定与总开关 ───

INTRAST_NET_PATTERNS = ('.local', '.internal', '.intra', '.lan')


def is_private_host(host: str) -> bool:
    """内网地址段基线（P0 口径：环回 + 私网 + 链路本地 + 内网后缀）

    正式基线随 #10 定稿后由版本发布替换，现场与管理员均不可改。
    """
    if not host:
        return False
    host = host.strip().lower().rstrip('.')
    if any(host.endswith(sfx) for sfx in INTRAST_NET_PATTERNS):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_multicast or ip.is_reserved)
    except ValueError:
        pass
    # 无点的主机名按内网短名处理；其余按内网后缀判断结果为准
    if '.' not in host:
        return True
    return host.endswith(('.corp', '.intr'))


def is_egress_target(url: str) -> bool:
    """接口型来源的目标地址是否落在企业内网之外（出网类来源）"""
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.hostname or ''
    return not is_private_host(host)


def egress_allowed() -> bool:
    """全平台出网总开关（出厂为关）"""
    from backend.apps.settings.models import SystemSetting
    raw = SystemSetting.get_value('mcp_egress_enabled', 'false')
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


def set_egress_allowed(enabled: bool, actor=None) -> bool:
    """切换出网总开关并即时生效；变更入审计（egress_toggle）"""
    from django.db import transaction

    from backend.apps.settings.models import SystemSetting
    from backend.apps.users.models import log_audit

    SystemSetting.set_value('mcp_egress_enabled', 'true' if enabled else 'false',
                            '允许平台出网（B8 总开关）')
    with transaction.atomic():
        MCPServer.objects.filter(requires_egress=True).exclude(
            status='disabled').update(
            status='unknown' if enabled else 'egress_blocked',
            last_error='' if enabled else '全平台出网总开关为关，该来源不可连接',
        )
    log_audit(actor=actor, action='egress_toggle', target='出网总开关',
              detail='开放' if enabled else '关闭')
    return enabled


# ─── B8-a 出网域名白名单 ───

STRICT_DOMAINS_KEY = 'mcp_egress_strict_domains'
_HOSTNAME_RE = re.compile(
    r'^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*'
    r'[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')


def egress_strict_enabled() -> bool:
    """严格域名白名单开关（出厂为关：行为与引入白名单前完全一致）"""
    from backend.apps.settings.models import SystemSetting
    raw = SystemSetting.get_value(STRICT_DOMAINS_KEY, 'false')
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


def set_egress_strict(enabled: bool, actor=None) -> bool:
    """切换严格白名单；已在白名单内的来源无需改状态，只影响下一次连接"""
    from backend.apps.settings.models import SystemSetting
    from backend.apps.users.models import log_audit

    SystemSetting.set_value(STRICT_DOMAINS_KEY, 'true' if enabled else 'false',
                            '出网类来源按允许域名严格校验（B8-a）')
    log_audit(actor=actor, action='egress_strict_toggle', target='出网域名白名单',
              detail='开启严格校验' if enabled else '关闭严格校验（沿用原有行为）')
    return enabled


def url_host(url: str) -> str:
    return (urlparse(url or '').hostname or '').strip().lower().rstrip('.')


def normalize_domain(value) -> str:
    """规范化单条域名：小写、去尾点；非法形态抛 ValueError"""
    text = str(value or '').strip().lower().rstrip('.')
    if not text:
        raise ValueError('域名不能为空')
    if '://' in text or '/' in text or ':' in text:
        raise ValueError(f'「{value}」只填主机名，不带协议、端口或路径')
    wildcard = text.startswith('*.')
    base = text[2:] if wildcard else text
    if '*' in base or not base:
        raise ValueError(f'「{value}」不是合法域名：通配只支持最左侧的 *.example.com')
    try:
        ipaddress.ip_address(base)
    except ValueError:
        if not _HOSTNAME_RE.match(base):
            raise ValueError(f'「{value}」不是合法域名')
    return f'*.{base}' if wildcard else base


def normalize_domains(values) -> list:
    out = []
    for item in (values or []):
        domain = normalize_domain(item)
        if domain not in out:
            out.append(domain)
    return out


def domain_allows(host: str, patterns) -> bool:
    """host 是否命中允许清单：精确域名，或 *.example.com 匹配其子域（不含裸域）"""
    host = (host or '').strip().lower().rstrip('.')
    if not host:
        return False
    for raw in patterns or []:
        try:
            pattern = normalize_domain(raw)
        except ValueError:
            continue
        if pattern.startswith('*.'):
            if host.endswith(pattern[1:]):
                return True
        elif host == pattern:
            return True
    return False


def resolved_addresses(host: str) -> list:
    """解析主机当前指向的 IPv4（A 记录），用于连接前复验防 DNS rebinding。

    只看 A 记录：AAAA 既不解回也不参与判定，避免站点只有 A 记录可连、
    但额外解析出非全局 IPv6 时被整体误拦。A 记录为空由调用方按解析失败处理。
    """
    infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def resolved_private_hit(host: str) -> str:
    """返回命中的私网/保留 IPv4；无 A 记录或解析失败抛 OSError 由调用方归类"""
    addresses = resolved_addresses(host)
    if not addresses:
        raise OSError(f'{host} 没有可用的 A 记录')
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_multicast or ip.is_reserved):
            return addr
    return ''


def egress_gate(server) -> dict:
    """出网闸门判定（B8-a）

    严格白名单关闭时只看总开关，与引入白名单前行为一致；
    开启后依次校验：总开关 → 声明域名覆盖 → DNS 解析结果不落私网段。
    """
    host = url_host(server.url)
    gate = {
        'active': False, 'strict': egress_strict_enabled(), 'host': host,
        'allowed_domains': list(server.egress_domains or []),
        'blocked': False, 'reason_code': '', 'user_message': '', 'admin_message': '',
        'resolved_address': '',
    }
    if server.transport != 'http_mcp' or not server.requires_egress:
        return gate
    gate['active'] = True

    if not egress_allowed():
        gate.update(
            blocked=True, reason_code='egress_switch_off',
            user_message='该来源属于出网类来源，平台出网总开关当前为关，暂不可连接。',
            admin_message='全平台出网总开关为关；请在「工具来源 → 全平台出网」评估后开启。',
        )
        return gate

    if not gate['strict']:
        return gate

    domains = gate['allowed_domains']
    if not domain_allows(host, domains):
        allowed = '、'.join(domains) if domains else '未声明'
        gate.update(
            blocked=True, reason_code='egress_domain_not_allowed',
            user_message='该来源只允许访问已登记的外部地址，本次目标不在允许范围内，请联系平台管理员补充。',
            admin_message=f'目标主机 {host or "（空）"} 不在该来源的允许域名内（当前：{allowed}）；'
                          f'请编辑来源补充精确域名或 *.example.com 通配。',
        )
        return gate

    try:
        hit = resolved_private_hit(host)
    except OSError:
        gate.update(
            blocked=True, reason_code='egress_dns_failed',
            user_message='该来源的地址当前无法解析，请联系平台管理员核对域名。',
            admin_message=f'域名 {host} 解析失败，无法确认连接目标，已按安全策略拦截。',
        )
        return gate
    if hit:
        gate.update(
            blocked=True, reason_code='egress_private_resolved', resolved_address=hit,
            user_message='该来源当前指向内网地址，已被安全策略拦截，请联系平台管理员核对。',
            admin_message=f'域名 {host} 解析到私网/保留地址 {hit}，已拦截（防 DNS rebinding）；'
                          f'请核对域名解析，确需访问内网请改用内网地址并重新登记来源。',
        )
    return gate

# ─── B3 失败归类 ───

FAILURE_CATEGORY_LABELS = {
    'address_unreachable': '地址不通',
    'auth_failed': '认证失败',
    'protocol_incompatible': '协议不兼容',
    'launch_failed': '启动失败',
    'timeout': '超时',
    'no_response': '超时未响应',
    'egress_blocked': '出网未开放',
    'unknown': '未知错误',
}


def categorize_failure(exc: BaseException) -> tuple:
    """把原始异常归成 PRD 的六类，返回 (归类码, 可读原因, 原始信息)"""
    raw = f'{type(exc).__name__}: {exc}'
    text = str(exc).lower()
    name = type(exc).__name__

    if isinstance(exc, asyncio.TimeoutError) or 'timeout' in text or 'timed out' in text:
        if 'read' in text or 'response' in text or 'idle' in text:
            return 'no_response', '服务在超时时间内没有响应，可能已卡住或负载过高', raw
        return 'timeout', '连接或工具发现未在平台固定超时内完成', raw
    if isinstance(exc, FileNotFoundError) or 'winerror 2' in text or 'no such file' in text \
            or 'not found' in text or 'program not found' in text or 'filenotfounderror' in text:
        return 'launch_failed', '启动命令或解释器不存在，请核对平台内置的允许前缀', raw
    if 'permission' in text or 'errno 13' in text or 'refused to execute' in text:
        return 'launch_failed', '进程无权限启动，请检查工作目录与文件权限', raw
    if 'cancelled' in text and 'in message' in text:
        return 'launch_failed', '进程在握手完成前退出', raw
    if name in ('HTTPStatusError',) or '401' in text or '403' in text or 'unauthorized' in text \
            or 'forbidden' in text or 'invalid api key' in text:
        return 'auth_failed', '认证被拒绝，请检查认证头或密钥', raw
    if 'connecterror' in name.lower() or 'connection refused' in text or 'getaddrinfo' in text \
            or 'name or service not known' in text or 'unreachable' in text \
            or 'connect' in text and 'error' in text:
        return 'address_unreachable', '无法连接到服务地址，请确认地址与网络可达', raw
    if 'invalid request' in text or 'unsupported' in text or 'protocolversion' in text \
            or 'not a valid' in text or 'json' in text or 'parse' in text \
            or '406' in text or '404' in text or 'method not found' in text \
            or 'mcp' in text and 'version' in text:
        return 'protocol_incompatible', '对端不是平台支持的 MCP 协议形态，或版本不兼容', raw
    return 'unknown', '连接或工具发现失败', raw


# ─── B3 连通性测试与工具发现 ───

def params_summary(schema: dict) -> list:
    """入参摘要：只取属性名、类型、是否必填，供管理端列表展示"""
    props = (schema or {}).get('properties') or {}
    required = set((schema or {}).get('required') or [])
    out = []
    for key, val in props.items():
        out.append({
            'name': key,
            'type': (val or {}).get('type', 'string'),
            'required': key in required,
            'description': (val or {}).get('description', ''),
        })
    return out


async def _probe(server: MCPServer, secrets: dict):
    """一次性握手 + list_tools，返回工具字典列表"""
    from mcp import ClientSession

    headers = dict(server.headers or {})
    headers.update({str(k): str(v) for k, v in (secrets.get('headers') or {}).items()})
    env_extra = {str(k): str(v) for k, v in (secrets.get('env') or {}).items()}
    timeout = server.effective_timeout

    if server.transport == 'stdio':
        from mcp.client.stdio import StdioServerParameters, stdio_client
        params = StdioServerParameters(
            command=server.command,
            args=[str(a) for a in (server.args or [])],
            env={**os.environ, **env_extra} if env_extra else None,
            cwd=server.cwd or None,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=timeout)
                result = await asyncio.wait_for(session.list_tools(), timeout=timeout)
                return _to_tool_dicts(result)

    from mcp.client.streamable_http import streamablehttp_client
    async with streamablehttp_client(
        server.url,
        headers=headers or None,
        timeout=timeout,
        sse_read_timeout=timeout,
    ) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=timeout)
            result = await asyncio.wait_for(session.list_tools(), timeout=timeout)
            return _to_tool_dicts(result)


def _to_tool_dicts(result) -> list:
    tools = []
    for item in (getattr(result, 'tools', None) or []):
        schema = getattr(item, 'inputSchema', None) or {}
        try:
            schema = json.loads(json.dumps(schema, ensure_ascii=False))
        except (TypeError, ValueError):
            schema = {}
        tools.append({
            'name': item.name,
            'description': item.description or '',
            'input_schema': schema,
            'params': params_summary(schema),
        })
    return tools


def _run_in_thread(coro_factory, timeout: float):
    """在独立线程的独立事件循环里跑协程（WSGI 视图内不能复用 asyncio.run 于已有循环）"""
    box = {}

    def _target():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            box['value'] = loop.run_until_complete(coro_factory())
        except BaseException as exc:  # noqa: BLE001 - 原样回传给调用方归类
            box['error'] = exc
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                asyncio.set_event_loop(None)
                loop.close()

    thread = threading.Thread(target=_target, daemon=True, name='mcp-probe')
    thread.start()
    thread.join(timeout + 10)
    if thread.is_alive():
        raise asyncio.TimeoutError('探测线程未在限定时间内结束')
    if 'error' in box:
        raise box['error']
    return box['value']


def sync_tools(server: MCPServer, tools: list) -> dict:
    """把发现的工具落到 MCPTool：新工具默认关闭、消失的标 missing，
    并同步来源级 enabled_tools 快照（B4）。返回新增/失效统计。"""
    discovered = {t['name']: t for t in tools}
    present_names = set(discovered)
    now = timezone.now()

    existing = {t.name: t for t in server.tools.all()}
    new_names, missing_names = [], []

    for name, info in discovered.items():
        row = existing.get(name)
        if row is None:
            MCPTool.objects.create(
                server=server, name=name, description=info['description'],
                input_schema=info['input_schema'], enabled=False, state='present',
                last_seen_at=now,
            )
            new_names.append(name)
        else:
            row.description = info['description']
            row.input_schema = info['input_schema']
            row.last_seen_at = now
            if row.state != 'present':
                row.state = 'present'
            row.save(update_fields=['description', 'input_schema', 'last_seen_at', 'state'])

    for name, row in existing.items():
        if name not in present_names:
            if row.state != 'missing':
                row.state = 'missing'
                row.save(update_fields=['state'])
            missing_names.append(name)

    enabled = [n for n in (server.enabled_tools or []) if n in present_names]
    server.discovered_tools = [
        {'name': t['name'], 'description': t['description'], 'params': t['params']}
        for t in tools
    ]
    server.enabled_tools = enabled
    server.save(update_fields=['discovered_tools', 'enabled_tools'])
    return {'new_tools': new_names, 'missing_tools': missing_names}


def test_server(server: MCPServer, actor=None) -> dict:
    """连通性测试 + 工具发现（B3）

    成功：状态转正常、更新最近检查时间、缓存工具清单；
    失败：状态转异常、保留上次工具清单并标注可能过期，原因归类，原始信息折叠。
    测试失败不改变挂载关系与已勾选的工具白名单。
    """
    started = time.monotonic()

    gate = egress_gate(server)
    if gate['blocked']:
        return _finish_blocked(server, started, actor, gate)

    if server.transport == 'stdio' and not match_command_prefix(server.command, server.args):
        return _finish_failed(
            server, started, 'protocol_incompatible',
            '启动命令未命中平台内置的允许前缀',
            'command not whitelisted', actor=actor, audit_action='mcp_command_denied',
        )

    secrets = server.get_secrets()
    timeout = server.effective_timeout
    try:
        tools = _run_in_thread(lambda: _probe(server, secrets), timeout)
    except BaseException as exc:  # noqa: BLE001 - 归类后回传
        category, reason, raw = categorize_failure(exc)
        return _finish_failed(server, started, category, reason, raw, actor=actor)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    stats = sync_tools(server, tools)

    server.status = 'healthy'
    server.last_check_at = timezone.now()
    server.last_error = ''
    server.save(update_fields=['status', 'last_check_at', 'last_error'])

    _audit(actor, 'mcp_test', server, f'成功，发现 {len(tools)} 个工具，耗时 {elapsed_ms}ms')
    if gate['active']:
        _audit(actor, 'mcp_egress_allowed', server,
               f'出网放行：{gate["host"]}（严格白名单：{"开" if gate["strict"] else "关"}）')
    return {
        'status': 'healthy',
        'ok': True,
        'elapsed_ms': elapsed_ms,
        'timeout_seconds': timeout,
        'tools': tools,
        'new_tools': stats['new_tools'],
        'missing_tools': stats['missing_tools'],
        'tools_stale': False,
        'managed_runtime_ready': server.transport != 'stdio',
        'managed_runtime_note': _managed_runtime_note(server),
        'egress': gate,
        'failure_category': '',
        'failure_label': '',
        'failure_message': '',
        'detail': '',
    }


def audit_command_denied(request, command: str, args: list) -> None:
    """命令未命中白名单时入审计（B2 要求每条命中失败都留痕，供后续版本清单依据）"""
    from backend.apps.users.models import log_audit
    argv = ' '.join([command, *[str(a) for a in (args or [])]])
    log_audit(actor=getattr(request, 'user', None), action='mcp_command_denied',
              target=f'命令:{command}'[:200],
              detail=f'保存被阻断，命令行：{argv[:400]}；该需求应联系平台版本发布')


def _managed_runtime_note(server: MCPServer) -> str:
    if server.transport != 'stdio':
        return ''
    if server.lifecycle != 'persistent':
        return '命令型来源的连接方式已锁定为「保持会话/常驻」，每次独立会话不受支持'
    return ('本次测试为一次性拉起并回收；「保持会话」所需的平台常驻托管尚未随版本交付，'
            'P0 会话侧不注入命令型来源，业务调用请改用接口型（HTTP）来源')


def _finish_failed(server, started, category, reason, raw, actor=None,
                   audit_action='mcp_test') -> dict:
    elapsed_ms = int((time.monotonic() - started) * 1000)
    server.status = 'error'
    server.last_check_at = timezone.now()
    server.last_error = reason[:500]
    server.save(update_fields=['status', 'last_check_at', 'last_error'])
    _audit(actor, audit_action, server,
           f'失败（{FAILURE_CATEGORY_LABELS.get(category, category)}）：{reason}｜{raw[:300]}')
    return {
        'status': 'error',
        'ok': False,
        'elapsed_ms': elapsed_ms,
        'timeout_seconds': server.effective_timeout,
        'tools': server.discovered_tools or [],
        'tools_stale': bool(server.discovered_tools),
        'new_tools': [],
        'missing_tools': [],
        'managed_runtime_ready': False,
        'managed_runtime_note': _managed_runtime_note(server),
        'failure_category': category,
        'failure_label': FAILURE_CATEGORY_LABELS.get(category, category),
        'failure_message': reason,
        'detail': raw[:2000],
    }


def _finish_blocked(server, started, actor=None, gate=None) -> dict:
    elapsed_ms = int((time.monotonic() - started) * 1000)
    gate = gate or egress_gate(server)
    server.status = 'egress_blocked'
    server.last_check_at = timezone.now()
    server.last_error = (gate.get('admin_message') or '出网连接被闸门拦截')[:500]
    server.save(update_fields=['status', 'last_check_at', 'last_error'])
    _audit(actor, 'mcp_egress_denied', server,
           f'出网拦截（{gate.get("reason_code") or "unknown"}）：'
           f'{gate.get("admin_message") or ""}')
    return {
        'status': 'egress_blocked',
        'ok': False,
        'elapsed_ms': elapsed_ms,
        'timeout_seconds': server.effective_timeout,
        'tools': server.discovered_tools or [],
        'tools_stale': bool(server.discovered_tools),
        'new_tools': [],
        'missing_tools': [],
        'managed_runtime_ready': False,
        'managed_runtime_note': '',
        'egress': gate,
        'failure_category': 'egress_blocked',
        'failure_label': FAILURE_CATEGORY_LABELS['egress_blocked'],
        'failure_message': gate.get('user_message') or '该来源为出网类来源，连接被平台闸门拦截',
        'detail': f'egress gate blocked: {gate.get("reason_code") or "unknown"}',
    }


def _audit(actor, action, server, detail):
    from backend.apps.users.models import log_audit
    log_audit(actor=actor, action=action, target=f'工具来源:{server.name}', detail=detail)


# ─── B6 引用检查 ───

def server_references(server: MCPServer) -> list:
    """列出引用该来源的智能体及各自勾选的工具（B6 删除前展示）"""
    from backend.apps.agenthub.models import AgentMCPServer

    rows = AgentMCPServer.objects.filter(server=server).select_related('agent')
    return [{'id': r.agent_id, 'name': r.agent.name, 'status': r.agent.status,
             'tool_names': list(r.tool_names or [])} for r in rows]


def unmount_server(server: MCPServer, actor=None) -> int:
    """从全部智能体上摘除该来源（B6 出路二），逐项写入审计"""
    from backend.apps.agenthub.models import AgentMCPServer, AgentProfile
    affected = list(AgentProfile.objects.filter(mcp_servers=server))
    for agent in affected:
        tools = []
        rec = AgentMCPServer.objects.filter(agent=agent, server=server).first()
        if rec:
            tools = list(rec.tool_names or [])
        AgentMCPServer.objects.filter(agent=agent, server=server).delete()
        _audit(actor, 'mcp_unmount', server,
               f'因删除来源摘除智能体「{agent.name}」，被摘工具：{json.dumps(tools, ensure_ascii=False)}')
    return len(affected)
