<script setup lang="ts">
/**
 * 风扇控制 —— 复刻旧页 renderFanControl 及其全部交互
 * （旧页 templates/index.html：2245-3470 主体 + 3473-3605 曲线编辑器）。
 *
 * 为什么这页用 v-html + 同名全局函数（而不是结构化 Vue 模板）：
 *   这是一个「大子系统」——风扇卡片 / 曲线编辑器 / 3 套模式体 / 硬盘休眠联动 / 温度概览，
 *   全靠 DOM id 就地操作的命令式代码（滑块 120ms 防抖、5s 轮询就地把 textContent 改掉、
 *   canvas 手动重绘、classList 切换「接管锁定」态）。逐字移植保真度最高、行为与旧页完全一致。
 *   交互函数沿用旧页同名并挂到 window（v-html 里的 inline onclick 才有效），onUnmounted 注销。
 *
 * hero 用 PanelHero（Vue 组件），「接管风扇控制」开关走 #action 插槽；
 * 页面主体走 v-html，整体重渲染靠 :key 强制换 DOM —— 等价旧页 `innerHTML = …`，
 * 保证轮询就地改过的 textContent 一定被重置（否则会出现「旧值残留」）。
 *
 * 数据源：GET /api/system?force=1
 *   后端该方法注释即写明「供 #system 与 #fan 按需刷新」，比 /api/all 轻得多；
 *   返回的 system.sensors.fans 已含 label / voltage / hidden / rule / rule_source /
 *   active_mode / has_curve / pwm_mode / manual_active / target_pct 等全字段。
 * 实时轮询：/api/fan/status 1s（转速 / 占空比 / 目标 / 徽标 / 预设高亮就地更新；
 *          转速数字与 CPU 频率同款秒级刷新，接口是 hwmon 直读、开销很小）
 *          /api/fan/temps  5s（「当前温度」概览 chip）
 * 写接口（全部 POST，后端均 @require_admin）：/api/fan/control、/api/fan/set、
 *          /api/fan/rules、/api/fan/labels（读-改-写整份 map）、/api/fan/disk_temp。
 */
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import PanelHero from '../components/PanelHero.vue'
import { apiFetch } from '../lib/api'
import { ICON_SVG } from '../lib/icons'
import { tempColor } from '../lib/format'
import { pageCacheGet, pageCacheSet } from '../lib/pageCache'

/* ============================== 工具 ============================== */

function esc(s: unknown): string {
  return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
    c === '&' ? '&amp;' : c === '<' ? '&lt;' : c === '>' ? '&gt;' : '&quot;')
}

/** 旧页 iconSvg() 等价物：Vue 壳没有 SVG sprite，直接把 lib/icons.ts 的 path 内联进字符串 */
function iconSvg(name: string): string {
  return '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">' + (ICON_SVG[name] || '') + '</svg>'
}

function fanUid(hwmon: string, idx: number | string): string {
  return 'fan_' + String(hwmon || '').split('/').pop() + '_' + (idx == null ? '' : idx)
}

/* ============================== 状态 ==============================
 * 这些是旧页的模块级全局量（SHOW_HIDDEN_FANS / FAN_CTRL_ENABLED / FAN_ZERO_SINCE …），
 * 移植为组件内部变量；只有驱动 Vue 渲染的部分（hero）才用 ref。
 */

let DATA: any = null
let FAN_LIST: any[] = []
let SHOW_HIDDEN_FANS = false

// 「接管风扇控制」总开关：关闭后 nasdash 仅监控、不写 PWM，控速交还系统原生
let FAN_CTRL_ENABLED = true
let FAN_CTRL_CHANGING = false   // 开关操作进行中，禁止轮询/同步把状态刷回旧值
let FAN_CTRL_SYNCING = false    // 同步请求在途，避免面板重绘时叠发多个请求
let FAN_CTRL_TIMER: any = null  // 开关兜底解锁定时器

let FAN_TOUCH_TS = 0            // 用户最近一次在风扇面板里操作的时间（60s 窗口内视为「正在配置」）
const FAN_ZERO_SINCE: Record<string, number> = {}
const FAN_HIDE_SINCE: Record<string, number> = {}
const FAN_HW_EXPANDED: Record<string, boolean> = {}   // id → 是否展开「说明」详情

let LAST_TEMPS: any = null
const LAST_DISK_TEMPS: Record<string, any> = {}       // dev → {temp, asleep, no_sleep}，防读数抖动
let _fanStatusBusy = false
let _tempsBusy = false
let _touchBound = false
let fanTimer: any = null
let tempTimer: any = null

/* —— hero 用的响应式量 —— */
const pageHtml = ref('')
const renderSeq = ref(0)
const ctrlEnabled = ref(true)
const ctrlChanging = ref(false)
const heroStats = ref<any[]>([])
const lastUpdate = ref('')
const busy = ref(true)
const loadError = ref('')

/* ============================== 数据 ============================== */

function allFans(): any[] {
  return ((DATA && DATA.system && DATA.system.sensors && DATA.system.sensors.fans) || [])
    .filter((f: any) => f.controllable)
}

function computeHeroStats(): void {
  const all = allFans()
  const ctrl = all.length
  const stopped = all.filter(f => f.stopped || (f.rpm || 0) < 1).length
  const rpms = all.map(f => f.rpm || 0).filter(x => x > 0)
  const avg = rpms.length ? Math.round(rpms.reduce((a, b) => a + b, 0) / rpms.length) : 0
  heroStats.value = [
    { v: String(ctrl), k: '可控风扇' },
    { v: String(stopped), k: '停转', cls: stopped ? 'warn' : '' },
    { v: avg ? avg + ' RPM' : '—', k: '平均转速' },
  ]
}

/** 取一份完整风扇数据并整体重渲染（旧页 loadData() 在本页的等价物） */
async function loadFanData(force = true): Promise<void> {
  // 切页签回来先用缓存把整页画出来（秒开），随后 force=1 拉最新替换
  if (!pageHtml.value) {
    const cached = pageCacheGet<typeof DATA>('fan:system')
    if (cached) {
      DATA = cached
      computeHeroStats()
      renderBody()
    }
  }
  busy.value = true
  try {
    const r = await apiFetch('/api/system' + (force ? '?force=1' : ''), 30000)
    DATA = await r.json()
    pageCacheSet('fan:system', DATA)
    loadError.value = ''
  } catch (e) {
    loadError.value = '获取风扇数据失败'
  } finally {
    busy.value = false
  }
  await refreshFanControlState()
  lastUpdate.value = '更新于 ' + new Date().toLocaleTimeString()
  computeHeroStats()
  renderBody()
  startTempPolling()
  startFanStatusPolling()
}

/* ============================== 渲染 ============================== */

/** 整体重渲染页面主体（hero 是 Vue 组件，不在其中） */
function renderBody(): void {
  pageHtml.value = renderFanBody()
  renderSeq.value++
  nextTick(() => { afterRender() })
}

function renderFanBody(): string {
  const all = allFans()
  const hiddenCount = all.filter(f => f.hidden).length
  const fans = SHOW_HIDDEN_FANS ? all : all.filter(f => !f.hidden)
  FAN_LIST = fans

  if (!all.length) {
    return '<div class="card"><div class="loading">未检测到可调控风扇（部分 NAS 由系统固件统一控温，本工具不接管）</div></div>'
  }

  const presets: [string, string][] = [
    ['0', '停转'], ['auto', '默认'], ['100', '全速'],
    ['10', '10%'], ['20', '20%'], ['30', '30%'], ['40', '40%'],
    ['50', '50%'], ['60', '60%'], ['70', '70%'], ['80', '80%'], ['90', '90%'],
  ]
  const presetHtml = presets
    .map(p => `<button class="preset-btn ${p[0] === '100' ? 'full' : ''}" data-preset="${p[0]}" onclick="applyFanPreset('${p[0]}')">${p[1]}</button>`)
    .join('')

  const cards = fans.map(f => fanCardHtml(f)).join('')

  const hideToggle = hiddenCount > 0
    ? `<button class="btn-mini" style="margin-left:8px" onclick="toggleShowHiddenFans()">${SHOW_HIDDEN_FANS ? ('收起已隐藏(' + hiddenCount + ')') : ('显示已隐藏通道(' + hiddenCount + ')')}</button>`
    : ''
  const gridInner = cards || `<div class="loading">可见风扇均已隐藏${hiddenCount > 0 ? '，点上方「显示已隐藏通道」可展开' : ''}。</div>`

  const lock = FAN_CTRL_ENABLED ? ''
    : `<div class="fan-lock-banner">${iconSvg('alert')} 风扇接管已关闭：nasdash 不再根据温度自动调速。若飞牛自带风扇服务已配置好，会由它接管；否则保持当前转速。开启上方「接管风扇控制」才能用下方调速。</div>`

  return `<div class="fan-page${FAN_CTRL_ENABLED ? '' : ' fan-locked'}">
    ${lock}
    ${renderSleepLinkPanel()}
    ${renderTempOverview()}
    <div class="fan-presets">
      <span class="label">一键调速：</span>${presetHtml}
      ${hideToggle}
      <span class="fan-global-state" id="fan-global-state"></span>
    </div>
    <div class="fan-rule-tip">${iconSvg('bulb')} 每张卡片 3 个按钮选控速方案：<b>线性温控</b>（开转/全速 直线）、<b>曲线温控</b>（≥2 节点分段插值）、<b>手动固定</b>（拖滑块）。3 选 1 即生效、互不影响，徽标实时显示当前跑的是哪个。</div>
    <div class="fan-grid">${gridInner}</div>
  </div>`
}

function fanCardHtml(f: any): string {
  const id = fanUid(f.hwmon, f.idx)
  const pct = (f.pwm != null) ? f.pwm : 50
  const isManual = (f.mode === 'manual')
  // 3 按钮：当前激活态由后端 active_mode 决定（linear / curve / manual）
  const activeBtn = isManual ? 'manual' : (f.active_mode || (f.mode === 'curve' ? 'curve' : 'linear'))
  const noCurve = !f.has_curve
  const pi = fanPillInfo(f)

  return `<div class="fan-card" id="${id}-box">
      <div class="fan-card-head">
        <input class="fan-label-input" id="${id}-label" value="${esc(f.label || f.name || '')}" placeholder="自定义名称（如 CPU 风扇）" maxlength="40">
        <span class="pill ${pi.cls}" id="${id}-pill">${pi.txt}</span>
        <span class="fan-label-save">
          <select class="fan-volt-select" id="${id}-volt" title="风扇供电电压">
            <option value="12V" ${f.voltage === '12V' ? 'selected' : ''}>12V</option>
            <option value="5V" ${f.voltage === '5V' ? 'selected' : ''}>5V</option>
            <option value="未知" ${(!f.voltage || f.voltage === '未知') ? 'selected' : ''}>未知</option>
          </select>
          <button class="btn-mini" onclick="saveFanLabel('${f.hwmon}', ${f.idx})">保存标注</button>
          <button class="btn-mini" onclick="toggleFanHidden('${f.hwmon}', ${f.idx}, ${f.hidden ? 'false' : 'true'})" title="隐藏无风扇/无转速的空通道，可随时恢复">${f.hidden ? '取消隐藏' : '隐藏'}</button>
          <span id="${id}-label-state" class="fan-label-state"></span>
        </span>
      </div>
      <div class="fan-rpm" ${f.has_tach ? '' : 'title="读不到转速：可能是一分二分线器的副风扇（转速线未接）、风扇本身未接转速线，或主板未把该通道布线到风扇接口。若此通道无风扇，可点下方『隐藏』把它收起。"'}><span id="${id}-rpm">${f.has_tach ? (f.rpm || 0) : '—'}</span><small id="${id}-rpm-unit">${f.has_tach ? 'RPM' : '无转速信号'}</small></div>
      <div class="fan-hw-note" id="${id}-hwnote" style="display:none">
        <span class="fan-hw-sum" id="${id}-hwsum"></span>
        <button class="fan-hw-toggle" id="${id}-hwtoggle" type="button" onclick="toggleFanHwNote('${id}')">说明 ▾</button>
        <div class="fan-hw-detail" id="${id}-hwdetail" style="display:none"></div>
      </div>
      ${f.pwm_mode === 'dc' ? `<div class="fan-dc-note">${iconSvg('plug')} 此接口当前是 <b>DC（电压）调速</b>：调速靠改供电电压，低档位比 PWM 更容易转不动或直接停转。若风扇低速时抖动、启动不了，把最低转速调高一些，或切回 PWM 调速。</div>` : ''}
      <div class="fan-speed-bar"><div class="fan-speed-fill" id="${id}-bar" style="width:${pct}%"></div></div>
      <div class="fan-pct-line">
        <span>当前 <b id="${id}-cur">${pct}</b>%</span>
        <span>目标 <b id="${id}-tgt"></b></span>
      </div>
      <div class="fan-seg fan-seg-3" id="${id}-seg">
        <button type="button" class="seg-btn ${activeBtn === 'linear' ? 'active' : ''}" data-mode="linear" onclick="setFanMode('${f.hwmon}', ${f.idx}, 'linear')">线性温控</button>
        <button type="button" class="seg-btn ${activeBtn === 'curve' ? 'active' : ''}" data-mode="curve" ${noCurve ? 'title="点进来画一条自己的温度-转速曲线（至少 2 个节点）"' : ''} onclick="setFanMode('${f.hwmon}', ${f.idx}, 'curve')">曲线温控${noCurve ? '<sup class="seg-sup">未设</sup>' : ''}</button>
        <button type="button" class="seg-btn ${activeBtn === 'manual' ? 'active' : ''}" data-mode="manual" onclick="setFanMode('${f.hwmon}', ${f.idx}, 'manual')">手动固定</button>
      </div>
      <div id="${id}-linear" class="fan-mode-body" style="display:${activeBtn === 'linear' ? 'block' : 'none'}">
        ${fanRuleLinearBody(f, id)}
      </div>
      <div id="${id}-curve" class="fan-mode-body" style="display:${activeBtn === 'curve' ? 'block' : 'none'}">
        ${fanRuleCurveBody(f, id)}
      </div>
      <div id="${id}-manual" class="fan-mode-body" style="display:${activeBtn === 'manual' ? 'block' : 'none'}">
        <input type="range" min="0" max="100" value="${pct}" class="fan-slider" data-hwmon="${f.hwmon}" data-idx="${f.idx}" id="${id}-slider">
        <div class="fan-card-actions">
          <span class="fan-custom">
            <label class="curve-pt-input" title="自定义该风扇占空比（0=停转）">
              <input type="number" min="0" max="100" step="1" value="${pct}" class="fan-custom-input" id="${id}-custom" data-hwmon="${f.hwmon}" data-idx="${f.idx}">
              <span class="unit">%</span>
            </label>
            <button class="btn-mini" onclick="applyFanCustom('${f.hwmon}', ${f.idx})">应用</button>
          </span>
          <button class="btn-mini" onclick="setFanMode('${f.hwmon}', ${f.idx}, '${(f.has_curve && f.active_mode === 'curve') ? 'curve' : 'linear'}')">恢复温控</button>
          <span class="fan-card-state" id="${id}-state"></span>
        </div>
      </div>
    </div>`
}

/* —— 每台风扇的温控规则编辑体（线性 / 曲线）—— */

const _RULE_DEFAULT: Record<string, any> = {
  disk: { start: 40, full: 60, min: 30, max: 100, rec: 35 },
  cpu: { start: 45, full: 70, min: 30, max: 100, rec: 40 },
  mb: { start: 45, full: 70, min: 30, max: 100, rec: 40 },
  'combo_max:cpu,mb': { start: 45, full: 70, min: 30, max: 100, rec: 40 },
  'combo_avg:cpu,mb': { start: 45, full: 70, min: 30, max: 100, rec: 40 },
}

function fanRuleSourceRow(f: any, id: string, body: string): string {
  const src = f.rule_source || ''
  const srcOpts: [string, string][] = [
    ['', '关闭（手动/默认）'],
    ['disk', '硬盘温度'],
    ['cpu', 'CPU 温度'],
    ['mb', '主板温度'],
    ['combo_max:cpu,mb', '主板+CPU（取大）'],
    ['combo_avg:cpu,mb', '主板+CPU（平均）'],
  ]
  const opts = srcOpts.map(o => `<option value="${o[0]}" ${src === o[0] ? 'selected' : ''}>${o[1]}</option>`).join('')
  const pre = body + '_'   // 避免 linear / curve 两体的源下拉 id 撞车
  return `<div class="fan-rule-source">
    <label>温度源</label>
    <select class="fan-rule-src" id="${id}-${pre}rsrc" onchange="onRuleSrcChange('${f.hwmon}',${f.idx},'${body}')">${opts}</select>
  </div>`
}

function fanRuleLinearBody(f: any, id: string): string {
  const r = f.rule || null
  const src = f.rule_source || ''
  const d = _RULE_DEFAULT[src] || _RULE_DEFAULT.disk
  const v = (k: string, dv: any) => (r && r[k] != null) ? r[k] : dv
  const sid = id + '-linear_'
  return `<div class="fan-rule fan-rule-linear" id="${id}-rule-l">
    ${fanRuleSourceRow(f, id, 'linear')}
    <div class="fan-rule-fields">
      <span><label>开转</label><input type="number" id="${sid}rstart" min="0" max="110" value="${v('start_temp', d.start)}">°C</span>
      <span><label>全速</label><input type="number" id="${sid}rfull" min="0" max="120" value="${v('full_temp', d.full)}">°C</span>
      <span><label>开转占空</label><input type="number" id="${sid}rmin" min="0" max="100" value="${v('min_pwm', d.min)}">%</span>
      <span><label>全速占空</label><input type="number" id="${sid}rmax" min="0" max="100" value="${v('max_pwm', d.max)}">%</span>
      <span><label>恢复</label><input type="number" id="${sid}rrec" min="0" max="120" value="${v('recover_temp', d.rec)}">°C</span>
    </div>
    <label class="fan-rule-stop"><input type="checkbox" id="${sid}rstop" ${r && r.stop_below_start ? 'checked' : ''}> 低于开转温度即停转（强制 0%）</label>
    <span class="fan-stop-hint">不勾：温度掉到「恢复」温度以下才把这个风扇交还主板自动控制。另外，部分风扇（尤其 CPU 散热器）自带最低转速保护，下发 0% 也停不到底，属于风扇/主板限制——真遇到本卡片会提示。</span>
    <div class="fan-rule-actions">
      <button class="btn-mini" onclick="saveFanRule('${f.hwmon}',${f.idx},'linear')">保存为线性</button>
      <span class="fan-rule-state" id="${sid}rstate"></span>
    </div>
  </div>`
}

function fanRuleCurveBody(f: any, id: string): string {
  const cprefix = id + '-r'
  return `<div class="fan-rule fan-rule-curve" id="${id}-rule-c">
    ${fanRuleSourceRow(f, id, 'curve')}
    <div class="fan-rule-curve-wrap" id="${id}-curve-wrap">
      ${renderCurveEditor(cprefix, (f.rule && f.rule.curve) ? f.rule.curve : null)}
    </div>
    <div class="fan-rule-actions">
      <button class="btn-mini" onclick="saveFanRule('${f.hwmon}',${f.idx},'curve')">保存为曲线</button>
      <span class="fan-rule-state" id="${id}-curve_rstate"></span>
    </div>
    <div class="fan-rule-hint">${iconSvg('bulb')} 至少设 2 个曲线节点（推荐 ≥3 个）。设完后点「保存为曲线」即激活曲线温控。</div>
  </div>`
}

/* —— 硬盘休眠联动面板 —— */

function renderSleepLinkPanel(): string {
  return '<div class="card sleep-panel" id="sleep-link-panel">'
    + '<div class="sleep-panel-head" onclick="toggleSleepPanel()">'
      + '<span>' + iconSvg('power') + '硬盘休眠联动（监控盘休眠/空闲时自动停转受控风扇）</span>'
      + '<span class="sleep-panel-toggle" id="sleep-panel-toggle">▸</span>'
    + '</div>'
    + '<div class="sleep-panel-body" id="sleep-panel-body" style="display:none">'
      + '<label class="dt-toggle"><input type="checkbox" id="sl-sleep"> 监控盘全部休眠时停转风扇</label>'
      + '<label class="dt-toggle">监控盘连续无读写满 <span class="num-stepper"><button type="button" class="ns-btn" onclick="stepIdle(-1)">−</button><input type="text" inputmode="numeric" pattern="[0-9]*" id="sl-idle" maxlength="3" value="5"><button type="button" class="ns-btn" onclick="stepIdle(1)">+</button></span> 分钟也停转风扇</label>'
      + '<div class="dt-actions"><button class="btn-mini" onclick="saveSleepLink()">保存</button><span class="dt-save-state" id="sl-state"></span></div>'
      + '<div class="dt-hint">选「硬盘温度」为温度源的风扇，会在监控盘全部休眠或连续空闲时自动停转。监控盘默认取全部硬盘；此联动仅控制「是否停转」，不影响温度曲线调速。</div>'
      + '<div class="dt-hint-title">硬盘已经休眠，风扇却还在转？按顺序对一下：</div>'
      + '<ol class="dt-hint-list">'
        + '<li><b>先看风扇的温度源</b>：只有温度源选了「硬盘温度」的风扇才吃这个联动。选 CPU / 主板的风扇跟硬盘休不休眠没关系，照常按温度转。</li>'
        + '<li><b>固态盘、NVMe、SAS 企业盘不会休眠</b>，不算在「全部休眠」里。其中 SAS 企业盘和 SATA 固态装在盘位里、风扇吹得到，温度高于开转温度时为安全不停转。</li>'
        + '<li><b>M.2 固态（NVMe）是例外</b>：它自带散热片贴在主板上、被动散热，机箱风扇的风本来就吹不到，而且 50 多度对它是完全正常的工作温度。所以它 65°C 以下不参与温控——既不会催风扇转，也不会拦着风扇停。装了带小风扇的 M.2 主动散热器的话，超过 65°C 一样会正常响应。</li>'
        + '<li><b>只要还有一块机械盘在读写</b>（没满上面设的空闲分钟数）就不停。后台的相册扫描、备份、快照、Docker 都可能一直在戳硬盘。</li>'
        + '<li><b>风扇本身可能停不下来</b>：部分风扇（尤其 CPU 散热器）自带最低转速保护，软件已经下发 0% 也会保持在一个固定转速。真遇到这种，上面对应的风扇卡片会直接标出来提醒你。</li>'
      + '</ol>'
    + '</div>'
  + '</div>'
}

/* —— 当前温度概览（CPU / 主板 / 各硬盘实时温度）—— */

function cpuTempUnified(): number | null {
  if (LAST_TEMPS && LAST_TEMPS.cpu_temp != null) return LAST_TEMPS.cpu_temp
  return (DATA && DATA.system && DATA.system.cpu_temp != null) ? DATA.system.cpu_temp : null
}

function _renderTempChip(name: string, val: any, opts: any): string {
  opts = opts || {}
  const showVal = (val != null) ? val + '°C' : '—'
  const color = opts.asleep ? 'var(--muted)' : tempColor(val, opts.trip || 60)
  let tag = ''
  if (opts.asleep) tag = '<span class="temp-chip-tag off">休眠</span>'
  else if (opts.is_nvme) tag = '<span class="temp-chip-tag off">被动散热</span>'
  else if (opts.no_sleep) tag = '<span class="temp-chip-tag on">常驻</span>'
  const ti = opts.title ? ' title="' + esc(opts.title) + '"' : ''
  return '<span class="temp-chip' + (opts.asleep ? ' off' : '') + '"' + ti + '><span class="temp-chip-name">' + esc(name) + '</span><span class="temp-chip-val" style="color:' + color + '">' + showVal + '</span>' + tag + '</span>'
}

function _buildTempRow(): string {
  if (!LAST_TEMPS) return ''
  const cpu = LAST_TEMPS.cpu_temp, mb = LAST_TEMPS.mb_temp
  const disks = (LAST_TEMPS.disks || [])
  let html = '<div class="temp-cat">'
    + '<span class="temp-cat-label">核心</span>'
    + _renderTempChip('CPU', cpu, { trip: 100 })
    + _renderTempChip('主板', mb, { trip: 90 })
    + '</div>'
  const cats = [{ k: 'NVMe', label: 'NVMe' }, { k: 'SSD', label: 'SSD' }, { k: 'HDD', label: '机械' }]
  let anyDisk = false
  cats.forEach(c => {
    const inCat = disks.filter((d: any) => d.category === c.k)
    if (!inCat.length) return
    anyDisk = true
    inCat.sort((a: any, b: any) => (b.is_system ? 1 : 0) - (a.is_system ? 1 : 0))
    html += '<div class="temp-cat">'
    html += '<span class="temp-cat-label">' + c.label + '</span>'
    inCat.forEach((d: any) => {
      const name = d.name || (d.dev || '').replace(/^\/dev\//, '')
      const isNv = !!d.is_nvme
      const nvStart = d.nvme_start_temp || 65
      html += _renderTempChip(name, d.temp, {
        asleep: !!d.asleep, no_sleep: !!d.no_sleep, is_nvme: isNv,
        // 告警色阈值也要分量纲：机械盘 60°C 该警惕，M.2 固态 60°C 还很凉快
        trip: isNv ? 75 : 60,
        title: isNv
          ? ('M.2 固态多为被动散热（自带散热片、贴在主板上），机箱风扇的气流基本吹不到它，'
             + '而且 ' + nvStart + '°C 以下对固态属于完全正常的工作温度。\n'
             + '因此它不会去催风扇转、也不会阻止风扇停转。只有超过 ' + nvStart + '°C（接近降频保护线）才会参与风扇温控——'
             + '装了带小风扇的 M.2 主动散热器时，这条同样生效。')
          : ''
      })
    })
    html += '</div>'
  })
  if (!anyDisk) html += '<span class="temp-chip-empty">未检测到硬盘</span>'
  return html
}

function renderTempOverview(): string {
  return '<div class="card temp-overview" id="temp-overview">'
    + '<div class="temp-overview-head">' + iconSvg('thermo') + ' 当前温度 <span class="temp-updated" id="temp-updated"></span></div>'
    + '<div class="temp-row" id="temp-row">' + (LAST_TEMPS ? _buildTempRow() : '') + '</div>'
  + '</div>'
}

function _paintTemps(): void {
  const row = document.getElementById('temp-row'); if (!row || !LAST_TEMPS) return
  row.innerHTML = _buildTempRow()
  const up = document.getElementById('temp-updated'); if (up) up.textContent = '· 更新于 ' + new Date().toLocaleTimeString()
}

async function fetchTemps(): Promise<void> {
  if (_tempsBusy) return           // 上一次没回来就跳过，避免请求堆叠挤爆网关
  _tempsBusy = true
  try {
    const r = await apiFetch('/api/fan/temps?_=' + Date.now(), 30000)
    const j = await r.json()
    // 未读出（temp=null）但前一刻读到的盘沿用上次值，避免温度在「44°C」与「N/A」间反复横跳
    const curDevs: Record<string, boolean> = {}
    ;(j.disks || []).forEach((d: any) => {
      const dev = d.dev
      curDevs[dev] = true
      const prev = LAST_DISK_TEMPS[dev]
      if (d.temp == null && prev && !d.asleep) d.temp = prev.temp
      LAST_DISK_TEMPS[dev] = { temp: d.temp, asleep: !!d.asleep, no_sleep: !!d.no_sleep }
    })
    Object.keys(LAST_DISK_TEMPS).forEach(k => { if (!curDevs[k]) delete LAST_DISK_TEMPS[k] })
    LAST_TEMPS = j
    _paintTemps()
    // 就地刷新「检测页 / 温度页 hero」里的 CPU 温度（元素不存在时自动跳过，无副作用）
    try {
      const ct2 = cpuTempUnified()
      const ctStr = ct2 != null ? ct2 + '°C' : 'N/A'
      const e1 = document.getElementById('sys-cpu-temp-kv')
      if (e1) { e1.textContent = ctStr; e1.style.color = tempColor(ct2, 100) }
      const e2 = document.getElementById('hero-cpu-temp-temps'); if (e2) e2.textContent = ctStr
    } catch (e) { /* 忽略 */ }
  } catch (e) { /* 忽略 */ }
  finally { _tempsBusy = false }
}

function startTempPolling(): void {
  if (tempTimer) return
  fetchTemps()
  tempTimer = window.setInterval(fetchTemps, 5000)
}

/* ============================== 非响应式交互 ============================== */

function markFanTouch(): void { FAN_TOUCH_TS = Date.now() }

/** 用户是否正在操作风扇面板：是则跳过整页重建，避免 30s 刷新把正在编辑的表单冲掉 */
function isFanPanelBusy(): boolean {
  const a: any = document.activeElement
  if (a && a.classList && (a.classList.contains('fan-slider') || a.classList.contains('fan-custom-input'))) return true
  if (a && a.closest && a.closest('.fan-card') && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName || '')) return true
  if (FAN_TOUCH_TS && (Date.now() - FAN_TOUCH_TS) < 60000) return true
  const sb = document.getElementById('sleep-panel-body')
  if (sb && sb.style.display === 'block') return true
  return false
}

/** 仅局部更新「接管锁定态」视觉（横幅 / 面板 class），不整面板重绘 */
function applyFanLockVisual(): void {
  try {
    const fp = document.getElementById('fan'); if (!fp) return
    const page = fp.querySelector('.fan-page') as HTMLElement | null
    if (page) page.classList.toggle('fan-locked', !FAN_CTRL_ENABLED)
    let banner = fp.querySelector('.fan-lock-banner') as HTMLElement | null
    if (!FAN_CTRL_ENABLED) {
      if (!banner && page) {
        banner = document.createElement('div')
        banner.className = 'fan-lock-banner'
        banner.innerHTML = iconSvg('alert') + ' 风扇接管已关闭：nasdash 不再根据温度自动调速。若飞牛自带风扇服务已配置好，会由它接管；否则保持当前转速。开启上方「接管风扇控制」才能用下方调速。'
        // 必须挂到 .fan-page 下（渲染时 lock banner 就是 .fan-page 的第一个子节点）
        page.insertBefore(banner, page.firstChild)
      }
    } else if (banner) { banner.remove() }
  } catch (e) { /* 视觉降级即可，绝不阻断开关逻辑 */ }
}

/** 开关兜底解锁：无论成功/失败/异常/超时，都要把「操作中」复位（否则表现为「开关点不动」） */
function releaseFanCtrlLock(): void {
  FAN_CTRL_CHANGING = false
  ctrlChanging.value = false
  if (FAN_CTRL_TIMER) { clearTimeout(FAN_CTRL_TIMER); FAN_CTRL_TIMER = null }
  const el = document.getElementById('fanCtrlToggle') as HTMLInputElement | null
  if (el) el.disabled = false
}

function setFanControl(on: boolean): void {
  if (FAN_CTRL_CHANGING) return    // 上一次还没落定，忽略连点
  // 乐观更新：先按目标状态立即改锁定态视觉，开关瞬间响应，等 POST 结果再微调
  FAN_CTRL_ENABLED = on
  FAN_CTRL_CHANGING = true
  ctrlEnabled.value = on
  ctrlChanging.value = true
  applyFanLockVisual()
  // 兜底看门狗：极端情况（请求被网关吞掉、页面切后台）也要在 10s 后解锁，永不卡死
  FAN_CTRL_TIMER = window.setTimeout(() => { releaseFanCtrlLock(); refreshFanControlState() }, 10000)
  apiFetch('/api/fan/control', 15000, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled: on }),
  }).then(r => r.json()).then((d: any) => {
    releaseFanCtrlLock()
    if (d && d.ok) {
      FAN_CTRL_ENABLED = on; ctrlEnabled.value = on; applyFanLockVisual()
    } else {
      FAN_CTRL_ENABLED = !on; ctrlEnabled.value = !on; applyFanLockVisual()
      alert('操作失败：' + ((d && d.error) || '未知错误'))
    }
  }).catch(() => {
    releaseFanCtrlLock()
    FAN_CTRL_ENABLED = !on; ctrlEnabled.value = !on; applyFanLockVisual()
    alert('网络错误，请重试')
  })
}

function refreshFanControlState(): void {
  // 用户正在切开关时后端状态可能还没落定，此时同步会把它刷回旧状态造成「跳回」
  if (FAN_CTRL_CHANGING || FAN_CTRL_SYNCING) return
  FAN_CTRL_SYNCING = true
  apiFetch('/api/fan/control?_=' + Date.now(), 15000).then(r => r.json()).then((d: any) => {
    FAN_CTRL_SYNCING = false
    if (FAN_CTRL_CHANGING) return
    const en = !!(d && d.enabled)
    if (en !== FAN_CTRL_ENABLED) {
      FAN_CTRL_ENABLED = en
      ctrlEnabled.value = en
      renderBody()
    }
  }).catch(() => { FAN_CTRL_SYNCING = false })
}

function syncFanData(hwmon: string, idx: number, mode: string): void {
  const fans = (DATA && DATA.system && DATA.system.sensors && DATA.system.sensors.fans) || []
  fans.forEach((ff: any) => { if (ff.hwmon === hwmon && ff.idx === idx) ff.mode = mode })
}

function fanSrcLabel(v: any): string {
  if (!v) return ''
  if (String(v).indexOf('disk') === 0) return '硬盘'
  if (v === 'cpu') return 'CPU'
  if (v === 'mb') return '主板'
  return v
}

function setFanPill(id: string, txt: string, cls?: string): void {
  const p = document.getElementById(id + '-pill')
  if (!p) return
  p.textContent = txt
  p.className = 'pill ' + (cls || 'b-info')
}

function toggleFanHwNote(id: string): void {
  FAN_HW_EXPANDED[id] = !FAN_HW_EXPANDED[id]
  const d = document.getElementById(id + '-hwdetail')
  if (d) d.style.display = FAN_HW_EXPANDED[id] ? 'block' : 'none'
  const t = document.getElementById(id + '-hwtoggle')
  if (t) t.textContent = FAN_HW_EXPANDED[id] ? '收起 ▴' : '说明 ▾'
}

/** 「已下发 0% 却仍在转」的折叠提示（硬件最低转速保护） */
function updateFanFloorNote(id: string, f: any): void {
  const el = document.getElementById(id + '-hwnote') as HTMLElement | null
  if (!el) return
  // pwm_enable==1 才是 nasdash 真正在控速；==2 是已交还主板自动，那种情况下读到的 0 不算我们下发的
  const engaged = (f && (f.pwm_enable == 1 || f.pwm_enable === '1'))
  const zeroButSpinning = f && engaged && f.has_tach && (f.pwm != null && f.pwm <= 0) && (f.rpm > 0)
  if (zeroButSpinning) {
    FAN_HIDE_SINCE[id] = 0
    if (!FAN_ZERO_SINCE[id]) FAN_ZERO_SINCE[id] = Date.now()
    // 风扇从高转降到停需要几秒惯性，8 秒后仍在转才判定为「硬件停不下来」
    if (Date.now() - FAN_ZERO_SINCE[id] < 8000) return
    el.style.display = 'flex'
    const sum = document.getElementById(id + '-hwsum')
    if (sum) sum.innerHTML = iconSvg('alert') + ' 已下发 0%，但此风扇仍在转（硬件最低转速保护，软件停不下来）'
    const d = document.getElementById(id + '-hwdetail')
    if (d) {
      d.style.display = FAN_HW_EXPANDED[id] ? 'block' : 'none'
      d.innerHTML = '多为风扇自带最低转速保护或主板对该接口锁了下限，属<b>硬件限制</b>，软件无法再降。'
        + '<b>临时绕过：</b>把该风扇「低于开转温度即停转（强制 0%）」的勾去掉，温度低时降到 min_pwm 即可，不再卡在尴尬的中间转速。'
        + '<b>真正停转：</b>进 BIOS 找 Fan Stop / 0 RPM 相关选项，或更换支持 0 转停转的风扇。'
    }
    const t = document.getElementById(id + '-hwtoggle')
    if (t) t.textContent = FAN_HW_EXPANDED[id] ? '收起 ▴' : '说明 ▾'
  } else {
    FAN_ZERO_SINCE[id] = 0
    if (el.style.display !== 'none') {
      // 当前显示中：记退场时间，1.5s 后才真正隐藏（防硬件在「0 ↔ 下限」间抖时反复闪）
      if (!FAN_HIDE_SINCE[id]) FAN_HIDE_SINCE[id] = Date.now()
      if (Date.now() - FAN_HIDE_SINCE[id] < 1500) return
    }
    FAN_HIDE_SINCE[id] = 0
    el.style.display = 'none'
  }
}

/** 状态标签的唯一真源：整页渲染与 5s 轮询必须共用这一份 */
function fanPillInfo(f: any): { txt: string; cls: string } {
  if (!f) return { txt: '', cls: 'b-info' }
  if (f.mode === 'manual' || f.manual_active) return { txt: '手动固定', cls: 'b-warn' }
  if (!f.rule) return { txt: '未设温控', cls: 'b-muted' }
  const am = f.active_mode || (f.mode === 'curve' ? 'curve' : 'linear')
  if (am === 'curve' && !f.has_curve) return { txt: '曲线温控（待设曲线）', cls: 'b-muted' }
  const src = fanSrcLabel(f.rule_source)
  return { txt: (am === 'curve' ? '曲线温控' : '线性温控') + (src ? ('·' + src) : ''), cls: 'b-info' }
}

async function setFanMode(hwmon: string, idx: number, mode: string): Promise<void> {
  markFanTouch()
  const id = fanUid(hwmon, idx)
  const seg = document.getElementById(id + '-seg')
  if (seg) seg.querySelectorAll('.seg-btn').forEach((b: any) => { b.classList.toggle('active', b.dataset.mode === mode) })
  const linearBox = document.getElementById(id + '-linear') as HTMLElement | null
  const curveBox = document.getElementById(id + '-curve') as HTMLElement | null
  const manBox = document.getElementById(id + '-manual') as HTMLElement | null
  if (linearBox) linearBox.style.display = 'none'
  if (curveBox) curveBox.style.display = 'none'
  if (manBox) manBox.style.display = 'none'

  if (mode === 'manual') {
    if (manBox) manBox.style.display = 'block'
    const sl = document.getElementById(id + '-slider') as HTMLInputElement | null
    const cust = document.getElementById(id + '-custom') as HTMLInputElement | null
    let v = sl ? parseInt(sl.value, 10) : (cust ? parseInt(cust.value, 10) : 50)
    if (isNaN(v)) v = 50
    await applyFan(hwmon, idx, v)
    syncFanData(hwmon, idx, 'manual')
    setFanPill(id, '手动固定', 'b-warn')
  } else if (mode === 'linear') {
    if (linearBox) linearBox.style.display = 'block'
    await setFanAuto(hwmon, idx, 'linear')
    syncFanData(hwmon, idx, 'auto')
    const lsel = document.getElementById(id + '-linear_rsrc') as HTMLSelectElement | null
    const lsrc = lsel ? fanSrcLabel(lsel.value) : ''
    setFanPill(id, lsrc ? ('线性温控·' + lsrc) : '未设温控', lsrc ? 'b-info' : 'b-muted')
  } else if (mode === 'curve') {
    if (curveBox) {
      curveBox.style.display = 'block'
      // 首次展开时初始化曲线节点表格 + 曲线图（DOM 已由 fanRuleCurveBody 渲染好）
      renderCurvePoints(id + '-r')
      drawCurve(id + '-r')
    }
    // 切 active_mode='curve' 前先校验曲线 ≥2 节点，否则后端会回退到线性
    const pts = readCurve(id + '-r')
    if (pts.length < 2) {
      // 没曲线：提示用户先设曲线，但仍切到「曲线」按钮（让用户能继续编辑）
      setFanPill(id, '曲线温控（待设曲线）', 'b-muted')
    } else {
      await setFanAuto(hwmon, idx, 'curve')
      syncFanData(hwmon, idx, 'auto')
      const csel = document.getElementById(id + '-curve_rsrc') as HTMLSelectElement | null
      const csrc = csel ? fanSrcLabel(csel.value) : ''
      setFanPill(id, csrc ? ('曲线温控·' + csrc) : '曲线温控', 'b-info')
    }
  }
}

async function onRuleSrcChange(hwmon: string, idx: number, body: string): Promise<void> {
  body = body || 'linear'
  const id = fanUid(hwmon, idx)
  const pre = body + '_'
  const sel = document.getElementById(id + '-' + pre + 'rsrc') as HTMLSelectElement | null
  if (!sel) return
  const src = sel.value
  // 下拉框是「温度源开关」：选「关闭」清规则；选温度源按该源推荐默认值立即落盘，
  // 否则刷新整页重渲染会把未保存的选择打回原样（用户反馈：点关闭刷新后又回到温度联动）。
  if (src) {
    const d = _RULE_DEFAULT[src] || _RULE_DEFAULT.disk
    const setv = (elid: string, val: any) => { const e = document.getElementById(elid) as HTMLInputElement | null; if (e) e.value = val }
    setv(id + '-' + pre + 'rstart', d.start); setv(id + '-' + pre + 'rfull', d.full)
    setv(id + '-' + pre + 'rmin', d.min); setv(id + '-' + pre + 'rmax', d.max); setv(id + '-' + pre + 'rrec', d.rec)
  }
  await saveFanRule(hwmon, idx, body === 'curve' ? 'curve' : 'linear')
}

async function saveFanRule(hwmon: string, idx: number, activeMode?: string): Promise<void> {
  const id = fanUid(hwmon, idx)
  const body = activeMode || 'linear'
  const pre = body + '_'
  const st = document.getElementById(id + '-' + pre + 'rstate')
  const sel = document.getElementById(id + '-' + pre + 'rsrc') as HTMLSelectElement | null
  const key = hwmon + '::' + idx
  const src = sel ? sel.value : ''
  const body4: any = { key: key }
  if (!src) {
    body4.rule = null    // 关闭 → 清除该风扇联动
  } else {
    const pts = readCurve(id + '-r')
    let start: number, full: number, minp: number, maxp: number, rec: number, stopv: boolean
    if (body === 'curve') {
      // 曲线体没有线性输入框（旧页同源缺陷：曾致「保存为曲线」静默失败、从未发出 POST）。
      // 基线字段直接从曲线节点推导，与后端缺省语义对齐（start_temp 缺省取首点温度、recover 取 start-5）。
      if (!pts || pts.length < 2) {
        if (st) { st.textContent = '请先添加至少 2 个曲线节点'; st.className = 'fan-rule-state' }
        return
      }
      const temps = pts.map((p: any) => Number(p[0]))
      const pwms = pts.map((p: any) => Number(p[1]))
      start = Math.min(...temps)
      full = Math.max(...temps)
      if (!(full > start)) {
        if (st) { st.textContent = '曲线节点温度需有跨度（至少两个不同温度）'; st.className = 'fan-rule-state' }
        return
      }
      minp = Math.min(...pwms)
      maxp = Math.max(...pwms)
      rec = Math.max(0, start - 5)
      stopv = false
    } else {
      const gv = (elid: string) => parseFloat((document.getElementById(elid) as HTMLInputElement).value)
      start = gv(id + '-' + pre + 'rstart')
      full = gv(id + '-' + pre + 'rfull')
      minp = gv(id + '-' + pre + 'rmin')
      maxp = gv(id + '-' + pre + 'rmax')
      rec = gv(id + '-' + pre + 'rrec')
      const stopEl = document.getElementById(id + '-' + pre + 'rstop') as HTMLInputElement | null
      stopv = stopEl ? stopEl.checked : false
      if (!(full > start)) { if (st) { st.textContent = '全速温度须大于开转温度'; st.className = 'fan-rule-state' } return }
      if (!(rec < start)) { if (st) { st.textContent = '恢复温度须小于开转温度'; st.className = 'fan-rule-state' } return }
    }
    // 保留旧 active_mode：仅当「保存为线性/曲线」显式指定时才覆盖（老配置可能没这字段 → 曲线优先推断）
    const fans = (DATA && DATA.system && DATA.system.sensors && DATA.system.sensors.fans) || []
    const f = fans.find((x: any) => x.hwmon === hwmon && x.idx === idx)
    const curActive = (f && f.active_mode) || ((f && f.rule && f.rule.curve && f.rule.curve.length >= 2) ? 'curve' : 'linear')
    const newActive = activeMode || curActive
    body4.rule = { enabled: true, source: src, start_temp: start, full_temp: full, min_pwm: minp, max_pwm: maxp, recover_temp: rec, stop_below_start: stopv, curve: pts, active_mode: newActive }
  }
  if (st) { st.textContent = '保存中…'; st.className = 'fan-rule-state' }
  try {
    const r = await apiFetch('/api/fan/rules', 20000, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body4),
    })
    const j = await r.json()
    if (st) {
      st.innerHTML = j.ok ? (iconSvg('check') + ' 已保存') : ('失败：' + (j.error || ''))
      st.className = j.ok ? 'fan-rule-state ok' : 'fan-rule-state'
    }
    if (j.ok) {
      // 同步缓存中该风扇的 active_mode（避免刷新前徽标/按钮高亮错位）
      const fans = (DATA && DATA.system && DATA.system.sensors && DATA.system.sensors.fans) || []
      if (body4.rule) {
        fans.forEach((ff: any) => {
          if (ff.hwmon === hwmon && ff.idx === idx) {
            ff.active_mode = body4.rule.active_mode
            ff.rule = body4.rule
            ff.has_curve = !!(body4.rule.curve && body4.rule.curve.length >= 2)
          }
        })
      }
      // 曲线已成型：抹掉「曲线温控」按钮上的「未设」角标
      if (body4.rule && body4.rule.curve && body4.rule.curve.length >= 2) {
        const curveBtn = document.querySelector('#' + id + '-seg [data-mode="curve"]') as HTMLElement | null
        if (curveBtn) { curveBtn.classList.remove('disabled'); curveBtn.removeAttribute('title'); curveBtn.textContent = '曲线温控' }
      }
      // 顶部标签同步成刚保存的这套方案
      if (!body4.rule) setFanPill(id, '未设温控', 'b-muted')
      else setFanPill(id, (body4.rule.active_mode === 'curve' ? '曲线温控' : '线性温控') + '·' + fanSrcLabel(src), 'b-info')

      if (activeMode) {
        await setFanAuto(hwmon, idx, undefined)   // 已经保存了 active_mode，无需再发
        syncFanData(hwmon, idx, 'auto')
      } else {
        await loadFanData(false)                  // 旧页此处 loadData()
      }
    }
  } catch (e) {
    if (st) { st.textContent = '请求失败（需管理员登录）'; st.className = 'fan-rule-state' }
  }
}

async function applyFanPreset(val: string): Promise<void> {
  const gs = document.getElementById('fan-global-state')
  if (!FAN_LIST.length) return
  if (val === 'auto') {
    if (gs) gs.textContent = '正在恢复自动控温…'
    for (const f of FAN_LIST) await setFanAuto(f.hwmon, f.idx)
    if (gs) gs.textContent = '已全部交还自动控温'
  } else {
    const pwm = parseInt(val, 10)
    if (gs) gs.textContent = '正在设为 ' + pwm + '%…'
    for (const f of FAN_LIST) await applyFan(f.hwmon, f.idx, pwm)
    if (gs) gs.textContent = '已全部设为 ' + pwm + '%'
  }
  highlightPreset(val)
}

function clearPresetHighlight(): void {
  document.querySelectorAll('.preset-btn[data-preset]').forEach(b => b.classList.remove('active'))
}
function highlightPreset(val: string): void {
  clearPresetHighlight()
  const b = document.querySelector('.preset-btn[data-preset="' + val + '"]')
  if (b) b.classList.add('active')
}
/** 根据各卡真实目标/模式，高亮当前统一生效的档位（无统一档位则不高亮） */
function updatePresetHighlight(fans: any[]): void {
  clearPresetHighlight()
  if (!fans || !fans.length) return
  const manualFans = fans.filter(f => f.mode !== 'auto' && f.mode !== 'off')
  if (manualFans.length === 0) {
    const b = document.querySelector('.preset-btn[data-preset="auto"]')
    if (b) b.classList.add('active')
    return
  }
  const targets = manualFans.map(f => (f.target_pct != null ? f.target_pct : f.pwm)).filter(p => p != null)
  if (targets.length === manualFans.length && targets.every((t: any) => t === targets[0])) {
    const b = document.querySelector('.preset-btn[data-preset="' + targets[0] + '"]')
    if (b) b.classList.add('active')
  }
}

async function applyFanCustom(hwmon: string, idx: number): Promise<void> {
  const id = fanUid(hwmon, idx)
  const inp = document.getElementById(id + '-custom') as HTMLInputElement | null
  if (!inp) return
  let v = parseInt(inp.value, 10)
  if (isNaN(v)) return
  v = Math.max(0, Math.min(100, v))
  inp.value = String(v)
  await applyFan(hwmon, idx, v)
}

async function applyFan(hwmon: string, idx: number, pwm: number): Promise<void> {
  const id = fanUid(hwmon, idx)
  const st = document.getElementById(id + '-state')
  if (st) st.textContent = '目标 ' + pwm + '%，平滑过渡中…'
  try {
    const r = await apiFetch('/api/fan/set', 20000, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hwmon: hwmon, idx: idx, pwm: pwm }),
    })
    const j = await r.json()
    if (st) st.textContent = j.ok ? ('目标 ' + pwm + '%，平滑过渡中') : ('失败：' + (j.error || ''))
    if (j.ok) syncFanData(hwmon, idx, 'manual')
  } catch (e) { if (st) st.textContent = '请求失败' }
}

async function setFanAuto(hwmon: string, idx: number, activeMode?: string): Promise<void> {
  // activeMode: 'linear' | 'curve' | undefined（保留兼容）
  const id = fanUid(hwmon, idx)
  const st = document.getElementById(id + '-state')
  if (st) st.textContent = '恢复中…'
  try {
    // 1) 切到 auto 控速（清除手动覆盖）
    const r = await apiFetch('/api/fan/set', 20000, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hwmon: hwmon, idx: idx, mode: 'auto' }),
    })
    const j = await r.json()
    const owner = j.owner === 'ext_service' ? '（已交还系统风扇服务）' : '（nasdash 接管温控）'
    if (st) st.textContent = j.ok ? ('已恢复自动 ' + owner) : ('失败：' + (j.error || ''))
    if (j.ok) syncFanData(hwmon, idx, 'auto')
    // 2) 同步 active_mode 到 fan_rules（如有规则）
    //    注意：此处沿用旧页口径 —— 查的是 id+'-rsrc'，而卡片里实际是 '-linear_rsrc'/'-curve_rsrc'，
    //    故该分支在旧页同样是「查不到即跳过」；不改行为，以免与旧页配置语义分叉。
    if (activeMode && j.ok) {
      const srcEl = document.getElementById(id + '-rsrc') as HTMLSelectElement | null
      const src = srcEl ? srcEl.value : ''
      if (src) {
        const fans = (DATA && DATA.system && DATA.system.sensors && DATA.system.sensors.fans) || []
        const f = fans.find((x: any) => x.hwmon === hwmon && x.idx === idx)
        const curRule = (f && f.rule) || {}
        curRule.active_mode = activeMode
        const r2 = await apiFetch('/api/fan/rules', 20000, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: hwmon + '::' + idx, rule: curRule }),
        })
        const j2 = await r2.json()
        if (!j2.ok) {
          const st2 = document.getElementById(id + '-rstate')
          if (st2) { st2.textContent = '保存方案失败：' + (j2.error || ''); st2.className = 'fan-rule-state' }
        }
      }
    }
  } catch (e) { if (st) st.textContent = '请求失败' }
}

async function saveFanLabel(hwmon: string, idx: number): Promise<void> {
  const id = fanUid(hwmon, idx)
  const labInp = document.getElementById(id + '-label') as HTMLInputElement | null
  const voltSel = document.getElementById(id + '-volt') as HTMLSelectElement | null
  const ntChk = document.getElementById(id + '-notach') as HTMLInputElement | null
  const st = document.getElementById(id + '-label-state')
  if (!labInp || !voltSel) return
  try {
    const r = await apiFetch('/api/fan/labels', 20000)
    const cur = await r.json()
    const k = hwmon + '::' + idx
    const e = cur[k] || {}
    e.name = labInp.value.trim(); e.voltage = voltSel.value   // 保留 e.hidden 不被覆盖
    if (ntChk) e.no_tach = !!ntChk.checked
    cur[k] = e
    const r2 = await apiFetch('/api/fan/labels', 20000, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cur),
    })
    const j = await r2.json()
    if (st) st.innerHTML = j.ok ? (iconSvg('check') + ' 已保存') : '失败'
    if (j.ok) await loadFanData(false)
  } catch (e) { if (st) st.textContent = '请求失败' }
}

async function toggleFanHidden(hwmon: string, idx: number, hide: boolean): Promise<void> {
  try {
    const r = await apiFetch('/api/fan/labels', 20000)
    const cur = await r.json()
    const k = hwmon + '::' + idx
    const e = cur[k] || {}
    e.hidden = !!hide; cur[k] = e
    await apiFetch('/api/fan/labels', 20000, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cur),
    })
    // 本地即时更新缓存并重渲染面板，避免走全量重载导致点击后卡顿等待整页刷新
    const fans = (DATA && DATA.system && DATA.system.sensors && DATA.system.sensors.fans) || []
    fans.forEach((f: any) => { if (f.hwmon === hwmon && f.idx === idx) f.hidden = !!hide })
    if (!hide) SHOW_HIDDEN_FANS = true   // 取消隐藏后确保该风扇仍可见
    refreshFanPanel()
  } catch (e) { /* 忽略 */ }
}

function toggleShowHiddenFans(): void {
  SHOW_HIDDEN_FANS = !SHOW_HIDDEN_FANS
  refreshFanPanel()
}

/** 只重渲主体（不动 hero）。用户正在输入时跳过重建，避免把正在编辑的框换掉 */
function refreshFanPanel(): void {
  const ae: any = document.activeElement
  if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'SELECT' || ae.tagName === 'TEXTAREA')) return
  renderBody()
}

/* —— 硬盘休眠联动 —— */

function toggleSleepPanel(): void {
  const b = document.getElementById('sleep-panel-body')
  const t = document.getElementById('sleep-panel-toggle')
  if (!b) return
  const show = (b.style.display !== 'block')
  b.style.display = show ? 'block' : 'none'
  if (t) t.textContent = show ? '▾' : '▸'
  if (show) initSleepLink()
  markFanTouch()
}

async function initSleepLink(): Promise<void> {
  try {
    const r = await apiFetch('/api/fan/disk_temp?_=' + Date.now(), 20000)
    const j = await r.json()
    const c = j.config || {}
    const sl = document.getElementById('sl-sleep') as HTMLInputElement | null
    if (sl) sl.checked = (c.sleep_stop !== false)
    const idle = document.getElementById('sl-idle') as HTMLInputElement | null
    if (idle) idle.value = String(c.idle_minutes != null ? c.idle_minutes : 5)
  } catch (e) { /* 忽略 */ }
}

function stepIdle(delta: number): void {
  const el = document.getElementById('sl-idle') as HTMLInputElement | null
  if (!el) return
  let v = parseInt(el.value, 10); if (isNaN(v)) v = 5
  v = Math.min(120, Math.max(1, v + delta))
  el.value = String(v)
  el.dispatchEvent(new Event('input', { bubbles: true }))
  markFanTouch()
}

async function saveSleepLink(): Promise<void> {
  const st = document.getElementById('sl-state')
  const sl = document.getElementById('sl-sleep') as HTMLInputElement | null
  const sleep = sl ? sl.checked : true
  const idleEl = document.getElementById('sl-idle') as HTMLInputElement | null
  const idle = idleEl ? parseFloat(idleEl.value) : 5
  if (st) { st.textContent = '保存中…'; st.className = 'dt-save-state' }
  try {
    const r = await apiFetch('/api/fan/disk_temp', 20000, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sleep_stop: sleep, idle_minutes: idle }),
    })
    const j = await r.json()
    if (st) {
      st.innerHTML = j.ok ? (iconSvg('check') + ' 已保存') : ('失败：' + (j.error || ''))
      st.className = j.ok ? 'dt-save-state ok' : 'dt-save-state'
    }
  } catch (e) { if (st) { st.textContent = '请求失败'; st.className = 'dt-save-state' } }
}

/* ============================== 5s 状态轮询 ============================== */

async function fetchFanStatus(): Promise<void> {
  if (_fanStatusBusy) return    // 上一次没回来就跳过：避免请求无限堆叠把网关挤爆
  _fanStatusBusy = true
  try {
    const r = await apiFetch('/api/fan/status?_=' + Date.now(), 30000)
    if (!r || !r.ok) return
    const j = await r.json()
    if (!j || !j.fans) return
    ;(j.fans || []).forEach((f: any) => {
      const id = fanUid(f.hwmon, f.idx)
      const cur = document.getElementById(id + '-cur')
      const tgt = document.getElementById(id + '-tgt')
      const rpm = document.getElementById(id + '-rpm')
      const sl = document.getElementById(id + '-slider') as HTMLInputElement | null
      if (cur) cur.textContent = (f.pwm != null ? f.pwm : '--')
      if (tgt) tgt.textContent = (f.target_pct != null && f.target_pct !== f.pwm)
        ? ('→ 目标 ' + f.target_pct + '%')
        : (f.mode === 'auto' ? '· 自动控温' : (f.mode === 'sys_temp' ? '· 系统温度控' : (f.mode === 'disk_temp' ? '· 硬盘温度控' : '')))
      if (rpm) rpm.textContent = (f.has_tach ? (f.rpm || 0) : '—')
      const unit = document.getElementById(id + '-rpm-unit'); if (unit) unit.textContent = (f.has_tach ? 'RPM' : '无转速信号')
      const bar = document.getElementById(id + '-bar')
      if (bar) bar.style.width = (f.pwm != null ? f.pwm : 50) + '%'
      // 实时刷新状态徽标（必须走 fanPillInfo，与整页渲染同一份逻辑）；
      // 例外：用户刚操作完（60s 触摸窗内）不覆盖，避免把即时反馈冲掉
      if (!isFanPanelBusy()) {
        const pi = fanPillInfo(f)
        setFanPill(id, pi.txt, pi.cls)
      }
      // 「已下发 0% 却仍在转」检测（部分风扇自带最低转速保护）
      updateFanFloorNote(id, f)
      if (sl && document.activeElement !== sl) {
        // 手动模式下滑块停在用户设定的目标值（避免松手后回弹误以为没生效）；自动模式跟随实时值
        sl.value = String((f.mode === 'manual' && f.target_pct != null) ? f.target_pct : (f.pwm != null ? f.pwm : 50))
      }
    })
    updatePresetHighlight(j.fans)
    // hero 的「平均转速 / 停转」跟着实时数据走（旧页此处靠整页重载才更新）
    const fans0 = (DATA && DATA.system && DATA.system.sensors && DATA.system.sensors.fans) || []
    ;(j.fans || []).forEach((f: any) => {
      fans0.forEach((ff: any) => {
        if (ff.hwmon === f.hwmon && ff.idx === f.idx) { ff.rpm = f.rpm; ff.stopped = (f.rpm || 0) < 1 }
      })
    })
    computeHeroStats()
  } catch (e) { /* 忽略 */ }
  finally { _fanStatusBusy = false }
}

function startFanStatusPolling(): void {
  if (fanTimer) return
  fetchFanStatus()
  // 1 秒一拍（与系统资源页 CPU 频率同款秒级刷新）；_fanStatusBusy 防堆叠，请求慢时自动跳过
  fanTimer = window.setInterval(fetchFanStatus, 1000)
}

/* ============================== 曲线编辑器 ============================== */

const CURVE_PRESETS: Record<string, number[][]> = {
  silent: [[30, 20], [45, 30], [60, 45], [75, 70]],
  balanced: [[30, 30], [45, 45], [60, 65], [75, 90]],
  perf: [[30, 45], [45, 65], [60, 85], [75, 100]],
}

function renderCurveEditor(prefix: string, initCurve: any): string {
  return `<div class="dt-section">
    <div class="dt-section-title">自定义温度→PWM 曲线 <span class="badge b-info">可选</span></div>
    <div style="font-size:12px;color:var(--muted);margin-bottom:6px">不设置则使用上方「开转/全速」线性映射。设了曲线后，按曲线分段插值调速（点击预设一键生成，可再微调每个节点）。</div>
    <div class="curve-presets">
      <button class="preset-btn" onclick="applyCurvePreset('${prefix}','silent')">静音</button>
      <button class="preset-btn" onclick="applyCurvePreset('${prefix}','balanced')">均衡</button>
      <button class="preset-btn" onclick="applyCurvePreset('${prefix}','perf')">性能</button>
      <button class="preset-btn" onclick="applyCurvePreset('${prefix}','clear')">清除曲线</button>
    </div>
    <input type="hidden" id="${prefix}-curve-data" value="${initCurve ? JSON.stringify(initCurve) : '[]'}">
    <div id="${prefix}-curve-pts"></div>
    <button class="btn-mini" onclick="addCurvePoint('${prefix}')">+ 添加节点</button>
    <canvas id="${prefix}-curve-canvas" class="curve-canvas"></canvas>
  </div>`
}

function applyCurvePreset(prefix: string, kind: string): void {
  const data = document.getElementById(prefix + '-curve-data') as HTMLInputElement | null; if (!data) return
  const pts = kind === 'clear' ? [] : (CURVE_PRESETS[kind] || []).map(p => p.slice())
  data.value = JSON.stringify(pts)
  renderCurvePoints(prefix)
  drawCurve(prefix)
  // 分段按钮的「未设」角标与顶部 pill 是在整页渲染时按 f.has_curve 算的，
  // 编辑器内清空/预设切换不会触发重绘，需在此手动同步
  const id = prefix.replace(/-r$/, '')
  const curveBtn = document.querySelector('#' + id + '-seg [data-mode="curve"]') as HTMLElement | null
  const hasEnough = pts.length >= 2
  if (curveBtn) {
    if (hasEnough) { curveBtn.innerHTML = '曲线温控'; curveBtn.removeAttribute('title') }
    else { curveBtn.innerHTML = '曲线温控<sup class="seg-sup">未设</sup>'; curveBtn.setAttribute('title', '点进来画一条自己的温度-转速曲线（至少 2 个节点）') }
  }
  if (!hasEnough) setFanPill(id, '未设温控', 'b-muted')
}

function renderCurvePoints(prefix: string): void {
  const data = document.getElementById(prefix + '-curve-data') as HTMLInputElement | null
  const box = document.getElementById(prefix + '-curve-pts')
  if (!data || !box) return
  let pts: any[] = []; try { pts = JSON.parse(data.value || '[]') } catch (e) { /* 忽略 */ }
  if (!pts.length) { box.innerHTML = '<div class="dt-empty" style="padding:6px">未设置曲线，使用线性映射</div>'; return }
  const rows = pts.map((p, i) =>
    '<tr>'
    + '<td class="curve-pt-num">' + (i + 1) + '</td>'
    + '<td><label class="curve-pt-input"><input type="number" class="cp-temp" data-i="' + i + '" value="' + p[0] + '" min="20" max="95" step="1"><span class="unit">°C</span></label></td>'
    + '<td><label class="curve-pt-input"><input type="number" class="cp-pwm" data-i="' + i + '" value="' + p[1] + '" min="0" max="100" step="1"><span class="unit">%</span></label></td>'
    + '<td style="text-align:right"><button class="curve-pt-del" type="button" title="删除该节点" onclick="removeCurvePoint(\'' + prefix + '\',' + i + ')">' + iconSvg('x') + '</button></td>'
    + '</tr>'
  ).join('')
  box.innerHTML = '<table class="curve-pt-table">'
    + '<thead><tr><th>#</th><th>温度</th><th>占空比</th><th></th></tr></thead>'
    + '<tbody>' + rows + '</tbody></table>'
  box.querySelectorAll('input').forEach(inp => {
    inp.addEventListener('change', () => {
      const i = parseInt((inp as HTMLInputElement).dataset.i as string)
      const tEl = box.querySelector('.cp-temp[data-i="' + i + '"]') as HTMLInputElement
      const pEl = box.querySelector('.cp-pwm[data-i="' + i + '"]') as HTMLInputElement
      try {
        const arr = JSON.parse(data.value || '[]')
        arr[i] = [parseFloat(tEl.value) || 0, parseFloat(pEl.value) || 0]
        data.value = JSON.stringify(arr)
      } catch (e) { /* 忽略 */ }
      drawCurve(prefix)
    })
  })
}

function addCurvePoint(prefix: string): void {
  const data = document.getElementById(prefix + '-curve-data') as HTMLInputElement | null; if (!data) return
  let pts: any[] = []; try { pts = JSON.parse(data.value || '[]') } catch (e) { /* 忽略 */ }
  let last = pts.length ? pts[pts.length - 1][0] + 10 : 50
  if (last > 92) last = 92
  pts.push([last, 60])
  data.value = JSON.stringify(pts)
  renderCurvePoints(prefix)
  drawCurve(prefix)
  markFanTouch()
}

function removeCurvePoint(prefix: string, i: number): void {
  const data = document.getElementById(prefix + '-curve-data') as HTMLInputElement | null; if (!data) return
  let pts: any[] = []; try { pts = JSON.parse(data.value || '[]') } catch (e) { /* 忽略 */ }
  if (i >= 0 && i < pts.length) pts.splice(i, 1)
  data.value = JSON.stringify(pts)
  renderCurvePoints(prefix)
  drawCurve(prefix)
  markFanTouch()
}

function readCurve(prefix: string): any[] {
  try { return JSON.parse((document.getElementById(prefix + '-curve-data') as HTMLInputElement).value || '[]') }
  catch (e) { return [] }
}

function drawCurve(prefix: string): void {
  const cv = document.getElementById(prefix + '-curve-canvas') as HTMLCanvasElement | null; if (!cv) return
  const data = document.getElementById(prefix + '-curve-data') as HTMLInputElement | null; if (!data) return
  let pts: any[] = []; try { pts = JSON.parse(data.value || '[]') } catch (e) { /* 忽略 */ }
  const dpr = window.devicePixelRatio || 1
  const cssW = cv.clientWidth, cssH = cv.clientHeight
  if (!cssW || !cssH) return   // 面板隐藏时 clientWidth=0，跳过绘制，避免用兜底尺寸画出被 CSS 拉伸变形的图
  cv.width = Math.round(cssW * dpr); cv.height = Math.round(cssH * dpr)
  const ctx = cv.getContext('2d'); if (!ctx) return
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, cssW, cssH)
  ctx.strokeStyle = 'rgba(128,128,128,.18)'; ctx.lineWidth = 1
  ctx.strokeRect(22, 10, cssW - 32, cssH - 30)
  ctx.fillStyle = '#8a93a6'; ctx.font = '10px sans-serif'
  ctx.fillText('PWM%', 4, 16); ctx.fillText('温度℃', cssW - 44, cssH - 4)
  if (pts.length) {
    const minT = 20, maxT = 95
    const sorted = pts.slice().sort((a, b) => a[0] - b[0])
    ctx.strokeStyle = '#4aa3ff'; ctx.lineWidth = 2; ctx.beginPath()
    sorted.forEach((p, i) => {
      const x = 22 + (cssW - 32) * ((Math.min(Math.max(p[0], minT), maxT) - minT) / (maxT - minT))
      const y = 10 + (cssH - 30) * (1 - p[1] / 100)
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)
    })
    ctx.stroke()
    ctx.fillStyle = '#ff7a59'
    sorted.forEach(p => {
      const x = 22 + (cssW - 32) * ((Math.min(Math.max(p[0], minT), maxT) - minT) / (maxT - minT))
      const y = 10 + (cssH - 30) * (1 - p[1] / 100)
      ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.fill()
    })
  }
}

/* ============================== 渲染后处理 ============================== */

/** 每次整体重渲染后：绑定触摸监听 / 滑块 / 曲线图 / 起轮询 */
function afterRender(): void {
  bindFanRootTouch()
  // 首屏就处于「曲线温控」的风扇：立刻把节点表格和曲线图画出来（隐藏的等切过去时再画，避免 display:none 量不到宽度）
  ;(FAN_LIST || []).forEach(f => {
    const fid = fanUid(f.hwmon, f.idx)
    const cb = document.getElementById(fid + '-curve') as HTMLElement | null
    if (cb && cb.style.display === 'block') {
      try { renderCurvePoints(fid + '-r'); drawCurve(fid + '-r') } catch (e) { /* 忽略 */ }
    }
  })
  document.querySelectorAll('.fan-slider').forEach(sl => {
    sl.addEventListener('input', () => {
      clearPresetHighlight()
      const el = sl as HTMLInputElement
      const id = fanUid(el.dataset.hwmon as string, el.dataset.idx as string)
      const v = el.value
      const cur = document.getElementById(id + '-cur'); if (cur) cur.textContent = v
      const tgt = document.getElementById(id + '-tgt'); if (tgt) tgt.textContent = '→ 目标 ' + v + '%'
      const anyEl = el as any
      clearTimeout(anyEl._t)
      anyEl._t = setTimeout(() => applyFan(el.dataset.hwmon as string, parseInt(el.dataset.idx as string, 10), parseInt(el.value, 10)), 120)
    })
  })
  document.querySelectorAll('.fan-custom-input').forEach(inp => {
    inp.addEventListener('keydown', (e: any) => {
      if (e.key === 'Enter') {
        e.preventDefault()
        const el = inp as HTMLInputElement
        applyFanCustom(el.dataset.hwmon as string, parseInt(el.dataset.idx as string, 10))
      }
    })
  })
}

/** 事件委托挂在常驻容器 #fan 上（重渲染不影响它），记录用户最近操作时间 */
function bindFanRootTouch(): void {
  const fanRoot = document.getElementById('fan')
  if (fanRoot && !_touchBound) {
    _touchBound = true
    ;['click', 'input', 'change', 'focusin'].forEach(ev => {
      fanRoot.addEventListener(ev, markFanTouch, true)
    })
  }
}

/* ============================== 生命周期 / 接线 ============================== */

function onToggleCtrl(e: Event): void {
  const t = e.target as HTMLInputElement
  setFanControl(!!t.checked)
}

const GLOBALS: Record<string, any> = {
  setFanControl, refreshFanControlState, setFanMode, saveFanRule, onRuleSrcChange,
  applyFanPreset, applyFanCustom, applyFan, setFanAuto, saveFanLabel, toggleFanHidden,
  toggleShowHiddenFans, toggleFanHwNote, toggleSleepPanel, stepIdle, saveSleepLink,
  applyCurvePreset, addCurvePoint, removeCurvePoint, refreshFanPanel,
}

onMounted(async () => {
  Object.keys(GLOBALS).forEach(k => { (window as any)[k] = GLOBALS[k] })
  bindFanRootTouch()
  await loadFanData(true)
})

onUnmounted(() => {
  Object.keys(GLOBALS).forEach(k => { try { delete (window as any)[k] } catch (e) { /* 忽略 */ } })
  if (fanTimer) { clearInterval(fanTimer); fanTimer = null }
  if (tempTimer) { clearInterval(tempTimer); tempTimer = null }
  if (FAN_CTRL_TIMER) { clearTimeout(FAN_CTRL_TIMER); FAN_CTRL_TIMER = null }
  _touchBound = false
  // 清掉「接管锁定」补丁可能留下的横幅（它是命令式 insert 的，不在 Vue 管辖内）
  try {
    const fp = document.getElementById('fan')
    const banner = fp && fp.querySelector('.fan-lock-banner')
    if (banner) banner.remove()
  } catch (e) { /* 忽略 */ }
})
</script>

<template>
  <div id="fan">
    <PanelHero
      icon="fan"
      title="风扇温控"
      sub="预设档位 / 实时转速 / 曲线调速"
      :stats="heroStats"
      :last-update="lastUpdate"
      :busy="busy"
      @refresh="loadFanData(true)"
    >
      <template #action>
        <label class="hero-switch" title="关闭后 nasdash 不再根据温度自动调速。若飞牛自带风扇服务已配置好，会交还它接管；否则保持当前转速，不再写 PWM。">
          <input id="fanCtrlToggle" type="checkbox" :checked="ctrlEnabled" :disabled="ctrlChanging" @change="onToggleCtrl" />
          <span class="hero-switch-track"><span class="hero-switch-knob" /></span>
          <span class="hero-switch-txt">接管风扇控制</span>
        </label>
      </template>
    </PanelHero>

    <div v-if="loadError" class="man-loaderr">{{ loadError }}</div>
    <div :key="renderSeq" v-html="pageHtml" />
  </div>
</template>
