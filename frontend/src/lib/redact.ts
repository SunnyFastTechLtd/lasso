/**
 * 展示层脱敏：只在界面上对敏感字段做掩码，不改存储、不改接口返回。
 *
 * 覆盖两类常见泄漏面：
 * 1) 键值对（JSON / 表单 / query 风格）：password、secret、token、api_key、
 *    authorization、cookie、session、private_key、access_key、client_secret 等；
 * 2) 无键名的凭据：Bearer/Basic 令牌、sk-/AKIA/ghp_/xoxb- 前缀密钥、JWT、
 *    URL 查询串里的 token/key/secret/signature。
 *
 * 已是掩码的值（含 •）原样保留，重复调用不会叠加。
 */
export interface MaskResult {
  /** 掩码后的文本 */
  text: string
  /** 命中并替换的处数，0 表示无需脱敏 */
  hits: number
}

export const MASK = '••••••'

/** 命中即整值替换；长值保留末 4 位便于比对，但不还原原文 */
function maskValue(raw: string): string {
  if (!raw || raw.includes('•')) return raw
  const quote = raw[0] === '"' || raw[0] === "'" ? raw[0] : ''
  const inner = quote ? raw.slice(1, -1) : raw
  if (!inner) return raw
  const tail = inner.length >= 12 ? inner.slice(-4) : ''
  return `${quote}${MASK}${tail}${quote}`
}

const KEY_NAME =
  '(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|apikey|authorization|cookie|session|access[_-]?key|private[_-]?key|client[_-]?secret)'

/** 键值对：prefix 保留键名与分隔符，只替换值（支持带引号与裸值） */
const KEY_VALUE_RE = new RegExp(
  `("[A-Za-z0-9_.-]*${KEY_NAME}[A-Za-z0-9_.-]*"\\s*[:=]\\s*|\\b[A-Za-z0-9_.-]*${KEY_NAME}[A-Za-z0-9_.-]*\\s*[:=]\\s*)` +
    `("(?:[^"\\\\]|\\\\.)*"|'(?:[^'\\\\]|\\\\.)*'|[^\\s,;&}\\]<>]+)`,
  'gi',
)

const AUTH_SCHEME_RE = /\b(Bearer|Basic|Token)\s+[A-Za-z0-9\-._~+/]{8,}=*/gi
const KNOWN_KEY_RE = /\b(?:sk-ant-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{8,}|AKIA[0-9A-Z]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})/g
const JWT_RE = /\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}/g
const URL_QUERY_RE = /([?&](?:token|access_token|refresh_token|api_key|key|secret|password|sig|signature)=)([^&\s"'<>]+)/gi

export function maskSensitive(input: string): MaskResult {
  if (!input) return { text: input, hits: 0 }
  let hits = 0

  let out = input.replace(KEY_VALUE_RE, (_m, prefix: string, value: string) => {
    const masked = maskValue(value)
    if (masked === value) return _m
    hits += 1
    return prefix + masked
  })

  out = out.replace(AUTH_SCHEME_RE, (_m, scheme: string) => {
    hits += 1
    return `${scheme} ${MASK}`
  })
  out = out.replace(KNOWN_KEY_RE, () => {
    hits += 1
    return MASK
  })
  out = out.replace(JWT_RE, () => {
    hits += 1
    return MASK
  })
  out = out.replace(URL_QUERY_RE, (_m, prefix: string, value: string) => {
    const masked = maskValue(value)
    if (masked === value) return _m
    hits += 1
    return prefix + masked
  })

  return { text: out, hits }
}
