import type { ReactNode } from 'react'

export interface SegItem<V extends string = string> {
  value: V
  label: ReactNode
}

interface SegProps<V extends string> {
  items: SegItem<V>[]
  value: V
  onChange: (next: V) => void
  className?: string
}

/** 分段筛选：状态由 data-active 驱动；数量只在统计卡上展示，筛选段不重复计数 */
export function Seg<V extends string>({ items, value, onChange, className = '' }: SegProps<V>) {
  return (
    <div className={`seg ${className}`} role="tablist">
      {items.map((item) => (
        <button
          key={item.value}
          type="button"
          role="tab"
          aria-selected={item.value === value}
          className="seg-item"
          data-active={String(item.value === value)}
          onClick={() => onChange(item.value)}
        >
          {item.label}
        </button>
      ))}
    </div>
  )
}
