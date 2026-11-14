import type { NavKey } from '@/types/nav'

export interface NavItem {
  key: NavKey
  label: string
  icon: string
  path: string
  /** 是否已在本期实现（false 为路由占位） */
  built: boolean
  /** 仅管理员可见；路由侧还有 RequireAdmin 硬拦截 */
  adminOnly?: boolean
}

// 导航词汇与设计稿 rail 一致，不得随意改名
export const navItems: NavItem[] = [
  { key: 'chat', label: '对话工作台', icon: 'MessageSquare', path: '/chat', built: true },
  { key: 'agent-builder', label: '智能体装配', icon: 'Bot', path: '/agents', built: true },
  { key: 'mcp-servers', label: '工具', icon: 'Plug', path: '/mcp', built: true },
  { key: 'skills', label: '技能', icon: 'Puzzle', path: '/skills', built: true },
  { key: 'knowledge', label: '知识库', icon: 'BookOpen', path: '/knowledge', built: true },
  { key: 'usage', label: '用量看板', icon: 'BarChart3', path: '/usage', built: true },
  { key: 'users', label: '用户管理', icon: 'Users', path: '/users', built: true, adminOnly: true },
  { key: 'departments', label: '部门管理', icon: 'Building2', path: '/departments', built: true, adminOnly: true },
  { key: 'settings', label: '系统设置', icon: 'Settings', path: '/settings', built: true },
]

export const navLabel = (key: NavKey): string =>
  navItems.find((item) => item.key === key)?.label ?? ''
