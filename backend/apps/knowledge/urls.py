from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    KnowledgeBaseViewSet, DocumentViewSet, ChatViewSet, TiangongSwitchesView,
    TiangongSourceViewSet, TiangongSourceProposalViewSet,
    TiangongSearchBindingViewSet, TiangongSearchCandidatesView,
    TiangongDiscoverySettingsView, TiangongSearxngCheckView, TiangongBingCheckView,
)

router = DefaultRouter()
router.register(r'bases', KnowledgeBaseViewSet, basename='knowledge-base')
router.register(r'documents', DocumentViewSet, basename='document')
router.register(r'chat', ChatViewSet, basename='knowledge-chat')
router.register(r'tiangong-sources', TiangongSourceViewSet, basename='tiangong-source')
router.register(r'tiangong-source-proposals', TiangongSourceProposalViewSet,
                basename='tiangong-source-proposal')
router.register(r'tiangong-search-bindings', TiangongSearchBindingViewSet,
                basename='tiangong-search-binding')

urlpatterns = [
    path('tiangong/switches/', TiangongSwitchesView.as_view(), name='tiangong-switches'),
    path('tiangong-search/candidates/', TiangongSearchCandidatesView.as_view(),
         name='tiangong-search-candidates'),
    path('tiangong-search/settings/', TiangongDiscoverySettingsView.as_view(),
         name='tiangong-search-settings'),
    path('tiangong-search/searxng/check/', TiangongSearxngCheckView.as_view(),
         name='tiangong-searxng-check'),
    path('tiangong-search/bing/check/', TiangongBingCheckView.as_view(),
         name='tiangong-bing-check'),
    path('', include(router.urls)),
]
