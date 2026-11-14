import { useMemo, useState, type ReactNode } from 'react'
import { sha256 } from 'js-sha256'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { CodeBlock } from '@/components/ui/CodeBlock'
import { Icon } from '@/components/ui/Icon'
import { Textarea } from '@/components/ui/Field'
import { Drawer } from '@/components/ui/Overlay'
import { Panel, SectionLabel } from '@/components/ui/Panel'
import { useSkill, useSkillReferences, useApproveSkill, useRejectSkill, useSetSkillEnabled } from '@/api/hooks/useSkills'
import { toast } from '@/store/toast'
import { useUserStore } from '@/store/user'
import { fmtDateTime, fmtRelative } from '@/utils/format'
import type { Skill } from '@/types/skill'
import { ChangedBadge, LiveBadge, SkillStatusBadge, extractRiskChallenge, type RiskChallenge } from './shared'
import { RiskConfirmModal } from './RiskConfirmModal'
import { SkillFiles } from './SkillFiles'
import { SkillVersionsModal } from './SkillVersionsModal'

interface Props {
  skillId: number
  /** 列表行数据：详情返回前先用于渲染 */
  fallback: Skill | null
  onClose: () => void
  onEdit: (skill: Skill) => void
  onDelete: (skill: Skill) => void
}

function Meta({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="list-row">
      <span className="hk w-[92px] flex-none">{k}</span>
      <span className="min-w-0 flex-1 text-xs text-sidebar-foreground">{children}</span>
    </div>
  )
}

/** C2/C3/C6 明细抽屉：正文全文 + 审核闸门（看正文→勾选→指纹确认）+ 启停 */
export function SkillDrawer({ skillId, fallback, onClose, onEdit, onDelete }: Props) {
  const isAdmin = useUserStore((s) => s.user?.is_staff ?? false)
  const { data: fresh } = useSkill(skillId)
  const skill = fresh ?? fallback

  const [bodyOpen, setBodyOpen] = useState(false)
  const [approvedOpen, setApprovedOpen] = useState(false)
  const [ack, setAck] = useState(false)
  const [reason, setReason] = useState('')
  const [refsOpen, setRefsOpen] = useState(false)
  const [versionsOpen, setVersionsOpen] = useState(false)
  const [challenge, setChallenge] = useState<RiskChallenge | null>(null)

  const approve = useApproveSkill()
  const reject = useRejectSkill()
  const setActive = useSetSkillEnabled()
  const { data: refs } = useSkillReferences(skillId, refsOpen)

  const currentBody = skill?.body ?? ''
  const bodyHash = useMemo(() => sha256(currentBody), [currentBody])
  const reviewable = isAdmin && skill?.status === 'pending'
  const busy = approve.isPending || reject.isPending

  async function runApprove(extra?: { risk_ack: true; risk_fingerprint: string }) {
    try {
      const result = await approve.mutateAsync({
        id: skillId,
        payload: { body_ack: true, body_hash: bodyHash, ...extra },
      })
      toast.success(
        `已通过审核：「${skill?.name ?? ''}」生成 v${result.current_version}，对新会话生效`,
      )
      setAck(false)
      setReason('')
      setChallenge(null)
    } catch (err) {
      const next = extractRiskChallenge(err)
      if (next && !extra) {
        setAck(false)
        setChallenge(next)
        return
      }
      toast.error((err as Error).message)
      setAck(false)
      setChallenge(null)
    }
  }

  async function runReject() {
    const trimmed = reason.trim()
    if (!trimmed) {
      toast.warning('驳回必须填写理由，创建者需要看到原因')
      return
    }
    try {
      await reject.mutateAsync({ id: skillId, payload: { body_ack: true, body_hash: bodyHash, reason: trimmed } })
      toast.success(`已驳回「${skill?.name ?? ''}」`)
      setAck(false)
      setReason('')
    } catch (err) {
      toast.error((err as Error).message)
      setAck(false)
    }
  }

  if (!skill) {
    return (
      <Drawer open onClose={onClose} title="技能明细" meta="读取中…">
        <div className="empty-state">
          <Icon name="LoaderCircle" size={18} className="animate-spin" />
        </div>
      </Drawer>
    )
  }

  const canEnable = skill.status === 'disabled'
  const canDisable = skill.status === 'active' || skill.status === 'builtin'

  return (
    <Drawer
      open
      onClose={onClose}
      title={
        <span className="flex flex-wrap items-center gap-[8px]">
          {skill.name}
          <SkillStatusBadge skill={skill} />
          {skill.live && <LiveBadge />}
          {skill.status === 'pending' && skill.body_changed && <ChangedBadge />}
        </span>
      }
      meta={`标识 ${skill.slug} · ${skill.source_display} · 正文 ${skill.char_count} 字${
        skill.reviewed_by_name ? ` · ${skill.reviewed_by_name} 审核于 ${fmtDateTime(skill.reviewed_at ?? '')}` : ''
      }`}
      footer={
        isAdmin ? (
          <>
            {skill.editable && (
              <Button variant="ghost" icon="Pencil" onClick={() => onEdit(skill)}>
                编辑
              </Button>
            )}
            <Button
              variant="ghost"
              icon="History"
              onClick={() => setVersionsOpen(true)}
              title={skill.current_version ? `当前 v${skill.current_version}` : '尚无版本'}
            >
              版本历史
            </Button>
            <span className="flex-1" />
            {canEnable && (
              <Button
                variant="default"
                icon="Power"
                loading={setActive.isPending}
                title={skill.approved_body ? '恢复启用（沿用审核通过版）' : '尚无审核通过的生效版本'}
                onClick={async () => {
                  try {
                    await setActive.mutateAsync({ id: skill.id, enabled: true })
                    toast.success(`已恢复启用「${skill.name}」`)
                  } catch (err) {
                    toast.error((err as Error).message)
                  }
                }}
              >
                恢复启用
              </Button>
            )}
            {canDisable && (
              <Button
                variant="default"
                icon="PowerOff"
                loading={setActive.isPending}
                onClick={async () => {
                  try {
                    await setActive.mutateAsync({ id: skill.id, enabled: false })
                    toast.success(`已停用「${skill.name}」：即时对新会话生效，挂载保留视为未挂载`)
                  } catch (err) {
                    toast.error((err as Error).message)
                  }
                }}
              >
                停用
              </Button>
            )}
            {skill.editable && (
              <Button variant="danger" icon="Trash2" onClick={() => onDelete(skill)}>
                删除
              </Button>
            )}
          </>
        ) : (
          <span className="field-hint">只读视图：上传、审核、启停与删除仅平台管理员可操作</span>
        )
      }
    >
      <div className="col-stack">
        {/* 风险扫描提示（C2 #7：可疑项仅提示、不阻断保存，审核时一并核对） */}
        {skill.scan_warnings?.length > 0 && (
          <Panel title="风险扫描提示（仅提示，未阻断保存）" icon="TriangleAlert" padded>
            <div className="col-stack gap-[6px]">
              {skill.scan_warnings.map((w, i) => (
                <div key={i} className="list-row">
                  <Icon name="TriangleAlert" size={13} className="text-state-warning" />
                  <span className="text-xs leading-relaxed text-sidebar-foreground">{w}</span>
                </div>
              ))}
            </div>
            <div className="field-hint mt-[6px]">命中项不阻断保存；请审核时确认这些描述是否属于合理业务需要。</div>
          </Panel>
        )}

        {/* 校验问题（C2：逐条原因） */}
        {(skill.validation_errors.length > 0 || skill.reject_reason) && (
          <Panel title="校验与驳回" icon="TriangleAlert" padded>
            <div className="col-stack gap-[6px]">
              {skill.reject_reason && (
                <div className="rounded-md border border-state-error-border bg-state-error-soft px-[10px] py-[8px] text-xs leading-relaxed text-state-error">
                  驳回理由：{skill.reject_reason}
                </div>
              )}
              {skill.validation_errors.map((e, i) => (
                <div key={i} className="list-row">
                  <Icon name="CircleX" size={13} className="text-state-error" />
                  <span className="text-xs leading-relaxed text-sidebar-foreground">{e}</span>
                </div>
              ))}
            </div>
          </Panel>
        )}

        {/* 规范正文 + 审核闸门（C3） */}
        <Panel
          title="规范正文"
          icon="FileText"
          meta={`${skill.char_count} 字${skill.body_changed && skill.approved_body ? ' · 与审核通过版不一致' : ''}`}
          actions={
            <Button quiet variant="ghost" icon={bodyOpen ? 'ChevronDown' : 'ChevronRight'} onClick={() => setBodyOpen((v) => !v)}>
              {bodyOpen ? '收起正文' : '展开正文'}
            </Button>
          }
          padded={false}
        >
          {!bodyOpen ? (
            <div className="px-[12px] pb-[10px] text-xs leading-[1.6] text-muted-foreground">
              {skill.has_body ? '正文默认折叠，审核前需展开全文阅读。' : '尚无正文：请编辑后提交审核。'}
            </div>
          ) : (
            <div className="px-[12px] pb-[10px]">
              <CodeBlock code={skill.body ?? '（空）'} maxHeight={420} />
            </div>
          )}
          {skill.body_changed && skill.approved_body && (
            <div className="border-t px-[12px] py-[8px]">
              <button
                type="button"
                className="flex items-center gap-[4px] text-xs text-muted-foreground hover:text-sidebar-foreground"
                onClick={() => setApprovedOpen((v) => !v)}
              >
                <Icon name={approvedOpen ? 'ChevronDown' : 'ChevronRight'} size={12} />
                查看当前生效版本（{skill.approved_body.length} 字）
              </button>
              {approvedOpen && (
                <div className="mt-[6px]">
                  <CodeBlock code={skill.approved_body} maxHeight={300} />
                </div>
              )}
            </div>
          )}
        </Panel>

        <SkillFiles skill={skill} editable={isAdmin && skill.editable} />

        {reviewable && (
          <Panel title="审核（C3：确认已读正文后启用）" icon="ShieldCheck" padded>
            <div className="col-stack gap-[8px]">
              <label className="flex cursor-pointer items-start gap-[6px] text-xs leading-relaxed text-sidebar-foreground">
                <input
                  type="checkbox"
                  className="mt-[2px]"
                  disabled={!bodyOpen || !skill.has_body || busy}
                  checked={ack}
                  onChange={(e) => setAck(e.target.checked)}
                />
                <span>
                  我已展开并通读全文，确认无越权指令与危险工具要求
                  <span className="mono block text-muted-foreground">body_hash {bodyHash.slice(0, 16)}…</span>
                </span>
              </label>
              {!bodyOpen && <div className="field-hint">须先展开正文才能勾选确认（防盲签）</div>}
              <Field reason={reason} setReason={setReason} />
              <div className="flex items-center gap-[8px]">
                <Button variant="primary" icon="Check" loading={approve.isPending} disabled={!ack} onClick={() => void runApprove()}>
                  通过并启用
                </Button>
                <Button
                  variant="danger"
                  icon="X"
                  loading={reject.isPending}
                  disabled={!ack || !reason.trim()}
                  onClick={runReject}
                >
                  驳回
                </Button>
                <span className="field-hint">正文在审核期间被改动会导致确认失效，需重新阅读正文</span>
              </div>
            </div>
          </Panel>
        )}

        {/* 台账信息 */}
        <Panel title="基本信息" icon="Info" padded>
          <div className="col-stack gap-[2px]">
            <Meta k="适用说明">{skill.description || <span className="text-muted-foreground">（缺失）</span>}</Meta>
            <Meta k="来源">{skill.source_display}{skill.entry_file && <span className="mono text-xs text-muted-foreground"> · {skill.entry_file}</span>}</Meta>
            <Meta k="可见范围">
              {skill.visibility === 'department'
                ? `指定部门：${skill.allowed_departments.join('、') || '—'}`
                : skill.visibility_display}
            </Meta>
            <Meta k="生效范围">
              {skill.scope_mode === 'departments'
                ? `指定部门：${skill.applicable_departments.join('、') || '—'}${skill.scope_include_sub_departments ? '（含子部门）' : ''}`
                : '全员'}
              <span className="ml-[6px] text-xs text-muted-foreground">仅影响运行时注入，不影响本次可见性</span>
            </Meta>
            <Meta k="灰度比例">
              {skill.rollout_percent >= 100
                ? '全量'
                : `${skill.rollout_percent}%（基础范围内按用户稳定分桶，上调只增不减）`}
            </Meta>
            <Meta k="定向用户">
              {skill.applicable_users.length > 0
                ? `${skill.applicable_users.map((u) => u.username).join('、')}（命中即生效，不受部门与比例限制）`
                : '无'}
            </Meta>
            <Meta k="高危确认">
              {skill.risk_ack ? (
                <span className="text-xs">
                  {skill.risk_ack.by || '—'} 于 {fmtDateTime(skill.risk_ack.at)} 确认放行 {skill.risk_ack.rules.length} 条规则
                  <span className="ml-[4px] text-muted-foreground">
                    规则集 <span className="tnum">{skill.risk_ack.ruleset_version}</span>
                  </span>
                </span>
              ) : (
                <span className="text-muted-foreground">无</span>
              )}
            </Meta>
            <Meta k="依赖工具">
              {skill.requires.length > 0 ? (
                <div className="flex flex-wrap gap-[4px]">
                  {skill.requires.map((r) => (
                    <span key={r} className="chip mono">{r}</span>
                  ))}
                </div>
              ) : (
                '无'
              )}
            </Meta>
            <Meta k="运行时镜像">
              {skill.mirror_path ? (
                <span className="mono break-all text-xs">{skill.mirror_path}</span>
              ) : (
                <span className="text-muted-foreground">未进运行时（审核启用或恢复后自动同步）</span>
              )}
            </Meta>
            <Meta k="装配状态">
              {skill.mountable ? (
                <Badge tone="success" dot>可挂载</Badge>
              ) : (
                <Badge tone="neutral">不可挂载</Badge>
              )}
              <span className="text-xs text-muted-foreground"> 被 {skill.agent_count} 个智能体挂载</span>
            </Meta>
            <Meta k="时间">
              <span className="text-xs">
                创建 {fmtDateTime(skill.created_at)} · 更新 {fmtRelative(skill.updated_at)}
              </span>
            </Meta>
          </div>
        </Panel>

        {/* 引用检查（C6） */}
        <Panel
          title="挂载引用"
          icon="Bot"
          meta={`挂载该技能的智能体 ${skill.agent_count} 个`}
          actions={
            <Button quiet variant="ghost" onClick={() => setRefsOpen((v) => !v)}>
              {refsOpen ? '收起清单' : '检查引用'}
            </Button>
          }
          padded
        >
          {!refsOpen ? (
            <div className="field-hint">被挂载时不可直接删除；停用不破坏挂载关系（视为未挂载）。</div>
          ) : !refs ? (
            <div className="text-xs text-muted-foreground">读取引用…</div>
          ) : refs.references.length === 0 ? (
            <div className="text-xs text-muted-foreground">无智能体挂载，可直接删除</div>
          ) : (
            <div className="col-stack gap-[4px]">
              {refs.references.map((r) => (
                <div key={r.agent_id} className="list-row">
                  <span className="row-main min-w-0">
                    <span className="row-title">{r.agent_name}</span>
                    <span className="row-sub truncate">
                      <span className="mono">{r.agent_slug}</span> · v
                      <span className="tnum">{r.published_version ?? '—'}</span> · 挂载于{' '}
                      <span className="tnum">{fmtRelative(r.mounted_at)}</span>
                    </span>
                  </span>
                  <Badge tone={r.agent_status === 'published' ? 'success' : 'neutral'}>{r.agent_status_display}</Badge>
                </div>
              ))}
            </div>
          )}
        </Panel>

        <SkillVersionsModal
          open={versionsOpen}
          skillId={skill.id}
          skillName={skill.name}
          currentVersion={skill.current_version}
          editable={isAdmin && skill.editable}
          onClose={() => setVersionsOpen(false)}
        />

        <RiskConfirmModal
          challenge={challenge}
          title="正文或附件命中高危规则"
          busy={approve.isPending}
          onCancel={() => setChallenge(null)}
          onConfirm={() =>
            void runApprove({ risk_ack: true, risk_fingerprint: challenge?.fingerprint ?? '' })
          }
        />
      </div>
    </Drawer>
  )
}

/** 驳回理由输入（独立小组件保持抽屉主体可读） */
function Field({ reason, setReason }: { reason: string; setReason: (v: string) => void }) {
  return (
    <div>
      <SectionLabel>驳回理由（驳回必填，≤200 字）</SectionLabel>
      <Textarea
        className="mt-[6px]"
        rows={2}
        maxLength={200}
        value={reason}
        placeholder="如：正文含未申报的出网命令，请删除第 3 节后重新提交"
        onChange={(e) => setReason(e.target.value)}
      />
    </div>
  )
}
