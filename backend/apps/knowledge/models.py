from django.db import models
from django.conf import settings


def visible_kb_query(user):
    """知识库可见性过滤 Q 对象：公开 + 自己创建 + 本部门共享

    管理员（is_staff）无限制，由调用方短路；department 为空的用户
    不参与部门共享（避免空串互相可见）。
    """
    q = models.Q(scope='public') | models.Q(created_by=user)
    if user.department:
        q |= models.Q(scope='department', department=user.department)
    return q


def can_manage_kb(user, kb) -> bool:
    """知识库写权限：属主或平台管理员

    可见范围（公开 / 本部门 / 私有）只决定能不能读；写操作按 PRD「创建/修改自己的
    知识库、上传资料」收口到属主。部门共享库对同部门成员是只读协作（可检索、可预览），
    不因为能看见就能改别人的库或删别人的文档。
    """
    return bool(user.is_staff or kb.created_by_id == user.id)


class KnowledgeBase(models.Model):
    """知识库"""

    SCOPE_CHOICES = [
        ('public', '公开'),
        ('department', '本部门'),
        ('private', '私有'),
    ]

    name = models.CharField('名称', max_length=200)
    description = models.TextField('描述', blank=True, default='')
    icon = models.CharField('图标', max_length=50, blank=True, default='Database')
    color = models.CharField('主题色', max_length=20, blank=True, default='#6366f1')
    is_public = models.BooleanField('是否公开', default=True)
    # 二代权限：scope 为权威字段；is_public 保留兼容（写请求未带 scope 时按其映射）
    scope = models.CharField('可见范围', max_length=20, choices=SCOPE_CHOICES, default='public')
    department = models.CharField('所属部门', max_length=100, blank=True, default='')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        verbose_name='创建者'
    )
    document_count = models.IntegerField('文档数量', default=0)
    chunk_count = models.IntegerField('切片数量', default=0)
    size_bytes = models.BigIntegerField('占用空间(字节)', default=0)

    # 天工模式：建库时开启，之后可改；开启时由后台从公网与模型采集知识
    tiangong_enabled = models.BooleanField('天工模式', default=False)
    tiangong_topics = models.TextField('采集主题', blank=True, default='')
    tiangong_seeds = models.TextField('采集入口', blank=True, default='',
                                      help_text='一行一个 URL；留空则只做模型侧采集')
    tiangong_status = models.CharField('天工状态', max_length=20, blank=True, default='idle',
                                       choices=[('idle', '未采集'), ('queued', '排队中'),
                                                ('running', '采集中'), ('done', '已完成'),
                                                ('failed', '采集失败')])
    tiangong_last_run_at = models.DateTimeField('最近采集时间', null=True, blank=True)
    tiangong_last_error = models.CharField('最近采集错误', max_length=300, blank=True, default='')

    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'ask_knowledge_bases'
        verbose_name = '知识库'
        verbose_name_plural = verbose_name
        ordering = ['-updated_at']

    def __str__(self):
        return self.name


class Document(models.Model):
    """文档"""

    STATUS_CHOICES = [
        ('pending', '待处理'),
        ('processing', '处理中'),
        ('completed', '已完成'),
        ('failed', '失败'),
    ]

    knowledge_base = models.ForeignKey(
        KnowledgeBase,
        on_delete=models.CASCADE,
        related_name='documents',
        verbose_name='所属知识库'
    )
    title = models.CharField('标题', max_length=500)
    file_name = models.CharField('文件名', max_length=500, blank=True, default='')
    file_type = models.CharField('文件类型', max_length=20, blank=True, default='')
    file_size = models.BigIntegerField('文件大小(字节)', default=0)
    file_path = models.FileField('文件路径', upload_to='knowledge/%Y/%m/')
    status = models.CharField('处理状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    error_msg = models.TextField('错误信息', blank=True, default='')
    chunk_count = models.IntegerField('切片数量', default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        verbose_name='上传者'
    )
    # 女娲模式抓取入库的来源信息（上传文件时留空，不影响既有逻辑）
    source_url = models.CharField('来源地址', max_length=500, blank=True, default='',
                                  db_index=True)
    source_title = models.CharField('来源标题', max_length=500, blank=True, default='')
    source_site = models.CharField('来源站点', max_length=200, blank=True, default='')
    source_license = models.CharField('来源许可证', max_length=100, blank=True, default='')
    fetched_at = models.DateTimeField('抓取时间', null=True, blank=True)
    content_sha256 = models.CharField('内容指纹', max_length=64, blank=True, default='')
    # 来源类型：上传 / 天工公网抓取 / 天工模型生成
    source_type = models.CharField('来源类型', max_length=10, blank=True, default='upload',
                                   choices=[('upload', '上传'), ('web', '外部网页'),
                                            ('model', '模型生成')])
    source_model = models.CharField('生成模型', max_length=100, blank=True, default='')
    # OCR 逐页报告：哪些页有文字层、哪些页走了 OCR、哪些页未解析及原因。
    # 结构见 apps/knowledge/services.py::extract_pages，空 dict 表示未走过提取链路。
    extraction_report = models.JSONField('提取报告', default=dict, blank=True)

    created_at = models.DateTimeField('上传时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'ask_knowledge_documents'
        verbose_name = '文档'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        constraints = [
            # 同一知识库内同一来源只入库一次；上传文件（source_url 为空）不受约束
            models.UniqueConstraint(fields=['knowledge_base', 'source_url'],
                                    condition=~models.Q(source_url=''),
                                    name='uniq_kb_document_source_url'),
        ]

    def __str__(self):
        return self.title


class DocumentChunk(models.Model):
    """文档分块（含嵌入向量）"""

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name='chunks',
        verbose_name='所属文档'
    )
    chunk_index = models.IntegerField('分块序号')
    content = models.TextField('分块内容')
    embedding = models.JSONField('嵌入向量', default=list)
    # 禁检标记：False 时检索跳过（软删除，重建索引时同样跳过）
    is_active = models.BooleanField('可检索', default=True)
    # 切片级元数据：{"page": 3, "ocr": true, "ocr_engine": "tesseract"}；
    # 页码/OCR 只放这里，不写进 content，避免污染正文关键词与向量
    meta = models.JSONField('元数据', default=dict, blank=True)

    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'ask_knowledge_document_chunks'
        verbose_name = '文档分块'
        verbose_name_plural = verbose_name
        ordering = ['chunk_index']

    def __str__(self):
        return f'{self.document.title} #{self.chunk_index}'


class TiangongSource(models.Model):
    """天工来源库（信息源白名单）

    平台级维护：管理员批准候选来源或手工录入后，L4 只从启用来源抓正文。
    来源库为空时只跑知识库的临时采集入口（tiangong_seeds）。
    """

    STATUS_CHOICES = [
        ('untested', '未检查'),
        ('ok', '可达'),
        ('failed', '不可达'),
    ]

    name = models.CharField('名称', max_length=200)
    domain = models.CharField('域名', max_length=255, unique=True,
                              help_text='不含协议与路径，如 example.gov.cn')
    entry_urls = models.TextField('入口地址', blank=True, default='',
                                  help_text='一行一个 URL；留空则从 https://域名/ 开始')
    topic_tags = models.TextField('主题标签', blank=True, default='',
                                  help_text='逗号/顿号分隔，用于主题匹配加分')
    description = models.CharField('说明', max_length=300, blank=True, default='')
    crawl_rules = models.JSONField('抓取规则', default=dict, blank=True,
                                   help_text='可覆盖 max_pages / max_depth / delay')
    note = models.CharField('备注', max_length=255, blank=True, default='')
    enabled = models.BooleanField('启用', default=True)
    # 越小越先抓；同优先级按名称排序，保证单轮抓取顺序可预期
    priority = models.PositiveIntegerField('优先级', default=100)
    last_checked_at = models.DateTimeField('最近检查时间', null=True, blank=True)
    last_status = models.CharField('最近检查结果', max_length=16,
                                   choices=STATUS_CHOICES, default='untested')
    last_error = models.CharField('最近检查错误', max_length=300, blank=True, default='')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, verbose_name='创建者')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'ask_knowledge_tiangong_sources'
        verbose_name = '天工来源库'
        verbose_name_plural = verbose_name
        ordering = ['priority', 'name']

    def __str__(self):
        return f'{self.name}（{self.domain}）'


class TiangongSourceProposal(models.Model):
    """候选来源提案：搜索层只产候选，管理员批准后才进入来源库并允许抓正文"""

    SOURCE_TYPE_CHOICES = [
        ('government', '政府机关'),
        ('university', '院校/学术'),
        ('media', '媒体'),
        ('enterprise', '企业'),
        ('organization', '机构/协会'),
        ('other', '其他'),
    ]
    DISCOVERED_BY_CHOICES = [
        ('mcp', 'L1 MCP 搜索'),
        ('searxng', 'L2 SearXNG'),
        ('bing', 'L3 Bing 保底'),
        ('model', '模型建议'),
        ('seeds', '采集入口'),
    ]
    STATUS_CHOICES = [
        ('pending', '待审批'),
        ('approved', '已批准'),
        ('rejected', '已拒绝'),
    ]

    run = models.ForeignKey('TiangongRun', on_delete=models.SET_NULL, null=True, blank=True,
                            related_name='source_proposals', verbose_name='采集记录')
    knowledge_base = models.ForeignKey('KnowledgeBase', on_delete=models.SET_NULL,
                                       null=True, blank=True,
                                       related_name='tiangong_proposals',
                                       verbose_name='来源知识库')
    source_type = models.CharField('来源类型', max_length=20,
                                   choices=SOURCE_TYPE_CHOICES, default='other')
    discovered_by = models.CharField('发现方式', max_length=20,
                                     choices=DISCOVERED_BY_CHOICES, default='searxng')
    domain = models.CharField('域名', max_length=255, db_index=True)
    url = models.CharField('地址', max_length=500, db_index=True)
    title = models.CharField('标题', max_length=500, blank=True, default='')
    snippet = models.TextField('摘要', blank=True, default='')
    topics = models.TextField('主题词', blank=True, default='')
    score = models.FloatField('分数', default=0)
    status = models.CharField('状态', max_length=16, choices=STATUS_CHOICES,
                              default='pending', db_index=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, verbose_name='审批人')
    reviewed_at = models.DateTimeField('审批时间', null=True, blank=True)
    reject_reason = models.CharField('拒绝原因', max_length=300, blank=True, default='')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'ask_knowledge_tiangong_source_proposals'
        verbose_name = '天工候选来源'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at'],
                         name='idx_tg_proposal_status_created'),
        ]

    def __str__(self):
        return f'{self.title or self.url}（{self.get_status_display()}）'


class TiangongSearchBinding(models.Model):
    """L1 MCP 搜索绑定：把某个已接入 MCP 工具映射成天工搜索主路

    不硬编码厂商：管理员从扫描候选里确认工具后，配置 query/limit 入参与结果字段映射。
    凭据仍由 MCPServer.secrets_enc 保存，绑定表不落任何明文密钥；
    工具或来源被停用时，绑定自动停用（signals）。
    """

    server = models.ForeignKey('mcp.MCPServer', on_delete=models.CASCADE,
                               related_name='tiangong_search_bindings',
                               verbose_name='MCP 来源')
    tool = models.ForeignKey('mcp.MCPTool', on_delete=models.CASCADE,
                             related_name='tiangong_search_bindings',
                             verbose_name='搜索工具')
    enabled = models.BooleanField('启用', default=True)
    priority = models.PositiveIntegerField('优先级', default=100,
                                           help_text='数字越小越先调用')
    query_field = models.CharField('查询入参名', max_length=64, default='query')
    limit_field = models.CharField('数量入参名', max_length=64, blank=True, default='',
                                   help_text='留空表示该工具不支持数量参数')
    default_limit = models.PositiveSmallIntegerField('默认返回条数', default=5)
    results_path = models.CharField('结果列表路径', max_length=120, default='results',
                                    help_text='点号路径，如 results / data.items；留空表示顶层列表')
    url_field = models.CharField('URL 字段', max_length=64, default='url')
    title_field = models.CharField('标题字段', max_length=64, default='title')
    snippet_field = models.CharField('摘要字段', max_length=64, default='content')
    extra_args = models.JSONField('固定入参', default=dict, blank=True,
                                  help_text='每次调用都会带上的静态参数')
    note = models.CharField('备注', max_length=255, blank=True, default='')
    last_status = models.CharField('最近测试结果', max_length=16, blank=True, default='',
                                   choices=[('', '未测试'), ('ok', '可用'),
                                            ('failed', '失败')])
    last_error = models.CharField('最近错误', max_length=300, blank=True, default='')
    last_latency_ms = models.PositiveIntegerField('最近耗时(ms)', default=0)
    last_result_count = models.PositiveIntegerField('最近结果数', default=0)
    last_checked_at = models.DateTimeField('最近测试时间', null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, verbose_name='创建者')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'ask_knowledge_tiangong_search_bindings'
        verbose_name = '天工 MCP 搜索绑定'
        verbose_name_plural = verbose_name
        ordering = ['priority', 'id']
        constraints = [
            models.UniqueConstraint(fields=['server', 'tool'],
                                    name='uniq_tiangong_binding_per_tool'),
        ]

    def __str__(self):
        return f'{self.server.name} · {self.tool.name}'

    @property
    def unusable_reason(self) -> str:
        """绑定不可用的原因码；空串表示可用"""
        if not self.enabled:
            return 'mcp_disabled'
        if not self.server.is_active or self.server.status in ('disabled', 'egress_blocked'):
            return 'mcp_disabled'
        if self.tool.state != 'present':
            return 'mcp_tool_disabled'
        if not self.tool.enabled:
            return 'mcp_tool_disabled'
        return ''

    @property
    def usable(self) -> bool:
        return not self.unusable_reason


class TiangongRun(models.Model):
    """天工采集记录：每次采集的触发人、来源计数与降级原因（可追溯到人）"""

    STATUS_CHOICES = [
        ('queued', '排队中'),
        ('running', '采集中'),
        ('done', '已完成'),
        ('failed', '失败'),
        ('skipped', '跳过'),
    ]

    knowledge_base = models.ForeignKey(
        KnowledgeBase, on_delete=models.CASCADE, related_name='tiangong_runs',
        verbose_name='知识库')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='queued')
    trigger = models.CharField('触发方式', max_length=20, default='manual',
                               choices=[('create', '建库自动'), ('manual', '手动补充')])
    topics = models.TextField('采集主题', blank=True, default='')
    web_created = models.IntegerField('网页入库数', default=0)
    model_created = models.IntegerField('模型生成数', default=0)
    skipped = models.IntegerField('跳过数', default=0)
    # 四层发现链路的聚合记录：只记层、计数与首条原因，不逐条记 URL
    discovery_layer = models.CharField('命中的搜索层', max_length=20, blank=True, default='',
                                       choices=[('', '未启用'), ('l1_mcp', 'L1 MCP'),
                                                ('l2_searxng', 'L2 SearXNG'),
                                                ('l3_bing', 'L3 Bing'),
                                                ('base', '仅来源库')])
    fallback_chain = models.JSONField('降级链', default=list, blank=True)
    search_queries = models.PositiveIntegerField('搜索查询数', default=0)
    search_hits = models.PositiveIntegerField('搜索层命中数', default=0)
    candidates = models.PositiveIntegerField('候选数', default=0)
    proposals = models.PositiveIntegerField('新候选提案数', default=0)
    approved = models.PositiveIntegerField('本轮已批准来源数', default=0)
    pages_fetched = models.PositiveIntegerField('抓取页数', default=0)
    quality_rejected = models.PositiveIntegerField('质量过滤数', default=0)
    sources_tried = models.PositiveIntegerField('尝试来源数', default=0)
    degraded_reason = models.CharField('降级原因', max_length=200, blank=True, default='')
    error = models.CharField('错误', max_length=300, blank=True, default='')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                              null=True, verbose_name='触发人')
    created_at = models.DateTimeField('开始时间', auto_now_add=True)
    finished_at = models.DateTimeField('结束时间', null=True, blank=True)

    class Meta:
        db_table = 'ask_knowledge_tiangong_runs'
        verbose_name = '天工采集记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f'天工采集 #{self.pk}（{self.get_status_display()}）'
