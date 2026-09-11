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

type Alert = { title?: string; detail?: string; level?: string }
type LogLvl = 'ERROR' | 'WARN' | 'INFO'
type LogEntry = { ts: string; lvl: LogLvl; txt: string; raw: string }

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
  busy.value = true
  try {
    const r = await apiFetch('/api/alerts?_=' + Date.now(), 30000)
    const j = await r.json()
    fillConfig(j.config || {})
    alerts.value = j.alerts || []
    setLastUpdate(j.evaluated_at)
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
    const r = await apiFetch('/api/report?format=' + fmt + '&_=' + Date.now(), 60000)
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

const logOpen = ref(false)
const logLoading = ref(false)
const logLoadErr = ref('')
const logEntries = ref<LogEntry[]>([])
const logRawText = ref('')

const nE = computed(() => logEntries.value.filter(e => e.lvl === 'ERROR').length)
const nW = computed(() => logEntries.value.filter(e => e.lvl === 'WARN').length)
const errText = computed(() => logEntries.value.filter(e => e.lvl === 'ERROR').map(e => e.raw).join('\n'))
const warnText = computed(() => logEntries.value.filter(e => e.lvl === 'WARN').map(e => e.raw).join('\n'))

function lvlCls(lvl: LogLvl): string {
  // 与 panel.css 的 .log-line.lv-err / .lv-warn 对齐（旧页误写成 lv-error，见文件头注释）
  return lvl === 'ERROR' ? 'err' : lvl === 'WARN' ? 'warn' : 'info'
}

function parseLog(text: string): LogEntry[] {
  const out: LogEntry[] = []
  for (const line of String(text || '').split('\n')) {
    const s = line.replace(/^\s+|\s+$/g, '')
    if (!s) continue
    const m = s.match(/^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})[ ,]*(.*)$/)
    const ts = m ? m[1] : ''
    const txt = m ? m[2] : s
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

async function showRunLog(): Promise<void> {
  logOpen.value = true
  logLoading.value = true
  logLoadErr.value = ''
  logEntries.value = []
  logRawText.value = ''
  try {
    // 从健康报告 JSON 取 log_tail，避免重复后端实现（旧页同做法）
    const r = await apiFetch('/api/report?format=json&_=' + Date.now(), 60000)
    const j = await r.json()
    const text = j.log_tail || ''
    logRawText.value = text
    logEntries.value = parseLog(text)
  } catch (e) {
    logLoadErr.value = '加载日志失败：' + e
  }
  logLoading.value = false
}

function closeRunLog(): void {
  logOpen.value = false
}

function onKey(e: KeyboardEvent): void {
  if (e.key === 'Escape' && logOpen.value) closeRunLog()
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
        <div class="modal-title">运行日志（诊断）</div>
        <div class="modal-body">
          <div v-if="logLoading" class="log-empty">加载中…</div>
          <div v-else-if="logLoadErr" class="log-empty">{{ logLoadErr }}</div>
          <div v-else-if="!logEntries.length" class="log-empty">（暂无运行日志）</div>
          <template v-else>
            <div class="logstat">
              运行日志共 <b>{{ logEntries.length }}</b> 行：<b class="danger">错误 {{ nE }}</b> ·
              <b class="warn">警告 {{ nW }}</b> · 常规 {{ logEntries.length - nE - nW }}（错误/警告置顶着色，方便先看 bug）
            </div>
            <div class="log-list">
              <div v-for="(e, i) in logEntries" :key="i" class="log-line" :class="'lv-' + lvlCls(e.lvl)">
                <span v-if="e.ts" class="lt">{{ e.ts }}</span>{{ e.txt }}
              </div>
            </div>
          </template>
        </div>
        <div class="modal-actions">
          <button v-if="nE > 0" class="btn" @click="copyText(errText, '复制错误日志')">复制错误日志</button>
          <button v-if="nW > 0" class="btn" @click="copyText(warnText, '复制警告日志')">复制警告日志</button>
          <button v-if="logEntries.length" class="btn btn-primary" @click="copyText(logRawText, '复制全部日志')">复制全部日志</button>
          <button class="btn" @click="closeRunLog">关闭</button>
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
</style>
