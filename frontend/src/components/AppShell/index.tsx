import { useEffect, useRef, useState, type ReactNode } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { navItems } from '@/config/menu'
import { APP_CONFIG } from '@/config/app'
import logo from '@/assets/logo.png'
import { Icon } from '@/components/ui/Icon'
import { IconButton } from '@/components/ui/Button'
import { Toaster } from '@/components/ui/Toaster'
import { authApi, signOut } from '@/api/auth'
import { useConversations } from '@/api/hooks/useChat'
import { useUiStore } from '@/store/ui'
import { useUserStore } from '@/store/user'
import { useChatStatusStore } from '@/store/chatStatus'
import { initials } from '@/utils/format'

/** 当前一级导航标题（面包屑与左栏 active 共用） */
function useCurrentLabel(pathname: string) {
  const item = navItems.find((n) => pathname === n.path || pathname.startsWith(`${n.path}/`))
  return item?.label ?? '工作台'
}

function Rail({ activeLabel }: { activeLabel: string }) {
  const navigate = useNavigate()
  const collapsed = useUiStore((s) => s.railCollapsed)
  const toggleRail = useUiStore((s) => s.toggleRail)
  const user = useUserStore((s) => s.user)
  const visibleItems = navItems.filter((item) => !item.adminOnly || user?.is_staff)

  // Ctrl+B 折叠侧栏（状态栏有对应快捷键提示）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'b') {
        e.preventDefault()
        toggleRail()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [toggleRail])

  return (
    <nav className="rail" data-collapsed={String(collapsed)} aria-label="一级导航">
      <div className="rail-top">
        <button type="button" className="rail-workspace" onClick={() => navigate('/agents')}>
          <span className="ws-mark">
            <img src={logo} alt="" />
          </span>
          <span className="rail-cond ws-text">
            <span className="ws-name">{APP_CONFIG.shortName}</span>
          </span>
          <Icon name="ChevronDown" size={14} className="rail-cond ws-chev" />
        </button>
      </div>

      <div className="rail-nav">
        <div className="section-label rail-cond">控制台</div>
        {visibleItems.map((item) => (
          <NavLink
            key={item.key}
            to={item.path}
            className="nav-item"
            data-active={String(activeLabel === item.label)}
            title={item.label}
          >
            <Icon name={item.icon} size={15} />
            <span className="rail-cond rail-label">{item.label}</span>
            {!item.built && <span className="rail-cond badge badge-neutral">占位</span>}
          </NavLink>
        ))}
      </div>

      <div className="rail-foot">
        <div className="rail-cond section-label">当前账号</div>
        <ul className="rail-health-list rail-cond">
          <li>
            <span className="hk">部门</span>
            <span className="hv">{user?.department || '未登记'}</span>
          </li>
          <li>
            <span className="hk">权限</span>
            <span className="hv">{user?.is_staff ? '管理端' : '使用端'}</span>
          </li>
        </ul>
      </div>
    </nav>
  )
}

/** 右上角用户菜单：只暴露后端真实存在的能力 */
function UserMenu() {
  const [open, setOpen] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)
  const user = useUserStore((s) => s.user)
  const setUser = useUserStore((s) => s.setUser)
  const navigate = useNavigate()

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  return (
    <div className="relative" ref={boxRef}>
      <button type="button" className="user-chip" onClick={() => setOpen((v) => !v)}>
        <span className="avatar">{initials(user?.nickname || user?.username)}</span>
        <span className="flex flex-col leading-[1.25]">
          <span className="user-name">{user?.nickname || user?.username || '未登录'}</span>
          <span className="user-acct">{user?.position || user?.username || '—'}</span>
        </span>
        <Icon name="ChevronDown" size={14} className="text-muted-foreground" />
      </button>

      {open && (
        <div className="absolute right-0 top-[38px] z-50 w-[220px] rounded-lg border border-border bg-popover p-[6px] shadow-popover">
          <div className="px-[8px] py-[6px] text-xs text-muted-foreground">
            账号 <span className="mono text-sidebar-foreground">{user?.username ?? '—'}</span>
          </div>
          <button
            type="button"
            className="nav-item w-full"
            onClick={() => {
              setOpen(false)
              navigate('/account')
            }}
          >
            <Icon name="KeyRound" size={14} />
            <span className="rail-label">账号设置</span>
          </button>
          <button
            type="button"
            className="nav-item w-full"
            onClick={async () => {
              setOpen(false)
              await signOut()
              navigate('/login')
            }}
          >
            <Icon name="LogOut" size={14} />
            <span className="rail-label">退出登录</span>
          </button>
          <button
            type="button"
            className="nav-item w-full"
            onClick={() => {
              setOpen(false)
              void authApi.profile().then(setUser).catch(() => undefined)
            }}
          >
            <Icon name="RefreshCw" size={14} />
            <span className="rail-label">刷新档案</span>
          </button>
        </div>
      )}
    </div>
  )
}

function StatusBar() {
  const rounds = useChatStatusStore((s) => s.rounds)
  // 复用会话列表接口的 count（与对话页同一 queryKey，不额外轮询）
  const { data: conversations } = useConversations()
  const total = conversations?.count ?? 0

  return (
    <footer className="statusbar">
      <span className="status-metric">
        <Icon name="Server" size={12} />
        <span>接口</span>
        <span className="status-value mono">{APP_CONFIG.apiBaseUrl}</span>
      </span>
      <span className="status-sep" />
      <span className="status-metric" title="我的会话总数（对话页同一接口）">
        <Icon name="MessagesSquare" size={12} />
        <span>对话</span>
        <span className="status-value tnum">{total}</span>
      </span>
      <span className="status-sep" />
      <span className="status-metric" title="当前会话的提问轮次（用户消息数）">
        <Icon name="HardDrive" size={12} />
        <span>本轮</span>
        <span className="status-value tnum">{rounds}</span>
      </span>
      <span className="status-right">
        <span>渲染 1440 基准</span>
        <span className="status-sep" />
        <span>
          折叠侧栏 <span className="kbd">Ctrl</span> <span className="kbd">B</span>
        </span>
      </span>
    </footer>
  )
}

export function AppShell() {
  const location = useLocation()
  const navigate = useNavigate()
  const collapsed = useUiStore((s) => s.railCollapsed)
  const toggleRail = useUiStore((s) => s.toggleRail)
  const setUser = useUserStore((s) => s.setUser)
  const activeLabel = useCurrentLabel(location.pathname)

  // 进入控制台先校准身份：管理员可能已把本账号降级/停用，本地缓存里的 is_staff
  // 不能让菜单与路由守卫继续放行。401 会由拦截器统一清会话并回登录页。
  useEffect(() => {
    let cancelled = false
    authApi
      .profile()
      .then((profile) => {
        if (!cancelled) setUser(profile)
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [setUser])

  return (
    <div className="app-shell" data-device="desktop">
      <Rail activeLabel={activeLabel} />

      <div className="app-main">
        <header className="topbar">
          <div className="topbar-leading">
            <IconButton icon={collapsed ? 'PanelLeftOpen' : 'PanelLeftClose'} label="折叠导航栏" onClick={toggleRail} />
            <span className="hairline-v" />
            <nav className="breadcrumb" aria-label="面包屑">
              <span className="crumb">工作台</span>
              <Icon name="ChevronRight" size={12} className="crumb-sep" />
              <span className="crumb crumb-current">{activeLabel}</span>
            </nav>
          </div>
          <div className="topbar-trailing">
            <IconButton icon="Activity" label="用量看板" onClick={() => navigate('/usage')} />
            <IconButton icon="Settings" label="系统设置" onClick={() => navigate('/settings')} />
            <span className="hairline-v" />
            <UserMenu />
          </div>
        </header>

        <main className="workarea">
          <Outlet />
        </main>

        <StatusBar />
      </div>

      <Toaster />
    </div>
  )
}

/** 页体容器：1440 上限 + 20px 纵向节奏 */
export function PageContainer({ children }: { children: ReactNode }) {
  return <div className="work-inner">{children}</div>
}

export function PageHeader({
  title,
  subtitle,
  actions,
  badge,
}: {
  title: string
  subtitle?: string
  actions?: ReactNode
  badge?: ReactNode
}) {
  return (
    <header className="flex flex-col gap-2">
      <div className="page-head-row">
        <h1 className="page-title">{title}</h1>
        {badge}
        {actions && <div className="page-actions">{actions}</div>}
      </div>
      {subtitle && <p className="page-subtitle">{subtitle}</p>}
    </header>
  )
}
