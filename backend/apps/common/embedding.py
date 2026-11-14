"""
统一嵌入向量服务层（纯本地）

本地 fastembed（默认 BAAI/bge-small-zh-v1.5，512 维），完全离线，
与一体机「无需联网」定位一致；不消耗任何云端 token。

bge 系列模型官方用法：检索问题（query）需加指令前缀、文档段落（passage）不加，
fastembed 的 query_embed / passage_embed 已内置该处理，因此本层区分两个入口。
"""
import logging
from typing import List, Optional

from django.conf import settings

logger = logging.getLogger(__name__)

try:
    from fastembed import TextEmbedding
    HAS_FASTEMBED = True
except ImportError:
    TextEmbedding = None
    HAS_FASTEMBED = False

# 本地模型单例（懒加载，首次调用时才加载模型）
_local_model: Optional['TextEmbedding'] = None


def _get_local_model() -> 'TextEmbedding':
    """获取本地 fastembed 模型单例"""
    global _local_model
    if _local_model is not None:
        return _local_model

    if not HAS_FASTEMBED:
        raise RuntimeError('fastembed 未安装，请先执行 pip install fastembed')

    model_name = getattr(settings, 'FASTEMBED_MODEL', 'BAAI/bge-small-zh-v1.5')
    cache_dir = getattr(settings, 'FASTEMBED_CACHE_DIR', None)
    logger.info('加载本地嵌入模型 %s（缓存目录 %s）', model_name, cache_dir)
    _local_model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
    return _local_model


def reset_local_model() -> None:
    """重置本地模型单例（测试用）"""
    global _local_model
    _local_model = None


def embed_texts(texts: List[str]) -> List[List[float]]:
    """
    文档/段落向量化（知识库分块入库用）

    Args:
        texts: 文本列表

    Returns:
        向量列表，与输入一一对应
    """
    if not texts:
        return []

    model = _get_local_model()
    return [list(map(float, vec)) for vec in model.passage_embed(texts)]


def embed_query(text: str) -> List[float]:
    """
    检索问题向量化（知识库查询用，bge 会对 query 加检索指令前缀）

    Args:
        text: 单个查询文本

    Returns:
        查询向量
    """
    model = _get_local_model()
    return list(map(float, next(iter(model.query_embed([text])))))
