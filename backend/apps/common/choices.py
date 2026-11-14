"""跨域共用的枚举口径（可见范围等）

工具来源、技能、智能体三域都使用同一套可见范围，统一在此定义，
避免各 app 各写一份导致接口字段对不上。
"""
from django.db import models


class Visibility(models.TextChoices):
    """A6 可见范围"""

    PRIVATE = 'private', '仅自己'
    DEPARTMENT = 'department', '指定部门'
    PUBLIC = 'public', '全企业'
    ADMIN_ONLY = 'admin_only', '仅管理端'
