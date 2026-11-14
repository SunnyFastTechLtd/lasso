import type { ReactNode } from 'react'
import { Icon } from './Icon'

interface Column<T> {
  key: string
  title: ReactNode
  /** 编号/代码/URL 列使用等宽字体 */
  mono?: boolean
  /** 数字/时间列：无衬线 + 等宽数字 */
  tnum?: boolean
  align?: 'left' | 'right' | 'center'
  width?: number
  render?: (row: T, index: number) => ReactNode
  cellClassName?: string
  /** 次要列：≤1024px 收起（见 index.css 的 col-secondary），避免表格横向溢出 */
  secondary?: boolean
}

interface DataTableProps<T> {
  columns: Column<T>[]
  rows: T[]
  rowKey: (row: T, index: number) => string | number
  onRowClick?: (row: T) => void
  loading?: boolean
  empty?: ReactNode
  /** 附加到 table 上的类名（如 table-layout: fixed） */
  className?: string
}

/** 密集数据表：列定义即契约，缺省渲染 row[key] */
export function DataTable<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  loading = false,
  empty,
  className,
}: DataTableProps<T>) {
  const alignClass = (align?: Column<T>['align']) =>
    align === 'right' ? 'text-right' : align === 'center' ? 'text-center' : ''

  return (
    <table className={`data-table ${className ?? ''}`}>
      <thead>
        <tr>
          {columns.map((col) => (
            <th
              key={col.key}
              className={`${alignClass(col.align)} ${col.secondary ? 'col-secondary' : ''}`}
              style={col.width ? { width: col.width } : undefined}
            >
              {col.title}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {loading && (
          <tr>
            <td colSpan={columns.length}>
              <div className="flex items-center gap-[8px] py-[14px] text-muted-foreground">
                <Icon name="LoaderCircle" size={14} className="animate-spin" />
                <span className="text-xs">读取中…</span>
              </div>
            </td>
          </tr>
        )}
        {!loading && rows.length === 0 && (
          <tr>
            <td colSpan={columns.length}>{empty ?? <div className="empty-state">暂无数据</div>}</td>
          </tr>
        )}
        {!loading &&
          rows.map((row, index) => (
            <tr
              key={rowKey(row, index)}
              className={onRowClick ? 'cursor-pointer' : undefined}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
            >
              {columns.map((col) => (
                <td
                  key={col.key}
                  className={`${col.tnum && col.align === 'right' ? 'num-cell' : alignClass(col.align)} ${col.mono ? 'mono' : ''} ${col.tnum ? 'tnum' : ''} ${col.cellClassName ?? ''} ${col.secondary ? 'col-secondary' : ''}`}
                >
                  {col.render ? col.render(row, index) : String((row as Record<string, unknown>)[col.key] ?? '—')}
                </td>
              ))}
            </tr>
          ))}
      </tbody>
    </table>
  )
}
