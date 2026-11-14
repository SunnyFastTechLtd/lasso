from django.urls import path, include
from rest_framework.permissions import AllowAny
from rest_framework.routers import DefaultRouter
from .views import AuthViewSet, UserManageViewSet, DepartmentViewSet, AuditLogViewSet

router = DefaultRouter()
router.register(r'manage', UserManageViewSet, basename='user-manage')
router.register(r'departments', DepartmentViewSet, basename='department')
router.register(r'audit-logs', AuditLogViewSet, basename='audit-log')

# 直接映射各个 action 到具体 URL
# 注意：手写 as_view 不会像 Router 那样自动带上 @action 的 initkwargs。
# 匿名入口必须在此显式传 AllowAny；其余 action 不传，统一走 AuthViewSet 类级的
# IsAuthenticated，新增 action 不会因为漏传参数而变成匿名可访问。
auth_login = AuthViewSet.as_view({'post': 'login'}, permission_classes=[AllowAny])
auth_register = AuthViewSet.as_view({'post': 'register'}, permission_classes=[AllowAny])
auth_refresh = AuthViewSet.as_view({'post': 'refresh_token'}, permission_classes=[AllowAny])
auth_logout = AuthViewSet.as_view({'post': 'logout'})
auth_profile = AuthViewSet.as_view({
    'get': 'me',
    'put': 'update_profile',
    'patch': 'update_profile',
})
auth_change_password = AuthViewSet.as_view({'post': 'change_password'})

urlpatterns = [
    path('login/', auth_login, name='auth-login'),
    path('register/', auth_register, name='auth-register'),
    path('refresh/', auth_refresh, name='auth-refresh'),
    path('logout/', auth_logout, name='auth-logout'),
    path('profile/', auth_profile, name='auth-profile'),
    path('change-password/', auth_change_password, name='auth-change-password'),
    path('', include(router.urls)),
]
