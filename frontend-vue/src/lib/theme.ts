/**
 * 主题判定 —— 与既有 templates/index.html 顶部内联脚本保持完全同源，
 * 优先级：本地 nasdash_theme(dark/light) → 飞牛 fnos-theme-mode → URL 参数 → 系统偏好。
 */
export const THEME_KEY = 'nasdash_theme'

export type ThemeSaved = 'dark' | 'light' | 'auto'
export type ThemeApplied = 'dark' | 'light'

export function resolveTheme(): ThemeApplied {
  try {
    const saved = localStorage.getItem(THEME_KEY)
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

/** 循环切换：浅色 → 深色 → 跟随系统 → 浅色 */
export function nextTheme(cur: ThemeSaved): ThemeSaved {
  return cur === 'light' ? 'dark' : cur === 'dark' ? 'auto' : 'light'
}

export const THEME_ZH: Record<string, string> = { light: '浅色', dark: '深色', auto: '跟随系统' }
