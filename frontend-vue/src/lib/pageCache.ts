/**
 * 页签数据缓存 —— 让「切走再切回来」的页签秒开。
 *
 * 背景：页签切走时 Vue 页面会被销毁（v-if），切回来要重新拉接口，
 * 用户会看到一次「加载中」。现在每次拉到数据顺手存一份在内存里，
 * 下次进页面先用缓存立刻渲染，再在后台拉最新数据替换。
 *
 * 注意：只存内存（Map），不进 localStorage —— 刷新整页后缓存自然失效，
 * 不会出现「旧数据穿越」。只适合「展示型」数据，别把表单未保存状态放进来。
 */
const store = new Map<string, unknown>()

export function pageCacheGet<T>(key: string): T | undefined {
  return store.get(key) as T | undefined
}

export function pageCacheSet(key: string, value: unknown): void {
  store.set(key, value)
}
