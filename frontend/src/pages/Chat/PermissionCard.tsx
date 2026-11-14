import { useEffect, useState } from 'react'
import { revealSensitive } from '@/api/audit'
import { Icon } from '@/components/ui/Icon'
import { SensitiveText } from '@/components/ui/SensitiveText'
import type { PermissionDecision, PermissionRequest } from '@/types/chat'

const DECISION_LABEL: Record<string, string> = {
  allow_once: '已允许一次',
  allow_session: '本会话已允许',
  deny: '已拒绝',
  timeout: '已超时，按拒绝处理',
  restarted: '平台已重启，请求已失效，请重新发起',
}

interface Props {
  request: PermissionRequest
  /** 已收口的决定；null 表示仍在等待 */
  decision: string | null
  /** 试聊只允许单次/拒绝，不提供跨轮记忆 */
  allowSession?: boolean
  /** 会话 id：展开参数原文时按会话归属记审计 */
  conversationId?: number
  /** 返回 false 表示提交失败（卡片恢复可点，允许重试） */
  onDecide: (decision: PermissionDecision) => void | Promise<boolean>
}

/** D7：会话内逐次授权卡片，内联在过程区上方，不阻塞输入框 */
export function PermissionCard({ request, decision, allowSession = true, conversationId, onDecide }: Props) {
  const [remaining, setRemaining] = useState(request.timeout_seconds)
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (decision) return
    setRemaining(request.timeout_seconds)
    const timer = window.setInterval(() => {
      setRemaining((n) => (n > 0 ? n - 1 : 0))
    }, 1000)
    return () => window.clearInterval(timer)
  }, [request.request_id, request.timeout_seconds, decision])

  const done = !!decision

  const submit = async (value: PermissionDecision) => {
    if (done || submitting) return
    setSubmitting(true)
    const settled = await onDecide(value)
    if (settled === false) setSubmitting(false)
  }

  return (
    <div className="flex flex-col gap-2 rounded-md border border-state-warning-border bg-state-warning-soft px-2.5 py-2 text-xs">
      <div className="flex items-start gap-2">
        <Icon name="ShieldAlert" size={14} className="mt-[1px] flex-none text-state-warning" />
        <div className="min-w-0 flex-1">
          <div className="font-medium text-state-warning">需要授权后才会执行工具</div>
          <div className="mt-0.5 text-muted-foreground">
            <span className="text-sidebar-foreground">{request.source_name}</span>
            <span className="mx-1">·</span>
            <span className="text-sidebar-foreground">{request.tool_name}</span>
          </div>
          {request.args && (
            <div className="mt-0.5 flex items-baseline gap-[2px] break-all text-muted-foreground">
              <span className="flex-none">输入：</span>
              <SensitiveText
                value={request.args}
                className="min-w-0 line-clamp-3 break-all"
                showTitle
                audit={() =>
                  revealSensitive({
                    scope: 'chat',
                    field: 'args',
                    call_id: request.call_id,
                    conversation_id: conversationId,
                  })
                }
              />
            </div>
          )}
        </div>
        {!done && (
          <span className="flex-none text-muted-foreground tabular-nums">{remaining}s</span>
        )}
      </div>

      {done ? (
        <div className="pl-6 text-muted-foreground">{DECISION_LABEL[decision] ?? decision}</div>
      ) : (
        <div className="flex flex-wrap items-center gap-1.5 pl-6">
          <button
            type="button"
            disabled={submitting}
            className="chip border-primary-border text-primary hover:bg-primary/10 disabled:opacity-50"
            onClick={() => void submit('allow_once')}
          >
            允许一次
          </button>
          {allowSession && request.can_always && (
            <button
              type="button"
              disabled={submitting}
              className="chip hover:bg-muted disabled:opacity-50"
              onClick={() => void submit('allow_session')}
            >
              本会话始终允许
            </button>
          )}
          <button
            type="button"
            disabled={submitting}
            className="chip border-state-error-border text-state-error hover:bg-state-error-soft disabled:opacity-50"
            onClick={() => void submit('deny')}
          >
            拒绝
          </button>
          <span className="text-muted-foreground">超时将按拒绝处理</span>
        </div>
      )}
    </div>
  )
}
