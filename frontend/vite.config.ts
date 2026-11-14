import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

// 开发约定：/api 与 /media 代理到本机 8000，后端需先启动。
// 生产由 nginx 托管 dist 并反代 /api，见 docs/DEPLOYMENT.md。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  server: {
    port: 5173,
    host: true,
    proxy: {
      '/api': {
        target: process.env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // 只做依赖分包，不改路由与懒加载：业务代码仍留在入口 chunk
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return undefined
          if (/node_modules\/(react|react-dom|scheduler)\//.test(id)) return 'vendor-react'
          if (id.includes('node_modules/react-router')) return 'vendor-router'
          if (id.includes('node_modules/@tanstack')) return 'vendor-query'
          if (id.includes('node_modules/lucide-react')) return 'vendor-icons'
          if (/node_modules\/(react-markdown|remark-|micromark|mdast-|hast-|unified|vfile|unist-|ccount|character-entities|decode-named|devlop|trim-lines|property-information|style-to-object|inline-style-parser|html-url-attributes|markdown-table|longest-streak|zwitch|bail|trough)/.test(id)) {
            return 'vendor-markdown'
          }
          return 'vendor'
        },
      },
    },
  },
})
