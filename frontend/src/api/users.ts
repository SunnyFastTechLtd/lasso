import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { http } from './request'
import { queryKeys } from './queryKeys'
import type { Paginated } from '@/types/common'

/** 管理员视图下的用户（后端 AdminUserSerializer） */
export interface AdminUser {
  id: number
  username: string
  nickname: string
  email: string
  phone: string
  department: string
  position: string
  is_staff: boolean
  is_superuser: boolean
  is_active: boolean
  /** 是否为系统中最后一个具备管理权限的有效账号（后端计算） */
  is_last_admin: boolean
  /** 软删除标记：删除后仅保留用于追溯，不能登录 */
  is_deleted: boolean
  deleted_at: string | null
  deleted_by_name: string
  last_login: string | null
  date_joined: string
}

/** 一处活跃登录（后端基于 OutstandingToken，无最后使用时间字段） */
export interface UserSession {
  id: number
  jti: string
  created_at: string
  expires_at: string
  /** 后端未记录使用时间，恒为 null；界面需明确说明而不是展示假数据 */
  last_used: string | null
  ip: string | null
  user_agent: string
  source_action: 'login_success' | 'password_change' | null
  source_note: string
}

export interface UserSessionsResult {
  count: number
  results: UserSession[]
  last_used_supported: boolean
  last_used_note: string
  correlation_note: string
}

export interface AdminUserPayload {
  username?: string
  password?: string
  nickname?: string
  email?: string
  phone?: string
  department?: string
  position?: string
  is_staff?: boolean
  is_active?: boolean
}

export interface AdminUserListParams extends Record<string, unknown> {
  page: number
  page_size: number
  search?: string
  /** true 时只返回已软删除、保留用于追溯的账号 */
  deleted?: boolean
}

export const usersApi = {
  list: (params: AdminUserListParams) => http.get<Paginated<AdminUser>>('/auth/manage/', params),
  create: (payload: AdminUserPayload) => http.post<AdminUser>('/auth/manage/', payload),
  update: (id: number, payload: AdminUserPayload) =>
    http.patch<AdminUser>(`/auth/manage/${id}/`, payload),
  remove: (id: number) => http.delete<void>(`/auth/manage/${id}/`),
  sessions: (id: number) => http.get<UserSessionsResult>(`/auth/manage/${id}/sessions/`),
  revokeSessions: (id: number) =>
    http.post<{ revoked: number; detail: string }>(`/auth/manage/${id}/revoke-sessions/`),
}

export function useAdminUsers(params: AdminUserListParams, enabled = true) {
  return useQuery({
    queryKey: queryKeys.users.list(params),
    queryFn: () => usersApi.list(params),
    placeholderData: (prev) => prev,
    enabled,
  })
}

function useInvalidateUsers() {
  const queryClient = useQueryClient()
  return () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.users.root }),
      queryClient.invalidateQueries({ queryKey: queryKeys.departments }),
    ])
}

export function useCreateUser() {
  const invalidate = useInvalidateUsers()
  return useMutation({ mutationFn: usersApi.create, onSuccess: invalidate })
}

export function useUpdateUser() {
  const invalidate = useInvalidateUsers()
  return useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: AdminUserPayload }) =>
      usersApi.update(id, payload),
    onSuccess: invalidate,
  })
}

export function useDeleteUser() {
  const invalidate = useInvalidateUsers()
  return useMutation({ mutationFn: usersApi.remove, onSuccess: invalidate })
}

export function useUserSessions(id: number | null) {
  return useQuery({
    queryKey: queryKeys.users.sessions(id ?? 0),
    queryFn: () => usersApi.sessions(id as number),
    enabled: id !== null,
  })
}

export function useRevokeUserSessions() {
  const invalidate = useInvalidateUsers()
  return useMutation({ mutationFn: usersApi.revokeSessions, onSuccess: invalidate })
}
