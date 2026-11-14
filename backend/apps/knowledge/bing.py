"""天工 L3：Bing 无头浏览器兜底

严格触发：总开关 + L3 开关（默认关闭）+ L1/L2 都无有效结果 + 熔断未打开 +
当日额度未用完 + 浏览器可用。结果只生成候选提案（discovered_by='bing'），
绝不抓正文入库；L3 失败不影响 L4 来源库抓取。

合规：只访问 Bing 网页搜索，低频、限速、统一 UA、GET、无 Cookie；
不绕过验证码，检测到即记 `bing_captcha` 并当日熔断。
"""
import base64
import logging
import os
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, parse_qsl, quote_plus, urlencode, urlparse, urlunparse

from django.utils import timezone

from backend.apps.nuwa import knowledge_fetch, net
from backend.apps.settings.models import SystemSetting

logger = logging.getLogger(__name__)

try:  # Playwright 是部署前置，未安装时走可读降级，不影响其他搜索层
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    PLAYWRIGHT_AVAILABLE = True
except Exception:  # noqa: BLE001 - 缺依赖只影响 L3
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None
    PLAYWRIGHT_AVAILABLE = False

KEY_ENABLED = 'tiangong_search_bing_enabled'
KEY_DAILY_CAP = 'tiangong_search_bing_daily_cap'
KEY_MAX_QUERIES = 'tiangong_search_bing_max_queries'
KEY_MAX_RESULTS = 'tiangong_search_bing_max_results'
KEY_TIMEOUT = 'tiangong_search_bing_timeout'
KEY_INTERVAL = 'tiangong_search_bing_interval'
KEY_FAIL_COUNT = 'tiangong_search_bing_fail_count'
KEY_OPEN_UNTIL = 'tiangong_search_bing_open_until'
KEY_DAILY_DATE = 'tiangong_search_bing_daily_date'
KEY_DAILY_COUNT = 'tiangong_search_bing_daily_count'
KEY_LAST_STATUS = 'tiangong_search_bing_last_status'
KEY_LAST_ERROR = 'tiangong_search_bing_last_error'
KEY_LAST_AT = 'tiangong_search_bing_last_at'

DEFAULT_DAILY_CAP = 50
DEFAULT_MAX_QUERIES = 3
DEFAULT_MAX_RESULTS = 5
DEFAULT_TIMEOUT = 30
DEFAULT_INTERVAL = 1
CIRCUIT_HOURS = 24
CIRCUIT_THRESHOLD = 3
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0 Safari/537.36 Lasso-Tiangong/1.0')
SEARCH_URL = 'https://www.bing.com/search?q={query}&setlang=zh-Hans&cc=CN'
BING_RESULT_SELECTOR = '#b_results > li.b_algo'
BING_EXTRACT_JS = """(nodes) => nodes.map(li => {
  if (li.className && /\\bb_ad\\b/.test(li.className)) return null;
  const a = li.querySelector('h2 a');
  const p = li.querySelector('.b_caption p') || li.querySelector('.b_snippet')
    || li.querySelector('p');
  return a ? {href: a.href || '', title: (a.innerText || '').trim(),
              snippet: p ? (p.innerText || '').trim() : ''} : null;
})"""
CAPTCHA_MARKS = ('captcha', 'verify you are human', 'unusual traffic',
                 '验证码', '人机验证', '请输入验证码')
NO_RESULT_MARKS = ('没有与此相关的结果', 'there are no results', '未找到结果')
TRACKING_KEYS = ('gclid', 'fbclid', 'msclkid', 'yclid', 'mc_cid', 'mc_eid',
                 'spm', 'from', 'ref', 'ref_src', 'ved', 'ei', '_openstat')

_browser_cache = {'checked_at': 0.0, 'ok': False, 'reason': '', 'detail': ''}


def _int(key: str, default: int, low: int, high: int) -> int:
    raw = SystemSetting.get_value(key, str(default))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def config() -> dict:
    raw = str(SystemSetting.get_value(KEY_ENABLED, 'false')).strip().lower()
    return {
        'enabled': raw in ('1', 'true', 'yes', 'on'),
        'daily_cap': _int(KEY_DAILY_CAP, DEFAULT_DAILY_CAP, 1, 200),
        'max_queries': _int(KEY_MAX_QUERIES, DEFAULT_MAX_QUERIES, 1, 3),
        'max_results': _int(KEY_MAX_RESULTS, DEFAULT_MAX_RESULTS, 1, 10),
        'timeout': _int(KEY_TIMEOUT, DEFAULT_TIMEOUT, 10, 60),
        'interval': _int(KEY_INTERVAL, DEFAULT_INTERVAL, 1, 5),
    }


def _today() -> str:
    return timezone.localdate().isoformat()


def daily_used() -> int:
    if SystemSetting.get_value(KEY_DAILY_DATE, '') != _today():
        return 0
    return _int(KEY_DAILY_COUNT, 0, 0, 9999)


def _bump_daily(used: int) -> None:
    SystemSetting.set_value(KEY_DAILY_DATE, _today(), 'Bing 当日调用日期')
    SystemSetting.set_value(KEY_DAILY_COUNT, str(daily_used() + max(0, used)),
                            'Bing 当日调用次数')


def circuit_state() -> dict:
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
        'hours': CIRCUIT_HOURS,
    }


def _open_circuit() -> None:
    until = (timezone.now() + timedelta(hours=CIRCUIT_HOURS)).isoformat()
    SystemSetting.set_value(KEY_OPEN_UNTIL, until, 'Bing 熔断截止时间')


def _record_failure(immediate: bool = False) -> None:
    count = _int(KEY_FAIL_COUNT, 0, 0, 99) + 1
    SystemSetting.set_value(KEY_FAIL_COUNT, str(count), 'Bing 连续失败计数')
    if immediate or count >= CIRCUIT_THRESHOLD:
        _open_circuit()


def _record_success() -> None:
    SystemSetting.set_value(KEY_FAIL_COUNT, '0', 'Bing 连续失败计数')
    SystemSetting.set_value(KEY_OPEN_UNTIL, '', 'Bing 熔断截止时间')


def reset_circuit() -> dict:
    _record_success()
    return circuit_state()


def _set_last(status: str, error: str = '') -> None:
    SystemSetting.set_value(KEY_LAST_STATUS, status, 'Bing 最近运行状态')
    SystemSetting.set_value(KEY_LAST_ERROR, (error or '')[:200], 'Bing 最近失败原因')
    SystemSetting.set_value(KEY_LAST_AT, timezone.now().isoformat(), 'Bing 最近运行时间')


def state() -> dict:
    """设置页展示：开关、可用性、熔断、当日额度与最近结果"""
    available, reason, detail = _browser_available()
    return {
        **config(),
        'available': available,
        'unavailable_reason': reason,
        'unavailable_detail': detail,
        'circuit': circuit_state(),
        'daily_used': daily_used(),
        'last_status': SystemSetting.get_value(KEY_LAST_STATUS, ''),
        'last_error': SystemSetting.get_value(KEY_LAST_ERROR, ''),
        'last_at': SystemSetting.get_value(KEY_LAST_AT, ''),
    }


def _browser_available(force: bool = False) -> tuple:
    """轻量健康检查：包已安装 + Chromium 可执行文件存在；缓存 120 秒"""
    now = time.monotonic()
    if not force and now - _browser_cache['checked_at'] < 120:
        return _browser_cache['ok'], _browser_cache['reason'], _browser_cache['detail']
    if not PLAYWRIGHT_AVAILABLE:
        result = (False, 'bing_unavailable',
                  '未安装 playwright（部署前置见 deploy/install_playwright.sh）')
    else:
        try:
            with sync_playwright() as pw:
                executable = pw.chromium.executable_path
        except Exception as exc:  # noqa: BLE001 - 缺浏览器只影响 L3
            result = (False, 'bing_browser_missing',
                      f'{type(exc).__name__}: {exc}'[:200])
        else:
            if executable and os.path.exists(executable):
                result = (True, '', executable)
            else:
                result = (False, 'bing_browser_missing',
                          f'Chromium 未安装：{executable or "-"}')
    _browser_cache.update({'checked_at': now, 'ok': result[0],
                           'reason': result[1], 'detail': result[2]})
    return result


def health_check() -> dict:
    """显式健康检查：真正启动一次 headless Chromium 后关闭"""
    started = time.monotonic()
    available, reason, detail = _browser_available(force=True)
    info = {
        'ok': False,
        'reason': reason,
        'detail': detail,
        'executable': detail if available else '',
        'elapsed_ms': 0,
        'circuit': circuit_state(),
        'daily_used': daily_used(),
        'daily_cap': config()['daily_cap'],
    }
    if not available:
        info['elapsed_ms'] = int((time.monotonic() - started) * 1000)
        return info
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, timeout=15000)
            browser.close()
    except Exception as exc:  # noqa: BLE001 - 健康检查失败如实回报
        info['reason'] = 'bing_health_failed'
        info['detail'] = f'{type(exc).__name__}: {exc}'[:200]
    else:
        info['ok'] = True
        info['detail'] = 'Chromium 可启动'
    info['elapsed_ms'] = int((time.monotonic() - started) * 1000)
    return info


# ─── 结果解析 ─────────────────────────────────────────────────────

def _decode_bing_url(href: str) -> str:
    """还原 Bing 跳转链接 u=a1<base64>，并剔除 Bing 自身页面"""
    if not href:
        return ''
    parsed = urlparse(href)
    host = (parsed.hostname or '').lower()
    if host.endswith('bing.com') and parsed.path.startswith('/ck/a'):
        value = parse_qs(parsed.query).get('u', [''])[0]
        payload = value[2:] if value.startswith('a1') else value
        payload += '=' * (-len(payload) % 4)
        try:
            decoded = base64.urlsafe_b64decode(payload).decode('utf-8', 'replace')
        except Exception:  # noqa: BLE001 - 非法跳转链接直接丢弃
            return ''
        if not decoded.startswith(('http://', 'https://')):
            return ''
        href = decoded
        parsed = urlparse(href)
        host = (parsed.hostname or '').lower()
    if host.endswith('bing.com'):
        return ''
    return href


def _clean_url(url: str) -> str:
    """清理跟踪参数与 fragment；保留业务查询参数"""
    parsed = urlparse(url)
    pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith('utm_') or lowered in TRACKING_KEYS:
            continue
        pairs.append((key, value))
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, '',
                       urlencode(pairs, doseq=True), ''))


def _normalize_url(url: str) -> str:
    if not url:
        return ''
    ok, _reason = net.is_public_https(url)
    if not ok:
        return ''
    normalized = knowledge_fetch.normalize_url(url)
    return normalized if normalized.startswith('https://') else ''


def _normalize_items(items, max_results: int) -> list:
    """把浏览器抽取的原始结果映射为统一候选；按 URL 去重"""
    results = []
    seen = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        raw = _decode_bing_url((item.get('href') or '').strip())
        url = _normalize_url(_clean_url(raw)) if raw else ''
        if not url or url in seen:
            continue
        seen.add(url)
        results.append({
            'url': url,
            'title': ((item.get('title') or url).strip() or url)[:500],
            'snippet': (item.get('snippet') or '').strip()[:1000],
            'score': 0.0,
            'domain': net.host_of(url),
            'engine': 'bing',
        })
        if len(results) >= max_results:
            break
    return results


def _is_captcha(text: str) -> bool:
    lowered = (text or '').lower()
    return any(mark in lowered for mark in CAPTCHA_MARKS)


def _run_browser(queries, cfg: dict) -> dict:
    """同步 Playwright 执行；只返回内存结果，不保存页面 HTML"""
    outcome = {'results': [], 'reason': '', 'queries_used': 0}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, timeout=cfg['timeout'] * 1000)
        context = None
        try:
            context = browser.new_context(
                user_agent=USER_AGENT, locale='zh-CN',
                viewport={'width': 1366, 'height': 900},
            )
            page = context.new_page()
            page.set_default_timeout(cfg['timeout'] * 1000)
            seen = set()
            for index, query in enumerate(queries):
                if index:
                    time.sleep(cfg['interval'])
                outcome['queries_used'] += 1
                url = SEARCH_URL.format(query=quote_plus(query))
                try:
                    response = page.goto(url, wait_until='domcontentloaded',
                                         timeout=cfg['timeout'] * 1000)
                except PlaywrightTimeoutError:
                    outcome['reason'] = 'bing_timeout'
                    continue
                status = getattr(response, 'status', 0) if response else 0
                if status == 429:
                    outcome['reason'] = 'bing_rate_limited'
                    return outcome
                if status == 403:
                    outcome['reason'] = 'bing_blocked'
                    continue
                body = (page.inner_text('body') or '')[:4000]
                if _is_captcha(f'{page.title() or ""} {body}'):
                    outcome['reason'] = 'bing_captcha'
                    return outcome
                raw_items = page.eval_on_selector_all(BING_RESULT_SELECTOR, BING_EXTRACT_JS)
                normalized = _normalize_items(raw_items, cfg['max_results'])
                if normalized:
                    for item in normalized:
                        if item['url'] in seen:
                            continue
                        seen.add(item['url'])
                        outcome['results'].append(item)
                    outcome['reason'] = ''
                elif any(mark in body.lower() for mark in NO_RESULT_MARKS):
                    outcome['reason'] = 'bing_empty'
                else:
                    outcome['reason'] = 'bing_parse_error'
            return outcome
        finally:
            if context is not None:
                context.close()
            browser.close()


def search(queries, actor=None) -> tuple:
    """L3 入口：返回 (results, reason, elapsed_ms)；任何失败都不抛异常"""
    cfg = config()
    started = time.monotonic()
    if not cfg['enabled']:
        return [], 'bing_disabled', 0
    if circuit_state()['open']:
        return [], 'bing_circuit_open', 0
    if daily_used() >= cfg['daily_cap']:
        return [], 'bing_daily_limit', 0
    available, reason, detail = _browser_available()
    if not available:
        _set_last('failed', f'{reason}：{detail}')
        return [], reason, int((time.monotonic() - started) * 1000)

    cleaned = [str(q).strip() for q in (queries or []) if str(q).strip()]
    cleaned = cleaned[:cfg['max_queries']]
    if not cleaned:
        return [], 'bing_empty', 0
    try:
        outcome = _run_browser(cleaned, cfg)
    except Exception as exc:  # noqa: BLE001 - 浏览器异常只影响 L3
        logger.info('Bing 浏览器执行失败：%s', exc)
        _bump_daily(len(cleaned))
        _record_failure()
        _set_last('failed', f'bing_browser_error：{type(exc).__name__}: {exc}'[:200])
        return [], 'bing_browser_error', int((time.monotonic() - started) * 1000)

    _bump_daily(outcome['queries_used'])
    results = outcome['results']
    reason = outcome['reason']
    if reason == 'bing_captcha':
        _record_failure(immediate=True)
        _set_last('failed', 'bing_captcha')
        return [], reason, int((time.monotonic() - started) * 1000)
    if reason == 'bing_rate_limited':
        _record_failure(immediate=True)
        _set_last('failed', reason)
        return [], reason, int((time.monotonic() - started) * 1000)
    if results:
        _record_success()
        _set_last('ok', '')
        return results, '', int((time.monotonic() - started) * 1000)
    _record_failure()
    _set_last('failed', reason or 'bing_empty')
    return [], reason or 'bing_empty', int((time.monotonic() - started) * 1000)
