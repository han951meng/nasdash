<script setup lang="ts">
/**
 * Hero 右侧控件组 —— 复刻老页面 heroControls()：刷新时间 + 自动刷新开关 + 立即刷新 + 主题切换。
 * 放在新页面的 hero 里，位置与老页面一致（右上角）。
 *
 * 自动刷新开关（老页同口径）：
 *  - 开启后每 30 秒替当前页面点一次「立即刷新」；偏好存 localStorage（nasdash_autorefresh），默认关。
 *  - 同一时刻只有当前页面的实例在运行，所以 30s 一拍永远只刷「当前页」，不会全站乱刷。
 *  - 风扇控制页传 autoTick=false 跳过 30s 整页重建（老页同做法）：该页实时数据本就每秒
 *    就地刷新，整页重建反而会冲掉用户正在拖的滑块 / 正在编辑的曲线。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import AppIcon from './AppIcon.vue'
import { useTheme } from '../lib/useTheme'

const props = defineProps<{ lastUpdate?: string; busy?: boolean; /** false = 不参与 30s 自动刷新（风扇页用） */ autoTick?: boolean }>()
const emit = defineEmits<{ (e: 'refresh'): void }>()

const { saved, applied, cycle } = useTheme()

const themeIcon = computed(() => (saved.value === 'auto' ? 'contrast' : applied.value === 'dark' ? 'moon' : 'sun'))
const themeTitle = computed(
  () => `主题：${saved.value === 'auto' ? '跟随系统' : saved.value === 'dark' ? '深色' : '浅色'}（当前显示${applied.value === 'dark' ? '深色' : '浅色'}），点击切换`,
)

/* ---------------- 自动刷新（老页同款开关） ---------------- */
const AUTO_KEY = 'nasdash_autorefresh'
const auto = ref(false)
let autoTimer = 0

try {
  auto.value = localStorage.getItem(AUTO_KEY) === '1'
} catch {
  /* 沙箱下 localStorage 受限时静默降级为「不记住」 */
}

function setAuto(on: boolean): void {
  auto.value = on
  try {
    localStorage.setItem(AUTO_KEY, on ? '1' : '0')
  } catch {
    /* 忽略 */
  }
  if (autoTimer) {
    clearInterval(autoTimer)
    autoTimer = 0
  }
  if (on && props.autoTick !== false) {
    autoTimer = window.setInterval(() => emit('refresh'), 30000)
  }
}

onMounted(() => {
  if (auto.value) setAuto(true)
})
onUnmounted(() => {
  if (autoTimer) clearInterval(autoTimer)
})
</script>

<template>
  <div class="hero-ctrls">
    <span class="lastUpdate hd-time">{{ props.lastUpdate || '加载中…' }}</span>
    <label class="switch" title="自动刷新：每 30 秒自动刷新当前页（偏好会被记住）">
      <input type="checkbox" :checked="auto" @change="setAuto(($event.target as HTMLInputElement).checked)" />
      <span class="slider" />自动刷新
    </label>
    <span class="spin spinner" :style="{ display: props.busy ? 'inline-block' : 'none' }" />
    <button class="icon-btn" title="立即刷新" @click="emit('refresh')"><AppIcon name="refresh" /></button>
    <button class="icon-btn" :title="themeTitle" @click="cycle"><AppIcon :name="themeIcon" /></button>
  </div>
</template>
