import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { mcpApi } from '@/api/mcp'
import { queryKeys } from '@/api/queryKeys'
import type { Paginated } from '@/types/common'
import type { McpExportRow, McpListParams, McpServer, McpServerPayload, McpStatus } from '@/types/mcp'

/** 任意写操作后统一刷新 mcp 域缓存（key 前缀一致，代价可接受） */
function invalidateMcp(qc: QueryClient) {
  void qc.invalidateQueries({ queryKey: ['mcp'] })
}

const COUNT_STATUSES: McpStatus[] = ['healthy', 'unknown', 'error', 'disabled', 'egress_blocked']

export function useMcpServers(params: McpListParams) {
  return useQuery({
    queryKey: queryKeys.mcp.list(params),
    queryFn: () => mcpApi.list(params),
  })
}

/** 指标卡：并行取各状态总数（page_size=1 只读 count，均为真实接口） */
export function useMcpStatusCounts(enabled = true) {
  return useQuery({
    queryKey: ['mcp', 'status-counts'],
    enabled,
    staleTime: 30_000,
    queryFn: async () => {
      const [total, ...byStatus] = await Promise.all([
        mcpApi.list({ page: 1, page_size: 1 }),
        ...COUNT_STATUSES.map((status) => mcpApi.list({ page: 1, page_size: 1, status })),
      ])
      const counts: Record<string, number> = { total: total.count }
      COUNT_STATUSES.forEach((status, i) => {
        counts[status] = (byStatus[i] as Paginated<McpServer>).count
      })
      return counts
    },
  })
}

export function useMcpServer(id: number | null) {
  return useQuery({
    queryKey: queryKeys.mcp.detail(id ?? 0),
    enabled: id !== null,
    queryFn: () => mcpApi.detail(id as number),
  })
}

export function useMcpTools(id: number | null, params: { page?: number; page_size?: number; search?: string; state?: string }) {
  return useQuery({
    queryKey: queryKeys.mcp.tools(id ?? 0, params),
    enabled: id !== null,
    queryFn: () => mcpApi.tools(id as number, params),
  })
}

export function useMcpReferences(id: number | null, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.mcp.references(id ?? 0),
    enabled: id !== null && enabled,
    queryFn: () => mcpApi.references(id as number),
  })
}

export function useCommandPrefixes(enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.mcp.prefixes,
    enabled,
    staleTime: 300_000,
    queryFn: () => mcpApi.commandPrefixes(),
  })
}

/** B8 出网开关：接口仅管理员可读写 */
export function useEgress(enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.mcp.egress,
    enabled,
    queryFn: () => mcpApi.getEgress(),
  })
}

export function useSetEgress() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { enabled?: boolean; strict_domains?: boolean }) => mcpApi.setEgress(input),
    onSuccess: () => invalidateMcp(qc),
  })
}

/** 新增（返回体附带自动测试）或 PATCH 更新 */
export function useSaveMcpServer() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number | null; payload: McpServerPayload }) =>
      input.id === null ? mcpApi.create(input.payload) : mcpApi.update(input.id, input.payload),
    onSuccess: () => invalidateMcp(qc),
  })
}

export function useTestMcpServer() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => mcpApi.test(id),
    onSuccess: () => invalidateMcp(qc),
  })
}

export function useToggleMcpTools(serverId: number) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { names: string[]; enabled: boolean }) => mcpApi.toggleTools(serverId, input),
    onSuccess: () => invalidateMcp(qc),
  })
}

export function useSetMcpActive() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; active: boolean }) =>
      input.active ? mcpApi.enable(input.id) : mcpApi.disable(input.id),
    onSuccess: () => invalidateMcp(qc),
  })
}

export function useDeleteMcpServer() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; confirmUnmount?: boolean }) => mcpApi.remove(input.id, input.confirmUnmount),
    onSuccess: () => invalidateMcp(qc),
  })
}

/** 跨环境导入：按名称去重，导入后来源为未验证，需逐个测试 */
export function useImportMcpServers() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { version: number; servers: McpExportRow[] }) => mcpApi.importConfig(input),
    onSuccess: () => invalidateMcp(qc),
  })
}
