"""注册出厂内置演示工具来源（PRD B7）

内置来源「演示设备状态查询」随版本发布，指向本仓库自带的 demo MCP 服务
（backend/apps/mcp/demo_server.py，默认 http://127.0.0.1:8931/mcp）。

口径要点（与 PRD B7 一致）：
- 出厂状态为「未验证」：不因为内置就跳过连通性测试，管理员仍需手动"连接并发现工具"
- 工具启停默认全关：走 B4 勾选流程
- 不可删除、不可编辑地址与命令：由 serializers._reject_builtin_edits 兜住
- 正式交付若客户已有真实系统：用 --disable 停用隐藏，而不是删除

用法：
    python manage.py init_builtin_mcp              # 幂等注册/修复内置来源
    python manage.py init_builtin_mcp --disable    # 停用内置来源（交付实施清单项）
    python manage.py init_builtin_mcp --enable     # 重新启用
"""
from django.core.management.base import BaseCommand

from backend.apps.mcp.demo_server import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_HTTP_PATH, SERVER_NAME
from backend.apps.mcp.models import MCPServer
from backend.apps.users.models import log_audit

BUILTIN_SLUG = 'demo-devices'
BUILTIN_URL = f'http://{DEFAULT_HOST}:{DEFAULT_PORT}{DEFAULT_HTTP_PATH}'
BUILTIN_DESCRIPTION = '平台内置演示来源：查询离线设备列表与单台设备详情，正式交付可停用（不删除）'


class Command(BaseCommand):
    help = '注册/修复出厂内置演示工具来源（B7），幂等'

    def add_arguments(self, parser):
        parser.add_argument('--disable', action='store_true', help='停用内置来源（正式交付环境用）')
        parser.add_argument('--enable', action='store_true', help='重新启用内置来源')

    def handle(self, *args, **options):
        server, created = MCPServer.objects.get_or_create(
            slug=BUILTIN_SLUG,
            defaults={
                'name': SERVER_NAME,
                'description': BUILTIN_DESCRIPTION,
                'transport': 'http_mcp',
                'url': BUILTIN_URL,
                'lifecycle': 'per_session',
                'is_builtin': True,
                'is_active': True,
                'status': 'unknown',
                'visibility': 'public',
                'requires_egress': False,
            },
        )
        if created:
            log_audit(actor=None, action='mcp_create', target=server.name,
                      detail='出厂内置来源注册（B7）')
            self.stdout.write(self.style.SUCCESS(
                f'已注册内置来源：{server.name}（{BUILTIN_URL}，状态=未验证，工具待勾选）'))
            return

        if options['disable']:
            server.is_active = False
            server.status = 'disabled'
            server.save(update_fields=['is_active', 'status', 'updated_at'])
            log_audit(actor=None, action='mcp_disable', target=server.name,
                      detail='内置演示来源停用（交付实施清单）')
            self.stdout.write(self.style.SUCCESS(f'已停用内置来源：{server.name}'))
            return

        if options['enable']:
            server.is_active = True
            server.status = 'unknown' if server.status == 'disabled' else server.status
            server.save(update_fields=['is_active', 'status', 'updated_at'])
            log_audit(actor=None, action='mcp_update', target=server.name, detail='内置演示来源启用')
            self.stdout.write(self.style.SUCCESS(f'已启用内置来源：{server.name}（状态={server.status}）'))
            return

        # 已存在：只修复出厂字段，绝不覆盖 status / enabled_tools（保留现场勾选与检测结果）
        changed = []
        if server.url != BUILTIN_URL:
            server.url = BUILTIN_URL
            changed.append('url')
        if not server.is_builtin:
            server.is_builtin = True
            changed.append('is_builtin')
        if server.name != SERVER_NAME:
            server.name = SERVER_NAME
            changed.append('name')
        if changed:
            server.save(update_fields=changed + ['updated_at'])
        self.stdout.write(self.style.SUCCESS(
            f'内置来源已存在：{server.name}（状态={server.status}，'
            f'启用工具={len(server.enabled_tools or [])}，修复字段={changed or "无"}）'))
