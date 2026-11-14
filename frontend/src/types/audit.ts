/** GET /api/auth/audit-logs/ 行（AuditLogSerializer） */
export interface AuditLogItem {
  id: number
  actor: number | null
  actor_name: string
  action: string
  action_display: string
  target: string
  detail: string
  /** 认证事件采集的客户端来源；非 HTTP 事件为空 */
  ip: string | null
  user_agent: string
  created_at: string
  /** B3：是否存在可查看的工具调用全文（后端按 Exists() 注解，不逐行查全文表） */
  has_body: boolean
}

/** 后端过滤参数名固定为 actor / action / target / date_start / date_end */
export interface AuditListParams {
  page: number
  page_size: number
  actor?: string
  action?: string
  target?: string
  /** YYYY-MM-DD，含当日 */
  date_start?: string
  date_end?: string
}

/** GET /api/auth/audit-logs/{id}/body/ —— B3 契约（ToolCallBody，90 天保留） */
export interface ApiToolCallBody {
  id: number
  /** 试聊不产生会话，此处为 null */
  conversation_id: number | null
  call_id: string
  tool: string
  /** ToolResultState：success / error / interrupted / denied / running */
  state: string
  egress: boolean
  created_at: string
  /** 入库全文（超过 256KB 时为截存后的前缀） */
  result: string
  /** 原始字数（截存前） */
  result_chars: number
  result_sha256: string
  storage_truncated: boolean
}
