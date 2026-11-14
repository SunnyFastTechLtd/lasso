import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/Button'
import { Drawer } from '@/components/ui/Overlay'
import { Field, Input, Select } from '@/components/ui/Field'
import { Panel } from '@/components/ui/Panel'
import { PasswordHint } from '@/components/ui/PasswordHint'
import type { AdminUser, AdminUserPayload } from '@/api/users'
import type { Department } from '@/api/departments'
import { passwordProblems, readableError } from '@/utils/password'

interface Props {
  open: boolean
  /** null＝新建 */
  user: AdminUser | null
  departments: Department[]
  onClose: () => void
  /** 返回 Promise：父组件负责“降级二次确认”，表单据失败展示可读错误 */
  onSubmit: (payload: AdminUserPayload) => Promise<void>
}

/** 用户名与 Django 默认 User 字段口径一致；建号后不可改（会话与审计都以它为准） */
const USERNAME_RE = /^[\w.@+-]+$/

export function UserForm({ open, user, departments, onClose, onSubmit }: Props) {
  const isEdit = user !== null

  const [username, setUsername] = useState('')
  const [nickname, setNickname] = useState('')
  const [email, setEmail] = useState('')
  const [phone, setPhone] = useState('')
  const [position, setPosition] = useState('')
  const [department, setDepartment] = useState('')
  const [role, setRole] = useState<'user' | 'admin'>('user')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [errors, setErrors] = useState<string[]>([])
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (!open) return
    setUsername(user?.username ?? '')
    setNickname(user?.nickname ?? '')
    setEmail(user?.email ?? '')
    setPhone(user?.phone ?? '')
    setPosition(user?.position ?? '')
    setDepartment(user?.department ?? '')
    setRole(user?.is_staff || user?.is_superuser ? 'admin' : 'user')
    setPassword('')
    setConfirm('')
    setErrors([])
    setSubmitting(false)
  }, [open, user])

  const roleLocked = Boolean(user?.is_superuser || user?.is_last_admin)

  async function submit() {
    const next: string[] = []
    const trimmedUsername = username.trim()
    if (!isEdit) {
      if (!trimmedUsername) next.push('请填写用户名')
      else if (!USERNAME_RE.test(trimmedUsername)) next.push('用户名只能包含字母、数字及 @ . + - _')
      const problems = passwordProblems(password, trimmedUsername)
      if (problems.length) next.push(`密码不符合要求：${problems.join('、')}`)
      if (password !== confirm) next.push('两次输入的密码不一致')
    }
    if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim())) next.push('邮箱格式不正确')
    if (next.length) {
      setErrors(next)
      return
    }

    const payload: AdminUserPayload = {
      nickname: nickname.trim(),
      email: email.trim(),
      phone: phone.trim(),
      position: position.trim(),
      department: department.trim(),
    }
    if (!roleLocked) payload.is_staff = role === 'admin'
    if (!isEdit) {
      payload.username = trimmedUsername
      payload.password = password
    }

    setErrors([])
    setSubmitting(true)
    try {
      await onSubmit(payload)
    } catch (err) {
      setErrors([readableError(err)])
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={isEdit ? `编辑账号 · ${user?.username}` : '新建账号'}
      meta={isEdit ? '用户名不可修改；角色变更会立即影响其管理入口' : '密码由管理员设置，账号可稍后再启用'}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={submitting}>
            取消
          </Button>
          <Button variant="primary" icon="Check" loading={submitting} onClick={submit}>
            {isEdit ? '保存修改' : '创建账号'}
          </Button>
        </>
      }
    >
      {errors.length > 0 && (
        <div className="mb-[10px] rounded-md border border-state-error-border bg-state-error-soft px-[10px] py-[8px] text-xs leading-relaxed text-state-error">
          {errors.map((e, i) => (
            <div key={i}>{e}</div>
          ))}
        </div>
      )}

      <div className="col-stack">
        <Panel title="账号信息" icon="User" padded>
          <div className="flex flex-col gap-[10px]">
            <Field
              label="用户名"
              required={!isEdit}
              hint={isEdit ? '用户名创建后不可修改（会话与审计记录以此关联）' : '登录名，创建后不可修改'}
            >
              <Input
                value={username}
                disabled={isEdit}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="如：zhangsan"
                maxLength={150}
                autoComplete="off"
              />
            </Field>
            <Field label="昵称">
              <Input value={nickname} onChange={(e) => setNickname(e.target.value)} placeholder="对外显示名" maxLength={50} />
            </Field>
            <Field label="邮箱">
              <Input
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="name@example.com"
                maxLength={254}
                autoComplete="off"
              />
            </Field>
            <Field label="手机号">
              <Input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="选填" maxLength={20} autoComplete="off" />
            </Field>
            <Field label="职位">
              <Input value={position} onChange={(e) => setPosition(e.target.value)} placeholder="如：产品经理" maxLength={64} />
            </Field>
            <Field label="部门" hint="部门需先在「部门管理」中创建；留空表示未分配">
              <Select value={department} onChange={(e) => setDepartment(e.target.value)}>
                <option value="">未分配</option>
                {departments.map((d) => (
                  <option key={d.id} value={d.name}>
                    {d.name}
                  </option>
                ))}
              </Select>
            </Field>
            <Field
              label="角色"
              hint={
                user?.is_superuser
                  ? '超级管理员的权限不由本页调整'
                  : user?.is_last_admin
                    ? '这是系统中最后一个管理员，不能降级；请先添加另一位管理员'
                    : '管理员可进入用户管理、部门管理与系统设置；降级为普通用户需要再次确认'
              }
            >
              <Select
                value={role}
                disabled={roleLocked}
                onChange={(e) => setRole(e.target.value as 'user' | 'admin')}
              >
                <option value="user">普通用户</option>
                <option value="admin">管理员</option>
              </Select>
            </Field>
          </div>
        </Panel>

        {!isEdit && (
          <Panel title="初始密码" icon="KeyRound" padded>
            <div className="flex flex-col gap-[10px]">
              <Field label="密码" required>
                <Input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="new-password"
                />
              </Field>
              <Field label="确认密码" required>
                <Input
                  type="password"
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  autoComplete="new-password"
                />
              </Field>
              <PasswordHint password={password} username={username} />
            </div>
          </Panel>
        )}
      </div>
    </Drawer>
  )
}
