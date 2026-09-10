<script setup lang="ts">
/**
 * 统一图标组件：彩色插画走 PNG（base64 data URL），线性图标走 24x24 SVG path。
 * 类名沿用老页面的 `.ic` / `.ico`，直接吃到 panel.css 里的尺寸规则
 * （`.tab img.ic` = 30x30、`.hero-ic img.ic` = 48x48、`.ico` = 描边样式）。
 */
import { computed } from 'vue'
import { ICON_PNG, ICON_SVG } from '../lib/icons'

const props = defineProps<{ name: string }>()

const png = computed(() => ICON_PNG[props.name] || '')
const svg = computed(() => ICON_SVG[props.name] || '')
</script>

<template>
  <img v-if="png" :src="png" class="ic" alt="" />
  <svg
    v-else
    class="ico ic"
    viewBox="0 0 24 24"
    v-html="svg"
  />
</template>
