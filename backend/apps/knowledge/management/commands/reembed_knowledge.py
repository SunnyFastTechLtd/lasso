"""
重建知识库向量索引

切换嵌入模型（如 Ark → 本地 bge-small-zh）或切换向量库（local → qdrant）后，
存量分块的向量已失效，需对全部文档重跑处理流水线（提取 → 分块 → 向量化 → 入库）。

用法：
    python manage.py reembed_knowledge            # 同步重建全部文档
    python manage.py reembed_knowledge --kb 3     # 仅重建指定知识库
    python manage.py reembed_knowledge --enqueue  # 丢给 Celery worker 异步执行
"""
from django.core.management.base import BaseCommand

from backend.apps.knowledge.models import Document
from backend.apps.knowledge.services import process_document
from backend.apps.knowledge.tasks import process_document_task


class Command(BaseCommand):
    help = '重建知识库向量索引（切换嵌入模型/向量库后执行）'

    def add_arguments(self, parser):
        parser.add_argument('--kb', type=int, default=None, help='仅重建指定知识库 ID')
        parser.add_argument('--enqueue', action='store_true', help='丢给 Celery 异步执行')

    def handle(self, *args, **options):
        qs = Document.objects.order_by('id')
        if options['kb']:
            qs = qs.filter(knowledge_base_id=options['kb'])

        total = qs.count()
        self.stdout.write(f'待重建文档数：{total}')

        ok = failed = 0
        for doc in qs:
            if options['enqueue']:
                process_document_task.delay(doc.id)
                ok += 1
                continue

            process_document(doc)
            doc.refresh_from_db()
            if doc.status == 'completed':
                ok += 1
            else:
                failed += 1
                self.stderr.write(f'  [失败] 文档 {doc.id}《{doc.title}》：{doc.error_msg}')
            self.stdout.write(f'  [{ok + failed}/{total}] {doc.title} → {doc.status}')

        verb = '已投递' if options['enqueue'] else '完成'
        self.stdout.write(self.style.SUCCESS(f'{verb}：成功 {ok}，失败 {failed}'))
