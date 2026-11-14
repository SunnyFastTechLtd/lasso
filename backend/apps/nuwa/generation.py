"""骨架生成：模板 + 单次结构化输出 + 最多一次修复

可重复性靠三样东西落库：brief_hash、模板版本、模型名 + 原始输出。
解析失败不编造字段，返回空白可编辑骨架并把错误带回前端。
"""
import hashlib
import json
import logging
import re
import time

from backend.apps.common.llm import ModelConfigError, chat_completion

from backend.apps.nuwa import config

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE_VERSION = 'v1'

SPEECH_TIERS = {'low', 'mid', 'high'}
RETRIEVAL_TIERS = {'few', 'standard', 'many'}
VISIBILITY_VALUES = {'private', 'department', 'public'}
PLACEHOLDERS = {'current_date', 'user_name', 'department'}
NAME_MAX = 30
DESC_MAX = 200
PROMPT_MAX = 4000
BRIEF_MAX = 2000

SYSTEM_TEMPLATE = """你是企业智能体平台的装配助手。根据用户描述，产出一个智能体的配置骨架。

只输出一个 JSON 对象，不要输出解释、不要用 Markdown 代码块。字段：
- name：2–30 字，中文优先，体现岗位/场景，不要带引号
- description：≤200 字，说明"做什么、给谁用、输出什么"
- job_tags：字符串数组，从 [行政, 人事, 财务, 法务, 研发, 市场, 销售, 客服, 知识管理, 数据分析] 中选 1–3 个
- icon：从 [Bot, FileText, Search, Database, Calculator, Scale, Code, ChartBar, Headphones, BookOpen] 中选一个
- prompt：系统提示词，包含角色、职责边界、输出格式、拒答口径；不要编造企业内部事实；可用占位符只有 {current_date} {user_name} {department}
- speech_tier：low（照本宣科）/ mid（平衡）/ high（多发挥）
- retrieval_tier：few（一次查不到就直说）/ standard（允许少量补查）/ many（允许多次查询后汇总）
- welcome_message：≤120 字，第一人称的一句话开场
- visibility_suggestion：private / department / public
- knowledge_queries：≤6 条中文检索关键词，用于找资料

不确定的字段给保守默认值，不要留空，不要输出 null。"""


def brief_hash(brief: str) -> str:
    return hashlib.sha256((brief or '').strip().encode('utf-8')).hexdigest()


def _user_prompt(brief: str, audience: str, models: list, departments: list) -> str:
    return (
        f'用户描述：{brief}\n'
        f'使用对象：{audience or "未指定"}\n'
        f'可选模型：{"、".join(models) if models else "平台未配置"}\n'
        f'可选部门：{"、".join(departments) if departments else "未提供"}\n'
        '请按系统说明输出 JSON。'
    )


def _extract_json(text: str) -> dict:
    text = (text or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\n?', '', text)
        text = re.sub(r'\n?```$', '', text).strip()
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('输出中没有 JSON 对象')
    return json.loads(text[start:end + 1])


def _clip(value, limit: int) -> str:
    return str(value or '').strip()[:limit]


def normalize_skeleton(raw: dict, brief: str, models: list) -> dict:
    """把模型输出收敛到平台枚举与长度限制内，未知值给保守默认"""
    name = _clip(raw.get('name'), NAME_MAX) or (_clip(brief, 12) or '新建智能体')
    speech = str(raw.get('speech_tier') or 'mid').lower()
    retrieval = str(raw.get('retrieval_tier') or 'standard').lower()
    visibility = str(raw.get('visibility_suggestion') or 'private').lower()
    tags = raw.get('job_tags')
    if not isinstance(tags, list):
        tags = []
    skeleton = {
        'name': name,
        'description': _clip(raw.get('description'), DESC_MAX),
        'job_tags': [str(tag)[:20] for tag in tags][:3],
        'icon': _clip(raw.get('icon'), 64) or 'Bot',
        'prompt': _clip(raw.get('prompt'), PROMPT_MAX),
        'speech_tier': speech if speech in SPEECH_TIERS else 'mid',
        'retrieval_tier': retrieval if retrieval in RETRIEVAL_TIERS else 'standard',
        'welcome_message': _clip(raw.get('welcome_message'), 120),
        'visibility': visibility if visibility in VISIBILITY_VALUES else 'private',
        'knowledge_queries': [str(q)[:40] for q in (raw.get('knowledge_queries') or [])
                              if str(q).strip()][:6],
        'model_name': models[0] if models else '',
    }
    unknown = sorted({item for item in re.findall(r'\{([a-zA-Z_][\w]*)\}', skeleton['prompt'])
                      if item not in PLACEHOLDERS})
    if unknown:
        skeleton['unknown_placeholders'] = unknown
    return skeleton


def blank_skeleton(brief: str, models: list) -> dict:
    """LLM 不可用或解析失败时的可编辑空骨架（不编造内容）"""
    return {
        'name': _clip(brief, 12) or '新建智能体',
        'description': _clip(brief, DESC_MAX),
        'job_tags': [],
        'icon': 'Bot',
        'prompt': '',
        'speech_tier': 'mid',
        'retrieval_tier': 'standard',
        'welcome_message': '',
        'visibility': 'private',
        'knowledge_queries': [],
        'model_name': models[0] if models else '',
        'is_blank': True,
    }


def generate(brief: str, *, audience: str = '', models: list | None = None,
             departments: list | None = None, force: bool = False,
             previous: dict | None = None) -> dict:
    """返回 {skeleton, meta, error}；error 非空表示走了兜底骨架"""
    models = models or []
    departments = departments or []
    started = time.monotonic()
    meta = {
        'template_version': PROMPT_TEMPLATE_VERSION,
        'brief_hash': brief_hash(brief),
        'model': models[0] if models else '',
        'raw_output': '',
        'retries': 0,
        'duration_ms': 0,
    }
    messages = [
        {'role': 'system', 'content': SYSTEM_TEMPLATE},
        {'role': 'user', 'content': _user_prompt(brief, audience, models, departments)},
    ]
    error = ''
    for attempt in range(2):
        try:
            text = chat_completion(messages, temperature=0.2, max_tokens=1800,
                                   timeout=60, model=models[0] if models else None)
            meta['raw_output'] = text
            skeleton = normalize_skeleton(_extract_json(text), brief, models)
            meta['duration_ms'] = int((time.monotonic() - started) * 1000)
            return {'skeleton': skeleton, 'meta': meta, 'error': ''}
        except ModelConfigError as exc:
            error = str(exc)
            break
        except Exception as exc:
            logger.warning('女娲骨架生成第 %s 次失败: %s', attempt + 1, exc)
            error = f'{type(exc).__name__}: {exc}'[:300]
            meta['retries'] = attempt + 1
            if attempt == 0:
                messages = messages + [
                    {'role': 'assistant', 'content': meta.get('raw_output') or '（空）'},
                    {'role': 'user',
                     'content': f'上一次输出无法解析（{error}）。请只输出一个合法 JSON 对象。'},
                ]
    meta['duration_ms'] = int((time.monotonic() - started) * 1000)
    skeleton = previous or blank_skeleton(brief, models)
    return {'skeleton': skeleton, 'meta': meta, 'error': error}


def knowledge_queries(skeleton: dict, brief: str) -> list:
    """资料检索关键词：优先用骨架给的，没有再退回描述分句"""
    queries = list(skeleton.get('knowledge_queries') or [])
    if not queries:
        queries = [part.strip() for part in re.split(r'[，。；,;.\n]', brief)
                   if 2 <= len(part.strip()) <= 20][:6]
    return queries[:6]


def allowed_daily_sessions(user) -> tuple:
    """每用户每日会话上限，返回 (是否允许, 已用, 上限)"""
    from django.utils import timezone

    from backend.apps.nuwa.models import NuwaSession

    limit = config.daily_limit()
    today = timezone.localdate()
    used = NuwaSession.objects.filter(owner=user, created_at__date=today).count()
    return used < limit, used, limit
