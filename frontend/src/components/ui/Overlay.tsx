import { useEffect, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Button, IconButton } from './Button'
import { Icon } from './Icon'

/** 浮层通用副作用：Esc 关闭 + 滚动锁 */
function useOverlay(open: boolean, onClose: () => void) {
  useEffect(() => {
    if (!open) return
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => {
      document.body.style.overflow = prev
      window.removeEventListener('keydown', onKey)
    }
  }, [open, onClose])
}

interface ModalProps {
  open: boolean
  title: ReactNode
  icon?: string
  onClose: () => void
  footer?: ReactNode
  wide?: boolean
  /** 超宽弹窗（如编辑页右侧试聊面板并排），优先于 wide */
  xwide?: boolean
  children: ReactNode
}

export function Modal({ open, title, icon, onClose, footer, wide = false, xwide = false, children }: ModalProps) {
  useOverlay(open, onClose)
  if (!open) return null

  return createPortal(
    <div className="scrim scrim-center" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${xwide ? 'modal-xwide' : wide ? 'modal-wide' : ''}`} role="dialog" aria-modal="true">
        <header className="modal-head">
          {icon && <Icon name={icon} size={15} className="flex-none text-primary" />}
          <span className="modal-title min-w-0 truncate">{title}</span>
          <IconButton icon="X" label="关闭" className="ml-auto" onClick={onClose} />
        </header>
        <div className="modal-body">{children}</div>
        {footer && <footer className="modal-foot">{footer}</footer>}
      </div>
    </div>,
    document.body,
  )
}

interface DrawerProps {
  open: boolean
  title: ReactNode
  meta?: ReactNode
  onClose: () => void
  footer?: ReactNode
  children: ReactNode
}

/** 右侧明细抽屉，宽度取 --inspector-width */
export function Drawer({ open, title, meta, onClose, footer, children }: DrawerProps) {
  useOverlay(open, onClose)
  if (!open) return null

  return createPortal(
    <div className="scrim scrim-right" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <aside className="drawer" role="dialog" aria-modal="true">
        <header className="modal-head">
          <span className="flex min-w-0 flex-col leading-tight">
            <span className="modal-title truncate">{title}</span>
            {meta && <span className="text-xs text-muted-foreground">{meta}</span>}
          </span>
          <IconButton icon="X" label="关闭" className="ml-auto" onClick={onClose} />
        </header>
        <div className="modal-body flex-1">{children}</div>
        {footer && <footer className="modal-foot rounded-none">{footer}</footer>}
      </aside>
    </div>,
    document.body,
  )
}

interface ConfirmProps {
  open: boolean
  title: ReactNode
  message?: ReactNode
  confirmText?: string
  cancelText?: string
  tone?: 'default' | 'danger'
  loading?: boolean
  onConfirm: () => void
  onCancel: () => void
  children?: ReactNode
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmText = '确认',
  cancelText = '取消',
  tone = 'default',
  loading = false,
  onConfirm,
  onCancel,
  children,
}: ConfirmProps) {
  return (
    <Modal
      open={open}
      title={title}
      icon={tone === 'danger' ? 'TriangleAlert' : 'Info'}
      onClose={loading ? () => undefined : onCancel}
      footer={
        <>
          <Button variant="ghost" onClick={onCancel} disabled={loading}>
            {cancelText}
          </Button>
          <Button variant={tone === 'danger' ? 'danger' : 'primary'} onClick={onConfirm} loading={loading}>
            {confirmText}
          </Button>
        </>
      }
    >
      {message && <p className="text-sm leading-[1.6] text-muted-foreground">{message}</p>}
      {children}
    </Modal>
  )
}
