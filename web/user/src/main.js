import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import './theme.css'
import App from './App.vue'
import router, { preloadRoutes } from './router.js'

createApp(App).use(ElementPlus, { locale: zhCn }).use(router).mount('#app')

// 首屏渲染后趁空闲预取所有路由分块，之后页面切换即时无等待。
const idle = window.requestIdleCallback || ((cb) => setTimeout(cb, 200))
idle(() => preloadRoutes())
