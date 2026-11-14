import type { ReactNode } from 'react'

export type BadgeTone = 'neutral' | 'primary' | 'success' | 'warning' | 'error'

interface BadgeProps {
  tone?: BadgeTone
  dot?: boolean
  children: ReactNode
  className?: string
  title?: string
}

/** 状态徽标：11px / 20px 高 / 仅真实状态色 */
export function Badge({ tone = 'neutral', dot = false, children, className = '', title }: BadgeProps) {
  return (
    <span className={`badge badge-${tone} ${className}`} title={title}>
      {dot && <i className="dot" />}
      {children}
    </span>
  )
}

export function Chip({ children, className = '', title }: { children: ReactNode; className?: string; title?: string }) {
  return (
    <span className={`chip ${className}`} title={title}>
      {children}
    </span>
  )
}
