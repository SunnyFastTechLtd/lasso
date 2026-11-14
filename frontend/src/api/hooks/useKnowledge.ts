import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { knowledgeApi, knowledgeQueryKeys } from '@/api/knowledge'
import type {
  DocumentListParams,
  DocumentUploadInput,
  KbListParams,
  KnowledgeBasePayload,
  TiangongSearchBindingPayload,
  TiangongSourcePayload,
} from '@/api/knowledge'

/** 任意写操作后统一刷新 knowledge 域缓存 */
function invalidateKnowledge(qc: ReturnType<typeof useQueryClient>) {
  void qc.invalidateQueries({ queryKey: knowledgeQueryKeys.all })
}

/** 后端不支持服务端搜索，此处一次取满单页上限（page_size=100）供前端过滤 */
export function useKnowledgeBases(params: KbListParams) {
  return useQuery({
    queryKey: knowledgeQueryKeys.bases(params),
    queryFn: () => knowledgeApi.listBases(params),
    // 天工采集在 Celery 中异步执行：有排队/采集中的库时每 3s 轮询，全部落定后自动停止
    refetchInterval: (query) =>
      query.state.data?.results?.some(
        (b) => b.tiangong_status === 'queued' || b.tiangong_status === 'running',
      )
        ? 3000
        : false,
  })
}

export function useCreateKnowledgeBase() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (payload: KnowledgeBasePayload) => knowledgeApi.createBase(payload),
    onSuccess: () => invalidateKnowledge(qc),
  })
}

export function useUpdateKnowledgeBase() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; payload: Partial<KnowledgeBasePayload> }) =>
      knowledgeApi.updateBase(input.id, input.payload),
    onSuccess: () => invalidateKnowledge(qc),
  })
}

export function useDeleteKnowledgeBase() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => knowledgeApi.removeBase(id),
    onSuccess: () => invalidateKnowledge(qc),
  })
}

export function useKnowledgeBaseStats(id: number | null, enabled = true) {
  return useQuery({
    queryKey: knowledgeQueryKeys.baseStats(id ?? 0),
    enabled: id !== null && enabled,
    staleTime: 15_000,
    queryFn: () => knowledgeApi.baseStats(id as number),
  })
}

export function useKnowledgeDocuments(params: DocumentListParams, enabled: boolean) {
  return useQuery({
    queryKey: knowledgeQueryKeys.documents(params),
    enabled,
    queryFn: () => knowledgeApi.listDocuments(params),
    // 切片在 Celery 中异步执行：有「待处理/处理中」的资料时每 3s 轮询，全部落定后自动停止
    refetchInterval: (query) =>
      query.state.data?.results?.some((d) => d.status === 'pending' || d.status === 'processing')
        ? 3000
        : false,
  })
}

export function useUploadDocument() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: DocumentUploadInput) => knowledgeApi.uploadDocument(input),
    onSuccess: () => invalidateKnowledge(qc),
  })
}

export function useDeleteDocument() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => knowledgeApi.removeDocument(id),
    onSuccess: () => invalidateKnowledge(qc),
  })
}

export function useReprocessDocument() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => knowledgeApi.reprocessDocument(id),
    onSuccess: () => invalidateKnowledge(qc),
  })
}

export function useDocumentChunks(docId: number | null) {
  return useQuery({
    queryKey: knowledgeQueryKeys.chunks(docId ?? 0),
    enabled: docId !== null,
    queryFn: () => knowledgeApi.chunks(docId as number),
  })
}

export function useTiangongRuns(kbId: number | null, enabled = true) {
  return useQuery({
    queryKey: ['knowledge', 'tiangong-runs', kbId ?? 0],
    enabled: kbId !== null && enabled,
    queryFn: () => knowledgeApi.tiangongRuns(kbId as number),
  })
}

export function useRunTiangong() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => knowledgeApi.runTiangong(id),
    onSuccess: () => invalidateKnowledge(qc),
  })
}

export function useTiangongSources(params: Record<string, unknown>, enabled = true) {
  return useQuery({
    queryKey: knowledgeQueryKeys.sources(params),
    enabled,
    queryFn: () => knowledgeApi.listTiangongSources(params),
  })
}

export function useCreateTiangongSource() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (payload: TiangongSourcePayload) => knowledgeApi.createTiangongSource(payload),
    onSuccess: () => invalidateKnowledge(qc),
  })
}

export function useUpdateTiangongSource() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; payload: Partial<TiangongSourcePayload> }) =>
      knowledgeApi.updateTiangongSource(input.id, input.payload),
    onSuccess: () => invalidateKnowledge(qc),
  })
}

export function useDeleteTiangongSource() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => knowledgeApi.removeTiangongSource(id),
    onSuccess: () => invalidateKnowledge(qc),
  })
}

export function useCheckTiangongSource() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => knowledgeApi.checkTiangongSource(id),
    onSuccess: () => invalidateKnowledge(qc),
  })
}

export function useTiangongProposals(params: Record<string, unknown>, enabled = true) {
  return useQuery({
    queryKey: knowledgeQueryKeys.proposals(params),
    enabled,
    queryFn: () => knowledgeApi.listTiangongProposals(params),
  })
}

export function useApproveTiangongProposal() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => knowledgeApi.approveTiangongProposal(id),
    onSuccess: () => invalidateKnowledge(qc),
  })
}

export function useRejectTiangongProposal() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; reason: string }) =>
      knowledgeApi.rejectTiangongProposal(input.id, input.reason),
    onSuccess: () => invalidateKnowledge(qc),
  })
}

export function useTiangongSearchSettings(enabled = true) {
  return useQuery({
    queryKey: knowledgeQueryKeys.searchSettings(),
    enabled,
    queryFn: () => knowledgeApi.tiangongSearchSettings(),
  })
}

export function useUpdateTiangongSearchSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (payload: Parameters<typeof knowledgeApi.updateTiangongSearchSettings>[0]) =>
      knowledgeApi.updateTiangongSearchSettings(payload),
    onSuccess: () => invalidateKnowledge(qc),
  })
}

export function useCheckSearxng() {
  return useMutation({ mutationFn: () => knowledgeApi.checkSearxng() })
}

export function useCheckBing() {
  return useMutation({ mutationFn: () => knowledgeApi.checkBing() })
}

export function useTiangongBindings(params: Record<string, unknown>, enabled = true) {
  return useQuery({
    queryKey: knowledgeQueryKeys.bindings(params),
    enabled,
    queryFn: () => knowledgeApi.listTiangongBindings(params),
  })
}

export function useScanTiangongCandidates() {
  return useMutation({ mutationFn: () => knowledgeApi.scanTiangongCandidates() })
}

export function useCreateTiangongBinding() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (payload: TiangongSearchBindingPayload) =>
      knowledgeApi.createTiangongBinding(payload),
    onSuccess: () => invalidateKnowledge(qc),
  })
}

export function useUpdateTiangongBinding() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; payload: Partial<TiangongSearchBindingPayload> }) =>
      knowledgeApi.updateTiangongBinding(input.id, input.payload),
    onSuccess: () => invalidateKnowledge(qc),
  })
}

export function useDeleteTiangongBinding() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => knowledgeApi.removeTiangongBinding(id),
    onSuccess: () => invalidateKnowledge(qc),
  })
}

export function useTestTiangongBinding() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; query: string }) =>
      knowledgeApi.testTiangongBinding(input.id, input.query),
    onSuccess: () => invalidateKnowledge(qc),
  })
}
