import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { PageContainer, PageHeader } from '@/components/AppShell'
import { Badge } from '@/components/ui/Badge'
import { Button, IconButton } from '@/components/ui/Button'
import { EmptyState } from '@/components/ui/EmptyState'
import { Input } from '@/components/ui/Field'
import { Icon } from '@/components/ui/Icon'
import { DataTable } from '@/components/ui/DataTable'
import { Pagination } from '@/components/ui/Pagination'
import { Panel, SectionLabel } from '@/components/ui/Panel'
import { ConfirmDialog, Modal } from '@/components/ui/Overlay'
import { Seg } from '@/components/ui/Seg'
import {
  useDeleteSkill,
  useSetSkillEnabled,
  useSkills,
  useSkillSummary,
} from '@/api/hooks/useSkills'
import { useUserStore } from '@/store/user'
import { toast } from '@/store/toast'
import { ApiError } from '@/api/request'
import { fmtRelative } from '@/utils/format'
import type { Skill, SkillDeleteConflictBody, SkillListParams } from '@/types/skill'
import { ChangedBadge, LiveBadge, SkillStatusBadge } from './shared'
import { SkillDrawer } from './SkillDrawer'
import { SkillForm } from './SkillForm'
import { MirrorModal } from './MirrorModal'
import { MarketBrowser } from '@/pages/Market'

const STATUS_SEG: { value: string; label: string }[] = [
  { value: '', label: '全部' },
  { value: 'pending', label: '待审核' },
  { value: 'active', label: '已启用' },
  { value: 'disabled', label: '已停用' },
  { value: 'invalid', label: '校验未通过' },
]

export default function SkillsPage() {
  const isAdmin = useUserStore((s) => s.user?.is_staff ?? false)
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = searchParams.get('tab') === 'market' ? 'market' : 'local'

  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [status, setStatus] = useState('')
  const [source, setSource] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [search, setSearch] = useState('')

  const [drawerId, setDrawerId] = useState<number | null>(null)
  const [formOpen, setFormOpen] = useState(false)
  const [formSkill, setFormSkill] = useState<Skill | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Skill | null>(null)
  const [conflict, setConflict] = useState<{ skill: Skill; body: SkillDeleteConflictBody } | null>(null)
  const [mirrorOpen, setMirrorOpen] = useState(false)

  useEffect(() => {
    const t = window.setTimeout(() => {
      setSearch(searchInput.trim())
      setPage(1)
    }, 350)
    return () => window.clearTimeout(t)
  }, [searchInput])

  const params: SkillListParams = {
    page,
    page_size: pageSize,
    ...(search ? { search } : {}),
    ...(status ? { status } : {}),
    ...(source ? { source } : {}),
  }

  const { data, isFetching } = useSkills(params)
  const { data: summary } = useSkillSummary()
  const setActive = useSetSkillEnabled()
  const del = useDeleteSkill()

  const bodyLimit = summary?.body_limit ?? 8000

  function openDrawer(skill: Skill) {
    setDrawerId(skill.id)
  }

  function openCreate() {
    setFormSkill(null)
    setFormOpen(true)
  }

  function openEdit(skill: Skill) {
    setFormSkill(skill)
    setFormOpen(true)
  }

  async function toggleEnabled(skill: Skill) {
    const enable = skill.status === 'disabled'
    try {
      await setActive.mutateAsync({ id: skill.id, enabled: enable })
      toast.success(enable ? `已恢复启用「${skill.name}」` : `已停用「${skill.name}」：即时对新会话生效`)
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return
    try {
      await del.mutateAsync(deleteTarget.id)
      toast.success(`已删除「${deleteTarget.name}」并从运行时镜像移除`)
      setDeleteTarget(null)
      if (drawerId === deleteTarget.id) setDrawerId(null)
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        const body = err.body as Partial<SkillDeleteConflictBody> | null
        if (body && body.code === 'skill_referenced') {
          setConflict({ skill: deleteTarget, body: body as SkillDeleteConflictBody })
          setDeleteTarget(null)
        } else {
          toast.error(err.message)
        }
      } else {
        toast.error((err as Error).message)
      }
    }
  }

  async function conflictDisableOnly() {
    if (!conflict) return
    try {
      await setActive.mutateAsync({ id: conflict.skill.id, enabled: false })
      toast.success(`已停用「${conflict.skill.name}」：挂载关系保留但视为未挂载`)
      setConflict(null)
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  const counts = summary?.counts

  const drawerFallback = drawerId !== null ? (data?.results.find((s) => s.id === drawerId) ?? null) : null

  if (tab === 'market') {
    return (
      <PageContainer>
        <PageHeader
          title="技能"
          subtitle="公开技能仓库检索：导入走单级审批，落库为「待审核」，通过后才进运行时"
          actions={
            <Seg
              items={[
                { value: 'local', label: '技能' },
                { value: 'market', label: '技能市场' },
              ]}
              value={tab}
              onChange={(next) => setSearchParams(next === 'market' ? { tab: 'market' } : {})}
            />
          }
        />
        <div className="mt-[14px]">
          <MarketBrowser kind="skill" embedded />
        </div>
      </PageContainer>
    )
  }

  return (
    <PageContainer>
      <PageHeader
        title="技能"
        subtitle="沉淀「怎么做」的方法论：上传≠生效，审核启用后才同步进运行时镜像；也可在市场里检索公开技能"
        actions={
          <div className="flex items-center gap-[8px]">
            <Seg
              items={[
                { value: 'local', label: '技能' },
                { value: 'market', label: '技能市场' },
              ]}
              value={tab}
              onChange={(next) => setSearchParams(next === 'market' ? { tab: 'market' } : {})}
            />
            {isAdmin ? (
              <div className="flex items-center gap-[8px]">
                <Button icon="Layers" onClick={() => setMirrorOpen(true)}>
                  运行时镜像
                </Button>
                <Button variant="primary" icon="Plus" onClick={openCreate}>
                  上传技能
                </Button>
              </div>
            ) : (
              <Badge tone="neutral">只读视图</Badge>
            )}
          </div>
        }
      />

      <div className="mt-[14px] grid grid-cols-2 gap-[10px] md:grid-cols-5">
        {(
          [
            ['总技能', counts?.total, ''],
            ['待审核', counts?.pending, 'warning'],
            ['已启用', counts?.active, 'success'],
            ['已停用', counts?.disabled, 'neutral'],
            ['校验未通过', counts?.invalid, 'error'],
          ] as const
        ).map(([label, value, tone]) => (
          <div key={label} className="panel px-[12px] py-[10px]">
            <SectionLabel>{label}</SectionLabel>
            <div
              className={`metric-value mt-[4px] ${
                tone === 'success' ? 'text-state-success' : tone === 'error' ? 'text-state-error' : tone === 'warning' ? 'text-state-warning' : ''
              }`}
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
            placeholder="搜索名称 / 适用说明…"
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
            { value: '', label: '全部来源' },
            { value: 'upload', label: '上传/粘贴' },
            { value: 'repo', label: '技能仓库' },
            { value: 'builtin', label: '出厂内置' },
          ]}
          value={source}
          onChange={(v) => {
            setSource(v)
            setPage(1)
          }}
        />
        <span className="toolbar-note">
          正文上限 {bodyLimit.toLocaleString()} 字
          {counts && counts.edited_pending > 0 && ` · 编辑在审 ${counts.edited_pending} 个`}
        </span>
      </div>

      <Panel padded={false}>
        <DataTable
          columns={[
            {
              key: 'name',
              title: '名称',
              render: (s) => (
                <span className="row-main">
                  <span className="row-title flex items-center gap-[6px]">
                    {s.name}
                    {s.live && <LiveBadge />}
                  </span>
                  <span className="row-sub truncate">
                    <span className="mono">{s.slug}</span>
                    {s.description ? ` · ${s.description}` : ''}
                  </span>
                </span>
              ),
            },
            {
              key: 'status',
              title: '状态',
              width: 150,
              render: (s) => (
                <span className="flex items-center gap-[6px]">
                  <SkillStatusBadge skill={s} />
                  {s.status === 'pending' && s.body_changed && <ChangedBadge />}
                </span>
              ),
            },
            { key: 'source', title: '来源', width: 96, render: (s) => s.source_display },
            {
              key: 'chars',
              title: '正文字数',
              width: 96,
              tnum: true,
              align: 'right',
              secondary: true,
              render: (s) => (
                <span className={s.char_count > bodyLimit ? 'text-state-error' : undefined}>
                  {s.char_count.toLocaleString()}
                </span>
              ),
            },
            {
              key: 'agents',
              title: '挂载',
              width: 84,
              secondary: true,
              render: (s) =>
                s.agent_count > 0 ? (
                  <Badge tone="primary">{s.agent_count} 个智能体</Badge>
                ) : (
                  <span className="text-xs text-muted-foreground">未挂载</span>
                ),
            },
            {
              key: 'updated',
              title: '更新',
              width: 104,
              render: (s) => <span className="text-xs text-muted-foreground">{fmtRelative(s.updated_at)}</span>,
            },
            {
              key: 'ops',
              title: '操作',
              width: 118,
              render: (s) =>
                isAdmin ? (
                  <span className="flex items-center justify-end gap-[4px]" onClick={(e) => e.stopPropagation()}>
                    <IconButton
                      icon="Pencil"
                      label={s.editable ? '编辑（改动后回到待审核）' : '内置技能不可编辑'}
                      disabled={!s.editable}
                      onClick={() => openEdit(s)}
                    />
                    {(s.status === 'active' || s.status === 'builtin' || s.status === 'disabled') && (
                      <IconButton
                        icon={s.status === 'disabled' ? 'Power' : 'PowerOff'}
                        label={s.status === 'disabled' ? '恢复启用' : '停用'}
                        onClick={() => toggleEnabled(s)}
                      />
                    )}
                    <IconButton
                      icon="Trash2"
                      label={s.editable ? '删除' : '内置技能不可删除'}
                      disabled={!s.editable}
                      onClick={() => setDeleteTarget(s)}
                    />
                  </span>
                ) : (
                  <span className="text-xs text-muted-foreground">—</span>
                ),
            },
          ]}
          rows={data?.results ?? []}
          rowKey={(s) => s.id}
          onRowClick={openDrawer}
          loading={!data}
          empty={
            <EmptyState
              icon="FileText"
              title={search ? '没有匹配的技能' : '还没有技能规范'}
              description={
                search
                  ? '换个关键词，或调整状态/来源筛选'
                  : '上传一份 SKILL.md，或让领域专家把「怎么做」写成规范：审核启用后所有装配了它的智能体即刻受益'
              }
              action={
                isAdmin ? (
                  <Button variant="primary" icon="Plus" onClick={openCreate}>
                    上传技能
                  </Button>
                ) : undefined
              }
            />
          }
        />
        <div className="flex items-center justify-between px-[12px]">
          {isFetching ? <span className="text-xs text-muted-foreground">读取中…</span> : <span />}
          {data && (
            <Pagination
              page={page}
              pageSize={pageSize}
              count={data.count}
              totalPages={Math.max(1, Math.ceil(data.count / pageSize))}
              onPageChange={setPage}
              onPageSizeChange={(v) => {
                setPageSize(v)
                setPage(1)
              }}
            />
          )}
        </div>
      </Panel>

      {drawerId !== null && (
        <SkillDrawer
          skillId={drawerId}
          fallback={drawerFallback}
          onClose={() => setDrawerId(null)}
          onEdit={(s) => {
            setDrawerId(null)
            openEdit(s)
          }}
          onDelete={(s) => {
            setDrawerId(null)
            setDeleteTarget(s)
          }}
        />
      )}

      <SkillForm open={formOpen} skill={formSkill} bodyLimit={bodyLimit} onClose={() => setFormOpen(false)} />

      <ConfirmDialog
        open={deleteTarget !== null}
        title="删除技能规范"
        message={
          deleteTarget
            ? `将彻底删除「${deleteTarget.name}」及其镜像文件；如仍想保留台账记录，请改用「停用」。`
            : ''
        }
        tone="danger"
        loading={del.isPending}
        onConfirm={confirmDelete}
        onCancel={() => setDeleteTarget(null)}
      >
        删除不可恢复。若该规范被任何智能体挂载，后端会拒绝删除并返回引用清单。
      </ConfirmDialog>

      <Modal
        open={conflict !== null}
        title={`该技能正被智能体挂载${conflict ? `（${conflict.body.references.length} 个）` : ''}`}
        onClose={() => setConflict(null)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setConflict(null)}>
              返回
            </Button>
            <Button
              variant="danger"
              icon="PowerOff"
              loading={setActive.isPending}
              onClick={conflictDisableOnly}
            >
              只停用不删除
            </Button>
          </>
        }
      >
        {conflict && (
          <div className="col-stack gap-[8px]">
            <div className="text-xs leading-relaxed text-sidebar-foreground">
              平台不允许删除仍被挂载的规范（否则装配关系与审计断链）。停用可即时让新会话不再加载该技能，挂载关系保留、恢复后自动生效。
            </div>
            <div className="col-stack gap-[4px]">
              {conflict.body.references.map((r) => (
                <div key={r.agent_id} className="list-row">
                  <span className="row-main min-w-0">
                    <span className="row-title">{r.agent_name}</span>
                    <span className="row-sub truncate">
                      <span className="mono">{r.agent_slug}</span> · v{r.published_version ?? '—'} · 挂载于{' '}
                      {fmtRelative(r.mounted_at)}
                    </span>
                  </span>
                  <Badge tone={r.agent_status === 'published' ? 'success' : 'neutral'}>{r.agent_status_display}</Badge>
                </div>
              ))}
            </div>
          </div>
        )}
      </Modal>

      <MirrorModal open={mirrorOpen} onClose={() => setMirrorOpen(false)} />
    </PageContainer>
  )
}
