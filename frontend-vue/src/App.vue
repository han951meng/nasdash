<script setup lang="ts">
/**
 * 阶段 2 · 新主入口外壳
 *
 * 结构（复用老页面的 .header/.container/.sidebar/.tabs/.panel 类名，
 * 因此观感与旧版面板完全一致）：
 *  - 左侧：11 个模块页签（分组与老页面一致）
 *  - 右侧内容区：
 *      「系统资源」「温度监控」→ Vue 原生页面
 *      尚未迁移的 9 个模块   → iframe 内嵌旧版面板对应页（?embed=1 隐藏旧页自己的侧边栏，避免套娃）
 *
 * 回滚通道：旧版完整面板仍在 /legacy/，随时可切回。
 */
import { computed, ref, watch } from 'vue'
import AppIcon from './components/AppIcon.vue'
import SystemResources from './pages/SystemResources.vue'
import Temperature from './pages/Temperature.vue'
import { legacyUrl } from './lib/api'
import { useTheme } from './lib/useTheme'

interface TabDef {
  id: string
  label: string
  icon: string
}
interface NavBlock {
  group?: string
  tabs: TabDef[]
}

const NAV: NavBlock[] = [
  { tabs: [{ id: 'detect', label: '硬件配置检测', icon: 'detect' }] },
  {
    group: '资源监控',
    tabs: [
      { id: 'system', label: '系统资源', icon: 'system' },
      { id: 'temps', label: '温度监控', icon: 'thermo' },
      { id: 'sys-hist', label: '历史趋势', icon: 'history' },
    ],
  },
  {
    group: '存储',
    tabs: [
      { id: 'disks', label: '硬盘 SMART', icon: 'hdd' },
      { id: 'storage', label: '存储卷', icon: 'storage' },
    ],
  },
  {
    group: '其他',
    tabs: [
      { id: 'fan', label: '风扇控制', icon: 'fan' },
      { id: 'docker', label: 'Docker', icon: 'docker' },
      { id: 'automation', label: '控制与自动化', icon: 'automation' },
      { id: 'manual', label: '操作手册', icon: 'manual' },
      { id: 'about', label: '关于 nasdash', icon: 'about' },
    ],
  },
]

const ALL_TABS: TabDef[] = NAV.flatMap(b => b.tabs)

/** 已迁成 Vue 原生页的模块；其余仍靠内嵌旧页 */
const NATIVE_TABS = new Set(['system', 'temps'])

/**
 * 首屏落点：默认「硬件配置检测」（与老版本一致 —— 打开先看整机体检总览）。
 * 带 #xxx 时优先恢复该页签。
 * 必须在 setup 阶段就定下来：如果先渲染默认页、挂载后再按 hash 改，
 * 内嵌的旧页会先按旧页签加载一遍再被切走，等于又闪一次。
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

const frame = ref<HTMLIFrameElement | null>(null)
/** 首次进入未迁移模块后就地挂载，之后一直常驻：换页签只发消息，不再重载整页旧面板 */
const frameMounted = ref(false)
/** 旧页 load 完成前不能给它发消息（监听器还没注册） */
const frameReady = ref(false)
/** 挂载时用的那个页签，写进 iframe 首次的 URL（之后 src 不再变） */
const firstLegacyTab = ref('')
/** 载入期间又被点走的页签，等 load 完补发 */
const pendingTab = ref<string | null>(null)

const { applied } = useTheme()

const currentLabel = computed(() => ALL_TABS.find(t => t.id === tab.value)?.label ?? '')
/** 当前停在 Vue 原生页；内嵌旧页的可见性与之相反 */
const isNativeTab = computed(() => NATIVE_TABS.has(tab.value))
/**
 * iframe 的 src 只定一次。
 * 之前是跟着当前页签变的，点一次未迁移模块就整页重载一次 3MB 旧面板 —— 既慢，
 * 又会让旧页先画出写死 active 的「硬件配置检测」再切走（用户看到的「闪一下」）。
 * 现在首次带上 ?tab=，之后固定不动，换页签改走 postMessage 就地 switchTab。
 */
const frameSrc = computed(() => (frameMounted.value ? legacyUrl(firstLegacyTab.value) : ''))

function postTheme(): void {
  const w = frame.value?.contentWindow
  if (!w) return
  try {
    w.postMessage({ type: 'nasdash-theme', theme: applied.value }, '*')
  } catch {
    /* 忽略 */
  }
}
/** 直接发（调用方保证旧页已就绪） */
function postTabNow(t: string): void {
  const w = frame.value?.contentWindow
  if (!w) return
  try {
    w.postMessage({ type: 'nasdash-tab', tab: t }, '*')
  } catch {
    /* 忽略 */
  }
}
function onFrameLoad(): void {
  frameReady.value = true
  postTheme()
  const t = pendingTab.value
  pendingTab.value = null
  // 首屏那次已由 URL 里的 ?tab= 切好，不必重复发
  if (t && t !== firstLegacyTab.value) postTabNow(t)
}

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

// 主题变化时通知内嵌的旧页面跟随（旧页监听 message 后重新套用主题）
watch(applied, postTheme)

// 页签变化：未迁移模块 → 确保旧页已挂载并切过去；已挂载就直接发消息，不重载。
// immediate：首屏如果就落在未迁移模块（默认就是「硬件配置检测」），这一轮就要把旧页挂起来。
watch(tab, t => {
  if (NATIVE_TABS.has(t)) return
  if (!frameMounted.value) {
    firstLegacyTab.value = t
    pendingTab.value = t
    frameMounted.value = true
    return
  }
  if (frameReady.value) postTabNow(t)
  else pendingTab.value = t
}, { immediate: true })
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
            @click="select(t.id)"
          >
            <AppIcon :name="t.icon" /> {{ t.label }}
          </button>
        </template>
      </div>
    </aside>

    <main class="content">
      <div class="panel active">
        <SystemResources v-if="tab === 'system'" />
        <Temperature v-else-if="tab === 'temps'" />
        <!-- 尚未迁移的模块：内嵌旧局面板（embed=1 让它收起自己的侧边栏/顶栏）。
             这里必须用 v-show 而不是 v-else：切到 Vue 原生页时只把旧页藏起来、
             不销毁，否则从「系统资源」回到任一旧模块都要重载一次约 3MB 的旧页面。 -->
        <div v-show="!isNativeTab" class="legacy-host">
          <iframe
            v-if="frameMounted"
            ref="frame"
            :src="frameSrc"
            class="legacy-frame"
            :title="currentLabel"
            @load="onFrameLoad"
          />
          <div v-if="!frameReady" class="legacy-loading">正在载入模块…</div>
        </div>
      </div>
    </main>
  </div>

  <div class="nav-overlay" :class="{ show: sidebarOpen }" @click="sidebarOpen = false" />
</template>
