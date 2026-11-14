"""MCP 工具来源序列化器（B1/B2/B5/B7/B8 的字段与校验口径）

业务规则仍以 models.py 为准，本层只做：条件必填、命令前缀闸门、密钥掩码回填、
出网标记判定、内置来源保护，以及只读统计字段的组装。
"""
from rest_framework import serializers

from backend.apps.mcp import services
from backend.apps.mcp.models import TIMEOUT_BY_TRANSPORT, MCPServer, MCPTool

MASK_PREFIX = '******'
SECRET_GROUPS = ('headers', 'env')


class MCPServerSerializer(serializers.ModelSerializer):
    """来源读写。

    写入面：名称/描述/连接类型与参数/连接方式/超时/密钥/可见范围。
    只读面：slug、状态机字段、发现结果、内置与出网标记（出网由平台判定，不可自取消）。
    """

    secrets = serializers.JSONField(
        write_only=True, required=False,
        help_text='形如 {"headers": {"Authorization": "……"}, "env": {"K": "V"}}；整值替换')
    secrets_masked = serializers.SerializerMethodField()
    has_secrets = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    transport_display = serializers.CharField(source='get_transport_display', read_only=True)
    lifecycle_display = serializers.CharField(source='get_lifecycle_display', read_only=True)
    timeout_default = serializers.SerializerMethodField()
    tool_count = serializers.SerializerMethodField()
    enabled_tool_count = serializers.SerializerMethodField()
    new_tool_count = serializers.SerializerMethodField()
    agent_count = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    allowed_department_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, allow_empty=True,
        write_only=True,
        help_text='可见范围为"指定部门"时填写部门 ID 列表；M2M，读取面见 allowed_departments')
    allowed_departments = serializers.SlugRelatedField(
        many=True, read_only=True, slug_field='name')

    class Meta:
        model = MCPServer
        fields = [
            'id', 'name', 'slug', 'description',
            'transport', 'transport_display', 'url', 'headers', 'egress_domains',
            'command', 'args', 'cwd',
            'secrets', 'secrets_masked', 'has_secrets',
            'is_stateful', 'lifecycle', 'lifecycle_display',
            'timeout_seconds', 'effective_timeout', 'timeout_default',
            'enabled_tools', 'discovered_tools',
            'tool_count', 'enabled_tool_count', 'new_tool_count', 'agent_count',
            'status', 'status_display', 'last_check_at', 'last_error',
            'requires_egress', 'is_builtin', 'is_active',
            'visibility', 'allowed_department_ids', 'allowed_departments',
            'created_by_name', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'slug', 'discovered_tools', 'status', 'last_check_at', 'last_error',
            'requires_egress', 'is_builtin', 'is_active', 'created_at', 'updated_at',
        ]

    def get_timeout_default(self, obj):
        return TIMEOUT_BY_TRANSPORT.get(obj.transport, 30)

    def get_secrets_masked(self, obj):
        return obj.get_masked_secrets()

    def get_has_secrets(self, obj):
        return bool(obj.secrets_enc)

    def get_tool_count(self, obj):
        return obj.tools.filter(state='present').count()

    def get_enabled_tool_count(self, obj):
        return len(obj.enabled_tools or [])

    def get_new_tool_count(self, obj):
        return obj.tools.filter(enabled=False, state='present').count()

    def get_agent_count(self, obj):
        return obj.agentmcpserver_set.count()

    def get_created_by_name(self, obj):
        return obj.created_by.display_name if obj.created_by else ''

    # ─── 字段级校验 ───

    def validate_name(self, value):
        value = (value or '').strip()
        qs = MCPServer.objects.filter(name=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError('名称已存在；改名会破坏已有授权规则，请先处理引用')
        return value

    def validate_url(self, value):
        value = (value or '').strip()
        if value and not value.lower().startswith(('http://', 'https://')):
            raise serializers.ValidationError('接口型来源地址需为 http/https 合法地址')
        return value

    def validate_headers(self, value):
        value = value or {}
        if not isinstance(value, dict):
            raise serializers.ValidationError('请求头需为键值对象')
        for key, val in value.items():
            if MASK_PREFIX in str(val):
                raise serializers.ValidationError(
                    f'请求头 {key} 含密钥形态的值，请改放 secrets 字段')
        return value

    def validate_args(self, value):
        value = value or []
        if not isinstance(value, list):
            raise serializers.ValidationError('启动参数需为字符串数组')
        return [str(v) for v in value]

    def validate_egress_domains(self, value):
        """允许域名：精确主机或 *.example.com，只填主机名；出网来源必须覆盖 url 主机"""
        if value in (None, ''):
            domains = []
        elif not isinstance(value, list):
            raise serializers.ValidationError('请提交域名数组')
        else:
            try:
                domains = services.normalize_domains(value)
            except ValueError as exc:
                raise serializers.ValidationError(str(exc)) from exc
        url = ((self.initial_data.get('url') if hasattr(self, 'initial_data') else '')
               or getattr(self.instance, 'url', '') or '')
        if url and services.is_egress_target(url):
            host = services.url_host(url)
            if not domains:
                raise serializers.ValidationError(
                    f'出网类来源必须声明允许访问的域名，且需覆盖 {host}；'
                    f'支持精确域名与 *.example.com 通配')
            if not services.domain_allows(host, domains):
                raise serializers.ValidationError(
                    f'允许域名未覆盖服务地址主机 {host}；'
                    f'请补充 {host}，或对应父域的 *.example.com 通配')
        return domains

    def validate_timeout_seconds(self, value):
        if value in (None, 0):
            return None
        value = int(value)
        if not 5 <= value <= 600:
            raise serializers.ValidationError('超时可留空按平台固定值，或取 5–600 秒')
        return value

    # ─── 跨字段校验 ───

    def validate(self, attrs):
        instance = self.instance
        transport = attrs.get('transport', getattr(instance, 'transport', 'http_mcp'))
        url = attrs.get('url', getattr(instance, 'url', ''))
        command = attrs.get('command', getattr(instance, 'command', ''))
        args = attrs.get('args', getattr(instance, 'args', []))
        lifecycle = attrs.get('lifecycle', getattr(instance, 'lifecycle', 'per_session'))

        if instance is not None and instance.is_builtin:
            self._reject_builtin_edits(attrs, instance)

        if transport == 'http_mcp':
            if not url:
                raise serializers.ValidationError({'url': '接口型来源必须填写服务地址'})
            # B8：出网标记由平台判定，管理员不可自行取消
            attrs['requires_egress'] = services.is_egress_target(url)
            if attrs['requires_egress']:
                # B8-a：出网来源必须声明允许域名，且覆盖服务地址主机
                host = services.url_host(url)
                domains = attrs.get(
                    'egress_domains',
                    ((getattr(instance, 'egress_domains', None) or []) if instance else []))
                if not domains:
                    raise serializers.ValidationError(
                        f'出网类来源必须声明允许访问的域名，且需覆盖 {host}；'
                        f'支持精确域名与 *.example.com 通配')
                if not services.domain_allows(host, domains):
                    raise serializers.ValidationError(
                        f'允许域名未覆盖服务地址主机 {host}；'
                        f'请补充 {host}，或对应父域的 *.example.com 通配')
        else:
            if not command:
                raise serializers.ValidationError({'command': '命令型来源必须填写启动命令'})
            if match := services.match_command_prefix(command, args):
                attrs.setdefault('command_note', match['usage'])
            else:
                services.audit_command_denied(
                    self.context.get('request'), command, args)
                raise serializers.ValidationError({
                    'command': '启动命令未命中平台内置的允许前缀白名单，保存已被阻断；'
                               '如需新增前缀请联系平台版本发布（界面不提供新增入口）'
                })
            # B2 例外：命令型不支持"每次独立会话"，连接方式锁定为常驻
            attrs['lifecycle'] = 'persistent'
            attrs['is_stateful'] = True

        if 'secrets' in attrs:
            attrs['secrets'] = self._resolve_masked_secrets(attrs['secrets'])

        # 可见范围为"指定部门"时必须给出部门，否则装配页选择器会无人可见
        visibility = attrs.get('visibility', getattr(instance, 'visibility', 'public'))
        dept_ids = attrs.get('allowed_department_ids')
        if visibility == 'department' and not (dept_ids or (
                instance is not None and instance.allowed_departments.exists())):
            raise serializers.ValidationError(
                {'allowed_department_ids': '可见范围选为指定部门时，必须至少选择一个部门'})
        if visibility != 'department' and instance is not None:
            attrs['allowed_department_ids'] = []
        return attrs

    def _reject_builtin_edits(self, attrs, instance):
        locked = {
            'transport': instance.transport, 'url': instance.url,
            'command': instance.command, 'args': instance.args, 'cwd': instance.cwd,
        }
        errors = {}
        for field, original in locked.items():
            if field in attrs and attrs[field] != original:
                errors[field] = '内置演示来源不可编辑地址与命令，只能改名称、描述与工具启停'
        if errors:
            raise serializers.ValidationError(errors)

    def _resolve_masked_secrets(self, submitted):
        """掩码占位＝未修改，回填原值；显式删除的键才删除（B5 例外说明）"""
        if not isinstance(submitted, dict):
            raise serializers.ValidationError({'secrets': 'secrets 需为 {"headers":{},"env":{}} 结构'})
        unknown = set(submitted) - set(SECRET_GROUPS)
        if unknown:
            raise serializers.ValidationError(
                {'secrets': f'不支持的密钥分组：{"、".join(sorted(unknown))}，只允许 headers 与 env'})

        current = self.instance.get_secrets() if self.instance else {}
        resolved = {}
        for group in SECRET_GROUPS:
            if group not in submitted:
                if current.get(group):
                    resolved[group] = dict(current[group])
                continue
            merged = {}
            for key, val in (submitted[group] or {}).items():
                if self._is_mask(val) and key in (current.get(group) or {}):
                    merged[key] = current[group][key]
                else:
                    merged[key] = str(val)
            if merged:
                resolved[group] = merged
        return resolved

    @staticmethod
    def _is_mask(value) -> bool:
        return str(value or '').startswith(MASK_PREFIX)

    # ─── 写库 ───

    def _resolve_departments(self, ids):
        """部门 ID 列表 → Department 实例列表"""
        from backend.apps.users.models import Department

        return list(Department.objects.filter(pk__in=ids))

    def create(self, validated_data):
        validated_data.pop('command_note', None)
        secrets = validated_data.pop('secrets', None)
        dept_ids = validated_data.pop('allowed_department_ids', None)
        request = self.context.get('request')
        server = MCPServer.objects.create(
            created_by=getattr(request, 'user', None), **validated_data)
        if secrets:
            server.set_secrets(secrets)
            server.save(update_fields=['secrets_enc'])
        if dept_ids:
            server.allowed_departments.set(self._resolve_departments(dept_ids))
        return server

    def update(self, instance, validated_data):
        validated_data.pop('command_note', None)
        secrets = validated_data.pop('secrets', None)
        dept_ids = validated_data.pop('allowed_department_ids', None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if secrets is not None:
            instance.set_secrets(secrets)
            instance.save(update_fields=['secrets_enc'])
        if dept_ids is not None:
            instance.allowed_departments.set(self._resolve_departments(dept_ids))
        return instance


class MCPToolSerializer(serializers.ModelSerializer):
    """来源内单个工具（B4 启停与失效标记）"""

    params = serializers.SerializerMethodField()
    state_display = serializers.CharField(source='get_state_display', read_only=True)

    class Meta:
        model = MCPTool
        fields = ['name', 'full_name', 'description', 'input_schema', 'params',
                  'enabled', 'state', 'state_display', 'first_seen_at', 'last_seen_at']
        read_only_fields = ['full_name', 'description', 'input_schema', 'params',
                            'state', 'first_seen_at', 'last_seen_at']

    def get_params(self, obj):
        return services.params_summary(obj.input_schema or {})
