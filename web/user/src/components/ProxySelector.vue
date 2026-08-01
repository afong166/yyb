<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api.js'

const props = defineProps({ allowAccount: { type: Boolean, default: false } })
const model = defineModel({
  type: Object,
  default: () => ({ proxyMode: 'direct', proxyUrl: '', proxyRegionCode: '', proxyRegionName: '' })
})
const configured = ref(false)
const localCode = ref('150100')
const provinces = ref([])
const cities = ref([])
const provinceCode = ref('')
const loadingProvince = ref(false)
const loadingCity = ref(false)

const mode = computed({
  get: () => model.value?.proxyMode || (props.allowAccount ? 'account' : 'direct'),
  set: (v) => { model.value = { ...model.value, proxyMode: v } }
})
const regionCode = computed({
  get: () => String(model.value?.proxyRegionCode || ''),
  set: (v) => {
    const item = cities.value.find((x) => x.regionCode === String(v))
    model.value = { ...model.value, proxyRegionCode: String(v || ''), proxyRegionName: item?.regionName || '' }
  }
})
const isLocal = computed(() => regionCode.value === localCode.value)

async function loadProvinces() {
  if (provinces.value.length || loadingProvince.value) return
  loadingProvince.value = true
  try { provinces.value = (await api.proxyRegions()).regions || [] }
  catch (e) { ElMessage.error(e.message) }
  finally { loadingProvince.value = false }
}
async function loadCities(code, keepRegion = false) {
  cities.value = []
  if (!keepRegion) regionCode.value = ''
  if (!code) return
  loadingCity.value = true
  try { cities.value = (await api.proxyRegions(code)).regions || [] }
  catch (e) { ElMessage.error(e.message) }
  finally { loadingCity.value = false }
}
watch(mode, (v) => { if (v === 'short') loadProvinces() })
watch(provinceCode, (v) => {
  const keep = !!regionCode.value && String(regionCode.value).slice(0, 2) === String(v).slice(0, 2)
  loadCities(v, keep)
})

onMounted(async () => {
  try {
    const r = await api.proxyCapabilities()
    configured.value = !!r.shortProxyConfigured
    localCode.value = String(r.localRegionCode || '150100')
  } catch { /* 页面其它功能仍可用 */ }
  if (mode.value === 'short') {
    await loadProvinces()
    if (regionCode.value) provinceCode.value = `${regionCode.value.slice(0, 2)}0000`
  }
})
</script>

<template>
  <div class="proxy-selector">
    <el-radio-group v-model="mode" size="small">
      <el-radio-button v-if="allowAccount" value="account">账号默认</el-radio-button>
      <el-radio-button value="direct">本机直连</el-radio-button>
      <el-radio-button value="long">长效代理</el-radio-button>
      <el-radio-button value="short" :disabled="!configured">51短效代理</el-radio-button>
    </el-radio-group>

    <el-input
      v-if="mode === 'long'"
      v-model="model.proxyUrl"
      placeholder="socks5://user:pass@host:port"
      clearable
    />

    <div v-if="mode === 'short'" class="region-row">
      <el-select v-model="provinceCode" placeholder="选择省份" filterable :loading="loadingProvince">
        <el-option v-for="p in provinces" :key="p.regionCode" :label="p.regionName" :value="p.regionCode" />
      </el-select>
      <el-select v-model="regionCode" placeholder="选择城市" filterable :loading="loadingCity" :disabled="!provinceCode">
        <el-option v-for="c in cities" :key="c.regionCode" :label="c.regionName" :value="c.regionCode" />
      </el-select>
    </div>

    <p v-if="mode === 'short' && !configured" class="hint warn">管理员尚未配置 51代理 API。</p>
    <p v-else-if="mode === 'short' && isLocal" class="hint ok">呼和浩特使用服务器本机 IP，不提取代理、不产生费用。</p>
    <p v-else-if="mode === 'short'" class="hint">系统会一直复用数据库中的代理；只有实际连接失败才删除并按所选城市重新提取。</p>
    <p v-else-if="mode === 'account'" class="hint">沿用该微信账号扫码时保存的直连、长效代理或短效地区。</p>
  </div>
</template>

<style scoped>
.proxy-selector { display: grid; gap: 10px; }
.region-row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.hint { margin: 0; color: var(--ink-3, #8a8f99); font-size: 12px; line-height: 1.55; }
.hint.warn { color: #d46b08; }
.hint.ok { color: #16834a; }
@media (max-width: 520px) { .region-row { grid-template-columns: 1fr; } }
</style>
