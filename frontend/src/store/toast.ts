import { create } from 'zustand'

export type ToastTone = 'info' | 'success' | 'warning' | 'error'

export interface ToastItem {
  id: number
  tone: ToastTone
  message: string
}

interface ToastState {
  items: ToastItem[]
  push: (tone: ToastTone, message: string) => void
  dismiss: (id: number) => void
}

let seq = 0

export const useToastStore = create<ToastState>((set, get) => ({
  items: [],

  push: (tone, message) => {
    const id = ++seq
    set({ items: [...get().items, { id, tone, message }] })
    window.setTimeout(() => get().dismiss(id), tone === 'error' ? 6000 : 3200)
  },

  dismiss: (id) => set({ items: get().items.filter((item) => item.id !== id) }),
}))

const emit = (tone: ToastTone) => (message: string) => useToastStore.getState().push(tone, message)

export const toast = {
  info: emit('info'),
  success: emit('success'),
  warning: emit('warning'),
  error: emit('error'),
}
