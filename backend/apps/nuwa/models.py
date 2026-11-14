"""女娲模式：市场目录、向导会话与外部条目导入申请

三块数据各自独立：
- MarketSource / MarketItem / MarketSyncRun：外部市场（MCP registry、技能仓库）的
  本地索引。在线检索命中即写这里，离线时只读这里，两者共用同一套表。
- NuwaSession：向导会话，按步骤持久化，刷新可恢复。
- NuwaImportRequest：外部 MCP / 技能落库申请，复用「提交人不可自审」的单级审批范式。

安全边界不在模型层：命令白名单、域名校验、正文扫描都在 services 里做，
模型只保存已归一化的结果。
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

MARKET_KIND_MCP = 'mcp'
MARKET_KIND_SKILL = 'skill'
MARKET_KIND_CHOICES = [
    (MARKET_KIND_MCP, 'MCP 工具来源'),
    (MARKET_KIND_SKILL, '技能'),
]


class MarketSource(models.Model):
    """外部市场源（管理员维护；内置两条：官方 MCP registry、anthropics/skills）"""

    KIND_CHOICES = [
        ('mcp_registry', 'MCP 官方目录'),
        ('skill_repo', '技能仓库'),
        ('local_catalog', '本地目录包'),
    ]

    name = models.CharField('名称', max_length=64, unique=True)
    kind = models.CharField('类型', max_length=16, choices=KIND_CHOICES)
    base_url = models.CharField('接口地址', max_length=300, blank=True, default='')
    repo = models.CharField('仓库', max_length=200, blank=True, default='',
                            help_text='技能源用，形如 anthropics/skills')
    branch = models.CharField('分支', max_length=64, blank=True, default='main')
    auth_env = models.CharField('凭据环境变量名', max_length=64, blank=True, default='',
                                help_text='如 GITHUB_TOKEN；为空则匿名访问（有速率限制）')
    enabled = models.BooleanField('已启用', default=True)
    is_builtin = models.BooleanField('内置源', default=False)
    sync_interval_hours = models.PositiveIntegerField('同步间隔（小时）', default=168)
    last_sync_at = models.DateTimeField('最后同步时间', null=True, blank=True)
    last_sync_status = models.CharField('最后同步状态', max_length=16, blank=True, default='')
    last_error = models.CharField('最后错误', max_length=300, blank=True, default='')
    item_count = models.IntegerField('条目数', default=0)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'lasso_nuwa_market_sources'
        verbose_name = '女娲市场源'
        verbose_name_plural = verbose_name
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def stale(self) -> bool:
        if not self.last_sync_at:
            return True
        hours = (timezone.now() - self.last_sync_at).total_seconds() / 3600
        return hours > max(self.sync_interval_hours, 1)


class MarketItem(models.Model):
    """外部条目在本地的一份副本（在线命中也落这里，离线只读这里）"""

    TRUST_CHOICES = [
        ('trusted', '可信发布方'),
        ('unverified', '未验证'),
        ('blocked', '已拦截'),
    ]
    STATUS_CHOICES = [
        ('active', '可用'),
        ('stale', '已过期'),
        ('blocked', '已拦截'),
    ]

    source = models.ForeignKey(MarketSource, on_delete=models.CASCADE, related_name='items',
                               verbose_name='来源')
    kind = models.CharField('条目类型', max_length=16, choices=MARKET_KIND_CHOICES)
    external_id = models.CharField('外部标识', max_length=200)
    version = models.CharField('版本', max_length=64, blank=True, default='')
    title = models.CharField('名称', max_length=200)
    summary = models.CharField('能力摘要', max_length=500, blank=True, default='')
    publisher = models.CharField('发布方', max_length=120, blank=True, default='')
    homepage = models.CharField('主页', max_length=300, blank=True, default='')
    repository_url = models.CharField('仓库地址', max_length=300, blank=True, default='')
    license = models.CharField('许可证', max_length=100, blank=True, default='')
    trust_level = models.CharField('可信度', max_length=16, choices=TRUST_CHOICES,
                                   default='unverified')
    transport = models.CharField('连接类型', max_length=16, blank=True, default='')
    install_payload = models.JSONField('安装参数', default=dict, blank=True)
    egress_host = models.CharField('目标主机', max_length=200, blank=True, default='')
    requires_egress = models.BooleanField('需要出网', default=True)
    has_scripts = models.BooleanField(
        '含脚本', default=False,
        help_text='MCP 为本地 stdio 包；技能仓库带 scripts/ 目录或正文声明脚本')
    capabilities = models.JSONField('能力标签', default=list, blank=True)
    embedding = models.JSONField('摘要向量', default=list, blank=True)
    content_hash = models.CharField('内容指纹', max_length=64, blank=True, default='')
    raw = models.JSONField('原始条目', default=dict, blank=True)
    status = models.CharField('状态', max_length=16, choices=STATUS_CHOICES, default='active')
    fetched_at = models.DateTimeField('抓取时间', default=timezone.now)
    created_at = models.DateTimeField('首次入库', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'lasso_nuwa_market_items'
        verbose_name = '女娲市场条目'
        verbose_name_plural = verbose_name
        ordering = ['title']
        constraints = [
            models.UniqueConstraint(fields=['source', 'kind', 'external_id', 'version'],
                                    name='uniq_nuwa_market_item'),
        ]
        indexes = [
            models.Index(fields=['kind', 'status'], name='idx_nuwa_item_kind_status'),
            models.Index(fields=['external_id'], name='idx_nuwa_item_external'),
        ]

    def __str__(self):
        return self.title

    @property
    def search_text(self) -> str:
        return ' '.join(filter(None, [self.title, self.summary,
                                      ' '.join(self.capabilities or [])]))


class MarketSyncRun(models.Model):
    """一次同步/导入批次（保留失败原因，便于运维判断网络状况）"""

    STATUS_CHOICES = [
        ('running', '进行中'),
        ('ok', '成功'),
        ('partial', '部分成功'),
        ('failed', '失败'),
    ]

    source = models.ForeignKey(MarketSource, on_delete=models.CASCADE, related_name='runs',
                               verbose_name='来源')
    mode = models.CharField('方式', max_length=16, default='online',
                            help_text='online=在线同步，import=导入离线包')
    status = models.CharField('状态', max_length=16, choices=STATUS_CHOICES, default='running')
    added = models.IntegerField('新增', default=0)
    updated = models.IntegerField('更新', default=0)
    skipped = models.IntegerField('跳过', default=0)
    failed = models.IntegerField('失败', default=0)
    error = models.CharField('错误', max_length=300, blank=True, default='')
    cursor = models.CharField('分页游标', max_length=200, blank=True, default='')
    started_at = models.DateTimeField('开始时间', auto_now_add=True)
    finished_at = models.DateTimeField('结束时间', null=True, blank=True)

    class Meta:
        db_table = 'lasso_nuwa_market_sync_runs'
        verbose_name = '女娲同步批次'
        verbose_name_plural = verbose_name
        ordering = ['-started_at']

    def __str__(self):
        return f'{self.source_id}:{self.status}'


class NuwaSession(models.Model):
    """向导会话：每一步都落库，刷新后按 step 恢复"""

    STATUS_CHOICES = [
        ('drafting', '描述与骨架'),
        ('matching', '挑选工具与技能'),
        ('knowledge', '挑选资料'),
        ('ready', '待确认'),
        ('created', '已建草稿'),
        ('abandoned', '已放弃'),
    ]

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name='nuwa_sessions', verbose_name='创建人')
    brief = models.TextField('需求描述')
    brief_hash = models.CharField('描述指纹', max_length=64, blank=True, default='')
    audience = models.CharField('使用对象', max_length=120, blank=True, default='')
    department = models.CharField('面向部门', max_length=100, blank=True, default='')
    step = models.PositiveSmallIntegerField('当前步骤', default=1)
    status = models.CharField('状态', max_length=16, choices=STATUS_CHOICES, default='drafting')
    generated = models.JSONField('生成的骨架', default=dict, blank=True)
    generation_meta = models.JSONField('生成元信息', default=dict, blank=True)
    edited_fields = models.JSONField('已人工修改的字段', default=list, blank=True)
    candidates = models.JSONField('市场候选', default=dict, blank=True)
    selections = models.JSONField('勾选结果', default=dict, blank=True)
    knowledge_plan = models.JSONField('资料检索计划', default=dict, blank=True)
    knowledge_items = models.JSONField('资料候选', default=list, blank=True)
    agent = models.ForeignKey('agenthub.AgentProfile', on_delete=models.SET_NULL, null=True,
                              blank=True, related_name='nuwa_sessions', verbose_name='产出草稿')
    error = models.TextField('最近错误', blank=True, default='')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    expires_at = models.DateTimeField('过期时间', null=True, blank=True)

    class Meta:
        db_table = 'lasso_nuwa_sessions'
        verbose_name = '女娲会话'
        verbose_name_plural = verbose_name
        ordering = ['-updated_at']

    def __str__(self):
        return f'女娲会话#{self.pk}'

    @property
    def expired(self) -> bool:
        return bool(self.expires_at and self.expires_at < timezone.now())


class NuwaImportRequest(models.Model):
    """外部 MCP / 技能落库申请：提交不生效，管理员且非提交人审批后才落库"""

    KIND_CHOICES = [
        ('mcp_server', 'MCP 工具来源'),
        ('skill', '技能'),
    ]
    STATUS_CHOICES = [
        ('pending', '待审批'),
        ('approved', '已通过'),
        ('rejected', '已驳回'),
    ]

    kind = models.CharField('对象类型', max_length=16, choices=KIND_CHOICES)
    market_item = models.ForeignKey(MarketItem, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='import_requests', verbose_name='市场条目')
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='nuwa_imports_requested',
                                     verbose_name='提交人')
    draft_payload = models.JSONField('归一化后的落库参数', default=dict, blank=True)
    risk_flags = models.JSONField('风险提示', default=list, blank=True)
    status = models.CharField('状态', max_length=16, choices=STATUS_CHOICES,
                              default='pending', db_index=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='nuwa_imports_reviewed',
                                    verbose_name='审批人')
    reviewed_at = models.DateTimeField('审批时间', null=True, blank=True)
    review_note = models.CharField('审批意见', max_length=200, blank=True, default='')
    result = models.JSONField('处理结果', default=dict, blank=True)
    created_at = models.DateTimeField('提交时间', auto_now_add=True)

    class Meta:
        db_table = 'lasso_nuwa_import_requests'
        verbose_name = '女娲导入申请'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_kind_display()}（{self.get_status_display()}）'
