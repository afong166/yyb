import { createRouter, createWebHashHistory } from 'vue-router'
import { reactive } from 'vue'
import { api, setUnauthorizedHandler } from './api.js'

const routes = [
  { path: '/login', component: () => import('./views/Login.vue'), meta: { guest: true } },
  { path: '/register', component: () => import('./views/Register.vue'), meta: { guest: true } },
  { path: '/', redirect: '/dashboard' },
  { path: '/dashboard', component: () => import('./views/Dashboard.vue') },
  { path: '/projects', component: () => import('./views/Projects.vue') },
  { path: '/projects/:id', component: () => import('./views/ProjectDetail.vue') },
  { path: '/tasks', component: () => import('./views/Tasks.vue') },
  { path: '/panels', component: () => import('./views/Panels.vue') },
  { path: '/api-docs', component: () => import('./views/ApiDocs.vue') },
  { path: '/changelog', component: () => import('./views/Changelog.vue') },
  { path: '/profile', component: () => import('./views/Profile.vue') }
]

// 使用 hash 路由，刷新 /#/changelog 时不会再请求服务器 /changelog，避免静态站/反代未配置 fallback 导致 404。
const router = createRouter({ history: createWebHashHistory('/'), routes })

// 简单的会话状态缓存（响应式：顶栏用户名等 UI 需随登录态更新）
export const auth = reactive({ user: null, license: null, loaded: false })

export async function refreshAuth() {
  try {
    const r = await api.me()
    auth.user = r.user
    auth.license = r.license
  } catch {
    auth.user = null
    auth.license = null
  }
  auth.loaded = true
  return auth.user
}

// 任意接口返回 401（会话过期）时：清空登录态并跳回登录页，避免 UI 仍显示已登录却处处失败
setUnauthorizedHandler(() => {
  auth.user = null
  auth.license = null
  if (router.currentRoute.value.path !== '/login') {
    router.push('/login')
  }
})

router.beforeEach(async (to) => {
  if (!auth.loaded) await refreshAuth()
  if (to.meta.guest) {
    if (auth.user) return '/dashboard'
    return true
  }
  if (!auth.user) return '/login'
  return true
})

// 空闲时预取所有路由分块，消除首次点击各页面时的网络加载卡顿。
export function preloadRoutes() {
  for (const r of routes) {
    if (typeof r.component === 'function') {
      try { r.component() } catch { /* 忽略预取失败，真正导航时会重试 */ }
    }
  }
}

export default router
