from django.apps import AppConfig

class KnowledgeConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'backend.apps.knowledge'
    verbose_name = '知识库'

    def ready(self):
        from . import signals  # noqa: F401 - 注册 MCP 停用联动信号

