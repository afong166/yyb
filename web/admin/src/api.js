import axios from 'axios'

const http = axios.create({
  withCredentials: true,
  headers: { 'X-Requested-With': 'XMLHttpRequest' }
})
let onUnauthorized = null
export function setUnauthorizedHandler(fn) { onUnauthorized = fn }

http.interceptors.response.use(
  (r) => r,
  (err) => {
    const url = err?.config?.url || ''
    const isAuthEndpoint = /\/api\/admin\/(login|me)\b/.test(url)
    if (err?.response?.status === 401 && !isAuthEndpoint && onUnauthorized) {
      try { onUnauthorized() } catch { /* ignore */ }
    }
    return Promise.reject(new Error(err?.response?.data?.error || err?.message || '请求失败'))
  }
)

export const api = {
  login: (d) => http.post('/api/admin/login', d).then((r) => r.data),
  logout: () => http.post('/api/admin/logout').then((r) => r.data),
  me: () => http.get('/api/admin/me').then((r) => r.data),
  changePassword: (password) => http.post('/api/admin/change-password', { password }).then((r) => r.data),
  stats: () => http.get('/api/admin/stats').then((r) => r.data),
  users: (status) => http.get('/api/admin/users', { params: status ? { status } : {} }).then((r) => r.data),
  user: (id) => http.get(`/api/admin/users/${id}`).then((r) => r.data),
  approve: (id) => http.post(`/api/admin/users/${id}/approve`).then((r) => r.data),
  enable: (id) => http.post(`/api/admin/users/${id}/enable`).then((r) => r.data),
  disable: (id) => http.post(`/api/admin/users/${id}/disable`).then((r) => r.data),
  resetPassword: (id, password) => http.post(`/api/admin/users/${id}/reset-password`, { password }).then((r) => r.data),
  deleteUser: (id) => http.delete(`/api/admin/users/${id}`).then((r) => r.data),
  issueLicense: (id, d) => http.post(`/api/admin/users/${id}/authcode`, d).then((r) => r.data),
  licenseStatus: (id, status) => http.post(`/api/admin/users/${id}/authcode/status`, { status }).then((r) => r.data),
  deleteLicense: (id) => http.delete(`/api/admin/users/${id}/authcode`).then((r) => r.data),
  callRecords: (params) => http.get('/api/admin/call-records', { params }).then((r) => r.data),
  audit: () => http.get('/api/admin/audit').then((r) => r.data),
  // projects
  projects: () => http.get('/api/admin/projects').then((r) => r.data),
  project: (id) => http.get(`/api/admin/projects/${id}`).then((r) => r.data),
  createProject: (d) => http.post('/api/admin/projects', d).then((r) => r.data),
  updateProject: (id, d) => http.put(`/api/admin/projects/${id}`, d).then((r) => r.data),
  shelf: (id, on) => http.post(`/api/admin/projects/${id}/shelf`, { on }).then((r) => r.data),
  deleteProject: (id) => http.delete(`/api/admin/projects/${id}`).then((r) => r.data),
  proxySettings: () => http.get('/api/admin/proxy/settings').then((r) => r.data),
  saveProxySettings: (d) => http.put('/api/admin/proxy/settings', d).then((r) => r.data)
}
export default http
