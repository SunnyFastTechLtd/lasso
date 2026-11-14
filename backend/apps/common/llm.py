"""
统一大模型服务层（基于 OpenAI 兼容 API）

支持所有兼容 OpenAI 格式的服务：
- 本地 Ollama（base_url=http://127.0.0.1:11434/v1）
- vLLM / llama.cpp 服务端
- OpenRouter / 其他第三方代理
- 官方 OpenAI（base_url=https://api.openai.com/v1）

后续本地大模型发布也按 OpenAI 兼容格式暴露接口，直接接入即可。
"""
import json
import logging
import time
from typing import List, Dict, Optional, Generator, Any

logger = logging.getLogger(__name__)

try:
    from openai import OpenAI, APIError, APIConnectionError, APITimeoutError, AuthenticationError
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False


# 单例客户端缓存
_client: Optional['OpenAI'] = None

# 配置缓存（避免每次请求都查库）
# 带 TTL：多 worker 部署时 reset_client 只清当前进程，其余进程最长 TTL 秒后
# 自动重新加载，避免配置更新后各 worker 长期不一致
_config_cache: Dict[str, str] = {}
_config_loaded = False
_config_loaded_at = 0.0
_CONFIG_CACHE_TTL = 60.0


def _load_config_from_db() -> Dict[str, str]:
    """从数据库加载配置（带 TTL 缓存）"""
    global _config_cache, _config_loaded, _config_loaded_at
    if _config_loaded and (time.monotonic() - _config_loaded_at) < _CONFIG_CACHE_TTL:
        return _config_cache

    try:
        from backend.apps.settings.models import SystemSetting
        qs = SystemSetting.objects.filter(key__startswith='llm_')
        _config_cache = {item.key: item.value for item in qs}
    except Exception as e:
        # 加载失败（如启动竞态）时不缓存，下次调用重试；
        # 不回退到环境变量，调用方会按「未配置」明确失败
        logger.warning('LLM 配置从数据库加载失败，下次调用时重试: %s', e)
        _config_cache = {}
        return _config_cache

    _config_loaded = True
    _config_loaded_at = time.monotonic()
    return _config_cache


# 模型配置必填项：缺任何一项都视为未配置，由调用方明确报错
MODEL_REQUIRED_FIELDS = {
    'llm_base_url': 'API 地址',
    'llm_api_key': 'API Key',
    'llm_model': '模型名',
}
MODEL_CONFIG_HINT = '请到「系统设置 → 模型接入与凭据」填写并保存完整的接入配置。'

# 推理参数不是必填项，库里没有时使用代码内默认值（不是环境变量）
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT = 120


class ModelConfigError(RuntimeError):
    """数据库里没有可用的模型配置。

    不静默回落到环境变量，也不塞默认 base_url/api_key/model：宁可明确失败，
    让运维知道要去平台的模型配置界面补齐。
    """

    def __init__(self, missing: Optional[List[str]] = None):
        self.missing = list(missing or [])
        if self.missing:
            fields = '、'.join(MODEL_REQUIRED_FIELDS[key] for key in self.missing)
            message = f'平台尚未配置可用的模型接入：缺少 {fields}。{MODEL_CONFIG_HINT}'
        else:
            message = f'平台尚未配置可用的模型接入。{MODEL_CONFIG_HINT}'
        super().__init__(message)


def _number(raw, cast, default):
    """可选推理参数解析失败时用代码内默认值，不回落环境变量"""
    if raw in (None, ''):
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        logger.warning('模型配置中的数值项无法解析（%r），使用默认值 %s', raw, default)
        return default


def get_llm_config(key: str, default=None):
    """获取模型配置项：唯一来源是数据库 SystemSetting。

    空值/缺失返回 default，不再读取 settings 或环境变量。
    """
    db_config = _load_config_from_db()
    value = db_config.get(key)
    if value is not None and value != '':
        return value
    return default


def load_model_config() -> Dict[str, Any]:
    """读取并校验数据库模型配置，缺失必填项时抛 ModelConfigError。"""
    db_config = _load_config_from_db()
    missing = [
        key for key in MODEL_REQUIRED_FIELDS
        if not str(db_config.get(key) or '').strip()
    ]
    if missing:
        raise ModelConfigError(missing)

    return {
        'base_url': str(db_config['llm_base_url']).strip(),
        'api_key': str(db_config['llm_api_key']).strip(),
        'model': str(db_config['llm_model']).strip(),
        'temperature': _number(db_config.get('llm_temperature'), float, DEFAULT_TEMPERATURE),
        'max_tokens': _number(db_config.get('llm_max_tokens'), int, DEFAULT_MAX_TOKENS),
        'timeout': _number(db_config.get('llm_timeout'), int, DEFAULT_TIMEOUT),
    }


def reset_client():
    """重置客户端和配置缓存（配置变更时调用）"""
    global _client, _config_loaded
    _client = None
    _config_loaded = False


def get_client() -> 'OpenAI':
    """获取 OpenAI 兼容客户端（单例）"""
    global _client
    if _client is not None:
        return _client

    if not HAS_OPENAI:
        raise RuntimeError('openai SDK 未安装，请先执行 pip install openai')

    # 缺必填项时直接抛 ModelConfigError，不做任何环境变量兜底
    cfg = load_model_config()

    _client = OpenAI(
        base_url=cfg['base_url'],
        api_key=cfg['api_key'],
        timeout=cfg['timeout'],
    )
    return _client


# 候选配置字段名（前端/序列化器口径）→ SystemSetting 键名
_CANDIDATE_KEY_MAP = {
    'base_url': 'llm_base_url',
    'api_key': 'llm_api_key',
    'model': 'llm_model',
    'temperature': 'llm_temperature',
    'max_tokens': 'llm_max_tokens',
    'timeout': 'llm_timeout',
}


def _resolve_config(candidate: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """候选配置覆盖当前生效配置，得到一份完整的探测用配置。

    只从数据库读取当前值；候选值用于保存前的连通性验证。缺必填项时抛
    ModelConfigError，调用方（probe_model）转成可操作的失败结果。
    """
    cfg = {v: get_llm_config(v) for v in _CANDIDATE_KEY_MAP.values()}
    if candidate:
        for field, key in _CANDIDATE_KEY_MAP.items():
            value = candidate.get(field)
            if value is not None and value != '':
                cfg[key] = str(value)

    missing = [
        key for key in MODEL_REQUIRED_FIELDS
        if not str(cfg.get(key) or '').strip()
    ]
    if missing:
        raise ModelConfigError(missing)
    return cfg


def check_health() -> Dict[str, Any]:
    """检查大模型服务健康状态（轻探测，只打通模型清单接口）

    会话每轮开头都会调它（chat/views），因此这里不做真实补全，避免每条消息
    多付一次推理开销；需要"该模型确实可用"的结论时用 probe_model()。
    数据库配置不完整时返回 not_configured，并给出可操作的提示。
    """
    try:
        cfg = load_model_config()
    except ModelConfigError as e:
        return {
            'status': 'not_configured',
            'base_url': get_llm_config('llm_base_url', ''),
            'error': str(e),
            'missing_fields': e.missing,
        }

    base_url = cfg['base_url']
    try:
        client = get_client()
        models = client.models.list()
        model_list = [m.id for m in models.data]
        return {
            'status': 'ok',
            'base_url': base_url,
            'current_model': cfg['model'],
            'available_models': model_list[:20],  # 最多返回 20 个
            'model_count': len(model_list),
        }
    except APIConnectionError as e:
        return {
            'status': 'offline',
            'base_url': base_url,
            'error': f'无法连接大模型服务：{e}',
        }
    except AuthenticationError as e:
        return {
            'status': 'unauthorized',
            'base_url': base_url,
            'error': f'API Key 无效：{e}',
        }
    except Exception as e:
        return {
            'status': 'error',
            'base_url': base_url,
            'error': str(e),
        }


def probe_model(candidate: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """F1 保存前的深度检查：按候选配置真实发一次 1 token 补全。

    candidate 传入「尚未落库的候选配置」时按候选探测——临时建客户端，不写单例、
    不改配置缓存，因此现网生效配置不受影响；不传则探测当前生效配置。
    通过标准是一次成功的对话调用：清单接口能通不代表该模型可用。
    """
    try:
        cfg = _resolve_config(candidate)
    except ModelConfigError as e:
        return {
            'status': 'not_configured',
            'base_url': get_llm_config('llm_base_url', ''),
            'current_model': get_llm_config('llm_model', ''),
            'error': str(e),
            'missing_fields': e.missing,
        }

    base_url = cfg['llm_base_url']
    model = cfg['llm_model']
    result: Dict[str, Any] = {'base_url': base_url, 'current_model': model}

    if not HAS_OPENAI:
        return {**result, 'status': 'error', 'error': 'openai SDK 未安装，请先执行 pip install openai'}

    probe_timeout = min(int(cfg['llm_timeout'] or DEFAULT_TIMEOUT), 30)
    try:
        client = OpenAI(base_url=base_url, api_key=cfg['llm_api_key'], timeout=probe_timeout)
        client.chat.completions.create(
            model=model,
            messages=[{'role': 'user', 'content': 'ping'}],
            max_tokens=1,
            temperature=0,
        )
    except APIConnectionError as e:
        return {**result, 'status': 'offline', 'error': f'无法连接大模型服务：{e}'}
    except AuthenticationError as e:
        return {**result, 'status': 'unauthorized', 'error': f'API Key 无效：{e}'}
    except Exception as e:
        return {**result, 'status': 'error', 'error': f'模型「{model}」调用失败：{e}'}

    model_list: List[str] = []
    try:
        model_list = [m.id for m in client.models.list().data]
    except Exception as e:
        # 清单接口不支持/偶发失败不影响连通结论，仅少一份可选模型
        logger.info('获取模型清单失败（忽略）: %s', e)

    return {
        **result,
        'status': 'ok',
        'verified_model': model,
        'available_models': model_list[:20],  # 最多返回 20 个
        'model_count': len(model_list),
    }


def normalize_usage(usage) -> Dict[str, int]:
    """把模型服务返回的 usage 归一成落库字段；缺失时返回零值"""
    return {
        'input_tokens': int(getattr(usage, 'prompt_tokens', 0) or 0),
        'output_tokens': int(getattr(usage, 'completion_tokens', 0) or 0),
        'total_tokens': int(getattr(usage, 'total_tokens', 0) or 0),
    }


def chat_completion(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    stream: bool = False,
    timeout: Optional[float] = None,
    return_usage: bool = False,
) -> Any:
    """
    对话补全

    Args:
        messages: 对话消息列表，格式为 [{"role": "user", "content": "..."}]
        model: 模型名称，默认使用配置中的模型
        temperature: 温度参数
        max_tokens: 最大输出 token 数
        stream: 是否流式输出
        timeout: 单次请求超时（秒），不传则使用客户端全局超时
        return_usage: 非流式时是否连同真实 token 用量一起返回

    Returns:
        非流式：默认返回完整回答文本字符串；return_usage=True 时返回
                (文本, {"input_tokens": …, "output_tokens": …, "total_tokens": …})
        流式：返回生成器，yield 每个 token 文本
    """
    # 模型选择只认数据库激活配置；缺必填项时抛 ModelConfigError（可操作提示）
    cfg = load_model_config()
    client = get_client()
    model = model or cfg['model']
    temperature = temperature if temperature is not None else cfg['temperature']
    max_tokens = max_tokens or cfg['max_tokens']

    if stream:
        return _chat_stream(
            client=client,
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    text = response.choices[0].message.content or ''
    if return_usage:
        return text, normalize_usage(getattr(response, 'usage', None))
    return text


def _chat_stream(client, messages, model, temperature, max_tokens) -> Generator[str, None, None]:
    """流式对话生成器"""
    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
        stream_options={'include_usage': True},
    )
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


# ─── 常用 Prompt 构建工具 ───

def build_rag_prompt(question: str, contexts: List[Dict[str, Any]]) -> str:
    """
    构建 RAG 问答的 Prompt

    Args:
        question: 用户问题
        contexts: 检索到的上下文，每项包含 title、content、score 等字段

    Returns:
        组装好的 system prompt 中的参考资料部分
    """
    context_text = '\n\n'.join([
        f"【文档 {i+1}】{ctx.get('title', '未知文档')}\n{ctx.get('content', '')}"
        for i, ctx in enumerate(contexts)
    ])

    return f"""你是一个专业的知识库问答助手。请基于以下参考资料回答用户的问题。

参考资料：
{context_text}

用户问题：{question}

回答要求：
1. 仅基于参考资料中的信息回答，不要编造
2. 如果参考资料中没有相关信息，明确告知用户"该问题暂无相关资料"
3. 回答要条理清晰，重点突出
4. 引用相关文档时，标注文档序号"""


def build_reading_prompt(question: str, document_content: str, page_range: str = '') -> str:
    """
    构建 AI 阅读的 Prompt

    Args:
        question: 用户问题
        document_content: 文档内容摘要
        page_range: 页码范围
    """
    range_text = f'（重点参考第 {page_range} 页）' if page_range else ''

    return f"""你是一个专业的文档阅读助手。请基于以下文档内容回答用户的问题{range_text}。

文档内容：
{document_content}

用户问题：{question}

回答要求：
1. 仅基于文档内容回答，不要编造
2. 回答要准确、简洁、有条理
3. 重要信息可以适当展开说明"""


# ─── SSE 流式输出格式 ───

def format_sse_event(event: str, data: str) -> bytes:
    """
    格式化 SSE 事件

    Args:
        event: 事件类型（start / chunk / end / error）
        data: 事件数据

    Returns:
        SSE 格式的字节串
    """
    return f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'.encode('utf-8')
