import { Badge, type BadgeTone } from '@/components/ui/Badge'
import type { Skill, SkillRiskHit, SkillStatus } from '@/types/skill'

/** 决策 10：422 风险确认挑战（命中规则 + 指纹），非 422 返回 null */
export interface RiskChallenge {
  hits: SkillRiskHit[]
  fingerprint: string
  rulesetVersion: string
  code: string
}

export function extractRiskChallenge(err: unknown): RiskChallenge | null {
  const e = err as { status?: number; body?: Record<string, unknown> | null }
  if (e?.status !== 422) return null
  const body = e.body ?? {}
  const hits = Array.isArray(body.hits) ? (body.hits as SkillRiskHit[]) : []
  const fingerprint = typeof body.fingerprint === 'string' ? body.fingerprint : ''
  if (!hits.length || !fingerprint) return null
  return {
    hits,
    fingerprint,
    rulesetVersion: typeof body.ruleset_version === 'string' ? body.ruleset_version : '',
    code: typeof body.code === 'string' ? body.code : 'risk_ack_required',
  }
}

/** 五态状态机（PRD 4.4/C2–C4）→ 徽标色调；文案一律用后端 status_display */
const STATUS_TONE: Record<SkillStatus, BadgeTone> = {
  invalid: 'error',
  pending: 'warning',
  active: 'success',
  disabled: 'neutral',
  builtin: 'primary',
}

export function SkillStatusBadge({ skill }: { skill: Skill }) {
  return (
    <Badge tone={STATUS_TONE[skill.status] ?? 'neutral'} dot>
      {skill.status_display}
    </Badge>
  )
}

/** 编辑稿在审：旧生效版本仍在运行时（C4 口径） */
export function LiveBadge() {
  return (
    <Badge tone="primary" title="存在待审核的编辑稿，当前生效的仍是上一次通过审核的正文">
      编辑在审
    </Badge>
  )
}

export function ChangedBadge() {
  return <Badge tone="warning" title="当前正文与审核通过版不一致">正文已改动</Badge>
}

export { extractFieldErrors } from '@/pages/Mcp/shared'
