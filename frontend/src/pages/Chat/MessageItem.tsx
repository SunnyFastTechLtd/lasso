import { useState } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { revealSensitive } from '@/api/audit'
import { Icon } from '@/components/ui/Icon'
import { Button } from '@/components/ui/Button'
import { SensitiveText } from '@/components/ui/SensitiveText'
import { fmtBytes } from '@/utils/format'
import { toast } from '@/store/toast'
import type { ChatAttachment, ChatSource, GeneratedFile, PermissionDecision } from '@/types/chat'
import { TOOL_RUNNING, toolFailed } from '@/types/chat'
import { fmtElapsed, type AssistantView } from './shared'
import { PermissionCard } from './PermissionCard'

interface Props {
  role: 'user' | 'assistant'
  /** user：正文；assistant：仅正文兜底（优先用 view.content） */
  text?: string
  /** D5：用户气泡下方的附件标记 */
  attachments?: ChatAttachment[]
  view?: AssistantView
  time?: string
  onOpenSource?: (source: ChatSource) => void
  onRetry?: () => void
  onDownload?: (file: GeneratedFile) => void
  /** D2：仅最新一版当前答案给出「重新生成」入口 */
  canRegenerate?: boolean
  onRegenerate?: () => void
  /** D7：授权卡片决定回调；试聊传入时隐藏「本会话始终允许」 */
  onDecidePermission?: (decision: PermissionDecision) => void | Promise<boolean>
  allowSessionGrant?: boolean
  /** D8：对当前答案点「以后都这样」，让平台归纳一句偏好 */
  canRemember?: boolean
  onRemember?: () => void
  /** 会话 id：展开工具参数/结果原文时按会话归属记审计 */
  conversationId?: number
}

/** D2 复制答案：局域网 http 访问时 navigator.clipboard 不可用，退回 execCommand */
async function copyAnswer(content: string): Promise<void> {
  let ok = false
  try {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(content)
      ok = true
    }
  } catch {
    /* 无权限或非安全上下文，走下面的兜底 */
  }
  if (!ok) {
    try {
      const ta = document.createElement('textarea')
      ta.value = content
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      ok = document.execCommand('copy')
      ta.remove()
    } catch {
      ok = false
    }
  }
  if (ok) toast.success('答案已复制')
  else toast.error('复制失败，请手动选中文本')
}

export function MessageItem({ role, text, attachments, view, onOpenSource, onRetry, onDownload, canRegenerate, onRegenerate, onDecidePermission, allowSessionGrant = true, canRemember, onRemember, conversationId }: Props) {
  const [showPrev, setShowPrev] = useState(false)

  if (role === 'user') {
    const atts = attachments ?? []
    return (
      <div className="flex flex-col items-end gap-1">
        {atts.length > 0 && (
          <div className="flex max-w-[78%] flex-wrap justify-end gap-1.5">
            {atts.map((a, i) => (
              <span
                key={`${a.name}-${i}`}
                className="chip flex max-w-[240px] items-center gap-1 text-xs"
                title={`${a.name} · ${fmtBytes(a.size)}${a.truncated ? ' · 正文已截断' : ''}`}
              >
                <Icon name="Paperclip" size={10} className="flex-none" />
                <span className="min-w-0 truncate">{a.name}</span>
                {a.truncated && <span className="flex-none text-state-warning">已截断</span>}
              </span>
            ))}
          </div>
        )}
        <div className="max-w-[78%] rounded-lg rounded-br-sm bg-primary-border/25 px-3 py-2 text-sm leading-[1.65] whitespace-pre-wrap">
          {text}
        </div>
      </div>
    )
  }

  const v = view
  if (!v) return null
  const toolsRunning = v.streaming && v.tools.some((t) => t.state === TOOL_RUNNING)

  // D2：上一版默认折叠，展开后与当前版同屏对比，随时可复制
  if (v.superseded && !showPrev) {
    return (
      <div className="flex flex-wrap items-center gap-2 rounded-md border border-border/70 bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
        <Icon name="History" size={12} />
        <span>上一版答案 · 已被重新生成</span>
        <button type="button" className="hover:text-sidebar-foreground" onClick={() => setShowPrev(true)}>
          展开对比
        </button>
        {v.content && (
          <button type="button" className="hover:text-sidebar-foreground" onClick={() => void copyAnswer(v.content)}>
            复制
          </button>
        )}
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-2">
      {v.superseded && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <Icon name="History" size={12} />
          <span>上一版答案 · 已被重新生成</span>
          <button type="button" className="hover:text-sidebar-foreground" onClick={() => setShowPrev(false)}>
            收起
          </button>
        </div>
      )}
      {/* D6 降级条 */}
      {v.degrade.length > 0 && (
        <div className="flex items-start gap-2 rounded-md border border-state-warning-border bg-state-warning-soft px-3 py-2 text-xs leading-[1.6]">
          <Icon name="TriangleAlert" size={13} className="mt-[2px] flex-none text-state-warning" />
          <div className="flex flex-col">
            <span className="text-state-warning">本轮装配已降级</span>
            {v.degrade.map((note, i) => (
              <span key={i} className="text-muted-foreground">{note}</span>
            ))}
          </div>
        </div>
      )}

      {/* D8：本轮带入的偏好条数，过程区可见 */}
      {v.preferences > 0 && (
        <div className="flex items-center gap-1 text-xs text-muted-foreground">
          <Icon name="Sparkles" size={11} />
          已带入 {v.preferences} 条你的偏好
        </div>
      )}

      {/* D7 授权卡片：内联等待，不阻塞 Composer */}
      {v.permission && onDecidePermission && (
        <PermissionCard
          request={v.permission}
          decision={v.permissionDecision}
          allowSession={allowSessionGrant}
          conversationId={conversationId}
          onDecide={onDecidePermission}
        />
      )}

      {/* 思考过程（推理模型分片） */}
      {v.thinking && (
        <details className="rounded-md border border-border bg-muted/40 px-3 py-2 text-xs">
          <summary className="cursor-pointer select-none text-muted-foreground">
            <Icon name="Brain" size={12} className="mr-1 inline align-[-2px]" />
            思考过程
          </summary>
          <div className="mt-1.5 max-h-[200px] overflow-y-auto leading-[1.65] whitespace-pre-wrap text-muted-foreground">
            {v.thinking}
          </div>
        </details>
      )}

      {/* D2 过程区：工具调用逐条可见 */}
      {v.tools.length > 0 && (
        <details className="rounded-md border border-border bg-muted/40 px-3 py-2 text-xs" open={toolsRunning || undefined}>
          <summary className="cursor-pointer select-none text-muted-foreground">
            <Icon name="Wrench" size={12} className="mr-1 inline align-[-2px]" />
            工具调用 · {v.tools.length} 次
          </summary>
          <div className="mt-1.5 flex flex-col gap-1.5">
            {v.tools.map((t) => {
              const running = t.state === TOOL_RUNNING
              const failed = !running && toolFailed(t.state)
              return (
                <div key={t.call_id} className="rounded border border-border/60 bg-card/60 px-2.5 py-1.5">
                  <div className="flex items-center gap-1.5">
                    {running ? (
                      <Icon name="LoaderCircle" size={12} className="animate-spin text-primary" />
                    ) : failed ? (
                      <Icon name="CircleX" size={12} className="text-state-error" />
                    ) : (
                      <Icon name="CircleCheck" size={12} className="text-state-success" />
                    )}
                    <span className="truncate font-medium text-sidebar-foreground">{t.display}</span>
                    {t.elapsed !== null && !running && (
                      <span className="ml-auto flex-none text-muted-foreground">{fmtElapsed(t.elapsed)}</span>
                    )}
                  </div>
                  {t.args && (
                    <div className="mt-0.5 flex items-baseline gap-[2px] text-muted-foreground">
                      <span className="flex-none">参数：</span>
                      <SensitiveText
                        value={t.args}
                        className="min-w-0 truncate"
                        showTitle
                        audit={() =>
                          revealSensitive({
                            scope: 'chat',
                            field: 'args',
                            call_id: t.call_id,
                            conversation_id: conversationId,
                          })
                        }
                      />
                    </div>
                  )}
                  {t.egress && (
                    <div className="mt-0.5 flex items-center gap-1 text-state-warning" title="该工具走外网出口，参数摘要已发送到所示来源">
                      <Icon name="Globe" size={11} />
                      外发：参数摘要已发送至 {t.egress_host || '外部来源'}
                    </div>
                  )}
                  {t.result && (
                    <div className="mt-0.5 flex items-baseline gap-[2px] text-muted-foreground">
                      <span className="flex-none">返回：</span>
                      <SensitiveText
                        value={t.result}
                        className="min-w-0 line-clamp-2"
                        showTitle
                        audit={() =>
                          revealSensitive({
                            scope: 'chat',
                            field: 'result',
                            call_id: t.call_id,
                            conversation_id: conversationId,
                          })
                        }
                      />
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </details>
      )}

      {/* 正文（打字机输出） */}
      {(v.content || (v.streaming && !v.error)) && (
        <div className="max-w-[88%] rounded-lg rounded-bl-sm bg-card border border-border px-3.5 py-2.5 text-sm leading-[1.7]">
          {v.content && (
            <div className="md-body">
              <Markdown remarkPlugins={[remarkGfm]}>{v.content}</Markdown>
            </div>
          )}
          {v.streaming && !v.error && <span className="ml-0.5 inline-block h-[14px] w-[7px] translate-y-[2px] animate-pulse bg-primary/70" />}
        </div>
      )}

      {/* D2 操作条：复制答案；重新生成只对最新一版当前答案开放 */}
      {v.content && !v.streaming && (
        <div className="flex items-center gap-3 pl-0.5 text-xs text-muted-foreground">
          <button
            type="button"
            className="flex items-center gap-1 hover:text-sidebar-foreground"
            onClick={() => void copyAnswer(v.content)}
          >
            <Icon name="Copy" size={11} />
            复制
          </button>
          {canRegenerate && !v.superseded && (
            <button
              type="button"
              className="flex items-center gap-1 hover:text-sidebar-foreground"
              title="保留这一版为「上一版」，按同一提问重出一版"
              onClick={onRegenerate}
            >
              <Icon name="RefreshCw" size={11} />
              重新生成
            </button>
          )}
          {canRemember && !v.superseded && (
            <button
              type="button"
              className="flex items-center gap-1 hover:text-sidebar-foreground"
              title="把这次的要求归纳成一句，确认后下次新会话继续生效"
              onClick={onRemember}
            >
              <Icon name="Sparkles" size={11} />
              以后都这样
            </button>
          )}
        </div>
      )}

      {/* 引用来源：可点开看原文片段 */}
      {v.sources.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-muted-foreground">引用</span>
          {v.sources.map((s, i) => (
            <button
              key={i}
              type="button"
              onClick={() => onOpenSource?.(s)}
              className="chip flex items-center gap-1 border-primary-border text-primary hover:bg-primary/10"
              title={`${s.knowledge_base_name} · ${s.title}${s.meta?.ocr ? '（OCR 识别，可能有误差）' : ''}`}
            >
              <Icon name="BookOpen" size={11} />
              <span className="max-w-[180px] truncate">{s.file_name || s.title}</span>
              {s.meta?.ocr && (
                <span className="text-xs text-state-warning" title="OCR 识别，可能有误差">OCR</span>
              )}
              <span className="text-xs opacity-70">{s.score.toFixed(2)}</span>
            </button>
          ))}
        </div>
      )}

      {/* 生成产物下载 */}
      {v.generatedFiles.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {v.generatedFiles.map((f) => (
            <button
              key={f.id}
              type="button"
              disabled={f.is_expired}
              onClick={() => onDownload?.(f)}
              className="chip flex items-center gap-1"
              title={`${f.name} · ${fmtBytes(f.size)}`}
            >
              <Icon name={f.is_expired ? 'FileX' : 'FileDown'} size={11} />
              <span className="max-w-[200px] truncate">{f.name}</span>
            </button>
          ))}
        </div>
      )}

      {/* 错误条 + 重试 */}
      {v.error && (
        <div className="flex items-center gap-2 rounded-md border border-state-error-border bg-state-error-soft px-2.5 py-1.5 text-xs">
          <Icon name="CircleAlert" size={13} className="flex-none text-state-error" />
          <span className="min-w-0 flex-1 leading-[1.5] text-state-error">{v.error}</span>
          {onRetry && (
            <Button quiet variant="default" icon="RotateCcw" onClick={onRetry}>
              重试
            </Button>
          )}
        </div>
      )}
      {v.permissionInterrupted && (
        <div className="text-xs text-state-warning">本次授权请求已失效，操作未执行</div>
      )}
      {v.stopped && !v.permissionInterrupted && (
        <div className="text-xs text-muted-foreground">已停止生成</div>
      )}
      {!v.content && !v.streaming && v.permissionNotes.length > 0 && (
        <div className="text-xs text-muted-foreground">
          本次有工具调用未获授权，操作未执行
        </div>
      )}
    </div>
  )
}
