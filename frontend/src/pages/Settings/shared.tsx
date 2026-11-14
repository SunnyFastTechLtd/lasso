import { Badge } from '@/components/ui/Badge'

/**
 * 与后端 AuditLog.ACTION_CHOICES 逐项对齐（backend/apps/users/models.py），
 * 前端不臆造动作值；列表展示优先用后端返回的 action_display，本表用于筛选与兜底。
 */
export const AUDIT_ACTION_GROUPS: { label: string; options: { value: string; label: string }[] }[] = [
  {
    label: '模型接入',
    options: [
      { value: 'llm_update', label: '更新 LLM 配置' },
      { value: 'llm_reset', label: '重置 LLM 配置' },
    ],
  },
  {
    label: '出网与工具来源',
    options: [
      { value: 'egress_toggle', label: '切换出网总开关' },
      { value: 'mcp_create', label: '创建工具来源' },
      { value: 'mcp_update', label: '编辑工具来源' },
      { value: 'mcp_test', label: '连通性测试' },
      { value: 'mcp_tool_toggle', label: '来源工具启停' },
      { value: 'mcp_disable', label: '停用工具来源' },
      { value: 'mcp_delete', label: '删除工具来源' },
      { value: 'mcp_unmount', label: '摘除挂载' },
      { value: 'mcp_secret_update', label: '变更来源密钥' },
      { value: 'mcp_command_denied', label: '命令被白名单拒绝' },
      { value: 'mcp_egress_denied', label: '出网被出口清单拒绝' },
    ],
  },
  {
    label: '网络代理',
    options: [
      { value: 'proxy_create', label: '创建代理' },
      { value: 'proxy_update', label: '编辑代理' },
      { value: 'proxy_delete', label: '删除代理' },
      { value: 'proxy_test', label: '测试代理连通性' },
      { value: 'proxy_bind', label: '更新代理绑定' },
      { value: 'proxy_apply', label: '应用 SearXNG 代理配置' },
    ],
  },
  {
    label: '智能体',
    options: [
      { value: 'agent_create', label: '创建智能体' },
      { value: 'agent_update', label: '编辑智能体' },
      { value: 'agent_publish', label: '发布智能体' },
      { value: 'agent_disable', label: '停用智能体' },
      { value: 'agent_archive', label: '归档智能体' },
      { value: 'agent_delete', label: '删除智能体' },
      { value: 'agent_restore', label: '智能体恢复草稿' },
      { value: 'agent_visibility', label: '改智能体可见范围' },
      { value: 'agent_mount', label: '智能体挂载变更' },
      { value: 'agent_risk_ack', label: '发布时确认风险项' },
      { value: 'agent_rollback', label: '回滚到历史版本' },
      { value: 'agent_version_diff', label: '版本对比' },
    ],
  },
  {
    label: '技能',
    options: [
      { value: 'skill_upload', label: '上传技能' },
      { value: 'skill_invalid', label: '技能校验失败' },
      { value: 'skill_approve', label: '技能审核通过' },
      { value: 'skill_reject', label: '技能驳回' },
      { value: 'skill_disable', label: '技能停用' },
      { value: 'skill_update', label: '编辑技能' },
      { value: 'skill_delete', label: '删除技能' },
      { value: 'skill_rollback', label: '回滚技能版本' },
      { value: 'skill_risk_ack', label: '技能高危内容确认放行' },
      { value: 'skill_attachment_types_update', label: '修改技能包附件类型' },
    ],
  },
  {
    label: '认证与账号安全',
    options: [
      { value: 'login_success', label: '登录成功' },
      { value: 'login_failed', label: '登录失败' },
      { value: 'logout', label: '登出' },
      { value: 'password_change', label: '修改密码' },
      { value: 'password_reset', label: '管理员重置密码' },
      { value: 'user_enable', label: '启用账号' },
      { value: 'user_disable', label: '停用账号' },
      { value: 'force_logout', label: '强制下线' },
    ],
  },
  {
    label: '账号与部门',
    options: [
      { value: 'user_create', label: '创建用户' },
      { value: 'user_update', label: '修改用户' },
      { value: 'user_delete', label: '删除用户' },
      { value: 'department_create', label: '创建部门' },
      { value: 'department_update', label: '修改部门' },
      { value: 'department_delete', label: '删除部门' },
    ],
  },
  {
    label: '知识与调用留痕',
    options: [
      { value: 'kb_delete', label: '删除知识库' },
      { value: 'tool_call', label: '工具调用' },
      { value: 'tool_failed', label: '工具调用失败或被拒' },
      { value: 'trial_tool_call', label: '试聊工具调用' },
      { value: 'tool_body_view', label: '查看工具调用全文' },
      { value: 'sensitive_reveal', label: '展开查看敏感内容' },
    ],
  },
]

export const AUDIT_ACTION_LABELS: Record<string, string> = AUDIT_ACTION_GROUPS.reduce<Record<string, string>>(
  (acc, group) => {
    group.options.forEach((o) => {
      acc[o.value] = o.label
    })
    return acc
  },
  {},
)

/** 被拒 / 失败类动作：表格里用警示色，避免管理员把「拒绝」读成「正常执行」 */
const WARN_ACTIONS = new Set(['mcp_command_denied', 'mcp_egress_denied', 'skill_invalid', 'skill_reject', 'tool_failed', 'login_failed'])

export function auditActionLabel(action: string, display?: string): string {
  return display || AUDIT_ACTION_LABELS[action] || action
}

export function AuditActionCell({ action, display }: { action: string; display?: string }) {
  const label = auditActionLabel(action, display)
  const warn = WARN_ACTIONS.has(action)
  return (
    <span className="flex min-w-0 flex-col gap-[2px]">
      {warn ? (
        <Badge tone="warning" className="self-start">
          {label}
        </Badge>
      ) : (
        <span className="row-title">{label}</span>
      )}
      <span className="mono text-xs text-muted-foreground">{action}</span>
    </span>
  )
}

/** 健康检查状态中文口径（对应后端 check_health 的 status） */
export const HEALTH_LABEL: Record<string, string> = {
  ok: '连通正常',
  offline: '无法连接',
  unauthorized: '密钥无效',
  not_configured: '尚未配置模型接入',
  error: '检查异常',
}
