import { http } from './request'

/** 用量看板（PRD F4：P0 只读切片 + P1 三维度完整版）：与 backend /api/usage/ 契约逐字段对齐 */

export type UsageGranularity = 'day' | 'week'
export type UsageScope = 'all' | 'dept'
export type UsageMetricKey = 'conversations' | 'askers' | 'tool_calls' | 'egress_calls' | 'failures'
export type UsageBreakdownDimension = 'agent' | 'source' | 'department'

export interface UsageMetrics {
  conversations: number
  askers: number
  /** 内网来源的工具调用（不含出网） */
  tool_calls: number
  /** 出网来源的工具调用（与内网互斥，P0 出网总开关关闭时恒为 0） */
  egress_calls: number
  failures: number
}

export interface UsageBucket extends UsageMetrics {
  /** 按日＝日期，按周＝周起始标签，由后端给字符串 */
  bucket: string
}

/** F4 完整版：单个维度下的一行聚合；平均值缺失时为 null（不是 0） */
export interface UsageBreakdownRow {
  key: string
  calls: number
  failures: number
  /** 0–1 的比例值，展示时乘 100 */
  failure_rate: number
  avg_elapsed_ms: number | null
}

export interface UsageOverview {
  scope: UsageScope
  department: string | null
  granularity: UsageGranularity
  start: string
  end: string
  metrics: UsageMetrics
  series: UsageBucket[]
  /** 每项指标的统计口径说明，界面悬浮展示 */
  definitions: Partial<Record<UsageMetricKey, string>>
  /** 按智能体 / 工具来源 / 部门的只读聚合 */
  breakdowns: Record<UsageBreakdownDimension, UsageBreakdownRow[]>
  breakdown_definitions: Partial<Record<UsageBreakdownDimension, string>>
}

export type UsageOverviewParams = {
  granularity: UsageGranularity
  start: string
  end: string
  scope: UsageScope
}

export const usageQueryKeys = {
  all: ['usage'] as const,
  overview: (params: Record<string, unknown>) => ['usage', 'overview', params] as const,
}

/** 五项指标即本期全部口径：工具调用按内网/出网互斥归属，均为 0 时不出网 */
export const USAGE_METRICS: {
  key: UsageMetricKey
  label: string
  icon: string
  barClass: string
  textClass: string
}[] = [
  /* 颜色即语义：规模用靛/青，正常工具调用用绿，出网用紫，失败用红 */
  { key: 'conversations', label: '会话数', icon: 'MessagesSquare', barClass: 'bg-chart-1', textClass: '' },
  { key: 'askers', label: '提问人数', icon: 'Users', barClass: 'bg-chart-3', textClass: '' },
  { key: 'tool_calls', label: '内网工具调用', icon: 'Wrench', barClass: 'bg-chart-4', textClass: '' },
  { key: 'egress_calls', label: '出网调用次数', icon: 'Globe', barClass: 'bg-chart-2', textClass: '' },
  { key: 'failures', label: '失败/被拒次数', icon: 'TriangleAlert', barClass: 'bg-chart-5', textClass: 'text-state-error' },
]

export const USAGE_GRANULARITY_LABEL: Record<UsageGranularity, string> = {
  day: '按日',
  week: '按周',
}

export const USAGE_BREAKDOWN_LABEL: Record<UsageBreakdownDimension, string> = {
  agent: '按智能体',
  source: '按工具来源',
  department: '按部门',
}

export const usageApi = {
  overview: (params: UsageOverviewParams) => http.get<UsageOverview>('/usage/overview/', params),
  /** F4 完整版：当前口径的 CSV 导出，返回 Blob 由页面触发下载 */
  exportCsv: (params: Omit<UsageOverviewParams, 'granularity'>) =>
    http.get<Blob>('/usage/export/', params, { responseType: 'blob', timeout: 120_000 }),
}
