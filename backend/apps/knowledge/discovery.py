"""天工 L4：来源库正文抓取

只从「已启用来源站 + 知识库临时采集入口」抓取。搜索层（L1/L2/L3）的结果
必须先经管理员审批写入来源库，本模块不会读取候选提案直接抓正文。

链路：robots + Crawl-delay → sitemap（索引展开 1 层）→ 站内 1~2 层链接 →
质量过滤 → 主题打分 → 单主机串行 + 页数硬上限。

抓取结果只返回内存中的 page dict，由 tiangong.py 决定是否入库；本模块不写 Document。
"""
import hashlib
import html as html_lib
import logging
import re
import socket
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse

from backend.apps.nuwa import knowledge_fetch, net

logger = logging.getLogger(__name__)

# 天工抓取统一 UA：与女娲的 UA 区分，便于目标站点按 robots 分组识别
USER_AGENT = 'Lasso-Tiangong/1.0 (enterprise agent platform; curated source crawl)'
DEFAULT_DELAY = 1.0
MAX_DELAY = 5.0
MAX_PAGES_PER_SOURCE = 3
MAX_PAGES_PER_RUN = 8
HARD_PAGES_PER_RUN = 10
MAX_SITEMAP_BYTES = 1024 * 1024
MAX_SITEMAP_LOCS = 200
MAX_SITEMAP_CHILDREN = 3
MAX_LINKS_PER_PAGE = 80
MIN_TOPIC_SCORE = 2

SKIP_SUFFIXES = (
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico', '.css', '.js',
    '.zip', '.rar', '.7z', '.gz', '.tar', '.exe', '.dmg', '.apk',
    '.mp3', '.mp4', '.avi', '.mov', '.pdf', '.doc', '.docx', '.xls', '.xlsx',
)

# 主题词后缀：中文主题常见「工伤认定流程」这类写法，去掉通用后缀才匹配得上标题
TOPIC_SUFFIXES = (
    '有哪些', '是什么', '怎么做', '最新', '政策', '规定', '办法', '通知',
    '指南', '流程', '标准', '要求', '细则', '解读', '问答',
)

_robots_cache: dict = {}
_last_hit: dict = {}


class CrawlBudget:
    """单轮抓取页数预算：来源站之间共享，超过硬上限直接停止"""

    def __init__(self, max_pages: int = MAX_PAGES_PER_RUN):
        try:
            limit = int(max_pages)
        except (TypeError, ValueError):
            limit = MAX_PAGES_PER_RUN
        self.max_pages = max(1, min(limit, HARD_PAGES_PER_RUN))
        self.used = 0

    def remaining(self) -> int:
        return max(0, self.max_pages - self.used)

    def take(self) -> bool:
        if self.remaining() <= 0:
            return False
        self.used += 1
        return True


def keywords_of(topics: str) -> list:
    """把主题文本拆成可匹配的关键词；去重后最多 10 个"""
    words = []
    for part in re.split(r'[,，、;；\s]+', topics or ''):
        part = part.strip()
        if len(part) < 2:
            continue
        candidates = [part]
        for suffix in TOPIC_SUFFIXES:
            if part.endswith(suffix) and len(part) - len(suffix) >= 2:
                candidates.append(part[: -len(suffix)])
        for candidate in candidates:
            if candidate and candidate not in words:
                words.append(candidate)
    return words[:10]


def topic_score(keywords, *, url: str = '', title: str = '', anchor: str = '',
                body: str = '', tags: str = '') -> int:
    """主题打分：URL 2 分、锚文本 2 分、标题 3 分、正文 1 分、来源标签 1 分"""
    if not keywords:
        return 0
    hay_url = (url or '').lower()
    hay_anchor = (anchor or '').lower()
    hay_title = (title or '').lower()
    hay_body = (body or '').lower()
    hay_tags = (tags or '').lower()
    score = 0
    for keyword in keywords:
        word = keyword.lower()
        if word in hay_url:
            score += 2
        if word in hay_anchor:
            score += 2
        if word in hay_title:
            score += 3
        if word in hay_body:
            score += 1
        if word in hay_tags:
            score += 1
    return score


def source_matches(tags: str, keywords) -> bool:
    """来源标签与主题无交集时跳过整站，避免一个通用库被无关来源污染"""
    if not keywords or not tags:
        return True
    tag_list = [tag.strip().lower() for tag in re.split(r'[,，、;；\s]+', tags) if tag.strip()]
    for keyword in keywords:
        word = keyword.lower()
        for tag in tag_list:
            if word in tag or tag in word:
                return True
    return False


def quality_issue(*, url: str, title: str, text: str, html: str = '',
                  link_count: int = 0) -> str:
    """质量过滤：返回可读原因；空串表示通过"""
    if len(text) < 80:
        return '正文过短'
    if len(text) >= 200 and text.count('\ufffd') / len(text) > 0.02:
        return '乱码比例过高'
    if link_count > 60 and link_count * 30 > len(text):
        return '链接密度过高'
    low_title = (title or '').lower()
    if any(mark in low_title for mark in ('404', '500', '页面不存在', 'not found', 'error')):
        return '疑似错误页'
    head = (low_title + text[:200]).lower()
    if len(text) < 600 and any(mark in head for mark in ('请登录', 'login required', 'sign in')):
        return '疑似登录页'
    if len(text) < 200 and len(re.findall(r'<script\b', html or '', re.I)) >= 3:
        return '疑似 JS 空壳'
    return ''


def _proxy_for(url: str):
    """天工代理：精确域名 > 通配 > 来源站 > 直连；配置异常不阻断直连"""
    from backend.apps.settings import proxies

    try:
        proxy_url, _reason = proxies.tiangong_proxy_for_url(url)
    except Exception:  # noqa: BLE001 - 代理配置异常不能阻断正文抓取
        logger.exception('解析天工代理失败：%s', url)
        return None
    return proxy_url


def _robots(domain: str, scheme: str = 'https') -> tuple:
    """取 robots 解析器与 Crawl-delay；取不到按允许处理（与既有抓取口径一致）"""
    key = f'{scheme}://{domain}'
    if key in _robots_cache:
        return _robots_cache[key]
    parser = urllib.robotparser.RobotFileParser()
    delay = DEFAULT_DELAY
    try:
        raw, _ = net.http_get(f'{key}/robots.txt', timeout=5, max_bytes=64 * 1024,
                              headers={'User-Agent': USER_AGENT},
                              proxy=_proxy_for(f'{key}/'))
        parser.parse(raw.decode('utf-8', errors='replace').splitlines())
        declared = parser.crawl_delay(USER_AGENT) or parser.crawl_delay('*')
        if declared:
            delay = max(DEFAULT_DELAY, min(MAX_DELAY, float(declared)))
    except net.RemoteError:
        parser.parse([])
    except Exception:  # noqa: BLE001 - robots 解析异常按允许处理
        parser.parse([])
    result = (parser, delay)
    _robots_cache[key] = result
    return result


def _throttle(host: str, delay: float):
    last = _last_hit.get(host, 0.0)
    wait = delay - (time.monotonic() - last)
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.monotonic()


def _links_from(html: str, base_url: str, host: str) -> list:
    """提取同站链接与锚文本；只保留 http(s) 与页面地址"""
    pairs = re.findall(
        r'<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        html or '', re.S | re.I,
    )
    links = []
    for href, inner in pairs[:MAX_LINKS_PER_PAGE]:
        href = html_lib.unescape(href.strip())
        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            continue
        absolute = urljoin(base_url, href)
        if urlparse(absolute).scheme not in ('http', 'https'):
            continue
        if net.host_of(absolute) != host:
            continue
        anchor = re.sub(r'<[^>]+>', '', inner)
        anchor = html_lib.unescape(anchor).strip()[:80]
        links.append((knowledge_fetch.normalize_url(absolute), anchor))
    return links


def _dns_is_public(host: str) -> bool:
    """DNS 私网复验（仅 A 记录）

    只看 IPv4：有 A 记录且全部为公网才放行；A 记录为空或任一落在私网/保留段
    一律拒绝。AAAA 不参与判定，避免站点额外解析出非全局 IPv6 时被整体误拦；
    「内网一律拦」的底线不变（字面内网地址在 is_public_https 已先拦一次）。
    """
    from backend.apps.mcp.services import is_private_host, resolved_addresses

    try:
        addresses = resolved_addresses(host)
    except OSError:
        return False
    if not addresses:
        return False
    return not any(is_private_host(addr) for addr in addresses)


def fetch_page(url: str) -> dict:
    """抓取单页：公网/https → 白名单 → robots → 限速 → 正文清洗

    失败抛 net.RemoteError；返回含 text/content_sha256/links 的页面信息。
    """
    ok, reason = net.is_public_https(url)
    if not ok:
        raise net.RemoteError('blocked', reason)
    if not _dns_is_public(net.host_of(url)):
        raise net.RemoteError('blocked', '域名解析到内网地址，已按安全策略拒绝')
    allowed, reason = knowledge_fetch.domain_allowed(url)
    if not allowed:
        raise net.RemoteError('blocked', reason)
    parsed_url = urlparse(url)
    host = net.host_of(url)
    parser, delay = _robots(host, parsed_url.scheme or 'https')
    if not parser.can_fetch(USER_AGENT, url):
        raise net.RemoteError('blocked', 'robots.txt 禁止抓取该路径')
    if not _looks_like_html(url):
        raise net.RemoteError('blocked', '非网页类型，已跳过')
    _throttle(host, delay)
    raw, _headers = net.http_get(url, timeout=net.PAGE_TIMEOUT,
                                 max_bytes=knowledge_fetch.MAX_TEXT_BYTES * 2,
                                 headers={'User-Agent': USER_AGENT},
                                 proxy=_proxy_for(url))
    html = raw.decode('utf-8', errors='replace')
    parsed = knowledge_fetch.parse_html(html)
    body = parsed['text']
    if len(body) < 80:
        raise net.RemoteError('empty_content', '页面没有可用的正文（可能是 JS 渲染或空页）')
    return {
        'url': url,
        'title': (parsed['title'] or url)[:200],
        'text': body[:knowledge_fetch.MAX_TEXT_BYTES],
        'html': html,
        'content_sha256': hashlib.sha256(body.encode('utf-8')).hexdigest(),
        'site': host,
        'links': _links_from(html, url, host),
        'link_count': len(re.findall(r'<a\b', html, re.I)),
    }


def _looks_like_html(url: str) -> bool:
    path = (urlparse(url).path or '').lower()
    return not path.endswith(SKIP_SUFFIXES)


def _same_host(url: str, domain: str) -> bool:
    host = net.host_of(url)
    return host == domain or host.endswith('.' + domain)


def _sitemap_locs(domain: str, scheme: str = 'https') -> list:
    """sitemap 发现：普通 sitemap 或索引展开 1 层（最多 3 个子 sitemap，200 条）"""
    locs = []
    for path in ('/sitemap.xml', '/sitemap_index.xml'):
        try:
            raw, _ = net.http_get(f'{scheme}://{domain}{path}', timeout=8,
                                  max_bytes=MAX_SITEMAP_BYTES,
                                  headers={'User-Agent': USER_AGENT},
                                  proxy=_proxy_for(f'{scheme}://{domain}{path}'))
        except net.RemoteError:
            continue
        text = raw.decode('utf-8', errors='replace')
        found = re.findall(r'<loc>\s*([^<\s]+)\s*</loc>', text)
        if '<sitemapindex' in text.lower():
            for child in found[:MAX_SITEMAP_CHILDREN]:
                try:
                    raw_child, _ = net.http_get(child, timeout=8,
                                                max_bytes=MAX_SITEMAP_BYTES,
                                                headers={'User-Agent': USER_AGENT},
                                                proxy=_proxy_for(child))
                except net.RemoteError:
                    continue
                locs.extend(re.findall(r'<loc>\s*([^<\s]+)\s*</loc>',
                                       raw_child.decode('utf-8', errors='replace')))
        else:
            locs.extend(found)
        if locs:
            break

    urls = []
    for loc in locs:
        loc = html_lib.unescape(loc.strip())
        if not loc or not _same_host(loc, domain):
            continue
        normalized = knowledge_fetch.normalize_url(loc)
        if not normalized.startswith('https://'):
            continue
        if not _looks_like_html(normalized):
            continue
        if normalized not in urls:
            urls.append(normalized)
        if len(urls) >= MAX_SITEMAP_LOCS:
            break
    return urls


def _entry_urls(source) -> list:
    urls = []
    for line in re.split(r'[\s,;]+', getattr(source, 'entry_urls', '') or ''):
        line = line.strip()
        if line.startswith(('http://', 'https://')):
            normalized = knowledge_fetch.normalize_url(line)
            if normalized and normalized not in urls:
                urls.append(normalized)
    if not urls:
        urls = [f'https://{(source.domain or "").strip().lower().rstrip("/")}/']
    return urls[:5]


def crawl_source(source, keywords, budget: CrawlBudget, *, existing_urls=None,
                 max_pages: int = MAX_PAGES_PER_SOURCE) -> dict:
    """抓取一个来源站：入口页优先，sitemap 按主题排序，站内链接 1~2 层

    返回 {pages, fetched, rejected, skipped, reason}；不写库。
    """
    existing = set(existing_urls or ())
    domain = (source.domain or '').strip().lower().rstrip('.')
    result = {'pages': [], 'fetched': 0, 'rejected': 0, 'skipped': 0, 'reason': ''}
    if not domain:
        result['reason'] = '来源缺少域名'
        return result

    rules = source.crawl_rules or {}
    try:
        rule_pages = int(rules.get('max_pages') or max_pages)
    except (TypeError, ValueError):
        rule_pages = max_pages
    # 来源级规则只能收紧或等于管理员上限，不能突破单来源硬上限
    per_source = max(1, min(max_pages, rule_pages, MAX_PAGES_PER_SOURCE))
    try:
        depth_limit = int(rules.get('max_depth') or 1)
    except (TypeError, ValueError):
        depth_limit = 1
    depth_limit = max(1, min(depth_limit, 2))
    tags = source.topic_tags or ''

    entries = _entry_urls(source)
    queue = [(url, 0, '', True) for url in entries]  # (url, depth, anchor, forced)
    for loc in _sitemap_locs(domain, 'https'):
        score = topic_score(keywords, url=loc, tags=tags)
        if keywords and score < MIN_TOPIC_SCORE:
            continue  # 有主题词时只把 sitemap 里相关的页面排进队列
        queue.append((loc, 1, '', False))

    seen = set()
    failures = []
    while queue and result['fetched'] < per_source and budget.remaining() > 0:
        url, depth, anchor, forced = queue.pop(0)
        if url in seen or url in existing:
            continue
        seen.add(url)
        if not _same_host(url, domain):
            continue
        try:
            page = fetch_page(url)
        except net.RemoteError as exc:
            failures.append(exc.message or exc.reason)
            result['skipped'] += 1
            continue
        issue = quality_issue(url=url, title=page['title'], text=page['text'],
                              html=page.get('html', ''), link_count=page.get('link_count', 0))
        if issue:
            result['rejected'] += 1
            continue
        score = topic_score(keywords, url=url, title=page['title'], anchor=anchor,
                            body=page['text'], tags=tags)
        if not forced and keywords and score < MIN_TOPIC_SCORE:
            result['rejected'] += 1
            continue
        if not budget.take():
            break
        result['fetched'] += 1
        page['score'] = score
        result['pages'].append(page)
        existing.add(url)

        if depth < depth_limit:
            for link_url, link_anchor in page.get('links', []):
                if link_url in seen:
                    continue
                next_score = topic_score(keywords, url=link_url, anchor=link_anchor, tags=tags)
                if keywords and next_score < MIN_TOPIC_SCORE:
                    continue
                if not keywords:
                    # 没有主题词时不扩展全站链接，只抓入口页
                    continue
                if depth + 1 >= 2 and next_score < 3:
                    continue
                queue.append((link_url, depth + 1, link_anchor, False))

    if failures and not result['pages']:
        result['reason'] = f'{len(failures)} 个页面抓取失败：{failures[0][:100]}'
    elif failures:
        result['reason'] = f'{len(failures)} 个页面抓取失败，已入库其余页面'
    return result


def probe_source(source) -> dict:
    """只读检查来源可达性：robots / Crawl-delay / sitemap 条目数"""
    domain = (source.domain or '').strip().lower().rstrip('.')
    if not domain:
        return {'ok': False, 'error': '来源缺少域名', 'robots_ok': False,
                'crawl_delay': DEFAULT_DELAY, 'sitemap_count': 0}
    try:
        parser, delay = _robots(domain, 'https')
        robots_ok = parser.can_fetch(USER_AGENT, f'https://{domain}/')
    except Exception as exc:  # noqa: BLE001 - 检查失败如实回报
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'[:200],
                'robots_ok': False, 'crawl_delay': DEFAULT_DELAY, 'sitemap_count': 0}
    count = 0
    if robots_ok:
        try:
            count = len(_sitemap_locs(domain, 'https'))
        except Exception:  # noqa: BLE001 - sitemap 缺失不等于来源不可达
            count = 0
    return {
        'ok': robots_ok,
        'robots_ok': robots_ok,
        'crawl_delay': delay,
        'sitemap_count': count,
        'error': '' if robots_ok else 'robots.txt 不允许抓取入口页',
    }
