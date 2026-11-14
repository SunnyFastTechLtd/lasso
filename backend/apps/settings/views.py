import json
import os

from django.db import transaction
from django.db.models import ProtectedError
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.views import APIView
from django.conf import settings
from django.core.files.storage import default_storage
from .models import ProxyEndpoint, ResourceProxyBinding, SystemSetting
from .serializers import (
    SystemSettingSerializer, LLMConfigSerializer, BrandingSerializer,
    ProxyEndpointSerializer, ProxyBindingPutSerializer,
)
from . import proxies as proxy_service
from backend.apps.common.llm import (
    DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE, DEFAULT_TIMEOUT,
    MODEL_CONFIG_HINT, MODEL_REQUIRED_FIELDS,
    probe_model, reset_client,
)
from backend.apps.users.models import log_audit


VERIFIED_MODELS_KEY = 'llm_verified_models'
# 可用模型列表上限：内网网关常挂几十个模型，装配页下拉不需要全部
VERIFIED_MODELS_LIMIT = 30


def mark_verified_model(model_name: str):
    """把通过健康检查的模型写入「已验证可用」列表（A3 模型下拉的唯一来源）"""
    model_name = (model_name or '').strip()
    if not model_name:
        return []
    raw = SystemSetting.get_value(VERIFIED_MODELS_KEY, '')
    names = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                names = [str(v).strip() for v in parsed if str(v).strip()]
        except (TypeError, ValueError):
            names = []
    if model_name not in names:
        names.insert(0, model_name)
    names = names[:VERIFIED_MODELS_LIMIT]
    SystemSetting.set_value(VERIFIED_MODELS_KEY, json.dumps(names, ensure_ascii=False),
                            '已通过健康检查的模型（智能体模型下拉候选）')
    return names


def _stored_number(key, cast, default):
    """读取库里的可选数值项；缺失/非法时用代码内默认值"""
    raw = SystemSetting.get_value(key, '')
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


class SystemSettingViewSet(viewsets.ViewSet):
    """系统设置接口"""

    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        # 配置修改与模型探测都需要管理员权限：
        # 探测会发起外部请求并把结果写入「已验证可用模型」，不能让任意登录用户触发
        if self.action in ['update_llm_config', 'reset_llm_config',
                           'update_skill_package', 'test_llm_connection']:
            return [IsAdminUser()]
        return super().get_permissions()

    @action(detail=False, methods=['get'], url_path='llm')
    def llm_config(self, request):
        """获取 LLM 配置（隐藏敏感信息）。

        唯一来源是数据库；未配置时返回空值并给出缺失项，不回落到环境变量。
        """
        base_url = SystemSetting.get_value('llm_base_url', '')
        api_key = SystemSetting.get_value('llm_api_key', '')
        model = SystemSetting.get_value('llm_model', '')
        temperature = _stored_number('llm_temperature', float, DEFAULT_TEMPERATURE)
        max_tokens = _stored_number('llm_max_tokens', int, DEFAULT_MAX_TOKENS)
        timeout = _stored_number('llm_timeout', int, DEFAULT_TIMEOUT)

        # 只显示 api_key 前后各 3 位
        masked_key = api_key[:3] + '***' + api_key[-3:] if len(api_key) > 6 else '***'

        raw_verified = SystemSetting.get_value(VERIFIED_MODELS_KEY, '')
        try:
            verified = json.loads(raw_verified) if raw_verified else []
        except (TypeError, ValueError):
            verified = []

        current = {
            'llm_base_url': base_url,
            'llm_api_key': api_key,
            'llm_model': model,
        }
        missing_fields = [
            key for key in MODEL_REQUIRED_FIELDS
            if not str(current.get(key) or '').strip()
        ]

        return Response({
            'base_url': base_url,
            'api_key_masked': masked_key,
            'model': model,
            'temperature': temperature,
            'max_tokens': max_tokens,
            'timeout': timeout,
            # A3：智能体模型下拉的唯一候选来源
            'verified_models': [str(v) for v in verified] if isinstance(verified, list) else [],
            # 配置来源与完整性：前端据此提示去模型配置界面补齐
            'config_source': 'database',
            'configured': not missing_fields,
            'missing_fields': missing_fields,
            'hint': MODEL_CONFIG_HINT if missing_fields else '',
        })

    @action(detail=False, methods=['post'], url_path='llm/update')
    def update_llm_config(self, request):
        """更新 LLM 配置（管理员）——F1：按候选配置先检查，通过才落库"""
        serializer = LLMConfigSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # 先用候选配置探测，现网生效配置此时不受影响
        health = probe_model(data)
        if health.get('status') != 'ok':
            log_audit(
                actor=request.user,
                action='llm_update',
                target='LLM配置',
                detail=f"未保存（健康检查{health.get('status')}）：模型 {data['model']}，"
                       f"base_url={data['base_url']}；{(health.get('error') or '')[:200]}",
            )
            return Response({
                'detail': '健康检查未通过，本次配置未保存，现网仍使用原配置',
                'saved': False,
                'health': health,
            })

        # 保存到数据库
        SystemSetting.set_value('llm_base_url', data['base_url'], '大模型 API 地址')
        SystemSetting.set_value('llm_api_key', data['api_key'], '大模型 API Key')
        SystemSetting.set_value('llm_model', data['model'], '主模型名称')
        SystemSetting.set_value('llm_temperature', str(data['temperature']), '采样温度')
        SystemSetting.set_value('llm_max_tokens', str(data['max_tokens']), '最大生成长度')
        SystemSetting.set_value('llm_timeout', str(data['timeout']), '请求超时时间（秒）')

        # 重置单例 client 和配置缓存，下次调用时重新加载
        reset_client()
        mark_verified_model(health.get('verified_model') or data['model'])

        log_audit(
            actor=request.user,
            action='llm_update',
            target='LLM配置',
            detail=f"更新模型为 {data['model']}，base_url={data['base_url']}，健康检查通过",
        )

        return Response({
            'detail': '配置已更新并通过健康检查',
            'saved': True,
            'health': health,
        })

    @action(detail=False, methods=['post'], url_path='llm/test')
    def test_llm_connection(self, request):
        """测试 LLM 连接（用已保存配置），通过则该模型进入可用列表"""
        health = probe_model()
        if health.get('status') == 'ok':
            mark_verified_model(health.get('verified_model'))
        return Response(health)

    @action(detail=False, methods=['post'], url_path='llm/reset')
    def reset_llm_config(self, request):
        """清空数据库中的模型配置（管理员）。

        不再有「环境变量默认」可回退：清空后平台会明确拒绝调用模型，
        直到管理员重新填写并保存一套完整配置。
        """
        keys_to_delete = [
            'llm_base_url', 'llm_api_key', 'llm_model',
            'llm_temperature', 'llm_max_tokens', 'llm_timeout',
            # 可用模型列表属于被清空的这套接入凭据，一并清除
            VERIFIED_MODELS_KEY,
        ]
        SystemSetting.objects.filter(key__in=keys_to_delete).delete()

        # 重置单例 client 和配置缓存
        reset_client()

        log_audit(
            actor=request.user,
            action='llm_reset',
            target='LLM配置',
            detail='清空数据库内的模型配置（不再回退环境变量）',
        )

        return Response({
            'detail': '已清空数据库内的模型配置；重新配置前平台将拒绝调用模型',
        })

    @action(detail=False, methods=['get'], url_path='skill-package')
    def skill_package(self, request):
        """技能包附件类型（决策 10：管理员可配置，出厂保守集）"""
        from backend.apps.skills import services as skill_services

        return Response({
            'allowed_extensions': skill_services.allowed_extensions(),
            'default_extensions': skill_services.DEFAULT_ALLOWED_EXTENSIONS,
            'denied_extensions': sorted(skill_services.DENIED_EXTENSIONS),
            'max_attachment_bytes': skill_services.MAX_ATTACHMENT_BYTES,
            'max_attachments': skill_services.MAX_ATTACHMENTS,
            'max_package_bytes': skill_services.MAX_PACKAGE_BYTES,
            'ruleset_version': skill_services.RULESET_VERSION,
        })

    @action(detail=False, methods=['post'], url_path='skill-package/update')
    def update_skill_package(self, request):
        """修改技能包附件类型；不可接受项直接 400，变更写审计"""
        from backend.apps.skills import services as skill_services

        raw = request.data.get('allowed_extensions')
        if not isinstance(raw, list):
            return Response({'detail': 'allowed_extensions 必须是字符串数组'},
                            status=status.HTTP_400_BAD_REQUEST)
        invalid = skill_services.invalid_extensions(raw)
        if invalid:
            return Response({
                'detail': '存在不可接受的扩展名（格式非法或在禁止清单内）',
                'invalid_extensions': invalid,
            }, status=status.HTTP_400_BAD_REQUEST)
        cleaned = skill_services.normalize_extensions(raw)
        if not cleaned:
            return Response({'detail': '至少保留一种可上传的文本类型'},
                            status=status.HTTP_400_BAD_REQUEST)

        before = skill_services.allowed_extensions()
        SystemSetting.set_value(
            skill_services.SKILL_PACKAGE_SETTINGS_KEY,
            json.dumps({'allowed_extensions': cleaned}, ensure_ascii=False),
            '技能包允许上传的附件类型',
        )
        log_audit(
            actor=request.user,
            action='skill_attachment_types_update',
            target='技能包附件类型',
            detail=f'{",".join(before)} → {",".join(cleaned)}',
        )
        return Response({
            'detail': '已更新技能包附件类型',
            'allowed_extensions': cleaned,
            'previous_extensions': before,
        })


DEFAULT_SITE_NAME = '商客AI一体机'
BRANDING_LOGO_DIR = 'branding'


def _get_branding_data(request=None):
    """读取品牌配置，LOGO 相对路径会转成绝对 URL"""
    site_name = SystemSetting.get_value('site_name', DEFAULT_SITE_NAME)
    site_logo = SystemSetting.get_value('site_logo', '')
    if site_logo and request:
        if site_logo.startswith('/'):
            site_logo = request.build_absolute_uri(site_logo)
    return {'site_name': site_name, 'site_logo': site_logo}


class ProxyEndpointViewSet(viewsets.ModelViewSet):
    """平台级代理条目（读写均仅管理员）

    写操作会立刻触发一次 SearXNG 配置应用（渲染 + restart + 健康检查），
    响应里带 `apply`，前端据此展示“已生效/未生效”。
    """

    queryset = ProxyEndpoint.objects.all()
    serializer_class = ProxyEndpointSerializer
    permission_classes = [IsAdminUser]

    def _audit(self, actor, action_name, target, detail):
        log_audit(actor=actor, action=action_name, target=target, detail=detail)

    def _apply(self, request, response):
        proxy_service.clear_cache()
        response.data['apply'] = proxy_service.apply_searxng(request.user)
        return response

    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        obj = ProxyEndpoint.objects.get(pk=response.data['id'])
        self._audit(request.user, 'proxy_create', obj.name,
                    f'{obj.scheme}://{obj.host}:{obj.port}，启用={obj.enabled}')
        return self._apply(request, response)

    def update(self, request, *args, **kwargs):
        obj = self.get_object()
        before = {'name': obj.name, 'scheme': obj.scheme, 'host': obj.host,
                  'port': obj.port, 'enabled': obj.enabled}
        had_password = obj.has_password
        response = super().update(request, *args, **kwargs)
        obj.refresh_from_db()
        changed_secret = had_password != obj.has_password or 'password' in request.data
        detail = f'{before} → {obj.name}/{obj.scheme}://{obj.host}:{obj.port}，启用={obj.enabled}'
        if changed_secret:
            detail += '；凭据整值替换（值不落审计）'
        self._audit(request.user, 'proxy_update', obj.name, detail)
        return self._apply(request, response)

    def destroy(self, request, *args, **kwargs):
        obj = self.get_object()
        bound = obj.bindings.count()
        if bound:
            return Response({'detail': f'该代理仍被 {bound} 个资源绑定，先解绑再删除'},
                            status=status.HTTP_409_CONFLICT)
        name = obj.name
        try:
            obj.delete()
        except ProtectedError:
            return Response({'detail': '该代理仍被资源绑定，先解绑再删除'},
                            status=status.HTTP_409_CONFLICT)
        self._audit(request.user, 'proxy_delete', name, '删除代理条目')
        proxy_service.clear_cache()
        return Response({'deleted': True, 'apply': proxy_service.apply_searxng(request.user)})

    @action(detail=True, methods=['post'], url_path='test')
    def test(self, request, pk=None):
        proxy = self.get_object()
        result = proxy_service.test_endpoint(proxy, actor=request.user)
        result['endpoint'] = self.get_serializer(proxy).data
        return Response(result)

    @action(detail=False, methods=['post'], url_path='apply')
    def apply_bindings(self, request):
        """重试应用：把当前绑定重新写进 SearXNG 并重启"""
        result = proxy_service.apply_searxng(request.user)
        return Response({'apply': result, 'state': proxy_service.binding_state()})


class ProxyBindingView(APIView):
    """代理绑定：GET 读整表，PUT 整表覆盖后立刻应用"""

    permission_classes = [IsAdminUser]

    def get(self, request):
        return Response(proxy_service.binding_state())

    def put(self, request):
        serializer = ProxyBindingPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        engines = data.get('engines', {})
        rows = data.get('tiangong', [])

        proxy_ids = set(v for v in engines.values() if v) | {r['proxy_id'] for r in rows}
        existing = set(ProxyEndpoint.objects.filter(pk__in=proxy_ids)
                       .values_list('pk', flat=True))
        missing = sorted(proxy_ids - existing)
        if missing:
            return Response({'detail': f'代理不存在：{missing}'},
                            status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            ResourceProxyBinding.objects.filter(
                resource_type__in=[ResourceProxyBinding.TYPE_SEARXNG_ENGINE,
                                   ResourceProxyBinding.TYPE_TIANGONG_DOMAIN,
                                   ResourceProxyBinding.TYPE_TIANGONG_SOURCE],
            ).delete()
            for name, proxy_id in engines.items():
                if proxy_id:
                    ResourceProxyBinding.objects.create(
                        resource_type=ResourceProxyBinding.TYPE_SEARXNG_ENGINE,
                        resource_key=name, proxy_id=proxy_id)
            for row in rows:
                kind = (ResourceProxyBinding.TYPE_TIANGONG_DOMAIN
                        if row['kind'] == 'domain'
                        else ResourceProxyBinding.TYPE_TIANGONG_SOURCE)
                # 天工绑定不经过 SearXNG 应用，保存即生效
                ResourceProxyBinding.objects.create(
                    resource_type=kind, resource_key=row['key'],
                    proxy_id=row['proxy_id'], applied=True)

        summary = '，'.join(
            [f'{k}={v or "直连"}' for k, v in engines.items()]
            + [f'{r["key"]}→{r["proxy_id"]}' for r in rows]
        ) or '全部直连'
        log_audit(actor=request.user, action='proxy_bind', target='代理绑定',
                  detail=summary[:200])

        proxy_service.clear_cache()
        result = proxy_service.apply_searxng(request.user)
        return Response({'saved': True, 'apply': result,
                         'state': proxy_service.binding_state()})


class BrandingView(APIView):
    """企业品牌配置：GET 公开，POST 仅管理员（支持 LOGO 上传）"""

    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAdminUser()]

    def get(self, request):
        return Response(_get_branding_data(request))

    def post(self, request):
        serializer = BrandingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        site_name = data['site_name']
        site_logo_file = data.get('site_logo')

        logo_path = ''
        if site_logo_file:
            ext = os.path.splitext(site_logo_file.name)[1].lower() or '.png'
            saved_path = default_storage.save(
                f'{BRANDING_LOGO_DIR}/logo{ext}',
                site_logo_file,
            )
            logo_path = f'{settings.MEDIA_URL}{saved_path}'

        SystemSetting.set_value('site_name', site_name, '企业名称')
        if logo_path:
            SystemSetting.set_value('site_logo', logo_path, '企业 LOGO')

        return Response(_get_branding_data(request))
