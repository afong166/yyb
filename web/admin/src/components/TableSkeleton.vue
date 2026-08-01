<script setup>
// 表格首屏加载占位：微光骨架，替代生硬的空白/转圈。
defineProps({ rows: { type: Number, default: 4 }, cols: { type: Number, default: 5 } })
</script>

<template>
  <el-skeleton animated class="tsk">
    <template #template>
      <div class="tsk-head">
        <el-skeleton-item v-for="c in cols" :key="'h' + c" variant="text" />
      </div>
      <div v-for="r in rows" :key="r" class="tsk-row">
        <el-skeleton-item v-for="c in cols" :key="r + '-' + c" variant="text" />
      </div>
    </template>
  </el-skeleton>
</template>

<style scoped>
.tsk { padding: 2px 0; }
.tsk-head, .tsk-row { display: flex; gap: 20px; align-items: center; }
.tsk-head { padding: 6px 0 18px; }
.tsk-row { padding: 13px 0; border-top: 1px solid var(--line); }
.tsk :deep(.el-skeleton__item) { flex: 1; height: 13px; border-radius: 6px; }
.tsk-head :deep(.el-skeleton__item) { height: 11px; max-width: 64%; opacity: 0.75; }
.tsk-row :deep(.el-skeleton__item:first-child) { flex: 0 0 8%; }
</style>
