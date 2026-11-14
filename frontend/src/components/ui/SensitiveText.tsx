import { useMemo, useState } from 'react'
import { maskSensitive } from '@/lib/redact'
import { toast } from '@/store/toast'

/**
 * 展开审计回调：展开前先写「谁在什么时候看了哪条」，失败则保持掩码。
 * 未提供时只做掩码、不提供展开入口（失败关闭）。
 */
export type RevealAudit = () => Promise<unknown>

export interface RevealState {
  /** 当前应展示的文本：未展开为掩码，展开后为原文 */
  shown: string
  /** 命中并脱敏的处数 */
  hits: number
  revealed: boolean
  busy: boolean
  toggle: () => Promise<void>
}

/** 掩码 + 受审计的展开；供需要把按钮放在正文外部的场景复用 */
export function useSensitiveReveal(value: string, audit?: RevealAudit): RevealState {
  const { text, hits } = useMemo(() => maskSensitive(value), [value])
  const [revealed, setRevealed] = useState(false)
  const [busy, setBusy] = useState(false)

  async function toggle() {
    if (revealed) {
      setRevealed(false)
      return
    }
    if (!audit) return
    setBusy(true)
    try {
      await audit()
      setRevealed(true)
      toast.success('已显示原文，本次展开已写入审计')
    } catch (err) {
      toast.error(`展开失败，保持脱敏：${(err as Error).message}`)
    } finally {
      setBusy(false)
    }
  }

  return { shown: revealed ? value : text, hits, revealed, busy, toggle }
}

interface Props {
  /** 原始文本（后端返回原文，掩码只发生在展示层） */
  value: string
  className?: string
  /** 多行全文（工具返回）：用 pre 渲染，按钮放在正文下方 */
  block?: boolean
  /** 鼠标悬停提示使用当前展示文本（掩码态不给原文） */
  showTitle?: boolean
  audit?: RevealAudit
}

/**
 * 敏感内容展示：默认掩码，命中时可显式展开原文。
 * 展开是「可见且可追溯」的入口——先落审计再显示。
 */
export function SensitiveText({ value, className = '', block = false, showTitle = false, audit }: Props) {
  const { shown, hits, revealed, busy, toggle } = useSensitiveReveal(value, audit)

  const btn =
    hits > 0 && audit ? (
      <button
        type="button"
        className="whitespace-nowrap text-xs text-primary hover:underline disabled:opacity-60"
        disabled={busy}
        onClick={() => void toggle()}
        title={revealed ? '重新隐藏并保持脱敏展示' : '展开原文会记录查看人、时间与对象'}
      >
        {busy ? '记录中…' : revealed ? '隐藏原文' : `显示原文（${hits} 处已脱敏）`}
      </button>
    ) : null

  if (block) {
    return (
      <div className="col-stack gap-[4px]">
        <pre className={className} title={showTitle ? shown : undefined}>
          {shown}
        </pre>
        {btn}
      </div>
    )
  }

  // 正文与按钮拆成兄弟节点：正文可以被截断，按钮始终可见可点
  return (
    <>
      <span className={className} title={showTitle ? shown : undefined}>
        {shown}
      </span>
      {btn && <span className="ml-[6px] flex-none whitespace-nowrap">{btn}</span>}
    </>
  )
}
