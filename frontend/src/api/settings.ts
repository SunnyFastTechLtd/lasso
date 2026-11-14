import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { http } from './request'
import type { Paginated } from '@/types/common'
import type { AuditListParams, AuditLogItem } from '@/types/audit'

/**
 * 系统设置（F1 模型接入 / F3 审计）本模块自己的 query key。
 * queryKeys.ts 属共享文件，本期不在其中新增。
 */
export const settingsKeys = {
  llmConfig: ['settings', 'llm-config'] as const,
  skillPackage: ['settings', 'skill-package'] as const,
  auditLogs: (params: AuditListParams) => ['settings', 'audit-logs', params] as const,
}

/** 决策 10：技能包附件类型由管理员维护，后端另有结构性上限 */
export interface SkillPackageConfig {
  allowed_extensions: string[]
  default_extensions: string[]
  denied_extensions: string[]
  max_attachment_bytes: number
  max_attachments: number
  max_package_bytes: number
  ruleset_version: string
}

/** check_health() 五态：连通 / 无法连接 / 密钥无效 / 未配置 / 其它异常 */
export type LlmHealthStatus = 'ok' | 'offline' | 'unauthorized' | 'not_configured' | 'error'

export interface LlmHealth {
  status: LlmHealthStatus
  base_url?: string
  /** status=ok 时返回 */
  current_model?: string
  /** 本次通过对话连通性验证的模型，ok 时后端据此写入可用模型列表 */
  verified_model?: string
  available_models?: string[]
  model_count?: number
  /** 非 ok 时返回失败原因原文 */
  error?: string
}

/** GET /api/system/settings/llm/ —— 密钥只回掩码，明文不回流前端 */
export interface LlmConfig {
  base_url: string
  api_key_masked: string
  model: string
  temperature: number
  max_tokens: number
  timeout: number
  /** 已通过健康检查的模型（智能体模型下拉的唯一候选） */
  verified_models?: string[]
  /** 模型配置唯一来源：数据库 */
  config_source?: 'database'
  /** 数据库必填项是否齐全 */
  configured?: boolean
  /** 缺失的必填项键名（configured=false 时） */
  missing_fields?: string[]
  /** 可操作提示（指向模型配置界面） */
  hint?: string
}

export interface LlmConfigPayload {
  base_url: string
  api_key: string
  model: string
  temperature: number
  max_tokens: number
  timeout: number
}

/** POST /llm/update/：先按候选配置健康检查，通过才落库；saved=false 即现网配置未变 */
export interface LlmUpdateResult {
  detail: string
  saved: boolean
  health: LlmHealth
}

/** Django 端点一律带尾斜杠；写接口仅平台管理员（403 由全局拦截器给出文案） */
export const settingsApi = {
  llmConfig: () => http.get<LlmConfig>('/system/settings/llm/'),

  updateLlmConfig: (payload: LlmConfigPayload) =>
    http.post<LlmUpdateResult>('/system/settings/llm/update/', payload),

  /** 用「已保存」的配置做一次连通探测，不含未保存的表单改动 */
  testLlmConfig: () => http.post<LlmHealth>('/system/settings/llm/test/'),

  resetLlmConfig: () => http.post<{ detail: string }>('/system/settings/llm/reset/'),

  skillPackage: () => http.get<SkillPackageConfig>('/system/settings/skill-package/'),

  updateSkillPackage: (allowed: string[]) =>
    http.post<{ detail: string; allowed_extensions: string[]; previous_extensions: string[] }>(
      '/system/settings/skill-package/update/',
      { allowed_extensions: allowed },
    ),

  auditLogs: (params: AuditListParams) =>
    http.get<Paginated<AuditLogItem>>('/auth/audit-logs/', { ...params }),
}

export function useLlmConfig(enabled = true) {
  return useQuery({
    queryKey: settingsKeys.llmConfig,
    enabled,
    queryFn: settingsApi.llmConfig,
  })
}

export function useUpdateLlmConfig() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (payload: LlmConfigPayload) => settingsApi.updateLlmConfig(payload),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: settingsKeys.llmConfig })
    },
  })
}

export function useTestLlmConfig() {
  return useMutation({ mutationFn: () => settingsApi.testLlmConfig() })
}

export function useResetLlmConfig() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => settingsApi.resetLlmConfig(),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: settingsKeys.llmConfig })
    },
  })
}

export function useSkillPackageConfig(enabled = true) {
  return useQuery({
    queryKey: settingsKeys.skillPackage,
    enabled,
    queryFn: settingsApi.skillPackage,
  })
}

export function useUpdateSkillPackage() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (allowed: string[]) => settingsApi.updateSkillPackage(allowed),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: settingsKeys.skillPackage })
    },
  })
}

export function useAuditLogs(params: AuditListParams) {
  return useQuery({
    queryKey: settingsKeys.auditLogs(params),
    queryFn: () => settingsApi.auditLogs(params),
  })
}
