import { useState } from 'react'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { EmptyState } from '@/components/ui/EmptyState'
import { Icon } from '@/components/ui/Icon'
import { Modal } from '@/components/ui/Overlay'
import { Pagination } from '@/components/ui/Pagination'
import { SectionLabel } from '@/components/ui/Panel'
import { DataTable } from '@/components/ui/DataTable'
import {
  useAgentVersion,
  useAgentVersions,
  useAgentVersionCompare,
  useRollbackAgentVersion,
} from '@/api/hooks/useAgents'
import { ApiError } from '@/api/request'
import { toast } from '@/store/toast'
import { fmtDateTime } from '@/utils/format'
import { DiffLine } from './PublishModal'
import type { AgentVersion, RollbackResponse, SnapshotDependency } from '@/types/agent'

interface Props {
  open: boolean
  agentId: number | null
  agentName: string
  currentVersion: number
  onClose: () => void
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex gap-[8px] py-[3px] text-xs">
      <span className="w-[92px] flex-none text-muted-foreground">{k}</span>
      <span className="min-w-0 break-words">{v}</span>
    </div>
  )
}

/** 快照引用已不可用 / 已降级的显式标注（A8 已知缺陷） */
function DepLine({ dep }: { dep: SnapshotDependency }) {
  return (
    <div className="flex items-start gap-[7px] py-[3px]">
      <Badge tone={dep.level === 'blocking' ? 'error' : 'warning'}>
        {dep.level === 'blocking' ? '不可用' : '告警'}
      </Badge>
      <span className="min-w-0 text-xs leading-[1.6]">
        {dep.message}
        {dep.version !== undefined && (
          <span className="ml-[6px] text-muted-foreground">〔v{dep.version}〕</span>
        )}
      </span>
    </div>
  )
}

export function VersionsModal({ open, agentId, agentName, currentVersion, onClose }: Props) {
  const [page, setPage] = useState(1)
  const [picked, setPicked] = useState<number | null>(null)
  const [selected, setSelected] = useState<number[]>([])
  const [blocking, setBlocking] = useState<SnapshotDependency[] | null>(null)
  const [ackWarnings, setAckWarnings] = useState<SnapshotDependency[] | null>(null)
  const [ackChecked, setAckChecked] = useState(false)

  const { data: list, isFetching } = useAgentVersions(open ? agentId : null, page, open)
  const { data: detail, isLoading: snapLoading } = useAgentVersion(
    open ? agentId : null,
    open && selected.length < 2 ? picked : null,
  )
  const compare = useAgentVersionCompare(
    open ? agentId : null,
    selected.length === 2 ? selected[0] : null,
    selected.length === 2 ? selected[1] : null,
  )
  const rollback = useRollbackAgentVersion()

  function resetRollbackState() {
    setBlocking(null)
    setAckWarnings(null)
    setAckChecked(false)
  }

  function pick(v: AgentVersion) {
    setPicked(v.version === picked ? null : v.version)
    resetRollbackState()
  }

  function toggle(v: AgentVersion) {
    resetRollbackState()
    setSelected((prev) => {
      if (prev.includes(v.version)) return prev.filter((n) => n !== v.version)
      if (prev.length >= 2) {
        toast.info('最多勾选两个版本进行对比')
        return prev
      }
      return [...prev, v.version]
    })
  }

  function doRollback(version: number, riskAck = false) {
    if (agentId === null) return
    rollback.mutate(
      { id: agentId, version, risk_ack: riskAck },
      {
        onSuccess: (res: RollbackResponse) => {
          toast.success(res.detail)
          setPicked(null)
          setSelected([])
          resetRollbackState()
          onClose()
        },
        onError: (err) => {
          if (err instanceof ApiError) {
            const body = err.body as Partial<RollbackResponse> | null
            if (body?.code === 'risk_ack_required') {
              setAckWarnings(body.warnings ?? [])
              setBlocking(null)
              setAckChecked(false)
            } else if (body?.blocking?.length) {
              setBlocking(body.blocking)
              setAckWarnings(null)
            }
            toast.error(err.message)
            return
          }
          toast.error((err as Error).message)
        },
      },
    )
  }

  const snap = detail?.snapshot

  return (
    <Modal
      open={open}
      title={`发布历史 · ${agentName}`}
      icon="History"
      wide
      onClose={() => {
        setPicked(null)
        setSelected([])
        resetRollbackState()
        setPage(1)
        onClose()
      }}
      footer={<Button onClick={onClose}>关闭</Button>}
    >
      {isFetching && !list && (
        <div className="flex items-center gap-[7px] py-[20px] text-xs text-muted-foreground">
          <Icon name="LoaderCircle" size={14} className="animate-spin" /> 正在加载发布历史…
        </div>
      )}
      {list && list.results.length === 0 && (
        <EmptyState
          title="暂无发布历史"
          description="智能体每次通过发布检查并成功发布，都会在此沉淀一个只读版本快照。"
        />
      )}
      {list && list.results.length > 0 && (
        <>
          <div className="mb-[6px] flex items-center justify-between text-xs text-muted-foreground">
            <span>勾选任意两个版本进行对比（最多 2 个）；点击行可查看单版本快照</span>
            {selected.length === 2 && (
              <Button quiet onClick={() => setSelected([])}>
                退出对比
              </Button>
            )}
          </div>
          <DataTable
            columns={[
              {
                key: 'pick',
                title: '',
                width: 34,
                render: (v: AgentVersion) => (
                  <input
                    type="checkbox"
                    className="size-[13px] accent-primary"
                    aria-label={`选择 v${v.version} 参与对比`}
                    checked={selected.includes(v.version)}
                    onClick={(e) => e.stopPropagation()}
                    onChange={() => toggle(v)}
                  />
                ),
              },
              {
                key: 'version',
                title: '版本',
                width: 96,
                mono: true,
                render: (v: AgentVersion) => (
                  <span className="flex items-center gap-[6px]">
                    v{v.version}
                    {v.version === currentVersion && (
                      <span className="text-xs text-state-success">当前版本</span>
                    )}
                  </span>
                ),
              },
              { key: 'by', title: '发布人', width: 110, render: (v: AgentVersion) => v.published_by_name || '—' },
              {
                key: 'at',
                title: '发布时间',
                width: 150,
                render: (v: AgentVersion) => <span className="text-xs">{fmtDateTime(v.created_at)}</span>,
              },
              {
                key: 'summary',
                title: '变更摘要',
                render: (v: AgentVersion) => (
                  <span className="row-sub">{v.change_summary || '首次发布'}</span>
                ),
              },
            ]}
            rows={list.results}
            rowKey={(v) => v.version}
            onRowClick={pick}
          />
          <Pagination
            page={list.page}
            totalPages={list.total_pages}
            count={list.count}
            pageSize={list.page_size}
            onPageChange={setPage}
          />

          {selected.length === 2 && (
            <div className="mt-[10px] rounded border border-border/60 px-[12px] py-[10px]">
              <div className="flex items-center justify-between gap-[8px]">
                <SectionLabel>
                  版本对比
                  {compare.data && ` v${compare.data.base_version} → v${compare.data.target_version}`}
                </SectionLabel>
                {compare.data && (
                  <span className="flex items-center gap-[6px]">
                    {compare.data.unavailable_count > 0 && (
                      <Badge tone="error">{compare.data.unavailable_count} 项不可用</Badge>
                    )}
                    {compare.data.warning_count > 0 && (
                      <Badge tone="warning">{compare.data.warning_count} 项告警</Badge>
                    )}
                  </span>
                )}
              </div>

              {compare.isLoading && (
                <div className="mt-[8px] flex items-center gap-[7px] text-xs text-muted-foreground">
                  <Icon name="LoaderCircle" size={13} className="animate-spin" /> 正在比对…
                </div>
              )}
              {compare.error && (
                <div className="mt-[8px] text-xs text-state-error">
                  {(compare.error as Error).message}
                </div>
              )}
              {compare.data && (
                <div className="mt-[8px] space-y-[10px]">
                  {compare.data.dependencies.length > 0 && (
                    <div className="rounded border border-state-warning/40 bg-state-warning/5 px-[10px] py-[8px]">
                      <div className="mb-[4px] text-xs text-muted-foreground">
                        该版本引用的部门 / 来源 / 技能 / 知识库按名称或 slug 记录，
                        被删除或改名后无法可靠还原，回滚前请确认：
                      </div>
                      {compare.data.dependencies.map((dep, i) => (
                        <DepLine key={`${dep.code}-${dep.version}-${i}`} dep={dep} />
                      ))}
                    </div>
                  )}

                  {compare.data.no_change && (
                    <div className="text-xs text-muted-foreground">两个版本配置无实质差异。</div>
                  )}
                  {!compare.data.no_change &&
                    compare.data.sections.map((section) => (
                      <div key={section.key}>
                        <SectionLabel>{section.title}</SectionLabel>
                        <div className="mt-[4px] space-y-[4px]">
                          {section.items.map((item, i) => (
                            <DiffLine key={`${item.field}-${i}`} item={item} />
                          ))}
                        </div>
                      </div>
                    ))}
                </div>
              )}
            </div>
          )}

          {picked !== null && selected.length < 2 && (
            <div className="mt-[10px] rounded border border-border/60 px-[12px] py-[10px]">
              <div className="flex items-center justify-between gap-[8px]">
                <SectionLabel>v{picked} 发布快照（只读）</SectionLabel>
                <Button
                  quiet
                  variant="primary"
                  icon="RotateCcw"
                  loading={rollback.isPending}
                  disabled={picked === currentVersion}
                  title={picked === currentVersion ? '该版本已是当前线上版本' : undefined}
                  onClick={() => doRollback(picked)}
                >
                  以此版本重新发布
                </Button>
              </div>
              {picked === currentVersion && (
                <div className="mt-[4px] text-xs text-muted-foreground">
                  当前已发布的就是 v{picked}，无需回滚。
                </div>
              )}
              {snapLoading && (
                <div className="mt-[8px] flex items-center gap-[7px] text-xs text-muted-foreground">
                  <Icon name="LoaderCircle" size={13} className="animate-spin" /> 加载快照…
                </div>
              )}
              {snap && (
                <div className="mt-[8px] space-y-[2px]">
                  <Row k="名称/编码" v={<span>{snap.name} <span className="mono text-xs text-muted-foreground">{snap.slug}</span></span>} />
                  <Row k="模型" v={<span className="mono">{snap.model_name || '—'}</span>} />
                  <Row
                    k="档位"
                    v={`语音 ${snap.speech_tier} · 检索 ${snap.retrieval_tier}${snap.enable_thinking ? ' · 深度思考开' : ''}`}
                  />
                  <Row
                    k="工具挂载"
                    v={
                      snap.mcp_servers.length === 0 ? (
                        <span className="text-muted-foreground">无</span>
                      ) : (
                        <span className="flex flex-col gap-[3px]">
                          {snap.mcp_servers.map((m) => (
                            <span key={m.server} className="text-xs">
                              <span className="font-medium">{m.server_name}</span>
                              {m.tool_names.length > 0 ? (
                                <span className="mono text-muted-foreground"> · {m.tool_names.join(', ')}</span>
                              ) : (
                                <span className="text-muted-foreground"> · （未勾选工具）</span>
                              )}
                            </span>
                          ))}
                        </span>
                      )
                    }
                  />
                  <Row
                    k="允许工具"
                    v={<span className="mono text-xs">{snap.allow_tool_names.join(', ') || '—'}</span>}
                  />
                  <Row
                    k="技能"
                    v={snap.skills.length ? <span className="mono text-xs">{snap.skills.join(', ')}</span> : <span className="text-muted-foreground">无</span>}
                  />
                  <Row
                    k="内置能力"
                    v={snap.builtin_capabilities.length ? snap.builtin_capabilities.join(', ') : <span className="text-muted-foreground">无</span>}
                  />
                  <Row k="知识库" v={snap.knowledge_base_ids.length ? snap.knowledge_base_ids.join(', ') : <span className="text-muted-foreground">无</span>} />
                  <Row k="权限档位" v={<span className="mono">{snap.permission_tier}</span>} />
                  <Row
                    k="可见范围"
                    v={`${snap.visibility}${snap.allowed_departments.length ? ` · ${snap.allowed_departments.join('、')}` : ''}${snap.include_sub_departments ? ' · 含子部门' : ''}`}
                  />
                  <Row
                    k="提示词"
                    v={<pre className="mt-[2px] max-h-[220px] overflow-auto whitespace-pre-wrap rounded bg-muted/50 p-[8px] text-xs leading-[1.6]">{snap.prompt || '（空）'}</pre>}
                  />
                </div>
              )}

              {blocking && blocking.length > 0 && (
                <div className="mt-[10px] rounded border border-state-error/40 bg-state-error/5 px-[10px] py-[8px]">
                  <div className="mb-[4px] text-xs text-state-error">
                    该版本引用的对象已不可用，回滚会得到不可运行的配置，已阻断：
                  </div>
                  {blocking.map((dep, i) => (
                    <DepLine key={`${dep.code}-${i}`} dep={dep} />
                  ))}
                </div>
              )}
              {ackWarnings && ackWarnings.length > 0 && (
                <div className="mt-[10px] rounded border border-state-warning/40 bg-state-warning/5 px-[10px] py-[8px]">
                  <div className="mb-[4px] text-xs text-state-warning">
                    该版本引用的对象已停用或改名，回滚后能力/可见范围会降级：
                  </div>
                  {ackWarnings.map((dep, i) => (
                    <DepLine key={`${dep.code}-${i}`} dep={dep} />
                  ))}
                  <label className="mt-[8px] flex items-start gap-[6px] text-xs">
                    <input
                      type="checkbox"
                      className="mt-[3px] size-[13px] accent-primary"
                      checked={ackChecked}
                      onChange={(e) => setAckChecked(e.target.checked)}
                    />
                    <span>
                      我已了解风险，仍按 v{picked} 重新发布
                      <span className="block text-xs text-muted-foreground">
                        留痕字段：risk_ack；确认人与时间会写入审计
                      </span>
                    </span>
                  </label>
                  <Button
                    className="mt-[8px]"
                    variant="primary"
                    loading={rollback.isPending}
                    disabled={!ackChecked}
                    onClick={() => picked !== null && doRollback(picked, true)}
                  >
                    确认并重新发布
                  </Button>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </Modal>
  )
}
