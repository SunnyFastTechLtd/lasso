import { useEffect, useMemo, useRef, useState } from 'react'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Field, Input, Select, Textarea } from '@/components/ui/Field'
import { Icon } from '@/components/ui/Icon'
import { Modal } from '@/components/ui/Overlay'
import { SectionLabel } from '@/components/ui/Panel'
import { Seg } from '@/components/ui/Seg'
import { Switch } from '@/components/ui/Switch'
import { useDepartments } from '@/api/departments'
import {
  useAgent,
  useAgentOptions,
  useMountOptions,
  useUpdateAgent,
} from '@/api/hooks/useAgents'
import { ApiError } from '@/api/request'
import { useUserStore } from '@/store/user'
import { toast } from '@/store/toast'
import { extractFieldErrors } from '@/pages/Mcp/shared'
import type {
  AgentPayload,
  AgentVisibility,
  KnowledgeMode,
  PermissionTier,
  RetrievalTier,
  SpeechTier,
} from '@/types/agent'
import { MountPicker, selectionFromMountOptions, type MountSelection } from './MountPicker'
import { AgentStatusBadge, ICON_PRESETS, JOB_TAG_PRESETS, PendingChangeBadge } from './shared'
import { TrialPanel } from './TrialPanel'

type Tab = 'basic' | 'model' | 'mounts' | 'perm'

const TIER_HINT: Record<SpeechTier, string> = {
  low: '标准合成，稳定优先',
  mid: '多音色 / 语音克隆',
  high: '音色、语速、情绪深度自定义',
}
const RETRIEVAL_HINT: Record<RetrievalTier, string> = {
  few: '少量引用',
  standard: '常规检索',
  many: '大上下文聚合',
}

interface EditState {
  name: string
  description: string
  icon: string
  job_tags: string[]
  model_name: string
  speech_tier: SpeechTier
  retrieval_tier: RetrievalTier
  knowledge_mode: KnowledgeMode
  followup_rewrite: boolean
  enable_thinking: boolean
  prompt: string
  /** D8：可被使用者关闭的默认跨会话偏好（每行一条） */
  default_preferences: string[]
  permission_tier: PermissionTier
  visibility: AgentVisibility
  allowed_department_ids: number[]
  include_sub_departments: boolean
}

interface Props {
  open: boolean
  agentId: number | null
  onClose: () => void
  onSaved?: () => void
}

export function AgentForm({ open, agentId, onClose, onSaved }: Props) {
  const { data: agent } = useAgent(open ? agentId : null)
  const { data: options } = useAgentOptions()
  const { data: mountOptions } = useMountOptions(open ? agentId : null)
  const update = useUpdateAgent()
  const isAdmin = useUserStore((s) => s.user?.is_staff ?? false)

  const [tab, setTab] = useState<Tab>('basic')
  const [form, setForm] = useState<EditState | null>(null)
  const [mounts, setMounts] = useState<MountSelection | null>(null)
  const [tagInput, setTagInput] = useState('')
  const [errors, setErrors] = useState<string[]>([])
  const [trial, setTrial] = useState(false)
  const promptWrapRef = useRef<HTMLDivElement>(null)

  const { data: departments } = useDepartments(open && form?.visibility === 'department')

  // 打开/切换智能体时回填（挂载选择态来自后端实时计算）
  useEffect(() => {
    if (!open || !agent || agent.id !== agentId) return
    setTab('basic')
    setErrors([])
    setTrial(false)
    setForm({
      name: agent.name,
      description: agent.description,
      icon: agent.icon,
      job_tags: agent.job_tags,
      model_name: agent.model_name,
      speech_tier: agent.speech_tier,
      retrieval_tier: agent.retrieval_tier,
      knowledge_mode: agent.knowledge_mode ?? 'on_demand',
      followup_rewrite: agent.followup_rewrite ?? true,
      enable_thinking: agent.enable_thinking,
      prompt: agent.prompt,
      default_preferences: agent.default_preferences ?? [],
      permission_tier: agent.permission_tier,
      visibility: agent.visibility,
      allowed_department_ids: agent.allowed_department_ids,
      include_sub_departments: agent.include_sub_departments,
    })
  }, [open, agentId, agent])

  useEffect(() => {
    if (open && mountOptions && mountOptions.mcp_servers) {
      setMounts(selectionFromMountOptions(mountOptions))
    }
  }, [open, mountOptions])

  const readOnly = !!agent?.is_builtin && !isAdmin
  const canSave = !!(form && mounts) && !readOnly && !update.isPending

  const modelOptions = useMemo(() => {
    const set = new Set(options?.models ?? [])
    if (form?.model_name) set.add(form.model_name)
    return [...set]
  }, [options?.models, form?.model_name])

  function set<K extends keyof EditState>(key: K, value: EditState[K]) {
    setForm((f) => (f ? { ...f, [key]: value } : f))
  }

  function addTag(tag: string) {
    const t = tag.trim()
    if (!t || !form) return
    if (!form.job_tags.includes(t)) set('job_tags', [...form.job_tags, t])
    setTagInput('')
  }

  function insertPlaceholder(token: string) {
    const ta = promptWrapRef.current?.querySelector('textarea')
    if (!ta) {
      set('prompt', form ? form.prompt + token : token)
      return
    }
    const start = ta.selectionStart ?? ta.value.length
    const end = ta.selectionEnd ?? ta.value.length
    const next = ta.value.slice(0, start) + token + ta.value.slice(end)
    set('prompt', next)
    requestAnimationFrame(() => {
      ta.focus()
      ta.selectionStart = ta.selectionEnd = start + token.length
    })
  }

  async function save() {
    if (!agent || !form || !mounts) return
    if (form.name.trim().length < 2 || form.name.trim().length > 30) {
      toast.error('名称需 2-30 个字符')
      setTab('basic')
      return
    }
    if (form.visibility === 'department' && form.allowed_department_ids.length === 0) {
      toast.error('按部门可见时至少选择一个部门')
      setTab('perm')
      return
    }
    const payload: AgentPayload = {
      name: form.name.trim(),
      description: form.description,
      job_tags: form.job_tags,
      icon: form.icon,
      model_name: form.model_name,
      speech_tier: form.speech_tier,
      retrieval_tier: form.retrieval_tier,
      knowledge_mode: form.knowledge_mode,
      followup_rewrite: form.followup_rewrite,
      enable_thinking: form.enable_thinking,
      prompt: form.prompt,
      default_preferences: form.default_preferences,
      mcp_mounts: mounts.mcp.map((m) => ({ server: m.server, tool_names: m.tool_names })),
      skill_ids: mounts.skill_ids,
      knowledge_base_ids: mounts.knowledge_base_ids,
      builtin_capabilities: mounts.builtin_capabilities,
      permission_tier: form.permission_tier,
      visibility: form.visibility,
      allowed_department_ids: form.allowed_department_ids,
      include_sub_departments: form.include_sub_departments,
    }
    try {
      await update.mutateAsync({ id: agent.id, payload })
      setErrors([])
      toast.success(
        agent.status === 'draft'
          ? `已保存草稿「${payload.name}」，发布检查通过后才会启用`
          : `已保存「${payload.name}」——记得重新发布，线上才会切到新版本`,
      )
      onSaved?.()
      onClose()
    } catch (err) {
      if (err instanceof ApiError) {
        const errs = extractFieldErrors(err.body)
        setErrors(errs.length ? errs : [err.message])
        toast.error(errs[0] ?? err.message)
      } else {
        toast.error((err as Error).message)
      }
    }
  }

  return (
    <Modal
      open={open && !!agent && !!form}
      title={agent ? `装配智能体 · ${agent.name}` : '装配智能体'}
      icon="Bot"
      wide
      xwide={trial}
      onClose={onClose}
      footer={
        <>
          {agent && (
            <span className="mr-auto flex items-center gap-[6px] text-xs text-muted-foreground">
              <AgentStatusBadge status={agent.status} display={agent.status_display} />
              {agent.pending_changes && <PendingChangeBadge />}
              {agent.current_version > 0 && <span className="mono">v{agent.current_version}</span>}
              <span className="mono">{agent.slug}</span>
            </span>
          )}
          {!readOnly && (
            <Button
              variant={trial ? 'primary' : 'default'}
              quiet
              icon="FlaskConical"
              onClick={() => setTrial((v) => !v)}
            >
              {trial ? '收起试聊' : '试聊'}
            </Button>
          )}
          <Button onClick={onClose}>取消</Button>
          <Button variant="primary" loading={update.isPending} disabled={!canSave} onClick={save}>
            保存配置
          </Button>
        </>
      }
    >
      {agent && form && (
        <div className={trial ? 'flex items-start gap-[14px]' : ''}>
        <div className={`space-y-[12px] ${trial ? 'min-w-0 flex-1' : ''}`}>
          {errors.length > 0 && (
            <div className="rounded border border-state-error/40 bg-state-error/10 px-[10px] py-[8px] text-xs leading-[1.6] text-state-error">
              {errors.map((e, i) => (
                <div key={i}>{e}</div>
              ))}
            </div>
          )}
          {readOnly && (
            <div className="flex items-center gap-[7px] rounded border border-border/60 bg-muted/50 px-[10px] py-[7px] text-xs">
              <Icon name="Shield" size={13} className="text-muted-foreground" />
              出厂内置模板不可直接修改；如需定制，请在列表行点「克隆」生成自己的副本。
            </div>
          )}
          {agent.is_builtin && isAdmin && (
            <div className="flex items-center gap-[7px] rounded border border-primary/30 bg-primary/5 px-[10px] py-[7px] text-xs">
              <Icon name="Shield" size={13} className="text-primary" />
              内置智能体：保存后与出厂配置不一致会标记「已定制」，可在列表行「恢复出厂配置」写回基线；改动只影响草稿，需重新发布才生效。
            </div>
          )}
          {agent.tool_conflicts.length > 0 && (
            <div className="rounded border border-state-warning/40 bg-state-warning/10 px-[10px] py-[7px] text-xs text-state-warning">
              同名工具冲突：
              {agent.tool_conflicts.map((c) => ` ${c.tool}（${c.owners.join('/')}）`).join('；')}
            </div>
          )}

          <Seg
            items={[
              { value: 'basic', label: '基础信息' },
              { value: 'model', label: '模型与提示词' },
              { value: 'mounts', label: '能力挂载' },
              { value: 'perm', label: '权限与可见范围' },
            ]}
            value={tab}
            onChange={(v) => setTab(v as Tab)}
          />

          {tab === 'basic' && (
            <div className="space-y-[12px]">
              <div className="grid grid-cols-2 gap-[10px]">
                <Field label="名称" required hint="2-30 字；同名会被拒绝并提示占用者">
                  <Input
                    value={form.name}
                    disabled={readOnly}
                    maxLength={30}
                    onChange={(e) => set('name', e.target.value)}
                  />
                </Field>
                <Field label="唯一标识" hint="编码由后端按拼音自动生成并保证唯一">
                  <Input readOnly value={agent.slug} className="mono cursor-default text-muted-foreground" />
                </Field>
              </div>
              <Field label="用途描述" hint="展示在使用端，写清楚这个智能体干什么">
                <Textarea
                  rows={2}
                  value={form.description}
                  disabled={readOnly}
                  onChange={(e) => set('description', e.target.value)}
                />
              </Field>
              <Field label="岗位标签" hint="用于使用端分组；点击预设或输入后回车">
                <div className="flex flex-wrap items-center gap-[6px]">
                  {form.job_tags.map((t) => (
                    <Badge key={t} tone="primary">
                      <span className="flex items-center gap-[4px]">
                        {t}
                        {!readOnly && (
                          <button
                            type="button"
                            aria-label={`移除标签 ${t}`}
                            className="text-current opacity-70 hover:opacity-100"
                            onClick={() => set('job_tags', form.job_tags.filter((x) => x !== t))}
                          >
                            <Icon name="X" size={10} />
                          </button>
                        )}
                      </span>
                    </Badge>
                  ))}
                  {JOB_TAG_PRESETS.filter((t) => !form.job_tags.includes(t)).map((t) => (
                    <button
                      key={t}
                      type="button"
                      disabled={readOnly}
                      className="chip cursor-pointer"
                      onClick={() => addTag(t)}
                    >
                      + {t}
                    </button>
                  ))}
                  {!readOnly && (
                    <Input
                      className="w-[120px]"
                      value={tagInput}
                      placeholder="自定义标签…"
                      onChange={(e) => setTagInput(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          e.preventDefault()
                          addTag(tagInput)
                        }
                      }}
                    />
                  )}
                </div>
              </Field>
              <Field label="图标" hint="预设图标库，用于使用端头像">
                <div className="flex flex-wrap gap-[6px]">
                  {ICON_PRESETS.map((name) => (
                    <button
                      key={name}
                      type="button"
                      disabled={readOnly}
                      title={name}
                      className={`flex h-[30px] w-[30px] items-center justify-center rounded border ${
                        form.icon === name ? 'border-primary bg-primary/10 text-primary' : 'border-border/60 text-muted-foreground'
                      } ${readOnly ? '' : 'hover:bg-muted/60'}`}
                      onClick={() => set('icon', form.icon === name ? '' : name)}
                    >
                      <Icon name={name} size={15} />
                    </button>
                  ))}
                </div>
              </Field>
            </div>
          )}

          {tab === 'model' && (
            <div className="space-y-[12px]">
              <Field label="大语言模型" required hint="仅可选已核验清单；留空则发布检查会被拦截">
                <Select
                  value={form.model_name}
                  disabled={readOnly}
                  onChange={(e) => set('model_name', e.target.value)}
                >
                  <option value="">（未选择，发布前必填）</option>
                  {modelOptions.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </Select>
              </Field>
              <div className="grid grid-cols-2 gap-[10px]">
                <Field label="语音档位" hint="预设参数映射，不直接暴露原始参数">
                  <Seg
                    items={(options?.speech_tiers ?? []).map((t) => ({ value: t.value, label: t.label }))}
                    value={form.speech_tier}
                    onChange={(v) => set('speech_tier', v as SpeechTier)}
                  />
                  <div className="field-hint">{TIER_HINT[form.speech_tier]}</div>
                </Field>
                <Field
                  label="检索档位"
                  hint={
                    (options?.retrieval_tiers ?? []).find((t) => t.value === form.retrieval_tier)
                      ? `最多 ${(options?.retrieval_tiers ?? []).find((t) => t.value === form.retrieval_tier)?.max_rounds} 轮工具调用`
                      : undefined
                  }
                >
                  <Seg
                    items={(options?.retrieval_tiers ?? []).map((t) => ({ value: t.value, label: t.label }))}
                    value={form.retrieval_tier}
                    onChange={(v) => set('retrieval_tier', v as RetrievalTier)}
                  />
                  <div className="field-hint">{RETRIEVAL_HINT[form.retrieval_tier]}</div>
                </Field>
              </div>
              <div className="flex items-center justify-between rounded border border-border/60 px-[10px] py-[8px]">
                <div>
                  <div className="text-xs font-medium">深度思考（思维链）</div>
                  <div className="text-xs text-muted-foreground">按模型支持情况开启；不支持时运行期自动降级</div>
                </div>
                <Switch checked={form.enable_thinking} disabled={readOnly} onChange={(v) => set('enable_thinking', v)} />
              </div>
              <Field
                label="提示词"
                required
                hint="单花括号占位符运行时替换：点击右侧插入；发布检查会校验是否残留未替换占位符"
              >
                <div ref={promptWrapRef}>
                <Textarea
                  rows={8}
                  className="leading-[1.7]"
                  value={form.prompt}
                  disabled={readOnly}
                  placeholder={options?.prompt_required}
                  onChange={(e) => set('prompt', e.target.value)}
                />
                </div>
                <div className="mt-[6px] flex flex-wrap gap-[6px]">
                  {(options?.placeholders ?? []).map((p) => (
                    <button
                      key={p.token}
                      type="button"
                      disabled={readOnly}
                      title={p.desc}
                      className={`chip mono ${readOnly ? '' : 'cursor-pointer hover:bg-muted/60'}`}
                      onClick={() => insertPlaceholder(p.token)}
                    >
                      {p.token}
                    </button>
                  ))}
                </div>
              </Field>
              <Field
                label="默认偏好（预置）"
                hint="每行一条，最多 8 条、单条 100 字；使用者可在个人中心关闭，未被关闭时追加在提示词末尾"
              >
                <Textarea
                  rows={3}
                  value={form.default_preferences.join('\n')}
                  disabled={readOnly}
                  placeholder="例如：用「你」称呼我，先给结论再给依据"
                  onChange={(e) =>
                    set(
                      'default_preferences',
                      e.target.value
                        .split('\n')
                        .map((s) => s.trim().slice(0, 100))
                        .filter(Boolean)
                        .slice(0, 8),
                    )
                  }
                />
              </Field>
            </div>
          )}

          {tab === 'mounts' && (
            <div>
              {!mounts ? (
                <div className="flex items-center gap-[7px] py-[20px] text-xs text-muted-foreground">
                  <Icon name="LoaderCircle" size={14} className="animate-spin" /> 正在加载装配选择器…
                </div>
              ) : (
                <>
                  <MountPicker options={mountOptions!} value={mounts} onChange={setMounts} readOnly={readOnly} />
                  <div className="mt-[16px] rounded border border-border/60 px-[12px] py-[10px]">
                    <SectionLabel>知识库检索策略</SectionLabel>
                    <div className="mt-[8px] flex flex-wrap items-center gap-[10px]">
                      <Seg
                        items={[
                          { value: 'on_demand', label: '按需查' },
                          { value: 'always', label: '每轮必查' },
                        ]}
                        value={form.knowledge_mode}
                        onChange={(next) => {
                          if (!readOnly) setForm({ ...form, knowledge_mode: next as KnowledgeMode })
                        }}
                      />
                      <span className="text-xs text-muted-foreground">
                        {form.knowledge_mode === 'always'
                          ? '回答前先检索，适合制度、合规类'
                          : '模型判断本轮是否检索，闲聊不查库、回答更快'}
                      </span>
                    </div>
                    <div className="mt-[10px]">
                      <Switch
                        checked={form.followup_rewrite}
                        disabled={readOnly}
                        onChange={(next) => setForm({ ...form, followup_rewrite: next })}
                        label="追问补全"
                        hint="把「那它的价格呢」这类省略式追问补成完整问题再检索"
                      />
                    </div>
                    <p className="mt-[8px] text-xs text-muted-foreground">引用来源始终展示，不可关闭。</p>
                  </div>
                </>
              )}
            </div>
          )}

          {tab === 'perm' && (
            <div className="space-y-[12px]">
              <Field label="工具权限档位">
                <div className="space-y-[6px]">
                  {(options?.permission_tiers ?? []).map((t) => {
                    const disabled = !t.selectable && form.permission_tier !== t.value
                    return (
                      <label
                        key={t.value}
                        className={`flex items-start gap-[8px] rounded border px-[10px] py-[7px] ${
                          form.permission_tier === t.value ? 'border-primary bg-primary/5' : 'border-border/60'
                        } ${disabled ? 'opacity-60' : readOnly ? '' : 'cursor-pointer'}`}
                        title={disabled ? '暂未开放：需工具侧二次确认链路' : t.desc}
                      >
                        <input
                          type="radio"
                          name="permission_tier"
                          className="mt-[2px] accent-primary"
                          disabled={disabled || readOnly}
                          checked={form.permission_tier === t.value}
                          onChange={() => set('permission_tier', t.value)}
                        />
                        <span className="min-w-0">
                          <span className="text-xs font-medium">
                            {t.label}
                            {disabled && <span className="ml-[6px] text-xs text-muted-foreground">（暂未开放）</span>}
                            {t.value === 'whitelist' && form.permission_tier === t.value && (
                              <span className="ml-[6px] text-xs text-muted-foreground">
                                允许清单自动同步为已勾选工具
                              </span>
                            )}
                          </span>
                          <span className="block text-xs text-muted-foreground">{t.desc}</span>
                        </span>
                      </label>
                    )
                  })}
                </div>
              </Field>
              <Field label="可见范围" required>
                <div className="space-y-[6px]">
                  {(options?.visibilities ?? []).map((v) => {
                    const disabled = v.selectable === false && form.visibility !== v.value
                    return (
                      <label
                        key={v.value}
                        className={`flex items-center gap-[8px] rounded border px-[10px] py-[7px] ${
                          form.visibility === v.value ? 'border-primary bg-primary/5' : 'border-border/60'
                        } ${disabled ? 'opacity-60' : readOnly ? '' : 'cursor-pointer'}`}
                        title={disabled ? v.reason : undefined}
                      >
                        <input
                          type="radio"
                          name="visibility"
                          className="accent-primary"
                          disabled={disabled || readOnly}
                          checked={form.visibility === v.value}
                          onChange={() => set('visibility', v.value)}
                        />
                        <span className="min-w-0">
                          <span className="text-xs">{v.label}</span>
                          {v.desc && (
                            <span className="block text-xs text-muted-foreground">{v.desc}</span>
                          )}
                          {disabled && (
                            <span className="block text-xs text-muted-foreground">（{v.reason}）</span>
                          )}
                        </span>
                      </label>
                    )
                  })}
                </div>
              </Field>
              {form.visibility === 'department' && (
                <Field label="可见部门" required hint="仅管理员可维护部门清单">
                  <div className="flex flex-wrap gap-[6px]">
                    {(departments ?? []).map((d) => {
                      const on = form.allowed_department_ids.includes(d.id)
                      return (
                        <button
                          key={d.id}
                          type="button"
                          disabled={readOnly}
                          className={`chip ${on ? 'border-primary bg-primary/10 text-primary' : ''} ${readOnly ? '' : 'cursor-pointer'}`}
                          onClick={() =>
                            set(
                              'allowed_department_ids',
                              on
                                ? form.allowed_department_ids.filter((x) => x !== d.id)
                                : [...form.allowed_department_ids, d.id],
                            )
                          }
                        >
                          {d.name}
                        </button>
                      )
                    })}
                    {departments && departments.length === 0 && (
                      <span className="text-xs text-muted-foreground">暂无部门可选</span>
                    )}
                  </div>
                </Field>
              )}
              {form.visibility === 'department' && (
                <div className="flex items-center justify-between rounded border border-border/60 px-[10px] py-[8px]">
                  <div>
                    <div className="text-xs font-medium">包含子部门</div>
                    <div className="text-xs text-muted-foreground">选中部门的下级部门同样可见</div>
                  </div>
                  <Switch
                    checked={form.include_sub_departments}
                    disabled={readOnly}
                    onChange={(v) => set('include_sub_departments', v)}
                  />
                </div>
              )}
              <div className="text-xs text-muted-foreground">
                <SectionLabel>归属</SectionLabel>
                <p className="mt-[4px]">
                  创建人 {agent.created_by_name || '—'} · 归属部门 {agent.owner_department_name || '—'} · 最近修改
                  {agent.updated_by_name ? ` ${agent.updated_by_name}` : ''}
                </p>
              </div>
            </div>
          )}
        </div>
        {trial && (
          <TrialPanel
            agentId={agent.id}
            agentName={agent.name}
            status={agent.status}
            statusDisplay={agent.status_display}
          />
        )}
        </div>
      )}
    </Modal>
  )
}
