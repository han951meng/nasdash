/** 数值格式化 —— 与 templates/index.html 的 fmtSpeed / fmtLoad / tempColor 保持同源口径 */

/** 字节速率：B/s / KB/s / MB/s（与老页面 fmtSpeed 完全一致） */
export function fmtSpeed(bps: number | null | undefined): string {
  if (bps == null || Number.isNaN(bps)) return '-'
  if (bps < 1024) return bps.toFixed(0) + ' B/s'
  if (bps < 1048576) return (bps / 1024).toFixed(1) + ' KB/s'
  return (bps / 1048576).toFixed(2) + ' MB/s'
}

/** 负载：两位小数 */
export function fmtLoad(v: unknown): string {
  const n = parseFloat(String(v))
  return Number.isNaN(n) ? String(v) : n.toFixed(2)
}

/** 温度语义色：绿正常 / 橙偏高 / 红危险（阈值对齐老页面） */
export function tempColor(t: number | null | undefined, trip?: number | null): string {
  if (t == null) return 'var(--muted)'
  const pct = trip ? t / trip : t / 60
  if (pct > 0.9) return 'var(--red)'
  if (pct > 0.75) return 'var(--orange)'
  return 'var(--green)'
}

/** 使用率语义色（CPU / 内存 / 卷） */
export function usageColor(pct: number, hi = 85, mid = 70): string {
  if (pct > hi) return 'var(--red)'
  if (pct > mid) return 'var(--orange)'
  return 'var(--green)'
}

/** 数字取整并夹到 [0,100]，非数字返回 null */
export function clampPct(v: unknown): number | null {
  const n = parseFloat(String(v))
  if (Number.isNaN(n)) return null
  return Math.min(100, Math.max(0, n))
}
