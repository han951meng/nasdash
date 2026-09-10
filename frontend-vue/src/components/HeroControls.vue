<script setup lang="ts">
/**
 * Hero 右侧控件组 —— 复刻老页面 heroControls()：刷新时间 + 立即刷新 + 主题切换。
 * 放在新页面的 hero 里，位置与老页面一致（右上角）。
 */
import { computed } from 'vue'
import AppIcon from './AppIcon.vue'
import { useTheme } from '../lib/useTheme'

const props = defineProps<{ lastUpdate?: string; busy?: boolean }>()
const emit = defineEmits<{ (e: 'refresh'): void }>()

const { saved, applied, cycle } = useTheme()

const themeIcon = computed(() => (saved.value === 'auto' ? 'contrast' : applied.value === 'dark' ? 'moon' : 'sun'))
const themeTitle = computed(
  () => `主题：${saved.value === 'auto' ? '跟随系统' : saved.value === 'dark' ? '深色' : '浅色'}（当前显示${applied.value === 'dark' ? '深色' : '浅色'}），点击切换`,
)
</script>

<template>
  <div class="hero-ctrls">
    <span class="lastUpdate hd-time">{{ props.lastUpdate || '加载中…' }}</span>
    <span class="spin spinner" :style="{ display: props.busy ? 'inline-block' : 'none' }" />
    <button class="icon-btn" title="立即刷新" @click="emit('refresh')"><AppIcon name="refresh" /></button>
    <button class="icon-btn" :title="themeTitle" @click="cycle"><AppIcon :name="themeIcon" /></button>
  </div>
</template>
