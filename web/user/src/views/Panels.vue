<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api.js'

const TYPES = [
  { key: 'qinglong', name: '青龙面板', idLabel: 'Client ID', secretLabel: 'Client Secret', hint: '在青龙「系统设置 → 应用设置」创建应用获取 Client ID / Secret' },
  { key: 'daidai', name: '呆呆面板', idLabel: 'App Key', secretLabel: 'App Secret', hint: '在呆呆面板「开放 API」创建应用获取 App Key / App Secret' }
]

const state = ref({})
const testing = ref({})
const ready = ref(false)

async function load() {
  const r = await api.panels().catch(() => ({ panels: [] }))
  const map = {}
  for (const t of TYPES) {
    const saved = (r.panels || []).find((p) => p.panelType === t.key)
    map[t.key] = { baseUrl: saved?.baseUrl || '', clientId: saved?.clientId || '', clientSecret: '', hasSecret: !!saved?.hasSecret, lastTestOk: saved?.lastTestOk }
  }
  state.value = map
  ready.value = true
}

async function save(type) {
  const f = state.value[type]
  try {
    await api.savePanel(type, { baseUrl: f.baseUrl, clientId: f.clientId, clientSecret: f.clientSecret })
    ElMessage.success('已保存')
    f.clientSecret = ''
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function test(type) {
  const f = state.value[type]
  testing.value[type] = true
  try {
    const r = await api.testPanel(type, { baseUrl: f.baseUrl, clientId: f.clientId, clientSecret: f.clientSecret })
    if (r.ok) ElMessage.success((r.message || '连接成功') + (r.note ? '（' + r.note + '）' : ''))
    else ElMessage.error(r.error || '连接失败')
    load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    testing.value[type] = false
  }
}
onMounted(load)
</script>

<template>
  <div>
    <h2 class="ph">面板设置</h2>
    <p class="pd">配置你自己的定时任务面板，部分项目可通过你的面板运行获取 code。密钥加密存储、永不回显。</p>
    <template v-if="ready">
      <div v-for="t in TYPES" :key="t.key" class="card mt">
        <div class="row"><h3>{{ t.name }}</h3><el-tag v-if="state[t.key].hasSecret" size="small" :type="state[t.key].lastTestOk ? 'success':'info'">{{ state[t.key].lastTestOk ? '上次测试通过' : '已配置' }}</el-tag></div>
        <el-form label-width="110px" class="mt">
          <el-form-item label="面板地址"><el-input v-model="state[t.key].baseUrl" placeholder="http://面板IP或域名:端口（青龙默认 5700，按你的实际端口填）" /></el-form-item>
          <el-form-item :label="t.idLabel"><el-input v-model="state[t.key].clientId" /></el-form-item>
          <el-form-item :label="t.secretLabel">
            <el-input v-model="state[t.key].clientSecret" type="password" show-password :placeholder="state[t.key].hasSecret ? '已保存，留空则不修改' : ''" />
          </el-form-item>
          <div class="hint">{{ t.hint }}</div>
          <div class="row mt">
            <el-button :loading="testing[t.key]" @click="test(t.key)">测试连接</el-button>
            <el-button type="primary" @click="save(t.key)">保存</el-button>
          </div>
        </el-form>
      </div>
    </template>
  </div>
</template>

<style scoped>
.ph { margin: 0 0 4px; }
.pd { color: #888; margin: 0 0 12px; font-size: 14px; }
.mt { margin-top: 16px; }
.row { display: flex; align-items: center; gap: 10px; }
h3 { margin: 0; }
.hint { color: #aaa; font-size: 12px; margin: 4px 0 0 110px; }
</style>
