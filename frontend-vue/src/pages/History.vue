<script setup lang="ts">
/**
 * 历史趋势（sys-hist）—— Vue 原生页
 *
 * 复刻旧页 templates/index.html 的 initHistory / drawHist / loadHistory，
 * 以及「生成分析报告」整套（computeStats / metricNarrative / buildOverview /
 * sparkline / asciiSpark / toCSV / svgChartImg / toMarkdown / toHTML / downloadReport）。
 *
 * 数据：GET /api/history?range=24h|7d|30d —— 纯只读，无写操作、无鉴权门槛。
 * 主图为 canvas 手绘折线（6 个维度自选）；报告在浏览器本地生成后下载（不经过服务器）。
 */
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import PanelHero from '../components/PanelHero.vue'
import { apiFetch } from '../lib/api'
import { fmtSpeed } from '../lib/format'
import { pageCacheGet, pageCacheSet } from '../lib/pageCache'

type Metric = 'disk' | 'net' | 'power' | 'temp' | 'fan' | 'mem'
type Range = '24h' | '7d' | '30d'
type RptFmt = 'html' | 'md' | 'csv'
type Level = 'ok' | 'warn' | 'danger'

interface HistPoint {
  ts: number
  [key: string]: number | null
}

/* ---------------- 常量 ---------------- */

const METRIC_TABS: { id: Metric; label: string }[] = [
  { id: 'disk', label: '磁盘 I/O' },
  { id: 'net', label: '网络' },
  { id: 'power', label: '功耗' },
  { id: 'temp', label: '温度' },
  { id: 'fan', label: '风扇' },
  { id: 'mem', label: '内存' },
]
const METRIC_TITLE: Record<Metric, string> = {
  disk: '磁盘 I/O 历史趋势',
  net: '网络速率历史趋势',
  power: 'CPU 功耗历史趋势',
  temp: '温度历史趋势',
  fan: '风扇转速历史趋势',
  mem: '内存占用历史趋势',
}
const RANGE_TABS: { id: Range; label: string; stat: string }[] = [
  { id: '24h', label: '24小时', stat: '24 小时' },
  { id: '7d', label: '7天', stat: '7 天' },
  { id: '30d', label: '30天', stat: '30 天' },
]
const RANGE_ZH: Record<Range, string> = { '24h': '24 小时', '7d': '7 天', '30d': '30 天' }
const RPT_RANGES: { id: Range; label: string }[] = RANGE_TABS.map(r => ({ id: r.id, label: r.stat }))
const RPT_FORMATS: { id: RptFmt; label: string }[] = [
  { id: 'html', label: '单文件 HTML（含图）' },
  { id: 'md', label: 'Markdown' },
  { id: 'csv', label: 'CSV 数据' },
]

/** 主图各维度的字段 / 颜色 / 图例名 / 量纲 */
const CHART_CFG: Record<Metric, { keys: [string, string, string][]; unit: string }> = {
  disk: {
    keys: [
      ['disk_read', '#4aa3ff', '读'],
      ['disk_write', '#ff7a59', '写'],
    ],
    unit: 'speed',
  },
  net: {
    keys: [
      ['net_rx', '#3ec97a', '↓下载'],
      ['net_tx', '#ffb020', '↑上传'],
    ],
    unit: 'speed',
  },
  power: { keys: [['cpu_power', '#b07cff', 'CPU 功耗']], unit: 'w' },
  temp: {
    keys: [
      ['cpu_temp', '#4aa3ff', 'CPU温度'],
      ['mb_temp', '#3ec97a', '主板温度'],
      ['disk_temp_max', '#ff7a59', '硬盘最高温'],
      ['raid_temp', '#b07cff', '阵列卡'],
      ['gpu_temp', '#ffb020', 'GPU温度'],
    ],
    unit: 'c',
  },
  fan: { keys: [['fan_rpm_avg', '#4aa3ff', '风扇平均转速']], unit: 'rpm' },
  mem: { keys: [['mem_used_pct', '#3ec97a', '内存占用率']], unit: 'pct' },
}
const UNIT_TEXT: Record<string, string> = {
  w: '单位 W',
  c: '单位 °C',
  rpm: '单位 RPM',
  pct: '单位 %',
  speed: '速率自适应 B/s·KB/s·MB/s',
}

/* ---------------- 主图状态 ---------------- */

const histMetric = ref<Metric>('disk')
const histRange = ref<Range>('24h')
const busy = ref(true)
/** hero 右上角的刷新时间（之前没传，控件一直显示「加载中…」） */
const lastUpdate = ref('')
const canvasEl = ref<HTMLCanvasElement | null>(null)
const legendHtml = ref('')
let lastHist: { points?: HistPoint[] } | null = null
let themeObserver: MutationObserver | null = null
let resizeTimer = 0

// ===== 温度红线（用户可设，纯前端 localStorage，不写服务端）=====
const TEMP_THRESHOLD_KEY = 'nasdash_temp_threshold'
function loadTempThreshold(): number {
  try {
    const raw = localStorage.getItem(TEMP_THRESHOLD_KEY)
    if (raw != null) {
      const v = Number(raw)
      if (!isNaN(v)) return Math.max(30, Math.min(120, Math.round(v)))
    }
  } catch {
    /* 忽略 */
  }
  return 75
}
const tempThreshold = ref<number>(loadTempThreshold())
function setTempThreshold(v: number): void {
  const n = Math.max(30, Math.min(120, Math.round(Number(v) || 75)))
  tempThreshold.value = n
  try {
    localStorage.setItem(TEMP_THRESHOLD_KEY, String(n))
  } catch {
    /* 忽略 */
  }
  drawHist(lastHist)
}

const heroStats = computed(() => [
  { v: RANGE_ZH[histRange.value], k: '当前区间' },
  { v: '30 天', k: '数据保留' },
])
const sectionTitle = computed(() => METRIC_TITLE[histMetric.value])

function cssVar(name: string, fallback = ''): string {
  return (getComputedStyle(document.documentElement).getPropertyValue(name) || '').trim() || fallback
}
function fmtTime(ts: number): string {
  const d = new Date(ts)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** 画主图（canvas 手绘，口径与旧页 drawHist 完全一致） */
function drawHist(d: { points?: HistPoint[] } | null): void {
  lastHist = d
  const cv = canvasEl.value
  if (!cv) return
  const pts = (d && d.points) || []
  const dpr = window.devicePixelRatio || 1
  const cssW = cv.clientWidth || 600
  const cssH = 170
  cv.width = Math.round(cssW * dpr)
  cv.height = Math.round(cssH * dpr)
  const ctx = cv.getContext('2d')
  if (!ctx) return
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, cssW, cssH)
  const muted = cssVar('--muted', '#8a93a6')

  if (!pts.length) {
    ctx.fillStyle = muted
    ctx.font = '13px sans-serif'
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    ctx.fillText('暂无数据（需运行一段时间后才会有历史记录）', cssW / 2, cssH / 2)
    legendHtml.value = ''
    return
  }

  const CFG = CHART_CFG[histMetric.value] || { keys: [], unit: 'speed' }
  let maxV = 1
  CFG.keys.forEach(k => pts.forEach(p => { const v = p[k[0]] || 0; if (v > maxV) maxV = v }))
  // 温度图：用户红线可能高于当前读数，若不加会被自动缩放顶出画面 → 抬升上限保它在图内
  if (CFG.unit === 'c') maxV = Math.max(maxV, tempThreshold.value || 0)
  const padL = 46, padR = 10, padT = 10, padB = 20
  const X = (i: number) => padL + (cssW - padL - padR) * (i / ((pts.length - 1) || 1))
  const Y = (v: number) => cssH - padB - (cssH - padB - padT) * (v / maxV)
  const fmtAxis = (v: number) =>
    CFG.unit === 'w' ? Math.round(v) + 'W'
      : CFG.unit === 'c' ? Math.round(v) + '°C'
        : CFG.unit === 'rpm' ? Math.round(v) + ' RPM'
          : CFG.unit === 'pct' ? Math.round(v) + '%'
            : fmtSpeed(v)

  ctx.strokeStyle = 'rgba(128,128,128,0.15)'
  ctx.lineWidth = 1
  ctx.fillStyle = muted
  ctx.font = '10px sans-serif'
  ctx.textAlign = 'right'
  ctx.textBaseline = 'middle'
  for (let g = 0; g <= 3; g++) {
    const yy = padT + (cssH - padT - padB) * (g / 3)
    ctx.beginPath()
    ctx.moveTo(padL, yy)
    ctx.lineTo(cssW - padR, yy)
    ctx.stroke()
    ctx.fillText(fmtAxis(maxV * (1 - g / 3)), padL - 4, yy)
  }

  const line = (key: string, color: string) => {
    ctx.strokeStyle = color
    ctx.lineWidth = 1.6
    ctx.beginPath()
    pts.forEach((p, i) => {
      const xx = X(i)
      const yy = Y(p[key] || 0)
      if (i) ctx.lineTo(xx, yy)
      else ctx.moveTo(xx, yy)
    })
    ctx.stroke()
  }
  CFG.keys.forEach(k => line(k[0], k[1]))

  // 用户自定义温度红线（仅在温度量纲显示）：线上方淡红危险区 + 红色虚线 + 标签
  if (CFG.unit === 'c' && tempThreshold.value > 0 && tempThreshold.value <= maxV) {
    const ty = Y(tempThreshold.value)
    ctx.fillStyle = 'rgba(229,72,77,0.08)'
    ctx.fillRect(padL, padT, cssW - padL - padR, ty - padT)
    ctx.save()
    ctx.strokeStyle = 'rgba(229,72,77,0.9)'
    ctx.lineWidth = 1.2
    ctx.setLineDash([5, 4])
    ctx.beginPath()
    ctx.moveTo(padL, ty)
    ctx.lineTo(cssW - padR, ty)
    ctx.stroke()
    ctx.restore()
    ctx.fillStyle = 'rgba(229,72,77,0.95)'
    ctx.font = '10px sans-serif'
    ctx.textAlign = 'right'
    ctx.textBaseline = 'bottom'
    ctx.fillText('红线 ' + Math.round(tempThreshold.value) + '°C', cssW - padR - 2, ty - 2)
  }

  ctx.fillStyle = muted
  ctx.textBaseline = 'alphabetic'
  ctx.textAlign = 'left'
  ctx.fillText(fmtTime(pts[0].ts), padL, cssH - 6)
  ctx.textAlign = 'right'
  ctx.fillText(fmtTime(pts[pts.length - 1].ts), cssW - padR, cssH - 6)

  legendHtml.value =
    CFG.keys.map(k => `${k[2]} <span style="color:${k[1]}">■</span>`).join(' &nbsp; ') +
    ' &nbsp; ' + (UNIT_TEXT[CFG.unit] || UNIT_TEXT.speed) +
    '（数据自部署起累积，自动保留 30 天）'
  if (CFG.unit === 'c') {
    legendHtml.value += ' &nbsp; <span style="color:#e5484d">┄ 红线 ' + Math.round(tempThreshold.value) + '°C</span>'
  }
}

async function loadHistory(): Promise<void> {
  // 切页签回来先用缓存把图画出来（秒开），再拉最新数据替换
  const cacheKey = 'hist:' + histRange.value
  if (!lastHist) {
    const cached = pageCacheGet<{ points?: HistPoint[] }>(cacheKey)
    if (cached?.points?.length) {
      busy.value = false
      await nextTick()
      drawHist(cached)
    }
  }
  // 已有图（缓存或上次的）就不转圈：后台静默拉最新
  if (!lastHist) busy.value = true
  try {
    const r = await apiFetch('/api/history?range=' + histRange.value + '&_=' + Date.now(), 20000)
    const d = await r.json()
    await nextTick()
    drawHist(d)
    pageCacheSet(cacheKey, d)
    lastUpdate.value = '更新于 ' + new Date().toLocaleTimeString('zh-CN')
  } catch {
    /* 拉取失败保持上一次的图，不打断页面（与旧页一致） */
  } finally {
    busy.value = false
  }
}

function pickMetric(m: Metric): void {
  histMetric.value = m
  drawHist(lastHist)
}
function pickRange(r: Range): void {
  histRange.value = r
  void loadHistory()
}

function onResize(): void {
  if (resizeTimer) window.clearTimeout(resizeTimer)
  resizeTimer = window.setTimeout(() => drawHist(lastHist), 150)
}

/* ---------------- 生成分析报告 ---------------- */

interface ReportMetricDef {
  key: string
  label: string
  unit: string
  color: string
  warn?: number
  danger?: number
  optional?: boolean
  special?: string
  info?: boolean
}
interface Stat {
  n: number
  mean: number
  min: number
  max: number
  maxTs: number | null
  minTs: number | null
  trend: 'up' | 'down' | 'flat'
  values: number[]
}
interface Narrative {
  level: Level
  text: string
}
interface ReportItem {
  m: ReportMetricDef
  s: Stat
  nar: Narrative | null
}

const REPORT_METRICS: ReportMetricDef[] = [
  { key: 'cpu_temp', label: 'CPU 温度', unit: '°C', color: '#4aa3ff', warn: 75, danger: 85 },
  { key: 'mb_temp', label: '主板温度', unit: '°C', color: '#3ec97a', warn: 60, danger: 70 },
  { key: 'gpu_temp', label: 'GPU 温度', unit: '°C', color: '#ffb020', warn: 80, danger: 90, optional: true },
  { key: 'disk_temp_max', label: '硬盘最高温', unit: '°C', color: '#ff7a59', warn: 55, danger: 65 },
  { key: 'raid_temp', label: '阵列卡温度', unit: '°C', color: '#b07cff', warn: 70, danger: 80 },
  { key: 'fan_rpm_avg', label: '风扇平均转速', unit: 'RPM', color: '#4aa3ff', special: 'fan' },
  { key: 'mem_used_pct', label: '内存占用率', unit: '%', color: '#3ec97a', warn: 85, danger: 95 },
  { key: 'cpu_power', label: 'CPU 功耗', unit: 'W', color: '#b07cff', info: true },
  { key: 'net_rx', label: '网络下载', unit: 'speed', color: '#3ec97a', info: true },
  { key: 'net_tx', label: '网络上传', unit: 'speed', color: '#ffb020', info: true },
  { key: 'disk_read', label: '磁盘读取', unit: 'speed', color: '#4aa3ff', info: true },
  { key: 'disk_write', label: '磁盘写入', unit: 'speed', color: '#ff7a59', info: true },
]
const REPORT_GROUPS: { title: string; keys: string[] }[] = [
  { title: '温度', keys: ['cpu_temp', 'mb_temp', 'gpu_temp', 'disk_temp_max', 'raid_temp'] },
  { title: '散热', keys: ['fan_rpm_avg'] },
  { title: '资源占用', keys: ['mem_used_pct', 'cpu_power'] },
  { title: '网络与磁盘 I/O', keys: ['net_rx', 'net_tx', 'disk_read', 'disk_write'] },
]

function fmtTimeFull(ts: number): string {
  const d = new Date(ts)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}
function trendText(t: Stat['trend']): string {
  return t === 'up' ? '呈上升趋势' : t === 'down' ? '呈下降趋势' : '基本平稳'
}
function computeStats(pts: HistPoint[], key: string): Stat | null {
  const vs = pts.map(p => p[key]).filter((v): v is number => typeof v === 'number' && !isNaN(v))
  if (!vs.length) return null
  const n = vs.length
  const mean = vs.reduce((a, b) => a + b, 0) / n
  const min = Math.min(...vs)
  const max = Math.max(...vs)
  let maxTs: number | null = null
  let minTs: number | null = null
  for (const p of pts) {
    if (typeof p[key] === 'number') {
      if (maxTs === null && p[key] === max) maxTs = p.ts
      if (minTs === null && p[key] === min) minTs = p.ts
    }
  }
  const h = Math.max(1, Math.floor(n / 2))
  const fmean = vs.slice(0, h).reduce((a, b) => a + b, 0) / h
  const smean = vs.slice(h).reduce((a, b) => a + b, 0) / Math.max(1, n - h)
  let trend: Stat['trend'] = 'flat'
  const diff = mean ? (smean - fmean) / mean : 0
  if (diff > 0.1) trend = 'up'
  else if (diff < -0.1) trend = 'down'
  return { n, mean, min, max, maxTs, minTs, trend, values: vs }
}
function metricNarrative(m: ReportMetricDef, s: Stat | null): Narrative | null {
  if (!s) return null
  const u = m.unit === '°C' ? '°C' : m.unit === 'RPM' ? ' RPM' : m.unit === '%' ? '%' : m.unit === 'W' ? 'W' : ''
  const fm = (v: number) => (m.unit === 'speed' ? fmtSpeed(v) : String(Math.round(v * 10) / 10))
  const tMax = s.maxTs ? fmtTime(s.maxTs) : '-'
  const tr = trendText(s.trend)
  if (m.special === 'fan') {
    if (s.mean < 1) {
      return { level: 'danger', text: `${m.label}约为 0（或数据缺失），疑似风扇停转，请立即检查风扇接线与供电！` }
    }
    return {
      level: 'ok',
      text: `${m.label}平均 ${Math.round(s.mean)} RPM，最高 ${Math.round(s.max)} RPM（${tMax}），最低 ${Math.round(s.min)} RPM，运转平稳。`,
    }
  }
  if (m.info) {
    return { level: 'ok', text: `${m.label}平均 ${fm(s.mean)}，峰值 ${fm(s.max)}（${tMax}）${tr}。` }
  }
  let level: Level = 'ok'
  let extra = ''
  if (m.danger && s.max >= m.danger) {
    level = 'danger'
    extra = `最高 ${fm(s.max)}${u} 已超过 ${m.danger}${u} 危险线，建议尽快处理（清灰 / 改善风道 / 降负载）。`
  } else if (m.warn && s.max >= m.warn) {
    level = 'warn'
    extra = `最高 ${fm(s.max)}${u} 超过 ${m.warn}${u} 关注线，建议关注散热情况。`
  }
  const head = `${m.label}平均 ${fm(s.mean)}${u}，最高 ${fm(s.max)}${u}（${tMax}），最低 ${fm(s.min)}${u}，${tr}`
  const tail = level === 'ok' ? '，处于正常范围内。' : '，' + extra
  return { level, text: head + tail }
}
function buildOverview(items: ReportItem[]): Narrative {
  const dangers = items.filter(x => x.nar && x.nar.level === 'danger')
  const warns = items.filter(x => x.nar && x.nar.level === 'warn')
  if (dangers.length) {
    return { level: 'danger', text: `检测到 ${dangers.length} 项危险指标（${dangers.map(x => x.m.label).join('、')}），建议尽快排查散热与负载。` }
  }
  if (warns.length) {
    return { level: 'warn', text: `整体运行基本正常，但有 ${warns.length} 项指标偏高（${warns.map(x => x.m.label).join('、')}），建议关注。` }
  }
  return { level: 'ok', text: '过去区间内各项指标均处于正常范围，运行健康，无紧急风险。' }
}
function sparkline(values: number[], color: string, w?: number, h?: number): string {
  const W = w || 240, H = h || 42
  if (!values || values.length < 2) return ''
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const X = (i: number) => (i / (values.length - 1)) * W
  const Y = (v: number) => H - 2 - ((v - min) / span) * (H - 4)
  const pts = values.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ')
  return `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" preserveAspectRatio="none" style="display:block"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round"/></svg>`
}
/** 文本迷你折线（ASCII sparkline）：任何纯文本环境都能看到趋势形状 */
function asciiSpark(values: number[], w?: number): string {
  const W = w || 32
  if (!values || values.length < 2) return ''
  const bars = '▁▂▃▄▅▆▇█'
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const n = values.length
  let s = ''
  for (let i = 0; i < W; i++) {
    const idx = Math.floor((i / W) * (n - 1))
    const v = values[idx]
    s += bars[Math.min(bars.length - 1, Math.round(((v - min) / span) * (bars.length - 1)))]
  }
  return s
}
function toCSV(points: HistPoint[]): string {
  const defs: { key: string; label: string; speed?: boolean }[] = [
    { key: 'disk_read', label: '磁盘读取(KB/s)', speed: true },
    { key: 'disk_write', label: '磁盘写入(KB/s)', speed: true },
    { key: 'net_rx', label: '网络下载(KB/s)', speed: true },
    { key: 'net_tx', label: '网络上传(KB/s)', speed: true },
    { key: 'cpu_power', label: 'CPU功耗(W)' },
    { key: 'cpu_temp', label: 'CPU温度(°C)' },
    { key: 'mb_temp', label: '主板温度(°C)' },
    { key: 'gpu_temp', label: 'GPU温度(°C)' },
    { key: 'disk_temp_max', label: '硬盘最高温(°C)' },
    { key: 'raid_temp', label: '阵列卡温度(°C)' },
    { key: 'fan_rpm_avg', label: '风扇转速(RPM)' },
    { key: 'mem_used_pct', label: '内存占用(%)' },
  ]
  const head = '时间,' + defs.map(d => d.label).join(',')
  const rows = points.map(
    p =>
      fmtTimeFull(p.ts) +
      ',' +
      defs
        .map(d => {
          const v = p[d.key]
          if (v == null || isNaN(v)) return ''
          if (d.speed) return (v / 1024).toFixed(1)
          return String(Math.round(v * 10) / 10)
        })
        .join(','),
  )
  return head + '\n' + rows.join('\n')
}
/**
 * 真折线图：内嵌为独立 SVG 图片（base64 data-uri）。
 * VS Code / Typora / Obsidian 等查看器都会渲染为真实折线图，与 HTML 版视觉一致；
 * 不依赖 Mermaid 引擎，避免 VS Code 预览渲染不出 xychart-beta 的问题。
 */
function svgChartImg(m: ReportMetricDef, pairs: { ts: number; v: number }[]): string {
  if (!pairs || pairs.length < 2) return ''
  const conv = m.unit === 'speed' ? (v: number) => v / 1024 : (v: number) => v
  const unitLabel = m.unit === 'speed' ? 'KB/s' : m.unit
  const all = pairs.map(p => ({ t: p.ts, v: conv(p.v) }))
  const step = Math.max(1, Math.ceil(all.length / 60))
  const ds = all.filter((_, i) => i % step === 0 || i === all.length - 1)
  if (ds.length < 2) return ''
  const W = 560, H = 132, padL = 10, padR = 10, padT = 24, padB = 18
  const vals = ds.map(d => d.v)
  const mn = Math.min(...vals)
  const mx = Math.max(...vals)
  const span = mx - mn || 1
  const lo = mn - span * 0.12
  const hi = mx + span * 0.12
  const X = (i: number) => padL + (i / (ds.length - 1)) * (W - padL - padR)
  const Y = (v: number) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB)
  const pts = ds.map((d, i) => `${X(i).toFixed(1)},${Y(d.v).toFixed(1)}`).join(' ')
  const last = ds[ds.length - 1].v
  const lastTxt = m.unit === 'speed' ? Math.round(last * 10) / 10 + ' KB/s' : Math.round(last * 10) / 10 + unitLabel
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">` +
    `<rect x="0" y="0" width="${W}" height="${H}" fill="#fafbfc"/>` +
    `<polyline points="${pts}" fill="none" stroke="${m.color}" stroke-width="1.6" stroke-linejoin="round"/>` +
    `<text x="${W - 8}" y="15" text-anchor="end" font-size="11" fill="#6b7280" font-family="sans-serif">${m.label}（${unitLabel}） 最新 ${lastTxt}</text>` +
    `<text x="8" y="${H - 5}" font-size="10" fill="#9aa1ad" font-family="sans-serif">${fmtTime(ds[0].t)}</text>` +
    `<text x="${W - 8}" y="${H - 5}" text-anchor="end" font-size="10" fill="#9aa1ad" font-family="sans-serif">${fmtTime(ds[ds.length - 1].t)}</text>` +
    `</svg>`
  let b64 = ''
  try {
    b64 = btoa(unescape(encodeURIComponent(svg)))
  } catch {
    return ''
  }
  return `![${m.label} 折线图](data:image/svg+xml;base64,${b64})`
}
function toMarkdown(range: Range, genAt: string, items: ReportItem[], overview: Narrative, points: HistPoint[]): string {
  const rt = RANGE_ZH[range] || range
  const n = items.length ? items[0].s.n : 0
  let md = '# nasdash 历史趋势分析报告\n\n'
  md += `- 数据区间：${rt}（历史数据自动保留 30 天）\n`
  md += `- 生成时间：${genAt}\n`
  md += `- 数据样本：${n} 条（每 30 秒聚合一点）\n\n`
  md += `- 说明：下方折线图为内嵌 SVG 图片。在 VS Code 中请按 **Cmd+Shift+V（Mac）/ Ctrl+Shift+V（Win）** 打开「预览」视图查看真图；代码视图、GitHub 网页、微信中只显示图片链接文字属正常现象（下方「文本趋势线」仍可看趋势）。若需任意环境都为真图，请下载 HTML 版。\n\n`
  md += `## 总体结论\n\n${overview.text}\n\n`
  REPORT_GROUPS.forEach(g => {
    const gs = items.filter(x => g.keys.includes(x.m.key))
    if (!gs.length) return
    md += `## ${g.title}\n\n`
    gs.forEach(x => {
      const pairs = points
        .map(p => ({ ts: p.ts, v: p[x.m.key] }))
        .filter((p): p is { ts: number; v: number } => typeof p.v === 'number' && !isNaN(p.v))
      md += `### ${x.m.label}\n${x.nar ? x.nar.text : ''}\n\n${svgChartImg(x.m, pairs)}\n\n> 趋势（${x.m.label}）：${asciiSpark(x.s.values)}\n\n`
    })
  })
  const warns = items.filter(x => x.nar && x.nar.level !== 'ok')
  if (warns.length) {
    md += `## 异常与建议\n\n`
    warns.forEach(x => (md += `- 【${x.nar && x.nar.level === 'danger' ? '危险' : '关注'}】${x.nar ? x.nar.text : ''}\n`))
    md += `\n建议：优先检查机箱风道与防尘，必要时调整风扇策略或降低负载。\n`
  }
  return md
}
function toHTML(range: Range, genAt: string, items: ReportItem[], overview: Narrative): string {
  const rt = RANGE_ZH[range] || range
  const n = items.length ? items[0].s.n : 0
  const ovClass = overview.level === 'danger' ? 'danger' : overview.level === 'warn' ? 'warn' : 'ok'
  let body = ''
  REPORT_GROUPS.forEach(g => {
    const gs = items.filter(x => g.keys.includes(x.m.key))
    if (!gs.length) return
    body += `<h2>${g.title}</h2>`
    gs.forEach(x => {
      const lvl = x.nar ? x.nar.level : 'ok'
      const badge =
        lvl === 'danger'
          ? '<span class="badge danger">危险</span>'
          : lvl === 'warn'
            ? '<span class="badge warn">关注</span>'
            : '<span class="badge ok">正常</span>'
      body += `<div class="item ${lvl}"><div class="item-h"><span class="dot" style="background:${x.m.color}"></span><b>${x.m.label}</b>${badge}</div><div class="spark">${sparkline(x.s.values, x.m.color)}</div><div class="nar">${x.nar ? x.nar.text : ''}</div></div>`
    })
  })
  const warns = items.filter(x => x.nar && x.nar.level !== 'ok')
  let advice = ''
  if (warns.length) {
    advice = `<div class="block danger-block"><h2>异常与建议</h2><ul>${warns.map(x => `<li><b>${x.m.label}：</b>${x.nar ? x.nar.text : ''}</li>`).join('')}</ul><p>建议：优先检查机箱风道与防尘，必要时调整风扇策略或降低负载。</p></div>`
  }
  const CSS = `*{box-sizing:border-box}body{font-family:-apple-system,"Microsoft YaHei",sans-serif;background:#f5f6f8;color:#1f2430;margin:0;padding:24px;line-height:1.6}.wrap{max-width:840px;margin:0 auto;background:#fff;border-radius:12px;padding:28px;box-shadow:0 1px 3px rgba(0,0,0,.08)}header h1{font-size:20px;margin:0 0 6px}.meta{color:#6b7280;font-size:13px}.overview{border-radius:10px;padding:14px 16px;margin:18px 0;font-size:15px}.overview.ok{background:#e9f7ef;color:#1b7f4b;border:1px solid #bfe6cf}.overview.warn{background:#fff6e6;color:#9a6a00;border:1px solid #ffe0a3}.overview.danger{background:#fdecec;color:#b42323;border:1px solid #f6c9c9}h2{font-size:16px;margin:22px 0 10px;border-left:3px solid #4aa3ff;padding-left:8px}.item{border:1px solid #eef0f3;border-radius:10px;padding:12px 14px;margin-bottom:10px}.item.danger{border-color:#f3c2c2;background:#fff7f7}.item.warn{border-color:#ffe2b0;background:#fffaef}.item-h{display:flex;align-items:center;gap:8px;font-size:14px}.dot{width:10px;height:10px;border-radius:50%;display:inline-block}.badge{font-size:11px;padding:1px 8px;border-radius:10px;margin-left:auto}.badge.ok{background:#e9f7ef;color:#1b7f4b}.badge.warn{background:#fff6e6;color:#9a6a00}.badge.danger{background:#fdecec;color:#b42323}.spark{margin:8px 0}.nar{font-size:13px;color:#374151}.block.danger-block{background:#fff7f7;border:1px solid #f3c2c2;border-radius:10px;padding:14px 16px;margin-top:18px}.block ul{margin:8px 0;padding-left:20px}.block li{margin:4px 0;font-size:13px}footer{margin-top:24px;color:#9aa1ad;font-size:12px;text-align:center}`
  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>nasdash 历史趋势分析报告</title><style>${CSS}</style></head><body><div class="wrap"><header><h1>nasdash 历史趋势分析报告</h1><div class="meta">数据区间：${rt} · 生成时间：${genAt} · 样本 ${n} 条（每 30 秒一点，自动保留 30 天）</div></header><div class="overview ${ovClass}">${overview.text}</div>${body}${advice}<footer>本报告由 nasdash 在您的 NAS 本地生成，数据未离开本机。</footer></div></body></html>`
}
function downloadReport(filename: string, content: string, mime: string, bom: boolean): void {
  const blob = new Blob(bom ? ['\ufeff' + content] : [content], { type: mime + ';charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

/* ---------------- 报告弹窗 ---------------- */

const reportOpen = ref(false)
const rptRange = ref<Range>('7d')
const rptFmt = ref<RptFmt>('html')
const reportStatus = ref('')

const rptHint = computed(() => {
  return {
    html: '<b>HTML</b>：任意环境（双击即开）均显示真折线图，推荐大多数人使用。',
    md: '<b>Markdown</b>：折线图为内嵌图片，需用支持预览的查看器——VS Code 按 <b>Cmd/Ctrl+Shift+V</b> 进「预览」视图才看得到图；代码视图、GitHub 网页、微信里只显示文字，属正常。',
    csv: '<b>CSV</b>：原始数据（已换算 KB/s、中文表头），用 Excel / 表格软件直接打开分析。',
  }[rptFmt.value] || ''
})

function closeReport(): void {
  reportOpen.value = false
  reportStatus.value = ''
}
async function genReport(range: Range, fmt: RptFmt): Promise<void> {
  reportStatus.value = '生成中…'
  try {
    const r = await apiFetch('/api/history?range=' + range + '&_=' + Date.now(), 30000)
    const d = await r.json()
    const pts: HistPoint[] = d.points || []
    if (pts.length < 3) {
      alert('历史样本不足（仅 ' + pts.length + ' 条），无法生成有意义的分析。请让 NAS 运行更长时间后再试。')
      reportStatus.value = ''
      return
    }
    const items: ReportItem[] = REPORT_METRICS.map(m => ({ m, s: computeStats(pts, m.key) }))
      .filter((x): x is { m: ReportMetricDef; s: Stat } => !!x.s)
      .map(x => ({ m: x.m, s: x.s, nar: metricNarrative(x.m, x.s) }))
    const overview = buildOverview(items)
    const genAt = new Date().toLocaleString('zh-CN')
    let content = ''
    let mime = ''
    let ext = ''
    let bom = false
    if (fmt === 'csv') {
      content = toCSV(pts)
      mime = 'text/csv'
      ext = 'csv'
      bom = true
    } else if (fmt === 'md') {
      content = toMarkdown(range, genAt, items, overview, pts)
      mime = 'text/markdown'
      ext = 'md'
    } else {
      content = toHTML(range, genAt, items, overview)
      mime = 'text/html'
      ext = 'html'
    }
    const fn = `nasdash-历史报告-${range}-${new Date().toISOString().slice(0, 10)}.${ext}`
    downloadReport(fn, content, mime, bom)
    reportStatus.value = '已下载：' + fn
    setTimeout(() => {
      if (reportStatus.value.startsWith('已下载')) reportStatus.value = ''
    }, 4000)
  } catch (e) {
    reportStatus.value = '生成失败：' + e
  }
}

/* ---------------- 生命周期 ---------------- */

onMounted(async () => {
  await loadHistory()
  // 主题切换重绘（canvas 取 --muted 等主题色，需跟着变）
  themeObserver = new MutationObserver(() => drawHist(lastHist))
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
  window.addEventListener('resize', onResize)
})
onUnmounted(() => {
  if (themeObserver) themeObserver.disconnect()
  window.removeEventListener('resize', onResize)
  if (resizeTimer) window.clearTimeout(resizeTimer)
})
</script>

<template>
  <PanelHero
    icon="history"
    title="历史趋势"
    sub="多维度趋势（自适应时间范围）"
    :stats="heroStats"
    :last-update="lastUpdate"
    :busy="busy"
    @refresh="loadHistory"
  />

  <div class="seg" style="margin-bottom: 8px; flex-wrap: wrap">
    <button
      v-for="m in METRIC_TABS"
      :key="m.id"
      class="seg-btn"
      :class="{ active: histMetric === m.id }"
      @click="pickMetric(m.id)"
    >
      {{ m.label }}
    </button>
  </div>

  <div style="display: flex; justify-content: flex-end; margin-bottom: 6px">
    <button class="btn btn-primary" style="font-size: 13px; padding: 6px 14px" @click="reportOpen = true">
      生成分析报告
    </button>
  </div>

  <div class="section-title">{{ sectionTitle }}</div>

  <div class="card">
    <div class="chart-head">
      <div class="seg">
        <button
          v-for="r in RANGE_TABS"
          :key="r.id"
          class="seg-btn"
          :class="{ active: histRange === r.id }"
          @click="pickRange(r.id)"
        >
          {{ r.label }}
        </button>
      </div>
      <div v-if="histMetric === 'temp'" class="thr-ctl">
        <span class="thr-label">红线</span>
        <input
          class="thr-input"
          type="number"
          min="30"
          max="120"
          step="1"
          v-model.number="tempThreshold"
          @change="setTempThreshold(tempThreshold)"
        />
        <span class="thr-unit">°C</span>
      </div>
    </div>
    <canvas ref="canvasEl" style="width: 100%; height: 170px; margin-top: 10px; display: block" />
    <div v-if="legendHtml" style="font-size: 12px; color: var(--muted); margin-top: 6px" v-html="legendHtml" />
  </div>

  <div class="modal-overlay" :class="{ show: reportOpen }">
    <div class="modal-box">
      <div class="modal-title">生成历史趋势分析报告</div>
      <div class="modal-body">
        <div style="margin-bottom: 14px">
          <div style="font-size: 13px; color: var(--text-3); margin-bottom: 6px">时间范围</div>
          <div class="seg">
            <button
              v-for="r in RPT_RANGES"
              :key="r.id"
              class="seg-btn"
              :class="{ active: rptRange === r.id }"
              @click="rptRange = r.id"
            >
              {{ r.label }}
            </button>
          </div>
        </div>
        <div>
          <div style="font-size: 13px; color: var(--text-3); margin-bottom: 6px">导出格式（三选一）</div>
          <div class="seg">
            <button
              v-for="f in RPT_FORMATS"
              :key="f.id"
              class="seg-btn"
              :class="{ active: rptFmt === f.id }"
              @click="rptFmt = f.id"
            >
              {{ f.label }}
            </button>
          </div>
          <div style="font-size: 12px; color: var(--text-3); margin-top: 8px; line-height: 1.6" v-html="rptHint" />
        </div>
        <div style="font-size: 12px; color: var(--text-3); margin-top: 10px; min-height: 16px">{{ reportStatus }}</div>
      </div>
      <div class="modal-actions">
        <button class="btn" @click="closeReport">取消</button>
        <button class="btn btn-primary" @click="genReport(rptRange, rptFmt)">生成并下载</button>
      </div>
    </div>
  </div>
</template>
