"""女娲市场目录同步命令

用法：
  python manage.py nuwa_sync                       # 在线同步两条内置源
  python manage.py nuwa_sync --source mcp-registry --query excel --limit 30
  python manage.py nuwa_sync --export data/nuwa/market-cache.json
  python manage.py nuwa_sync --import data/nuwa/market-cache.json   # 内网全程不出网
"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from backend.apps.nuwa import market
from backend.apps.nuwa.models import MarketSource


class Command(BaseCommand):
    help = '同步女娲市场目录（在线）或导入/导出离线目录包'

    def add_arguments(self, parser):
        parser.add_argument('--source', default='all',
                            help='all | mcp-registry | skill-repo | 源名称')
        parser.add_argument('--query', default='', help='在线检索关键词（仅 MCP 目录）')
        parser.add_argument('--limit', type=int, default=50)
        parser.add_argument('--export', default='', help='导出离线包到指定路径')
        parser.add_argument('--import', dest='import_file', default='',
                            help='从离线包导入（不出网）')
        parser.add_argument('--dry-run', action='store_true', help='只打印，不写库')

    def handle(self, *args, **options):
        if options['import_file']:
            path = Path(options['import_file'])
            if not path.exists():
                raise CommandError(f'离线包不存在：{path}')
            if options['dry_run']:
                self.stdout.write(f'[dry-run] 将导入 {path}')
                return
            runs = market.import_cache(path)
            for run in runs:
                self.stdout.write(f'导入 {run.source.name}: +{run.added}/{run.updated}')
            return

        if options['export']:
            path = Path(options['export'])
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = market.export_cache(path)
            self.stdout.write(f'已导出 {path}（{len(payload["sources"])} 个源）')
            return

        selected = options['source']
        if selected == 'all':
            sources = [source for source in market.ensure_builtin_sources() if source.enabled]
        elif selected == 'mcp-registry':
            sources = [source for source in market.ensure_builtin_sources()
                       if source.kind == 'mcp_registry']
        elif selected == 'skill-repo':
            sources = [source for source in market.ensure_builtin_sources()
                       if source.kind == 'skill_repo']
        else:
            sources = list(MarketSource.objects.filter(name=selected, enabled=True))
            if not sources:
                raise CommandError(f'未找到启用的源：{selected}')

        for source in sources:
            if options['dry_run']:
                self.stdout.write(f'[dry-run] 将同步 {source.name}')
                continue
            run = market.sync_source(source, query=options['query'],
                                     limit=options['limit'])
            self.stdout.write(
                f'{source.name}: {run.status} +{run.added} ~{run.updated} !{run.failed} '
                f'{run.error}'
            )

        if settings.DEBUG:
            self.stdout.write(self.style.WARNING('当前 DEBUG=true，出网失败会降级而不报错'))
