<script setup lang="ts">
/**
 * 硬盘 SMART（disks）—— 阶段 3 第 8 页
 *
 * 复刻旧页 renderDisks / renderDiskTest / diskInfoCard 及相关自检逻辑：
 *   · 每块盘的 SMART 卡（健康徽章 / 型号 / 容量类型 / 转速 / 序列号 / 温度条
 *     + 按介质分三套细节：SAS / NVMe / ATA）
 *   · 硬盘自检（A/B/C 三档）：
 *       long      SMART 长自检（固件级，只读，所有盘可跑）
 *       surface   只读表面扫描（只读逐扇区，所有盘可跑，带扇区网格）
 *       badblocks 全面坏块扫描（破坏性，仅独立盘可跑）
 *   · 自检进行中：进度条 / 已运行·预计剩余 / 扇区网格 / 最小化 / 中止
 *   · 自检记录弹窗（/api/disks/selftest/history）
 *
 * 写操作（POST，均 @require_admin）：/api/disks/selftest、/api/disks/selftest/abort
 *  以及磁盘卡上的「定位闪灯」→ /api/raid/locate（与硬件配置检测页同一接口）。
 */
import { ref, computed, onMounted, onUnmounted } from 'vue'
import PanelHero from '../components/PanelHero.vue'
import AppIcon from '../components/AppIcon.vue'
import { apiFetch } from '../lib/api'
import { tempColor } from '../lib/format'

interface Disk {
  dev: string
  type?: string
  health?: string
  health_ok?: boolean
  asleep?: boolean
  temp?: number | null
  temp_trip?: number
  power_on_hours?: number | null
  // SAS
  defects?: number
  pending?: number
  non_medium_errors?: number
  read_errors?: number
  write_errors?: number
  // NVMe
  percentage_used?: number | null
  available_spare?: number | null
  critical_warning?: string
  data_units_read?: string
  data_units_written?: string
  // ATA
  reallocated?: number | null
  uncorrectable?: number | null
  udma_crc?: number
  // 通用
  model?: string
  brand?: string
  vendor?: string
  size?: string
  rota?: string
  feature?: string
  dual_actuator?: boolean
  rpm?: string
  serial?: string
  locate_supported?: boolean
  slot?: string
  standalone?: boolean
}

interface Job {
  state: 'running' | 'done' | 'error' | 'aborted' | string
  type?: string
  progress?: number | null
  started_at?: number
  elapsed?: number
  message?: string
  eta_total_hint?: number
  total_bytes?: number
  blocksize?: number
  bad_blocks?: number[]
  result?: { read_errors?: string | number; write_errors?: string | number; corrected?: string | number }
}

interface HistRow {
  dev?: string
  type?: string
  state?: string
  bad_blocks?: number
  bad_lbas?: number[]
  elapsed?: number
  total_bytes?: number
  message?: string
  finished_at?: number
  smart_before?: Record<string, string | number>
  smart_after?: Record<string, string | number>
  result?: { read_errors?: string | number; write_errors?: string | number; corrected?: string | number }
}

const disks = ref<Disk[]>([])
const jobs = ref<Record<string, Job>>({})
const busy = ref(false)
const error = ref('')
const lastUpdate = ref('')

const minMap = ref<Record<string, boolean>>({})
const stateMsg = ref<Record<string, string>>({})
const stateErr = ref<Record<string, boolean>>({})
const locateOn = ref<Record<string, boolean>>({})
const locateBusy = ref<Record<string, boolean>>({})

// 1 秒计时用的「当前时刻」
const nowSec = ref(Math.floor(Date.now() / 1000))

let tickTimer: number | null = null
let pollTimer: number | null = null
let disposed = false

/* ---------- 自检方式选择弹窗（旧页 appPick） ---------- */
const pickOpen = ref(false)
const pickDev = ref('')
const pickModel = ref('')
const pickStandalone = ref(false)
const pickOptions = computed(() => [
  {
    type: 'long',
    label: 'SMART 长自检（固件级）',
    desc: '由硬盘固件自检盘体逻辑健康，只读不伤数据，所有盘都能跑',
    disabled: false,
    note: '',
  },
  {
    type: 'surface',
    label: '只读表面扫描',
    desc: '逐扇区读盘面找坏块，不写数据不伤盘，所有盘（含在用盘/阵列成员）都能跑，扫完看扇区网格',
    disabled: false,
    note: '',
  },
  {
    type: 'badblocks',
    label: '全面坏块扫描（会清空数据）',
    desc: '写满整盘再读回校验，最彻底，但会清空盘上所有数据',
    disabled: !pickStandalone.value,
    note: pickStandalone.value ? '' : '该盘不是独立盘（在用/阵列成员），不能做清空式扫描',
  },
])

/* ---------- 二次确认弹窗（旧页 appConfirm） ---------- */
const confirmOpen = ref(false)
const confirmTitle = ref('')
const confirmBody = ref('')
const confirmOk = ref('确定')
const confirmCancel = ref('取消')
const confirmDanger = ref(false)
let confirmResolve: ((v: boolean) => void) | null = null

/* ---------- 自检记录弹窗 ---------- */
const histOpen = ref(false)
const histDev = ref('')
const histLoading = ref(false)
const histRows = ref<HistRow[]>([])
const histErr = ref('')

/* ================= 工具函数（与旧页同口径） ================= */
function fmtHours(h?: number | null): string {
  if (h == null) return '-'
  if (h >= 8760) return (h / 8760).toFixed(1) + ' 年'
  return h + ' 小时'
}

function fmtSec(s?: number | null): string {
  if (s == null) return '-'
  const n = Math.round(s)
  if (n < 60) return n + '秒'
  let m = Math.floor(n / 60)
  const h = Math.floor(m / 60)
  m = m % 60
  if (h) return h + '小时' + m + '分'
  return m + '分' + (n % 60) + '秒'
}

function fmtBytes(b?: number): string {
  if (!b) return '0'
  const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let i = 0
  let x = b
  while (x >= 1024 && i < u.length - 1) { x /= 1024; i++ }
  return (x >= 100 ? Math.round(x) : Math.round(x * 10) / 10) + u[i]
}

/** 双磁臂特征串清理：双磁臂已单独标注时不再重复显示 */
function diskFeatureClean(feat?: string, isDualActuator?: boolean): string {
  if (!feat) return ''
  if (isDualActuator) return ''
  return String(feat).replace(/双磁臂?\(双执行器\)|双执行器/g, '').trim()
}

function jobTypeName(t?: string): string {
  return t === 'surface' ? '只读表面扫描' : (t === 'badblocks' ? '坏块慢扫' : 'SMART 长自检')
}

/** NVMe 临界告警十六进制是否非零 */
function cwBad(d: Disk): boolean {
  return (parseInt(d.critical_warning || '0', 16) || 0) > 0
}

/* ================= Hero ================= */
const heroStats = computed(() => {
  const total = disks.value.length
  const healthy = disks.value.filter(d => d.health_ok).length
  const nvme = disks.value.filter(d => (d.type || '').toLowerCase() === 'nvme').length
  let maxT: number | null = null
  disks.value.forEach(d => {
    if (d.temp != null) maxT = maxT == null ? d.temp : Math.max(maxT, d.temp)
  })
  if (!total) {
    return [
      { v: '0', k: '硬盘总数' },
      { v: '0/0', k: '健康' },
      { v: '0', k: 'NVMe' },
      { v: '—', k: '最高温度' },
    ]
  }
  return [
    { v: String(total), k: '硬盘总数' },
    { html: healthy + '<small>/' + total + '</small>', k: '健康', cls: healthy === total ? '' : 'warn' },
    { v: String(nvme), k: 'NVMe' },
    { v: maxT != null ? maxT + '°C' : '—', k: '最高温度' },
  ]
})

const badDisks = computed(() => disks.value.filter(d => d.health_ok === false))

/* ================= 数据加载 ================= */
async function loadDisks(force = false): Promise<void> {
  if (disposed) return
  busy.value = true
  try {
    const r = await apiFetch('/api/disks' + (force ? '?force=1' : ''), 30000)
    const j = await r.json()
    if (j.error) {
      error.value = '接口错误：' + j.error
    } else {
      error.value = ''
      disks.value = j.disks || []
      lastUpdate.value = j.time || ''
    }
  } catch (e) {
    error.value = '获取硬盘数据失败：' + String((e as Error).message || e)
  } finally {
    busy.value = false
  }
}

/* ================= 自检状态轮询 ================= */
const anyRunning = computed(() => Object.values(jobs.value).some(j => j.state === 'running'))

function stopTick(): void {
  if (tickTimer != null) { window.clearInterval(tickTimer); tickTimer = null }
}

function stopPoll(): void {
  if (pollTimer != null) { window.clearTimeout(pollTimer); pollTimer = null }
}

function tick(): void {
  nowSec.value = Math.floor(Date.now() / 1000)
}

async function pollDiskTests(): Promise<void> {
  if (disposed) return
  try {
    const r = await apiFetch('/api/disks/selftest?_=' + Date.now(), 20000)
    const j = await r.json()
    if (j.ok) jobs.value = j.jobs || {}
  } catch {
    /* 轮询失败静默，下一轮再试 */
  }
  const running = Object.values(jobs.value).some(j => j.state === 'running')
  if (running && tickTimer == null) {
    tick()
    tickTimer = window.setInterval(tick, 1000)
  }
  if (!running) stopTick()
  stopPoll()
  if (running) pollTimer = window.setTimeout(pollDiskTests, 3000)
  if (disposed) { stopTick(); stopPoll() }
}

/* ================= 每盘：自检面板计算 ================= */
function elapsedOf(dev: string): number {
  const job = jobs.value[dev]
  if (!job) return 0
  return job.started_at ? Math.max(0, nowSec.value - job.started_at) : (job.elapsed || 0)
}

function pctOf(dev: string): number {
  const job = jobs.value[dev]
  if (!job || job.progress == null) return 0
  return Math.min(100, Math.round(job.progress))
}

/** 预计剩余（有进度时按比例推算；否则提示「计算中…」） */
function etaOf(dev: string): { remain: string; hint: string } {
  const job = jobs.value[dev]
  if (!job || !job.started_at) return { remain: '', hint: '' }
  const el = elapsedOf(dev)
  if (job.progress != null && job.progress > 0) {
    const total = el * 100 / job.progress
    return { remain: ' · 预计剩余 ' + fmtSec(Math.max(0, total - el)) + ' / 总用时约 ' + fmtSec(total), hint: '' }
  }
  const h = job.eta_total_hint ? '参考总用时约 ' + fmtSec(job.eta_total_hint) : '刚开始，进度更新较慢'
  return { remain: ' · 预计剩余 计算中…', hint: '（' + h + '）' }
}

function isGridJob(dev: string): boolean {
  const t = jobs.value[dev]?.type
  return t === 'badblocks' || t === 'surface'
}

/** 扇区网格 640 格：灰=未扫 / 绿=正常 / 红=坏块 / 黄=当前扫描位置 */
function gridCells(dev: string): string[] {
  const job = jobs.value[dev]
  const n = 640
  if (!job) return new Array(n).fill('sg-cell')
  const total = job.total_bytes || 0
  const bs = job.blocksize || 4096
  const pct = job.progress || 0
  const green = total > 0 ? Math.floor(pct / 100 * n) : 0
  const bad: Record<number, boolean> = {}
  if (total > 0 && job.bad_blocks && job.bad_blocks.length) {
    const spanB = total / n
    for (const lba of job.bad_blocks) {
      const idx = Math.floor(lba * bs / spanB)
      if (idx >= 0 && idx < n) bad[idx] = true
    }
  }
  const out: string[] = []
  for (let c = 0; c < n; c++) {
    let cls = 'sg-cell'
    if (bad[c]) cls += ' bad'
    else if (c < green) cls += ' ok'
    else if (c === green) cls += ' cur'
    out.push(cls)
  }
  return out
}

/* ================= 自检动作（写操作） ================= */
async function startDiskTest(dev: string, type: string, model: string): Promise<void> {
  const m = model || '未知型号'
  if (type === 'badblocks') {
    const ok = await appConfirm({
      title: '确认对 ' + m + ' 执行 B 档坏块慢扫？',
      body: '目标磁盘：' + m + '（/dev/' + dev + '）\n\nB 类坏块慢扫会对该盘执行读写覆盖测试，这会擦除盘上所有数据，且耗时很长。\n请再次确认该盘为独立盘（未挂载、非 RAID/LVM 成员）。',
      danger: true,
      ok: '确定执行',
      cancel: '取消',
    })
    if (!ok) return
  }
  if (type === 'surface') {
    const ok = await appConfirm({
      title: '确认对 ' + m + ' 执行 C 档只读表面扫描？',
      body: '目标磁盘：' + m + '（/dev/' + dev + '）\n\nC 档是只读表面扫描：只读取盘面数据判断能否正常读出，不写入任何数据、不会伤盘，可用于正在用的盘/阵列成员。\n但会持续读取整块盘，耗时较长，扫描期间该盘的 IO 会被占用。',
      ok: '开始扫描',
      cancel: '取消',
    })
    if (!ok) return
  }
  stateMsg.value[dev] = '启动中…'
  stateErr.value[dev] = false
  try {
    const r = await apiFetch('/api/disks/selftest', 30000, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dev, type, confirm: true }),
    })
    const j = await r.json()
    stateMsg.value[dev] = j.ok ? '已启动' : ('失败：' + (j.error || ''))
    stateErr.value[dev] = !j.ok
    if (j.ok) pollDiskTests()
  } catch {
    stateMsg.value[dev] = '请求失败'
    stateErr.value[dev] = true
  }
}

async function abortDiskTest(dev: string): Promise<void> {
  try {
    const r = await apiFetch('/api/disks/selftest/abort', 30000, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dev }),
    })
    const j = await r.json()
    if (!j.ok) {
      stateMsg.value[dev] = '中止失败：' + (j.error || '')
      stateErr.value[dev] = true
    } else {
      pollDiskTests()
    }
  } catch {
    stateMsg.value[dev] = '请求失败'
    stateErr.value[dev] = true
  }
}

function clearDiskTest(dev: string): void {
  const next = { ...jobs.value }
  delete next[dev]
  jobs.value = next
}

/** 定位闪灯（写操作，POST /api/raid/locate） */
async function raidLocate(slot: string): Promise<void> {
  const on = !locateOn.value[slot]
  const action = on ? 'start' : 'stop'
  locateBusy.value[slot] = true
  try {
    const r = await apiFetch('/api/raid/locate', 30000, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slot, action }),
    })
    const j = await r.json()
    if (j.ok) {
      locateOn.value[slot] = on
    } else {
      alert('定位失败：' + (j.error || (j.out ? String(j.out).slice(0, 120) : '未知')))
    }
  } catch (e) {
    alert('请求失败：' + (e as Error).message)
  } finally {
    locateBusy.value[slot] = false
  }
}

/* ================= 弹窗交互 ================= */
function openPick(d: Disk): void {
  pickDev.value = d.dev
  pickModel.value = d.model || '未知型号'
  pickStandalone.value = !!d.standalone
  pickOpen.value = true
}

function choosePick(type: string): void {
  pickOpen.value = false
  if (type) startDiskTest(pickDev.value, type, pickModel.value)
}

function appConfirm(opts: { title?: string; body?: string; ok?: string; cancel?: string; danger?: boolean }): Promise<boolean> {
  confirmTitle.value = opts.title || '请确认'
  confirmBody.value = opts.body || ''
  confirmOk.value = opts.ok || '确定'
  confirmCancel.value = opts.cancel || '取消'
  confirmDanger.value = !!opts.danger
  confirmOpen.value = true
  return new Promise<boolean>(resolve => { confirmResolve = resolve })
}

function closeConfirm(v: boolean): void {
  confirmOpen.value = false
  if (confirmResolve) { confirmResolve(v); confirmResolve = null }
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    if (confirmOpen.value) closeConfirm(false)
    if (pickOpen.value) pickOpen.value = false
    if (histOpen.value) histOpen.value = false
  } else if (e.key === 'Enter' && confirmOpen.value) {
    closeConfirm(true)
  }
}

async function showDiskTestHistory(dev: string): Promise<void> {
  histDev.value = dev
  histOpen.value = true
  histLoading.value = true
  histErr.value = ''
  histRows.value = []
  try {
    const r = await apiFetch('/api/disks/selftest/history' + (dev ? '?dev=' + encodeURIComponent(dev) : ''), 30000)
    const j = await r.json()
    histRows.value = (j && j.history) || []
  } catch (e) {
    histErr.value = '加载失败：' + String((e as Error).message || e)
  } finally {
    histLoading.value = false
  }
}

function histTypeName(t?: string): string {
  return t === 'surface' ? '只读表面扫描' : (t === 'badblocks' ? '坏块慢扫' : 'SMART 长自检')
}

function histStateText(s?: string): { text: string; color: string } {
  // 注意：旧页这里写的是 var(--warn)，但主题里只有 --warning —— 该变量未定义，
  // 导致「中止」一直是继承色。新页改用已定义的 --warning（同 lv-error 那类隐性 bug）。
  if (s === 'done') return { text: '完成', color: 'var(--success)' }
  if (s === 'error') return { text: '失败', color: 'var(--danger)' }
  return { text: '中止', color: 'var(--warning)' }
}

/** 单行说明 + 悬停全文（口径与旧页逐字一致） */
function histDesc(h: HistRow): { segs: { text: string; cls: string }[]; title: string } {
  const segs: { text: string; cls: string }[] = []
  const tips: string[] = []
  if (h.total_bytes) {
    const speed = Math.round(h.total_bytes / Math.max(1, h.elapsed || 0) / 1048576)
    const cap = '扫描 ' + fmtBytes(h.total_bytes) + (speed ? ' · 平均 ' + speed + 'MB/s' : '')
    segs.push({ text: cap, cls: '' })
    tips.push(cap)
  }
  if (h.bad_lbas && h.bad_lbas.length) {
    const bbTxt = '坏块 ' + h.bad_blocks + ' · LBA ' + h.bad_lbas.join(', ') + ((h.bad_blocks || 0) > 20 ? ' …' : '')
    segs.push({ text: bbTxt, cls: 'danger' })
    tips.push(bbTxt)
  }
  const sb = h.smart_before || {}
  const sa = h.smart_after || {}
  const deltas: [string | number | undefined, string | number | undefined, string][] = [
    [sb[5], sa[5], '重映射'],
    [sb[197], sa[197], '待处理'],
    [sb[198], sa[198], '不可校正'],
  ]
  for (const [b, a, name] of deltas) {
    if (b == null && a == null) continue
    const bv = b == null ? '-' : b
    const av = a == null ? '-' : a
    const chg = b != null && a != null && a !== b
    const num = (a as number) - (b as number)
    const val = name + ' ' + bv + (chg ? '→' + av + ' (' + (num > 0 ? '+' : '') + num + ')' : '=' + av)
    segs.push({ text: val, cls: chg ? 'danger' : 'dim' })
    tips.push(val)
  }
  if (h.result && h.result.read_errors !== undefined
    && (String(h.result.read_errors) !== '0' || String(h.result.write_errors) !== '0' || String(h.result.corrected) !== '0')) {
    const rTxt = '读错 ' + h.result.read_errors + '/写错 ' + h.result.write_errors + '/纠正 ' + h.result.corrected
    segs.push({ text: rTxt, cls: '' })
    tips.push(rTxt)
  }
  if (!segs.length) segs.push({ text: h.message || '', cls: '' })
  return { segs, title: tips.length ? tips.join('\n') : (h.message || '') }
}

function histTime(finished?: number): string {
  const ts = new Date((finished || 0) * 1000)
  if (isNaN(ts.getTime())) return '-'
  const p = (n: number) => String(n).padStart(2, '0')
  return ts.getFullYear() + '-' + p(ts.getMonth() + 1) + '-' + p(ts.getDate()) + ' ' + p(ts.getHours()) + ':' + p(ts.getMinutes())
}

/* ================= 生命周期 ================= */
onMounted(() => {
  loadDisks()
  pollDiskTests()
  document.addEventListener('keydown', onKeydown)
})

onUnmounted(() => {
  disposed = true
  stopTick()
  stopPoll()
  document.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <div>
    <PanelHero
      icon="hdd"
      title="硬盘 SMART"
      sub="全部硬盘健康度 / 温度 / 寿命"
      :stats="heroStats"
      :last-update="lastUpdate"
      :busy="busy"
      @refresh="loadDisks(true)"
    />

    <div v-if="error" class="card"><div class="loading">{{ error }}</div></div>

    <div v-else-if="!disks.length && !busy" class="card"><div class="loading">未检测到硬盘</div></div>

    <template v-else>
      <!-- 健康异常汇总警告 -->
      <div
        v-if="badDisks.length"
        style="background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.4);border-radius:10px;padding:14px 16px;margin-bottom:14px"
      >
        <div style="font-weight:600;color:#b91c1c;margin-bottom:6px">⚠️ 检测到 {{ badDisks.length }} 块硬盘健康异常</div>
        <ul style="margin:0 0 8px 18px;padding:0;font-size:13px;line-height:1.7">
          <li v-for="d in badDisks" :key="d.dev">
            <b>{{ d.dev }}</b>（{{ d.model || '未知型号' }}）：SMART 健康状态
            <b style="color:var(--red)">{{ d.health || '异常' }}</b>
          </li>
        </ul>
        <div style="font-size:13px;color:var(--text)">
          建议：<b>尽快备份</b>这块盘上的重要数据，并准备更换硬盘；在换新盘之前，避免往它上面写入关键文件。
        </div>
      </div>

      <!-- 说明卡：自检会伤盘吗 -->
      <div class="card disk-info-card">
        <h3><AppIcon name="bulb" /> 硬盘自检会伤盘吗？</h3>
        <div class="note">
          <p>平时 nasdash 只是<b class="safe">只读 SMART 属性</b>（读健康度、温度、通电时间等），<b>不会扫描盘面，也不会写入数据</b>，对硬盘没有额外磨损。刷新一次也就 1～2 秒，点刷新/自动刷新都不会让你的硬盘一直转或一直扫。</p>
          <p>只有当你手动点下面每块盘里的「硬盘自检」，才是真正的自检——点开后按盘的情况可选三种方式：</p>
          <p>• <b>SMART 长自检（固件级）</b>：由硬盘固件只读自检整盘，不伤数据，适合二手盘/老盘体检，所有盘都能跑。</p>
          <p>• <b>只读表面扫描</b>：像硬盘哨兵那样<b>只读</b>逐扇区扫盘面找坏块，不写数据、不伤盘，<b>所有盘都能跑</b>（包括正在用的盘/阵列成员），耗时较长；扫描时显示扇区网格实时进度。</p>
          <p>• <b>全面坏块扫描</b>：会<b class="warn">写满整盘并清空数据</b>，最彻底但很慢。因此只让「独立盘」（没挂载、不在阵列、不在 LVM）跑；在用盘该项直接不显示。</p>
        </div>
      </div>

      <!-- 每块盘一张卡 -->
      <div class="cards">
        <div v-for="d in disks" :key="d.dev" class="card disk-card" :class="d.asleep ? '' : (d.health_ok ? '' : 'bad')">
          <div class="disk-head">
            <div style="display:flex;align-items:center;gap:8px;min-width:0">
              <span class="name">{{ d.dev }}</span>
              <span v-if="d.asleep" class="badge b-muted">休眠</span>
              <span v-else class="badge" :class="d.health_ok ? 'b-ok' : 'b-bad'">{{ d.health }}</span>
            </div>
            <button
              v-if="d.locate_supported && d.slot"
              class="btn-mini"
              :data-locate-slot="d.slot"
              :disabled="locateBusy[d.slot]"
              @click="raidLocate(d.slot!)"
            >{{ locateBusy[d.slot] ? '定位中…' : (locateOn[d.slot] ? '停止闪灯' : '定位闪灯') }}</button>
          </div>

          <div class="kv"><span class="k" title="硬盘的品牌与完整型号名称">型号</span><span class="v">{{ d.model ? ((d.brand ? d.brand + ' ' : '') + (d.vendor ? d.vendor + ' ' : '') + d.model) : '—' }}</span></div>
          <div class="kv">
            <span class="k" title="硬盘总容量与接口/介质类型（NVMe/SAS/SATA · SSD/HDD）">容量 / 类型</span>
            <span class="v">{{ d.size }} · {{ (d.type || '').toLowerCase() === 'nvme' ? 'NVMe' : ((d.type === 'sas') ? 'SAS' : 'SATA') }} · {{ d.rota == '1' ? 'HDD' : 'SSD' }}{{ diskFeatureClean(d.feature, d.dual_actuator) ? ' · ' + diskFeatureClean(d.feature, d.dual_actuator) : '' }}</span>
          </div>
          <div class="kv"><span class="k" title="机械盘每分钟转数；SSD 无转动，显示为固态">转速</span><span class="v">{{ d.rpm || ((d.rota == '1' || d.type === 'sas') ? '—' : '固态(SSD)') }}</span></div>
          <div class="kv"><span class="k" title="硬盘出厂唯一编号（SN）">序列号</span><span class="v" style="font-size:12px">{{ d.serial || '—' }}</span></div>

          <div style="margin-top:8px" title="硬盘当前温度传感器读数">
            <span style="font-size:13px;color:var(--muted)">温度 {{ d.temp != null ? d.temp + '°C' : 'N/A' }}</span>
            <div class="temp-bar">
              <div
                class="temp-fill"
                :style="{ width: (d.temp != null ? Math.min(d.temp / (d.temp_trip || 60) * 100, 100) : 0) + '%', background: tempColor(d.temp, d.temp_trip || 60) }"
              />
            </div>
          </div>

          <!-- SAS 细节 -->
          <template v-if="d.type === 'sas'">
            <div class="kv"><span class="k" title="硬盘累计通电运行小时数">已用时长</span><span class="v">{{ fmtHours(d.power_on_hours) }}</span></div>
            <div class="kv"><span class="k" title="SAS 硬盘报告的已发现缺陷扇区数">缺陷扇区</span><span class="v" :style="{ color: (d.defects || 0) > 0 ? 'var(--red)' : 'var(--text)' }">{{ d.defects }}</span></div>
            <div class="kv"><span class="k" title="SAS 硬盘尚待处理的缺陷扇区">待处理缺陷</span><span class="v" :style="{ color: (d.pending || 0) > 0 ? 'var(--orange)' : 'var(--text)' }">{{ d.pending }}</span></div>
            <div class="kv"><span class="k" title="SAS 硬盘与介质无关的报错次数（如通信/协议层）">非介质错误</span><span class="v">{{ d.non_medium_errors || 0 }}</span></div>
            <div class="kv"><span class="k" title="SAS 累计不可纠正读错误数">读错误(不可纠正)</span><span class="v">{{ d.read_errors }}</span></div>
            <div class="kv"><span class="k" title="SAS 累计不可纠正写错误数">写错误(不可纠正)</span><span class="v">{{ d.write_errors }}</span></div>
          </template>

          <!-- NVMe 细节 -->
          <template v-else-if="d.type === 'nvme'">
            <div class="kv"><span class="k" title="硬盘累计通电运行小时数">已用时长</span><span class="v">{{ fmtHours(d.power_on_hours) }}</span></div>
            <div class="kv"><span class="k" title="SSD/NVMe 已消耗的预计寿命百分比">已用寿命</span><span class="v" :style="{ color: (d.percentage_used || 0) > 0 ? 'var(--orange)' : 'var(--text)' }">{{ d.percentage_used != null ? d.percentage_used + '%' : 'N/A' }}</span></div>
            <div class="kv"><span class="k" title="SSD/NVMe 保留的备用块剩余比例，低于阈值可能掉速或报废">剩余备用</span><span class="v">{{ d.available_spare != null ? d.available_spare + '%' : 'N/A' }}</span></div>
            <div class="kv"><span class="k" title="NVMe 固态盘的告警标志：0x00 表示一切正常、无任何告警；若变成 0x01、0x02 等数字，才代表某项（如温度/备用空间/可靠性）超标需关注">临界告警</span><span class="v" :style="{ color: cwBad(d) ? 'var(--red)' : 'var(--green)' }">{{ cwBad(d) ? ('0x' + d.critical_warning + ' 告警') : '正常' }}</span></div>
            <div class="kv"><span class="k" title="SSD/NVMe 累计读取的数据量">读取量</span><span class="v">{{ d.data_units_read || 'N/A' }}</span></div>
            <div class="kv"><span class="k" title="SSD/NVMe 累计写入的数据量">写入量</span><span class="v">{{ d.data_units_written || 'N/A' }}</span></div>
          </template>

          <!-- ATA（SATA）细节 -->
          <template v-else>
            <div class="kv"><span class="k" title="硬盘累计通电运行小时数">已用时长</span><span class="v">{{ fmtHours(d.power_on_hours) }}</span></div>
            <div class="kv"><span class="k" title="硬盘已自动替换的坏扇区数量，>0 表示盘体已有损伤">重映射扇区</span><span class="v" :style="{ color: (d.reallocated || 0) > 0 ? 'var(--red)' : 'var(--text)' }">{{ d.reallocated != null ? d.reallocated : '—' }}</span></div>
            <div class="kv"><span class="k" title="读写时被发现不稳定、尚待确认或替换的扇区">待处理扇区</span><span class="v" :style="{ color: (d.pending || 0) > 0 ? 'var(--orange)' : 'var(--text)' }">{{ d.pending != null ? d.pending : '—' }}</span></div>
            <div class="kv"><span class="k" title="发生错误且无法通过重映射修复的扇区，>0 风险较高">不可纠正扇区</span><span class="v" :style="{ color: (d.uncorrectable || 0) > 0 ? 'var(--red)' : 'var(--text)' }">{{ d.uncorrectable != null ? d.uncorrectable : '—' }}</span></div>
            <div class="kv"><span class="k" title="SATA 传输过程中产生的 CRC 校验错误，多为数据线/接口接触问题">UDMA CRC 错误</span><span class="v">{{ d.udma_crc || 0 }}</span></div>
          </template>

          <!-- 硬盘自检面板 -->
          <div
            v-if="jobs[d.dev] && jobs[d.dev].state === 'running'"
            class="disk-test-section"
            :class="{ 'disk-test-min': minMap[d.dev] }"
          >
            <template v-if="minMap[d.dev]">
              <AppIcon name="pulse" />硬盘自检 <b>{{ d.dev }}</b> · {{ jobTypeName(jobs[d.dev].type) }}
              <span class="pct">{{ pctOf(d.dev) }}%</span>
              <button class="btn-mini" @click="minMap[d.dev] = false">展开</button>
              <button class="btn-mini" @click="abortDiskTest(d.dev)">中止</button>
              <span class="disk-test-msg">{{ stateMsg[d.dev] }}</span>
            </template>
            <template v-else>
              <div class="disk-test-title">
                <AppIcon name="pulse" />硬盘自检 · {{ jobTypeName(jobs[d.dev].type) }} · {{ d.dev }}
                <button class="btn-mini" style="margin-left:auto" title="收起面板，后台继续扫描" @click="minMap[d.dev] = true">最小化</button>
              </div>
              <div class="disk-test-msg">
                {{ jobs[d.dev].message }} · 已运行 <span>{{ fmtSec(elapsedOf(d.dev)) }}</span>{{ etaOf(d.dev).remain }}<span v-if="etaOf(d.dev).hint">{{ etaOf(d.dev).hint }}</span>
              </div>
              <div v-if="jobs[d.dev].progress != null" class="disk-test-progress">
                <div class="disk-test-progress-fill" :style="{ width: pctOf(d.dev) + '%' }" />
              </div>
              <div v-if="jobs[d.dev].progress != null" class="disk-test-pct" style="text-align:right;font-size:12px;color:var(--primary);font-weight:600;margin-top:3px">{{ pctOf(d.dev) }}%</div>

              <template v-if="isGridJob(d.dev)">
                <div class="surface-grid">
                  <i v-for="(c, i) in gridCells(d.dev)" :key="i" :class="c" />
                </div>
                <div class="surface-legend">
                  <span><i class="sg-cell ok" />正常</span><span><i class="sg-cell" />未扫</span>
                  <span><i class="sg-cell bad" />坏块</span><span><i class="sg-cell cur" />扫描位置</span>
                </div>
              </template>

              <div class="disk-test-actions">
                <button class="btn-mini" @click="abortDiskTest(d.dev)">中止</button>
                <span class="disk-test-msg" :class="{ error: stateErr[d.dev] }">{{ stateMsg[d.dev] }}</span>
              </div>
            </template>
          </div>

          <div v-else-if="jobs[d.dev] && (jobs[d.dev].state === 'done' || jobs[d.dev].state === 'error' || jobs[d.dev].state === 'aborted')" class="disk-test-section">
            <div class="disk-test-title">
              <AppIcon name="pulse" />硬盘自检 · {{ jobs[d.dev].state === 'done' ? '完成' : (jobs[d.dev].state === 'error' ? '失败' : '中止') }}
            </div>
            <div class="disk-test-msg" :class="jobs[d.dev].state === 'done' ? 'done' : (jobs[d.dev].state === 'error' ? 'error' : '')">
              {{ jobs[d.dev].message }}<template v-if="jobs[d.dev].result && jobs[d.dev].result!.read_errors !== undefined"> · 读错误 {{ jobs[d.dev].result!.read_errors }} / 写错误 {{ jobs[d.dev].result!.write_errors }} / 可纠正 {{ jobs[d.dev].result!.corrected }}</template><template v-if="jobs[d.dev].bad_blocks && jobs[d.dev].bad_blocks!.length"> · <b style="color:var(--danger)">坏块 {{ jobs[d.dev].bad_blocks!.length }} 个</b></template>
            </div>
            <div class="disk-test-actions">
              <button class="btn-mini" @click="clearDiskTest(d.dev)">清除</button>
            </div>
          </div>

          <div v-else class="disk-test-section">
            <div class="disk-test-title"><AppIcon name="pulse" />硬盘自检</div>
            <div class="disk-test-btns">
              <button class="btn-mini" @click="openPick(d)">硬盘自检</button>
              <button class="btn-mini" @click="showDiskTestHistory(d.dev)">自检记录</button>
            </div>
            <div class="disk-test-msg" :class="{ error: stateErr[d.dev] }">{{ stateMsg[d.dev] }}</div>
          </div>
        </div>
      </div>
    </template>

    <!-- ===== 自检方式选择（旧页 appPick） ===== -->
    <div v-if="pickOpen" class="modal-overlay show" @click.self="pickOpen = false">
      <div class="modal-box">
        <div class="modal-title">对 {{ pickModel }}（/dev/{{ pickDev }}）执行哪种自检？</div>
        <div class="modal-body">
          <button
            v-for="o in pickOptions"
            :key="o.type"
            class="pick-opt"
            :class="{ 'pick-opt-disabled': o.disabled }"
            :disabled="o.disabled"
            @click="choosePick(o.type)"
          >
            <span class="pick-opt-t">{{ o.label }}</span>
            <span class="pick-opt-d">{{ o.desc }}</span>
            <span v-if="o.note" class="pick-opt-note">{{ o.note }}</span>
          </button>
        </div>
        <div class="modal-actions">
          <button class="btn" @click="pickOpen = false">取消</button>
        </div>
      </div>
    </div>

    <!-- ===== 二次确认（旧页 appConfirm） ===== -->
    <div v-if="confirmOpen" class="modal-overlay show" @click.self="closeConfirm(false)">
      <div class="modal-box">
        <div class="modal-title">{{ confirmTitle }}</div>
        <div class="modal-body" style="white-space:pre-wrap">{{ confirmBody }}</div>
        <div class="modal-actions">
          <button class="btn" @click="closeConfirm(false)">{{ confirmCancel }}</button>
          <button class="btn" :class="confirmDanger ? 'btn-danger' : 'btn-primary'" @click="closeConfirm(true)">{{ confirmOk }}</button>
        </div>
      </div>
    </div>

    <!-- ===== 自检记录（旧页 showDiskTestHistory） ===== -->
    <div v-if="histOpen" class="modal-overlay show modal-wide" @click.self="histOpen = false">
      <div class="modal-box">
        <div class="modal-title">硬盘自检记录{{ histDev ? ' · /dev/' + histDev : '' }}</div>
        <div class="modal-body">
          <div v-if="histLoading" class="log-empty">加载中…</div>
          <div v-else-if="histErr" class="log-empty">{{ histErr }}</div>
          <div v-else-if="!histRows.length" class="log-empty">暂无自检记录</div>
          <template v-else>
            <div class="hist-head">
              <b class="c1">盘</b><span class="c2">类型</span><span class="c3">结果</span>
              <span class="c4">坏块</span><span class="c5">用时</span>
              <span class="c6">说明</span><span class="c7">时间</span>
            </div>
            <div v-for="(h, i) in histRows" :key="i" class="hist-row">
              <b class="c1">{{ h.dev || '' }}</b>
              <span class="c2">{{ histTypeName(h.type) }}</span>
              <span class="c3" :style="{ color: histStateText(h.state).color }">{{ histStateText(h.state).text }}</span>
              <span class="c4">
                <span v-if="(h.bad_blocks || 0) > 0" style="color:var(--danger)">坏块 {{ h.bad_blocks }}</span>
                <span v-else>坏块 0</span>
              </span>
              <span class="c5">{{ fmtSec(h.elapsed || 0) }}</span>
              <span class="c6" :title="histDesc(h).title">
                <template v-for="(s, si) in histDesc(h).segs" :key="si">
                  <span
                    :style="{ color: s.cls === 'danger' ? 'var(--danger)' : (s.cls === 'dim' ? 'var(--text-3)' : 'inherit') }"
                  >{{ s.text }}</span><span v-if="si < histDesc(h).segs.length - 1"> · </span>
                </template>
              </span>
              <span class="c7">{{ histTime(h.finished_at) }}</span>
            </div>
            <div class="hist-foot">仅保留最近 50 条 · 应用重启后仍可回查</div>
          </template>
        </div>
        <div class="modal-actions">
          <button class="btn" @click="histOpen = false">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>
