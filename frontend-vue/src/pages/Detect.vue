<script setup lang="ts">
/**
 * 阶段 3 · 硬件配置检测（Vue 原生页）
 *
 * 复刻 templates/index.html 的 renderDetect（老页 line 3756-4172）：
 *  - 快照：/api/all（system + raid + disks + time）—— 检测页是整机概览，与老页同走全量
 *  - 实时：/api/metrics 每 1s（CPU 全核平均频率，蓝色 .js-cpu-freq-live）
 *          /api/fan/temps 每 5s（CPU 温度 #sys-cpu-temp-kv）
 * 交互（网口 Tab / 阵列卡定位·一致性检查·热备·CopyBack / 定时巡检）沿用老页同名全局函数挂到 window，
 * 因此 v-html 内的 inline onclick 与老页逐字一致；这些函数全部走 apiFetch（带 index.cgi 前缀，子路径下也通）。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import PanelHero from '../components/PanelHero.vue'
import type { HeroStat } from '../components/PanelHero.vue'
import { apiFetch } from '../lib/api'
import { pageCacheGet, pageCacheSet } from '../lib/pageCache'
import { tempColor } from '../lib/format'

// ============ 通用小工具（与老页 templates/index.html 同源口径）============
function esc(s: unknown): string {
  return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
    c === '&' ? '&amp;' : c === '<' ? '&lt;' : c === '>' ? '&gt;' : '&quot;',
  )
}
function parseSizeToB(s: string): number {
  if (!s) return 0
  const m = String(s).match(/([\d.]+)\s*([TGM])B?/i)
  if (!m) return 0
  const v = parseFloat(m[1])
  const u = m[2].toUpperCase()
  return u === 'T' ? v * 1e12 : u === 'G' ? v * 1e9 : v * 1e6
}
function fmtBToSize(b: number): string {
  if (b >= 1e12) return (b / 1e12).toFixed(1) + 'T'
  if (b >= 1e9) return Math.round(b / 1e9) + 'G'
  if (b >= 1e6) return Math.round(b / 1e6) + 'M'
  return '?'
}
/** 双磁臂硬盘：同一序列号被系统拆成多个设备 → 合并成一块显示（纯前端） */
function mergeDualActuator(list: any[]): any[] {
  if (!Array.isArray(list)) return list || []
  const byKey: Record<string, any[]> = {}
  const out: any[] = []
  list.forEach((d: any) => {
    const sn = (d.serial || '').trim().toUpperCase()
    if (!sn) {
      out.push(d)
      return
    }
    const key = sn + '|' + (d.model || '') + '|' + (d.type || '').toUpperCase()
    ;(byKey[key] = byKey[key] || []).push(d)
  })
  Object.keys(byKey).forEach(key => {
    const arr = byKey[key]
    if (arr.length > 1) {
      const first = arr[0]
      const devs = arr.map((x: any) => x.dev)
      const totalB = arr.reduce((s: number, x: any) => s + parseSizeToB(x.size), 0)
      const validTemps = arr.map((x: any) => x.temp).filter((x: any) => x != null)
      const merged = Object.assign({}, first)
      merged.dev = devs.join(' + ')
      merged.arms = devs
      merged.dual_actuator = true
      merged.size = fmtBToSize(totalB)
      merged.health_ok = arr.every((x: any) => x.health_ok)
      merged.health = arr.some((x: any) => !x.health_ok) ? '异常' : first.health || 'OK'
      merged.temp = validTemps.length ? Math.max.apply(null, validTemps) : null
      out.push(merged)
    } else {
      out.push(arr[0])
    }
  })
  return out
}
function diskFeatureClean(feat: any, isDualActuator: boolean): string {
  if (!feat) return ''
  if (isDualActuator) return ''
  return String(feat).replace(/双磁臂?\(双执行器\)|双执行器/g, '').trim()
}
/** 一行 kv；v 为已格式化字符串或 null；note 为该字段不适用时的说明 */
function gpuRow(k: string, v: any, note?: string | null): string {
  if (v != null && v !== '') {
    return `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`
  }
  if (note) {
    return `<div class="kv"><span class="k">${esc(k)}</span><span class="v" style="color:var(--muted);font-size:12px">${esc(note)}</span></div>`
  }
  return `<div class="kv"><span class="k">${esc(k)}</span><span class="v">—</span></div>`
}
function pcieLocText(pci: string): string {
  if (!pci) return ''
  const m = pci.match(/^([0-9a-f]{2}):([0-9a-f]{2})\.(\d)$/i)
  if (!m) return '主板 PCIe 扩展槽'
  const bus = parseInt(m[1], 16)
  const dev = parseInt(m[2], 16)
  const fn = parseInt(m[3], 10)
  return `主板 PCIe 扩展槽（总线 ${bus} · 设备 ${dev}${fn ? ' · 功能 ' + fn : ''}）`
}
// 老页 sprite #i-check；Vue 壳无 sprite，内联同款 24x24 图标（.ico 描边样式由 panel.css 提供）
const CHECK_SVG =
  '<svg class="ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg>'

// ============ 交互（沿用老页全局函数名，供 v-html 内 inline onclick 调用）============
// WOL 开启时的 BIOS 提醒文案（模板与拨动时都要用，抽成常量避免两处不一致）
const WOL_BIOS_HINT = '关机唤不醒？检查主板 BIOS 里的 Wake on LAN（并把 ErP 省电关掉）'
let _nicIdx = 0
const _locateOn: Record<string, boolean> = {}
const _ccTimers: Record<string, number> = {}

// 网卡 WOL 开关（v2.3.0 3.2）：调后端写 ethtool，成功后由后端持久化、开机回放
async function toggleWol(name: string, enable: boolean, el: HTMLInputElement): Promise<void> {
  const box = el.closest('.kv')
  const st = box ? (box.querySelector('.wol-sw-state') as HTMLElement | null) : null
  el.disabled = true
  if (st) {
    st.textContent = '设置中…'
    st.style.color = 'var(--muted)'
  }
  try {
    const r = await apiFetch('/api/network/wol', 20000, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, enable }),
    })
    const j = await r.json()
    if (j && j.ok) {
      if (st) {
        st.textContent = enable ? '已开启' : '已关闭'
        st.style.color = enable ? 'var(--green)' : 'var(--muted)'
      }
      // BIOS 提示跟着开关走：开了就补上、关了就去掉（与整页渲染口径一致）
      if (box) {
        const cur = box.querySelector('.wol-hint') as HTMLElement | null
        const ctl = box.querySelector('.wol-ctl')
        if (enable && !cur && ctl && ctl.parentNode) {
          const sp = document.createElement('span')
          sp.className = 'wol-hint'
          sp.textContent = WOL_BIOS_HINT
          ctl.parentNode.insertBefore(sp, ctl)
        } else if (!enable && cur) {
          cur.remove()
        }
      }
    } else {
      el.checked = !enable
      if (st) {
        st.textContent = j && j.error ? String(j.error) : '设置失败'
        st.style.color = 'var(--red)'
      }
    }
  } catch (e) {
    el.checked = !enable
    if (st) {
      st.textContent = '请求失败'
      st.style.color = 'var(--red)'
    }
  } finally {
    el.disabled = false
  }
}

function switchSysNic(idx: number): void {
  _nicIdx = idx
  document.querySelectorAll('.sys-nic-tab').forEach(x => x.classList.remove('active'))
  const tb = document.querySelector('.sys-nic-tab[data-sys-nic-idx="' + idx + '"]')
  if (tb) tb.classList.add('active')
  document.querySelectorAll('.sys-nic-detail').forEach((d: any) => (d.style.display = 'none'))
  const el = document.getElementById('sys-nic-detail-' + idx)
  if (el) el.style.display = 'block'
}

function raidLocate(slot: string): void {
  const on = !_locateOn[slot]
  const action = on ? 'start' : 'stop'
  let btn: any = null
  try {
    btn = document.querySelector('[data-locate-slot="' + slot + '"]')
  } catch {
    /* 忽略 */
  }
  if (btn) {
    btn.disabled = true
    btn.textContent = '定位中…'
  }
  apiFetch('/api/raid/locate', 30000, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ slot, action }),
  })
    .then(r => r.json())
    .then((j: any) => {
      if (btn) btn.disabled = false
      if (j.ok) {
        _locateOn[slot] = on
        if (btn) btn.textContent = on ? '停止闪灯' : '定位闪灯'
      } else {
        if (btn) btn.textContent = '定位闪灯'
        alert('定位失败：' + (j.error || (j.out ? j.out.slice(0, 120) : '未知')))
      }
    })
    .catch((e: any) => {
      if (btn) {
        btn.disabled = false
        btn.textContent = '定位闪灯'
      }
      alert('请求失败：' + e.message)
    })
}

function ccId(vd: string): string {
  return vd.replace('/', '_')
}
function raidCC(vd: string, action: string): void {
  if (action === 'start' && !confirm('一致性检查会在后台扫描「' + vd + '」，期间磁盘负载升高，是否继续？')) return
  apiFetch('/api/raid/cc', 30000, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ vd, action }),
  })
    .then(r => r.json())
    .then((j: any) => {
      if (!j.ok) {
        alert('操作失败：' + (j.error || '未知'))
        return
      }
      if (action === 'start') startCCPoll(vd)
      else if (action === 'stop') stopCCPoll(vd)
      else startCCPoll(vd) // pause/resume 继续轮询
    })
    .catch((e: any) => alert('请求失败：' + e.message))
}
function startCCPoll(vd: string): void {
  stopCCPoll(vd)
  const id = ccId(vd)
  _ccTimers[vd] = window.setInterval(() => {
    apiFetch('/api/raid/cc?vd=' + encodeURIComponent(vd), 30000)
      .then(r => r.json())
      .then((j: any) => {
        if (!j.ok) {
          stopCCPoll(vd)
          return
        }
        const st = document.getElementById('cc-state-' + id)
        const bar: any = document.getElementById('cc-bar-' + id)
        const pct = document.getElementById('cc-pct-' + id)
        if (st) st.textContent = ({ idle: '空闲', running: '运行中', paused: '已暂停' } as any)[j.state] || j.state
        if (bar) bar.style.width = (j.progress || 0) + '%'
        if (pct) pct.textContent = j.progress != null ? j.progress + '%' : '—'
        if (j.state === 'idle') stopCCPoll(vd)
      })
      .catch(() => stopCCPoll(vd))
  }, 3000)
}
function stopCCPoll(vd: string): void {
  if (_ccTimers[vd]) {
    clearInterval(_ccTimers[vd])
    _ccTimers[vd] = 0
  }
}
function saveCCSchedule(): void {
  const t = (document.getElementById('ccsched-time') as HTMLInputElement | null)?.value || '00:00'
  const parts = t.split(':')
  const data = {
    enabled: (document.getElementById('ccsched-enable') as HTMLInputElement | null)?.checked,
    period: (document.getElementById('ccsched-period') as HTMLSelectElement | null)?.value,
    hour: parseInt(parts[0], 10) || 0,
    minute: parseInt(parts[1], 10) || 0,
    weekday: parseInt((document.getElementById('ccsched-weekday') as HTMLSelectElement | null)?.value || '0', 10) || 0,
  }
  apiFetch('/api/raid/cc/schedule', 30000, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
    .then(r => r.json())
    .then((j: any) => {
      if (!j.ok) alert('保存失败：' + (j.error || '未知'))
      else alert('定时巡检调度已保存')
    })
    .catch((e: any) => alert('请求失败：' + e.message))
}
function raidHotspare(slot: string | null, action: string): void {
  if (!slot) slot = (document.getElementById('hs-slot') as HTMLSelectElement | null)?.value || null
  if (!slot) {
    alert('请先选择一块盘')
    return
  }
  if (action !== 'remove' && !confirm('将 ' + slot + ' 设为' + (action === 'add_dedicated' ? '专用' : '全局') + '热备？该盘数据将被清空。')) return
  apiFetch('/api/raid/hotspare', 60000, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ slot, action }),
  })
    .then(r => r.json())
    .then((j: any) => {
      if (!j.ok) alert('操作失败：' + (j.error || '未知'))
      else {
        alert('操作成功')
        location.reload()
      }
    })
    .catch((e: any) => alert('请求失败：' + e.message))
}
function saveCopyBack(): void {
  const el = document.getElementById('cb-enable') as HTMLInputElement | null
  const action = el && el.checked ? 'enable' : 'disable'
  apiFetch('/api/raid/copyback', 30000, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action }),
  })
    .then(r => r.json())
    .then((j: any) => {
      if (!j.ok) alert('操作失败：' + (j.error || '未知'))
      else {
        alert('已' + (action === 'enable' ? '开启' : '关闭') + '自动 CopyBack')
        location.reload()
      }
    })
    .catch((e: any) => alert('请求失败：' + e.message))
}
function raidCopyBack(slot: string | null, action: string): void {
  if (!slot) slot = (document.getElementById('cb-slot') as HTMLSelectElement | null)?.value || null
  if (!slot) {
    alert('请先在上方选择一块故障盘')
    return
  }
  if (!confirm('将对 ' + slot + ' 手动触发 CopyBack 换盘？该操作须已配置热备盘。')) return
  apiFetch('/api/raid/copyback', 60000, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ slot, action }),
  })
    .then(r => r.json())
    .then((j: any) => {
      if (!j.ok) alert('操作失败：' + (j.error || '未知'))
      else {
        alert('已触发 CopyBack')
        location.reload()
      }
    })
    .catch((e: any) => alert('请求失败：' + e.message))
}

// 网卡卡内联 tabs
function renderSysNetCard(nics: any[] | undefined): string {
  const sysNics = (nics || []).filter((n: any) => {
    const nm = (n.name || '').toLowerCase()
    return !/^(lo|ovs-system|docker|virbr|br-)|veth/.test(nm)
  })
  if (!sysNics.length) return `<h3>网络</h3><div class="loading">未检测到网卡</div>`
  if (_nicIdx >= sysNics.length) _nicIdx = 0
  const tabs = sysNics
    .map(
      (_n: any, i: number) =>
        `<button class="sys-nic-tab nic-tab ${i === _nicIdx ? 'active' : ''}" data-sys-nic-idx="${i}" onclick="switchSysNic(${i})">网口${i + 1}</button>`,
    )
    .join('')
  const details = sysNics
    .map((n: any, i: number) => {
      const st = (n.state || '').toUpperCase()
      const sc = st === 'UP' ? 'var(--green)' : 'var(--red)'
      const sp = n.speed ? n.speed + ' Mbps' : 'N/A'
      const duplex = n.duplex ? n.duplex + '双工' : ''
      const cfg = [sp, duplex, n.mtu ? 'MTU ' + n.mtu : ''].filter(Boolean).join(' · ')
      const hw = [n.model, n.driver ? '驱动 ' + n.driver : '', n.bus_info ? '总线 ' + n.bus_info : ''].filter(Boolean).join(' · ')
      // 「未跑满」提示：网卡支持速率 > 当前协商速率（且在线）时给出，提示瓶颈在对端/网线
      const _curSp = parseInt(n.speed || '', 10)
      const _maxSp = parseInt(n.max_speed || '', 10)
      const capHint = st === 'UP' && _maxSp && _curSp && _maxSp > _curSp
        ? `<div class="nic-cap-hint">此网卡最高支持 ${_maxSp} Mbps，当前仅协商到 ${_curSp} Mbps，未跑满。瓶颈通常在对端网口（交换机 / 路由器）或网线——两端都需支持 ${_maxSp} 才能跑满。</div>`
        : ''
      const ipv6 = n.ipv6 || '无'
      // WOL（网络唤醒）只读状态（v2.3.0 3.2）：后端解析 ethtool 的 Supports/Wake-on
      const wolKnown =
        (n.wol_supported !== null && n.wol_supported !== undefined) || !!n.wol_support
      const wolTxt = !wolKnown
        ? '未知'
        : !n.wol_supported
          ? '不支持'
          : n.wol_enabled
            ? '支持（已开启）'
            : '支持（未开启）'
      const wolColor = wolKnown && n.wol_supported
        ? n.wol_enabled
          ? 'var(--green)'
          : 'var(--orange)'
        : 'var(--muted)'
      const wolTitle =
        '网络唤醒：关机或休眠后，可从局域网发一条指令远程开机。需网卡支持 + 主板 BIOS 里也开启才有效'
      // 硬件支持 WOL 的口才给开关；不支持/未知只显示文字
      const wolRow =
        wolKnown && n.wol_supported
          ? `<div class="kv"><span class="k" title="${wolTitle}">WOL 网络唤醒</span><span class="v wol-v">${n.wol_enabled ? `<span class="wol-hint">${WOL_BIOS_HINT}</span>` : ''}<span class="wol-ctl"><span class="wol-sw-state">${n.wol_enabled ? '已开启' : '已关闭'}</span><label class="wol-sw"><input type="checkbox" ${n.wol_enabled ? 'checked' : ''} onchange="toggleWol('${esc(n.name)}', this.checked, this)"><i class="wol-sw-ui"></i></label></span></span></div>`
          : `<div class="kv"><span class="k" title="${wolTitle}">WOL 网络唤醒</span><span class="v" style="color:${wolColor}">${esc(wolTxt)}</span></div>`
      return `<div class="sys-nic-detail nic-detail" id="sys-nic-detail-${i}" style="${i === _nicIdx ? '' : 'display:none'}">
      <div class="nic-ifname">${esc(n.name)}</div>
      <div class="kv"><span class="k" title="设备在当前局域网里的网络门牌号">IPv4 地址</span><span class="v">${esc(n.ip || '无 IP')}</span></div>
      <div class="kv"><span class="k" title="新一代网络地址，部分网络环境才用得到">IPv6 地址</span><span class="v">${esc(ipv6)}</span></div>
      <div class="kv"><span class="k" title="网卡出厂自带的唯一物理编号，相当于网卡的身份证">MAC 地址</span><span class="v">${esc(n.mac || '-')}</span></div>
      <div class="kv"><span class="k">网络配置</span><span class="v">${esc(cfg)}</span></div>
      ${hw ? `<div class="kv"><span class="k">硬件信息</span><span class="v">${esc(hw)}</span></div>` : ''}
      ${wolRow}
      ${capHint}
      <div class="kv"><span class="k">状态</span><span class="v" style="color:${sc}">${esc(st)}</span></div>
    </div>`
    })
    .join('')
  return `<h3>网络</h3><div class="nic-tabs">${tabs}</div>${details}`
}

// ============ 内存条详情弹窗（v2.3.0 第10步 ① SPD 完整解码：点击内存卡看完整 SPD）============
let _memMods: any[] = []
let _memBoard: any = {}
function showMemDetail(i: number): void {
  const m = _memMods[i]
  if (!m || !m.installed) return
  const ov = document.getElementById('memDetailModal')
  const title = document.getElementById('memDetailTitle')
  const body = document.getElementById('memDetailBody')
  if (!ov || !title || !body) return
  title.textContent = '内存详情 · ' + (m.locator || 'DIMM')
  const SPD_NA = '不适用（该内存条未写入）'
  const DMI_NA = '不适用（dmidecode 未返回）'
  const srcTxt = m.source === 'spd' ? 'SPD 直读（decode-dimms + 原始字节）' : 'dmidecode -t 17'
  const kv = (k: string, v: string, mode: 'spd' | 'dmi' | 'none' = 'none', span2 = false, na = '') => {
    let naTxt: string
    if (na) naTxt = '不适用（' + na + '）'
    else if (mode === 'spd') naTxt = SPD_NA
    else if (mode === 'dmi') naTxt = DMI_NA
    else naTxt = '—'
    const val = v
      ? `<span class="v">${esc(v)}</span>`
      : `<span class="v"><span style="color:var(--muted);font-size:12px">${esc(naTxt)}</span></span>`
    return `<div class="kv${span2 ? ' span2' : ''}"><span class="k">${esc(k)}</span>${val}</div>`
  }
  // 组内两列排（长值行用 span2 通栏），避免单列过长导致默认窗口下显示不全
  const grp = (h: string, rows: string) =>
    `<div class="mem-grp"><div class="mem-grp-h">${esc(h)}</div><div class="mem-grp-body">${rows}</div></div>`
  let html = ''
  html += grp('基本',
    kv('容量', m.size || '') +
    kv('类型', m.type || '') +
    kv('额定频率', m.speed || '') +
    kv('实际运行频率', m.cfg_speed || '', 'dmi') +
    kv('ECC', m.ecc || '', 'dmi') +
    kv('插槽 / 通道', m.locator || '')
  )
  html += grp('SPD 详情（decode-dimms 直读）',
    kv('模组厂商', m.manufacturer || m.brand || '') +
    kv('颗粒厂商', m.dram_manufacturer || '') +
    kv('型号（料号）', m.part || '', 'spd') +
    kv('生产日期', m.manufacture_date || '', 'spd') +
    kv('序列号', m.serial || '', 'spd')
  )
  html += grp('DMI 详情（dmidecode -t 17）',
    kv('Rank（颗粒）', m.rank || '', 'dmi') +
    kv('总位宽', m.total_width || '', 'dmi') +
    kv('数据位宽', m.data_width || '', 'dmi') +
    kv('电压', m.voltage || '', 'dmi') +
    kv('外形规格', m.form_factor || '', 'dmi')
  )
  const b = _memBoard || {}
  let edacRow: string
  if (b.edac && b.edac.available) {
    edacRow = kv('EDAC 内存错误', `ce(可纠正)=${b.edac.ce} · ue(不可纠正)=${b.edac.ue}`, 'none', true)
  } else {
    edacRow = `<div class="kv span2"><span class="k">EDAC 内存错误</span><span class="v"><span style="color:var(--muted);font-size:12px">本机未启用 EDAC（消费级主板常见）</span></span></div>`
  }
  html += grp('板级 / ECC',
    kv('最大支持容量', b.max_capacity || '', 'dmi') +
    kv('插槽总数', b.num_devices || '', 'dmi') +
    kv('板级 ECC 类型', b.ecc_type || '', 'dmi') +
    edacRow +
    kv('数据来源', srcTxt, 'none', true)
  )
  body.innerHTML = html
  ov.classList.add('show')
}
function closeMemDetail(): void {
  const ov = document.getElementById('memDetailModal')
  if (ov) ov.classList.remove('show')
}


// ============ 主体渲染（复刻 renderDetect，去掉老页 hero —— 由 PanelHero 承担）============
function buildBody(D: any): string {
  if (!D) return ''
  const r = D.raid || {}
  const disks: any[] = D.disks || []
  const s = D.system || {}
  const pwColor = (w: number | null) =>
    w == null ? 'var(--muted)' : w > 40 ? 'var(--red)' : w > 20 ? 'var(--orange)' : 'var(--green)'
  // 系统配置
  const cpuPct =
    s.cpu_usage != null
      ? s.cpu_usage
      : s.cpu_threads
        ? Math.min(Math.round(((parseFloat((s.load && s.load[0]) || 0) / s.cpu_threads) * 100) || 0), 100)
        : 0
  const ci = s.cpu_info || {}
  const memMods = (s.memory_modules && s.memory_modules.modules) || []
  const mm = s.memory_modules
  const memSummary = mm
    ? `插槽 ${mm.installed}/${mm.slots}` +
      (mm.brand_summary && mm.brand_summary !== '未知' ? ` · ${mm.brand_summary}` : '') +
      (mm.empty > 0 ? ` · ${mm.empty} 空槽` : '')
    : ''
  const chA = memMods.some((m: any) => /ChannelA/i.test(m.locator) && m.installed)
  const chB = memMods.some((m: any) => /ChannelB/i.test(m.locator) && m.installed)
  const chanTxt = chA && chB ? '双通道' : chA || chB ? '单通道' : '—'
  const memSlotHtml = memMods
    .map((m: any, i: number) => {
      const ch = /ChannelA/i.test(m.locator) ? 'A' : /ChannelB/i.test(m.locator) ? 'B' : ''
      if (m.installed) {
        return `<div class="mem-slot filled" onclick="showMemDetail(${i})" title="点击查看完整 SPD 信息">
        <div class="ms-ch">通道 ${ch}</div>
        <div class="ms-size">${m.size || '-'}</div>
        <div class="ms-info">${m.brand || m.manufacturer || '内存'}${m.speed ? ' · ' + m.speed : ''}</div>
        ${m.dram_manufacturer ? `<div class="ms-info" style="opacity:.7">颗粒 ${m.dram_manufacturer}</div>` : ''}
        <div class="ms-more">详情 ›</div>
      </div>`
      }
      return `<div class="mem-slot empty"><div class="ms-ch">通道 ${ch}</div><div class="ms-size" style="color:var(--muted)">空槽</div></div>`
    })
    .join('')
  const memCard = `
  <div class="card" style="grid-column:1/-1">
    <h3>内存</h3>
    <div class="kv"><span class="k">总量</span><span class="v">${(s.memory && s.memory.total) || '-'}</span></div>
    <div class="kv"><span class="k">内存插槽</span><span class="v">${memSummary || '-'}</span></div>
    <div class="mem-slots">${memSlotHtml}</div>
    <div style="margin-top:8px;font-size:12px;color:var(--muted)">通道 ${chanTxt} · 品牌取自 SPD 直读；灰色为空槽 · 实时占用率见「系统资源」页</div>
  </div>`
  const bm = (s.board && s.board.manufacturer) || ''
  const boardHasDMI = bm && bm.toLowerCase() !== 'default string' && bm.toLowerCase() !== 'to be filled by o.e.m.'
  const boardCard = `
  <div class="card">
    <h3>主板</h3>
    ${
      boardHasDMI
        ? `
    <div class="kv"><span class="k">品牌</span><span class="v">${(s.board && s.board.manufacturer) || '-'}</span></div>
    <div class="kv"><span class="k">型号</span><span class="v">${(s.board && s.board.product) || '-'}</span></div>
    <div class="kv"><span class="k">版本</span><span class="v">${(s.board && s.board.version) || '-'}</span></div>
    <div class="kv"><span class="k" title="主板最底层的启动程序版本">BIOS</span><span class="v">${(s.board && s.board.bios_vendor) || '-'} ${(s.board && s.board.bios_version) || ''} (${(s.board && s.board.bios_date) || ''})</span></div>
    <div class="kv"><span class="k">芯片组</span><span class="v">${(s.board && s.board.chipset) || '未知'}</span></div>
    `
        : `
    <div class="kv"><span class="k">芯片组</span><span class="v">${(s.board && s.board.chipset) || '未知'}</span></div>
    <div class="kv"><span class="k">说明</span><span class="v" style="font-size:12px;color:var(--orange)">${(s.board && s.board.note) || 'BIOS 未写入主板信息'}</span></div>
    `
    }
  </div>`
  const sysInfoCard = `
  <div class="card">
    <h3>系统信息</h3>
    <div class="kv"><span class="k">设备名称</span><span class="v">${s.hostname || '-'}</span></div>
    <div class="kv"><span class="k">系统</span><span class="v">${D.fnos_version ? esc(D.fnos_version) + ' (Debian 12 底层)' : s.os || '-'}</span></div>
    <div class="kv"><span class="k" title="系统最核心的程序版本，相当于手机底层的操作系统">内核</span><span class="v">${s.kernel || '-'}</span></div>
    <div class="kv"><span class="k">运行时间</span><span class="v">${s.uptime || '-'}</span></div>
    <div class="kv"><span class="k">虚拟内存</span><span class="v">${s.swap && s.swap.used ? s.swap.used + ' / ' + s.swap.total : '-'}</span></div>
  </div>`
  const gpuCard = `
  <div class="card">
    <h3>显卡</h3>
    ${
      s.gpus && s.gpus.length
        ? s.gpus
            .map((g: any) => {
              const isIgpu = g.type === '核显'
              let rows = `<div class="kv"><span class="k">${esc(g.type || '显卡')}</span><span class="v"${g.name_full || g.name_arch ? ` title="完整设备名：${esc(g.name_full || g.name)}${g.name_arch ? ' (' + g.name_arch + ')' : ''}"` : ''}>${esc(g.name || '')}</span></div>`
              // 常驻：GPU 温度
              const tcol = g.temp != null ? tempColor(g.temp, 95) : 'var(--muted)'
              rows += gpuRow('GPU 温度', g.temp != null ? '<span style="color:' + tcol + '">' + g.temp + '°C</span>' : null)
              // 常驻：显存容量
              let vramTxt: string | null = null
              if (g.memory_total != null) {
                const gb = g.memory_total / 1024
                vramTxt = (gb >= 1 ? gb.toFixed(gb % 1 ? 1 : 0) + ' GB' : g.memory_total + ' MB') + (g.mem_type ? ' ' + g.mem_type : '')
              } else if (g.vram) {
                vramTxt = g.vram
              }
              rows += gpuRow('显存容量', vramTxt, isIgpu ? '核显与 CPU 共用内存，无独立显存' : null)
              // 折叠「更多参数」
              let det = ''
              det += gpuRow('核心频率', g.core_clock != null ? g.core_clock + ' MHz' : null, isIgpu && g.core_clock == null ? '核显频率信息暂不可用' : null)
              det += gpuRow('显存频率', g.mem_clock != null ? g.mem_clock + ' MHz' : null, isIgpu ? '核显与 CPU 共用内存，无独立显存频率' : null)
              det += gpuRow(
                '显存位宽',
                g.bus_width != null ? g.bus_width + ' 位' : null,
                isIgpu ? '核显与 CPU 共用内存总线，无独立显存位宽' : g.vendor === '1002' && g.bus_width == null ? '该 AMD 显卡型号暂未在识别表中，无法读取位宽' : null,
              )
              if (g.pci) {
                const pcieLines = [`<span>${esc(g.pci)}</span>`, `<span style="color:var(--muted);font-size:12px">${esc(pcieLocText(g.pci))}</span>`]
                if (g.pcie && g.pcie.gen_sta) {
                  let ch = `PCIe ${g.pcie.gen_sta} ${g.pcie.width_sta}`
                  if (g.pcie.width_cap && g.pcie.width_cap !== g.pcie.width_sta) {
                    ch += ` <span style="color:var(--orange)">（协商 ${g.pcie.width_sta}，上限 ${g.pcie.width_cap} · 未跑满）</span>`
                  } else if (g.pcie.width_cap) {
                    ch += `（协商 ${g.pcie.width_sta}，上限 ${g.pcie.width_cap}）`
                  }
                  pcieLines.push(`<span>${ch}</span>`)
                } else if (isIgpu) {
                  pcieLines.push(`<span style="color:var(--muted);font-size:12px">集成于 CPU，不占用独立 PCIe 插槽</span>`)
                }
                det += `<div class="kv" style="align-items:flex-start"><span class="k" style="padding-top:2px">PCIe</span><span class="v" style="text-align:right;display:flex;flex-direction:column;gap:3px">${pcieLines.join('')}</span></div>`
              }
              if (g.driver) {
                const dv = g.driver_ver ? ` <span style="color:var(--muted);font-size:12px">${esc(g.driver_ver)}</span>` : ''
                det += `<div class="kv"><span class="k">驱动</span><span class="v">${esc(g.driver)}${dv}</span></div>`
              }
              if (g.power_draw != null || g.power_cap != null) {
                let ptext = g.power_draw != null ? g.power_draw + ' W' : ''
                if (g.power_cap != null) ptext += (ptext ? ' / ' : '') + '上限 ' + g.power_cap + ' W'
                let pcol = 'var(--muted)'
                if (g.power_draw != null && g.power_cap != null && g.power_cap > 0) {
                  const rr = g.power_draw / g.power_cap
                  if (rr >= 0.95) pcol = 'var(--red)'
                  else if (rr >= 0.85) pcol = 'var(--orange)'
                }
                det += `<div class="kv"><span class="k">功耗</span><span class="v" style="color:${pcol}">${esc(ptext)}</span></div>`
              } else if (isIgpu) {
                det += gpuRow('功耗', null, '核显与 CPU 共用电源，未单独统计')
              } else {
                det += gpuRow('功耗', null, null)
              }
              rows += `<details class="gpu-detail"><summary>更多参数</summary><div class="detail-body">${det}</div></details>`
              return rows
            })
            .join('<div class="gpu-sep"></div>')
        : '<div class="loading">未检测到核显 / 独显，或 BIOS 已禁用</div>'
    }
  </div>`
  const sysInfoBody = `
  <div class="detect-cards">
    ${sysInfoCard}
    ${boardCard}
    <div class="card">
      <h3>处理器</h3>
      <div class="kv"><span class="k">型号</span><span class="v">${s.cpu_model || '-'}</span></div>
      <div class="kv"><span class="k">核心/线程</span><span class="v">${s.cpu_cores || 0}核 / ${s.cpu_threads || 0}线程</span></div>
      ${(() => {
        const min = ci.min_freq_mhz != null ? Math.round(ci.min_freq_mhz) + ' MHz' : null
        const max = ci.max_freq_mhz != null ? Math.round(ci.max_freq_mhz) + ' MHz' : null
        const range = min && max ? '范围 ' + min + ' - ' + max : max ? max : s.cpu_freq != null ? '最大 ' + Math.round(parseFloat(s.cpu_freq)) + ' MHz' : ''
        const live =
          ci.current_freq_mhz != null
            ? `<span class="js-cpu-freq-live" data-pref="当前" style="color:var(--blue)" title="每秒刷新：全部核心瞬时频率的平均值（各核负载不同会各自变频）">当前 ${Math.round(ci.current_freq_mhz)} MHz</span>`
            : ''
        const txt = live + (live && range ? ' · ' : '') + range
        return `<div class="kv"><span class="k" title="当前频率＝全部核心瞬时频率的平均值（各核负载不同会各自变频），蓝色数字每秒刷新；范围为最小~最大频率">频率</span><span class="v">${txt || 'N/A'}</span></div>`
      })()}
      <div class="kv"><span class="k">CPU 温度</span><span class="v" id="sys-cpu-temp-kv" style="color:${tempColor(s.cpu_temp, 100)}">${s.cpu_temp != null ? s.cpu_temp + '°C' : 'N/A'}</span></div>
      <div class="kv"><span class="k">CPU 使用率</span><span class="v">${cpuPct}%</span></div>
      ${(() => {
        const pw = s.power
        if (!pw || !pw.ok) return ''
        let rows = `<div class="kv"><span class="k">实时功耗</span><span class="v" style="color:${pwColor(pw.total)}">${pw.total != null ? pw.total + ' W' : '—'}</span></div>`
        if (pw.core != null) rows += `<div class="kv"><span class="k">核心</span><span class="v">${pw.core} W</span></div>`
        if (pw.uncore != null) rows += `<div class="kv"><span class="k">非核心</span><span class="v">${pw.uncore} W</span></div>`
        if (pw.dram != null) rows += `<div class="kv"><span class="k">内存</span><span class="v">${pw.dram} W</span></div>`
        return rows
      })()}
      ${(() => {
        let det = ''
        det += gpuRow('架构', ci.arch)
        det += gpuRow('厂商', ci.vendor)
        det += gpuRow('插槽数', ci.sockets != null ? ci.sockets + ' 个' : null)
        det += gpuRow('每插槽核心', ci.cores_per_socket != null ? ci.cores_per_socket + ' 核' : null)
        det += gpuRow('每核心线程', ci.threads_per_core != null ? ci.threads_per_core + ' 线程' : null)
        let freqRange = ''
        if (ci.min_freq_mhz != null && ci.max_freq_mhz != null) freqRange = Math.round(ci.min_freq_mhz) + ' - ' + Math.round(ci.max_freq_mhz) + ' MHz'
        else if (ci.max_freq_mhz != null) freqRange = Math.round(ci.max_freq_mhz) + ' MHz'
        det += gpuRow('频率范围', freqRange || null)
        det += gpuRow('BogoMIPS', ci.bogomips != null ? ci.bogomips.toFixed(2) : null)
        if (ci.family != null || ci.model_id != null || ci.stepping != null) {
          const spec = [ci.family != null ? 'Family ' + ci.family : '', ci.model_id != null ? 'Model ' + ci.model_id : '', ci.stepping != null ? 'Stepping ' + ci.stepping : ''].filter(Boolean).join(' / ')
          det += gpuRow('型号规格', spec)
        }
        det += gpuRow('虚拟化', ci.virtualization, ci.virtualization == null ? '未检测到硬件虚拟化支持' : null)
        if (ci.l1d || ci.l1i || ci.l2 || ci.l3) {
          det += gpuRow('L1 数据缓存', ci.l1d || null)
          det += gpuRow('L1 指令缓存', ci.l1i || null)
          det += gpuRow('L2 缓存', ci.l2 || null)
          det += gpuRow('L3 缓存', ci.l3 || null)
        }
        det += gpuRow('地址宽度', ci.address_sizes || null)
        det += gpuRow('字节序', ci.byte_order || null)
        det += gpuRow('NUMA 节点', ci.numa_nodes != null ? ci.numa_nodes + ' 个' : null)
        if (ci.features) {
          const feats: string[] = []
          if (ci.features.lm) feats.push('64-bit')
          if (ci.features.ht) feats.push('超线程')
          if (ci.features.aes) feats.push('AES-NI')
          if (ci.features.avx512) feats.push('AVX-512')
          else if (ci.features.avx2) feats.push('AVX2')
          else if (ci.features.avx) feats.push('AVX')
          det += gpuRow('指令集特性', feats.length ? feats.join(' · ') : '—')
        }
        return `<details class="gpu-detail"><summary>更多参数</summary><div class="detail-body">${det}</div></details>`
      })()}
    </div>
    ${gpuCard}
    <div class="card" style="grid-column:1/-1" id="sys-net-card">${renderSysNetCard(s.nics)}</div>
    ${memCard}
  </div>`
  // 阵列卡 & 磁盘
  let vdRows = ''
  if (r.mode === 'mega' && r.virtual_drives && r.virtual_drives.length) {
    vdRows = r.virtual_drives
      .map((v: any) => {
        const wp = v.write_policy || ''
        const wpBadge =
          wp === 'WriteBack'
            ? '<span class="badge b-ok" title="写回：先写阵列卡缓存再刷盘，性能更好（依赖 CacheVault/电池保护）">写回 (WB)</span>'
            : wp === 'WriteThrough'
              ? '<span class="badge b-info" title="直写：直接写盘，不经由阵列卡缓存，最稳">直写 (WT)</span>'
              : esc(v.cache_raw || '-')
        return `<tr><td>${esc(v.name || '')}${v.dgvd ? ' (' + esc(v.dgvd) + ')' : ''}</td><td>${esc(v.type || '-')}</td><td>${esc(v.size || '-')}</td><td>${esc(v.state || '-')}</td><td>${esc(v.access || '-')}</td><td>${wpBadge}</td></tr>`
      })
      .join('')
  }
  const ccSched = r.cc_schedule || {}
  const cb = (r.auto_copyback || '').toLowerCase()
  const cbActiveList = r.drives && r.drives.length ? r.drives.filter((d: any) => d.copyback_active).map((d: any) => d.slot) : []
  const cbNeeded = r.copyback_needed || []
  const cbFailedDrives = r.drives && r.drives.length ? r.drives.filter((d: any) => d.failed && !d.copyback_active) : []
  let raidCardsHtml: string
  if (r.mode === 'mega') {
    raidCardsHtml = `
    <div class="card">
      <h3 title="管理多块硬盘组成阵列的硬件卡">RAID 控制器</h3>
      <div class="kv"><span class="k">型号</span><span class="v">${r.model}</span></div>
      <div class="kv"><span class="k">模式</span><span class="v"><span class="badge b-info" title="阵列模式：由这张卡把多块硬盘统一管理成 RAID">MegaRAID (IR)</span></span></div>
      <div class="kv"><span class="k">状态</span><span class="v" style="color:var(--green)">${CHECK_SVG} 正常</span></div>
      <div class="kv"><span class="k">序列号</span><span class="v">${r.serial || '-'}</span></div>
      <div class="kv"><span class="k" title="这块阵列卡在硬盘总线上的唯一编号">SAS 地址</span><span class="v">${r.sas_address || '-'}</span></div>
      <div class="kv"><span class="k" title="阵列卡插在主板上哪个插槽的物理位置">PCI 地址</span><span class="v">${r.pci || '-'}</span></div>
      <div class="kv"><span class="k" title="阵列卡主控芯片温度（ROC），更贴近过热风险点，风扇控温以它为准">芯片温度 (ROC)</span><span class="v" style="color:${tempColor(r.roc_temp, 85)}">${r.roc_temp != null ? r.roc_temp + '°C' : 'N/A'}</span></div>
      ${r.controller_temp != null ? `<div class="kv"><span class="k" title="控制器/板载环境温度，与飞牛界面、storcli 摘要、多数对照工具默认读到的温度一致">控制器温度</span><span class="v" style="color:${tempColor(r.controller_temp, 85)}">${r.controller_temp}°C</span></div>` : ''}
      ${r.roc_temp != null && r.controller_temp != null ? `<div class="kv"><span class="k">两者差异</span><span class="v" style="color:var(--muted);font-size:12px">芯片比控制器高 ${r.roc_temp - r.controller_temp}°C（两个不同传感器，非故障）</span></div>` : ''}
    </div>
    <div class="card">
      <h3>固件版本</h3>
      <div class="kv"><span class="k" title="固件完整包版本（控制器的底层软件）">FW Package</span><span class="v">${r.fw_package || '-'}</span></div>
      <div class="kv"><span class="k" title="固件的版本号">FW Version</span><span class="v">${r.fw_version || '-'}</span></div>
      <div class="kv"><span class="k" title="主板最底层的启动程序版本">BIOS</span><span class="v">${r.bios_version || '-'}</span></div>
      <div class="kv"><span class="k">驱动</span><span class="v">${r.driver || '-'}</span></div>
    </div>
    <div class="card">
      <h3>缓存 / 电池</h3>
      <div class="kv"><span class="k" title="阵列卡的掉电保护缓存，断电也能保住数据">CacheVault</span><span class="v">${r.cachevault || '-'}</span></div>
      <div class="kv"><span class="k" title="直接透传给系统、不走阵列的硬盘数量">JBOD 盘数</span><span class="v">${r.jbod_count || 0}</span></div>
      <div class="kv"><span class="k">物理盘</span><span class="v">${r.drives ? r.drives.length : 0} 块</span></div>
    </div>
    ${
      r.mode === 'mega' && r.virtual_drives && r.virtual_drives.length
        ? `<div class="card">
      <h3>逻辑盘（Virtual Drive）· 缓存策略</h3>
      <div style="overflow-x:auto"><table class="table"><thead><tr><th>名称/ID</th><th>级别</th><th>容量</th><th>状态</th><th>访问</th><th>缓存策略（写回/直写）</th></tr></thead><tbody>${vdRows}</tbody></table></div>
      <div style="margin-top:8px;font-size:12px;color:var(--muted)">写回(WB)：数据先写入阵列卡缓存再异步刷盘，速度更快，但依赖 CacheVault/电池在断电时保护缓存；直写(WT)：直接写入硬盘，最稳妥但稍慢。</div>
      ${
        r.virtual_drives && r.virtual_drives.length
          ? `
      <div class="section-title" style="margin-top:14px">一致性检查（Consistency Check）</div>
      <div class="cc-list">
        ${r.virtual_drives
          .map((v: any) => {
            const vid = v.dgvd || ''
            const id = vid.replace('/', '_')
            return `<div class="cc-row" data-vd="${esc(vid)}">
            <span class="cc-name">${esc(v.name || '')} (${esc(vid)})</span>
            <span class="cc-state" id="cc-state-${id}">空闲</span>
            <span class="cc-bar"><span class="cc-bar-fill" id="cc-bar-${id}"></span></span>
            <span class="cc-pct" id="cc-pct-${id}">—</span>
            <button class="btn-mini" onclick="raidCC('${esc(vid)}','start')">开始</button>
            <button class="btn-mini" onclick="raidCC('${esc(vid)}','pause')">暂停</button>
            <button class="btn-mini" onclick="raidCC('${esc(vid)}','resume')">恢复</button>
            <button class="btn-mini" onclick="raidCC('${esc(vid)}','stop')">停止</button>
          </div>`
          })
          .join('')}
      </div>
      <div style="margin-top:6px;font-size:12px;color:var(--muted)">一致性检查会扫描逻辑盘校验数据一致性，期间磁盘负载升高；可随时暂停/恢复/停止。仅当存在硬件 RAID 逻辑盘时可用。</div>
      <div class="section-title" style="margin-top:14px">定时巡检调度</div>
      <div class="cc-schedule">
        <label class="switch-row"><input type="checkbox" id="ccsched-enable" ${ccSched.enabled ? 'checked' : ''}/> 启用自动一致性检查</label>
        <div class="cc-sched-row">
          <select id="ccsched-period" onchange="document.getElementById('ccsched-weekday-wrap').style.display = this.value==='weekly'?'inline':'none'">
            <option value="daily" ${ccSched.period === 'daily' ? 'selected' : ''}>每天</option>
            <option value="weekly" ${ccSched.period === 'weekly' ? 'selected' : ''}>每周</option>
          </select>
          <span id="ccsched-weekday-wrap" style="display:${ccSched.period === 'weekly' ? 'inline' : 'none'}">
            <select id="ccsched-weekday">
              ${['周一', '周二', '周三', '周四', '周五', '周六', '周日'].map((w, i) => `<option value="${i}" ${ccSched.weekday === i ? 'selected' : ''}>${w}</option>`).join('')}
            </select>
          </span>
          <input type="time" id="ccsched-time" value="${String(ccSched.hour).padStart(2, '0')}:${String(ccSched.minute).padStart(2, '0')}"/>
          <button class="btn-mini" onclick="saveCCSchedule()">保存</button>
        </div>
        <div style="margin-top:4px;font-size:12px;color:var(--muted)">到点后由后台对所有硬件逻辑盘自动执行一次一致性检查；仅当存在硬件 RAID 逻辑盘时生效。</div>
      </div>
      <div class="section-title" style="margin-top:14px">热备盘（Hot Spare）</div>
      <div class="hs-list">
        ${
          r.hotspares && r.hotspares.length
            ? r.hotspares
                .map((h: any) => {
                  return `<div class="hs-row"><span>${esc(h.slot)} · ${esc(h.model || '-')} · ${h.hs_type === 'global' ? '全局' : '专用'}</span><button class="btn-mini" onclick="raidHotspare('${esc(h.slot)}','remove')">移除</button></div>`
                })
                .join('')
            : '<div style="font-size:12px;color:var(--muted)">当前无热备盘。可在下方把某块盘设为热备（设为热备会清空该盘数据）。</div>'
        }
      </div>
      <div class="hs-add">
        <span>将盘设为热备：</span>
        <select id="hs-slot"><option value="">选择盘</option>${
          r.drives && r.drives.length
            ? r.drives
                .filter((d: any) => !/(GHS|DHS)/i.test(d.state || ''))
                .map((d: any) => `<option value="${esc(d.slot)}">${esc(d.slot)} · ${esc(d.model || '-')}</option>`)
                .join('')
            : ''
        }</select>
        <button class="btn-mini" onclick="raidHotspare(null,'add')">全局热备</button>
        <button class="btn-mini" onclick="raidHotspare(null,'add_dedicated')">专用热备</button>
      </div>
      <div class="section-title" style="margin-top:14px">CopyBack 自动换盘</div>
      <div class="cb-block">
        <div class="cb-row">
          <span>控制器自动 CopyBack（某盘故障且有热备盘时，自动把数据复制到热备盘完成换盘）</span>
          <label class="switch-row"><input type="checkbox" id="cb-enable" ${cb === 'enabled' ? 'checked' : ''} onchange="saveCopyBack()"/> ${cb === 'enabled' ? '已开启' : '已关闭'}</label>
        </div>
        <div class="cb-status">
          ${cbActiveList.length ? cbActiveList.map((sl: any) => `<div class="cb-active">盘 ${esc(sl)} 正在执行 CopyBack 自动换盘…</div>`).join('') : cbNeeded.length ? cbNeeded.map((d: any) => `<div class="cb-needed">盘 ${esc(d.slot)} 已故障，待自动 CopyBack 换盘到热备盘</div>`).join('') : '<div style="font-size:12px;color:var(--muted)">当前无故障盘需要换盘。</div>'}
        </div>
        <div class="cb-add">
          <span>手动对故障盘触发 CopyBack：</span>
          <select id="cb-slot"><option value="">选择故障盘</option>${cbFailedDrives.map((d: any) => `<option value="${esc(d.slot)}">${esc(d.slot)} · ${esc(d.model || '-')}</option>`).join('')}</select>
          <button class="btn-mini" onclick="raidCopyBack(null,'start')">手动触发</button>
        </div>
        <div style="margin-top:4px;font-size:12px;color:var(--muted)">CopyBack 须已配置热备盘。建议保持「自动 CopyBack」开启：某盘故障时阵列卡会自动换盘，无需人工干预；也可在故障发生时点「手动触发」。</div>
      </div>
      `
          : ''
      }
    </div>`
        : ''
    }
  `
  } else if (r.mode === 'hba') {
    raidCardsHtml = `
    <div class="card">
      <h3>控制器 (HBA 直通)</h3>
      <div class="kv"><span class="k">型号</span><span class="v">${r.model}</span></div>
      <div class="kv"><span class="k">模式</span><span class="v"><span class="badge b-info" title="直通模式：硬盘直接交给系统管理，不做任何阵列">IT 直通</span></span></div>
      <div class="kv"><span class="k">状态</span><span class="v" style="color:var(--green)">${CHECK_SVG} 正常工作</span></div>
      ${r.controller_temp != null ? `<div class="kv"><span class="k">芯片温度</span><span class="v" style="color:${tempColor(r.controller_temp, 85)};font-weight:600">${r.controller_temp}°C</span></div>` : ''}
      <div class="kv"><span class="k">说明</span><span class="v" style="font-size:12px">${r.note || ''}</span></div>
    </div>`
  } else {
    raidCardsHtml = `<div class="card"><h3>阵列卡</h3><div class="loading">${r.note || '未检测到阵列卡'}</div></div>`
  }
  function chTip(ch: string): string {
    if (!ch) return ''
    if (ch.indexOf('阵列卡') >= 0) return '这块硬盘由阵列卡统一管理，不是直接插在主板上'
    if (ch.indexOf('M.2') >= 0) return '直接插在主板的 M.2 插槽（系统盘常见）'
    if (ch.indexOf('SATA') >= 0) return '直接插在主板的 SATA 接口'
    if (ch.indexOf('SAS') >= 0) return '直接插在主板的 SAS 接口'
    return '硬盘的连接通道'
  }
  const diskItem = (d: any) => {
    const t = (d.type || '').toUpperCase()
    const isSas = t === 'SAS'
    const isNvme = t === 'NVME'
    const typeBadge = `<span class="badge ${isSas ? 'b-sas' : isNvme ? 'b-nvme' : 'b-sata'}">${isSas ? 'SAS' : isNvme ? 'NVMe' : 'SATA'}</span>`
    const daBadge = d.dual_actuator ? ` <span class="badge b-info" title="同一块物理硬盘有 ${d.arms ? d.arms.length : 2} 个独立磁臂，被系统识别成多个设备，实际是一块盘">双磁臂</span>` : ''
    const feat = diskFeatureClean(d.feature, d.dual_actuator)
    const featBadge = feat ? ` <span class="badge b-info">${esc(feat)}</span>` : ''
    const ch = d.channel || ''
    const chBadge = ch ? ` <span class="ch-tag" title="${esc(chTip(ch))}">${esc(ch)}</span>` : ''
    const label = d.dev || (d.raid_only ? (d.channel || ('阵列卡 ' + (d.slot || ''))) : '') || '—'
    const locateBtn = d.locate_supported && d.slot ? ` <button class="btn-mini" data-locate-slot="${esc(d.slot)}" onclick="raidLocate('${esc(d.slot)}')" title="让对应硬盘指示灯闪烁，便于在机箱里找到它">定位闪灯</button>` : ''
    return `<div class="disk-mini">
      <span>${typeBadge}${daBadge} ${esc(label)} · ${d.brand ? d.brand + ' ' : ''}${d.vendor ? d.vendor + ' ' : ''}${d.model}${chBadge}${featBadge}</span>
      <span><span style="color:${tempColor(d.temp, d.temp_trip || 60)};font-weight:600">${d.temp != null ? d.temp + '°C' : 'N/A'}</span> · <span class="badge ${d.asleep ? 'b-muted' : d.health_ok ? 'b-ok' : 'b-bad'}">${d.asleep ? '休眠' : esc(d.health)}</span>${locateBtn}</span>
    </div>`
  }
  const merged = mergeDualActuator(disks)
  const sasDisks = merged.filter((d: any) => (d.type || '').toUpperCase() === 'SAS')
  const sataDisks = merged.filter((d: any) => {
    const t = (d.type || '').toUpperCase()
    return t === 'SATA' || t === 'ATA'
  })
  const nvmeDisks = merged.filter((d: any) => (d.type || '').toUpperCase() === 'NVME')
  const otherDisks = merged.filter((d: any) => {
    const t = (d.type || '').toUpperCase()
    return t !== 'SAS' && t !== 'SATA' && t !== 'ATA' && t !== 'NVME'
  })
  let diskMini = ''
  if (nvmeDisks.length) diskMini += `<div class="disk-group-title">NVMe 硬盘 (${nvmeDisks.length})</div>` + nvmeDisks.map(diskItem).join('')
  if (sasDisks.length) diskMini += `<div class="disk-group-title">SAS 硬盘 (${sasDisks.length})</div>` + sasDisks.map(diskItem).join('')
  if (sataDisks.length) diskMini += `<div class="disk-group-title">SATA 硬盘 (${sataDisks.length})</div>` + sataDisks.map(diskItem).join('')
  if (otherDisks.length) diskMini += `<div class="disk-group-title">其他硬盘 (${otherDisks.length})</div>` + otherDisks.map(diskItem).join('')
  if (!disks.length) diskMini = '<div class="loading">未检测到硬盘</div>'
  const raidDisk = `
  <div class="grid-2 raid-disk-split">
    <div class="split-col">${raidCardsHtml}</div>
    <div class="split-col disk-scroll">
      <div class="card">
        <h3>物理硬盘信息</h3>
        ${diskMini}
      </div>
    </div>
  </div>`
  return `
  <div class="detect-title" style="margin-bottom:10px">系统配置</div>
  ${sysInfoBody}
  <div class="detect-title" style="margin:20px 0 10px">阵列卡 &amp; 磁盘</div>
  ${raidDisk}
  `
}

// ============ 组件状态 ============
const data = ref<any>(null)
const err = ref('')
const busy = ref(false)
const lastUpdate = ref('')
const bodyEl = ref<HTMLElement | null>(null)
// 实时值（不进入 bodyHtml，故不触发重渲；只经 applyLive 就地改字）
const freqMhz = ref<number | null>(null)
const cpuTempVal = ref<number | null>(null)

let metricTimer = 0
let tempsTimer = 0
let metricBusy = false
let tempsBusy = false
let _diskRO: ResizeObserver | null = null

function syncDiskScroll(): void {
  const root = bodyEl.value
  if (!root) return
  const split = root.querySelector('.raid-disk-split') as HTMLElement | null
  if (!split) return
  const cols = split.querySelectorAll(':scope > .split-col')
  if (cols.length < 2) return
  const left = cols[0] as HTMLElement
  const right = split.querySelector(':scope > .split-col.disk-scroll') as HTMLElement | null
  if (!right) return
  const single = getComputedStyle(split).gridTemplateColumns.split(' ').length === 1
  if (_diskRO) {
    _diskRO.disconnect()
    _diskRO = null
  }
  if (single) {
    right.style.maxHeight = ''
    return
  }
  const apply = () => {
    right.style.maxHeight = left.offsetHeight + 'px'
  }
  apply()
  _diskRO = new ResizeObserver(apply)
  _diskRO.observe(left)
}

function applyLive(): void {
  const root = bodyEl.value
  if (!root) return
  if (freqMhz.value != null) {
    const f = Math.round(freqMhz.value)
    root.querySelectorAll('.js-cpu-freq-live').forEach(el => {
      const pref = el.getAttribute('data-pref')
      el.textContent = (pref ? pref + ' ' : '') + f + ' MHz'
    })
  }
  const e1 = root.querySelector('#sys-cpu-temp-kv') as HTMLElement | null
  if (e1) {
    const t = cpuTempVal.value
    e1.textContent = t != null ? t + '°C' : 'N/A'
    e1.style.color = tempColor(t, 100)
  }
}

const bodyHtml = computed(() => (data.value ? buildBody(data.value) : ''))
watch(bodyHtml, () => {
  void nextTick(() => {
    syncDiskScroll()
    applyLive()
    _memMods = (data.value && data.value.system && data.value.system.memory_modules && data.value.system.memory_modules.modules) || []
    _memBoard = (data.value && data.value.system && data.value.system.memory_modules) || {}
  })
})

// ============ hero 派生（与老页 renderDetect 状态栏同口径）============
const mergedDisks = computed(() => mergeDualActuator((data.value && data.value.disks) || []))
const fans = computed(() => {
  const s = data.value && data.value.system
  return (((s && s.sensors && s.sensors.fans) || []) as any[]).filter(f => !f.hidden && f.has_tach)
})
const fanOk = computed(() => fans.value.length > 0 && fans.value.every(f => !(f.stopped || f.rpm < 1)))
const raidMode = computed(() => (data.value && data.value.raid && data.value.raid.mode) || 'none')

const heroStats = computed<HeroStat[]>(() => {
  const f = fans.value
  const fanStr = f.length === 0 ? 'N/A' : fanOk.value ? '正常' : '异常'
  const rm = raidMode.value
  const raidStr = rm === 'mega' || rm === 'hba' ? '正常' : rm === 'mega_error' ? '读取失败' : '未配置'
  const sas = mergedDisks.value.filter(d => (d.type || '').toUpperCase() === 'SAS')
  const sata = mergedDisks.value.filter(d => {
    const t = (d.type || '').toUpperCase()
    return t === 'SATA' || t === 'ATA'
  })
  const sasHealthy = sas.filter(d => d.health_ok).length
  const sataHealthy = sata.filter(d => d.health_ok).length
  const out: HeroStat[] = [
    { v: fanStr, k: '散热风扇', cls: f.length === 0 ? '' : fanOk.value ? '' : 'bad' },
    { v: raidStr, k: '阵列卡', cls: rm === 'mega' || rm === 'hba' ? '' : 'warn' },
  ]
  if (sas.length) out.push({ v: String(sasHealthy), unit: '/' + sas.length, k: 'SAS 硬盘健康', cls: sasHealthy === sas.length ? '' : 'warn' })
  if (sata.length) out.push({ v: String(sataHealthy), unit: '/' + sata.length, k: 'SATA 硬盘健康', cls: sataHealthy === sata.length ? '' : 'warn' })
  return out
})

const badge = computed(() => {
  const rm = raidMode.value
  const raidOk = rm === 'mega' || rm === 'hba'
  const sas = mergedDisks.value.filter(d => (d.type || '').toUpperCase() === 'SAS')
  const sata = mergedDisks.value.filter(d => {
    const t = (d.type || '').toUpperCase()
    return t === 'SATA' || t === 'ATA'
  })
  const allDiskOk = sas.filter(d => d.health_ok).length === sas.length && sata.filter(d => d.health_ok).length === sata.length
  const sysOk = (fans.value.length === 0 || fanOk.value) && raidOk && allDiskOk
  return { ok: sysOk, text: sysOk ? '系统运行正常' : '存在需关注项' }
})

const sub = computed(() => {
  const t = data.value && data.value.time
  return `实时监测设备健康状态与硬件详情 · 最近检测 ${t || '—'}`
})

// ============ 拉取 ============
async function fetchAll(force = false): Promise<void> {
  if (busy.value && !force) return
  // 切页签回来先上缓存秒开，再拉最新数据替换
  if (!data.value) {
    const cached = pageCacheGet<any>('detect')
    if (cached) data.value = cached
  }
  // 有内容（缓存或已有数据）就不转圈：后台静默拉最新
  if (!data.value) busy.value = true
  try {
    const r = await apiFetch('/api/all' + (force ? '?force=1&_=' : '?_=') + Date.now(), 60000)
    if (!r.ok) return
    const j = await r.json()
    if (j && j.error) {
      err.value = '加载失败：' + j.error
      return
    }
    err.value = ''
    data.value = j
    pageCacheSet('detect', j)
    if (j.system && j.system.cpu_temp != null && cpuTempVal.value == null) {
      cpuTempVal.value = j.system.cpu_temp
    }
    lastUpdate.value = '更新于 ' + (j.time || '') + (j.elapsed != null ? ` (${j.elapsed}s)` : '')
  } catch {
    /* 网络抖动忽略 */
  } finally {
    busy.value = false
  }
}

async function fetchMetrics(): Promise<void> {
  if (metricBusy) return
  metricBusy = true
  try {
    const r = await apiFetch('/api/metrics', 30000)
    if (!r.ok) return
    const d = await r.json()
    if (d && d.cpu_freq_mhz != null) {
      const f = parseFloat(String(d.cpu_freq_mhz))
      if (!Number.isNaN(f)) freqMhz.value = f
    }
    applyLive()
  } catch {
    /* 忽略 */
  } finally {
    metricBusy = false
  }
}

async function fetchTemps(): Promise<void> {
  if (tempsBusy) return
  tempsBusy = true
  try {
    const r = await apiFetch('/api/fan/temps', 30000)
    if (!r.ok) return
    const j = await r.json()
    if (j && j.cpu_temp != null) cpuTempVal.value = j.cpu_temp
    applyLive()
  } catch {
    /* 忽略 */
  } finally {
    tempsBusy = false
  }
}

async function refreshAll(): Promise<void> {
  await fetchAll(true)
}

onMounted(() => {
  // v-html 内 inline onclick 依赖这些全局函数名（与老页同名）
  ;(window as any).switchSysNic = switchSysNic
  ;(window as any).raidLocate = raidLocate
  ;(window as any).raidCC = raidCC
  ;(window as any).raidHotspare = raidHotspare
  ;(window as any).raidCopyBack = raidCopyBack
  ;(window as any).saveCCSchedule = saveCCSchedule
  ;(window as any).saveCopyBack = saveCopyBack
  ;(window as any).toggleWol = toggleWol
  ;(window as any).showMemDetail = showMemDetail
  ;(window as any).closeMemDetail = closeMemDetail

  void fetchAll(true)
  void fetchTemps()
  void fetchMetrics()
  metricTimer = window.setInterval(() => void fetchMetrics(), 1000)
  tempsTimer = window.setInterval(() => void fetchTemps(), 5000)
})

onUnmounted(() => {
  if (metricTimer) window.clearInterval(metricTimer)
  if (tempsTimer) window.clearInterval(tempsTimer)
  Object.keys(_ccTimers).forEach(k => {
    if (_ccTimers[k]) clearInterval(_ccTimers[k])
  })
  if (_diskRO) {
    _diskRO.disconnect()
    _diskRO = null
  }
  ;['switchSysNic', 'raidLocate', 'raidCC', 'raidHotspare', 'raidCopyBack', 'saveCCSchedule', 'saveCopyBack', 'toggleWol', 'showMemDetail', 'closeMemDetail'].forEach(k => {
    try {
      delete (window as any)[k]
    } catch {
      ;(window as any)[k] = undefined
    }
  })
})
</script>

<template>
  <div>
    <PanelHero
      icon="detect"
      title="硬件配置检测"
      :sub="sub"
      :badge="badge"
      :stats="heroStats"
      :last-update="lastUpdate"
      :busy="busy"
      @refresh="refreshAll"
    />
    <div v-if="err" class="loading" style="color: var(--red)">{{ err }}</div>
    <div v-show="!err" ref="bodyEl" v-html="bodyHtml" />
    <div class="modal-overlay" id="memDetailModal" onclick="if(event.target===this)closeMemDetail()">
      <div class="modal-box mem-modal">
        <div class="modal-title" id="memDetailTitle">内存详情</div>
        <div class="modal-detail" id="memDetailBody"></div>
        <div class="modal-actions"><button class="btn" onclick="closeMemDetail()">关闭</button></div>
      </div>
    </div>
  </div>
</template>
