"""把数据库中已启用的技能镜像到磁盘目录（AgentScope 的加载器只认目录）"""
from django.core.management.base import BaseCommand

from backend.apps.skills.services import mirror_root, rebuild_all_mirror


class Command(BaseCommand):
    help = '全量重建技能镜像目录（部署后/迁移后/手工修库后自愈用），幂等'

    def add_arguments(self, parser):
        parser.add_argument('--root', action='store_true',
                            help='只打印镜像根目录，不做重建')

    def handle(self, *args, **options):
        root = mirror_root()
        if options['root']:
            self.stdout.write(str(root))
            return
        result = rebuild_all_mirror()
        self.stdout.write(self.style.SUCCESS(
            f'技能镜像已重建：根目录 {root}，生效 {result["mirrored"]} 个'
            f'（{", ".join(result["slugs"]) or "无"}），'
            f'移除 {result["removed"]} 个残留目录'))
