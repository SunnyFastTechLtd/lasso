"""MCP 工具来源接口路由（底座前缀 api/mcp/）

路径约定（与设计稿 mcp-servers.html 的接口调用一一对应）：
  GET    /api/mcp/servers/                来源列表（分页 / search / status / transport）
  POST   /api/mcp/servers/                新增来源（B3：保存后自动测一次）
  GET    /api/mcp/servers/{id}/           来源详情
  PUT    /api/mcp/servers/{id}/           整体更新
  PATCH  /api/mcp/servers/{id}/           局部更新（密钥掩码＝未修改）
  DELETE /api/mcp/servers/{id}/           删除（B6：有引用默认 409，confirm_unmount 才摘除）
  POST   /api/mcp/servers/{id}/test/      连通性测试 + 工具发现
  GET    /api/mcp/servers/{id}/tools/     工具清单（B4）
  POST   /api/mcp/servers/{id}/tools/toggle/  工具启停
  POST   /api/mcp/servers/{id}/disable/   停用
  POST   /api/mcp/servers/{id}/enable/    恢复
  GET    /api/mcp/servers/{id}/references/    引用检查
  GET    /api/mcp/servers/command-prefixes/   B2 内置只读白名单
  GET|POST /api/mcp/servers/egress/       B8 出网总开关
  GET    /api/mcp/servers/mountable/      装配页选择器
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from backend.apps.mcp.views import MCPServerViewSet

router = DefaultRouter()
router.register(r'servers', MCPServerViewSet, basename='mcp-server')

app_name = 'mcp'
urlpatterns = [
    path('', include(router.urls)),
]
