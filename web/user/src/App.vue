<script setup>
import { useRoute, useRouter } from 'vue-router'
import { computed } from 'vue'
import { api } from './api.js'
import { auth, refreshAuth } from './router.js'

const route = useRoute()
const router = useRouter()
const showChrome = computed(() => !route.meta.guest)

const nav = [
  { path: '/dashboard', label: '控制台', short: '控制台' },
  { path: '/projects', label: '项目', short: '项目' },
  { path: '/tasks', label: '定时任务', short: '定时' },
  { path: '/panels', label: '面板设置', short: '面板' },
  { path: '/api-docs', label: '接口文档', short: '文档' },
  { path: '/changelog', label: '更新日志', short: '更新' },
  { path: '/profile', label: '我的', short: '我的' }
]

// 底部标签栏图标（线性风格，随主题色高亮）
const tabIcons = {
  '/dashboard': '<path d="M3 9.7 12 3l9 6.7V21H3z"/><path d="M9.2 21v-6.2h5.6V21"/>',
  '/projects': '<rect x="3.5" y="3.5" width="7" height="7" rx="1.3"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.3"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.3"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.3"/>',
  '/tasks': '<circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 1.5"/><path d="M9 2.5h6"/>',
  '/panels': '<line x1="4" y1="8" x2="20" y2="8"/><line x1="4" y1="16" x2="20" y2="16"/><circle cx="9" cy="8" r="2.2"/><circle cx="15" cy="16" r="2.2"/>',
  '/api-docs': '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/><line x1="9" y1="12" x2="15" y2="12"/><line x1="9" y1="16" x2="15" y2="16"/>',
  '/changelog': '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5l3.2 2"/>',
  '/profile': '<circle cx="12" cy="8" r="3.6"/><path d="M5 20c0-3.6 3.1-5.6 7-5.6s7 2 7 5.6"/>'
}

async function logout() {
  await api.logout().catch(() => {})
  await refreshAuth()
  router.push('/login')
}
</script>

<template>
  <div v-if="showChrome" class="shell">
    <header class="topbar">
      <div class="brand"><span class="logo">志</span><span class="bt">须知少时凌云志</span></div>
      <nav class="nav nav-top">
        <router-link v-for="n in nav" :key="n.path" :to="n.path" class="navlink">{{ n.label }}</router-link>
      </nav>
      <div class="spacer" />
      <span class="who" v-if="auth.user"><span class="dot" /><span class="uname">{{ auth.user.username }}</span></span>
      <el-button size="small" text @click="logout">退出</el-button>
    </header>
    <main class="main">
      <router-view v-slot="{ Component }">
        <transition name="page" mode="out-in">
          <component :is="Component" />
        </transition>
      </router-view>
    </main>
    <nav class="tabbar">
      <router-link v-for="n in nav" :key="n.path" :to="n.path" class="tablink">
        <svg class="tabi" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" v-html="tabIcons[n.path]" />
        <span class="tabt">{{ n.short }}</span>
      </router-link>
    </nav>
  </div>
  <router-view v-else />
</template>

<style scoped>
.shell { min-height: 100vh; }
.topbar {
  display: flex; align-items: center; gap: 20px; padding: 0 26px; height: 62px;
  background: rgba(255, 255, 255, 0.82); backdrop-filter: saturate(1.4) blur(10px);
  border-bottom: 1px solid var(--line); position: sticky; top: 0; z-index: 20;
}
.brand { display: flex; align-items: center; gap: 10px; font-weight: 700; min-width: 0; }
.logo {
  width: 30px; height: 30px; border-radius: 9px; display: grid; place-items: center; flex: none;
  color: #fff; font-size: 15px; background: linear-gradient(135deg, #4a82ff, #2f6bf6);
  box-shadow: 0 4px 12px rgba(47, 107, 246, 0.35);
}
.bt { color: var(--ink); letter-spacing: -0.02em; white-space: nowrap; }
.nav { display: flex; gap: 4px; }
.navlink {
  padding: 8px 14px; border-radius: 10px; color: var(--ink-2); text-decoration: none; font-size: 14px;
  transition: background 0.2s var(--ease), color 0.2s var(--ease), transform 0.11s var(--ease);
}
.navlink:hover { background: #f3f5fa; color: var(--ink); }
.navlink:active { transform: scale(0.95); }
.navlink.router-link-active { background: var(--brand-50); color: var(--brand); font-weight: 600; }
.spacer { flex: 1; }
.who { display: inline-flex; align-items: center; gap: 6px; color: var(--ink-2); font-size: 13px; min-width: 0; }
.who .uname { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dot { width: 7px; height: 7px; border-radius: 50%; background: #34c759; box-shadow: 0 0 0 3px rgba(52, 199, 89, 0.15); flex: none; }
.main { max-width: 1120px; margin: 0 auto; padding: 28px 26px 48px; }

/* 底部标签栏：仅移动端显示 */
.tabbar { display: none; }

@media (max-width: 820px) {
  .topbar { gap: 10px; padding: 0 14px; height: 54px; }
  .nav-top { display: none; }
  .bt { font-size: 15px; }
  .who { max-width: 34vw; }
  .main { padding: 18px 16px calc(84px + env(safe-area-inset-bottom)); }

  .tabbar {
    display: flex; position: fixed; left: 0; right: 0; bottom: 0; z-index: 50;
    background: rgba(255, 255, 255, 0.92); backdrop-filter: saturate(1.5) blur(14px);
    border-top: 1px solid var(--line);
    padding: 6px 4px calc(6px + env(safe-area-inset-bottom));
  }
  .tablink {
    flex: 1; display: flex; flex-direction: column; align-items: center; gap: 3px;
    text-decoration: none; color: var(--ink-3); font-size: 11px; padding: 5px 0 3px;
    border-radius: 10px; transition: color 0.18s var(--ease);
  }
  .tablink:active { transform: scale(0.94); }
  .tablink.router-link-active { color: var(--brand); font-weight: 600; }
  .tabi { width: 22px; height: 22px; }
  .tabt { line-height: 1; }
}

@media (max-width: 360px) {
  .bt { display: none; }
}
</style>
