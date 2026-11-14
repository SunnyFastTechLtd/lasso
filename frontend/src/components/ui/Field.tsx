import type { InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from 'react'

interface FieldProps {
  label?: ReactNode
  hint?: ReactNode
  error?: ReactNode
  required?: boolean
  className?: string
  children: ReactNode
}

/** 表单字段容器：标签 + 控件 + 提示/错误 */
export function Field({ label, hint, error, required, className = '', children }: FieldProps) {
  return (
    <div className={`min-w-0 ${className}`}>
      {label && (
        <label className="field-label">
          {label}
          {required && <span className="text-state-error"> *</span>}
        </label>
      )}
      {children}
      {error ? <div className="field-error">{error}</div> : hint ? <div className="field-hint">{hint}</div> : null}
    </div>
  )
}

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  invalid?: boolean
}

export function Input({ invalid = false, className = '', ...rest }: InputProps) {
  return <input className={`field ${className}`} aria-invalid={invalid || undefined} {...rest} />
}

interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  invalid?: boolean
}

export function Textarea({ invalid = false, className = '', ...rest }: TextareaProps) {
  return <textarea className={`field ${className}`} aria-invalid={invalid || undefined} {...rest} />
}

interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  invalid?: boolean
}

export function Select({ invalid = false, className = '', children, ...rest }: SelectProps) {
  return (
    <select className={`field ${className}`} aria-invalid={invalid || undefined} {...rest}>
      {children}
    </select>
  )
}
