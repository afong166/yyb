<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api.js'

const rows = ref([])
function fmt(ts) { return ts ? new Date(ts).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }) : '—' }
onMounted(async () => {
  try { rows.value = (await api.audit()).audit || [] }
  catch (e) { ElMessage.error(e.message || '加载审计日志失败') }
})
</script>

<template>
  <div>
    <h2>审计日志</h2>
    <div class="card rise">
      <el-table :data="rows" size="small">
        <el-table-column prop="admin_id" label="管理员" width="90" />
        <el-table-column prop="action" label="操作" width="180" />
        <el-table-column prop="target_type" label="对象" width="100" />
        <el-table-column prop="target_id" label="对象ID" width="120" show-overflow-tooltip />
        <el-table-column prop="detail" label="详情" min-width="160" show-overflow-tooltip />
        <el-table-column prop="ip" label="IP" width="130" />
        <el-table-column label="时间" width="180"><template #default="{ row }">{{ fmt(row.created_at) }}</template></el-table-column>
      </el-table>
    </div>
  </div>
</template>

<style scoped>
h2 { margin: 0 0 14px; }
</style>
