import { useState, type FormEvent } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { APP_CONFIG } from '@/config/app'
import logo from '@/assets/logo.png'
import { signIn } from '@/api/auth'
import { Icon } from '@/components/ui/Icon'
import { Field, Input } from '@/components/ui/Field'
import { Button } from '@/components/ui/Button'
import type { ApiError } from '@/api/request'

const FEATURES = [
  { icon: 'Bot', text: '多智能体协同，任务自动编排' },
  { icon: 'Puzzle', text: '技能与工具接入，能力可扩展' },
  { icon: 'BookOpen', text: '知识库检索增强，回答有据可依' },
]

export default function LoginPage() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const navigate = useNavigate()
  const location = useLocation()

  const target = (location.state as { from?: string } | null)?.from || '/agents'

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (!username.trim() || !password) {
      setError('请输入用户名与密码')
      return
    }
    setLoading(true)
    setError('')
    try {
      await signIn({ username: username.trim(), password })
      navigate(target, { replace: true })
    } catch (err) {
      const apiErr = err as ApiError
      setError(apiErr.status === 401 ? '用户名或密码错误' : apiErr.detail || '登录失败，请稍后重试')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-page">
      <section className="login-brand">
        <div className="login-brand-inner">
          <span className="ws-mark login-logo">
            <img src={logo} alt="" />
          </span>
          <h1 className="login-brand-name">{APP_CONFIG.name}</h1>
          <p className="login-brand-subtitle">{APP_CONFIG.subtitle}</p>

          <ul className="login-features">
            {FEATURES.map((item) => (
              <li className="login-feature" key={item.text}>
                <span className="login-feature-icon">
                  <Icon name={item.icon} size={18} style={{ strokeWidth: 2 }} />
                </span>
                <span>{item.text}</span>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section className="login-form-panel">
        <div className="login-card">
          <h2 className="login-title">使用企业账号登录</h2>
          <p className="login-desc">账号由管理员统一分配</p>

          <form className="login-form" onSubmit={onSubmit}>
            <Field label="用户名">
              <Input
                autoFocus
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </Field>

            <Field label="密码">
              <Input
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </Field>

            {error && (
              <div
                className="badge badge-error"
                style={{ height: 'auto', padding: '6px 8px', whiteSpace: 'normal' }}
              >
                <Icon name="CircleAlert" size={12} />
                {error}
              </div>
            )}

            <Button type="submit" variant="primary" loading={loading} className="w-full">
              {loading ? '登录中' : '登录'}
            </Button>
          </form>

          <div className="login-note">
            <Icon name="Info" size={14} />
            <span>如需重置密码或调整权限，请联系平台管理员</span>
          </div>
        </div>
      </section>
    </div>
  )
}
