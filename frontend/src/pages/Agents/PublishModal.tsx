import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Icon } from '@/components/ui/Icon'
import { Modal } from '@/components/ui/Overlay'
import { SectionLabel } from '@/components/ui/Panel'
import { agentsApi } from '@/api/agents'
import { usePublishAgent } from '@/api/hooks/useAgents'
import { ApiError } from '@/api/request'
import { toast } from '@/store/toast'
import type { CheckItem, DiffItem, PublishCheck, PublishResponse } from '@/types/agent'

interface Props {
  open: boolean
  agentId: number | null
  agentName: string
  onClose: () => void
}

function LevelIcon({ level }: { level: CheckItem['level'] }) {
  if (level === 'blocking') return <Icon name="CircleX" size={13} className="mt-[2px] shrink-0 text-state-error" />
  if (level === 'warning') return <Icon name="AlertTriangle" size={13} className="mt-[2px] shrink-0 text-state-warning" />
  return <Icon name="Info" size={13} className="mt-[2px] shrink-0 text-muted-foreground" />
}

function CheckLine({ item }: { item: CheckItem }) {
  return (
    <div className={`check-line ${item.level === 'blocking' ? 'is-blocking' : ''}`}>
      <LevelIcon level={item.level} />
      <span className="min-w-0">
        <span className="text-xs">
          {item.label}
          <span className="ml-[6px] text-xs text-muted-foreground">〔{item.section_title}〕</span>
        </span>
        <span className="block text-xs text-muted-foreground">{item.message}</span>
      </span>
    </div>
  )
}

export function Chips({ items, tone }: { items: string[]; tone: 'add' | 'remove' }) {
  return (
    <span className="flex flex-wrap gap-[4px]">
      {items.map((t) => (
        <span
          key={t}
          className={`mono rounded px-[5px] py-[1px] text-xs ${
            tone === 'add' ? 'bg-state-success/10 text-state-success' : 'bg-state-error/10 text-state-error'
          }`}
        >
          {t}
        </span>
      ))}
    </span>
  )
}

export function DiffLine({ item }: { item: DiffItem }) {
  let body: ReactNode = null
  switch (item.kind) {
    case 'text':
      body = (
        <span className="text-xs text-muted-foreground">
          {item.new_summary}
          {item.delta !== 0 && (
            <span className={item.delta > 0 ? 'ml-[6px] text-state-success' : 'ml-[6px] text-state-error'}>
              {item.delta > 0 ? `+${item.delta}` : item.delta} 字
            </span>
          )}
        </span>
      )
      break
    case 'list':
      body = (
        <span className="flex flex-wrap items-center gap-[6px]">
          {item.added.length > 0 && <Chips items={item.added} tone="add" />}
          {item.removed.length > 0 && <Chips items={item.removed} tone="remove" />}
          <span className="text-xs text-muted-foreground">{item.detail}</span>
        </span>
      )
      break
    case 'bool':
      body = (
        <span className="text-xs text-muted-foreground">
          {item.old} → <span className="text-foreground">{item.new}</span>
        </span>
      )
      break
    case 'value':
      body = (
        <span className="text-xs text-muted-foreground">
          {item.old_display ?? '—'} → <span className="text-foreground">{item.new_display ?? '—'}</span>
        </span>
      )
      break
    case 'added':
    case 'removed':
      body = (
        <span className="flex flex-wrap items-center gap-[6px]">
          <span className="text-xs">{item.server}</span>
          <Chips items={item.tools} tone={item.kind === 'added' ? 'add' : 'remove'} />
          <span className="text-xs text-muted-foreground">{item.detail}</span>
        </span>
      )
      break
    case 'tools':
      body = (
        <span className="flex flex-wrap items-center gap-[6px]">
          <span className="text-xs">{item.server}</span>
          {item.added.length > 0 && <Chips items={item.added} tone="add" />}
          {item.removed.length > 0 && <Chips items={item.removed} tone="remove" />}
        </span>
      )
      break
  }
  return (
    <div className="check-line">
      <span className="min-w-0">
        <span className="text-xs">{item.label}</span>
        <span className="mt-[2px] block">{body}</span>
      </span>
    </div>
  )
}

export function PublishModal({ open, agentId, agentName, onClose }: Props) {
  const [check, setCheck] = useState<PublishCheck | null>(null)
  const [loading, setLoading] = useState(false)
  const [riskAck, setRiskAck] = useState(false)
  const [diffConfirmed, setDiffConfirmed] = useState(false)
  const publish = usePublishAgent()

  const refresh = useCallback(async () => {
    if (agentId === null) return
    setLoading(true)
    try {
      setCheck(await agentsApi.publishCheck(agentId))
    } catch (err) {
      toast.error((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [agentId])

  useEffect(() => {
    if (open && agentId !== null) {
      setCheck(null)
      setRiskAck(false)
      setDiffConfirmed(false)
      void refresh()
    }
  }, [open, agentId, refresh])

  const needsDiff = !!check && !check.first_publish && !check.diff.no_change
  const gateOk =
    !!check &&
    check.can_publish &&
    (!check.requires_ack || riskAck) &&
    (!needsDiff || diffConfirmed)

  async function doPublish() {
    if (agentId === null || !gateOk) return
    try {
      const res: PublishResponse = await publish.mutateAsync({
        id: agentId,
        risk_ack: riskAck,
        confirm_diff: needsDiff ? diffConfirmed : undefined,
      })
      toast.success(res.detail || `已发布 v${res.version?.version ?? ''}`)
      onClose()
    } catch (err) {
      if (err instanceof ApiError) {
        const body = err.body as Partial<PublishResponse> | null
        if (body?.check) {
          setCheck(body.check)
          if (body.code === 'diff_review_required') setDiffConfirmed(false)
        }
        toast.error(err.message)
        return
      }
      toast.error((err as Error).message)
    }
  }

  const headerTone =
    check == null ? 'neutral' : check.status === 'blocked' ? 'error' : check.status === 'ack_required' ? 'warning' : 'success'
  const headerText =
    check == null ? '检查中…' : check.status === 'blocked' ? '不可发布' : check.status === 'ack_required' ? '需确认后发布' : '检查通过'

  return (
    <Modal
      open={open}
      title={`发布检查 · ${agentName}`}
      icon="Rocket"
      wide
      onClose={onClose}
      footer={
        <>
          <span className="mr-auto text-xs text-muted-foreground">
            {check && (check.first_publish ? '首次发布' : `基线 v${check.base_version} → 新发布 v${check.next_version}`)}
          </span>
          <Button onClick={onClose}>返回修改</Button>
          <Button variant="primary" icon="Rocket" loading={publish.isPending} disabled={!gateOk || loading} onClick={doPublish}>
            发布新版本
          </Button>
        </>
      }
    >
      {loading && !check && (
        <div className="flex items-center gap-[7px] py-[20px] text-xs text-muted-foreground">
          <Icon name="LoaderCircle" size={14} className="animate-spin" /> 正在执行发布前检查…
        </div>
      )}
      {check && (
        <div className="space-y-[12px]">
          <div className="flex items-center gap-[8px]">
            <Badge tone={headerTone === 'neutral' ? 'neutral' : headerTone}>{headerText}</Badge>
            <span className="text-xs text-muted-foreground tnum">
              拦截 {check.counts.blocking} · 警告 {check.counts.warnings} · 提示 {check.counts.notices}
            </span>
            <button type="button" className="ml-auto text-xs text-primary hover:underline" onClick={() => void refresh()}>
              重新检查
            </button>
          </div>

          {check.blocking.length > 0 && (
            <div>
              <SectionLabel>必须修复（拦截发布）</SectionLabel>
              <div className="mt-[6px] space-y-[4px]">
                {check.blocking.map((c) => (
                  <CheckLine key={c.code} item={c} />
                ))}
              </div>
            </div>
          )}

          {check.warnings.length > 0 && (
            <div>
              <SectionLabel>风险警告（需逐项确认）</SectionLabel>
              <div className="mt-[6px] space-y-[4px]">
                {check.warnings.map((c) => (
                  <CheckLine key={c.code} item={c} />
                ))}
              </div>
            </div>
          )}

          {check.notices.length > 0 && (
            <details className="rounded border border-border/60 px-[10px] py-[7px]">
              <summary className="cursor-pointer text-xs text-muted-foreground">
                提示（不拦截，自动放行）· {check.notices.length} 条
              </summary>
              <div className="mt-[6px] space-y-[4px]">
                {check.notices.map((c) => (
                  <CheckLine key={c.code} item={c} />
                ))}
              </div>
            </details>
          )}

          <div>
            <SectionLabel>发布差异{check.diff.first_publish ? '（首次发布，与上一版对比不适用）' : ''}</SectionLabel>
            {check.diff.no_change ? (
              <p className="mt-[6px] text-xs text-muted-foreground">{check.diff.summary}</p>
            ) : (
              <div className="mt-[6px] space-y-[8px]">
                <p className="text-xs text-muted-foreground">{check.diff.summary}</p>
                {check.diff.sections.map((sec) => (
                  <div key={sec.key} className="rounded border border-border/60 px-[10px] py-[7px]">
                    <div className="text-xs font-medium">{sec.title}</div>
                    <div className="mt-[4px] space-y-[4px]">
                      {sec.items.map((item, i) => (
                        <DiffLine key={`${sec.key}-${item.field}-${i}`} item={item} />
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {(check.requires_ack || needsDiff) && (
            <div className="space-y-[6px] rounded border border-state-warning/40 bg-state-warning/10 px-[10px] py-[8px]">
              {check.requires_ack && (
                <label className="flex cursor-pointer items-start gap-[7px] text-xs">
                  <input
                    type="checkbox"
                    className="mt-[3px] accent-primary"
                    checked={riskAck}
                    onChange={(e) => setRiskAck(e.target.checked)}
                  />
                  <span>
                    我已逐项阅读并确认接受上述 {check.counts.warnings} 条风险警告
                    <span className="block text-xs text-muted-foreground">
                      留痕字段：risk_ack；记录确认人与时间，见发布历史
                    </span>
                  </span>
                </label>
              )}
              {needsDiff && (
                <label className="flex cursor-pointer items-start gap-[7px] text-xs">
                  <input
                    type="checkbox"
                    className="mt-[3px] accent-primary"
                    checked={diffConfirmed}
                    onChange={(e) => setDiffConfirmed(e.target.checked)}
                  />
                  <span>
                    我已查看发布差异（影响对话页提示词与已发布行为）
                    <span className="block text-xs text-muted-foreground">
                      留痕字段：confirm_diff；服务端在存在线上版本且有变化时强制要求
                    </span>
                  </span>
                </label>
              )}
            </div>
          )}
        </div>
      )}
    </Modal>
  )
}
