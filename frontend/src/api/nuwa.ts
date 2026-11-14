import { http } from '@/api/request'

export type CandidateKind = 'mcp' | 'skill'

export interface NuwaSkeleton {
  name: string
  description: string
  job_tags: string[]
  icon: string
  prompt: string
  speech_tier: 'low' | 'mid' | 'high'
  retrieval_tier: 'few' | 'standard' | 'many'
  welcome_message: string
  visibility: 'private' | 'department' | 'public'
  knowledge_queries: string[]
  model_name: string
  is_blank?: boolean
  unknown_placeholders?: string[]
}

export interface NuwaSession {
  id: number
  brief: string
  audience: string
  department: string
  step: number
  status: string
  generated: Partial<NuwaSkeleton>
  generation_meta: Record<string, unknown>
  edited_fields: string[]
  candidates: MatchResult | Record<string, never>
  selections: {
    mcp?: number[]
    skills?: number[]
    submitted?: { id: number; kind: string; title: string; risk_flags: string[] }[]
    local?: { kind: string; id: number; title: string }[]
  }
  knowledge_plan: { urls?: string[]; queries?: string[]; discover?: boolean; kb_id?: number }
  knowledge_items: KnowledgeItem[]
  agent: number | null
  agent_name?: string
  error: string
  expired: boolean
  created_at: string
  updated_at: string
}

export interface MarketCandidate {
  id: number
  kind: CandidateKind
  external_id: string
  title: string
  summary: string
  publisher: string
  homepage: string
  repository_url: string
  license: string
  license_declared: boolean
  trust_level: 'trusted' | 'unverified' | 'blocked'
  transport: string
  requires_egress: boolean
  has_scripts: boolean
  egress_host: string
  source: string
  score: number
  reason: string
}

export interface MarketItem {
  id: number
  source: number
  source_name: string
  kind: CandidateKind
  external_id: string
  version: string
  title: string
  summary: string
  publisher: string
  homepage: string
  repository_url: string
  license: string
  license_declared: boolean
  trust_level: 'trusted' | 'unverified' | 'blocked'
  transport: string
  requires_egress: boolean
  has_scripts: boolean
  egress_host: string
  capabilities: string[]
  status: string
  fetched_at: string
  my_request: { id: number; status: 'pending' | 'approved' | 'rejected'; created_at: string } | null
}

export interface MarketFacet {
  id: number
  name: string
  kind: string
  count: number
}

export interface MarketItemPage {
  count: number
  results: MarketItem[]
}

/** 市场检索降级口径：向导与独立市场页共用同一套文案 */
export const MARKET_DEGRADED_TEXT: Record<string, string> = {
  switch_off: '管理员已关闭女娲在线检索，以下为本地缓存目录。',
  timeout: '在线检索超时，已回落到本地缓存目录。',
  network_error: '网络不可达，已回落到本地缓存目录。',
  rate_limited: '外部市场限流，已回落到本地缓存目录，稍后可重试。',
  offline_no_cache: '无法访问市场目录且本地无缓存，请联系管理员执行 nuwa_sync。',
  http_error: '外部市场返回异常，已回落到本地缓存目录。',
}

export interface MatchResult {
  items: MarketCandidate[]
  mode: 'online' | 'cached' | 'mixed'
  degraded_reason: string
  cache_synced_at: string
  online_hits: number
  online_attempted: boolean
}

export interface KnowledgeItem {
  url: string
  status: 'ok' | 'failed' | 'skipped'
  title: string
  site: string
  reason: string
  words: number
  content_sha256: string
  file: string
  preview?: string
}

export interface KnowledgePreviewResult {
  items: KnowledgeItem[]
  mode: string
  degraded_reason: string
  message?: string
  ok: number
  failed: number
  skipped: number
}

export interface KnowledgeBaseOption {
  id: number
  name: string
  scope: string
}

export const nuwaApi = {
  createSession: (body: { brief: string; audience?: string; department?: string }) =>
    http.post<NuwaSession>('/nuwa/sessions/', body),

  getSession: (id: number) => http.get<NuwaSession>(`/nuwa/sessions/${id}/`),

  updateSession: (id: number, body: Record<string, unknown>) =>
    http.patch<NuwaSession>(`/nuwa/sessions/${id}/`, body),

  regenerate: (id: number) => http.post<NuwaSession>(`/nuwa/sessions/${id}/regenerate/`),

  matches: (id: number, body: { kinds?: CandidateKind[]; query?: string; limit?: number }) =>
    http.post<MatchResult>(`/nuwa/sessions/${id}/matches/`, body),

  saveSelections: (id: number, body: { mcp: number[]; skills: number[] }) =>
    http.post<NuwaSession['selections']>(`/nuwa/sessions/${id}/selections/`, body),

  planKnowledge: (
    id: number,
    body: { queries?: string[]; urls?: string[]; discover?: boolean },
  ) => http.post(`/nuwa/sessions/${id}/knowledge/plan/`, body),

  previewKnowledge: (id: number, body: { urls: string[]; discover?: boolean }) =>
    http.post<KnowledgePreviewResult>(`/nuwa/sessions/${id}/knowledge/preview/`, body),

  commitKnowledge: (
    id: number,
    body: { urls: string[]; kb_id?: number; kb_name?: string; scope?: string },
  ) =>
    http.post<{
      knowledge_base_id: number
      created: { document_id: number; title: string; url: string }[]
      skipped: { url: string; reason: string }[]
    }>(`/nuwa/sessions/${id}/knowledge/commit/`, body),

  createAgent: (id: number) =>
    http.post<{
      agent_id: number
      created: boolean
      mounted: { mcp: { id: number; name: string }[]; skills: { id: number; name: string }[] }
      pending_imports: { id: number; kind: string; title: string; risk_flags: string[] }[]
      warnings: string[]
    }>(`/nuwa/sessions/${id}/create-agent/`),

  abandon: (id: number) => http.post(`/nuwa/sessions/${id}/abandon/`),

  // ─── 独立市场页（与向导第三步同一套检索/导入） ───

  searchMarket: (body: { query?: string; kinds?: CandidateKind[]; source?: number; limit?: number }) =>
    http.post<MatchResult>('/nuwa/market/search/', body),

  marketItems: (params: {
    page?: number
    page_size?: number
    kind?: CandidateKind
    source?: number
    search?: string
    trust?: 'trusted' | 'unverified'
  }) => http.get<MarketItemPage>('/nuwa/items/', params),

  marketItem: (id: number) => http.get<MarketItem>(`/nuwa/items/${id}/`),

  marketFacets: () => http.get<MarketFacet[]>('/nuwa/items/facets/'),

  submitImport: (marketItem: number) =>
    http.post<{
      id: number
      kind: string
      status: string
      risk_flags: string[]
      item_title: string
      created: boolean
    }>('/nuwa/import-requests/', { market_item: marketItem }),

  myImportRequests: () =>
    http.get<{ count: number; results: Record<string, unknown>[] }>('/nuwa/import-requests/', {
      page_size: 50,
    }),

  switches: () =>
    http.get<{
      market_online: boolean
      knowledge_online: boolean
      knowledge_allowlist: string[]
      agent_tool_egress: boolean
    }>('/nuwa/switches/'),

  knowledgeBases: () =>
    http.get<{ results: KnowledgeBaseOption[] } | KnowledgeBaseOption[]>('/knowledge/bases/', {
      page_size: 100,
    }),

  agentOptions: () =>
    http.get<{
      models: string[]
      speech_tiers?: { value: string; label: string }[]
      retrieval_tiers?: { value: string; label: string }[]
    }>('/agents/options/'),
}
