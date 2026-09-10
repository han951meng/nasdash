import { createApp } from 'vue'
import App from './App.vue'
import './style.css'

/**
 * 主题判定：与既有 templates/index.html 第 7 行的内联脚本保持完全同源，
 * 优先级：本地 nasdash_theme(dark/light) → 飞牛 fnos-theme-mode → URL 参数 → 系统偏好。
 * 这样探针页嵌在飞牛里时，主题行为跟主面板一致。
 */
export function resolveTheme(): 'dark' | 'light' {
  try {
    const saved = localStorage.getItem('nasdash_theme')
    if (saved === 'dark') return 'dark'
    if (saved === 'light') return 'light'

    let fnos: string | null = null
    try {
      const f = localStorage.getItem('fnos-theme-mode')
      if (f === 'dark' || f === 'light') fnos = f
    } catch {
      /* localStorage 在某些沙箱下不可用 */
    }
    if (!fnos) {
      const p = new URLSearchParams(location.search).get('fnos-theme-mode')
      if (p === 'dark' || p === 'light') fnos = p
    }
    if (!fnos && window.parent && window.parent !== window) {
      try {
        const pp = new URLSearchParams(window.parent.location.search).get('fnos-theme-mode')
        if (pp === 'dark' || pp === 'light') fnos = pp
      } catch {
        /* 跨域时读不到父窗口，忽略 */
      }
    }
    if (fnos === 'dark') return 'dark'
    if (fnos === 'light') return 'light'

    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
      ? 'dark'
      : 'light'
  } catch {
    return 'light'
  }
}

document.documentElement.setAttribute('data-theme', resolveTheme())

createApp(App).mount('#app')
