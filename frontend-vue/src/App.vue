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
import { computed, onMounted, ref, watch } from 'vue'
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

const tab = ref('system')
const sidebarOpen = ref(false)
const frame = ref<HTMLIFrameElement | null>(null)

const { applied } = useTheme()

const currentLabel = computed(() => ALL_TABS.find(t => t.id === tab.value)?.label ?? '')
/** iframe 的 src：切到未迁移模块时内嵌旧页面板对应页签 */
const frameSrc = computed(() => legacyUrl(tab.value))

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
watch(applied, t => {
  const w = frame.value?.contentWindow
  if (w) {
    try {
      w.postMessage({ type: 'nasdash-theme', theme: t }, '*')
    } catch {
      /* 忽略 */
    }
  }
})

onMounted(() => {
  const h = location.hash.replace(/^#/, '')
  if (h && ALL_TABS.some(t => t.id === h)) tab.value = h
})
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
        <!-- 尚未迁移的模块：内嵌旧局面板（embed=1 让它收起自己的侧边栏/顶栏） -->
        <iframe
          v-else
          ref="frame"
          :src="frameSrc"
          class="legacy-frame"
          :title="currentLabel"
        />
      </div>
    </main>
  </div>

  <div class="nav-overlay" :class="{ show: sidebarOpen }" @click="sidebarOpen = false" />
</template>
