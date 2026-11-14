"""资料抓取与入库

链路：URL 白名单/公网校验 → robots → 限速抓取 → 正文清洗 → 写临时文件 →
用户确认后建 Document 并同步跑既有向量化流水线。

抓取文本先落 MEDIA_ROOT/nuwa_tmp，不塞进会话 JSON，避免大字段拖慢列表查询。
"""
import hashlib
import html
import json
import logging
import re
import time
import urllib.robotparser
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

from backend.apps.knowledge.models import Document, KnowledgeBase
from backend.apps.knowledge.services import process_document

from backend.apps.nuwa import config, net

logger = logging.getLogger(__name__)

TRACKING_PARAMS = ('utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
                   'gclid', 'fbclid', 'spm', 'from')
MAX_ITEMS = 20
MAX_TEXT_BYTES = 200 * 1024
SAME_HOST_DELAY = 1.0

_robots_cache: dict = {}
_last_hit: dict = {}


class _TextExtractor(HTMLParser):
    """只取正文：跳过脚本/样式/导航/页脚等，保留标题"""

    SKIP_TAGS = {'script', 'style', 'noscript', 'nav', 'footer', 'header', 'aside', 'form'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.title = ''
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == 'title':
            self._in_title = True
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        if tag in ('p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'h4', 'tr'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag == 'title':
            self._in_title = False
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
            return
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        raw = ''.join(self.parts)
        raw = re.sub(r'[ \t\u3000]+', ' ', raw)
        raw = re.sub(r'\n{3,}', '\n\n', raw)
        return raw.strip()


def normalize_url(url: str) -> str:
    """去 fragment、去追踪参数、统一小写 host、去尾斜杠"""
    parsed = urlparse((url or '').strip())
    host = (parsed.hostname or '').lower()
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
    path = parsed.path.rstrip('/') or '/'
    return urlunparse((parsed.scheme.lower(), host, path,
                       parsed.params, urlencode(query), ''))


def domain_allowed(url: str) -> tuple:
    """白名单为空＝不限域名；非空＝只允许名单内域名及其子域"""
    host = net.host_of(url)
    allow = config.knowledge_allowlist()
    if not allow:
        return True, ''
    for entry in allow:
        if host == entry or host.endswith('.' + entry):
            return True, ''
    return False, f'{host} 不在管理员配置的抓取白名单内'


def _proxy_for(url: str) -> str | None:
    """按「精确域名 > 最左通配 > 来源站 > 直连」取代理；配置不可用时直连"""
    from backend.apps.settings import proxies

    try:
        proxy_url, _reason = proxies.tiangong_proxy_for_url(url)
    except Exception:  # noqa: BLE001 - 代理配置异常不能阻断正文抓取
        logger.exception('解析天工代理失败：%s', url)
        return None
    return proxy_url


def robots_allows(url: str) -> tuple:
    """遵守 robots.txt；取不到（404/超时）按允许处理并留原因"""
    parsed = urlparse(url)
    host = parsed.hostname or ''
    if host in _robots_cache:
        allowed = _robots_cache[host]
        return allowed, '' if allowed else 'robots.txt 禁止抓取该路径'
    parser = urllib.robotparser.RobotFileParser()
    try:
        raw, _ = net.http_get(f'{parsed.scheme}://{host}/robots.txt', timeout=5,
                              max_bytes=64 * 1024, proxy=_proxy_for(url))
        parser.parse(raw.decode('utf-8', errors='replace').splitlines())
    except net.RemoteError:
        _robots_cache[host] = True
        return True, ''
    allowed = parser.can_fetch(net.USER_AGENT, url)
    _robots_cache[host] = allowed
    return allowed, '' if allowed else 'robots.txt 禁止抓取该路径'


def _throttle(host: str):
    last = _last_hit.get(host, 0.0)
    wait = SAME_HOST_DELAY - (time.monotonic() - last)
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.monotonic()


def parse_html(text: str) -> dict:
    """从 HTML 提取标题与正文（女娲预览与天工来源抓取共用同一口径）"""
    parser = _TextExtractor()
    try:
        parser.feed(text)
        body = parser.text()
        title = parser.title
    except Exception:
        body, title = text, ''
    if not title:
        match = re.search(r'<title[^>]*>(.*?)</title>', text, re.S | re.I)
        title = html.unescape(match.group(1)).strip() if match else ''
    return {'title': title, 'text': body}


def fetch_page(url: str) -> dict:
    """抓取单页并清洗正文；失败抛 RemoteError（调用方转成逐条状态）"""
    ok, reason = net.is_public_https(url)
    if not ok:
        raise net.RemoteError('blocked', reason)
    allowed, reason = domain_allowed(url)
    if not allowed:
        raise net.RemoteError('blocked', reason)
    allowed, reason = robots_allows(url)
    if not allowed:
        raise net.RemoteError('blocked', reason)
    _throttle(net.host_of(url))
    raw, _headers = net.http_get(url, timeout=net.PAGE_TIMEOUT,
                                 max_bytes=MAX_TEXT_BYTES * 2, proxy=_proxy_for(url))
    text = raw.decode('utf-8', errors='replace')
    parsed = parse_html(text)
    body, title = parsed['text'], parsed['title']
    if len(body) < 80:
        raise net.RemoteError('empty_content', '页面没有可用的正文（可能是 JS 渲染或空页）')
    return {
        'title': (title or url)[:200],
        'text': body[:MAX_TEXT_BYTES],
        'content_sha256': hashlib.sha256(body.encode('utf-8')).hexdigest(),
        'site': net.host_of(url),
    }


def discover_same_host(url: str, limit: int = 10) -> list:
    """同域浅层发现：优先 sitemap.xml，失败则取页面内同域链接（深度 1）"""
    parsed = urlparse(url)
    host = parsed.hostname or ''
    found = []
    try:
        raw, _ = net.http_get(f'{parsed.scheme}://{host}/sitemap.xml', timeout=8,
                              max_bytes=1024 * 1024, proxy=_proxy_for(url))
        locations = re.findall(r'<loc>\s*([^<\s]+)\s*</loc>', raw.decode('utf-8', 'replace'))
        for loc in locations:
            if net.host_of(loc) == host and loc not in found:
                found.append(loc)
            if len(found) >= limit:
                break
    except net.RemoteError:
        pass
    if not found:
        try:
            raw, _ = net.http_get(url, timeout=net.PAGE_TIMEOUT,
                                  max_bytes=MAX_TEXT_BYTES, proxy=_proxy_for(url))
            links = re.findall(r'href=["\']([^"\']+)["\']', raw.decode('utf-8', 'replace'))
            for link in links:
                if link.startswith('//'):
                    link = f'{parsed.scheme}:{link}'
                elif link.startswith('/'):
                    link = f'{parsed.scheme}://{host}{link}'
                if link.startswith(('http://', 'https://')) and net.host_of(link) == host:
                    normalized = normalize_url(link)
                    if normalized not in found:
                        found.append(normalized)
                if len(found) >= limit:
                    break
        except net.RemoteError:
            pass
    return found[:limit]


def tmp_dir(session_id: int):
    path = settings.MEDIA_ROOT / 'nuwa_tmp' / str(session_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def preview(session, urls: list, *, discover: bool = False) -> dict:
    """抓取预览：逐条结果，不落库；成功条目写临时文件供确认后入库"""
    if not config.knowledge_online():
        return {
            'items': [],
            'mode': 'offline',
            'degraded_reason': 'switch_off',
            'message': '女娲在线抓取已关闭，可改为上传文件或粘贴正文。',
        }
    if not config.nuwa_enabled():
        return {'items': [], 'mode': 'offline', 'degraded_reason': 'disabled',
                'message': '女娲模式已停用。'}

    targets = []
    for raw in (urls or [])[:MAX_ITEMS]:
        url = normalize_url(raw)
        if url and url not in targets:
            targets.append(url)

    results = []
    failures = 0
    for url in targets:
        entry = {'url': url, 'status': 'ok', 'title': '', 'site': net.host_of(url),
                 'reason': '', 'words': 0, 'content_sha256': '', 'file': ''}
        try:
            page = fetch_page(url)
            stored = tmp_dir(session.pk) / f'{page["content_sha256"][:16]}.md'
            stored.write_text(page['text'], encoding='utf-8')
            entry.update({
                'title': page['title'], 'words': len(page['text']),
                'content_sha256': page['content_sha256'],
                'file': str(stored.relative_to(settings.MEDIA_ROOT)),
                'preview': page['text'][:300],
            })
        except net.RemoteError as exc:
            entry.update({'status': 'failed' if exc.reason != 'blocked' else 'skipped',
                          'reason': exc.message or exc.reason})
            failures += 1
        except Exception as exc:
            logger.warning('女娲抓取异常 %s: %s', url, exc)
            entry.update({'status': 'failed', 'reason': f'{type(exc).__name__}: {exc}'[:200]})
            failures += 1
        results.append(entry)
        if failures >= 3 and all(item['status'] != 'ok' for item in results[-3:]):
            break

    if discover and targets:
        extra = [url for url in discover_same_host(targets[0], limit=8)
                 if url not in targets]
        for url in extra[:max(0, MAX_ITEMS - len(results))]:
            entry = {'url': url, 'status': 'ok', 'title': '', 'site': net.host_of(url),
                     'reason': '', 'words': 0, 'content_sha256': '', 'file': ''}
            try:
                page = fetch_page(url)
                stored = tmp_dir(session.pk) / f'{page["content_sha256"][:16]}.md'
                stored.write_text(page['text'], encoding='utf-8')
                entry.update({'title': page['title'], 'words': len(page['text']),
                              'content_sha256': page['content_sha256'],
                              'file': str(stored.relative_to(settings.MEDIA_ROOT)),
                              'preview': page['text'][:300]})
            except net.RemoteError as exc:
                entry.update({'status': 'failed', 'reason': exc.message or exc.reason})
            except Exception as exc:
                entry.update({'status': 'failed', 'reason': str(exc)[:200]})
            results.append(entry)

    return {
        'items': results,
        'mode': 'online',
        'degraded_reason': '',
        'ok': sum(1 for item in results if item['status'] == 'ok'),
        'failed': sum(1 for item in results if item['status'] == 'failed'),
        'skipped': sum(1 for item in results if item['status'] == 'skipped'),
    }


def ensure_kb(user, *, kb_id=None, name: str = '', scope: str = 'public') -> KnowledgeBase:
    if kb_id:
        kb = KnowledgeBase.objects.filter(pk=kb_id).first()
        if kb is None:
            raise ValueError('知识库不存在')
        from backend.apps.knowledge.models import can_manage_kb

        if not can_manage_kb(user, kb):
            raise PermissionError('只有知识库创建者或平台管理员可以写入')
        return kb
    return KnowledgeBase.objects.create(
        name=name or '女娲资料库',
        description='女娲模式按需求抓取并入库的资料',
        scope=scope if scope in ('public', 'department', 'private') else 'public',
        is_public=scope != 'private',
        department=getattr(user, 'department', '') or '',
        created_by=user,
    )


def commit(session, items: list, *, kb_id=None, kb_name: str = '', scope: str = 'public',
           user=None) -> dict:
    """把预览中勾选的条目写入知识库并同步向量化"""
    kb = ensure_kb(user, kb_id=kb_id, name=kb_name, scope=scope)
    created, skipped = [], []
    for item in items or []:
        url = item.get('url') or ''
        rel = item.get('file') or ''
        if url and Document.objects.filter(knowledge_base=kb, source_url=url).exists():
            skipped.append({'url': url, 'reason': '同知识库已存在同一来源'})
            continue
        path = settings.MEDIA_ROOT / rel if rel else None
        if not path or not path.exists():
            skipped.append({'url': url, 'reason': '临时文件已失效，请重新抓取'})
            continue
        text = path.read_text(encoding='utf-8', errors='replace')[:MAX_TEXT_BYTES]
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        if Document.objects.filter(knowledge_base=kb, content_sha256=digest).exists():
            skipped.append({'url': url, 'reason': '内容与已有文档重复'})
            continue
        doc = Document(
            knowledge_base=kb,
            title=(item.get('title') or url)[:500],
            file_name=(urlparse(url).path.rsplit('/', 1)[-1] or 'page') + '.md',
            file_type='md',
            file_size=len(text.encode('utf-8')),
            uploaded_by=user,
            status='pending',
            source_url=url[:500],
            source_title=(item.get('title') or '')[:500],
            source_site=(item.get('site') or '')[:200],
            source_license=(item.get('license') or '')[:100],
            fetched_at=timezone.now(),
            content_sha256=digest,
            source_type='web',
        )
        doc.file_path.save(doc.file_name, ContentFile(text.encode('utf-8')), save=False)
        doc.save()
        kb.document_count += 1
        kb.size_bytes += doc.file_size
        kb.save(update_fields=['document_count', 'size_bytes', 'updated_at'])
        try:
            process_document(doc)
        except Exception as exc:  # 解析失败保留文档与错误信息，不影响其余条目
            logger.warning('女娲入库文档处理失败 %s: %s', doc.pk, exc)
        created.append({'document_id': doc.pk, 'title': doc.title, 'url': url})
        try:
            path.unlink()
        except OSError:
            pass
    return {'knowledge_base_id': kb.pk, 'created': created, 'skipped': skipped}
