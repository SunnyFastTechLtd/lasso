import { useLayoutEffect } from 'react'
import { BrowserRouter, useLocation } from 'react-router-dom'
import { AppRoutes } from '@/router'
import { applyTheme, isLoginPath, useThemeStore } from '@/store/theme'

/** 路由切换时在绘制前同步主题：登录页强制浅色石墨，进入应用恢复用户偏好 */
function ThemeRouteSync() {
  const { pathname } = useLocation()
  const mode = useThemeStore((s) => s.mode)
  const skin = useThemeStore((s) => s.skin)

  useLayoutEffect(() => {
    const applied = applyTheme(mode, skin, isLoginPath(pathname))
    useThemeStore.setState({ resolved: applied })
  }, [pathname, mode, skin])

  return null
}

export default function App() {
  return (
    <BrowserRouter>
      <ThemeRouteSync />
      <AppRoutes />
    </BrowserRouter>
  )
}
