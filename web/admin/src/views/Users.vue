<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api.js'
import CountUp from '../components/CountUp.vue'
import TableSkeleton from '../components/TableSkeleton.vue'

const router = useRouter()
const users = ref([])
const filter = ref('')
const kw = ref('')
const loading = ref(false)
const stat = ref({ total: 0, pending: 0, active: 0 })
const pool = ref(null)  // MMTLS 会话池监控

// 分页（本地）
const page = ref(1)
const pageSize = 15

const disabledCount = computed(() => Math.max(0, stat.value.total - stat.value.pending - stat.value.active))

function fmt(ts) {
  return ts ? new Date(ts).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }) : '—'
}

async function load() {
  loading.value = true
  try {
    users.value = (await api.users(filter.value)).users || []
    page.value = 1
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}
async function loadStats() {
  try {
    const s = await api.stats()
    stat.value = s.users || { total: 0, pending: 0, active: 0 }
    pool.value = s.sessionPool || null
  } catch {
    /* 统计失败不阻断列表 */
  }
}
function refreshAll() {
  load()
  loadStats()
}

const filtered = computed(() => {
  const k = kw.value.trim().toLowerCase()
  if (!k) return users.value
  return users.value.filter((u) => u.username.toLowerCase().includes(k) || String(u.id) === k)
})
const paged = computed(() => filtered.value.slice((page.value - 1) * pageSize, page.value * pageSize))

function setFilter(f) {
  if (filter.value === f) return
  filter.value = f
  kw.value = ''
  load()
}

async function approve(u) {
  try {
    await api.approve(u.id)
    ElMessage.success('已通过审核')
    refreshAll()
  } catch (e) {
    ElMessage.error(e.message)
  }
}
async function toggle(u) {
  try {
    if (u.status === 'disabled') await api.enable(u.id)
    else await api.disable(u.id)
    refreshAll()
  } catch (e) {
    ElMessage.error(e.message)
  }
}
async function del(u) {
  try {
    await ElMessageBox.confirm(
      `确定删除用户「${u.username}」？将同时删除其授权码、绑定的微信账号与调用记录，且不可恢复。`,
      '删除用户',
      { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' }
    )
  } catch {
    return
  }
  try {
    await api.deleteUser(u.id)
    ElMessage.success('已删除')
    refreshAll()
  } catch (e) {
    ElMessage.error(e.message)
  }
}
async function copy(t) {
  if (!t) return
  try {
    await navigator.clipboard.writeText(t)
    ElMessage.success('已复制授权码')
  } catch {
    ElMessage.warning('复制失败')
  }
}

const statusMeta = (s) => (s === 'active' ? { t: 'success', l: '已启用' } : s === 'pending' ? { t: 'warning', l: '待审核' } : { t: 'info', l: '已禁用' })

onMounted(() => {
  load()
  loadStats()
})
</script>

<template>
  <div>
    <div class="row">
      <h2>用户管理</h2>
      <div class="spacer" />
      <el-input v-model="kw" placeholder="搜索用户名 / ID" clearable style="width:200px" @input="page = 1" />
      <el-button :loading="loading" @click="refreshAll">刷新</el-button>
    </div>

    <!-- 统计条（点击筛选） -->
    <div class="stats">
      <div class="st" :class="{ on: filter === '' }" @click="setFilter('')"><div class="n"><CountUp :value="stat.total" /></div><div class="l">全部用户</div></div>
      <div class="st warn" :class="{ on: filter === 'pending' }" @click="setFilter('pending')">
        <div class="n"><CountUp :value="stat.pending" /></div><div class="l">待审核<span v-if="stat.pending" class="badge">!</span></div>
      </div>
      <div class="st ok" :class="{ on: filter === 'active' }" @click="setFilter('active')"><div class="n"><CountUp :value="stat.active" /></div><div class="l">已启用</div></div>
      <div class="st" :class="{ on: filter === 'disabled' }" @click="setFilter('disabled')"><div class="n"><CountUp :value="disabledCount" /></div><div class="l">已禁用</div></div>
    </div>

    <!-- MMTLS 会话池监控：0-RTT 复用越多，取码/云函数越快 -->
    <div v-if="pool" class="pool">
      <span class="pt">会话池</span>
      <span class="pi">缓存 <b>{{ pool.cached }}</b></span>
      <span class="pi">存活 <b class="g">{{ pool.live }}</b></span>
      <span class="pi">过期 <b>{{ pool.expired }}</b></span>
      <span class="sep">·</span>
      <span class="pi">总请求 <b>{{ pool.totalRequests }}</b></span>
      <span class="pi">0-RTT <b class="g">{{ pool.hit0rtt }}</b></span>
      <span class="pi">重握 <b>{{ pool.hitRelogin }}</b></span>
      <span class="pi">重建 <b class="w">{{ pool.rebuild }}</b></span>
      <span class="sep">·</span>
      <span class="pi">复用率 <b class="g">{{ (pool.reuseRate * 100).toFixed(1) }}%</b></span>
    </div>

    <div class="card rise">
      <TableSkeleton v-if="loading && !users.length" :rows="6" :cols="6" />
      <el-table v-else :data="paged" v-loading="loading" size="small" style="width:100%"
                :row-class-name="({ row }) => (row.status === 'pending' ? 'pending-row' : '')">
        <el-table-column prop="id" label="ID" width="64" />
        <el-table-column prop="username" label="用户名" min-width="150" show-overflow-tooltip />
        <el-table-column label="状态" width="94">
          <template #default="{ row }"><el-tag size="small" :type="statusMeta(row.status).t">{{ statusMeta(row.status).l }}</el-tag></template>
        </el-table-column>
        <el-table-column label="微信账号" width="90" align="center">
          <template #default="{ row }">
            <span :class="row.wechatCount ? 'wx' : 'muted'">{{ row.wechatCount || 0 }}</span>
          </template>
        </el-table-column>
        <el-table-column label="授权码" min-width="200">
          <template #default="{ row }">
            <span v-if="row.license" class="lic copyable" :title="'点击复制：' + row.license.key" @click="copy(row.license.key)">
              <span class="mono">{{ row.license.key }}</span>
              <span class="quota">{{ row.license.usedCount }}/{{ row.license.maxUsers }}</span>
            </span>
            <span v-else class="muted">未发放</span>
          </template>
        </el-table-column>
        <el-table-column label="注册时间" width="170">
          <template #default="{ row }"><span class="tcell">{{ fmt(row.createdAt) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="240" fixed="right">
          <template #default="{ row }">
            <el-button v-if="row.status === 'pending'" size="small" type="success" @click="approve(row)">通过</el-button>
            <el-button v-if="row.status !== 'pending'" size="small" :type="row.status === 'disabled' ? 'success' : 'warning'" @click="toggle(row)">{{ row.status === 'disabled' ? '启用' : '禁用' }}</el-button>
            <el-button size="small" @click="router.push('/users/' + row.id)">详情</el-button>
            <el-button size="small" type="danger" plain @click="del(row)">删除</el-button>
          </template>
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

.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 14px; }
.pool { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 14px; margin: -4px 0 16px;
  padding: 9px 14px; border-radius: 10px; background: #f5f7fb; border: 1px solid #eaeef5;
  font-size: 12.5px; color: #6b7280; }
.pool .pt { font-weight: 700; color: #374151; }
.pool .pi b { color: #374151; font-variant-numeric: tabular-nums; }
.pool .pi b.g { color: #2f9e44; }
.pool .pi b.w { color: #e6a23c; }
.pool .sep { color: #cbd5e1; }
.st {
  background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 12px 16px;
  text-align: center; box-shadow: var(--shadow-sm); cursor: pointer;
  transition: border-color 0.2s var(--ease), transform 0.12s var(--ease), box-shadow 0.2s var(--ease);
}
.st:hover { border-color: var(--line-2); transform: translateY(-2px); box-shadow: var(--shadow); }
.st.on { border-color: var(--brand); box-shadow: 0 0 0 2px var(--brand-50); }
.st .n { font-size: 22px; font-weight: 700; color: var(--ink); font-variant-numeric: tabular-nums; }
.st .l { font-size: 12px; color: var(--ink-3); margin-top: 2px; position: relative; display: inline-block; }
.st.warn .n { color: #e8912d; }
.st.ok .n { color: #2f9e44; }
.badge { position: absolute; top: -4px; right: -12px; background: #e6534d; color: #fff; font-size: 10px; width: 15px; height: 15px; line-height: 15px; border-radius: 50%; }

.mono { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
.lic { display: inline-flex; align-items: center; gap: 8px; }
.quota { color: var(--ink-3); font-size: 12px; }
.wx { font-weight: 600; color: var(--brand); }
.muted { color: #bbb; }
.copyable { cursor: pointer; border-radius: 4px; padding: 1px 4px; transition: background 0.15s; }
.copyable:hover { background: var(--brand-50); }
.tcell { color: var(--ink-2); font-size: 12px; font-variant-numeric: tabular-nums; }
.pager { display: flex; justify-content: flex-end; margin-top: 14px; }

:deep(.pending-row) { background: #fff8ec; }

@media (max-width: 600px) {
  .stats { grid-template-columns: repeat(2, 1fr); }
  .row > .spacer { display: none; }
  .row :deep(.el-input) { flex: 1 1 160px; }
}
</style>
