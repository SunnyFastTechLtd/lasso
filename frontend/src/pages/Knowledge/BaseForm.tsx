import { useEffect, useMemo, useState } from 'react'
import { Button } from '@/components/ui/Button'
import { Field, Input, Select, Textarea } from '@/components/ui/Field'
import { Modal } from '@/components/ui/Overlay'
import { Panel } from '@/components/ui/Panel'
import { Seg } from '@/components/ui/Seg'
import { useCreateKnowledgeBase, useUpdateKnowledgeBase } from '@/api/hooks/useKnowledge'
import { useDepartments } from '@/api/departments'
import { Switch } from '@/components/ui/Switch'
import { KB_SCOPE_HINT, KB_SCOPE_LABEL, type KbScope, type KnowledgeBase, type KnowledgeBasePayload } from '@/api/knowledge'
import { useUserStore } from '@/store/user'
import { toast } from '@/store/toast'
import { extractFieldErrors } from './shared'

const SCOPE_ITEMS: { value: KbScope; label: string }[] = (['public', 'department', 'private'] as KbScope[]).map(
  (v) => ({ value: v, label: KB_SCOPE_LABEL[v] }),
)

interface Props {
  open: boolean
  /** null＝新建知识库 */
  base: KnowledgeBase | null
  onClose: () => void
}

/** E1 知识库建档/编辑：可见范围（企业库/部门库/个人库）是检索权限的唯一权威字段 */
export function BaseForm({ open, base, onClose }: Props) {
  const isEdit = base !== null
  const myDept = useUserStore((s) => s.user?.department ?? '')

  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [icon, setIcon] = useState('')
  const [scope, setScope] = useState<KbScope>('public')
  const [department, setDepartment] = useState('')
  const [tiangongEnabled, setTiangongEnabled] = useState(false)
  const [tiangongTopics, setTiangongTopics] = useState('')
  const [tiangongSeeds, setTiangongSeeds] = useState('')
  const [formErrors, setFormErrors] = useState<string[]>([])

  const create = useCreateKnowledgeBase()
  const update = useUpdateKnowledgeBase()
  const busy = create.isPending || update.isPending

  useEffect(() => {
    if (!open) return
    setFormErrors([])
    setName(base?.name ?? '')
    setDescription(base?.description ?? '')
    setIcon(base?.icon ?? '')
    setScope(base?.scope ?? 'public')
    setDepartment(base?.department ?? myDept)
    setTiangongEnabled(base?.tiangong_enabled ?? false)
    setTiangongTopics(base?.tiangong_topics ?? '')
    setTiangongSeeds(base?.tiangong_seeds ?? '')
  }, [open, base, myDept])

  const { data: departments } = useDepartments(open && scope === 'department')

  const deptOptions = useMemo(() => {
    const names = (departments ?? []).map((d) => d.name)
    if (department && !names.includes(department)) names.unshift(department)
    return names
  }, [departments, department])

  async function submit() {
    setFormErrors([])
    const trimmed = name.trim()
    if (!trimmed) {
      setFormErrors(['请填写知识库名称'])
      return
    }
    if (scope === 'department' && !department.trim()) {
      setFormErrors(['部门共享的知识库需要指定部门'])
      return
    }
    const payload: KnowledgeBasePayload = {
      name: trimmed,
      description: description.trim(),
      icon: icon.trim() || 'Database',
      scope,
      department: scope === 'department' ? department.trim() : '',
      is_public: scope === 'public',
      tiangong_enabled: tiangongEnabled,
      tiangong_topics: tiangongEnabled ? tiangongTopics.trim() : '',
      tiangong_seeds: tiangongEnabled ? tiangongSeeds.trim() : '',
    }
    try {
      if (base) {
        await update.mutateAsync({ id: base.id, payload })
        toast.success(`已保存知识库「${payload.name}」`)
      } else {
        await create.mutateAsync(payload)
        toast.success(`已新建知识库「${payload.name}」，上传资料后即可被检索`)
      }
      onClose()
    } catch (err) {
      const errs = extractFieldErrors((err as { body?: Record<string, unknown> | null }).body ?? null)
      setFormErrors(errs.length ? errs : [(err as Error).message || '保存失败'])
    }
  }

  return (
    <Modal
      open={open}
      wide
      icon="BookOpen"
      title={isEdit ? `编辑知识库 · ${base.name}` : '新建知识库'}
      onClose={busy ? () => undefined : onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            取消
          </Button>
          <Button variant="primary" icon="Check" loading={busy} onClick={submit}>
            {isEdit ? '保存' : '建档'}
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
        <Panel title="基本信息" icon="Info" padded>
          <div className="form-grid">
            <Field label="名称" required hint="装配端在智能体属性里按名称挂载">
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：用工与工伤政策库" maxLength={200} disabled={busy} />
            </Field>
            <Field label="图标名" hint="lucide 图标名，留空＝Database">
              <Input value={icon} onChange={(e) => setIcon(e.target.value)} placeholder="Database" maxLength={50} disabled={busy} />
            </Field>
            <Field label="适用说明" className="span-full" hint="展示给装配人，帮助判断该不该挂这个库">
              <Textarea
                rows={3}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="收录哪些资料、面向哪些岗位、更新频率"
                disabled={busy}
              />
            </Field>
          </div>
        </Panel>

        <Panel title="可见范围" icon="ShieldCheck" padded>
          <div className="col-stack gap-[8px]">
            <Seg items={SCOPE_ITEMS} value={scope} onChange={(v) => setScope(v as KbScope)} className="self-start" />
            <div className="field-hint">{KB_SCOPE_HINT[scope]}</div>
            {scope === 'department' && (
              <Field label="共享部门" required error={!department.trim() ? '部门共享的知识库需要指定部门' : undefined}>
                <Select value={department} onChange={(e) => setDepartment(e.target.value)} disabled={busy}>
                  <option value="">请选择部门…</option>
                  {deptOptions.map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </Select>
              </Field>
            )}
            <div className="field-hint">
              智能体挂载知识库不会扩大用户可读范围：检索时仍按用户自身可见范围过滤（E2）。
            </div>
          </div>
        </Panel>
        <Panel title="天工模式" icon="Sparkles" padded>
          <div className="col-stack gap-[8px]">
            <Switch
              checked={tiangongEnabled}
              onChange={setTiangongEnabled}
              disabled={busy}
              label="获取相关知识"
              hint="开启后建库即自动采集一轮：公开网页正文与模型整理的资料，都会带来源标记留在这个知识库里"
            />
            <details className="field-hint">
              <summary className="cursor-pointer select-none">采集规则与来源边界</summary>
              <div className="mt-[4px] leading-[1.6]">
                两类来源分开展示：「外部网页」保留原始地址、站点与抓取时间；「模型生成」记录生成模型名，是整理稿而非原始出处。
                资料自动入库并切片，可随时删除；同一地址或同一主题只入库一次。与女娲的联网开关互不影响，关掉女娲不会停掉天工，反之亦然。
              </div>
            </details>
            {tiangongEnabled && (
              <>
                <Field label="采集主题" hint="留空＝用名称与适用说明；模型据此写整理稿">
                  <Textarea
                    rows={2}
                    value={tiangongTopics}
                    onChange={(e) => setTiangongTopics(e.target.value)}
                    placeholder="如：工伤认定流程、停工留薪期、劳动能力鉴定"
                    disabled={busy}
                  />
                </Field>
                <Field label="采集入口" hint="一行一个 URL，最多 5 个；本次临时补充入口，留空则使用来源库">
                  <Textarea
                    rows={3}
                    value={tiangongSeeds}
                    onChange={(e) => setTiangongSeeds(e.target.value)}
                    placeholder="https://example.com/policy&#10;https://example.com/guide"
                    disabled={busy}
                  />
                </Field>
                <div className="field-hint">
                  公网正文优先从「天工来源库」抓取；搜索发现的新站点只生成候选，管理员批准后才进入来源库。
                </div>
              </>
            )}
            {isEdit && base?.tiangong_last_error && (
              <div className="text-xs text-state-warning">最近采集：{base.tiangong_last_error}</div>
            )}
          </div>
        </Panel>
      </div>
    </Modal>
  )
}
