/** 后端 User 序列化字段（无 role，管理员只看 is_staff） */
export interface User {
  id: number
  username: string
  nickname: string
  email: string
  phone: string
  department: string
  position: string
  is_staff: boolean
  date_joined: string
  storage_used: number
}

export interface LoginPayload {
  username: string
  password: string
  remember?: boolean
}

/** POST /api/auth/login/ → {access, refresh, user} */
export interface LoginResult {
  access: string
  refresh: string
  user: User
}

/** POST /api/auth/refresh/ → {access, refresh?}；开启轮换时同时返回新 refresh */
export interface RefreshResult {
  access: string
  refresh?: string
}
