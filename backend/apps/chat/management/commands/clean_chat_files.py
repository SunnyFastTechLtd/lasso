import os
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from backend.apps.chat.models import ChatGeneratedFile


class Command(BaseCommand):
    help = '清理指定天数（默认 7 天）前的对话生成文件物理文件，并标记记录为已过期'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=7)

    def handle(self, *args, **options):
        days = options['days']
        cutoff = timezone.now() - timedelta(days=days)
        qs = ChatGeneratedFile.objects.filter(created_at__lt=cutoff, is_expired=False)
        count = qs.count()

        for record in qs:
            if record.file:
                try:
                    record.file.delete(save=False)
                except Exception as exc:
                    self.stderr.write(f'删除文件失败 record={record.pk}: {exc}')
            record.is_expired = True
            record.save(update_fields=['is_expired'])

        self.stdout.write(self.style.SUCCESS(f'已标记 {count} 条 {days} 天前的对话生成文件为过期'))
