"""
知识库异步任务

文档向量化（提取 → 分块 → embedding → 入库/Qdrant）是 CPU + 模型密集操作，
大文档可能耗时数十秒，丢给 Celery worker 异步执行，避免阻塞上传请求。
"""
import logging

from celery import shared_task

from .models import Document
from .services import process_document

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=0, queue='knowledge',
             acks_late=True, soft_time_limit=900, time_limit=960)
def process_document_task(self, doc_id: int) -> None:
    """
    异步执行文档处理流水线

    走独立 knowledge 队列（见 deploy/systemd/lasso-knowledge-worker.service）：
    OCR + 向量化是重任务，不和对话/智能体抢 worker。acks_late 保证 worker 被杀后
    任务回到队列；软超时 15 分钟，硬超时 16 分钟。

    文档不存在（已删除）时直接忽略；处理失败由 process_document
    内部兜底（标记 failed + error_msg），不再重试。
    """
    try:
        doc = Document.objects.get(id=doc_id)
    except Document.DoesNotExist:
        logger.warning('process_document_task：文档 %s 不存在，跳过', doc_id)
        return

    process_document(doc)


@shared_task(bind=True, max_retries=0, queue='knowledge')
def tiangong_run_task(self, run_id: int) -> None:
    """天工采集：建库自动跑首轮与手动补充共用；失败由 tiangong.execute 兜底

    与文档处理同走 knowledge 队列：抓取/解析是重任务，且避免落到默认 celery
    队列被旧版 lasso-worker 用旧代码执行（首轮运行曾因此跑出旧格式审计）。
    """
    from . import tiangong

    try:
        tiangong.execute(run_id)
    except Exception:  # execute 已落库，这里只兜住任务本身
        logger.exception('tiangong_run_task 异常 run=%s', run_id)
