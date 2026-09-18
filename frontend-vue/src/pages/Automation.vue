<script setup lang="ts">
/**
 * 控制与自动化（automation）—— 复刻旧页 renderAutomation / fillAutomationConfig /
 * saveAutomationConfig / testNotify / exportReport / showRunLog。
 *
 * ⚠️ 本页是阶段 3 的**第一个写操作页**，两处 POST 后端都带 @require_admin()：
 *   - 保存配置  POST /api/alerts/config
 *   - 测试通知  POST /api/alerts/test
 * 读接口：GET /api/alerts（配置 + 活动告警，告警每 15s 刷）、GET /api/report（报告 / 运行日志）。
 *
 * 与旧页的一处差异（有意为之）：旧页运行日志的分级类名写成 `lv-` + toLowerCase()，
 * 即 ERROR → `lv-error`，而 CSS 只有 `.lv-err` / `.lv-warn` —— 错误行从来没被标红过。
 * 本页改用与 CSS 对齐的 `lv-err` / `lv-warn` / `lv-info`，错误行这才真的高亮。
 */
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { apiFetch } from '../lib/api'
import AppIcon from '../components/AppIcon.vue'
import PanelHero from '../components/PanelHero.vue'
import { pageCacheGet, pageCacheSet } from '../lib/pageCache'

type Alert = { title?: string; detail?: string; level?: string }
type LogLvl = 'ERROR' | 'WARN' | 'INFO'
type LogEntry = {
  ts: string
  lvl: LogLvl
  txt: string
  raw: string
  /** 访问日志专用列：请求（方法 + 路径，已去掉缓存参数与协议尾巴） */
  req?: string
  /** HTTP 状态码（如 200） */
  status?: string
  /** 响应大小（人性化格式，如 2.8 KB） */
  size?: string
}

const LEVELS = [
  { id: 'danger', label: '严重 (danger)' },
  { id: 'warn', label: '警告 (warn)' },
  { id: 'info', label: '信息 (info)' },
] as const

const CHANNELS = [
  { id: 'system', label: '面板日志' },
  { id: 'telegram', label: 'Telegram' },
  { id: 'bark', label: 'Bark' },
  { id: 'email', label: '邮件' },
] as const

/* ---------------- 配置 / 告警 ---------------- */

const cfg = reactive({
  enabled: false,
  disk_health: false,
  memory_max: 90 as number | string,
  temp: { enabled: true, cpu_max: 85 as number | string, mb_max: 75 as number | string, disk_max: 60 as number | string },
  channels: {
    system: true,
    telegram: { enabled: false, bot_token: '', chat_id: '' },
    bark: { enabled: false, url: '' },
    email: { enabled: false, smtp_host: '', smtp_port: 465 as number | string, user: '', pass: '', to: '' },
  },
  level_channels: { danger: [] as string[], warn: [] as string[], info: [] as string[] },
})

const alerts = ref<Alert[]>([])
const busy = ref(false)
const lastUpdate = ref('')
const saveState = ref('')
const saveOk = ref(false)
const exportState = ref('')

let alertTimer = 0

function fillConfig(c: Record<string, any>): void {
  const src: Record<string, any> = c || {}
  cfg.enabled = !!src.enabled
  cfg.disk_health = !!src.disk_health
  cfg.memory_max = src.memory_max != null ? src.memory_max : 90

  const t = src.temp || {}
  cfg.temp.enabled = t.enabled !== false
  cfg.temp.cpu_max = t.cpu_max != null ? t.cpu_max : 85
  cfg.temp.mb_max = t.mb_max != null ? t.mb_max : 75
  cfg.temp.disk_max = t.disk_max != null ? t.disk_max : 60

  const ch = src.channels || {}
  cfg.channels.system = ch.system !== false
  const tg = ch.telegram || {}
  cfg.channels.telegram.enabled = !!tg.enabled
  cfg.channels.telegram.bot_token = tg.bot_token || ''
  cfg.channels.telegram.chat_id = tg.chat_id || ''
  const bk = ch.bark || {}
  cfg.channels.bark.enabled = !!bk.enabled
  cfg.channels.bark.url = bk.url || ''
  const em = ch.email || {}
  cfg.channels.email.enabled = !!em.enabled
  cfg.channels.email.smtp_host = em.smtp_host || ''
  cfg.channels.email.smtp_port = em.smtp_port != null ? em.smtp_port : 465
  cfg.channels.email.user = em.user || ''
  cfg.channels.email.pass = em.pass || ''
  cfg.channels.email.to = em.to || ''

  const lc = src.level_channels || {}
  for (const lv of LEVELS) {
    cfg.level_channels[lv.id] = Array.isArray(lc[lv.id]) ? lc[lv.id].filter((x: string) => CHANNELS.some(cc => cc.id === x)) : []
  }
}

function setLastUpdate(at?: string): void {
  if (at) lastUpdate.value = '更新于 ' + at
}

async function loadConfig(): Promise<void> {
  // 切页签回来先上缓存秒开（表单值随后会被服务器数据覆盖，未保存的编辑不依赖缓存）
  if (!alerts.value.length) {
    const cached = pageCacheGet<{ config: Record<string, any>; alerts: Alert[]; evaluated_at?: string }>('automation')
    if (cached) {
      fillConfig(cached.config || {})
      alerts.value = cached.alerts || []
      setLastUpdate(cached.evaluated_at)
    }
  }
  // 有内容就不转圈：后台静默拉最新
  if (!alerts.value.length) busy.value = true
  try {
    const r = await apiFetch('/api/alerts?_=' + Date.now(), 30000)
    const j = await r.json()
    fillConfig(j.config || {})
    alerts.value = j.alerts || []
    setLastUpdate(j.evaluated_at)
    pageCacheSet('automation', { config: j.config || {}, alerts: j.alerts || [], evaluated_at: j.evaluated_at })
  } catch {
    /* 与旧页一致：静默失败，不打断页面 */
  }
  busy.value = false
}

/** 15s 只刷「活动告警」，不碰表单 —— 否则会把用户正在编辑的配置冲掉（旧页同口径） */
async function refreshAlerts(): Promise<void> {
  try {
    const r = await apiFetch('/api/alerts?_=' + Date.now(), 30000)
    const j = await r.json()
    alerts.value = j.alerts || []
    setLastUpdate(j.evaluated_at)
  } catch {
    /* 忽略 */
  }
}

function lvlHas(lvl: string, ch: string): boolean {
  return (cfg.level_channels as Record<string, string[]>)[lvl].includes(ch)
}
function lvlToggle(lvl: string, ch: string, on: boolean): void {
  const arr = (cfg.level_channels as Record<string, string[]>)[lvl]
  const i = arr.indexOf(ch)
  if (on && i < 0) arr.push(ch)
  if (!on && i >= 0) arr.splice(i, 1)
}
function onLvlChange(lvl: string, ch: string, ev: Event): void {
  const el = ev.target as HTMLInputElement
  lvlToggle(lvl, ch, !!el.checked)
}

/* ---------------- 保存 / 测试通知（写操作） ---------------- */

function num(v: number | string, d: number): number {
  const n = parseFloat(String(v))
  return Number.isFinite(n) ? n : d
}
function int(v: number | string, d: number): number {
  const n = parseInt(String(v), 10)
  return Number.isFinite(n) ? n : d
}

function buildPayload(): Record<string, unknown> {
  return {
    enabled: cfg.enabled,
    disk_health: cfg.disk_health,
    memory_max: num(cfg.memory_max, 90),
    temp: {
      enabled: cfg.temp.enabled,
      cpu_max: num(cfg.temp.cpu_max, 85),
      mb_max: num(cfg.temp.mb_max, 75),
      disk_max: num(cfg.temp.disk_max, 60),
    },
    channels: {
      system: cfg.channels.system,
      telegram: {
        enabled: cfg.channels.telegram.enabled,
        bot_token: cfg.channels.telegram.bot_token,
        chat_id: cfg.channels.telegram.chat_id,
      },
      bark: { enabled: cfg.channels.bark.enabled, url: cfg.channels.bark.url },
      email: {
        enabled: cfg.channels.email.enabled,
        smtp_host: cfg.channels.email.smtp_host,
        smtp_port: int(cfg.channels.email.smtp_port, 465),
        user: cfg.channels.email.user,
        pass: cfg.channels.email.pass,
        to: cfg.channels.email.to,
      },
    },
    level_channels: {
      danger: cfg.level_channels.danger.slice(),
      warn: cfg.level_channels.warn.slice(),
      info: cfg.level_channels.info.slice(),
    },
  }
}

async function saveConfig(): Promise<void> {
  saveState.value = '保存中…'
  saveOk.value = false
  try {
    const r = await apiFetch('/api/alerts/config', 30000, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildPayload()),
    })
    const j = await r.json().catch(() => null)
    if (j && j.ok) {
      saveState.value = ' 已保存'
      saveOk.value = true
      await refreshAlerts()
    } else {
      saveState.value = '失败：' + ((j && j.error) || 'HTTP ' + r.status)
      saveOk.value = false
    }
  } catch {
    saveState.value = '请求失败'
    saveOk.value = false
  }
}

async function testNotify(): Promise<void> {
  saveState.value = '发送中…'
  saveOk.value = false
  try {
    // SMTP / Bark 走外网可能较慢，给足超时
    const r = await apiFetch('/api/alerts/test', 60000, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    })
    const j = await r.json().catch(() => null)
    saveState.value = '已尝试（' + JSON.stringify((j && j.results) || {}) + '）'
    saveOk.value = true
  } catch {
    saveState.value = '请求失败'
    saveOk.value = false
  }
}

/* ---------------- 一键健康报告 / 运行日志 ---------------- */

function downloadBlob(filename: string, content: string, mime: string): void {
  const blob = new Blob([content], { type: mime + ';charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

async function exportReport(fmt: 'json' | 'html'): Promise<void> {
  exportState.value = '导出中…'
  try {
    const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
    const url = '/api/report?format=' + fmt + (redactExport.value ? '&redact=1' : '') + '&_=' + Date.now()
    const r = await apiFetch(url, 60000)
    if (fmt === 'json') {
      const j = await r.json()
      downloadBlob('nasdash-report-' + ts + '.json', JSON.stringify(j, null, 2), 'application/json')
    } else {
      const html = await r.text()
      downloadBlob('nasdash-report-' + ts + '.html', html, 'text/html')
    }
    exportState.value = '已导出'
  } catch (e) {
    exportState.value = '导出失败'
    alert('导出失败：' + e)
  }
}

/** 导出报告时是否脱敏（内网 IP / MAC / 序列号 / 主机名 / 云盘账号） */
const redactExport = ref(false)

const logOpen = ref(false)
const logLoading = ref(false)
const logLoadErr = ref('')
const logEntries = ref<LogEntry[]>([])
const logRawText = ref('')
/** 本次拉取日志的时间（弹窗标题右侧显示，方便确认看的是什么时候的日志） */
const logFetchedAt = ref('')
/** 错误历史圈（后端内存长期保留，不被 60 行日志尾部刷掉） */
const errRing = ref<{ ts: string; text: string; n: number }[]>([])

const nE = computed(() => logEntries.value.filter(e => e.lvl === 'ERROR').length)
const nW = computed(() => logEntries.value.filter(e => e.lvl === 'WARN').length)
const errText = computed(() => logEntries.value.filter(e => e.lvl === 'ERROR').map(e => e.raw).join('\n'))
const warnText = computed(() => logEntries.value.filter(e => e.lvl === 'WARN').map(e => e.raw).join('\n'))

function lvlCls(lvl: LogLvl): string {
  // 与 panel.css 的 .log-line.lv-err / .lv-warn 对齐（旧页误写成 lv-error，见文件头注释）
  return lvl === 'ERROR' ? 'err' : lvl === 'WARN' ? 'warn' : 'info'
}

/** 304 -> OK 绿；3xx/4xx -> 警告黄；5xx -> 错误红 */
function statusLvl(code: number): LogLvl {
  if (code >= 500) return 'ERROR'
  if (code >= 300) return 'WARN'
  return 'INFO'
}

/** 状态码翻译成普通用户能懂的话（悬停可看原始状态码，给开发者排查用） */
function statusText(e: LogEntry): string {
  if (!e.status) return e.lvl === 'ERROR' ? '错误' : e.lvl === 'WARN' ? '警告' : '—'
  const code = parseInt(e.status, 10)
  if (code >= 500) return '服务器出错'
  if (code >= 400) return '请求异常'
  if (code >= 300) return '重定向'
  return '成功'
}

function fmtBytes(n: number): string {
  if (!isFinite(n)) return '—'
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(2) + ' MB'
}

const MONTH_NUM: Record<string, string> = {
  Jan: '01', Feb: '02', Mar: '03', Apr: '04', May: '05', Jun: '06',
  Jul: '07', Aug: '08', Sep: '09', Oct: '10', Nov: '11', Dec: '12',
}

/** 12/Sep/2026 00:53:01 这类格式统一成 09-12 00:53:01（年份极少排障用不到，省宽度） */
function normTime(dd: string, mon: string, hhmmss: string): string {
  const m = MONTH_NUM[mon] || mon
  return `${m}-${dd} ${hhmmss}`
}

function parseLog(text: string): LogEntry[] {
  const out: LogEntry[] = []
  for (const line of String(text || '').split('\n')) {
    const s = line.replace(/^\s+|\s+$/g, '')
    if (!s) continue

    // 访问日志（nginx 风格）：IP - - [12/Sep/2026 00:53:01] "GET /api/x HTTP/1.1" 200 2907
    // IP 是访问来源（面板经本机网关转发，恒为本机），对排障无意义，不展示；
    // 路径里的 ?_=时间戳 是前端防缓存的随机参数，也一并去掉
    const am = s.match(/^\S+ \S+ \S+ \[(\d{2})\/(\w{3})\/\d{4} (\d{2}:\d{2}:\d{2})\] "([A-Z]+) (\S+)(?: [^"]*)?" (\d{3}) (\d+|-)$/)
    if (am) {
      const code = parseInt(am[6], 10)
      const path = am[5].replace(/[?&]_=\d+/g, '').replace(/\?$/, '')
      out.push({
        ts: normTime(am[1], am[2], am[3]),
        lvl: statusLvl(code),
        txt: s,
        raw: s,
        req: am[4] + ' ' + path,
        status: String(code),
        size: am[7] === '-' ? '—' : fmtBytes(parseInt(am[7], 10)),
      })
      continue
    }

    // 应用日志：2026-09-12 00:53:01 消息内容
    const m = s.match(/^\[?\d{4}-(\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\]?[ ,]*(.*)$/)
    const ts = m ? `${m[1]} ${m[2]}` : ''
    const txt = m ? m[3] : s
    const low = txt.toLowerCase()
    let lvl: LogLvl = 'INFO'
    if (
      low.indexOf('traceback') >= 0 || low.indexOf('error') >= 0 || low.indexOf('exception') >= 0 ||
      low.indexOf('failed') >= 0 || txt.indexOf('失败') >= 0 || txt.indexOf('错误') >= 0
    ) {
      lvl = 'ERROR'
    } else if (low.indexOf('warn') >= 0 || low.indexOf('warning') >= 0 || txt.indexOf('警告') >= 0) {
      lvl = 'WARN'
    }
    out.push({ ts, lvl, txt, raw: s })
  }
  const order: Record<LogLvl, number> = { ERROR: 0, WARN: 1, INFO: 2 }
  return out.sort((a, b) => order[a.lvl] - order[b.lvl])
}

/** 日志弹窗自动刷新定时器：打开期间每 10 秒静默拉一次最新日志 */
let logTimer: number | undefined

async function loadLog(silent = false): Promise<void> {
  if (!silent) {
    logLoading.value = true
    logLoadErr.value = ''
    logEntries.value = []
    logRawText.value = ''
    errRing.value = []
    logFetchedAt.value = ''
  }
  try {
    // 从健康报告 JSON 取 log_tail，避免重复后端实现（旧页同做法）
    const r = await apiFetch('/api/report?format=json&_=' + Date.now(), 60000)
    const j = await r.json()
    const text = j.log_tail || ''
    logRawText.value = text
    logEntries.value = parseLog(text)
    errRing.value = j.error_ring || []
    logFetchedAt.value = new Date().toLocaleTimeString('zh-CN')
  } catch (e) {
    if (!silent) logLoadErr.value = '加载日志失败：' + e
    // 静默刷新失败不打扰用户，保留上一次内容
  }
  logLoading.value = false
}

async function showRunLog(): Promise<void> {
  logOpen.value = true
  await loadLog()
  stopLogTimer()
  logTimer = window.setInterval(() => {
    if (logOpen.value && !logLoading.value) loadLog(true)
  }, 10000)
}

function stopLogTimer(): void {
  if (logTimer) {
    window.clearInterval(logTimer)
    logTimer = undefined
  }
}

function closeRunLog(): void {
  logOpen.value = false
  stopLogTimer()
}

/** 错误记录二级弹窗 */
const ringOpen = ref(false)
/** 复制用的纯文本版（[时间] 内容（连续出现 N 次）） */
const ringRawText = computed(() =>
  errRing.value.map(e => '[' + e.ts + '] ' + e.text + (e.n > 1 ? '（连续出现 ' + e.n + ' 次）' : '')).join('\n'),
)
/** 清除按钮二次确认态：第一下点亮（3 秒内再点才真清） */
const ringConfirm = ref(false)
let ringConfirmTimer = 0

function showRing(): void {
  ringOpen.value = true
}

function closeRing(): void {
  ringOpen.value = false
  ringConfirm.value = false
  window.clearTimeout(ringConfirmTimer)
}

async function clearErrRing(): Promise<void> {
  // 两步确认：第一下只亮红色警示，3 秒内再点才真清（防手滑，清除不可恢复）
  if (!ringConfirm.value) {
    ringConfirm.value = true
    window.clearTimeout(ringConfirmTimer)
    ringConfirmTimer = window.setTimeout(() => (ringConfirm.value = false), 3000)
    return
  }
  ringConfirm.value = false
  window.clearTimeout(ringConfirmTimer)
  try {
    await apiFetch('/api/errors/clear', 10000, { method: 'POST' })
  } catch {
    /* 清除失败不弹错，下次自动刷新会再同步 */
  }
  errRing.value = []
  toastMsg.value = '错误记录已清除'
  window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => (toastMsg.value = ''), 2000)
}

function onKey(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    if (ringOpen.value) closeRing()
    else if (logOpen.value) closeRunLog()
  }
}

/* ---------------- toast（复制反馈） ---------------- */

const toastMsg = ref('')
let toastTimer = 0
function toast(msg: string): void {
  toastMsg.value = msg
  window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => { toastMsg.value = '' }, 2600)
}

function copyText(txt: string, label: string): void {
  const short = label.replace('复制', '')
  if (!txt) {
    toast('没有可复制的' + short)
    return
  }
  const done = (): void => toast('已复制' + short)
  const fallback = (): void => {
    const ta = document.createElement('textarea')
    ta.value = txt
    document.body.appendChild(ta)
    ta.select()
    try {
      document.execCommand('copy')
      done()
    } catch {
      window.prompt('请手动复制：', txt)
    }
    ta.remove()
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(txt).then(done).catch(fallback)
  } else {
    fallback()
  }
}

/* ---------------- 生命周期 ---------------- */

onMounted(async () => {
  document.addEventListener('keydown', onKey)
  await loadConfig()
  alertTimer = window.setInterval(refreshAlerts, 15000)
})

onUnmounted(() => {
  document.removeEventListener('keydown', onKey)
  if (alertTimer) window.clearInterval(alertTimer)
  window.clearTimeout(toastTimer)
  stopLogTimer()
})
</script>

<template>
  <div>
    <PanelHero
      icon="automation"
      title="控制与自动化"
      sub="温度 / 硬盘健康告警、外部通知与一键健康报告"
      :last-update="lastUpdate"
      :busy="busy"
      @refresh="loadConfig"
    />

    <!-- 活动告警 -->
    <div class="auto-card">
      <h3><AppIcon name="search" /> 活动告警 <span class="badge b-info">每 15 秒刷新</span></h3>
      <ul class="auto-alerts">
        <li v-if="!alerts.length" class="ok"><AppIcon name="check" />当前无活动告警</li>
        <li v-for="(a, i) in alerts" :key="i" :class="a.level === 'danger' ? 'danger' : 'warn'">
          <b>{{ a.title }}</b> — {{ a.detail }}
        </li>
      </ul>
    </div>

    <!-- 告警规则 -->
    <div class="auto-card">
      <h3><AppIcon name="gear" /> 告警规则</h3>
      <label class="dt-toggle"><input v-model="cfg.enabled" type="checkbox"> 启用告警监控</label>
      <div class="auto-grid" style="margin: 12px 0">
        <div class="auto-field">
          <label>CPU 最高温度</label>
          <input v-model.number="cfg.temp.cpu_max" type="number" min="40" max="100" />
          <span class="unit">°C</span>
        </div>
        <div class="auto-field">
          <label>主板/芯片组最高</label>
          <input v-model.number="cfg.temp.mb_max" type="number" min="40" max="100" />
          <span class="unit">°C</span>
        </div>
        <div class="auto-field">
          <label>硬盘最高温度</label>
          <input v-model.number="cfg.temp.disk_max" type="number" min="30" max="90" />
          <span class="unit">°C</span>
        </div>
        <div class="auto-field">
          <label>内存占用上限</label>
          <input v-model.number="cfg.memory_max" type="number" min="50" max="100" />
          <span class="unit">%</span>
        </div>
      </div>
      <label class="dt-toggle"><input v-model="cfg.temp.enabled" type="checkbox"> 启用温度告警</label>
      <label class="dt-toggle"><input v-model="cfg.disk_health" type="checkbox"> 启用硬盘健康告警（SMART pre-fail / 异常）</label>
    </div>

    <!-- 通知渠道 -->
    <div class="auto-card">
      <h3><AppIcon name="wifi" /> 通知渠道</h3>
      <label class="dt-toggle" style="margin-bottom: 12px">
        <input v-model="cfg.channels.system" type="checkbox"> 面板内提示（始终记录到应用通知日志）
      </label>

      <div class="chan">
        <h4><AppIcon name="send" /> Telegram</h4>
        <label class="dt-toggle"><input v-model="cfg.channels.telegram.enabled" type="checkbox"> 启用</label>
        <div class="chan-grid">
          <div class="auto-field">
            <label>Bot Token</label>
            <input v-model="cfg.channels.telegram.bot_token" type="text" placeholder="123456:ABCdef..." />
          </div>
          <div class="auto-field">
            <label>Chat ID</label>
            <input v-model="cfg.channels.telegram.chat_id" type="text" placeholder="123456789" />
          </div>
        </div>
      </div>

      <div class="chan">
        <h4><AppIcon name="bell" /> Bark</h4>
        <label class="dt-toggle"><input v-model="cfg.channels.bark.enabled" type="checkbox"> 启用</label>
        <div class="chan-grid">
          <div class="auto-field">
            <label>推送地址</label>
            <input v-model="cfg.channels.bark.url" type="text" placeholder="https://api.day.app/你的Key" />
          </div>
        </div>
      </div>

      <div class="chan">
        <h4><AppIcon name="mail" /> 邮件 (SMTP)</h4>
        <label class="dt-toggle"><input v-model="cfg.channels.email.enabled" type="checkbox"> 启用</label>
        <div class="chan-grid">
          <div class="auto-field"><label>SMTP 服务器</label><input v-model="cfg.channels.email.smtp_host" type="text" placeholder="smtp.qq.com" /></div>
          <div class="auto-field"><label>端口</label><input v-model.number="cfg.channels.email.smtp_port" type="number" /></div>
          <div class="auto-field"><label>账号</label><input v-model="cfg.channels.email.user" type="text" /></div>
          <div class="auto-field"><label>密码/授权码</label><input v-model="cfg.channels.email.pass" type="password" /></div>
          <div class="auto-field"><label>接收地址</label><input v-model="cfg.channels.email.to" type="text" /></div>
        </div>
      </div>

      <div class="chan" style="grid-column: 1/-1">
        <h4><AppIcon name="bell" /> 按严重级别选通知渠道</h4>
        <div class="lvl-matrix">
          <div class="lvl-row lvl-head">
            <span>级别</span>
            <span v-for="c in CHANNELS" :key="c.id">{{ c.label }}</span>
          </div>
          <div v-for="l in LEVELS" :key="l.id" class="lvl-row">
            <span>{{ l.label }}</span>
            <input
              v-for="c in CHANNELS"
              :key="c.id"
              type="checkbox"
              :checked="lvlHas(l.id, c.id)"
              @change="onLvlChange(l.id, c.id, $event)"
            />
          </div>
        </div>
        <div style="font-size: 12px; color: var(--muted); margin-top: 6px">
          严重事件默认通知所有渠道；可单独关闭某级别的某个渠道（面板日志始终记录，便于事后排查）。
        </div>
      </div>

      <div class="auto-actions">
        <button class="btn-mini" @click="saveConfig"><AppIcon name="save" /> 保存配置</button>
        <button class="btn-mini" @click="testNotify"><AppIcon name="upload" /> 发送测试通知</button>
        <span class="dt-save-state" :class="{ ok: saveOk }">
          <AppIcon v-if="saveOk" name="check" />{{ saveState }}
        </span>
      </div>
    </div>

    <!-- 一键健康报告 -->
    <div class="auto-card">
      <h3><AppIcon name="report" /> 一键健康报告</h3>
      <div style="font-size: 13px; color: var(--muted); margin-bottom: 12px">
        汇总全部硬件状态与活动告警，导出供排查留档。JSON 可直接查看，HTML 为排版报告（内含融合分级的运行日志）。<b>遇到 bug 或异常</b>时，点「查看运行日志」在弹窗中查看错误/警告（置顶着色），确认后再决定要不要复制贴给开发者。
      </div>
      <label class="dt-toggle" style="display: flex; align-items: flex-start; gap: 8px; margin-bottom: 10px">
        <input v-model="redactExport" type="checkbox" style="margin-top: 2px">
        <span>脱敏导出（把内网 IP、MAC、设备序列号、主机名、云盘账号打码，报告顶部会标「已脱敏」）——贴论坛 / 群里前建议勾上</span>
      </label>
      <div class="auto-actions">
        <button class="btn-mini" @click="exportReport('json')">导出 JSON</button>
        <button class="btn-mini" @click="exportReport('html')">导出 HTML 报告</button>
        <button class="btn-mini" @click="showRunLog"><AppIcon name="clipboard" /> 查看运行日志</button>
        <span class="dt-save-state" :class="{ ok: exportState === '已导出' }">{{ exportState }}</span>
      </div>
    </div>

    <!-- 运行日志弹窗 -->
    <div class="modal-overlay modal-wide" :class="{ show: logOpen }" @click.self="closeRunLog">
      <div class="modal-box">
        <div class="modal-title log-title-row">
          <span>运行日志（诊断）</span>
          <span v-if="logFetchedAt" class="log-fetched-at">自动更新于 {{ logFetchedAt }}（每 10 秒）</span>
        </div>
        <div class="modal-body">
          <div v-if="logLoading" class="log-empty">加载中…</div>
          <div v-else-if="logLoadErr" class="log-empty">{{ logLoadErr }}</div>
          <div v-else-if="!logEntries.length" class="log-empty">（暂无运行日志）</div>
          <template v-else>
            <div class="logstat">
              运行日志共 <b>{{ logEntries.length }}</b> 行：<b class="danger">错误 {{ nE }}</b> ·
              <b class="warn">警告 {{ nW }}</b> · 常规 {{ logEntries.length - nE - nW }}（错误/警告置顶着色，方便先看 bug）
            </div>
            <div class="log-table-wrap">
              <table class="log-table">
                <thead>
                  <tr>
                    <th class="c-ln">#</th>
                    <th class="c-ts">时间</th>
                    <th>请求 / 内容</th>
                    <th class="c-st">状态</th>
                    <th class="c-sz">大小</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(e, i) in logEntries" :key="i" :class="'lv-' + lvlCls(e.lvl)">
                    <td class="c-ln">{{ i + 1 }}</td>
                    <td class="c-ts">{{ e.ts || '—' }}</td>
                    <td class="c-req">{{ e.req || e.txt }}</td>
                    <td class="c-st">
                      <span
                        :class="e.lvl === 'ERROR' ? 'danger' : e.lvl === 'WARN' ? 'warn' : 'muted'"
                        :title="e.status ? 'HTTP ' + e.status : '程序日志行（非网页请求），没有响应大小'"
                      >{{ statusText(e) }}</span>
                    </td>
                    <!-- 程序日志行没有"响应大小"概念，此格留空（不画"—"占位），级别文字对齐在"状态"表头下 -->
                    <td class="c-sz">{{ e.size || '' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </template>
          <div class="log-footnote">
            说明：运行日志是滚动窗口，最多保留最近 <b>60 行</b>，更早的会被新日志自动顶掉；
            真正出错的记录不受此限制——会长期保留（最近 <b>100</b> 条）。有错误记录时，下方才会出现「<b>查看错误记录</b>」按钮，没有就不显示；
            表格里标黄的「请求异常」属普通请求失败，不进错误记录。
          </div>
        </div>
        <div class="modal-actions">
          <button
            v-if="errRing.length"
            class="btn"
            title="与上方「警告」数口径不同：错误记录只收真正出错的（服务器错误 / 程序异常），长期保留最多 100 条；普通请求异常（如 404）只在上表黄色标注，不进这里"
            @click="showRing"
          >查看错误记录（{{ errRing.length }}）</button>
          <button v-if="nE > 0" class="btn" @click="copyText(errText, '复制错误日志')">复制错误日志</button>
          <button v-if="nW > 0" class="btn" @click="copyText(warnText, '复制警告日志')">复制警告日志</button>
          <button v-if="logEntries.length" class="btn btn-primary" @click="copyText(logRawText, '复制全部日志')">复制全部日志</button>
          <button class="btn" @click="closeRunLog">关闭</button>
        </div>
      </div>
    </div>

    <!-- 错误记录弹窗（二级）：后端内存长期保留，不受 60 行日志尾部限制 -->
    <div class="modal-overlay" :class="{ show: ringOpen }" @click.self="closeRing">
      <div class="modal-box ring-box">
        <div class="modal-title">错误记录（长期保留，最多 100 条）</div>
        <div class="modal-body">
          <div v-if="!errRing.length" class="log-empty">（当前没有错误记录）</div>
          <template v-else>
            <div class="logstat">
              共 <b class="danger">{{ errRing.length }}</b> 条（连续重复已合并计数）；这些记录不会被运行日志刷掉，应用重启后清零。
            </div>
            <div class="log-table-wrap ring-table-wrap">
              <table class="log-table ring-table">
                <thead>
                  <tr>
                    <th class="c-ln">#</th>
                    <th class="c-ts">捕获时间</th>
                    <th>错误内容</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(e, i) in errRing" :key="i" class="lv-err">
                    <td class="c-ln">{{ i + 1 }}</td>
                    <td class="c-ts">{{ e.ts }}</td>
                    <td class="c-txt">{{ e.text }}<span v-if="e.n > 1" class="xN">（连续出现 {{ e.n }} 次）</span></td>
                  </tr>
                </tbody>
              </table>
            </div>
          </template>
        </div>
        <div class="modal-actions">
          <button v-if="errRing.length" class="btn" @click="copyText(ringRawText, '复制错误记录')">复制错误记录</button>
          <button
            v-if="errRing.length"
            class="btn"
            :class="{ 'btn-danger': ringConfirm }"
            @click="clearErrRing"
          >{{ ringConfirm ? '再点一次确认清除（不可恢复）' : '清除错误记录' }}</button>
          <button class="btn" @click="closeRing">关闭</button>
        </div>
      </div>
    </div>

    <div v-if="toastMsg" class="au-toast">{{ toastMsg }}</div>
  </div>
</template>

<style scoped>
.au-toast {
  position: fixed;
  left: 50%;
  bottom: 32px;
  transform: translateX(-50%);
  background: rgba(20, 30, 48, 0.92);
  color: #fff;
  padding: 10px 18px;
  border-radius: 10px;
  font-size: 13px;
  z-index: 9999;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.25);
}
/* 日志弹窗标题行：左侧标题、右侧本次拉取时间 */
.log-title-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}
.log-fetched-at {
  font-size: 12px;
  font-weight: 400;
  color: var(--muted, #8a8f98);
  font-variant-numeric: tabular-nums;
}
/* 清除按钮二次确认态 */
.btn-danger {
  border-color: var(--danger, #f55050);
  color: #fff;
  background: var(--danger, #f55050);
}
/* 错误记录表格（复用 .log-table 样式，整行红色） */
/* 弹窗加宽到与日志弹窗协调（默认 420px 太窄，内容列挤成细长条） */
.ring-box {
  width: 100%;
  max-width: min(720px, calc(100vw - 32px));
}
.ring-table-wrap {
  max-height: 46vh;
}
.ring-table .c-txt {
  color: var(--danger, #f55050);
  word-break: break-all;
  white-space: pre-wrap;
}
/* 记录行垂直居中：单条短记录顶着上沿、下面拖一大块红底很难看。
   注意：下面的 .log-table tbody td 也设了 vertical-align:top 且写在后面，
   同权重时后来者赢，这里必须抬高权重（双类名）才能真正生效 */
.log-table.ring-table tbody td {
  vertical-align: middle;
  line-height: 1.6;
}
.ring-table .xN {
  margin-left: 8px;
  color: var(--muted, #8a8f98);
  font-size: 11.5px;
  white-space: nowrap;
}
/* 弹窗底部说明文字 */
.log-footnote {
  margin-top: 10px;
  font-size: 12px;
  color: var(--muted, #8a8f98);
  line-height: 1.6;
}
.log-footnote b {
  color: var(--text, #d5dae2);
  font-weight: 600;
}
/* 运行日志表格 */
.log-table-wrap {
  border: 1px solid var(--border, rgba(128, 138, 155, 0.25));
  border-radius: 10px;
  overflow: auto;
  max-height: 52vh;
}
.log-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.log-table thead th {
  position: sticky;
  top: 0;
  background: var(--card, #1a2232);
  color: var(--muted, #8a8f98);
  text-align: left;
  font-weight: 600;
  font-size: 12px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border, rgba(128, 138, 155, 0.25));
  white-space: nowrap;
  z-index: 1;
}
.log-table tbody td {
  padding: 5px 10px;
  border-bottom: 1px solid var(--border, rgba(128, 138, 155, 0.12));
  vertical-align: top;
  font-variant-numeric: tabular-nums;
}
.log-table tbody tr:last-child td {
  border-bottom: none;
}
.log-table .c-ln {
  color: var(--muted, #8a8f98);
  text-align: right;
  width: 34px;
  user-select: none;
}
.log-table .c-ts {
  white-space: nowrap;
  color: var(--muted, #8a8f98);
}
.log-table .c-req {
  word-break: break-all;
}
.log-table .c-st {
  white-space: nowrap;
  text-align: right;
}
.log-table .c-sz {
  white-space: nowrap;
  text-align: right;
  color: var(--muted, #8a8f98);
}
.log-table tr.lv-err td {
  background: var(--danger-bg, rgba(245, 80, 80, 0.12));
}
.log-table tr.lv-err .c-req {
  color: var(--danger, #f55050);
}
.log-table tr.lv-warn td {
  background: var(--warning-bg, rgba(240, 165, 30, 0.1));
}
.log-table tr.lv-warn .c-req {
  color: var(--warning, #f0a51e);
}
.log-table .muted {
  color: var(--muted, #8a8f98);
}
</style>
