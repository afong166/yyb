import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'url'

// 管理端 SPA。base=/admin/，构建到 dist/web/admin，由后端在 /admin 托管。
export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  base: '/admin/',
  plugins: [vue()],
  build: {
    outDir: fileURLToPath(new URL('../../dist/web/admin', import.meta.url)),
    emptyOutDir: true
  },
  server: {
    port: 5174,
    proxy: {
      '/api': { target: 'http://127.0.0.1:18273', changeOrigin: true, cookieDomainRewrite: '' }
    }
  }
})
