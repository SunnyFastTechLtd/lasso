import { useEffect, useState } from 'react'
import { PageContainer, PageHeader } from '@/components/AppShell'
import { Button, IconButton } from '@/components/ui/Button'
import { EmptyState } from '@/components/ui/EmptyState'
import { Input } from '@/components/ui/Field'
import { Icon } from '@/components/ui/Icon'
import { DataTable } from '@/components/ui/DataTable'
import { Pagination } from '@/components/ui/Pagination'
import { Panel, SectionLabel } from '@/components/ui/Panel'
import { ConfirmDialog } from '@/components/ui/Overlay'
import { Seg } from '@/components/ui/Seg'
import {
  useAgentAction,
  useAgents,
  useAgentSummary,
  useCloneAgent,
  useDeleteAgent,
  useImportRequestSummary,
  useRestoreBuiltin,
} from '@/api/hooks/useAgents'
import { agentsApi } from '@/api/agents'
import { ApiError } from '@/api/request'
import { useUserStore } from '@/store/user'
import { toast } from '@/store/toast'
import { fmtRelative } from '@/utils/format'
import type { Agent, AgentDeleteConflictBody, AgentListItem, AgentListParams } from '@/types/agent'
import { AdminOnlyBadge, AgentStatusBadge, MountSummary, PendingChangeBadge } from './shared'
import { AgentCreate } from './AgentCreate'
import { AgentForm } from './AgentForm'
import { AgentImportModal } from './ImportModal'
import { NuwaWizard } from './Nuwa'
import { PublishModal } from './PublishModal'
import { VersionsModal } from './VersionsModal'

type PendingAction = { agent: AgentListItem; action: 'disable' | 'enable' | 'archive' | 'restore' }

const STATUS_SEG = [
  { value: '', label: '全部' },
  { value: 'draft', label: '草稿' },
  { value: 'published', label: '已发布' },
  { value: 'disabled', label: '已停用' },
  { value: 'archived', label: '已归档' },
]

export default function AgentsPage() {
  const isAdmin = useUserStore((s) => s.user?.is_staff ?? false)

  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [status, setStatus] = useState('')
  const [visibility, setVisibility] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [search, setSearch] = useState('')

  const [createOpen, setCreateOpen] = useState(false)
  const [nuwaOpen, setNuwaOpen] = useState(false)
  const [editorId, setEditorId] = useState<number | null>(null)
  const [publishTarget, setPublishTarget] = useState<{ id: number; name: string } | null>(null)
  const [versionsTarget, setVersionsTarget] = useState<{ id: number; name: string; current: number } | null>(null)
  const [cloneTarget, setCloneTarget] = useState<AgentListItem | null>(null)
  const [cloneName, setCloneName] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<AgentListItem | null>(null)
  const [archiveAfterDelete, setArchiveAfterDelete] = useState<AgentListItem | null>(null)
  const [restoreFactoryTarget, setRestoreFactoryTarget] = useState<AgentListItem | null>(null)
  const [pending, setPending] = useState<PendingAction | null>(null)
  const [importOpen, setImportOpen] = useState(false)
  const [exportingId, setExportingId] = useState<number | null>(null)
  const [exportedHint, setExportedHint] = useState('')

  useEffect(() => {
    const t = window.setTimeout(() => {
      setSearch(searchInput.trim())
      setPage(1)
    }, 350)
    return () => window.clearTimeout(t)
  }, [searchInput])

  const params: AgentListParams = {
    page,
    page_size: pageSize,
    ...(search ? { search } : {}),
    status: status || 'draft,published,disabled',
    ...(visibility ? { visibility } : {}),
  }

  const { data, isFetching } = useAgents(params)
  const { data: summary } = useAgentSummary()
  const { data: importSummary } = useImportRequestSummary()
  const action = useAgentAction()
  const clone = useCloneAgent()
  const del = useDeleteAgent()
  const restoreFactory = useRestoreBuiltin()

  const counts = summary

  function openEditor(agent: AgentListItem | Agent) {
    setEditorId(agent.id)
  }

  async function runAction(target: AgentListItem, next: 'disable' | 'enable' | 'archive' | 'restore') {
    try {
      await action.mutateAsync({ id: target.id, action: next })
      const label = { disable: '已停用', enable: '已启用', archive: '已归档', restore: '已恢复为草稿' }[next]
      toast.success(`${label}「${target.name}」`)
      setPending(null)
    } catch (err) {
      toast.error((err as Error).message)
      setPending(null)
    }
  }

  function askAction(target: AgentListItem, next: 'disable' | 'enable' | 'archive' | 'restore') {
    if (next === 'enable' || next === 'restore') {
      void runAction(target, next)
      return
    }
    setPending({ agent: target, action: next })
  }

  async function confirmDelete() {
    if (!deleteTarget) return
    const target = deleteTarget
    try {
      await del.mutateAsync(target.id)
      toast.success(`已删除草稿「${target.name}」`)
      setDeleteTarget(null)
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        const body = err.body as Partial<AgentDeleteConflictBody> | null
        if (body?.code === 'agent_not_archived') {
          setDeleteTarget(null)
          setArchiveAfterDelete(target)
          return
        }
      }
      toast.error((err as Error).message)
      setDeleteTarget(null)
    }
  }

  async function archiveThenDelete() {
    if (!archiveAfterDelete) return
    try {
      await action.mutateAsync({ id: archiveAfterDelete.id, action: 'archive' })
      toast.success(`「${archiveAfterDelete.name}」已归档：再点一次删除即可完成清理`)
    } catch (err) {
      toast.error((err as Error).message)
    } finally {
      setArchiveAfterDelete(null)
    }
  }

  async function doClone() {
    if (!cloneTarget) return
    try {
      const created = await clone.mutateAsync({ id: cloneTarget.id, name: cloneName.trim() || undefined })
      toast.success(`已克隆为草稿「${created.name}」，可继续装配`)
      setCloneTarget(null)
      setCloneName('')
      openEditor(created)
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  async function doRestoreFactory() {
    if (!restoreFactoryTarget) return
    try {
      await restoreFactory.mutateAsync(restoreFactoryTarget.id)
      toast.success(`已恢复出厂配置「${restoreFactoryTarget.name}」，重新发布后使用端生效`)
      setRestoreFactoryTarget(null)
    } catch (err) {
      toast.error((err as Error).message)
      setRestoreFactoryTarget(null)
    }
  }

  async function exportAgent(agent: AgentListItem) {
    setExportingId(agent.id)
    try {
      const payload = await agentsApi.exportAgent(agent.id)
      const filename = `agent-${agent.slug}-${new Date().toISOString().slice(0, 10)}.json`
      setExportedHint(`已导出「${agent.name}」：${filename}（不含密钥，导入后需重新填写）`)
      toast.success(`已导出「${agent.name}」，包内不含密钥，导入需重新填写`)
      // 先渲染提示再触发下载：部分浏览器在下载时会让当前页面短暂失焦
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = filename
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (err) {
      toast.error((err as Error).message)
    } finally {
      setExportingId(null)
    }
  }

  const emptyGuide = counts && counts.total === 0 && summary?.guide

  return (
    <PageContainer>
      <PageHeader
        title="智能体装配（Agents）"
        subtitle="装配 ≠ 上线：草稿通过发布检查并显式发布后，使用端才可见"
        actions={
          <>
            <Button icon="Info" onClick={() => toast.info('四分区装配：基础信息 → 模型与提示词 → 能力挂载 → 权限与可见范围；发布须经检查闸门')}>
              装配指引
            </Button>
            <Button
              icon="Upload"
              onClick={() => setImportOpen(true)}
              title="跨环境导入：提交后需另一名管理员审批，通过后才创建草稿"
            >
              导入{importSummary?.actionable ? `（待审 ${importSummary.actionable}）` : ''}
            </Button>
            <Button
              icon="Sparkles"
              onClick={() => setNuwaOpen(true)}
              title="用一段自然语言生成智能体骨架，并在线挑工具、技能与资料"
            >
              女娲模式
            </Button>
            <Button variant="primary" icon="Plus" onClick={() => setCreateOpen(true)}>
              新建智能体
            </Button>
          </>
        }
      />

      {exportedHint && (
        <div className="mt-[10px] flex items-center gap-[6px] text-xs text-state-success" role="status">
          <Icon name="Check" size={13} />
          <span className="min-w-0 truncate">{exportedHint}</span>
        </div>
      )}

      <div className="mt-[14px] grid grid-cols-2 gap-[10px] md:grid-cols-5">
        {(
          [
            ['草稿', counts?.draft, ''],
            ['已发布', counts?.published, 'success'],
            ['已停用', counts?.disabled, 'warning'],
            ['已归档', counts?.archived, ''],
            ['全部（含内置 ' + (counts?.builtin ?? '—') + '）', counts?.total, ''],
          ] as const
        ).map(([label, value, tone]) => (
          <div key={label} className="panel px-[12px] py-[10px]">
            <SectionLabel>{label}</SectionLabel>
            <div
              className={`metric-value mt-[4px] ${tone === 'success' ? 'text-state-success' : tone === 'warning' ? 'text-state-error' : ''}`}
            >
              {value ?? <span className="text-muted-foreground">—</span>}
            </div>
          </div>
        ))}
      </div>

      <div className="page-toolbar">
        <div className="relative w-[240px]">
          <Icon name="Search" size={13} className="pointer-events-none absolute left-[9px] top-1/2 -translate-y-1/2 text-muted-foreground" />
          <Input
            className="pl-[28px]"
            value={searchInput}
            placeholder="搜索名称 / 编码 / 描述…"
            onChange={(e) => setSearchInput(e.target.value)}
          />
        </div>
        <Seg
          items={STATUS_SEG}
          value={status}
          onChange={(v) => {
            setStatus(v)
            setPage(1)
          }}
        />
        <Seg
          items={[
            { value: '', label: '全部范围' },
            { value: 'private', label: '仅自己' },
            { value: 'department', label: '部门' },
            { value: 'public', label: '全公司' },
          ]}
          value={visibility}
          onChange={(v) => {
            setVisibility(v)
            setPage(1)
          }}
        />
        <span className="toolbar-note">
          {isAdmin ? '管理端视图：列表已按可见范围过滤' : '使用端视图：仅显示你可见的智能体'}
        </span>
      </div>

      <Panel padded={false}>
        {emptyGuide ? (
          <EmptyState
            icon="Bot"
            title="还没有智能体"
            description={summary?.guide}
            action={
              <Button variant="primary" icon="Plus" onClick={() => setCreateOpen(true)}>
                新建第一个智能体
              </Button>
            }
          />
        ) : (
          <>
            <DataTable
              className="data-table-fixed"
              columns={[
                {
                  key: 'name',
                  title: '名称',
                  render: (a) => (
                    <span className="row-main">
                      <span className="row-title flex items-center gap-[6px]">
                        {a.icon && <Icon name={a.icon} size={13} className="text-muted-foreground" />}
                        {a.name}
                        {(a.is_builtin || a.is_customized) && (
                          <span
                            className="rounded bg-muted px-[5px] py-[1px] text-xs text-muted-foreground"
                            title={a.is_builtin ? (a.is_customized ? '内置智能体，已改动出厂配置，可在操作列恢复出厂' : '出厂内置') : '已定制'}
                          >
                            {a.is_builtin ? (a.is_customized ? '内置 · 已定制' : '内置') : '已定制'}
                          </span>
                        )}
                        {a.visibility === 'admin_only' && <AdminOnlyBadge />}
                      </span>
                      <span className="row-sub truncate">
                        <span className="mono">{a.slug}</span>
                        {a.description ? ` · ${a.description}` : ''}
                      </span>
                    </span>
                  ),
                },
                {
                  key: 'status',
                  title: '状态',
                  width: 150,
                  render: (a) => (
                    <span className="flex flex-wrap items-center gap-[5px]">
                      <AgentStatusBadge status={a.status} display={a.status_display} />
                      {a.pending_changes && <PendingChangeBadge />}
                    </span>
                  ),
                },
                {
                  key: 'version',
                  title: '版本',
                  width: 68,
                  mono: true,
                  render: (a) => (a.current_version > 0 ? `v${a.current_version}` : <span className="text-muted-foreground">—</span>),
                },
                {
                  key: 'mounts',
                  title: '挂载',
                  width: 170,
                  secondary: true,
                  render: (a) => <MountSummary m={a.mount_summary} />,
                },
                {
                  key: 'model',
                  title: '模型',
                  width: 118,
                  secondary: true,
                  render: (a) =>
                    a.model_name ? (
                      <span className="mono text-xs">{a.model_name}</span>
                    ) : (
                      <span className="text-xs text-state-warning">未配置</span>
                    ),
                  },
                {
                  key: 'updated',
                  title: '最近修改',
                  width: 110,
                  secondary: true,
                  render: (a) => (
                    <span className="text-xs text-muted-foreground">
                      {fmtRelative(a.updated_at)}
                      {a.updated_by_name && <span className="block">{a.updated_by_name}</span>}
                    </span>
                  ),
                },
                {
                  key: 'ops',
                  title: '操作',
                  width: 236,
                  render: (a) => (
                    <span className="flex items-center justify-end gap-[3px]" onClick={(e) => e.stopPropagation()}>
                      {(a.status === 'draft' || a.status === 'published') && (
                        <IconButton
                          icon="Rocket"
                          label={a.pending_changes ? '发布检查（存在未发布改动）' : '发布检查'}
                          className={a.pending_changes ? 'text-state-warning' : ''}
                          onClick={() => setPublishTarget({ id: a.id, name: a.name })}
                        />
                      )}
                      <IconButton
                        icon="Download"
                        label="导出配置（跨环境导入用，不含密钥）"
                        disabled={exportingId === a.id}
                        onClick={() => void exportAgent(a)}
                      />
                      <IconButton
                        icon="Pencil"
                        label={a.is_builtin && !isAdmin ? '查看装配（内置模板不可直接修改）' : a.is_builtin ? '装配（内置件，改动后可恢复出厂）' : '装配'}
                        onClick={() => openEditor(a)}
                      />
                      {a.is_builtin && a.is_customized && (
                        <IconButton icon="RotateCcw" label="恢复出厂配置" onClick={() => setRestoreFactoryTarget(a)} />
                      )}
                      {a.current_version > 0 && (
                        <IconButton icon="History" label="发布历史" onClick={() => setVersionsTarget({ id: a.id, name: a.name, current: a.current_version })} />
                      )}
                      {a.status !== 'archived' && (
                        <IconButton
                          icon="Copy"
                          label="克隆为草稿"
                          onClick={() => {
                            setCloneTarget(a)
                            setCloneName(`${a.name} 副本`)
                          }}
                        />
                      )}
                      {a.status === 'published' && (
                        <IconButton icon="PowerOff" label="停用（使用端即时隐藏）" onClick={() => askAction(a, 'disable')} />
                      )}
                      {a.status === 'disabled' && (
                        <IconButton icon="Power" label="恢复启用" onClick={() => askAction(a, 'enable')} />
                      )}
                      {a.status !== 'archived' ? (
                        <IconButton icon="Archive" label="归档（下线，保留历史）" onClick={() => askAction(a, 'archive')} />
                      ) : (
                        <IconButton icon="RotateCcw" label="恢复为草稿（需重新发布上线）" onClick={() => askAction(a, 'restore')} />
                      )}
                      {(a.status === 'draft' || a.status === 'archived') && (
                        <IconButton icon="Trash2" label="删除" onClick={() => setDeleteTarget(a)} />
                      )}
                    </span>
                  ),
                },
              ]}
              rows={data?.results ?? []}
              rowKey={(a) => a.id}
              onRowClick={openEditor}
              loading={isFetching && !data}
            />
            {data && data.results.length > 0 && (
              <Pagination
                page={data.page}
                totalPages={data.total_pages}
                count={data.count}
                pageSize={data.page_size}
                onPageChange={setPage}
                onPageSizeChange={(n) => {
                  setPageSize(n)
                  setPage(1)
                }}
              />
            )}
          </>
        )}
      </Panel>

      <NuwaWizard
        open={nuwaOpen}
        onClose={() => setNuwaOpen(false)}
        onCreated={(agentId) => {
          setNuwaOpen(false)
          setEditorId(agentId)
        }}
      />
      <AgentCreate
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(agent) => {
          setCreateOpen(false)
          openEditor(agent)
        }}
      />
      <AgentForm
        open={editorId !== null}
        agentId={editorId}
        onClose={() => setEditorId(null)}
      />
      <PublishModal
        open={publishTarget !== null}
        agentId={publishTarget?.id ?? null}
        agentName={publishTarget?.name ?? ''}
        onClose={() => setPublishTarget(null)}
      />
      <VersionsModal
        open={versionsTarget !== null}
        agentId={versionsTarget?.id ?? null}
        agentName={versionsTarget?.name ?? ''}
        currentVersion={versionsTarget?.current ?? 0}
        onClose={() => setVersionsTarget(null)}
      />
      <AgentImportModal open={importOpen} onClose={() => setImportOpen(false)} />

      <ConfirmDialog
        open={!!cloneTarget}
        title={`克隆「${cloneTarget?.name ?? ''}」`}
        message="复制全量配置为新的草稿（内置模板的定制入口），不影响源智能体。"
        confirmText="克隆并装配"
        loading={clone.isPending}
        onConfirm={doClone}
        onCancel={() => setCloneTarget(null)}
      >
        <div className="mt-[8px]">
          <Input value={cloneName} maxLength={30} placeholder="新名称（默认：原名 副本）" onChange={(e) => setCloneName(e.target.value)} />
        </div>
      </ConfirmDialog>

      <ConfirmDialog
        open={!!deleteTarget}
        title={`删除「${deleteTarget?.name ?? ''}」`}
        message={
          deleteTarget?.status === 'archived'
            ? '已归档智能体将被物理删除，发布历史一并移除，不可恢复。'
            : '仅草稿可删除；已发布/已停用的智能体需先归档再删除。'
        }
        tone="danger"
        confirmText="删除"
        loading={del.isPending}
        onConfirm={confirmDelete}
        onCancel={() => setDeleteTarget(null)}
      />

      <ConfirmDialog
        open={!!restoreFactoryTarget}
        title="恢复出厂配置"
        message={
          restoreFactoryTarget
            ? `「${restoreFactoryTarget.name}」的草稿配置将写回出厂基线，其上挂载的工具来源与技能会被移除。线上仍是 v${restoreFactoryTarget.current_version}，需重新发布才影响使用端。`
            : ''
        }
        confirmText="恢复出厂"
        loading={restoreFactory.isPending}
        onConfirm={doRestoreFactory}
        onCancel={() => setRestoreFactoryTarget(null)}
      />

      <ConfirmDialog
        open={!!archiveAfterDelete}
        title="需先归档"
        message={`「${archiveAfterDelete?.name ?? ''}」当前是线上/停用状态，后端要求先归档下线再删除。是否现在归档？归档后再点一次删除即可完成清理。`}
        confirmText="现在归档"
        loading={action.isPending}
        onConfirm={archiveThenDelete}
        onCancel={() => setArchiveAfterDelete(null)}
      />

      <ConfirmDialog
        open={!!pending}
        title={pending?.action === 'disable' ? `停用「${pending?.agent.name ?? ''}」` : `归档「${pending?.agent.name ?? ''}」`}
        message={
          pending?.action === 'disable'
            ? '停用后使用端即时隐藏该智能体，历史会话不受影响；可随时恢复启用。'
            : '归档即下线并保留发布历史，可在归档视图恢复；如需删除请归档后再操作。'
        }
        tone="danger"
        confirmText={pending?.action === 'disable' ? '停用' : '归档'}
        loading={action.isPending}
        onConfirm={() => pending && void runAction(pending.agent, pending.action)}
        onCancel={() => setPending(null)}
      />
    </PageContainer>
  )
}
