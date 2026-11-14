import { http } from './request'
import { APP_CONFIG, STORAGE_KEYS } from '@/config/app'
import { useUserStore } from '@/store/user'
import type { Paginated } from '@/types/common'
import type {
  MirrorRebuild,
  MirrorState,
  RiskAckPayload,
  Skill,
  SkillFileMeta,
  SkillFilesUploadInput,
  SkillListParams,
  SkillPayload,
  SkillReferencesResponse,
  SkillReviewPayload,
  SkillSummary,
  SkillUploadInput,
  SkillVersion,
  SkillVersionCompare,
  SkillVersionList,
} from '@/types/skill'

/** 附件下载同样带 JWT，走 blob 转存（与对话产物下载一致） */
async function downloadAuthedFile(url: string, name: string): Promise<void> {
  const token = useUserStore.getState().token ?? localStorage.getItem(STORAGE_KEYS.TOKEN)
  const res = await fetch(`${APP_CONFIG.apiBaseUrl}${url}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!res.ok) throw new Error('文件下载失败')
  const blob = await res.blob()
  const objUrl = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = objUrl
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(objUrl)
}

/** Django 端点一律带尾斜杠；写接口仅平台管理员（403 由全局拦截器给出文案） */
export const skillsApi = {
  list: (params: SkillListParams) => http.get<Paginated<Skill>>('/skills/', params),

  detail: (id: number) => http.get<Skill>(`/skills/${id}/`),

  /** C1 JSON 建档（粘贴正文） */
  create: (payload: SkillPayload) => http.post<Skill>('/skills/', payload),

  /** C1 multipart 建档：后端解析 SKILL.md frontmatter，name 缺失/冲突 400 */
  upload: (input: SkillUploadInput) => {
    const fd = new FormData()
    fd.append('file', input.file)
    if (input.name?.trim()) fd.append('name', input.name.trim())
    if (input.description?.trim()) fd.append('description', input.description.trim())
    if (input.scope_mode) fd.append('scope_mode', input.scope_mode)
    if (input.scope_include_sub_departments !== undefined) {
      fd.append('scope_include_sub_departments', String(input.scope_include_sub_departments))
    }
    input.applicable_department_ids?.forEach((id) => fd.append('applicable_department_ids', String(id)))
    if (input.rollout_percent !== undefined) fd.append('rollout_percent', String(input.rollout_percent))
    input.applicable_user_ids?.forEach((id) => fd.append('applicable_user_ids', String(id)))
    if (input.risk_ack) fd.append('risk_ack', 'true')
    if (input.risk_fingerprint) fd.append('risk_fingerprint', input.risk_fingerprint)
    return http.upload<Skill>('/skills/', fd)
  },

  /* ─── C1 技能包附件 ─── */

  listFiles: (id: number) =>
    http.get<{ files: SkillFileMeta[]; file_count: number; package_bytes: number; scan_warnings: string[] }>(
      `/skills/${id}/files/`,
    ),

  uploadFiles: (id: number, input: SkillFilesUploadInput) => {
    const fd = new FormData()
    input.files.forEach((file, index) => {
      fd.append('files', file)
      fd.append('paths', input.paths[index] || file.name)
    })
    if (input.risk_ack) fd.append('risk_ack', 'true')
    if (input.risk_fingerprint) fd.append('risk_fingerprint', input.risk_fingerprint)
    return http.upload<Skill>(`/skills/${id}/files/`, fd)
  },

  deleteFile: (id: number, fileId: number) => http.delete<Skill>(`/skills/${id}/files/${fileId}/`),

  downloadFile: (url: string, name: string) => downloadAuthedFile(url, name),

  /* ─── C4 版本历史 / 对比 / 回滚 ─── */

  versions: (id: number) => http.get<SkillVersionList>(`/skills/${id}/versions/`),

  version: (id: number, version: number) => http.get<SkillVersion>(`/skills/${id}/versions/${version}/`),

  compareVersions: (id: number, left: number, right: number) =>
    http.get<SkillVersionCompare>(`/skills/${id}/versions/compare/`, { left, right }),

  rollback: (id: number, version: number, ack?: RiskAckPayload) =>
    http.post<{ detail: string; new_version: number; files: number; skill: Skill }>(
      `/skills/${id}/versions/${version}/rollback/`,
      ack ?? {},
    ),

  /** C4 编辑（仅非内置）：改动 name/description/body 后回到待审核 */
  update: (id: number, payload: Partial<SkillPayload> & Partial<RiskAckPayload>) =>
    http.patch<Skill>(`/skills/${id}/`, payload),

  /** C6 删除：内置 400；被挂载 409（无强制删除，需先摘除或只停用） */
  remove: (id: number) => http.delete<void>(`/skills/${id}/`),

  approve: (id: number, payload: SkillReviewPayload & Partial<RiskAckPayload>) =>
    http.post<Skill>(`/skills/${id}/approve/`, payload),

  reject: (id: number, payload: SkillReviewPayload) =>
    http.post<Skill>(`/skills/${id}/reject/`, payload),

  disable: (id: number) => http.post<Skill>(`/skills/${id}/disable/`),

  /** 无审核通过版时 400：请编辑正文后提交审核 */
  enable: (id: number) => http.post<Skill>(`/skills/${id}/enable/`),

  references: (id: number) => http.get<SkillReferencesResponse>(`/skills/${id}/references/`),

  summary: () => http.get<SkillSummary>('/skills/summary/'),

  mirrorState: () => http.get<MirrorState>('/skills/mirror/'),

  mirrorRebuild: () => http.post<MirrorRebuild>('/skills/mirror/'),
}
