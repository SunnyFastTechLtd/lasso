import { create } from 'zustand'
import { STORAGE_KEYS } from '@/config/app'

interface UiState {
  railCollapsed: boolean
  toggleRail: () => void
  setRailCollapsed: (v: boolean) => void
}

export const useUiStore = create<UiState>((set, get) => ({
  railCollapsed: localStorage.getItem(STORAGE_KEYS.RAIL) === 'true',

  toggleRail: () => {
    const next = !get().railCollapsed
    localStorage.setItem(STORAGE_KEYS.RAIL, String(next))
    set({ railCollapsed: next })
  },

  setRailCollapsed: (v) => {
    localStorage.setItem(STORAGE_KEYS.RAIL, String(v))
    set({ railCollapsed: v })
  },
}))
