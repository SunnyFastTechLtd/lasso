from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ConversationViewSet, ChatViewSet, ModelViewSet,
    ChatGeneratedFileDownloadView,
)
from .preferences import (
    UserPreferenceView, UserPreferenceDetailView,
    PresetPreferenceView, PreferenceSuggestView,
)

router = DefaultRouter()
router.register(r'conversations', ConversationViewSet, basename='conversation')
router.register(r'chat', ChatViewSet, basename='chat')
router.register(r'models', ModelViewSet, basename='model')

urlpatterns = [
    path('', include(router.urls)),
    path('files/<int:pk>/download/', ChatGeneratedFileDownloadView.as_view(), name='chat-file-download'),
    # D8 跨会话偏好：全部按登录用户隔离，无管理员查看他人偏好的入口
    path('preferences/', UserPreferenceView.as_view(), name='chat-preferences'),
    path('preferences/suggest/', PreferenceSuggestView.as_view(), name='chat-preference-suggest'),
    path('preferences/preset/', PresetPreferenceView.as_view(), name='chat-preference-preset'),
    path('preferences/<int:pk>/', UserPreferenceDetailView.as_view(), name='chat-preference-detail'),
]
