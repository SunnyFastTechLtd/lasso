from urllib.parse import quote

from django.db import models

from backend.apps.mcp.models import _fernet


class SystemSetting(models.Model):
    """系统配置项（键值对存储）"""

    key = models.CharField(max_length=100, unique=True, verbose_name='配置键')
    value = models.TextField(blank=True, default='', verbose_name='配置值')
    description = models.CharField(max_length=255, blank=True, default='', verbose_name='说明')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'system_setting'
        verbose_name = '系统配置'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.key}: {self.value[:50]}'

    @classmethod
    def get_value(cls, key: str, default: str = '') -> str:
        try:
            return cls.objects.get(key=key).value
        except cls.DoesNotExist:
            return default

    @classmethod
    def set_value(cls, key: str, value: str, description: str = ''):
        obj, created = cls.objects.update_or_create(
            key=key,
            defaults={'value': value, 'description': description},
        )
        return obj


class ProxyEndpoint(models.Model):
    """平台级代理条目（供 SearXNG 引擎与天工正文抓取共用）

    密码与 MCP 来源密钥同一套加密口径（Fernet + MCP_SECRET_KEY）：
    读接口只回 has_password，更新时留空表示不改；明文不进审计、不进日志。
    """

    SCHEME_CHOICES = [
        ('http', 'HTTP'),
        ('socks5', 'SOCKS5'),
    ]
    STATUS_CHOICES = [
        ('untested', '未测试'),
        ('ok', '通过'),
        ('failed', '失败'),
    ]

    name = models.CharField('名称', max_length=100, unique=True)
    scheme = models.CharField('协议', max_length=10, choices=SCHEME_CHOICES, default='http')
    host = models.CharField('主机', max_length=255)
    port = models.PositiveIntegerField('端口')
    username = models.CharField('认证用户名', max_length=150, blank=True, default='')
    password_enc = models.TextField('密码（加密）', blank=True, default='')
    enabled = models.BooleanField('启用', default=True)
    note = models.CharField('备注', max_length=255, blank=True, default='')
    last_test_at = models.DateTimeField('最近测试时间', null=True, blank=True)
    last_test_status = models.CharField('最近测试结果', max_length=16,
                                        choices=STATUS_CHOICES, default='untested')
    last_test_ms = models.PositiveIntegerField('最近测试耗时(毫秒)', null=True, blank=True)
    last_test_error = models.TextField('最近测试错误', blank=True, default='')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'system_proxy_endpoint'
        verbose_name = '代理条目'
        verbose_name_plural = verbose_name
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.scheme}://{self.host}:{self.port})'

    @property
    def has_password(self) -> bool:
        return bool(self.password_enc)

    def set_password(self, raw: str):
        """整值替换；空串表示清空"""
        text = (raw or '').strip()
        self.password_enc = (
            _fernet().encrypt(text.encode('utf-8')).decode('ascii') if text else ''
        )

    def get_password(self) -> str:
        if not self.password_enc:
            return ''
        try:
            return _fernet().decrypt(self.password_enc.encode('ascii')).decode('utf-8')
        except Exception:  # noqa: BLE001 - 密钥轮换/损坏时按不可用处理
            return ''

    def url(self, with_credentials: bool = True) -> str:
        """拼出可用的代理地址；认证信息做 URL 编码"""
        auth = ''
        if with_credentials and self.username:
            user = quote(self.username, safe='')
            password = quote(self.get_password(), safe='')
            auth = f'{user}:{password}@' if self.password_enc else f'{user}@'
        return f'{self.scheme}://{auth}{self.host}:{self.port}'

    def display_url(self) -> str:
        """界面展示用：有认证时只露用户名，不露密码"""
        auth = ''
        if self.username:
            auth = f'{self.username}:***@' if self.has_password else f'{self.username}@'
        return f'{self.scheme}://{auth}{self.host}:{self.port}'


class ResourceProxyBinding(models.Model):
    """按资源绑定代理：一个资源只能绑一个代理，同一代理可被多个资源复用"""

    TYPE_SEARXNG_ENGINE = 'searxng_engine'
    TYPE_TIANGONG_DOMAIN = 'tiangong_domain'
    TYPE_TIANGONG_SOURCE = 'tiangong_source'
    TYPE_CHOICES = [
        (TYPE_SEARXNG_ENGINE, 'SearXNG 引擎'),
        (TYPE_TIANGONG_DOMAIN, '天工抓取域名'),
        (TYPE_TIANGONG_SOURCE, '天工来源站'),
    ]

    resource_type = models.CharField('资源类型', max_length=32, choices=TYPE_CHOICES)
    resource_key = models.CharField('资源标识', max_length=255,
                                   help_text='引擎名 / 域名或 *.通配 / 来源站 ID')
    proxy = models.ForeignKey(ProxyEndpoint, on_delete=models.PROTECT,
                              related_name='bindings', verbose_name='代理')
    enabled = models.BooleanField('启用', default=True)
    # 最近一次「应用」是否把这行写进了线上配置；应用失败时置 False，界面标未生效
    applied = models.BooleanField('已生效', default=False)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'system_resource_proxy_binding'
        verbose_name = '资源代理绑定'
        verbose_name_plural = verbose_name
        ordering = ['resource_type', 'resource_key']
        constraints = [
            models.UniqueConstraint(fields=['resource_type', 'resource_key'],
                                    name='uniq_resource_proxy'),
        ]

    def __str__(self):
        return f'{self.resource_type}:{self.resource_key} -> {self.proxy_id}'
