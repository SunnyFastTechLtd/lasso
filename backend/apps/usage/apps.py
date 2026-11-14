from django.apps import AppConfig


class UsageConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'backend.apps.usage'
    label = 'usage'
    verbose_name = '用量概览'
