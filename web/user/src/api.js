import axios from 'axios'

// 同源部署（后端托管前端）；开发用 vite 代理。凭 httpOnly cookie 鉴权。
const http = axios.create({
  withCredentials: true,
  headers: { 'X-Requested-With': 'XMLHttpRequest' }
})

// 会话失效(401)回调：由 router 注册，用于清空登录态并跳回登录页
let onUnauthorized = null
export function setUnauthorizedHandler(fn) { onUnauthorized = fn }

http.interceptors.response.use(
  (r) => r,
  (err) => {
    const status = err?.response?.status
    const url = err?.config?.url || ''
    // 登录/注册/取自身信息接口的 401 由各自流程处理，不触发全局跳转（避免登录页上误跳/循环）
    const isAuthEndpoint = /\/api\/auth\/(login|register|me)\b/.test(url)
    if (status === 401 && !isAuthEndpoint && onUnauthorized) {
      try { onUnauthorized() } catch { /* ignore */ }
    }
    const msg = err?.response?.data?.error || err?.message || '请求失败'
    return Promise.reject(new Error(msg))
  }
)

export const api = {
  // auth
  register: (d) => http.post('/api/auth/register', d).then((r) => r.data),
  login: (d) => http.post('/api/auth/login', d).then((r) => r.data),
  logout: () => http.post('/api/auth/logout').then((r) => r.data),
  me: () => http.get('/api/auth/me').then((r) => r.data),
  // accounts
  accounts: () => http.get('/api/accounts').then((r) => r.data),
  refreshAccount: (openid) => http.post('/api/accounts/refresh', { openid }).then((r) => r.data),
  deleteAccount: (openid) => http.post('/api/accounts/delete', { openid }).then((r) => r.data),
  proxyCapabilities: () => http.get('/api/proxy/capabilities').then((r) => r.data),
  proxyRegions: (parentCode = '') => http.get('/api/proxy/regions', { params: parentCode ? { parentCode } : {} }).then((r) => r.data),
  // login (扫码)
  loginStart: (d, legacyLoginSource = 1) => {
    const body = typeof d === 'string' ? { ...(d ? { proxyUrl: d, proxyMode: 'long' } : {}), loginSource: legacyLoginSource } : d
    return http.post('/api/login/start', body || {}).then((r) => r.data)
  },
  loginStatus: (sessionId) => http.get('/api/login/status', { params: { sessionId } }).then((r) => r.data),
  loginStop: (sessionId) => http.post('/api/login/stop', { sessionId }).then((r) => r.data),
  // 取码 / 云函数 / 手机号
  getCode: (d) => http.post('/api/yyb/get-code', d).then((r) => r.data),
  getCodes: (d) => http.post('/api/yyb/get-codes', d).then((r) => r.data),
  invokeCloud: (d) => http.post('/api/yyb/invoke-cloud', d).then((r) => r.data),
  getPhone: (d) => http.post('/api/yyb/get-phone', d).then((r) => r.data),
  getUserInfo: (d) => http.post('/api/yyb/get-userinfo', d).then((r) => r.data),
  cloudCallFunction: (d) => http.post('/api/yyb/cloud-call-function', d).then((r) => r.data),
  cloudCallContainer: (d) => http.post('/api/yyb/cloud-call-container', d).then((r) => r.data),
  // projects
  projects: () => http.get('/api/projects').then((r) => r.data),
  project: (id) => http.get(`/api/projects/${id}`).then((r) => r.data),
  runProject: (id, params) => http.post(`/api/projects/${id}/run`, { params }).then((r) => r.data),
  runProjectStart: (id, params) => http.post(`/api/projects/${id}/run-start`, { params }).then((r) => r.data),
  runProjectPoll: (id, runId, cursor) =>
    http.get(`/api/projects/${id}/run-poll`, { params: { runId, cursor } }).then((r) => r.data),
  submitProject: (id, params) => http.post(`/api/projects/${id}/submit`, { params }).then((r) => r.data),
  // 定时任务
  tasks: () => http.get('/api/tasks').then((r) => r.data),
  createTask: (d) => http.post('/api/tasks', d).then((r) => r.data),
  updateTask: (id, d) => http.put(`/api/tasks/${id}`, d).then((r) => r.data),
  toggleTask: (id) => http.post(`/api/tasks/${id}/toggle`).then((r) => r.data),
  deleteTask: (id) => http.delete(`/api/tasks/${id}`).then((r) => r.data),
  runTask: (id) => http.post(`/api/tasks/${id}/run`).then((r) => r.data),
  runTaskStart: (id) => http.post(`/api/tasks/${id}/run-start`).then((r) => r.data),
  runTaskPoll: (id, runId, cursor) =>
    http.get(`/api/tasks/${id}/run-poll`, { params: { runId, cursor } }).then((r) => r.data),
  // panels
  panels: () => http.get('/api/panels').then((r) => r.data),
  savePanel: (type, d) => http.put(`/api/panels/${type}`, d).then((r) => r.data),
  testPanel: (type, d) => http.post(`/api/panels/${type}/test`, d || {}).then((r) => r.data),
  deletePanel: (type) => http.delete(`/api/panels/${type}`).then((r) => r.data)
}

export default http
