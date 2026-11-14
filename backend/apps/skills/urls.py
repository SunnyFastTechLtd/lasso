"""技能规范接口路由（底座前缀 api/skills/，见可行性报告 3.4）

路径约定（与管理端技能页一一对应）：
  GET    /api/skills/                 列表（分页 / search / status / source，不带正文）
  POST   /api/skills/                 上传建档（C1：支持 JSON 粘贴或 file= 规范文件）
  GET    /api/skills/{id}/            详情（C2：正文全文、字数、挂载的智能体、校验失败原因）
  PATCH  /api/skills/{id}/            编辑（C4：回待审核，原启用版本继续生效）
  DELETE /api/skills/{id}/            删除（C6：被挂载 409，内置 400）
  POST   /api/skills/{id}/approve/    审核通过（C3：必须带 body_ack + body_hash）
  POST   /api/skills/{id}/reject/     驳回（C3：理由必填，创建者可见）
  POST   /api/skills/{id}/disable/    停用（C4：即时对新会话生效）
  POST   /api/skills/{id}/enable/     恢复启用（用最近审核通过的正文）
  GET    /api/skills/{id}/references/ 被哪些智能体挂载
  GET    /api/skills/options/         装配页选择器（A4：未启用的灰置并给原因）
  GET    /api/skills/summary/         各状态计数与待审核队列长度
  GET|POST /api/skills/mirror/        运行时镜像可见性 / 全量重建
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from backend.apps.skills.views import SkillViewSet

router = DefaultRouter()
router.register(r'', SkillViewSet, basename='skill')

app_name = 'skills'
urlpatterns = [
    path('', include(router.urls)),
]
