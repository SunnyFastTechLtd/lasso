import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Icon } from '@/components/ui/Icon'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { EmptyState } from '@/components/ui/EmptyState'
import { Drawer, Modal } from '@/components/ui/Overlay'
import { Textarea } from '@/components/ui/Field'
import { chatApi, decidePermission as decideChatPermission, downloadGeneratedFile, preferenceApi, regenerateChat, streamChat } from '@/api/chat'
import { useConversations, useMessages, useModelConfig, useModelHealth, useRemoveConversation, useAgentSwitchPolicy, useSwitchAgent } from '@/api/hooks/useChat'
import { useAgents } from '@/api/hooks/useAgents'
import { useKnowledgeBases } from '@/api/hooks/useKnowledge'
import { toast } from '@/store/toast'
import { useChatStatusStore } from '@/store/chatStatus'
import type { ApiError } from '@/api/request'
import type { ChatAttachment, ChatSendPayload, ChatSource, ChatSseEvent, GeneratedFile, PermissionDecision, PermissionRequest, ToolCallInfo } from '@/types/chat'
import { TOOL_RUNNING } from '@/types/chat'
import { ConversationList } from './ConversationList'
import { MessageItem } from './MessageItem'
import { Composer } from './Composer'
import { convTitle, emptyView, noteToText, viewFromMessage, type AssistantView } from './shared'

export default function ChatPage() {
  const qc = useQueryClient()

  const [activeId, setActiveId] = useState<number | null>(null)
  const [agentId, setAgentId] = useState<number | null>(null)
  const [kbIds, setKbIds] = useState<number[]>([])

  const [view, setView] = useState<AssistantView | null>(null)
  /** D5：本轮待展示的用户气泡，附件要和正文一起留到最后一条服务端消息到位 */
  const [pendingUser, setPendingUser] = useState<{ text: string; attachments: ChatAttachment[] } | null>(null)
  const [streaming, setStreaming] = useState(false)
  const [source, setSource] = useState<ChatSource | null>(null)
  /** D2：正在被重生的那一版，流式期间就按「上一版」呈现，避免两版并列看着都像当前答案 */
  const [regenId, setRegenId] = useState<number | null>(null)
  /** D8：会话级偏好开关的提交中状态 */
  const [prefBusy, setPrefBusy] = useState(false)
  /** P1-c：切换助手弹窗 */
  const [switchOpen, setSwitchOpen] = useState(false)
  /** D8：「以后都这样」弹窗：messageId 缺省表示归纳本会话最新一条回答 */
  const [remember, setRemember] = useState<{ open: boolean; messageId?: number; loading: boolean; text: string; saving: boolean }>({
    open: false,
    loading: false,
    text: '',
    saving: false,
  })

  const abortRef = useRef<AbortController | null>(null)
  /** D7：当前待决授权，停止生成时据此尽力回传拒绝 */
  const permissionRef = useRef<PermissionRequest | null>(null)
  const lastPayloadRef = useRef<ChatSendPayload | null>(null)
  const lastRegenRef = useRef<number | null>(null)
  const convIdRef = useRef<number | null>(null)
  const expectedCountRef = useRef<number | null>(null)
  /** 本轮服务端会多几条消息：一问一答 +2，重新生成只 +1 */
  const streamDeltaRef = useRef(2)
  /** D1：新会话是否已应用过默认助手（用户手动改过就不再自动选） */
  const defaultedRef = useRef(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  const convsQ = useConversations()
  const convs = convsQ.data?.results ?? []
  const conv = convs.find((c) => c.id === activeId) ?? null

  const msgsQ = useMessages(activeId)
  const messages = msgsQ.data?.results ?? []

  /* 外壳状态栏「对话」= 当前会话的提问轮次（role=user 的消息数） */
  const rounds = messages.reduce((n, m) => n + (m.role === 'user' ? 1 : 0), 0)
  const syncChatStatus = useChatStatusStore((s) => s.sync)
  useEffect(() => {
    syncChatStatus(activeId, rounds)
  }, [activeId, rounds, syncChatStatus])
  useEffect(() => () => syncChatStatus(null, 0), [syncChatStatus])

  /** D1：选择器只列「已发布 + 对我可见」，草稿/停用/归档不进对话页 */
  const agentsQ = useAgents({ page: 1, page_size: 100, only_user_side: 1 })
  const agents = agentsQ.data?.results ?? []
  const kbsQ = useKnowledgeBases({ page: 1, page_size: 100 })
  const kbs = (kbsQ.data?.results ?? []).map((k) => ({ id: k.id, name: k.name }))

  const healthQ = useModelHealth()
  const configQ = useModelConfig()
  const modelOnline = healthQ.data ? healthQ.data.status === 'ok' : null
  const modelLabel = configQ.data ? `${configQ.data.model}` : ''

  const removeMut = useRemoveConversation()

  /** P1-c：切换入口只在后端开关打开时出现；关闭时本页行为与旧版一致 */
  const switchPolicyQ = useAgentSwitchPolicy()
  const switchEnabled = Boolean(switchPolicyQ.data?.enabled)
  const switchMut = useSwitchAgent()

  async function switchAgent(nextAgentId: number) {
    if (!conv) return
    try {
      const next = await switchMut.mutateAsync({ id: conv.id, agentId: nextAgentId })
      await qc.invalidateQueries({ queryKey: ['chat', 'conversations'] })
      setSwitchOpen(false)
      toast.success(`已切换到「${next.agent_name || '新助手'}」，历史已带入新会话`)
      setActiveId(next.id)
    } catch (e) {
      toast.error((e as ApiError).message || '切换失败')
    }
  }

  /* 切换会话时同步装配选择器（有意不把 convs 放进依赖，避免列表刷新覆盖用户未发送的改动） */
  useEffect(() => {
    if (!streaming) {
      // 撤下上一会话的流式块与错误/降级条，避免残留串台
      setView(null)
      setPendingUser(null)
      setRegenId(null)
      expectedCountRef.current = null
    }
    if (activeId === null) {
      setAgentId(null)
      setKbIds([])
      defaultedRef.current = false
      return
    }
    const c = convs.find((x) => x.id === activeId)
    if (c) {
      setAgentId(c.agent)
      setKbIds(c.knowledge_base_ids ?? [])
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId])

  /* D1：新会话默认「最近使用的助手」，首次使用回落出厂「通用问答」；用户手动选过就不再干预 */
  useEffect(() => {
    if (activeId !== null || defaultedRef.current || agentId !== null) return
    const list = agentsQ.data?.results ?? []
    if (!list.length) return
    const recent = convs.find((c) => c.agent && list.some((a) => a.id === c.agent))
    const factory = list.find((a) => a.is_builtin && a.name === '通用问答')
    const pick = recent?.agent ?? factory?.id ?? null
    if (pick !== null) setAgentId(pick)
    defaultedRef.current = true
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, agentId, agentsQ.data, convs])

  /** D1：助手已下线的历史会话只读，输入区换成下线提示 */
  const agentOffline = !!conv && (conv.agent_status === 'disabled' || conv.agent_status === 'archived')
  /** P1-c：已被切换承接的旧会话只读，避免同一段对话在两个会话里继续分叉 */
  const convReadonly = !!conv?.is_readonly
  /** D1：会话已绑定助手后中途不换，选择器锁定 */
  const agentLocked = !!conv && conv.agent !== null
  /** D8：能记偏好的前提是已绑定且未下线；新会话刚建好时列表可能还没刷新，回落选择器 */
  const canRemember = (conv?.agent ?? agentId) !== null && !agentOffline

  /* 消息滚动：新分片 / 新消息都贴底 */
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [view, messages.length, pendingUser])

  /* 服务端消息数到位后再撤下本地流式块，避免用户/助手气泡闪断 */
  useEffect(() => {
    if (expectedCountRef.current === null) return
    if (view?.error) return // 错误块保留，由用户决定重试
    if (messages.length >= expectedCountRef.current) {
      expectedCountRef.current = null
      setView(null)
      setPendingUser(null)
      setRegenId(null)
      void qc.invalidateQueries({ queryKey: ['chat', 'conversations'] })
    }
  }, [messages.length, view, qc])

  /** D2：只有最新一版当前答案能重新生成（更早的版本已是「上一版」） */
  const lastAssistantId = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const m = messages[i]
      if (m.role === 'assistant' && !m.meta?.superseded) return m.id
    }
    return null
  }, [messages])

  const handleEvent = useCallback((ev: ChatSseEvent) => {
    switch (ev.event) {
      case 'start': {
        const cid = ev.data.conversation_id
        convIdRef.current = cid
        setPendingUser(null)
        setActiveId((prev) => (prev === null ? cid : prev))
        // 用户消息（含附件）此刻已落库：立刻拉一次历史，本轮提问在流式期间保持可见
        void qc.invalidateQueries({ queryKey: ['chat', 'messages', cid] })
        setView((v) => (v ? {
          ...v,
          sources: ev.data.sources ?? [],
          preferences: ev.data.preferences ?? 0,
        } : v))
        break
      }
      case 'degrade':
        setView((v) => (v ? { ...v, degrade: [...v.degrade, ...ev.data.notes.map(noteToText)] } : v))
        break
      case 'thinking':
        setView((v) => (v ? { ...v, thinking: v.thinking + ev.data.content } : v))
        break
      case 'chunk':
        setView((v) => (v ? { ...v, content: v.content + ev.data.content } : v))
        break
      case 'tool':
        setView((v) => {
          if (!v) return v
          const t: ToolCallInfo = {
            call_id: ev.data.call_id,
            name: ev.data.name,
            display: ev.data.display ?? ev.data.name,
            args: ev.data.args || '',
            state: TOOL_RUNNING,
            result: '',
            elapsed: null,
            egress: ev.data.egress === true,
            egress_host: ev.data.egress_host || '',
          }
          return { ...v, tools: [...v.tools, t] }
        })
        break
      case 'tool_args':
        setView((v) =>
          v
            ? {
                ...v,
                tools: v.tools.map((t) => (t.call_id === ev.data.call_id ? { ...t, args: t.args + (ev.data.args ?? '') } : t)),
              }
            : v,
        )
        break
      case 'tool_result':
        setView((v) =>
          v
            ? {
                ...v,
                tools: v.tools.map((t) =>
                  t.call_id === ev.data.call_id
                    ? { ...t, state: ev.data.state, result: ev.data.result ?? t.result, elapsed: ev.data.elapsed ?? null, egress: ev.data.egress ?? t.egress, egress_host: ev.data.egress_host || t.egress_host }
                    : t,
                ),
              }
            : v,
        )
        break
      case 'permission_request':
        permissionRef.current = ev.data
        setView((v) => (v ? { ...v, permission: ev.data, permissionDecision: null } : v))
        break
      case 'permission_result':
        permissionRef.current = null
        setView((v) => (v ? { ...v, permissionDecision: ev.data.decision } : v))
        break
      case 'end': {
        const files: GeneratedFile[] = (ev.data && typeof ev.data === 'object' ? ev.data.generated_files : undefined) ?? []
        // E3 按需查：引用来源在模型真正调用检索工具后才产生，收尾时补进当前视图
        const endSources = ev.data && typeof ev.data === 'object' ? ev.data.sources : undefined
        setStreaming(false)
        // 流结束后服务端已落库：立刻刷新会话列表与消息，避免侧栏要手动刷新才见新会话
        void qc.invalidateQueries({ queryKey: ['chat', 'conversations'] })
        const endCid = convIdRef.current
        if (endCid !== null) void qc.invalidateQueries({ queryKey: ['chat', 'messages', endCid] })
        setView((v) => {
          if (!v) return v
          const done = {
            ...v,
            streaming: false,
            generatedFiles: files,
            ...(Array.isArray(endSources) ? { sources: endSources } : {}),
          }
          // 出错收尾：保留错误块；正常收尾：等待历史消息到位后由 effect 撤下
          if (!done.error) {
            const total = typeof ev.data === 'object' ? ev.data.message_count : undefined
            expectedCountRef.current = typeof total === 'number' ? total : messages.length + streamDeltaRef.current
          }
          return done
        })
        break
      }
      case 'error':
        setView((v) => (v ? { ...v, error: typeof ev.data === 'string' ? ev.data : '回答生成失败' } : v))
        break
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages.length])

  /** D7：提交授权决定；410 表示已超时，卡片按拒绝收口 */
  const onDecidePermission = useCallback(async (decision: PermissionDecision): Promise<boolean> => {
    const request = permissionRef.current
    if (!request) return false
    try {
      await decideChatPermission(request.request_id, decision)
      permissionRef.current = null
      setView((v) => (v ? { ...v, permissionDecision: decision } : v))
      return true
    } catch (e) {
      const err = e as ApiError
      if (err.status === 410) {
        permissionRef.current = null
        const restarted = err.code === 'permission_restarted'
        setView((v) => (v ? { ...v, permissionDecision: restarted ? 'restarted' : 'timeout' } : v))
        toast.error(restarted ? '平台服务已重启，授权已失效，请重新发起本轮对话' : '授权请求已超时，已按拒绝处理')
        return true
      }
      if (err.status === 409) return true
      toast.error(err.message || '授权提交失败')
      return false
    }
  }, [])

  /** 用户主动停止：服务端已把部分回答落库，展示中性「已停止生成」提示并刷新列表 */
  const finishStop = useCallback(() => {
    const request = permissionRef.current
    if (request) {
      // 尽力回传拒绝，避免服务端空等到 60 秒；失败也不影响后续超时兜底
      permissionRef.current = null
      void decideChatPermission(request.request_id, 'deny').catch(() => {})
    }
    setStreaming(false)
    setPendingUser(null)
    setRegenId(null)
    void qc.invalidateQueries({ queryKey: ['chat', 'conversations'] })
    const stopCid = convIdRef.current
    if (stopCid !== null) void qc.invalidateQueries({ queryKey: ['chat', 'messages', stopCid] })
    setView((v) => (v ? { ...v, streaming: false, stopped: true } : v))
  }, [qc])

  /** 流式请求异常：错误块保留，由用户决定重试；被重生的那一版恢复为当前答案 */
  const finishError = useCallback((e: unknown) => {
    setView((v) => (v ? { ...v, streaming: false, error: (e as ApiError).message || '请求失败' } : v))
    setPendingUser(null)
    setRegenId(null)
    setStreaming(false)
  }, [])

  const send = useCallback(
    async (text: string, attachments: ChatAttachment[] = []) => {
      if (streaming) return
      const payload: ChatSendPayload = { message: text, stream: true }
      if (activeId) payload.conversation_id = activeId
      else if (agentId !== null) payload.agent_id = agentId
      payload.knowledge_base_ids = kbIds
      // D5：只带本轮附件，上一轮的已在历史消息里，不重复注入
      if (attachments.length > 0) payload.attachments = attachments
      lastPayloadRef.current = payload
      lastRegenRef.current = null

      convIdRef.current = activeId
      expectedCountRef.current = null
      streamDeltaRef.current = 2
      setPendingUser({ text, attachments })
      setRegenId(null)
      permissionRef.current = null
      setView({ ...emptyView(), streaming: true })
      setStreaming(true)

      const ac = new AbortController()
      abortRef.current = ac
      try {
        await streamChat(payload, handleEvent, ac.signal)
        if (ac.signal.aborted) finishStop()
      } catch (e) {
        if (ac.signal.aborted) {
          finishStop()
        } else {
          finishError(e)
        }
      } finally {
        abortRef.current = null
      }
    },
    [streaming, activeId, agentId, kbIds, handleEvent, finishStop, finishError],
  )

  /** D2 重新生成：只重出最新一版回答，旧版由服务端转「上一版」留档，不新增提问 */
  const regenerate = useCallback(
    async (oldId: number) => {
      if (streaming || activeId === null) return
      convIdRef.current = activeId
      expectedCountRef.current = null
      streamDeltaRef.current = 1
      lastRegenRef.current = oldId
      setRegenId(oldId)
      setView({ ...emptyView(), streaming: true })
      setStreaming(true)

      const ac = new AbortController()
      abortRef.current = ac
      try {
        await regenerateChat({ conversation_id: activeId, message_id: oldId }, handleEvent, ac.signal)
        if (ac.signal.aborted) finishStop()
      } catch (e) {
        if (ac.signal.aborted) {
          finishStop()
        } else {
          finishError(e)
        }
      } finally {
        abortRef.current = null
      }
    },
    [streaming, activeId, handleEvent, finishStop, finishError],
  )

  const stop = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  const retry = useCallback(() => {
    if (streaming) return
    const rid = lastRegenRef.current
    if (rid !== null) {
      void regenerate(rid)
      return
    }
    const p = lastPayloadRef.current
    if (!p) return
    void send(p.message, p.attachments ?? [])
  }, [streaming, send, regenerate])

  const newChat = useCallback(() => {
    if (streaming) return
    setActiveId(null)
    setView(null)
    setPendingUser(null)
    setRegenId(null)
    permissionRef.current = null
    expectedCountRef.current = null
  }, [streaming])

  const openSource = useCallback((s: ChatSource) => setSource(s), [])

  const download = useCallback(async (f: GeneratedFile) => {
    try {
      await downloadGeneratedFile(f.url, f.name)
    } catch (e) {
      toast.error((e as ApiError).message || '文件下载失败')
    }
  }, [])

  /** D8：会话级偏好开关，只影响本会话是否代入，不删除任何已确认偏好 */
  const togglePreferences = useCallback(async () => {
    if (!conv || !conv.agent || prefBusy) return
    setPrefBusy(true)
    try {
      await chatApi.updateConversation(conv.id, { use_preferences: !conv.use_preferences })
      await qc.invalidateQueries({ queryKey: ['chat', 'conversations'] })
      toast.success(conv.use_preferences ? '本会话不再代入偏好' : '本会话恢复代入偏好')
    } catch (e) {
      toast.error((e as ApiError).message || '设置失败')
    } finally {
      setPrefBusy(false)
    }
  }, [conv, prefBusy, qc])

  /** D8：「以后都这样」：先由服务端归纳一句，再让用户改完确认 */
  const openRemember = useCallback(async (messageId?: number) => {
    if (activeId === null) return
    setRemember({ open: true, messageId, loading: true, text: '', saving: false })
    try {
      const r = await preferenceApi.suggest(activeId, messageId)
      setRemember((p) => ({ ...p, loading: false, text: r.suggestion }))
    } catch (e) {
      setRemember((p) => ({ ...p, loading: false }))
      toast.error((e as ApiError).message || '无法归纳这条偏好')
    }
  }, [activeId])

  const saveRemember = useCallback(async () => {
    const agent = conv?.agent ?? agentId
    const content = remember.text.trim()
    if (!agent || !content) return
    setRemember((p) => ({ ...p, saving: true }))
    try {
      await preferenceApi.create(agent, content)
      toast.success('已记住，之后的新会话会自动代入')
      setRemember({ open: false, loading: false, text: '', saving: false })
    } catch (e) {
      toast.error((e as ApiError).message || '保存失败')
      setRemember((p) => ({ ...p, saving: false }))
    }
  }, [conv, agentId, remember.text])

  return (
    <div className="chat-shell flex min-h-0 gap-3">
      {/* 会话列表 */}
      <aside className="flex w-[250px] flex-none flex-col">
        <ConversationList
          items={convs}
          total={convsQ.data?.count ?? 0}
          activeId={activeId}
          loading={convsQ.isPending}
          onSelect={(id) => {
            if (id !== activeId && !streaming) setActiveId(id)
          }}
          onNew={newChat}
          onDelete={(id) =>
            removeMut
              .mutateAsync(id)
              .then(() => {
                toast.success('会话已删除')
                if (id === activeId) newChat()
              })
              .catch((e: ApiError) => {
                toast.error(e.message || '删除失败')
                throw e
              })
          }
        />
      </aside>

      {/* 对话区 */}
      <section className="panel flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-xs text-muted-foreground">
          <Icon name="MessageSquare" size={13} className="text-primary" />
          <span className="truncate text-xs text-sidebar-foreground">
            {conv ? convTitle(conv.title) : '新对话'}
          </span>
          {conv && (
            <Badge tone={conv.agent ? 'primary' : 'neutral'}>{conv.agent_name || '平台通用助手'}</Badge>
          )}
          {agentOffline && (
            <Badge tone="warning" title="历史会话只读，不能继续追问">
              助手已下线
            </Badge>
          )}
          {conv?.agent && !agentOffline && (
            <button
              type="button"
              className="chip flex items-center gap-1 text-xs hover:text-sidebar-foreground disabled:opacity-60"
              title={
                conv.use_preferences
                  ? '本会话会代入你确认过的长期偏好；点击只在本次会话关闭'
                  : '本会话不代入你的长期偏好；点击恢复'
              }
              disabled={prefBusy}
              onClick={() => void togglePreferences()}
            >
              <Icon name="Sparkles" size={10} />
              偏好{conv.use_preferences ? '开' : '关'}
            </button>
          )}
          {convReadonly && (
            <Badge tone="neutral" title="已切换为新会话承接，历史只读">
              只读
            </Badge>
          )}
          {conv?.switched_from_title && !convReadonly && (
            <Badge tone="neutral" title={`由会话「${conv.switched_from_title}」切换而来`}>
              接续会话
            </Badge>
          )}
          {conv && switchEnabled && !agentOffline && !convReadonly && (
            <button
              type="button"
              className="chip flex items-center gap-1 text-xs hover:text-sidebar-foreground disabled:opacity-60"
              title="切换助手会新建一个会话承接当前历史，当前会话保留为只读"
              disabled={streaming || switchMut.isPending}
              onClick={() => setSwitchOpen(true)}
            >
              <Icon name="ArrowLeftRight" size={10} />
              切换助手
            </button>
          )}
          {activeId === null && agentId !== null && (
            <Badge tone="primary">{agents.find((a) => a.id === agentId)?.name ?? '智能体'}</Badge>
          )}
          <span className="ml-auto flex items-center gap-1.5">
            {modelOnline === null ? (
              <Icon name="LoaderCircle" size={12} className="animate-spin" />
            ) : (
              <span
                className={`inline-block h-[7px] w-[7px] rounded-full ${modelOnline ? 'bg-state-success' : 'bg-state-error'}`}
              />
            )}
            <span title={modelLabel}>{modelOnline ? (modelLabel || '模型已连接') : '模型端点不可用'}</span>
          </span>
        </div>

        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          {msgsQ.isPending && activeId !== null ? (
            <div className="py-8 text-center text-xs text-muted-foreground">加载历史消息…</div>
          ) : messages.length === 0 && !view && !pendingUser ? (
            <EmptyState
              icon="Sparkles"
              title="从一个问题开始"
              description={
                activeId === null
                  ? '可在下方选择智能体与知识库后发送第一条消息；智能体决定提示词与工具档位，知识库提供引用来源。'
                  : '这个会话还没有消息。'
              }
            />
          ) : (
            <div className="mx-auto flex max-w-[860px] flex-col gap-4">
              {messages
                .filter((m) => m.role !== 'system')
                .map((m) =>
                  m.role === 'user' ? (
                    <MessageItem key={m.id} role="user" text={m.content} attachments={m.attachments} />
                  ) : (
                    <MessageItem
                      key={m.id}
                      role="assistant"
                      view={m.id === regenId ? { ...viewFromMessage(m), superseded: true } : viewFromMessage(m)}
                      onOpenSource={openSource}
                      onDownload={download}
                      canRegenerate={m.id === lastAssistantId && !streaming && !agentOffline}
                      onRegenerate={() => void regenerate(m.id)}
                      canRemember={canRemember && !m.meta?.superseded}
                      onRemember={() => void openRemember(m.id)}
                      conversationId={activeId ?? convIdRef.current ?? undefined}
                    />
                  ),
                )}
              {pendingUser && (
                <MessageItem role="user" text={pendingUser.text} attachments={pendingUser.attachments} />
              )}
              {view && (
                <MessageItem
                  role="assistant"
                  view={view}
                  onOpenSource={openSource}
                  onRetry={view.error ? retry : undefined}
                  onDownload={download}
                  onDecidePermission={onDecidePermission}
                  canRemember={canRemember}
                  onRemember={() => void openRemember()}
                  conversationId={activeId ?? convIdRef.current ?? undefined}
                />
              )}
            </div>
          )}
        </div>

        <div className="border-t border-border bg-background/50 p-2">
          {agentOffline || convReadonly ? (
            /* D1 / P1-c：已下线助手或已切换承接的历史会话只读，不能继续追问 */
            <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
              <Icon name="Lock" size={13} className="text-state-warning" />
              {agentOffline
                ? '该助手已下线，历史会话只读，不能继续追问。'
                : '该会话已切换为新助手会话，历史只读，请到新会话继续对话。'}
              <Button quiet className="ml-auto" onClick={newChat} disabled={streaming}>
                新建会话
              </Button>
            </div>
          ) : (
            <Composer
              streaming={streaming}
              agents={agents.map((a) => ({
                id: a.id,
                name: a.name,
                description: a.description,
                jobTags: a.job_tags,
                hasTools: a.mount_summary.mcp_servers > 0,
                hasKb: a.mount_summary.knowledge_bases > 0,
                hasSkills: a.mount_summary.skills > 0,
              }))}
              agentId={agentId}
              agentName={conv?.agent_name}
              onAgentChange={(id) => {
                defaultedRef.current = true
                setAgentId(id)
              }}
              locked={agentLocked}
              kbs={kbs}
              kbIds={kbIds}
              onKbChange={setKbIds}
              onSend={(t, atts) => void send(t, atts)}
              onStop={stop}
            />
          )}
        </div>
      </section>

      {/* 引用来源抽屉 */}
      <Drawer
        open={source !== null}
        title="引用来源"
        onClose={() => setSource(null)}
        footer={
          <div className="flex items-center justify-between">
            <span className="text-xs text-muted-foreground">
              {source ? `${source.knowledge_base_name} · 片段 #${source.chunk_index}` : ''}
            </span>
            <Button quiet onClick={() => setSource(null)}>关闭</Button>
          </div>
        }
      >
        {source && (
          <div className="flex flex-col gap-2 text-sm">
            <div className="text-xs text-sidebar-foreground">{source.title || source.file_name}</div>
            <div className="text-xs text-muted-foreground">
              相关度 {source.score.toFixed(3)}
              {source.file_name ? ` · ${source.file_name}` : ''}
              {source.meta?.page ? ` · 第 ${source.meta.page} 页` : ''}
            </div>
            {source.meta?.ocr && (
              <div className="rounded-md border border-state-warning/40 bg-state-warning/10 px-2 py-1 text-xs text-state-warning">
                该片段由 OCR 识别，文字可能有误差，引用前请核对原文。
              </div>
            )}
            <div className="rounded-md border border-border bg-muted/40 p-2.5 text-xs leading-[1.7] whitespace-pre-wrap">
              {source.content || '（该片段无正文文本，可能是图片页）'}
            </div>
          </div>
        )}
      </Drawer>

      {/* D8：「以后都这样」先由服务端归纳，再由用户改完确认 */}
      <Modal
        open={remember.open}
        title="记住这条偏好"
        icon="Sparkles"
        onClose={remember.saving ? () => undefined : () => setRemember({ open: false, loading: false, text: '', saving: false })}
        footer={
          <>
            <Button
              variant="ghost"
              onClick={() => setRemember({ open: false, loading: false, text: '', saving: false })}
              disabled={remember.saving}
            >
              取消
            </Button>
            <Button
              variant="primary"
              onClick={() => void saveRemember()}
              loading={remember.saving}
              disabled={remember.loading || !remember.text.trim()}
            >
              记住
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-2">
          <p className="text-sm leading-[1.6] text-muted-foreground">
            只记住表达要求与常查对象，用于这个助手的后续新会话；不会记录对话正文或业务数据。
          </p>
          <Textarea
            rows={3}
            value={remember.text}
            maxLength={100}
            placeholder={remember.loading ? '正在归纳这次回答…' : '例如：用简称称呼我，先给结论再给依据'}
            disabled={remember.loading || remember.saving}
            onChange={(e) => setRemember((p) => ({ ...p, text: e.target.value }))}
          />
          <div className="text-xs text-muted-foreground">{remember.text.length}/100 · 每个助手最多 8 条</div>
        </div>
      </Modal>

      {/* P1-c：切换助手 = 新建会话承接当前历史，旧会话保留只读 */}
      <Modal
        open={switchOpen}
        title="切换助手"
        icon="ArrowLeftRight"
        onClose={switchMut.isPending ? () => undefined : () => setSwitchOpen(false)}
        footer={
          <Button variant="ghost" onClick={() => setSwitchOpen(false)} disabled={switchMut.isPending}>
            取消
          </Button>
        }
      >
        <div className="flex flex-col gap-2">
          <p className="text-sm leading-[1.6] text-muted-foreground">
            切换后会新建一个会话并承接当前历史，本会话保留为只读，避免同一段对话分成两个版本。
          </p>
          {agents.filter((a) => a.id !== (conv?.agent ?? null)).map((a) => (
            <button
              key={a.id}
              type="button"
              disabled={switchMut.isPending}
              className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2 text-left hover:bg-muted/60 disabled:opacity-60"
              onClick={() => void switchAgent(a.id)}
            >
              <span className="flex min-w-0 flex-col leading-tight">
                <span className="row-title">{a.name}</span>
                <span className="row-sub truncate">{a.description || '暂无说明'}</span>
              </span>
              <Icon name="ChevronRight" size={12} className="flex-none text-muted-foreground" />
            </button>
          ))}
          {agents.filter((a) => a.id !== (conv?.agent ?? null)).length === 0 && (
            <div className="text-xs text-muted-foreground">没有其他可用助手。</div>
          )}
        </div>
      </Modal>
    </div>
  )
}
