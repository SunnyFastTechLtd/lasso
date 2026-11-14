import axios from 'axios'
import { APP_CONFIG, STORAGE_KEYS } from '@/config/app'
import { useUserStore } from '@/store/user'
import type { RefreshResult } from '@/types/auth'

/**
 * 独立的 axios 实例，刻意不挂 request.ts 的拦截器：
 * 刷新失败本身也是 401，走共享实例会再次触发「401 → 刷新」逻辑，形成递归。
 */
const refreshClient = axios.create({
  baseURL: APP_CONFIG.apiBaseUrl,
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
})

/* ------------------------------------------------------------------ *
 * 跨标签协调
 *
 * 同一 refresh 在 ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION 下只能换一次，
 * 两个标签各自刷新必然有一个 401 并被踢下线，因此必须选出一个「刷新主」。
 * 主通道用 BroadcastChannel 分发开始/成功/失败事件；不可用时回落到 localStorage
 * 的 storage 事件。共享 localStorage 同时充当最终事实来源：即使广播全部丢失，
 * 等待方也会轮询到新 token 或失败标记，不会永久停留在等待态。
 * ------------------------------------------------------------------ */
const AUTH_CHANNEL_NAME = 'lasso-auth'
const AUTH_EVENT_KEY = 'lasso_auth_event'
const REFRESH_LOCK_KEY = 'lasso_refresh_lock'
const REFRESH_FAIL_KEY = 'lasso_refresh_fail_at'

/** 锁 TTL 略大于刷新请求超时（15s），避免请求还在途时锁先过期 */
const LOCK_TTL_MS = 17_000
const WAIT_TIMEOUT_MS = 20_000
const WAIT_POLL_MS = 150
const SETTLE_MS = 80

const TAB_ID = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`

type AuthEvent =
  | { type: 'refresh-start'; tab: string; at: number }
  | { type: 'refresh-success'; tab: string; at: number; access: string; refresh: string }
  | { type: 'refresh-failure'; tab: string; at: number }

interface RefreshLock {
  owner: string
  tab: string
  expiresAt: number
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

let channel: BroadcastChannel | null = null
try {
  if (typeof BroadcastChannel !== 'undefined') {
    channel = new BroadcastChannel(AUTH_CHANNEL_NAME)
  }
} catch {
  channel = null
}

/** 最近一次观察到其它标签开始刷新的时间，用于抢锁后的二次确认 */
let lastLeaderStartAt = 0

function publish(event: AuthEvent) {
  if (channel) {
    channel.postMessage(event)
    return
  }
  try {
    localStorage.setItem(AUTH_EVENT_KEY, JSON.stringify(event))
  } catch {
    // 广播不可用时，等待方仍会通过 localStorage 的 token / 失败标记兜底
  }
}

/** 清会话并回登录页；token.ts 不反向依赖 request.ts，避免循环引用 */
export function resetSession() {
  useUserStore.getState().logout()
  if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
    window.location.href = '/login'
  }
}

function markFailure() {
  try {
    localStorage.setItem(REFRESH_FAIL_KEY, String(Date.now()))
  } catch {
    // 存不进去时至少还有广播与超时兜底
  }
}

function readFailureAt() {
  try {
    return Number(localStorage.getItem(REFRESH_FAIL_KEY)) || 0
  } catch {
    return 0
  }
}

function readStoredTokens() {
  try {
    return {
      access: localStorage.getItem(STORAGE_KEYS.TOKEN),
      refresh: localStorage.getItem(STORAGE_KEYS.REFRESH),
    }
  } catch {
    return { access: null, refresh: null }
  }
}

/** 其它标签已完成轮换：直接采用共享存储里的新 token，不再自己发刷新 */
function adoptStoredIfChanged(myRefresh: string | null): string | null {
  const stored = readStoredTokens()
  if (stored.access && stored.refresh && stored.refresh !== myRefresh) {
    useUserStore.getState().setTokens(stored.access, stored.refresh)
    return stored.access
  }
  return null
}

function readLock(): RefreshLock | null {
  try {
    const raw = localStorage.getItem(REFRESH_LOCK_KEY)
    if (!raw) return null
    const lock = JSON.parse(raw) as RefreshLock
    if (!lock?.owner || !lock?.expiresAt || lock.expiresAt <= Date.now()) {
      return null
    }
    return lock
  } catch {
    return null
  }
}

function releaseLock() {
  const lock = readLock()
  if (lock && lock.tab === TAB_ID) {
    try {
      localStorage.removeItem(REFRESH_LOCK_KEY)
    } catch {
      // 忽略：锁到期会自然失效
    }
  }
}

function handleRemote(event: AuthEvent) {
  if (!event || event.tab === TAB_ID) return

  if (event.type === 'refresh-start') {
    lastLeaderStartAt = Math.max(lastLeaderStartAt, event.at || 0)
    return
  }

  if (event.type === 'refresh-success') {
    // 已登出的标签不因其它标签刷新而被重新拉回登录态
    if (!useUserStore.getState().refresh) return
    if (event.access && event.refresh) {
      useUserStore.getState().setTokens(event.access, event.refresh)
    }
    try {
      localStorage.removeItem(REFRESH_FAIL_KEY)
    } catch {
      // 忽略
    }
    return
  }

  if (event.type === 'refresh-failure') {
    if (useUserStore.getState().refresh) resetSession()
  }
}

if (channel) {
  channel.onmessage = (event: MessageEvent<AuthEvent>) => handleRemote(event.data)
} else if (typeof window !== 'undefined') {
  window.addEventListener('storage', (event: StorageEvent) => {
    if (event.key !== AUTH_EVENT_KEY || !event.newValue) return
    try {
      handleRemote(JSON.parse(event.newValue) as AuthEvent)
    } catch {
      // 忽略非法载荷
    }
  })
}

const currentRefreshToken = () =>
  useUserStore.getState().refresh ?? localStorage.getItem(STORAGE_KEYS.REFRESH)

/** 当前标签内的在途刷新；同标签并发 401 共享同一个 Promise */
let inflightRefresh: Promise<string> | null = null

async function requestRefresh(refreshToken: string): Promise<{ access: string; refresh: string }> {
  const { data } = await refreshClient.post<RefreshResult>('/auth/refresh/', {
    refresh: refreshToken,
  })
  // 轮换开启后后端会返回新 refresh，必须覆盖写回，否则第二次刷新必失败。
  // 后端若未返回（关闭轮换），沿用旧 refresh。
  const nextRefresh = data.refresh || refreshToken
  useUserStore.getState().setTokens(data.access, nextRefresh)
  return { access: data.access, refresh: nextRefresh }
}

/**
 * 抢占跨标签刷新锁。
 *
 * localStorage 没有 compare-and-set，所以用「写候选锁 → settle 窗口 → 回读校验」：
 * 1. 先看是否已有未过期的他人锁，有则直接让位；
 * 2. 写入带唯一 owner 的候选锁，等待一个 80~160ms 的 settle 窗口，让并发的
 *    竞争者互相覆盖、彼此可见；
 * 3. 回读锁内容：只有 owner 仍是自己才继续。若已被覆盖，说明另一个标签最后
 *    写入并成为唯一候选，本方转为等待；
 * 4. 再检查 settle 期间是否收到他人的 refresh-start 广播，收到则主动让位。
 *
 * 极端时序（A 校验通过后、B 才覆盖锁）由 B 的 settle 窗口兜住：A 在请求前
 * 广播 refresh-start，B 醒来后因 lastLeaderStartAt 晚于自己的抢锁起点而让位。
 * 若广播本身丢失导致仍重复刷新，performRefresh 的「401 后先采用共享存储里的
 * 新 token」逻辑会自愈，不会把用户踢下线。
 */
async function claimLeadership(): Promise<boolean> {
  const startedAt = Date.now()
  const existing = readLock()
  if (existing && existing.tab !== TAB_ID) return false

  const owner = `${TAB_ID}:${startedAt}:${Math.random().toString(36).slice(2)}`
  const lock: RefreshLock = {
    owner,
    tab: TAB_ID,
    expiresAt: startedAt + LOCK_TTL_MS,
  }
  try {
    localStorage.setItem(REFRESH_LOCK_KEY, JSON.stringify(lock))
  } catch {
    return false
  }

  await sleep(SETTLE_MS + Math.random() * 80)

  const confirmed = readLock()
  if (!confirmed || confirmed.owner !== owner) return false
  if (lastLeaderStartAt > startedAt) return false

  publish({ type: 'refresh-start', tab: TAB_ID, at: Date.now() })
  return true
}

async function performRefresh(myRefresh: string): Promise<string> {
  try {
    const data = await requestRefresh(myRefresh)
    try {
      localStorage.removeItem(REFRESH_FAIL_KEY)
    } catch {
      // 忽略
    }
    publish({
      type: 'refresh-success',
      tab: TAB_ID,
      at: Date.now(),
      access: data.access,
      refresh: data.refresh,
    })
    return data.access
  } catch (error) {
    // 极小概率的抢锁竞态：另一个标签已经轮换成功，这里不能再把自己踢下线
    const adopted = adoptStoredIfChanged(myRefresh)
    if (adopted) return adopted

    const status = (error as { response?: { status?: number } })?.response?.status
    if (status === 401 || status === 403) {
      markFailure()
      publish({ type: 'refresh-failure', tab: TAB_ID, at: Date.now() })
      resetSession()
    }
    throw error
  } finally {
    releaseLock()
  }
}

async function waitForLeader(myRefresh: string | null, isRetry: boolean): Promise<string> {
  const startedAt = Date.now()
  const deadline = startedAt + WAIT_TIMEOUT_MS

  while (Date.now() < deadline) {
    const adopted = adoptStoredIfChanged(myRefresh)
    if (adopted) return adopted
    if (readFailureAt() >= startedAt) {
      resetSession()
      throw new Error('其它标签刷新失败，已下线')
    }
    if (!readLock()) {
      // 持锁标签已关闭或锁已过期：兜底自己再竞争一次，避免永远等待
      if (isRetry) break
      return refreshWithCoordination(true)
    }
    await sleep(WAIT_POLL_MS)
  }

  const adopted = adoptStoredIfChanged(myRefresh)
  if (adopted) return adopted
  throw new Error('等待其它标签刷新超时')
}

async function refreshWithCoordination(isRetry = false): Promise<string> {
  const myRefresh = currentRefreshToken()
  if (!myRefresh) {
    resetSession()
    throw new Error('no refresh token')
  }

  const adopted = adoptStoredIfChanged(myRefresh)
  if (adopted) return adopted

  if (readLock()) {
    return waitForLeader(myRefresh, isRetry)
  }

  const isLeader = await claimLeadership()
  if (!isLeader) {
    return waitForLeader(myRefresh, isRetry)
  }
  return performRefresh(myRefresh)
}

/**
 * 单飞刷新 access token（标签内 + 跨标签）。
 *
 * - 标签内：并发的 401 共享同一个 inflightRefresh Promise。
 * - 跨标签：通过 localStorage 锁选出唯一刷新主，其余标签等待其广播/共享 token。
 * - 失败：刷新主广播 refresh-failure 并写失败标记，所有在线标签一起清会话回登录页。
 * - 兜底：广播丢失时等待方轮询共享存储；持锁标签关闭或锁过期后，等待方会重新
 *   抢锁；最长等待 WAIT_TIMEOUT_MS 后抛错，由调用方清会话，不会永久卡住。
 */
export function refreshAccessToken(): Promise<string> {
  if (!inflightRefresh) {
    inflightRefresh = refreshWithCoordination().finally(() => {
      inflightRefresh = null
    })
  }
  return inflightRefresh
}
