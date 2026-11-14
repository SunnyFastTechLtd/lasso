/**
 * A3 试聊面板：用智能体「最新保存」的配置（草稿也行）直接起一轮临时会话。
 * 验后即弃——服务端零落库，历史由本组件每轮回传；工具调用在后端以
 * 「试聊」标记计入审计。与正式会话同一条 SSE 事件流（收尾事件名为 done）。
 */
import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/Button'
import { Icon } from '@/components/ui/Icon'
import { Textarea } from '@/components/ui/Field'
import { decidePermission as decideChatPermission, streamTrial } from '@/api/chat'
import { ApiError } from '@/api/request'
import {
  TOOL_RUNNING,
  toolFailed,
  type PermissionDecision,
  type PermissionRequest,
  type ToolCallInfo,
  type TrialSseEvent,
} from '@/types/chat'
import { emptyView, fmtElapsed, type AssistantView } from '@/pages/Chat/shared'
import { PermissionCard } from '@/pages/Chat/PermissionCard'
import { maskSensitive } from '@/lib/redact'
import type { AgentStatus } from '@/types/agent'
import { AgentStatusBadge } from './shared'

interface Props {
  agentId: number
  agentName: string
  status: AgentStatus
  statusDisplay: string
}

type Row = { role: 'user'; content: string } | { role: 'assistant'; view: AssistantView }

export function TrialPanel({ agentId, agentName, status, statusDisplay }: Props) {
  const [rows, setRows] = useState<Row[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const listRef = useRef<HTMLDivElement>(null)
  /** D7：试聊同样走授权卡片，但不跨轮记忆「始终允许」 */
  const permissionRef = useRef<PermissionRequest | null>(null)

  // 关面板/卸载时中断在途流
  useEffect(() => () => abortRef.current?.abort(), [])
  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [rows])

  function patchLast(mut: (v: AssistantView) => AssistantView) {
    setRows((rs) => {
      const out = [...rs]
      for (let i = out.length - 1; i >= 0; i -= 1) {
        const r = out[i]
        if (r.role === 'assistant') {
          out[i] = { role: 'assistant', view: mut(r.view) }
          break
        }
      }
      return out
    })
  }

  async function send() {
    const msg = input.trim()
    if (!msg || busy) return
    // 只回传正文；出错行不进上下文
    const history = rows.flatMap<{ role: 'user' | 'assistant'; content: string }>((r) =>
      r.role === 'user'
        ? [{ role: 'user' as const, content: r.content }]
        : r.view.content
          ? [{ role: 'assistant' as const, content: r.view.content }]
          : [],
    )
    setRows((rs) => [
      ...rs,
      { role: 'user', content: msg },
      { role: 'assistant', view: { ...emptyView(), streaming: true } },
    ])
    setInput('')
    setBusy(true)
    permissionRef.current = null
    const ac = new AbortController()
    abortRef.current = ac

    const handle = (ev: TrialSseEvent) => {
      switch (ev.event) {
        case 'chunk':
          patchLast((v) => ({ ...v, content: v.content + ev.data.content }))
          break
        case 'thinking':
          patchLast((v) => ({ ...v, thinking: v.thinking + ev.data.content }))
          break
        case 'degrade':
          patchLast((v) => ({ ...v, degrade: [...v.degrade, ...ev.data.notes] }))
          break
        case 'tool':
          patchLast((v) => ({
            ...v,
            tools: [
              ...v.tools,
              {
                call_id: ev.data.call_id,
                name: ev.data.name,
                display: ev.data.display,
                args: ev.data.args ?? '',
                state: TOOL_RUNNING,
                result: '',
                elapsed: null,
                egress: ev.data.egress === true,
                egress_host: ev.data.egress_host || '',
              },
            ],
          }))
          break
        case 'tool_args':
          patchLast((v) => ({
            ...v,
            tools: v.tools.map((t) =>
              t.call_id === ev.data.call_id ? { ...t, args: t.args + (ev.data.args ?? '') } : t,
            ),
          }))
          break
        case 'tool_result':
          patchLast((v) => ({
            ...v,
            tools: v.tools.map((t) =>
              t.call_id === ev.data.call_id
                ? { ...t, state: ev.data.state, result: ev.data.result ?? t.result, elapsed: ev.data.elapsed ?? null, egress: ev.data.egress ?? t.egress, egress_host: ev.data.egress_host || t.egress_host }
                : t,
            ),
          }))
          break
        case 'permission_request':
          permissionRef.current = ev.data
          patchLast((v) => ({ ...v, permission: ev.data, permissionDecision: null }))
          break
        case 'permission_result':
          permissionRef.current = null
          patchLast((v) => ({ ...v, permissionDecision: ev.data.decision }))
          break
        case 'error':
          patchLast((v) => ({ ...v, error: ev.data.message || '试聊失败' }))
          break
        default:
          break
      }
    }

    try {
      await streamTrial({ agent_id: agentId, message: msg, messages: history }, handle, ac.signal)
    } catch (err) {
      const message = err instanceof ApiError ? err.message : (err as Error).message
      patchLast((v) => ({ ...v, error: message }))
    } finally {
      abortRef.current = null
      setBusy(false)
      patchLast((v) => ({ ...v, streaming: false, stopped: ac.signal.aborted }))
    }
  }

  /** D7：试聊授权提交；超时/重复点击按服务端语义收口 */
  async function decide(decision: PermissionDecision): Promise<boolean> {
    const request = permissionRef.current
    if (!request) return false
    try {
      await decideChatPermission(request.request_id, decision)
      permissionRef.current = null
      patchLast((v) => ({ ...v, permissionDecision: decision }))
      return true
    } catch (err) {
      if (err instanceof ApiError && err.status === 410) {
        permissionRef.current = null
        patchLast((v) => ({ ...v, permissionDecision: err.code === 'permission_restarted' ? 'restarted' : 'timeout' }))
        return true
      }
      if (err instanceof ApiError && err.status === 409) return true
      const message = err instanceof ApiError ? err.message : (err as Error).message
      patchLast((v) => ({ ...v, error: message }))
      return false
    }
  }

  function stop() {
    const request = permissionRef.current
    if (request) {
      permissionRef.current = null
      void decideChatPermission(request.request_id, 'deny').catch(() => {})
    }
    abortRef.current?.abort()
  }

  const canSend = !!input.trim() && !busy

  return (
    <aside className="flex h-[520px] w-[340px] flex-none flex-col overflow-hidden rounded-[10px] border border-border/70 bg-card">
      <header className="flex flex-none items-center gap-[7px] border-b border-border/60 px-[14px] py-[10px]">
        <Icon name="FlaskConical" size={14} className="flex-none text-primary" />
        <span className="min-w-0 truncate text-xs font-medium">试聊 · {agentName}</span>
        <AgentStatusBadge status={status} display={statusDisplay} />
        <span className="ml-auto flex-none text-xs text-muted-foreground">不留会话记录</span>
      </header>
      <div className="flex-none border-b border-border/40 bg-muted/40 px-[14px] py-[9px] text-xs leading-[1.6] text-muted-foreground">
        按最近一次保存的配置运行（未发布草稿也可试）；结果仅供参考，工具调用计入审计并标记「试聊」。
      </div>

      <div ref={listRef} className="min-h-0 flex-1 space-y-[14px] overflow-y-auto px-[14px] py-[12px]">
        {rows.length === 0 && (
          <div className="flex h-full flex-col items-center justify-center gap-[6px] text-center text-xs text-muted-foreground">
            <Icon name="MessagesSquare" size={18} />
            在下方输入问题，立刻验证这套配置的回答与工具调用
          </div>
        )}
        {rows.map((r, i) =>
          r.role === 'user' ? (
            <div key={i} className="flex justify-end">
              <div className="max-w-[86%] rounded-[10px] rounded-br-[3px] bg-primary px-[12px] py-[9px] text-xs leading-[1.65] whitespace-pre-wrap text-primary-foreground">
                {r.content}
              </div>
            </div>
          ) : (
            <BotRow key={i} view={r.view} onDecide={decide} />
          ),
        )}
      </div>

      <footer className="flex-none border-t border-border/60 px-[14px] py-[10px]">
        <Textarea
          rows={2}
          value={input}
          placeholder="输入试聊问题，Enter 发送 / Shift+Enter 换行"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              void send()
            }
          }}
        />
        <div className="mt-[8px] flex items-center gap-[6px]">
          <Button variant="ghost" quiet disabled={busy || rows.length === 0} onClick={() => setRows([])}>
            清空
          </Button>
          {busy ? (
            <Button variant="default" quiet className="ml-auto" icon="CircleStop" onClick={stop}>
              停止
            </Button>
          ) : (
            <Button variant="primary" quiet className="ml-auto" icon="Send" disabled={!canSend} onClick={() => void send()}>
              发送
            </Button>
          )}
        </div>
      </footer>
    </aside>
  )
}

function BotRow({ view, onDecide }: { view: AssistantView; onDecide: (d: PermissionDecision) => void | Promise<boolean> }) {
  return (
    <div className="flex flex-col gap-[8px]">
      {view.degrade.length > 0 && (
        <div className="rounded border border-state-warning/40 bg-state-warning/10 px-[10px] py-[8px] text-xs leading-[1.6] text-state-warning">
          {view.degrade.map((n, i) => (
            <div key={i}>{n}</div>
          ))}
        </div>
      )}
      {view.permission && (
        <PermissionCard
          request={view.permission}
          decision={view.permissionDecision}
          allowSession={false}
          onDecide={onDecide}
        />
      )}
      {view.tools.map((t) => (
        <ToolRow key={t.call_id} tool={t} />
      ))}
      {(view.content || view.streaming) && (
        <div className="max-w-[92%] self-start rounded-[10px] rounded-bl-[3px] border border-border/60 bg-muted/50 px-[12px] py-[9px] text-xs leading-[1.65] whitespace-pre-wrap">
          {view.content}
          {view.streaming && !view.content && (
            <span className="inline-flex items-center gap-[4px] text-muted-foreground">
              <Icon name="LoaderCircle" size={12} className="animate-spin" /> 正在生成…
            </span>
          )}
        </div>
      )}
      {view.stopped && !view.streaming && (
        <div className="text-xs text-muted-foreground">已停止生成</div>
      )}
      {view.error && (
        <div className="rounded border border-state-error/40 bg-state-error/10 px-[10px] py-[8px] text-xs leading-[1.6] text-state-error">
          {view.error}
        </div>
      )}
    </div>
  )
}

function ToolRow({ tool }: { tool: ToolCallInfo }) {
  const running = tool.state === TOOL_RUNNING
  const failed = toolFailed(tool.state)
  // 悬浮提示等同展示面：先掩码，避免悬停即拿到明文（试聊不落库，也没有可追溯的展开入口）
  const tip = tool.result
    ? `结果：${maskSensitive(tool.result).text}`
    : tool.args
      ? maskSensitive(tool.args).text
      : undefined
  return (
    <div
      title={tip}
      className="rounded border border-border/50 bg-muted/30 px-[10px] py-[6px] text-xs"
    >
      <div className="flex items-center gap-[6px]">
        {running ? (
          <Icon name="LoaderCircle" size={11} className="flex-none animate-spin text-muted-foreground" />
        ) : failed ? (
          <Icon name="CircleX" size={11} className="flex-none text-state-error" />
        ) : (
          <Icon name="Wrench" size={11} className="flex-none text-primary" />
        )}
        <span className="min-w-0 truncate font-medium">{tool.display || tool.name}</span>
        {!running && (
          <span className="ml-auto flex-none text-muted-foreground">
            {failed ? '失败' : fmtElapsed(tool.elapsed)}
          </span>
        )}
      </div>
      {tool.egress && (
        <div className="mt-[3px] flex items-center gap-[4px] text-state-warning">
          <Icon name="Globe" size={10} />
          外发：参数摘要已发送至 {tool.egress_host || '外部来源'}
        </div>
      )}
    </div>
  )
}
