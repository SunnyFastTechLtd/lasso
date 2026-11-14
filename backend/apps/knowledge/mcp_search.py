"""天工 L1：MCP 搜索主路

不硬编码厂商：
- 扫描已接入 MCP 工具的 name/description/input_schema，给出搜索工具候选与映射建议；
- 管理员确认后写入 TiangongSearchBinding（query/limit 入参与结果字段映射、优先级）；
- 凭据只从 MCPServer.secrets_enc 读取，绑定表不保存明文；
- 工具/来源停用时绑定自动停用（knowledge.signals）。

调用失败或空结果转成可读原因码，交回 tiangong._discover 继续走 L2。
"""
import asyncio
import json
import logging
import re
import time

from django.utils import timezone

from backend.apps.knowledge.models import TiangongSearchBinding
from backend.apps.mcp.models import MCPTool
from backend.apps.nuwa import knowledge_fetch, net
from backend.apps.users.models import log_audit

logger = logging.getLogger(__name__)

MIN_CANDIDATE_SCORE = 20
QUERY_FIELDS = ('query', 'q', 'keyword', 'keywords', 'search_query', 'searchQuery', 'text')
LIMIT_FIELDS = ('limit', 'count', 'num_results', 'max_results', 'top_k', 'n')
SEARCH_MARKS = ('search', 'websearch', 'web_search', 'search_web', 'google', 'bing',
                'baidu', 'duckduckgo', '检索', '搜索', '联网')
FETCH_MARKS = ('fetch', 'crawl', 'browser', 'open_url', 'visit', 'scrape', 'download')
URL_ALIASES = ('url', 'link', 'href', 'source_url', 'website')
TITLE_ALIASES = ('title', 'name', 'headline')
SNIPPET_ALIASES = ('content', 'snippet', 'description', 'summary', 'abstract')
COMMON_LIST_KEYS = ('results', 'items', 'data', 'organic_results', 'webPages', 'value')


class ToolCallError(Exception):
    """工具调用/返回值无法使用时抛出，带可读原因码"""

    def __init__(self, reason: str, message: str = ''):
        super().__init__(message or reason)
        self.reason = reason
        self.message = message or reason


def _schema_props(tool: MCPTool) -> dict:
    return ((tool.input_schema or {}).get('properties') or {})


def _first_prop(props: dict, names) -> str:
    lowered = {str(key).lower(): key for key in props}
    for name in names:
        if name.lower() in lowered:
            return str(lowered[name.lower()])
    return ''


def score_tool(tool: MCPTool) -> tuple:
    """给单个工具打搜索候选分；返回 (score, reasons, suggested_mapping)"""
    name = (tool.name or '').lower()
    description = (tool.description or '').lower()
    props = _schema_props(tool)
    reasons = []
    score = 0

    hit_name = [mark for mark in SEARCH_MARKS if mark in name]
    if hit_name:
        score += 40
        reasons.append(f'工具名包含 {hit_name[0]}')
    hit_desc = [mark for mark in SEARCH_MARKS if mark in description]
    if hit_desc:
        score += 20
        reasons.append(f'描述包含 {hit_desc[0]}')
    query_field = _first_prop(props, QUERY_FIELDS)
    if query_field:
        score += 15
        reasons.append(f'入参含查询字段 {query_field}')
    limit_field = _first_prop(props, LIMIT_FIELDS)
    if limit_field:
        score += 5
        reasons.append(f'入参含数量字段 {limit_field}')

    if any(mark in name for mark in FETCH_MARKS) and not hit_name:
        score = max(0, score - 30)
        reasons.append('更像抓取工具，已降权')
    if not query_field:
        score = max(0, score - 10)

    suggested = {
        'query_field': query_field or 'query',
        'limit_field': limit_field,
        'default_limit': 5,
        'results_path': 'results',
        'url_field': 'url',
        'title_field': 'title',
        'snippet_field': 'content',
    }
    return score, reasons, suggested


def scan_candidates(limit: int = 50) -> list:
    """扫描所有已接入 MCP 工具，返回搜索候选（不调用工具、不改任何开关）"""
    bound = set(TiangongSearchBinding.objects.values_list('server_id', 'tool_id'))
    out = []
    for tool in MCPTool.objects.select_related('server').order_by('server__name', 'name'):
        score, reasons, suggested = score_tool(tool)
        if score < MIN_CANDIDATE_SCORE:
            continue
        out.append({
            'server_id': tool.server_id,
            'server_name': tool.server.name,
            'server_status': tool.server.status,
            'server_active': tool.server.is_active,
            'tool_id': tool.id,
            'tool_name': tool.name,
            'tool_full_name': tool.full_name,
            'tool_enabled': tool.enabled,
            'tool_state': tool.state,
            'description': tool.description,
            'input_schema': tool.input_schema,
            'score': score,
            'reasons': reasons,
            'suggested_mapping': suggested,
            'bound': (tool.server_id, tool.id) in bound,
        })
    out.sort(key=lambda item: (-item['score'], item['server_name'], item['tool_name']))
    return out[:limit]


# ─── 调用与结果映射 ─────────────────────────────────────────────

def _headers_of(server) -> dict:
    headers = dict(server.headers or {})
    secrets = server.get_secrets()
    headers.update({str(k): str(v) for k, v in (secrets.get('headers') or {}).items()})
    return headers


def _env_of(server) -> dict:
    secrets = server.get_secrets()
    return {str(k): str(v) for k, v in (secrets.get('env') or {}).items()}


def _call_tool(server, tool_name: str, arguments: dict):
    """一次性建立 MCP 会话并调用工具；凭据只在内存中装配"""
    from mcp import ClientSession
    from backend.apps.mcp.services import _run_in_thread

    timeout = server.effective_timeout

    if server.transport == 'stdio':
        import os
        from mcp.client.stdio import StdioServerParameters, stdio_client

        env_extra = _env_of(server)
        params = StdioServerParameters(
            command=server.command,
            args=[str(a) for a in (server.args or [])],
            env={**os.environ, **env_extra} if env_extra else None,
            cwd=server.cwd or None,
        )

        async def run_stdio():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout=timeout)
                    return await asyncio.wait_for(
                        session.call_tool(tool_name, arguments=arguments), timeout=timeout)

        return _run_in_thread(run_stdio, timeout)

    from mcp.client.streamable_http import streamablehttp_client

    async def run_http():
        async with streamablehttp_client(
            server.url, headers=_headers_of(server) or None,
            timeout=timeout, sse_read_timeout=timeout,
        ) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=timeout)
                return await asyncio.wait_for(
                    session.call_tool(tool_name, arguments=arguments), timeout=timeout)

    return _run_in_thread(run_http, timeout)


def _reason_from_exception(exc: BaseException) -> str:
    from backend.apps.mcp.services import categorize_failure

    category, _message, _raw = categorize_failure(exc)
    return {
        'timeout': 'mcp_timeout',
        'no_response': 'mcp_timeout',
        'auth_failed': 'mcp_auth_error',
        'protocol_incompatible': 'mcp_schema_mismatch',
        'address_unreachable': 'mcp_call_failed',
        'launch_failed': 'mcp_call_failed',
    }.get(category, 'mcp_call_failed')


def _payload_of(result):
    """优先 structuredContent，其次把文本块按 JSON 解析"""
    if getattr(result, 'isError', False):
        texts = [item.text for item in (getattr(result, 'content', None) or [])
                 if getattr(item, 'type', '') == 'text']
        raise ToolCallError('mcp_call_failed', '；'.join(texts)[:300] or '工具返回错误')
    structured = getattr(result, 'structuredContent', None)
    if structured is not None:
        return structured
    texts = [item.text for item in (getattr(result, 'content', None) or [])
             if getattr(item, 'type', '') == 'text' and getattr(item, 'text', '')]
    if not texts:
        return None
    joined = '\n'.join(texts).strip()
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return {'_text': joined}


def _pick_path(node, path: str):
    parts = [part for part in (path or '').split('.') if part]

    def walk(current, index):
        if index >= len(parts):
            return current
        part = parts[index]
        if isinstance(current, dict):
            return walk(current.get(part), index + 1)
        if isinstance(current, list):
            collected = []
            for item in current:
                found = walk(item, index)
                if isinstance(found, list):
                    collected.extend(found)
                elif found is not None:
                    collected.append(found)
            return collected
        return None

    return walk(node, 0)


def _results_list(payload, path: str):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    node = _pick_path(payload, path) if path else None
    if not isinstance(node, (list, dict)):
        node = None
    if node is None:
        for key in COMMON_LIST_KEYS:
            if isinstance(payload.get(key), list):
                node = payload[key]
                break
    if node is None:
        lists = [value for value in payload.values() if isinstance(value, list)]
        node = lists[0] if len(lists) == 1 else None
    if isinstance(node, dict):
        return [node]
    return node


def _field(item: dict, path: str, aliases) -> str:
    candidates = [path] if path else []
    candidates.extend(aliases)
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if '.' in candidate:
            value = _pick_path(item, candidate)
        else:
            value = item.get(candidate)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ''


def _normalize_url(raw: str) -> str:
    url = (raw or '').strip()
    if not url:
        return ''
    ok, _reason = net.is_public_https(url)
    if not ok:
        return ''
    normalized = knowledge_fetch.normalize_url(url)
    return normalized if normalized.startswith('https://') else ''


def extract_results(binding: TiangongSearchBinding, result) -> tuple:
    """把工具返回映射成统一结果；返回 (results, 原始条数)"""
    payload = _payload_of(result)
    if payload is None:
        raise ToolCallError('mcp_result_parse_error', '工具没有返回可解析内容')
    items = _results_list(payload, binding.results_path)
    if items is None:
        raise ToolCallError('mcp_result_parse_error',
                            f'按路径 {binding.results_path or "顶层"} 未找到结果列表')
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = _normalize_url(_field(item, binding.url_field, URL_ALIASES))
        if not url:
            continue
        title = _field(item, binding.title_field, TITLE_ALIASES) or url
        snippet = _field(item, binding.snippet_field, SNIPPET_ALIASES)
        try:
            score = float(item.get('score') or 0)
        except (TypeError, ValueError):
            score = 0.0
        results.append({
            'url': url,
            'title': title[:500],
            'snippet': snippet[:1000],
            'score': score,
            'domain': net.host_of(url),
            'engine': f'mcp:{binding.tool.name}'[:50],
        })
    return results, len(items)


def call_binding(binding: TiangongSearchBinding, query: str, limit: int = None) -> dict:
    """调用单个绑定，返回 {results, reason, raw_count, elapsed_ms, error}"""
    outcome = {'results': [], 'reason': '', 'raw_count': 0,
               'elapsed_ms': 0, 'error': ''}
    if not binding.usable:
        outcome['reason'] = binding.unusable_reason
        return outcome
    arguments = dict(binding.extra_args or {})
    arguments[binding.query_field or 'query'] = query
    if binding.limit_field:
        arguments[binding.limit_field] = int(limit or binding.default_limit)
    started = time.monotonic()
    try:
        result = _call_tool(binding.server, binding.tool.name, arguments)
        results, raw_count = extract_results(binding, result)
    except ToolCallError as exc:
        outcome['reason'] = exc.reason
        outcome['error'] = exc.message[:300]
    except Exception as exc:  # noqa: BLE001 - 统一归类为可读原因码
        logger.info('MCP 搜索调用失败 %s：%s', binding, exc)
        outcome['reason'] = _reason_from_exception(exc)
        outcome['error'] = f'{type(exc).__name__}: {exc}'[:300]
    else:
        outcome['results'] = results
        outcome['raw_count'] = raw_count
        if not results:
            outcome['reason'] = 'mcp_empty'
    outcome['elapsed_ms'] = int((time.monotonic() - started) * 1000)
    return outcome


def _audit(actor, binding, query_count: int, result_count: int,
           elapsed_ms: int, reason: str) -> None:
    target = f'{binding.server.name}/{binding.tool.name}' if binding else 'MCP 搜索'
    log_audit(actor=actor, action='tiangong_search_mcp_call', target=target[:100],
              detail=(f'bindings=1；queries={query_count}；results={result_count}；'
                      f'elapsed_ms={elapsed_ms}；reason={reason or "ok"}'))


def search(queries, actor=None, max_results: int = 5) -> tuple:
    """按绑定优先级调用 MCP 搜索，返回 (results, reason, elapsed_ms)

    命中即停；失败/空结果换下一个绑定，全部失败后交回 L2。
    """
    queries = [str(q).strip() for q in (queries or []) if str(q).strip()]
    bindings = list(TiangongSearchBinding.objects
                    .select_related('server', 'tool')
                    .order_by('priority', 'id'))
    if not bindings:
        return [], 'mcp_not_configured', 0
    started = time.monotonic()
    last_reason = ''
    last_binding = None
    seen = set()
    for binding in bindings:
        last_binding = binding
        if not binding.usable:
            last_reason = binding.unusable_reason
            continue
        collected = []
        failure = ''
        for query in queries:
            outcome = call_binding(binding, query, binding.default_limit)
            if outcome['reason']:
                failure = outcome['reason']
                break
            collected.extend(outcome['results'])
            if len(collected) >= max_results:
                break
        if collected:
            results = []
            for item in collected:
                if item['url'] in seen:
                    continue
                seen.add(item['url'])
                results.append(item)
                if len(results) >= max_results:
                    break
            elapsed = int((time.monotonic() - started) * 1000)
            _audit(actor, binding, len(queries), len(results), elapsed, '')
            return results, '', elapsed
        last_reason = failure or 'mcp_empty'
    elapsed = int((time.monotonic() - started) * 1000)
    _audit(actor, last_binding, len(queries), 0, elapsed, last_reason or 'mcp_empty')
    return [], last_reason or 'mcp_empty', elapsed


def test_binding(binding: TiangongSearchBinding, query: str = '测试', actor=None) -> dict:
    """测试绑定并把结果写回最近状态；不修改 MCP 工具开关"""
    outcome = call_binding(binding, query or '测试', binding.default_limit)
    binding.last_status = 'ok' if outcome['results'] else 'failed'
    binding.last_error = (outcome.get('error') or outcome.get('reason') or '')[:300]
    binding.last_latency_ms = outcome['elapsed_ms']
    binding.last_result_count = len(outcome['results'])
    binding.last_checked_at = timezone.now()
    binding.save(update_fields=['last_status', 'last_error', 'last_latency_ms',
                                'last_result_count', 'last_checked_at', 'updated_at'])
    log_audit(actor=actor, action='tiangong_search_binding_test',
              target=str(binding)[:100],
              detail=(f'query={query[:60]}；results={len(outcome["results"])}；'
                      f'elapsed_ms={outcome["elapsed_ms"]}；'
                      f'reason={outcome["reason"] or "ok"}'))
    return outcome
