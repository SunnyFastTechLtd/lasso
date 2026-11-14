import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/Button'
import { Modal } from '@/components/ui/Overlay'
import { Field, Input } from '@/components/ui/Field'
import { PasswordHint } from '@/components/ui/PasswordHint'
import type { AdminUser } from '@/api/users'
import { passwordProblems, readableError } from '@/utils/password'

interface Props {
  target: AdminUser | null
  onClose: () => void
  onSubmit: (password: string) => Promise<void>
}

/** 管理员重置密码：明确写出「其它设备下线、当前 access 最长再活 60 分钟」的后果 */
export function ResetPasswordDialog({ target, onClose, onSubmit }: Props) {
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (!target) return
    setPassword('')
    setConfirm('')
    setError('')
    setSubmitting(false)
  }, [target])

  async function submit() {
    const problems = passwordProblems(password, target?.username)
    if (problems.length) {
      setError(`密码不符合要求：${problems.join('、')}`)
      return
    }
    if (password !== confirm) {
      setError('两次输入的密码不一致')
      return
    }
    setError('')
    setSubmitting(true)
    try {
      await onSubmit(password)
    } catch (err) {
      setError(readableError(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal
      open={target !== null}
      title={`重置密码 · ${target?.username ?? ''}`}
      icon="KeyRound"
      onClose={submitting ? () => undefined : onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={submitting}>
            取消
          </Button>
          <Button variant="primary" icon="Check" loading={submitting} onClick={submit}>
            确认重置
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-[10px]">
        <p className="text-xs leading-relaxed text-muted-foreground">
          重置后该账号的全部 refresh token 立即吊销，其它设备下线；已签发的 access token 最长 60 分钟内仍可访问，到期即失效。
          请通过安全渠道把新密码告知对方，并要求其首次登录后自行修改。
        </p>
        {error && (
          <div className="rounded-md border border-state-error-border bg-state-error-soft px-[10px] py-[8px] text-xs leading-relaxed text-state-error">
            {error}
          </div>
        )}
        <Field label="新密码" required>
          <Input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
          />
        </Field>
        <Field label="确认新密码" required>
          <Input
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
          />
        </Field>
        <PasswordHint password={password} username={target?.username} />
      </div>
    </Modal>
  )
}
