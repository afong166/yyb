<script setup>
import { ref } from 'vue'

function authHeaders(auth) {
  if (auth === 'admin') {
    return 'Content-Type: application/json\nX-Requested-With: XMLHttpRequest\nCookie: yyb_admin_sid=…   # 管理员登录后自动携带'
  }
  return 'Content-Type: application/json'
}
const authLabel = { admin: '管理员会话（Cookie）', none: '无' }

const groups = [
  {
    title: '管理员认证',
    note: '首个管理员由后端启动时打印的「管理员令牌」引导：用户名 admin、密码=该令牌，登录后请立即改密。登录成功后下发独立的管理员会话 Cookie。',
    items: [
      { m: 'POST', p: '/api/admin/login', auth: 'none', d: '管理员登录', body: '{\n  "username": "admin",\n  "password": "<管理员令牌或已改的密码>"\n}', resp: '{\n  "success": true,\n  "admin": { "id": 1, "username": "admin" }\n}' },
      { m: 'POST', p: '/api/admin/logout', auth: 'admin', d: '退出', body: '', resp: '{ "success": true }' },
      { m: 'GET', p: '/api/admin/me', auth: 'admin', d: '当前管理员', body: '', resp: '{ "success": true, "admin": { "id": 1, "username": "admin" } }' },
      { m: 'POST', p: '/api/admin/change-password', auth: 'admin', d: '修改管理员密码（至少 8 位）', body: '{\n  "password": "newStrongPassword"\n}', resp: '{ "success": true }' }
    ]
  },
  {
    title: '用户与授权码',
    items: [
      { m: 'GET', p: '/api/admin/users?status=pending', auth: 'admin', d: '用户列表（status 可选：pending/active/disabled）', body: '', resp: '{\n  "success": true,\n  "users": [ { "id": 3, "username": "newbie01", "status": "active", "license": { "key": "XXXX-…", "maxUsers": 2, "usedCount": 0 } } ]\n}' },
      { m: 'GET', p: '/api/admin/users/:id', auth: 'admin', d: '用户详情：账号 + 授权码 + 绑定微信账号 + 调用记录', body: '', resp: '{\n  "success": true,\n  "user": { "id": 3, "username": "newbie01", "status": "active" },\n  "license": { "key": "XXXX-…", "maxUsers": 2 },\n  "wechatAccounts": [ … ],\n  "callRecords": [ … ], "callCount": 12\n}' },
      { m: 'POST', p: '/api/admin/users/:id/approve', auth: 'admin', d: '通过注册审核（置为 active）', body: '', resp: '{ "success": true, "user": { "id": 3, "status": "active" } }' },
      { m: 'POST', p: '/api/admin/users/:id/disable', auth: 'admin', d: '禁用用户', body: '', resp: '{ "success": true, "user": { "id": 3, "status": "disabled" } }' },
      { m: 'POST', p: '/api/admin/users/:id/enable', auth: 'admin', d: '启用用户', body: '', resp: '{ "success": true, "user": { "id": 3, "status": "active" } }' },
      { m: 'POST', p: '/api/admin/users/:id/reset-password', auth: 'admin', d: '重置用户密码', body: '{\n  "password": "newpass123"\n}', resp: '{ "success": true }' },
      { m: 'DELETE', p: '/api/admin/users/:id', auth: 'admin', d: '删除用户（连带授权码/微信账号/调用记录）', body: '', resp: '{ "success": true }' },
      { m: 'POST', p: '/api/admin/users/:id/authcode', auth: 'admin', d: '发放 / 更新该用户授权码（配额=可绑定微信账号数）', body: '{\n  "maxUsers": 3,\n  "note": "vip",\n  "expiresAt": 0   // 0=永久，或毫秒时间戳\n}', resp: '{\n  "success": true,\n  "license": { "key": "XXXX-XXXX-XXXX-XXXX", "maxUsers": 3, "status": "active" }\n}' },
      { m: 'POST', p: '/api/admin/users/:id/authcode/status', auth: 'admin', d: '启用 / 禁用授权码', body: '{\n  "status": "disabled"   // active | disabled\n}', resp: '{ "success": true, "license": { "status": "disabled" } }' },
      { m: 'DELETE', p: '/api/admin/users/:id/authcode', auth: 'admin', d: '删除该用户授权码', body: '', resp: '{ "success": true }' }
    ]
  },
  {
    title: '项目管理',
    items: [
      { m: 'GET', p: '/api/admin/projects', auth: 'admin', d: '全部项目', body: '', resp: '{\n  "success": true,\n  "projects": [ { "id": 1, "name": "京东签到", "status": "on", "panelType": "qinglong" } ]\n}' },
      { m: 'POST', p: '/api/admin/projects', auth: 'admin', d: '新增项目（runConfig 里 submitPanels=可提交的面板，envName=默认变量名）', body: '{\n  "name": "京东 Code 登录获取 Cookie",\n  "summary": "一键换取京东 Cookie",\n  "intro": "## 简介",\n  "tutorial": "## 使用教程",\n  "status": "on",\n  "runConfig": {\n    "builtin": "jd-code-login",\n    "appid": "wx73247c7819d61796",\n    "submitPanels": ["qinglong", "daidai"],\n    "envName": "JD_COOKIE"\n  }\n}', resp: '{ "success": true, "project": { "id": 2, "name": "…", "submitPanels": ["qinglong","daidai"] } }' },
      { m: 'PUT', p: '/api/admin/projects/:id', auth: 'admin', d: '编辑项目', body: '{\n  "summary": "更新后的简介",\n  "tutorial": "更新后的教程"\n}', resp: '{ "success": true, "project": { … } }' },
      { m: 'POST', p: '/api/admin/projects/:id/shelf', auth: 'admin', d: '上架 / 下架', body: '{\n  "on": true\n}', resp: '{ "success": true, "project": { "status": "on" } }' },
      { m: 'DELETE', p: '/api/admin/projects/:id', auth: 'admin', d: '删除项目', body: '', resp: '{ "success": true }' }
    ]
  },
  {
    title: '监控与审计',
    items: [
      { m: 'GET', p: '/api/admin/call-records?userId=&limit=200', auth: 'admin', d: '调用记录（userId 可选筛选某用户）', body: '', resp: '{\n  "success": true,\n  "records": [ { "user_id": 2, "action": "get-code", "appid": "wx…", "result": "ok", "ms": 812, "created_at": 1782900000000 } ]\n}' },
      { m: 'GET', p: '/api/admin/audit?limit=200', auth: 'admin', d: '管理员操作审计日志', body: '', resp: '{\n  "success": true,\n  "audit": [ { "admin_id": 1, "action": "license-issue", "target_type": "user", "target_id": "3" } ]\n}' },
      { m: 'GET', p: '/api/admin/stats', auth: 'admin', d: '概览统计（含 MMTLS 会话池监控）', body: '', resp: '{\n  "success": true,\n  "users": { "total": 4, "pending": 0, "active": 4 },\n  "licenses": 4,\n  "recentCalls": 500,\n  "sessionPool": {\n    "cached": 3, "live": 2, "expired": 1, "ttlSeconds": 600,\n    "totalRequests": 128, "hit0rtt": 96, "hitRelogin": 12, "rebuild": 20,\n    "reuseRate": 0.844\n  }\n}' }
    ]
  }
]
const mColor = { GET: '#2f9e44', POST: '#e6534d', PUT: '#e8912d', DELETE: '#c0392b' }

const drawer = ref(false)
const cur = ref(null)
function open(it) {
  cur.value = it
  drawer.value = true
}

// 去掉示例请求体里的 // 行内注释（字符串内部的 // 会被保留），供 cURL 直接使用
function stripJsonComments(s) {
  let out = '', inStr = false, esc = false
  for (let i = 0; i < s.length; i++) {
    const c = s[i]
    if (inStr) {
      out += c
      if (esc) esc = false
      else if (c === '\\') esc = true
      else if (c === '"') inStr = false
      continue
    }
    if (c === '"') { inStr = true; out += c; continue }
    if (c === '/' && s[i + 1] === '/') { while (i < s.length && s[i] !== '\n') i++; continue }
    out += c
  }
  return out
}
// 压成单行合法 JSON（去注释后 parse 再 stringify）；失败则退回单行文本
function compactBody(body) {
  const stripped = stripJsonComments(body)
  try { return JSON.stringify(JSON.parse(stripped)) }
  catch { return stripped.replace(/\s*\n\s*/g, ' ').trim() }
}
function curlHeaders(auth) {
  const h = [`-H 'Content-Type: application/json'`]
  if (auth === 'admin') {
    h.push(`-H 'X-Requested-With: XMLHttpRequest'`)
    h.push(`-H 'Cookie: yyb_admin_sid=<管理员会话Cookie>'`)
  }
  return h
}
// 生成可直接复制运行的完整 cURL：域名取当前站点，body 压成单行合法 JSON
function curlFor(it) {
  if (!it) return ''
  const origin = (typeof window !== 'undefined' && window.location && window.location.origin) || 'https://你的域名'
  const parts = [`curl -X ${it.m} '${origin}${it.p}'`, ...curlHeaders(it.auth)]
  if (it.body) parts.push(`-d '${compactBody(it.body)}'`)
  return parts.join(' \\\n  ')
}

const copied = ref('')
async function copy(t, tag = '') {
  try {
    await navigator.clipboard.writeText(t)
    copied.value = tag
    setTimeout(() => { if (copied.value === tag) copied.value = '' }, 1500)
  } catch {
    /* ignore */
  }
}
</script>

<template>
  <div>
    <h2 class="ph">接口文档</h2>
    <p class="pd">点击任意接口查看鉴权方式、请求头、请求体与响应示例。管理接口均需管理员会话 Cookie。</p>

    <div v-for="(g, gi) in groups" :key="g.title" class="card rise" :class="'rise-' + Math.min(gi + 1, 3)" style="margin-bottom:16px">
      <div class="ghead"><h3>{{ g.title }}</h3></div>
      <div v-if="g.note" class="gnote">{{ g.note }}</div>
      <div v-for="it in g.items" :key="it.m + it.p" class="ep" @click="open(it)">
        <span class="m" :style="{ background: mColor[it.m] }">{{ it.m }}</span>
        <code class="path">{{ it.p }}</code>
        <span class="d">{{ it.d }}</span>
        <span class="arrow">›</span>
      </div>
    </div>

    <el-drawer v-model="drawer" :title="cur ? cur.m + ' ' + cur.p : ''" size="560" direction="rtl">
      <div v-if="cur" class="detail">
        <p class="dd">{{ cur.d }}</p>
        <div class="sec curlsec">
          <div class="slabel">完整 cURL（复制后替换 &lt;管理员会话Cookie&gt; 与 :id 等占位即可调用）
            <el-button size="small" type="primary" @click="copy(curlFor(cur), 'curl')">{{ copied === 'curl' ? '已复制 ✓' : '复制 cURL' }}</el-button>
          </div>
          <pre class="code curl">{{ curlFor(cur) }}</pre>
        </div>
        <div class="sec">
          <div class="slabel">鉴权方式</div>
          <el-tag size="small" type="info" effect="light">{{ authLabel[cur.auth] }}</el-tag>
        </div>
        <div class="sec">
          <div class="slabel">请求头 <el-button size="small" text @click="copy(authHeaders(cur.auth), 'headers')">{{ copied === 'headers' ? '已复制 ✓' : '复制' }}</el-button></div>
          <pre class="code">{{ authHeaders(cur.auth) }}</pre>
        </div>
        <div class="sec" v-if="cur.body">
          <div class="slabel">请求体 <el-button size="small" text @click="copy(cur.body, 'body')">{{ copied === 'body' ? '已复制 ✓' : '复制' }}</el-button></div>
          <pre class="code">{{ cur.body }}</pre>
        </div>
        <div class="sec">
          <div class="slabel">响应示例 <el-button size="small" text @click="copy(cur.resp, 'resp')">{{ copied === 'resp' ? '已复制 ✓' : '复制' }}</el-button></div>
          <pre class="code">{{ cur.resp }}</pre>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.ph { margin: 0 0 4px; }
.pd { color: var(--ink-2); margin: 0 0 18px; font-size: 14px; }
.ghead { margin-bottom: 12px; }
h3 { margin: 0; }
.gnote { color: var(--ink-2); font-size: 13px; line-height: 1.75; background: var(--brand-50); padding: 12px 14px; border-radius: 10px; margin-bottom: 12px; }
.ep { display: flex; align-items: center; gap: 12px; padding: 11px 8px; border-top: 1px dashed var(--line-2); cursor: pointer; border-radius: 8px; transition: background 0.18s var(--ease); }
.ep:first-of-type { border-top: none; }
.ep:hover { background: var(--brand-50); }
.m { color: #fff; font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 6px; min-width: 60px; text-align: center; }
.path { font-family: ui-monospace, Consolas, monospace; font-size: 13px; color: var(--ink); }
.d { color: var(--ink-2); font-size: 13px; }
.arrow { margin-left: auto; color: var(--ink-3); font-size: 20px; }
.detail { padding: 4px 4px 24px; }
.dd { color: var(--ink); font-size: 14px; margin: 0 0 18px; line-height: 1.6; }
.sec { margin-bottom: 18px; }
.slabel { font-size: 13px; font-weight: 600; color: var(--ink-2); margin-bottom: 8px; display: flex; align-items: center; justify-content: space-between; }
.code { background: #0f1729; color: #d6e2ff; font-family: ui-monospace, Consolas, monospace; font-size: 12.5px; line-height: 1.65; padding: 14px 16px; border-radius: 10px; overflow: auto; white-space: pre-wrap; word-break: break-word; margin: 0; }
.curlsec { padding: 12px; border-radius: 12px; background: var(--brand-50); margin-bottom: 20px; }
.curlsec .slabel { color: var(--ink); font-weight: 700; }
.code.curl { background: #0b1220; color: #e3ecff; border: 1px solid #22406b; }
@media (max-width: 600px) {
  .ep { flex-wrap: wrap; gap: 8px 10px; padding: 12px 6px; }
  .path { word-break: break-all; }
  .d { flex-basis: 100%; order: 3; font-size: 12px; }
  .arrow { order: 2; }
}
</style>
