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
import { pageCacheGet, pageCacheSet } from '../lib/pageCache'
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
  // 双磁臂盘合并（v2.3.0 3.4）：同一物理盘的多个逻辑名合成一张卡显示
  dev_label?: string
  devs?: string[]
  size_total?: string
  // 通道来源（阵列卡通道 / 主板直连），弹窗「硬盘档案」里显示
  channel?: string
  channel_type?: string
  // v2.3.1：仅阵列卡可见的物理盘（无 /dev 节点）唯一身份证，前端按它渲染，避免 dev 空串撞 key
  dev_id?: string
  // v2.3.1：标记仅阵列卡可见（无 /dev 节点，无法跑自检）
  raid_only?: boolean
  // v2.3.0 3.4：后端算好的健康分级 + 缺陷趋势
  health_grade?: HealthGrade
  health_trend?: HealthTrend | null
  // v2.3.0 3.8：运维自定义盘名（custom_name 空串 = 未命名；name_key 回传改名请求用）
  custom_name?: string
  name_key?: string
}

interface HealthGrade {
  level: 'green' | 'yellow' | 'red' | 'unknown' | string
  label: string
  reasons: string[]
  counters?: Record<string, number>
  source?: string
}

interface HealthTrend {
  deltas: Record<string, number>
  delta_total: number
  prev_ts: number
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
/** 累计读写量显示成短值："185,134,624 [94.7 TB]" → "94.7 TB"；没有人话标注就原样 */
function dataUnitShort(s?: string | null): string {
  if (!s) return '—'
  const m = s.match(/\[([^\]]+)\]/)
  return (m ? m[1] : s).trim()
}
function fmtHours(h?: number | null): string {
  if (h == null) return '-'
  if (h >= 8760) return (h / 8760).toFixed(1) + ' 年'
  return h + ' 小时'
}

/** 卡面格子里的短时长：25910 小时 → 「25910h」，超过一年用「3.0年」 */
function fmtHoursShort(h?: number | null): string {
  if (h == null) return '—'
  if (h >= 8760) return (h / 8760).toFixed(1) + '年'
  return h + 'h'
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

/* ================= 健康分级 + 缺陷趋势（v2.3.0 3.4） ================= */
/** 分级档位：后端没给（老缓存/接口异常）时按原来的 SMART 判定兜底，不至于显示空白 */
function gradeLevel(d: Disk): string {
  const lv = d.health_grade?.level
  if (lv) return lv
  if (d.asleep) return 'unknown'
  return d.health_ok === false ? 'red' : 'green'
}
const GRADE_LABEL: Record<string, string> = { green: '正常', yellow: '注意', red: '异常', unknown: '未知' }
const GRADE_CLS: Record<string, string> = { green: 'b-ok', yellow: 'b-warn', red: 'b-bad', unknown: 'b-muted' }
const GRADE_COLOR: Record<string, string> = {
  green: 'var(--green)', yellow: 'var(--orange)', red: 'var(--red)', unknown: 'var(--muted)',
}
function gradeLabel(d: Disk): string {
  return d.health_grade?.label || GRADE_LABEL[gradeLevel(d)] || '—'
}
function gradeCls(d: Disk): string {
  return GRADE_CLS[gradeLevel(d)] || 'b-muted'
}
function gradeColor(d: Disk): string {
  return GRADE_COLOR[gradeLevel(d)] || 'var(--text)'
}
/** 分级原因（红在前、黄在后，后端已排好序）；绿盘给一句肯定话 */
function gradeReason(d: Disk): string {
  const rs = d.health_grade?.reasons || []
  if (rs.length) return rs.join('；')
  return gradeLevel(d) === 'green' ? '未发现异常信号' : ''
}
/** 缺陷趋势：与上一次采样（默认 6h 前）比，只看只增不减的计数 */
const TREND_LABEL: Record<string, string> = {
  media_err: '介质错误', other_err: '其它错误', bbm_err: '坏块管理错误', pred_fail: '预测性失败',
  reallocated: '重映射扇区', pending: '待处理扇区', uncorrectable: '不可纠正扇区',
  udma_crc: '接口 CRC 错误', defects: 'SAS 缺陷扇区', non_medium_errors: '非介质错误',
  read_errors: '不可纠正读错误', write_errors: '不可纠正写错误',
  percentage_used: 'SSD 寿命', endurance_used: 'SSD 寿命',
}
function fmtAge(ts: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + ' 分钟前'
  if (s < 86400) return (s / 3600).toFixed(1) + ' 小时前'
  return Math.round(s / 86400) + ' 天前'
}

/* ================= 卡面「对比条」（v2.3.0 3.4 收尾） =================
   卡片主界面只留「多块盘并排时能一眼横着比」的少数指标，其余全进徽章弹窗。
   判断标准就一条：**这个数字会不会自己变、且能不能横向比**——
   身份类（型号/容量/转速）认盘用，留；实时类（温度）留；缺陷与寿命留最关键的
   一格用来比「谁有伤、谁快用废」；细分计数、读写量、通道槽位、SN 都进弹窗。 */
/** 盘名：双磁臂盘合并后显示 "sda/sdb"，普通盘就是 dev */
function diskName(d: Disk): string {
  // v2.3.0 3.8：自定义名优先（「数据盘1」比 sda/E0:S3 好认），原代号在卡上作副标
  // v2.3.1：仅阵列卡可见的物理盘没有 dev 节点，回退到「阵列卡 槽位」便于辨认
  if (d.raid_only) return d.custom_name || d.dev_label || d.channel || ('阵列卡 ' + (d.slot || '')) || '阵列卡硬盘'
  return d.custom_name || d.dev_label || d.dev || ''
}
/** v2.3.1：渲染用的唯一身份证——raid_only 盘用 dev_id，普通盘用 dev */
function diskKey(d: Disk): string { return d.dev_id || d.dev || '' }
/** 容量：双磁臂盘标出「×2」（每臂一半，整盘见 size_total / 提示） */
function sizeText(d: Disk): string {
  const n = d.devs?.length || 0
  if (!d.size) return '—'
  return n > 1 ? d.size + ' ×' + n : d.size
}
/** 容量 / 类型 那一行的整串（含接口、双磁臂标注；介质 HDD/SSD 由转速行表达，不在这里重复） */
function capText(d: Disk): string {
  const t = (d.type || '').toLowerCase() === 'nvme' ? 'NVMe' : ((d.type === 'sas') ? 'SAS' : 'SATA')
  const parts = [sizeText(d), t]
  if (d.devs && d.devs.length > 1) parts.push('双磁臂')
  else if (diskFeatureClean(d.feature, d.dual_actuator)) parts.push(diskFeatureClean(d.feature, d.dual_actuator))
  return parts.join(' · ')
}
/** 容量行的悬浮说明：双磁臂盘讲清「为什么显示 ×2」与整盘容量 */
function capTip(d: Disk): string {
  if (d.devs && d.devs.length > 1) {
    return '双磁臂硬盘：两个执行器共用一个盘体，各向系统暴露一个逻辑盘（' + d.dev_label + '），'
      + '每臂 ' + (d.size || '?') + '，整盘 ' + (d.size_total || '约两倍') + '。nasdash 只显示一张卡，数据取两个执行器中更差的一侧。'
  }
  return '硬盘总容量与接口类型（NVMe/SAS/SATA）；机械盘（HDD）还是固态盘（SSD）看下面「转速」那一行'
}
/** 品牌前缀：优先中文品牌名，没有才退回 vendor 英文名
    （避免出现「希捷(Seagate) SEAGATE …」这种同一来源被拼两遍） */
function brandPrefix(d: Disk): string {
  const b = d.brand || d.vendor || ''
  return b ? b + ' ' : ''
}
/** 型号整串（品牌 + 型号） */
function modelText(d: Disk): string {
  return d.model ? brandPrefix(d) + d.model : '—'
}
/** 卡面 4 格对比条：只放能横向比的指标，按盘型选（机械盘比缺陷、固态比寿命）。
    没有值的格会被跳过——老固态盘 SMART 里没有寿命字段时，自动退回比扇区计数，
    不留一排「—」占着位置。 */
function compareCells(d: Disk): Array<{ k: string; label: string; value: string; tip: string; hot: boolean }> {
  type Cell = { k: string; label: string; value: string; tip: string; hot: boolean; has?: boolean }
  const na = d.asleep ? '（硬盘休眠中，读不到真实数据）' : ''
  const cells: Cell[] = [{
    k: 'temp',
    label: '当前温度',
    value: d.temp != null ? d.temp + '℃' : '—',
    tip: '实时读数，几秒就变；多块盘并排时直接横着比谁更热。超过该盘高温线 ' + tempTrip(d) + '℃ 会判为「注意」。' + na,
    hot: tempHot(d),
  }]
  // 候选池：按 key 存放，取哪两个由盘型和「有没有值」决定
  const POOL: Record<string, Cell> = {
    wear: {
      k: 'wear', label: '已用寿命',
      value: d.percentage_used != null ? d.percentage_used + '%' : '—',
      tip: '固态盘预计寿命的已消耗百分比（累计值，只增不减）。超过 80% 判为「注意」。' + na,
      hot: (d.percentage_used || 0) > 80,
      has: d.percentage_used != null,
    },
    spare: {
      k: 'spare', label: '剩余备用',
      value: d.available_spare != null ? d.available_spare + '%' : '—',
      tip: '固态盘保留的备用块剩余比例（累计值，只会变少）。低于 10% 判为「注意」。' + na,
      hot: d.available_spare != null && d.available_spare < 10,
      has: d.available_spare != null,
    },
    defects: {
      k: 'defects', label: d.type === 'sas' ? '缺陷扇区' : '重映射',
      value: (d.type === 'sas' ? d.defects : d.reallocated) != null ? String(d.type === 'sas' ? d.defects : d.reallocated) : '—',
      tip: '已经坏掉、被备用块顶替的扇区数（累计值，只增不减）。>0 说明盘体已有损伤，横着比一眼能看出哪块盘带伤。' + na,
      hot: ((d.type === 'sas' ? d.defects : d.reallocated) || 0) > 0,
      has: (d.type === 'sas' ? d.defects : d.reallocated) != null,
    },
    pending: {
      k: 'pending', label: '待处理',
      value: d.pending != null ? String(d.pending) : '—',
      tip: '读写时发现不稳定、还没确认或替换的扇区（累计值）。>0 是坏道的前一步信号，要盯着。' + na,
      hot: (d.pending || 0) > 0,
      has: d.pending != null,
    },
  }
  // 候选池顺序按盘型定：固态先看寿命/备用，机械先看缺陷；没值的自动落到下一个
  const order = d.rota !== '1' ? ['wear', 'spare', 'defects', 'pending'] : ['defects', 'pending', 'wear', 'spare']
  const pool = order.map(k => POOL[k]).filter(Boolean) as Cell[]
  const picked = pool.filter(x => x.has).slice(0, 2)
  if (!picked.length) picked.push(...pool.slice(0, 2))   // 全都没值（休眠中）时留占位，保持卡片高度一致
  cells.push(...picked)
  cells.push({
    k: 'hours', label: '已用时长',
    value: fmtHoursShort(d.power_on_hours),
    tip: '累计通电运行时间（只增不减）。横着比能看出哪块盘最老、最该列入更换计划。' + na,
    hot: false,
  })
  return cells
}

/* ===== 健康详情弹窗（v2.3.0 3.4）：徽章点开看完整依据 + 缺陷趋势 + 各项计数 =====
   卡片里不再重复摆「健康分级」这一行；徽章本身就是入口，只在有新增缺陷时挂个小角标。 */
const gradeDev = ref<string | null>(null)
const gradeDisk = computed<Disk | null>(() => disks.value.find(x => diskKey(x) === gradeDev.value) || null)
function openGrade(d: Disk) {
  gradeDev.value = diskKey(d)
}

/* ===== 改名弹窗（v2.3.0 3.8）：给盘起个好认的名字，存后端配置、重启不丢 ===== */
const renameOpen = ref(false)
const renameTarget = ref<Disk | null>(null)
const renameValue = ref('')
const renameBusy = ref(false)
const renameErr = ref('')

function openRename(d: Disk) {
  renameTarget.value = d
  renameValue.value = d.custom_name || ''
  renameErr.value = ''
  renameOpen.value = true
}
async function saveRename(clear = false): Promise<void> {
  const d = renameTarget.value
  if (!d || renameBusy.value) return
  const name = clear ? '' : renameValue.value.trim()
  if (!clear && !name) { renameErr.value = '名字不能为空（想清除请点「清除」）'; return }
  renameBusy.value = true
  renameErr.value = ''
  try {
    const r = await apiFetch('/api/disks/rename', 15000, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ key: d.name_key || d.serial || d.dev, name }),
    })
    const j = await r.json()
    if (!j.ok) {
      renameErr.value = j.error || '保存失败'
      return
    }
    renameOpen.value = false
    await loadDisks(false)
  } catch (e) {
    renameErr.value = '保存失败：' + String((e as Error).message || e)
  } finally {
    renameBusy.value = false
  }
}
/** 徽章角标：本次采样相比上次**新增**的缺陷数（后端只报增长），没有就是 0、不显示 */
function trendUp(d: Disk): number {
  return d.health_trend?.delta_total || 0
}
/** 趋势明细：拆成「介质错误 +3」这样的短句，供徽章提示与弹窗共用 */
function trendParts(d: Disk): string[] {
  return Object.entries(d.health_trend?.deltas || {}).map(([k, v]) => (TREND_LABEL[k] || k) + ' +' + v)
}
/** 一句话说清「这个档意味着什么」——绿/黄/红/未知各一句 */
function gradeExplain(d: Disk): string {
  const lv = gradeLevel(d)
  if (lv === 'green') return '这块盘没有发现任何异常信号，各项缺陷计数均为 0，可以放心用。'
  if (lv === 'yellow') return '这块盘已经有损伤，或者温度/寿命接近上限。还能继续用，但建议做好备份，并留意下面的计数有没有继续变大。'
  if (lv === 'red') return '这块盘报了明确的故障信号。建议尽快把重要数据备份出来，并准备更换。'
  return '暂时读不到这块盘的健康数据（多为休眠中），不代表它坏了——硬盘转起来后会自动补上。'
}
/** 弹窗里的计数清单：只列后端真的给到的项，按「阵列卡错误 → 扇区缺陷 → 寿命」排。
    注意：**温度不在这里**——它是实时读数（几秒就变），既不是缺陷也不是寿命消耗，
    后端快照/趋势也显式把它排除（_TREND_COUNTERS 不含 temp）。放这一格里会让人误以为
    温度也会被累计、拿去跟上次比。温度单独放在标题栏那一行显示。 */
const COUNTER_META: Array<[string, string, string]> = [
  ['media_err', '阵列卡介质错误', '阵列卡从盘体读数据时遇到的介质层错误次数'],
  ['other_err', '阵列卡其它错误', '不属于介质错误的其它报错，多为通信/协议层'],
  ['bbm_err', '坏块管理错误', '阵列卡坏块管理表（BBM）记录的替换失败次数'],
  ['pred_fail', '阵列卡预测性失败', '阵列卡判断该盘可能即将失效的次数，>0 即高风险'],
  ['pending', '待处理扇区', '读写时被发现不稳定、尚待确认或替换的扇区，>0 有恶化风险'],
  ['uncorrectable', '不可纠正扇区', '发生错误且无法通过重映射修复的扇区，>0 风险较高'],
  ['reallocated', '重映射扇区', '盘体已用备用块顶掉的坏扇区数量，>0 说明已有损伤'],
  ['defects', 'SAS 缺陷扇区', 'SAS 盘缺陷表记录的已发现缺陷扇区数，>0 说明已有损伤'],
  ['non_medium_errors', 'SAS 非介质错误', 'SAS 盘与介质无关的报错次数（多为通信/协议层）'],
  ['read_errors', 'SAS 读错误(不可纠正)', 'SAS 盘累计无法纠正的读错误次数'],
  ['write_errors', 'SAS 写错误(不可纠正)', 'SAS 盘累计无法纠正的写错误次数'],
  ['udma_crc', '接口 CRC 错误', 'SATA 传输层的 CRC 校验错误，>0 多为数据线/接口接触不良，不是盘体坏了（换线通常能解决）'],
  ['percentage_used', 'SSD 寿命已用', '固态盘预计寿命的已消耗百分比（超过 80% 标红）'],
  ['endurance_used', 'SSD 寿命已用（阵列卡报）', '阵列卡读到的固态盘寿命消耗百分比（超过 80% 标红）'],
  ['available_spare', 'NVMe 剩余备用', '固态盘保留的备用块剩余比例。这是反向指标：数字越大越健康，低于 10% 标红'],
]
/** 每项计数的「危险高亮」规则：多数是 >0 就红；寿命类 >80% 才红；剩余备用是反向指标 <10% 才红 */
function counterHot(k: string, v: number): boolean {
  if (k === 'available_spare') return v < 10
  if (k === 'percentage_used' || k === 'endurance_used') return v > 80
  return v > 0
}
function counterRows(d: Disk): Array<{ k: string; name: string; tip: string; v: number; hot: boolean; unit: string }> {
  const cs = d.health_grade?.counters || {}
  const out: Array<{ k: string; name: string; tip: string; v: number; hot: boolean; unit: string }> = []
  for (const [k, name, tip] of COUNTER_META) {
    const raw = cs[k]
    if (typeof raw !== 'number') continue
    const unit = k === 'percentage_used' || k === 'endurance_used' || k === 'available_spare' ? '%' : ''
    out.push({ k, name, tip, v: raw, hot: counterHot(k, raw), unit })
  }
  return out
}
/** 温度是否已达该盘高温线（默认 55℃）——到了就是分级里的黄档判据之一，弹窗里要点出来 */
function tempHot(d: Disk): boolean {
  return d.temp != null && d.temp >= (d.temp_trip || 55)
}
function tempTrip(d: Disk): number {
  return d.temp_trip || 55
}

/* ================= Hero ================= */
const heroStats = computed(() => {
  const total = disks.value.length
  // 「健康」口径 = 分级为绿（有损伤的黄色盘不再计入健康，但会在下面的黄卡里点名）
  const healthy = disks.value.filter(d => gradeLevel(d) === 'green').length
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

const badDisks = computed(() => disks.value.filter(d => gradeLevel(d) === 'red'))
const warnDisks = computed(() => disks.value.filter(d => gradeLevel(d) === 'yellow'))

/* ================= 数据加载 ================= */
async function loadDisks(force = false): Promise<void> {
  if (disposed) return
  // 切页签回来先上缓存秒开，再拉最新数据替换
  if (!disks.value.length) {
    const cached = pageCacheGet<{ disks: Disk[]; time: string }>('disks')
    if (cached) {
      disks.value = cached.disks || []
      lastUpdate.value = cached.time || ''
    }
  }
  // 有内容就不转圈：后台静默拉最新
  if (!disks.value.length) busy.value = true
  try {
    const r = await apiFetch('/api/disks' + (force ? '?force=1' : ''), 30000)
    const j = await r.json()
    if (j.error) {
      error.value = '接口错误：' + j.error
    } else {
      error.value = ''
      disks.value = j.disks || []
      lastUpdate.value = j.time || ''
      pageCacheSet('disks', { disks: disks.value, time: lastUpdate.value })
    }
  } catch (e) {
    error.value = '获取硬盘数据失败：' + String((e as Error).message || e)
  } finally {
    busy.value = false
  }
  maybeFocusDisk()
}

/** 卷映射跳转落点（v2.3.0 3.6）：存储卷页点盘后把 dev 写进 sessionStorage，
 *  本页首次拿到数据后滚动到该盘的健康卡并高亮 2 秒。双磁臂盘的副臂（sdb）
 *  没有独立卡，落到主臂卡（devs 里包含它）。 */
function maybeFocusDisk(): void {
  let dev = ''
  try {
    dev = sessionStorage.getItem('nasdash_focus_disk') || ''
    sessionStorage.removeItem('nasdash_focus_disk')
  } catch { return }
  if (!dev) return
  const target = disks.value.find(d => diskKey(d) === dev || (d.devs || []).includes(dev))
  if (!target) return
  requestAnimationFrame(() => {
    const el = document.querySelector(`.disk-card[data-dev="${diskKey(target)}"]`)
    if (!el) return
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    el.classList.add('focus-flash')
    setTimeout(() => el.classList.remove('focus-flash'), 2000)
  })
}

/* ================= 自检状态轮询 ================= */
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
  pickDev.value = diskKey(d)
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
            <b>{{ diskName(d) }}</b>（{{ d.model || '未知型号' }}）：{{ gradeReason(d) }}
          </li>
        </ul>
        <div style="font-size:13px;color:var(--text)">
          建议：<b>尽快备份</b>这块盘上的重要数据，并准备更换硬盘；在换新盘之前，避免往它上面写入关键文件。
        </div>
      </div>

      <!-- 需要注意的盘（黄）：先盯住，不用马上换 -->
      <div
        v-if="warnDisks.length"
        style="background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.4);border-radius:10px;padding:14px 16px;margin-bottom:14px"
      >
        <div style="font-weight:600;color:#b45309;margin-bottom:6px">🔍 {{ warnDisks.length }} 块硬盘需要注意（先用着，但要盯住）</div>
        <ul style="margin:0 0 8px 18px;padding:0;font-size:13px;line-height:1.7">
          <li v-for="d in warnDisks" :key="d.dev">
            <b>{{ diskName(d) }}</b>（{{ d.model || '未知型号' }}）：{{ gradeReason(d) }}
          </li>
        </ul>
        <div style="font-size:13px;color:var(--text)">
          建议：把这块盘上的<span style="font-weight:600">重要数据也做一份备份</span>，并在「缺陷趋势」里留意数值有没有继续变大。
        </div>
      </div>

      <!-- 说明卡：自检会伤盘吗（默认收起，点标题展开） -->
      <details class="card disk-info-card">
        <summary><AppIcon name="bulb" /> 硬盘自检会伤盘吗？<i class="faq-hint" aria-hidden="true"></i></summary>
        <div class="note">
          <p><b class="safe">平时不伤盘</b>：本页只读 SMART 健康数据，不扫盘面、不写数据。只有手动点某块盘的「硬盘自检」才会真扫，三种里两种只读：</p>
          <p>• <b>SMART 长自检 / 只读表面扫描</b>：只读不写、不伤数据，<b>所有盘都能跑</b>（包括在用的阵列成员）。</p>
          <p>• <b>全面坏块扫描</b>：会<b class="warn">写满整盘并清空数据</b>，最彻底但很慢，只对「独立盘」开放，在用盘直接不显示这个选项。</p>
          <p><b>健康徽章</b>：<b class="safe">绿=正常</b>、<b class="warn">黄=注意</b>、<b style="color:var(--red)">红=尽快备份换盘</b>，判据全部来自盘和阵列卡自报数据。<b>点徽章</b>看详情：为什么这一档、各项计数、缺陷趋势（每 6 小时记一次，涨了徽章会挂 <b class="warn">↑N</b> 角标）。</p>
          <p><b>卡面</b>只放能横着比的：温度、缺陷/寿命、已用时长；序列号、读写量等细节都在徽章弹窗里。</p>
          <p><b>累计读写量</b>：NVMe、SATA 固态、SAS 盘都会显示；部分老款 SATA 机械盘固件不记总读写，这块硬件层面就拿不到，相应行直接不显示（不是故障）。</p>
        </div>
      </details>

      <!-- 每块盘一张卡 -->
      <div class="cards">
        <div v-for="d in disks" :key="diskKey(d)" class="card disk-card" :data-dev="diskKey(d)" :class="d.asleep ? '' : (gradeLevel(d) === 'red' ? 'bad' : '')">
          <div class="disk-head">
            <div style="display:flex;align-items:center;gap:8px;min-width:0">
              <span class="name" :title="d.custom_name ? ('原代号：' + (d.dev_label || d.dev) + '（悬停可随时查看，终端找盘用）') : '盘符/槽位代号，点「改名」可起个好认的名字'">{{ diskName(d) }}</span>
              <button
                class="badge grade-btn"
                :class="d.asleep ? 'b-muted' : gradeCls(d)"
                :title="d.asleep
                  ? '休眠中，点了看说明'
                  : ('健康分级：' + gradeLabel(d) + '（依据：' + (d.health_grade?.source || 'SMART') + '）· 点开看详细依据与缺陷趋势')"
                @click="openGrade(d)"
              >
                {{ d.asleep ? '休眠' : gradeLabel(d) }}
                <span v-if="!d.asleep && trendUp(d)" class="grade-up" :title="'相比上次采样新增缺陷 ' + trendUp(d) + ' 项'">↑{{ trendUp(d) }}</span>
              </button>
            </div>
            <div style="display:flex;align-items:center;gap:6px;flex:0 0 auto">
              <button class="btn-mini" title="给这块盘起个好认的名字（如「数据盘1」），重启不丢" @click="openRename(d)">改名</button>
            <button
              v-if="d.locate_supported && d.slot"
              class="btn-mini"
              :data-locate-slot="d.slot"
              :disabled="locateBusy[d.slot]"
              @click="raidLocate(d.slot!)"
            >{{ locateBusy[d.slot] ? '定位中…' : (locateOn[d.slot] ? '停止闪灯' : '定位闪灯') }}</button>
            </div>
          </div>

          <!-- 健康分级已收进标题栏徽章的弹窗（点徽章看依据 + 缺陷趋势），卡内不再重复一行 -->

          <!-- 身份 3 行：认盘用，多块盘并排也能横着比 -->
          <div class="kv"><span class="k" title="硬盘的品牌与完整型号名称">型号</span><span class="v">{{ modelText(d) }}</span></div>
          <div class="kv"><span class="k" :title="capTip(d)">容量 / 类型</span><span class="v">{{ capText(d) }}</span></div>
          <div class="kv"><span class="k" title="机械盘每分钟转数；SSD 无转动，显示为固态">转速</span><span class="v">{{ d.rpm || ((d.rota == '1' || d.type === 'sas') ? '—' : '固态(SSD)') }}</span></div>

          <!-- 对比条：只放「能横着比」的少数指标（温度 / 缺陷或寿命 / 已用时长）；
               细分计数、读写量、通道槽位、SN 全部在徽章弹窗里 -->
          <div class="cmp-grid">
            <div v-for="c in compareCells(d)" :key="c.k" class="cmp-cell" :title="c.tip">
              <span class="cmp-k">{{ c.label }}</span>
              <span class="cmp-v" :class="{ hot: c.hot }">{{ c.value }}</span>
            </div>
          </div>

          <div class="temp-bar" :title="'温度占该盘高温线（' + tempTrip(d) + '℃）的比例，横着比一眼看出谁更热、谁更接近上限'">
            <div
              class="temp-fill"
              :style="{ width: (d.temp != null ? Math.min(d.temp / (d.temp_trip || 60) * 100, 100) : 0) + '%', background: tempColor(d.temp, d.temp_trip || 60) }"
            />
            <span class="temp-bar-label">{{ d.temp != null ? Math.round(d.temp) + '℃' : '—' }}</span>
          </div>

          <!-- v2.3.0 3.4 收尾：原先这里的 SAS/NVMe/ATA 累计计数（缺陷扇区、待处理、
               非介质错误、读写错误、已用寿命、剩余备用、临界告警、读写量）已全部移进
               「健康详情」弹窗——它们要么只在出问题时才非 0、要么需要连着上下文看，
               摆在卡面上只占地方、还不便于横向比较；卡面只留上面的对比条。 -->

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

          <div v-else-if="!d.raid_only" class="disk-test-section">
            <div class="disk-test-title"><AppIcon name="pulse" />硬盘自检</div>
            <div class="disk-test-btns">
              <button class="btn-mini" @click="openPick(d)">硬盘自检</button>
              <button class="btn-mini" @click="showDiskTestHistory(d.dev)">自检记录</button>
            </div>
            <div class="disk-test-msg" :class="{ error: stateErr[d.dev] }">{{ stateMsg[d.dev] }}</div>
          </div>
          <div v-else class="disk-test-section">
            <div class="disk-test-title"><AppIcon name="pulse" />硬盘自检</div>
            <div class="disk-test-msg">物理盘由阵列卡接管，nasdash 无法在其上跑自检（无 /dev 节点）</div>
          </div>
        </div>
      </div>
    </template>

    <!-- ===== 健康分级详情（v2.3.0 3.4）：点硬盘名旁边的徽章打开 ===== -->
    <div v-if="gradeDisk" class="modal-overlay show" @click.self="gradeDev = null">
      <div class="modal-box" style="max-width:700px;max-height:90vh">
        <div class="modal-title">健康详情 · {{ diskName(gradeDisk) }}</div>
        <div class="modal-body hd-body">
          <div class="hd-id">
            <b>{{ diskName(gradeDisk) }}</b>
            <span v-if="gradeDisk.custom_name" class="sub-name">{{ gradeDisk.dev_label || gradeDisk.dev }}</span>
            <span>{{ gradeDisk.model ? modelText(gradeDisk) : '未知型号' }}</span>
            <span :title="capTip(gradeDisk)">{{ sizeText(gradeDisk) }}</span>
            <span v-if="gradeDisk.devs && gradeDisk.devs.length > 1" class="hd-dual" :title="capTip(gradeDisk)">
              双磁臂 · 整盘 {{ gradeDisk.size_total || '约两倍' }}
            </span>
            <!-- 温度是实时读数，跟下面的「累计计数」不是一类东西，单独放标题栏这一行 -->
            <span
              class="hd-temp"
              :class="{ hot: tempHot(gradeDisk) }"
              :title="'当前温度是实时读数，几秒就会变，不参与缺陷累计、也不跟上次比；该盘高温线 ' + tempTrip(gradeDisk) + '℃（超过会判为「注意」）'"
            >
              当前温度 <b>{{ gradeDisk.temp != null ? gradeDisk.temp + '℃' : 'N/A' }}</b>
              <span class="hd-trip">&nbsp;· 高温线 {{ tempTrip(gradeDisk) }}℃</span>
            </span>
          </div>

          <div class="hd-grade" :style="{ borderLeftColor: gradeColor(gradeDisk) }">
            <span class="hd-dot" :style="{ background: gradeColor(gradeDisk) }" />
            <div>
              <div class="hd-level" :style="{ color: gradeColor(gradeDisk) }">
                {{ gradeLabel(gradeDisk) }}
                <span v-if="trendUp(gradeDisk)" style="font-size:12px;font-weight:600;color:var(--orange);margin-left:6px">
                  相比上次 +{{ trendUp(gradeDisk) }}
                </span>
              </div>
              <div class="hd-explain">{{ gradeExplain(gradeDisk) }}</div>
            </div>
          </div>

          <div class="hd-sec">
            <h4>为什么是这一档</h4>
            <ul v-if="(gradeDisk.health_grade?.reasons || []).length" class="hd-reasons">
              <li v-for="(r, i) in (gradeDisk.health_grade?.reasons || [])" :key="i">{{ r }}</li>
            </ul>
            <div v-else class="hd-none">没有发现任何异常信号——各项缺陷计数都为 0，温度也在安全范围内。</div>
          </div>

          <div class="hd-sec">
            <h4>缺陷趋势</h4>
            <div v-if="gradeDisk.health_trend && gradeDisk.health_trend.prev_ts" class="hd-trend">
              较上次采样（{{ fmtAge(gradeDisk.health_trend.prev_ts) }}）：
              <b v-if="trendParts(gradeDisk).length" class="up">{{ trendParts(gradeDisk).join('、') }}</b>
              <b v-else class="flat">无新增缺陷</b>
            </div>
            <div v-else-if="counterRows(gradeDisk).length" class="hd-none">
              首次快照已记录，现在还没有「上一次」可比。<b>下次采样（约 6 小时后）</b>起这里会显示「比上次多了多少」——数字只增不减，一旦开始涨就说明盘在变坏。
            </div>
            <div v-else class="hd-none">这块盘没有可跟踪的缺陷计数，暂时做不了趋势对比。</div>
            <div v-if="counterRows(gradeDisk).length" class="hd-note">
              后台每 6 小时记一次，保留 180 天；只跟<b>上一次</b>比，所以这里看到的是「最近 6 小时新长出来的缺陷」。
            </div>
          </div>

          <div class="hd-sec">
            <h4>各项缺陷 / 寿命计数<span class="hd-h4note">累计值 · 只增不减</span></h4>
            <div v-if="counterRows(gradeDisk).length" class="hd-counters">
              <div v-for="c in counterRows(gradeDisk)" :key="c.k" class="hd-counter" :title="c.tip">
                <span class="ck">{{ c.name }}</span>
                <span class="cv" :class="{ hot: c.hot }">{{ c.v }}{{ c.unit }}</span>
              </div>
            </div>
            <div v-else class="hd-none">没有可显示的计数（休眠中或读不到 SMART）。</div>
          </div>

          <div class="hd-sec">
            <h4>硬盘档案<span class="hd-h4note">认盘 / 报修用 · 不参与分级</span></h4>
            <div class="hd-counters">
              <div class="hd-counter" title="硬盘出厂唯一编号，报修、查保修要用它"><span class="ck">序列号</span><span class="cv" :title="gradeDisk.serial || '—'">{{ gradeDisk.serial || '—' }}</span></div>
              <div class="hd-counter" title="这块盘接在哪：阵列卡通道 / 主板直连"><span class="ck">通道</span><span class="cv" :title="gradeDisk.channel || '—'">{{ gradeDisk.channel || '—' }}</span></div>
              <div v-if="gradeDisk.slot" class="hd-counter" title="阵列卡上的物理槽位（对应机箱里的盘位）"><span class="ck">槽位</span><span class="cv" :title="gradeDisk.slot">{{ gradeDisk.slot }}</span></div>
              <div class="hd-counter" title="机械盘每分钟转数；固态盘没有转动部件"><span class="ck">转速</span><span class="cv">{{ gradeDisk.rpm || ((gradeDisk.rota == '1' || gradeDisk.type === 'sas') ? '—' : '固态(SSD)') }}</span></div>
              <div class="hd-counter" title="累计通电运行时间"><span class="ck">已用时长</span><span class="cv">{{ fmtHours(gradeDisk.power_on_hours) }}</span></div>
              <div v-if="gradeDisk.data_units_read" class="hd-counter" title="开机以来累计读取总量（悬停看精确值）"><span class="ck">累计读取量</span><span class="cv" :title="gradeDisk.data_units_read">{{ dataUnitShort(gradeDisk.data_units_read) }}</span></div>
              <div v-if="gradeDisk.data_units_written" class="hd-counter" title="开机以来累计写入总量（悬停看精确值）"><span class="ck">累计写入量</span><span class="cv" :title="gradeDisk.data_units_written">{{ dataUnitShort(gradeDisk.data_units_written) }}</span></div>
              <div v-if="gradeDisk.critical_warning != null" class="hd-counter" title="NVMe 告警标志：0x00 表示一切正常"><span class="ck">临界告警</span><span class="cv" :class="{ hot: cwBad(gradeDisk) }">{{ cwBad(gradeDisk) ? ('0x' + gradeDisk.critical_warning + ' 告警') : '正常' }}</span></div>
            </div>
            <div v-if="gradeDisk.devs && gradeDisk.devs.length > 1" class="hd-note">
              <b>双磁臂硬盘</b>：两个执行器共用一个盘体，系统里会看到 <b>{{ gradeDisk.dev_label }}</b> 两个逻辑盘。已合并成一张卡，各项数值取两臂中<b>更差的一侧</b>；自检、定位等操作默认作用在 <b>{{ gradeDisk.dev }}</b>。
            </div>
          </div>

          <div class="hd-src">
            判定依据来自：{{ gradeDisk.health_grade?.source || '硬盘自身 SMART' }}。全部是硬盘和阵列卡自己上报的数据，nasdash 只做汇总分级，不猜、不做推断。
          </div>
        </div>
        <div class="modal-actions">
          <button class="btn" @click="gradeDev = null">关闭</button>
        </div>
      </div>
    </div>

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

    <!-- ===== 改名弹窗（v2.3.0 3.8） ===== -->
    <div v-if="renameOpen && renameTarget" class="modal-overlay show" @click.self="renameOpen = false">
      <div class="modal-box" style="max-width:420px">
        <div class="modal-title">改名 · {{ renameTarget.dev_label || renameTarget.dev }}</div>
        <div class="modal-body">
          <div style="font-size:13px;color:var(--text-2);margin-bottom:10px">
            给这块盘起个好认的名字（如「数据盘1」「仓库盘」），卡片、健康详情都会优先显示它；原代号保留作副标。留空点保存无效，<b>清除</b>恢复显示代号。重启不丢。
          </div>
          <input
            v-model="renameValue"
            class="rename-input"
            type="text"
            maxlength="40"
            placeholder="如：数据盘1"
            @keyup.enter="saveRename()"
          />
          <div v-if="renameErr" style="color:var(--danger);font-size:12px;margin-top:8px">{{ renameErr }}</div>
        </div>
        <div class="modal-actions">
          <button class="btn" :disabled="renameBusy" @click="renameOpen = false">取消</button>
          <button v-if="renameTarget.custom_name" class="btn" :disabled="renameBusy" @click="saveRename(true)">清除</button>
          <button class="btn btn-primary" :disabled="renameBusy || !renameValue.trim()" @click="saveRename()">{{ renameBusy ? '保存中…' : '保存' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>
