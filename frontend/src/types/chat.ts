/** 对话工作台（D1-D6）：与 backend/apps/chat 序列化器 / SSE 事件逐字段对齐 */

export interface Conversation {
  id: number
  title: string
  type: 'chat' | 'knowledge'
  resource_id: number | null
  knowledge_base_ids: number[]
  /** D1：绑定的智能体定义；null = 平台通用助手 */
  agent: number | null
  agent_name: string
  /** D1：已停用/已归档的助手，历史会话只读 */
  agent_status: string
  /** D8：本会话是否带入跨会话偏好；用户可临时关闭 */
  use_preferences: boolean
  /** P1-c：切换来源会话（仅用于展示，不参与绑定判断） */
  switched_from: number | null
  switched_from_title: string
  /** P1-c：已被新会话承接，历史只读 */
  is_readonly: boolean
  message_count: number
  last_message_at: string
  created_at: string
  updated_at: string
}

export interface ChatMessage {
  id: number
  role: 'user' | 'assistant' | 'system'
  content: string
  meta: MessageMeta
  attachments: ChatAttachment[]
  created_at: string
}

/** D5 会话内临时附件：上传接口原样返回、随本轮消息落库，不入知识库、不向量化 */
export interface ChatAttachment {
  name: string
  size: number
  type: string
  /** 后端提取出的正文（超过 6000 字会被截断） */
  text: string
  truncated?: boolean
}

export interface MessageMeta {
  sources?: ChatSource[]
  /** D6 降级说明（随消息留档，历史回看可见） */
  degrade?: string[]
  generated_files?: GeneratedFile[]
  /** 停止生成时落库的部分回答 */
  stopped?: boolean
  /** D2：这一版已被重新生成取代，只留档对比，不进模型上下文 */
  superseded?: boolean
  superseded_by?: number
  /** D2：当前版本替代了哪一条旧答案 */
  regenerates?: number
  /** D7：本轮授权决定留档，用于无正文时解释“工具未获授权” */
  permission?: { tool: string; decision: string }[]
  /** D7：授权等待期间断线/刷新，本次操作未执行 */
  permission_interrupted?: boolean
  /** D8：本轮实际带入的偏好条数；0/缺省＝未带入 */
  preferences?: number
}

/** D7 授权卡片：工具名、来源名、本次输入摘要均由服务端事件给出 */
export interface PermissionRequest {
  request_id: string
  call_id: string
  tool_name: string
  source_name: string
  display: string
  args: string
  timeout_seconds: number
  can_always: boolean
}

export type PermissionDecision = 'allow_once' | 'allow_session' | 'deny'

/** 跨库检索引用来源（start 事件与消息 meta 同构） */
export interface ChatSource {
  knowledge_base_id: number
  knowledge_base_name: string
  title: string
  file_name: string
  score: number
  chunk_id: number
  doc_id: number
  chunk_index: number
  content: string
  /** 页码与 OCR 标记；meta.ocr 为 true 时展示「可能有误差」 */
  meta?: {
    page?: number | null
    ocr?: boolean
    ocr_engine?: string
  }
}

export interface GeneratedFile {
  id: number
  name: string
  type: string
  size: number
  url: string
  is_expired: boolean
}

/** 过程区单条工具调用（D2：来源名·工具名口径，与审计同源） */
export interface ToolCallInfo {
  call_id: string
  name: string
  display: string
  args: string
  state: string
  result: string
  elapsed: number | null
  /** B8-b1：该工具走外网出口，参数摘要已发送到 egress_host */
  egress?: boolean
  egress_host?: string
}

export const TOOL_RUNNING = 'running'

export const toolFailed = (state: string) =>
  ['error', 'interrupted', 'denied'].includes(state)

/** POST /api/chat/chat/send/ 请求体 */
export interface ChatSendPayload {
  message: string
  conversation_id?: number
  /** 不传 = 新建会话用通用助手 / 沿用会话已绑定智能体 */
  agent_id?: number
  /** 不传 = 沿用会话已勾选知识库；列表（含空）= 本次显式设置 */
  knowledge_base_ids?: number[]
  /** D5 本轮临时附件：元素即上传接口的响应，原样回传，不入知识库 */
  attachments?: ChatAttachment[]
  stream: true
}

/** POST /api/chat/chat/regenerate/ 请求体（D2：重出最新一版回答，旧版留档） */
export interface ChatRegeneratePayload {
  conversation_id: number
  /** 点了哪一版就传哪一版，与服务端最新一版不一致时后端报错而非静默重生成 */
  message_id?: number
}

/** POST /api/chat/chat/trial/ 请求体（A3 试聊：按最新保存配置起临时会话，服务端零落库） */
export interface TrialChatPayload {
  agent_id: number
  message: string
  /** 多轮上下文由前端每轮回传；服务端只保留 user/assistant 最近 20 条 */
  messages: { role: 'user' | 'assistant'; content: string }[]
}

/** 试聊 SSE 事件：start 带 thread_id（无会话可绑），收尾事件名为 done */
export type TrialSseEvent =
  | { event: 'start'; data: { type: 'start'; thread_id: string } }
  | { event: 'degrade'; data: { type: 'degrade'; notes: string[] } }
  | { event: 'thinking'; data: { type: 'thinking'; content: string } }
  | { event: 'chunk'; data: { type: 'chunk'; content: string } }
  | { event: 'tool'; data: { type: 'tool'; call_id: string; name: string; display: string; args: string; egress?: boolean; egress_host?: string } }
  | { event: 'tool_args'; data: { type: 'tool_args'; call_id: string; args: string; egress?: boolean; egress_host?: string } }
  | { event: 'tool_result'; data: { type: 'tool_result'; call_id: string; state: string; result: string; elapsed: number; egress?: boolean; egress_host?: string } }
  | { event: 'permission_request'; data: { type: 'permission_request' } & PermissionRequest }
  | { event: 'permission_result'; data: { type: 'permission_result'; request_id: string; call_id: string; display?: string; decision: string } }
  | { event: 'done'; data: { type: 'done' } }
  | { event: 'error'; data: { type: 'error'; message: string } }

/** SSE 事件（event 名 → data 结构） */
export type ChatSseEvent =
  | { event: 'start'; data: { conversation_id: number; message_id: number; sources: ChatSource[]; preferences?: number } }
  | { event: 'degrade'; data: { type: 'degrade'; notes: string[] } }
  | { event: 'thinking'; data: { type: 'thinking'; content: string } }
  | { event: 'chunk'; data: { content: string } }
  | { event: 'tool'; data: { type: 'tool'; call_id: string; name: string; display: string; args: string; egress?: boolean; egress_host?: string } }
  | { event: 'tool_args'; data: { type: 'tool_args'; call_id: string; name: string; display: string; args: string; egress?: boolean; egress_host?: string } }
  | { event: 'tool_result'; data: { type: 'tool_result'; call_id: string; name: string; display: string; state: string; result: string; args?: string; elapsed: number; egress?: boolean; egress_host?: string } }
  | { event: 'permission_request'; data: { type: 'permission_request' } & PermissionRequest }
  | { event: 'permission_result'; data: { type: 'permission_result'; request_id: string; call_id: string; display?: string; decision: string } }
  | { event: 'end'; data: { message_count?: number; generated_files?: GeneratedFile[]; sources?: ChatSource[] } | '' }
  | { event: 'error'; data: string }
