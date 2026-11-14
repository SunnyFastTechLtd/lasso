import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Icon } from '@/components/ui/Icon'
import { Modal } from '@/components/ui/Overlay'
import type { RiskChallenge } from './shared'

interface Props {
  challenge: RiskChallenge | null
  title?: string
  busy?: boolean
  onCancel: () => void
  onConfirm: () => void
}

/**
 * 决策 10：高危命中「提示 + 确认放行」。
 * 只有确认后前端才会带 risk_ack + risk_fingerprint 重发请求；不确认则后端不落库。
 */
export function RiskConfirmModal({ challenge, title = '检测到高危内容', busy = false, onCancel, onConfirm }: Props) {
  if (!challenge) return null
  const groups = challenge.hits.reduce<Record<string, typeof challenge.hits>>((acc, hit) => {
    const key = hit.file || 'SKILL.md'
    ;(acc[key] ||= []).push(hit)
    return acc
  }, {})

  return (
    <Modal
      open
      title={title}
      icon="TriangleAlert"
      wide
      onClose={busy ? () => undefined : onCancel}
      footer={
        <>
          <Button variant="ghost" onClick={onCancel} disabled={busy}>
            取消，不保存
          </Button>
          <Button variant="danger" icon="ShieldAlert" loading={busy} onClick={onConfirm}>
            我已阅读并确认放行
          </Button>
        </>
      }
    >
      <div className="col-stack gap-[10px]">
        <div className="rounded-md border border-state-warning-border bg-state-warning-soft px-[10px] py-[8px] text-xs leading-relaxed text-state-warning">
          以下内容命中高危规则，保存后会对智能体运行时生效。确认放行将记录确认人、规则与文件；
          未确认则本次内容不会落库。
        </div>

        {Object.entries(groups).map(([file, hits]) => (
          <div key={file} className="rounded-md border border-border">
            <div className="flex items-center gap-[6px] border-b px-[10px] py-[6px]">
              <Icon name="FileWarning" size={13} className="text-state-warning" />
              <span className="mono truncate text-xs text-sidebar-foreground">{file}</span>
              <Badge tone="warning">{hits.length} 条</Badge>
            </div>
            <div className="col-stack gap-[6px] px-[10px] py-[8px]">
              {hits.map((hit, index) => (
                <div key={`${hit.rule}-${index}`} className="col-stack gap-[2px]">
                  <div className="flex flex-wrap items-center gap-[6px]">
                    <span className="text-xs font-semibold text-sidebar-foreground">{hit.label || hit.reason}</span>
                    <span className="text-xs text-muted-foreground">
                      <span className="mono">
                        {hit.rule}@{hit.rule_version ?? 1}
                      </span>{' '}
                      · 第 <span className="tnum">{hit.line}</span> 行
                    </span>
                  </div>
                  <div className="text-xs leading-relaxed text-muted-foreground">{hit.reason}</div>
                  {hit.snippet && (
                    <code className="mono break-all rounded bg-muted px-[6px] py-[3px] text-xs text-sidebar-foreground">
                      {hit.snippet}
                    </code>
                  )}
                </div>
              ))}
            </div>
          </div>
        ))}

        <div className="break-all text-xs text-muted-foreground">
          规则集 <span className="tnum">{challenge.rulesetVersion || '—'}</span> · 指纹{' '}
          <span className="mono">{challenge.fingerprint.slice(0, 16)}</span>…
        </div>
        <div className="field-hint">
          指纹与本次提交的正文、附件绑定：确认后再改动内容，需要重新确认。
        </div>
      </div>
    </Modal>
  )
}
