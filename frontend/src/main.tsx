import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { queryClient } from '@/api/queryClient'
import { initTheme } from '@/store/theme'
import './index.css'

initTheme()

const rootEl = document.getElementById('root')
if (!rootEl) throw new Error('缺少 #root 挂载节点')

createRoot(rootEl).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
)
