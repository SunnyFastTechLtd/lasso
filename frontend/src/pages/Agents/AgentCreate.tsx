import { useState } from 'react'
import { Button } from '@/components/ui/Button'
import { Field, Input, Textarea } from '@/components/ui/Field'
import { Modal } from '@/components/ui/Overlay'
import { Switch } from '@/components/ui/Switch'
import { useCreateAgent } from '@/api/hooks/useAgents'
import { ApiError } from '@/api/request'
import { toast } from '@/store/toast'
import { extractFieldErrors } from '@/pages/Mcp/shared'
import type { Agent } from '@/types/agent'

interface Props {
  open: boolean
  onClose: () => void
  onCreated: (agent: Agent) => void
}

/** A2 新建向导：只定名称与用途，完整配置进装配页 */
export function AgentCreate({ open, onClose, onCreated }: Props) {
  const create = useCreateAgent()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [emptyDraft, setEmptyDraft] = useState(false)
  const [errors, setErrors] = useState<string[]>([])

  function reset() {
    setName('')
    setDescription('')
    setEmptyDraft(false)
    setErrors([])
  }

  async function submit() {
    if (!emptyDraft && (name.trim().length < 2 || name.trim().length > 30)) {
      toast.error('名称需 2-30 个字符')
      return
    }
    try {
      const agent = await create.mutateAsync(
        emptyDraft ? {} : { name: name.trim(), description: description.trim() },
      )
      toast.success(`已创建草稿「${agent.name}」，请完成装配后再发布`)
      reset()
      onCreated(agent)
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
      open={open}
      title="新建智能体"
      icon="Plus"
      onClose={() => {
        reset()
        onClose()
      }}
      footer={
        <>
          <Button onClick={() => {
            reset()
            onClose()
          }}>
            取消
          </Button>
          <Button variant="primary" loading={create.isPending} disabled={!emptyDraft && !name.trim()} onClick={submit}>
            创建并装配
          </Button>
        </>
      }
    >
      <div className="space-y-[12px]">
        {errors.length > 0 && (
          <div className="rounded border border-state-error/40 bg-state-error/10 px-[10px] py-[8px] text-xs leading-[1.6] text-state-error">
            {errors.map((e, i) => (
              <div key={i}>{e}</div>
            ))}
          </div>
        )}
        <div className="flex items-center justify-between rounded border border-border/60 px-[10px] py-[8px]">
          <div>
            <div className="text-xs font-medium">从空白草稿开始</div>
            <div className="text-xs text-muted-foreground">
              占位创建（名称与提示词由装配向导补全），用于分步装配演示
            </div>
          </div>
          <Switch checked={emptyDraft} onChange={setEmptyDraft} />
        </div>
        {!emptyDraft && (
          <>
            <Field
              label="名称"
              required
              hint="2-30 字；全平台唯一，重名会提示占用者；唯一编码按拼音自动生成"
            >
              <Input
                value={name}
                maxLength={30}
                placeholder="如：合同审查助手"
                onChange={(e) => setName(e.target.value)}
              />
            </Field>
            <Field label="用途描述" hint="展示在使用端卡片，写清楚这个智能体干什么">
              <Textarea
                rows={3}
                value={description}
                placeholder="面向 XX 场景，帮助完成 XX…"
                onChange={(e) => setDescription(e.target.value)}
              />
            </Field>
          </>
        )}
        <p className="text-xs leading-[1.6] text-muted-foreground">
          创建即为草稿：模型、提示词、能力挂载、权限与可见范围都在装配页完成；
          发布检查全部通过后才会进入使用端。
        </p>
      </div>
    </Modal>
  )
}
