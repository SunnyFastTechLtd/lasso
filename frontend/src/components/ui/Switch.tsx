import type { ReactNode } from 'react'

interface SwitchProps {
  checked: boolean
  onChange: (next: boolean) => void
  disabled?: boolean
  label?: ReactNode
  hint?: ReactNode
}

/** 开关：状态由 data-on 驱动（对齐设计稿 .switch） */
export function Switch({ checked, onChange, disabled = false, label, hint }: SwitchProps) {
  const control = (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={typeof label === 'string' ? label : undefined}
      disabled={disabled}
      className="switch"
      data-on={String(checked)}
      onClick={() => onChange(!checked)}
    />
  )

  if (!label && !hint) return control

  return (
    <label className="flex cursor-pointer items-center gap-[10px]">
      {control}
      <span className="flex min-w-0 flex-col gap-[2px]">
        {label && <span className="text-sm text-foreground">{label}</span>}
        {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
      </span>
    </label>
  )
}
