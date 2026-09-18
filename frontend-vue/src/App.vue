<script setup lang="ts">
/**
 * 主入口外壳（阶段 2 起接管）。
 *
 * 结构（复用老页面的 .header/.container/.sidebar/.tabs/.panel 类名，
 * 因此观感与旧版面板完全一致）：
 *  - 左侧：11 个模块页签（分组与老页面一致）
 *  - 右侧内容区：**11 个模块全部为 Vue 原生页** ——
 *      硬件配置检测 / 系统资源 / 温度监控 / 历史趋势 / 操作手册 / 关于 /
 *      Docker / 控制与自动化 / 存储卷 / 硬盘 SMART / 风扇控制
 *
 * v2.3.0 第 11 步「旧页退休瘦身」：旧版单页面板（templates/index.html）与
 * 壳内 iframe 回滚宿主已全部删除，界面 100% 由本应用渲染，不再有任何旧页依赖。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import AppIcon from './components/AppIcon.vue'
import SystemResources from './pages/SystemResources.vue'
import Temperature from './pages/Temperature.vue'
import History from './pages/History.vue'
import Manual from './pages/Manual.vue'
import About from './pages/About.vue'
import Docker from './pages/Docker.vue'
import Ports from './pages/Ports.vue'
import Detect from './pages/Detect.vue'
import Automation from './pages/Automation.vue'
import Storage from './pages/Storage.vue'
import Disks from './pages/Disks.vue'
import Fan from './pages/Fan.vue'
import { freshness } from './lib/api'
import { useTheme } from './lib/useTheme'

interface TabDef {
  id: string
  label: string
  icon: string
  /** 是否实时轮询页：是则在页眉显示「数据于 N 秒前更新」 */
  realtime?: boolean
}
interface NavBlock {
  group?: string
  tabs: TabDef[]
}

const NAV: NavBlock[] = [
  { tabs: [{ id: 'detect', label: '硬件配置检测', icon: 'detect', realtime: true }] },
  {
    group: '资源监控',
    tabs: [
      { id: 'system', label: '系统资源', icon: 'system', realtime: true },
      { id: 'temps', label: '温度监控', icon: 'thermo', realtime: true },
      { id: 'sys-hist', label: '历史趋势', icon: 'history' },
    ],
  },
  {
    group: '存储',
    tabs: [
      { id: 'disks', label: '硬盘 SMART', icon: 'hdd', realtime: true },
      { id: 'storage', label: '存储卷', icon: 'storage', realtime: true },
    ],
  },
  {
    group: '其他',
    tabs: [
      { id: 'fan', label: '风扇控制', icon: 'fan', realtime: true },
      { id: 'docker', label: 'Docker', icon: 'docker', realtime: true },
      { id: 'ports', label: '端口占用', icon: 'ports', realtime: true },
      { id: 'automation', label: '控制与自动化', icon: 'automation', realtime: true },
      { id: 'manual', label: '操作手册', icon: 'manual' },
      { id: 'about', label: '关于 nasdash', icon: 'about' },
    ],
  },
]

const ALL_TABS: TabDef[] = NAV.flatMap(b => b.tabs)

/**
 * 首屏落点：默认「硬件配置检测」（与老版本一致 —— 打开先看整机体检总览）。
 * 带 #xxx 时优先恢复该页签。
 */
const startTab = (() => {
  try {
    const h = location.hash.replace(/^#/, '')
    if (h && ALL_TABS.some(t => t.id === h)) return h
  } catch {
    /* 某些沙箱下 location 受限，忽略 */
  }
  return 'detect'
})()

const tab = ref(startTab)
const sidebarOpen = ref(false)

// 主题（模块级单例）：这里调用一次，确保外壳一挂载就把 data-theme 落到 <html>，
// 不必等某个页面的 hero 控件先渲染。
useTheme()

/** 当前页签标题，供无障碍朗读 */
const currentLabel = computed(() => ALL_TABS.find(t => t.id === tab.value)?.label ?? '')

function select(id: string): void {
  tab.value = id
  sidebarOpen.value = false
  try {
    // 用 hash 记录当前页签：刷新后仍停在原页（不触发服务端请求）
    history.replaceState(null, '', '#' + id)
  } catch {
    /* 某些沙箱下 history 受限，忽略 */
  }
  window.scrollTo({ top: 0, left: 0, behavior: 'smooth' })
}

/** 跨页跳转总线（v2.3.0 3.6）：任意 Vue 原生页 dispatch nasdash-nav 即可换页签。
 *  用 window 事件而不是 props/路由：外壳与页面是松耦合的，页面不该知道外壳结构。 */
function onNav(e: Event): void {
  const id = (e as CustomEvent).detail?.tab
  if (id && ALL_TABS.some(t => t.id === id)) select(id)
}
onMounted(() => window.addEventListener('nasdash-nav', onNav))
onUnmounted(() => window.removeEventListener('nasdash-nav', onNav))

// 数据新鲜度（3.9，隐形版）：平时不显示；只有实时页数据超过 10 秒没刷新才弹出黄色「数据已 N 秒未更新」告警（后端卡死/轮询断开时才有用，避免常驻装饰条）
const realtime = computed(() => ALL_TABS.find(t => t.id === tab.value)?.realtime === true)
const now = ref(Date.now())
let tick = 0
const staleSec = computed(() => (freshness.lastAny ? Math.floor((now.value - freshness.lastAny) / 1000) : -1))
const freshnessVisible = computed(() => realtime.value && staleSec.value > 10)
onMounted(() => { tick = window.setInterval(() => { now.value = Date.now() }, 1000) })
onUnmounted(() => { if (tick) window.clearInterval(tick) })
</script>

<template>
  <div class="header">
    <button class="btn menu-toggle" @click="sidebarOpen = !sidebarOpen">
      <AppIcon name="menu" /> 模块
    </button>
  </div>

  <div class="container">
    <aside class="sidebar" :class="{ open: sidebarOpen }">
      <div class="sidebar-title">监控模块</div>
      <div class="tabs">
        <template v-for="(block, bi) in NAV" :key="bi">
          <div v-if="block.group" class="sb-group">{{ block.group }}</div>
          <button
            v-for="t in block.tabs"
            :key="t.id"
            class="tab"
            :class="{ active: t.id === tab }"
            :title="t.label"
            @click="select(t.id)"
          >
            <AppIcon :name="t.icon" /> {{ t.label }}
          </button>
        </template>
      </div>
    </aside>

    <main class="content" :aria-label="currentLabel">
      <div v-if="freshnessVisible" class="freshness stale">
        <span class="fresh-dot" />
        数据已 {{ staleSec }} 秒未更新 · 后端可能已断开
      </div>
      <div class="panel active">
        <Detect v-if="tab === 'detect'" />
        <SystemResources v-else-if="tab === 'system'" />
        <Temperature v-else-if="tab === 'temps'" />
        <History v-else-if="tab === 'sys-hist'" />
        <Manual v-else-if="tab === 'manual'" />
        <About v-else-if="tab === 'about'" />
        <Docker v-else-if="tab === 'docker'" />
        <Ports v-else-if="tab === 'ports'" />
        <Automation v-else-if="tab === 'automation'" />
        <Storage v-else-if="tab === 'storage'" />
        <Disks v-else-if="tab === 'disks'" />
        <Fan v-else-if="tab === 'fan'" />
      </div>
    </main>
  </div>

  <div class="nav-overlay" :class="{ show: sidebarOpen }" @click="sidebarOpen = false" />
</template>
