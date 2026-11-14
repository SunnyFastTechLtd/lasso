import { Seg } from './Seg'
import { IconButton } from './Button'

interface PaginationProps {
  page: number
  totalPages: number
  count: number
  pageSize: number
  onPageChange: (page: number) => void
  onPageSizeChange?: (size: number) => void
  className?: string
}

/** 后端分页固定 5 键，无 next/previous，只能自行算总页数 */
export function Pagination({
  page,
  totalPages,
  count,
  pageSize,
  onPageChange,
  onPageSizeChange,
  className = '',
}: PaginationProps) {
  const safeTotal = Math.max(totalPages, 1)
  const sizeItems = [10, 20, 50, 100].map((v) => ({ value: String(v), label: `${v} 行` }))

  return (
    <div className={`panel-foot ${className}`}>
      <div className="flex min-w-0 items-center gap-[8px]">
        <span>
          共 <span className="tnum text-sidebar-foreground">{count}</span> 条 · 第{' '}
          <span className="tnum text-sidebar-foreground">
            {page}/{safeTotal}
          </span>{' '}
          页
        </span>
        {onPageSizeChange && (
          <Seg
            items={sizeItems}
            value={String(pageSize)}
            onChange={(v) => onPageSizeChange(Number(v))}
            className="ml-[6px]"
          />
        )}
      </div>
      <div className="flex flex-none items-center gap-[6px]">
        <IconButton icon="ChevronsLeft" label="首页" disabled={page <= 1} onClick={() => onPageChange(1)} />
        <IconButton icon="ChevronLeft" label="上一页" disabled={page <= 1} onClick={() => onPageChange(page - 1)} />
        <IconButton
          icon="ChevronRight"
          label="下一页"
          disabled={page >= safeTotal}
          onClick={() => onPageChange(page + 1)}
        />
        <IconButton
          icon="ChevronsRight"
          label="末页"
          disabled={page >= safeTotal}
          onClick={() => onPageChange(safeTotal)}
        />
      </div>
    </div>
  )
}
