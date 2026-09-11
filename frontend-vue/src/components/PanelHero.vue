<script setup lang="ts">
/**
 * 统一面板 Hero 横幅 —— 复刻老页面 panelHero() 的 DOM 结构与类名，
 * 因此新页面与旧页面观感完全一致（含 .hero-stats 指标格、右上角控件组）。
 */
import AppIcon from './AppIcon.vue'
import HeroControls from './HeroControls.vue'

export interface HeroStat {
  /** 主数值（字符串，已格式化） */
  v?: string
  /** 需要富文本（如网络 ↑↓ 双行）时用它，优先于 v */
  html?: string
  /** 单位（小字） */
  unit?: string
  /** 指标名 */
  k: string
  /** 额外类名，如 'net' */
  cls?: string
  /** 悬浮说明 */
  tip?: string
}

const props = defineProps<{
  icon?: string
  title: string
  sub?: string
  stats?: HeroStat[]
  /** 健康徽章 */
  badge?: { ok: boolean; text: string }
  lastUpdate?: string
  busy?: boolean
}>()

const emit = defineEmits<{ (e: 'refresh'): void }>()
</script>

<template>
  <div class="hero">
    <div class="hero-head">
      <div class="hero-left">
        <div v-if="props.icon" class="hero-ic"><AppIcon :name="props.icon" /></div>
        <div class="hero-text">
          <div class="hero-title">{{ props.title }}</div>
          <div v-if="props.sub" class="hero-sub">{{ props.sub }}</div>
        </div>
      </div>
      <div class="hero-right">
        <div v-if="props.badge" class="hero-badge" :class="{ warn: !props.badge.ok }">
          <span class="dot" />{{ props.badge.text }}
        </div>
        <div class="hero-action">
          <HeroControls :last-update="props.lastUpdate" :busy="props.busy" @refresh="emit('refresh')" />
          <!-- 页面自定义控件（旧页 panelHero 的 o.action，如风扇页的「接管风扇控制」开关） -->
          <slot name="action" />
        </div>
      </div>
    </div>
    <div v-if="props.stats && props.stats.length" class="hero-stats">
      <div
        v-for="(st, i) in props.stats"
        :key="i"
        class="hs"
        :class="[st.cls || '', st.k === '已运行' ? 'hs-uptime' : '']"
      >
        <div v-if="st.html" class="hs-v" v-html="st.html" />
        <div v-else class="hs-v">{{ st.v }}<small v-if="st.unit">{{ st.unit }}</small></div>
        <div class="hs-k" :title="st.tip || ''">{{ st.k }}</div>
      </div>
    </div>
  </div>
</template>
