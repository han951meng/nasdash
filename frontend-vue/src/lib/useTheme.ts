/** 主题状态（模块级单例，壳与各页共用一份） */
import { ref } from 'vue'
import { THEME_KEY, THEME_ZH, nextTheme, resolveTheme } from './theme'
import type { ThemeApplied, ThemeSaved } from './theme'

const saved = ref<ThemeSaved>('auto')
const applied = ref<ThemeApplied>('light')
let inited = false

function sync(): void {
  const s = localStorage.getItem(THEME_KEY)
  saved.value = s === 'dark' || s === 'light' || s === 'auto' ? s : 'auto'
  const t = resolveTheme()
  applied.value = t
  document.documentElement.setAttribute('data-theme', t)
}

export function useTheme() {
  if (!inited) {
    inited = true
    sync()
  }
  const cycle = (): void => {
    saved.value = nextTheme(saved.value)
    localStorage.setItem(THEME_KEY, saved.value)
    sync()
  }
  const label = (): string => THEME_ZH[saved.value] ?? saved.value
  return { saved, applied, cycle, label, sync }
}
