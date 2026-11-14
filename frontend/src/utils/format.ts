const pad = (n: number) => String(n).padStart(2, '0')

/** 2026-11-02 09:32:46（设计稿密集表时间戳格式） */
export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

/** MM-DD HH:mm —— 表格里省年份 */
export function fmtShortTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 相对时间：刚刚 / n 分钟前 / n 小时前 / 昨天 / 具体日期 */
export function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return '从未'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '从未'
  const diff = Date.now() - d.getTime()
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  if (diff < 172_800_000) return '昨天'
  return fmtShortTime(iso)
}

const numberFormatter = new Intl.NumberFormat('zh-CN')

export function fmtNumber(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—'
  return numberFormatter.format(n)
}

/** 存储量：后端 storage_used 以字节计 */
export function fmtBytes(bytes: number | null | undefined): string {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = bytes
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i += 1
  }
  return `${value >= 10 ? Math.round(value) : value.toFixed(1)} ${units[i]}`
}

/** 头像占位取字：中文名取首字，英文名取首字母 */
export function initials(name: string | null | undefined, fallback = '用户'): string {
  const text = (name || fallback).trim()
  if (!text) return '用户'
  if (/^[A-Za-z]/.test(text)) {
    const parts = text.split(/[\s._-]+/).filter(Boolean)
    return parts
      .slice(0, 2)
      .map((p) => p[0]!.toUpperCase())
      .join('')
  }
  return text.slice(0, 1)
}
