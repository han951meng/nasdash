<script setup lang="ts">
/**
 * 关于 nasdash（Vue 版，阶段 3 第 2 个迁移模块）
 *
 * 复刻 templates/index.html 的 renderAbout：
 *  - PanelHero 副标题显示当前版本（nasdash {current}）
 *  - 版本信息卡：fnOS 版本（/api/system 顶层 fnos_version）、运行内核（/api/system system.kernel）
 *  - 项目卡：项目主页 / 论坛地址外链 + 「检查新版本」按钮（拉 /api/version?force=1）
 *
 * 样式全部复用全局 panel.css 的 .detect-cards/.card/.kv/.btn-mini/.card h3（与旧页同源），
 * 本组件只补 .about-err/.about-update 两个局部状态条，不重定义布局类，避免 scoped 的 data-v 失配。
 */
import { onMounted, ref } from 'vue'
import PanelHero from '../components/PanelHero.vue'
import { apiFetch } from '../lib/api'
import { pageCacheGet, pageCacheSet } from '../lib/pageCache'

const appVersion = ref('')
const fnosVersion = ref('')
const kernel = ref('')
const error = ref('')
/** hero 右上角的刷新时间（之前没传，控件一直显示「加载中…」） */
const lastUpdate = ref('')

// 检查更新状态（本地内联提示，不复用旧页全局 #updateBanner）
const checking = ref(false)
const updateMsg = ref('')
const updateUrl = ref('')
const updateFound = ref(false)

function esc(s: string): string {
  return s.replace(/[&<>"]/g, c =>
    c === '&' ? '&amp;' : c === '<' ? '&lt;' : c === '>' ? '&gt;' : '&quot;',
  )
}

/** 版本号归一化：保证恰好一个 v 前缀（/api/version 的 current/latest 已带 v，避免拼出 vv2.1.0） */
function normV(x: string): string {
  if (!x) return x
  return x[0] === 'v' || x[0] === 'V' ? x : 'v' + x
}

async function loadInfo(): Promise<void> {
  error.value = ''
  // 切页签回来先上缓存秒开，再拉最新
  const cached = pageCacheGet<{ appVersion: string; fnosVersion: string; kernel: string }>('about')
  if (cached) {
    appVersion.value = cached.appVersion
    fnosVersion.value = cached.fnosVersion
    kernel.value = cached.kernel
  }
  try {
    // 当前安装版本（用于 hero 副标题）
    try {
      const vr = await apiFetch('/api/version', 15000)
      if (vr.ok) {
        const vj = (await vr.json()) as { current?: string }
        appVersion.value = vj.current || ''
      }
    } catch {
      /* 版本取不到不影响主体 */
    }

    // 版本信息卡：fnOS 版本（顶层）+ 运行内核（system.kernel）
    const sr = await apiFetch('/api/system', 30000)
    if (!sr.ok) throw new Error('HTTP ' + sr.status)
    const sj = (await sr.json()) as { fnos_version?: string; system?: { kernel?: string } }
    fnosVersion.value = sj.fnos_version || ''
    kernel.value = sj.system?.kernel || ''
    lastUpdate.value = '更新于 ' + new Date().toLocaleTimeString('zh-CN')
    pageCacheSet('about', { appVersion: appVersion.value, fnosVersion: fnosVersion.value, kernel: kernel.value })
  } catch (e) {
    error.value = '关于页加载失败：' + esc(String(e))
  }
}

async function checkUpdate(): Promise<void> {
  checking.value = true
  updateMsg.value = ''
  updateFound.value = false
  updateUrl.value = ''
  try {
    const r = await apiFetch('/api/version?force=1', 15000)
    if (!r.ok) throw new Error('HTTP ' + r.status)
    const d = (await r.json()) as {
      current?: string
      latest?: string
      update_available?: boolean
      url?: string
      error?: string
    }
    const cur = normV(d.current || appVersion.value)
    if (d.update_available && d.latest) {
      updateFound.value = true
      updateUrl.value = d.url || 'https://github.com/han951meng/nasdash/releases'
      updateMsg.value = '发现新版本 ' + normV(d.latest) + '（当前 ' + cur + '）'
    } else if (d.error) {
      updateMsg.value = '检查更新失败：' + d.error
    } else {
      updateMsg.value = '已是最新版本（' + cur + '）'
    }
  } catch (e) {
    updateMsg.value = '检查更新出错：' + esc(String(e))
  } finally {
    checking.value = false
  }
}

onMounted(() => {
  void loadInfo()
})
</script>

<template>
  <div>
    <PanelHero
      icon="about"
      title="关于 nasdash"
      :sub="appVersion ? 'nasdash ' + appVersion : 'nasdash'"
      :last-update="lastUpdate"
    />

    <div v-if="error" class="about-err">{{ error }}</div>

    <div class="detect-cards" v-show="!error">
      <div class="card">
        <h3>版本信息</h3>
        <div class="kv">
          <span class="k" title="飞牛系统版本，相当于手机底层的操作系统">fnOS 版本</span>
          <span class="v">{{ fnosVersion ? fnosVersion + ' (Debian 底层)' : '（切到「硬件配置检测」后自动显示）' }}</span>
        </div>
        <div class="kv">
          <span class="k" title="系统内核版本，最底层的那层程序">运行内核</span>
          <span class="v">{{ kernel || '—' }}</span>
        </div>
      </div>

      <div class="card">
        <h3>项目</h3>
        <div class="kv">
          <span class="k">项目主页</span>
          <span class="v"><a href="https://github.com/han951meng/nasdash" target="_blank" rel="noopener">github.com/han951meng/nasdash</a></span>
        </div>
        <div class="kv">
          <span class="k">论坛地址</span>
          <span class="v"><a href="https://club.fnnas.com/forum.php?mod=viewthread&tid=67060" target="_blank" rel="noopener">club.fnnas.com 讨论帖</a></span>
        </div>
        <div class="kv">
          <span class="k">检查更新</span>
          <span class="v"><button class="btn-mini" type="button" :disabled="checking" @click="checkUpdate">{{ checking ? '⏳' : '检查新版本' }}</button></span>
        </div>
      </div>
    </div>

    <div v-if="updateMsg" class="about-update" :class="{ found: updateFound }">
      <span>{{ updateMsg }}</span>
      <a v-if="updateFound" :href="updateUrl" target="_blank" rel="noopener">前往下载 ↗</a>
    </div>
  </div>
</template>

<style scoped>
.about-err {
  color: var(--red, #e5484d);
  padding: 12px 14px;
  font-size: 13px;
}
.about-update {
  margin-top: 14px;
  padding: 10px 14px;
  border-radius: var(--r-md, 10px);
  background: var(--card);
  border: 1px solid var(--border);
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.about-update.found {
  border-color: var(--green, #2da44e);
}
.about-update a {
  color: var(--link);
  text-decoration: none;
}
.about-update a:hover {
  text-decoration: underline;
}
</style>
