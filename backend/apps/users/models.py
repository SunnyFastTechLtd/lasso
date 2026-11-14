from django.contrib.auth.models import AbstractUser
from django.db import models
from .managers import UserManager


class User(AbstractUser):
    """扩展用户模型"""

    objects = UserManager()

    nickname = models.CharField('昵称', max_length=64, blank=True, default='')
    avatar = models.ImageField('头像', upload_to='avatars/', blank=True, null=True)
    phone = models.CharField('手机号', max_length=20, blank=True, default='')
    department = models.CharField('部门', max_length=100, blank=True, default='')
    position = models.CharField('职位', max_length=100, blank=True, default='')

    # 用量统计
    total_chat_count = models.IntegerField('总对话次数', default=0)
    total_tokens_used = models.BigIntegerField('总Token使用量', default=0)
    storage_used = models.BigIntegerField('已用存储空间(字节)', default=0)

    # 软删除：管理员删除账号只标记并停用，不物理删行。
    # 这样会话正文、生成文件与审计外键都不会被 CASCADE 连带销毁，追责链完整；
    # 代价是用户名继续占用（唯一约束含已删除行），且需要在列表里显式过滤。
    is_deleted = models.BooleanField('已删除', default=False, db_index=True)
    deleted_at = models.DateTimeField('删除时间', null=True, blank=True)
    deleted_by = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='deleted_users', verbose_name='删除人',
    )

    class Meta:
        db_table = 'ask_users'
        verbose_name = '用户'
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.username

    @property
    def display_name(self):
        return self.nickname or self.username


class Department(models.Model):
    """部门：用户与知识库部门字段的下拉候选源"""

    name = models.CharField('部门名称', max_length=100, unique=True)
    parent = models.ForeignKey(
        'self', on_delete=models.CASCADE, null=True, blank=True,
        related_name='children', verbose_name='上级部门',
    )
    description = models.CharField('描述', max_length=255, blank=True, default='')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'ask_departments'
        verbose_name = '部门'
        verbose_name_plural = '部门'
        ordering = ['name']

    def __str__(self):
        return self.name

    def descendant_ids(self):
        """自身 + 全部后代部门 ID（A6「可见范围含子部门」用）"""
        out = {self.pk}
        stack = [self.pk]
        while stack:
            child_ids = list(Department.objects.filter(
                parent_id=stack.pop()).values_list('id', flat=True))
            for cid in child_ids:
                if cid not in out:
                    out.add(cid)
                    stack.append(cid)
        return out


class AuditLog(models.Model):
    """管理员操作日志"""

    ACTION_CHOICES = [
        ('llm_update', '更新 LLM 配置'),
        ('llm_reset', '重置 LLM 配置'),
        ('user_create', '创建用户'),
        ('user_update', '修改用户'),
        ('user_delete', '删除用户'),
        ('login_success', '登录成功'),
        ('login_failed', '登录失败'),
        ('logout', '登出'),
        ('password_change', '修改密码'),
        ('password_reset', '管理员重置密码'),
        ('user_enable', '启用账号'),
        ('user_disable', '停用账号'),
        ('force_logout', '强制下线'),
        ('kb_delete', '删除知识库'),
        ('department_create', '创建部门'),
        ('department_update', '修改部门'),
        ('department_delete', '删除部门'),
        ('egress_toggle', '切换出网总开关'),
        ('agent_create', '创建智能体'),
        ('agent_update', '编辑智能体'),
        ('agent_publish', '发布智能体'),
        ('agent_disable', '停用智能体'),
        ('agent_archive', '归档智能体'),
        ('agent_delete', '删除智能体'),
        ('agent_restore', '智能体恢复草稿'),
        ('agent_visibility', '改智能体可见范围'),
        ('agent_mount', '智能体挂载变更'),
        ('agent_risk_ack', '发布时确认风险项'),
        ('agent_rollback', '回滚到历史版本'),
        ('agent_version_diff', '版本对比'),
        ('agent_export', '跨环境导出智能体'),
        ('agent_import_request', '提交智能体导入申请'),
        ('agent_import_approve', '审批通过智能体导入'),
        ('agent_import_reject', '驳回智能体导入'),
        ('mcp_create', '创建工具来源'),
        ('mcp_update', '编辑工具来源'),
        ('mcp_command_denied', '命令被白名单拒绝'),
        ('mcp_egress_denied', '出网被出口清单拒绝'),
        ('mcp_test', '连通性测试'),
        ('mcp_tool_toggle', '来源工具启停'),
        ('mcp_disable', '停用工具来源'),
        ('mcp_delete', '删除工具来源'),
        ('mcp_unmount', '摘除挂载'),
        ('mcp_secret_update', '变更来源密钥'),
        ('skill_upload', '上传技能'),
        ('skill_invalid', '技能校验失败'),
        ('skill_approve', '技能审核通过'),
        ('skill_reject', '技能驳回'),
        ('skill_disable', '技能停用'),
        ('skill_update', '编辑技能'),
        ('skill_delete', '删除技能'),
        ('skill_rollback', '回滚技能版本'),
        ('skill_risk_ack', '技能包高危内容确认放行'),
        ('skill_attachment_types_update', '修改技能包附件类型'),
        ('tool_call', '工具调用'),
        ('tool_failed', '工具调用失败或被拒'),
        ('trial_tool_call', '试聊工具调用'),
        ('tool_body_view', '查看工具调用全文'),
        ('sensitive_reveal', '展开查看敏感内容'),
        ('proxy_create', '创建代理'),
        ('proxy_update', '编辑代理'),
        ('proxy_delete', '删除代理'),
        ('proxy_test', '测试代理连通性'),
        ('proxy_bind', '更新代理绑定'),
        ('proxy_apply', '应用 SearXNG 代理配置'),
        ('tiangong_switches_update', '更新天工开关'),
        ('tiangong_run_started', '开始天工采集'),
        ('tiangong_run_finished', '结束天工采集'),
        ('tiangong_source_create', '创建天工来源'),
        ('tiangong_source_update', '编辑天工来源'),
        ('tiangong_source_delete', '删除天工来源'),
        ('tiangong_source_check', '检查天工来源可达性'),
        ('tiangong_source_proposal_approve', '批准候选来源'),
        ('tiangong_source_proposal_reject', '拒绝候选来源'),
        ('tiangong_search_searxng_call', 'SearXNG 搜索调用'),
        ('tiangong_search_mcp_call', 'MCP 搜索调用'),
        ('tiangong_search_binding_create', '创建 MCP 搜索绑定'),
        ('tiangong_search_binding_update', '编辑 MCP 搜索绑定'),
        ('tiangong_search_binding_delete', '删除 MCP 搜索绑定'),
        ('tiangong_search_binding_test', '测试 MCP 搜索绑定'),
        ('tiangong_search_settings_update', '更新天工搜索设置'),
    ]

    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='操作人',
    )
    # 操作人用户名快照：即使账号后续被物理清理（actor 被置空），
    # 审计仍能显示是谁做的，不依赖用户行是否还在。
    actor_name = models.CharField(
        '操作人用户名快照', max_length=150, blank=True, default='', db_index=True,
    )
    action = models.CharField('操作类型', max_length=32, choices=ACTION_CHOICES)
    target = models.CharField('操作对象', max_length=200)
    detail = models.TextField('详情', blank=True, default='')
    # 认证事件的全链路来源信息；非 HTTP 触发的系统事件留空。
    # ip 只在能解析成合法地址时写入，避免 PostgreSQL inet 校验失败。
    ip = models.GenericIPAddressField('客户端 IP', null=True, blank=True)
    user_agent = models.CharField('User-Agent', max_length=512, blank=True, default='')
    # F4：该条留痕是否走外网出口（工具调用按此互斥归入「出网调用」或「内网工具调用」）
    egress = models.BooleanField('走外网出口', default=False, db_index=True)
    # F4 完整版：工具调用按智能体 / 工具来源 / 部门三维度统计所依赖的快照字段。
    # 智能体与来源用名字快照，配置改名或删除后历史统计仍可读；
    # 部门维度沿用 actor.department 实时口径（与 P0 用量页一致）。
    agent_name = models.CharField(
        '智能体名称快照', max_length=200, blank=True, default='', db_index=True,
    )
    source_name = models.CharField(
        '工具来源名称快照', max_length=200, blank=True, default='', db_index=True,
    )
    elapsed_ms = models.PositiveIntegerField('工具调用耗时(毫秒)', null=True, blank=True)
    created_at = models.DateTimeField('操作时间', auto_now_add=True)

    class Meta:
        db_table = 'ask_audit_logs'
        verbose_name = '操作日志'
        verbose_name_plural = '操作日志'
        ordering = ['-created_at']

    def __str__(self):
        actor_name = self.actor_name or (self.actor.username if self.actor else '系统')
        return f'{actor_name} {self.get_action_display()} {self.target}'


def log_audit(actor, action, target, detail='', egress=False, ip=None, user_agent='',
              agent_name='', source_name='', elapsed_ms=None):
    """便捷写入审计日志

    ip / user_agent 由调用方从请求上下文提取后传入；认证类事件必须带上，
    普通管理操作可留空（保持既有行为不变）。
    agent_name / source_name / elapsed_ms 仅工具调用留痕使用（F4 三维度统计）。
    """
    return AuditLog.objects.create(
        actor=actor,
        actor_name=(getattr(actor, 'username', '') or '')[:150],
        action=action,
        target=str(target)[:200],
        detail=detail,
        egress=bool(egress),
        ip=ip or None,
        user_agent=(user_agent or '')[:512],
        agent_name=(agent_name or '')[:200],
        source_name=(source_name or '')[:200],
        elapsed_ms=elapsed_ms,
    )
