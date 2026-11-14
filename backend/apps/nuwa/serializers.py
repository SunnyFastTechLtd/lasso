"""女娲接口序列化：会话、市场源、目录条目、导入申请"""
from rest_framework import serializers

from backend.apps.nuwa.models import (MarketItem, MarketSource, NuwaImportRequest,
                                      NuwaSession)


class NuwaSessionSerializer(serializers.ModelSerializer):
    expired = serializers.BooleanField(read_only=True)
    agent_name = serializers.CharField(source='agent.name', read_only=True)

    class Meta:
        model = NuwaSession
        fields = ['id', 'brief', 'audience', 'department', 'step', 'status',
                  'generated', 'generation_meta', 'edited_fields', 'candidates',
                  'selections', 'knowledge_plan', 'knowledge_items',
                  'agent', 'agent_name', 'error', 'expired',
                  'created_at', 'updated_at', 'expires_at']
        read_only_fields = fields


class MarketSourceSerializer(serializers.ModelSerializer):
    stale = serializers.BooleanField(read_only=True)

    class Meta:
        model = MarketSource
        fields = ['id', 'name', 'kind', 'base_url', 'repo', 'branch', 'auth_env',
                  'enabled', 'is_builtin', 'sync_interval_hours', 'last_sync_at',
                  'last_sync_status', 'last_error', 'item_count', 'stale',
                  'created_at', 'updated_at']
        read_only_fields = ['is_builtin', 'last_sync_at', 'last_sync_status',
                            'last_error', 'item_count']


class MarketItemSerializer(serializers.ModelSerializer):
    source_name = serializers.CharField(source='source.name', read_only=True)
    license_declared = serializers.SerializerMethodField()
    my_request = serializers.SerializerMethodField()

    class Meta:
        model = MarketItem
        fields = ['id', 'source', 'source_name', 'kind', 'external_id', 'version',
                  'title', 'summary', 'publisher', 'homepage', 'repository_url',
                  'license', 'license_declared', 'trust_level', 'transport',
                  'requires_egress', 'has_scripts', 'egress_host', 'capabilities',
                  'status', 'fetched_at', 'my_request']
        read_only_fields = fields

    @staticmethod
    def get_license_declared(obj) -> bool:
        return bool(obj.license)

    def get_my_request(self, obj) -> dict | None:
        """当前用户对该条目最近一条申请，供市场页就地展示状态"""
        user = getattr(self.context.get('request'), 'user', None)
        if not user or not user.is_authenticated:
            return None
        latest = (obj.import_requests.filter(requested_by=user)
                  .order_by('-created_at').first())
        if not latest:
            return None
        return {'id': latest.pk, 'status': latest.status,
                'created_at': latest.created_at.isoformat()}


class NuwaImportRequestSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source='get_kind_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    requested_by_name = serializers.CharField(source='requested_by.username',
                                             read_only=True)
    item_title = serializers.CharField(source='market_item.title', read_only=True)

    class Meta:
        model = NuwaImportRequest
        fields = ['id', 'kind', 'kind_display', 'market_item', 'item_title',
                  'requested_by', 'requested_by_name', 'draft_payload', 'risk_flags',
                  'status', 'status_display', 'reviewed_by', 'reviewed_at',
                  'review_note', 'result', 'created_at']
        read_only_fields = fields
