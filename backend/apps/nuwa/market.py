"""市场目录：在线检索 → 归一化 → 写缓存；失败自动回落本地

在线与离线共用同一张 `MarketItem` 表：
- 在线命中的条目先归一化、过安全校验，再 upsert 进表；
- 检索失败/开关关闭时只读表，接口照常返回，用 mode/degraded_reason 说明来源。

安全校验只在这里做一次，落库的 install_payload 已经是"可以直接建 MCPServer /
Skill"的形态，审批通过后不再二次推断。
"""
import hashlib
import json
import logging
import re
import time
from datetime import timedelta
from urllib.parse import quote, urlparse

from django.db import transaction
from django.utils import timezone

from backend.apps.mcp.services import match_command_prefix
from backend.apps.nuwa import config, net
from backend.apps.nuwa.models import (MARKET_KIND_MCP, MARKET_KIND_SKILL, MarketItem,
                                      MarketSource, MarketSyncRun)

logger = logging.getLogger(__name__)

REGISTRY_BASE = 'https://registry.modelcontextprotocol.io'
SKILLS_REPO = 'anthropics/skills'
GITHUB_API = 'https://api.github.com'
RAW_BASE = 'https://raw.githubusercontent.com'

TRUSTED_PUBLISHERS = ('modelcontextprotocol', 'anthropics', 'microsoft', 'github', 'openai')

ALLOWED_RUNTIMES = ('npx', 'uvx', 'node', 'python', 'python3')
SHELL_META = re.compile(r'[;|&$`()<>\n\r]')
WINDOWS_ABS = re.compile(r'^[A-Za-z]:[\\/]')


def ensure_builtin_sources() -> list:
    """内置两条市场源：官方 MCP registry、anthropics/skills"""
    mcp, _ = MarketSource.objects.get_or_create(
        name='MCP 官方目录',
        defaults={'kind': 'mcp_registry', 'base_url': REGISTRY_BASE, 'is_builtin': True},
    )
    skill, _ = MarketSource.objects.get_or_create(
        name='Anthropic Skills',
        defaults={'kind': 'skill_repo', 'repo': SKILLS_REPO, 'branch': 'main',
                  'base_url': 'https://github.com/anthropics/skills', 'is_builtin': True},
    )
    return [mcp, skill]


# ─── 归一化 ───

def _fingerprint(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _publisher_trusted(*values) -> bool:
    text = ' '.join(str(v or '').lower() for v in values)
    return any(name in text for name in TRUSTED_PUBLISHERS)


def validate_args(args) -> tuple:
    """命令参数安全校验：拒 shell 元字符、绝对路径、换行"""
    for arg in args or []:
        text = str(arg)
        if SHELL_META.search(text):
            return False, f'参数含 shell 元字符：{text[:40]}'
        if text.startswith('-') and text in ('-c', '--eval'):
            return False, f'参数不允许：{text}'
        if text.startswith('/') or WINDOWS_ABS.match(text):
            return False, f'参数不允许绝对路径：{text[:40]}'
    return True, ''


def normalize_registry_entry(entry: dict, source: MarketSource) -> dict | None:
    """官方 registry 条目 → MarketItem 字段；不可用返回 None

    同一接口并存多套 schema，这里按"字段存在即取"降级，不假设版本。
    """
    server = entry.get('server') or entry
    name = str(server.get('name') or '').strip()
    if not name:
        return None
    title = str(server.get('title') or name).strip()[:200]
    summary = str(server.get('description') or '').strip()[:500]
    version = str(server.get('version') or '').strip()[:64]
    repo = ''
    repository = server.get('repository')
    if isinstance(repository, dict):
        repo = str(repository.get('url') or '')
    homepage = str(server.get('websiteUrl') or server.get('homepage') or '').strip()[:300]
    publisher = name.split('/')[0] if '/' in name else ''
    trusted = _publisher_trusted(publisher, repo, name)

    payload = None
    transport = ''
    egress_host = ''
    requires_egress = True

    remotes = server.get('remotes') or []
    for remote in remotes:
        url = str((remote or {}).get('url') or '').strip()
        if not url:
            continue
        ok, _reason = net.is_public_https(url)
        if not ok:
            continue
        payload = {'type': 'http_mcp', 'url': url, 'headers': {}}
        transport = 'http_mcp'
        egress_host = net.host_of(url)
        break

    if payload is None:
        for package in server.get('packages') or []:
            runtime = str((package or {}).get('runtimeHint') or '').strip()
            identifier = str((package or {}).get('identifier') or '').strip()
            registry_type = str((package or {}).get('registryType') or '').strip()
            if runtime not in ALLOWED_RUNTIMES or not identifier:
                continue
            args = [str(a.get('value') if isinstance(a, dict) else a)
                    for a in (package.get('runtimeArguments') or [])]
            if runtime == 'npx' and '-y' not in args:
                args.insert(0, '-y')
            args.append(identifier)
            ok, _reason = validate_args(args)
            if not ok:
                continue
            command = runtime
            if not match_command_prefix(command, args):
                continue
            env = []
            for item in package.get('environmentVariables') or []:
                key = str((item or {}).get('name') or '').strip()
                if not re.match(r'^[A-Z][A-Z0-9_]{0,63}$', key):
                    continue
                env.append({
                    'name': key,
                    'required': bool(item.get('isRequired')),
                    'secret': bool(item.get('isSecret')),
                    'description': str(item.get('description') or '')[:200],
                    'default': str(item.get('default') or ''),
                })
            payload = {'type': 'stdio', 'command': command, 'args': args,
                       'env': env, 'registry_type': registry_type}
            transport = 'stdio'
            requires_egress = True
            break

    if payload is None:
        return None

    payload.update({'external_id': name, 'version': version, 'source_kind': source.kind})
    return {
        'kind': MARKET_KIND_MCP,
        'external_id': name,
        'version': version,
        'title': title,
        'summary': summary,
        'publisher': publisher[:120],
        'homepage': homepage,
        'repository_url': repo[:300],
        'license': '',
        'trust_level': 'trusted' if trusted else 'unverified',
        'transport': transport,
        'install_payload': payload,
        'egress_host': egress_host,
        'requires_egress': requires_egress,
        'has_scripts': transport == 'stdio',
        'capabilities': _capabilities(name, title, summary),
        'content_hash': _fingerprint(payload),
        'raw': server,
    }


def _capabilities(*texts) -> list:
    """能力标签：用于展示与关键词打分（不做语义推断，只取原文词）"""
    words = []
    for text in texts:
        for token in re.split(r'[^0-9A-Za-z\u4e00-\u9fa5]+', str(text or '').lower()):
            token = token.strip()
            if 2 <= len(token) <= 24 and token not in words:
                words.append(token)
    return words[:20]


def parse_skill_md(text: str) -> dict:
    """解析 SKILL.md 的 frontmatter（不引入 yaml 依赖）

    只取 name / description / license；支持 `description: >` 这类块标量，
    否则摘要会变成一个字面量 ">"。
    """
    meta = {}
    body = text
    if text.startswith('---'):
        parts = text.split('---', 2)
        if len(parts) >= 3:
            body = parts[2]
            current_key = ''
            buffer = []

            def flush():
                if current_key and buffer:
                    meta[current_key] = ' '.join(part for part in buffer if part).strip()

            for line in parts[1].splitlines():
                stripped = line.strip()
                if line[:1] not in (' ', '\t') and ':' in line:
                    flush()
                    key, value = line.split(':', 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    current_key = key if key in ('name', 'description', 'license') else ''
                    buffer = []
                    if value in ('>', '|', '>-', '|-', '>+', '|+'):
                        continue
                    if current_key and value:
                        meta[current_key] = value
                elif current_key:
                    buffer.append(stripped)
            flush()
    return {'meta': meta, 'body': body.strip()}


def normalize_skill(name: str, repo: str, branch: str,
                    skill_md: str | None = None) -> dict:
    path = f'skills/{name}'
    payload = {
        'type': 'skill',
        'repo': repo,
        'branch': branch,
        'path': path,
        'skill_md_url': f'{RAW_BASE}/{repo}/{branch}/{path}/SKILL.md',
    }
    meta, body = {}, ''
    if skill_md:
        parsed = parse_skill_md(skill_md)
        meta, body = parsed['meta'], parsed['body']
        payload['frontmatter'] = meta
    title = (meta.get('name') or name)[:200]
    summary = (meta.get('description') or body[:200] or '').strip()[:500]
    return {
        'kind': MARKET_KIND_SKILL,
        'external_id': name,
        'version': hashlib.sha256((skill_md or name).encode('utf-8')).hexdigest()[:12],
        'title': title,
        'summary': summary,
        'publisher': repo.split('/')[0],
        'homepage': f'https://github.com/{repo}/tree/{branch}/{path}',
        'repository_url': f'https://github.com/{repo}',
        'license': meta.get('license', ''),
        'trust_level': 'trusted' if _publisher_trusted(repo) else 'unverified',
        'transport': '',
        'install_payload': payload,
        'egress_host': '',
        'requires_egress': False,
        'has_scripts': 'scripts/' in (skill_md or ''),
        'capabilities': _capabilities(title, summary, name),
        'content_hash': _fingerprint(payload),
        'raw': {'meta': meta, 'body_preview': body[:1000]},
    }


# ─── 写入缓存 ───

def _embed_batch(texts: list) -> list:
    if not texts:
        return []
    from pathlib import Path

    from django.conf import settings

    cache_dir = getattr(settings, 'FASTEMBED_CACHE_DIR', None)
    # 模型尚未下载到本地时不触发在线下载：市场检索不能被一次模型拉取拖住，
    # 此时回落到关键词打分，功能可用、结果标注理由为"关键词命中"。
    if cache_dir and not Path(cache_dir).exists():
        return []
    try:
        from backend.apps.common.embedding import embed_texts

        return embed_texts(texts)
    except Exception as exc:  # 模型缺失/首次下载失败都不阻塞检索
        logger.warning('女娲市场向量化不可用，回落到关键词打分: %s', exc)
        return []


def upsert_item(source: MarketSource, data: dict, *, with_embedding: bool = True) -> tuple:
    """按 (source, kind, external_id, version) upsert，返回 (obj, created)"""
    key = {
        'source': source, 'kind': data['kind'],
        'external_id': data['external_id'], 'version': data['version'],
    }
    defaults = {k: v for k, v in data.items()
                if k not in ('kind', 'external_id', 'version')}
    defaults['fetched_at'] = timezone.now()
    if with_embedding:
        embedding = _embed_batch([f"{data.get('title', '')} {data.get('summary', '')}"])[0] \
            if data.get('title') or data.get('summary') else []
        if embedding:
            defaults['embedding'] = embedding
    obj, created = MarketItem.objects.update_or_create(defaults=defaults, **key)
    return obj, created


# ─── 在线抓取 ───

def fetch_registry(source: MarketSource, query: str = '', limit: int = 20) -> list:
    base = (source.base_url or REGISTRY_BASE).rstrip('/')
    params = [f'limit={max(1, min(limit, 50))}']
    if query:
        params.append(f'search={quote(query[:80])}')
    url = f'{base}/v0/servers?' + '&'.join(params)
    data = net.request_json(url, timeout=net.MARKET_TIMEOUT, retries=1, backoff=1.0,
                            headers=net.auth_headers(source.auth_env))
    out = []
    for entry in data.get('servers') or []:
        item = normalize_registry_entry(entry, source)
        if item:
            out.append(item)
    return out


def _github_headers(source: MarketSource) -> dict:
    headers = {'Accept': 'application/vnd.github+json'}
    headers.update(net.auth_headers(source.auth_env))
    return headers


def fetch_skill_index(source: MarketSource) -> list:
    repo = source.repo or SKILLS_REPO
    branch = source.branch or 'main'
    url = f'{GITHUB_API}/repos/{repo}/contents/skills?ref={quote(branch)}'
    data = net.request_json(url, timeout=net.MARKET_TIMEOUT, retries=1,
                            headers=_github_headers(source))
    names = [str(item.get('name')) for item in data if item.get('type') == 'dir']
    return [normalize_skill(name, repo, branch) for name in names]


def fetch_skill_detail(source: MarketSource, item: dict, *, timeout: int) -> dict:
    """补抓 SKILL.md，拿到名称/说明/许可证；失败保持原样"""
    payload = item.get('install_payload') or {}
    url = payload.get('skill_md_url')
    if not url:
        return item
    try:
        text = net.request_text(url, timeout=timeout, retries=1, backoff=1.0,
                                headers=net.auth_headers(source.auth_env))
    except net.RemoteError as exc:
        logger.info('技能正文抓取失败 %s: %s', url, exc.reason)
        return item
    merged = normalize_skill(item['external_id'], payload.get('repo') or SKILLS_REPO,
                             payload.get('branch') or 'main', text)
    merged['kind'] = MARKET_KIND_SKILL
    return merged


def sync_source(source: MarketSource, *, query: str = '', limit: int = 50,
                mode: str = 'online') -> MarketSyncRun:
    """把一个源同步进本地表；部分失败也保留已入库条目"""
    run = MarketSyncRun.objects.create(source=source, mode=mode)
    try:
        if source.kind == 'mcp_registry':
            items = fetch_registry(source, query=query, limit=limit)
        elif source.kind == 'skill_repo':
            items = fetch_skill_index(source)
            items = items[:max(1, limit)]
            for index, item in enumerate(items):
                items[index] = fetch_skill_detail(source, item,
                                                  timeout=net.SKILL_FILE_TIMEOUT)
                if index < len(items) - 1:
                    time.sleep(0.15)  # 匿名 API 限速，逐条小步
        else:
            items = []
        for data in items:
            _obj, created = upsert_item(source, data, with_embedding=False)
            if created:
                run.added += 1
            else:
                run.updated += 1
        run.status = 'ok' if items else 'failed'
        if not items:
            run.error = '未取到任何可用条目（可能被限流或条目均未通过校验）'
    except net.RemoteError as exc:
        run.status = 'failed'
        run.error = f'{exc.reason}: {exc.message}'[:300]
    except Exception as exc:  # 防御：同步失败不能影响既有目录
        logger.exception('女娲市场同步异常')
        run.status = 'failed'
        run.error = f'{type(exc).__name__}: {exc}'[:300]
    finally:
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'added', 'updated', 'skipped', 'failed',
                                'error', 'finished_at'])
        source.last_sync_at = timezone.now()
        source.last_sync_status = run.status
        source.last_error = run.error
        source.item_count = source.items.count()
        source.save(update_fields=['last_sync_at', 'last_sync_status', 'last_error',
                                   'item_count', 'updated_at'])
    return run


def sync_all(*, query: str = '', limit: int = 50) -> list:
    return [sync_source(source, query=query, limit=limit)
            for source in ensure_builtin_sources() if source.enabled]


# ─── 离线包 ───

def export_cache(path) -> dict:
    """导出离线包：别的机器 --import 后即可在断网环境使用"""
    sources = []
    for source in MarketSource.objects.all():
        sources.append({
            'name': source.name, 'kind': source.kind, 'base_url': source.base_url,
            'repo': source.repo, 'branch': source.branch, 'is_builtin': source.is_builtin,
            'items': [{
                'kind': item.kind, 'external_id': item.external_id,
                'version': item.version, 'title': item.title, 'summary': item.summary,
                'publisher': item.publisher, 'homepage': item.homepage,
                'repository_url': item.repository_url, 'license': item.license,
                'trust_level': item.trust_level, 'transport': item.transport,
                'install_payload': item.install_payload, 'egress_host': item.egress_host,
                'requires_egress': item.requires_egress,
                'has_scripts': item.has_scripts, 'capabilities': item.capabilities,
                'content_hash': item.content_hash, 'raw': item.raw,
            } for item in source.items.all()],
        })
    payload = {'version': 1, 'exported_at': timezone.now().isoformat(), 'sources': sources}
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return payload


def import_cache(path) -> list:
    with open(path, encoding='utf-8') as handle:
        payload = json.load(handle)
    runs = []
    for block in payload.get('sources') or []:
        source, _ = MarketSource.objects.get_or_create(
            name=block.get('name') or '导入目录',
            defaults={'kind': block.get('kind') or 'local_catalog',
                      'base_url': block.get('base_url', ''),
                      'repo': block.get('repo', ''), 'branch': block.get('branch', ''),
                      'is_builtin': bool(block.get('is_builtin'))},
        )
        run = MarketSyncRun.objects.create(source=source, mode='import')
        for data in block.get('items') or []:
            data = dict(data)
            data.pop('id', None)
            _obj, created = upsert_item(source, data)
            if created:
                run.added += 1
            else:
                run.updated += 1
        run.status = 'ok'
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'added', 'updated', 'finished_at'])
        source.last_sync_at = timezone.now()
        source.last_sync_status = 'ok'
        source.item_count = source.items.count()
        source.save(update_fields=['last_sync_at', 'last_sync_status', 'item_count',
                                   'updated_at'])
        runs.append(run)
    return runs


# ─── 检索与打分 ───

def _cosine(left: list, right: list) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = sum(a * a for a in left) ** 0.5
    norm_right = sum(b * b for b in right) ** 0.5
    if not norm_left or not norm_right:
        return 0.0
    return dot / (norm_left * norm_right)


def _lexical(query: str, text: str) -> tuple:
    """字符二元组重合度（中文友好），返回 (分数, 命中词)"""
    query = (query or '').lower()
    text = (text or '').lower()
    if not query or not text:
        return 0.0, []
    hits = [word for word in re.split(r'[^0-9a-z\u4e00-\u9fa5]+', query)
            if len(word) >= 2 and word in text]
    grams = {query[i:i + 2] for i in range(len(query) - 1)}
    overlap = sum(1 for gram in grams if gram in text) / max(1, len(grams))
    return min(1.0, overlap + 0.1 * len(hits)), hits[:5]


def score_items(query: str, items: list) -> list:
    """余弦 + 关键词，返回带 score/reason 的 dict 列表（稳定排序）"""
    vectors = _embed_batch([query]) if query else []
    query_vector = vectors[0] if vectors else []
    scored = []
    for item in items:
        semantic = _cosine(query_vector, item.embedding or []) if query_vector else 0.0
        lexical, hits = _lexical(query, item.search_text)
        score = round(0.65 * semantic + 0.35 * lexical, 4) if query else 0.5
        if score <= 0.02 and query:
            continue
        reason = '语义相近' if semantic >= lexical else '关键词命中'
        if hits:
            reason = '命中关键词：' + '、'.join(hits)
        scored.append({'item': item, 'score': score, 'reason': reason})
    scored.sort(key=lambda row: (-row['score'], row['item'].title))
    return scored


def _search_term(query: str) -> str:
    """把一整句需求收敛成一个检索词：优先英文/数字词，其次取前 20 个汉字

    官方 registry 的 search 是关键词匹配，直接丢整句会命中 0 条。
    """
    tokens = [token for token in re.split(r'[^0-9A-Za-z]+', query or '') if len(token) >= 3]
    if tokens:
        return max(tokens, key=len)[:40]
    han = re.sub(r'[^\u4e00-\u9fa5]', '', query or '')
    return han[:20]


def _cache_synced_at() -> str:
    latest = MarketSource.objects.filter(items__isnull=False).order_by('-last_sync_at').first()
    return latest.last_sync_at.isoformat() if latest and latest.last_sync_at else ''


def search(query: str, *, kinds: list | None = None, limit: int = 12,
           source_id: int | None = None) -> dict:
    """在线优先，本地兜底；任何失败都返回 200 语义的数据

    `source_id` 只缩小范围（市场页的来源筛选），不改变降级口径。
    """
    kinds = kinds or [MARKET_KIND_MCP, MARKET_KIND_SKILL]
    degraded = ''
    online_count = 0
    online_attempted = False
    if config.market_online():
        for source in ensure_builtin_sources():
            if not source.enabled:
                continue
            if source_id and source.pk != source_id:
                continue
            online_attempted = True
            try:
                items = (fetch_registry(source, query=_search_term(query),
                                        limit=max(limit, 20))
                         if source.kind == 'mcp_registry' and MARKET_KIND_MCP in kinds
                         else fetch_skill_index(source) if source.kind == 'skill_repo'
                         and MARKET_KIND_SKILL in kinds else [])
            except net.RemoteError as exc:
                degraded = degraded or exc.reason
                continue
            except Exception as exc:
                logger.warning('女娲在线检索异常: %s', exc)
                degraded = degraded or 'network_error'
                continue
            for data in items:
                upsert_item(source, data, with_embedding=False)
                online_count += 1
    else:
        degraded = 'switch_off'

    cached_qs = MarketItem.objects.filter(kind__in=kinds).exclude(status='blocked')
    if source_id:
        cached_qs = cached_qs.filter(source_id=source_id)
    cached = list(cached_qs[:400])
    missing = [item for item in cached if not item.embedding][:80]
    if missing:
        vectors = _embed_batch([item.search_text for item in missing])
        for item, vector in zip(missing, vectors):
            if vector:
                item.embedding = vector
                item.save(update_fields=['embedding'])
    ranked = score_items(query, cached)[:limit]
    items = [{
        'id': row['item'].pk,
        'kind': row['item'].kind,
        'external_id': row['item'].external_id,
        'version': row['item'].version,
        'title': row['item'].title,
        'summary': row['item'].summary,
        'publisher': row['item'].publisher,
        'homepage': row['item'].homepage,
        'repository_url': row['item'].repository_url,
        'license': row['item'].license,
        'license_declared': bool(row['item'].license),
        'trust_level': row['item'].trust_level,
        'transport': row['item'].transport,
        'requires_egress': row['item'].requires_egress,
        'has_scripts': row['item'].has_scripts,
        'egress_host': row['item'].egress_host,
        'capabilities': row['item'].capabilities,
        'source': row['item'].source.name,
        'fetched_at': row['item'].fetched_at.isoformat(),
        'score': row['score'],
        'reason': row['reason'],
    } for row in ranked]

    if online_count and degraded:
        mode = 'mixed'
    elif online_count:
        mode = 'online'
    else:
        mode = 'cached'
    if mode == 'cached' and not items and not degraded:
        degraded = 'offline_no_cache'
    return {
        'items': items,
        'mode': mode,
        'degraded_reason': degraded,
        'cache_synced_at': _cache_synced_at(),
        'online_hits': online_count,
        'online_attempted': online_attempted,
    }


def prune_stale(days: int = 60) -> int:
    """超期未再命中的条目标 stale（不删除，避免离线环境丢目录）"""
    cutoff = timezone.now() - timedelta(days=days)
    return MarketItem.objects.filter(fetched_at__lt=cutoff, status='active') \
        .update(status='stale')
