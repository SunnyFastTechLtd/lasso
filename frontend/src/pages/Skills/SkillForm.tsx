import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/Button'
import { Field, Input, Select, Textarea } from '@/components/ui/Field'
import { Icon } from '@/components/ui/Icon'
import { Panel } from '@/components/ui/Panel'
import { Seg } from '@/components/ui/Seg'
import { Drawer } from '@/components/ui/Overlay'
import { useCreateSkill, useUpdateSkill } from '@/api/hooks/useSkills'
import { useDepartments } from '@/api/departments'
import { useAdminUsers } from '@/api/users'
import { toast } from '@/store/toast'
import type { RiskAckPayload, Skill, SkillScope, SkillVisibility } from '@/types/skill'
import { extractFieldErrors, extractRiskChallenge, type RiskChallenge } from './shared'
import { RiskConfirmModal } from './RiskConfirmModal'

const VISIBILITIES: { value: SkillVisibility; label: string }[] = [
  { value: 'admin_only', label: '仅管理端' },
  { value: 'private', label: '仅自己' },
  { value: 'department', label: '指定部门' },
  { value: 'public', label: '全企业' },
]

interface Props {
  open: boolean
  /** null＝新增建档 */
  skill: Skill | null
  bodyLimit: number
  onClose: () => void
}

/** C1 建档 + C4 编辑：粘贴正文或上传 SKILL.md；编辑 name/描述/正文会回到待审核 */
export function SkillForm({ open, skill, bodyLimit, onClose }: Props) {
  const isEdit = skill !== null
  const [mode, setMode] = useState<'paste' | 'upload'>('paste')
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [body, setBody] = useState('')
  const [requiresText, setRequiresText] = useState('')
  const [visibility, setVisibility] = useState<SkillVisibility>('admin_only')
  const [deptIds, setDeptIds] = useState<number[]>([])
  const [scopeMode, setScopeMode] = useState<SkillScope>('all')
  const [scopeSub, setScopeSub] = useState(false)
  const [scopeDeptIds, setScopeDeptIds] = useState<number[]>([])
  const [rolloutPercent, setRolloutPercent] = useState(100)
  const [selectedUsers, setSelectedUsers] = useState<Record<number, string>>({})
  const [userSearch, setUserSearch] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [formErrors, setFormErrors] = useState<string[]>([])
  const [riskChallenge, setRiskChallenge] = useState<RiskChallenge | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const create = useCreateSkill()
  const update = useUpdateSkill()
  const busy = create.isPending || update.isPending

  useEffect(() => {
    if (!open) return
    setFormErrors([])
    setMode('paste')
    setFile(null)
    if (fileRef.current) fileRef.current.value = ''
    setName(skill?.name ?? '')
    setDescription(skill?.description ?? '')
    setBody(skill?.body ?? '')
    setRequiresText((skill?.requires ?? []).join('\n'))
    setVisibility(skill?.visibility ?? 'admin_only')
    setDeptIds([])
    setScopeMode(skill?.scope_mode ?? 'all')
    setScopeSub(skill?.scope_include_sub_departments ?? false)
    setScopeDeptIds([])
    setRolloutPercent(skill?.rollout_percent ?? 100)
    setSelectedUsers(Object.fromEntries(
      (skill?.applicable_users ?? []).map((u) => [u.id, u.username])))
    setUserSearch('')
    setRiskChallenge(null)
  }, [open, skill])

  const { data: departments } = useDepartments(
    open && (visibility === 'department' || scopeMode === 'departments'),
  )
  // 定向用户选择器：服务端按关键词搜索，限额 100 条（后端分页上限）
  const { data: userPage } = useAdminUsers({
    page: 1,
    page_size: 100,
    ...(userSearch.trim() ? { search: userSearch.trim() } : {}),
  }, open)
  const userOptions = (userPage?.results ?? []).filter(
    (u) => u.is_active && !u.is_deleted && !(u.id in selectedUsers),
  )

  const scopeProps = {
    scopeMode,
    onScopeMode: setScopeMode,
    scopeSub,
    onScopeSub: setScopeSub,
    selected: scopeDeptIds,
    onSelected: setScopeDeptIds,
    departments,
    busy,
    rolloutPercent,
    onRolloutPercent: setRolloutPercent,
    users: userOptions,
    selectedUsers,
    onToggleUser: (id: number, username: string) =>
      setSelectedUsers((prev) => {
        const next = { ...prev }
        if (next[id]) delete next[id]
        else next[id] = username
        return next
      }),
    userSearch,
    onUserSearch: setUserSearch,
  }

  // 编辑态回填部门 ID（读取面只给部门名）
  useEffect(() => {
    if (!open || !skill || !departments) return
    const toIds = (names: string[]) =>
      names
        .map((n) => departments.find((d) => d.name === n)?.id)
        .filter((id): id is number => id !== undefined)
    setDeptIds(toIds(skill.allowed_departments))
    setScopeDeptIds(toIds(skill.applicable_departments))
  }, [open, skill, departments])

  async function submit(ack?: RiskAckPayload) {
    setFormErrors([])
    const requires = requiresText.split('\n').map((s) => s.trim()).filter(Boolean)
    try {
      if (skill) {
        if (!name.trim()) {
          setFormErrors(['请填写技能名称'])
          return
        }
        const res = await update.mutateAsync({
          id: skill.id,
          payload: {
            name: name.trim(),
            description: description.trim(),
            body,
            requires,
            visibility,
            allowed_department_ids: visibility === 'department' ? deptIds : [],
            scope_mode: scopeMode,
            scope_include_sub_departments: scopeSub,
            applicable_department_ids: scopeMode === 'departments' ? scopeDeptIds : [],
            rollout_percent: rolloutPercent,
            applicable_user_ids: Object.keys(selectedUsers).map(Number),
            ...ack,
          },
        })
        const base =
          res.status === 'pending'
            ? '已保存并提交审核；启用中的旧版本在通过前继续生效'
            : '已保存（仅可见范围变更，未回退审核状态）'
        if (res.scan_warnings?.length) toast.warning(`${base}；风险扫描提示：${res.scan_warnings.join('；')}`)
        else toast.success(base)
        onClose()
        return
      }
      if (mode === 'upload') {
        if (!file) {
          setFormErrors(['请选择规范文件（SKILL.md / .md / .txt，UTF-8，≤200KB）'])
          return
        }
        const res = await create.mutateAsync({
          mode: 'upload',
          input: {
            file,
            name: name.trim(),
            description: description.trim(),
            scope_mode: scopeMode,
            scope_include_sub_departments: scopeSub,
            applicable_department_ids: scopeMode === 'departments' ? scopeDeptIds : [],
            rollout_percent: rolloutPercent,
            applicable_user_ids: Object.keys(selectedUsers).map(Number),
            ...ack,
          },
        })
        finishCreate(res)
        return
      }
      if (!name.trim()) {
        setFormErrors(['请填写技能名称'])
        return
      }
      // 必填项不在前端拦截：后端口径是校验失败直接 400 并逐条给原因（C1/C2 #7）
      const res = await create.mutateAsync({
        mode: 'json',
        payload: {
          name: name.trim(),
          description: description.trim(),
          body,
          requires,
          visibility,
          allowed_department_ids: visibility === 'department' ? deptIds : [],
          scope_mode: scopeMode,
          scope_include_sub_departments: scopeSub,
          applicable_department_ids: scopeMode === 'departments' ? scopeDeptIds : [],
          ...ack,
        },
      })
      finishCreate(res)
    } catch (err) {
      const challenge = extractRiskChallenge(err)
      if (challenge && !ack) {
        // 决策 10：高危只提示，用户明确确认后带指纹重发；不确认则不落库
        setRiskChallenge(challenge)
        return
      }
      const bodyErr = (err as { body?: Record<string, unknown> | null }).body ?? null
      const errs = extractFieldErrors(bodyErr)
      setFormErrors(errs.length ? errs : [(err as Error).message || '保存失败'])
      setRiskChallenge(null)
    }
  }

  function finishCreate(skillRow: Skill) {
    const base = `已建档「${skillRow.name}」并进入待审核队列`
    // 可疑命中不阻断保存，但必须让保存者看到，避免静默放过（C2 #7）
    if (skillRow.scan_warnings?.length) toast.warning(`${base}；风险扫描提示：${skillRow.scan_warnings.join('；')}`)
    else toast.success(base)
    onClose()
  }

  return (
    <>
    <Drawer
      open={open}
      onClose={onClose}
      title={skill ? `编辑技能 · ${skill.name}` : '上传技能规范'}
      meta={
        skill
          ? `标识 ${skill.slug} · 改动名称/说明/正文将回到待审核（C4）`
          : '上传≠生效：建档后需管理员审核启用，才会进入运行时镜像'
      }
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            取消
          </Button>
          <Button variant="primary" icon="Check" loading={busy} onClick={() => void submit()}>
            {isEdit ? '保存并提交' : '建档'}
          </Button>
        </>
      }
    >
      {formErrors.length > 0 && (
        <div className="mb-[10px] rounded-md border border-state-error-border bg-state-error-soft px-[10px] py-[8px] text-xs leading-relaxed text-state-error">
          {formErrors.map((e, i) => (
            <div key={i}>{e}</div>
          ))}
        </div>
      )}

      <div className="col-stack">
        {!isEdit && (
          <Seg
            items={[
              { value: 'paste', label: '粘贴正文' },
              { value: 'upload', label: '上传 SKILL.md' },
            ]}
            value={mode}
            onChange={(v) => setMode(v as 'paste' | 'upload')}
            className="self-start"
          />
        )}

        {!isEdit && mode === 'upload' ? (
          <Panel title="规范文件" icon="FileUp" padded>
            <div className="col-stack gap-[8px]">
              <div className="field-hint">
                优先读取 YAML frontmatter 的 name / description；缺失时回落一级标题与首段。文件 ≤200KB、UTF-8 文本。
              </div>
              <input
                ref={fileRef}
                type="file"
                accept=".md,.txt,.markdown"
                className="text-xs"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
              <Field label="名称（留空＝按文件解析）" hint="名称是企业内唯一身份：缺失或与已有技能冲突将直接 400">
                <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：合同风险审查" maxLength={128} />
              </Field>
              <Field label="适用说明（留空＝按文件解析）">
                <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="一句话说明何时使用该技能" maxLength={200} />
              </Field>
              <ScopeFields {...scopeProps} />
              <div className="field-hint">上传建档默认「仅管理端」可见，建档后可在编辑中调整可见范围。</div>
            </div>
          </Panel>
        ) : (
          <>
            <Panel title="基本信息" icon="Info" padded>
              <div className="form-grid">
                <Field label="名称" required hint="重名将被拒绝；改名影响已有引用口径">
                  <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：合同风险审查" maxLength={128} disabled={busy} />
                </Field>
                <Field label="可见范围" required hint="非管理员按 公开/自己创建/命中部门 过滤可见">
                  <Select value={visibility} onChange={(e) => setVisibility(e.target.value as SkillVisibility)}>
                    {VISIBILITIES.map((v) => (
                      <option key={v.value} value={v.value}>
                        {v.label}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="适用说明" className="span-full" hint="装配页选择器展示，供装配人判断何时挂载">
                  <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="一句话说明何时使用该技能" maxLength={200} />
                </Field>
                {visibility === 'department' && (
                  <Field label="可见部门" required className="span-full">
                    {departments ? (
                      <div className="flex flex-wrap gap-[6px]">
                        {departments.map((d) => (
                          <button
                            key={d.id}
                            type="button"
                            className="chip"
                            data-active={String(deptIds.includes(d.id))}
                            onClick={() =>
                              setDeptIds((prev) => (prev.includes(d.id) ? prev.filter((x) => x !== d.id) : [...prev, d.id]))
                            }
                          >
                            {d.name}
                          </button>
                        ))}
                        {departments.length === 0 && <span className="text-xs text-muted-foreground">暂无部门，请先在系统设置建档</span>}
                      </div>
                    ) : (
                      <span className="text-xs text-muted-foreground">读取部门…</span>
                    )}
                  </Field>
                )}
                <ScopeFields {...scopeProps} />
              </div>
            </Panel>

            <Panel
              title="规范正文"
              icon="FileText"
              meta={`${body.length} / ${bodyLimit} 字`}
              padded
            >
              <Textarea
                rows={16}
                value={body}
                className="mono text-xs"
                placeholder={'# 技能名称\n\n## 适用场景\n…\n\n## 执行步骤\n1. …'}
                onChange={(e) => setBody(e.target.value)}
              />
              {body.length > bodyLimit && (
                <div className="mt-[6px] flex items-center gap-[4px] text-xs text-state-error">
                  <Icon name="TriangleAlert" size={12} />
                  超出上限（{bodyLimit} 字）：无法保存，请精简正文后再提交
                </div>
              )}
            </Panel>

            <Panel title="依赖工具（每行一个工具全称，可留空）" icon="Wrench" padded>
              <Textarea
                rows={3}
                value={requiresText}
                className="mono text-xs"
                placeholder={'mcp__knowledge__search\nexec_shell'}
                onChange={(e) => setRequiresText(e.target.value)}
              />
            </Panel>
          </>
        )}
      </div>
    </Drawer>

    <RiskConfirmModal
      challenge={riskChallenge}
      title="技能内容命中高危规则"
      busy={busy}
      onCancel={() => setRiskChallenge(null)}
      onConfirm={() =>
        void submit({ risk_ack: true, risk_fingerprint: riskChallenge?.fingerprint ?? '' })
      }
    />
    </>
  )
}

interface ScopeFieldsProps {
  scopeMode: SkillScope
  onScopeMode: (v: SkillScope) => void
  scopeSub: boolean
  onScopeSub: (v: boolean) => void
  selected: number[]
  onSelected: (updater: (prev: number[]) => number[]) => void
  departments?: { id: number; name: string }[]
  busy?: boolean
  rolloutPercent: number
  onRolloutPercent: (v: number) => void
  users?: { id: number; username: string; department: string }[]
  selectedUsers: Record<number, string>
  onToggleUser: (id: number, username: string) => void
  userSearch: string
  onUserSearch: (v: string) => void
}

/** C5 生效范围：基础范围（全员/部门）→ 灰度比例 → 定向用户白名单（命中即生效） */
function ScopeFields({
  scopeMode, onScopeMode, scopeSub, onScopeSub, selected, onSelected, departments, busy,
  rolloutPercent, onRolloutPercent, users, selectedUsers, onToggleUser, userSearch, onUserSearch,
}: ScopeFieldsProps) {
  const targeted = Object.entries(selectedUsers)
  return (
    <>
      <Field
        label="生效范围（C5，运行时灰度）"
        hint="只影响运行时是否注入该技能，不影响本页可见性；管理员与定向用户始终生效"
      >
        <Select
          value={scopeMode}
          disabled={busy}
          onChange={(e) => onScopeMode(e.target.value as SkillScope)}
        >
          <option value="all">全员生效</option>
          <option value="departments">仅指定部门生效</option>
        </Select>
      </Field>
      {scopeMode === 'departments' && (
        <Field label="生效部门" required className="span-full">
          <label className="mb-[6px] flex items-center gap-[6px] text-xs text-sidebar-foreground">
            <input
              type="checkbox"
              checked={scopeSub}
              disabled={busy}
              onChange={(e) => onScopeSub(e.target.checked)}
            />
            包含所选部门的子部门
          </label>
          {departments ? (
            <div className="flex flex-wrap gap-[6px]">
              {departments.map((d) => (
                <button
                  key={d.id}
                  type="button"
                  className="chip"
                  data-active={String(selected.includes(d.id))}
                  onClick={() =>
                    onSelected((prev) =>
                      prev.includes(d.id) ? prev.filter((x) => x !== d.id) : [...prev, d.id],
                    )
                  }
                >
                  {d.name}
                </button>
              ))}
              {departments.length === 0 && (
                <span className="text-xs text-muted-foreground">暂无部门，请先在系统设置建档</span>
              )}
            </div>
          ) : (
            <span className="text-xs text-muted-foreground">读取部门…</span>
          )}
        </Field>
      )}
      <Field
        label="灰度比例"
        hint="基础范围命中后再按比例放开：0=不放开，100=全量；按用户稳定分桶，上调只增不减"
      >
        <div className="flex items-center gap-[6px]">
          <Input
            type="number"
            min={0}
            max={100}
            value={String(rolloutPercent)}
            disabled={busy}
            className="w-[92px]"
            onChange={(e) => {
              const raw = Number(e.target.value)
              onRolloutPercent(Number.isFinite(raw) ? Math.min(100, Math.max(0, Math.round(raw))) : 0)
            }}
          />
          <span className="text-xs text-muted-foreground">%</span>
        </div>
      </Field>
      <Field
        label={`定向用户（可选，已选 ${targeted.length} / 200）`}
        className="span-full"
        hint="白名单命中即生效，不受部门与灰度比例限制；按用户名搜索后点击添加"
      >
        {targeted.length > 0 && (
          <div className="mb-[6px] flex flex-wrap gap-[6px]">
            {targeted.map(([id, username]) => (
              <button
                key={id}
                type="button"
                className="chip"
                data-active="true"
                disabled={busy}
                onClick={() => onToggleUser(Number(id), username)}
              >
                {username} ×
              </button>
            ))}
          </div>
        )}
        <Input
          value={userSearch}
          disabled={busy}
          placeholder="搜索用户名 / 姓名"
          onChange={(e) => onUserSearch(e.target.value)}
        />
        <div className="mt-[6px] flex flex-wrap gap-[6px]">
          {(users ?? []).slice(0, 20).map((u) => (
            <button
              key={u.id}
              type="button"
              className="chip"
              disabled={busy}
              onClick={() => onToggleUser(u.id, u.username)}
            >
              {u.username}
              {u.department ? `（${u.department}）` : ''}
            </button>
          ))}
          {(users ?? []).length === 0 && (
            <span className="text-xs text-muted-foreground">
              {userSearch.trim() ? '没有匹配的账号' : '暂无更多可选账号'}
            </span>
          )}
          {(users ?? []).length > 20 && (
            <span className="text-xs text-muted-foreground">仅显示前 20 个，继续输入关键词缩小范围</span>
          )}
        </div>
      </Field>
    </>
  )
}
