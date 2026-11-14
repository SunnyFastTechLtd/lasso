import { create } from 'zustand'
import { STORAGE_KEYS } from '@/config/app'
import type { User } from '@/types/auth'

interface UserState {
  token: string | null
  refresh: string | null
  user: User | null
  setAuth: (token: string, refresh: string, user: User) => void
  setTokens: (token: string, refresh?: string) => void
  setUser: (user: User) => void
  logout: () => void
}

const readUser = (): User | null => {
  const raw = localStorage.getItem(STORAGE_KEYS.USER)
  if (!raw) return null
  try {
    return JSON.parse(raw) as User
  } catch {
    return null
  }
}

export const useUserStore = create<UserState>((set) => ({
  token: localStorage.getItem(STORAGE_KEYS.TOKEN),
  refresh: localStorage.getItem(STORAGE_KEYS.REFRESH),
  user: readUser(),

  setAuth: (token, refresh, user) => {
    localStorage.setItem(STORAGE_KEYS.TOKEN, token)
    if (refresh) localStorage.setItem(STORAGE_KEYS.REFRESH, refresh)
    localStorage.setItem(STORAGE_KEYS.USER, JSON.stringify(user))
    set({ token, refresh: refresh || localStorage.getItem(STORAGE_KEYS.REFRESH), user })
  },

  /** 刷新成功后只换 token；refresh 有值时必须覆盖（轮换后旧 refresh 已作废） */
  setTokens: (token, refresh) => {
    localStorage.setItem(STORAGE_KEYS.TOKEN, token)
    if (refresh) localStorage.setItem(STORAGE_KEYS.REFRESH, refresh)
    set({ token, refresh: refresh || localStorage.getItem(STORAGE_KEYS.REFRESH) })
  },

  setUser: (user) => {
    localStorage.setItem(STORAGE_KEYS.USER, JSON.stringify(user))
    set({ user })
  },

  logout: () => {
    localStorage.removeItem(STORAGE_KEYS.TOKEN)
    localStorage.removeItem(STORAGE_KEYS.REFRESH)
    localStorage.removeItem(STORAGE_KEYS.USER)
    set({ token: null, refresh: null, user: null })
  },
}))

export const isLoggedIn = () => Boolean(useUserStore.getState().token)
