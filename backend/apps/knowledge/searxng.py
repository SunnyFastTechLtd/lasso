"""天工 L2：SearXNG 检索客户端

只调用本机实例（默认 127.0.0.1:8888）的 JSON 接口，结果只用于生成候选来源提案，
不抓正文、不写 Document。

熔断：连续 3 次失败或空结果 → 打开 15 分钟，期间直接返回 `searxng_circuit_open`，
不再对实例发请求；恢复后计数清零。
"""
import json
import logging
import time
from collections import Counter
from datetime import datetime, timedelta

from django.utils import timezone

from backend.apps.nuwa import knowledge_fetch, net
from backend.apps.settings.models import SystemSetting

logger = logging.getLogger(__name__)

KEY_ENABLED = 'tiangong_search_searxng_enabled'
KEY_URL = 'tiangong_searxng_url'
KEY_TIMEOUT = 'tiangong_searxng_timeout'
KEY_MAX_QUERIES = 'tiangong_search_max_queries'
KEY_MAX_RESULTS = 'tiangong_search_max_results'
KEY_MIN_RESULTS = 'tiangong_search_min_results'
KEY_FAIL_COUNT = 'tiangong_searxng_fail_count'
KEY_OPEN_UNTIL = 'tiangong_searxng_open_until'

DEFAULT_URL = 'http://127.0.0.1:8888'
DEFAULT_TIMEOUT = 15
DEFAULT_MAX_QUERIES = 3
DEFAULT_MAX_RESULTS = 5
DEFAULT_MIN_RESULTS = 1
PER_QUERY_TIMEOUT = 10
CIRCUIT_THRESHOLD = 3
CIRCUIT_MINUTES = 15
MAX_RESULTS_CAP = 10


def _int(key: str, default: int, low: int, high: int) -> int:
    raw = SystemSetting.get_value(key, str(default))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def config() -> dict:
    raw = str(SystemSetting.get_value(KEY_ENABLED, 'true')).strip().lower()
    return {
        'enabled': raw in ('1', 'true', 'yes', 'on'),
        'url': (SystemSetting.get_value(KEY_URL, DEFAULT_URL) or DEFAULT_URL).rstrip('/'),
        'timeout': _int(KEY_TIMEOUT, DEFAULT_TIMEOUT, 3, 60),
        'max_queries': _int(KEY_MAX_QUERIES, DEFAULT_MAX_QUERIES, 1, 10),
        'max_results': _int(KEY_MAX_RESULTS, DEFAULT_MAX_RESULTS, 1, MAX_RESULTS_CAP),
        'min_results': _int(KEY_MIN_RESULTS, DEFAULT_MIN_RESULTS, 1, MAX_RESULTS_CAP),
    }


def circuit_state() -> dict:
    """当前熔断状态：打开到什么时候、连续失败次数"""
    open_until = SystemSetting.get_value(KEY_OPEN_UNTIL, '')
    opened = False
    if open_until:
        try:
            opened = datetime.fromisoformat(open_until) > timezone.now()
        except (TypeError, ValueError):
            opened = False
    return {
        'open': opened,
        'open_until': open_until if opened else '',
        'fail_count': _int(KEY_FAIL_COUNT, 0, 0, 99),
        'threshold': CIRCUIT_THRESHOLD,
        'window_minutes': CIRCUIT_MINUTES,
    }


def reset_circuit(actor=None) -> dict:
    SystemSetting.set_value(KEY_FAIL_COUNT, '0', 'SearXNG 连续失败计数')
    SystemSetting.set_value(KEY_OPEN_UNTIL, '', 'SearXNG 熔断截止时间')
    if actor is not None:
        from backend.apps.users.models import log_audit

        log_audit(actor=actor, action='tiangong_search_settings_update',
                  target='SearXNG 熔断', detail='管理员手动重置熔断')
    return circuit_state()


def _record_failure():
    count = _int(KEY_FAIL_COUNT, 0, 0, 99) + 1
    SystemSetting.set_value(KEY_FAIL_COUNT, str(count), 'SearXNG 连续失败计数')
    if count >= CIRCUIT_THRESHOLD:
        until = (timezone.now() + timedelta(minutes=CIRCUIT_MINUTES)).isoformat()
        SystemSetting.set_value(KEY_OPEN_UNTIL, until, 'SearXNG 熔断截止时间')


def _record_success():
    SystemSetting.set_value(KEY_FAIL_COUNT, '0', 'SearXNG 连续失败计数')
    SystemSetting.set_value(KEY_OPEN_UNTIL, '', 'SearXNG 熔断截止时间')


def _reason_of(exc: BaseException) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return 'searxng_parse_error'
    if isinstance(exc, net.RemoteError):
        return {
            'timeout': 'searxng_timeout',
            'proxy_error': 'searxng_unreachable',
            'network_error': 'searxng_unreachable',
            'http_error': 'searxng_http_error',
            'rate_limited': 'searxng_http_error',
            'too_large': 'searxng_parse_error',
        }.get(exc.reason, 'searxng_unreachable')
    return 'searxng_unreachable'


def _search_url(base: str, query: str) -> str:
    from urllib.parse import urlencode

    params = urlencode({
        'q': query,
        'format': 'json',
        'language': 'zh-CN',
        'categories': 'general',
        'pageno': 1,
        'safesearch': 0,
    })
    return f'{base}/search?{params}'


def _normalize(item: dict) -> dict:
    url = (item.get('url') or '').strip()
    ok, _reason = net.is_public_https(url)
    if not ok:
        return {}
    normalized = knowledge_fetch.normalize_url(url)
    if not normalized.startswith('https://'):
        return {}
    return {
        'url': normalized,
        'title': (item.get('title') or normalized)[:500],
        'snippet': (item.get('content') or '')[:1000],
        'engine': (item.get('engine') or '')[:50],
        'score': float(item.get('score') or 0),
        'domain': net.host_of(normalized),
    }


def search(queries) -> dict:
    """按查询串行检索，返回 {results, reason, queries, elapsed_ms, engines}

    任何一层失败都转成可读原因码；熔断打开时不再发请求。
    """
    cfg = config()
    result = {'results': [], 'reason': '', 'queries': 0, 'elapsed_ms': 0, 'engines': {}}
    if not cfg['enabled']:
        result['reason'] = 'searxng_disabled'
        return result
    queries = [str(q).strip() for q in (queries or []) if str(q).strip()]
    queries = queries[:cfg['max_queries']]
    result['queries'] = len(queries)
    if not queries:
        result['reason'] = 'searxng_empty'
        return result
    state = circuit_state()
    if state['open']:
        result['reason'] = 'searxng_circuit_open'
        return result

    started = time.monotonic()
    deadline = started + cfg['timeout']
    seen = set()
    engines = Counter()
    last_reason = ''
    for query in queries:
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            last_reason = last_reason or 'searxng_timeout'
            break
        try:
            raw, _headers = net.http_get(
                _search_url(cfg['url'], query),
                timeout=max(1, min(PER_QUERY_TIMEOUT, int(remaining))),
                max_bytes=2 * 1024 * 1024,
                headers={'Accept': 'application/json'},
            )
            payload = json.loads(raw.decode('utf-8', errors='replace'))
        except (net.RemoteError, json.JSONDecodeError) as exc:
            last_reason = _reason_of(exc)
            logger.info('SearXNG 检索失败（%s）：%s', query, last_reason)
            continue
        if not isinstance(payload, dict):
            last_reason = 'searxng_parse_error'
            continue
        for item in payload.get('results') or []:
            if len(result['results']) >= cfg['max_results']:
                break
            if not isinstance(item, dict):
                continue
            normalized = _normalize(item)
            if not normalized or normalized['url'] in seen:
                continue
            seen.add(normalized['url'])
            result['results'].append(normalized)
            if normalized['engine']:
                engines[normalized['engine']] += 1
        if len(result['results']) >= cfg['max_results']:
            break

    result['elapsed_ms'] = int((time.monotonic() - started) * 1000)
    result['engines'] = dict(engines.most_common(3))
    if result['results']:
        _record_success()
        return result

    result['reason'] = last_reason or 'searxng_empty'
    _record_failure()
    return result


def _http_probe(url: str, timeout: int) -> tuple:
    started = time.monotonic()
    try:
        raw, _headers = net.http_get(url, timeout=timeout, max_bytes=512 * 1024,
                                     headers={'Accept': 'application/json'})
        return True, raw, int((time.monotonic() - started) * 1000), ''
    except net.RemoteError as exc:
        return False, b'', int((time.monotonic() - started) * 1000), _reason_of(exc)


def health_check() -> dict:
    """先探 /healthz（2 秒），404/不存在时退化为一次 JSON 检索"""
    cfg = config()
    ok, raw, elapsed, reason = _http_probe(f'{cfg["url"]}/healthz', 2)
    detail = ''
    if ok:
        detail = raw.decode('utf-8', errors='replace').strip()[:80]
    else:
        ok, raw, elapsed, reason = _http_probe(_search_url(cfg['url'], 'ping'), 5)
        if ok:
            try:
                payload = json.loads(raw.decode('utf-8', errors='replace'))
                detail = f'JSON 检索可用，返回 {len(payload.get("results") or [])} 条'
            except json.JSONDecodeError:
                ok = False
                reason = 'searxng_parse_error'
    return {
        'ok': ok,
        'url': cfg['url'],
        'elapsed_ms': elapsed,
        'detail': detail,
        'reason': reason,
        'circuit': circuit_state(),
    }
