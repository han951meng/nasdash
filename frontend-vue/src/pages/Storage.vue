<script setup lang="ts">
/**
 * 阶段 3 · 存储卷页（Vue 版）
 *
 * 复刻 templates/index.html 的 renderStorage(st)（旧页 3608-3651）：
 *  - /api/storage 取 {raid_arrays, volumes, topology, cloud_mounts}
 *  - hero 四指标：RAID 阵列数 / 存储卷数 / 平均使用率(仅算本地卷) / 拓扑有无
 *  - 三段：RAID 阵列表 + 存储卷容量表(进度条「智能两态」+ 云盘标记) + 存储拓扑 <pre>
 *  - **纯只读**：此页无任何写操作（RAID 卡控制属于「硬件配置检测」页）。
 *    旧页对存储卷默认也不轮询（auto-refresh 默认关），这里同样只在挂载时拉一次 + 手动刷新。
 */
import { computed, onMounted, ref } from 'vue'
import PanelHero from '../components/PanelHero.vue'
import type { HeroStat } from '../components/PanelHero.vue'
import { apiFetch } from '../lib/api'
import { pageCacheGet, pageCacheSet } from '../lib/pageCache'

interface RaidArray {
  name?: string
  state?: string
  level?: string
  size?: string
  disks?: string[]
}
interface Volume {
  mount?: string
  size?: string | null
  used?: string | null
  avail?: string | null
  pcent?: string | null
  fstype?: string
  cloud?: boolean
  cloud_type?: string
  cloud_name?: string
  cloud_proto?: string
}
interface StorageResp {
  raid_arrays?: RaidArray[]
  volumes?: Volume[]
  topology?: string
  cloud_mounts?: Volume[]
  volume_map?: VolumeMap
}

/** 卷映射树（v2.3.0 3.6）：fnOS 卷 → md/LVM → 物理盘 */
interface MapDisk {
  dev: string
  serial?: string
  model?: string
  temp?: number | null
  asleep?: boolean
  slot?: string
  pd_state?: string
}
interface MapVolume {
  mount: string
  fs?: string
  size?: string | null
  used?: string | null
  pcent?: string | null
  lvm?: string | null
  md?: { name: string; level?: string; state?: string; size?: string } | null
  disks: MapDisk[]
}
interface VolumeMap {
  volumes: MapVolume[]
  loose: MapDisk[]
}

const st = ref<StorageResp | null>(null)
const lastUpdate = ref('')
const busy = ref(false)
const error = ref('')

const raidArrays = computed<RaidArray[]>(() => st.value?.raid_arrays || [])
const volumes = computed<Volume[]>(() => st.value?.volumes || [])
const topology = computed(() => st.value?.topology || '')
const vmap = computed<VolumeMap | null>(() => st.value?.volume_map || null)

/** 型号太长时截断（弹 hover 由 title 补全），卡内保持一行 */
function shortModel(m?: string): string {
  const s = (m || '').trim()
  if (!s) return ''
  return s.length > 26 ? s.slice(0, 26) + '…' : s
}
/** 点物理盘 → 跳「硬盘 SMART」页并高亮该盘的健康卡（App.vue 监听 nasdash-nav 换页签） */
function focusDisk(dev: string): void {
  try {
    sessionStorage.setItem('nasdash_focus_disk', dev)
  } catch { /* 某些环境禁 sessionStorage，忽略（跳转仍在，只是不高亮） */ }
  window.dispatchEvent(new CustomEvent('nasdash-nav', { detail: { tab: 'disks' } }))
}

/** 平均使用率只算本地卷：云挂载无使用率（后端置空），计入会把均值拉低成假象 */
const avgPct = computed(() => {
  const local = volumes.value.filter(v => !v.cloud)
  if (!local.length) return 0
  const sum = local.reduce((a, v) => a + (parseInt(v.pcent || '0', 10) || 0), 0)
  return Math.round(sum / local.length)
})

const heroStats = computed<HeroStat[]>(() => {
  const p = avgPct.value
  return [
    { v: String(raidArrays.value.length), k: 'RAID 阵列' },
    { v: String(volumes.value.length), k: '存储卷' },
    { v: p + '%', k: '平均使用率', cls: p > 85 ? 'bad' : p > 70 ? 'warn' : '' },
    { v: topology.value ? '有' : '无', k: '拓扑' },
  ]
})

// ===== 表格格式化（与旧页逐字一致）=====
function raidOk(a: RaidArray): boolean {
  return a.state === 'active'
}
function cloudLabel(v: Volume): string {
  return (v.cloud_type || '云盘') + (v.cloud_name ? ' · ' + v.cloud_name : '')
}
/** 使用率数字：pcent 形如 "45%" → 45 */
function pctNum(v: Volume): number {
  const n = parseInt(v.pcent || '', 10)
  return isNaN(n) ? 0 : n
}
/** 色条中段阈值配色：>85 红 / >70 橙 / 其余绿（与旧页同一套语义色） */
function fillColor(v: Volume): string {
  const p = pctNum(v)
  return p > 85 ? 'var(--red)' : p > 70 ? 'var(--orange)' : 'var(--green)'
}
/**
 * 智能两态：色条够宽时白字嵌在条内（锚在色条末端）；太窄时数字挪到条右侧按语义色显示，
 * 避免被圆角切掉。数字在条外时去掉色条内边距，否则 0% 也会被 padding 撑出一小块绿色。
 */
function insideFill(v: Volume): boolean {
  return pctNum(v) >= 25
}
function fillStyle(v: Volume): string {
  const p = pctNum(v)
  return `width:${p}%;background:${fillColor(v)}` + (insideFill(v) ? '' : ';padding-right:0')
}
function outStyle(v: Volume): string {
  return `left:calc(${pctNum(v)}% + 8px);color:${fillColor(v)}`
}

async function load(force = false): Promise<void> {
  if (force) busy.value = true
  // 切页签回来先上缓存秒开，再拉最新数据替换
  if (!st.value) {
    const cached = pageCacheGet<StorageResp>('storage')
    if (cached) st.value = cached
  }
  try {
    const r = await apiFetch('/api/storage', 20000)
    if (!r.ok) {
      error.value = '存储接口返回异常（HTTP ' + r.status + '）'
      return
    }
    const j = (await r.json()) as { storage?: StorageResp; error?: string }
    if (j.error) {
      error.value = '存储接口错误：' + j.error
      return
    }
    st.value = j.storage || null
    pageCacheSet('storage', st.value)
    error.value = ''
    lastUpdate.value = '更新于 ' + new Date().toLocaleTimeString('zh-CN')
  } catch (e) {
    error.value = '获取存储数据失败：' + (e instanceof Error ? e.message : String(e))
  } finally {
    busy.value = false
  }
}

function refreshAll(): void {
  void load(true)
}

onMounted(() => {
  void load(true)
})
</script>

<template>
  <div>
    <PanelHero
      icon="storage"
      title="存储卷"
      sub="RAID 阵列 / 卷容量 / 拓扑"
      :stats="heroStats"
      :last-update="lastUpdate"
      :busy="busy"
      @refresh="refreshAll"
    />

    <div v-if="error" class="card" style="border-color: var(--danger)">
      <div class="note" style="color: var(--danger)">{{ error }}</div>
    </div>

    <!-- 卷映射（v2.3.0 3.6）：结构对齐飞牛原生「存储空间管理」，盘块可点跳健康卡 -->
    <div class="section-title">卷映射</div>
    <div class="card">
      <div v-for="v in vmap?.volumes || []" :key="v.mount" class="vmap-vol">
        <div class="vmap-head">
          <b class="vmap-name">{{ v.mount }}</b>
          <span class="vmap-cap">容量 {{ v.used || '—' }} / {{ v.size || '—' }}</span>
        </div>
        <div class="vmap-bar"><i :style="{ width: v.pcent || '0%' }" /></div>
        <div class="vmap-meta">
          <span v-if="v.md"><i>RAID</i>{{ v.md.name }} · {{ v.md.level }} · {{ v.md.state }}</span>
          <span v-else><i>RAID</i>无（直连）</span>
          <span v-if="v.lvm" :title="'LVM 卷名：' + v.lvm"><i>逻辑卷</i>LVM</span>
          <span><i>文件系统</i>{{ v.fs }}</span>
        </div>
        <div class="vmap-cap2">使用硬盘 {{ v.disks.length }}</div>
        <div class="vmap-pds">
          <button
            v-for="d in v.disks"
            :key="d.dev"
            class="vmap-pd"
            :title="(d.model || '') + '（点击跳到硬盘 SMART 看健康详情）'"
            @click="focusDisk(d.dev)"
          >
            <b>{{ d.dev }}</b>
            <span class="vmap-sub">{{ shortModel(d.model) }}</span>
            <span v-if="d.slot" class="vmap-sub">槽位 {{ d.slot }}</span>
            <span class="vmap-temp" :class="{ asleep: d.asleep }">{{ d.asleep ? '休眠' : d.temp != null ? d.temp + '℃' : '—' }}</span>
            <span class="vmap-go">›</span>
          </button>
        </div>
      </div>
      <div v-if="vmap?.loose?.length" class="vmap-vol" style="border-bottom:0;padding-bottom:2px">
        <div class="vmap-cap2">未入卷的盘 · {{ vmap.loose.length }} 块（独立盘，不属于任何存储卷）</div>
        <div class="vmap-pds">
          <button
            v-for="d in vmap.loose"
            :key="d.dev"
            class="vmap-pd"
            :title="(d.model || '') + '（点击跳到硬盘 SMART 看健康详情）'"
            @click="focusDisk(d.dev)"
          >
            <b>{{ d.dev }}</b>
            <span class="vmap-sub">{{ shortModel(d.model) }}</span>
            <span class="vmap-temp" :class="{ asleep: d.asleep }">{{ d.asleep ? '休眠' : d.temp != null ? d.temp + '℃' : '—' }}</span>
            <span class="vmap-go">›</span>
          </button>
        </div>
      </div>
    </div>

    <div class="section-title">RAID 阵列</div>
    <div class="card">
      <table class="table">
        <thead>
          <tr>
            <th>阵列</th>
            <th>级别</th>
            <th>状态</th>
            <th>容量</th>
            <th>成员盘</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(a, i) in raidArrays" :key="i">
            <td>{{ a.name }}</td>
            <td>
              <span class="badge" :class="raidOk(a) ? 'b-ok' : 'b-bad'">{{ a.level }}</span>
            </td>
            <td>{{ a.state }}</td>
            <td>{{ a.size }}</td>
            <td>{{ (a.disks || []).join(', ') }}</td>
          </tr>
          <tr v-if="!raidArrays.length">
            <td colspan="5">无</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="section-title">存储卷容量</div>
    <div class="card">
      <table class="table">
        <thead>
          <tr>
            <th>挂载点</th>
            <th>文件系统</th>
            <th>总容量</th>
            <th>已用/可用</th>
            <th>使用率</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="(v, i) in volumes" :key="i">
            <!-- 云挂载（飞牛「远程挂载」的网盘）：容量由云端掌握、后端不报配额 -->
            <tr v-if="v.cloud">
              <td>
                {{ v.mount }}
                <div class="cloud-tag">☁ {{ cloudLabel(v) }}</div>
              </td>
              <td>{{ v.cloud_proto || 'rclone WebDAV' }}</td>
              <td style="color: var(--c-text-2)">—</td>
              <td style="color: var(--c-text-2)">—</td>
              <td style="color: var(--c-text-2)">云端挂载 · 不提供容量</td>
            </tr>
            <tr v-else>
              <td>{{ v.mount }}</td>
              <td>{{ v.fstype }}</td>
              <td>{{ v.size }}</td>
              <td>{{ v.used }} / {{ v.avail }}</td>
              <td style="min-width: 140px">
                <div class="progress">
                  <div class="progress-fill" :style="fillStyle(v)">
                    {{ insideFill(v) ? pctNum(v) + '%' : '' }}
                  </div>
                  <span v-if="!insideFill(v)" class="progress-pct" :style="outStyle(v)">
                    {{ pctNum(v) }}%
                  </span>
                </div>
              </td>
            </tr>
          </template>
          <tr v-if="!volumes.length">
            <td colspan="5">无</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="section-title">存储拓扑</div>
    <div class="card"><pre>{{ topology }}</pre></div>
  </div>
</template>
