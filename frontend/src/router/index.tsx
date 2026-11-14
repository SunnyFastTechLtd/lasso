import type { ReactNode } from 'react'
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { AppShell, PageContainer } from '@/components/AppShell'
import { Button } from '@/components/ui/Button'
import { EmptyState } from '@/components/ui/EmptyState'
import LoginPage from '@/pages/Login'
import ChatPage from '@/pages/Chat'
import AgentsPage from '@/pages/Agents'
import McpPage from '@/pages/Mcp'
import SkillsPage from '@/pages/Skills'
import KnowledgePage from '@/pages/Knowledge'
import TiangongPage from '@/pages/Knowledge/Tiangong'
import UsagePage from '@/pages/Usage'
import SettingsPage from '@/pages/Settings'
import UsersPage from '@/pages/Users'
import DepartmentsPage from '@/pages/Departments'
import AccountPage from '@/pages/Account'
import { useUserStore } from '@/store/user'

/** 未登录统一回登录页，并记住来源 */
function RequireAuth({ children }: { children: ReactNode }) {
  const token = useUserStore((s) => s.token)
  const location = useLocation()
  if (!token) return <Navigate to="/login" state={{ from: location.pathname }} replace />
  return <>{children}</>
}

/**
 * 管理页硬拦截：菜单隐藏只是体验层，直接敲 URL 也必须挡住。
 * 身份以 AppShell 启动时用 /auth/profile/ 校准后的 store 为准（管理员降级后即时生效）。
 */
function RequireAdmin({ children }: { children: ReactNode }) {
  const user = useUserStore((s) => s.user)
  const navigate = useNavigate()
  if (!user?.is_staff) {
    return (
      <PageContainer>
        <EmptyState
          icon="ShieldAlert"
          title="仅管理员可访问"
          description="当前账号没有管理权限。如果刚刚被调整了角色，请刷新页面以同步最新身份。"
          action={<Button onClick={() => navigate('/agents')}>返回工作台</Button>}
        />
      </PageContainer>
    )
  }
  return <>{children}</>
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/"
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route index element={<Navigate to="/agents" replace />} />
        <Route path="chat" element={<ChatPage />} />
        <Route path="agents" element={<AgentsPage />} />
        <Route path="mcp" element={<McpPage />} />
        <Route path="skills" element={<SkillsPage />} />
        {/* 旧市场路由收敛进「工具」「技能」的第二个选项卡，保留链接可用 */}
        <Route path="market/mcp" element={<Navigate to="/mcp?tab=market" replace />} />
        <Route path="market/skills" element={<Navigate to="/skills?tab=market" replace />} />
        <Route path="knowledge" element={<KnowledgePage />} />
        <Route
          path="knowledge/tiangong"
          element={
            <RequireAdmin>
              <TiangongPage />
            </RequireAdmin>
          }
        />
        <Route path="usage" element={<UsagePage />} />
        <Route
          path="users"
          element={
            <RequireAdmin>
              <UsersPage />
            </RequireAdmin>
          }
        />
        <Route
          path="departments"
          element={
            <RequireAdmin>
              <DepartmentsPage />
            </RequireAdmin>
          }
        />
        <Route path="account" element={<AccountPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/agents" replace />} />
      </Route>
    </Routes>
  )
}
