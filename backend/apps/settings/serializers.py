import os
import re

from rest_framework import serializers
from .models import ProxyEndpoint, SystemSetting

_HOST_RE = re.compile(r'^[A-Za-z0-9]([A-Za-z0-9.-]{0,253})$')
_WILDCARD_RE = re.compile(r'^\*\.[A-Za-z0-9]([A-Za-z0-9.-]{0,253})$')


class SystemSettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = SystemSetting
        fields = ['key', 'value', 'description', 'updated_at']


class BrandingSerializer(serializers.Serializer):
    """企业品牌配置序列化器（LOGO 上传校验）"""

    site_name = serializers.CharField(max_length=100, required=True, trim_whitespace=True)
    site_logo = serializers.FileField(required=False, allow_null=True, allow_empty_file=False)

    def validate_site_logo(self, value):
        if value is None:
            return value
        if value.size > 500 * 1024:
            raise serializers.ValidationError('LOGO 大小不能超过 500KB')
        ext = os.path.splitext(value.name)[1].lower()
        if ext not in ('.png', '.jpg', '.jpeg', '.svg'):
            raise serializers.ValidationError('仅支持 png/jpg/svg 格式')
        return value


class LLMConfigSerializer(serializers.Serializer):
    """LLM 配置序列化器"""
    base_url = serializers.URLField(required=True, help_text='大模型 API 地址')
    api_key = serializers.CharField(required=True, help_text='API Key')
    model = serializers.CharField(required=True, help_text='主模型名称')
    temperature = serializers.FloatField(required=False, default=0.7, min_value=0, max_value=2)
    max_tokens = serializers.IntegerField(required=False, default=4096, min_value=256)
    timeout = serializers.IntegerField(required=False, default=120, min_value=10)


class ProxyEndpointSerializer(serializers.ModelSerializer):
    """代理条目：密码只写不读，读接口只回 has_password"""

    password = serializers.CharField(write_only=True, required=False, allow_blank=True,
                                     trim_whitespace=True, max_length=200)
    has_password = serializers.BooleanField(read_only=True)
    display_url = serializers.SerializerMethodField()

    class Meta:
        model = ProxyEndpoint
        fields = ['id', 'name', 'scheme', 'host', 'port', 'username', 'password',
                  'has_password', 'display_url', 'enabled', 'note',
                  'last_test_at', 'last_test_status', 'last_test_ms', 'last_test_error',
                  'updated_at']
        read_only_fields = ['last_test_at', 'last_test_status', 'last_test_ms',
                            'last_test_error', 'updated_at']

    def get_display_url(self, obj):
        return obj.display_url()

    def validate_name(self, value):
        name = (value or '').strip()
        if not name:
            raise serializers.ValidationError('名称不能为空')
        return name

    def validate_host(self, value):
        host = (value or '').strip()
        if '://' in host or '/' in host or ' ' in host:
            raise serializers.ValidationError('只填主机名或 IP，不带协议、端口或路径')
        if not _HOST_RE.match(host):
            raise serializers.ValidationError('主机名格式不合法')
        return host

    def validate_port(self, value):
        if not 1 <= int(value) <= 65535:
            raise serializers.ValidationError('端口范围 1 ~ 65535')
        return value

    def validate(self, attrs):
        password = attrs.get('password')
        username = attrs.get('username', getattr(self.instance, 'username', ''))
        if password and not username:
            raise serializers.ValidationError({'username': '填写密码时必须同时填写用户名'})
        return attrs

    def create(self, validated_data):
        password = validated_data.pop('password', '')
        instance = ProxyEndpoint(**validated_data)
        instance.set_password(password)
        instance.save()
        return instance

    def update(self, instance, validated_data):
        # 更新留空 = 不改（密码是只写字段，界面不会回显原文）
        password = validated_data.pop('password', '')
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


class ProxyBindingPutSerializer(serializers.Serializer):
    """整表保存绑定：engines 为引擎→代理 ID 映射，tiangong 为行列表"""

    engines = serializers.DictField(child=serializers.IntegerField(allow_null=True),
                                    required=False)
    tiangong = serializers.ListField(child=serializers.DictField(), required=False)

    def validate_engines(self, value):
        from backend.apps.settings.proxies import SEARXNG_ENGINES

        unknown = [key for key in value if key not in SEARXNG_ENGINES]
        if unknown:
            raise serializers.ValidationError(f'未知引擎：{", ".join(unknown)}')
        return value

    def validate_tiangong(self, value):
        rows = []
        seen = set()
        for item in value:
            kind = str(item.get('kind') or '').strip()
            key = str(item.get('key') or '').strip().lower().rstrip('.')
            proxy_id = item.get('proxy_id')
            if kind not in ('domain', 'source'):
                raise serializers.ValidationError('绑定类型只支持 domain / source')
            if kind == 'domain':
                if not (_HOST_RE.match(key) or _WILDCARD_RE.match(key)):
                    raise serializers.ValidationError(
                        f'「{key}」不是合法域名：通配只支持最左侧的 *.example.com')
            elif not str(key).isdigit():
                raise serializers.ValidationError(f'来源站标识必须是数字 ID：{key}')
            if not isinstance(proxy_id, int):
                raise serializers.ValidationError(f'「{key}」缺少代理')
            if (kind, key) in seen:
                raise serializers.ValidationError(f'「{key}」重复绑定')
            seen.add((kind, key))
            rows.append({'kind': kind, 'key': key, 'proxy_id': proxy_id})
        return rows
