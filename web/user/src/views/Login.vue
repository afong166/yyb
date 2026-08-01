<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '../api.js'
import { refreshAuth } from '../router.js'

const router = useRouter()
const form = ref({ username: '', password: '' })
const loading = ref(false)

async function submit() {
  if (!form.value.username || !form.value.password) return ElMessage.warning('请输入用户名和密码')
  loading.value = true
  try {
    await api.login(form.value)
    await refreshAuth()
    ElMessage.success('登录成功')
    router.push('/dashboard')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-wrap">
    <div class="auth-card">
      <h2>用户登录</h2>
      <el-input v-model="form.username" placeholder="用户名" size="large" class="mb" @keyup.enter="submit" />
      <el-input v-model="form.password" type="password" placeholder="密码" size="large" class="mb" show-password @keyup.enter="submit" />
      <el-button type="primary" size="large" class="full" :loading="loading" @click="submit">登录</el-button>
      <div class="foot">还没有账号？<router-link to="/register">注册一个</router-link></div>
    </div>
  </div>
</template>

<style scoped>
.auth-wrap { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; background: linear-gradient(135deg,#eef3ff,#f5f7fa); }
.auth-card { width: min(360px, 100%); background: #fff; padding: 32px; border-radius: 16px; box-shadow: 0 10px 40px rgba(0,0,0,.06); }
@media (max-width: 480px) { .auth-card { padding: 24px 20px; } }
h2 { margin: 0 0 22px; color: #2f6bf6; text-align: center; }
.mb { margin-bottom: 14px; }
.full { width: 100%; }
.foot { margin-top: 16px; text-align: center; color: #888; font-size: 13px; }
</style>
