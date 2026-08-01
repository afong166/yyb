<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api.js'

const state = ref({ configured: false, source: 'none', maskedApiUrl: '' })
const apiUrl = ref('')
const loading = ref(false)

async function load() {
  loading.value = true
  try { state.value = await api.proxySettings() }
  catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}

async function save() {
  if (!apiUrl.value.trim()) return ElMessage.warning('请粘贴 51代理生成的完整 API URL')
  loading.value = true
  try {
    state.value = await api.saveProxySettings({ apiUrl: apiUrl.value.trim() })
    apiUrl.value = ''
    ElMessage.success('51代理 API 已保存')
  } catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}

async function clearConfig() {
  await ElMessageBox.confirm('确定删除数据库中的 51代理 API 配置？', '确认', { type: 'warning' })
  try {
    state.value = await api.saveProxySettings({ clear: true })
    apiUrl.value = ''
    ElMessage.success('数据库配置已删除')
  } catch (e) { ElMessage.error(e.message) }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="head">
      <div><h2>51短效代理</h2><p>所有用户共用一份提取 API，用户只能选择地区，无法看到 API 凭据。</p></div>
      <el-tag :type="state.configured ? 'success' : 'danger'">{{ state.configured ? '已配置' : '未配置' }}</el-tag>
    </div>

    <div class="card" v-loading="loading">
      <div class="row"><span>配置来源</span><b>{{ state.source === 'db' ? '数据库（明文）' : '无' }}</b></div>
      <div class="row"><span>当前地址</span><code>{{ state.maskedApiUrl || '—' }}</code></div>

      <label>新的 API URL</label>
      <el-input v-model="apiUrl" type="password" show-password clearable
        placeholder="粘贴 51代理 API提取页面生成的完整 getapi2 URL" />
      <p class="hint">API 按原始 http/https 地址明文保存。系统调用时强制使用 qty=1、port=2、format=json，并覆盖 regionCode；不会返回给普通用户。</p>

      <div class="actions">
        <el-button type="primary" :loading="loading" @click="save">保存</el-button>
        <el-button type="danger" plain :disabled="!state.configured" @click="clearConfig">删除数据库配置</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page { max-width: 920px; margin: 0 auto; }
.head { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 18px; }
h2, h3 { margin: 0; color: var(--ink); }
.head p, .hint { color: var(--ink-3); font-size: 13px; line-height: 1.65; }
.card { background: #fff; border: 1px solid var(--line); border-radius: 16px; padding: 22px; display: grid; gap: 14px; }
.row { display: grid; grid-template-columns: 110px 1fr; gap: 12px; align-items: start; font-size: 14px; }
.row span { color: var(--ink-3); }
code { overflow-wrap: anywhere; color: var(--ink-2); }
label { font-size: 14px; font-weight: 600; color: var(--ink); }
.actions { display: flex; gap: 10px; }
@media (max-width: 680px) { .row { grid-template-columns: 1fr; } }
</style>
