import axios, { type AxiosInstance, type AxiosRequestConfig, type InternalAxiosRequestConfig } from 'axios'
import { APP_CONFIG, STORAGE_KEYS } from '@/config/app'
import { useUserStore } from '@/store/user'
import { refreshAccessToken, resetSession } from '@/api/token'
import type { ApiErrorBody } from '@/types/common'

/**
 * 统一错误对象。
 * 后端字段级 ValidationError 会被 exceptions 层抹平成 repr 字符串，
 * 因此这里保留原始 response.data，页面可就地读取字段级信息。
 */
export class ApiError extends Error {
  status: number
  code: number | string | null
  detail: string
  body: ApiErrorBody | null

  constructor(status: number, detail: string, code: number | string | null, body: ApiErrorBody | null) {
    super(detail)
    this.name = 'ApiError'
    this.detail = detail
    this.status = status
    this.code = code
    this.body = body
  }
}

type RetriableConfig = InternalAxiosRequestConfig & { _retry?: boolean }

/** 匿名入口：登录失败与刷新失败本身就是 401，不能再触发刷新或清会话 */
const ANONYMOUS_URLS = ['/auth/login/', '/auth/refresh/']

const isAnonymousEntry = (url = '') => ANONYMOUS_URLS.some((path) => url.includes(path))

const request: AxiosInstance = axios.create({
  baseURL: APP_CONFIG.apiBaseUrl,
  timeout: 60000,
  headers: { 'Content-Type': 'application/json' },
})

request.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  // 匿名入口（登录/刷新）不带 Authorization：残留的过期 token 会让 SimpleJWT
  // 在视图执行前直接 401，导致用户明明口令正确也无法登录
  if (isAnonymousEntry(config.url)) return config
  const token = useUserStore.getState().token ?? localStorage.getItem(STORAGE_KEYS.TOKEN)
  if (token && config.headers) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

request.interceptors.response.use(
  (response) => response.data,
  async (error) => {
    const status: number = error.response?.status ?? 0
    const body: ApiErrorBody | null = error.response?.data ?? null
    const original = error.config as RetriableConfig | undefined

    // access 过期：单飞刷新一次后重放原请求；刷新失败或重放仍 401 才清会话
    if (status === 401 && original && !isAnonymousEntry(original.url)) {
      if (!original._retry) {
        original._retry = true
        try {
          await refreshAccessToken()
          return request(original)
        } catch {
          resetSession()
        }
      } else {
        resetSession()
      }
    }

    const detail =
      body?.detail ||
      (error.response?.data ? '请求失败，请检查填写内容' : '') ||
      error.message ||
      '请求失败，请稍后重试'

    return Promise.reject(new ApiError(status, String(detail), body?.code ?? null, body))
  },
)

export const http = {
  get: <T = unknown>(url: string, params?: Record<string, unknown>, config?: AxiosRequestConfig) =>
    request.get(url, { params, ...config }) as unknown as Promise<T>,

  post: <T = unknown>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
    request.post(url, data, config) as unknown as Promise<T>,

  put: <T = unknown>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
    request.put(url, data, config) as unknown as Promise<T>,

  patch: <T = unknown>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
    request.patch(url, data, config) as unknown as Promise<T>,

  delete: <T = unknown>(url: string, config?: AxiosRequestConfig) =>
    request.delete(url, config) as unknown as Promise<T>,

  upload: <T = unknown>(url: string, formData: FormData, config?: AxiosRequestConfig) =>
    request.post(url, formData, {
      timeout: 5 * 60 * 1000,
      headers: { 'Content-Type': 'multipart/form-data' },
      ...config,
    }) as unknown as Promise<T>,
}

export default request
