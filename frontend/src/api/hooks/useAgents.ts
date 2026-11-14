import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { agentsApi } from '@/api/agents'
import { queryKeys } from '@/api/queryKeys'
import type { AgentExportPackage, AgentListParams, AgentPayload } from '@/types/agent'

/** 任意写操作后统一刷新 agents 域缓存（含 skills 挂载计数） */
function invalidateAgents(qc: QueryClient) {
  void qc.invalidateQueries({ queryKey: ['agents'] })
  void qc.invalidateQueries({ queryKey: ['skills'] })
}

export function useAgents(params: AgentListParams) {
  return useQuery({
    queryKey: queryKeys.agents.list(params),
    queryFn: () => agentsApi.list(params),
  })
}

/** A1 顶部统计卡 + 空状态引导 */
export function useAgentSummary() {
  return useQuery({
    queryKey: queryKeys.agents.summary,
    staleTime: 15_000,
    queryFn: () => agentsApi.summary(),
  })
}

/** 驱动全部枚举：两档预设/权限/可见范围/占位符/状态 */
export function useAgentOptions() {
  return useQuery({
    queryKey: queryKeys.agents.options,
    staleTime: 60_000,
    queryFn: () => agentsApi.options(),
  })
}

export function useAgent(id: number | null) {
  return useQuery({
    queryKey: queryKeys.agents.detail(id ?? 0),
    enabled: id !== null,
    queryFn: () => agentsApi.detail(id as number),
  })
}

/** A4 装配选择器：勾选态来自后端实时计算 */
export function useMountOptions(id: number | null) {
  return useQuery({
    queryKey: queryKeys.agents.mountOptions(id ?? 0),
    enabled: id !== null,
    queryFn: () => agentsApi.mountOptions(id as number),
  })
}

export function useCreateAgent() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (payload: Partial<AgentPayload>) => agentsApi.create(payload),
    onSuccess: () => invalidateAgents(qc),
  })
}

export function useUpdateAgent() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; payload: Partial<AgentPayload> }) =>
      agentsApi.update(input.id, input.payload),
    onSuccess: () => invalidateAgents(qc),
  })
}

export function useDeleteAgent() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => agentsApi.remove(id),
    onSuccess: () => invalidateAgents(qc),
  })
}

export function useAgentAction() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: {
      id: number
      action: 'disable' | 'enable' | 'archive' | 'restore'
    }) => agentsApi[input.action](input.id),
    onSuccess: () => invalidateAgents(qc),
  })
}

export function useRestoreBuiltin() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => agentsApi.restoreBuiltin(id),
    onSuccess: () => invalidateAgents(qc),
  })
}

export function useCloneAgent() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; name?: string }) => agentsApi.clone(input.id, input.name),
    onSuccess: () => invalidateAgents(qc),
  })
}

/** A7 发布：错误体也内嵌 check，由调用方消化 */
export function usePublishAgent() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; risk_ack?: boolean; confirm_diff?: boolean }) =>
      agentsApi.publish(input.id, { risk_ack: input.risk_ack, confirm_diff: input.confirm_diff }),
    onSuccess: () => invalidateAgents(qc),
  })
}

/** A8 只读历史 */
export function useAgentVersions(id: number | null, page = 1, enabled = true) {
  return useQuery({
    queryKey: queryKeys.agents.versions(id ?? 0, { page }),
    enabled: id !== null && enabled,
    queryFn: () => agentsApi.versions(id as number, page),
  })
}

export function useAgentVersion(id: number | null, version: number | null) {
  return useQuery({
    queryKey: queryKeys.agents.version(id ?? 0, version ?? 0),
    enabled: id !== null && version !== null,
    queryFn: () => agentsApi.versionDetail(id as number, version as number),
  })
}

/** A8-1 任选两版本对比：仅在恰好选中两个版本时请求 */
export function useAgentVersionCompare(
  id: number | null,
  left: number | null,
  right: number | null,
) {
  return useQuery({
    queryKey: queryKeys.agents.versionCompare(id ?? 0, left ?? 0, right ?? 0),
    enabled: id !== null && left !== null && right !== null && left !== right,
    staleTime: 30_000,
    queryFn: () => agentsApi.compareVersions(id as number, left as number, right as number),
  })
}

/** A8-2 回滚：错误体含 blocking/warnings，由调用方消化 */
export function useRollbackAgentVersion() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; version: number; risk_ack?: boolean }) =>
      agentsApi.rollbackVersion(input.id, input.version, { risk_ack: input.risk_ack }),
    onSuccess: () => invalidateAgents(qc),
  })
}

/** 跨环境导入申请列表（单级审批状态） */
export function useImportRequests(
  params: { page?: number; page_size?: number; status?: string } = {},
  enabled = true,
) {
  return useQuery({
    queryKey: queryKeys.agents.importRequests(params),
    enabled,
    queryFn: () => agentsApi.importRequests(params),
  })
}

/** 审批状态计数（管理员 actionable 为可审批数量） */
export function useImportRequestSummary(enabled = true) {
  return useQuery({
    queryKey: queryKeys.agents.importSummary,
    enabled,
    staleTime: 15_000,
    queryFn: () => agentsApi.importRequestSummary(),
  })
}

/** 导入包预览：依赖映射与阻断/告警，不落库 */
export function usePreviewImport() {
  return useMutation({
    mutationFn: (payload: AgentExportPackage) => agentsApi.previewImport(payload),
  })
}

export function useCreateImportRequest() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (payload: AgentExportPackage) => agentsApi.createImportRequest(payload),
    onSuccess: () => invalidateAgents(qc),
  })
}

/** 审批（通过/驳回）：均要求管理员且非提交人，由后端强校验 */
export function useReviewImportRequest() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; action: 'approve' | 'reject'; note?: string }) =>
      input.action === 'approve'
        ? agentsApi.approveImportRequest(input.id, input.note)
        : agentsApi.rejectImportRequest(input.id, input.note),
    onSuccess: () => invalidateAgents(qc),
  })
}
