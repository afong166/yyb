<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api.js'
import CountUp from '../components/CountUp.vue'
import TableSkeleton from '../components/TableSkeleton.vue'

const records = ref([])
const total = ref(0)
const loading = ref(false)

// 筛选：用户ID（服务端）+ 操作/结果/关键词（本地）
const uid = ref('')
const fAction = ref('')
const fResult = ref('')
const kw = ref('')

// 分页（本地）
const page = ref(1)
const pageSize = 20

// 自动刷新
const auto = ref(false)
let autoTimer = null

const FETCH_LIMIT = 500

// 操作元数据：友好名称 + 标签配色
const ACTIONS = {
  'get-code': { label: '获取 Code', type: '' },
  'get-codes': { label: '批量 Code', type: 'info' },
  'jd-code-login': { label: '京东登录', type: 'warning' },
  'eleme-code-login': { label: '饿了么登录', type: 'warning' },
  'mxbc-code-login': { label: '蜜雪登录', type: 'warning' },
  'maidong-scan': { label: '脉动扫码', type: 'warning' },
  'nongwu-tavern': { label: '浓五酒馆', type: 'warning' },
  'submit-panel': { label: '提交面板', type: 'success' },
  'invoke-cloud': { label: '调用云函数', type: 'info' },
  'get-phone': { label: '获取手机号', type: 'warning' }
}
const actionMeta = (a) => ACTIONS[a] || { label: a || '—', type: 'info' }

function fmt(ts) {
  return ts ? new Date(ts).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }) : '—'
}
// 关联对象：取码类显示 appid，项目类（京东/提交面板）显示项目名
function relation(row) {
  return row.appid || row.projectName || ''
}

async function load() {
  loading.value = true
  try {
    const params = { limit: FETCH_LIMIT }
    if (uid.value) params.userId = uid.value
    const r = await api.callRecords(params)
    records.value = r.records || []
    total.value = r.total ?? records.value.length
    const maxPage = Math.max(1, Math.ceil(filtered.value.length / pageSize))
    if (page.value > maxPage) page.value = maxPage
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

const filtered = computed(() => {
  const k = kw.value.trim().toLowerCase()
  return records.value.filter((r) => {
    if (fAction.value && r.action !== fAction.value) return false
    if (fResult.value && r.result !== fResult.value) return false
    if (k) {
      const hay = `${r.username || ''} ${r.appid || ''} ${r.openid || ''} ${r.code || ''} ${r.projectName || ''} ${r.ip || ''}`.toLowerCase()
      if (!hay.includes(k)) return false
    }
    return true
  })
})
const paged = computed(() => filtered.value.slice((page.value - 1) * pageSize, page.value * pageSize))

// 统计（基于当前筛选结果）
const stat = computed(() => {
  const list = filtered.value
  const ok = list.filter((r) => r.result === 'ok').length
  const fail = list.length - ok
  const rate = list.length ? Math.round((ok / list.length) * 100) : 0
  return { count: list.length, ok, fail, rate }
})

function resetPage() {
  page.value = 1
}
function toggleAuto(v) {
  if (v) autoTimer = setInterval(load, 10000)
  else if (autoTimer) {
    clearInterval(autoTimer)
    autoTimer = null
  }
}

async function copy(t) {
  if (!t) return
  try {
    await navigator.clipboard.writeText(t)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败')
  }
}

onMounted(load)
onBeforeUnmount(() => autoTimer && clearInterval(autoTimer))
</script>

<template>
  <div>
    <div class="row">
      <h2>调用记录</h2>
      <div class="spacer" />
      <el-switch v-model="auto" active-text="自动刷新" inline-prompt @change="toggleAuto" />
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <!-- 统计条 -->
    <div class="stats">
      <div class="st"><div class="n"><CountUp :value="total" /></div><div class="l">累计调用</div></div>
      <div class="st"><div class="n"><CountUp :value="stat.count" /></div><div class="l">当前筛选</div></div>
      <div class="st ok"><div class="n"><CountUp :value="stat.ok" /></div><div class="l">成功</div></div>
      <div class="st fail"><div class="n"><CountUp :value="stat.fail" /></div><div class="l">失败</div></div>
      <div class="st"><div class="n"><CountUp :value="stat.rate" />%</div><div class="l">成功率</div></div>
    </div>

    <!-- 筛选条 -->
    <div class="filters">
      <el-input v-model="uid" placeholder="用户ID" style="width:120px" clearable @clear="load" @keyup.enter="load" />
      <el-select v-model="fAction" placeholder="操作类型" clearable style="width:150px" @change="resetPage">
        <el-option v-for="(m, k) in ACTIONS" :key="k" :label="m.label" :value="k" />
      </el-select>
      <el-select v-model="fResult" placeholder="结果" clearable style="width:110px" @change="resetPage">
        <el-option label="成功" value="ok" />
        <el-option label="失败" value="fail" />
      </el-select>
      <el-input v-model="kw" placeholder="搜索 用户名/appid/openid/code/IP" clearable style="width:280px" @input="resetPage" />
      <div class="spacer" />
      <el-button size="small" @click="load">查询用户</el-button>
    </div>

    <div class="card rise">
      <TableSkeleton v-if="loading && !records.length" :rows="6" :cols="7" />
      <el-table v-else :data="paged" v-loading="loading" size="small" empty-text="无记录" style="width:100%">
        <el-table-column label="用户" min-width="130">
          <template #default="{ row }">
            <div class="user"><span class="uname">{{ row.username || '—' }}</span><span class="uid">#{{ row.user_id }}</span></div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="110">
          <template #default="{ row }"><el-tag size="small" :type="actionMeta(row.action).type" effect="light">{{ actionMeta(row.action).label }}</el-tag></template>
        </el-table-column>
        <el-table-column label="关联（appid / 项目）" min-width="180">
          <template #default="{ row }">
            <span v-if="relation(row)" class="copyable mono" :title="'点击复制：' + relation(row)" @click="copy(relation(row))">{{ relation(row) }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="openid" min-width="150">
          <template #default="{ row }">
            <span v-if="row.openid" class="copyable mono ell" :title="'点击复制：' + row.openid" @click="copy(row.openid)">{{ row.openid }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="结果" width="80" align="center">
          <template #default="{ row }"><el-tag size="small" :type="row.result === 'ok' ? 'success' : 'danger'">{{ row.result === 'ok' ? '成功' : '失败' }}</el-tag></template>
        </el-table-column>
        <el-table-column label="获取内容（Code / 手机号）" min-width="180">
          <template #default="{ row }">
            <span v-if="row.code" class="copyable code" :title="'点击复制：' + row.code" @click="copy(row.code)">{{ row.code }}</span>
            <span v-else-if="row.result === 'fail'" class="muted">—</span>
            <span v-else class="muted">（无）</span>
          </template>
        </el-table-column>
        <el-table-column prop="error" label="错误" min-width="140" show-overflow-tooltip>
          <template #default="{ row }"><span :class="{ err: row.error }">{{ row.error || '—' }}</span></template>
        </el-table-column>
        <el-table-column label="耗时" width="80" align="right">
          <template #default="{ row }"><span class="mono">{{ row.ms ? row.ms + 'ms' : '—' }}</span></template>
        </el-table-column>
        <el-table-column prop="ip" label="IP" width="130" />
        <el-table-column label="时间" width="170">
          <template #default="{ row }"><span class="tcell">{{ fmt(row.created_at) }}</span></template>
        </el-table-column>
      </el-table>
      <div class="pager" v-if="filtered.length > pageSize">
        <el-pagination
          small background layout="prev, pager, next, total"
          :page-size="pageSize" :total="filtered.length" v-model:current-page="page"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.row { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }
.spacer { flex: 1; }
h2 { margin: 0; }

.stats { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 14px; }
.st { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 12px 16px; text-align: center; box-shadow: var(--shadow-sm); }
.st .n { font-size: 22px; font-weight: 700; color: var(--ink); font-variant-numeric: tabular-nums; }
.st .l { font-size: 12px; color: var(--ink-3); margin-top: 2px; }
.st.ok .n { color: #2f9e44; }
.st.fail .n { color: #e6534d; }

.filters { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }

.user { display: flex; align-items: baseline; gap: 6px; }
.uname { font-weight: 600; }
.uid { color: var(--ink-3); font-size: 12px; }
.mono { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
.ell { display: inline-block; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom; }
.code { font-family: ui-monospace, Consolas, monospace; font-size: 12px; color: var(--brand); font-weight: 600; }
.copyable { cursor: pointer; border-radius: 4px; padding: 1px 3px; transition: background 0.15s; }
.copyable:hover { background: var(--brand-50); }
.muted { color: #bbb; }
.err { color: #e6534d; }
.tcell { color: var(--ink-2); font-size: 12px; font-variant-numeric: tabular-nums; }
.pager { display: flex; justify-content: flex-end; margin-top: 14px; }

@media (max-width: 600px) {
  .stats { grid-template-columns: repeat(3, 1fr); }
  .filters :deep(.el-input), .filters :deep(.el-select) { width: 100% !important; flex: 1 1 140px; }
  .row > .spacer, .filters > .spacer { display: none; }
}
</style>
