<script setup lang="ts">
/**
 * 阶段 2 · 系统资源页（Vue 版，正式入口第一页）
 *
 * 复刻 templates/index.html 的 renderSystem + fetchNetRates 三段式刷新：
 *  - /api/system  首屏 + 每 30s（含 60s 服务端缓存）：内存明细、运行时长、显卡清单
 *  - /api/metrics 每 1s（纯读缓存，实测 0.002~0.08s）：CPU/内存/网络/显卡/磁盘 I/O
 * 数据与老页面同源、同口径：OVS 桥去重、核显不回退显存占用、GPU 双线（温度橙 + 显存蓝）。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import Sparkline from '../components/Sparkline.vue'
import PanelHero from '../components/PanelHero.vue'
import type { HeroStat } from '../components/PanelHero.vue'
import { apiFetch } from '../lib/api'
import { clampPct, fmtLoad, fmtSpeed } from '../lib/format'

interface MemInfo {
  used?: string
  available?: string
  cached?: string
  percent?: number
  total?: string
}
interface GpuItem {
  name?: string
  name_full?: string
  type?: string
  vendor?: string
  temp?: number | null
  mem_pct?: number | null
  mem_used?: number | null
  mem_total?: number | null
  util?: number | null
}
interface SysInfo {
  cpu_usage?: number | null
  cpu_threads?: number
  load?: (string | number)[]
  memory?: MemInfo
  uptime?: string
  nics?: { name?: string }[]
  gpus?: GpuItem[]
}
interface NetItem {
  name?: string
  rx_rate?: number
  tx_rate?: number
}
interface DiskItem {
  device?: string
  brand?: string
  model?: string
  size?: string
  standby?: boolean
  busy?: number
  read_rate?: number
  write_rate?: number
}
interface MetricsResp {
  cpu_usage?: number | null
  mem_percent?: number | null
  load?: (string | number)[]
  net?: NetItem[]
  gpu?: GpuItem[]
  diskio?: DiskItem[]
}

const HIST_LEN = 60

const sys = ref<SysInfo | null>(null)
const live = ref<MetricsResp | null>(null)
const lastUpdate = ref('')
const busy = ref(false)

const cpuHist = ref<number[]>([])
const memHist = ref<number[]>([])
const netHist = ref<{ rx: number; tx: number }[]>([])
const gpuHist = ref<{ temp: (number | null)[]; mem: (number | null)[] }[]>([])
const selectedGpu = ref(0)

let sysTimer = 0
let metricTimer = 0
let sysBusy = false
let metricBusy = false

// ===== 取值口径（与老页面完全一致）=====
/** 过滤虚拟/重复网口：去掉 ovs-system，且 X 与 X-ovs 同时存在时只留 X-ovs */
function pickNet(src: NetItem[]): NetItem[] {
  const ovsBridges = new Set(
    src.filter(n => /[-.]ovs$/.test(n.name || '')).map(n => (n.name || '').replace(/[-.]ovs$/, '')),
  )
  return src.filter(
    n =>
      n.name !== 'ovs-system' &&
      !ovsBridges.has(n.name || '') &&
      !((n.rx_rate || 0) === 0 && (n.tx_rate || 0) === 0),
  )
}

function primaryGpu(list: GpuItem[]): GpuItem | null {
  if (!list.length) return null
  return list.find(g => g.vendor === '10de' || g.vendor === '1002') || list[0]
}

// ===== 派生值 =====
const netRows = computed<NetItem[]>(() => pickNet(live.value?.net || []))
const netRx = computed(() => netRows.value.reduce((a, n) => a + (n.rx_rate || 0), 0))
const netTx = computed(() => netRows.value.reduce((a, n) => a + (n.tx_rate || 0), 0))
const rxTxt = computed(() => (netRows.value.length ? fmtSpeed(netRx.value) : '—'))
const txTxt = computed(() => (netRows.value.length ? fmtSpeed(netTx.value) : '—'))

const cpuPct = computed(() => {
  const l = live.value
  if (l?.cpu_usage != null) return Math.round(parseFloat(String(l.cpu_usage)))
  const s = sys.value
  if (s?.cpu_usage != null) return Math.round(parseFloat(String(s.cpu_usage)))
  return null
})

const memPct = computed(() => {
  const l = live.value
  if (l?.mem_percent != null) return Math.round(parseFloat(String(l.mem_percent)))
  const p = sys.value?.memory?.percent
  return p != null ? Math.round(p) : null
})

const memInfo = computed<MemInfo>(() => sys.value?.memory || {})

/** 主显卡实时占用率：优先真实 util；核显不回退显存占用（那是整机内存占用率，口径不符） */
const gpuPct = computed(() => {
  const g = primaryGpu(live.value?.gpu || [])
  if (!g) return null
  if (g.util != null) return Math.round(parseFloat(String(g.util)))
  if ((g.vendor === '10de' || g.vendor === '1002') && g.mem_pct != null) {
    return Math.round(parseFloat(String(g.mem_pct)))
  }
  return null
})

const gpuList = computed<GpuItem[]>(() => {
  const liveG = live.value?.gpu || []
  const sysG = sys.value?.gpus || []
  return liveG.length ? liveG : sysG
})
const gpuName = computed(() => {
  const g = gpuList.value[selectedGpu.value] || gpuList.value[0]
  return g?.name || g?.type || '显卡'
})
const gpuNameFull = computed(() => {
  const g = gpuList.value[selectedGpu.value] || gpuList.value[0]
  return g?.name_full || g?.name || ''
})
const gpuLegend = computed(() => gpuList.value.length > 1)

const diskRows = computed<DiskItem[]>(() => live.value?.diskio || [])

const heroStats = computed<HeroStat[]>(() => [
  { v: cpuPct.value != null ? String(cpuPct.value) : '—', unit: '%', k: 'CPU 使用率' },
  {
    v: gpuPct.value != null ? String(gpuPct.value) : '—',
    unit: '%',
    k: 'GPU 占用',
    tip: '显卡实时使用率（优先独显，无独显时显示核显）',
  },
  {
    k: '网络',
    cls: 'net',
    html:
      '<div style="display:inline-grid;grid-template-columns:14px auto;gap:2px 6px;align-items:baseline;font-size:22px;font-weight:600;line-height:1.3">' +
      '<span style="color:var(--blue)">↑</span><span style="color:var(--blue)">' +
      txTxt.value +
      '</span>' +
      '<span style="color:var(--green)">↓</span><span style="color:var(--green)">' +
      rxTxt.value +
      '</span></div>',
  },
  { v: memPct.value != null ? String(memPct.value) : '—', unit: '%', k: '内存占用' },
  { v: sys.value?.uptime ? String(sys.value.uptime).replace(/\s+/g, '') : '—', k: '已运行' },
])

const loadText = computed(() => {
  const arr = live.value?.load || sys.value?.load || []
  return arr.map(fmtLoad).join(' / ') || '—'
})

// ===== 折线数据 =====
const cpuLines = computed(() => [{ data: cpuHist.value as (number | null)[], color: 'var(--blue)' }])
const memLines = computed(() => [{ data: memHist.value as (number | null)[], color: '#a855f7' }])
const netLines = computed(() => [
  { data: netHist.value.map(p => p.tx), color: 'var(--blue)' },
  { data: netHist.value.map(p => p.rx), color: 'var(--green)' },
])
const gpuLines = computed(() => {
  const h = gpuHist.value[selectedGpu.value]
  if (!h) return [{ data: [] as (number | null)[], color: 'var(--orange)' }]
  return [
    { data: h.mem, color: 'var(--blue)' },
    { data: h.temp, color: 'var(--orange)' },
  ]
})

// ===== 拉取 =====
async function fetchSys(force = false): Promise<void> {
  if (sysBusy && !force) return
  sysBusy = true
  if (force) busy.value = true
  try {
    const r = await apiFetch('/api/system', 60000)
    if (!r.ok) return
    const j = (await r.json()) as { system?: SysInfo } & SysInfo
    sys.value = j.system || j
  } catch {
    /* 网络抖动忽略，下一轮再试 */
  } finally {
    sysBusy = false
    busy.value = false
  }
}

async function fetchMetrics(): Promise<void> {
  if (metricBusy) return
  metricBusy = true
  try {
    const r = await apiFetch('/api/metrics', 30000)
    if (!r.ok) return
    const d = (await r.json()) as MetricsResp
    live.value = d

    const c = clampPct(d.cpu_usage)
    if (c != null) {
      cpuHist.value.push(c)
      if (cpuHist.value.length > HIST_LEN) cpuHist.value.shift()
    }
    const m = clampPct(d.mem_percent)
    if (m != null) {
      memHist.value.push(m)
      if (memHist.value.length > HIST_LEN) memHist.value.shift()
    }
    if (Array.isArray(d.gpu)) {
      d.gpu.forEach((g, i) => {
        if (!gpuHist.value[i]) gpuHist.value[i] = { temp: [], mem: [] }
        const t = g.temp != null ? Math.min(120, Math.max(0, parseFloat(String(g.temp)) || 0)) : null
        gpuHist.value[i].temp.push(t)
        if (gpuHist.value[i].temp.length > HIST_LEN) gpuHist.value[i].temp.shift()
        const mp =
          g.mem_total != null && g.mem_total > 0
            ? g.mem_pct != null
              ? g.mem_pct
              : Math.round(((g.mem_used || 0) / g.mem_total) * 100)
            : null
        gpuHist.value[i].mem.push(mp)
        if (gpuHist.value[i].mem.length > HIST_LEN) gpuHist.value[i].mem.shift()
      })
    }
    const rows = pickNet(d.net || [])
    netHist.value.push({
      rx: rows.reduce((a, n) => a + (n.rx_rate || 0), 0),
      tx: rows.reduce((a, n) => a + (n.tx_rate || 0), 0),
    })
    if (netHist.value.length > HIST_LEN) netHist.value.shift()

    lastUpdate.value = '更新于 ' + new Date().toLocaleTimeString('zh-CN')
  } catch {
    /* 忽略 */
  } finally {
    metricBusy = false
  }
}

async function refreshAll(): Promise<void> {
  await Promise.all([fetchSys(true), fetchMetrics()])
}

onMounted(() => {
  void fetchSys(true)
  void fetchMetrics()
  metricTimer = window.setInterval(() => void fetchMetrics(), 1000)
  sysTimer = window.setInterval(() => void fetchSys(), 30000)
})
onUnmounted(() => {
  if (metricTimer) window.clearInterval(metricTimer)
  if (sysTimer) window.clearInterval(sysTimer)
})
</script>

<template>
  <div>
    <PanelHero
      icon="system"
      title="系统资源"
      sub="CPU / 内存 / 网络实时状态"
      :stats="heroStats"
      :last-update="lastUpdate"
      :busy="busy"
      @refresh="refreshAll"
    />

    <div class="sys-grid">
      <div class="card sys-area-cpu">
        <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 6px; justify-content: space-between">
          <h3 style="margin: 0" title="CPU 实时使用率（飞牛系统资源页风格）">CPU</h3>
          <span :style="{ fontSize: '30px', fontWeight: 700, color: 'var(--blue)', lineHeight: 1 }">
            {{ cpuPct != null ? cpuPct + '%' : '—' }}
          </span>
        </div>
        <div style="position: relative">
          <Sparkline :lines="cpuLines" :max="100" :w="600" :h="100" :css-h="80" :line-width="2" />
        </div>
      </div>

      <div class="card sys-area-mem">
        <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 6px; justify-content: space-between">
          <h3 style="margin: 0" title="内存实时使用率（飞牛系统资源页风格）">内存</h3>
          <span :style="{ fontSize: '30px', fontWeight: 700, color: '#a855f7', lineHeight: 1 }">
            {{ memPct != null ? memPct + '%' : '—' }}
          </span>
        </div>
        <div style="position: relative">
          <Sparkline :lines="memLines" :max="100" :w="600" :h="100" :css-h="80" :line-width="2" />
        </div>
        <div style="margin-top: 8px; font-size: 12px">
          <div class="kv"><span class="k">应用占用</span><span class="v" style="font-weight: 600">{{ memInfo.used || '—' }}</span></div>
          <div class="kv">
            <span
              class="k"
              title="可用 = 缓存 + 真空闲。缓存是系统把读过的文件留在内存里备用，程序需要时会自动腾出来，不用手动清理"
            >
              可用（含缓存 {{ memInfo.cached || '—' }}，可自动回收）
            </span>
            <span class="v" style="font-weight: 600">{{ memInfo.available || '—' }}</span>
          </div>
        </div>
      </div>

      <div class="card sys-area-disk">
        <h3 title="磁盘的读写速度，数字越大说明盘越忙">磁盘 I/O</h3>
        <template v-if="diskRows.length">
          <div v-for="(d, i) in diskRows" :key="i" class="kv">
            <span class="k">
              {{ d.device }}<span v-if="d.standby" class="tag-sleep">待机</span>
              <template v-if="[d.brand, d.model, d.size].filter(Boolean).join(' · ')">
                <br /><small style="color: var(--muted)">{{ [d.brand, d.model, d.size].filter(Boolean).join(' · ') }}</small>
              </template>
            </span>
            <span class="v" style="font-size: 12px">
              读{{ fmtSpeed(d.read_rate) }} / 写{{ fmtSpeed(d.write_rate) }}
              <span v-if="typeof d.busy === 'number'" class="disk-busy">占用 {{ d.busy }}%</span>
            </span>
          </div>
        </template>
        <div v-else class="loading">采样中…</div>
      </div>

      <div class="card sys-area-netlive">
        <div style="display: flex; justify-content: space-between; align-items: baseline; margin: 0 0 6px; gap: 8px">
          <h3 style="margin: 0; color: var(--text-1)" title="实时上下行速率（合并全部网口）">网络</h3>
          <div class="gpu-legend">
            <span class="gpu-legend-item">
              <i style="width: 10px; height: 3px; border-radius: 2px; display: inline-block; background: var(--blue)" />↑ 上传
            </span>
            <span class="gpu-legend-item">
              <i style="width: 10px; height: 3px; border-radius: 2px; display: inline-block; background: var(--green)" />↓ 下载
            </span>
          </div>
        </div>
        <div style="position: relative">
          <Sparkline :lines="netLines" max="auto" :w="800" :h="120" :css-h="80" />
        </div>
        <div style="display: flex; justify-content: space-between; font-size: 13px; font-weight: 600; margin-top: 4px">
          <span style="color: var(--blue)">↑ {{ txTxt }}</span>
          <span style="color: var(--green)">↓ {{ rxTxt }}</span>
        </div>
      </div>

      <div class="card sys-area-gpu">
        <div class="gpu-head-row">
          <h3>显卡</h3>
          <select v-if="gpuLegend" class="gpu-select" :value="selectedGpu" @change="selectedGpu = Number(($event.target as HTMLSelectElement).value)">
            <option v-for="(g, i) in gpuList" :key="i" :value="i">
              {{ g.name || g.type || '显卡' }}{{ g.type ? ' · ' + g.type : '' }}
            </option>
          </select>
        </div>
        <div v-if="gpuList.length" class="gpu-block">
          <div class="gpu-name" :title="gpuNameFull">{{ gpuName }}</div>
          <div class="gpu-legend">
            <span class="gpu-legend-item"><i class="gpu-dot temp" />温度</span>
            <span class="gpu-legend-item"><i class="gpu-dot mem" />显存</span>
          </div>
          <div class="gpu-combo"><Sparkline :lines="gpuLines" :max="100" :w="600" :h="90" /></div>
        </div>
        <div v-else class="loading">未检测到核显 / 独显，或 BIOS 已禁用</div>
      </div>
    </div>

    <div style="margin-top: 12px; font-size: 12px; color: var(--text-2)">
      负载 {{ loadText }}（共 {{ sys?.cpu_threads ?? '—' }} 线程）
    </div>
  </div>
</template>
