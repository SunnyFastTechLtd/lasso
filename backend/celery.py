"""
Celery 异步任务队列

Broker 使用 Redis。音频转写、知识库向量化等耗时任务
统一通过 Celery 异步执行，避免阻塞 Web 请求。

开发/测试环境无 Redis 时，可在 .env 设 CELERY_TASK_ALWAYS_EAGER=True
（默认即为 True），任务将同步执行；生产环境必须显式设为 False。
"""
import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')

app = Celery('backend')
# 从 Django settings 读取 CELERY_ 前缀配置（lazy，测试 override_settings 可生效）
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
