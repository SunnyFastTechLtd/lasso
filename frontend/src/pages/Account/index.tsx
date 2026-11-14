import { useEffect, useState } from 'react'
import { PageContainer, PageHeader } from '@/components/AppShell'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Field, Input } from '@/components/ui/Field'
import { Icon } from '@/components/ui/Icon'
import { Panel } from '@/components/ui/Panel'
import { PasswordHint } from '@/components/ui/PasswordHint'
import { preferenceApi, type PreferenceItem, type PreferenceList } from '@/api/chat'
import { authApi } from '@/api/auth'
import { useUserStore } from '@/store/user'
import { toast } from '@/store/toast'
import { passwordProblems, readableError } from '@/utils/password'

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

/** 个人中心：本人资料 + 本人密码；部门/用户名/角色均只读，避免界面给出可编辑假象 */
export default function AccountPage() {
  const user = useUserStore((s) => s.user)
  const setUser = useUserStore((s) => s.setUser)
  const setTokens = useUserStore((s) => s.setTokens)

  const [nickname, setNickname] = useState('')
  const [email, setEmail] = useState('')
  const [phone, setPhone] = useState('')
  const [position, setPosition] = useState('')
  const [profileError, setProfileError] = useState('')
  const [savingProfile, setSavingProfile] = useState(false)

  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [passwordError, setPasswordError] = useState('')
  const [savingPassword, setSavingPassword] = useState(false)

  /** D8：平台替本人记住的跨会话偏好（只本人可见） */
  const [prefs, setPrefs] = useState<PreferenceList | null>(null)
  const [prefsLoading, setPrefsLoading] = useState(true)
  const [prefBusy, setPrefBusy] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    preferenceApi
      .list()
      .then((d) => alive && setPrefs(d))
      .catch(() => alive && setPrefs(null))
      .finally(() => alive && setPrefsLoading(false))
    return () => {
      alive = false
    }
  }, [])

  async function refreshPrefs() {
    try {
      setPrefs(await preferenceApi.list())
    } catch {
      /* 保持现有列表，下次操作再试 */
    }
  }

  /** D8：预置项「不再带入」是关闭而非删除，可随时恢复 */
  async function togglePref(agentId: number, item: PreferenceItem) {
    const key = `${agentId}:${item.content}`
    setPrefBusy(key)
    try {
      if (item.id === null) await preferenceApi.setPreset(agentId, item.content, !item.muted)
      else if (item.muted) await preferenceApi.restore(item.id)
      else await preferenceApi.remove(item.id)
      await refreshPrefs()
      toast.success(item.muted ? '已恢复带入' : '已不再带入')
    } catch (err) {
      toast.error(readableError(err))
    } finally {
      setPrefBusy(null)
    }
  }

  /** D8：自建项直接删除；没有行记录的预置项不在删除入口里 */
  async function deletePref(agentId: number, item: PreferenceItem) {
    if (item.id === null) return
    const key = `${agentId}:${item.content}`
    setPrefBusy(key)
    try {
      await preferenceApi.remove(item.id)
      await refreshPrefs()
      toast.success('已删除')
    } catch (err) {
      toast.error(readableError(err))
    } finally {
      setPrefBusy(null)
    }
  }

  useEffect(() => {
    setNickname(user?.nickname ?? '')
    setEmail(user?.email ?? '')
    setPhone(user?.phone ?? '')
    setPosition(user?.position ?? '')
  }, [user])

  async function saveProfile() {
    if (email && !EMAIL_RE.test(email.trim())) {
      setProfileError('邮箱格式不正确')
      return
    }
    setProfileError('')
    setSavingProfile(true)
    try {
      const updated = await authApi.updateProfile({
        nickname: nickname.trim(),
        email: email.trim(),
        phone: phone.trim(),
        position: position.trim(),
      })
      setUser(updated)
      toast.success('资料已更新')
    } catch (err) {
      setProfileError(readableError(err))
    } finally {
      setSavingProfile(false)
    }
  }

  async function savePassword() {
    if (!oldPassword) {
      setPasswordError('请填写当前密码')
      return
    }
    const problems = passwordProblems(newPassword, user?.username)
    if (problems.length) {
      setPasswordError(`新密码不符合要求：${problems.join('、')}`)
      return
    }
    if (newPassword !== confirmPassword) {
      setPasswordError('两次输入的新密码不一致')
      return
    }
    if (newPassword === oldPassword) {
      setPasswordError('新密码不能与当前密码相同')
      return
    }
    setPasswordError('')
    setSavingPassword(true)
    try {
      const res = await authApi.changePassword({
        old_password: oldPassword,
        new_password: newPassword,
        confirm_password: confirmPassword,
      })
      // 后端已吊销全部旧 refresh 并为当前设备签发了新 token：必须写回，
      // 否则 access 到期后会因刷新失败被自己的改密踢下线。
      setTokens(res.access, res.refresh)
      setOldPassword('')
      setNewPassword('')
      setConfirmPassword('')
      toast.success('密码已修改：其它设备已下线，当前会话已自动续期')
    } catch (err) {
      setPasswordError(readableError(err))
    } finally {
      setSavingPassword(false)
    }
  }

  return (
    <PageContainer>
      <PageHeader
        title="个人中心"
        subtitle="维护本人资料与登录密码；部门与角色由管理员调整"
        badge={<Badge tone={user?.is_staff ? 'primary' : 'neutral'}>{user?.is_staff ? '管理员' : '普通用户'}</Badge>}
      />

      <div className="mt-[14px] grid items-start gap-[10px] lg:grid-cols-2">
        <Panel title="基本资料" icon="User" padded>
          <div className="form-grid">
            <Field label="用户名" hint="登录名不可修改">
              <Input value={user?.username ?? ''} disabled />
            </Field>
            <Field label="部门" hint="部门归属影响可见范围，需由管理员调整">
              <Input value={user?.department || '未分配'} disabled />
            </Field>
            <Field label="昵称">
              <Input value={nickname} onChange={(e) => setNickname(e.target.value)} maxLength={50} />
            </Field>
            <Field label="职位">
              <Input value={position} onChange={(e) => setPosition(e.target.value)} maxLength={64} />
            </Field>
            <Field label="邮箱">
              <Input
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="name@example.com"
                maxLength={254}
                autoComplete="email"
              />
            </Field>
            <Field label="手机号">
              <Input value={phone} onChange={(e) => setPhone(e.target.value)} maxLength={20} autoComplete="tel" />
            </Field>
          </div>

          {profileError && <div className="mt-[8px] field-error">{profileError}</div>}

          <div className="mt-[10px] flex justify-end">
            <Button variant="primary" icon="Check" loading={savingProfile} onClick={saveProfile}>
              保存资料
            </Button>
          </div>
        </Panel>

        <Panel title="登录密码" icon="KeyRound" padded>
          <div className="flex flex-col gap-[10px]">
            <p className="text-xs leading-relaxed text-muted-foreground">
              修改成功后，其它设备上的登录态立即失效；当前页面会自动换用新令牌，不需要重新登录。
            </p>
            {passwordError && (
              <div className="rounded-md border border-state-error-border bg-state-error-soft px-[10px] py-[8px] text-xs leading-relaxed text-state-error">
                {passwordError}
              </div>
            )}
            <Field label="当前密码" required>
              <Input
                type="password"
                value={oldPassword}
                onChange={(e) => setOldPassword(e.target.value)}
                autoComplete="current-password"
              />
            </Field>
            <Field label="新密码" required>
              <Input
                type="password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                autoComplete="new-password"
              />
            </Field>
            <Field label="确认新密码" required>
              <Input
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                autoComplete="new-password"
              />
            </Field>
            <PasswordHint password={newPassword} username={user?.username} />
            <div className="flex justify-end">
              <Button variant="primary" icon="KeyRound" loading={savingPassword} onClick={savePassword}>
                修改密码
              </Button>
            </div>
          </div>
        </Panel>
      </div>

      <div className="mt-[10px]">
        <Panel title="平台替我记了什么" icon="Sparkles" padded>
          <div className="flex flex-col gap-[10px]">
            <p className="text-xs leading-relaxed text-muted-foreground">
              只记录你确认过的表达要求与常查对象，仅你本人可见；每个助手最多 {prefs?.max_items ?? 8} 条、
              单条最多 {prefs?.max_chars ?? 100} 字。
              {prefs ? `超过 ${prefs.stale_days} 天没被带入的条目会置灰提示，但不会自动删除。` : ''}
            </p>

            {prefsLoading ? (
              <div className="py-4 text-center text-xs text-muted-foreground">加载中…</div>
            ) : !prefs || prefs.groups.length === 0 ? (
              <div className="rounded-md border border-border bg-muted/40 px-[10px] py-3 text-xs text-muted-foreground">
                还没有记住任何偏好。在对话里对满意的回答点「以后都这样」，确认后就会出现在这里。
              </div>
            ) : (
              <div className="flex flex-col gap-[10px]">
                {prefs.groups.map((g) => (
                  <div key={g.agent.id} className="rounded-md border border-border">
                    <div className="flex items-center gap-1.5 border-b border-border bg-muted/30 px-[10px] py-1.5 text-xs">
                      <Icon name="Bot" size={12} className="text-primary" />
                      <span className="truncate text-sidebar-foreground">{g.agent.name}</span>
                      <span className="ml-auto text-muted-foreground">{g.items.length} 条</span>
                    </div>
                    <ul className="divide-y divide-border">
                      {g.items.map((item) => {
                        const key = `${g.agent.id}:${item.content}`
                        const busy = prefBusy === key
                        return (
                          <li
                            key={`${item.source}-${item.content}`}
                            className={`flex items-center gap-2 px-[10px] py-1.5 text-xs ${item.muted ? 'opacity-55' : ''}`}
                          >
                            <span className="min-w-0 flex-1 leading-[1.6]">
                              <span className={item.muted ? 'line-through' : ''}>{item.content}</span>
                              {item.source === 'preset' && (
                                <Badge tone="neutral" className="ml-1.5">助手预置</Badge>
                              )}
                              {item.stale && !item.muted && (
                                <Badge tone="warning" className="ml-1.5" title="长期没被带入，可能已不再需要">
                                  久未使用
                                </Badge>
                              )}
                            </span>
                            {item.source === 'preset' ? (
                              <Button
                                quiet
                                className="flex-none"
                                loading={busy}
                                onClick={() => void togglePref(g.agent.id, item)}
                              >
                                {item.muted ? '恢复' : '不再带入'}
                              </Button>
                            ) : (
                              <Button
                                quiet
                                className="flex-none"
                                loading={busy}
                                onClick={() => void deletePref(g.agent.id, item)}
                              >
                                删除
                              </Button>
                            )}
                          </li>
                        )
                      })}
                    </ul>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Panel>
      </div>
    </PageContainer>
  )
}
