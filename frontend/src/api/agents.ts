import { http } from './request'
import type { Paginated } from '@/types/common'
import type {
  Agent,
  AgentExportPackage,
  AgentImportPreview,
  AgentImportRequest,
  AgentImportSubmitResult,
  AgentImportSummary,
  AgentListItem,
  AgentListParams,
  AgentOptions,
  AgentPayload,
  AgentSummary,
  AgentVersion,
  AgentVersionCompare,
  AgentVersionDetail,
  MountOptions,
  PublishCheck,
  PublishResponse,
  RollbackResponse,
} from '@/types/agent'

const base = '/agents'

export const agentsApi = {
  list: (params: AgentListParams) => http.get<Paginated<AgentListItem>>(`${base}/`, params),
  create: (payload: Partial<AgentPayload>) => http.post<Agent>(`${base}/`, payload),
  detail: (id: number) => http.get<Agent>(`${base}/${id}/`),
  update: (id: number, payload: Partial<AgentPayload>) => http.patch<Agent>(`${base}/${id}/`, payload),
  remove: (id: number) => http.delete<void>(`${base}/${id}/`),

  options: () => http.get<AgentOptions>(`${base}/options/`),
  summary: () => http.get<AgentSummary>(`${base}/summary/`),
  /** A4 四分区装配选择器（含当前勾选态） */
  mountOptions: (id: number) => http.get<MountOptions>(`${base}/${id}/mount-options/`),

  /** A7 只读预检（幂等 GET） */
  publishCheck: (id: number) => http.get<PublishCheck>(`${base}/${id}/publish-check/`),
  publish: (id: number, body: { risk_ack?: boolean; confirm_diff?: boolean }) =>
    http.post<PublishResponse>(`${base}/${id}/publish/`, body),

  disable: (id: number) => http.post<Agent>(`${base}/${id}/disable/`),
  enable: (id: number) => http.post<Agent>(`${base}/${id}/enable/`),
  archive: (id: number) => http.post<Agent>(`${base}/${id}/archive/`),
  restore: (id: number) => http.post<Agent>(`${base}/${id}/restore/`),
  /** 4.2 内置智能体恢复出厂配置（只改草稿，需重新发布才影响使用端） */
  restoreBuiltin: (id: number) => http.post<Agent>(`${base}/${id}/restore-builtin/`),
  clone: (id: number, name?: string) => http.post<Agent>(`${base}/${id}/clone/`, name ? { name } : {}),

  versions: (id: number, page = 1) =>
    http.get<Paginated<AgentVersion>>(`${base}/${id}/versions/`, { page }),
  versionDetail: (id: number, version: number) =>
    http.get<AgentVersionDetail>(`${base}/${id}/versions/${version}/`),
  /** A8-1 任选两版本对比（left/right 为版本号，服务端按旧→新归一化） */
  compareVersions: (id: number, left: number, right: number) =>
    http.get<AgentVersionCompare>(`${base}/${id}/versions/compare/`, { left, right }),
  /** A8-2 以此版本重新发布（重走 A7 检查，生成新版本号） */
  rollbackVersion: (id: number, version: number, body: { risk_ack?: boolean } = {}) =>
    http.post<RollbackResponse>(`${base}/${id}/versions/${version}/rollback/`, body),

  /** 跨环境导出：不含密钥，导入后需重新填写并重新发布 */
  exportAgent: (id: number) => http.get<AgentExportPackage>(`${base}/${id}/export/`),

  /** 导入包预览：依赖映射结果 + 阻断/告警，不写库 */
  previewImport: (packagePayload: AgentExportPackage) =>
    http.post<AgentImportPreview>(`${base}/import-requests/preview/`, { package: packagePayload }),
  /** 提交导入申请（单级审批，提交不生效） */
  createImportRequest: (packagePayload: AgentExportPackage) =>
    http.post<AgentImportSubmitResult>(`${base}/import-requests/`, { package: packagePayload }),
  /** 导入申请列表：管理员全量、普通用户仅自己 */
  importRequests: (params: { page?: number; page_size?: number; status?: string } = {}) =>
    http.get<Paginated<AgentImportRequest>>(`${base}/import-requests/`, params),
  importRequestSummary: () =>
    http.get<AgentImportSummary>(`${base}/import-requests/summary/`),
  /** 审批通过：管理员且非提交人；落库为草稿 */
  approveImportRequest: (id: number, note?: string) =>
    http.post<{ detail: string; agent: Agent; request: AgentImportRequest }>(
      `${base}/import-requests/${id}/approve/`,
      note ? { note } : {},
    ),
  rejectImportRequest: (id: number, note?: string) =>
    http.post<{ detail: string; request: AgentImportRequest }>(
      `${base}/import-requests/${id}/reject/`,
      note ? { note } : {},
    ),
}
