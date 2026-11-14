/** MCP 工具来源（B1–B8）：与 backend/apps/mcp 序列化器逐字段对齐 */

export type McpTransport = 'http_mcp' | 'stdio'
export type McpStatus = 'unknown' | 'healthy' | 'error' | 'disabled' | 'egress_blocked'
export type McpLifecycle = 'persistent' | 'per_session' | 'ephemeral'
export type McpVisibility = 'private' | 'department' | 'public' | 'admin_only'
export type McpToolState = 'present' | 'missing'

/** 入参摘要（services.params_summary 口径） */
export interface ToolParam {
  name: string
  type: string
  required: boolean
  description: string
}

/** 来源级 discovered_tools 快照 / 测试结果里的工具项 */
export interface DiscoveredTool {
  name: string
  description: string
  params: ToolParam[]
  input_schema?: Record<string, unknown>
}

/** MCPTool（B4） */
export interface McpToolItem {
  name: string
  full_name: string
  description: string
  input_schema: Record<string, unknown>
  params: ToolParam[]
  enabled: boolean
  state: McpToolState
  state_display: string
  first_seen_at: string | null
  last_seen_at: string | null
}

/** 密钥分组（只回掩码，后 4 位可见） */
export interface SecretsMasked {
  headers?: Record<string, string>
  env?: Record<string, string>
}

export interface McpServer {
  id: number
  name: string
  slug: string
  description: string
  transport: McpTransport
  transport_display: string
  url: string
  headers: Record<string, string>
  egress_domains: string[]
  command: string
  args: string[]
  cwd: string
  secrets_masked: SecretsMasked
  has_secrets: boolean
  is_stateful: boolean
  lifecycle: McpLifecycle
  lifecycle_display: string
  timeout_seconds: number | null
  effective_timeout: number
  timeout_default: number
  enabled_tools: string[]
  discovered_tools: DiscoveredTool[]
  tool_count: number
  enabled_tool_count: number
  new_tool_count: number
  agent_count: number
  status: McpStatus
  status_display: string
  last_check_at: string | null
  last_error: string
  requires_egress: boolean
  is_builtin: boolean
  is_active: boolean
  visibility: McpVisibility
  allowed_departments: string[]
  created_by_name: string
  created_at: string
  updated_at: string
}

/** 写请求载荷（secrets 整值替换；掩码值＝未修改） */
export interface McpServerPayload {
  name: string
  description: string
  transport: McpTransport
  url?: string
  headers?: Record<string, string>
  egress_domains?: string[]
  command?: string
  args?: string[]
  cwd?: string
  is_stateful?: boolean
  lifecycle?: McpLifecycle
  timeout_seconds?: number | null
  visibility: McpVisibility
  allowed_department_ids?: number[]
  secrets?: { headers: Record<string, string>; env: Record<string, string> }
}

/** B3 连通性测试结果 */
export interface ConnectivityTest {
  status: McpStatus
  ok: boolean
  elapsed_ms: number
  timeout_seconds: number
  tools: DiscoveredTool[]
  new_tools: string[]
  missing_tools: string[]
  tools_stale: boolean
  managed_runtime_ready: boolean
  managed_runtime_note: string
  failure_category: string
  failure_label: string
  failure_message: string
  detail: string
}

/** POST …/test/ 响应：结果 + 最新 server */
export interface TestResult extends ConnectivityTest {
  server: McpServer
}

/** B6 引用检查 */
export interface ServerReference {
  id: number
  name: string
  status: string
  tool_names: string[]
}

export interface ReferencesResponse {
  count: number
  references: ServerReference[]
}

/** DELETE 409 冲突体 */
export interface DeleteConflictBody {
  detail: string
  code: 'mcp_server_referenced'
  references: ServerReference[]
  options: string[]
}

/** B2 命令前缀白名单（内置只读） */
export interface CommandPrefixes {
  editable: boolean
  policy: string
  prefixes: { prefix: string; usage: string }[]
}

/** B8 出网总开关 */
export interface EgressState {
  enabled: boolean
  /** 严格域名白名单：关闭时沿用原有行为（只看总开关） */
  strict_domains?: boolean
  blocked_count?: number
  note?: string
  detail?: string
}

/** 装配页选择器 */
export interface MountableServer {
  id: number
  name: string
  slug: string
  status: McpStatus
  is_builtin: boolean
  requires_egress: boolean
  warning: string
  tools: { name: string; full_name: string; description: string }[]
}

/** 跨环境导出体：只含非敏感字段，密钥需在目标环境重新填写 */
export interface McpExportRow {
  name: string
  description: string
  transport: McpTransport
  url: string
  headers: Record<string, string>
  command: string
  args: string[]
  cwd: string
  is_stateful: boolean
  lifecycle: McpLifecycle
  timeout_seconds: number | null
  visibility: McpVisibility
  allowed_departments: string[]
  has_secrets: boolean
  is_builtin: boolean
  egress_domains?: string[]
}

export interface McpExportPayload {
  version: number
  exported_at: string
  count: number
  servers: McpExportRow[]
  note: string
}

export interface McpImportResult {
  created: string[]
  skipped: string[]
  errors: { index: number; name: string; detail: string }[]
  note: string
}

export type McpListParams = {
  page?: number
  page_size?: number
  search?: string
  status?: string
  transport?: string
}
