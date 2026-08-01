import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'url'

// 用户端 SPA。构建到 dist/web/user，由后端在 / 托管。
export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  base: '/',
  plugins: [vue()],
  build: {
    outDir: fileURLToPath(new URL('../../dist/web/user', import.meta.url)),
    emptyOutDir: true
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:18273', changeOrigin: true, cookieDomainRewrite: '' },
      '/wx': { target: 'http://127.0.0.1:18273', changeOrigin: true }
    }
  }
})
