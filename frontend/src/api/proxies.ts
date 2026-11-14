import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { http } from './request'

/**
 * 网络代理（平台托管 SearXNG 配置 + 天工抓取按域名/来源站分流）。
 * 代理地址与认证信息属基础设施信息，读写均仅平台管理员。
 */
export const proxyKeys = {
  state: ['settings', 'proxies'] as const,
}

export type ProxyScheme = 'http' | 'socks5'
export type ProxyTestStatus = 'untested' | 'ok' | 'failed'

export interface ProxyEndpoint {
  id: number
  name: string
  scheme: ProxyScheme
  host: string
  port: number
  username: string
  /** 密码只回布尔值，明文不回流前端 */
  has_password: boolean
  /** 展示用地址：有认证时只露用户名 */
  display_url: string
  enabled: boolean
  note: string
  last_test_at: string | null
  last_test_status: ProxyTestStatus
  last_test_ms: number | null
  last_test_error: string
  updated_at: string
}

export interface ProxyTestResult {
  ok: boolean
  status: 'ok' | 'failed'
  elapsed_ms: number
  /** timeout / refused / auth_failed / tls_error / http_error / network_error / proxy_error */
  reason: string
  message: string
  endpoint: ProxyEndpoint
}

export interface ProxyApplyResult {
  applied: boolean
  changed?: boolean
  error?: string
  detail: string
}

export interface EngineBinding {
  name: string
  proxy_id: number | null
  /** 最近一次应用是否写入线上配置；失败时为 false，界面标「未生效」 */
  applied: boolean
}

export interface TiangongBinding {
  kind: 'domain' | 'source'
  key: string
  proxy_id: number
  applied: boolean
}

export interface ProxySource {
  id: number
  name: string
  domain: string
}

export interface ApplyState {
  status?: 'ok' | 'failed'
  error?: string
  detail?: string
  at?: string
}

export interface ProxyState {
  proxies: ProxyEndpoint[]
  engines: EngineBinding[]
  tiangong: TiangongBinding[]
  /** 来源库（L4）落地前为空数组，界面据此隐藏来源站选择 */
  sources: ProxySource[]
  apply: ApplyState
}

export interface ProxyPayload {
  name: string
  scheme: ProxyScheme
  host: string
  port: number
  username?: string
  /** 新增时为空表示无认证；编辑时省略/留空表示不修改 */
  password?: string
  enabled: boolean
  note?: string
}

export interface BindingPayload {
  engines: Record<string, number | null>
  tiangong: { kind: 'domain' | 'source'; key: string; proxy_id: number }[]
}

export const proxyApi = {
  state: () => http.get<ProxyState>('/system/settings/proxy-bindings/'),
  create: (payload: ProxyPayload) =>
    http.post<ProxyEndpoint & { apply: ProxyApplyResult }>('/system/settings/proxies/', payload),
  update: (id: number, payload: Partial<ProxyPayload>) =>
    http.patch<ProxyEndpoint & { apply: ProxyApplyResult }>(`/system/settings/proxies/${id}/`, payload),
  remove: (id: number) =>
    http.delete<{ deleted: boolean; apply: ProxyApplyResult }>(`/system/settings/proxies/${id}/`),
  test: (id: number) => http.post<ProxyTestResult>(`/system/settings/proxies/${id}/test/`),
  saveBindings: (payload: BindingPayload) =>
    http.put<{ saved: boolean; apply: ProxyApplyResult; state: ProxyState }>(
      '/system/settings/proxy-bindings/',
      payload,
    ),
  apply: () =>
    http.post<{ apply: ProxyApplyResult; state: ProxyState }>('/system/settings/proxies/apply/'),
}

export function useProxyState(enabled = true) {
  return useQuery({
    queryKey: proxyKeys.state,
    enabled,
    queryFn: proxyApi.state,
  })
}

function invalidate(qc: ReturnType<typeof useQueryClient>) {
  void qc.invalidateQueries({ queryKey: proxyKeys.state })
}

export function useCreateProxy() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: proxyApi.create, onSuccess: () => invalidate(qc) })
}

export function useUpdateProxy() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (args: { id: number; payload: Partial<ProxyPayload> }) =>
      proxyApi.update(args.id, args.payload),
    onSuccess: () => invalidate(qc),
  })
}

export function useDeleteProxy() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: proxyApi.remove, onSuccess: () => invalidate(qc) })
}

export function useTestProxy() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: proxyApi.test, onSuccess: () => invalidate(qc) })
}

export function useSaveBindings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: proxyApi.saveBindings,
    onSuccess: (data) => {
      if (data?.state) qc.setQueryData(proxyKeys.state, data.state)
      void qc.invalidateQueries({ queryKey: proxyKeys.state })
    },
  })
}

export function useApplyProxies() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: proxyApi.apply,
    onSuccess: (data) => {
      if (data?.state) qc.setQueryData(proxyKeys.state, data.state)
      void qc.invalidateQueries({ queryKey: proxyKeys.state })
    },
  })
}
