"""MCP 工具来源（PRD 第 6 章 B 组）

一个 MCPServer = 一个外部工具来源（接口型 HTTP MCP / 命令型 stdio）。
MCPTool = 该来源被发现出来的单个工具，工具级启停在来源维度记录（B4），
智能体维度的逐个勾选见 agenthub.AgentMCPServer。

密钥（headers / env 中的敏感值）一律 Fernet 加密落库（B5）：
读接口只回显掩码、整值替换、不进模型上下文、不落审计、不参与导出。
"""
import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models
from django.utils.text import slugify

from backend.apps.common.choices import Visibility


# 平台随版本维护的固定超时（B1：不提供输入项）
TIMEOUT_BY_TRANSPORT = {'http_mcp': 30, 'stdio': 90}


def _fernet() -> Fernet:
    """取 MCP_SECRET_KEY；未配置时由 SECRET_KEY 派生，绝不落明文"""
    key = (getattr(settings, 'MCP_SECRET_KEY', '') or '').strip()
    if not key:
        digest = hashlib.sha256(settings.SECRET_KEY.encode('utf-8')).digest()
        key = base64.urlsafe_b64encode(digest).decode('ascii')
    return Fernet(key.encode('ascii') if isinstance(key, str) else key)


class MCPServer(models.Model):
    """外部工具来源"""

    TRANSPORT_CHOICES = [
        ('http_mcp', '接口型'),
        ('stdio', '命令型'),
    ]
    LIFECYCLE_CHOICES = [
        ('persistent', '常驻复用'),
        ('per_session', '每次独立会话'),
        ('ephemeral', '会话结束即回收'),
    ]
    # 工具来源状态机（PRD 4.3）
    STATUS_CHOICES = [
        ('unknown', '未验证'),
        ('healthy', '正常'),
        ('error', '异常'),
        ('disabled', '已停用'),
        ('egress_blocked', '出网未开放'),
    ]

    name = models.CharField('名称', max_length=128, unique=True)
    slug = models.CharField('标识', max_length=64, unique=True, editable=False,
                            help_text='工具全称 mcp__{slug}__{tool} 的组成部分，创建后不可改')
    description = models.CharField('描述', max_length=200, blank=True, default='')

    transport = models.CharField('连接类型', max_length=16, choices=TRANSPORT_CHOICES, default='http_mcp')

    # ── 接口型 ──
    url = models.CharField('服务地址', max_length=500, blank=True, default='')
    headers = models.JSONField('请求头', default=dict, blank=True,
                               help_text='非敏感请求头明文；敏感值放 secrets')
    # B8-a：出网类来源必须声明允许访问的域名（精确域名或 *.example.com）
    egress_domains = models.JSONField('允许的出网域名', default=list, blank=True,
                                      help_text='仅出网类来源需要；支持精确域名与 *.example.com 通配')

    # ── 命令型 ──
    command = models.CharField('启动命令', max_length=255, blank=True, default='')
    args = models.JSONField('启动参数', default=list, blank=True)
    cwd = models.CharField('工作目录', max_length=500, blank=True, default='')

    # 加密后的 {"headers": {...}, "env": {...}}（B5）
    secrets_enc = models.TextField('密钥（加密）', blank=True, default='')

    is_stateful = models.BooleanField('有状态连接', default=False)
    lifecycle = models.CharField('连接方式', max_length=16, choices=LIFECYCLE_CHOICES, default='per_session')
    timeout_seconds = models.IntegerField('超时秒数', null=True, blank=True,
                                          help_text='留空按平台固定值：接口型 30 秒 / 命令型 90 秒')

    # 来源级工具启停（B4：新发现工具默认关闭，此列表内才注册进 Toolkit）
    enabled_tools = models.JSONField('启用的工具名', default=list, blank=True)
    # 最近一次连通性测试发现的工具快照（缓存，便于列表页展示）
    discovered_tools = models.JSONField('发现的工具', default=list, blank=True)

    status = models.CharField('状态', max_length=16, choices=STATUS_CHOICES, default='unknown')
    last_check_at = models.DateTimeField('最后检测时间', null=True, blank=True)
    last_error = models.CharField('最后错误', max_length=500, blank=True, default='')

    requires_egress = models.BooleanField('需要出网', default=False,
                                          help_text='出网类来源受平台出网总开关约束（B8）')
    is_builtin = models.BooleanField('内置来源', default=False,
                                     help_text='内置演示来源不可删除（B7）')
    is_active = models.BooleanField('已启用', default=True)

    visibility = models.CharField('可见范围', max_length=16, choices=Visibility.choices,
                                  default=Visibility.PRIVATE)
    allowed_departments = models.ManyToManyField(
        'users.Department', blank=True, related_name='+', verbose_name='可见部门',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='mcp_servers_created', verbose_name='创建人',
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'lasso_mcp_servers'
        verbose_name = '工具来源'
        verbose_name_plural = verbose_name
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or 'mcp'
            slug, n = base, 2
            while MCPServer.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f'{base}-{n}'
                n += 1
            self.slug = slug
        super().save(*args, **kwargs)

    @property
    def effective_timeout(self) -> int:
        return self.timeout_seconds or TIMEOUT_BY_TRANSPORT.get(self.transport, 30)

    # ─── 密钥读写（B5） ───

    def set_secrets(self, secrets: dict) -> None:
        """整体替换密钥；secrets 形如 {"headers": {...}, "env": {...}}，空字典清空"""
        payload = json.dumps(secrets or {}, ensure_ascii=False, sort_keys=True)
        self.secrets_enc = _fernet().encrypt(payload.encode('utf-8')).decode('ascii') if secrets else ''

    def get_secrets(self) -> dict:
        """解密后的真实密钥，仅供运行时装配使用，禁止直接返回给接口层"""
        if not self.secrets_enc:
            return {}
        try:
            return json.loads(_fernet().decrypt(self.secrets_enc.encode('ascii')).decode('utf-8'))
        except (InvalidToken, ValueError):
            return {}

    def get_masked_secrets(self) -> dict:
        """接口回显用：只保留每个值后 4 位"""
        out = {}
        for group, kv in self.get_secrets().items():
            out[group] = {
                k: ('' if not v else '******' + str(v)[-4:]) for k, v in (kv or {}).items()
            }
        return out


class MCPTool(models.Model):
    """来源内单个工具（B4 启停 / 失效标记，工具全称统一口径见 7.1）"""

    STATE_CHOICES = [
        ('present', '正常'),
        ('missing', '工具已失效'),
    ]

    server = models.ForeignKey(MCPServer, on_delete=models.CASCADE, related_name='tools', verbose_name='所属来源')
    name = models.CharField('工具名', max_length=128)
    full_name = models.CharField('工具全称', max_length=160, unique=True,
                                 help_text='mcp__{server slug}__{tool name}')
    description = models.TextField('描述', blank=True, default='')
    input_schema = models.JSONField('入参结构', default=dict, blank=True)
    enabled = models.BooleanField('已启用', default=False)
    state = models.CharField('状态', max_length=16, choices=STATE_CHOICES, default='present')
    first_seen_at = models.DateTimeField('首次发现', auto_now_add=True)
    last_seen_at = models.DateTimeField('最后发现', null=True, blank=True)

    class Meta:
        db_table = 'lasso_mcp_tools'
        verbose_name = '来源工具'
        verbose_name_plural = verbose_name
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(fields=['server', 'name'], name='uniq_mcp_tool_per_server'),
        ]

    def __str__(self):
        return self.full_name

    def save(self, *args, **kwargs):
        if not self.full_name:
            self.full_name = f'mcp__{self.server.slug}__{self.name}'
        super().save(*args, **kwargs)
