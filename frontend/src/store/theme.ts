import { create } from 'zustand'
import { STORAGE_KEYS } from '@/config/app'

export type ThemeMode = 'dark' | 'light' | 'system'
export type ResolvedTheme = 'dark' | 'light'
export type SkinKey = 'graphite' | 'purple' | 'navy' | 'green' | 'bluegold'

export interface SkinOption {
  value: SkinKey
  label: string
  note: string
  /** 设置页色卡预览：主色 + 背景 + 卡片（取深色态，保证辨识度） */
  preview: { primary: string; background: string; card: string; accent: string }
}

export const SKINS: SkinOption[] = [
  {
    value: 'graphite',
    label: '石墨',
    note: '默认 · 冷静中性',
    preview: { primary: '#93a5b8', background: '#16181b', card: '#202327', accent: '#93a3b3' },
  },
  {
    value: 'purple',
    label: '霓虹紫',
    note: '高对比 · 科技感',
    preview: { primary: '#a5b4fc', background: '#17161f', card: '#201f2b', accent: '#c084fc' },
  },
  {
    value: 'navy',
    label: '藏青政务',
    note: '稳重 · 政务蓝',
    preview: { primary: '#8ab4e8', background: '#131a23', card: '#1b2430', accent: '#5fa8d3' },
  },
  {
    value: 'green',
    label: '墨绿',
    note: '低饱和 · 长时阅读',
    preview: { primary: '#5eead4', background: '#121a18', card: '#1a2421', accent: '#2dd4bf' },
  },
  {
    value: 'bluegold',
    label: '藏蓝描金',
    note: '深蓝底 · 金色强调',
    preview: { primary: '#8fb0d4', background: '#101826', card: '#182333', accent: '#d9b45e' },
  },
]

const SKIN_KEYS = new Set<string>(SKINS.map((s) => s.value))

export const normalizeSkin = (raw: string | null | undefined): SkinKey =>
  raw && SKIN_KEYS.has(raw) ? (raw as SkinKey) : 'graphite'

const media = () => window.matchMedia('(prefers-color-scheme: dark)')

const resolve = (mode: ThemeMode): ResolvedTheme =>
  mode === 'system' ? (media().matches ? 'dark' : 'light') : mode

/** 登录页固定浅色石墨，不跟随用户偏好（偏好仍原样保留在 localStorage） */
export const isLoginPath = (pathname: string) =>
  pathname === '/login' || pathname.startsWith('/login/')

/**
 * 落到 html 上：class 驱动明暗、data-skin 驱动皮肤，二者正交。
 * login=true 时强制浅色 + 石墨，仅改 DOM，不写入任何存储。
 */
export function applyTheme(mode: ThemeMode, skin: SkinKey, login = false): ResolvedTheme {
  const resolved: ResolvedTheme = login ? 'light' : resolve(mode)
  const root = document.documentElement
  root.classList.toggle('light', resolved === 'light')
  root.classList.toggle('dark', resolved === 'dark')
  root.dataset.skin = login ? 'graphite' : skin
  // color-scheme 交给 CSS（:root / html.light / @media print），避免内联样式压过打印降级
  document
    .querySelector('meta[name="color-scheme"]')
    ?.setAttribute('content', resolved)
  return resolved
}

const readMode = (): ThemeMode => {
  const raw = localStorage.getItem(STORAGE_KEYS.THEME)
  return raw === 'light' || raw === 'system' ? raw : 'dark'
}

const readSkin = (): SkinKey => normalizeSkin(localStorage.getItem(STORAGE_KEYS.SKIN))

interface ThemeState {
  mode: ThemeMode
  resolved: ResolvedTheme
  skin: SkinKey
  setMode: (mode: ThemeMode) => void
  setSkin: (skin: SkinKey) => void
}

const initialMode = readMode()
const initialSkin = readSkin()

export const useThemeStore = create<ThemeState>((set, get) => ({
  mode: initialMode,
  resolved: resolve(initialMode),
  skin: initialSkin,

  setMode: (mode) => {
    localStorage.setItem(STORAGE_KEYS.THEME, mode)
    set({ mode, resolved: applyTheme(mode, get().skin) })
  },

  setSkin: (skin) => {
    localStorage.setItem(STORAGE_KEYS.SKIN, skin)
    applyTheme(get().mode, skin)
    set({ skin })
  },
}))

/** 应用启动时对齐一次（首帧已由 index.html 内联脚本处理），并监听系统主题 */
export function initTheme() {
  const { mode, skin } = useThemeStore.getState()
  applyTheme(mode, skin, isLoginPath(window.location.pathname))
  media().addEventListener('change', () => {
    const state = useThemeStore.getState()
    if (state.mode === 'system') {
      const login = isLoginPath(window.location.pathname)
      useThemeStore.setState({ resolved: applyTheme(state.mode, state.skin, login) })
    }
  })
}
