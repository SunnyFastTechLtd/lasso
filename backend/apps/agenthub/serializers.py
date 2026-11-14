"""智能体装配序列化器（A1–A6 的字段与校验口径）

列表面（A1）不带提示词正文，只给挂载摘要与最近修改信息；
详情面（A2/A3/A4/A5/A6）给全量配置；写入面把四个挂载分区整体覆盖式落库。
档位一律只能选预设，数值由平台维护（A3）。
"""
from rest_framework import serializers

from backend.apps.agenthub import services
from backend.apps.agenthub.models import (
    AgentImportRequest, AgentProfile, AgentStatus, AgentVersion, KnowledgeMode, PermissionTier,
    RetrievalTier, SpeechTier,
)
from backend.apps.common.choices import Visibility


class AgentListSerializer(serializers.ModelSerializer):
    """A1 列表行：名称/用途描述/状态/可见范围/挂载摘要/最近修改时间与修改人

    提示词正文不进列表（A1 明确"列表不展示提示词正文"）。
    """

    status_display = serializers.CharField(source='get_status_display', read_only=True)
    visibility_display = serializers.CharField(source='get_visibility_display', read_only=True)
    mount_summary = serializers.SerializerMethodField()
    updated_by_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    pending_changes = serializers.SerializerMethodField()

    class Meta:
        model = AgentProfile
        fields = [
            'id', 'name', 'slug', 'description', 'job_tags', 'icon',
            'status', 'status_display', 'visibility', 'visibility_display',
            'model_name', 'speech_tier', 'retrieval_tier',
            'permission_tier', 'mount_summary',
            'is_builtin', 'is_customized', 'current_version', 'published_at',
            'pending_changes', 'created_by_name', 'updated_by_name',
            'created_at', 'updated_at',
        ]

    def get_mount_summary(self, obj):
        return obj.mount_summary

    def get_updated_by_name(self, obj):
        return obj.updated_by.display_name if obj.updated_by else ''

    def get_created_by_name(self, obj):
        return obj.created_by.display_name if obj.created_by else ''

    def get_pending_changes(self, obj):
        """A7/7.3：改了还没重新发布，列表上给"有未发布的修改"标记"""
        return services.pending_changes(obj)


class AgentDetailSerializer(serializers.ModelSerializer):
    """详情与编辑回填：全量配置（含提示词正文）+ 挂载明细 + 检查结论入口"""

    status_display = serializers.CharField(source='get_status_display', read_only=True)
    visibility_display = serializers.CharField(source='get_visibility_display', read_only=True)
    speech_tier_display = serializers.CharField(source='get_speech_tier_display', read_only=True)
    retrieval_tier_display = serializers.CharField(source='get_retrieval_tier_display', read_only=True)
    knowledge_mode_display = serializers.CharField(source='get_knowledge_mode_display', read_only=True)
    permission_tier_display = serializers.CharField(source='get_permission_tier_display', read_only=True)
    mount_summary = serializers.SerializerMethodField()
    mcp_mounts = serializers.SerializerMethodField()
    skill_items = serializers.SerializerMethodField()
    knowledge_bases = serializers.SerializerMethodField()
    tool_conflicts = serializers.SerializerMethodField()
    speech_params = serializers.SerializerMethodField()
    max_tool_rounds = serializers.SerializerMethodField(allow_null=True)
    allowed_departments = serializers.SlugRelatedField(many=True, read_only=True, slug_field='name')
    allowed_department_ids = serializers.SerializerMethodField()
    owner_department_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    updated_by_name = serializers.SerializerMethodField()
    published_by_name = serializers.SerializerMethodField()
    pending_changes = serializers.SerializerMethodField()
    placeholders = serializers.SerializerMethodField()

    class Meta:
        model = AgentProfile
        fields = [
            'id', 'name', 'slug', 'description', 'job_tags', 'icon',
            'model_name', 'speech_tier', 'speech_tier_display',
            'retrieval_tier', 'retrieval_tier_display', 'enable_thinking', 'prompt',
            'knowledge_mode', 'knowledge_mode_display', 'followup_rewrite',
            'default_preferences',
            'placeholders', 'speech_params', 'max_tool_rounds',
            'mcp_mounts', 'skill_items', 'knowledge_bases', 'builtin_capabilities',
            'tool_conflicts', 'mount_summary',
            'permission_tier', 'permission_tier_display', 'allow_tool_names',
            'visibility', 'visibility_display', 'allowed_department_ids',
            'allowed_departments', 'include_sub_departments',
            'status', 'status_display', 'current_version', 'pending_changes',
            'is_builtin', 'is_customized',
            'owner_department_name', 'created_by_name', 'updated_by_name',
            'published_by_name', 'published_at', 'created_at', 'updated_at',
        ]

    def get_mount_summary(self, obj):
        return obj.mount_summary

    def get_mcp_mounts(self, obj):
        rows = []
        for mount in obj.agentmcpserver_set.select_related('server').order_by('server__name'):
            server = mount.server
            rows.append({
                'server': server.pk, 'server_name': server.name, 'server_slug': server.slug,
                'server_status': server.status,
                'server_status_display': server.get_status_display(),
                'warning': ('' if server.status == 'healthy' else
                            f'来源{server.get_status_display()}'),
                'tool_names': list(mount.tool_names or []),
                'tools': [{'name': t,
                           'full_name': services.tool_full_name(server.slug, t),
                           'display_name': services.tool_display_name(server.name, t)}
                          for t in (mount.tool_names or [])],
            })
        return rows

    def get_skill_items(self, obj):
        return [{'id': s.pk, 'name': s.name, 'slug': s.slug, 'description': s.description,
                 'status': s.status, 'status_display': s.get_status_display(),
                 'requires': list(s.requires or [])} for s in obj.skills.all()]

    def get_knowledge_bases(self, obj):
        from backend.apps.knowledge.models import KnowledgeBase

        ids = [int(i) for i in (obj.knowledge_base_ids or []) if str(i).isdigit()]
        rows = {kb.pk: kb for kb in KnowledgeBase.objects.filter(pk__in=ids)}
        return [{'id': pk, 'name': rows[pk].name if pk in rows else '',
                 'document_count': rows[pk].document_count if pk in rows else None,
                 'missing': pk not in rows}
                for pk in ids]

    def get_tool_conflicts(self, obj):
        return services.tool_conflicts(obj)

    def get_speech_params(self, obj):
        return obj.speech_params

    def get_max_tool_rounds(self, obj):
        return obj.max_tool_rounds

    def get_allowed_department_ids(self, obj):
        return list(obj.allowed_departments.values_list('pk', flat=True))

    def get_owner_department_name(self, obj):
        return obj.owner_department.name if obj.owner_department else ''

    def get_created_by_name(self, obj):
        return obj.created_by.display_name if obj.created_by else ''

    def get_updated_by_name(self, obj):
        return obj.updated_by.display_name if obj.updated_by else ''

    def get_published_by_name(self, obj):
        return obj.published_by.display_name if obj.published_by else ''

    def get_pending_changes(self, obj):
        return services.pending_changes(obj)

    def get_placeholders(self, obj):
        return services.PROMPT_PLACEHOLDERS


class AgentWriteSerializer(serializers.ModelSerializer):
    """A2/A3/A4/A5/A6 写入面

    草稿允许描述、提示词、模型为空（A1 允许先建后配），
    但一旦填写就必须合法；能否发布由 services.prepublish_check 逐项判定（A7）。
    """

    allowed_department_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, allow_empty=True,
        write_only=True)
    mcp_mounts = serializers.ListField(
        child=serializers.JSONField(), required=False, write_only=True,
        help_text='[{"server": ID, "tool_names": ["tool", …]}]，整体覆盖')
    skill_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, allow_empty=True,
        write_only=True)
    knowledge_base_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, allow_empty=True,
        write_only=True)
    # 先收字符串再做中文校验，避免 DRF 默认的 invalid_choice 英文提示
    knowledge_mode = serializers.CharField(required=False)
    default_preferences = serializers.ListField(
        child=serializers.CharField(), required=False, allow_empty=True,
        help_text='管理员预置的偏好，最多 8 条、单条 100 字；用户可自行关闭')
    builtin_capabilities = serializers.ListField(
        child=serializers.CharField(), required=False, allow_empty=True, write_only=True)
    allow_tool_names = serializers.ListField(
        child=serializers.CharField(), required=False, allow_empty=True, write_only=True,
        help_text='白名单工具唯一键；省略＝与勾选的工具一致')

    class Meta:
        model = AgentProfile
        fields = [
            'name', 'description', 'job_tags', 'icon',
            'model_name', 'speech_tier', 'retrieval_tier', 'enable_thinking', 'prompt',
            'knowledge_mode', 'followup_rewrite', 'default_preferences',
            'mcp_mounts', 'skill_ids', 'knowledge_base_ids', 'builtin_capabilities',
            'permission_tier', 'allow_tool_names',
            'visibility', 'allowed_department_ids', 'include_sub_departments',
        ]
        extra_kwargs = {
            'name': {'required': True, 'validators': [], 'error_messages': {
                'max_length': '名称不超过 30 个字',
                'min_length': '名称至少 2 个字'}},
            'description': {'required': False, 'allow_blank': True},
            'model_name': {'required': False, 'allow_blank': True},
            'prompt': {'required': False, 'allow_blank': True},
            'speech_tier': {'error_messages': {
                'invalid_choice': '说话方式只能选三档预设，不支持自填数值'}},
            'retrieval_tier': {'error_messages': {
                'invalid_choice': '查询次数上限只能选三档预设，不支持自填数值'}},
        }

    MOUNT_KEYS = ('mcp_mounts', 'skill_ids', 'knowledge_base_ids', 'builtin_capabilities')

    # ─── 字段级 ───

    def validate_name(self, value):
        value = (value or '').strip()
        if len(value) < 2:
            raise serializers.ValidationError('名称至少 2 个字')
        occupant = services.occupant_of_name(value, self.instance.pk if self.instance else None)
        if occupant is not None:
            state = occupant.get_status_display()
            owner = occupant.created_by.display_name if occupant.created_by else '其他管理员'
            raise serializers.ValidationError(
                f'名称已被占用：{owner} 创建的「{occupant.name}」（{state}），请换个名字')
        return value

    def validate_description(self, value):
        return (value or '').strip()

    def validate_model_name(self, value):
        value = (value or '').strip()
        if not value:
            return value
        options = services.verified_models()
        if not options:
            raise serializers.ValidationError(
                '系统设置里还没有"已验证可用"的模型，请先在系统设置完成模型连通性验证')
        if not services.model_available(value):
            raise serializers.ValidationError(
                f'模型「{value}」不在已验证可用列表内，当前可选：{"、".join(options)}')
        return value

    def validate_speech_tier(self, value):
        if value not in SpeechTier.values:
            raise serializers.ValidationError('说话方式只能选三档预设，不支持自填数值')
        return value

    def validate_retrieval_tier(self, value):
        if value not in RetrievalTier.values:
            raise serializers.ValidationError('查询次数上限只能选三档预设，不支持自填数值')
        return value

    def validate_knowledge_mode(self, value):
        if value not in KnowledgeMode.values:
            raise serializers.ValidationError('检索调用策略只能选「按需查」或「每轮必查」')
        return value

    def validate_default_preferences(self, value):
        """D8 预置偏好：复用与用户自建项同一套口径，避免管理员绕过内容限制"""
        from backend.apps.chat.preferences import MAX_ITEMS, validate_content
        if len(value) > MAX_ITEMS:
            raise serializers.ValidationError(f'默认偏好最多 {MAX_ITEMS} 条')
        cleaned = []
        for raw in value:
            content = validate_content(raw)
            if content not in cleaned:
                cleaned.append(content)
        return cleaned

    def validate_permission_tier(self, value):
        if not value:
            raise serializers.ValidationError('授权档位必须显式选定，不允许留空')
        if value not in PermissionTier.values:
            raise serializers.ValidationError('授权档位非法')
        return value

    def validate_visibility(self, value):
        if value not in Visibility.values:
            raise serializers.ValidationError('可见范围非法')
        if value == Visibility.ADMIN_ONLY:
            user = getattr(self.context.get('request'), 'user', None)
            if not (user and (user.is_staff or user.is_superuser)):
                raise serializers.ValidationError(
                    '「仅管理端」可见范围只有平台管理员可以设置')
        return value

    def validate_job_tags(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError('岗位标签需为字符串数组')
        return [str(v).strip() for v in value if str(v).strip()]

    def validate_mcp_mounts(self, value):
        for item in value or []:
            if not isinstance(item, dict) or not item.get('server'):
                raise serializers.ValidationError('每项需形如 {"server": ID, "tool_names": []}')
            names = item.get('tool_names')
            if names is not None and not isinstance(names, list):
                raise serializers.ValidationError('tool_names 需为工具名数组')
        return value

    # ─── 跨字段 ───

    def validate(self, attrs):
        visibility = attrs.get('visibility',
                               getattr(self.instance, 'visibility', Visibility.PRIVATE))
        dept_ids = attrs.get('allowed_department_ids')
        if visibility == Visibility.DEPARTMENT and not (dept_ids or (
                self.instance is not None and self.instance.allowed_departments.exists())):
            raise serializers.ValidationError(
                {'allowed_department_ids': '可见范围选为指定部门时必须至少选择一个部门'})
        if visibility != Visibility.DEPARTMENT:
            attrs['allowed_department_ids'] = []
        return attrs

    # ─── 写库 ───

    def _mount_kwargs(self, validated_data):
        return {key: validated_data.pop(key) for key in self.MOUNT_KEYS if key in validated_data}

    def _apply_departments(self, agent, dept_ids):
        from backend.apps.users.models import Department

        if dept_ids is None:
            return
        agent.allowed_departments.set(Department.objects.filter(pk__in=dept_ids))

    def create(self, validated_data):
        mount_kwargs = self._mount_kwargs(validated_data)
        allow = validated_data.pop('allow_tool_names', None)
        dept_ids = validated_data.pop('allowed_department_ids', None)
        request = self.context['request']
        agent = AgentProfile.objects.create(
            created_by=request.user, updated_by=request.user, **validated_data)
        services.sync_mounts(agent, actor=request.user, **mount_kwargs)
        self._apply_departments(agent, dept_ids)
        services.sync_allow_list(agent, allow)
        agent.refresh_from_db()
        return agent

    def update(self, instance, validated_data):
        mount_kwargs = self._mount_kwargs(validated_data)
        allow = validated_data.pop('allow_tool_names', None)
        dept_ids = validated_data.pop('allowed_department_ids', None)
        request = self.context['request']
        slug_before = instance.slug
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.slug = slug_before  # A2：标识创建后不可改
        instance.updated_by = request.user
        instance.save()
        services.sync_mounts(instance, actor=request.user, **mount_kwargs)
        self._apply_departments(instance, dept_ids)
        if 'mcp_mounts' in mount_kwargs or allow is not None:
            services.sync_allow_list(instance, allow)
        instance.refresh_from_db()
        return instance


class AgentVersionSerializer(serializers.ModelSerializer):
    """A8 发布历史行（只读）：版本号/发布人/发布时间/变更摘要"""

    published_by_name = serializers.SerializerMethodField()

    class Meta:
        model = AgentVersion
        fields = ['version', 'published_by_name', 'created_at', 'change_summary', 'diff']

    def get_published_by_name(self, obj):
        return obj.published_by.display_name if obj.published_by else ''


class AgentVersionDetailSerializer(AgentVersionSerializer):
    """查看某版本的完整配置（A8 只读留档，无编辑入口）"""

    class Meta(AgentVersionSerializer.Meta):
        fields = AgentVersionSerializer.Meta.fields + ['snapshot']


class AgentImportRequestSerializer(serializers.ModelSerializer):
    """导入申请列表行：名称/状态/提交与审批信息/依赖告警，不含导入包正文"""

    status_display = serializers.CharField(source='get_status_display', read_only=True)
    submitted_by_name = serializers.SerializerMethodField()
    reviewed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = AgentImportRequest
        fields = [
            'id', 'agent_name', 'description', 'status', 'status_display',
            'warnings', 'result', 'submitted_by', 'submitted_by_name', 'submitted_at',
            'reviewed_by_name', 'reviewed_at', 'review_note',
        ]

    def get_submitted_by_name(self, obj):
        return obj.submitted_by.display_name if obj.submitted_by else ''

    def get_reviewed_by_name(self, obj):
        return obj.reviewed_by.display_name if obj.reviewed_by else ''


class AgentImportRequestDetailSerializer(AgentImportRequestSerializer):
    """导入申请详情：附原始导入包，供审批人核对"""

    class Meta(AgentImportRequestSerializer.Meta):
        fields = AgentImportRequestSerializer.Meta.fields + ['package']
