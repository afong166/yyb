<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '../api.js'
import { refreshAuth } from '../router.js'

const router = useRouter()
const form = ref({ username: 'admin', password: '' })
const loading = ref(false)

async function submit() {
  loading.value = true
  try {
    await api.login(form.value)
    await refreshAuth()
    router.push('/users')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="wrap">
    <div class="card2">
      <h2>管理后台</h2>
      <el-input v-model="form.username" placeholder="管理员用户名" size="large" class="mb" />
      <el-input v-model="form.password" type="password" placeholder="密码" size="large" class="mb" show-password @keyup.enter="submit" />
      <el-button type="danger" size="large" class="full" :loading="loading" @click="submit">登录</el-button>
    </div>
  </div>
</template>

<style scoped>
.wrap {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  background:
    radial-gradient(800px 480px at 100% -5%, rgba(230, 83, 77, 0.07), transparent 60%),
    radial-gradient(700px 480px at -5% 105%, rgba(230, 83, 77, 0.05), transparent 55%),
    #f6f7f9;
  animation: cardIn 0.5s var(--ease, ease) both;
  padding: 20px;
}
.card2 { width: min(384px, 100%); background: #fff; padding: 34px; border-radius: 18px; box-shadow: 0 20px 55px rgba(30, 40, 70, 0.14); border: 1px solid var(--line, #eef0f4); }
@media (max-width: 480px) { .card2 { padding: 26px 20px; } }
@keyframes cardIn { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: none; } }
h2 { margin: 0 0 24px; color: #e6534d; text-align: center; }
.sub { color: #999; font-size: 12px; margin: 10px 0 22px; line-height: 1.7; text-align: center; }
.mb { margin-bottom: 14px; }
.full { width: 100%; }
</style>
