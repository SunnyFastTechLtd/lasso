import { useEffect, useState } from 'react'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { CodeBlock } from '@/components/ui/CodeBlock'
import { Select } from '@/components/ui/Field'
import { Icon } from '@/components/ui/Icon'
import { Modal } from '@/components/ui/Overlay'
import { Panel } from '@/components/ui/Panel'
import {
  useCompareSkillVersions,
  useRollbackSkill,
  useSkillVersion,
  useSkillVersions,
} from '@/api/hooks/useSkills'
import { skillsApi } from '@/api/skills'
import { toast } from '@/store/toast'
import { fmtBytes, fmtDateTime } from '@/utils/format'
import type { RiskAckPayload, SkillVersion } from '@/types/skill'
import { extractRiskChallenge, type RiskChallenge } from './shared'
import { RiskConfirmModal } from './RiskConfirmModal'

interface Props {
  open: boolean
  skillId: number
  skillName: string
  currentVersion: number
  editable: boolean
  onClose: () => void
}

/** C4 版本历史：列表 + 正文查看 + 版本差异 + 回滚（高危需重新确认） */
export function SkillVersionsModal({ open, skillId, skillName, currentVersion, editable, onClose }: Props) {
  const [left, setLeft] = useState<number | null>(null)
  const [right, setRight] = useState<number | null>(null)
  const [viewVersion, setViewVersion] = useState<number | null>(null)
  const [challenge, setChallenge] = useState<RiskChallenge | null>(null)
  const [rollbackTarget, setRollbackTarget] = useState<SkillVersion | null>(null)

  const { data, isLoading } = useSkillVersions(open ? skillId : null, open)
  const detail = useSkillVersion(open ? skillId : null, viewVersion)
  const compare = useCompareSkillVersions(open ? skillId : null, left, right)
  const rollback = useRollbackSkill()

  const versions = data?.versions ?? []

  useEffect(() => {
    if (!open || versions.length === 0) return
    const numbers = versions.map((v) => v.version).sort((a, b) => a - b)
    setRight((prev) => prev ?? numbers[numbers.length - 1])
    setLeft((prev) => prev ?? numbers[numbers.length - 2] ?? numbers[numbers.length - 1])
  }, [open, versions])

  useEffect(() => {
    if (!open) {
      setLeft(null)
      setRight(null)
      setViewVersion(null)
      setChallenge(null)
      setRollbackTarget(null)
    }
  }, [open])

  async function runRollback(ack?: RiskAckPayload) {
    if (!rollbackTarget) return
    try {
      const res = await rollback.mutateAsync({
        id: skillId,
        version: rollbackTarget.version,
        ack,
      })
      toast.success(res.detail || `已回滚到 v${rollbackTarget.version}`)
      setRollbackTarget(null)
      setChallenge(null)
    } catch (err) {
      const next = extractRiskChallenge(err)
      if (next && !ack) {
        setChallenge(next)
        return
      }
      toast.error((err as Error).message)
      setChallenge(null)
    }
  }

  return (
    <>
      <Modal
        open={open}
        title={`版本历史 · ${skillName}`}
        icon="History"
        wide
        onClose={onClose}
        footer={
          <>
            <span className="field-hint flex-1">回滚不会删除历史：会以目标版本内容生成一个新版本。</span>
            <Button variant="ghost" onClick={onClose}>
              关闭
            </Button>
          </>
        }
      >
        <div className="col-stack gap-[10px]">
          <Panel title="版本列表" icon="Layers" meta={`当前 v${currentVersion}`} padded>
            {isLoading ? (
              <div className="text-xs text-muted-foreground">读取版本…</div>
            ) : versions.length === 0 ? (
              <div className="field-hint">尚无审核通过版本。首次审核通过后会生成 v1。</div>
            ) : (
              <div className="col-stack gap-[4px]">
                {versions.map((version) => {
                  const isCurrent = version.version === currentVersion
                  return (
                    <div key={version.version} className="list-row">
                      <span className="row-main min-w-0">
                        <span className="row-title flex items-center gap-[6px]">
                          v{version.version}
                          {isCurrent && <Badge tone="success">当前生效</Badge>}
                          {version.risk_ack && <Badge tone="warning">已确认高危</Badge>}
                        </span>
                        <span className="row-sub text-xs text-muted-foreground">
                          {version.char_count} 字 · {version.files.length} 个附件 ·{' '}
                          {version.approved_by_name || '—'} · {fmtDateTime(version.approved_at)}
                          {version.change_summary ? ` · ${version.change_summary}` : ''}
                        </span>
                      </span>
                      <Button
                        quiet
                        variant="ghost"
                        icon={viewVersion === version.version ? 'ChevronDown' : 'ChevronRight'}
                        onClick={() =>
                          setViewVersion((prev) => (prev === version.version ? null : version.version))
                        }
                      >
                        正文
                      </Button>
                      {editable && !isCurrent && (
                        <Button quiet variant="ghost" icon="RotateCcw" onClick={() => setRollbackTarget(version)}>
                          回滚
                        </Button>
                      )}
                    </div>
                  )
                })}
              </div>
            )}

            {viewVersion !== null && (
              <div className="mt-[8px] border-t pt-[8px]">
                <div className="mb-[6px] flex items-center gap-[6px] text-xs text-muted-foreground">
                  <Icon name="FileText" size={12} />
                  v{viewVersion} 正文
                  {detail.data && (
                    <span>· <span className="tnum">{detail.data.files.length}</span> 个附件 · <span className="mono">{detail.data.body_hash.slice(0, 12)}…</span></span>
                  )}
                </div>
                {detail.isLoading ? (
                  <div className="text-xs text-muted-foreground">读取正文…</div>
                ) : (
                  <CodeBlock code={detail.data?.body ?? '（空）'} maxHeight={260} />
                )}
                {detail.data && detail.data.files.length > 0 && (
                  <div className="mt-[6px] flex flex-wrap gap-[4px]">
                    {detail.data.files.map((file) => (
                      <button
                        key={file.id}
                        type="button"
                        className="chip mono"
                        onClick={() =>
                          skillsApi
                            .downloadFile(file.download_url, file.path.split('/').pop() || file.path)
                            .catch((err: Error) => toast.error(err.message))
                        }
                      >
                        {file.path} · {fmtBytes(file.size)}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
          </Panel>

          <Panel title="版本差异" icon="GitCompare" padded>
            {versions.length < 2 ? (
              <div className="field-hint">至少有两个版本后才可对比。</div>
            ) : (
              <>
                <div className="flex flex-wrap items-center gap-[8px]">
                  <Select
                    className="w-[120px]"
                    value={left ?? ''}
                    onChange={(e) => setLeft(Number(e.target.value))}
                  >
                    {versions.map((v) => (
                      <option key={v.version} value={v.version}>
                        v{v.version}
                      </option>
                    ))}
                  </Select>
                  <Icon name="ArrowRight" size={14} className="text-muted-foreground" />
                  <Select
                    className="w-[120px]"
                    value={right ?? ''}
                    onChange={(e) => setRight(Number(e.target.value))}
                  >
                    {versions.map((v) => (
                      <option key={v.version} value={v.version}>
                        v{v.version}
                      </option>
                    ))}
                  </Select>
                  {compare.data && (
                    <span className="text-xs text-muted-foreground">
                      正文 +{compare.data.body.stats.added} / -{compare.data.body.stats.removed} 行 ·{' '}
                      附件新增 {compare.data.files.added.length}、移除 {compare.data.files.removed.length}、变更{' '}
                      {compare.data.files.changed.length}
                    </span>
                  )}
                </div>

                {compare.isLoading && <div className="mt-[6px] text-xs text-muted-foreground">对比中…</div>}
                {compare.error && (
                  <div className="mt-[6px] text-xs text-state-error">{(compare.error as Error).message}</div>
                )}
                {compare.data && (
                  <div className="mt-[8px] max-h-[300px] overflow-auto rounded-md border">
                    {compare.data.body.rows.map((row, index) => (
                      <div
                        key={index}
                        className={`flex gap-[8px] px-[8px] py-[1px] text-xs ${
                          row.type === 'add'
                            ? 'bg-state-success-soft text-state-success'
                            : row.type === 'del'
                              ? 'bg-state-error-soft text-state-error'
                              : 'text-muted-foreground'
                        }`}
                      >
                        <span className="tnum w-[34px] flex-none text-right opacity-60">{row.left_no ?? ''}</span>
                        <span className="tnum w-[34px] flex-none text-right opacity-60">{row.right_no ?? ''}</span>
                        <span className="mono whitespace-pre-wrap break-all">{row.text || ' '}</span>
                      </div>
                    ))}
                  </div>
                )}

                {compare.data && (
                  <div className="mt-[8px] col-stack gap-[3px]">
                    {compare.data.files.added.map((f) => (
                      <div key={`a-${f.path}`} className="text-xs text-state-success">
                        + {f.path}（{fmtBytes(f.size)}）
                      </div>
                    ))}
                    {compare.data.files.removed.map((f) => (
                      <div key={`r-${f.path}`} className="text-xs text-state-error">
                        − {f.path}（{fmtBytes(f.size)}）
                      </div>
                    ))}
                    {compare.data.files.changed.map((f) => (
                      <div key={`c-${f.path}`} className="text-xs text-state-warning">
                        ~ {f.path}（{fmtBytes(f.old_size)} → {fmtBytes(f.new_size)}）
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}
          </Panel>
        </div>
      </Modal>

      <RiskConfirmModal
        challenge={challenge}
        title={`回滚到 v${rollbackTarget?.version ?? ''} 命中高危规则`}
        busy={rollback.isPending}
        onCancel={() => setChallenge(null)}
        onConfirm={() =>
          void runRollback({ risk_ack: true, risk_fingerprint: challenge?.fingerprint ?? '' })
        }
      />

      <Modal
        open={rollbackTarget !== null && challenge === null}
        title={`回滚到 v${rollbackTarget?.version ?? ''}`}
        icon="RotateCcw"
        onClose={() => setRollbackTarget(null)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setRollbackTarget(null)} disabled={rollback.isPending}>
              取消
            </Button>
            <Button
              variant="primary"
              icon="RotateCcw"
              loading={rollback.isPending}
              onClick={() => void runRollback()}
            >
              确认回滚
            </Button>
          </>
        }
      >
        <div className="col-stack gap-[6px] text-xs leading-relaxed text-muted-foreground">
          <div>将把正文与附件恢复为 v{rollbackTarget?.version} 的内容，并生成一个新版本。</div>
          <div>已发布智能体不受影响：技能版本与 A8 版本快照解耦，下一条消息按新版本注入。</div>
          <div>历史版本不会被覆盖，可再次回滚。</div>
        </div>
      </Modal>
    </>
  )
}
