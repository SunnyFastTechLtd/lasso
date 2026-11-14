"""智能体装配接口路由（底座前缀 api/agents/，PRD 第 6 章 A 组）

路径约定（与设计稿 agents.html / agent-editor.html 的接口调用一一对应）：
  GET    /api/agents/                     A1 列表（分页 / search / status / visibility / job_tag）
  POST   /api/agents/                     A2 新建（名称唯一、slug 自动生成且不可改）
  GET    /api/agents/{id}/                详情（全量配置 + 挂载 + 工具冲突标注）
  PUT    /api/agents/{id}/                整体更新
  PATCH  /api/agents/{id}/               局部更新（含 A4 挂载、A6 可见范围）
  DELETE /api/agents/{id}/               A1 删除（已发布/已停用 409，需先归档）
  GET|POST /api/agents/{id}/publish-check/  A7 发布前检查清单 + 与线上版本的差异
  POST   /api/agents/{id}/publish/        A7 发布（risk_ack 放行警示、confirm_diff 看过差异）
  POST   /api/agents/{id}/disable/        A6 停用（用户侧即时移除，历史会话可读）
  POST   /api/agents/{id}/enable/         恢复
  POST   /api/agents/{id}/archive/        A1 归档
  POST   /api/agents/{id}/restore/        归档 → 草稿
  POST   /api/agents/{id}/restore-builtin/  4.2 内置智能体恢复出厂配置（只改草稿，需重新发布）
  POST   /api/agents/{id}/clone/          A1 复制新建（连挂载一起复制，产物为草稿）
  GET    /api/agents/{id}/versions/       A8 发布历史（只读，分页）
  GET    /api/agents/{id}/versions/{n}/   A8 查看某版本完整配置（无编辑与回滚入口，回滚为 P1）
  GET    /api/agents/{id}/mount-options/  A4 四分区候选（工具逐个可勾、灰置带原因）
  GET    /api/agents/options/             A3/A5 模型、三档预设、授权档位、内置能力、占位符
  GET    /api/agents/summary/             A1 顶部统计与空状态引导
  GET    /api/agents/{id}/export/         跨环境导出（不含密钥，导入需重新填写）
  POST   /api/agents/import-requests/preview/  导入包预览（依赖映射 + 阻断/告警）
  POST   /api/agents/import-requests/     提交导入申请（单级审批，提交不生效）
  GET    /api/agents/import-requests/     导入申请列表（管理员全量，普通用户仅自己）
  POST   /api/agents/import-requests/{id}/approve/  审批通过（管理员且非提交人，落库为草稿）
  POST   /api/agents/import-requests/{id}/reject/   驳回（管理员且非提交人）
"""
from django.urls import include, path
from rest_framework.routers import SimpleRouter

from backend.apps.agenthub.views import AgentImportRequestViewSet, AgentProfileViewSet

# 前缀为空（底座已是 api/agents/），用 SimpleRouter 避免 DefaultRouter
# 再注册一个被列表路由遮蔽的 API 根视图
router = SimpleRouter()
router.register(r'', AgentProfileViewSet, basename='agent')

# 导入申请必须注册在智能体路由之前：否则 import-requests/ 会被
# ^(?P<pk>[^/.]+)/$ 的详情路由当成 pk 吃掉
import_router = SimpleRouter()
import_router.register(r'import-requests', AgentImportRequestViewSet,
                       basename='agent-import-request')

app_name = 'agenthub'
urlpatterns = [
    path('', include(import_router.urls)),
    path('', include(router.urls)),
]