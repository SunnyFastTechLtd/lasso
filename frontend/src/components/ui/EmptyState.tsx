import type { ReactNode } from 'react'
import { Icon } from './Icon'

interface EmptyStateProps {
  icon?: string
  title: ReactNode
  description?: ReactNode
  action?: ReactNode
  compact?: boolean
}

export function EmptyState({ icon = 'Inbox', title, description, action, compact = false }: EmptyStateProps) {
  return (
    <div className={`empty-state ${compact ? 'py-[22px]' : ''}`}>
      <Icon name={icon} size={compact ? 18 : 22} />
      <div className="text-sm text-sidebar-foreground">{title}</div>
      {description && <div className="max-w-[420px] text-xs leading-[1.5]">{description}</div>}
      {action && <div className="mt-[6px]">{action}</div>}
    </div>
  )
}
