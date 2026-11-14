"""智能体定义与版本（PRD 第 6 章 A 组）

AgentProfile = 一份装配：模型 + 提示词 + 说话方式档 + 查询次数档 + 工具来源（逐个勾选工具）
+ 技能 + 知识库 + 平台内置能力 + 授权档位 + 可见范围。
运行时装配入口在 backend/apps/agents/core/registry.py::create_agent，
本模块提供它读取的定义。

状态机（PRD 4.3）：草稿 / 已发布 / 已停用 / 已归档。
A4 要求"来源可展开逐个勾选工具"，因此 AgentMCPServer 用显式中间模型保存
per-agent 的工具勾选明细（区别于 MCPServer.enabled_tools 的来源级启停）。
A8 每次成功发布递增版本号并归档当次快照，P0 历史只读。
"""
from django.conf import settings
from django.core.validators import MinLengthValidator
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from backend.apps.common.choices import Visibility


class AgentStatus(models.TextChoices):
    DRAFT = 'draft', '草稿'
    PUBLISHED = 'published', '已发布'
    DISABLED = 'disabled', '已停用'
    ARCHIVED = 'archived', '已归档'


class SpeechTier(models.TextChoices):
    """A3 说话方式三档预设，数值映射由平台维护、现场不可调"""

    LOW = 'low', '照本宣科，少发挥'
    MID = 'mid', '平衡'
    HIGH = 'high', '多发挥，适合初稿与文案'


class RetrievalTier(models.TextChoices):
    """A3 查询次数上限三档预设（一次回答内"调工具→看结果→再决定"的最大往返）"""

    FEW = 'few', '一次查不到就直说'
    STANDARD = 'standard', '允许少量补查'
    MANY = 'many', '允许连续多次查询后汇总'


class KnowledgeMode(models.TextChoices):
    """E3 检索调用策略：默认按需查，合规类智能体可改为每轮必查"""

    ON_DEMAND = 'on_demand', '按需查（智能调用）'
    ALWAYS = 'always', '每轮必查'


class PermissionTier(models.TextChoices):
    """A5 授权档位：必须显式选定，缺省推荐档为白名单

    映射到 AgentScope PermissionMode（可行性报告 4.1，已在 2.0.9 实测）：
      auto      -> BYPASS
      whitelist -> DEFAULT + allow_rules(mcp__{slug}__{tool})
      ask       -> DEFAULT（P0 界面置灰不可选，未实装授权入口会静默挂起）
    """

    ASK = 'ask', '逐次询问'
    WHITELIST = 'whitelist', '白名单'
    AUTO = 'auto', '自动放行'


# A3 三档预设 → 运行时数值（平台内置，不暴露给客户现场）
SPEECH_PRESETS = {
    SpeechTier.LOW: {'temperature': 0.1, 'top_p': 0.8},
    SpeechTier.MID: {'temperature': 0.4, 'top_p': 0.9},
    SpeechTier.HIGH: {'temperature': 0.8, 'top_p': 0.95},
}
RETRIEVAL_PRESETS = {
    RetrievalTier.FEW: 1,
    RetrievalTier.STANDARD: 3,
    RetrievalTier.MANY: 8,
}

# A4 ④ 平台内置能力候选
BUILTIN_CAPABILITIES = [
    ('generate_file', '文件生成'),
    ('knowledge_search', '资料检索'),
]


class AgentProfile(models.Model):
    """智能体定义"""

    name = models.CharField('名称', max_length=30, unique=True,
                            validators=[MinLengthValidator(2)],
                            help_text='2–30 字，企业内唯一（A2）')
    slug = models.CharField('标识', max_length=64, unique=True, editable=False,
                            help_text='小写无空格，创建后不可改；历史会话与引用依赖其稳定')
    description = models.CharField('用途描述', max_length=200, blank=True, default='')
    job_tags = models.JSONField('岗位标签', default=list, blank=True,
                                 help_text='对话页分组用，可多选，如行政/人事/财务/知识管理')
    icon = models.CharField('图标', max_length=64, blank=True, default='',
                            help_text='预置图标库标识，不支持上传外部图片')

    # ── A3 模型与说话方式 ──
    model_name = models.CharField('模型', max_length=128, blank=True, default='',
                                  help_text='系统设置中"已验证可用"的模型名')
    speech_tier = models.CharField('说话方式', max_length=16, choices=SpeechTier.choices,
                                   default=SpeechTier.MID)
    retrieval_tier = models.CharField('查询次数上限', max_length=16,
                                      choices=RetrievalTier.choices, default=RetrievalTier.STANDARD)
    knowledge_mode = models.CharField(
        '检索调用策略', max_length=16, choices=KnowledgeMode.choices,
        default=KnowledgeMode.ON_DEMAND,
        help_text='E3：按需查由模型判断本轮是否检索；每轮必查先检索再作答')
    followup_rewrite = models.BooleanField(
        '追问补全', default=True,
        help_text='E3：把省略式追问补成完整问题后再检索')
    enable_thinking = models.BooleanField('开启思考过程', default=False)
    prompt = models.TextField('提示词', blank=True, default='',
                              help_text='支持占位符 {current_date} {user_name} {department}')
    welcome_message = models.CharField(
        '欢迎语', max_length=200, blank=True, default='',
        help_text='对话页新会话的开场白；留空则用平台默认文案（女娲模式会生成一条）')

    # ── D8 跨会话偏好 ──
    default_preferences = models.JSONField(
        '默认偏好', default=list, blank=True,
        help_text='管理员预置、对该智能体所有使用者默认生效；用户可自行关闭，关闭只影响本人')

    # ── A4 能力挂载 ──
    mcp_servers = models.ManyToManyField(
        'mcp.MCPServer', through='AgentMCPServer', related_name='agents',
        verbose_name='工具来源', blank=True,
    )
    skills = models.ManyToManyField(
        'skills.Skill', through='AgentSkill', related_name='agents',
        verbose_name='技能', blank=True,
    )
    knowledge_base_ids = models.JSONField('知识库', default=list, blank=True)
    builtin_capabilities = models.JSONField('平台内置能力', default=list, blank=True,
                                             help_text='取值见 BUILTIN_CAPABILITIES')

    # ── A5 授权档位 ──
    permission_tier = models.CharField('授权档位', max_length=16, choices=PermissionTier.choices,
                                       default=PermissionTier.WHITELIST)
    allow_tool_names = models.JSONField('白名单工具', default=list, blank=True,
                                        help_text='档位为白名单时放行的工具全称')

    # ── A6 可见范围 ──
    visibility = models.CharField('可见范围', max_length=16, choices=Visibility.choices,
                                  default=Visibility.PRIVATE)
    allowed_departments = models.ManyToManyField(
        'users.Department', blank=True, related_name='+', verbose_name='可见部门',
    )
    include_sub_departments = models.BooleanField('含子部门', default=False,
                                                  help_text='A6：勾选后所选部门的子部门同样可见')

    status = models.CharField('状态', max_length=16, choices=AgentStatus.choices,
                              default=AgentStatus.DRAFT)
    current_version = models.IntegerField('当前版本号', default=0,
                                          help_text='每次成功发布 +1，历史版本只读（A8）')

    is_builtin = models.BooleanField('内置智能体', default=False,
                                     help_text='出厂自带，不可删除')
    is_customized = models.BooleanField('已改过内置配置', default=False,
                                        help_text='内置智能体被改动后置本标记，用于"恢复默认"')

    owner_department = models.ForeignKey(
        'users.Department', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='agents_owned', verbose_name='归属部门',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='agents_created', verbose_name='创建人',
    )
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='agents_published', verbose_name='最后发布人',
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='agents_updated', verbose_name='最后修改人',
        help_text='A1 列表列"最近修改时间与修改人"，任何配置变更与挂载变更都会刷新',
    )
    published_at = models.DateTimeField('最后发布时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'lasso_agent_profiles'
        verbose_name = '智能体'
        verbose_name_plural = verbose_name
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or 'agent'
            slug, n = base, 2
            while AgentProfile.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f'{base}-{n}'
                n += 1
            self.slug = slug
        super().save(*args, **kwargs)

    # ─── 运行时取值 ───

    @property
    def speech_params(self) -> dict:
        return SPEECH_PRESETS.get(self.speech_tier, SPEECH_PRESETS[SpeechTier.MID])

    @property
    def max_tool_rounds(self) -> int:
        return RETRIEVAL_PRESETS.get(self.retrieval_tier, RETRIEVAL_PRESETS[RetrievalTier.STANDARD])

    @property
    def visible_to_users(self) -> bool:
        """对话页选择器只展示"已发布且对自己可见"的智能体（D1）"""
        return self.status == AgentStatus.PUBLISHED

    @property
    def mount_summary(self) -> dict:
        """列表页挂载摘要：工具来源数 / 技能数 / 知识库数（A1）"""
        return {
            'mcp_servers': self.mcp_servers.count(),
            'skills': self.skills.count(),
            'knowledge_bases': len(self.knowledge_base_ids or []),
        }

    # ─── A8 快照 ───

    def snapshot(self) -> dict:
        """当次配置的完整快照，发布时归档进 AgentVersion"""
        mounts = [
            {
                'server': m.server.slug,
                'server_name': m.server.name,
                'tool_names': list(m.tool_names or []),
            }
            for m in self.agentmcpserver_set.select_related('server').order_by('server__name')
        ]
        return {
            'name': self.name,
            'slug': self.slug,
            'description': self.description,
            'job_tags': self.job_tags,
            'icon': self.icon,
            'model_name': self.model_name,
            'speech_tier': self.speech_tier,
            'retrieval_tier': self.retrieval_tier,
            'knowledge_mode': self.knowledge_mode,
            'followup_rewrite': self.followup_rewrite,
            'default_preferences': list(self.default_preferences or []),
            'enable_thinking': self.enable_thinking,
            'prompt': self.prompt,
            'welcome_message': self.welcome_message,
            'mcp_servers': mounts,
            'skills': [s.slug for s in self.skills.all()],
            'knowledge_base_ids': list(self.knowledge_base_ids or []),
            'builtin_capabilities': list(self.builtin_capabilities or []),
            'permission_tier': self.permission_tier,
            'allow_tool_names': list(self.allow_tool_names or []),
            'visibility': self.visibility,
            'allowed_departments': sorted(d.name for d in self.allowed_departments.all()),
            'include_sub_departments': self.include_sub_departments,
        }

    def publish(self, actor, change_summary: str = '') -> 'AgentVersion':
        """发布成功后归档版本（A8：递增版本号 + 快照 + 变更摘要）

        发布前检查（A7）由 services.prepublish_check 完成，本方法只负责落版本。
        """
        version = self.current_version + 1
        snapshot = self.snapshot()
        previous = AgentVersion.objects.filter(agent=self).order_by('-version').first()
        record = AgentVersion.objects.create(
            agent=self,
            version=version,
            snapshot=snapshot,
            diff=AgentVersion.diff_of(previous.snapshot if previous else None, snapshot),
            change_summary=change_summary[:200],
            published_by=actor,
        )
        self.current_version = version
        self.status = AgentStatus.PUBLISHED
        self.published_by = actor
        self.published_at = timezone.now()
        self.save(update_fields=['current_version', 'status', 'published_by',
                                 'published_at', 'updated_at'])
        return record


class AgentMCPServer(models.Model):
    """智能体 × 工具来源，保存 per-agent 的工具勾选明细（A4）"""

    agent = models.ForeignKey(AgentProfile, on_delete=models.CASCADE, verbose_name='智能体')
    server = models.ForeignKey('mcp.MCPServer', on_delete=models.CASCADE, verbose_name='工具来源')
    tool_names = models.JSONField('勾选的工具', default=list, blank=True,
                                  help_text='来源内工具名列表；空列表表示未勾选任何工具')
    added_at = models.DateTimeField('挂载时间', auto_now_add=True)

    class Meta:
        db_table = 'lasso_agent_mcp_servers'
        verbose_name = '智能体工具来源'
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=['agent', 'server'], name='uniq_agent_mcp_server'),
        ]

    def __str__(self):
        return f'{self.agent_id}<-{self.server_id}'


class AgentSkill(models.Model):
    """智能体 × 技能（A4：只有已启用技能可挂载）"""

    agent = models.ForeignKey(AgentProfile, on_delete=models.CASCADE, verbose_name='智能体')
    skill = models.ForeignKey('skills.Skill', on_delete=models.CASCADE, related_name='agent_links',
                              verbose_name='技能')
    added_at = models.DateTimeField('挂载时间', auto_now_add=True)

    class Meta:
        db_table = 'lasso_agent_skills'
        verbose_name = '智能体技能'
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=['agent', 'skill'], name='uniq_agent_skill'),
        ]

    def __str__(self):
        return f'{self.agent_id}<-{self.skill_id}'


class ImportRequestStatus(models.TextChoices):
    """跨环境导入申请状态（单级审批：提交不生效，另一人审批通过后才落库）"""

    PENDING = 'pending', '待审批'
    APPROVED = 'approved', '已通过'
    REJECTED = 'rejected', '已驳回'


class AgentImportRequest(models.Model):
    """智能体跨环境导入申请

    提交与生效分离：任何登录用户可提交导入包，平台管理员且非提交人审批通过后
    才在目标环境创建智能体；产物一律为草稿，仍需走 A7 发布检查才会上线。
    """

    agent_name = models.CharField('导入名称', max_length=30)
    description = models.CharField('用途描述', max_length=200, blank=True, default='')
    package = models.JSONField('导入包')
    warnings = models.JSONField('提交时的依赖告警', default=list, blank=True)
    status = models.CharField('状态', max_length=16, choices=ImportRequestStatus.choices,
                              default=ImportRequestStatus.PENDING, db_index=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='agent_imports_submitted', verbose_name='提交人',
    )
    submitted_at = models.DateTimeField('提交时间', auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='agent_imports_reviewed', verbose_name='审批人',
    )
    reviewed_at = models.DateTimeField('审批时间', null=True, blank=True)
    review_note = models.CharField('审批意见', max_length=200, blank=True, default='')
    result = models.JSONField('导入结果', default=dict, blank=True,
                              help_text='审批通过时写入创建的智能体与告警明细')

    class Meta:
        db_table = 'lasso_agent_import_requests'
        verbose_name = '智能体导入申请'
        verbose_name_plural = verbose_name
        ordering = ['-submitted_at']

    def __str__(self):
        return f'{self.agent_name}（{self.get_status_display()}）'


class AgentVersion(models.Model):
    """发布归档的版本快照（A8，P0 只读历史）"""

    agent = models.ForeignKey(AgentProfile, on_delete=models.CASCADE, related_name='versions',
                              verbose_name='智能体')
    version = models.IntegerField('版本号', help_text='v1、v2… 自动递增，无需管理员命名')
    snapshot = models.JSONField('配置快照')
    diff = models.JSONField('与上一版的分区差异', default=dict, blank=True,
                            help_text='A7 逐分区差异视图；P0 历史只读也用它展示')
    change_summary = models.CharField('变更摘要', max_length=200, blank=True, default='')
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='agent_versions_published', verbose_name='发布人',
    )
    created_at = models.DateTimeField('发布时间', auto_now_add=True)

    class Meta:
        db_table = 'lasso_agent_versions'
        verbose_name = '智能体版本'
        verbose_name_plural = verbose_name
        ordering = ['-version']
        constraints = [
            models.UniqueConstraint(fields=['agent', 'version'], name='uniq_agent_version_no'),
        ]

    def __str__(self):
        return f'{self.agent.name} v{self.version}'

    @staticmethod
    def diff_of(previous: dict | None, current: dict) -> dict:
        """按分区比较两份快照，返回 {分区: {'old':..., 'new':...}}

        分区口径与 A7 检查项一致：基本信息 / 模型与说话方式 / 工具来源 /
        技能 / 知识库 / 内置能力 / 授权档位 / 可见范围。
        """
        if not previous:
            return {'created': {'old': None, 'new': True}}
        fields_by_section = {
            'basic': ['name', 'description', 'job_tags', 'icon'],
            'model': ['model_name', 'speech_tier', 'retrieval_tier', 'enable_thinking',
                      'prompt', 'welcome_message'],
            'mcp_servers': ['mcp_servers'],
            'skills': ['skills'],
            'knowledge_bases': ['knowledge_base_ids', 'knowledge_mode', 'followup_rewrite'],
            'preferences': ['default_preferences'],
            'builtin_capabilities': ['builtin_capabilities'],
            'permission': ['permission_tier', 'allow_tool_names'],
            'visibility': ['visibility', 'allowed_departments', 'include_sub_departments'],
        }
        diff = {}
        for section, fields in fields_by_section.items():
            changed = {
                f: {'old': previous.get(f), 'new': current.get(f)}
                for f in fields if previous.get(f) != current.get(f)
            }
            if changed:
                diff[section] = changed
        return diff
