import { ApiError } from '@/api/request'

/**
 * 口令强度：与后端 AUTH_PASSWORD_VALIDATORS 保持同一口径（至少 8 位 / 非全数字 /
 * 非常见口令 / 不与用户名过于相似）。后端仍是最终判定，这里只是让用户在提交前
 * 就能看懂要求，而不是等到 400 再猜。
 */
export const PASSWORD_MIN_LENGTH = 8

const COMMON_PASSWORDS = new Set([
  'password',
  'password1',
  '12345678',
  '123456789',
  '11111111',
  '88888888',
  'qwerty123',
  'abc12345',
  'admin123',
  'admin1234',
])

export interface PasswordRule {
  label: string
  passed: (password: string, username?: string) => boolean
}

export const PASSWORD_RULES: PasswordRule[] = [
  {
    label: `至少 ${PASSWORD_MIN_LENGTH} 个字符`,
    passed: (p) => p.length >= PASSWORD_MIN_LENGTH,
  },
  {
    label: '不能全部是数字',
    passed: (p) => p.length > 0 && !/^\d+$/.test(p),
  },
  {
    label: '不能是常见弱口令',
    passed: (p) => p.length > 0 && !COMMON_PASSWORDS.has(p.toLowerCase()),
  },
  {
    label: '不能包含用户名',
    passed: (p, username) =>
      p.length > 0 && (!username?.trim() || !p.toLowerCase().includes(username.trim().toLowerCase())),
  },
]

/** 返回未满足的要求；空数组表示前端口径下合格 */
export function passwordProblems(password: string, username?: string): string[] {
  if (!password) return ['请填写密码']
  return PASSWORD_RULES.filter((r) => !r.passed(password, username)).map((r) => r.label)
}

const FIELD_LABELS: Record<string, string> = {
  password: '密码',
  new_password: '新密码',
  confirm_password: '确认密码',
  old_password: '原密码',
  username: '用户名',
  nickname: '昵称',
  email: '邮箱',
  phone: '手机号',
  department: '部门',
  position: '职位',
}

/** 后端把字段级错误压成了 str(dict)，这里抽出可读中文，避免直接弹 JSON */
function parseFieldErrors(detail: string): string | null {
  if (!detail.trim().startsWith('{')) return null
  const parts: string[] = []
  for (const match of detail.matchAll(/'([a-z_]+)'\s*:\s*\[([^\]]*)\]/g)) {
    const label = FIELD_LABELS[match[1]] ?? match[1]
    // DRF 的 repr 是 ErrorDetail(string='…', code='…')，优先只取 string 值，
    // 否则会把 code 也当成一条错误文案展示给用户。
    const byString = [...match[2].matchAll(/string=(['"])(.*?)\1/g)].map((m) => m[2])
    const raw = byString.length
      ? byString
      : [...match[2].matchAll(/(['"])(.*?)\1/g)].map((m) => m[2])
    const messages = raw.map((message) => translatePasswordMessage(message)).filter(Boolean)
    if (messages.length) parts.push(`${label}：${messages.join('；')}`)
  }
  if (parts.length) return parts.join('；')
  return (
    detail
      .replace(/[{}[\]']/g, '')
      .replace(/:\s*/g, '：')
      .replace(/,\s*/g, '；')
      .trim() || null
  )
}

/** Django 口令校验文案兜底（zh-hans 未命中时是英文） */
export function translatePasswordMessage(message: string): string {
  const lower = message.toLowerCase()
  if (lower.includes('too short') || lower.includes('at least')) {
    const match = lower.match(/at least (\d+)/)
    return `密码至少需要 ${match ? match[1] : PASSWORD_MIN_LENGTH} 位`
  }
  if (lower.includes('entirely numeric')) return '密码不能全部是数字'
  if (lower.includes('too common')) return '密码过于常见，请换一个'
  if (lower.includes('too similar')) return '密码不能与用户名或昵称过于相似'
  if (lower.includes('too long')) return '密码过长'
  return message.trim()
}

/** 统一的用户可读错误文案（API 失败、字段校验都适用） */
export function readableError(err: unknown): string {
  const detail = err instanceof ApiError ? err.detail : (err as Error)?.message
  if (!detail) return '操作失败，请稍后重试'
  return parseFieldErrors(detail) ?? translatePasswordMessage(detail)
}
