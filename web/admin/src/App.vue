<script setup>
import { useRoute, useRouter } from 'vue-router'
import { computed, ref, watch } from 'vue'
import { api } from './api.js'
import { auth, refreshAuth } from './router.js'

const route = useRoute()
const router = useRouter()
const showChrome = computed(() => !route.meta.guest)
const menuOpen = ref(false)
// 移动端：切换路由后自动收起抽屉菜单
watch(() => route.path, () => { menuOpen.value = false })
const nav = [
  { path: '/users', label: '用户管理' },
  { path: '/projects', label: '项目管理' },
  { path: '/call-records', label: '调用记录' },
  { path: '/audit', label: '审计日志' },
  { path: '/proxy-settings', label: '短效代理' },
  { path: '/api-docs', label: '接口文档' }
]
async function logout() {
  await api.logout().catch(() => {})
  await refreshAuth()
  router.push('/login')
}
</script>

<template>
  <div v-if="showChrome" class="shell" :class="{ 'menu-open': menuOpen }">
    <header class="mtop">
      <button class="burger" @click="menuOpen = true" aria-label="打开菜单">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
          <line x1="4" y1="7" x2="20" y2="7" /><line x1="4" y1="12" x2="20" y2="12" /><line x1="4" y1="17" x2="20" y2="17" />
        </svg>
      </button>
      <span class="mbrand"><span class="mlogo">志</span>须知少时凌云志 · 管理</span>
    </header>
    <div class="scrim" @click="menuOpen = false" />
    <aside class="side">
      <div class="brand">
        <span class="logo">志</span>
        <span class="btxt">须知少时凌云志<small>管理后台</small></span>
      </div>
      <nav class="navwrap">
        <router-link v-for="n in nav" :key="n.path" :to="n.path" class="navlink">
          <span class="bar" /><span class="lbl">{{ n.label }}</span>
        </router-link>
      </nav>
      <div class="side-foot">
        <div class="who"><span class="adot" />{{ auth.admin?.username }}</div>
        <el-button size="small" text @click="logout">退出登录</el-button>
      </div>
    </aside>
    <main class="main">
      <router-view v-slot="{ Component }">
        <transition name="page" mode="out-in">
          <component :is="Component" />
        </transition>
      </router-view>
    </main>
  </div>
  <router-view v-else />
</template>

<style scoped>
.shell { display: flex; min-height: 100vh; }
.side {
  width: 234px; background: var(--surface); color: var(--ink-2); display: flex; flex-direction: column;
  padding: 22px 16px; position: sticky; top: 0; height: 100vh;
  border-right: 1px solid var(--line);
}
.brand { display: flex; align-items: center; gap: 11px; padding: 4px 8px 22px; }
.logo {
  width: 34px; height: 34px; border-radius: 10px; display: grid; place-items: center; flex: none;
  color: #fff; font-size: 16px; font-weight: 700; background: linear-gradient(135deg, #ef6b66, #e6534d);
  box-shadow: 0 6px 16px rgba(230, 83, 77, 0.35);
}
.btxt { display: flex; flex-direction: column; font-weight: 700; color: var(--ink); letter-spacing: -0.02em; line-height: 1.25; font-size: 15px; }
.btxt small { font-weight: 500; color: var(--ink-3); font-size: 11px; letter-spacing: 0; }
.navwrap { display: flex; flex-direction: column; gap: 2px; }
.navlink {
  position: relative; display: flex; align-items: center; gap: 10px;
  padding: 11px 14px; border-radius: 11px; color: var(--ink-2); text-decoration: none; font-size: 14px;
  transition: background 0.22s var(--ease), color 0.22s var(--ease), transform 0.11s var(--ease);
}
.navlink:active { transform: scale(0.97); }
.navlink .bar {
  position: absolute; left: 5px; top: 50%; width: 3px; height: 0; border-radius: 3px; background: var(--brand);
  transform: translateY(-50%); transition: height 0.28s var(--ease);
}
.navlink:hover { background: #f5f6f9; color: var(--ink); }
.navlink.router-link-active { background: var(--brand-50); color: var(--brand); font-weight: 600; }
.navlink.router-link-active .bar { height: 18px; }
.side-foot { margin-top: auto; padding: 14px 8px 4px; border-top: 1px solid var(--line); }
.who { display: flex; align-items: center; gap: 7px; font-size: 13px; margin-bottom: 8px; color: var(--ink-2); }
.adot { width: 7px; height: 7px; border-radius: 50%; background: #34c759; box-shadow: 0 0 0 3px rgba(52, 199, 89, 0.15); }
.main { flex: 1; min-width: 0; padding: 30px 40px 52px; overflow: auto; }

/* 移动端顶栏 + 遮罩，桌面端隐藏 */
.mtop { display: none; }
.scrim { display: none; }

@media (max-width: 820px) {
  .shell { display: block; }
  .mtop {
    display: flex; align-items: center; gap: 12px; height: 54px; padding: 0 14px;
    position: sticky; top: 0; z-index: 40;
    background: rgba(255, 255, 255, 0.9); backdrop-filter: saturate(1.4) blur(10px);
    border-bottom: 1px solid var(--line);
  }
  .burger {
    display: grid; place-items: center; width: 38px; height: 38px; flex: none;
    border: 1px solid var(--line-2); border-radius: 10px; background: #fff; color: var(--ink); cursor: pointer;
    transition: transform 0.11s var(--ease), background 0.18s var(--ease);
  }
  .burger:active { transform: scale(0.94); background: #f5f6f9; }
  .burger svg { width: 20px; height: 20px; }
  .mbrand { display: flex; align-items: center; gap: 8px; font-weight: 700; color: var(--ink); font-size: 15px; }
  .mlogo {
    width: 26px; height: 26px; border-radius: 8px; display: grid; place-items: center; flex: none;
    color: #fff; font-size: 13px; font-weight: 700; background: linear-gradient(135deg, #ef6b66, #e6534d);
  }

  /* 侧栏改为左侧滑入抽屉 */
  .side {
    position: fixed; left: 0; top: 0; height: 100vh; width: 264px; max-width: 82vw;
    transform: translateX(-100%); transition: transform 0.28s var(--ease);
    z-index: 60; box-shadow: 0 20px 55px rgba(30, 40, 70, 0.18);
  }
  .shell.menu-open .side { transform: translateX(0); }

  .scrim {
    display: block; position: fixed; inset: 0; z-index: 55;
    background: rgba(15, 20, 35, 0.42); opacity: 0; pointer-events: none;
    transition: opacity 0.28s var(--ease);
  }
  .shell.menu-open .scrim { opacity: 1; pointer-events: auto; }

  .main { padding: 18px 16px 40px; height: auto; }
}
</style>
