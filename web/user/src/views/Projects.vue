<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '../api.js'

const router = useRouter()
const projects = ref([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    projects.value = (await api.projects()).projects || []
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}
onMounted(load)
</script>

<template>
  <div>
    <h2 class="ph">项目广场</h2>
    <p class="pd">选择一个项目，查看简介与使用教程，或通过你自己的面板运行。</p>
    <div class="grid">
      <template v-if="loading && !projects.length">
        <div v-for="i in 4" :key="'sk' + i" class="proj card sk"><el-skeleton animated :rows="2" /></div>
      </template>
      <template v-else>
        <div v-for="p in projects" :key="p.id" class="proj card hover" @click="router.push('/projects/' + p.id)">
          <div class="pico">{{ p.icon || '🧩' }}</div>
          <div class="pmain">
            <div class="pname">{{ p.name }}</div>
            <div class="psum">{{ p.summary || '暂无简介' }}</div>
            <el-tag v-if="p.panelType" size="small" type="info">{{ p.panelType === 'qinglong' ? '青龙面板' : '呆呆面板' }}</el-tag>
          </div>
        </div>
        <el-empty v-if="!projects.length" description="暂无上架项目" />
      </template>
    </div>
  </div>
</template>

<style scoped>
.ph { margin: 0 0 4px; }
.pd { color: #888; margin: 0 0 18px; font-size: 14px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px,1fr)); gap: 16px; }
.proj { display: flex; gap: 14px; cursor: pointer; transition: .15s; }
.proj:hover { box-shadow: 0 8px 24px rgba(47,107,246,.12); transform: translateY(-2px); }
.pico { font-size: 34px; }
.proj.sk { display: block; cursor: default; }
.proj.sk :deep(.el-skeleton) { width: 100%; }
.pname { font-weight: 600; margin-bottom: 6px; }
.psum { color: #888; font-size: 13px; margin-bottom: 8px; min-height: 34px; }
</style>
