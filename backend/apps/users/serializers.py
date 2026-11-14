from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models
from .models import AuditLog, Department

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    """用户信息序列化器

    department 只读：部门归属涉及可见范围与统计口径，普通用户不能通过资料接口
    自行改（含改成不存在的值）。确需调整走 UserManageViewSet，那里有存在性校验。
    """

    class Meta:
        model = User
        fields = [
            'id', 'username', 'nickname', 'email', 'phone',
            'department', 'position', 'is_staff',
            'date_joined', 'total_chat_count', 'total_tokens_used',
            'storage_used',
        ]
        read_only_fields = [
            'id', 'username', 'is_staff', 'department', 'date_joined',
            'total_chat_count', 'total_tokens_used', 'storage_used',
        ]


class LoginSerializer(serializers.Serializer):
    """登录序列化器"""
    username = serializers.CharField(required=True, max_length=150)
    password = serializers.CharField(required=True, max_length=128)
    remember = serializers.BooleanField(default=False)


class RegisterSerializer(serializers.ModelSerializer):
    """注册序列化器"""
    password = serializers.CharField(write_only=True, min_length=6)
    confirm_password = serializers.CharField(write_only=True, min_length=6)

    class Meta:
        model = User
        fields = ['username', 'password', 'confirm_password', 'nickname', 'email']

    def validate(self, attrs):
        if attrs['password'] != attrs['confirm_password']:
            raise serializers.ValidationError({'confirm_password': '两次密码不一致'})
        return attrs

    def create(self, validated_data):
        validated_data.pop('confirm_password')
        user = User.objects.create_user(**validated_data)
        return user


class ChangePasswordSerializer(serializers.Serializer):
    """修改密码序列化器

    强度校验统一走 Django 的 validate_password，口径即 settings.AUTH_PASSWORD_VALIDATORS，
    与后续新增的口令入口保持一致，不在序列化器里另立规则。
    """
    old_password = serializers.CharField(required=True)
    new_password = serializers.CharField(required=True, min_length=6)
    confirm_password = serializers.CharField(required=True, min_length=6)

    def validate(self, attrs):
        if attrs['new_password'] != attrs['confirm_password']:
            raise serializers.ValidationError({'confirm_password': '两次密码不一致'})
        return attrs

    def validate_new_password(self, value):
        # 上下文传入当前用户，UserAttributeSimilarityValidator 才能比对用户名/昵称
        try:
            validate_password(value, self.context.get('user'))
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value


class AdminUserSerializer(serializers.ModelSerializer):
    """管理员维护用户：可设置密码、权限与启用状态

    is_last_admin 由后端计算后下发：前端据此在按钮上直接体现「最后一个管理员
    不能降级/停用」，而不是让管理员点下去才吃 400。is_superuser 只读暴露，
    避免超级管理员被界面误显示成普通用户（其超管权限不经本接口修改）。
    """

    password = serializers.CharField(write_only=True, required=False, min_length=6)
    is_superuser = serializers.BooleanField(read_only=True)
    is_last_admin = serializers.SerializerMethodField()
    deleted_by_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'username', 'password', 'nickname', 'email', 'phone',
            'department', 'position', 'is_staff', 'is_superuser', 'is_active',
            'is_last_admin', 'is_deleted', 'deleted_at', 'deleted_by_name',
            'last_login', 'date_joined',
        ]
        read_only_fields = [
            'id', 'is_superuser', 'is_last_admin', 'is_deleted', 'deleted_at',
            'deleted_by_name', 'last_login', 'date_joined',
        ]

    def get_deleted_by_name(self, obj):
        return obj.deleted_by.username if obj.deleted_by_id else ''

    def get_is_last_admin(self, obj):
        """该账号是否系统中最后一个具备管理权限的有效账号

        列表查询已注解 has_other_active_admin，避免逐行 count；单对象响应
        （创建/更新回显）没有注解时回退查一次。
        """
        if not obj.is_active or not (obj.is_staff or obj.is_superuser):
            return False
        has_other = getattr(obj, 'has_other_active_admin', None)
        if has_other is None:
            has_other = User.objects.filter(is_active=True).filter(
                models.Q(is_staff=True) | models.Q(is_superuser=True)
            ).exclude(pk=obj.pk).exists()
        return not has_other

    def validate_department(self, value):
        """部门必须从已存在的 Department 中选择（空字符串表示未分配）"""
        value = (value or '').strip()
        if value and not Department.objects.filter(name=value).exists():
            raise serializers.ValidationError('部门不存在，请先创建部门')
        return value

    def validate(self, attrs):
        """管理员设置/重置密码与改密接口同一套强度口径（AUTH_PASSWORD_VALIDATORS）

        建号与重置都校验：否则管理员可以先建一个弱口令账号再登录，等于绕过强度要求。
        """
        password = attrs.get('password')
        if password:
            try:
                validate_password(password, self.instance)
            except DjangoValidationError as exc:
                raise serializers.ValidationError({'password': list(exc.messages)})
        return attrs

    def create(self, validated_data):
        password = validated_data.pop('password', None)
        user = User(**validated_data)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        user = super().update(instance, validated_data)
        if password:
            user.set_password(password)
            user.save()
        return user


class DepartmentSerializer(serializers.ModelSerializer):
    """部门序列化器"""

    class Meta:
        model = Department
        fields = ['id', 'name', 'description', 'created_at']
        read_only_fields = ['id', 'created_at']

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('部门名称不能为空')
        # 更新时排除自身
        qs = Department.objects.filter(name=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError('部门名称已存在')
        return value


class AuditLogSerializer(serializers.ModelSerializer):
    """审计日志序列化器"""

    actor_name = serializers.SerializerMethodField()
    action_display = serializers.CharField(source='get_action_display', read_only=True)
    # 列表用 Exists() 注解一次判定，不逐行查全文表；未注解时回退 False（不额外查库）
    has_body = serializers.SerializerMethodField()

    def get_actor_name(self, obj):
        """优先用写入时的用户名快照：账号被物理清理后 actor 为空也能追溯。"""
        if obj.actor_name:
            return obj.actor_name
        if obj.actor_id:
            return obj.actor.display_name
        return '系统'

    def get_has_body(self, obj):
        return bool(getattr(obj, 'has_body', False))

    class Meta:
        model = AuditLog
        fields = [
            'id', 'actor', 'actor_name', 'action', 'action_display',
            'target', 'detail', 'ip', 'user_agent', 'created_at', 'has_body',
        ]
        read_only_fields = fields
