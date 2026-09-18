import { createApp } from 'vue'
import App from './App.vue'
import './panel.css'
import { resolveTheme } from './lib/theme'

// 首屏前先定主题，避免闪白（与旧页时代顶部内联脚本的做法一致）
document.documentElement.setAttribute('data-theme', resolveTheme())

createApp(App).mount('#app')
