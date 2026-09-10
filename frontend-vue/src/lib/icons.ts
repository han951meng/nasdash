/**
 * 图标单一数据源 —— 与 templates/index.html 同源：
 * - 彩色插画 PNG（ui/images/icon-*.png）用 `?inline` 内联成 base64 data URL，
 *   与老页面一致，避免飞牛网关对静态资源返回 302。
 * - 线性 SVG 走 24x24 viewBox 的 path 数据，由 <AppIcon> 渲染成 <svg>。
 */
import iconDetect from '../../../ui/images/icon-detect.png?inline'
import iconSystem from '../../../ui/images/icon-system.png?inline'
import iconHistory from '../../../ui/images/icon-history.png?inline'
import iconHdd from '../../../ui/images/icon-hdd.png?inline'
import iconStorage from '../../../ui/images/icon-storage.png?inline'
import iconFan from '../../../ui/images/icon-fan.png?inline'
import iconDocker from '../../../ui/images/icon-docker.png?inline'
import iconAutomation from '../../../ui/images/icon-automation.png?inline'
import iconManual from '../../../ui/images/icon-manual.png?inline'
import iconAbout from '../../../ui/images/icon-about.png?inline'
import iconRaid from '../../../ui/images/icon-raid.png?inline'

/** 彩色插画图标（PNG data URL） */
export const ICON_PNG: Record<string, string> = {
  detect: iconDetect,
  system: iconSystem,
  history: iconHistory,
  hdd: iconHdd,
  storage: iconStorage,
  fan: iconFan,
  docker: iconDocker,
  automation: iconAutomation,
  manual: iconManual,
  about: iconAbout,
  raid: iconRaid,
}

/** 线性 SVG 图标（24x24 路径，与老页面 ICONS 对象逐条同源） */
export const ICON_SVG: Record<string, string> = {
  thermo: '<path d="M14 4v10.54a4 4 0 1 1-4 0V4a2 2 0 0 1 4 0Z"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
  alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="M4.93 4.93l1.41 1.41"/><path d="M17.66 17.66l1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="M6.34 17.66l-1.41 1.41"/><path d="M19.07 4.93l-1.41 1.41"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  contrast: '<circle cx="12" cy="12" r="10"/><path d="M12 18a6 6 0 0 0 0-12v12Z"/>',
  menu: '<path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h16"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  check: '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
  pulse: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
}
