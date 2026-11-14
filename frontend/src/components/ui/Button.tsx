import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { Icon } from './Icon'

export type ButtonVariant = 'default' | 'primary' | 'ghost' | 'danger'

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  /** 24px 高的紧凑按钮 */
  quiet?: boolean
  icon?: string
  loading?: boolean
  children?: ReactNode
}

export function Button({
  variant = 'default',
  quiet = false,
  icon,
  loading = false,
  children,
  className = '',
  disabled,
  type = 'button',
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      className={`btn btn-${variant} ${quiet ? 'btn-quiet' : ''} ${className}`}
      {...rest}
    >
      {loading ? <Icon name="LoaderCircle" size={14} className="animate-spin" /> : icon && <Icon name={icon} size={14} />}
      {children}
    </button>
  )
}

interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  icon: string
  /** 无障碍与悬浮说明文案 */
  label: string
  active?: boolean
}

export function IconButton({ icon, label, active = false, className = '', ...rest }: IconButtonProps) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      className={`icon-btn ${active ? 'text-primary' : ''} ${className}`}
      {...rest}
    >
      <Icon name={icon} size={14} />
    </button>
  )
}
