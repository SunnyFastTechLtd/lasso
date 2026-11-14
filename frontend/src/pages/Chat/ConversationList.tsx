import { useState } from 'react'
import type { Conversation } from '@/types/chat'
import { Icon } from '@/components/ui/Icon'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/Overlay'
import { EmptyState } from '@/components/ui/EmptyState'
import { fmtRelative } from '@/utils/format'
import { convTitle } from './shared'

interface Props {
  items: Conversation[]
  total: number
  activeId: number | null
  loading: boolean
  onSelect: (id: number) => void
  onNew: () => void
  onDelete: (id: number) => Promise<unknown>
}

export function ConversationList({ items, total, activeId, loading, onSelect, onNew, onDelete }: Props) {
  const [confirmId, setConfirmId] = useState<number | null>(null)
  const [removing, setRemoving] = useState(false)

  const doDelete = async () => {
    if (confirmId === null) return
    setRemoving(true)
    try {
      await onDelete(confirmId)
      setConfirmId(null)
    } catch {
      /* 失败保持弹窗关闭后由上层 toast 提示，列表自动回滚 */
    } finally {
      setRemoving(false)
    }
  }

  return (
    <div className="panel flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2.5">
        <span className="text-xs font-semibold text-sidebar-foreground">会话</span>
        <span className="num text-xs text-muted-foreground">{total}</span>
        <Button className="ml-auto" quiet variant="primary" icon="Plus" onClick={onNew}>
          新建
        </Button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {loading ? (
          <div className="px-3 py-8 text-xs text-muted-foreground">加载中…</div>
        ) : items.length === 0 ? (
          <EmptyState title="还没有会话" description="点「新建」开始第一轮对话" />
        ) : (
          items.map((c) => (
            <div
              key={c.id}
              role="button"
              tabIndex={0}
              onClick={() => onSelect(c.id)}
              onKeyDown={(e) => e.key === 'Enter' && onSelect(c.id)}
              className={`group flex cursor-pointer items-start gap-2 rounded-md px-3 py-2.5 ${
                c.id === activeId ? 'bg-primary/12 text-primary' : 'hover:bg-muted/60'
              }`}
            >
              <Icon
                name="MessageSquare"
                size={14}
                className={`mt-[3px] flex-none ${c.id === activeId ? '' : 'text-muted-foreground'}`}
              />
              <div className="min-w-0 flex-1">
                <div className="truncate text-xs text-sidebar-foreground">{convTitle(c.title)}</div>
                <div className="mt-1 flex items-center gap-1.5 truncate text-xs text-muted-foreground">
                  <span className="max-w-[110px] truncate">{c.agent_name || '通用助手'}</span>
                  <span>·</span>
                  <span>{c.last_message_at ? fmtRelative(c.last_message_at) : fmtRelative(c.created_at)}</span>
                </div>
              </div>
              <button
                type="button"
                aria-label="删除会话"
                onClick={(e) => {
                  e.stopPropagation()
                  setConfirmId(c.id)
                }}
                className="mt-[1px] hidden flex-none text-muted-foreground hover:text-state-error group-hover:block"
              >
                <Icon name="Trash2" size={13} />
              </button>
            </div>
          ))
        )}
      </div>

      <ConfirmDialog
        open={confirmId !== null}
        tone="danger"
        title="删除会话"
        message="会话与全部消息将一并删除，不可恢复。确认继续？"
        confirmText="删除"
        loading={removing}
        onCancel={() => setConfirmId(null)}
        onConfirm={() => void doDelete()}
      />
    </div>
  )
}
