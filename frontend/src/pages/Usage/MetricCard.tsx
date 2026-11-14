import { Icon } from '@/components/ui/Icon'
import { SectionLabel } from '@/components/ui/Panel'

interface Props {
  label: string
  value: number | undefined
  icon: string
  textClass?: string
  /** 后端返回的统计口径说明，悬浮展示 */
  definition?: string
  /** 固定说明文案：0 值时解释为什么是 0（如「当前无出网来源」） */
  note?: string
}

/** 指标卡：数值 + 悬浮「统计口径」说明，口径文案一律取接口 definitions */
export function MetricCard({ label, value, icon, textClass = '', definition, note }: Props) {
  return (
    <div className="panel group relative px-[12px] py-[10px]">
      <div className="flex items-center justify-between gap-[6px]">
        <SectionLabel>{label}</SectionLabel>
        <Icon name={icon} size={13} className="flex-none text-muted-foreground" />
      </div>
      <div className={`metric-value mt-[4px] ${textClass}`}>
        {value === undefined ? <span className="text-muted-foreground">—</span> : value.toLocaleString('zh-CN')}
      </div>
      <div className="mt-[2px] flex items-center gap-[4px] text-xs text-muted-foreground">
        <Icon name="Info" size={11} />
        {note && value === 0 ? note : '统计口径'}
      </div>
      <div className="pointer-events-none absolute inset-x-[8px] top-[calc(100%-6px)] z-30 hidden rounded-md border border-border bg-popover px-[10px] py-[8px] text-xs leading-[1.55] shadow-popover group-hover:block">
        <span className="text-sidebar-foreground">「{label}」统计口径：</span>
        {definition || '后端未返回该指标的口径说明'}
      </div>
    </div>
  )
}
