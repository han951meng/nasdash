<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { resolveTheme } from './main'

/**
 * 阶段 1b · 温度监控页 Vue 版
 * 复刻 templates/index.html 的 renderTemps + 当前温度概览逻辑，组件化、挂在独立入口 /vue/：
 *  - 温度墙：主板测点 + 阵列卡芯片温度 + 显卡温度，长设备名不撑破格子、文字居中
 *  - ★最准 徽标：仅 "CPU 封装温度" 标，悬停解释"CPU 内部传感器直读，最准确"
 *  - 独显/核显分支：采信后端 type（核显/独显），后端无有效 type 才回退按厂商编号判断
 *  - 每 5 秒轮询 /api/fan/temps（统一温度快照）
 * 不动主面板 / 不替正式入口。
 */

// 接口基址：子路径下不能写裸 /api/*（实测 404），统一从当前路径截 index.cgi 再拼
const API_BASE = (() => {
  const m = location.pathname.match(/^(.*\/index\.cgi)\/?/)
  return m ? m[1] : ''
})()
const API = API_BASE + '/api/fan/temps'
const PAGE_PATH = location.pathname

// ===== 类型 =====
interface SensorItem { name: string; value: number; raw?: string; max?: number | null; crit?: number | null }
interface GpuItem { name?: string; vendor?: string; type?: string; temp?: number | null }
interface DiskItem {
  dev: string; name?: string; category?: string; is_system?: boolean
  temp?: number | null; asleep?: boolean; no_sleep?: boolean; is_nvme?: boolean; nvme_start_temp?: number
}
interface TempsResp {
  cpu_temp?: number | null
  mb_temp?: number | null
  disks?: DiskItem[]
  sensors?: SensorItem[]
  raid_temp?: number | null
  gpus?: GpuItem[]
}
// 温度墙一项
interface WallEntry { name: string; raw?: string; value: number; max?: number | null; crit?: number | null }

// 不直观 / 重复测点（与主页面 EXCLUDE_TEMPS 一致）
const EXCLUDE_TEMPS = new Set([
  'CPU PSS', 'CPU VRM', 'PECI Agent 0 Calibration', '内存温度 1', '内存温度 2', '复合温度',
])

// ===== 状态 =====
const data = ref<TempsResp | null>(null)
const httpStatus = ref<number | null>(null)
const latencyMs = ref<number | null>(null)
const errorText = ref('')
const pollCount = ref(0)
const lastAt = ref('')
const ticks = ref(0)
// 硬盘温度缓存：读不出(temp=null)但前一刻读到时沿用上次值，避免数字反复消失（与主页面同意图）
const lastDiskTemps = ref<Record<string, number>>({})
let pollTimer: number | undefined
let tickTimer: number | undefined

// ===== 配色（与主页面 tempColor 同源）=====
function tempColor(t: number | null, trip?: number | null): string {
  if (t == null) return 'var(--muted)'
  const pct = trip ? t / trip : t / 60
  if (pct > 0.9) return 'var(--red)'
  if (pct > 0.75) return 'var(--orange)'
  return 'var(--green)'
}

// ===== 温度墙 =====
const wallEntries = computed<WallEntry[]>(() => {
  const d = data.value
  if (!d) return []
  // 主板测点全览（过滤无效 0°C / 负温 / 无参考测点）
  const sens = (d.sensors || [])
    .filter(t => typeof t.value === 'number' && t.value > 0 && t.value < 150 && !EXCLUDE_TEMPS.has(t.name))
    .map(t => ({ name: t.name, raw: t.raw, value: t.value as number, max: t.max, crit: t.crit }))
  // 阵列卡芯片温度
  const raidEntry: WallEntry[] = (typeof d.raid_temp === 'number')
    ? [{ name: '阵列卡芯片温度', raw: 'Controller Temperature (ROC)', value: d.raid_temp, max: 80, crit: 90 }]
    : []
  // 显卡温度：独显/核显口径采信后端 type，回退前端按厂商编号
  const gpusSrc = d.gpus || []
  const gpuEntries = gpusSrc
    .map((g) => {
      const _t = String(g.type || '').trim()
      const isIgpu = _t === '核显' ? true : _t === '独显' ? false : (g.vendor !== '10de' && g.vendor !== '1002')
      const nm = g.name || '显卡'
      const label = (isIgpu ? '核显' : '独显') + (gpusSrc.length > 1 ? (' ' + nm) : '')
      return { name: label, raw: nm + ' GPU 温度', value: g.temp as number, max: 95, crit: 100 }
    })
    .filter(e => typeof e.value === 'number' && e.value > 0 && e.value < 150)
  return [...sens, ...raidEntry, ...gpuEntries]
})

const isBest = (name: string) => name === 'CPU 封装温度'

// 汇总
const summary = computed(() => {
  const entries = wallEntries.value
  if (!entries.length) return { max: null as number | null, avg: null as number | null, warn: 0, crit: 0 }
  let sum = 0
  let max: number | null = null
  let warn = 0
  let crit = 0
  entries.forEach(t => {
    const v = t.value
    sum += v
    if (t.crit && v >= t.crit) crit++
    else if (t.max && v >= t.max) warn++
    else if (v >= 85) warn++
    if (max === null || v > max) max = v
  })
  return { max, avg: Math.round(sum / entries.length), warn, crit }
})
const statusText = computed(() => {
  const s = summary.value
  if (s.crit) return `${s.crit} 个临界`
  if (s.warn) return `${s.warn} 个偏高`
  return '全部正常'
})
const statusOk = computed(() => !summary.value.crit && !summary.value.warn)
const cpuTemp = computed(() => data.value?.cpu_temp ?? null)
const raidTemp = computed(() => data.value?.raid_temp ?? null)

// ===== 当前温度概览（CPU/主板 + 硬盘分 NVMe/SSD/HDD）=====
interface OvChip { name: string; val: number | null; color: string; tag: string; tagClass: string; title: string }
interface OvCat { label: string; chips: OvChip[] }
const overviewCats = computed<OvCat[]>(() => {
  const d = data.value
  if (!d) return []
  const out: OvCat[] = []
  const cpu = d.cpu_temp ?? null
  const mb = d.mb_temp ?? null
  out.push({
    label: '核心',
    chips: [
      { name: 'CPU', val: cpu, color: tempColor(cpu, 100), tag: '', tagClass: '', title: '' },
      { name: '主板', val: mb, color: tempColor(mb, 90), tag: '', tagClass: '', title: '' },
    ],
  })
  const cats = [
    { k: 'NVMe', label: 'NVMe' },
    { k: 'SSD', label: 'SSD' },
    { k: 'HDD', label: '机械' },
  ]
  cats.forEach(c => {
    const inCat = (d.disks || []).filter(x => x.category === c.k)
    if (!inCat.length) return
    inCat.sort((a, b) => (b.is_system ? 1 : 0) - (a.is_system ? 1 : 0))
    const chips: OvChip[] = inCat.map(x => {
      const isNv = !!x.is_nvme
      let v = x.temp ?? null
      if (v == null && x.asleep && lastDiskTemps.value[x.dev] != null) v = lastDiskTemps.value[x.dev]
      const trip = isNv ? 75 : 60
      let tag = ''
      let tagClass = ''
      let title = ''
      if (x.asleep) {
        tag = '休眠'
        tagClass = 'off'
      } else if (isNv) {
        tag = '被动散热'
        tagClass = 'off'
        title = 'M.2 固态多为被动散热（自带散热片、贴在主板上），机箱风扇的气流基本吹不到它，而且 ' +
          (x.nvme_start_temp ?? 65) + '°C 以下对固态属于完全正常的工作温度。\n' +
          '因此它不会去催风扇转、也不会阻止风扇停转；只有超过 ' + (x.nvme_start_temp ?? 65) + '°C（接近降频保护线）才会参与风扇温控。'
      } else if (x.no_sleep) {
        tag = '常驻'
        tagClass = 'on'
      }
      return {
        name: x.name || (x.dev || '').replace(/^\/dev\//, ''),
        val: v,
        color: tempColor(v, trip),
        tag,
        tagClass,
        title,
      }
    })
    out.push({ label: c.label, chips })
  })
  return out
})

// ===== 拉取 =====
async function fetchTemps(): Promise<void> {
  const t0 = performance.now()
  try {
    const res = await fetch(API, { credentials: 'include', cache: 'no-store' })
    httpStatus.value = res.status
    latencyMs.value = Math.round(performance.now() - t0)
    if (!res.ok) {
      errorText.value = `HTTP ${res.status}`
      return
    }
    const j = (await res.json()) as TempsResp
    // 更新硬盘温度缓存（避免读不出时数字消失）
    ;(j.disks || []).forEach(x => {
      if (x.temp != null) lastDiskTemps.value[x.dev] = x.temp
    })
    data.value = j
    errorText.value = ''
    pollCount.value += 1
    lastAt.value = new Date().toLocaleTimeString('zh-CN')
  } catch (e) {
    latencyMs.value = Math.round(performance.now() - t0)
    errorText.value = e instanceof Error ? e.message : String(e)
  }
}

// ===== 主题 =====
const savedTheme = ref('auto')
const appliedTheme = ref<'dark' | 'light'>('light')
function applyFromStorage(): void {
  const v = localStorage.getItem('nasdash_theme') ?? 'auto'
  savedTheme.value = v
  const t = resolveTheme()
  appliedTheme.value = t
  document.documentElement.setAttribute('data-theme', t)
}
function cycleTheme(): void {
  const next = savedTheme.value === 'light' ? 'dark' : savedTheme.value === 'dark' ? 'auto' : 'light'
  localStorage.setItem('nasdash_theme', next)
  applyFromStorage()
}
const themeLabel = computed(() => {
  const zh: Record<string, string> = { light: '浅色', dark: '深色', auto: '跟随系统' }
  return `${zh[savedTheme.value] ?? savedTheme.value}（实际 ${appliedTheme.value === 'dark' ? '深色' : '浅色'}）`
})
const statusClass = computed(() => {
  if (errorText.value) return 'bad'
  if (httpStatus.value === 200) return 'ok'
  return 'idle'
})

onMounted(() => {
  applyFromStorage()
  void fetchTemps()
  tickTimer = window.setInterval(() => (ticks.value += 1), 1000)
  pollTimer = window.setInterval(() => void fetchTemps(), 5000)
})
onUnmounted(() => {
  if (tickTimer) window.clearInterval(tickTimer)
  if (pollTimer) window.clearInterval(pollTimer)
})
</script>

<template>
  <div class="wrap">
    <header class="head">
      <div>
        <h1 class="page-title">nasdash · 温度监控（Vue 版）</h1>
        <p class="sub">阶段 1b —— 组件化温度墙 + ★最准 徽标 + 每 5 秒轮询，独立入口不影响主面板</p>
      </div>
      <button class="theme-btn" @click="cycleTheme">主题：{{ themeLabel }}</button>
    </header>

    <!-- 接口状态 -->
    <div class="row">
      <span class="pill" :class="statusClass">
        {{ errorText ? '失败：' + errorText : httpStatus === 200 ? 'HTTP 200' : '请求中…' }}
      </span>
      <span class="meta">耗时 {{ latencyMs ?? '—' }} ms</span>
      <span class="meta">第 {{ pollCount }} 次采样 · {{ lastAt || '—' }}</span>
      <button class="mini" @click="fetchTemps">立即刷新</button>
    </div>

    <!-- Hero -->
    <section class="card hero">
      <div class="hero-top">
        <div>
          <h2 class="hero-title">温度监控</h2>
          <p class="hero-sub">主板 / CPU / 芯片组 / 阵列卡 温度与阈值</p>
        </div>
        <span class="badge" :class="statusOk ? 'ok' : 'bad'">{{ statusText }}</span>
      </div>
      <div class="hero-stats">
        <div class="hs"><b>{{ cpuTemp != null ? cpuTemp + '°C' : '—' }}</b><span>CPU 温度</span></div>
        <div class="hs"><b>{{ raidTemp != null ? raidTemp + '°C' : '—' }}</b><span>阵列卡温度</span></div>
        <div class="hs"><b>{{ summary.max != null ? summary.max + '°C' : '—' }}</b><span>最高温度</span></div>
        <div class="hs"><b>{{ summary.avg != null ? summary.avg + '°C' : '—' }}</b><span>平均温度</span></div>
      </div>
    </section>

    <!-- 温度墙 -->
    <section class="card">
      <h3>温度墙</h3>
      <div v-if="wallEntries.length" class="temp-wall">
        <div
          v-for="t in wallEntries"
          :key="t.name"
          class="tchip"
          :title="t.raw ? t.raw + '（原始测点名）' : ''"
        >
          <span class="tc-name">
            {{ t.name }}<span v-if="isBest(t.name)" class="best-tag" title="CPU 内部传感器直读，最准确">★最准</span>
          </span>
          <span class="tc-val" :style="{ color: tempColor(t.value, (t.crit || t.max || 85) as number | null) }">{{ t.value }}°C</span>
        </div>
      </div>
      <p v-else class="sub">{{ errorText ? '加载失败：' + errorText : '未检测到温度测点' }}</p>
      <div class="note">
        主板传感器测点 + 阵列卡芯片温度 + 显卡温度全览（已过滤无效 0°C / 负温）；鼠标悬停可查看原始英文名。
        颜色：<span style="color:var(--green)">绿正常</span> · <span style="color:var(--orange)">橙偏高</span> · <span style="color:var(--red)">红危险</span>
      </div>
    </section>

    <!-- 当前温度概览 -->
    <section class="card temp-overview">
      <div class="ov-head">
        <span>当前温度</span>
        <span class="ov-updated">{{ lastAt ? '· 更新于 ' + lastAt : '' }}</span>
      </div>
      <div v-if="overviewCats.length">
        <div v-for="cat in overviewCats" :key="cat.label" class="temp-cat">
          <span class="temp-cat-label">{{ cat.label }}</span>
          <span
            v-for="(c, i) in cat.chips"
            :key="cat.label + i"
            class="ochip"
            :class="{ off: c.tagClass === 'off' }"
            :title="c.title"
          >
            <span class="temp-chip-name">{{ c.name }}</span>
            <span class="temp-chip-val" :style="{ color: c.color }">{{ c.val != null ? c.val + '°C' : '—' }}</span>
            <span v-if="c.tag" class="temp-chip-tag" :class="c.tagClass">{{ c.tag }}</span>
          </span>
        </div>
      </div>
      <p v-else class="sub">等待首次数据…</p>
    </section>

    <footer class="foot">
      nasdash 前端重构 · 阶段 1b 温度监控页（Vue）· 产物由 Vite 构建并内联为单文件 HTML · 访问地址 {{ PAGE_PATH }}
    </footer>
  </div>
</template>

<style scoped>
.wrap {
  max-width: 880px;
  margin: 0 auto;
  padding: 28px 20px 56px;
}
.head {
  display: flex;
  align-items: flex-start;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.page-title {
  font-size: 22px;
  margin: 0 0 4px;
}
.sub {
  color: var(--c-text-2);
  font-size: 13px;
  margin: 0;
}
.theme-btn {
  margin-left: auto;
  background: var(--c-primary-bg);
  color: var(--c-primary);
  border: 1px solid transparent;
  border-radius: 999px;
  padding: 8px 16px;
  font-size: 13px;
  cursor: pointer;
  font-weight: 600;
}
.theme-btn:hover {
  filter: brightness(1.08);
}
.row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.pill {
  border-radius: 999px;
  padding: 3px 12px;
  font-size: 12.5px;
  font-weight: 600;
}
.pill.ok {
  background: rgba(0, 158, 97, 0.14);
  color: var(--c-success);
}
.pill.bad {
  background: rgba(219, 56, 44, 0.14);
  color: var(--c-danger);
}
.pill.idle {
  background: var(--c-primary-bg);
  color: var(--c-primary);
}
.meta {
  color: var(--c-text-2);
  font-size: 12.5px;
}
.mini {
  background: transparent;
  border: 1px solid var(--c-border);
  color: var(--c-text-1);
  border-radius: var(--r-md);
  padding: 4px 10px;
  font-size: 12.5px;
  cursor: pointer;
}
.mini:hover {
  border-color: var(--c-primary);
  color: var(--c-primary);
}
.card {
  background: var(--c-card);
  border: 1px solid var(--c-border);
  border-radius: var(--r-lg);
  box-shadow: var(--c-shadow);
  padding: 18px 20px;
  margin-bottom: 16px;
}
.card h3 {
  font-size: 15px;
  margin: 0 0 12px;
}
/* Hero */
.hero-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.hero-title {
  font-size: 20px;
  margin: 0;
}
.hero-sub {
  color: var(--c-text-2);
  font-size: 13px;
  margin: 4px 0 0;
}
.badge {
  font-size: 12px;
  font-weight: 600;
  padding: 4px 12px;
  border-radius: 999px;
  white-space: nowrap;
}
.badge.ok {
  background: rgba(0, 158, 97, 0.14);
  color: var(--c-success);
}
.badge.bad {
  background: rgba(219, 56, 44, 0.14);
  color: var(--c-danger);
}
.hero-stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(104px, 1fr));
  gap: 8px;
  margin-top: 18px;
}
.hs {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.hs b {
  font-size: 20px;
  font-weight: 700;
}
.hs span {
  font-size: 12px;
  color: var(--c-text-2);
}
/* 温度墙（长名不撑破、居中） */
.temp-wall {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
  gap: 10px;
  margin-top: 6px;
}
.tchip {
  border: 1px solid var(--c-border);
  background: var(--c-bg);
  border-radius: var(--r-md);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  /* 复位：允许长测点名换行收纳，不撑破列宽、文字居中 */
  min-width: 0;
  white-space: normal;
  overflow-wrap: anywhere;
  align-items: center;
  text-align: center;
}
.tchip .tc-name {
  font-size: 12px;
  color: var(--c-text-2);
  width: 100%;
}
.tchip .tc-val {
  font-size: 16px;
  font-weight: 600;
  width: 100%;
}
.best-tag {
  display: inline-block;
  white-space: nowrap;
  word-break: keep-all;
  font-size: 10px;
  color: #fff;
  background: var(--c-success);
  border-radius: 4px;
  padding: 1px 5px;
  margin-left: 4px;
  vertical-align: middle;
}
.note {
  margin-top: 10px;
  font-size: 12px;
  color: var(--c-text-2);
}
/* 当前温度概览 */
.ov-head {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.ov-updated {
  font-size: 11px;
  font-weight: 400;
  color: var(--c-text-2);
  margin-left: auto;
}
.temp-row,
.temp-cat {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: stretch;
}
.temp-cat {
  width: 100%;
  margin-top: 4px;
  padding-top: 6px;
  border-top: 1px dashed var(--c-border);
}
.temp-cat:first-of-type {
  margin-top: 8px;
  padding-top: 0;
  border-top: none;
}
.temp-cat-label {
  font-size: 11px;
  color: var(--c-text-2);
  min-width: 42px;
  font-weight: 600;
  align-self: center;
}
.ochip {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  background: var(--c-bg);
  border: 1px solid var(--c-border);
  border-radius: 6px;
  padding: 5px 9px;
  font-size: 12px;
  line-height: 1.4;
  white-space: nowrap;
  min-height: 34px;
}
.ochip.off {
  opacity: 0.65;
}
.temp-chip-name {
  color: var(--c-text-2);
}
.temp-chip-val {
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  min-width: 38px;
  text-align: right;
}
.temp-chip-tag {
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 6px;
  line-height: 1.3;
}
.temp-chip-tag.off {
  color: var(--c-primary);
  background: var(--c-primary-bg);
}
.temp-chip-tag.on {
  color: var(--c-text-2);
  background: transparent;
  border: 1px solid var(--c-border);
}
.foot {
  text-align: center;
  color: var(--c-text-2);
  font-size: 12px;
  margin-top: 24px;
}
</style>
