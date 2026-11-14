"""天工模式：知识库自动采集

四层网页发现（L1 MCP 搜索 → L2 SearXNG → L3 Bing 保底 → L4 来源库抓取）：
- L1 MCP 搜索：管理员确认搜索工具绑定后生效，未配置不影响后续层；
- L2 SearXNG：本机 JSON 检索，结果只生成候选提案，不直接抓正文；
- L3 Bing 保底：无头浏览器兜底，默认关闭，缺依赖/熔断/额度用尽都只影响本层；
- L4 来源库：已批准来源 + 知识库临时采集入口，抓正文入库（`source_type='web'`）。

审批边界：搜索层候选必须经管理员批准写入 `TiangongSource` 后，下一轮 L4 才会抓取；
未审批候选一律不抓正文。

模型侧：按「采集主题」让当前模型写一份结构化知识摘要，`source_type='model'`，
并记下生成模型名，不伪造引用。

去重：同库 `source_url` 唯一 + `content_sha256` 去重；提案按 URL 去重并 30 天过期。
审计：一次运行只记一条开始/结束聚合记录，不逐条记 URL。
"""
import hashlib
import json
import logging
import re
from datetime import timedelta

from django.core.files.base import ContentFile
from django.utils import timezone

from backend.apps.knowledge import discovery
from backend.apps.knowledge.models import (
    Document, KnowledgeBase, TiangongRun, TiangongSource, TiangongSourceProposal,
)
from backend.apps.knowledge.services import process_document
from backend.apps.settings.models import SystemSetting
from backend.apps.users.models import log_audit

logger = logging.getLogger(__name__)

KEY_ENABLED = 'tiangong_enabled'
KEY_ONLINE = 'tiangong_online'
KEY_DISCOVERY = 'tiangong_discovery_enabled'
KEY_MCP = 'tiangong_search_mcp_enabled'
KEY_SEARXNG = 'tiangong_search_searxng_enabled'
KEY_BING = 'tiangong_search_bing_enabled'
KEY_CRAWL_MAX_PAGES = 'tiangong_crawl_max_pages'
KEY_CRAWL_PER_SOURCE = 'tiangong_crawl_pages_per_source'

MAX_WEB_ITEMS = 5
MAX_MODEL_ITEMS = 1
MAX_TOPICS_CHARS = 600
MAX_TEXT_BYTES = 200 * 1024
MODEL_MAX_TOKENS = 1400
PROPOSAL_TTL_DAYS = 30


def _flag(key: str, default: str = 'true') -> bool:
    raw = SystemSetting.get_value(key, default)
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


def _int(key: str, default: int, low: int, high: int) -> int:
    raw = SystemSetting.get_value(key, str(default))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def enabled() -> bool:
    """管理员总开关：关掉后建库仍可勾选，但采集会跳过并说明原因"""
    return _flag(KEY_ENABLED)


def online_enabled() -> bool:
    """天工联网采集开关；与 nuwa_knowledge_online / mcp_egress_enabled 互不影响"""
    return _flag(KEY_ONLINE)


def discovery_enabled() -> bool:
    """天工来源发现总开关；关闭后回到 seeds + 同域浅层（仍走来源库抓取）"""
    return _flag(KEY_DISCOVERY)


def switches() -> dict:
    return {'enabled': enabled(), 'online': online_enabled(),
            'max_web_items': MAX_WEB_ITEMS, 'max_model_items': MAX_MODEL_ITEMS}


def set_switches(*, enabled_value=None, online_value=None, actor=None) -> dict:
    changed = []
    if enabled_value is not None:
        SystemSetting.set_value(KEY_ENABLED, 'true' if enabled_value else 'false',
                                '天工模式总开关')
        changed.append(f'总开关={"开" if enabled_value else "关"}')
    if online_value is not None:
        SystemSetting.set_value(KEY_ONLINE, 'true' if online_value else 'false',
                                '天工模式是否允许联网采集')
        changed.append(f'联网采集={"开" if online_value else "关"}')
    if changed:
        log_audit(actor=actor, action='tiangong_switches_update', target='天工模式开关',
                  detail='；'.join(changed))
    return switches()


def discovery_settings() -> dict:
    """搜索层与抓取的运行参数（仅管理员可改）"""
    from backend.apps.knowledge import bing, searxng

    return {
        'discovery_enabled': discovery_enabled(),
        'mcp_enabled': _flag(KEY_MCP),
        'searxng_enabled': _flag(KEY_SEARXNG),
        'bing_enabled': _flag(KEY_BING, 'false'),
        'searxng_url': searxng.config()['url'],
        'searxng_timeout': searxng.config()['timeout'],
        'max_queries': searxng.config()['max_queries'],
        'max_results': searxng.config()['max_results'],
        'min_results': searxng.config()['min_results'],
        'crawl_max_pages': _int(KEY_CRAWL_MAX_PAGES, discovery.MAX_PAGES_PER_RUN,
                                1, discovery.HARD_PAGES_PER_RUN),
        'crawl_pages_per_source': _int(KEY_CRAWL_PER_SOURCE,
                                       discovery.MAX_PAGES_PER_SOURCE,
                                       1, discovery.MAX_PAGES_PER_SOURCE),
        'searxng_circuit': searxng.circuit_state(),
        'bing_daily_cap': bing.config()['daily_cap'],
        'bing_max_queries': bing.config()['max_queries'],
        'bing_max_results': bing.config()['max_results'],
        'bing_timeout': bing.config()['timeout'],
        'bing_interval': bing.config()['interval'],
        'bing_state': bing.state(),
    }


def set_discovery_settings(data: dict, actor=None) -> dict:
    """更新搜索层开关与抓取参数；只写请求里出现的键"""
    from backend.apps.knowledge import bing, searxng

    changed = []
    bool_keys = {
        'discovery_enabled': (KEY_DISCOVERY, '天工来源发现总开关'),
        'mcp_enabled': (KEY_MCP, 'MCP 搜索开关（L1）'),
        'searxng_enabled': (KEY_SEARXNG, 'SearXNG 搜索开关'),
        'bing_enabled': (KEY_BING, 'Bing 保底开关'),
    }
    for field, (key, label) in bool_keys.items():
        if isinstance(data.get(field), bool):
            SystemSetting.set_value(key, 'true' if data[field] else 'false', label)
            changed.append(f'{label}={"开" if data[field] else "关"}')

    int_keys = {
        'searxng_timeout': (searxng.KEY_TIMEOUT, 'SearXNG 单轮超时（秒）', 3, 60),
        'max_queries': (searxng.KEY_MAX_QUERIES, '单轮最多查询数', 1, 10),
        'max_results': (searxng.KEY_MAX_RESULTS, '单层最多候选数', 1, 10),
        'min_results': (searxng.KEY_MIN_RESULTS, '命中即停的最小候选数', 1, 10),
        'crawl_max_pages': (KEY_CRAWL_MAX_PAGES, '单轮抓取页数上限', 1,
                            discovery.HARD_PAGES_PER_RUN),
        'crawl_pages_per_source': (KEY_CRAWL_PER_SOURCE, '单来源抓取页数', 1,
                                   discovery.MAX_PAGES_PER_SOURCE),
        'bing_daily_cap': (bing.KEY_DAILY_CAP, 'Bing 当日调用上限', 1, 200),
        'bing_max_queries': (bing.KEY_MAX_QUERIES, 'Bing 单轮查询数', 1, 3),
        'bing_max_results': (bing.KEY_MAX_RESULTS, 'Bing 单查询结果数', 1, 10),
        'bing_timeout': (bing.KEY_TIMEOUT, 'Bing 单页超时（秒）', 10, 60),
        'bing_interval': (bing.KEY_INTERVAL, 'Bing 查询间隔（秒）', 1, 5),
    }
    for field, (key, label, low, high) in int_keys.items():
        if data.get(field) is None:
            continue
        try:
            value = int(data[field])
        except (TypeError, ValueError):
            continue
        value = max(low, min(high, value))
        SystemSetting.set_value(key, str(value), label)
        changed.append(f'{label}={value}')

    url = (data.get('searxng_url') or '').strip()
    if url:
        if not re.match(r'^https?://[^\s]+$', url):
            raise ValueError('SearXNG 地址必须以 http:// 或 https:// 开头')
        SystemSetting.set_value(searxng.KEY_URL, url.rstrip('/'), 'SearXNG 地址')
        changed.append(f'SearXNG 地址={url.rstrip("/")}')

    if data.get('reset_circuit'):
        searxng.reset_circuit()
        changed.append('重置 SearXNG 熔断')
    if data.get('reset_bing_circuit'):
        bing.reset_circuit()
        changed.append('重置 Bing 熔断')

    if changed:
        log_audit(actor=actor, action='tiangong_search_settings_update',
                  target='天工搜索设置', detail='；'.join(changed)[:200])
    return discovery_settings()


def parse_seeds(text: str) -> list:
    seeds = []
    for line in re.split(r'[\s,;]+', text or ''):
        url = line.strip()
        if url.startswith(('http://', 'https://')) and url not in seeds:
            seeds.append(url[:500])
    return seeds[:MAX_WEB_ITEMS]


def topics_of(kb: KnowledgeBase) -> str:
    raw = (kb.tiangong_topics or '').strip()
    if not raw:
        raw = f'{kb.name} {kb.description}'.strip()
    return raw[:MAX_TOPICS_CHARS]


def create_run(kb: KnowledgeBase, *, trigger: str, actor) -> TiangongRun:
    run = TiangongRun.objects.create(knowledge_base=kb, trigger=trigger,
                                     topics=topics_of(kb), actor=actor)
    kb.tiangong_status = 'queued'
    kb.save(update_fields=['tiangong_status', 'updated_at'])
    return run


def _create_document(kb: KnowledgeBase, *, title: str, text: str, source_type: str,
                     source_url: str = '', source_site: str = '', source_license: str = '',
                     source_model: str = '', actor=None) -> tuple:
    """建文档并同步跑既有切片/向量化流水线；返回 (doc|None, 跳过原因)"""
    if not text or len(text) < 80:
        return None, '正文太短'
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    if source_url and Document.objects.filter(knowledge_base=kb, source_url=source_url).exists():
        return None, '同库已存在同一来源'
    if Document.objects.filter(knowledge_base=kb, content_sha256=digest).exists():
        return None, '内容与已有文档重复'
    suffix = '.md'
    doc = Document(
        knowledge_base=kb,
        title=(title or source_url or '天工采集')[:500],
        file_name=f'tiangong-{digest[:12]}{suffix}',
        file_type='md',
        file_size=len(text.encode('utf-8')),
        uploaded_by=actor,
        status='pending',
        source_url=source_url[:500],
        source_title=(title or '')[:500],
        source_site=source_site[:200],
        source_license=source_license[:100],
        source_type=source_type,
        source_model=source_model[:100],
        fetched_at=timezone.now(),
        content_sha256=digest,
    )
    doc.file_path.save(doc.file_name, ContentFile(text.encode('utf-8')), save=False)
    doc.save()
    kb.document_count += 1
    kb.size_bytes += doc.file_size
    kb.save(update_fields=['document_count', 'size_bytes', 'updated_at'])
    try:
        process_document(doc)
    except Exception as exc:  # 解析失败保留文档与错误信息，不影响其余条目
        logger.warning('天工入库文档处理失败 %s: %s', doc.pk, exc)
    return doc, ''


# ─── 搜索层（L1/L2/L3）────────────────────────────────────────────

def _queries_of(topics: str) -> list:
    """把主题拆成最多 N 个查询；去掉标点与重复项"""
    from backend.apps.knowledge import searxng

    parts = [part.strip() for part in re.split(r'[,，、;；\n]+', topics or '') if part.strip()]
    limit = searxng.config()['max_queries']
    return parts[:limit]


def _search_mcp(queries, actor=None) -> tuple:
    """L1：扫描候选 + 管理员确认后的绑定；无绑定不等于错误，交回 L2"""
    if not _flag(KEY_MCP):
        return [], 'mcp_disabled', 0
    from backend.apps.knowledge import mcp_search

    return mcp_search.search(queries, actor=actor)


def _search_searxng(queries, actor=None) -> tuple:
    from backend.apps.knowledge import searxng

    result = searxng.search(queries)
    log_audit(
        actor=actor, action='tiangong_search_searxng_call', target='SearXNG',
        detail=(f'查询 {result["queries"]} 个；结果 {len(result["results"])} 条；'
                f'耗时 {result["elapsed_ms"]}ms；原因 {result["reason"] or "ok"}；'
                f'引擎 {result["engines"] or "-"}'),
    )
    return result['results'], result['reason'], result['elapsed_ms']


def _search_bing(queries, actor=None) -> tuple:
    """L3：Bing 无头浏览器兜底；默认关闭，缺依赖/熔断/额度用尽都只影响本层"""
    from backend.apps.knowledge import bing

    return bing.search(queries, actor=actor)


def classify_source_type(domain: str) -> str:
    """按域名后缀粗分来源类型，仅用于候选列表分组，不参与抓取判定"""
    host = (domain or '').lower()
    if host.endswith(('.gov.cn', '.gov')) or '.gov.' in host:
        return 'government'
    if host.endswith(('.edu.cn', '.edu')) or '.ac.' in host:
        return 'university'
    if host.endswith(('.org.cn', '.org', '.ac.cn')):
        return 'organization'
    media_marks = ('news', 'daily', 'times', 'post', 'xinhuanet', 'people.com.cn',
                   'thepaper', 'caixin', 'jiemian', 'yicai')
    if any(mark in host for mark in media_marks):
        return 'media'
    if host.endswith(('.com.cn', '.com', '.cn')):
        return 'enterprise'
    return 'other'


def purge_expired_proposals(days: int = PROPOSAL_TTL_DAYS) -> int:
    """清理过期未审批候选（默认 30 天），避免候选页越积越多"""
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = TiangongSourceProposal.objects.filter(
        status='pending', created_at__lt=cutoff).delete()
    return deleted


def _record_proposals(kb: KnowledgeBase, run: TiangongRun, results: list,
                      discovered_by: str, topics: str) -> int:
    """搜索层结果落成候选提案；已在来源库或已存在同 URL 提案的跳过"""
    purge_expired_proposals()
    curated_domains = set(TiangongSource.objects.values_list('domain', flat=True))
    created = 0
    for index, item in enumerate(results):
        domain = item.get('domain') or ''
        url = item.get('url') or ''
        if not domain or not url or domain in curated_domains:
            continue
        if TiangongSourceProposal.objects.filter(
                url=url, status__in=('pending', 'approved')).exists():
            continue
        TiangongSourceProposal.objects.create(
            run=run,
            knowledge_base=kb,
            source_type=classify_source_type(domain),
            discovered_by=discovered_by,
            domain=domain[:255],
            url=url[:500],
            title=(item.get('title') or url)[:500],
            snippet=(item.get('snippet') or '')[:1000],
            topics=topics[:600],
            score=float(item.get('score') or max(1, len(results) - index)),
        )
        curated_domains.add(domain)
        created += 1
    return created


def _base_entry() -> dict:
    """没有搜索层命中时的降级链尾项：仅来源库/seeds 抓取"""
    return {'layer': 'l4_base', 'result': 'base', 'reason': 'base_only',
            'count': 0, 'elapsed_ms': 0}


def _discover(kb: KnowledgeBase, run: TiangongRun, keywords: list) -> None:
    """按 L1 → L2 → L3 顺序检索，命中即停；结果只写候选提案"""
    from backend.apps.knowledge import searxng

    queries = _queries_of(run.topics or topics_of(kb))
    run.search_queries = len(queries)
    chain = []
    if not queries:
        chain.append({'layer': 'discovery', 'result': 'skipped',
                      'reason': 'no_topics', 'count': 0, 'elapsed_ms': 0})
        chain.append(_base_entry())
        run.fallback_chain = chain
        run.discovery_layer = 'base'
        return

    min_results = searxng.config()['min_results']
    layers = [
        ('l1_mcp', _search_mcp),
        ('l2_searxng', _search_searxng),
        ('l3_bing', _search_bing),
    ]
    hit_layer = ''
    discovered_by = {'l1_mcp': 'mcp', 'l2_searxng': 'searxng', 'l3_bing': 'bing'}
    for layer, handler in layers:
        results, reason, elapsed = handler(queries, actor=run.actor)
        chain.append({
            'layer': layer,
            'result': 'hit' if len(results) >= min_results else ('empty' if not reason
                                                                 else 'failed'),
            'reason': reason,
            'count': len(results),
            'elapsed_ms': elapsed,
        })
        if len(results) >= min_results:
            run.search_hits = len(results)
            run.candidates = len(results)
            created = _record_proposals(kb, run, results, discovered_by[layer], run.topics)
            run.proposals = created
            chain[-1]['proposals'] = created
            hit_layer = layer
            break
    if not hit_layer:
        chain.append(_base_entry())
    run.fallback_chain = chain
    run.discovery_layer = hit_layer or 'base'


# ─── L4 来源库抓取 ────────────────────────────────────────────────

def _collect_web(kb: KnowledgeBase, run: TiangongRun, actor, keywords: list) -> dict:
    """来源库 + 临时采集入口抓正文；返回本轮聚合计数"""
    from backend.apps.nuwa import net

    stats = {'created': 0, 'skipped': 0, 'pages': 0, 'rejected': 0,
             'sources': 0, 'degraded': ''}
    max_pages = _int(KEY_CRAWL_MAX_PAGES, discovery.MAX_PAGES_PER_RUN,
                     1, discovery.HARD_PAGES_PER_RUN)
    per_source = _int(KEY_CRAWL_PER_SOURCE, discovery.MAX_PAGES_PER_SOURCE,
                      1, discovery.MAX_PAGES_PER_SOURCE)
    budget = discovery.CrawlBudget(max_pages)
    failures = []
    existing_urls = set(
        Document.objects.filter(knowledge_base=kb).exclude(source_url='')
        .values_list('source_url', flat=True))

    def ingest(page: dict) -> None:
        if page['content_sha256'] in seen_hashes:
            stats['skipped'] += 1
            return
        doc, reason = _create_document(
            kb, title=page['title'], text=page['text'], source_type='web',
            source_url=page['url'], source_site=page.get('site', ''), actor=actor)
        if doc is None:
            stats['skipped'] += 1
            if reason:
                failures.append(reason)
            return
        seen_hashes.add(page['content_sha256'])
        stats['created'] += 1

    seen_hashes = set()

    # 1) 知识库临时采集入口：最高优先级，不受主题门限约束
    from backend.apps.nuwa import knowledge_fetch

    for raw in parse_seeds(kb.tiangong_seeds):
        seed = knowledge_fetch.normalize_url(raw) or raw
        if budget.remaining() <= 0:
            break
        if seed in existing_urls:
            stats['skipped'] += 1
            continue
        try:
            page = discovery.fetch_page(seed)
        except net.RemoteError as exc:
            failures.append(exc.message or exc.reason)
            stats['skipped'] += 1
            continue
        issue = discovery.quality_issue(
            url=seed, title=page['title'], text=page['text'],
            html=page.get('html', ''), link_count=page.get('link_count', 0))
        if issue:
            stats['rejected'] += 1
            continue
        if not budget.take():
            break
        stats['pages'] += 1
        existing_urls.add(seed)
        ingest(page)

    # 2) 来源库：启用来源按优先级串行抓取
    for source in TiangongSource.objects.filter(enabled=True).order_by('priority', 'name'):
        if budget.remaining() <= 0:
            break
        if not discovery.source_matches(source.topic_tags, keywords):
            continue
        stats['sources'] += 1
        result = discovery.crawl_source(source, keywords, budget,
                                        existing_urls=existing_urls,
                                        max_pages=per_source)
        stats['pages'] += result['fetched']
        stats['rejected'] += result['rejected']
        stats['skipped'] += result['skipped']
        if result['reason']:
            failures.append(result['reason'])
        for page in result['pages']:
            existing_urls.add(page['url'])
            ingest(page)

    if failures:
        if stats['created'] == 0:
            stats['degraded'] = f'网页抓取失败：{failures[0][:120]}'
        else:
            stats['degraded'] = f'{len(failures)} 个页面未入库，其余已抓取'
    return stats


def _collect_model(kb: KnowledgeBase, run: TiangongRun, actor) -> tuple:
    """让模型按主题写一份知识摘要；未配置模型时如实降级"""
    from backend.apps.common import llm

    topics = run.topics or topics_of(kb)
    if not topics:
        return 0, '没有采集主题'
    fingerprint = hashlib.sha256(topics.encode('utf-8')).hexdigest()[:16]
    source_url = f'model://tiangong/{fingerprint}'
    if Document.objects.filter(knowledge_base=kb, source_url=source_url).exists():
        return 0, '同一主题已生成过模型知识'

    try:
        config = llm.load_model_config()
    except Exception as exc:
        return 0, f'模型未配置：{exc}'
    prompt = (
        '你是一名企业知识工程师。请围绕下面的主题，写一份可直接作为知识库文档的中文资料。\n'
        '要求：1) 用 Markdown，二级标题分节；2) 只写通用、可验证的常识与方法论，'
        '不要编造具体数字、法条编号、机构名称或链接；3) 不确定的地方明确写「需人工核实」；'
        '4) 开头用一句话说明这是模型生成的整理稿，不是原始出处。\n\n'
        f'主题：{topics}'
    )
    try:
        text = llm.chat_completion(
            [{'role': 'user', 'content': prompt}],
            temperature=0.3, max_tokens=MODEL_MAX_TOKENS, timeout=120,
        )
    except Exception as exc:
        return 0, f'模型生成失败：{type(exc).__name__}: {exc}'[:160]
    if not text or len(text.strip()) < 80:
        return 0, '模型返回内容过短'
    doc, reason = _create_document(
        kb, title=f'{topics[:80]} · 模型知识摘要', text=text.strip(),
        source_type='model', source_url=source_url,
        source_model=str(config.get('model') or '')[:100], actor=actor)
    if doc is None:
        return 0, reason
    return 1, ''


def execute(run_id: int) -> None:
    """执行一次采集；任何失败都落库成可读状态，不抛给调用方"""
    run = TiangongRun.objects.select_related('knowledge_base', 'actor').filter(pk=run_id).first()
    if run is None or run.status not in ('queued',):
        return
    kb = run.knowledge_base
    run.status = 'running'
    run.save(update_fields=['status'])
    kb.tiangong_status = 'running'
    kb.save(update_fields=['tiangong_status', 'updated_at'])

    degraded = []
    try:
        if not enabled():
            degraded.append('管理员已关闭天工模式')
        else:
            if not kb.tiangong_enabled:
                degraded.append('该知识库未开启天工模式')
            else:
                keywords = discovery.keywords_of(run.topics or topics_of(kb))
                if online_enabled():
                    if discovery_enabled():
                        _discover(kb, run, keywords)
                    else:
                        run.discovery_layer = 'base'
                        run.fallback_chain = [{'layer': 'discovery', 'result': 'skipped',
                                               'reason': 'discovery_disabled',
                                               'count': 0, 'elapsed_ms': 0}]
                    stats = _collect_web(kb, run, run.actor, keywords)
                    run.web_created = stats['created']
                    run.skipped += stats['skipped']
                    run.pages_fetched = stats['pages']
                    run.quality_rejected = stats['rejected']
                    run.sources_tried = stats['sources']
                    run.approved = stats['sources']
                    if stats['degraded']:
                        degraded.append(stats['degraded'])
                else:
                    degraded.append('联网采集已关闭，仅尝试模型侧')
                model_created, model_reason = _collect_model(kb, run, run.actor)
                run.model_created = model_created
                if model_reason:
                    degraded.append(model_reason)
    except Exception as exc:
        logger.exception('天工采集异常 run=%s', run_id)
        run.status = 'failed'
        run.error = f'{type(exc).__name__}: {exc}'[:300]
        run.degraded_reason = '；'.join(degraded)[:200]
        run.finished_at = timezone.now()
        run.save()
        kb.tiangong_status = 'failed'
        kb.tiangong_last_error = run.error
        kb.tiangong_last_run_at = run.finished_at
        kb.save(update_fields=['tiangong_status', 'tiangong_last_error',
                               'tiangong_last_run_at', 'updated_at'])
        log_audit(actor=run.actor, action='tiangong_run_finished',
                  target=kb.name, detail=f'失败：{run.error}')
        return

    run.finished_at = timezone.now()
    run.degraded_reason = '；'.join([item for item in degraded if item])[:200]
    created = run.web_created + run.model_created
    if created:
        run.status = 'done'
    elif not enabled() or not kb.tiangong_enabled:
        run.status = 'skipped'
    elif run.degraded_reason:
        run.status = 'failed'
    else:
        run.status = 'done'
    run.save()

    kb.tiangong_last_run_at = run.finished_at
    kb.tiangong_last_error = run.error or run.degraded_reason
    kb.tiangong_status = 'done' if run.status == 'done' else 'failed'
    kb.save(update_fields=['tiangong_status', 'tiangong_last_error',
                           'tiangong_last_run_at', 'updated_at'])

    log_audit(actor=run.actor, action='tiangong_run_finished', target=kb.name,
              detail=(f'queries={run.search_queries}；layer_used={run.discovery_layer or "-"}；'
                      f'chain={json.dumps(run.fallback_chain, ensure_ascii=False)}；'
                      f'candidates={run.candidates}；proposals={run.proposals}；'
                      f'sources_tried={run.sources_tried}；'
                      f'quality_rejected={run.quality_rejected}；'
                      f'pages={run.pages_fetched}；web={run.web_created}；'
                      f'model={run.model_created}；skipped={run.skipped}；'
                      f'degraded={run.degraded_reason or "无"}'))
