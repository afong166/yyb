<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api.js'

const user = ref(null)
const license = ref(null)

function fmt(ts) {
  return ts ? new Date(ts).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }) : '—'
}

onMounted(async () => {
  try {
    const r = await api.me()
    user.value = r.user
    license.value = r.license
  } catch (e) {
    ElMessage.error(e.message || '加载账号信息失败')
  }
})
</script>

<template>
  <div>
    <h2 class="ph">我的账号</h2>
    <div class="card" v-if="user">
      <el-descriptions :column="1" border>
        <el-descriptions-item label="用户名">{{ user.username }}</el-descriptions-item>
        <el-descriptions-item label="账号状态">
          <el-tag :type="user.status === 'active' ? 'success' : 'warning'" size="small">{{ user.status }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="注册时间">{{ fmt(user.createdAt) }}</el-descriptions-item>
      </el-descriptions>
    </div>

    <h2 class="ph mt">我的授权码</h2>
    <div class="card">
      <template v-if="license">
        <el-descriptions :column="1" border>
          <el-descriptions-item label="授权码">
            <span class="mono">{{ license.key }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="状态"><el-tag :type="license.status === 'active' ? 'success' : 'danger'" size="small">{{ license.status }}</el-tag></el-descriptions-item>
          <el-descriptions-item label="配额（可绑定微信账号数）">{{ license.usedCount }} / {{ license.maxUsers }}</el-descriptions-item>
          <el-descriptions-item label="有效期">{{ license.expiresAt ? fmt(license.expiresAt) : '永久' }}</el-descriptions-item>
        </el-descriptions>
        <p class="tip">授权码也可用于外部脚本（青龙等）：请求头 <code>X-License-Key</code> 或 wx_server 的 <code>auth</code> 参数。</p>
      </template>
      <el-empty v-else description="尚未分配授权码，请联系管理员" />
    </div>
  </div>
</template>

<style scoped>
.ph { margin: 0 0 12px; }
.mt { margin-top: 20px; }
.mono { font-family: ui-monospace, Consolas, monospace; letter-spacing: 1px; }
.tip { color: #888; font-size: 13px; margin-top: 12px; }
</style>
