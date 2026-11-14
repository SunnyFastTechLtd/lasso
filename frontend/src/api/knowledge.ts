import { http } from './request'
import type { Paginated } from '@/types/common'

/** 知识库管理（PRD E1/E2）：与 backend/apps/knowledge 序列化器逐字段对齐 */

export type KbScope = 'public' | 'department' | 'private'
export type DocStatus = 'pending' | 'processing' | 'completed' | 'failed'
export type TiangongStatus = 'idle' | 'queued' | 'running' | 'done' | 'failed'

export interface KnowledgeBase {
  id: number
  name: string
  description: string
  icon: string
  color: string
  /** 旧字段，写请求按 scope 同步 */
  is_public: boolean
  scope: KbScope
  department: string
  document_count: number
  chunk_count: number
  size_bytes: number
  /** 天工模式：从公网与模型自动采集知识 */
  tiangong_enabled: boolean
  tiangong_topics: string
  tiangong_seeds: string
  tiangong_status: TiangongStatus
  tiangong_last_run_at: string | null
  tiangong_last_error: string
  created_at: string
  updated_at: string
}

/** 建档/编辑载荷（color 不在管理端暴露，沿用后端默认） */
export interface KnowledgeBasePayload {
  name: string
  description?: string
  icon?: string
  scope: KbScope
  department?: string
  is_public: boolean
  tiangong_enabled?: boolean
  tiangong_topics?: string
  tiangong_seeds?: string
}

export interface TiangongRun {
  id: number
  knowledge_base: number
  status: 'queued' | 'running' | 'done' | 'failed' | 'skipped'
  trigger: 'create' | 'manual'
  topics: string
  web_created: number
  model_created: number
  skipped: number
  /** 搜索层命中的层与降级链（一次运行只记聚合，不逐条记 URL） */
  discovery_layer: '' | 'l1_mcp' | 'l2_searxng' | 'l3_bing' | 'base'
  fallback_chain: TiangongChainEntry[]
  search_queries: number
  search_hits: number
  candidates: number
  proposals: number
  approved: number
  pages_fetched: number
  quality_rejected: number
  sources_tried: number
  proposal_count: number
  degraded_reason: string
  error: string
  actor_name: string
  created_at: string
  finished_at: string | null
}

export interface TiangongChainEntry {
  layer: string
  result: 'hit' | 'empty' | 'failed' | 'skipped'
  reason: string
  count: number
  elapsed_ms: number
  proposals?: number
}

export type TiangongSourceStatus = 'untested' | 'ok' | 'failed'

/** 天工来源库：平台级白名单，只有启用来源会被 L4 抓正文 */
export interface TiangongSource {
  id: number
  name: string
  domain: string
  entry_urls: string
  topic_tags: string
  description: string
  crawl_rules: Record<string, unknown>
  note: string
  enabled: boolean
  priority: number
  last_checked_at: string | null
  last_status: TiangongSourceStatus
  last_status_display: string
  last_error: string
  created_by_name: string
  created_at: string
  updated_at: string
}

export interface TiangongSourcePayload {
  name: string
  domain: string
  entry_urls?: string
  topic_tags?: string
  description?: string
  crawl_rules?: Record<string, unknown>
  note?: string
  enabled?: boolean
  priority?: number
}

export type TiangongProposalStatus = 'pending' | 'approved' | 'rejected'
export type TiangongDiscoveredBy = 'mcp' | 'searxng' | 'bing' | 'model' | 'seeds'

/** 候选来源：搜索层只产候选，管理员批准后才进入来源库并被抓取 */
export interface TiangongProposal {
  id: number
  run: number | null
  knowledge_base: number | null
  knowledge_base_name: string
  source_type: string
  source_type_display: string
  discovered_by: TiangongDiscoveredBy
  discovered_by_display: string
  domain: string
  url: string
  title: string
  snippet: string
  topics: string
  score: number
  status: TiangongProposalStatus
  status_display: string
  reviewed_by: number | null
  reviewed_by_name: string
  reviewed_at: string | null
  reject_reason: string
  created_at: string
}

export interface SearxngCircuit {
  open: boolean
  open_until: string
  fail_count: number
  threshold: number
  window_minutes: number
}

/** L3 Bing 熔断：验证码/429 当日熔断 24 小时 */
export interface BingCircuit {
  open: boolean
  open_until: string
  fail_count: number
  threshold: number
  hours: number
}

export interface BingState {
  enabled: boolean
  daily_cap: number
  max_queries: number
  max_results: number
  timeout: number
  interval: number
  available: boolean
  unavailable_reason: string
  unavailable_detail: string
  circuit: BingCircuit
  daily_used: number
  last_status: string
  last_error: string
  last_at: string
}

export interface TiangongSearchSettings {
  discovery_enabled: boolean
  mcp_enabled: boolean
  searxng_enabled: boolean
  bing_enabled: boolean
  searxng_url: string
  searxng_timeout: number
  max_queries: number
  max_results: number
  min_results: number
  crawl_max_pages: number
  crawl_pages_per_source: number
  searxng_circuit: SearxngCircuit
  bing_daily_cap: number
  bing_max_queries: number
  bing_max_results: number
  bing_timeout: number
  bing_interval: number
  bing_state: BingState
}

export interface SearxngCheckResult {
  ok: boolean
  url: string
  elapsed_ms: number
  detail: string
  reason: string
  circuit: SearxngCircuit
}

export interface BingCheckResult {
  ok: boolean
  reason: string
  detail: string
  executable: string
  elapsed_ms: number
  circuit: BingCircuit
  daily_used: number
  daily_cap: number
}

/** L1 MCP 搜索绑定：只存映射，不存凭据 */
export interface TiangongSearchBinding {
  id: number
  server: number
  server_name: string
  server_status: string
  server_active: boolean
  tool: number
  tool_name: string
  tool_full_name: string
  tool_enabled: boolean
  tool_state: string
  enabled: boolean
  priority: number
  query_field: string
  limit_field: string
  default_limit: number
  results_path: string
  url_field: string
  title_field: string
  snippet_field: string
  extra_args: Record<string, unknown>
  note: string
  usable: boolean
  unusable_reason: string
  last_status: '' | 'ok' | 'failed'
  last_error: string
  last_latency_ms: number
  last_result_count: number
  last_checked_at: string | null
  created_by_name: string
  created_at: string
  updated_at: string
}

export interface TiangongSearchBindingPayload {
  server: number
  tool: number
  enabled?: boolean
  priority?: number
  query_field?: string
  limit_field?: string
  default_limit?: number
  results_path?: string
  url_field?: string
  title_field?: string
  snippet_field?: string
  extra_args?: Record<string, unknown>
  note?: string
}

export interface TiangongSearchCandidate {
  server_id: number
  server_name: string
  server_status: string
  server_active: boolean
  tool_id: number
  tool_name: string
  tool_full_name: string
  tool_enabled: boolean
  tool_state: string
  description: string
  input_schema: Record<string, unknown>
  score: number
  reasons: string[]
  suggested_mapping: {
    query_field: string
    limit_field: string
    default_limit: number
    results_path: string
    url_field: string
    title_field: string
    snippet_field: string
  }
  bound: boolean
}

export interface TiangongBindingTestResult {
  results: Array<{ url: string; title: string; snippet: string; domain: string; score: number; engine: string }>
  reason: string
  raw_count: number
  elapsed_ms: number
  error: string
  binding: TiangongSearchBinding
}

export interface KnowledgeBaseStats {
  id: number
  name: string
  document_count: number
  chunk_count: number
  size_bytes: number
  created_at: string
}

export interface ExtractionPage {
  page: number | null
  chars: number
  text_layer: boolean
  ocr: boolean
  engine: string
  status: 'ok' | 'failed'
  reason: string
}

/** 逐页提取报告：哪些页有文字层 / 走了 OCR / 未解析及原因 */
export interface ExtractionReport {
  mode?: 'auto' | 'text'
  ocr_used?: boolean
  pages_total?: number
  ocr_pages?: number[]
  failed_pages?: { page: number | null; reason: string }[]
  pages?: ExtractionPage[]
}

export interface KnowledgeDocument {
  id: number
  knowledge_base: number
  knowledge_base_name: string
  title: string
  file_name: string
  file_type: string
  file_size: number
  size_human: string
  file_url: string | null
  status: DocStatus
  error_msg: string
  chunk_count: number
  /** 逐页提取报告（OCR / 未解析明细）；老文档为空对象 */
  extraction_report?: ExtractionReport
  /** 来源：上传 / 天工公网抓取 / 天工模型生成 */
  source_type: 'upload' | 'web' | 'model'
  source_model: string
  source_url: string
  source_site: string
  source_title: string
  fetched_at: string | null
  uploaded_by_name: string
  created_at: string
  updated_at: string
}

/** 切片只读预览：content 用于人工核对分块质量 */
export interface DocumentChunk {
  id: number
  chunk_index: number
  content: string
  is_active: boolean
  has_embedding: boolean
  /** 页码与 OCR 标记（不写进正文） */
  meta?: {
    page?: number | null
    ocr?: boolean
    ocr_engine?: string
  }
  created_at: string
}

export type KbListParams = {
  page?: number
  page_size?: number
}

export type DocumentListParams = KbListParams & {
  knowledge_base_id?: number
}

export interface DocumentUploadInput {
  knowledge_base_id: number
  title?: string
  file: File
}

/** 后端无 filter_backends / search_fields：?search= 不生效，搜索在前端做 */
export const knowledgeQueryKeys = {
  all: ['knowledge'] as const,
  bases: (params: Record<string, unknown>) => ['knowledge', 'bases', params] as const,
  baseStats: (id: number) => ['knowledge', 'bases', id, 'stats'] as const,
  documents: (params: Record<string, unknown>) => ['knowledge', 'documents', params] as const,
  chunks: (docId: number) => ['knowledge', 'documents', docId, 'chunks'] as const,
  sources: (params: Record<string, unknown>) => ['knowledge', 'tiangong-sources', params] as const,
  proposals: (params: Record<string, unknown>) => ['knowledge', 'tiangong-proposals', params] as const,
  searchSettings: () => ['knowledge', 'tiangong-search-settings'] as const,
  bindings: (params: Record<string, unknown>) => ['knowledge', 'tiangong-bindings', params] as const,
}

/** scope 后端文案是 公开/本部门/私有，界面按 PRD 词汇改为 企业库/部门库/个人库 */
export const KB_SCOPE_LABEL: Record<KbScope, string> = {
  public: '企业库',
  department: '部门库',
  private: '个人库',
}

export const KB_SCOPE_HINT: Record<KbScope, string> = {
  public: '全企业可见可检索',
  department: '仅同名部门可见可检索，需填写部门',
  private: '仅创建者本人可见可检索',
}

export const knowledgeApi = {
  listBases: (params: KbListParams) => http.get<Paginated<KnowledgeBase>>('/knowledge/bases/', params),

  createBase: (payload: KnowledgeBasePayload) => http.post<KnowledgeBase>('/knowledge/bases/', payload),

  updateBase: (id: number, payload: Partial<KnowledgeBasePayload>) =>
    http.patch<KnowledgeBase>(`/knowledge/bases/${id}/`, payload),

  /** 硬删除：资料与向量索引一并清除，智能体挂载会留下悬空引用 */
  removeBase: (id: number) => http.delete<void>(`/knowledge/bases/${id}/`),

  baseStats: (id: number) => http.get<KnowledgeBaseStats>(`/knowledge/bases/${id}/stats/`),

  listDocuments: (params: DocumentListParams) => http.get<Paginated<KnowledgeDocument>>('/knowledge/documents/', params),

  uploadDocument: ({ knowledge_base_id, title, file }: DocumentUploadInput) => {
    const fd = new FormData()
    fd.append('knowledge_base_id', String(knowledge_base_id))
    if (title?.trim()) fd.append('title', title.trim())
    fd.append('file', file)
    return http.upload<KnowledgeDocument>('/knowledge/documents/upload/', fd)
  },

  removeDocument: (id: number) => http.delete<void>(`/knowledge/documents/${id}/`),

  /** 处理中 400：文档正在处理中，请稍后再试 */
  reprocessDocument: (id: number) => http.post<KnowledgeDocument>(`/knowledge/documents/${id}/reprocess/`),

  chunks: (docId: number) => http.get<DocumentChunk[]>(`/knowledge/documents/${docId}/chunks/`),

  /** 手动补充采集（天工）：同一库同时只允许一个任务 */
  runTiangong: (id: number) => http.post<TiangongRun>(`/knowledge/bases/${id}/tiangong/run/`),

  tiangongRuns: (id: number) => http.get<TiangongRun[]>(`/knowledge/bases/${id}/tiangong/runs/`),

  // 天工来源库（管理员写，其他只读）
  listTiangongSources: (params: Record<string, unknown>) =>
    http.get<Paginated<TiangongSource>>('/knowledge/tiangong-sources/', params),
  createTiangongSource: (payload: TiangongSourcePayload) =>
    http.post<TiangongSource>('/knowledge/tiangong-sources/', payload),
  updateTiangongSource: (id: number, payload: Partial<TiangongSourcePayload>) =>
    http.patch<TiangongSource>(`/knowledge/tiangong-sources/${id}/`, payload),
  removeTiangongSource: (id: number) => http.delete<void>(`/knowledge/tiangong-sources/${id}/`),
  checkTiangongSource: (id: number) =>
    http.post<{ ok: boolean; robots_ok: boolean; crawl_delay: number; sitemap_count: number; error: string; source: TiangongSource }>(
      `/knowledge/tiangong-sources/${id}/check/`,
    ),

  // 候选来源（管理员审批）
  listTiangongProposals: (params: Record<string, unknown>) =>
    http.get<Paginated<TiangongProposal>>('/knowledge/tiangong-source-proposals/', params),
  approveTiangongProposal: (id: number) =>
    http.post<{ proposal: TiangongProposal; source: TiangongSource; created: boolean }>(
      `/knowledge/tiangong-source-proposals/${id}/approve/`,
    ),
  rejectTiangongProposal: (id: number, reason: string) =>
    http.post<TiangongProposal>(`/knowledge/tiangong-source-proposals/${id}/reject/`, { reason }),

  // 搜索层与抓取参数
  tiangongSearchSettings: () =>
    http.get<TiangongSearchSettings>('/knowledge/tiangong-search/settings/'),
  updateTiangongSearchSettings: (
    payload: Partial<TiangongSearchSettings> & { reset_circuit?: boolean; reset_bing_circuit?: boolean },
  ) => http.post<TiangongSearchSettings>('/knowledge/tiangong-search/settings/', payload),
  checkSearxng: () =>
    http.post<SearxngCheckResult>('/knowledge/tiangong-search/searxng/check/'),
  checkBing: () =>
    http.post<BingCheckResult>('/knowledge/tiangong-search/bing/check/'),

  // L1 MCP 搜索绑定
  scanTiangongCandidates: () =>
    http.get<{ candidates: TiangongSearchCandidate[]; total: number }>(
      '/knowledge/tiangong-search/candidates/',
    ),
  listTiangongBindings: (params: Record<string, unknown>) =>
    http.get<Paginated<TiangongSearchBinding>>('/knowledge/tiangong-search-bindings/', params),
  createTiangongBinding: (payload: TiangongSearchBindingPayload) =>
    http.post<TiangongSearchBinding>('/knowledge/tiangong-search-bindings/', payload),
  updateTiangongBinding: (id: number, payload: Partial<TiangongSearchBindingPayload>) =>
    http.patch<TiangongSearchBinding>(`/knowledge/tiangong-search-bindings/${id}/`, payload),
  removeTiangongBinding: (id: number) =>
    http.delete<void>(`/knowledge/tiangong-search-bindings/${id}/`),
  testTiangongBinding: (id: number, query: string) =>
    http.post<TiangongBindingTestResult>(
      `/knowledge/tiangong-search-bindings/${id}/test/`, { query },
    ),
}
