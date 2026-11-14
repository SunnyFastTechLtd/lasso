import type { ReactNode } from 'react'
import { Icon } from './Icon'

interface PanelProps {
  title?: ReactNode
  icon?: string
  meta?: ReactNode
  actions?: ReactNode
  footer?: ReactNode
  className?: string
  bodyClassName?: string
  padded?: boolean
  children?: ReactNode
}

/** 面板 = 本系统唯一的卡片原型（对齐设计稿 .panel 度量） */
export function Panel({
  title,
  icon,
  meta,
  actions,
  footer,
  className = '',
  bodyClassName = '',
  padded = false,
  children,
}: PanelProps) {
  const hasHeader = title !== undefined || actions !== undefined || meta !== undefined
  return (
    <section className={`panel ${className}`}>
      {hasHeader && (
        <header className="panel-header">
          <div className="panel-title">
            {icon && <Icon name={icon} size={14} />}
            <span className="truncate">{title}</span>
            {meta && <span className="panel-meta">{meta}</span>}
          </div>
          {actions && <div className="flex flex-none items-center gap-[6px]">{actions}</div>}
        </header>
      )}
      <div className={`panel-body ${padded ? 'panel-body-padded' : ''} ${bodyClassName}`}>{children}</div>
      {footer && <footer className="panel-foot">{footer}</footer>}
    </section>
  )
}

export function SectionLabel({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={`section-label ${className}`}>{children}</div>
}
