import { createApp } from 'vue'
import App from './App.vue'
import './panel.css'
import './style.css'
import { resolveTheme } from './lib/theme'

// 首屏前先定主题，避免闪白（与老页面 templates/index.html 顶部内联脚本同源）
document.documentElement.setAttribute('data-theme', resolveTheme())

createApp(App).mount('#app')
