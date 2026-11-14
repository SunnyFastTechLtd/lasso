import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { PageContainer, PageHeader } from '@/components/AppShell'
import { Badge } from '@/components/ui/Badge'
import { Button, IconButton } from '@/components/ui/Button'
import { DataTable } from '@/components/ui/DataTable'
import { EmptyState } from '@/components/ui/EmptyState'
import { Field, Input } from '@/components/ui/Field'
import { Icon } from '@/components/ui/Icon'
import { Pagination } from '@/components/ui/Pagination'
import { Panel, SectionLabel } from '@/components/ui/Panel'
import { ConfirmDialog } from '@/components/ui/Overlay'
import { Seg } from '@/components/ui/Seg'
import {
  useDeleteDocument,
  useDeleteKnowledgeBase,
  useKnowledgeBases,
  useKnowledgeDocuments,
  useReprocessDocument,
  useRunTiangong,
  useUploadDocument,
} from '@/api/hooks/useKnowledge'
import { KB_SCOPE_LABEL, type KnowledgeBase, type KnowledgeDocument, type TiangongStatus } from '@/api/knowledge'
import { useUserStore } from '@/store/user'
import { toast } from '@/store/toast'
import { fmtBytes, fmtNumber, fmtRelative, fmtShortTime } from '@/utils/format'
import { DocStatusBadge, ExtractionSummary, ScopeBadge, extractFieldErrors } from './shared'
import { BaseForm } from './BaseForm'
import { ChunksDrawer } from './ChunksDrawer'

const SCOPE_SEG: { value: string; label: string }[] = [
  { value: '', label: '全部' },
  { value: 'public', label: KB_SCOPE_LABEL.public },
  { value: 'department', label: KB_SCOPE_LABEL.department },
  { value: 'private', label: KB_SCOPE_LABEL.private },
]

const TIANGONG_TEXT: Record<TiangongStatus, string> = {
  idle: '天工待采集',
  queued: '天工排队中',
  running: '天工采集中',
  done: '天工已采集',
  failed: '天工采集失败',
}

/** 后端无 filter_backends：一次取满分页上限（100），搜索与分页在前端做 */
const LOAD_SIZE = 100

export default function KnowledgePage() {
  const isAdmin = useUserStore((s) => s.user?.is_staff ?? false)
  const navigate = useNavigate()

  const [searchInput, setSearchInput] = useState('')
  const [search, setSearch] = useState('')
  const [scope, setScope] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [selectedId, setSelectedId] = useState<number | null>(null)

  const [formOpen, setFormOpen] = useState(false)
  const [formBase, setFormBase] = useState<KnowledgeBase | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<KnowledgeBase | null>(null)

  const [docPage, setDocPage] = useState(1)
  const [docPageSize, setDocPageSize] = useState(10)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [file, setFile] = useState<File | null>(null)
  const [docTitle, setDocTitle] = useState('')
  const [docErrors, setDocErrors] = useState<string[]>([])
  const [docDeleteTarget, setDocDeleteTarget] = useState<KnowledgeDocument | null>(null)
  const [chunksDoc, setChunksDoc] = useState<KnowledgeDocument | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const t = window.setTimeout(() => {
      setSearch(searchInput.trim())
      setPage(1)
    }, 350)
    return () => window.clearTimeout(t)
  }, [searchInput])

  const { data, isFetching, error, refetch } = useKnowledgeBases({ page: 1, page_size: LOAD_SIZE })
  const bases = useMemo(() => data?.results ?? [], [data])

  useEffect(() => {
    if (bases.length === 0) return
    if (selectedId === null || !bases.some((b) => b.id === selectedId)) setSelectedId(bases[0].id)
  }, [bases, selectedId])

  const filtered = useMemo(() => {
    const kw = search.toLowerCase()
    return bases.filter((b) => {
      if (scope && b.scope !== scope) return false
      if (!kw) return true
      return (
        b.name.toLowerCase().includes(kw) ||
        b.description.toLowerCase().includes(kw) ||
        b.department.toLowerCase().includes(kw)
      )
    })
  }, [bases, search, scope])

  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize))
  const safePage = Math.min(page, totalPages)
  const rows = filtered.slice((safePage - 1) * pageSize, safePage * pageSize)
  const selected = bases.find((b) => b.id === selectedId) ?? null
  const runTiangong = useRunTiangong()

  async function triggerTiangong() {
    if (!selected) return
    try {
      await runTiangong.mutateAsync(selected.id)
      toast.success('已提交天工采集，稍后在资料列表查看结果')
      void refetch()
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  const totals = useMemo(
    () =>
      bases.reduce(
        (acc, b) => {
          acc.docs += b.document_count
          acc.chunks += b.chunk_count
          acc.bytes += b.size_bytes
          return acc
        },
        { docs: 0, chunks: 0, bytes: 0 },
      ),
    [bases],
  )

  const {
    data: docData,
    isFetching: docsFetching,
    error: docsError,
    refetch: refetchDocs,
  } = useKnowledgeDocuments(
    { knowledge_base_id: selectedId ?? 0, page: docPage, page_size: docPageSize },
    selectedId !== null,
  )

  // 异步切片全部落定后刷新一次知识库列表，让侧栏的文档数与切片数跟上
  const activeDocCount =
    docData?.results?.filter((d) => d.status === 'pending' || d.status === 'processing').length ?? 0
  const prevActiveDocCount = useRef(0)
  useEffect(() => {
    if (prevActiveDocCount.current > 0 && activeDocCount === 0) void refetch()
    prevActiveDocCount.current = activeDocCount
  }, [activeDocCount, refetch])

  // 天工采集落定后刷新当前库的资料列表，把新采集到的资料带出来
  const activeTiangongCount = bases.filter(
    (b) => b.tiangong_status === 'queued' || b.tiangong_status === 'running',
  ).length
  const prevActiveTiangongCount = useRef(0)
  useEffect(() => {
    if (prevActiveTiangongCount.current > 0 && activeTiangongCount === 0) void refetchDocs()
    prevActiveTiangongCount.current = activeTiangongCount
  }, [activeTiangongCount, refetchDocs])

  const del = useDeleteKnowledgeBase()
  const upload = useUploadDocument()
  const delDoc = useDeleteDocument()
  const reprocess = useReprocessDocument()

  function openCreate() {
    setFormBase(null)
    setFormOpen(true)
  }

  function openEdit(base: KnowledgeBase) {
    setFormBase(base)
    setFormOpen(true)
  }

  async function confirmDeleteBase() {
    if (!deleteTarget) return
    try {
      await del.mutateAsync(deleteTarget.id)
      toast.success(`已删除知识库「${deleteTarget.name}」，资料与切片向量一并清除`)
      if (selectedId === deleteTarget.id) setSelectedId(null)
      setDeleteTarget(null)
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  function closeUpload() {
    setUploadOpen(false)
    setFile(null)
    setDocTitle('')
    setDocErrors([])
    if (fileRef.current) fileRef.current.value = ''
  }

  async function submitUpload() {
    if (!selected) return
    setDocErrors([])
    if (!file) {
      setDocErrors(['请选择要上传的资料文件'])
      return
    }
    try {
      const doc = await upload.mutateAsync({
        knowledge_base_id: selected.id,
        title: docTitle.trim(),
        file,
      })
      toast.success(`已上传「${doc.title}」并排队解析（提取 → 分块 → 向量化）`)
      closeUpload()
    } catch (err) {
      const errs = extractFieldErrors((err as { body?: Record<string, unknown> | null }).body ?? null)
      setDocErrors(errs.length ? errs : [(err as Error).message || '上传失败'])
    }
  }

  async function confirmDeleteDoc() {
    if (!docDeleteTarget) return
    try {
      await delDoc.mutateAsync(docDeleteTarget.id)
      toast.success(`已删除资料「${docDeleteTarget.title}」及其切片向量`)
      setDocDeleteTarget(null)
      if (chunksDoc?.id === docDeleteTarget.id) setChunksDoc(null)
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  async function runReprocess(doc: KnowledgeDocument) {
    try {
      await reprocess.mutateAsync(doc.id)
      toast.success(`已重新排队「${doc.title}」，处理完成后切片数会更新`)
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  return (
    <PageContainer>
      <PageHeader
        title="知识库管理"
        subtitle="沿用现网知识库：企业库 / 部门库 / 个人库、资料上传与切片检索；装配端把「检索哪些库」变成智能体属性（E1）"
        actions={
          isAdmin ? (
            <>
              <Button quiet icon="Globe" onClick={() => navigate('/knowledge/tiangong')}>
                天工来源与搜索
              </Button>
              <Button variant="primary" icon="Plus" onClick={openCreate}>
                新建知识库
              </Button>
            </>
          ) : (
            <Badge tone="neutral">只读视图</Badge>
          )
        }
      />

      <div className="mt-[14px] grid grid-cols-2 gap-[10px] md:grid-cols-4">
        {(
          [
            ['知识库', fmtNumber(bases.length)],
            ['资料', fmtNumber(totals.docs)],
            ['切片', fmtNumber(totals.chunks)],
            ['占用空间', fmtBytes(totals.bytes)],
          ] as const
        ).map(([label, value]) => (
          <div key={label} className="panel px-[12px] py-[10px]">
            <SectionLabel>{label}</SectionLabel>
            <div className="metric-value mt-[4px]">{value}</div>
          </div>
        ))}
      </div>

      <div className="page-toolbar mt-[12px]">
        <div className="relative w-[240px]">
          <Icon name="Search" size={13} className="pointer-events-none absolute left-[9px] top-1/2 -translate-y-1/2 text-muted-foreground" />
          <Input
            className="pl-[28px]"
            value={searchInput}
            placeholder="搜索名称 / 说明 / 部门…"
            onChange={(e) => setSearchInput(e.target.value)}
          />
        </div>
        <Seg
          items={SCOPE_SEG}
          value={scope}
          onChange={(v) => {
            setScope(v)
            setPage(1)
          }}
        />
        <span className="toolbar-note">库的可见范围决定谁能检索到，智能体挂载不扩大可读范围（E2）</span>
        <span className="toolbar-spacer" />
        {(data?.count ?? 0) > LOAD_SIZE && (
          <span className="toolbar-note text-state-warning">
            后端不支持服务端搜索：本页只载入前 {LOAD_SIZE} 个知识库，搜索与筛选在这 {LOAD_SIZE} 个内进行
          </span>
        )}
      </div>

      <div className="page-body mt-[12px]">
        <Panel
          className="min-w-0"
          title="知识库"
          icon="BookOpen"
          meta={`命中 ${filtered.length} / 共 ${data?.count ?? 0}`}
          padded={false}
        >
          {error ? (
            <EmptyState
              icon="TriangleAlert"
              title="知识库读取失败"
              description={error.message}
              action={
                <Button quiet icon="RefreshCw" onClick={() => void refetch()}>
                  重新加载
                </Button>
              }
            />
          ) : (
            <>
              <DataTable
                className="data-table-fixed"
                loading={isFetching && bases.length === 0}
                rows={rows}
                rowKey={(b) => b.id}
                onRowClick={(b) => {
                  setSelectedId(b.id)
                  setDocPage(1)
                  closeUpload()
                }}
                empty={
                  <EmptyState
                    compact
                    icon="BookOpen"
                    title={search || scope ? '没有符合条件的知识库' : '还没有知识库'}
                    description={
                      isAdmin
                        ? '新建知识库并上传资料，处理完成后即可在智能体装配页挂载检索'
                        : '请联系管理员建档；非管理员只看到企业库、本人创建与本部门共享的知识库'
                    }
                    action={
                      isAdmin && !search && !scope ? (
                        <Button quiet icon="Plus" onClick={openCreate}>
                          新建知识库
                        </Button>
                      ) : undefined
                    }
                  />
                }
                columns={[
                  {
                    key: 'name',
                    title: '名称',
                    render: (b) => (
                      <span className="row-main">
                        <span className="row-title flex items-center gap-[6px]">
                          <Icon name={b.icon || 'Database'} size={13} className="flex-none text-muted-foreground" />
                          {b.name}
                          {b.id === selectedId && <Badge tone="primary">查看中</Badge>}
                          {b.tiangong_enabled && <Badge tone="neutral">天工</Badge>}
                        </span>
                        <span className="row-sub truncate">{b.description || '未填写适用说明'}</span>
                      </span>
                    ),
                  },
                  {
                    key: 'scope',
                    title: '可见范围',
                    width: 112,
                    render: (b) => (
                      <span className="flex flex-col items-start gap-[2px] leading-[1.35]">
                        <ScopeBadge kb={b} />
                        {b.scope === 'department' && (
                          <span className="text-xs text-muted-foreground">{b.department}</span>
                        )}
                      </span>
                    ),
                  },
                  {
                    key: 'docs',
                    title: '资料 / 切片',
                    width: 118,
                    tnum: true,
                    align: 'right',
                    secondary: true,
                    render: (b) => `${b.document_count} 份 · ${b.chunk_count} 片`,
                  },
                  { key: 'size', title: '占用', width: 76, tnum: true, align: 'right', secondary: true, render: (b) => fmtBytes(b.size_bytes) },
                  {
                    key: 'updated',
                    title: '更新',
                    width: 84,
                    secondary: true,
                    render: (b) => <span className="text-xs text-muted-foreground">{fmtRelative(b.updated_at)}</span>,
                  },
                  {
                    key: 'ops',
                    title: '操作',
                    width: 96,
                    render: (b) =>
                      isAdmin ? (
                        <span className="flex items-center justify-end gap-[4px]" onClick={(e) => e.stopPropagation()}>
                          <IconButton icon="Pencil" label="编辑知识库" onClick={() => openEdit(b)} />
                          <IconButton
                            icon="Trash2"
                            label="删除知识库（资料与向量索引一并清除）"
                            onClick={() => setDeleteTarget(b)}
                          />
                        </span>
                      ) : (
                        <span className="text-xs text-muted-foreground">—</span>
                      ),
                  },
                ]}
              />
              {rows.length > 0 && (
                <Pagination
                  page={safePage}
                  totalPages={totalPages}
                  count={filtered.length}
                  pageSize={pageSize}
                  onPageChange={setPage}
                  onPageSizeChange={(size) => {
                    setPageSize(size)
                    setPage(1)
                  }}
                />
              )}
            </>
          )}
        </Panel>

        <div className="col-stack">
          <Panel title="当前知识库" icon="Info" padded>
            {selected ? (
              <div className="flex flex-col gap-[6px]">
                <div className="flex flex-wrap items-center gap-[6px]">
                  <span className="text-sm font-semibold">{selected.name}</span>
                  <ScopeBadge kb={selected} />
                </div>
                <ul className="flex flex-col gap-[4px] text-xs text-muted-foreground">
                  {(
                    [
                      ['资料', `${selected.document_count} 份`],
                      ['切片', `${selected.chunk_count} 片`],
                      ['占用', fmtBytes(selected.size_bytes)],
                      ['建档', fmtShortTime(selected.created_at)],
                      ['更新', fmtShortTime(selected.updated_at)],
                    ] as const
                  ).map(([k, v]) => (
                    <li key={k} className="flex items-center justify-between gap-[8px]">
                      <span>{k}</span>
                      <span className="tnum text-sidebar-foreground">{v}</span>
                    </li>
                  ))}
                </ul>
                {selected.tiangong_enabled && (
                  <div className="flex items-center justify-between gap-[8px]">
                    <span className="text-xs text-muted-foreground">
                      {TIANGONG_TEXT[selected.tiangong_status]}
                      {selected.tiangong_last_run_at
                        ? ` · ${fmtRelative(selected.tiangong_last_run_at)}`
                        : ''}
                    </span>
                    <Button quiet icon="RefreshCw" loading={runTiangong.isPending} onClick={triggerTiangong}>
                      补充采集
                    </Button>
                  </div>
                )}
                {selected.tiangong_enabled && selected.tiangong_last_error && (
                  <div className="text-xs leading-[1.5] text-state-warning">
                    {selected.tiangong_last_error}
                  </div>
                )}
              </div>
            ) : (
              <span className="text-xs text-muted-foreground">未选择知识库</span>
            )}
          </Panel>

          <Panel title="检索与越权边界" icon="ShieldCheck" padded>
            <div className="flex flex-col gap-[6px] text-xs leading-[1.6] text-muted-foreground">
              <p>
                <span className="text-sidebar-foreground">企业库</span>全企业可检索；
                <span className="text-sidebar-foreground">部门库</span>仅同名部门可检索；
                <span className="text-sidebar-foreground">个人库</span>仅创建者本人可检索。
              </p>
              <p>
                智能体挂载知识库只声明「允许检索哪些库」，实际命中仍按使用者自身可见范围过滤，不会因此读到他人或他部门资料。
              </p>
              <p className="text-state-warning">
                删除为硬删除：资料与向量索引一并清除，挂载它的智能体需重新选择知识库，否则发布检查按「挂载的知识库已不存在」阻断。
              </p>
            </div>
          </Panel>
        </div>
      </div>

      <Panel
        className="mt-[12px]"
        title={selected ? `资料 · ${selected.name}` : '资料'}
        icon="Files"
        meta={selected ? `共 ${docData?.count ?? 0} 份` : undefined}
        padded={false}
        actions={
          selected && isAdmin ? (
            <span className="flex items-center gap-[6px]">
              <IconButton icon="RefreshCw" label="重新读取资料列表" onClick={() => void refetchDocs()} />
              <Button
                quiet
                variant={uploadOpen ? 'default' : 'primary'}
                icon={uploadOpen ? 'X' : 'Upload'}
                onClick={() => (uploadOpen ? closeUpload() : setUploadOpen(true))}
              >
                {uploadOpen ? '取消上传' : '上传资料'}
              </Button>
            </span>
          ) : undefined
        }
      >
        {selected && uploadOpen && (
          <div className="panel-body-padded border-b border-border">
            <div className="form-grid">
              <Field
                label="资料文件"
                required
                error={docErrors.length ? docErrors.join('；') : undefined}
                hint="上传后进入解析：提取文本 → 分块 → 向量化"
              >
                <input
                  ref={fileRef}
                  type="file"
                  className="field text-xs"
                  disabled={upload.isPending}
                  onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                />
              </Field>
              <Field label="资料标题" hint="留空＝用文件名">
                <Input
                  value={docTitle}
                  placeholder="如：2026 年工伤保险待遇标准"
                  maxLength={500}
                  disabled={upload.isPending}
                  onChange={(e) => setDocTitle(e.target.value)}
                />
              </Field>
              <div className="span-full flex flex-wrap items-center gap-[8px]">
                <Button variant="primary" icon="Upload" loading={upload.isPending} onClick={submitUpload}>
                  开始上传
                </Button>
                <span className="text-xs text-muted-foreground">
                  上传人记为当前账号；解析中的资料显示「处理中」，稍后点右上角刷新查看结果
                </span>
              </div>
            </div>
          </div>
        )}

        {!selected ? (
          <EmptyState
            compact
            icon="BookOpen"
            title="先在上方选择一个知识库"
            description="选中后这里展示该库的资料清单、处理状态与切片"
          />
        ) : docsError ? (
          <EmptyState compact icon="TriangleAlert" title="资料读取失败" description={docsError.message} />
        ) : (
          <>
            <DataTable
              loading={docsFetching && (docData?.results.length ?? 0) === 0}
              rows={docData?.results ?? []}
              rowKey={(d) => d.id}
              onRowClick={(d) => setChunksDoc(d)}
              empty={
                <EmptyState
                  compact
                  icon="Files"
                  title="该知识库还没有资料"
                  description={
                    isAdmin
                      ? '上传制度、流程、政策原文等资料，处理完成后即可被智能体检索'
                      : '请联系管理员上传资料'
                  }
                  action={
                    isAdmin && !uploadOpen ? (
                      <Button quiet icon="Upload" onClick={() => setUploadOpen(true)}>
                        上传资料
                      </Button>
                    ) : undefined
                  }
                />
              }
              columns={[
                {
                  key: 'title',
                  title: '资料',
                  render: (d) => (
                    <span className="row-main">
                      <span className="row-title flex items-center gap-[6px]">
                        {d.title}
                        {d.source_type === 'web' && <Badge tone="neutral">外部网页</Badge>}
                        {d.source_type === 'model' && (
                          <Badge tone="warning">
                            模型生成{d.source_model ? ` · ${d.source_model}` : ''}
                          </Badge>
                        )}
                        <ExtractionSummary report={d.extraction_report} />
                        {d.error_msg && (
                          <span className="truncate text-xs text-state-error" title={d.error_msg}>
                            {d.error_msg}
                          </span>
                        )}
                      </span>
                      <span className="row-sub mono truncate" title={d.source_url || d.file_name}>
                        {d.source_url || d.file_name}
                      </span>
                    </span>
                  ),
                },
                { key: 'type', title: '类型', width: 70, secondary: true, render: (d) => d.file_type || '—' },
                { key: 'size', title: '大小', width: 88, tnum: true, align: 'right', render: (d) => d.size_human },
                {
                  key: 'status',
                  title: '处理状态',
                  width: 106,
                  render: (d) => <DocStatusBadge status={d.status} error={d.error_msg} />,
                },
                { key: 'chunks', title: '切片', width: 70, tnum: true, align: 'right', render: (d) => d.chunk_count },
                { key: 'uploader', title: '上传人', width: 96, secondary: true, render: (d) => d.uploaded_by_name || '—' },
                {
                  key: 'created',
                  title: '上传时间',
                  width: 108,
                  tnum: true,
                  secondary: true,
                  render: (d) => <span className="text-xs text-muted-foreground">{fmtShortTime(d.created_at)}</span>,
                },
                {
                  key: 'ops',
                  title: '操作',
                  width: 100,
                  render: (d) => (
                    <span className="flex items-center justify-end gap-[4px]" onClick={(e) => e.stopPropagation()}>
                      <IconButton icon="Layers" label="切片预览（只读）" onClick={() => setChunksDoc(d)} />
                      {isAdmin && (
                        <IconButton
                          icon="RotateCw"
                          label={d.status === 'processing' ? '处理中，暂不能重新处理' : '重新处理（提取/分块/向量化）'}
                          disabled={d.status === 'processing' || reprocess.isPending}
                          onClick={() => void runReprocess(d)}
                        />
                      )}
                      {isAdmin && (
                        <IconButton icon="Trash2" label="删除资料及其切片向量" onClick={() => setDocDeleteTarget(d)} />
                      )}
                    </span>
                  ),
                },
              ]}
            />
            {(docData?.results.length ?? 0) > 0 && (
              <Pagination
                page={docData?.page ?? 1}
                totalPages={docData?.total_pages ?? 1}
                count={docData?.count ?? 0}
                pageSize={docPageSize}
                onPageChange={setDocPage}
                onPageSizeChange={(size) => {
                  setDocPageSize(size)
                  setDocPage(1)
                }}
              />
            )}
          </>
        )}
      </Panel>

      <BaseForm open={formOpen} base={formBase} onClose={() => setFormOpen(false)} />

      <ChunksDrawer doc={chunksDoc} onClose={() => setChunksDoc(null)} />

      <ConfirmDialog
        open={deleteTarget !== null}
        tone="danger"
        title={deleteTarget ? `删除知识库 · ${deleteTarget.name}` : '删除知识库'}
        confirmText="硬删除"
        cancelText="保留"
        loading={del.isPending}
        onConfirm={confirmDeleteBase}
        onCancel={() => setDeleteTarget(null)}
      >
        {deleteTarget && (
          <div className="col-stack gap-[6px]">
            <p>
              该库的 <span className="tnum text-sidebar-foreground">{deleteTarget.document_count}</span> 份资料与{' '}
              <span className="tnum text-sidebar-foreground">{deleteTarget.chunk_count}</span> 个切片向量将一并清除，
              <span className="text-state-error">不可恢复</span>。
            </p>
            <p>挂载该库的智能体不会自动摘除引用，发布检查会按「挂载的知识库已不存在」阻断，需要重新选择知识库。</p>
          </div>
        )}
      </ConfirmDialog>

      <ConfirmDialog
        open={docDeleteTarget !== null}
        tone="danger"
        title={docDeleteTarget ? `删除资料 · ${docDeleteTarget.title}` : '删除资料'}
        confirmText="删除"
        loading={delDoc.isPending}
        onConfirm={confirmDeleteDoc}
        onCancel={() => setDocDeleteTarget(null)}
      >
        {docDeleteTarget && (
          <p>
            删除后该资料的{' '}
            <span className="tnum text-sidebar-foreground">{docDeleteTarget.chunk_count}</span> 个切片与向量索引一并清除，
            检索不再命中，<span className="text-state-error">不可恢复</span>。
          </p>
        )}
      </ConfirmDialog>
    </PageContainer>
  )
}
