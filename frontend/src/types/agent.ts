/** 智能体装配（A1–A8）：与 backend/apps/agenthub 序列化器逐字段对齐 */

export type AgentStatus = 'draft' | 'published' | 'disabled' | 'archived'
export type AgentVisibility = 'private' | 'department' | 'public' | 'admin_only'
export type SpeechTier = 'low' | 'mid' | 'high'
export type RetrievalTier = 'few' | 'standard' | 'many'
export type KnowledgeMode = 'on_demand' | 'always'
export type PermissionTier = 'ask' | 'whitelist' | 'auto'

/** A1 列表行：不含提示词正文，带挂载摘要与最近修改信息 */
export interface AgentListItem {
  id: number
  name: string
  slug: string
  description: string
  job_tags: string[]
  icon: string
  status: AgentStatus
  status_display: string
  visibility: AgentVisibility
  visibility_display: string
  model_name: string
  speech_tier: SpeechTier
  retrieval_tier: RetrievalTier
  permission_tier: PermissionTier
  mount_summary: { mcp_servers: number; skills: number; knowledge_bases: number }
  is_builtin: boolean
  is_customized: boolean
  current_version: number
  published_at: string | null
  /** 改了还没重新发布（A7/7.3 标记） */
  pending_changes: boolean
  created_by_name: string
  updated_by_name: string
  created_at: string
  updated_at: string
}

/** 详情/编辑回填：全量配置 + 挂载明细 + 工具冲突 */
export interface Agent {
  id: number
  name: string
  slug: string
  description: string
  job_tags: string[]
  icon: string
  model_name: string
  speech_tier: SpeechTier
  speech_tier_display: string
  retrieval_tier: RetrievalTier
  retrieval_tier_display: string
  /** E3 检索调用策略：按需查（默认）/ 每轮必查 */
  knowledge_mode: KnowledgeMode
  knowledge_mode_display: string
  /** E3 追问补全 */
  followup_rewrite: boolean
  enable_thinking: boolean
  prompt: string
  /** D8：管理员预置的跨会话偏好，使用者可逐条关闭 */
  default_preferences: string[]
  placeholders: { token: string; desc: string }[]
  speech_params: { temperature: number; top_p: number }
  max_tool_rounds: number
  mcp_mounts: {
    server: number
    server_name: string
    server_slug: string
    server_status: string
    server_status_display: string
    warning: string
    tool_names: string[]
    tools: { name: string; full_name: string; display_name: string }[]
  }[]
  skill_items: {
    id: number
    name: string
    slug: string
    description: string
    status: string
    status_display: string
    requires: string[]
  }[]
  knowledge_bases: { id: number; name: string; document_count: number | null; missing: boolean }[]
  builtin_capabilities: string[]
  /** A4：不同来源同名工具冲突标注 */
  tool_conflicts: { tool: string; owners: string[] }[]
  mount_summary: { mcp_servers: number; skills: number; knowledge_bases: number }
  permission_tier: PermissionTier
  permission_tier_display: string
  allow_tool_names: string[]
  visibility: AgentVisibility
  visibility_display: string
  allowed_department_ids: number[]
  allowed_departments: string[]
  include_sub_departments: boolean
  status: AgentStatus
  status_display: string
  current_version: number
  pending_changes: boolean
  is_builtin: boolean
  is_customized: boolean
  owner_department_name: string
  created_by_name: string
  updated_by_name: string
  published_by_name: string
  published_at: string | null
  created_at: string
  updated_at: string
}

/** A2/A3–A6 写入面：挂载四分区整体覆盖；allow_tool_names 省略＝与勾选工具一致 */
export interface AgentPayload {
  name: string
  description?: string
  job_tags?: string[]
  icon?: string
  model_name?: string
  speech_tier?: SpeechTier
  retrieval_tier?: RetrievalTier
  knowledge_mode?: KnowledgeMode
  followup_rewrite?: boolean
  enable_thinking?: boolean
  prompt?: string
  /** D8：默认偏好（预置），最多 8 条、单条 100 字 */
  default_preferences?: string[]
  mcp_mounts?: { server: number; tool_names: string[] }[]
  skill_ids?: number[]
  knowledge_base_ids?: number[]
  builtin_capabilities?: string[]
  permission_tier?: PermissionTier
  allow_tool_names?: string[]
  visibility?: AgentVisibility
  allowed_department_ids?: number[]
  include_sub_departments?: boolean
}

/** GET /api/agents/options/ */
export interface AgentOptions {
  models: string[]
  speech_tiers: { value: SpeechTier; label: string }[]
  retrieval_tiers: { value: RetrievalTier; label: string; max_rounds: number }[]
  permission_tiers: {
    value: PermissionTier
    label: string
    selectable: boolean
    mode: string
    desc: string
  }[]
  builtin_capabilities: { value: string; label: string }[]
  placeholders: { token: string; desc: string }[]
  visibilities: { value: AgentVisibility; label: string; desc?: string; selectable?: boolean; reason?: string }[]
  statuses: { value: AgentStatus; label: string }[]
  prompt_required: string
}

/** A4 装配选择器：来源可展开逐个勾工具、灰置带原因 */
export interface MountOptions {
  mcp_servers: {
    id: number
    name: string
    slug: string
    status: string
    status_display: string
    is_builtin: boolean
    verified: boolean
    warning: string
    used_by_agents: number
    checked: boolean
    tools: {
      name: string
      full_name: string
      display_name: string
      description: string
      enabled: boolean
      state: string
      selectable: boolean
      reason: string
      checked: boolean
    }[]
  }[]
  skills: {
    id: number
    name: string
    slug: string
    description: string
    status: string
    status_display: string
    selectable: boolean
    reason: string
    checked: boolean
    requires: string[]
  }[]
  knowledge_bases: {
    id: number
    name: string
    document_count: number
    empty: boolean
    checked: boolean
  }[]
  builtin_capabilities: { value: string; label: string; checked: boolean }[]
  conflicts: { tool: string; owners: string[] }[]
}

/** A7 发布前检查：三档结论逐项 + 差异视图 */
export interface CheckItem {
  level: 'blocking' | 'warning' | 'notice'
  code: string
  section: string
  section_title: string
  label: string
  field: string | null
  message: string
}

export type DiffItem =
  | {
      field: string
      label: string
      kind: 'text'
      old_summary: string
      old_length: number
      new_summary: string
      new_length: number
      delta: number
    }
  | { field: string; label: string; kind: 'list'; added: string[]; removed: string[]; detail: string }
  | { field: string; label: string; kind: 'bool'; old: string; new: string }
  | {
      field: string
      label: string
      kind: 'value'
      old: string | null
      new: string | null
      old_display: string | null
      new_display: string | null
    }
  | {
      field: string
      label: string
      kind: 'added' | 'removed'
      server: string
      tools: string[]
      detail: string
    }
  | {
      field: string
      label: string
      kind: 'tools'
      server: string
      added: string[]
      removed: string[]
      detail: string
    }

export interface AgentDiff {
  first_publish: boolean
  no_change: boolean
  base_version?: number
  sections: { key: string; title: string; items: DiffItem[] }[]
  summary: string
}

export interface PublishCheck {
  agent_id: number
  status: 'blocked' | 'ack_required' | 'pass'
  can_publish: boolean
  requires_ack: boolean
  first_publish: boolean
  base_version: number
  next_version: number
  blocking: CheckItem[]
  warnings: CheckItem[]
  notices: CheckItem[]
  counts: { blocking: number; warnings: number; notices: number }
  diff: AgentDiff
}

/** 发布接口响应 / 422·409 错误体（均内嵌同一份 check） */
export interface PublishResponse {
  detail: string
  version?: AgentVersion
  diff?: AgentDiff
  check: PublishCheck
  agent?: Agent
  code?: string
  ack_field?: string
}

/** A8 发布历史 */
export interface AgentVersion {
  version: number
  published_by_name: string
  created_at: string
  change_summary: string
  diff: Record<string, Record<string, { old: unknown; new: unknown }>>
}

export interface AgentVersionDetail extends AgentVersion {
  snapshot: {
    name: string
    slug: string
    description: string
    job_tags: string[]
    icon: string
    model_name: string
    speech_tier: string
    retrieval_tier: string
    /** E3（旧版本快照可能没有） */
    knowledge_mode?: KnowledgeMode
    followup_rewrite?: boolean
    enable_thinking: boolean
    prompt: string
    /** D8（旧版本快照可能没有） */
    default_preferences?: string[]
    mcp_servers: { server: string; server_name: string; tool_names: string[] }[]
    skills: string[]
    knowledge_base_ids: number[]
    builtin_capabilities: string[]
    permission_tier: string
    allow_tool_names: string[]
    visibility: string
    allowed_departments: string[]
    include_sub_departments: boolean
  }
}

/** A8 历史快照里引用对象的当前可用性（部门名/来源 slug/技能 slug 无法可靠还原） */
export interface SnapshotDependency {
  level: 'blocking' | 'warning'
  code: string
  section: string
  message: string
  field?: string | null
  source: 'snapshot'
  version?: number
}

/** A8-1 任选两版本对比（base 为旧、target 为新） */
export interface AgentVersionCompare {
  agent_id?: number
  base_version: number
  target_version: number
  left_version: number
  right_version: number
  no_change: boolean
  sections: { key: string; title: string; items: DiffItem[] }[]
  summary: string
  dependencies: SnapshotDependency[]
  unavailable_count: number
  warning_count: number
}

/** A8-2 回滚成功响应 / 422 错误体 */
export interface RollbackResponse {
  detail: string
  version?: AgentVersion
  source_version?: number
  diff?: AgentDiff
  check?: PublishCheck
  dependencies?: SnapshotDependency[]
  blocking?: SnapshotDependency[]
  warnings?: SnapshotDependency[]
  agent?: Agent
  code?: string
  ack_field?: string
}

/** A1 顶部统计与空状态引导 */
export interface AgentSummary {
  draft: number
  published: number
  disabled: number
  archived: number
  total: number
  builtin: number
  empty: boolean
  guide?: string
}

/** DELETE 已发布/已停用 409 冲突体 */
export interface AgentDeleteConflictBody {
  detail: string
  code: 'agent_not_archived'
  options: string[]
}

export type AgentListParams = {
  page?: number
  page_size?: number
  search?: string
  status?: string
  visibility?: string
  job_tag?: string
  /** D1 对话页选择器：只列已发布（可见范围由后端按用户过滤） */
  only_user_side?: 0 | 1
}

/** 跨环境导出包（version=1）：不含任何密钥，引用对象按 slug/名称导出 */
export interface AgentExportPackage {
  format: string
  version: number
  exported_at: string
  source?: { agent?: string; slug?: string; status?: string; current_version?: number }
  agent: {
    name: string
    description?: string
    model_name?: string
    [key: string]: unknown
  }
  note?: string
}

/** 导入依赖检查项：blocking 必须修正，warning 可继续但会被跳过/降级 */
export interface AgentImportIssue {
  level: 'blocking' | 'warning'
  code: string
  field: string
  message: string
}

export interface AgentImportPreview {
  blocking: AgentImportIssue[]
  warnings: AgentImportIssue[]
  summary: {
    agent_name?: string
    description?: string
    model_name?: string
    visibility?: AgentVisibility
    source?: { agent?: string; slug?: string; status?: string; current_version?: number }
    counts?: { mcp_servers: number; skills: number; knowledge_bases: number; departments: number }
  }
}

export type AgentImportStatus = 'pending' | 'approved' | 'rejected'

/** 导入申请（单级审批）：提交不生效，另一名管理员通过后才创建草稿 */
export interface AgentImportRequest {
  id: number
  agent_name: string
  description: string
  status: AgentImportStatus
  status_display: string
  warnings: AgentImportIssue[]
  result: {
    agent_id?: number
    agent_name?: string
    agent_slug?: string
    warnings?: AgentImportIssue[]
  }
  submitted_by: number | null
  submitted_by_name: string
  submitted_at: string
  reviewed_by_name: string
  reviewed_at: string | null
  review_note: string
  package?: AgentExportPackage
}

export interface AgentImportSummary {
  pending: number
  approved: number
  rejected: number
  total: number
  /** 管理员可审批数量（排除本人提交） */
  actionable: number
}

/** 提交导入申请的响应 */
export interface AgentImportSubmitResult {
  detail: string
  preview: AgentImportPreview
  request: AgentImportRequest
}
