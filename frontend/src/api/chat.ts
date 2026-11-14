import { http } from './request'
import { ApiError } from './request'
import { APP_CONFIG, STORAGE_KEYS } from '@/config/app'
import { useUserStore } from '@/store/user'
import type { Paginated } from '@/types/common'
import type {
  ChatAttachment,
  ChatMessage,
  ChatRegeneratePayload,
  ChatSendPayload,
  ChatSseEvent,
  Conversation,
  PermissionDecision,
  TrialChatPayload,
  TrialSseEvent,
} from '@/types/chat'

const base = '/chat'

function authHeader(): Record<string, string> {
  const token = useUserStore.getState().token ?? localStorage.getItem(STORAGE_KEYS.TOKEN)
  return token ? { Authorization: `Bearer ${token}` } : {}
}

/** D7：提交会话内授权决定；超时返回 410，重复点击返回 409 */
export const decidePermission = (request_id: string, decision: PermissionDecision) =>
  http.post<{ code: string }>(`${base}/chat/permission/`, { request_id, decision })

/** D8 单条跨会话偏好：id 为 null 表示尚未落库的智能体预置项 */
export interface PreferenceItem {
  id: number | null
  content: string
  source: 'user' | 'preset'
  muted: boolean
  /** 超过 90 天没被带入：界面置灰提示，不自动删除 */
  stale: boolean
  last_used_at: string | null
}

export interface PreferenceGroup {
  agent: { id: number; name: string }
  items: PreferenceItem[]
}

export interface PreferenceList {
  max_items: number
  max_chars: number
  stale_days: number
  groups: PreferenceGroup[]
}

export const preferenceApi = {
  list: () => http.get<PreferenceList>(`${base}/preferences/`),

  create: (agent_id: number, content: string) =>
    http.post<{ id: number; content: string; source: string; muted: boolean }>(
      `${base}/preferences/`, { agent_id, content }),

  /** 自建项直接删除；预置项由后端改为 muted，可再恢复 */
  remove: (id: number) =>
    http.delete<{ muted: boolean; deleted: boolean }>(`${base}/preferences/${id}/`),

  restore: (id: number) =>
    http.patch<{ id: number; muted: boolean; source: string }>(
      `${base}/preferences/${id}/`, { muted: false }),

  /** 没有行记录的预置项：按内容关闭/恢复 */
  setPreset: (agent_id: number, content: string, muted: boolean) =>
    http.post<{ id: number; muted: boolean; source: string }>(
      `${base}/preferences/preset/`, { agent_id, content, muted }),

  suggest: (conversation_id: number, message_id?: number) =>
    http.post<{ suggestion: string; agent_id: number }>(
      `${base}/preferences/suggest/`, { conversation_id, message_id }),
}

export const chatApi = {
  listConversations: (params: { page?: number; page_size?: number; type?: string }) =>
    http.get<Paginated<Conversation>>(`${base}/conversations/`, params),

  updateConversation: (id: number, payload: { title?: string; use_preferences?: boolean }) =>
    http.patch<Conversation>(`${base}/conversations/${id}/`, payload),

  removeConversation: (id: number) => http.delete<void>(`${base}/conversations/${id}/`),

  messages: (id: number, params: { page?: number; page_size?: number }) =>
    http.get<Paginated<ChatMessage>>(`${base}/conversations/${id}/messages/`, params),

  /** P1-c：切换助手 = 新建会话承接当前历史，旧会话转为只读 */
  switchAgent: (id: number, agentId: number) =>
    http.post<Conversation>(`${base}/conversations/${id}/switch-agent/`, { agent_id: agentId }),

  /** P1-c：切换入口是否展示由后端开关决定（默认关闭） */
  agentSwitchPolicy: () =>
    http.get<{ enabled: boolean }>(`${base}/conversations/agent-switch-policy/`),

  modelHealth: () => http.get<{ status: string; error?: string }>(`${base}/models/health/`),

  modelConfig: () =>
    http.get<{ base_url_display: string; model: string; is_local: boolean }>(`${base}/models/config/`),

  /**
   * D5 会话内临时附件：只提取正文供本轮提问使用，不入知识库、不向量化。
   * 返回 {name,size,type,text,truncated}，发送时原样带回即可。
   * 超限由后端 413 给出原因（单文件 10MB），前端不再重复校验大小。
   */
  uploadAttachment: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return http.upload<ChatAttachment>(`${base}/chat/upload_attachment/`, fd)
  },

  /** D7：提交会话内授权决定；超时返回 410，重复点击返回 409 */
  decidePermission,
}

/**
 * SSE 流式对话：EventSource 只支持 GET，这里用 fetch + ReadableStream。
 * 帧格式（后端 format_sse_event）：`event: NAME\ndata: <JSON 单行>\n\n`。
 * onEvent 逐事件回调；网络断开/主动 abort 走 finally，不做静默吞错。
 */
export async function streamChat(
  payload: ChatSendPayload,
  onEvent: (ev: ChatSseEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  return runStream(`${base}/chat/send/`, payload, onEvent, signal)
}

/** D2 重新生成：打 chat/regenerate/，事件流与 send 完全一致 */
export async function regenerateChat(
  payload: ChatRegeneratePayload,
  onEvent: (ev: ChatSseEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  return runStream(`${base}/chat/regenerate/`, payload, onEvent, signal)
}

/**
 * A3 试聊：用智能体最新保存（未必发布）的配置起一轮临时会话。
 * 服务端零落库，多轮上下文由面板每轮回传；工具调用以「试聊」标记进审计。
 */
export async function streamTrial(
  payload: TrialChatPayload,
  onEvent: (ev: TrialSseEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  return runStream(`${base}/chat/trial/`, payload, onEvent as (ev: ChatSseEvent) => void, signal)
}

async function runStream(
  path: string,
  body: unknown,
  onEvent: (ev: ChatSseEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  let res: Response
  try {
    res = await fetch(`${APP_CONFIG.apiBaseUrl}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeader() },
      body: JSON.stringify(body),
      signal,
    })
  } catch (e) {
    if ((e as Error).name === 'AbortError') return
    throw new ApiError(0, '无法连接对话服务，请检查后端是否运行', null, null)
  }

  if (res.status === 401) {
    // fetch 绕过 axios 拦截器：手动对齐"过期即回登录页"的全局策略
    useUserStore.getState().logout()
    if (window.location.pathname !== '/login') window.location.href = '/login'
    throw new ApiError(401, '登录已过期，请重新登录', null, null)
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null)
    throw new ApiError(res.status, String(body?.detail ?? `请求失败（${res.status}）`), body?.code ?? null, body)
  }
  if (!res.body) throw new ApiError(0, '浏览器不支持流式响应', null, null)

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let idx: number
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx)
        buf = buf.slice(idx + 2)
        const ev = parseFrame(frame)
        if (ev) onEvent(ev)
      }
    }
  } catch (e) {
    // 停止生成/切页会中断读流（Chrome 报 BodyStreamBuffer was aborted），
    // 属主动取消而非错误，静默返回，由调用方按 aborted 状态收尾
    if ((e as Error).name === 'AbortError' || /abort/i.test((e as Error).message)) return
    throw new ApiError(0, '连接中断，请稍后重试', null, null)
  }
}

function parseFrame(frame: string): ChatSseEvent | null {
  let event = ''
  let data = ''
  for (const line of frame.split('\n')) {
    if (line.startsWith('event: ')) event = line.slice(7).trim()
    else if (line.startsWith('data: ')) data = line.slice(6)
  }
  if (!event) return null
  let parsed: unknown = data
  try {
    parsed = JSON.parse(data)
  } catch {
    /* data 本身可能就是未加引号的纯文本 */
  }
  return { event, data: parsed } as ChatSseEvent
}

/** 产物文件下载：/chat/files/{id}/download/ 需带 JWT 头，走 blob 转存 */
export async function downloadGeneratedFile(url: string, name: string): Promise<void> {
  const res = await fetch(`${APP_CONFIG.apiBaseUrl}${url}`, { headers: authHeader() })
  if (!res.ok) {
    const body = await res.json().catch(() => null)
    throw new ApiError(res.status, String(body?.detail ?? '文件下载失败'), null, body)
  }
  const blob = await res.blob()
  const objUrl = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = objUrl
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(objUrl)
}
