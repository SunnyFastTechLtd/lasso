import { http } from './request'
import type { Paginated } from '@/types/common'
import type {
  CommandPrefixes,
  ConnectivityTest,
  EgressState,
  McpListParams,
  McpExportPayload,
  McpExportRow,
  McpImportResult,
  McpServer,
  McpServerPayload,
  McpToolItem,
  MountableServer,
  ReferencesResponse,
  TestResult,
} from '@/types/mcp'

/** Django 端点一律带尾斜杠 */
export const mcpApi = {
  list: (params: McpListParams) => http.get<Paginated<McpServer>>('/mcp/servers/', params),

  detail: (id: number) => http.get<McpServer>(`/mcp/servers/${id}/`),

  /** B1/B2 新增；响应附带 B3 自动测试结果 */
  create: (payload: McpServerPayload) =>
    http.post<McpServer & { connectivity_test: ConnectivityTest }>('/mcp/servers/', payload),

  update: (id: number, payload: Partial<McpServerPayload>) =>
    http.patch<McpServer>(`/mcp/servers/${id}/`, payload),

  /** B6：有引用默认 409；confirmUnmount=true 才删除并摘除挂载 */
  remove: (id: number, confirmUnmount = false) =>
    http.delete<void>(`/mcp/servers/${id}/`, { params: confirmUnmount ? { confirm_unmount: 'true' } : undefined }),

  test: (id: number) => http.post<TestResult>(`/mcp/servers/${id}/test/`),

  tools: (id: number, params: { page?: number; page_size?: number; search?: string; state?: string }) =>
    http.get<Paginated<McpToolItem>>(`/mcp/servers/${id}/tools/`, params),

  toggleTools: (id: number, payload: { names: string[]; enabled: boolean }) =>
    http.post<{ enabled_tools: string[]; changed: string[]; enabled: boolean; server: McpServer }>(
      `/mcp/servers/${id}/tools/toggle/`,
      payload,
    ),

  disable: (id: number) => http.post<McpServer>(`/mcp/servers/${id}/disable/`),

  enable: (id: number) => http.post<McpServer>(`/mcp/servers/${id}/enable/`),

  references: (id: number) => http.get<ReferencesResponse>(`/mcp/servers/${id}/references/`),

  /** B2 内置只读白名单（仅管理员可见） */
  commandPrefixes: () => http.get<CommandPrefixes>('/mcp/servers/command-prefixes/'),

  /** B8 出网总开关（读写均仅管理员） */
  getEgress: () => http.get<EgressState>('/mcp/servers/egress/'),
  setEgress: (input: { enabled?: boolean; strict_domains?: boolean }) =>
    http.post<EgressState>('/mcp/servers/egress/', input),

  mountable: () => http.get<MountableServer[]>('/mcp/servers/mountable/'),

  /** 跨环境导出：不含密钥；导入按名称去重，不覆盖现有配置 */
  exportConfig: () => http.get<McpExportPayload>('/mcp/servers/export/'),
  importConfig: (payload: { version: number; servers: McpExportRow[] }) =>
    http.post<McpImportResult>('/mcp/servers/import/', payload),
}
