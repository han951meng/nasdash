<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { version as vueVersion } from 'vue'
import { resolveTheme } from './main'

/**
 * 阶段 1a 探针页 —— 只干三件事：
 *  ① 证明 Vue 能在这套「飞牛网关 + Flask」环境里正常渲染
 *  ② 证明能拿到真实接口数据（/api/metrics）
 *  ③ 证明暗色/亮色切换正常
 * 不碰任何业务逻辑，坏了也不影响主页面。
 */

// ---------- ① 渲染 ----------
const mountedAt = ref(new Date().toLocaleString('zh-CN'))
const ticks = ref(0)
let tickTimer: number | undefined

// ---------- ② 接口 ----------
interface GpuItem {
  name?: string
  mem_pct?: number
  mem_used?: number
  mem_total?: number
}
interface NetItem {
  name: string
  rx_rate: number
  tx_rate: number
}
interface DiskItem {
  device: string
  busy?: number
  read_rate?: number
  write_rate?: number
}
interface Metrics {
  cpu_usage?: number
  mem_percent?: number
  load?: number[]
  time?: string
  gpu?: GpuItem[]
  net?: NetItem[]
  diskio?: DiskItem[]
}

/**
 * API 基址：不能用裸的 '/api/...'。
 * 实测（158 真机）：
 *  - 页面在 /cgi/ThirdParty/com.dashboard.nasdash/index.cgi/ 时，'/api/metrics' 返回 200；
 *  - 页面在 .../index.cgi/vue/ 时，同样写 '/api/metrics' 会落到飞牛网关 404（返回飞牛自己的 SPA 页）。
 * 所以统一从当前路径里截到 index.cgi 为止，再拼 /api/...，页面挂多深都成立。
 * 实测这样拼出的 .../index.cgi/api/metrics 返回 200 真实 JSON。
 */
const API_BASE = (() => {
  const m = location.pathname.match(/^(.*\/index\.cgi)\/?/)
  return m ? m[1] : ''
})()
const API = API_BASE + '/api/metrics'
const pagePath = location.pathname
const metrics = ref<Metrics | null>(null)
const httpStatus = ref<number | null>(null)
const latencyMs = ref<number | null>(null)
const errorText = ref('')
const pollCount = ref(0)
const lastAt = ref('')
const rawJson = ref('')
const showRaw = ref(false)
let pollTimer: number | undefined

async function fetchMetrics(): Promise<void> {
  const t0 = performance.now()
  try {
    const res = await fetch(API, { credentials: 'include', cache: 'no-store' })
    httpStatus.value = res.status
    const text = await res.text()
    latencyMs.value = Math.round(performance.now() - t0)
    if (!res.ok) {
      errorText.value = `HTTP ${res.status}`
      return
    }
    metrics.value = JSON.parse(text) as Metrics
    rawJson.value = text
    errorText.value = ''
    pollCount.value += 1
    lastAt.value = new Date().toLocaleTimeString('zh-CN')
  } catch (e) {
    latencyMs.value = Math.round(performance.now() - t0)
    errorText.value = e instanceof Error ? e.message : String(e)
  }
}

// ---------- ③ 主题 ----------
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
  // 与主面板一致的循环：浅 → 深 → auto
  const next = savedTheme.value === 'light' ? 'dark' : savedTheme.value === 'dark' ? 'auto' : 'light'
  localStorage.setItem('nasdash_theme', next)
  applyFromStorage()
}

const themeLabel = computed(() => {
  const zh = { light: '浅色', dark: '深色', auto: '跟随系统' } as Record<string, string>
  return `${zh[savedTheme.value] ?? savedTheme.value}（实际 ${appliedTheme.value === 'dark' ? '深色' : '浅色'}）`
})

const statusClass = computed(() => {
  if (errorText.value) return 'bad'
  if (httpStatus.value === 200) return 'ok'
  return 'idle'
})

function fmtBytes(n?: number): string {
  if (n == null) return '—'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024
    i += 1
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${u[i]}`
}

onMounted(() => {
  applyFromStorage()
  void fetchMetrics()
  tickTimer = window.setInterval(() => (ticks.value += 1), 1000)
  pollTimer = window.setInterval(() => void fetchMetrics(), 5000)
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
        <h1>nasdash · Vue 探针</h1>
        <p class="sub">阶段 1a —— 只验证「渲染 / 接口 / 主题」三件事，不影响主面板</p>
      </div>
      <button class="theme-btn" @click="cycleTheme">主题：{{ themeLabel }}</button>
    </header>

    <section class="card">
      <h2>① 渲染</h2>
      <dl>
        <div><dt>Vue 版本</dt><dd>{{ vueVersion }}</dd></div>
        <div><dt>挂载时间</dt><dd>{{ mountedAt }}</dd></div>
        <div><dt>响应式心跳</dt><dd><b class="num">{{ ticks }}</b> 秒（每秒自增，证明 Vue 活着）</dd></div>
        <div><dt>页面地址</dt><dd class="mono">{{ pagePath }}</dd></div>
        <div><dt>请求地址</dt><dd class="mono">{{ API }}</dd></div>
      </dl>
    </section>

    <section class="card">
      <h2>② 接口 <code>/api/metrics</code></h2>
      <div class="row">
        <span class="pill" :class="statusClass">
          {{ errorText ? '失败：' + errorText : httpStatus === 200 ? 'HTTP 200' : '请求中…' }}
        </span>
        <span class="meta">耗时 {{ latencyMs ?? '—' }} ms</span>
        <span class="meta">第 {{ pollCount }} 次采样 · {{ lastAt || '—' }}</span>
        <button class="mini" @click="fetchMetrics">立即刷新</button>
        <button class="mini" @click="showRaw = !showRaw">{{ showRaw ? '收起原始 JSON' : '查看原始 JSON' }}</button>
      </div>

      <dl v-if="metrics">
        <div><dt>CPU 使用率</dt><dd><b class="num">{{ metrics.cpu_usage ?? '—' }}%</b></dd></div>
        <div><dt>内存占用</dt><dd><b class="num">{{ metrics.mem_percent ?? '—' }}%</b></dd></div>
        <div>
          <dt>系统负载</dt>
          <dd class="mono">{{ (metrics.load ?? []).map((n) => n.toFixed(2)).join(' / ') || '—' }}</dd>
        </div>
        <div>
          <dt>网络</dt>
          <dd>
            <template v-if="metrics.net && metrics.net.length">
              <span v-for="n in metrics.net" :key="n.name" class="net-item">
                {{ n.name }}：↓ {{ n.rx_rate }} KB/s · ↑ {{ n.tx_rate }} KB/s
              </span>
            </template>
            <template v-else>—</template>
          </dd>
        </div>
        <div>
          <dt>显卡</dt>
          <dd>
            <template v-if="metrics.gpu && metrics.gpu.length">
              <span v-for="(g, i) in metrics.gpu" :key="i" class="net-item">
                {{ g.name || '（未命名）' }}
                <template v-if="g.mem_total">
                  · 显存 {{ fmtBytes(g.mem_used) }} / {{ fmtBytes(g.mem_total) }}（{{ g.mem_pct }}%）
                </template>
              </span>
            </template>
            <template v-else>—</template>
          </dd>
        </div>
        <div><dt>磁盘 I/O</dt><dd>共 {{ (metrics.diskio ?? []).length }} 块盘 · 采样于 {{ metrics.time || '—' }}</dd></div>
      </dl>
      <p v-else class="sub">等待首次数据…</p>

      <pre v-if="showRaw" class="raw">{{ rawJson }}</pre>
    </section>

    <section class="card">
      <h2>③ 主题</h2>
      <p class="sub">
        当前 <code>data-theme="{{ appliedTheme }}"</code>、本地偏好 <code>nasdash_theme={{ savedTheme }}</code>。
        点右上角按钮在「浅色 → 深色 → 跟随系统」间循环。
      </p>
      <div class="swatches">
        <div class="sw sw-bg">背景 var(--c-bg)</div>
        <div class="sw sw-card">卡片 var(--c-card)</div>
        <div class="sw sw-primary">主色 var(--c-primary)</div>
        <div class="sw sw-ok">成功 var(--c-success)</div>
        <div class="sw sw-warn">警告 var(--c-warning)</div>
      </div>
    </section>

    <footer class="foot">nasdash 前端重构 · 阶段 1a 探针 · 产物由 Vite 构建并内联为单文件 HTML</footer>
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
  margin-bottom: 20px;
}
.head h1 {
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
.card {
  background: var(--c-card);
  border: 1px solid var(--c-border);
  border-radius: var(--r-lg);
  box-shadow: var(--c-shadow);
  padding: 18px 20px;
  margin-bottom: 16px;
}
.card h2 {
  font-size: 15px;
  margin: 0 0 12px;
}
.card code {
  background: var(--c-primary-bg);
  color: var(--c-primary);
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 12.5px;
}
dl {
  margin: 0;
}
dl > div {
  display: flex;
  gap: 12px;
  padding: 6px 0;
  border-bottom: 1px dashed var(--c-border);
  font-size: 13.5px;
}
dl > div:last-child {
  border-bottom: none;
}
dt {
  flex: 0 0 96px;
  color: var(--c-text-2);
}
dd {
  margin: 0;
  flex: 1;
  word-break: break-word;
}
.num {
  font-size: 15px;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12.5px;
}
.row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.pill {
  border-radius: 999px;
  padding: 3px 12px;
  font-size: 12.5px;
  font-weight: 600;
}
.pill.ok {
  background: color-mix(in srgb, var(--c-success) 14%, transparent);
  color: var(--c-success);
}
.pill.bad {
  background: rgba(245, 63, 63, 0.14);
  color: #e5484d;
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
.net-item {
  display: inline-block;
  margin-right: 14px;
}
.raw {
  margin: 12px 0 0;
  padding: 12px;
  background: var(--c-bg);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  font-size: 12px;
  max-height: 260px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
.swatches {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.sw {
  padding: 10px 14px;
  border-radius: var(--r-md);
  font-size: 12.5px;
  border: 1px solid var(--c-border);
}
.sw-bg {
  background: var(--c-bg);
  color: var(--c-text-1);
}
.sw-card {
  background: var(--c-card);
  color: var(--c-text-1);
}
.sw-primary {
  background: var(--c-primary-bg);
  color: var(--c-primary);
}
.sw-ok {
  background: color-mix(in srgb, var(--c-success) 14%, transparent);
  color: var(--c-success);
}
.sw-warn {
  background: color-mix(in srgb, var(--c-warning) 16%, transparent);
  color: var(--c-warning);
}
.foot {
  text-align: center;
  color: var(--c-text-2);
  font-size: 12px;
  margin-top: 24px;
}
</style>
