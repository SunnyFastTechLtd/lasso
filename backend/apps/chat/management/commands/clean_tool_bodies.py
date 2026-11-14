"""清理工具调用返回全文（PRD 9.2 #4：全文保留 90 天，到期自动清理）

与 clean_chat_files 刻意不合并：对象不同（这里是数据库全文表，那里是对话生成
文件的物理文件 + 记录）、删除语义不同（这里硬删除，那里是软标记 is_expired），
保留期也不同（90 天 vs 7 天）。两者各自一行 cron，互不影响。
"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.models import Max, Min
from django.utils import timezone

from backend.apps.chat.models import ToolCallBody


class Command(BaseCommand):
    help = '硬删除超过保留期（默认 90 天）的工具调用返回全文，按 id 分片执行'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=90,
                            help='保留天数，默认 90（PRD 9.2 #4）')
        parser.add_argument('--batch', type=int, default=5000,
                            help='每片删除条数，默认 5000')
        parser.add_argument('--dry-run', action='store_true',
                            help='只统计不删除，输出待删数量与到期时间范围')

    def handle(self, *args, **options):
        days = options['days']
        batch = options['batch']
        if days <= 0:
            raise CommandError('--days 必须大于 0')
        if batch <= 0:
            raise CommandError('--batch 必须大于 0')

        cutoff = timezone.now() - timedelta(days=days)
        qs = ToolCallBody.objects.filter(created_at__lt=cutoff)
        total = qs.count()

        if options['dry_run']:
            span = qs.aggregate(earliest=Min('created_at'), latest=Max('created_at'))
            self.stdout.write(
                f'[dry-run] 保留 {days} 天，命中 {total} 条待删'
                f'（最早 {span["earliest"] or "-"}，最晚 {span["latest"] or "-"}），未删除任何行')
            return

        deleted = 0
        try:
            while True:
                # 先取一片 id 再按 id 删除：避免单事务锁表，也避免内存随总量增长
                ids = list(qs.values_list('id', flat=True)[:batch])
                if not ids:
                    break
                affected, _ = ToolCallBody.objects.filter(id__in=ids).delete()
                deleted += affected
                if len(ids) < batch:
                    break
        except Exception as exc:
            self.stderr.write(f'清理失败（已删除 {deleted} 条，剩余可重试）：{exc}')
            raise CommandError('clean_tool_bodies 执行失败') from exc

        remaining = qs.count()
        size = self._table_size()
        self.stdout.write(self.style.SUCCESS(
            f'保留 {days} 天：命中 {total} 条，已删除 {deleted} 条，剩余 {remaining} 条'
            + (f'，表大小 {size}' if size else '')))

    def _table_size(self):
        """PostgreSQL 下顺带打印表大小，便于容量观察；其它数据库跳过"""
        if connection.vendor != 'postgresql':
            return ''
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_size_pretty(pg_total_relation_size(%s))',
                               [ToolCallBody._meta.db_table])
                return cursor.fetchone()[0]
        except Exception:
            return ''
