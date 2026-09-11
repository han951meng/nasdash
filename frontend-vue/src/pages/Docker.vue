<script setup lang="ts">
/**
 * 阶段 3 · Docker 页（Vue 版）
 *
 * 复刻 templates/index.html 的 renderDocker：
 *  - /api/docker 取容器列表（状态/镜像/CPU/内存/网络速率与累计/端口/运行时长）
 *  - 旧页每 8s 轮询一次，这里同样 8s 定时刷新（内存/CPU% 由后端已按逻辑核数归一化，前端直接显示）
 *  - 网络列：有实时速率显示 ↑↓ 速率，无速率显示累计收发（与旧页口径一致）
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import PanelHero from '../components/PanelHero.vue'
import type { HeroStat } from '../components/PanelHero.vue'
import { apiFetch } from '../lib/api'
import { pageCacheGet, pageCacheSet } from '../lib/pageCache'
import { fmtSpeed } from '../lib/format'

interface DockerContainer {
  name?: string
  image?: string
  running?: boolean
  status?: string
  ports?: string
  cpu?: number | null
  cpu_raw?: number | null
  mem?: string | null
  mem_pct?: number | null
  mem_bytes?: number | null
  net_rx?: number | null
  net_tx?: number | null
  net_rx_rate?: number | null
  net_tx_rate?: number | null
  runtime?: string | null
}
interface DockerResp {
  running?: number
  total?: number
  containers?: DockerContainer[]
  ok?: boolean
}

const docker = ref<DockerResp | null>(null)
const lastUpdate = ref('')
const busy = ref(false)
const error = ref('')

let timer = 0
let fetching = false

// ===== 单位换算（与旧页 fmtBToSize 完全一致）=====
function fmtBToSize(b: number | null | undefined): string {
  if (b == null || isNaN(b)) return '?'
  if (b >= 1e12) return (b / 1e12).toFixed(1) + 'T'
  if (b >= 1e9) return Math.round(b / 1e9) + 'G'
  if (b >= 1e6) return Math.round(b / 1e6) + 'M'
  return '?'
}

// ===== 单元格格式化 =====
function fmtCpu(c: DockerContainer): string {
  return c.cpu != null ? c.cpu.toFixed(1) + '%' : '—'
}
function fmtMem(c: DockerContainer): string {
  return c.mem || 'N/A'
}
function netDown(c: DockerContainer): string {
  if (c.net_rx_rate != null) return fmtSpeed(c.net_rx_rate)
  if (c.net_rx != null) return '累计 ' + fmtBToSize(c.net_rx)
  return '—'
}
function netUp(c: DockerContainer): string {
  if (c.net_tx_rate != null) return fmtSpeed(c.net_tx_rate)
  if (c.net_tx != null) return '累计 ' + fmtBToSize(c.net_tx)
  return '—'
}
function netTitle(c: DockerContainer): string {
  const parts: string[] = []
  if (c.net_rx != null) parts.push('累计下行 ' + fmtBToSize(c.net_rx))
  if (c.net_tx != null) parts.push('上行 ' + fmtBToSize(c.net_tx))
  return parts.join(' / ')
}

const containers = computed<DockerContainer[]>(() => docker.value?.containers || [])
const isOk = computed(() => docker.value?.ok !== false)

const heroStats = computed<HeroStat[]>(() => {
  const d = docker.value
  const running = d?.running || 0
  const total = d?.total || 0
  return [
    { v: running + '/' + total, k: '运行中' },
    {
      v: isOk.value ? '正常' : '读取失败',
      k: '状态',
      cls: isOk.value ? '' : 'warn',
    },
    { v: String(total), k: '容器总数' },
  ]
})

async function load(force = false): Promise<void> {
  if (fetching) return
  fetching = true
  if (force) busy.value = true
  // 切页签回来先上缓存秒开，再拉最新数据替换
  if (!docker.value) {
    const cached = pageCacheGet<DockerResp>('docker')
    if (cached) docker.value = cached
  }
  try {
    const r = await apiFetch('/api/docker', 15000)
    if (!r.ok) {
      error.value = 'Docker 接口返回异常（HTTP ' + r.status + '）'
      return
    }
    const j = (await r.json()) as { docker?: DockerResp }
    docker.value = j.docker || null
    pageCacheSet('docker', docker.value)
    error.value = ''
    lastUpdate.value = '更新于 ' + new Date().toLocaleTimeString('zh-CN')
  } catch (e) {
    error.value = '获取 Docker 数据失败：' + (e instanceof Error ? e.message : String(e))
  } finally {
    fetching = false
    busy.value = false
  }
}

function refreshAll(): void {
  void load(true)
}

onMounted(() => {
  void load(true)
  // 旧页每 8s 轮询一次 Docker 资源占用
  timer = window.setInterval(() => void load(false), 8000)
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <div>
    <PanelHero
      icon="docker"
      title="Docker"
      sub="容器运行状态与资源占用"
      :stats="heroStats"
      :last-update="lastUpdate"
      :busy="busy"
      @refresh="refreshAll"
    />

    <div class="card">
      <h3>说明</h3>
      <div class="note">
        列出所有容器及其运行状态、占用内存与端口映射。内存仅对运行中容器有效；停止的容器显示 N/A。
      </div>
    </div>

    <div v-if="error" class="card" style="border-color: var(--danger)">
      <div class="note" style="color: var(--danger)">{{ error }}</div>
    </div>

    <div class="section-title" style="margin-top: 14px">容器列表</div>
    <div class="card">
      <table class="table">
        <thead>
          <tr>
            <th>状态</th>
            <th>名称 / 镜像</th>
            <th>CPU</th>
            <th>内存</th>
            <th>网络速率 (↓收/↑发)</th>
            <th>端口映射</th>
            <th>运行时长</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(c, i) in containers" :key="i">
            <td>
              <span class="badge" :class="c.running ? 'b-ok' : 'b-warn'">
                {{ c.running ? '运行' : '停止' }}
              </span>
            </td>
            <td>
              <b>{{ c.name }}</b
              ><br /><span style="font-size: 12px; color: var(--muted)">{{ c.image }}</span>
            </td>
            <td style="white-space: nowrap; text-align: right">{{ fmtCpu(c) }}</td>
            <td style="white-space: nowrap">
              {{ fmtMem(c)
              }}<template v-if="c.mem_pct != null"
                ><br /><small style="color: var(--muted)">{{ c.mem_pct.toFixed(1) }}%</small></template
              >
            </td>
            <td
              style="font-size: 12px; white-space: nowrap"
              :title="netTitle(c)"
            >
              ↓{{ netDown(c) }}<br />↑{{ netUp(c) }}
            </td>
            <td style="font-size: 12px">{{ c.ports || '-' }}</td>
            <td style="font-size: 12px; color: var(--muted)">{{ c.runtime || '—' }}</td>
          </tr>
          <tr v-if="!containers.length">
            <td colspan="7" class="loading">未检测到 Docker / 无容器</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
