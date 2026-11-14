"""
Qdrant 向量存储层

每个知识库对应一个独立 collection（{prefix}_kb_{id}），point id 即
DocumentChunk 主键，payload 仅存 doc_id 用于按文档删除；
分块内容仍由 DB（DocumentChunk）持有，检索命中后按 id 回表取内容。

settings.VECTOR_STORE == 'qdrant' 时启用，否则调用方走本地 numpy 余弦检索。
"""
import logging
from typing import Dict, List, Optional, Tuple

from django.conf import settings

logger = logging.getLogger(__name__)

try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        Distance, FieldCondition, Filter, FilterSelector,
        MatchValue, PointStruct, VectorParams,
    )
    HAS_QDRANT = True
except ImportError:
    QdrantClient = None
    HAS_QDRANT = False

# 客户端单例（懒加载）
_client: Optional['QdrantClient'] = None


def is_enabled() -> bool:
    """是否启用 Qdrant 向量检索"""
    return getattr(settings, 'VECTOR_STORE', 'local') == 'qdrant'


def get_client() -> 'QdrantClient':
    """获取 Qdrant 客户端单例"""
    global _client
    if _client is not None:
        return _client

    if not HAS_QDRANT:
        raise RuntimeError('qdrant-client 未安装，请先执行 pip install qdrant-client')

    _client = QdrantClient(
        host=getattr(settings, 'QDRANT_HOST', '127.0.0.1'),
        port=int(getattr(settings, 'QDRANT_PORT', 6333)),
        timeout=30,
    )
    return _client


def reset_client() -> None:
    """重置客户端单例（测试用）"""
    global _client
    _client = None


def collection_name(kb_id: int) -> str:
    prefix = getattr(settings, 'QDRANT_COLLECTION_PREFIX', 'ai_shangke')
    return f'{prefix}_kb_{kb_id}'


def ensure_collection(kb_id: int, dim: int) -> str:
    """
    确保知识库 collection 存在且向量维度匹配

    Raises:
        ValueError: collection 已存在但维度与当前嵌入模型不一致（需重建索引）
    """
    client = get_client()
    name = collection_name(kb_id)

    def _check_dim() -> str:
        info = client.get_collection(name)
        existing_dim = info.config.params.vectors.size
        if existing_dim != dim:
            raise ValueError(
                f'collection {name} 向量维度为 {existing_dim}，与当前嵌入模型维度 {dim} 不一致，'
                f'请执行 python manage.py reembed_knowledge 重建索引'
            )
        return name

    if client.collection_exists(name):
        return _check_dim()

    try:
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
    except Exception as exc:
        # 并发上传时多个任务可能同时创建同一 collection，409 冲突视为已成功
        if 'already exists' in str(exc):
            return _check_dim()
        raise
    logger.info('创建 Qdrant collection %s（dim=%d）', name, dim)
    return name


def upsert_chunks(kb_id: int, points: List[Tuple[int, List[float], int]]) -> None:
    """
    写入/更新分块向量

    Args:
        kb_id: 知识库 ID
        points: (chunk_id, vector, doc_id) 三元组列表
    """
    if not points:
        return
    dim = len(points[0][1])
    name = ensure_collection(kb_id, dim)
    get_client().upsert(
        collection_name=name,
        points=[
            PointStruct(id=chunk_id, vector=vector, payload={'doc_id': doc_id})
            for chunk_id, vector, doc_id in points
        ],
    )


def search(kb_id: int, vector: List[float], top_k: int = 5) -> List[Tuple[int, float]]:
    """
    ANN 检索，返回 (chunk_id, score) 列表，按相似度降序

    collection 不存在（尚无文档入库）时返回空列表。
    """
    client = get_client()
    name = collection_name(kb_id)
    if not client.collection_exists(name):
        return []

    # 注意：qdrant-client 1.16+ 已移除旧 search() 方法，统一用 query_points
    response = client.query_points(
        collection_name=name,
        query=vector,
        limit=top_k,
        with_payload=False,
    )
    return [(hit.id, float(hit.score)) for hit in response.points]


def delete_document_vectors(kb_id: int, doc_id: int) -> None:
    """删除指定文档的全部向量（文档删除时调用）"""
    client = get_client()
    name = collection_name(kb_id)
    if not client.collection_exists(name):
        return
    client.delete(
        collection_name=name,
        points_selector=FilterSelector(
            filter=Filter(must=[FieldCondition(key='doc_id', match=MatchValue(value=doc_id))])
        ),
    )


def delete_points(kb_id: int, chunk_ids: List[int]) -> None:
    """按分块 id 删除向量点（分块禁检时调用）"""
    if not chunk_ids:
        return
    client = get_client()
    name = collection_name(kb_id)
    if not client.collection_exists(name):
        return
    client.delete(collection_name=name, points_selector=list(chunk_ids))


def drop_collection(kb_id: int) -> None:
    """删除整个知识库 collection（知识库删除时调用）"""
    client = get_client()
    name = collection_name(kb_id)
    if client.collection_exists(name):
        client.delete_collection(name)


def build_chunks_map(chunk_ids: List[int]) -> Dict[int, 'object']:
    """按 id 批量取 DocumentChunk（含所属文档），供检索结果回表组装"""
    from .models import DocumentChunk
    chunks = DocumentChunk.objects.filter(id__in=chunk_ids).select_related('document')
    return {c.id: c for c in chunks}
