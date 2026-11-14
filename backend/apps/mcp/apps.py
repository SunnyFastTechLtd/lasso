from django.apps import AppConfig


class MCPConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'backend.apps.mcp'
    label = 'mcp'
    verbose_name = 'MCP 工具来源'
