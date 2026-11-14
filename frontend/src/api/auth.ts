import { http } from './request'
import { useUserStore } from '@/store/user'
import type { LoginPayload, LoginResult, User } from '@/types/auth'

/** 本人可编辑的资料字段；department/username/is_staff 均为只读 */
export interface ProfilePayload {
  nickname?: string
  email?: string
  phone?: string
  position?: string
}

export const authApi = {
  /** POST /api/auth/login/ → {access, refresh, user} */
  login: (payload: LoginPayload) => http.post<LoginResult>('/auth/login/', payload),

  /** 拉取当前用户，用于刷新页面后校准权限 */
  profile: () => http.get<User>('/auth/profile/'),

  /** PATCH /api/auth/profile/：部门等只读字段不会因提交而改变 */
  updateProfile: (payload: ProfilePayload) => http.patch<User>('/auth/profile/', payload),

  logout: (refresh?: string) =>
    http.post<{ detail: string }>('/auth/logout/', refresh ? { refresh } : undefined),

  /**
   * 修改本人密码：后端会吊销该用户全部 refresh，再为当前设备签发新的一对 token，
   * 因此这里必须把新 token 写回 store，否则 access 到期后会被自己的改密踢下线。
   */
  changePassword: (payload: { old_password: string; new_password: string; confirm_password: string }) =>
    http.post<{ detail: string; access: string; refresh: string }>('/auth/change-password/', payload),
}

/** 登录成功后写入 store */
export async function signIn(payload: LoginPayload): Promise<LoginResult> {
  const result = await authApi.login(payload)
  useUserStore.getState().setAuth(result.access, result.refresh, result.user)
  return result
}

/** 登出：把当前 refresh 交给后端拉黑；后端失败也要清本地会话 */
export async function signOut(): Promise<void> {
  const refresh = useUserStore.getState().refresh ?? undefined
  try {
    await authApi.logout(refresh)
  } catch {
    /* 忽略：本地清理优先 */
  }
  useUserStore.getState().logout()
}
