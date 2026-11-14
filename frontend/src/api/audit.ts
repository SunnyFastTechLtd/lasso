import { http } from './request'
import type { ApiToolCallBody } from '@/types/audit'

/**
 * 工具调用全文（B3 契约）：
 * - 200：正文 + 元数据，同时后端写入 tool_body_view 访问留痕；
 * - 403：非平台管理员；
 * - 404：历史数据未留存（尚无全文表时期）或超过 90 天已到期清理。
 * 404 两种文案由后端 detail 区分，前端直接展示。
 */
export const getAuditBody = (id: number) =>
  http.get<ApiToolCallBody>(`/auth/audit-logs/${id}/body/`)

/** 展开敏感内容（掩码后的原文）前先记审计：谁、何时、看了哪条 */
export interface SensitiveRevealPayload {
  /** audit：审计页（沿用管理员判定）；chat：会话过程区/试聊（沿用会话归属判定） */
  scope: 'audit' | 'chat'
  /** 被展开的字段：detail（审计详情）、result（工具返回）、args（工具参数） */
  field: 'detail' | 'result' | 'args'
  /** 本次掩码命中处数，仅用于留痕统计 */
  masked_fields?: number
  audit_id?: number
  conversation_id?: number
  call_id?: string
}

export const revealSensitive = (payload: SensitiveRevealPayload) =>
  http.post<{ ok: boolean }>('/auth/audit-logs/sensitive-reveal/', payload)
