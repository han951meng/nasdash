<script setup lang="ts">
/**
 * 温度监控页（Vue 版，阶段 1b 产出并入阶段 2 正式入口）
 *
 * 复刻 templates/index.html 的 renderTemps + 当前温度概览：
 *  - 温度墙：主板测点 + 阵列卡芯片温度 + 显卡温度，长设备名不撑破格子、文字居中
 *  - ★最准 徽标：仅 "CPU 封装温度" 标，悬停解释"CPU 内部传感器直读，最准确"
 *  - 独显/核显分支：采信后端 type（核显/独显），后端无有效 type 才回退按厂商编号判断
 *  - 每 5 秒轮询 /api/fan/temps（统一温度快照）
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import PanelHero from '../components/PanelHero.vue'
import type { HeroStat } from '../components/PanelHero.vue'
import { apiFetch } from '../lib/api'
import { tempColor } from '../lib/format'
import { pageCacheGet, pageCacheSet } from '../lib/pageCache'

// ===== 类型 =====
interface SensorItem {
  name: string
  value: number
  raw?: string
  max?: number | null
  crit?: number | null
}
interface GpuItem {
  name?: string
  vendor?: string
  type?: string
  temp?: number | null
}
interface DiskItem {
  dev: string
  name?: string
  category?: string
  is_system?: boolean
  temp?: number | null
  asleep?: boolean
  no_sleep?: boolean
  is_nvme?: boolean
  nvme_start_temp?: number
  intf?: string
}
/** 阵列卡物理盘（RAID5 等虚拟盘场景下物理盘不暴露 /dev，温度只能从阵列卡 storcli 拿） */
interface RaidDriveItem {
  slot?: string | number
  model?: string
  serial?: string
  temp?: number | null
  intf?: string
  size?: string
}
interface TempsResp {
  cpu_temp?: number | null
  mb_temp?: number | null
  disks?: DiskItem[]
  raid_drives?: RaidDriveItem[]
  sensors?: SensorItem[]
  raid_temp?: number | null
  raid_controller_temp?: number | null
  gpus?: GpuItem[]
}
interface WallEntry {
  name: string
  raw?: string
  value: number
  max?: number | null
  crit?: number | null
}

// 不直观 / 重复测点（与主页面 EXCLUDE_TEMPS 一致）
const EXCLUDE_TEMPS = new Set([
  'CPU PSS',
  'CPU VRM',
  'PECI Agent 0 Calibration',
  '内存温度 1',
  '内存温度 2',
  '复合温度',
])

const data = ref<TempsResp | null>(null)
const lastUpdate = ref('')
const busy = ref(false)
// 硬盘温度缓存：读不出(temp=null)但前一刻读到时沿用上次值，避免数字反复消失（与主页面同意图）
const lastDiskTemps = ref<Record<string, number>>({})
let pollTimer = 0

// ===== 温度墙 =====
const wallEntries = computed<WallEntry[]>(() => {
  const d = data.value
  if (!d) return []
  const sens = (d.sensors || [])
    .filter(t => typeof t.value === 'number' && t.value > 0 && t.value < 150 && !EXCLUDE_TEMPS.has(t.name))
    .map(t => ({ name: t.name, raw: t.raw, value: t.value as number, max: t.max, crit: t.crit }))
  const raidEntry: WallEntry[] = []
  if (typeof d.raid_temp === 'number')
    raidEntry.push({ name: '阵列卡芯片温度 (ROC)', raw: 'ROC temperature', value: d.raid_temp, max: 80, crit: 90 })
  if (typeof d.raid_controller_temp === 'number')
    raidEntry.push({ name: '阵列卡控制器温度', raw: 'Controller Temperature', value: d.raid_controller_temp, max: 80, crit: 90 })
  const gpusSrc = d.gpus || []
  const gpuEntries = gpusSrc
    .map(g => {
      const t = String(g.type || '').trim()
      const isIgpu = t === '核显' ? true : t === '独显' ? false : g.vendor !== '10de' && g.vendor !== '1002'
      const nm = g.name || '显卡'
      const label = (isIgpu ? '核显' : '独显') + (gpusSrc.length > 1 ? ' ' + nm : '')
      return { name: label, raw: nm + ' GPU 温度', value: g.temp as number, max: 95, crit: 100 }
    })
    .filter(e => typeof e.value === 'number' && e.value > 0 && e.value < 150)
  return [...sens, ...raidEntry, ...gpuEntries]
})

const isBest = (name: string): boolean => name === 'CPU 封装温度'

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
  if (!wallEntries.value.length) return '等待数据'
  if (s.crit) return `${s.crit} 个临界`
  if (s.warn) return `${s.warn} 个偏高`
  return '全部正常'
})
const statusOk = computed(() => !summary.value.crit && !summary.value.warn)

const heroStats = computed<HeroStat[]>(() => {
  const arr: HeroStat[] = [
    { v: data.value?.cpu_temp != null ? String(data.value.cpu_temp) : '—', unit: '°C', k: 'CPU 温度' },
    { v: data.value?.raid_temp != null ? String(data.value.raid_temp) : '—', unit: '°C', k: '阵列卡(芯片)' },
    { v: summary.value.max != null ? String(summary.value.max) : '—', unit: '°C', k: '最高温度' },
    { v: summary.value.avg != null ? String(summary.value.avg) : '—', unit: '°C', k: '平均温度' },
  ]
  // 控制器温度：阵列卡不支持该传感器（如 LSI 9271 单温度卡）时为 None，整块隐藏，不占位
  if (data.value?.raid_controller_temp != null) {
    arr.splice(2, 0, { v: String(data.value.raid_controller_temp), unit: '°C', k: '阵列卡(控制器)' })
  }
  return arr
})

// ===== 当前温度概览（CPU/主板 + 硬盘分 NVMe/SSD/HDD）=====
interface OvChip {
  name: string
  val: number | null
  color: string
  /** 名字前的身份徽标（如 SAS），与右侧状态 tag（常驻/休眠）分开 */
  badge: string
  tag: string
  tagClass: string
  /** 整卡压暗（只给「休眠」盘用；NVMe 被动散热只灰标签、不暗整卡，与旧页口径一致） */
  dim: boolean
  title: string
}
interface OvCat {
  label: string
  chips: OvChip[]
}
const overviewCats = computed<OvCat[]>(() => {
  const d = data.value
  if (!d) return []
  const out: OvCat[] = []
  const cpu = d.cpu_temp ?? null
  const mb = d.mb_temp ?? null
  out.push({
    label: '核心',
    chips: [
      { name: 'CPU', val: cpu, color: tempColor(cpu, 100), badge: '', tag: '', tagClass: '', dim: false, title: '' },
      { name: '主板', val: mb, color: tempColor(mb, 90), badge: '', tag: '', tagClass: '', dim: false, title: '' },
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
        let dim = false
        let title = ''
        if (x.asleep) {
          tag = '休眠'
          tagClass = 'off'
          dim = true
        } else if (isNv) {
          // 只让「被动散热」小标签变灰，整卡保持正常亮度（之前整卡 opacity 0.65，
          // 看起来像这块盘数据不可信，与 CPU/SSD 等正常盘字色不一致）
          tag = '被动散热'
          tagClass = 'off'
          title =
            'M.2 固态多为被动散热（自带散热片、贴在主板上），机箱风扇的气流基本吹不到它，而且 ' +
            (x.nvme_start_temp ?? 65) +
            '°C 以下对固态属于完全正常的工作温度。\n' +
            '因此它不会去催风扇转、也不会阻止风扇停转；只有超过 ' +
            (x.nvme_start_temp ?? 65) +
            '°C（接近降频保护线）才会参与风扇温控。'
        } else if (x.no_sleep) {
          tag = '常驻'
          tagClass = 'on'
        }
        return {
          name: x.name || (x.dev || '').replace(/^\/dev\//, ''),
          val: v,
          color: tempColor(v, trip),
          badge: (x.intf || '').toUpperCase().includes('SAS') ? 'SAS' : '',
          tag,
          tagClass,
          dim,
          title,
        }
      })
    out.push({ label: c.label, chips })
  })
  // 阵列卡物理盘：RAID5 等虚拟盘场景下物理盘不暴露 /dev、上面的 OS 盘列表看不到，
  // 温度由阵列卡(storcli)上报，单独成组展示（后端已按序列号与 OS 盘去重，不会重复）。
  const rd = d.raid_drives || []
  if (rd.length) {
    const chips: OvChip[] = rd.map(x => {
      const v = x.temp ?? null
      let nm = (x.model || '').trim()
      if (x.slot != null && x.slot !== '') nm = (nm ? nm + ' ' : '') + '槽位' + x.slot
      return {
        name: nm || '阵列卡硬盘',
        val: v,
        color: tempColor(v, 60),
        badge: '',
        tag: (x.intf || '').toUpperCase(),
        tagClass: (x.intf || '').toUpperCase().includes('SAS') ? 'on' : 'off',
        dim: false,
        title: '这块硬盘挂在阵列卡下（RAID 虚拟盘成员，系统里看不到独立盘符），温度由阵列卡上报。',
      }
    })
    out.push({ label: '阵列卡硬盘', chips })
  }
  return out
})

// ===== 拉取 =====
async function fetchTemps(): Promise<void> {
  // 切页签回来先上缓存秒开，再拉最新（缓存只补首次空屏，之后照常轮询）
  if (!data.value) {
    const cached = pageCacheGet<TempsResp>('temps')
    if (cached) data.value = cached
  }
  // 有内容就不转圈：后台静默拉最新，避免每次切页签都闪一下
  if (!data.value) busy.value = true
  try {
    const res = await apiFetch('/api/fan/temps', 30000)
    if (!res.ok) return
    const j = (await res.json()) as TempsResp
    ;(j.disks || []).forEach(x => {
      if (x.temp != null) lastDiskTemps.value[x.dev] = x.temp
    })
    data.value = j
    pageCacheSet('temps', j)
    lastUpdate.value = '更新于 ' + new Date().toLocaleTimeString('zh-CN')
  } catch {
    /* 忽略，下一轮再试 */
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  void fetchTemps()
  pollTimer = window.setInterval(() => void fetchTemps(), 5000)
})
onUnmounted(() => {
  if (pollTimer) window.clearInterval(pollTimer)
})
</script>

<template>
  <div>
    <PanelHero
      icon="thermo"
      title="温度监控"
      sub="主板 / CPU / 芯片组 / 阵列卡 温度与阈值"
      :stats="heroStats"
      :badge="{ ok: statusOk, text: statusText }"
      :last-update="lastUpdate"
      :busy="busy"
      @refresh="fetchTemps"
    />

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
      <p v-else class="sub">未检测到温度测点（或数据加载中…）</p>
      <div class="note">
        主板传感器测点 + 阵列卡芯片温度 + 显卡温度全览（已过滤无效 0°C / 负温）；鼠标悬停可查看原始英文名。
        颜色：<span style="color: var(--green)">绿正常</span> ·
        <span style="color: var(--orange)">橙偏高</span> ·
        <span style="color: var(--red)">红危险</span>
      </div>
    </section>

    <section class="card temp-overview">
      <div class="ov-head">
        <span>当前温度</span>
        <span class="ov-updated">{{ lastUpdate ? '· ' + lastUpdate : '' }}</span>
      </div>
      <div v-if="overviewCats.length">
        <div v-for="cat in overviewCats" :key="cat.label" class="temp-cat">
          <span class="temp-cat-label">{{ cat.label }}</span>
          <span
            v-for="(c, i) in cat.chips"
            :key="cat.label + i"
            class="ochip"
            :class="{ off: c.dim }"
            :title="c.title"
          >
            <span v-if="c.badge" class="temp-chip-badge">{{ c.badge }}</span>
            <span class="temp-chip-name">{{ c.name }}</span>
            <span class="temp-chip-val" :style="{ color: c.color }">{{ c.val != null ? c.val + '°C' : '—' }}</span>
            <span v-if="c.tag" class="temp-chip-tag" :class="c.tagClass">{{ c.tag }}</span>
          </span>
        </div>
      </div>
      <p v-else class="sub">等待首次数据…</p>
    </section>
  </div>
</template>

<style scoped>
.sub {
  color: var(--c-text-2);
  font-size: 13px;
  margin: 0;
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
.temp-cat {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: stretch;
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
.temp-chip-badge {
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 6px;
  line-height: 1.3;
  font-weight: 600;
  color: var(--c-primary);
  background: var(--c-primary-bg);
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
</style>
