<script setup>
// 数字滚动：数值加载/变化时从当前值缓动到目标值，带千分位；非数字原样渲染。
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'

const props = defineProps({
  value: { type: [Number, String], default: 0 },
  duration: { type: Number, default: 720 }
})

const isNum = (v) => typeof v === 'number' && Number.isFinite(v)
const display = ref(isNum(props.value) ? 0 : props.value)
let raf = 0

function render(v) {
  return isNum(v) ? v.toLocaleString('en-US') : v
}

function animate(to) {
  cancelAnimationFrame(raf)
  if (!isNum(to)) {
    display.value = to
    return
  }
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  const from = isNum(display.value) ? display.value : 0
  if (reduce || from === to) {
    display.value = to
    return
  }
  const start = performance.now()
  const step = (now) => {
    const p = Math.min(1, (now - start) / props.duration)
    const eased = 1 - Math.pow(1 - p, 3) // easeOutCubic
    display.value = Math.round(from + (to - from) * eased)
    if (p < 1) raf = requestAnimationFrame(step)
    else display.value = to
  }
  raf = requestAnimationFrame(step)
}

onMounted(() => animate(props.value))
watch(() => props.value, (v) => animate(v))
onBeforeUnmount(() => cancelAnimationFrame(raf))
</script>

<template>
  <span class="countup">{{ render(display) }}</span>
</template>

<style scoped>
.countup { font-variant-numeric: tabular-nums; }
</style>
