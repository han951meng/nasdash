<script setup lang="ts">
/**
 * 操作手册（Vue 版，阶段 3 第 1 个迁移模块）
 *
 * 复刻 templates/index.html 的 initManual：
 *  - 拉取 /manual?embed=1 片段（<style> + .man-body）就地渲染，手册样式不丢
 *  - 右侧可隐藏目录抽屉（滚动 1.6s 后自动收起）
 *  - 自动标红「本版本新增」段（版本号 == 当前版本）
 * 内容由后端渲染，前端只负责组装布局 + 目录 + 标红，故几乎无接口、无写操作，
 * 适合作为「旧页 → Vue 原生」迁移模板的第一页 —— 之后的模块照这个骨架套。
 *
 * 布局层样式（.man-layout / .man-content / .man-toc / .man-toc-btn / .man-red-block）
 * 全部复用全局 panel.css（与旧页面同源），本组件不重定义，避免 scoped 的 data-v 失配。
 */
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import PanelHero from '../components/PanelHero.vue'
import { apiFetch } from '../lib/api'
import { pageCacheGet, pageCacheSet } from '../lib/pageCache'

const busy = ref(true)
const lastUpdate = ref('')
const error = ref('')
const contentHtml = ref('')
const appVersion = ref('')

const contentRef = ref<HTMLElement | null>(null)
const tocBtnRef = ref<HTMLElement | null>(null)
const tocNavRef = ref<HTMLElement | null>(null)

let scrollTimer = 0
let scrollHandler: ((e: Event) => void) | null = null

function esc(s: string): string {
  return s.replace(/[&<>"]/g, c =>
    c === '&' ? '&amp;' : c === '<' ? '&lt;' : c === '>' ? '&gt;' : '&quot;',
  )
}

function openToc(): void {
  const nav = tocNavRef.value
  const btn = tocBtnRef.value
  if (!nav || !btn) return
  nav.classList.remove('collapsed')
  nav.setAttribute('aria-hidden', 'false')
  btn.textContent = '收起'
  btn.setAttribute('aria-label', '收起目录')
}
function closeToc(): void {
  const nav = tocNavRef.value
  const btn = tocBtnRef.value
  if (!nav || !btn) return
  nav.classList.add('collapsed')
  nav.setAttribute('aria-hidden', 'true')
  btn.textContent = '目录'
  btn.setAttribute('aria-label', '展开目录')
}
function toggleToc(): void {
  if (tocNavRef.value && !tocNavRef.value.classList.contains('collapsed')) closeToc()
  else openToc()
}

async function loadManual(): Promise<void> {
  error.value = ''
  // 切页签回来先上缓存秒开（手册内容随版本走、基本不变），随后仍拉一次最新
  const cached = pageCacheGet<string>('manual-html')
  if (cached && !contentHtml.value) {
    contentHtml.value = cached
    busy.value = false
    await nextTick()
    buildTocAndRed()
  }
  busy.value = true
  try {
    // 当前版本：用于 hero 副标题 + 「本版本新增」自动标红
    try {
      const vr = await apiFetch('/api/version', 15000)
      if (vr.ok) {
        const vj = (await vr.json()) as { current?: string }
        // 接口返回的是带 v 前缀的完整版本（如 v2.1.0），统一去前缀，
        // 模板里补 'v' 拼接，避免出现「vv2.1.0」；标红匹配用子串不受影响
        appVersion.value = (vj.current || '').replace(/^v/i, '')
      }
    } catch {
      /* 版本取不到不影响主体 */
    }

    const r = await apiFetch('/manual?embed=1&_=' + Date.now(), 30000)
    if (!r.ok) throw new Error('HTTP ' + r.status)
    const html = await r.text()
    contentHtml.value = html
    pageCacheSet('manual-html', html)
    lastUpdate.value = '更新于 ' + new Date().toLocaleTimeString('zh-CN')
    await nextTick()
    buildTocAndRed()
  } catch (e) {
    error.value = '操作手册加载失败：' + esc(String(e))
  } finally {
    busy.value = false
  }
}

function buildTocAndRed(): void {
  const content = contentRef.value
  if (!content) return
  const body = content.querySelector('.man-body')
  if (!body) return

  // 目录：从 h2/h3 生成
  const heads = Array.from(body.querySelectorAll('h2, h3')) as HTMLElement[]
  const ul = tocNavRef.value?.querySelector('ul')
  if (ul) {
    ul.innerHTML = ''
    heads.forEach((h, i) => {
      if (!h.id) h.id = 'mh' + i
      const li = document.createElement('li')
      li.className = h.tagName === 'H3' ? 'lv3' : 'lv2'
      const a = document.createElement('a')
      a.href = '#' + h.id
      a.textContent = h.textContent || ''
      a.addEventListener('click', ev => {
        ev.preventDefault()
        h.scrollIntoView({ behavior: 'smooth', block: 'start' })
        closeToc()
      })
      li.appendChild(a)
      ul.appendChild(li)
    })
  }

  // 自动标红「本版本新增」段：找到含当前版本号或「本版本新增」的起始元素，
  // 到其后第一个 <hr> 或「往期」为止，整段包进 .man-red-block
  const ver = appVersion.value
  if (ver) {
    const kids = Array.from(body.children) as HTMLElement[]
    let startIdx = -1
    for (let i = 0; i < kids.length; i++) {
      const t = kids[i].textContent || ''
      if (t.indexOf(ver) >= 0 || t.indexOf('本版本新增') >= 0) {
        startIdx = i
        break
      }
    }
    if (startIdx >= 0) {
      let endIdx = kids.length
      for (let j = startIdx + 1; j < kids.length; j++) {
        const t2 = kids[j].textContent || ''
        if (kids[j].tagName === 'HR' || t2.indexOf('往期') >= 0) {
          endIdx = j
          break
        }
      }
      const block = kids.slice(startIdx, endIdx)
      const refNode = kids[endIdx] || null
      const wrap = document.createElement('div')
      wrap.className = 'man-red-block'
      block.forEach(n => wrap.appendChild(n))
      body.insertBefore(wrap, refNode)
    }
  }
}

onMounted(() => {
  tocBtnRef.value?.addEventListener('click', toggleToc)
  scrollHandler = () => {
    if (!tocNavRef.value || tocNavRef.value.classList.contains('collapsed')) return
    if (scrollTimer) window.clearTimeout(scrollTimer)
    scrollTimer = window.setTimeout(closeToc, 1600)
  }
  if (scrollHandler) document.addEventListener('scroll', scrollHandler, true)
  void loadManual()
})

onUnmounted(() => {
  if (scrollHandler) document.removeEventListener('scroll', scrollHandler, true)
  if (scrollTimer) window.clearTimeout(scrollTimer)
})
</script>

<template>
  <div>
    <PanelHero
      icon="manual"
      title="操作手册"
      :sub="appVersion ? 'nasdash 使用说明 · v' + appVersion : 'nasdash 使用说明'"
      :last-update="lastUpdate"
      :busy="busy"
      @refresh="loadManual"
    />

    <div v-if="error" class="man-loaderr">{{ error }}</div>

    <div class="man-layout" v-show="!error">
      <div class="man-content" ref="contentRef" v-html="contentHtml"></div>
      <button class="man-toc-btn" id="manTocBtn" ref="tocBtnRef" type="button" aria-label="展开目录">
        目录
      </button>
      <nav class="man-toc collapsed" id="manToc" ref="tocNavRef" aria-hidden="true">
        <div class="man-toc-title">本页目录</div>
        <ul></ul>
      </nav>
    </div>
  </div>
</template>

<style scoped>
.man-loaderr {
  color: var(--red, #e5484d);
  padding: 12px 14px;
  font-size: 13px;
}
</style>
