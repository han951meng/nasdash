/**
 * 接口基址与请求封装。
 *
 * 铁律（2026-09-10 真机实测）：飞牛网关把第三方应用挂在
 * `/cgi/ThirdParty/<pkg>/index.cgi/` 之下，裸 `/api/*` 只在该根目录下有效；
 * 子路径（`/vue/`、`/legacy/`）会被网关 404。
 * 统一从当前路径截出 `index.cgi` 前缀再拼，任意层级都通。
 */
export const API_BASE = (() => {
  const m = location.pathname.match(/^(.*\/index\.cgi)\/?/)
  return m ? m[1] : ''
})()

/** 带超时的 fetch：普通 30s、重接口可传更长，避免飞牛慢通道把请求挂死 */
export function apiFetch(path: string, ms = 30000): Promise<Response> {
  const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null
  const to = ctrl ? window.setTimeout(() => { try { ctrl.abort() } catch { /* 忽略 */ } }, ms) : 0
  const p = fetch(API_BASE + path, {
    credentials: 'include',
    cache: 'no-store',
    ...(ctrl ? { signal: ctrl.signal } : {}),
  })
  return to ? p.finally(() => window.clearTimeout(to)) : p
}

/** 旧版面板某个页签的「被嵌入」地址：新壳里用 iframe 内嵌尚未迁移的模块 */
export function legacyUrl(tab: string): string {
  return `${API_BASE}/legacy/?embed=1&tab=${encodeURIComponent(tab)}`
}
