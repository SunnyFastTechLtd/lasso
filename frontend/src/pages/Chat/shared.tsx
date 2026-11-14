import type { ChatMessage, ChatSource, GeneratedFile, PermissionRequest, ToolCallInfo } from '@/types/chat'

/** 助手消息统一视图：流式过程与历史回看共用一套渲染 */
export interface AssistantView {
  content: string
  thinking: string
  tools: ToolCallInfo[]
  sources: ChatSource[]
  degrade: string[]
  generatedFiles: GeneratedFile[]
  error: string | null
  stopped: boolean
  streaming: boolean
  /** D2：已被重新生成取代的旧版本，折叠留档供对比 */
  superseded: boolean
  /** D7：当前等待中的授权请求 */
  permission: PermissionRequest | null
  /** D7：当前授权请求的收口状态 */
  permissionDecision: string | null
  /** D7：历史留档的授权决定，用于解释“工具未获授权” */
  permissionNotes: { tool: string; decision: string }[]
  /** D7：授权等待期间被刷新/断线打断 */
  permissionInterrupted: boolean
  /** D8：本轮实际带入的偏好条数 */
  preferences: number
}

export const emptyView = (): AssistantView => ({
  content: '',
  thinking: '',
  tools: [],
  sources: [],
  degrade: [],
  generatedFiles: [],
  error: null,
  stopped: false,
  streaming: false,
  superseded: false,
  permission: null,
  permissionDecision: null,
  permissionNotes: [],
  permissionInterrupted: false,
  preferences: 0,
})

/** 降级说明兜底：后端新契约是字符串，历史消息里可能还残留结构化对象 */
export const noteToText = (note: unknown): string => {
  if (typeof note === 'string') return note
  if (note && typeof note === 'object') {
    const n = note as { name?: string; reason?: string }
    return [n.name, n.reason].filter(Boolean).join('：') || JSON.stringify(note)
  }
  return String(note)
}

/** 历史消息：工具过程不随消息落库（审计已留痕），回看呈现正文+引用+降级 */
export function viewFromMessage(msg: ChatMessage): AssistantView {
  const meta = msg.meta ?? {}
  return {
    ...emptyView(),
    content: msg.content,
    sources: meta.sources ?? [],
    degrade: (meta.degrade ?? []).map(noteToText),
    generatedFiles: meta.generated_files ?? [],
    stopped: !!meta.stopped,
    superseded: !!meta.superseded,
    permissionNotes: meta.permission ?? [],
    permissionInterrupted: !!meta.permission_interrupted,
    preferences: meta.preferences ?? 0,
  }
}



export const fmtElapsed = (sec: number | null): string =>
  sec === null ? '' : sec >= 1 ? `${sec.toFixed(1)}s` : `${Math.round(sec * 1000)}ms`

/** 会话标题兜底（后端首条消息后会改写标题，切换瞬间可能仍是默认值） */
export const convTitle = (t: string | null | undefined) => (t && t.trim() ? t : '新对话')
