from rest_framework import serializers
from .models import Conversation, Message


class ConversationSerializer(serializers.ModelSerializer):
    """会话序列化器"""

    agent_name = serializers.CharField(source='agent.name', read_only=True, default='')
    # D1：已停用的智能体历史会话只读，前端据此把输入区换成下线提示
    agent_status = serializers.CharField(source='agent.status', read_only=True, default='')
    # P1-c：切换来源会话只用于展示（列表可标注「由 xx 切换而来」）
    switched_from_title = serializers.CharField(
        source='switched_from.title', read_only=True, default='')
    # P1-c：已被切换承接的旧会话只读；列表已注解，单条场景回退为一次 EXISTS
    is_readonly = serializers.SerializerMethodField()

    def get_is_readonly(self, obj):
        annotated = getattr(obj, 'switched_away', None)
        if annotated is not None:
            return bool(annotated)
        return obj.switch_successors.exists()

    class Meta:
        model = Conversation
        fields = [
            'id', 'title', 'type', 'resource_id', 'knowledge_base_ids',
            'agent', 'agent_name', 'agent_status', 'use_preferences',
            'switched_from', 'switched_from_title', 'is_readonly',
            'message_count', 'last_message_at',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'knowledge_base_ids', 'switched_from', 'message_count',
            'last_message_at', 'created_at', 'updated_at',
        ]


class MessageSerializer(serializers.ModelSerializer):
    """消息序列化器"""

    class Meta:
        model = Message
        fields = ['id', 'role', 'content', 'meta', 'attachments',
                  'input_tokens', 'output_tokens', 'created_at']
        read_only_fields = ['id', 'created_at']


class ChatRequestSerializer(serializers.Serializer):
    """对话请求"""
    conversation_id = serializers.IntegerField(required=False, default=None)
    message = serializers.CharField(required=True, max_length=8000)
    type = serializers.ChoiceField(
        choices=['chat', 'knowledge'],
        default='chat',
        required=False
    )
    resource_id = serializers.IntegerField(required=False, default=None)
    # D1：本次对话使用的智能体定义；不传则沿用会话已绑定的智能体
    agent_id = serializers.IntegerField(required=False, default=None, min_value=1)
    # 本轮对话勾选的知识库（可多个）；不传则沿用会话已有选择
    knowledge_base_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        default=None,
        allow_null=True,
        max_length=8,
    )
    stream = serializers.BooleanField(default=False)
    temperature = serializers.FloatField(required=False, default=None)
    # D5：附件为上传接口原样返回的字典（size 是整数、truncated 是布尔）。
    # 这里不再限定子字段类型——CharField 会把 bool 直接判为非法输入，
    # 导致前端把上传响应原样带回时 400，size 也会被强转成字符串。
    attachments = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        default=list,
    )


class ChatRegenerateSerializer(serializers.Serializer):
    """D2 重新生成：对会话内最新一版助手答案重出一版，旧版留档供对比"""

    conversation_id = serializers.IntegerField(required=True)
    # 可选：前端点了哪一版就传哪一版，与服务端最新一版不一致时报错，绝不静默重生成别的消息
    message_id = serializers.IntegerField(required=False, default=None, min_value=1)
