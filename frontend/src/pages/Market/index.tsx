import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { PageContainer, PageHeader } from '@/components/AppShell'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { DataTable } from '@/components/ui/DataTable'
import { EmptyState } from '@/components/ui/EmptyState'
import { Input, Select } from '@/components/ui/Field'
import { Drawer } from '@/components/ui/Overlay'
import { Pagination } from '@/components/ui/Pagination'
import { Panel } from '@/components/ui/Panel'
import {
  MARKET_DEGRADED_TEXT,
  nuwaApi,
  type CandidateKind,
  type MarketCandidate,
  type MarketFacet,
  type MarketItem,
  type MatchResult,
} from '@/api/nuwa'
import { toast } from '@/store/toast'
import { fmtRelative } from '@/utils/format'

/**
 * 独立市场页：工具市场 / 技能市场。
 *
 * 列表读本地目录（/nuwa/items/），「联网检索」走 /nuwa/market/search/ —— 与女娲
 * 第三步是同一个 market.search，导入同样落到 /nuwa/import-requests/ 的单级审批，
 * 页面只负责展示 mode / degraded_reason，不另起一套降级逻辑。
 */

interface Row {
  id: number
  kind: CandidateKind
  title: string
  summary: string
  publisher: string
  externalId: string
  sourceId: number
  sourceName: string
  license: string
  licenseDeclared: boolean
  trust: string
  transport: string
  requiresEgress: boolean
  hasScripts: boolean
  egressHost: string
  homepage: string
  repositoryUrl: string
  fetchedAt: string
  score?: number
  reason?: string
  myRequest: MarketItem['my_request']
}

const fromItem = (item: MarketItem): Row => ({
  id: item.id,
  kind: item.kind,
  title: item.title,
  summary: item.summary,
  publisher: item.publisher,
  externalId: item.external_id,
  sourceId: item.source,
  sourceName: item.source_name,
  license: item.license,
  licenseDeclared: item.license_declared,
  trust: item.trust_level,
  transport: item.transport,
  requiresEgress: item.requires_egress,
  hasScripts: item.has_scripts,
  egressHost: item.egress_host,
  homepage: item.homepage,
  repositoryUrl: item.repository_url,
  fetchedAt: item.fetched_at,
  myRequest: item.my_request,
})

const fromCandidate = (item: MarketCandidate, facets: MarketFacet[]): Row => ({
  id: item.id,
  kind: item.kind,
  title: item.title,
  summary: item.summary,
  publisher: item.publisher,
  externalId: item.external_id,
  sourceId: facets.find((facet) => facet.name === item.source)?.id ?? 0,
  sourceName: item.source,
  license: item.license,
  licenseDeclared: item.license_declared,
  trust: item.trust_level,
  transport: item.transport,
  requiresEgress: item.requires_egress,
  hasScripts: item.has_scripts,
  egressHost: item.egress_host,
  homepage: item.homepage,
  repositoryUrl: item.repository_url,
  fetchedAt: '',
  score: item.score,
  reason: item.reason,
  myRequest: null,
})

const REQUEST_LABEL: Record<string, { tone: 'warning' | 'success' | 'error'; text: string }> = {
  pending: { tone: 'warning', text: '待审批' },
  approved: { tone: 'success', text: '已通过' },
  rejected: { tone: 'error', text: '已驳回' },
}

/** 市场检索体：既可独立成页，也可嵌进「工具」「技能」页的第二个选项卡 */
export function MarketBrowser({ kind, embedded = false }: { kind: CandidateKind; embedded?: boolean }) {
  const isMcp = kind === 'mcp'

  const [searchInput, setSearchInput] = useState('')
  const [search, setSearch] = useState('')
  const [source, setSource] = useState('')
  const [trust, setTrust] = useState<'' | 'trusted' | 'unverified'>('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [rows, setRows] = useState<Row[]>([])
  const [count, setCount] = useState(0)
  const [loading, setLoading] = useState(true)
  const [online, setOnline] = useState<MatchResult | null>(null)
  const [onlineLoading, setOnlineLoading] = useState(false)
  const [facets, setFacets] = useState<MarketFacet[]>([])
  const [detailId, setDetailId] = useState<number | null>(null)
  const [detail, setDetail] = useState<MarketItem | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  // 切页（工具 ↔ 技能）时清空上一次的检索与筛选，避免两页状态串味
  useEffect(() => {
    setSearchInput('')
    setSearch('')
    setSource('')
    setTrust('')
    setPage(1)
    setOnline(null)
    setDetailId(null)
    setDetail(null)
  }, [kind])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setSearch(searchInput.trim())
      setPage(1)
    }, 350)
    return () => window.clearTimeout(timer)
  }, [searchInput])

  useEffect(() => {
    nuwaApi
      .marketFacets()
      .then(setFacets)
      .catch(() => setFacets([]))
  }, [])

  useEffect(() => {
    if (online) return
    let cancelled = false
    setLoading(true)
    nuwaApi
      .marketItems({
        page,
        page_size: pageSize,
        kind,
        ...(search ? { search } : {}),
        ...(source ? { source: Number(source) } : {}),
        ...(trust ? { trust } : {}),
      })
      .then((data) => {
        if (cancelled) return
        setRows((data.results ?? []).map(fromItem))
        setCount(data.count ?? 0)
      })
      .catch((err) => {
        if (!cancelled) toast.error((err as Error).message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [kind, search, source, trust, page, pageSize, online])

  const visibleRows = useMemo(() => {
    if (!online) return rows
    // 在线结果只在本地做筛选（结果集已固定，不分页）
    const keyword = searchInput.trim().toLowerCase()
    return rows.filter(
      (row) =>
        (!source || String(row.sourceId) === source) &&
        (!trust || row.trust === trust) &&
        (!keyword || `${row.title} ${row.summary}`.toLowerCase().includes(keyword)),
    )
  }, [online, rows, source, trust, searchInput])

  const sourceOptions = useMemo(
    () => facets.filter((facet) => (isMcp ? facet.kind !== 'skill_repo' : facet.kind !== 'mcp_registry')),
    [facets, isMcp],
  )

  async function runOnlineSearch() {
    setOnlineLoading(true)
    try {
      const result = await nuwaApi.searchMarket({
        query: searchInput.trim(),
        kinds: [kind],
        ...(source ? { source: Number(source) } : {}),
        limit: 30,
      })
      setOnline(result)
      setRows(result.items.map((item) => fromCandidate(item, facets)))
      if (result.mode === 'cached') {
        toast.warning(
          MARKET_DEGRADED_TEXT[result.degraded_reason] ?? '在线检索未命中，已展示本地缓存目录。',
        )
      }
    } catch (err) {
      toast.error((err as Error).message)
    } finally {
      setOnlineLoading(false)
    }
  }

  async function openDetail(row: Row) {
    setDetailId(row.id)
    setDetail(null)
    setDetailLoading(true)
    try {
      setDetail(await nuwaApi.marketItem(row.id))
    } catch (err) {
      toast.error((err as Error).message)
    } finally {
      setDetailLoading(false)
    }
  }

  async function submitImport() {
    if (!detail) return
    setSubmitting(true)
    try {
      const result = await nuwaApi.submitImport(detail.id)
      toast.success(
        result.created
          ? '已提交导入申请，等待管理员审批'
          : '已有待审批申请，无需重复提交',
      )
      setDetail(await nuwaApi.marketItem(detail.id))
      setRows((prev) =>
        prev.map((row) =>
          row.id === detail.id
            ? { ...row, myRequest: { id: result.id, status: result.status as 'pending', created_at: '' } }
            : row,
        ),
      )
    } catch (err) {
      toast.error((err as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  const totalPages = Math.max(1, Math.ceil(count / pageSize))
  const submitted = detail?.my_request

  const body = (
    <>
    <div className="space-y-[12px]">
        <Panel
          title="筛选"
          icon="Search"
          actions={
            online ? (
              <Button
                icon="RotateCcw"
                onClick={() => {
                  setOnline(null)
                  setPage(1)
                }}
              >
                回到本地目录
              </Button>
            ) : undefined
          }
        >
          <div className="flex flex-wrap items-center gap-[8px]">
            <Input
              className="min-w-[220px] flex-1"
              value={searchInput}
              placeholder={
                isMcp ? '搜索工具名称或能力，如 postgres / excel / 搜索' : '搜索技能名称或能力，如 pdf / 报告 / 翻译'
              }
              onChange={(e) => setSearchInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void runOnlineSearch()
              }}
            />
            <Select
              className="w-[190px]"
              value={source}
              onChange={(e) => {
                setSource(e.target.value)
                setPage(1)
              }}
            >
              <option value="">全部来源</option>
              {sourceOptions.map((facet) => (
                <option key={facet.id} value={facet.id}>
                  {facet.name}（{facet.count}）
                </option>
              ))}
            </Select>
            <Select
              className="w-[140px]"
              value={trust}
              onChange={(e) => {
                setTrust(e.target.value as '' | 'trusted' | 'unverified')
                setPage(1)
              }}
            >
              <option value="">全部可信度</option>
              <option value="trusted">可信发布方</option>
              <option value="unverified">未验证</option>
            </Select>
            <Button variant="primary" icon="Globe" loading={onlineLoading} onClick={runOnlineSearch}>
              联网检索
            </Button>
          </div>

          {online && (
            <div className="mt-[10px] rounded border border-border/60 bg-surface-1 px-[10px] py-[8px] text-xs leading-[1.6]">
              检索方式：
              <span className="font-medium">
                {online.mode === 'online'
                  ? '实时（公网）'
                  : online.mode === 'mixed'
                    ? '实时 + 本地缓存'
                    : '本地缓存'}
              </span>
              {online.cache_synced_at && (
                <span className="text-muted-foreground">
                  {' '}
                  · 缓存同步于 {online.cache_synced_at.slice(0, 10)}
                </span>
              )}
              <span className="text-muted-foreground">
                {' '}
                · 在线命中 {online.online_hits} 条
                {online.online_attempted ? '' : '（本次未尝试联网）'}
              </span>
              {online.degraded_reason && (
                <div className="mt-[4px] text-state-warning">
                  {MARKET_DEGRADED_TEXT[online.degraded_reason] ?? online.degraded_reason}
                </div>
              )}
            </div>
          )}
          {!online && (
            <div className="mt-[8px] text-xs text-muted-foreground">
              列表来自本地目录副本；点「联网检索」实时查询公网市场，网络不可用时自动回落到本地并标注原因。
            </div>
          )}
        </Panel>

        <Panel
          title={online ? '检索结果' : '本地目录'}
          icon={isMcp ? 'Plug' : 'Puzzle'}
          meta={online ? '按匹配度排序' : '按名称排序'}
        >
          <DataTable
            className="data-table-fixed"
            rows={visibleRows}
            loading={loading && !online}
            rowKey={(row) => row.id}
            onRowClick={openDetail}
            empty={
              <EmptyState
                icon="Inbox"
                title={online ? '在线检索没有命中' : '本地目录还没有条目'}
                description={
                  online
                    ? '换个关键词，或回到本地目录；网络不可用时会自动回落到缓存并标注原因。'
                    : '管理员可执行 python manage.py nuwa_sync 同步目录，或关闭在线开关后改用离线包导入。'
                }
                action={
                  !online ? (
                    <Button quiet icon="Globe" loading={onlineLoading} onClick={runOnlineSearch}>
                      联网检索
                    </Button>
                  ) : undefined
                }
              />
            }
            columns={[
              {
                key: 'title',
                title: '名称',
                width: 260,
                render: (row) => (
                  <div className="min-w-0">
                    <div className="flex items-center gap-[6px]">
                      <span className="truncate font-medium" title={row.title}>
                        {row.title}
                      </span>
                      {row.myRequest && (
                        <Badge tone={REQUEST_LABEL[row.myRequest.status]?.tone ?? 'neutral'}>
                          {REQUEST_LABEL[row.myRequest.status]?.text ?? row.myRequest.status}
                        </Badge>
                      )}
                      {row.score !== undefined && (
                        <span className="shrink-0 text-xs text-muted-foreground tnum">
                          匹配 {Math.round(row.score * 100)}%
                        </span>
                      )}
                    </div>
                    <div className="truncate text-xs text-muted-foreground" title={row.publisher || row.externalId}>
                      {row.publisher || row.externalId}
                    </div>
                  </div>
                ),
              },
              {
                key: 'summary',
                title: '能力摘要',
                width: 300,
                secondary: true,
                render: (row) => (
                  <span className="block truncate text-xs text-muted-foreground" title={row.summary || '（无摘要）'}>
                    {row.summary || '（无摘要）'}
                  </span>
                ),
              },
              {
                key: 'sourceName',
                title: '来源',
                width: 150,
                secondary: true,
                render: (row) => (
                  <span className="block truncate text-xs" title={row.sourceName}>
                    {row.sourceName}
                  </span>
                ),
              },
              {
                key: 'marks',
                title: '标记',
                width: 200,
                render: (row) => {
                  const marks = [
                    row.requiresEgress ? '需出网' : '',
                    row.hasScripts ? '含脚本' : '',
                    !row.licenseDeclared ? '许可证未声明' : '',
                  ].filter(Boolean)
                  return (
                    <Badge
                      tone={marks.length ? 'warning' : 'neutral'}
                      title={marks.length ? marks.join(' · ') : '无需出网 · 无脚本 · 许可证已声明'}
                    >
                      {marks.length ? `需复核 ${marks.length} 项` : '常规'}
                    </Badge>
                  )
                },
              },
              {
                key: 'trust',
                title: '可信度',
                width: 110,
                render: (row) => (
                  <Badge tone={row.trust === 'trusted' ? 'success' : 'neutral'}>
                    {row.trust === 'trusted' ? '可信发布方' : '未验证'}
                  </Badge>
                ),
              },
              {
                key: 'fetchedAt',
                title: '更新',
                width: 110,
                secondary: true,
                render: (row) =>
                  row.fetchedAt ? (
                    <span className="text-xs text-muted-foreground">{fmtRelative(row.fetchedAt)}</span>
                  ) : (
                    <span className="text-xs text-muted-foreground">{row.reason ?? '—'}</span>
                  ),
              },
            ]}
          />
          {!online && count > 0 && (
            <Pagination
              page={page}
              totalPages={totalPages}
              count={count}
              pageSize={pageSize}
              onPageChange={setPage}
              onPageSizeChange={(size) => {
                setPageSize(size)
                setPage(1)
              }}
            />
          )}
        </Panel>
      </div>

      <Drawer
        open={detailId !== null}
        title={detail?.title ?? (detailLoading ? '读取中…' : '条目详情')}
        meta={
          detail
            ? [detail.source_name, detail.publisher, detail.version && `v${detail.version}`]
                .filter(Boolean)
                .join(' · ')
            : detailId !== null
              ? `条目 #${detailId}`
              : undefined
        }
        onClose={() => setDetailId(null)}
        footer={
          <div className="flex w-full items-center justify-between gap-[8px]">
            <span className="text-xs text-muted-foreground">
              提交后进入待审批；审批通过才建来源/技能
            </span>
            <Button
              variant="primary"
              icon="Plus"
              loading={submitting}
              disabled={!detail || submitted?.status === 'pending' || submitted?.status === 'approved'}
              onClick={submitImport}
            >
              {submitted?.status === 'pending'
                ? '已提交待审批'
                : submitted?.status === 'approved'
                  ? '已通过审批'
                  : '提交导入申请'}
            </Button>
          </div>
        }
      >
        {!detail && (
          <div className="text-xs text-muted-foreground">
            {detailLoading ? '正在读取条目…' : '条目不可用，可能已被清理。'}
          </div>
        )}
        {detail && (
          <div className="space-y-[14px]">
            <div className="flex flex-wrap items-center gap-[6px]">
              <Badge tone={detail.kind === 'mcp' ? 'primary' : 'neutral'}>
                {detail.kind === 'mcp' ? 'MCP 工具来源' : '技能'}
              </Badge>
              {submitted && (
                <Badge tone={REQUEST_LABEL[submitted.status]?.tone ?? 'neutral'}>
                  我的申请：{REQUEST_LABEL[submitted.status]?.text ?? submitted.status}
                </Badge>
              )}
            </div>

            <dl className="space-y-[8px]">
              <DetailRow label="能力摘要" value={detail.summary || '—'} />
              <DetailRow
                label="可信度"
                value={detail.trust_level === 'trusted' ? '可信发布方' : '未验证'}
                warning={detail.trust_level !== 'trusted'}
              />
              {detail.transport && <DetailRow label="连接类型" value={detail.transport} mono />}
              <DetailRow
                label="是否需要出网"
                value={
                  detail.requires_egress
                    ? `需要${detail.egress_host ? `（目标主机 ${detail.egress_host}）` : ''}`
                    : '不需要'
                }
              />
              <DetailRow label="是否含脚本" value={detail.has_scripts ? '含脚本 / 本地可执行包' : '不含脚本'} />
              <DetailRow
                label="许可证"
                value={detail.license || '未声明'}
                warning={!detail.license_declared}
              />
              <DetailRow label="来源" value={detail.source_name} />
              <DetailRow label="外部标识" value={detail.external_id} mono />
              {detail.capabilities?.length > 0 && (
                <DetailRow label="能力标签" value={detail.capabilities.slice(0, 12).join(' · ')} />
              )}
              <DetailRow
                label="链接"
                value={
                  <span className="flex flex-col gap-[2px]">
                    {detail.homepage && (
                      <a className="text-primary hover:underline" href={detail.homepage} target="_blank" rel="noreferrer">
                        {detail.homepage}
                      </a>
                    )}
                    {detail.repository_url && (
                      <a
                        className="text-primary hover:underline"
                        href={detail.repository_url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {detail.repository_url}
                      </a>
                    )}
                    {!detail.homepage && !detail.repository_url && '未提供'}
                  </span>
                }
              />
            </dl>

            <div className="rounded border border-border/60 bg-surface-1 px-[10px] py-[8px] text-xs leading-[1.7] text-muted-foreground">
              审批口径：MCP 来源落库后状态为「未验证」且所有工具默认关闭，运行出网还需管理员另行授权；技能落库后为「待审核」，正文通过静态校验并可挂载后才对智能体可见。提交人不可是审批人。
            </div>
          </div>
        )}
      </Drawer>
    </>
  )

  if (embedded) return body

  return (
    <PageContainer>
      <PageHeader
        title={isMcp ? '工具市场' : '技能市场'}
        subtitle={
          isMcp
            ? '在线检索公开 MCP 目录。导入走管理员单级审批：落库后状态为「未验证」，所有工具默认关闭，出网仍需单独授权。'
            : '在线检索公开技能仓库。导入走管理员单级审批：落库后为「待审核」，审核通过前不进运行时。'
        }
        badge={<Badge tone="neutral">{online ? '在线结果' : '本地目录'}</Badge>}
      />
      {body}
    </PageContainer>
  )
}

function DetailRow({
  label,
  value,
  mono = false,
  warning = false,
}: {
  label: string
  value: ReactNode
  mono?: boolean
  warning?: boolean
}) {
  return (
    <div className="grid grid-cols-[92px_1fr] items-start gap-[8px]">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={`text-xs leading-[1.6] ${mono ? 'mono' : ''} ${warning ? 'text-state-warning' : ''}`}>
        {value}
      </dd>
    </div>
  )
}
