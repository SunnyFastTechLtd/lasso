"""技能规范（PRD 第 6 章 C 组）

一份 Skill = 一份带名称与一句话适用说明的作答规范。技能**不是工具**，
运行时由 AgentScope 的 Toolkit 把规范说明注入 system prompt（见可行性报告 4.2），
而 Toolkit 只认磁盘目录，所以技能正文以数据库为唯一真源，
再镜像到 SKILLS_MIRROR_DIR/{slug}/ 供 LocalSkillLoader 扫描。

状态机（PRD 4.4）：校验未通过 / 待审核 / 已启用 / 已停用 / 内置。
上传 ≠ 生效：只有管理员看过正文并审核启用后，技能才能被智能体挂载（C1–C3）。
P0 只做单文件规范（正文即 SKILL.md），SkillFile 为 P1 多文件技能包预留结构。
"""
import hashlib

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.utils.text import slugify
from django.utils import timezone

from backend.apps.common.choices import Visibility


# 正文长度上限（PRD C2 / 9.2 #7 已决：8000 字符，超限不允许保存，现场不可调）
MAX_BODY_CHARS = 8000


def body_fingerprint(body: str) -> str:
    """正文指纹，审核留痕用（C3：留人、时间、正文指纹）"""
    return hashlib.sha256((body or '').encode('utf-8')).hexdigest()


class SkillStatus(models.TextChoices):
    INVALID = 'invalid', '校验未通过'
    PENDING = 'pending', '待审核'
    ACTIVE = 'active', '已启用'
    DISABLED = 'disabled', '已停用'
    BUILTIN = 'builtin', '内置'


class SkillSource(models.TextChoices):
    BUILTIN = 'builtin', '出厂内置'
    UPLOAD = 'upload', '上传/粘贴'
    REPO = 'repo', '技能仓库'


class SkillScope(models.TextChoices):
    """运行时生效范围（C5）。departments 即部门灰度，逐步放开直到 all。"""

    ALL = 'all', '全企业生效'
    DEPARTMENTS = 'departments', '仅指定部门生效'


class Skill(models.Model):
    """作答规范本体"""

    name = models.CharField('技能名称', max_length=128, unique=True)
    slug = models.CharField('标识', max_length=64, unique=True, editable=False,
                            help_text='磁盘镜像目录名，创建后不可改')
    description = models.CharField('适用说明', max_length=200, blank=True, default='',
                                   help_text='一句话说明什么时候该读这份规范（C1 必填）')

    source = models.CharField('来源', max_length=16, choices=SkillSource.choices,
                              default=SkillSource.UPLOAD)
    entry_file = models.CharField('规范文件', max_length=64, default='SKILL.md')
    body = models.TextField('规范正文', blank=True, default='')
    body_hash = models.CharField('正文指纹', max_length=64, blank=True, default='',
                                 help_text='审核通过时的正文指纹，用于判断正文是否已变更')
    char_count = models.IntegerField('正文字数', default=0)
    # 保存时风险扫描的「可疑但未阻断」提示，供审核页展示（9.2 #7）
    scan_warnings = models.JSONField('风险扫描提示', default=list, blank=True)
    approved_body = models.TextField('生效正文', blank=True, default='',
                                     help_text='最近一次审核通过的正文；编辑中的正文在通过审核前'
                                               '不影响此字段，避免装配中的智能体读不到规范（C4）')
    current_version = models.PositiveIntegerField('当前生效版本号', default=0)
    # 附件文本的「可疑但不阻断」提示（C1）；高危确认信息见下方 risk_ack_*
    file_scan_warnings = models.JSONField('附件风险扫描提示', default=list, blank=True)

    # 决策 10：高危内容「提示 + 确认放行」的草稿侧留痕；任何内容变更都会清空
    risk_ack_at = models.DateTimeField('风险确认时间', null=True, blank=True)
    risk_ack_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='skills_risk_acked', verbose_name='风险确认人',
    )
    risk_ack_fingerprint = models.CharField('风险确认指纹', max_length=64, blank=True, default='')
    risk_ack_rules = models.JSONField('已确认风险规则', default=list, blank=True)
    risk_ack_files = models.JSONField('已确认风险文件', default=list, blank=True)
    risk_ack_ruleset_version = models.CharField('确认时的规则集版本', max_length=32,
                                                blank=True, default='')

    status = models.CharField('状态', max_length=16, choices=SkillStatus.choices,
                              default=SkillStatus.INVALID)
    validation_errors = models.JSONField('校验失败原因', default=list, blank=True,
                                         help_text='C2：逐条列出缺字段、正文为空、超长等')
    reject_reason = models.CharField('驳回理由', max_length=200, blank=True, default='')

    # 规范声明依赖的工具（A7 发布前检查：挂载了技能但来源缺失要能提示）
    requires = models.JSONField('依赖的工具', default=list, blank=True,
                                help_text='工具全称列表，如 mcp__demo-device__get_status')

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='skills_reviewed', verbose_name='审核人',
    )
    reviewed_at = models.DateTimeField('审核时间', null=True, blank=True)

    visibility = models.CharField('可见范围', max_length=16, choices=Visibility.choices,
                                  default=Visibility.ADMIN_ONLY,
                                  help_text='P0 技能只对挂载它的智能体生效（C5），此处仅控制管理端可见性')
    allowed_departments = models.ManyToManyField(
        'users.Department', blank=True, related_name='+', verbose_name='可见部门',
    )
    # C5：运行时生效范围（与上面的管理端可见范围分离，避免 P0 admin_only 语义回归）
    scope_mode = models.CharField('生效范围', max_length=16, choices=SkillScope.choices,
                                  default=SkillScope.ALL)
    scope_include_sub_departments = models.BooleanField('含子部门', default=False)
    applicable_departments = models.ManyToManyField(
        'users.Department', blank=True, related_name='+', verbose_name='生效部门',
    )
    # C5 灰度：基础范围（全员/部门）命中后，再按用户稳定分桶放开 rollout_percent%
    rollout_percent = models.PositiveSmallIntegerField(
        '灰度比例', default=100,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text='基础范围内按用户稳定分桶放开的百分比：0=不放开，100=全量')
    # C5 定向用户白名单：命中即生效，不受部门与灰度比例限制
    applicable_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name='+', verbose_name='定向用户',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='skills_created', verbose_name='创建人',
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'lasso_skills'
        verbose_name = '技能规范'
        verbose_name_plural = verbose_name
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or 'skill'
            slug, n = base, 2
            while Skill.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f'{base}-{n}'
                n += 1
            self.slug = slug
        self.char_count = len(self.body or '')
        super().save(*args, **kwargs)

    # ─── 状态机 ───

    @property
    def mountable(self) -> bool:
        """能否被智能体挂载（A4：未启用技能灰置不可选）"""
        return self.status in (SkillStatus.ACTIVE, SkillStatus.BUILTIN) or self.live

    @property
    def live(self) -> bool:
        """是否仍在会话运行时生效。

        C4 的口径：已启用技能被编辑后状态回到"待审核"，但原启用版本继续生效
        直至新版通过——因此有待审核编辑稿的技能依然可挂载、依然进 prompt。
        """
        return bool(self.approved_body) and self.status == SkillStatus.PENDING

    @property
    def editable(self) -> bool:
        """内置技能不可编辑、不可删除（C3/4.4、C6）"""
        return self.source != SkillSource.BUILTIN

    @property
    def runtime_body(self) -> str:
        """会话运行时实际注入的正文：以最近审核通过版本为准（C4）。

        待审核的编辑稿只在管理端预览，不参与运行时。
        """
        return self.approved_body or ''

    @property
    def body_changed(self) -> bool:
        """正文是否与审核通过版本不一致（审核留痕与列表标记用）"""
        return body_fingerprint(self.body) != (self.body_hash or '')

    def in_base_scope(self, user) -> bool:
        """C5 基础范围：全员，或部门命中（可选含子部门）。

        不含灰度比例与定向名单——装配页只用这一层，避免按人闪烁。
        """
        if self.scope_mode == SkillScope.ALL:
            return True

        department_name = (getattr(user, 'department', '') or '').strip()
        if not department_name:
            return False

        from backend.apps.users.models import Department

        department = Department.objects.filter(name=department_name).first()
        if department is None:
            return False

        allowed_ids = set(self.applicable_departments.values_list('id', flat=True))
        if not allowed_ids:
            return False
        if not self.scope_include_sub_departments:
            return department.pk in allowed_ids

        for allowed in Department.objects.filter(pk__in=allowed_ids):
            if department.pk in allowed.descendant_ids():
                return True
        return False

    def is_targeted(self, user) -> bool:
        """定向用户白名单：命中即生效，不受部门与灰度比例限制"""
        return any(u.pk == user.pk for u in self.applicable_users.all())

    def rollout_bucket(self, user) -> int:
        """C5 稳定分桶：同一技能同一用户永远落在同一桶

        用 slug + user pk 求哈希，比例上调时已命中的用户不会掉出去（只增不减）。
        """
        raw = f'{self.slug}:{user.pk}'.encode('utf-8')
        return int(hashlib.sha256(raw).hexdigest()[:8], 16) % 100

    def in_rollout(self, user) -> bool:
        if self.rollout_percent >= 100:
            return True
        if self.rollout_percent <= 0:
            return False
        return self.rollout_bucket(user) < self.rollout_percent

    def scope_denial_reason(self, user) -> str:
        """未命中时的可读原因，运行时降级说明用"""
        if not self.in_base_scope(user):
            return '技能生效范围不包含你所在部门'
        return f'尚未命中该技能的灰度比例（当前放开 {self.rollout_percent}%）'

    def applies_to(self, user) -> bool:
        """C5：技能是否对当前会话用户生效。

        判定顺序：管理员恒命中（审核试聊/排障）→ 定向用户白名单 →
        基础范围（全员/部门）→ 灰度比例。用户未设置部门时不命中基础范围，
        由运行时给出降级说明。
        """
        if user is None or not getattr(user, 'is_authenticated', False):
            return False
        if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
            return True
        if self.is_targeted(user):
            return True
        if not self.in_base_scope(user):
            return False
        return self.in_rollout(user)

    @property
    def mirror_dir(self):
        """DB → 磁盘镜像目录（LocalSkillLoader 扫描此目录）"""
        return settings.SKILLS_MIRROR_DIR / self.slug

    def validate_content(self) -> list:
        """C1/C2 校验：返回逐条失败原因，空列表即通过"""
        errors = []
        if not (self.name or '').strip():
            errors.append('缺少技能名称')
        elif Skill.objects.filter(name=self.name).exclude(pk=self.pk).exists():
            errors.append('名称与企业内已有技能冲突')
        if not (self.description or '').strip():
            errors.append('缺一句话适用说明')
        if not (self.body or '').strip():
            errors.append('规范正文为空')
        elif self.char_count > MAX_BODY_CHARS:
            errors.append(f'规范正文超长（{self.char_count} 字，上限 {MAX_BODY_CHARS} 字）')
        return errors

    def submit(self) -> str:
        """上传/编辑后提交：通过 → 待审核，失败 → 校验未通过并附原因。

        已停用的技能保持停用：C4 的"停用即时对新会话生效"意味着停用后
        不能再被一次编辑悄悄带回运行时（它带着旧的生效正文，状态一旦转
        待审核就会被判定为仍在生效）。要改稿先恢复启用，或重新提交审核。
        """
        self.validation_errors = self.validate_content()
        if self.status == SkillStatus.DISABLED:
            self.save(update_fields=['validation_errors', 'char_count', 'updated_at'])
            return self.status
        self.status = SkillStatus.INVALID if self.validation_errors else SkillStatus.PENDING
        self.save(update_fields=['validation_errors', 'status', 'char_count', 'updated_at'])
        return self.status

    def clear_risk_ack(self):
        """内容或附件变更后，旧的「高危放行」确认即失效"""
        self.risk_ack_at = None
        self.risk_ack_by = None
        self.risk_ack_fingerprint = ''
        self.risk_ack_rules = []
        self.risk_ack_files = []
        self.risk_ack_ruleset_version = ''

    @property
    def latest_version(self):
        return self.versions.order_by('-version').first()

    def approve(self, actor, change_summary: str = '') -> 'Skill':
        """C3 审核通过：正文指纹留痕，编辑稿转正为生效正文，转已启用。

        C4：每次审核通过都生成不可变版本快照（正文 + 附件引用 + 高危确认信息）。
        """
        with transaction.atomic():
            Skill.objects.select_for_update().filter(pk=self.pk).exists()
            self.approved_body = self.body or ''
            self.body_hash = body_fingerprint(self.body)
            self.status = SkillStatus.BUILTIN if self.source == SkillSource.BUILTIN else SkillStatus.ACTIVE
            self.validation_errors = []
            self.reject_reason = ''
            self.reviewed_by = actor
            self.reviewed_at = timezone.now()
            latest = self.versions.order_by('-version').first()
            self.current_version = max(self.current_version or 0,
                                       latest.version if latest else 0) + 1
            self.save(update_fields=['approved_body', 'body_hash', 'status', 'validation_errors',
                                     'reject_reason', 'reviewed_by', 'reviewed_at',
                                     'current_version', 'updated_at'])
            version = SkillVersion.objects.create(
                skill=self,
                version=self.current_version,
                body=self.approved_body,
                body_hash=self.body_hash,
                char_count=self.char_count,
                change_summary=(change_summary or '')[:200],
                approved_by=actor,
                approved_at=self.reviewed_at,
                risk_ack_at=self.risk_ack_at,
                risk_ack_by=self.risk_ack_by,
                risk_ack_rules=list(self.risk_ack_rules or []),
                risk_ack_files=list(self.risk_ack_files or []),
                risk_ack_ruleset_version=self.risk_ack_ruleset_version or '',
            )
            for item in self.files.select_related('blob').all():
                if item.entry or item.rel_path == self.entry_file or item.blob is None:
                    continue
                SkillVersionFile.objects.create(
                    version=version,
                    rel_path=item.rel_path,
                    blob=item.blob,
                    size=item.blob.size,
                    sha256=item.blob.sha256,
                )
        return self

    def reject(self, actor, reason: str) -> 'Skill':
        """C3 驳回：回到校验未通过并附理由，创建者可见"""
        self.status = SkillStatus.INVALID
        self.reject_reason = (reason or '')[:200]
        self.reviewed_by = actor
        self.reviewed_at = timezone.now()
        self.save(update_fields=['status', 'reject_reason', 'reviewed_by',
                                 'reviewed_at', 'updated_at'])
        return self

    def enable(self) -> 'Skill':
        """已停用 → 已启用（内置技能始终视为已启用）"""
        self.status = SkillStatus.BUILTIN if self.source == SkillSource.BUILTIN else SkillStatus.ACTIVE
        self.save(update_fields=['status', 'updated_at'])
        return self

    def disable(self) -> 'Skill':
        """C4：停用即时对新会话生效"""
        self.status = SkillStatus.DISABLED
        self.save(update_fields=['status', 'updated_at'])
        return self


class SkillBlob(models.Model):
    """技能包文件内容（内容寻址，跨版本去重）"""

    sha256 = models.CharField('内容指纹', max_length=64, unique=True)
    size = models.BigIntegerField('字节数', default=0)
    content = models.BinaryField('文件内容')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'lasso_skill_blobs'
        verbose_name = '技能文件内容'
        verbose_name_plural = verbose_name

    @classmethod
    def put(cls, raw: bytes) -> 'SkillBlob':
        digest = hashlib.sha256(raw).hexdigest()
        blob, _ = cls.objects.get_or_create(
            sha256=digest, defaults={'size': len(raw), 'content': raw})
        return blob


class SkillFile(models.Model):
    """技能包内附属文件（编辑稿，C1）"""

    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name='files', verbose_name='所属技能')
    rel_path = models.CharField('相对路径', max_length=255)
    entry = models.BooleanField('规范主文件', default=False)
    size = models.BigIntegerField('字节数', default=0)
    sha256 = models.CharField('内容指纹', max_length=64, blank=True, default='')
    blob = models.ForeignKey(SkillBlob, on_delete=models.PROTECT, null=True, blank=True,
                             related_name='+', verbose_name='文件内容')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'lasso_skill_files'
        verbose_name = '技能文件'
        verbose_name_plural = verbose_name
        ordering = ['rel_path']
        constraints = [
            models.UniqueConstraint(fields=['skill', 'rel_path'], name='uniq_skill_file_path'),
        ]

    def __str__(self):
        return f'{self.skill_id}:{self.rel_path}'


class SkillVersion(models.Model):
    """C4：审核通过产生的不可变版本快照（正文 + 附件引用 + 高危确认留痕）"""

    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name='versions',
                              verbose_name='所属技能')
    version = models.PositiveIntegerField('版本号')
    body = models.TextField('版本正文', blank=True, default='')
    body_hash = models.CharField('正文指纹', max_length=64, blank=True, default='')
    char_count = models.IntegerField('正文字数', default=0)
    change_summary = models.CharField('变更摘要', max_length=200, blank=True, default='')
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='+', verbose_name='审核人')
    approved_at = models.DateTimeField('审核时间')
    risk_ack_at = models.DateTimeField('风险确认时间', null=True, blank=True)
    risk_ack_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='+', verbose_name='风险确认人')
    risk_ack_rules = models.JSONField('已确认风险规则', default=list, blank=True)
    risk_ack_files = models.JSONField('已确认风险文件', default=list, blank=True)
    risk_ack_ruleset_version = models.CharField('确认时的规则集版本', max_length=32,
                                                blank=True, default='')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'lasso_skill_versions'
        verbose_name = '技能版本'
        verbose_name_plural = verbose_name
        ordering = ['-version']
        constraints = [
            models.UniqueConstraint(fields=['skill', 'version'], name='uniq_skill_version'),
        ]

    def __str__(self):
        return f'{self.skill_id}:v{self.version}'


class SkillVersionFile(models.Model):
    """C4：版本快照中的附件（内容引用 SkillBlob，支持回滚与差异对比）"""

    version = models.ForeignKey(SkillVersion, on_delete=models.CASCADE, related_name='files',
                                verbose_name='所属版本')
    rel_path = models.CharField('相对路径', max_length=255)
    blob = models.ForeignKey(SkillBlob, on_delete=models.PROTECT, related_name='+',
                             verbose_name='文件内容')
    size = models.BigIntegerField('字节数', default=0)
    sha256 = models.CharField('内容指纹', max_length=64, blank=True, default='')

    class Meta:
        db_table = 'lasso_skill_version_files'
        verbose_name = '技能版本文件'
        verbose_name_plural = verbose_name
        ordering = ['rel_path']
        constraints = [
            models.UniqueConstraint(fields=['version', 'rel_path'], name='uniq_skill_version_file'),
        ]

    def __str__(self):
        return f'{self.version_id}:{self.rel_path}'
