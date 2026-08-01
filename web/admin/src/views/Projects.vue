<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api.js'

const projects = ref([])
const loading = ref(false)
const dlg = ref(false)
const editing = ref(null)
const form = ref({})

function blank() {
  return { name: '', summary: '', intro: '', tutorial: '', icon: '🧩', submitPanels: [], envName: '', runConfigText: '', sortOrder: 0, status: 'off' }
}

async function load() {
  loading.value = true
  try { projects.value = (await api.projects()).projects || [] }
  catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}

function openCreate() { editing.value = null; form.value = blank(); dlg.value = true }

async function openEdit(p) {
  let full
  try { full = (await api.project(p.id)).project }
  catch (e) { return ElMessage.error(e.message || '加载项目失败') }
  editing.value = full
  const rc = { ...(full.runConfig || {}) }
  const submitPanels = Array.isArray(rc.submitPanels) ? rc.submitPanels : []
  const envName = rc.envName || ''
  delete rc.submitPanels
  delete rc.envName
  form.value = {
    name: full.name, summary: full.summary, intro: full.intro, tutorial: full.tutorial,
    icon: full.icon || '🧩', submitPanels, envName, sortOrder: full.sortOrder || 0,
    status: full.status, runConfigText: Object.keys(rc).length ? JSON.stringify(rc, null, 2) : ''
  }
  dlg.value = true
}

async function save() {
  let runConfig = {}
  if (form.value.runConfigText.trim()) {
    try { runConfig = JSON.parse(form.value.runConfigText) }
    catch { return ElMessage.error('高级配置不是合法 JSON') }
  }
  runConfig.submitPanels = form.value.submitPanels || []
  if (form.value.envName.trim()) runConfig.envName = form.value.envName.trim()
  const payload = {
    name: form.value.name, summary: form.value.summary, intro: form.value.intro,
    tutorial: form.value.tutorial, icon: form.value.icon, panelType: '',
    sortOrder: form.value.sortOrder, status: form.value.status, runConfig
  }
  try {
    if (editing.value) await api.updateProject(editing.value.id, payload)
    else await api.createProject(payload)
    ElMessage.success('已保存')
    dlg.value = false
    load()
  } catch (e) { ElMessage.error(e.message) }
}

async function toggleShelf(p) {
  try {
    await api.shelf(p.id, p.status !== 'on')
    load()
  } catch (e) { ElMessage.error(e.message); load() }
}
async function del(p) {
  await ElMessageBox.confirm(`删除项目「${p.name}」？`, '提示', { type: 'warning' }).catch(() => 'x')
  try { await api.deleteProject(p.id); load() } catch (e) { ElMessage.error(e.message) }
}
onMounted(load)
</script>

<template>
  <div>
    <div class="row"><h2>项目管理</h2><div class="spacer" /><el-button type="primary" @click="openCreate">新增项目</el-button></div>
    <div class="card rise">
      <el-table :data="projects" v-loading="loading" size="small">
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column label="项目" min-width="200">
          <template #default="{ row }"><span class="pico">{{ row.icon }}</span> {{ row.name }}</template>
        </el-table-column>
        <el-table-column prop="summary" label="简介" min-width="200" show-overflow-tooltip />
        <el-table-column label="提交面板" width="150">
          <template #default="{ row }">
            <span v-if="row.submitPanels && row.submitPanels.length">
              <el-tag v-for="pt in row.submitPanels" :key="pt" size="small" effect="plain" style="margin-right:4px">{{ pt === 'qinglong' ? '青龙' : '呆呆' }}</el-tag>
            </span>
            <span v-else style="color:#bbb">—</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }"><el-tag size="small" :type="row.status==='on'?'success':'info'">{{ row.status==='on'?'已上架':'未上架' }}</el-tag></template>
        </el-table-column>
        <el-table-column label="操作" width="220">
          <template #default="{ row }">
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" :type="row.status==='on'?'warning':'success'" @click="toggleShelf(row)">{{ row.status==='on'?'下架':'上架' }}</el-button>
            <el-button size="small" type="danger" text @click="del(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="dlg" :title="editing ? '编辑项目' : '新增项目'" width="720">
      <el-form label-width="90px">
        <el-form-item label="名称"><el-input v-model="form.name" /></el-form-item>
        <el-form-item label="图标(emoji)"><el-input v-model="form.icon" style="width:120px" /></el-form-item>
        <el-form-item label="简短介绍"><el-input v-model="form.summary" /></el-form-item>
        <el-form-item label="详情简介"><el-input v-model="form.intro" type="textarea" :rows="4" placeholder="支持 markdown 文本" /></el-form-item>
        <el-form-item label="使用教程"><el-input v-model="form.tutorial" type="textarea" :rows="6" placeholder="支持 markdown 文本" /></el-form-item>
        <el-form-item label="提交面板">
          <el-select v-model="form.submitPanels" multiple clearable placeholder="获取成功后可提交到这些面板（可多选）" style="width:320px">
            <el-option label="青龙面板" value="qinglong" />
            <el-option label="呆呆面板" value="daidai" />
          </el-select>
        </el-form-item>
        <el-form-item label="默认变量名">
          <el-input v-model="form.envName" style="width:220px" placeholder="如 JD_COOKIE（用户可改）" />
        </el-form-item>
        <el-form-item label="高级配置">
          <el-input v-model="form.runConfigText" type="textarea" :rows="3" placeholder='内置项目配置，如 {"builtin":"jd-code-login","appid":"wx73247c7819d61796"}' />
        </el-form-item>
        <el-form-item label="排序权重"><el-input-number v-model="form.sortOrder" /></el-form-item>
        <el-form-item label="上架状态"><el-switch v-model="form.status" active-value="on" inactive-value="off" active-text="上架" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="dlg=false">取消</el-button><el-button type="primary" @click="save">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>
.row { display: flex; align-items: center; margin-bottom: 14px; gap: 10px; flex-wrap: wrap; }
.spacer { flex: 1; }
h2 { margin: 0; }
.pico { font-size: 18px; }
</style>
