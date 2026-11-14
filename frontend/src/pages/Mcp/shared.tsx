import { Badge, type BadgeTone } from '@/components/ui/Badge'
import { Button, IconButton } from '@/components/ui/Button'
import { Input } from '@/components/ui/Field'
import type { McpServer, McpStatus } from '@/types/mcp'

/** 状态机（PRD 4.3）→ 徽标色调；文案一律用后端 status_display */
const STATUS_TONE: Record<McpStatus, BadgeTone> = {
  unknown: 'neutral',
  healthy: 'success',
  error: 'error',
  disabled: 'neutral',
  egress_blocked: 'warning',
}

export function StatusBadge({ server }: { server: McpServer }) {
  const tone = STATUS_TONE[server.status] ?? 'neutral'
  return (
    <Badge tone={tone} dot title={server.last_error || undefined}>
      {server.status_display}
    </Badge>
  )
}

export function BuiltinBadge() {
  return (
    <Badge tone="primary" title="内置演示来源：不可删除，地址与命令不可改">
      内置
    </Badge>
  )
}

export function EgressBadge() {
  return (
    <Badge tone="warning" dot title="出网类来源，受全平台出网总开关约束">
      出网
    </Badge>
  )
}

// ─── 键值对编辑器（headers / secrets 共用） ───

export interface KvRow {
  key: string
  value: string
}

export const kvToRows = (record: Record<string, string> | undefined): KvRow[] =>
  Object.entries(record ?? {}).map(([key, value]) => ({ key, value }))

export const rowsToKv = (rows: KvRow[]): Record<string, string> => {
  const out: Record<string, string> = {}
  for (const row of rows) {
    const k = row.key.trim()
    if (k) out[k] = row.value
  }
  return out
}

interface KvEditorProps {
  rows: KvRow[]
  onChange: (next: KvRow[]) => void
  keyPlaceholder?: string
  valuePlaceholder?: string
  disabled?: boolean
  addLabel?: string
}

export function KvEditor({
  rows,
  onChange,
  keyPlaceholder = '键',
  valuePlaceholder = '值',
  disabled = false,
  addLabel = '添加一行',
}: KvEditorProps) {
  const patch = (index: number, next: Partial<KvRow>) =>
    onChange(rows.map((r, i) => (i === index ? { ...r, ...next } : r)))

  return (
    <div className="flex flex-col gap-[6px]">
      {rows.map((row, i) => (
        <div key={i} className="flex items-center gap-[6px]">
          <Input
            className="flex-1"
            value={row.key}
            placeholder={keyPlaceholder}
            disabled={disabled}
            onChange={(e) => patch(i, { key: e.target.value })}
          />
          <Input
            className="flex-[1.4]"
            value={row.value}
            placeholder={valuePlaceholder}
            disabled={disabled}
            onChange={(e) => patch(i, { value: e.target.value })}
          />
          <IconButton
            icon="X"
            label="删除该行"
            disabled={disabled}
            onClick={() => onChange(rows.filter((_, idx) => idx !== i))}
          />
        </div>
      ))}
      {!disabled && (
        <div>
          <Button quiet variant="ghost" icon="Plus" onClick={() => onChange([...rows, { key: '', value: '' }])}>
            {addLabel}
          </Button>
        </div>
      )}
      {rows.length === 0 && disabled && <span className="text-xs text-muted-foreground">（无）</span>}
    </div>
  )
}

/**
 * 后端全局异常层会把 DRF 400 字段错误抹平成 Python repr 字符串，
 * 形如 {'command': [ErrorDetail(string='启动命令未命中…', code='invalid')]}。
 * 此处从 repr 中提取 string='…' 的中文文案，避免把 repr 原样展示给用户。
 */
const PY_ERR_RE = /string='((?:[^'\\]|\\.)*)'/g

function decodePyString(raw: string): string {
  return raw.replace(/\\(['\\])/g, '$1')
}

function pyErrorTexts(text: string): string[] | null {
  const hits = [...text.matchAll(PY_ERR_RE)]
  if (hits.length === 0) return null
  return hits.map((m) => decodePyString(m[1]))
}

/** 400 体提取：detail 或各字段的 DRF 错误列表（可能被全局异常层抹平为字符串） */
export function extractFieldErrors(body: Record<string, unknown> | null): string[] {
  if (!body) return []
  const out: string[] = []
  for (const [field, val] of Object.entries(body)) {
    if (field === 'detail' || field === 'code') continue
    if (Array.isArray(val)) {
      for (const v of val) {
        const s = String(v)
        const py = pyErrorTexts(s)
        out.push(...(py ?? [s]))
      }
    } else if (typeof val === 'string') {
      const py = pyErrorTexts(val)
      out.push(...(py ?? [`${field}: ${val}`]))
    }
  }
  if (typeof body.detail === 'string') {
    const py = pyErrorTexts(body.detail)
    if (py) out.unshift(...py)
    else out.unshift(body.detail)
  }
  return out
}
