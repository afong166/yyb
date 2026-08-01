<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api.js'
import { auth } from '../router.js'
import CountUp from '../components/CountUp.vue'
import TableSkeleton from '../components/TableSkeleton.vue'
import ProxySelector from '../components/ProxySelector.vue'

const accounts = ref([])
const stat = ref({ total: 0, active: 0, maxUsers: 0 })
const loading = ref(false)

// 搜索：按微信名 / openid 过滤
const search = ref('')
const filteredAccounts = computed(() => {
  const kw = search.value.trim().toLowerCase()
  if (!kw) return accounts.value
  return accounts.value.filter(
    (a) => (a.nickname || '').toLowerCase().includes(kw) || (a.openid || '').toLowerCase().includes(kw)
  )
})

// 分页（微信账号多时每页 5 条）
const page = ref(1)
const pageSize = 5
const pagedAccounts = computed(() => filteredAccounts.value.slice((page.value - 1) * pageSize, page.value * pageSize))
watch(search, () => { page.value = 1 })

// 自动续期（客户端引擎：到期前 N 分钟自动 refresh；配置持久化到 localStorage）
const AR_KEY = 'yyb_autorenew_v1'
// 默认开启自动续期：新用户/未手动配置过的浏览器打开网站即生效（到期前 5 分钟自动续期）；
// 后端 renew_loop 另有全局兜底，即使不开网页也会在临近到期时续期。
const autoRenew = ref({ enabled: true, leadMin: 5 })
const arDialog = ref(false)
const arCooldown = new Map()
let arTimer = null
let arRunning = false   // 防重入：60s 定时器可能在上一轮 tick 仍在续期时再次触发

function fmtTime(ts) {
  return ts ? new Date(ts).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }) : '—'
}
function remain(ts) {
  if (!ts) return '—'
  const s = Math.floor((ts - Date.now()) / 1000)
  if (s <= 0) return '已过期'
  const m = Math.floor(s / 60)
  if (m < 60) return `剩 ${m} 分钟`
  return `剩 ${Math.floor(m / 60)} 时 ${m % 60} 分`
}

function loadAR() {
  try {
    const r = localStorage.getItem(AR_KEY)
    if (r) Object.assign(autoRenew.value, JSON.parse(r))
  } catch {
    /* ignore */
  }
  if (!(autoRenew.value.leadMin > 0)) autoRenew.value.leadMin = 5
}
function startAR() {
  if (arTimer) return
  arTimer = setInterval(arTick, 60 * 1000)
  arTick()
}
function stopAR() {
  if (arTimer) {
    clearInterval(arTimer)
    arTimer = null
  }
}
async function arTick() {
  if (!autoRenew.value.enabled || arRunning) return
  arRunning = true
  try {
    let list = []
    try {
      list = (await api.accounts()).accounts || []
    } catch {
      return
    }
    const now = Date.now()
    const lead = autoRenew.value.leadMin * 60 * 1000
    const due = list.filter(
      (a) => a.status !== 'error' && a.expireAt && a.expireAt - now <= lead && now - (arCooldown.get(a.openid) || 0) > 90 * 1000
    )   // 跳过已标异常的账号：续期只能续 snsapi_login token，救不回应用宝取码授权，自动续期会把死号误刷成「正常」
    let ok = 0
    for (const a of due) {
      arCooldown.set(a.openid, Date.now())
      try {
        await api.refreshAccount(a.openid)
        ok++
      } catch {
        /* 单账号失败不阻断 */
      }
    }
    if (ok > 0 && arTimer) {   // arTimer 为空说明组件已卸载，跳过对已销毁组件的写入
      ElMessage.success(`自动续期：${ok} 个账号已续期`)
      loadAccounts()
    }
  } finally {
    arRunning = false
  }
}
function applyAR() {
  try {
    localStorage.setItem(AR_KEY, JSON.stringify(autoRenew.value))
  } catch {
    /* ignore */
  }
  if (autoRenew.value.enabled) startAR()
  else stopAR()
  arDialog.value = false
  ElMessage.success(
    autoRenew.value.enabled
      ? `已开启自动续期（到期前 ${autoRenew.value.leadMin} 分钟）`
      : '已关闭自动续期'
  )
}

// 扫码登录
const qr = ref('')
const scanning = ref(false)
const scanStarted = ref(false)
const starting = ref(false)
const loginProxy = ref({ proxyMode: 'direct', proxyUrl: '', proxyRegionCode: '', proxyRegionName: '' })
const loginSource = ref(1)   // 1=应用宝, 2=手游助手
let sessionId = ''
let pollTimer = null

// 获取操作（纯协议）：code / cloud(通用operateWXData) / encdata（手机号+加密数据）
//   / userinfo（wx.getUserInfo） / cloudfn（wx.cloud.callFunction） / cloudcontainer（wx.cloud.callContainer）
const mode = ref('code')
const codeForm = ref({
  openid: '', appid: '', param1: '', param2: '',
  // 云函数 callFunction
  functionName: '', functionData: '', cloudEnv: '',
  // 云托管 callContainer
  cloudHost: '', cloudPath: '', cloudMethod: 'GET', cloudHeaders: '', cloudData: '',
  cloudDirect: false
})
const cloudProxy = ref({ proxyMode: 'account', proxyUrl: '', proxyRegionCode: '', proxyRegionName: '' })
const codeResult = ref('')
const rawJson = ref('')
const elapsedMs = ref(0)
const showRaw = ref(false)
const codeLoading = ref(false)

const MODE_META = {
  code: { title: '获取 Code', btn: '获取 Code', okMsg: '获取 Code' },
  cloud: { title: '通用云操作', btn: '调用云操作', okMsg: '云操作调用' },
  encdata: { title: '获取encryptedData和iv', btn: '获取 encryptedData/iv', okMsg: '获取 encryptedData/iv' },
  userinfo: { title: '获取用户信息', btn: '获取用户信息', okMsg: '获取用户信息' },
  cloudfn: { title: '云函数', btn: '调用云函数', okMsg: '云函数调用' },
  cloudcontainer: { title: '云托管', btn: '调用云托管', okMsg: '云托管调用' }
}
const modeMeta = computed(() => MODE_META[mode.value])

async function loadAccounts() {
  loading.value = true
  try {
    const r = await api.accounts()
    accounts.value = r.accounts || []
    stat.value = { total: r.total, active: r.active, maxUsers: r.maxUsers }
    const maxPage = Math.max(1, Math.ceil(accounts.value.length / pageSize))
    if (page.value > maxPage) page.value = maxPage
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

function openScan() {
  qr.value = ''
  scanStarted.value = false
  scanning.value = true
}

async function startScan() {
  starting.value = true
  try {
    const r = await api.loginStart({ ...loginProxy.value, loginSource: loginSource.value })
    sessionId = r.sessionId
    qr.value = r.qrcodeDataUrl
    scanStarted.value = true
    if (pollTimer) clearInterval(pollTimer)   // 防御：重复开始时先清旧定时器，避免并存泄漏
    pollTimer = setInterval(pollScan, 1500)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    starting.value = false
  }
}

async function pollScan() {
  try {
    const r = await api.loginStatus(sessionId)
    if (r.status === 'success') {
      stopScan()
      ElMessage.success('登录成功：' + (r.account?.nickname || r.account?.openid || ''))
      loadAccounts()
    } else if (r.error) {
      stopScan()
      ElMessage.error(r.error)
    }
  } catch (e) {
    stopScan()
    ElMessage.error(e.message)
  }
}

function stopScan() {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = null
  scanning.value = false
  qr.value = ''
  scanStarted.value = false
  if (sessionId) api.loginStop(sessionId).catch(() => {})
  sessionId = ''
}

async function refreshAccount(openid) {
  try {
    await api.refreshAccount(openid)
    ElMessage.success('已续期')
    loadAccounts()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function delAccount(openid) {
  await ElMessageBox.confirm('确定删除该微信账号？', '提示', { type: 'warning' }).catch(() => 'cancel')
  try {
    await api.deleteAccount(openid)
    ElMessage.success('已删除')
    loadAccounts()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

function _primary(m, r) {
  if (m === 'code') return r?.code || r?.data?.code || ''
  if (m === 'encdata') {
    // 手机号 + 加密数据一起展示（同一后端调用）：先手机号，再 encryptedData / iv / cloudId / code
    const parts = []
    if (r?.mobile) {
      let s = '手机号：' + r.mobile
      const others = (r.customPhoneList || []).map((p) => p.mobile).filter(Boolean)
      if (others.length) s += '\n其它可选号码：' + others.join('、')
      parts.push(s)
    }
    if (r?.encryptedData) parts.push('encryptedData:\n' + r.encryptedData)
    if (r?.iv) parts.push('iv:\n' + r.iv)
    if (r?.cloudId) parts.push('cloudId:\n' + r.cloudId)
    if (r?.code) parts.push('code:\n' + r.code)
    return parts.join('\n\n') || r?.respJson || ''
  }
  if (m === 'userinfo') {
    // wx.getUserInfo：昵称/头像（来自 rawData）+ signature / encryptedData / iv / cloudId
    const parts = []
    let prof = {}
    try { prof = JSON.parse(r?.rawData || '{}') } catch { /* rawData 非 JSON 时忽略 */ }
    if (prof.nickName) parts.push('昵称：' + prof.nickName)
    if (prof.avatarUrl) parts.push('头像：' + prof.avatarUrl)
    if (r?.signature) parts.push('signature:\n' + r.signature)
    if (r?.encryptedData) parts.push('encryptedData:\n' + r.encryptedData)
    if (r?.iv) parts.push('iv:\n' + r.iv)
    if (r?.cloudId) parts.push('cloudId:\n' + r.cloudId)
    return parts.join('\n\n') || r?.respJson || ''
  }
  if (m === 'cloudfn' || m === 'cloudcontainer') {
    // 云函数 / 云托管：先给 WAF 状态提示，再展示解析后的 data / 兜底直连结果
    const pre = []
    if (r?.direct) {
      pre.push(r?.wafBlockedDirect
        ? '⚠ 直连仍被目标站 WAF 拦截。直连只能绕开「按网关出口 IP 拦截」这一类 WAF；若目标站是设备令牌/风控类 WAF（需 deviceToken 等客户端令牌），换 IP 也过不了'
        : '✓ 直连成功（已跳过腾讯网关，用' + (r?.viaProxy ? '代理' : '本机') + ' IP 直接打目标）')
    } else if (r?.wafBlocked) {
      pre.push(r?.wafFallback?.ok
        ? '⚠ 网关出口 IP 命中目标站 WAF，已用直连兜底成功' + (r?.viaProxy ? '（经代理）' : '（本机 IP）')
        : '⚠ 命中目标站 WAF：网关与直连均被拦。若是「按 IP 拦截」类 WAF，换个干净代理（住宅/移动/4G）可能过；若目标站用 deviceToken 等设备令牌风控（如东鹏），纯协议目前无法绕过')
    }
    let bodyText = ''
    if (r?.data !== undefined && r?.data !== null) {
      try { bodyText = JSON.stringify(r.data, null, 2) } catch { bodyText = r?.respJson || '' }
    } else if (r?.bodyText) {
      bodyText = r.bodyText
    } else {
      bodyText = r?.respJson || ''
    }
    return (pre.length ? pre.join('\n') + '\n\n' : '') + bodyText
  }
  // cloud（通用 operateWXData）
  return r?.respJson || ''
}

async function doAction() {
  const f = codeForm.value
  if (!f.openid || !f.appid) return ElMessage.warning('请选择账号并填写 appid')
  const m = mode.value
  // 云函数 / 云托管：前置校验 + 可选 JSON 解析（失败即停，不进请求）
  let fnData = {}
  let containerHeaders = {}
  if (m === 'cloudfn') {
    if (!f.functionName) return ElMessage.warning('请填写云函数名 functionName')
    if (f.functionData && f.functionData.trim()) {
      try { fnData = JSON.parse(f.functionData) } catch { return ElMessage.warning('functionData 不是合法 JSON') }
    }
  }
  if (m === 'cloudcontainer') {
    if (!f.cloudHost) return ElMessage.warning('请填写云托管域名 cloudHost')
    if (!f.cloudPath) return ElMessage.warning('请填写路径 path')
    if (f.cloudHeaders && f.cloudHeaders.trim()) {
      try { containerHeaders = JSON.parse(f.cloudHeaders) } catch { return ElMessage.warning('headers 不是合法 JSON') }
    }
  }
  codeLoading.value = true
  codeResult.value = ''
  rawJson.value = ''
  elapsedMs.value = 0
  const t0 = Date.now()
  try {
    const payload = { openid: f.openid, appid: f.appid }
    let r
    if (m === 'cloud') {
      payload.param1 = f.param1 || ''
      payload.param2 = f.param2 || ''
      r = await api.invokeCloud(payload)
    } else if (m === 'encdata') {
      // 手机号 + 加密数据同一个后端调用（get-phone），前端一并展示手机号与 encryptedData/iv
      payload.param2 = f.param2 || ''
      r = await api.getPhone(payload)
    } else if (m === 'userinfo') {
      // wx.getUserInfo：只需 openid/appid
      r = await api.getUserInfo(payload)
    } else if (m === 'cloudfn') {
      r = await api.cloudCallFunction({
        openid: f.openid, appid: f.appid,
        functionName: f.functionName, functionData: fnData, cloudEnv: f.cloudEnv || ''
      })
    } else if (m === 'cloudcontainer') {
      r = await api.cloudCallContainer({
        openid: f.openid, appid: f.appid,
        cloudHost: f.cloudHost, path: f.cloudPath, method: f.cloudMethod || 'GET',
        headers: containerHeaders, data: f.cloudData || '', ...cloudProxy.value,
        direct: !!f.cloudDirect
      })
    } else {
      r = await api.getCode(payload)
    }
    elapsedMs.value = Date.now() - t0
    rawJson.value = JSON.stringify(r, null, 2)
    const ok = r && r.success !== false && !r.error
    codeResult.value = _primary(m, r) || '（无直接结果，见下方原始 JSON）'
    showRaw.value = false   // 默认只显示过滤后的结果，原始 JSON 改为按需点击展开，避免加密字段一股脑堆出来
    if (ok) ElMessage.success(`${modeMeta.value.okMsg}成功 · 耗时 ${elapsedMs.value}ms`)
    else ElMessage.error(r?.error || `${modeMeta.value.okMsg}失败`)
  } catch (e) {
    elapsedMs.value = Date.now() - t0
    ElMessage.error(e.message)
  } finally {
    codeLoading.value = false
  }
}


async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选择')
  }
}

// 批量获取 Code：多选账号 + appid，一次并发取该小程序的 wx.login code
const batchDialog = ref(false)
const batchForm = ref({ appid: '', openids: [] })
const batchRunning = ref(false)
const batchResults = ref([])
const batchSummary = ref('')

function nickOf(openid) {
  const a = accounts.value.find((x) => x.openid === openid)
  return a ? a.nickname || openid : openid
}
function openBatch() {
  batchForm.value.appid = codeForm.value.appid || batchForm.value.appid || ''
  batchForm.value.openids = []
  batchResults.value = []
  batchSummary.value = ''
  batchDialog.value = true
}
function batchSelectAll() {
  batchForm.value.openids = accounts.value.map((a) => a.openid)
}
function batchClear() {
  batchForm.value.openids = []
}
async function runBatch() {
  const appid = batchForm.value.appid.trim()
  if (!appid) return ElMessage.warning('请填写小程序 appid')
  if (!batchForm.value.openids.length) return ElMessage.warning('请选择至少一个微信账号')
  batchRunning.value = true
  batchResults.value = []
  batchSummary.value = ''
  try {
    const r = await api.getCodes({ accounts: batchForm.value.openids, appid })
    batchResults.value = r.results || []
    batchSummary.value = r.summary || ''
    if (r.success) ElMessage.success(`批量获取完成 · ${r.summary || ''}`)
    else ElMessage.warning(`部分账号失败 · ${r.summary || ''}`)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    batchRunning.value = false
  }
}
function copyAllCodes() {
  const lines = batchResults.value
    .filter((r) => r.success && r.code)
    .map((r) => `${nickOf(r.openid)}\t${r.code}`)
  if (!lines.length) return ElMessage.warning('没有可复制的 Code')
  copyText(lines.join('\n'))
}

// 把已格式化的 JSON 字符串染色成带高亮的 HTML（键/字符串/数字/布尔/null 各配色）
function highlightJson(json) {
  if (!json) return ''
  const esc = json.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  return esc.replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false)\b|\bnull\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g,
    (m) => {
      let cls = 'jn'
      if (/^"/.test(m)) cls = /:$/.test(m) ? 'jk' : 'js'
      else if (/^(true|false)$/.test(m)) cls = 'jb'
      else if (m === 'null') cls = 'jnull'
      return `<span class="${cls}">${m}</span>`
    }
  )
}
const rawJsonHtml = computed(() => highlightJson(rawJson.value))

onMounted(() => {
  loadAccounts()
  loadAR()
  if (autoRenew.value.enabled) startAR()
})
onBeforeUnmount(() => {
  stopScan()
  stopAR()
})
</script>

<template>
  <div>
    <div class="stats">
      <div class="stat card hover rise rise-1"><div class="n"><CountUp :value="stat.total" /></div><div class="l">已绑定账号</div></div>
      <div class="stat card hover rise rise-2"><div class="n"><CountUp :value="stat.active" /></div><div class="l">正常</div></div>
      <div class="stat card hover rise rise-3"><div class="n"><CountUp :value="stat.maxUsers" /></div><div class="l">授权配额</div></div>
      <div class="stat card hover rise rise-4"><div class="n">{{ auth.license?.status === 'active' ? '有效' : '—' }}</div><div class="l">授权状态</div></div>
    </div>

    <div class="card mt">
      <div class="row wrap">
        <h3>微信账号</h3>
        <el-input v-model="search" placeholder="搜索微信名 / openid" clearable size="small" class="acc-search" />
        <el-tag v-if="autoRenew.enabled" size="small" type="success" effect="light" round>自动续期 · 到期前 {{ autoRenew.leadMin }} 分钟</el-tag>
        <div class="spacer" />
        <el-button size="small" @click="loadAccounts" :loading="loading">刷新</el-button>
        <el-button size="small" :type="autoRenew.enabled ? 'success' : 'default'" @click="arDialog = true">自动续期</el-button>
        <el-button size="small" type="primary" @click="openScan">扫码登录添加</el-button>
      </div>
      <TableSkeleton v-if="loading && !accounts.length" :rows="4" :cols="5" />
      <el-table v-else :data="pagedAccounts" v-loading="loading" size="small" style="width:100%" :empty-text="search.trim() ? '未找到匹配的微信账号' : '暂无账号，点击“扫码登录添加”'">
        <el-table-column label="账号" width="230" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="acc"><img v-if="row.headImgUrl" :src="row.headImgUrl" class="avatar" /><span>{{ row.nickname || row.openid }}</span><el-tag v-if="row.loginSource === 2" size="small" type="warning" effect="plain" round>手游</el-tag></div>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="90" align="center" header-align="center">
          <template #default="{ row }">
            <el-tooltip v-if="row.status === 'error' && row.statusError" :content="row.statusError" placement="top" effect="dark">
              <el-tag type="danger" size="small" style="cursor:help">异常</el-tag>
            </el-tooltip>
            <el-tag v-else :type="row.status === 'error' ? 'danger' : 'success'" size="small">{{ row.status === 'error' ? '异常' : '正常' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="登录时间" width="170" align="center" header-align="center">
          <template #default="{ row }"><span class="tcell">{{ fmtTime(row.loggedAt) }}</span></template>
        </el-table-column>
        <el-table-column label="到期时间" min-width="240" align="center" header-align="center">
          <template #default="{ row }">
            <span class="tcell">{{ fmtTime(row.expireAt) }}</span>
            <el-tag v-if="row.expireAt" size="small" effect="plain" :type="row.expireAt - Date.now() < 600000 ? 'warning' : 'info'" class="rtag">{{ remain(row.expireAt) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="代理" min-width="150" align="center" header-align="center">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :type="row.proxyMode === 'short' ? 'warning' : row.proxyMode === 'long' ? 'primary' : 'info'">
              {{ row.proxyMode === 'short' ? `短效 · ${row.proxyRegionName || '地区'}` : row.proxyMode === 'long' ? '长效代理' : '本机直连' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="240" align="center" header-align="center">
          <template #default="{ row }">
            <div class="ops">
              <el-button size="small" text @click="refreshAccount(row.openid)">续期</el-button>
              <el-button size="small" text @click="codeForm.openid = row.openid">选用</el-button>
              <el-button size="small" text type="danger" @click="delAccount(row.openid)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="filteredAccounts.length > pageSize" class="pager">
        <el-pagination
          small background layout="prev, pager, next, total"
          :page-size="pageSize" :total="filteredAccounts.length" v-model:current-page="page"
        />
      </div>
    </div>

    <el-dialog v-model="arDialog" title="自动续期设置" width="420">
      <div class="ar-body">
        <div class="ar-row">
          <div>
            <div class="ar-t">开启自动续期</div>
            <div class="ar-d">在账号到期前自动调用续期，保持登录态有效</div>
          </div>
          <el-switch v-model="autoRenew.enabled" />
        </div>
        <div class="ar-row">
          <div>
            <div class="ar-t">提前续期时间</div>
            <div class="ar-d">到期前多久开始自动续期</div>
          </div>
          <el-select v-model="autoRenew.leadMin" style="width:130px" :disabled="!autoRenew.enabled">
            <el-option :value="5" label="到期前 5 分钟" />
            <el-option :value="10" label="到期前 10 分钟" />
            <el-option :value="15" label="到期前 15 分钟" />
            <el-option :value="30" label="到期前 30 分钟" />
            <el-option :value="60" label="到期前 60 分钟" />
          </el-select>
        </div>
        <div class="ar-note">提示：自动续期在你打开本网站时生效（每分钟检查一次）；后端另有全局续期兜底，即使关闭网页也会在临近到期时续期。</div>
      </div>
      <template #footer>
        <el-button @click="arDialog = false">取消</el-button>
        <el-button type="primary" @click="applyAR">保存</el-button>
      </template>
    </el-dialog>

    <div class="card mt getcode">
      <div class="gc-head">
        <h3 class="gc-title">获取操作</h3>
        <div class="spacer" />
        <el-button size="small" type="primary" plain @click="openBatch">批量获取 Code</el-button>
      </div>
      <el-radio-group v-model="mode" class="mode-tabs">
        <el-radio-button value="code">获取 Code</el-radio-button>
        <el-radio-button value="cloud">通用云操作</el-radio-button>
        <el-radio-button value="encdata">获取encryptedData和iv</el-radio-button>
        <el-radio-button value="userinfo">获取用户信息</el-radio-button>
        <el-radio-button value="cloudfn">云函数</el-radio-button>
        <el-radio-button value="cloudcontainer">云托管</el-radio-button>
      </el-radio-group>
      <div class="form-grid">
        <div class="fg">
          <label>微信账号</label>
          <el-select v-model="codeForm.openid" placeholder="选择账号" size="large" style="width:100%">
            <el-option v-for="a in accounts" :key="a.openid" :label="a.nickname || a.openid" :value="a.openid" />
          </el-select>
        </div>
        <div class="fg">
          <label>小程序 appid</label>
          <el-input v-model="codeForm.appid" placeholder="如 wx1234567890abcdef" size="large" />
        </div>
        <div class="fg fg-btn">
          <el-button type="primary" size="large" :loading="codeLoading" @click="doAction" style="width:100%">{{ modeMeta.btn }}</el-button>
        </div>
      </div>

      <div v-if="mode === 'cloud'" class="extra-grid">
        <div class="fg">
          <label>param1 <span class="opt">（可选）</span></label>
          <el-input v-model="codeForm.param1" placeholder="云函数名 / 参数1" size="large" />
        </div>
        <div class="fg">
          <label>param2 <span class="opt">（可选，JSON）</span></label>
          <el-input v-model="codeForm.param2" placeholder='如 {"api_name":"...","data":{...}}' size="large" />
        </div>
      </div>
      <div v-else-if="mode === 'encdata'" class="extra-grid one">
        <div class="fg">
          <label>param2 <span class="opt">（可选，默认取微信绑定号码）</span></label>
          <el-input v-model="codeForm.param2" placeholder="留空即默认取手机号" size="large" />
        </div>
      </div>
      <div v-else-if="mode === 'cloudfn'" class="extra-grid">
        <div class="fg">
          <label>云函数名 functionName</label>
          <el-input v-model="codeForm.functionName" placeholder="如 login" size="large" />
        </div>
        <div class="fg">
          <label>云环境 cloudEnv <span class="opt">（可选）</span></label>
          <el-input v-model="codeForm.cloudEnv" placeholder="留空用默认环境" size="large" />
        </div>
        <div class="fg fg-wide">
          <label>functionData <span class="opt">（可选，JSON）</span></label>
          <el-input v-model="codeForm.functionData" placeholder='如 {"foo":"bar"}' size="large" />
        </div>
      </div>
      <div v-else-if="mode === 'cloudcontainer'" class="extra-grid">
        <div class="fg">
          <label>云托管域名 cloudHost</label>
          <el-input v-model="codeForm.cloudHost" placeholder="如 xxx.sh.wxcloudrun.com" size="large" />
        </div>
        <div class="fg">
          <label>请求方法 method</label>
          <el-select v-model="codeForm.cloudMethod" size="large" style="width:100%">
            <el-option value="GET" label="GET" />
            <el-option value="POST" label="POST" />
          </el-select>
        </div>
        <div class="fg fg-wide">
          <label>路径 / URL path</label>
          <el-input v-model="codeForm.cloudPath" placeholder="如 https://scan.xdp8.cn/...?code=xxx  或  /api/xxx" size="large" />
        </div>
        <div class="fg">
          <label>headers <span class="opt">（可选，JSON）</span></label>
          <el-input v-model="codeForm.cloudHeaders" placeholder='如 {"X-Request-App-Code":"xxx"}' size="large" />
        </div>
        <div class="fg">
          <label>data <span class="opt">（可选，POST body）</span></label>
          <el-input v-model="codeForm.cloudData" placeholder="POST 请求体（GET 留空）" size="large" />
        </div>
        <div class="fg fg-wide">
          <label>代理 proxyUrl <span class="opt">（仅对「按出口 IP 拦截」类 WAF 有用；留空回退账号绑定代理）</span></label>
          <ProxySelector v-model="cloudProxy" :allow-account="true" />
        </div>
        <div class="fg fg-wide">
          <div class="direct-row">
            <div>
              <div class="ar-t">强制直连（跳过腾讯网关）</div>
              <div class="ar-d">直接 HTTP 打目标 URL，只能绕开「网关出口 IP 被拦」类 WAF；目标站若需 deviceToken 等设备令牌风控（如东鹏），换 IP 也过不了。建议配一个干净代理（住宅/移动/4G）</div>
            </div>
            <el-switch v-model="codeForm.cloudDirect" />
          </div>
        </div>
      </div>

      <transition name="page">
        <div v-if="codeResult" class="result">
          <div class="rhead">
            <label class="rlabel">获取结果</label>
            <span v-if="elapsedMs" class="ms">耗时 {{ elapsedMs }} ms</span>
            <div class="spacer" />
            <el-button size="small" text @click="copyText(codeResult)">复制结果</el-button>
            <el-button size="small" text @click="showRaw = !showRaw">
              {{ showRaw ? '隐藏' : '查看' }}原始 JSON
            </el-button>
          </div>
          <el-input v-model="codeResult" type="textarea" :rows="['encdata','userinfo','cloudfn','cloudcontainer'].includes(mode) ? 8 : 3" readonly />
          <transition name="page">
            <div v-if="showRaw" class="raw">
              <div class="rhead">
                <label class="rlabel">原始响应 JSON</label>
                <div class="spacer" />
                <el-button size="small" text @click="copyText(rawJson)">复制 JSON</el-button>
              </div>
              <pre class="json-code"><code v-html="rawJsonHtml" /></pre>
            </div>
          </transition>
        </div>
      </transition>
    </div>

    <el-dialog v-model="batchDialog" title="批量获取 Code" width="620" class="batch-dlg">
      <div class="batch-body">
        <div class="fg">
          <label>小程序 appid</label>
          <el-input v-model="batchForm.appid" placeholder="如 wx1234567890abcdef" size="large" />
        </div>
        <div class="fg">
          <div class="bl-head">
            <label>选择微信账号<span class="opt">（已选 {{ batchForm.openids.length }} / {{ accounts.length }}）</span></label>
            <div class="spacer" />
            <el-button size="small" text @click="batchSelectAll">全选</el-button>
            <el-button size="small" text @click="batchClear">清空</el-button>
          </div>
          <el-select
            v-model="batchForm.openids" multiple filterable collapse-tags collapse-tags-tooltip
            placeholder="选择要取码的微信账号（可多选）" size="large" style="width:100%"
          >
            <el-option v-for="a in accounts" :key="a.openid" :label="a.nickname || a.openid" :value="a.openid" />
          </el-select>
        </div>
        <el-button type="primary" size="large" :loading="batchRunning" @click="runBatch" style="width:100%">
          {{ batchRunning ? '正在并发取码…' : '开始批量获取 Code' }}
        </el-button>

        <div v-if="batchResults.length" class="batch-result">
          <div class="rhead">
            <label class="rlabel">获取结果<span v-if="batchSummary" class="opt"> · {{ batchSummary }}</span></label>
            <div class="spacer" />
            <el-button size="small" text @click="copyAllCodes">复制全部 Code</el-button>
          </div>
          <el-table :data="batchResults" size="small" style="width:100%" max-height="320">
            <el-table-column label="账号" min-width="150" show-overflow-tooltip>
              <template #default="{ row }">{{ nickOf(row.openid) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="80" align="center">
              <template #default="{ row }">
                <el-tag :type="row.success ? 'success' : 'danger'" size="small">{{ row.success ? '成功' : '失败' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="Code / 错误" min-width="220">
              <template #default="{ row }">
                <code v-if="row.success" class="bcode">{{ row.code }}</code>
                <span v-else class="berr">{{ row.error || '失败' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="70" align="center">
              <template #default="{ row }">
                <el-button v-if="row.success && row.code" size="small" text @click="copyText(row.code)">复制</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>
      <template #footer>
        <el-button @click="batchDialog = false">关闭</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="scanning" title="微信扫码登录" width="380" @close="stopScan" class="scan-dlg">
      <div v-if="!scanStarted" class="scan-cfg">
        <el-select v-model="loginSource" size="large" style="width:100%">
          <el-option :value="1" label="应用宝" />
          <el-option :value="2" label="手游助手" />
        </el-select>
        <div class="tip">异地扫码请选择当地长效代理或 51短效代理地区；系统会把选择保存到账号，后续续期自动沿用。</div>
        <ProxySelector v-model="loginProxy" />
        <el-button type="primary" class="full" :loading="starting" @click="startScan">获取二维码</el-button>
      </div>
      <div v-else class="qr">
        <transition name="qrfade" appear>
          <img v-if="qr" :src="qr" />
        </transition>
        <p>请用微信扫码授权</p>
        <p v-if="loginProxy.proxyMode !== 'direct'" class="via">
          {{ loginProxy.proxyMode === 'short' ? `51短效：${loginProxy.proxyRegionName || '所选地区'}` : '经长效代理' }}
        </p>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.stats { display: grid; grid-template-columns: repeat(4,1fr); gap: 14px; }
.stat { text-align: center; padding: 18px; cursor: default; }
.stat .n { font-size: 26px; font-weight: 700; color: #2f6bf6; }
.stat .l { color: #888; font-size: 13px; margin-top: 4px; }
.mt { margin-top: 16px; }
.row { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.row.wrap { flex-wrap: wrap; }
.acc-search { width: 220px; }
@media (max-width: 560px) { .acc-search { width: 100%; } }
.spacer { flex: 1; }
h3 { margin: 0; }
.acc { display: flex; align-items: center; gap: 8px; }
.avatar { width: 26px; height: 26px; border-radius: 50%; }
.tcell { color: var(--ink-2); font-size: 13px; font-variant-numeric: tabular-nums; }
.rtag { margin-left: 8px; }
.ops { display: flex; flex-wrap: nowrap; align-items: center; justify-content: center; gap: 6px; }
.ops :deep(.el-button) { margin-left: 0 !important; padding: 4px 6px; }
.pager { display: flex; justify-content: flex-end; margin-top: 14px; }
/* 自动续期对话框 */
.ar-body { display: flex; flex-direction: column; gap: 18px; }
.ar-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.ar-t { font-weight: 600; font-size: 14px; }
.ar-d { color: var(--ink-3); font-size: 12px; margin-top: 3px; }
.ar-note { color: var(--ink-3); font-size: 12px; line-height: 1.7; background: #f6f8fc; padding: 12px 14px; border-radius: 10px; }
/* 获取操作表单 */
.gc-head { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
.gc-head .spacer { flex: 1; }
.getcode .gc-title { margin: 0; }
/* 批量获取 Code */
.batch-body { display: flex; flex-direction: column; gap: 16px; }
.bl-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.bl-head .spacer { flex: 1; }
.batch-result { margin-top: 4px; }
.bcode { font-family: ui-monospace, Consolas, monospace; font-size: 12.5px; color: var(--ink); word-break: break-all; }
.berr { color: #e6534d; font-size: 12.5px; }
.mode-tabs { margin-bottom: 20px; max-width: 100%; overflow-x: auto; white-space: nowrap; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr 160px; gap: 18px; align-items: end; }
.extra-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-top: 16px; }
.extra-grid.one { grid-template-columns: 1fr; }
.extra-grid .fg-wide { grid-column: 1 / -1; }
.direct-row { display: flex; align-items: center; justify-content: space-between; gap: 16px;
  background: #f6f8fc; padding: 12px 14px; border-radius: 10px; }
.fg { display: flex; flex-direction: column; gap: 8px; }
.fg label { font-size: 13px; color: var(--ink-2); font-weight: 600; }
.fg .opt { color: var(--ink-3); font-weight: 400; }
.result { margin-top: 20px; }
.rhead { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.rhead .spacer { flex: 1; }
.rlabel { font-size: 13px; color: var(--ink-2); font-weight: 600; }
.ms { font-size: 12px; color: var(--brand); background: var(--brand-50); padding: 2px 9px; border-radius: 20px; font-variant-numeric: tabular-nums; }
.raw { margin-top: 14px; }
.json-code {
  margin: 0; background: #0f1729; border: 1px solid #1c2740; border-radius: 12px;
  padding: 16px 18px; overflow: auto; max-height: 380px;
  font-family: ui-monospace, Consolas, 'SF Mono', monospace; font-size: 12.5px; line-height: 1.75;
  color: #cbd5e1; white-space: pre; tab-size: 2;
}
.json-code :deep(.jk) { color: #7cc4ff; }
.json-code :deep(.js) { color: #9ae6b4; }
.json-code :deep(.jn) { color: #f6ad55; }
.json-code :deep(.jb) { color: #d6bcfa; }
.json-code :deep(.jnull) { color: #8a93a6; font-style: italic; }
.json-code::-webkit-scrollbar { height: 8px; width: 8px; }
.json-code::-webkit-scrollbar-thumb { background: #2b3a5c; border-radius: 6px; }
@media (max-width: 760px) { .form-grid, .extra-grid { grid-template-columns: 1fr; } }
@media (max-width: 560px) {
  .stats { grid-template-columns: repeat(2, 1fr); }
  .stat { padding: 14px; }
  .stat .n { font-size: 22px; }
  .row { flex-wrap: wrap; }
  .row > .spacer { display: none; }
  .row .el-button { margin-left: 0; }
}

.qr { text-align: center; }
.qr img { width: 220px; height: 220px; }
.qr .via { color: var(--ink-3); font-size: 12px; margin-top: 2px; }
.scan-cfg { display: flex; flex-direction: column; gap: 14px; }
.scan-cfg .tip { color: var(--ink-2); font-size: 13px; line-height: 1.7; background: var(--brand-50); padding: 12px 14px; border-radius: 10px; }
.scan-cfg .full { width: 100%; }
.qrfade-enter-active { transition: opacity 0.4s var(--ease), transform 0.4s var(--ease); }
.qrfade-enter-from { opacity: 0; transform: scale(0.9); }
</style>
