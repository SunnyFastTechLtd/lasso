import { useEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { Icon } from '@/components/ui/Icon'
import { Button } from '@/components/ui/Button'
import { chatApi } from '@/api/chat'
import { toast } from '@/store/toast'
import type { ApiError } from '@/api/request'
import type { ChatAttachment } from '@/types/chat'
import { fmtBytes } from '@/utils/format'

interface Props {
  streaming: boolean
  /** D1：后端已按「已发布 + 对当前用户可见」过滤，前端只做搜索与岗位分组 */
  agents: {
    id: number
    name: string
    description?: string
    jobTags?: string[]
    hasTools?: boolean
    hasKb?: boolean
    hasSkills?: boolean
  }[]
  agentId: number | null
  /** 已绑定但不在「已发布可见」列表里的助手（如下线），仍要显示名字 */
  agentName?: string
  onAgentChange: (id: number | null) => void
  /** D1：会话已绑定助手，中途不换；换助手请新建会话 */
  locked?: boolean
  kbs: { id: number; name: string }[]
  kbIds: number[]
  onKbChange: (ids: number[]) => void
  /** D5：附件为已上传接口的响应，随本轮消息原样提交 */
  onSend: (text: string, attachments: ChatAttachment[]) => void
  onStop: () => void
}

/** 后端只做了这几类的正文提取，前端限制选择范围，避免选了之后才报错 */
const ACCEPT = '.txt,.md,.pdf,.docx,.xlsx,.pptx'

/** 浮层菜单通用收起：点外部或 Escape（不用全屏遮罩，避免吞掉输入区的点击） */
function useDismiss(open: boolean, close: () => void, refs: RefObject<HTMLElement | null>[]) {
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (refs.some((r) => r.current?.contains(t))) return
      close()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close()
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, close])
}

/** 向上弹出的浮层锚点（输入区贴近视口底部） */
function anchorOf(el: HTMLElement | null): { left: number; bottom: number } {
  const r = el?.getBoundingClientRect()
  return r ? { left: r.left, bottom: window.innerHeight - r.top + 6 } : { left: 0, bottom: 0 }
}

interface CapsInfo {
  hasTools?: boolean
  hasKb?: boolean
  hasSkills?: boolean
}

/** D1 能力摘要图标：知识库 / 外部工具 / 技能 */
function Caps({ a }: { a?: CapsInfo }) {
  return (
    <>
      {a?.hasKb && (
        <span title="该智能体挂载知识库" className="flex items-center">
          <Icon name="BookOpen" size={12} className="text-primary" />
        </span>
      )}
      {a?.hasTools && (
        <span title="该智能体包含外部工具" className="flex items-center">
          <Icon name="Wrench" size={12} className="text-primary" />
        </span>
      )}
      {a?.hasSkills && (
        <span title="该智能体挂载技能" className="flex items-center">
          <Icon name="Puzzle" size={12} className="text-primary" />
        </span>
      )}
    </>
  )
}

export function Composer({ streaming, agents, agentId, agentName, onAgentChange, locked, kbs, kbIds, onKbChange, onSend, onStop }: Props) {
  const [text, setText] = useState('')
  const [attachments, setAttachments] = useState<ChatAttachment[]>([])
  const [uploading, setUploading] = useState(false)
  const [kbOpen, setKbOpen] = useState(false)
  const [kbPos, setKbPos] = useState<{ left: number; bottom: number }>({ left: 0, bottom: 0 })
  const kbBtnRef = useRef<HTMLDivElement>(null)
  const kbMenuRef = useRef<HTMLDivElement>(null)
  const [agentOpen, setAgentOpen] = useState(false)
  const [agentPos, setAgentPos] = useState<{ left: number; bottom: number }>({ left: 0, bottom: 0 })
  const [agentKw, setAgentKw] = useState('')
  const agentBtnRef = useRef<HTMLDivElement>(null)
  const agentMenuRef = useRef<HTMLDivElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const ta = taRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = `${Math.min(ta.scrollHeight, 160)}px`
  }, [text])

  useDismiss(kbOpen, () => setKbOpen(false), [kbBtnRef, kbMenuRef])
  useDismiss(agentOpen, () => setAgentOpen(false), [agentBtnRef, agentMenuRef])

  /** D1：名称/用途模糊匹配后按岗位标签分组（无标签归到「通用」） */
  const agentGroups = useMemo(() => {
    const kw = agentKw.trim().toLowerCase()
    const hit = agents.filter(
      (a) =>
        !kw ||
        a.name.toLowerCase().includes(kw) ||
        (a.description ?? '').toLowerCase().includes(kw),
    )
    const groups = new Map<string, typeof hit>()
    hit.forEach((a) => {
      const tag = a.jobTags?.[0] || '通用'
      groups.set(tag, [...(groups.get(tag) ?? []), a])
    })
    return [...groups.entries()]
  }, [agents, agentKw])

  const submit = () => {
    const t = text.trim()
    // 附件只作为本轮提问的依据，正文仍必填（后端 message 不允许为空）
    if (!t || streaming || uploading) return
    setText('')
    onSend(t, attachments)
    setAttachments([])
  }

  /** 单个文件上传：大小/超限原因一律透传后端文案，前端不重复校验 */
  const pickFile = async (file: File) => {
    if (uploading) return
    setUploading(true)
    try {
      const att = await chatApi.uploadAttachment(file)
      setAttachments((prev) => [...prev, att])
      if (att.truncated) toast.success(`已读取 ${att.name}（正文过长，只取前段）`)
    } catch (e) {
      toast.error((e as ApiError).message || '附件上传失败')
    } finally {
      setUploading(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const toggleKb = (id: number) => {
    onKbChange(kbIds.includes(id) ? kbIds.filter((x) => x !== id) : [...kbIds, id])
  }

  const current = agents.find((a) => a.id === agentId)
  /** 已绑定但不在可选列表里的助手（已下线），仍显示会话真正的归属 */
  const displayName = current?.name ?? (agentId !== null ? agentName : undefined)

  return (
    <div className="panel mt-2 flex flex-col gap-1.5 p-2">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <div className="flex items-center gap-1" ref={agentBtnRef}>
          <Icon name="Sparkles" size={12} className="text-primary" />
          <button
            type="button"
            disabled={locked}
            title={
              locked
                ? '一条会话只对应一个助手，新建会话才会应用新的能力配置'
                : (current?.description ?? '选择智能体')
            }
            onClick={() => {
              if (!agentOpen) setAgentPos(anchorOf(agentBtnRef.current))
              setAgentOpen((v) => !v)
            }}
            className="chip flex max-w-[220px] items-center gap-1 disabled:cursor-not-allowed"
          >
            <span className="min-w-0 truncate">{displayName ?? '平台通用助手'}</span>
            <Icon name={locked ? 'Lock' : agentOpen ? 'ChevronUp' : 'ChevronDown'} size={11} />
          </button>
          <Caps a={current} />
        </div>
        {current?.description && (
          <span className="max-w-[360px] truncate" title={current.description}>
            {current.description}
          </span>
        )}
        {locked && (
          <span className="text-xs text-muted-foreground">换助手请新建会话</span>
        )}

        {agentOpen &&
          createPortal(
            <div
              ref={agentMenuRef}
              className="fixed z-50 flex w-[290px] flex-col gap-1.5 rounded-lg border border-border bg-card p-2 shadow-lg"
              style={{ left: agentPos.left, bottom: agentPos.bottom }}
            >
              <div className="relative">
                <Icon name="Search" size={12} className="pointer-events-none absolute left-[9px] top-1/2 -translate-y-1/2 text-muted-foreground" />
                <input
                  autoFocus
                  value={agentKw}
                  onChange={(e) => setAgentKw(e.target.value)}
                  placeholder="搜索智能体名称或用途"
                  className="input h-[32px] pl-6 text-xs"
                />
              </div>
              <div className="max-h-[220px] overflow-y-auto">
                {agentGroups.length === 0 ? (
                  <div className="px-2 py-1.5 text-muted-foreground">没有匹配的已发布助手</div>
                ) : (
                  agentGroups.map(([tag, items]) => (
                    <div key={tag} className="mb-1.5">
                      <div className="px-2 py-1.5 text-xs text-muted-foreground">{tag}</div>
                      {items.map((a) => (
                        <button
                          key={a.id}
                          type="button"
                          onClick={() => {
                            onAgentChange(a.id)
                            setAgentOpen(false)
                          }}
                          className={`flex w-full flex-col gap-0.5 rounded px-2 py-1.5 text-left hover:bg-muted/50 ${
                            a.id === agentId ? 'bg-muted/60' : ''
                          }`}
                        >
                          <span className="flex items-center gap-1">
                            <span className="min-w-0 flex-1 truncate text-sm">{a.name}</span>
                            <Caps a={a} />
                            {a.id === agentId && <Icon name="Check" size={11} className="text-primary" />}
                          </span>
                          {a.description && (
                            <span className="truncate text-xs text-muted-foreground">{a.description}</span>
                          )}
                        </button>
                      ))}
                    </div>
                  ))
                )}
              </div>
              <button
                type="button"
                onClick={() => {
                  onAgentChange(null)
                  setAgentOpen(false)
                }}
                className="rounded px-2 py-1.5 text-left hover:bg-muted/50"
              >
                不绑定智能体（平台通用助手）
              </button>
            </div>,
            document.body,
          )}

        <div className="relative" ref={kbBtnRef}>
          <button
            type="button"
            onClick={() => {
              const el = kbBtnRef.current
              if (el && !kbOpen) {
                const r = el.getBoundingClientRect()
                setKbPos({ left: r.left, bottom: window.innerHeight - r.top + 6 })
              }
              setKbOpen((v) => !v)
            }}
            className="chip flex items-center gap-1"
          >
            <Icon name="BookOpen" size={12} />
            知识库{kbIds.length > 0 ? ` · 已选 ${kbIds.length}` : ''}
            <Icon name={kbOpen ? 'ChevronUp' : 'ChevronDown'} size={11} />
          </button>
          {kbOpen &&
            createPortal(
                <div
                  ref={kbMenuRef}
                  className="fixed z-50 max-h-[220px] w-[240px] overflow-y-auto rounded-lg border border-border bg-card p-2 shadow-lg"
                  style={{ left: kbPos.left, bottom: kbPos.bottom }}
                >
                  {kbs.length === 0 ? (
                    <div className="px-2 py-1.5 text-muted-foreground">还没有可用的知识库</div>
                  ) : (
                    kbs.map((kb) => (
                      <label key={kb.id} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 hover:bg-muted/50">
                        <input
                          type="checkbox"
                          checked={kbIds.includes(kb.id)}
                          onChange={() => toggleKb(kb.id)}
                          className="h-3.5 w-3.5 accent-[var(--color-primary)]"
                        />
                        <span className="min-w-0 flex-1 truncate text-sm">{kb.name}</span>
                      </label>
                    ))
                  )}
                </div>,
              document.body,
            )}
        </div>

        <button
          type="button"
          disabled={uploading || streaming}
          onClick={() => fileRef.current?.click()}
          title="上传文件作为本轮提问的依据（不入知识库）"
          className="chip flex items-center gap-1 disabled:cursor-not-allowed disabled:opacity-60"
        >
          <Icon name={uploading ? 'LoaderCircle' : 'Paperclip'} size={12} className={uploading ? 'animate-spin' : ''} />
          {uploading ? '上传中' : '附件'}
        </button>
        <input
          ref={fileRef}
          type="file"
          accept={ACCEPT}
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0]
            if (f) void pickFile(f)
          }}
        />

        <span className="ml-auto">Enter 发送 · Shift+Enter 换行</span>
      </div>

      {attachments.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {attachments.map((a, i) => (
            <span
              // 同名文件可重复添加，索引兜底 key
              key={`${a.name}-${i}`}
              className="chip flex max-w-[280px] items-center gap-1"
              title={`${a.name} · ${fmtBytes(a.size)}${a.truncated ? ' · 正文已截断' : ''}`}
            >
              <Icon name="FileText" size={11} className="flex-none text-primary" />
              <span className="min-w-0 truncate">{a.name}</span>
              <span className="flex-none text-xs opacity-70">{fmtBytes(a.size)}</span>
              {a.truncated && <span className="flex-none text-xs text-state-warning">已截断</span>}
              <button
                type="button"
                aria-label={`移除 ${a.name}`}
                onClick={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}
                className="flex-none hover:text-state-error"
              >
                <Icon name="X" size={11} />
              </button>
            </span>
          ))}
        </div>
      )}

      <div className="flex items-end gap-2">
        <textarea
          ref={taRef}
          value={text}
          rows={2}
          placeholder="输入问题，Enter 发送…"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault()
              submit()
            }
          }}
          className="input min-h-[44px] max-h-[160px] flex-1 resize-none py-2"
        />
        {streaming ? (
          <Button variant="danger" icon="Square" onClick={onStop}>
            停止
          </Button>
        ) : (
          <Button variant="primary" icon="Send" disabled={!text.trim() || uploading} onClick={submit}>
            发送
          </Button>
        )}
      </div>
    </div>
  )
}
