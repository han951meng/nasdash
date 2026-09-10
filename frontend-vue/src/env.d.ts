/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}

// 彩色插画图标以 base64 data URL 内联（`?inline` 强制内联，避免网关静态资源 302）
declare module '*.png?inline' {
  const src: string
  export default src
}
