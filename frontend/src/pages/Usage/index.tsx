import { useMemo, useState } from 'react'
import { PageContainer, PageHeader } from '@/components/AppShell'
import { Badge } from '@/components/ui/Badge'
import { Button, IconButton } from '@/components/ui/Button'
import { EmptyState } from '@/components/ui/EmptyState'
import { Input } from '@/components/ui/Field'
import { Icon } from '@/components/ui/Icon'
import { Panel, SectionLabel } from '@/components/ui/Panel'
import { Seg } from '@/components/ui/Seg'
import { useUsageOverview } from '@/api/hooks/useUsage'
import { ApiError } from '@/api/request'
import {
  USAGE_BREAKDOWN_LABEL,
  USAGE_GRANULARITY_LABEL,
  USAGE_METRICS,
  usageApi,
  type UsageBreakdownDimension,
  type UsageGranularity,
  type UsageMetricKey,
  type UsageScope,
} from '@/api/usage'
import { useUserStore } from '@/store/user'
import { MetricCard } from './MetricCard'
import { TrendChart } from './TrendChart'

const GRANULARITY_ITEMS: { value: UsageGranularity; label: string }[] = (['day', 'week'] as UsageGranularity[]).map(
  (v) => ({ value: v, label: USAGE_GRANULARITY_LABEL[v] }),
)

const METRIC_SEG: { value: string; label: string }[] = [
  { value: 'all', label: '全部指标' },
  ...USAGE_METRICS.map((m) => ({ value: m.key, label: m.label })),
]

const BREAKDOWN_SEG: { value: string; label: string }[] = (
  ['agent', 'source', 'department'] as UsageBreakdownDimension[]
).map((v) => ({ value: v, label: USAGE_BREAKDOWN_LABEL[v] }))

/** 出网调用的空值说明：P0 出网总开关出厂为关，0 是正常状态而非故障 */
const EGRESS_EMPTY_NOTE = '当前无出网来源'

function toYMD(d: Date) {
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${day}`
}

function shiftDays(d: Date, n: number) {
  const c = new Date(d)
  c.setDate(c.getDate() + n)
  return c
}

/** 按日默认近 7 天，按周默认近 4 周 */
function defaultRange(g: UsageGranularity) {
  const end = new Date()
  const start = shiftDays(end, g === 'day' ? -6 : -27)
  return { start: toYMD(start), end: toYMD(end) }
}

export default function UsagePage() {
  const myDept = useUserStore((s) => s.user?.department ?? '')

  const [granularity, setGranularity] = useState<UsageGranularity>('day')
  const [scope, setScope] = useState<UsageScope>('all')
  const [range, setRange] = useState(() => defaultRange('day'))
  const [trendMetric, setTrendMetric] = useState<'all' | UsageMetricKey>('all')
  const [breakdownDim, setBreakdownDim] = useState<UsageBreakdownDimension>('agent')
  const [exporting, setExporting] = useState(false)
  const [exportError, setExportError] = useState('')

  const rangeValid = range.start <= range.end
  const params = useMemo(
    () => ({ granularity, start: range.start, end: range.end, scope }),
    [granularity, range, scope],
  )

  const { data, isFetching, error, refetch } = useUsageOverview(params, rangeValid)

  const scopeItems: { value: string; label: string }[] = [
    { value: 'all', label: '全企业' },
    ...(myDept ? [{ value: 'dept', label: `本部门 · ${myDept}` }] : []),
  ]

  const denied = error instanceof ApiError && error.status === 403
  const shownScope = data?.scope ?? scope
  const shownDept = data ? data.department : scope === 'dept' ? myDept : null

  function changeGranularity(next: UsageGranularity) {
    setGranularity(next)
    setRange(defaultRange(next))
  }

  /** 导出与当前看板同口径的 CSV；文件名带区间，后端已带 BOM 便于 Excel 打开 */
  async function handleExport() {
    setExporting(true)
    setExportError('')
    try {
      const blob = await usageApi.exportCsv({ start: range.start, end: range.end, scope })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `usage-${range.start}-${range.end}.csv`
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
    } catch (err) {
      setExportError(err instanceof ApiError ? err.message : '导出失败，请稍后重试')
    } finally {
      setExporting(false)
    }
  }

  const breakdownRows = data?.breakdowns?.[breakdownDim] ?? []
  const breakdownDefinition = data?.breakdown_definitions?.[breakdownDim]

  return (
    <PageContainer>
      <PageHeader
        title="用量看板"
        subtitle="只读统计：五项总量与趋势之外，按智能体 / 工具来源 / 部门看调用次数、失败率与平均耗时（F4）"
        actions={
          <span className="flex items-center gap-[6px]">
            <Badge tone="neutral" title="不含会话正文、提问与回答内容，不支持个人下钻">
              只读 · 无正文与个人明细
            </Badge>
            <Button
              quiet
              variant="ghost"
              icon="Download"
              loading={exporting}
              disabled={!rangeValid}
              onClick={() => void handleExport()}
            >
              导出 CSV
            </Button>
            <IconButton icon="RefreshCw" label="重新读取用量" onClick={() => void refetch()} />
          </span>
        }
      />

      <div className="page-toolbar mt-[14px]">
        <Seg items={GRANULARITY_ITEMS} value={granularity} onChange={(v) => changeGranularity(v as UsageGranularity)} />
        <Seg items={scopeItems} value={scope} onChange={(v) => setScope(v as UsageScope)} />
        <div className="flex items-center gap-[6px]">
          <span className="text-xs text-muted-foreground">起</span>
          <Input
            type="date"
            className="w-[146px] text-xs"
            value={range.start}
            max={range.end}
            onChange={(e) => setRange((r) => ({ ...r, start: e.target.value }))}
          />
          <span className="text-xs text-muted-foreground">止</span>
          <Input
            type="date"
            className="w-[146px] text-xs"
            value={range.end}
            min={range.start}
            onChange={(e) => setRange((r) => ({ ...r, end: e.target.value }))}
          />
          <Button quiet variant="ghost" icon="RotateCcw" onClick={() => setRange(defaultRange(granularity))}>
            默认区间
          </Button>
        </div>
        {!rangeValid && <span className="toolbar-note text-state-error">起始日期晚于结束日期，未发起查询</span>}
        {exportError && <span className="toolbar-note text-state-error">{exportError}</span>}
        <span className="toolbar-spacer" />
        {isFetching && <span className="toolbar-note">读取中…</span>}
      </div>

      <Panel padded className="mt-[12px]">
        <div className="flex flex-wrap items-center gap-[8px] text-xs">
          <SectionLabel>当前口径</SectionLabel>
          <span className="flex items-center gap-[5px] text-sidebar-foreground">
            <Icon name={shownScope === 'dept' ? 'Briefcase' : 'Globe'} size={13} />
            {shownScope === 'dept' ? `部门 · ${shownDept || '未归口部门'}` : '全企业'}
          </span>
          <span className="text-muted-foreground">｜</span>
          <span className="tnum text-xs text-sidebar-foreground">
            {data ? `${data.start} → ${data.end}` : `${range.start} → ${range.end}`}
          </span>
          <span className="text-muted-foreground">｜</span>
          <span className="text-sidebar-foreground">{USAGE_GRANULARITY_LABEL[data?.granularity ?? granularity]}</span>
          <span className="toolbar-spacer" />
          <span className="text-xs text-muted-foreground">口径以服务端返回为准；无会话正文、无个人明细</span>
        </div>
      </Panel>

      {denied ? (
        <Panel padded className="mt-[12px]">
          <EmptyState
            compact
            icon="ShieldCheck"
            title="没有该口径的查看权限"
            description={error instanceof ApiError ? error.message : '请联系管理员开通用量查看权限'}
            action={
              scope === 'all' ? (
                myDept ? (
                  <Button quiet icon="Briefcase" onClick={() => setScope('dept')}>
                    切到本部门口径
                  </Button>
                ) : undefined
              ) : (
                <Button quiet icon="Globe" onClick={() => setScope('all')}>
                  切回全企业口径
                </Button>
              )
            }
          />
        </Panel>
      ) : (
        <>
          <div className="mt-[12px] grid grid-cols-2 gap-[10px] md:grid-cols-5">
            {USAGE_METRICS.map((m) => (
              <MetricCard
                key={m.key}
                label={m.label}
                value={data?.metrics[m.key]}
                icon={m.icon}
                textClass={m.textClass}
                definition={data?.definitions[m.key]}
                note={m.key === 'egress_calls' ? EGRESS_EMPTY_NOTE : undefined}
              />
            ))}
          </div>

          <Panel
            padded
            className="mt-[12px]"
            title="用量趋势"
            icon="ChartColumn"
            meta={data ? `${data.series.length} 个时间桶` : undefined}
            actions={<Seg items={METRIC_SEG} value={trendMetric} onChange={(v) => setTrendMetric(v as 'all' | UsageMetricKey)} />}
          >
            <TrendChart series={data?.series ?? []} metric={trendMetric} />
          </Panel>

          <Panel
            padded
            className="mt-[12px]"
            title="分维度统计"
            icon="Table2"
            meta={breakdownDefinition}
            actions={
              <Seg
                items={BREAKDOWN_SEG}
                value={breakdownDim}
                onChange={(v) => setBreakdownDim(v as UsageBreakdownDimension)}
              />
            }
          >
            {breakdownRows.length === 0 ? (
              <EmptyState
                compact
                icon="Wrench"
                title="所选区间没有工具调用"
                description="分维度统计只看工具调用；会话数与提问人数仍可在上方查看"
              />
            ) : (
              <div className="overflow-x-auto">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>名称</th>
                      <th className="num-cell">调用次数</th>
                      <th className="num-cell">失败/被拒</th>
                      <th className="num-cell">失败率</th>
                      <th className="num-cell">平均耗时</th>
                    </tr>
                  </thead>
                  <tbody>
                    {breakdownRows.map((row) => (
                      <tr key={row.key} className="border-b border-border/60 last:border-b-0">
                        <td className="text-sidebar-foreground">{row.key}</td>
                        <td className="tnum num-cell">
                          {row.calls.toLocaleString('zh-CN')}
                        </td>
                        <td
                          className={`tnum num-cell ${
                            row.failures > 0 ? 'text-state-error' : ''
                          }`}
                        >
                          {row.failures.toLocaleString('zh-CN')}
                        </td>
                        <td className="tnum num-cell">
                          {(row.failure_rate * 100).toFixed(1)}%
                        </td>
                        <td className="tnum num-cell">
                          {row.avg_elapsed_ms == null ? '—' : `${row.avg_elapsed_ms} ms`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          {!data && !isFetching && rangeValid && (
            <Panel padded className="mt-[12px]">
              <EmptyState
                compact
                icon="TriangleAlert"
                title="用量数据未就绪"
                description={error instanceof Error ? error.message : '接口暂无返回，可点右上角重新读取'}
              />
            </Panel>
          )}
        </>
      )}
    </PageContainer>
  )
}
