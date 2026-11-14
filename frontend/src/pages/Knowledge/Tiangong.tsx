import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { PageContainer, PageHeader } from '@/components/AppShell'
import { Badge, type BadgeTone } from '@/components/ui/Badge'
import { Button, IconButton } from '@/components/ui/Button'
import { DataTable } from '@/components/ui/DataTable'
import { EmptyState } from '@/components/ui/EmptyState'
import { Field, Input, Textarea } from '@/components/ui/Field'
import { Modal, ConfirmDialog } from '@/components/ui/Overlay'
import { Panel } from '@/components/ui/Panel'
import { Seg } from '@/components/ui/Seg'
import { Switch } from '@/components/ui/Switch'
import {
  useApproveTiangongProposal,
  useCheckBing,
  useCheckSearxng,
  useCheckTiangongSource,
  useCreateTiangongBinding,
  useCreateTiangongSource,
  useDeleteTiangongBinding,
  useDeleteTiangongSource,
  useRejectTiangongProposal,
  useScanTiangongCandidates,
  useTiangongBindings,
  useTiangongProposals,
  useTiangongSearchSettings,
  useTiangongSources,
  useTestTiangongBinding,
  useUpdateTiangongBinding,
  useUpdateTiangongSearchSettings,
  useUpdateTiangongSource,
} from '@/api/hooks/useKnowledge'
import {
  type TiangongProposal,
  type TiangongSearchBinding,
  type TiangongSearchBindingPayload,
  type TiangongSearchCandidate,
  type TiangongSearchSettings,
  type TiangongSource,
  type TiangongSourceStatus,
} from '@/api/knowledge'
import { toast } from '@/store/toast'
import { useUserStore } from '@/store/user'
import { fmtRelative } from '@/utils/format'
import { extractFieldErrors } from './shared'

type SectionKey = 'sources' | 'proposals' | 'settings'

const SECTIONS: { value: SectionKey; label: string }[] = [
  { value: 'sources', label: '来源库' },
  { value: 'proposals', label: '候选来源' },
  { value: 'settings', label: '搜索与抓取' },
]

const SOURCE_TONE: Record<TiangongSourceStatus, BadgeTone> = {
  ok: 'success',
  failed: 'error',
  untested: 'neutral',
}

const PROPOSAL_SEG: { value: string; label: string }[] = [
  { value: 'pending', label: '待审批' },
  { value: 'approved', label: '已批准' },
  { value: 'rejected', label: '已拒绝' },
  { value: '', label: '全部' },
]

const PROPOSAL_TONE: Record<string, BadgeTone> = {
  pending: 'warning',
  approved: 'success',
  rejected: 'neutral',
}

interface SourceForm {
  name: string
  domain: string
  entry_urls: string
  topic_tags: string
  description: string
  priority: string
  enabled: boolean
  note: string
}

const EMPTY_FORM: SourceForm = {
  name: '',
  domain: '',
  entry_urls: '',
  topic_tags: '',
  description: '',
  priority: '100',
  enabled: true,
  note: '',
}

function formOf(source: TiangongSource): SourceForm {
  return {
    name: source.name,
    domain: source.domain,
    entry_urls: source.entry_urls,
    topic_tags: source.topic_tags,
    description: source.description,
    priority: String(source.priority),
    enabled: source.enabled,
    note: source.note,
  }
}

/** 天工来源治理：来源库、候选审批、搜索层与抓取参数（仅管理员可写） */
export default function TiangongPage() {
  const navigate = useNavigate()
  const isAdmin = useUserStore((s) => s.user?.is_staff ?? false)
  const [section, setSection] = useState<SectionKey>('sources')

  return (
    <PageContainer>
      <PageHeader
        title="天工来源与搜索"
        subtitle="搜索层只发现候选，管理员批准后进入来源库，再由 L4 抓正文入库；未审批候选一律不抓"
        actions={
          <>
            <Button quiet icon="ArrowLeft" onClick={() => navigate('/knowledge')}>
              返回知识库
            </Button>
            <Badge tone={isAdmin ? 'primary' : 'neutral'}>{isAdmin ? '管理员可配置' : '只读视图'}</Badge>
          </>
        }
      />

      <div className="page-toolbar mt-[12px]">
        <Seg items={SECTIONS} value={section} onChange={setSection} />
        <span className="toolbar-spacer" />
        <span className="toolbar-note">
          {section === 'sources' && '来源库即信息源白名单；知识库原有「采集入口」仍是本轮临时补充入口'}
          {section === 'proposals' && '候选来自 L1/L2/L3 搜索层；批准时创建或合并来源站，拒绝不抓取'}
          {section === 'settings' && '搜索层开关与参数：L1 MCP、L2 SearXNG、L3 Bing 保底（默认关闭）与抓取上限'}
        </span>
      </div>

      <div className="mt-[12px]">
        {section === 'sources' && <SourcesSection isAdmin={isAdmin} />}
        {section === 'proposals' && <ProposalsSection isAdmin={isAdmin} />}
        {section === 'settings' && <SettingsSection isAdmin={isAdmin} />}
      </div>
    </PageContainer>
  )
}

function SourcesSection({ isAdmin }: { isAdmin: boolean }) {
  const { data, isLoading, error, refetch } = useTiangongSources({ page: 1, page_size: 100 })
  const sources = useMemo(() => data?.results ?? [], [data])
  const create = useCreateTiangongSource()
  const update = useUpdateTiangongSource()
  const remove = useDeleteTiangongSource()
  const check = useCheckTiangongSource()
  const busy = create.isPending || update.isPending

  const [keyword, setKeyword] = useState('')
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<TiangongSource | null>(null)
  const [form, setForm] = useState<SourceForm>(EMPTY_FORM)
  const [formErrors, setFormErrors] = useState<string[]>([])
  const [deleteTarget, setDeleteTarget] = useState<TiangongSource | null>(null)

  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase()
    if (!kw) return sources
    return sources.filter(
      (s) =>
        s.name.toLowerCase().includes(kw) ||
        s.domain.toLowerCase().includes(kw) ||
        s.topic_tags.toLowerCase().includes(kw),
    )
  }, [sources, keyword])

  function openCreate() {
    setEditing(null)
    setForm(EMPTY_FORM)
    setFormErrors([])
    setFormOpen(true)
  }

  function openEdit(source: TiangongSource) {
    setEditing(source)
    setForm(formOf(source))
    setFormErrors([])
    setFormOpen(true)
  }

  async function submit() {
    setFormErrors([])
    if (!form.name.trim() || !form.domain.trim()) {
      setFormErrors(['请填写名称与域名'])
      return
    }
    const payload = {
      name: form.name.trim(),
      domain: form.domain.trim(),
      entry_urls: form.entry_urls.trim(),
      topic_tags: form.topic_tags.trim(),
      description: form.description.trim(),
      priority: Number(form.priority) || 100,
      enabled: form.enabled,
      note: form.note.trim(),
    }
    try {
      if (editing) {
        await update.mutateAsync({ id: editing.id, payload })
        toast.success(`已保存来源「${payload.name}」`)
      } else {
        await create.mutateAsync(payload)
        toast.success(`已新增来源「${payload.name}」`)
      }
      setFormOpen(false)
    } catch (err) {
      const errs = extractFieldErrors((err as { body?: Record<string, unknown> | null }).body ?? null)
      setFormErrors(errs.length ? errs : [(err as Error).message || '保存失败'])
    }
  }

  async function runCheck(source: TiangongSource) {
    try {
      const result = await check.mutateAsync(source.id)
      if (result.ok) {
        toast.success(`${source.name} 可达，robots 允许，sitemap ${result.sitemap_count} 条`)
      } else {
        toast.error(`${source.name} 检查未通过：${result.error || 'robots 不允许'}`)
      }
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return
    try {
      await remove.mutateAsync(deleteTarget.id)
      toast.success(`已删除来源「${deleteTarget.name}」`)
      setDeleteTarget(null)
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  return (
    <>
      <div className="page-toolbar">
        <div className="relative w-[240px]">
          <Input value={keyword} placeholder="搜索名称 / 域名 / 标签…" onChange={(e) => setKeyword(e.target.value)} />
        </div>
        <span className="toolbar-spacer" />
        {isAdmin && (
          <Button variant="primary" icon="Plus" onClick={openCreate}>
            新增来源
          </Button>
        )}
      </div>

      <Panel className="mt-[10px] min-w-0" title="来源库" icon="Globe" meta={`共 ${sources.length} 个`} padded={false}>
        {error ? (
          <EmptyState
            icon="TriangleAlert"
            title="来源库读取失败"
            description={error.message}
            action={<Button quiet icon="RefreshCw" onClick={() => void refetch()}>重新加载</Button>}
          />
        ) : (
          <DataTable
            loading={isLoading && sources.length === 0}
            rows={filtered}
            rowKey={(s) => s.id}
            empty={
              <EmptyState
                compact
                icon="Globe"
                title={keyword ? '没有匹配的来源' : '还没有来源'}
                description="批准候选来源或在管理员处手工录入；来源库为空时只跑知识库的临时采集入口"
              />
            }
            columns={[
              {
                key: 'name',
                title: '来源',
                render: (s) => (
                  <span className="row-main">
                    <span className="row-title flex items-center gap-[6px]">
                      {s.name}
                      <Badge tone={s.enabled ? 'primary' : 'neutral'}>{s.enabled ? '启用' : '停用'}</Badge>
                    </span>
                    <span className="row-sub mono truncate">{s.domain}</span>
                  </span>
                ),
              },
              {
                key: 'tags',
                title: '主题标签',
                secondary: true,
                render: (s) => <span className="text-xs text-muted-foreground">{s.topic_tags || '—'}</span>,
              },
              {
                key: 'priority',
                title: '优先级',
                width: 80,
                align: 'right',
                tnum: true,
                render: (s) => s.priority,
              },
              {
                key: 'checked',
                title: '最近检查',
                width: 150,
                render: (s) => (
                  <span className="flex items-center gap-[6px] text-xs">
                    <Badge tone={SOURCE_TONE[s.last_status]}>{s.last_status_display}</Badge>
                    <span className="text-muted-foreground">
                      {s.last_checked_at ? fmtRelative(s.last_checked_at) : '未检查'}
                    </span>
                  </span>
                ),
              },
              {
                key: 'actions',
                title: '操作',
                width: 150,
                align: 'right',
                render: (s) =>
                  isAdmin ? (
                    <span className="flex items-center justify-end gap-[2px]" onClick={(e) => e.stopPropagation()}>
                      <IconButton icon="Activity" label="检查可达性" onClick={() => void runCheck(s)} />
                      <IconButton icon="Pencil" label="编辑" onClick={() => openEdit(s)} />
                      <IconButton icon="Trash2" label="删除" onClick={() => setDeleteTarget(s)} />
                    </span>
                  ) : (
                    <span className="text-xs text-muted-foreground">只读</span>
                  ),
              },
            ]}
          />
        )}
      </Panel>

      <Modal
        open={formOpen}
        wide
        icon="Globe"
        title={editing ? `编辑来源 · ${editing.name}` : '新增来源'}
        onClose={busy ? () => undefined : () => setFormOpen(false)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setFormOpen(false)} disabled={busy}>
              取消
            </Button>
            <Button variant="primary" icon="Check" loading={busy} onClick={submit}>
              保存
            </Button>
          </>
        }
      >
        {formErrors.length > 0 && (
          <div className="mb-[10px] rounded-md border border-state-error-border bg-state-error-soft px-[10px] py-[8px] text-xs leading-relaxed text-state-error">
            {formErrors.map((e, i) => (
              <div key={i}>{e}</div>
            ))}
          </div>
        )}
        <div className="form-grid">
          <Field label="名称" required>
            <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} maxLength={200} />
          </Field>
          <Field label="域名" required hint="不含协议与路径，如 example.gov.cn">
            <Input
              value={form.domain}
              onChange={(e) => setForm({ ...form, domain: e.target.value })}
              placeholder="example.gov.cn"
              maxLength={255}
            />
          </Field>
          <Field label="入口地址" className="span-full" hint="一行一个 URL，最多 10 个；留空则从 https://域名/ 开始">
            <Textarea
              rows={3}
              value={form.entry_urls}
              onChange={(e) => setForm({ ...form, entry_urls: e.target.value })}
              placeholder="https://example.gov.cn/policy"
            />
          </Field>
          <Field label="主题标签" className="span-full" hint="逗号/顿号分隔；与知识库主题无交集时整站跳过">
            <Input
              value={form.topic_tags}
              onChange={(e) => setForm({ ...form, topic_tags: e.target.value })}
              placeholder="工伤认定、停工留薪期"
            />
          </Field>
          <Field label="说明" className="span-full">
            <Input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} maxLength={300} />
          </Field>
          <Field label="优先级" hint="数字越小越先抓">
            <Input
              type="number"
              min={0}
              value={form.priority}
              onChange={(e) => setForm({ ...form, priority: e.target.value })}
            />
          </Field>
          <Field label="备注">
            <Input value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} maxLength={255} />
          </Field>
          <div className="span-full">
            <Switch
              checked={form.enabled}
              onChange={(v) => setForm({ ...form, enabled: v })}
              label="启用该来源"
              hint="停用后不参与任何知识库的天工抓取"
            />
          </div>
        </div>
      </Modal>

      <ConfirmDialog
        open={deleteTarget !== null}
        tone="danger"
        title="删除来源"
        message={deleteTarget ? `将删除「${deleteTarget.name}」（${deleteTarget.domain}）。已入库的资料不受影响。` : ''}
        confirmText="删除"
        loading={remove.isPending}
        onConfirm={() => void confirmDelete()}
        onCancel={() => setDeleteTarget(null)}
      />
    </>
  )
}

function ProposalsSection({ isAdmin }: { isAdmin: boolean }) {
  const [status, setStatus] = useState('pending')
  const { data, isLoading, error, refetch } = useTiangongProposals(
    { page: 1, page_size: 100, status: status || undefined },
    true,
  )
  const proposals = useMemo(() => data?.results ?? [], [data])
  const approve = useApproveTiangongProposal()
  const reject = useRejectTiangongProposal()
  const [rejectTarget, setRejectTarget] = useState<TiangongProposal | null>(null)
  const [reason, setReason] = useState('')

  async function runApprove(item: TiangongProposal) {
    try {
      const result = await approve.mutateAsync(item.id)
      toast.success(result.created ? `已批准并新增来源 ${item.domain}` : `已批准并合并到来源 ${item.domain}`)
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  async function confirmReject() {
    if (!rejectTarget) return
    try {
      await reject.mutateAsync({ id: rejectTarget.id, reason: reason.trim() })
      toast.success('已拒绝，该候选不会被抓取')
      setRejectTarget(null)
      setReason('')
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  return (
    <>
      <div className="page-toolbar">
        <Seg items={PROPOSAL_SEG} value={status} onChange={setStatus} />
        <span className="toolbar-spacer" />
        <span className="toolbar-note">候选只在审批通过后进入来源库；30 天未处理自动清理</span>
      </div>

      <Panel className="mt-[10px] min-w-0" title="候选来源" icon="ListChecks" meta={`共 ${proposals.length} 条`} padded={false}>
        {error ? (
          <EmptyState
            icon="TriangleAlert"
            title="候选读取失败"
            description={error.message}
            action={<Button quiet icon="RefreshCw" onClick={() => void refetch()}>重新加载</Button>}
          />
        ) : (
          <DataTable
            loading={isLoading && proposals.length === 0}
            rows={proposals}
            rowKey={(p) => p.id}
            empty={
              <EmptyState
                compact
                icon="ListChecks"
                title="没有候选来源"
                description="开启 SearXNG 并让知识库跑一轮天工采集后，检索结果会出现在这里等待审批"
              />
            }
            columns={[
              {
                key: 'title',
                title: '候选',
                render: (p) => (
                  <span className="row-main">
                    <span className="row-title flex items-center gap-[6px]">
                      {p.title || p.url}
                      <Badge tone={PROPOSAL_TONE[p.status] ?? 'neutral'}>{p.status_display}</Badge>
                    </span>
                    <a
                      className="row-sub mono truncate text-primary hover:underline"
                      href={p.url}
                      target="_blank"
                      rel="noreferrer"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {p.url}
                    </a>
                    {p.snippet && <span className="row-sub truncate">{p.snippet}</span>}
                  </span>
                ),
              },
              {
                key: 'type',
                title: '来源类型',
                width: 110,
                render: (p) => <Badge tone="neutral">{p.source_type_display}</Badge>,
              },
              {
                key: 'discovered',
                title: '发现方式',
                width: 120,
                render: (p) => <span className="text-xs text-muted-foreground">{p.discovered_by_display}</span>,
              },
              {
                key: 'topics',
                title: '主题词',
                secondary: true,
                render: (p) => (
                  <span className="text-xs text-muted-foreground">
                    {p.topics || '—'}
                    {p.knowledge_base_name ? ` · ${p.knowledge_base_name}` : ''}
                  </span>
                ),
              },
              {
                key: 'actions',
                title: '操作',
                width: 130,
                align: 'right',
                render: (p) =>
                  p.status === 'pending' && isAdmin ? (
                    <span className="flex items-center justify-end gap-[4px]" onClick={(e) => e.stopPropagation()}>
                      <Button quiet icon="Check" loading={approve.isPending} onClick={() => void runApprove(p)}>
                        批准
                      </Button>
                      <Button
                        quiet
                        icon="X"
                        onClick={() => {
                          setRejectTarget(p)
                          setReason('')
                        }}
                      >
                        拒绝
                      </Button>
                    </span>
                  ) : (
                    <span className="text-xs text-muted-foreground">
                      {p.reviewed_by_name ? `${p.reviewed_by_name} 已处理` : '—'}
                    </span>
                  ),
              },
            ]}
          />
        )}
      </Panel>

      <ConfirmDialog
        open={rejectTarget !== null}
        title="拒绝候选来源"
        message={rejectTarget ? `拒绝后不会抓取「${rejectTarget.url}」。` : ''}
        confirmText="拒绝"
        loading={reject.isPending}
        onConfirm={() => void confirmReject()}
        onCancel={() => setRejectTarget(null)}
      >
        <Field label="拒绝原因" hint="可选，便于后续复盘">
          <Input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={300} />
        </Field>
      </ConfirmDialog>
    </>
  )
}

function SettingsSection({ isAdmin }: { isAdmin: boolean }) {
  const { data, isLoading, error, refetch } = useTiangongSearchSettings()
  const update = useUpdateTiangongSearchSettings()
  const checkSearxng = useCheckSearxng()
  const checkBing = useCheckBing()
  const [form, setForm] = useState<TiangongSearchSettings | null>(null)

  useEffect(() => {
    if (data) setForm(data)
  }, [data])

  const busy = update.isPending

  function patch<K extends keyof TiangongSearchSettings>(key: K, value: TiangongSearchSettings[K]) {
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev))
  }

  async function save(extra?: { reset_circuit?: boolean; reset_bing_circuit?: boolean }) {
    if (!form) return
    try {
      const payload = {
        discovery_enabled: form.discovery_enabled,
        mcp_enabled: form.mcp_enabled,
        searxng_enabled: form.searxng_enabled,
        bing_enabled: form.bing_enabled,
        searxng_url: form.searxng_url,
        searxng_timeout: form.searxng_timeout,
        max_queries: form.max_queries,
        max_results: form.max_results,
        min_results: form.min_results,
        crawl_max_pages: form.crawl_max_pages,
        crawl_pages_per_source: form.crawl_pages_per_source,
        bing_daily_cap: form.bing_daily_cap,
        bing_max_queries: form.bing_max_queries,
        bing_max_results: form.bing_max_results,
        bing_timeout: form.bing_timeout,
        bing_interval: form.bing_interval,
        ...extra,
      }
      await update.mutateAsync(payload)
      toast.success('已保存搜索与抓取设置')
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  async function runCheck() {
    try {
      const result = await checkSearxng.mutateAsync()
      if (result.ok) {
        toast.success(`SearXNG 可用（${result.elapsed_ms} ms）${result.detail ? `：${result.detail}` : ''}`)
      } else {
        toast.error(`SearXNG 不可用：${result.reason || '连接失败'}`)
      }
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  async function runBingCheck() {
    try {
      const result = await checkBing.mutateAsync()
      if (result.ok) {
        toast.success(`Bing 浏览器可用（${result.elapsed_ms} ms）`)
      } else {
        toast.error(`Bing 浏览器不可用：${reasonText(result.reason)}${result.detail ? `（${result.detail}）` : ''}`)
      }
      void refetch()
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  if (error) {
    return (
      <Panel title="搜索与抓取设置" icon="SlidersHorizontal" padded>
        <EmptyState
          icon="TriangleAlert"
          title="设置读取失败"
          description={error.message}
          action={<Button quiet icon="RefreshCw" onClick={() => void refetch()}>重新加载</Button>}
        />
      </Panel>
    )
  }

  if (!form) {
    return (
      <Panel title="搜索与抓取设置" icon="SlidersHorizontal" padded>
        <div className="text-sm text-muted-foreground">{isLoading ? '读取中…' : '暂无数据'}</div>
      </Panel>
    )
  }

  return (
    <div className="col-stack">
      <Panel title="搜索层" icon="Search" padded>
        <div className="col-stack gap-[10px]">
          <Switch
            checked={form.discovery_enabled}
            onChange={(v) => patch('discovery_enabled', v)}
            disabled={!isAdmin || busy}
            label="启用来源发现"
            hint="关闭后不调用任何搜索层，只抓来源库与知识库临时采集入口"
          />
          <Switch
            checked={form.mcp_enabled}
            onChange={(v) => patch('mcp_enabled', v)}
            disabled={!isAdmin || busy}
            label="MCP 搜索（L1）"
            hint="本轮仅预留开关与数据位，接入搜索绑定后生效"
          />
          <Switch
            checked={form.searxng_enabled}
            onChange={(v) => patch('searxng_enabled', v)}
            disabled={!isAdmin || busy}
            label="SearXNG 检索（L2）"
            hint="默认开启；实例不可达或返回值不足时会记录原因并继续走来源库"
          />
          <div className="field-hint">
            L2 不达标时按 L1 → L2 → L3 顺序降级；L3 Bing 保底默认关闭，只在 L1/L2 都无可用结果且浏览器可用时触发。
          </div>
          <div className="form-grid">
            <Field label="SearXNG 地址" hint="只允许本机或内网回环地址">
              <Input
                value={form.searxng_url}
                onChange={(e) => patch('searxng_url', e.target.value)}
                disabled={!isAdmin || busy}
                placeholder="http://127.0.0.1:8888"
              />
            </Field>
            <Field label="单轮超时（秒）">
              <Input
                type="number"
                min={3}
                max={60}
                value={form.searxng_timeout}
                onChange={(e) => patch('searxng_timeout', Number(e.target.value))}
                disabled={!isAdmin || busy}
              />
            </Field>
            <Field label="单轮最多查询数">
              <Input
                type="number"
                min={1}
                max={10}
                value={form.max_queries}
                onChange={(e) => patch('max_queries', Number(e.target.value))}
                disabled={!isAdmin || busy}
              />
            </Field>
            <Field label="单层最多候选数">
              <Input
                type="number"
                min={1}
                max={10}
                value={form.max_results}
                onChange={(e) => patch('max_results', Number(e.target.value))}
                disabled={!isAdmin || busy}
              />
            </Field>
            <Field label="命中即停阈值" hint="达到该候选数即停止后续搜索层">
              <Input
                type="number"
                min={1}
                max={10}
                value={form.min_results}
                onChange={(e) => patch('min_results', Number(e.target.value))}
                disabled={!isAdmin || busy}
              />
            </Field>
            <div className="flex items-end gap-[6px]">
              <Button quiet icon="Stethoscope" loading={checkSearxng.isPending} onClick={() => void runCheck()}>
                检查 SearXNG
              </Button>
            </div>
          </div>
          <div className="flex items-center justify-between rounded-md border border-border bg-card px-[10px] py-[8px]">
            <span className="text-xs text-muted-foreground">
              熔断状态：
              {form.searxng_circuit.open
                ? `已打开至 ${form.searxng_circuit.open_until}`
                : `未熔断（连续失败 ${form.searxng_circuit.fail_count}/${form.searxng_circuit.threshold}）`}
            </span>
            {isAdmin && (
              <Button quiet icon="RotateCcw" loading={busy} onClick={() => void save({ reset_circuit: true })}>
                重置熔断
              </Button>
            )}
          </div>
        </div>
      </Panel>

      <Panel title="Bing 保底（L3）" icon="Globe" padded>
        <div className="col-stack gap-[10px]">
          <Switch
            checked={form.bing_enabled}
            onChange={(v) => patch('bing_enabled', v)}
            disabled={!isAdmin || busy}
            label="启用 Bing 无头浏览器保底"
            hint="默认关闭；仅当总开关与 L3 开关都打开、L1/L2 均无可用结果（未配置/失败/空结果/额度用尽/熔断中）且浏览器健康检查通过时触发"
          />
          <div className="field-hint">
            只访问 Bing，统一 UA、低频限速；不解析其他引擎、不绕过验证码，遇到验证码即记原因并当日熔断。
            结果只生成候选提案（discovered_by=bing），正文仍需审批后由来源库抓取。
          </div>
          <div className="form-grid">
            <Field label="当日调用上限" hint="达到后本层当天不再触发">
              <Input
                type="number"
                min={1}
                max={200}
                value={form.bing_daily_cap}
                onChange={(e) => patch('bing_daily_cap', Number(e.target.value))}
                disabled={!isAdmin || busy}
              />
            </Field>
            <Field label="单轮查询数" hint="硬上限 3 个">
              <Input
                type="number"
                min={1}
                max={3}
                value={form.bing_max_queries}
                onChange={(e) => patch('bing_max_queries', Number(e.target.value))}
                disabled={!isAdmin || busy}
              />
            </Field>
            <Field label="单查询结果数" hint="硬上限 10 条">
              <Input
                type="number"
                min={1}
                max={10}
                value={form.bing_max_results}
                onChange={(e) => patch('bing_max_results', Number(e.target.value))}
                disabled={!isAdmin || busy}
              />
            </Field>
            <Field label="单页超时（秒）">
              <Input
                type="number"
                min={10}
                max={60}
                value={form.bing_timeout}
                onChange={(e) => patch('bing_timeout', Number(e.target.value))}
                disabled={!isAdmin || busy}
              />
            </Field>
            <Field label="查询间隔（秒）" hint="至少 1 秒">
              <Input
                type="number"
                min={1}
                max={5}
                value={form.bing_interval}
                onChange={(e) => patch('bing_interval', Number(e.target.value))}
                disabled={!isAdmin || busy}
              />
            </Field>
            <div className="flex items-end gap-[6px]">
              <Button quiet icon="Stethoscope" loading={checkBing.isPending} onClick={() => void runBingCheck()}>
                检查浏览器
              </Button>
            </div>
          </div>
          <div className="col-stack gap-[6px] rounded-md border border-border bg-card px-[10px] py-[8px]">
            <div className="flex flex-wrap items-center gap-[8px] text-xs">
              <Badge tone={form.bing_state.available ? 'success' : 'neutral'}>
                {form.bing_state.available ? '浏览器可用' : `浏览器不可用：${reasonText(form.bing_state.unavailable_reason)}`}
              </Badge>
              <span className="text-muted-foreground">
                今日已用 {form.bing_state.daily_used}/{form.bing_state.daily_cap}
              </span>
              <span className="text-muted-foreground">
                熔断：
                {form.bing_state.circuit.open
                  ? `已打开至 ${form.bing_state.circuit.open_until}`
                  : `未熔断（连续失败 ${form.bing_state.circuit.fail_count}/${form.bing_state.circuit.threshold}）`}
              </span>
            </div>
            {form.bing_state.last_status && (
              <span className="text-xs text-muted-foreground">
                最近一次：{form.bing_state.last_status === 'ok' ? '成功' : '失败'}
                {form.bing_state.last_error ? `（${reasonText(form.bing_state.last_error)}）` : ''}
                {form.bing_state.last_at ? ` · ${fmtRelative(form.bing_state.last_at)}` : ''}
              </span>
            )}
            {!form.bing_state.available && form.bing_state.unavailable_detail && (
              <span className="text-xs text-muted-foreground">
                部署前置：deploy/install_playwright.sh（{form.bing_state.unavailable_detail}）
              </span>
            )}
            {isAdmin && (
              <div className="flex justify-end gap-[6px]">
                <Button quiet icon="RotateCcw" loading={busy} onClick={() => void save({ reset_bing_circuit: true })}>
                  重置 Bing 熔断
                </Button>
              </div>
            )}
          </div>
        </div>
      </Panel>

      <McpBindingsSection isAdmin={isAdmin} />

      <Panel title="抓取预算" icon="Gauge" padded>
        <div className="form-grid">
          <Field label="单轮总页数" hint="硬上限 10 页">
            <Input
              type="number"
              min={1}
              max={10}
              value={form.crawl_max_pages}
              onChange={(e) => patch('crawl_max_pages', Number(e.target.value))}
              disabled={!isAdmin || busy}
            />
          </Field>
          <Field label="单来源页数" hint="单站点每轮最多 3 页">
            <Input
              type="number"
              min={1}
              max={3}
              value={form.crawl_pages_per_source}
              onChange={(e) => patch('crawl_pages_per_source', Number(e.target.value))}
              disabled={!isAdmin || busy}
            />
          </Field>
        </div>
        <div className="field-hint mt-[8px]">
          抓取统一遵守 robots 与 Crawl-delay（1~5 秒）、单主机串行、URL 与正文指纹去重；
          低于主题阈值的页面与低质量页面会被丢弃。
        </div>
        {isAdmin && (
          <div className="mt-[10px] flex justify-end">
            <Button variant="primary" icon="Check" loading={busy} onClick={() => void save()}>
              保存设置
            </Button>
          </div>
        )}
      </Panel>
    </div>
  )
}

interface BindingFormState {
  server: number
  tool: number
  server_name: string
  tool_name: string
  enabled: boolean
  priority: string
  query_field: string
  limit_field: string
  default_limit: string
  results_path: string
  url_field: string
  title_field: string
  snippet_field: string
  note: string
}

function bindingFormOf(candidate: TiangongSearchCandidate): BindingFormState {
  const suggested = candidate.suggested_mapping
  return {
    server: candidate.server_id,
    tool: candidate.tool_id,
    server_name: candidate.server_name,
    tool_name: candidate.tool_name,
    enabled: true,
    priority: '100',
    query_field: suggested.query_field,
    limit_field: suggested.limit_field,
    default_limit: String(suggested.default_limit),
    results_path: suggested.results_path,
    url_field: suggested.url_field,
    title_field: suggested.title_field,
    snippet_field: suggested.snippet_field,
    note: '',
  }
}

/** L1 MCP 搜索绑定：从扫描候选确认工具、配置映射、测试与停用 */
function McpBindingsSection({ isAdmin }: { isAdmin: boolean }) {
  const { data, isLoading, error, refetch } = useTiangongBindings({ page: 1, page_size: 100 })
  const bindings = useMemo(() => data?.results ?? [], [data])
  const scan = useScanTiangongCandidates()
  const create = useCreateTiangongBinding()
  const update = useUpdateTiangongBinding()
  const remove = useDeleteTiangongBinding()
  const test = useTestTiangongBinding()

  const [scanOpen, setScanOpen] = useState(false)
  const [candidates, setCandidates] = useState<TiangongSearchCandidate[]>([])
  const [formOpen, setFormOpen] = useState(false)
  const [form, setForm] = useState<BindingFormState | null>(null)
  const [formErrors, setFormErrors] = useState<string[]>([])
  const [deleteTarget, setDeleteTarget] = useState<TiangongSearchBinding | null>(null)
  const [testQuery, setTestQuery] = useState('工伤认定')
  const [testingId, setTestingId] = useState<number | null>(null)

  async function openScan() {
    setScanOpen(true)
    try {
      const result = await scan.mutateAsync()
      setCandidates(result.candidates)
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  function pickCandidate(candidate: TiangongSearchCandidate) {
    setForm(bindingFormOf(candidate))
    setFormErrors([])
    setScanOpen(false)
    setFormOpen(true)
  }

  async function submit() {
    if (!form) return
    setFormErrors([])
    const payload: TiangongSearchBindingPayload = {
      server: form.server,
      tool: form.tool,
      enabled: form.enabled,
      priority: Number(form.priority) || 100,
      query_field: form.query_field.trim(),
      limit_field: form.limit_field.trim(),
      default_limit: Number(form.default_limit) || 5,
      results_path: form.results_path.trim(),
      url_field: form.url_field.trim(),
      title_field: form.title_field.trim(),
      snippet_field: form.snippet_field.trim(),
      note: form.note.trim(),
    }
    try {
      await create.mutateAsync(payload)
      toast.success(`已添加绑定「${form.server_name} · ${form.tool_name}」`)
      setFormOpen(false)
    } catch (err) {
      const body = (err as { body?: Record<string, unknown> | null }).body ?? null
      const errs = extractFieldErrors(body)
      setFormErrors(errs.length ? errs : [(err as Error).message || '保存失败'])
    }
  }

  async function runTest(binding: TiangongSearchBinding) {
    setTestingId(binding.id)
    try {
      const result = await test.mutateAsync({ id: binding.id, query: testQuery })
      if (result.results.length) {
        toast.success(`测试通过：${result.results.length} 条结果，${result.elapsed_ms} ms`)
      } else {
        toast.error(`测试未取到结果：${reasonText(result.reason)}`)
      }
    } catch (err) {
      toast.error((err as Error).message)
    } finally {
      setTestingId(null)
    }
  }

  async function toggle(binding: TiangongSearchBinding, enabled: boolean) {
    try {
      await update.mutateAsync({ id: binding.id, payload: { enabled } })
      toast.success(enabled ? '已启用绑定' : '已停用绑定')
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return
    try {
      await remove.mutateAsync(deleteTarget.id)
      toast.success('已删除绑定')
      setDeleteTarget(null)
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  return (
    <>
      <Panel
        title="L1 MCP 搜索绑定"
        icon="Plug"
        meta={`共 ${bindings.length} 条 · 主路优先`}
        padded={false}
        actions={
          isAdmin ? (
            <Button quiet icon="Search" loading={scan.isPending} onClick={() => void openScan()}>
              扫描搜索工具
            </Button>
          ) : undefined
        }
      >
        {error ? (
          <EmptyState
            icon="TriangleAlert"
            title="绑定读取失败"
            description={error.message}
            action={<Button quiet icon="RefreshCw" onClick={() => void refetch()}>重新加载</Button>}
          />
        ) : (
          <DataTable
            loading={isLoading && bindings.length === 0}
            rows={bindings}
            rowKey={(b) => b.id}
            empty={
              <EmptyState
                compact
                icon="Plug"
                title="还没有 MCP 搜索绑定"
                description="扫描已接入的 MCP 工具，确认搜索工具并配置 query/limit 与结果字段映射；未配置时自动降级到 SearXNG"
              />
            }
            columns={[
              {
                key: 'binding',
                title: '绑定',
                render: (b) => (
                  <span className="row-main">
                    <span className="row-title flex items-center gap-[6px]">
                      {b.server_name} · {b.tool_name}
                      <Badge tone={b.usable ? 'success' : b.enabled ? 'warning' : 'neutral'}>
                        {b.usable ? '可用' : b.enabled ? reasonText(b.unusable_reason) : '已停用'}
                      </Badge>
                    </span>
                    <span className="row-sub mono truncate">{b.tool_full_name}</span>
                  </span>
                ),
              },
              {
                key: 'mapping',
                title: '映射',
                render: (b) => (
                  <span className="text-xs text-muted-foreground">
                    query={b.query_field} · limit={b.limit_field || '—'} · 路径 {b.results_path || '顶层'}
                  </span>
                ),
              },
              {
                key: 'priority',
                title: '优先级',
                width: 80,
                align: 'right',
                tnum: true,
                render: (b) => b.priority,
              },
              {
                key: 'last',
                title: '最近测试',
                width: 170,
                render: (b) => (
                  <span className="text-xs text-muted-foreground">
                    {b.last_status
                      ? `${b.last_status === 'ok' ? '通过' : '失败'} · ${b.last_result_count} 条 · ${b.last_latency_ms} ms`
                      : '未测试'}
                    {b.last_error ? <span className="block truncate">{b.last_error}</span> : null}
                  </span>
                ),
              },
              {
                key: 'actions',
                title: '操作',
                width: 200,
                align: 'right',
                render: (b) =>
                  isAdmin ? (
                    <span className="flex items-center justify-end gap-[2px]" onClick={(e) => e.stopPropagation()}>
                      <Switch checked={b.enabled} onChange={(v) => void toggle(b, v)} />
                      <Button
                        quiet
                        icon="Activity"
                        loading={testingId === b.id}
                        onClick={() => void runTest(b)}
                      >
                        测试
                      </Button>
                      <IconButton icon="Trash2" label="删除绑定" onClick={() => setDeleteTarget(b)} />
                    </span>
                  ) : (
                    <span className="text-xs text-muted-foreground">只读</span>
                  ),
              },
            ]}
          />
        )}
      </Panel>
      {isAdmin && bindings.length > 0 && (
        <div className="mt-[6px] flex items-center justify-end gap-[6px] text-xs text-muted-foreground">
          <span>测试查询</span>
          <Input
            className="w-[160px]"
            value={testQuery}
            onChange={(e) => setTestQuery(e.target.value)}
            maxLength={60}
          />
        </div>
      )}

      <Modal
        open={scanOpen}
        wide
        icon="Search"
        title="搜索工具候选"
        onClose={() => setScanOpen(false)}
        footer={<Button variant="ghost" onClick={() => setScanOpen(false)}>关闭</Button>}
      >
        {scan.isPending ? (
          <div className="text-sm text-muted-foreground">正在扫描已接入的 MCP 工具…</div>
        ) : candidates.length === 0 ? (
          <EmptyState
            compact
            icon="Plug"
            title="没有匹配到搜索工具"
            description="请先在 MCP 管理端接入并发现工具；候选只按工具名、描述与入参打分，不会自动启用工具"
          />
        ) : (
          <div className="col-stack">
            {candidates.map((item) => (
              <div
                key={`${item.server_id}-${item.tool_id}`}
                className="flex items-start justify-between gap-[10px] rounded-md border border-border px-[10px] py-[8px]"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-[6px] text-sm font-medium">
                    {item.server_name} · {item.tool_name}
                    <Badge tone={item.bound ? 'neutral' : 'primary'}>{item.bound ? '已绑定' : `得分 ${item.score}`}</Badge>
                    {!item.tool_enabled && <Badge tone="warning">工具未启用</Badge>}
                  </div>
                  <div className="mono truncate text-xs text-muted-foreground">{item.tool_full_name}</div>
                  <div className="mt-[2px] text-xs text-muted-foreground">
                    {item.reasons.join('；') || item.description || '—'}
                  </div>
                </div>
                <Button
                  quiet
                  icon="Plus"
                  disabled={item.bound}
                  onClick={() => pickCandidate(item)}
                >
                  使用
                </Button>
              </div>
            ))}
          </div>
        )}
      </Modal>

      <Modal
        open={formOpen}
        wide
        icon="Plug"
        title={form ? `新增绑定 · ${form.server_name} / ${form.tool_name}` : '新增绑定'}
        onClose={create.isPending ? () => undefined : () => setFormOpen(false)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setFormOpen(false)} disabled={create.isPending}>
              取消
            </Button>
            <Button variant="primary" icon="Check" loading={create.isPending} onClick={() => void submit()}>
              保存
            </Button>
          </>
        }
      >
        {form && (
          <>
            {formErrors.length > 0 && (
              <div className="mb-[10px] rounded-md border border-state-error-border bg-state-error-soft px-[10px] py-[8px] text-xs leading-relaxed text-state-error">
                {formErrors.map((e, i) => <div key={i}>{e}</div>)}
              </div>
            )}
            <div className="mb-[10px] rounded-md bg-muted px-[10px] py-[8px] text-xs text-muted-foreground">
              绑定只保存映射与优先级，不保存凭据；凭据仍由 MCP 来源的加密密钥管理。
              创建绑定不会自动启用该工具，需在 MCP 管理端单独开启。
            </div>
            <div className="form-grid">
              <Field label="优先级" hint="数字越小越先调用">
                <Input type="number" min={0} value={form.priority}
                  onChange={(e) => setForm({ ...form, priority: e.target.value })} />
              </Field>
              <Field label="默认返回条数" hint="1~10">
                <Input type="number" min={1} max={10} value={form.default_limit}
                  onChange={(e) => setForm({ ...form, default_limit: e.target.value })} />
              </Field>
              <Field label="查询入参名" required hint="工具 input_schema 里的查询字段，如 query / q">
                <Input value={form.query_field}
                  onChange={(e) => setForm({ ...form, query_field: e.target.value })} maxLength={64} />
              </Field>
              <Field label="数量入参名" hint="留空表示不传数量参数">
                <Input value={form.limit_field}
                  onChange={(e) => setForm({ ...form, limit_field: e.target.value })} maxLength={64} />
              </Field>
              <Field label="结果列表路径" hint="点号路径，如 results / data.items；留空表示顶层列表">
                <Input value={form.results_path}
                  onChange={(e) => setForm({ ...form, results_path: e.target.value })} maxLength={120} />
              </Field>
              <Field label="URL 字段" required>
                <Input value={form.url_field}
                  onChange={(e) => setForm({ ...form, url_field: e.target.value })} maxLength={64} />
              </Field>
              <Field label="标题字段">
                <Input value={form.title_field}
                  onChange={(e) => setForm({ ...form, title_field: e.target.value })} maxLength={64} />
              </Field>
              <Field label="摘要字段">
                <Input value={form.snippet_field}
                  onChange={(e) => setForm({ ...form, snippet_field: e.target.value })} maxLength={64} />
              </Field>
              <Field label="备注" className="span-full">
                <Input value={form.note}
                  onChange={(e) => setForm({ ...form, note: e.target.value })} maxLength={255} />
              </Field>
              <div className="span-full">
                <Switch checked={form.enabled} onChange={(v) => setForm({ ...form, enabled: v })}
                  label="启用该绑定" hint="停用后 L1 跳过，直接降级到 SearXNG" />
              </div>
            </div>
          </>
        )}
      </Modal>

      <ConfirmDialog
        open={deleteTarget !== null}
        tone="danger"
        title="删除搜索绑定"
        message={deleteTarget ? `将删除「${deleteTarget.server_name} · ${deleteTarget.tool_name}」的映射，不影响 MCP 工具本身。` : ''}
        confirmText="删除"
        loading={remove.isPending}
        onConfirm={() => void confirmDelete()}
        onCancel={() => setDeleteTarget(null)}
      />
    </>
  )
}

const REASON_LABELS: Record<string, string> = {
  mcp_not_configured: '未配置绑定',
  mcp_disabled: '已停用',
  mcp_tool_disabled: '工具已停用',
  mcp_unhealthy: '来源异常',
  mcp_call_failed: '调用失败',
  mcp_empty: '空结果',
  mcp_timeout: '调用超时',
  mcp_auth_error: '认证失败',
  mcp_schema_mismatch: '映射不匹配',
  mcp_result_parse_error: '结果解析失败',
  bing_disabled: '未开启',
  bing_circuit_open: '熔断中',
  bing_daily_limit: '当日额度用尽',
  bing_unavailable: '未安装 Playwright',
  bing_browser_missing: 'Chromium 未安装',
  bing_health_failed: '浏览器启动失败',
  bing_captcha: '触发验证码',
  bing_rate_limited: '被限流（429）',
  bing_blocked: '访问被拒绝（403）',
  bing_timeout: '页面超时',
  bing_parse_error: '结果解析失败',
  bing_empty: '空结果',
  bing_browser_error: '浏览器异常',
}

function reasonText(reason: string) {
  return REASON_LABELS[reason] || reason || '不可用'
}
