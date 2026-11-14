from django.apps import AppConfig


class AgentHubConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'backend.apps.agenthub'
    label = 'agenthub'
    verbose_name = '智能体定义'
