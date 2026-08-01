<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '../api.js'

const router = useRouter()
const form = ref({ username: '', password: '', password2: '' })
const loading = ref(false)

async function submit() {
  if (!form.value.username || !form.value.password) return ElMessage.warning('请填写完整')
  if (form.value.password !== form.value.password2) return ElMessage.warning('两次密码不一致')
  loading.value = true
  try {
    const r = await api.register({ username: form.value.username, password: form.value.password })
    ElMessage.success(r.message || '注册成功，等待管理员审核')
    router.push('/login')
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
      <h2>注册账号</h2>
      <p class="sub">注册后需管理员审核通过才能登录</p>
      <el-input v-model="form.username" placeholder="用户名（3-32位 字母/数字/._-）" size="large" class="mb" />
      <el-input v-model="form.password" type="password" placeholder="密码（至少6位）" size="large" class="mb" show-password />
      <el-input v-model="form.password2" type="password" placeholder="确认密码" size="large" class="mb" show-password @keyup.enter="submit" />
      <el-button type="primary" size="large" class="full" :loading="loading" @click="submit">注册</el-button>
      <div class="foot">已有账号？<router-link to="/login">去登录</router-link></div>
    </div>
  </div>
</template>

<style scoped>
.auth-wrap { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; background: linear-gradient(135deg,#eef3ff,#f5f7fa); }
.auth-card { width: min(380px, 100%); background: #fff; padding: 32px; border-radius: 16px; box-shadow: 0 10px 40px rgba(0,0,0,.06); }
@media (max-width: 480px) { .auth-card { padding: 24px 20px; } }
h2 { margin: 0; color: #2f6bf6; }
.sub { color: #888; margin: 6px 0 22px; font-size: 13px; }
.mb { margin-bottom: 14px; }
.full { width: 100%; }
.foot { margin-top: 16px; text-align: center; color: #888; font-size: 13px; }
</style>
