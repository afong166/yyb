<script setup>
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api.js'

const route = useRoute()
const router = useRouter()
const id = route.params.id
const data = ref(null)
const licForm = ref({ maxUsers: 1, note: '', expiresAt: 0 })

function fmt(ts) { return ts ? new Date(ts).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }) : '—' }

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
const relation = (row) => row.appid || row.projectName || ''
async function copy(t) {
  if (!t) return
  try {
    await navigator.clipboard.writeText(t)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败')
  }
}

async function load() {
  try {
    data.value = await api.user(id)
  } catch (e) {
    ElMessage.error(e.message || '加载用户失败')
    router.replace('/users')
    return
  }
  if (data.value.license) {
    licForm.value = {
      maxUsers: data.value.license.maxUsers,
      note: data.value.license.note,
      expiresAt: data.value.license.expiresAt || 0
    }
  }
}

async function saveLicense() {
  try {
    await api.issueLicense(id, {
      maxUsers: licForm.value.maxUsers,
      note: licForm.value.note,
      expiresAt: licForm.value.expiresAt || 0
    })
    ElMessage.success('已保存授权码')
    load()
  } catch (e) { ElMessage.error(e.message) }
}
async function toggleLicense() {
  try {
    const cur = data.value.license.status
    await api.licenseStatus(id, cur === 'active' ? 'disabled' : 'active')
    load()
  } catch (e) { ElMessage.error(e.message) }
}
async function resetPwd() {
  const { value } = await ElMessageBox.prompt('输入新密码（至少6位）', '重置密码', { inputType: 'password' }).catch(() => ({}))
  if (!value) return
  try { await api.resetPassword(id, value); ElMessage.success('已重置') } catch (e) { ElMessage.error(e.message) }
}
onMounted(load)
</script>

<template>
  <div v-if="data">
    <el-button text @click="router.push('/users')">← 返回用户列表</el-button>
    <div class="grid mt">
      <div class="card">
        <h3>账号信息</h3>
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="ID">{{ data.user.id }}</el-descriptions-item>
          <el-descriptions-item label="用户名">{{ data.user.username }}</el-descriptions-item>
          <el-descriptions-item label="状态"><el-tag size="small" :type="data.user.status==='active'?'success':'warning'">{{ data.user.status }}</el-tag></el-descriptions-item>
          <el-descriptions-item label="注册时间">{{ fmt(data.user.createdAt) }}</el-descriptions-item>
          <el-descriptions-item label="最近登录">{{ fmt(data.user.lastLoginAt) }}</el-descriptions-item>
        </el-descriptions>
        <el-button size="small" class="mt" @click="resetPwd">重置密码</el-button>
      </div>

      <div class="card">
        <h3>授权码</h3>
        <p v-if="data.license" class="mono">{{ data.license.key }}
          <el-tag size="small" :type="data.license.status==='active'?'success':'danger'">{{ data.license.status }}</el-tag>
        </p>
        <p v-else class="muted">尚未发放，填写下方并保存即发放</p>
        <el-form label-width="90px" size="small">
          <el-form-item label="配额数"><el-input-number v-model="licForm.maxUsers" :min="1" :max="999" /></el-form-item>
          <el-form-item label="备注"><el-input v-model="licForm.note" /></el-form-item>
        </el-form>
        <div class="row">
          <el-button type="primary" size="small" @click="saveLicense">{{ data.license ? '更新' : '发放' }}授权码</el-button>
          <el-button v-if="data.license" size="small" @click="toggleLicense">{{ data.license.status==='active'?'禁用':'启用' }}</el-button>
        </div>
      </div>
    </div>

    <div class="card mt">
      <h3>绑定的微信账号（{{ data.wechatAccounts.length }}）</h3>
      <el-table :data="data.wechatAccounts" size="small" empty-text="无">
        <el-table-column prop="nickname" label="昵称" />
        <el-table-column prop="openid" label="openid" show-overflow-tooltip />
        <el-table-column prop="status" label="状态" width="90" />
        <el-table-column label="登录时间" width="180"><template #default="{ row }">{{ fmt(row.logged_at) }}</template></el-table-column>
      </el-table>
    </div>

    <div class="card mt">
      <h3>调用记录（最近 {{ data.callRecords.length }} / 共 {{ data.callCount }}）</h3>
      <el-table :data="data.callRecords" size="small" empty-text="无" style="width:100%">
        <el-table-column label="操作" width="110">
          <template #default="{ row }"><el-tag size="small" :type="actionMeta(row.action).type" effect="light">{{ actionMeta(row.action).label }}</el-tag></template>
        </el-table-column>
        <el-table-column label="关联（appid / 项目）" min-width="180">
          <template #default="{ row }">
            <span v-if="relation(row)" class="copyable mono" :title="'点击复制：' + relation(row)" @click="copy(relation(row))">{{ relation(row) }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="结果" width="80" align="center">
          <template #default="{ row }"><el-tag size="small" :type="row.result==='ok'?'success':'danger'">{{ row.result==='ok'?'成功':'失败' }}</el-tag></template>
        </el-table-column>
        <el-table-column label="获取内容（Code / 手机号）" min-width="180">
          <template #default="{ row }">
            <span v-if="row.code" class="copyable code" :title="'点击复制：' + row.code" @click="copy(row.code)">{{ row.code }}</span>
            <span v-else class="muted">{{ row.result === 'fail' ? '—' : '（无）' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="80" align="right">
          <template #default="{ row }"><span class="mono">{{ row.ms ? row.ms + 'ms' : '—' }}</span></template>
        </el-table-column>
        <el-table-column label="时间" width="170"><template #default="{ row }">{{ fmt(row.created_at) }}</template></el-table-column>
      </el-table>
    </div>
  </div>
</template>

<style scoped>
.mt { margin-top: 16px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
h3 { margin: 0 0 12px; }
@media (max-width: 720px) { .grid { grid-template-columns: 1fr; } }
.mono { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
.code { font-family: ui-monospace, Consolas, monospace; font-size: 12px; color: var(--brand); font-weight: 600; }
.copyable { cursor: pointer; border-radius: 4px; padding: 1px 3px; transition: background 0.15s; }
.copyable:hover { background: var(--brand-50); }
.muted { color: #bbb; }
.row { display: flex; gap: 8px; }
</style>
