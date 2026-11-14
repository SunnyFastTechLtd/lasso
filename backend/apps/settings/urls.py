from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    SystemSettingViewSet, BrandingView, ProxyEndpointViewSet, ProxyBindingView,
)

router = DefaultRouter()
# 顺序有意义：settings/proxies 必须先于 settings 注册，否则会被 settings 的
# detail 路由（settings/<pk>/）抢先匹配。
router.register(r'settings/proxies', ProxyEndpointViewSet, basename='proxy-endpoint')
router.register(r'settings', SystemSettingViewSet, basename='setting')

urlpatterns = [
    path('branding/', BrandingView.as_view(), name='branding'),
    path('settings/proxy-bindings/', ProxyBindingView.as_view(), name='proxy-bindings'),
    path('', include(router.urls)),
]
