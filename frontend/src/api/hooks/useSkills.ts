import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { skillsApi } from '@/api/skills'
import { queryKeys } from '@/api/queryKeys'
import type {
  RiskAckPayload,
  SkillFilesUploadInput,
  SkillListParams,
  SkillPayload,
  SkillReviewPayload,
  SkillUploadInput,
} from '@/types/skill'

/** 任意写操作后统一刷新 skills 域缓存 */
function invalidateSkills(qc: QueryClient) {
  void qc.invalidateQueries({ queryKey: ['skills'] })
}

export function useSkills(params: SkillListParams) {
  return useQuery({
    queryKey: queryKeys.skills.list(params),
    queryFn: () => skillsApi.list(params),
  })
}

/** C3 队列与各状态计数（列表页顶部指标卡 + Seg 计数共用） */
export function useSkillSummary(enabled = true) {
  return useQuery({
    queryKey: queryKeys.skills.summary,
    enabled,
    staleTime: 15_000,
    queryFn: () => skillsApi.summary(),
  })
}

export function useSkill(id: number | null) {
  return useQuery({
    queryKey: queryKeys.skills.detail(id ?? 0),
    enabled: id !== null,
    queryFn: () => skillsApi.detail(id as number),
  })
}

export function useSkillReferences(id: number | null, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.skills.references(id ?? 0),
    enabled: id !== null && enabled,
    queryFn: () => skillsApi.references(id as number),
  })
}

/** C1 建档：粘贴正文走 JSON，上传文件走 multipart */
export function useCreateSkill() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { mode: 'json'; payload: SkillPayload } | { mode: 'upload'; input: SkillUploadInput }) =>
      input.mode === 'json' ? skillsApi.create(input.payload) : skillsApi.upload(input.input),
    onSuccess: () => invalidateSkills(qc),
  })
}

export function useUpdateSkill() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; payload: Partial<SkillPayload> }) =>
      skillsApi.update(input.id, input.payload),
    onSuccess: () => invalidateSkills(qc),
  })
}

export function useApproveSkill() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; payload: SkillReviewPayload & Partial<RiskAckPayload> }) =>
      skillsApi.approve(input.id, input.payload),
    onSuccess: () => invalidateSkills(qc),
  })
}

export function useRejectSkill() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; payload: SkillReviewPayload }) =>
      skillsApi.reject(input.id, input.payload),
    onSuccess: () => invalidateSkills(qc),
  })
}

export function useSetSkillEnabled() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; enabled: boolean }) =>
      input.enabled ? skillsApi.enable(input.id) : skillsApi.disable(input.id),
    onSuccess: () => invalidateSkills(qc),
  })
}

export function useDeleteSkill() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => skillsApi.remove(id),
    onSuccess: () => invalidateSkills(qc),
  })
}

/* ─── C1 附件 ─── */

export function useSkillFiles(id: number | null, enabled = true) {
  return useQuery({
    queryKey: ['skills', 'files', id ?? 0],
    enabled: id !== null && enabled,
    queryFn: () => skillsApi.listFiles(id as number),
  })
}

export function useUploadSkillFiles() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; payload: SkillFilesUploadInput }) =>
      skillsApi.uploadFiles(input.id, input.payload),
    onSuccess: () => invalidateSkills(qc),
  })
}

export function useDeleteSkillFile() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; fileId: number }) =>
      skillsApi.deleteFile(input.id, input.fileId),
    onSuccess: () => invalidateSkills(qc),
  })
}

/* ─── C4 版本 ─── */

export function useSkillVersions(id: number | null, enabled = true) {
  return useQuery({
    queryKey: ['skills', 'versions', id ?? 0],
    enabled: id !== null && enabled,
    queryFn: () => skillsApi.versions(id as number),
  })
}

export function useSkillVersion(id: number | null, version: number | null) {
  return useQuery({
    queryKey: ['skills', 'version', id ?? 0, version ?? 0],
    enabled: id !== null && version !== null,
    queryFn: () => skillsApi.version(id as number, version as number),
  })
}

export function useCompareSkillVersions(
  id: number | null,
  left: number | null,
  right: number | null,
) {
  return useQuery({
    queryKey: ['skills', 'compare', id ?? 0, left ?? 0, right ?? 0],
    enabled: id !== null && left !== null && right !== null,
    queryFn: () => skillsApi.compareVersions(id as number, left as number, right as number),
  })
}

export function useRollbackSkill() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; version: number; ack?: RiskAckPayload }) =>
      skillsApi.rollback(input.id, input.version, input.ack),
    onSuccess: () => invalidateSkills(qc),
  })
}

/** 运行时镜像可见性 + 全量重建（仅管理员） */
export function useMirrorState(enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.skills.mirror,
    enabled,
    queryFn: () => skillsApi.mirrorState(),
  })
}

export function useRebuildMirror() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => skillsApi.mirrorRebuild(),
    onSuccess: () => invalidateSkills(qc),
  })
}
