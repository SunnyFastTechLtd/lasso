/** 技能规范（C1–C6）：与 backend/apps/skills 序列化器逐字段对齐 */

export type SkillStatus = 'invalid' | 'pending' | 'active' | 'disabled' | 'builtin'
export type SkillSource = 'builtin' | 'upload' | 'repo'
export type SkillVisibility = 'private' | 'department' | 'public' | 'admin_only'
/** C5 生效范围：全员 / 指定部门（与可见范围是两套口径） */
export type SkillScope = 'all' | 'departments'

/** C1 技能包附件（正文之外的文件） */
export interface SkillFileMeta {
  id: number
  path: string
  size: number
  sha256: string
  updated_at: string
  download_url: string
}

/** 决策 10：高危命中项（含规则标识、规则集版本与命中位置） */
export interface SkillRiskHit {
  rule: string
  rule_version?: number
  ruleset_version?: string
  label?: string
  reason: string
  file: string
  line: number
  snippet?: string
}

/** 422 确认体：risk_fingerprint 与提交内容绑定，换内容后必须重新确认 */
export interface RiskAckPayload {
  risk_ack: true
  risk_fingerprint: string
}

export interface SkillRiskAck {
  at: string
  by: string
  ruleset_version: string
  rules: string[]
  files: string[]
}

export interface SkillVersion {
  version: number
  /** 列表接口不返回全文 */
  body?: string
  body_hash: string
  char_count: number
  change_summary: string
  approved_by_name: string
  approved_at: string
  risk_ack: Omit<SkillRiskAck, 'rules' | 'files'> | null
  files: { id: number; path: string; size: number; sha256: string; download_url: string }[]
}

export interface SkillVersionList {
  current_version: number
  versions: SkillVersion[]
}

/** 正文行级差异 + 附件增删改（与 services.compare_versions 逐字段对齐） */
export interface SkillVersionCompare {
  left_version: number
  right_version: number
  body: {
    stats: { added: number; removed: number; unchanged: number }
    rows: { type: 'equal' | 'del' | 'add'; text: string; left_no: number | null; right_no: number | null }[]
  }
  files: {
    added: { path: string; size: number }[]
    removed: { path: string; size: number }[]
    changed: { path: string; old_size: number; new_size: number }[]
    unchanged_count: number
  }
  summary: string
}

export interface Skill {
  id: number
  name: string
  slug: string
  description: string
  source: SkillSource
  source_display: string
  entry_file: string
  status: SkillStatus
  status_display: string
  char_count: number
  has_body: boolean
  /** 详情才有；列表（compact）不含 */
  body?: string
  approved_body?: string
  body_changed: boolean
  validation_errors: string[]
  /** 保存时风险扫描的可疑命中：仅提示不阻断，供审核页查看 */
  scan_warnings: string[]
  reject_reason: string
  requires: string[]
  mountable: boolean
  /** 编辑稿在审、旧审核通过版仍生效 */
  live: boolean
  /** 非内置可编辑 */
  editable: boolean
  visibility: SkillVisibility
  visibility_display: string
  allowed_departments: string[]
  /** C5 生效范围（运行时按部门灰度注入） */
  scope_mode: SkillScope
  scope_mode_display: string
  scope_include_sub_departments: boolean
  applicable_departments: string[]
  /** C5 灰度：基础范围内的放开百分比（0=不放开，100=全量） */
  rollout_percent: number
  /** C5 定向用户白名单（命中即生效，不受部门与比例限制） */
  applicable_users: { id: number; username: string; department: string }[]
  /** C4 当前版本号（0＝尚无审核通过版本） */
  current_version: number
  /** C1 技能包附件 */
  files: SkillFileMeta[]
  file_count: number
  package_bytes: number
  file_scan_warnings: string[]
  risk_ack: SkillRiskAck | null
  agent_count: number
  reviewed_by_name: string
  reviewed_at: string | null
  /** 空串＝未进运行时镜像 */
  mirror_path: string
  created_at: string
  updated_at: string
}

/** 建档/编辑载荷（requires 为工具全称列表） */
export interface SkillPayload {
  name: string
  description: string
  body: string
  requires: string[]
  visibility: SkillVisibility
  allowed_department_ids?: number[]
  /** C5：生效范围（与可见范围独立） */
  scope_mode?: SkillScope
  scope_include_sub_departments?: boolean
  applicable_department_ids?: number[]
  rollout_percent?: number
  applicable_user_ids?: number[]
}

/** 上传建档：multipart file=，name/description 可选覆盖 */
export interface SkillUploadInput {
  file: File
  name?: string
  description?: string
  scope_mode?: SkillScope
  scope_include_sub_departments?: boolean
  applicable_department_ids?: number[]
  rollout_percent?: number
  applicable_user_ids?: number[]
  risk_ack?: true
  risk_fingerprint?: string
}

/** 批量上传附件：paths 与 files 一一对应 */
export interface SkillFilesUploadInput {
  files: File[]
  paths: string[]
  risk_ack?: true
  risk_fingerprint?: string
}

/** C3 审核入参：body_hash＝sha256(正文) */
export interface SkillReviewPayload {
  body_ack: true
  body_hash: string
  reason?: string
}

export interface SkillAgentReference {
  agent_id: number
  agent_name: string
  agent_slug: string
  agent_status: string
  agent_status_display: string
  published_version: number | null
  mounted_at: string
}

export interface SkillReferencesResponse {
  count: number
  references: SkillAgentReference[]
}

/** DELETE 409 冲突体（options 仅为文字口径，无强制删除参数） */
export interface SkillDeleteConflictBody {
  detail: string
  code: 'skill_referenced'
  references: SkillAgentReference[]
  options: string[]
}

export interface SkillSummary {
  counts: {
    invalid: number
    pending: number
    active: number
    disabled: number
    builtin: number
    edited_pending: number
    total: number
  }
  pending: number
  body_limit: number
}

export interface MirrorState {
  root: string
  mirrored: { slug: string; name: string; status: string; path: string; runtime_chars: number }[]
}

export interface MirrorRebuild {
  mirrored: number
  removed: number
  slugs: string[]
  removed_slugs: string[]
}

/** A4 装配页选择器（F4 使用） */
export interface SkillOption {
  id: number
  name: string
  slug: string
  description: string
  status: SkillStatus
  status_display: string
  char_count: number
  source: SkillSource
  mountable: boolean
  reason: string
  live: boolean
  warning: string
  scope_mode: SkillScope
  scope_mode_display: string
  scope_departments: string[]
  /** C5 灰度比例与定向用户（装配页展示用） */
  rollout_percent: number
  scope_users: string[]
  applies_to_me: boolean
}

export type SkillListParams = {
  page?: number
  page_size?: number
  search?: string
  status?: string
  source?: string
}
