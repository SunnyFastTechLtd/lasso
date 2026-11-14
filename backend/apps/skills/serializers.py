"""技能规范序列化（C1/C2/C3 的字段口径）

写入侧口径（PRD 9.2 #7 + 决策 10）：名称缺失/冲突、适用说明缺失、正文为空、
正文超长（>8000 字符）属于结构性错误，直接 400 且不落库。高危风险命中不再硬阻断，
由视图层校验 risk_ack/risk_fingerprint 后放行（422 确认流程）。
保存成功的技能直接进入待审核；可疑但不高危的扫描项写入 scan_warnings 供审核展示。
"""
from rest_framework import serializers

from backend.apps.common.choices import Visibility
from backend.apps.skills.models import MAX_BODY_CHARS, Skill, SkillScope, SkillVersion
from backend.apps.skills.scanner import describe, scan_body
from backend.apps.skills.services import reference_count

# C5 定向用户白名单上限（单技能），超限 400，避免名单无限膨胀
MAX_TARGETED_USERS = 200


class SkillSerializer(serializers.ModelSerializer):
    """列表与详情共用；列表不带正文与生效正文，避免整页拖正文"""

    status_display = serializers.CharField(source='get_status_display', read_only=True)
    source_display = serializers.CharField(source='get_source_display', read_only=True)
    visibility_display = serializers.CharField(source='get_visibility_display', read_only=True)
    mountable = serializers.BooleanField(read_only=True)
    live = serializers.BooleanField(read_only=True)
    editable = serializers.BooleanField(read_only=True)
    body_changed = serializers.BooleanField(read_only=True)
    has_body = serializers.SerializerMethodField()
    agent_count = serializers.SerializerMethodField()
    reviewed_by_name = serializers.SerializerMethodField()
    mirror_path = serializers.SerializerMethodField()
    allowed_department_ids = serializers.ListField(
        child=serializers.IntegerField(), write_only=True, required=False)
    allowed_departments = serializers.SlugRelatedField(
        slug_field='name', many=True, read_only=True)
    applicable_department_ids = serializers.ListField(
        child=serializers.IntegerField(), write_only=True, required=False)
    applicable_departments = serializers.SlugRelatedField(
        slug_field='name', many=True, read_only=True)
    applicable_user_ids = serializers.ListField(
        child=serializers.IntegerField(), write_only=True, required=False)
    applicable_users = serializers.SerializerMethodField()
    scope_mode_display = serializers.CharField(source='get_scope_mode_display', read_only=True)
    files = serializers.SerializerMethodField()
    file_count = serializers.SerializerMethodField()
    package_bytes = serializers.SerializerMethodField()
    risk_ack = serializers.SerializerMethodField()

    class Meta:
        model = Skill
        fields = [
            'id', 'name', 'slug', 'description', 'source', 'source_display',
            'entry_file', 'status', 'status_display', 'char_count', 'has_body',
            'body', 'approved_body', 'body_changed', 'validation_errors',
            'reject_reason', 'scan_warnings', 'requires', 'mountable', 'live', 'editable',
            'visibility', 'visibility_display', 'allowed_department_ids',
            'allowed_departments', 'scope_mode', 'scope_mode_display',
            'scope_include_sub_departments', 'applicable_department_ids',
            'applicable_departments', 'rollout_percent', 'applicable_user_ids',
            'applicable_users', 'current_version', 'files', 'file_count',
            'package_bytes', 'file_scan_warnings', 'risk_ack',
            'agent_count', 'reviewed_by_name', 'reviewed_at',
            'mirror_path', 'created_at', 'updated_at',
        ]
        read_only_fields = ['slug', 'entry_file', 'status', 'validation_errors',
                            'reject_reason', 'scan_warnings', 'body_hash', 'approved_body',
                            'char_count', 'current_version', 'file_scan_warnings',
                            'reviewed_by', 'reviewed_at', 'created_at', 'updated_at']
        extra_kwargs = {
            'name': {'required': False, 'allow_blank': True, 'validators': []},
            'description': {'required': False, 'allow_blank': True},
            'body': {'required': False, 'allow_blank': True},
            'rollout_percent': {
                'min_value': 0, 'max_value': 100,
                'error_messages': {
                    'invalid': '灰度比例需为 0–100 的整数',
                    'min_value': '灰度比例不能小于 0',
                    'max_value': '灰度比例不能大于 100',
                },
            },
        }

    def get_has_body(self, obj) -> bool:
        return bool((obj.body or '').strip())

    def get_agent_count(self, obj) -> int:
        return reference_count(obj)

    def get_reviewed_by_name(self, obj) -> str:
        return obj.reviewed_by.username if obj.reviewed_by else ''

    def get_mirror_path(self, obj) -> str:
        """运行时镜像位置；未生效的技能回空串（前端据此显示"未进运行时"）"""
        if not (obj.mountable and obj.runtime_body):
            return ''
        return str(obj.mirror_dir / (obj.entry_file or 'SKILL.md'))

    def get_applicable_users(self, obj):
        """定向用户（读）：给 id 便于编辑回填，给用户名与部门便于识别"""
        return [{'id': u.pk, 'username': u.username,
                 'department': getattr(u, 'department', '') or ''}
                for u in obj.applicable_users.all()]

    def get_files(self, obj):
        items = [f for f in obj.files.all()
                 if not f.entry and f.rel_path != obj.entry_file]
        return [{
            'id': item.pk,
            'path': item.rel_path,
            'size': item.size,
            'sha256': item.sha256,
            'updated_at': item.updated_at,
            'download_url': f'/api/skills/{obj.pk}/files/{item.pk}/download/',
        } for item in sorted(items, key=lambda f: f.rel_path)]

    def get_file_count(self, obj) -> int:
        return len(self.get_files(obj))

    def get_package_bytes(self, obj) -> int:
        return sum(item['size'] for item in self.get_files(obj))

    def get_risk_ack(self, obj):
        if not obj.risk_ack_at:
            return None
        return {
            'at': obj.risk_ack_at,
            'by': obj.risk_ack_by.username if obj.risk_ack_by else '',
            'ruleset_version': obj.risk_ack_ruleset_version,
            'rules': obj.risk_ack_rules or [],
            'files': obj.risk_ack_files or [],
        }

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if self.context.get('compact'):
            data.pop('body', None)
            data.pop('approved_body', None)
        return data

    def validate(self, attrs):
        """逐条收集问题后一次性 400：字段名 + 中文原因，任意一条失败都不落库"""
        current = self.instance
        errors = {}

        name = _text(attrs, current, 'name')
        if not name.strip():
            errors['name'] = '缺少技能名称'
        else:
            qs = Skill.objects.filter(name=name.strip())
            if current is not None:
                qs = qs.exclude(pk=current.pk)
            conflict = qs.first()
            if conflict:
                errors['name'] = f'名称与企业内已有技能冲突：{conflict.name}'
            else:
                attrs['name'] = name.strip()

        visibility = _text(attrs, current, 'visibility') or Visibility.ADMIN_ONLY
        if visibility == Visibility.DEPARTMENT:
            dept_ids = attrs.get('allowed_department_ids')
            if dept_ids is None and current is not None:
                dept_ids = list(current.allowed_departments.values_list('id', flat=True))
            if not dept_ids:
                errors['allowed_department_ids'] = '可见范围为指定部门时必须至少选择一个部门'

        scope_mode = _text(attrs, current, 'scope_mode') or SkillScope.ALL
        if scope_mode not in SkillScope.values:
            errors['scope_mode'] = '生效范围取值无效'
        elif scope_mode == SkillScope.DEPARTMENTS:
            scope_ids = attrs.get('applicable_department_ids')
            if scope_ids is None and current is not None:
                scope_ids = list(current.applicable_departments.values_list('id', flat=True))
            if not scope_ids:
                errors['applicable_department_ids'] = '生效范围为指定部门时必须至少选择一个部门'

        # C5 灰度：比例取值范围由字段级校验给出中文提示；这里只校验定向名单
        if 'applicable_user_ids' in attrs:
            user_ids = attrs.get('applicable_user_ids') or []
            if len(user_ids) > MAX_TARGETED_USERS:
                errors['applicable_user_ids'] = \
                    f'定向用户最多 {MAX_TARGETED_USERS} 人（当前 {len(user_ids)} 人）'
            else:
                from backend.apps.users.models import User

                missing = sorted(set(user_ids) - set(
                    User.objects.filter(pk__in=user_ids).values_list('pk', flat=True)))
                if missing:
                    errors['applicable_user_ids'] = f'用户不存在：{missing}'

        # 内容校验一律在落库前完成（9.2 #7）：失败直接 400，不留 invalid 中间态。
        # 只有提交了正文才校验长度与扫描，避免"仅改可见范围"被历史正文误伤。
        if not _text(attrs, current, 'description').strip():
            errors['description'] = '缺一句话适用说明'
        self._scan_warnings = []
        self._high_risk_hits = []
        body_submitted = current is None or 'body' in attrs
        if body_submitted:
            body = attrs.get('body', '')
            body = body if isinstance(body, str) else ''
            if not body.strip():
                errors['body'] = '规范正文为空'
            elif len(body) > MAX_BODY_CHARS:
                errors['body'] = f'规范正文超长（{len(body)} 字，上限 {MAX_BODY_CHARS} 字）'
            else:
                result = scan_body(body)
                # 决策 10：高危不再硬阻断，交给视图层做 risk_ack 确认（422）
                self._high_risk_hits = result['high_risk']
                self._scan_warnings = [describe(item) for item in result['warnings']]

        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def create(self, validated_data):
        dept_ids = validated_data.pop('allowed_department_ids', [])
        scope_ids = validated_data.pop('applicable_department_ids', [])
        scope_user_ids = validated_data.pop('applicable_user_ids', [])
        validated_data['created_by'] = self.context['request'].user
        skill = Skill(**validated_data)
        skill.scan_warnings = getattr(self, '_scan_warnings', [])
        skill.save()
        if skill.visibility == Visibility.DEPARTMENT:
            skill.allowed_departments.set(dept_ids)
        if skill.scope_mode == SkillScope.DEPARTMENTS:
            skill.applicable_departments.set(scope_ids)
        if scope_user_ids:
            skill.applicable_users.set(scope_user_ids)
        skill.submit()
        return skill

    def update(self, instance, validated_data):
        dept_ids = validated_data.pop('allowed_department_ids', None)
        scope_ids = validated_data.pop('applicable_department_ids', None)
        scope_user_ids = validated_data.pop('applicable_user_ids', None)
        # validate() 会把当前名称回填进 attrs，因此只能按"值是否真的变了"
        # 判断是否需要重新提交审核，改可见范围/生效范围不该把已启用技能打回待审核
        before = {field: getattr(instance, field, '')
                  for field in ('name', 'description', 'body')}
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if 'body' in validated_data:
            instance.scan_warnings = getattr(self, '_scan_warnings', [])
        changed = any(before[field] != getattr(instance, field, '')
                      for field in ('name', 'description', 'body'))
        # 决策 10：确认的是「内容」，改名称/说明不该让已确认的高危放行失效
        if before['body'] != getattr(instance, 'body', ''):
            instance.clear_risk_ack()
        instance.save()
        if dept_ids is not None:
            instance.allowed_departments.set(
                dept_ids if instance.visibility == Visibility.DEPARTMENT else [])
        if scope_ids is not None:
            instance.applicable_departments.set(
                scope_ids if instance.scope_mode == SkillScope.DEPARTMENTS else [])
        if scope_user_ids is not None:
            instance.applicable_users.set(scope_user_ids)
        if changed:
            instance.submit()
        return instance


class SkillVersionSerializer(serializers.ModelSerializer):
    """C4 版本快照（含正文与附件清单；对比接口直接用服务层结构）"""

    approved_by_name = serializers.SerializerMethodField()
    files = serializers.SerializerMethodField()
    risk_ack = serializers.SerializerMethodField()

    class Meta:
        model = SkillVersion
        fields = ['version', 'body', 'body_hash', 'char_count', 'change_summary',
                  'approved_by_name', 'approved_at', 'risk_ack', 'files']
        read_only_fields = fields

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # 版本列表不回传全文，点开某个版本再取（避免一次拖几百 KB）
        if self.context.get('compact'):
            data.pop('body', None)
        return data

    def get_approved_by_name(self, obj) -> str:
        return obj.approved_by.username if obj.approved_by else ''

    def get_files(self, obj):
        return [{
            'id': item.pk,
            'path': item.rel_path,
            'size': item.size,
            'sha256': item.sha256,
            'download_url': (f'/api/skills/{obj.skill_id}/versions/{obj.version}/'
                             f'files/{item.pk}/download/'),
        } for item in obj.files.all()]

    def get_risk_ack(self, obj):
        if not obj.risk_ack_at:
            return None
        return {
            'at': obj.risk_ack_at,
            'by': obj.risk_ack_by.username if obj.risk_ack_by else '',
            'ruleset_version': obj.risk_ack_ruleset_version,
            'rules': obj.risk_ack_rules or [],
            'files': obj.risk_ack_files or [],
        }


class SkillReviewSerializer(serializers.Serializer):
    """C3 审核动作入参：必须带"已看过正文"的凭证，防止盲签"""

    body_ack = serializers.BooleanField(required=True)
    body_hash = serializers.CharField(required=True, max_length=64)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=200)

    def validate_body_ack(self, value):
        if not value:
            raise serializers.ValidationError('审核前必须展开并确认已看过正文')
        return value


def _text(attrs, instance, field):
    """取本次提交值，未提交时回落当前值"""
    value = attrs.get(field)
    if value is None and instance is not None:
        value = getattr(instance, field, '')
    return value if isinstance(value, str) else (value or '')
