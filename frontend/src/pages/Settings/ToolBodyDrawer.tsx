import { useEffect, useState } from 'react'
import { getAuditBody, revealSensitive } from '@/api/audit'
import { ApiError } from '@/api/request'
import { Badge, Chip } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { EmptyState } from '@/components/ui/EmptyState'
import { Icon } from '@/components/ui/Icon'
import { Drawer } from '@/components/ui/Overlay'
import { useSensitiveReveal } from '@/components/ui/SensitiveText'
import { toast } from '@/store/toast'
import type { ApiToolCallBody, AuditLogItem } from '@/types/audit'
import { fmtDateTime, fmtNumber } from '@/utils/format'

/** 仅这三类动作可能带工具调用全文；has_body 由后端列表一次性判定 */
const BODY_ACTIONS = new Set(['tool_call', 'tool_failed', 'trial_tool_call'])

/** 前端第一道防线：非管理员或后端判定无全文时不显示入口（后端仍做最终校验） */
export function canViewBody(row: AuditLogItem, isAdmin: boolean): boolean {
  return isAdmin && row.has_body && BODY_ACTIONS.has(row.action)
}

/** 超过 2 万字或已截存时提供 .txt 下载（前端 Blob，不新增接口） */
const DOWNLOAD_CHARS = 20_000

const STATE_LABEL: Record<string, string> = {
  success: '成功',
  error: '失败',
  interrupted: '中断',
  denied: '被拒',
  running: '执行中',
}

const STATE_WARN = new Set(['error', 'interrupted', 'denied'])

/** 局域网 http 下 navigator.clipboard 不可用，退回 execCommand（与 D2 复制一致） */
async function copyText(text: string): Promise<boolean> {
  let ok = false
  try {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text)
      ok = true
    }
  } catch {
    /* 无权限或非安全上下文，走兜底 */
  }
  if (!ok) {
    try {
      const ta = document.createElement('textarea')
      ta.value = text
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
  return ok
}

function downloadText(id: number, name: string, text: string) {
  const safe = (name || 'tool-call').replace(/[^\w.-]+/g, '_').slice(0, 40)
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `tool-call-${id}-${safe}.txt`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

interface Props {
  row: AuditLogItem | null
  onClose: () => void
}

/** 工具调用全文：右侧抽屉只读展示，元数据在头部，正文 pre-wrap 可横向滚动 */
export function ToolBodyDrawer({ row, onClose }: Props) {
  const [body, setBody] = useState<ApiToolCallBody | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)

  useEffect(() => {
    if (!row) return
    let alive = true
    setBody(null)
    setError(null)
    setLoading(true)
    getAuditBody(row.id)
      .then((data) => {
        if (!alive) return
        setBody(data)
        setLoading(false)
      })
      .catch((e: unknown) => {
        if (!alive) return
        setError(e instanceof ApiError ? e : new ApiError(0, (e as Error)?.message || '读取失败', null, null))
        setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [row])

  const canDownload = !!body && (body.storage_truncated || body.result_chars > DOWNLOAD_CHARS)

  // 返回全文同样只在展示层脱敏；复制/下载等「拿走原文」的动作要求先展开（展开即留痕）
  const reveal = useSensitiveReveal(body?.result ?? '', async () => {
    if (!row) throw new Error('缺少审计记录')
    await revealSensitive({ scope: 'audit', audit_id: row.id, field: 'result' })
  })

  return (
    <Drawer
      open={row !== null}
      onClose={onClose}
      title={row ? `工具调用全文 · 审计 #${row.id}` : '工具调用全文'}
      meta={row ? `${row.action_display} · ${fmtDateTime(row.created_at)}` : undefined}
      footer={
        <>
          {body && (
            <Button
              variant="ghost"
              icon="Copy"
              disabled={!reveal.revealed}
              title={reveal.revealed ? '复制完整原文' : '返回内容含敏感字段，先「显示原文」并记审计后再复制'}
              onClick={() => {
                void copyText(body.result).then((ok) =>
                  ok ? toast.success('全文已复制') : toast.error('复制失败，请手动选中文本'),
                )
              }}
            >
              复制
            </Button>
          )}
          {body && canDownload && (
            <Button
              variant="ghost"
              icon="Download"
              disabled={!reveal.revealed}
              title={reveal.revealed ? '下载完整原文' : '返回内容含敏感字段，先「显示原文」并记审计后再下载'}
              onClick={() => downloadText(body.id, body.call_id || body.tool, body.result)}
            >
              下载 .txt
            </Button>
          )}
          <Button variant="ghost" onClick={onClose}>
            关闭
          </Button>
        </>
      }
    >
      {loading && (
        <div className="panel-blank">
          <Icon name="LoaderCircle" size={14} className="animate-spin" />
          读取工具调用全文…
        </div>
      )}

      {!loading && error && error.status === 403 && (
        <EmptyState
          icon="Lock"
          title="仅管理员可查看工具调用全文"
          description="该接口按平台管理员校验身份；如确需查看，请联系平台管理员使用管理员账号登录。"
        />
      )}

      {!loading && error && error.status === 404 && (
        <EmptyState
          icon="FileQuestion"
          title="全文不可用"
          description={error.detail || '该记录未留存全文（历史数据）'}
        />
      )}

      {!loading && error && error.status !== 403 && error.status !== 404 && (
        <EmptyState
          icon="TriangleAlert"
          title="全文读取失败"
          description={error.detail || '请稍后重试；若持续失败请联系平台管理员。'}
        />
      )}

      {!loading && !error && !body && <EmptyState compact icon="Inbox" title="暂无正文" />}

      {!loading && body && (
        <div className="col-stack">
          {body.storage_truncated && (
            <div className="flex items-start gap-[6px] rounded-md border border-state-warning-border bg-surface-sunken px-[10px] py-[8px] text-xs leading-[1.5]">
              <Icon name="TriangleAlert" size={12} className="mt-[2px] flex-none text-state-warning" />
              <span>
                原文 {fmtNumber(body.result_chars)} 字，已截存（单条上限 256KB，此处仅展示前 262144 字）。下载同样为截存后的内容。
              </span>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-[6px]">
            <Chip>
              {body.conversation_id ? (
                <>
                  会话 <span className="mono">#{body.conversation_id}</span>
                </>
              ) : (
                '试聊（无会话）'
              )}
            </Chip>
            <Chip title={body.call_id}>
              调用 <span className="mono">{body.call_id || '—'}</span>
            </Chip>
            <Chip className="mono">{body.tool || '—'}</Chip>
            <Badge tone={STATE_WARN.has(body.state) ? 'warning' : 'success'}>
              {STATE_LABEL[body.state] || body.state || '未知'}
            </Badge>
            <Badge tone={body.egress ? 'primary' : 'neutral'}>{body.egress ? '出网调用' : '内网工具'}</Badge>
            <Chip className="tnum">{fmtDateTime(body.created_at)}</Chip>
            <Chip className="tnum">{fmtNumber(body.result_chars)} 字</Chip>
            {body.storage_truncated && <Badge tone="warning">已截存</Badge>}
          </div>

          <div className="flex flex-wrap items-center gap-[6px] text-xs text-muted-foreground">
            <Icon name="Lock" size={12} className="flex-none" />
            {reveal.hits > 0 ? (
              <>
                <span>返回内容已脱敏 {reveal.hits} 处（token / 密钥 / 口令等）</span>
                <button
                  type="button"
                  className="whitespace-nowrap text-xs text-primary hover:underline disabled:opacity-60"
                  disabled={reveal.busy}
                  title={reveal.revealed ? '重新隐藏并保持脱敏展示' : '展开原文会记录查看人、时间与对象'}
                  onClick={() => void reveal.toggle()}
                >
                  {reveal.busy ? '记录中…' : reveal.revealed ? '隐藏原文' : '显示原文'}
                </button>
              </>
            ) : (
              <span>未命中敏感字段，按原文展示</span>
            )}
          </div>

          <pre className="mono overflow-auto whitespace-pre-wrap break-normal rounded-md border border-border bg-surface-sunken px-[10px] py-[8px] text-xs leading-[1.6] text-sidebar-foreground">
            {reveal.shown || '（空返回）'}
          </pre>

          <div className="flex items-start gap-[6px] text-xs leading-[1.5] text-muted-foreground">
            <Icon name="Info" size={12} className="mt-[2px] flex-none" />
            <span>
              全文保留 90 天，到期自动清理；本次查看会写入一条「查看工具调用全文」访问留痕。
              原文指纹 sha256：<span className="mono">{body.result_sha256 || '—'}</span>
            </span>
          </div>
        </div>
      )}
    </Drawer>
  )
}
