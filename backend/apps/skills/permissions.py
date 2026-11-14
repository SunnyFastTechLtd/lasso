"""技能的管理端可见性口径

P0 技能的生效范围来自挂载它的智能体（C5），这里的 visibility 只控制
"谁在管理端技能页看得到这条记录"：默认 admin_only，即只有平台管理员可见。
"""
from django.db.models import Q

from backend.apps.common.choices import Visibility


def related_department_ids(user):
    """用户所在部门 + 其全部上级部门（上级被授予可见性，下级同样可见，A6）"""
    from backend.apps.users.models import Department

    dept = Department.objects.filter(name=user.department).first() if user.department else None
    related = set()
    while dept is not None:
        related.add(dept.pk)
        dept = Department.objects.filter(pk=dept.parent_id).first() if dept.parent_id else None
    return related


def visible_skill_query(user):
    """非管理员的可见技能过滤：全企业 + 自己创建 + 命中可见部门（含上级授予）"""
    q = Q(visibility=Visibility.PUBLIC) | Q(created_by=user)
    related = related_department_ids(user)
    if related:
        q |= Q(visibility=Visibility.DEPARTMENT, allowed_departments__in=related)
    return q
