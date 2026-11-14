from django.db import models
from django.conf import settings


class Conversation(models.Model):
    """对话会话"""

    TYPE_CHOICES = [
        ('chat', '通用对话'),
        ('knowledge', '知识库对话'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='conversations',
        verbose_name='用户'
    )
    title = models.CharField('标题', max_length=200, default='新对话')
    type = models.CharField('类型', max_length=20, choices=TYPE_CHOICES, default='chat')

    # 关联的资源 ID（知识库 ID、阅读文档 ID 等）
    resource_id = models.IntegerField('关联资源ID', null=True, blank=True)

    # 对话中勾选的知识库（可多个）：回答时跨库检索并给出引用来源
    knowledge_base_ids = models.JSONField('关联知识库ID', default=list, blank=True)

    # D1：会话绑定的智能体定义。为空表示沿用平台通用助手（历史会话不受影响），
    # 智能体删除后保留会话，故 SET_NULL 而不是 CASCADE
    agent = models.ForeignKey(
        'agenthub.AgentProfile',
        on_delete=models.SET_NULL,
        related_name='conversations',
        verbose_name='所属智能体',
        null=True,
        blank=True,
    )

    # 7.3：会话沿用开场快照。记录首轮消息时的已发布版本号，
    # 之后重发布/回滚只影响新会话，进行中的会话配置保持不变。
    agent_version = models.IntegerField(
        '开场版本号', null=True, blank=True,
        help_text='会话创建时智能体的已发布版本号；为空表示按最新版本装配')

    # P1-c：切换助手时新建会话承接，旧会话保持只读；该外键只用于展示来源，
    # 不参与绑定与权限判断。旧会话是否只读由「是否存在承接会话」推导
    switched_from = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        related_name='switch_successors',
        verbose_name='切换来源会话',
        null=True,
        blank=True,
    )

    # D7：会话级「始终允许」授权，存 AgentScope 运行期工具名；
    # 仅在该会话快照档位为 ask 且工具仍挂载时生效，不跨会话共享
    tool_grants = models.JSONField('会话工具授权', default=list, blank=True)

    # D8：本会话是否带入提问人的跨会话偏好；用户可临时关闭，只影响本会话
    use_preferences = models.BooleanField('带入跨会话偏好', default=True)

    message_count = models.IntegerField('消息数量', default=0)
    last_message_at = models.DateTimeField('最后消息时间', auto_now=True)

    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'ask_chat_conversations'
        verbose_name = '对话会话'
        verbose_name_plural = verbose_name
        ordering = ['-last_message_at']

    def __str__(self):
        return self.title


class Message(models.Model):
    """对话消息"""

    ROLE_CHOICES = [
        ('user', '用户'),
        ('assistant', '助手'),
        ('system', '系统'),
    ]

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='messages',
        verbose_name='所属会话'
    )
    role = models.CharField('角色', max_length=20, choices=ROLE_CHOICES)
    content = models.TextField('内容')

    # 真实 token 计量：取自模型服务返回的 usage（助手消息才有值）
    input_tokens = models.PositiveIntegerField('输入 token 数', default=0)
    output_tokens = models.PositiveIntegerField('输出 token 数', default=0)

    # 元数据（引用来源、token 用量等）
    meta = models.JSONField('元数据', default=dict, blank=True)

    # 临时附件（随消息一次性上下文，不入知识库/不建向量）
    # 结构：[{"name": "xxx.docx", "size": 12345, "type": "docx", "text": "提取文本"}]
    attachments = models.JSONField('附件', default=list, blank=True)

    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'ask_chat_messages'
        verbose_name = '对话消息'
        verbose_name_plural = verbose_name
        ordering = ['created_at']

    def __str__(self):
        return f'{self.role}: {self.content[:30]}'


class PendingPermission(models.Model):
    """D7 会话内逐次授权：待决请求落库，供决定接口跨线程/跨进程裁决

    等待方按 request_id 轮询本表，不再依赖进程内 Future；进程重启后
    instance_id 不再匹配，待决请求即失效（前端提示重新发起）。
    """

    STATUS_PENDING = 'pending'
    STATUS_RESOLVED = 'resolved'
    STATUS_EXPIRED = 'expired'
    STATUS_CHOICES = [
        (STATUS_PENDING, '待决定'),
        (STATUS_RESOLVED, '已决定'),
        (STATUS_EXPIRED, '已超时'),
    ]

    request_id = models.CharField('请求标识', max_length=64, unique=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='pending_permissions',
        verbose_name='发起人',
    )
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.SET_NULL,
        related_name='pending_permissions',
        verbose_name='所属会话',
        null=True,
        blank=True,
    )
    agent_id = models.IntegerField('智能体ID', null=True, blank=True)
    message_id = models.IntegerField('消息ID', null=True, blank=True)
    call_id = models.CharField('调用标识', max_length=64, blank=True, default='')
    tool_name = models.CharField('工具名', max_length=160, blank=True, default='')
    display = models.CharField('展示名', max_length=200, blank=True, default='')
    status = models.CharField(
        '状态', max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    decision = models.CharField('决定', max_length=16, blank=True, default='')
    instance_id = models.CharField('处理进程标识', max_length=64, blank=True, default='')
    expires_at = models.DateTimeField('过期时间', db_index=True)
    resolved_at = models.DateTimeField('决定时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'ask_chat_pending_permissions'
        verbose_name = '待决授权'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status'], name='ask_perm_user_status'),
        ]

    def __str__(self):
        return f'{self.display or self.tool_name} #{self.request_id[:8]}'


class ChatGeneratedFile(models.Model):
    """对话中 AI 生成的产物文件"""

    FILE_TYPE_CHOICES = [
        ('docx', 'Word'),
        ('xlsx', 'Excel'),
        ('pptx', 'PPT'),
        ('pdf', 'PDF'),
    ]

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='generated_files',
        verbose_name='所属会话',
        null=True,
        blank=True,
    )
    message = models.ForeignKey(
        Message,
        on_delete=models.SET_NULL,
        related_name='generated_files',
        verbose_name='关联消息',
        null=True,
        blank=True,
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='chat_generated_files',
        verbose_name='属主',
    )
    file = models.FileField('产物文件', upload_to='chat_generated/%Y/%m')
    file_name = models.CharField('文件名', max_length=255)
    file_type = models.CharField('文件类型', max_length=10, choices=FILE_TYPE_CHOICES)
    file_size = models.IntegerField('文件大小', default=0)
    is_expired = models.BooleanField('是否已过期', default=False)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'ask_chat_generated_files'
        verbose_name = '对话生成文件'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return self.file_name


class UserPreference(models.Model):
    """D8 跨会话偏好：用户 + 智能体范围内记住的长期要求

    两条来源：
    - user：用户在会话里点「以后都这样」并确认后保存；
    - preset：管理员在智能体上预置的默认偏好；用户关闭时只做 muted 标记，
      不物理删除，便于在个人设置里恢复。

    严格按用户隔离：查询一律带 user 条件，管理员与部门主管都没有查看他人偏好的入口。
    """

    SOURCE_USER = 'user'
    SOURCE_PRESET = 'preset'
    SOURCE_CHOICES = [
        (SOURCE_USER, '我确认的'),
        (SOURCE_PRESET, '智能体预置'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='agent_preferences',
        verbose_name='用户',
    )
    agent = models.ForeignKey(
        'agenthub.AgentProfile',
        on_delete=models.CASCADE,
        related_name='user_preferences',
        verbose_name='智能体',
    )
    content = models.CharField('偏好内容', max_length=200)
    source = models.CharField('来源', max_length=10, choices=SOURCE_CHOICES, default=SOURCE_USER)
    # 仅对 preset 有意义：用户关闭预置项后置 True，可恢复
    muted = models.BooleanField('已停用', default=False)
    last_used_at = models.DateTimeField('最近一次带入会话', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'ask_chat_user_preferences'
        verbose_name = '跨会话偏好'
        verbose_name_plural = verbose_name
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['user', 'agent', 'muted'], name='ask_pref_user_agent'),
        ]

    def __str__(self):
        return f'{self.user_id}/{self.agent_id}: {self.content[:30]}'


# 单条工具返回全文的落库上限（256KB）：超限截存并置 storage_truncated，
# 仍记录原始字数与指纹，避免个别超大返回把单行撑到 MB 级（PRD 9.2 #4）
TOOL_BODY_MAX_CHARS = 256 * 1024


class ToolCallBody(models.Model):
    """工具调用返回全文（PRD 9.2 #4：全文入库、保留 90 天、查看留痕）

    与审计留痕一一对应：审计表只放摘要（列表页轻量），全文独立成表，
    使「全文 90 天」与「审计留痕长期保留」可以分开处理。
    """

    audit = models.OneToOneField(
        'users.AuditLog',
        on_delete=models.CASCADE,
        related_name='tool_body',
        verbose_name='审计留痕',
    )
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='tool_bodies',
        verbose_name='所属会话',
        null=True,
        blank=True,
        help_text='试聊不产生会话，此处为空',
    )
    call_id = models.CharField('调用标识', max_length=64, blank=True, default='', db_index=True)
    tool = models.CharField('工具名', max_length=160, blank=True, default='')
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tool_call_bodies',
        verbose_name='操作人',
    )
    state = models.CharField('调用状态', max_length=16, blank=True, default='')
    egress = models.BooleanField('走外网出口', default=False)
    result = models.TextField('返回全文', blank=True, default='')
    result_chars = models.IntegerField('原始字数', default=0)
    result_sha256 = models.CharField('原文指纹', max_length=64, blank=True, default='')
    storage_truncated = models.BooleanField('超上限截存', default=False)
    created_at = models.DateTimeField('创建时间', auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'ask_tool_call_bodies'
        verbose_name = '工具调用全文'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['conversation', 'created_at'],
                         name='ask_tool_body_conv_time'),
        ]

    def __str__(self):
        return f'{self.tool or "工具"} #{self.call_id}'
