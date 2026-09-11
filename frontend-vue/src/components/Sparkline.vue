<script setup lang="ts">
/**
 * 通用迷你折线组件（canvas）——复刻 templates/index.html 的
 * drawSingleSpark / drawGpuSpark / drawNetSpark 三套绘制逻辑：
 *  - 曲线下方浅色填充（暗色主题提高不透明度，避免"闷"掉）
 *  - 无刻度、无网格、贴边留 2px
 *  - 数据为空时画一条 0 基线
 * 主题切换（data-theme 变化）时自动重绘。
 */
import { onMounted, onUnmounted, ref, watch } from 'vue'

export interface SparkLine {
  /** 数据点；null 表示该点缺失（折线跳过，填充按 0 计） */
  data: (number | null)[]
  /** 颜色，支持 'var(--x)' 或具体色值 */
  color: string
  /** 是否画曲线下方填充，默认 true */
  fill?: boolean
}

const props = withDefaults(
  defineProps<{
    lines: SparkLine[]
    /** 'auto' 时按数据最大值自适应（网络卡用），否则固定为该数值 */
    max?: number | 'auto'
    /** canvas 内部像素宽（会被 CSS 拉伸） */
    w?: number
    /** canvas 内部像素高 */
    h?: number
    /** CSS 显示高度 */
    cssH?: number
    lineWidth?: number
  }>(),
  { max: 100, w: 600, h: 100, cssH: 80, lineWidth: 1.8 },
)

const cv = ref<HTMLCanvasElement | null>(null)
let mo: MutationObserver | null = null

function cssVar(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}

/** 把 'var(--blue)' 解析成实际颜色值 */
function resolveColor(c: string): string {
  const m = /^var\(\s*(--[\w-]+)\s*(?:,\s*([^)]+))?\s*\)$/.exec((c || '').trim())
  if (!m) return c || '#888'
  return cssVar(m[1], (m[2] || '#888').trim())
}

function draw(): void {
  const el = cv.value
  if (!el) return
  const ctx = el.getContext('2d')
  if (!ctx) return

  const W = el.width
  const H = el.height
  ctx.clearRect(0, 0, W, H)

  const padT = 2
  const padB = 2
  const ih = H - padT - padB
  const isDark = document.documentElement.getAttribute('data-theme') === 'dark'
  const lines = props.lines || []

  // 完全无数据：画一条 0 基线后返回
  const hasData = lines.some(l => (l.data || []).some(v => v != null))
  if (!hasData) {
    const c = resolveColor(lines[0]?.color || 'var(--blue)')
    ctx.strokeStyle = c
    ctx.globalAlpha = 0.25
    ctx.lineWidth = 1.5
    ctx.beginPath()
    ctx.moveTo(0, padT + ih)
    ctx.lineTo(W, padT + ih)
    ctx.stroke()
    ctx.globalAlpha = 1
    return
  }

  // 量程：固定值，或按数据最大值（下限 1024，避免网络图被小流量放大成锯齿）
  let maxV: number
  if (props.max === 'auto') {
    let m = 0
    lines.forEach(l => l.data.forEach(v => { if (v != null && v > m) m = v }))
    maxV = m < 1024 ? 1024 : m
  } else {
    maxV = props.max
  }

  const px = (i: number, len: number) => (len <= 1 ? 0 : (i * W) / (len - 1))
  const py = (v: number) => padT + ih - (Math.max(0, Math.min(v, maxV)) / maxV) * ih

  // 按传入顺序绘制：后面的线盖在前面的上面（GPU 温度盖显存、网络下载盖上传）
  lines.forEach(l => {
    const arr = l.data || []
    if (!arr.length) return
    const color = resolveColor(l.color)

    if (l.fill !== false) {
      ctx.fillStyle = color
      ctx.globalAlpha = isDark ? 0.28 : 0.16
      ctx.beginPath()
      ctx.moveTo(px(0, arr.length), padT + ih)
      arr.forEach((v, i) => ctx.lineTo(px(i, arr.length), py(v == null ? 0 : v)))
      ctx.lineTo(px(arr.length - 1, arr.length), padT + ih)
      ctx.closePath()
      ctx.fill()
      ctx.globalAlpha = 1
    }

    ctx.strokeStyle = color
    ctx.lineWidth = props.lineWidth
    ctx.lineJoin = 'round'
    ctx.lineCap = 'round'
    ctx.beginPath()
    let started = false
    arr.forEach((v, i) => {
      if (v == null) return
      const x = px(i, arr.length)
      const y = py(v)
      if (!started) {
        ctx.moveTo(x, y)
        started = true
      } else {
        ctx.lineTo(x, y)
      }
    })
    ctx.stroke()
  })
}

watch(() => props.lines, draw, { deep: true })
watch(() => props.max, draw)

onMounted(() => {
  draw()
  // 主题切换时重绘（填充透明度依赖明暗）
  mo = new MutationObserver(() => draw())
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
})
onUnmounted(() => {
  if (mo) mo.disconnect()
})
</script>

<template>
  <canvas
    ref="cv"
    :width="w"
    :height="h"
    :style="{ width: '100%', height: cssH + 'px', display: 'block' }"
  />
</template>
