"""女娲模式路由（底座前缀 api/nuwa/）

  GET|POST /api/nuwa/sessions/                       建会话 + 生成骨架 / 我的会话
  GET|PATCH /api/nuwa/sessions/{id}/                 详情 / 编辑骨架
  POST /api/nuwa/sessions/{id}/regenerate/           重新生成
  POST /api/nuwa/sessions/{id}/matches/              市场检索（在线优先 + 缓存兜底）
  POST /api/nuwa/sessions/{id}/selections/           保存勾选并提交外部条目审批
  POST /api/nuwa/sessions/{id}/knowledge/plan/       资料检索计划
  POST /api/nuwa/sessions/{id}/knowledge/preview/    在线抓取预览
  POST /api/nuwa/sessions/{id}/knowledge/commit/     确认入库
  POST /api/nuwa/sessions/{id}/create-agent/         生成智能体草稿
  POST /api/nuwa/sessions/{id}/abandon/              放弃
  GET|POST /api/nuwa/sources/                        市场源（管理员）
  POST /api/nuwa/sources/{id}/sync/                  同步单个源（管理员）
  GET /api/nuwa/items/                               本地目录浏览/搜索（kind/source/search）
  GET /api/nuwa/items/facets/                        来源与条目数（市场页筛选器）
  POST /api/nuwa/items/{id}/block/                   拦截条目（管理员）
  GET|POST /api/nuwa/market/search/                  独立市场页检索（与向导同一套）
  GET /api/nuwa/import-requests/                     导入申请
  POST /api/nuwa/import-requests/                    就地提交导入申请（market_item）
  POST /api/nuwa/import-requests/{id}/approve|reject/ 单级审批（管理员且非提交人）
  GET|POST /api/nuwa/switches/                       女娲出网开关（管理员）
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from backend.apps.nuwa.views import (MarketItemViewSet, MarketSearchView, MarketSourceViewSet,
                                     NuwaImportRequestViewSet, NuwaSessionViewSet,
                                     NuwaSwitchesView)

router = DefaultRouter()
router.register(r'sessions', NuwaSessionViewSet, basename='nuwa-session')
router.register(r'sources', MarketSourceViewSet, basename='nuwa-source')
router.register(r'items', MarketItemViewSet, basename='nuwa-item')
router.register(r'import-requests', NuwaImportRequestViewSet, basename='nuwa-import-request')

app_name = 'nuwa'
urlpatterns = [
    path('switches/', NuwaSwitchesView.as_view(), name='nuwa-switches'),
    path('market/search/', MarketSearchView.as_view(), name='nuwa-market-search'),
    path('', include(router.urls)),
]
