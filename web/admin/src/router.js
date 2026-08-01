import { createRouter, createWebHashHistory } from 'vue-router'
import { reactive } from 'vue'
import { api, setUnauthorizedHandler } from './api.js'

const routes = [
  { path: '/login', component: () => import('./views/Login.vue'), meta: { guest: true } },
  { path: '/', redirect: '/users' },
  { path: '/users', component: () => import('./views/Users.vue') },
  { path: '/users/:id', component: () => import('./views/UserDetail.vue') },
  { path: '/projects', component: () => import('./views/Projects.vue') },
  { path: '/call-records', component: () => import('./views/CallRecords.vue') },
  { path: '/audit', component: () => import('./views/Audit.vue') },
  { path: '/proxy-settings', component: () => import('./views/ProxySettings.vue') },
  { path: '/api-docs', component: () => import('./views/ApiDocs.vue') }
]

// 管理端也使用 hash 路由，避免 /admin/users 这类子页面刷新依赖反代 rewrite。
const router = createRouter({ history: createWebHashHistory('/admin/'), routes })

export const auth = reactive({ admin: null, loaded: false })

export async function refreshAuth() {
  try {
    auth.admin = (await api.me()).admin
  } catch {
    auth.admin = null
  }
  auth.loaded = true
  return auth.admin
}

// 任意接口返回 401（会话过期）时清空登录态并跳回登录页
setUnauthorizedHandler(() => {
  auth.admin = null
  if (router.currentRoute.value.path !== '/login') {
    router.push('/login')
  }
})

router.beforeEach(async (to) => {
  if (!auth.loaded) await refreshAuth()
  if (to.meta.guest) return auth.admin ? '/users' : true
  return auth.admin ? true : '/login'
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
