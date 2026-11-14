"""
URL configuration for backend project.

Lasso 只保留四块底座（认证 / 对话 / 知识库 / 系统设置）
与三块装配域（智能体 / MCP 工具来源 / 技能）。
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

from backend.apps.common.media_auth import media_auth_view

urlpatterns = [
    path('admin/', admin.site.urls),

    # nginx auth_request 子请求入口：校验 /media/ 的登录态与文件归属
    path('api/media/auth/', media_auth_view, name='media-auth'),

    # API v1 —— 底座
    path('api/auth/', include('backend.apps.users.urls')),
    path('api/chat/', include('backend.apps.chat.urls')),
    path('api/knowledge/', include('backend.apps.knowledge.urls')),
    path('api/system/', include('backend.apps.settings.urls')),

    # API v1 —— 装配
    path('api/agents/', include('backend.apps.agenthub.urls')),
    path('api/mcp/', include('backend.apps.mcp.urls')),
    path('api/skills/', include('backend.apps.skills.urls')),

    # 女娲模式（自然语言建智能体 / 市场检索 / 资料抓取）
    path('api/nuwa/', include('backend.apps.nuwa.urls')),

    # API v1 —— F4 用量概览
    path('api/usage/', include('backend.apps.usage.urls')),
]

# 开发环境下提供媒体文件访问
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
