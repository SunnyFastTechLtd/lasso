from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from .models import (
    DocumentChunk, KnowledgeBase, Document, TiangongRun, TiangongSource,
    TiangongSourceProposal, TiangongSearchBinding, can_manage_kb,
)
from backend.apps.nuwa import net


class KnowledgeBaseSerializer(serializers.ModelSerializer):
    """知识库序列化器"""

    class Meta:
        model = KnowledgeBase
        fields = [
            'id', 'name', 'description', 'icon', 'color', 'is_public',
            'scope', 'department',
            'document_count', 'chunk_count', 'size_bytes',
            'tiangong_enabled', 'tiangong_topics', 'tiangong_seeds',
            'tiangong_status', 'tiangong_last_run_at', 'tiangong_last_error',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'document_count', 'chunk_count', 'size_bytes',
            'tiangong_status', 'tiangong_last_run_at', 'tiangong_last_error',
            'created_at', 'updated_at',
        ]

    def validate(self, attrs):
        # 兼容旧客户端：未显式传 scope 时按 is_public 映射
        if 'scope' not in attrs and 'is_public' in attrs:
            attrs['scope'] = 'public' if attrs['is_public'] else 'private'
        if attrs.get('scope') == 'department' and not attrs.get('department', getattr(self.instance, 'department', '')):
            raise serializers.ValidationError({'department': '部门共享的知识库需要指定部门'})
        return attrs


class DocumentSerializer(serializers.ModelSerializer):
    """文档序列化器"""

    knowledge_base_name = serializers.CharField(source='knowledge_base.name', read_only=True)
    uploaded_by_name = serializers.CharField(source='uploaded_by.display_name', read_only=True)
    size_human = serializers.SerializerMethodField()
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            'id', 'knowledge_base', 'knowledge_base_name',
            'title', 'file_name', 'file_type', 'file_size', 'size_human', 'file_url',
            'status', 'error_msg', 'chunk_count', 'extraction_report',
            'source_type', 'source_model', 'source_url', 'source_site', 'source_title',
            'fetched_at',
            'uploaded_by_name', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'status', 'error_msg', 'chunk_count', 'extraction_report',
            'source_type', 'source_model', 'source_url', 'source_site', 'source_title',
            'fetched_at',
            'uploaded_by_name', 'created_at', 'updated_at',
        ]

    def validate_knowledge_base(self, value):
        """建文档 / 迁移文档都只能落到自己创建的知识库（管理员不限）"""
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is None or can_manage_kb(user, value):
            return value
        raise PermissionDenied('只能在自己创建的知识库下管理文档')

    def get_file_url(self, obj):
        if not obj.file_path:
            return None
        request = self.context.get('request')
        url = obj.file_path.url
        return request.build_absolute_uri(url) if request else url

    def get_size_human(self, obj):
        size = obj.file_size
        if size < 1024:
            return f'{size} B'
        elif size < 1024 * 1024:
            return f'{size / 1024:.1f} KB'
        elif size < 1024 * 1024 * 1024:
            return f'{size / (1024 * 1024):.1f} MB'
        else:
            return f'{size / (1024 * 1024 * 1024):.2f} GB'


class DocumentChunkSerializer(serializers.ModelSerializer):
    """文档分块（分块预览/禁检管理）"""

    has_embedding = serializers.SerializerMethodField()

    class Meta:
        model = DocumentChunk
        fields = ['id', 'chunk_index', 'content', 'is_active', 'has_embedding',
                  'meta', 'created_at']
        read_only_fields = ['id', 'chunk_index', 'content', 'has_embedding',
                            'meta', 'created_at']

    def get_has_embedding(self, obj):
        return bool(obj.embedding)


class DocumentUploadSerializer(serializers.Serializer):
    """文档上传序列化器"""
    knowledge_base_id = serializers.IntegerField(required=True)
    title = serializers.CharField(max_length=500, required=False, default='')


class ChatRequestSerializer(serializers.Serializer):
    """知识库对话请求"""
    question = serializers.CharField(required=True, max_length=4000)
    knowledge_base_id = serializers.IntegerField(required=True)
    stream = serializers.BooleanField(default=False)


class TiangongSearchBindingSerializer(serializers.ModelSerializer):
    """L1 MCP 搜索绑定：只存映射，不存任何凭据"""

    server_name = serializers.CharField(source='server.name', read_only=True)
    server_status = serializers.CharField(source='server.status', read_only=True)
    server_active = serializers.BooleanField(source='server.is_active', read_only=True)
    tool_name = serializers.CharField(source='tool.name', read_only=True)
    tool_full_name = serializers.CharField(source='tool.full_name', read_only=True)
    tool_enabled = serializers.BooleanField(source='tool.enabled', read_only=True)
    tool_state = serializers.CharField(source='tool.state', read_only=True)
    created_by_name = serializers.CharField(source='created_by.display_name',
                                            read_only=True)
    usable = serializers.BooleanField(read_only=True)
    unusable_reason = serializers.CharField(read_only=True)

    class Meta:
        model = TiangongSearchBinding
        fields = [
            'id', 'server', 'server_name', 'server_status', 'server_active',
            'tool', 'tool_name', 'tool_full_name', 'tool_enabled', 'tool_state',
            'enabled', 'priority', 'query_field', 'limit_field', 'default_limit',
            'results_path', 'url_field', 'title_field', 'snippet_field', 'extra_args',
            'note', 'usable', 'unusable_reason',
            'last_status', 'last_error', 'last_latency_ms', 'last_result_count',
            'last_checked_at', 'created_by_name', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'server_name', 'server_status', 'server_active', 'tool_name',
            'tool_full_name', 'tool_enabled', 'tool_state', 'usable', 'unusable_reason',
            'last_status', 'last_error', 'last_latency_ms', 'last_result_count',
            'last_checked_at', 'created_by_name', 'created_at', 'updated_at',
        ]

    def validate(self, attrs):
        server = attrs.get('server') or getattr(self.instance, 'server', None)
        tool = attrs.get('tool') or getattr(self.instance, 'tool', None)
        if not server or not tool:
            raise serializers.ValidationError('请选择 MCP 来源与搜索工具')
        if tool.server_id != server.id:
            raise serializers.ValidationError({'tool': '工具不属于所选 MCP 来源'})
        query_field = attrs.get('query_field', getattr(self.instance, 'query_field', 'query'))
        if not (query_field or '').strip():
            raise serializers.ValidationError({'query_field': '查询入参名不能为空'})
        limit = attrs.get('default_limit',
                          getattr(self.instance, 'default_limit', 5))
        if not 1 <= int(limit) <= 10:
            raise serializers.ValidationError({'default_limit': '返回条数需在 1~10 之间'})
        return attrs


class TiangongRunSerializer(serializers.ModelSerializer):
    """天工采集记录：谁触发、采到什么、为什么降级"""

    actor_name = serializers.CharField(source='actor.display_name', read_only=True)
    proposal_count = serializers.SerializerMethodField()

    class Meta:
        model = TiangongRun
        fields = ['id', 'knowledge_base', 'status', 'trigger', 'topics',
                  'web_created', 'model_created', 'skipped',
                  'discovery_layer', 'fallback_chain', 'search_queries', 'search_hits',
                  'candidates', 'proposals', 'approved', 'pages_fetched', 'quality_rejected',
                  'sources_tried', 'proposal_count',
                  'degraded_reason', 'error', 'actor_name',
                  'created_at', 'finished_at']
        read_only_fields = fields

    def get_proposal_count(self, obj):
        return obj.source_proposals.count()


class TiangongSourceSerializer(serializers.ModelSerializer):
    """来源库：域名唯一，入口地址一行一个"""

    created_by_name = serializers.CharField(source='created_by.display_name', read_only=True)
    last_status_display = serializers.CharField(source='get_last_status_display',
                                                read_only=True)

    class Meta:
        model = TiangongSource
        fields = [
            'id', 'name', 'domain', 'entry_urls', 'topic_tags', 'description',
            'crawl_rules', 'note', 'enabled', 'priority',
            'last_checked_at', 'last_status', 'last_status_display', 'last_error',
            'created_by_name', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'last_checked_at', 'last_status', 'last_status_display', 'last_error',
            'created_by_name', 'created_at', 'updated_at',
        ]

    def validate_domain(self, value):
        raw = (value or '').strip().lower()
        if '://' in raw:
            raw = net.host_of(raw)
        raw = raw.split('/', 1)[0].split('?', 1)[0].rstrip('.')
        if not raw or '.' not in raw:
            raise serializers.ValidationError('请填写有效域名，如 example.gov.cn')
        return raw[:255]

    def validate_entry_urls(self, value):
        lines = []
        for line in (value or '').splitlines():
            line = line.strip()
            if not line:
                continue
            if not line.startswith(('http://', 'https://')):
                raise serializers.ValidationError('入口地址必须以 http:// 或 https:// 开头')
            lines.append(line[:500])
        return '\n'.join(lines[:10])


class TiangongSourceProposalSerializer(serializers.ModelSerializer):
    """候选来源：搜索层只产候选，管理员批准后才进入来源库"""

    source_type_display = serializers.CharField(source='get_source_type_display',
                                                read_only=True)
    discovered_by_display = serializers.CharField(source='get_discovered_by_display',
                                                  read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    reviewed_by_name = serializers.CharField(source='reviewed_by.display_name',
                                             read_only=True)
    knowledge_base_name = serializers.CharField(source='knowledge_base.name',
                                                read_only=True)

    class Meta:
        model = TiangongSourceProposal
        fields = [
            'id', 'run', 'knowledge_base', 'knowledge_base_name',
            'source_type', 'source_type_display',
            'discovered_by', 'discovered_by_display',
            'domain', 'url', 'title', 'snippet', 'topics', 'score',
            'status', 'status_display',
            'reviewed_by', 'reviewed_by_name', 'reviewed_at', 'reject_reason',
            'created_at',
        ]
        read_only_fields = fields
