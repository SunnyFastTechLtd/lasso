import { USAGE_METRICS } from '@/api/usage'
import type { UsageBucket, UsageMetricKey } from '@/api/usage'
import { EmptyState } from '@/components/ui/EmptyState'
import { fmtNumber } from '@/utils/format'

interface Props {
  series: UsageBucket[]
  /** all＝四项并排；选中单项则只画该项并按自身最大值缩放 */
  metric: 'all' | UsageMetricKey
}

/** 2026-10-27 → 10/27；周标签等其它格式原样截断展示 */
function shortLabel(bucket: string) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(bucket)
  return m ? `${m[2]}/${m[3]}` : bucket
}

/** 趋势图：纯 CSS 柱状，不引入图表库 */
export function TrendChart({ series, metric }: Props) {
  if (series.length === 0) {
    return (
      <EmptyState
        compact
        icon="ChartColumn"
        title="所选区间没有数据"
        description="换一个时间范围或口径再看；本看板只读，不提供会话正文与个人明细"
      />
    )
  }

  const keys = metric === 'all' ? USAGE_METRICS : USAGE_METRICS.filter((m) => m.key === metric)
  const maxOf = keys.map((k) => Math.max(1, ...series.map((b) => b[k.key] ?? 0)))
  const labelEvery = Math.max(1, Math.ceil(series.length / 14))

  return (
    <div className="col-stack gap-[8px]">
      <div className="flex flex-wrap items-center gap-[10px]">
        {keys.map((k) => (
          <span key={k.key} className="flex items-center gap-[5px] text-xs text-muted-foreground">
            <span className={`h-[8px] w-[8px] flex-none rounded-sm ${k.barClass}`} />
            {k.label}
          </span>
        ))}
        <span className="toolbar-spacer" />
        <span className="text-xs text-muted-foreground">柱高按各指标自身最大值归一，悬浮看具体值</span>
      </div>

      <div className="relative flex items-end gap-[3px]" style={{ height: 168 }}>
        {[0, 25, 50, 75].map((p) => (
          <div key={p} className="pointer-events-none absolute inset-x-0 border-t border-grid-line" style={{ top: `${p}%` }} />
        ))}
        {series.map((b) => (
          <div key={b.bucket} className="relative flex h-full min-w-0 flex-1 items-end gap-[2px]">
            {keys.map((k, i) => {
              const v = b[k.key] ?? 0
              const pct = Math.round((v / maxOf[i]) * 100)
              return (
                <div
                  key={k.key}
                  title={`${b.bucket} · ${k.label} ${fmtNumber(v)}`}
                  className={`min-w-0 flex-1 rounded-t-sm ${k.barClass}`}
                  style={{ height: `${v > 0 ? Math.max(pct, 2) : 0}%`, opacity: v > 0 ? 0.9 : 0.2 }}
                />
              )
            })}
          </div>
        ))}
        <div className="pointer-events-none absolute inset-x-0 bottom-0 border-t border-border" />
      </div>

      <div className="flex gap-[3px]">
        {series.map((b, idx) => (
          <div key={b.bucket} className="min-w-0 flex-1 text-center text-xs text-muted-foreground">
            {idx % labelEvery === 0 ? shortLabel(b.bucket) : ''}
          </div>
        ))}
      </div>
    </div>
  )
}
