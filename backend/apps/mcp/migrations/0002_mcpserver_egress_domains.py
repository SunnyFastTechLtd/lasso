"""B8-a：出网域名白名单字段 + 存量出网来源回填

回填口径：把每个出网来源当前 url 的主机名写入允许清单，保证严格白名单
开启前后存量来源都不会被"上线即全量拦截"。反向迁移只删字段，不动数据。
"""
from urllib.parse import urlparse

from django.db import migrations, models


def backfill_egress_domains(apps, schema_editor):
    MCPServer = apps.get_model('mcp', 'MCPServer')
    for server in MCPServer.objects.filter(transport='http_mcp', requires_egress=True):
        if server.egress_domains:
            continue
        host = (urlparse(server.url or '').hostname or '').strip().lower().rstrip('.')
        if host:
            server.egress_domains = [host]
            server.save(update_fields=['egress_domains'])


class Migration(migrations.Migration):

    dependencies = [
        ('mcp', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='mcpserver',
            name='egress_domains',
            field=models.JSONField(blank=True, default=list,
                                   help_text='仅出网类来源需要；支持精确域名与 *.example.com 通配',
                                   verbose_name='允许的出网域名'),
        ),
        migrations.RunPython(backfill_egress_domains, migrations.RunPython.noop),
    ]
