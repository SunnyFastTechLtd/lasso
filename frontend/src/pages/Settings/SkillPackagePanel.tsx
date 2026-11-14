import { useEffect, useState } from 'react'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Field, Input } from '@/components/ui/Field'
import { Icon } from '@/components/ui/Icon'
import { Panel } from '@/components/ui/Panel'
import { useSkillPackageConfig, useUpdateSkillPackage } from '@/api/settings'
import { toast } from '@/store/toast'
import { fmtBytes } from '@/utils/format'

/**
 * 决策 10：技能包允许上传的附件类型由管理员维护。
 * 出厂保守集；脚本类由管理员自行放开，变更写入审计（skill_attachment_types_update）。
 */
export function SkillPackagePanel() {
  const { data, isLoading } = useSkillPackageConfig()
  const save = useUpdateSkillPackage()
  const [draft, setDraft] = useState<string[]>([])
  const [input, setInput] = useState('')

  useEffect(() => {
    if (data) setDraft(data.allowed_extensions)
  }, [data])

  const denied = new Set(data?.denied_extensions ?? [])
  const dirty =
    !!data && (draft.length !== data.allowed_extensions.length ||
      draft.some((value, index) => value !== data.allowed_extensions[index]))

  function addExtension() {
    const raw = input.trim().toLowerCase()
    if (!raw) return
    const value = raw.startsWith('.') ? raw : `.${raw}`
    if (!/^\.[a-z0-9]{1,10}$/.test(value)) {
      toast.warning('扩展名格式不合法：只允许字母与数字，如 .py / .sh')
      return
    }
    if (denied.has(value)) {
      toast.warning(`${value} 属于可执行或二进制类型，不允许放开`)
      return
    }
    if (draft.includes(value)) {
      setInput('')
      return
    }
    setDraft((prev) => [...prev, value])
    setInput('')
  }

  async function submit() {
    if (draft.length === 0) {
      toast.warning('至少保留一种可上传的文本类型')
      return
    }
    try {
      const res = await save.mutateAsync(draft)
      toast.success(res.detail)
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  return (
    <Panel
      title="技能包附件类型"
      icon="FileCog"
      meta={data ? `规则集 ${data.ruleset_version}` : '读取中…'}
      actions={
        <>
          <Button
            quiet
            variant="ghost"
            icon="RotateCcw"
            disabled={!data || save.isPending}
            onClick={() => data && setDraft(data.default_extensions)}
          >
            恢复出厂默认
          </Button>
          <Button
            quiet
            variant="primary"
            icon="Check"
            loading={save.isPending}
            disabled={!dirty || draft.length === 0}
            onClick={() => void submit()}
          >
            保存
          </Button>
        </>
      }
      padded
    >
      <div className="col-stack gap-[10px]">
        <div className="field-hint">
          出厂只允许纯文本类附件；需要脚本类（.py / .sh / .js 等）时由管理员在此放开。
          可执行与二进制类型（exe/dll/zip/图片等）始终拒绝，不在可配置范围内。
        </div>

        <Field
          label="允许的扩展名"
          hint="保存后对下一次上传立即生效；已上传的附件不受影响，不会追溯删除"
        >
          <div className="flex flex-wrap gap-[6px]">
            {draft.map((ext) => (
              <span key={ext} className="chip mono" data-active="true">
                {ext}
                <button
                  type="button"
                  aria-label={`移除 ${ext}`}
                  className="ml-[4px] text-muted-foreground hover:text-state-error"
                  onClick={() => setDraft((prev) => prev.filter((x) => x !== ext))}
                  disabled={save.isPending}
                >
                  <Icon name="X" size={11} />
                </button>
              </span>
            ))}
            {draft.length === 0 && <span className="text-xs text-muted-foreground">（空，保存前必须至少加一项）</span>}
          </div>
        </Field>

        <div className="flex items-end gap-[8px]">
          <Field label="新增扩展名" className="w-[200px]">
            <Input
              value={input}
              placeholder=".py"
              disabled={save.isPending}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  addExtension()
                }
              }}
            />
          </Field>
          <Button variant="default" icon="Plus" disabled={!input.trim() || save.isPending} onClick={addExtension}>
            添加
          </Button>
        </div>

        <div className="flex flex-wrap items-center gap-[8px] text-xs text-muted-foreground">
          {isLoading ? (
            '读取配置…'
          ) : (
            <>
              <Badge tone="neutral">单个 ≤ {fmtBytes(data?.max_attachment_bytes)}</Badge>
              <Badge tone="neutral">最多 {data?.max_attachments} 个</Badge>
              <Badge tone="neutral">整包 ≤ {fmtBytes(data?.max_package_bytes)}</Badge>
              <span>始终拒绝：{data?.denied_extensions.slice(0, 12).join(' ')} …</span>
            </>
          )}
        </div>

        <div className="rounded-md border border-state-warning-border bg-state-warning-soft px-[10px] py-[8px] text-xs leading-relaxed text-state-warning">
          放开脚本类扩展名后，附件仍会做高危内容扫描：命中时上传者需明确确认放行，确认记录写入审计。
        </div>
      </div>
    </Panel>
  )
}
