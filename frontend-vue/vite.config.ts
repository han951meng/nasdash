import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { viteSingleFile } from 'vite-plugin-singlefile'

/**
 * 探针页构建配置（阶段 1a）
 *
 * 关键约束（来自《最终实施方案》）：
 * - 飞牛网关通过 /cgi/ThirdParty/com.dashboard.nasdash/index.cgi/ 反代到 Flask，
 *   静态资源若走 /assets/xxx.js 这样的根路径极易 404 或与其他应用打架。
 * - 因此用 vite-plugin-singlefile 把 JS/CSS 全部内联，产物就是一个独立 index.html，
 *   零路径风险、零 hash 问题。稳定后再考虑拆包。
 */
export default defineConfig({
  plugins: [vue(), viteSingleFile()],
  base: './',
  build: {
    // 直接产出到 Flask 的模板目录，构建完即就位
    outDir: '../templates/vue',
    // 只清空 templates/vue 自身，绝不动 templates/ 下其他内容
    emptyOutDir: true,
    cssCodeSplit: false,
    assetsInlineLimit: 100 * 1024 * 1024,
    // 飞牛 WebView / 旧内核浏览器兜底
    target: 'es2019',
    reportCompressedSize: false,
    rollupOptions: {
      output: {
        inlineDynamicImports: true
      }
    }
  },
  server: {
    port: 5273,
    // 本地开发时把 API 打到 158，便于快速迭代（阶段 1a 之后按需启用）
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:9800',
        changeOrigin: true
      }
    }
  }
})
