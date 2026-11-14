import { useToastStore, type ToastTone } from '@/store/toast'
import { Icon } from './Icon'

const toneIcon: Record<ToastTone, string> = {
  info: 'Info',
  success: 'Check',
  warning: 'AlertTriangle',
  error: 'CircleAlert',
}

export function Toaster() {
  const items = useToastStore((s) => s.items)
  const dismiss = useToastStore((s) => s.dismiss)

  if (items.length === 0) return null

  return (
    <div className="toast-stack">
      {items.map((item) => (
        <div key={item.id} className="toast" data-tone={item.tone} role="status">
          <Icon name={toneIcon[item.tone]} size={14} className="mt-[1px] flex-none" />
          <span className="min-w-0 flex-1">{item.message}</span>
          <button
            type="button"
            aria-label="关闭提示"
            className="flex-none text-muted-foreground hover:text-foreground"
            onClick={() => dismiss(item.id)}
          >
            <Icon name="X" size={13} />
          </button>
        </div>
      ))}
    </div>
  )
}
