# nasdash 2.0 UI 设计草案（Token + 组件类）

> 目标：和飞牛原生应用"一个声音说话"。飞牛/Semi Design 的核心不是某一种颜色，而是**统一的设计变量 + 统一的原子组件**。本草案先把"图纸"（Token）和"积木"（组件类）定下来，再逐页迁移。
>
> 当前状态：v1.9.9 已发布（干净基线）。昨夜未测的毛玻璃 UI 在 git stash 里，2.0 不从它改，而是**另起炉灶**按本规范重写前端样式层。

---

## 一、设计原则（3 条，定调性）

1. **克制**：扁平为主，细边框 + 轻阴影，几乎不用渐变/毛玻璃/光晕（昨晚毛玻璃浅色翻车就是反例）。
2. **统一**：全站颜色、圆角、间距、边框都来自同一套 Token，改主题只动变量，不再"改一处漏一处"。
3. **内容优先**：主信息大、次要信息淡、留白充足，一眼抓住重点（现在监控数据平铺、没有主次）。

---

## 二、设计 Token（地基）

### 2.1 颜色 —— 主色对齐飞牛 Semi Design 蓝 `#006AFF`

**浅色（Light）**

| 语义 | Token | 值 | 用途 |
|---|---|---|---|
| 主色 | `--primary` | `#006AFF` | 品牌色、选中态、主按钮、链接 |
| 主色悬浮 | `--primary-hover` | `#3385FF` | 按钮 hover |
| 主色按下 | `--primary-active` | `#0052CC` | 按钮 active |
| 成功 | `--success` | `#00B42A` | 正常/OK/PASSED/通过 |
| 警告 | `--warning` | `#FF7D00` | 偏高/注意 |
| 危险 | `--danger` | `#F53F3F` | 故障/超温/错误 |
| 文字-主 | `--text-1` | `#1D2129` | 标题、主数值 |
| 文字-次 | `--text-2` | `#4E5969` | 正文 |
| 文字-次次 | `--text-3` | `#86909C` | 辅助说明、副标题 |
| 文字-占位 | `--text-4` | `#C9CDD4` | placeholder |
| 边框 | `--border` | `#E5E6EB` | 卡片/输入框描边 |
| 分割线 | `--divider` | `#F2F3F5` | 行内分隔 |
| 填充底 | `--fill` | `#F2F3F5` | 子面板/标签背景 |
| 页面底 | `--bg` | `#F7F8FA` | 整体背景 |
| 卡片底 | `--card` | `#FFFFFF` | 卡片背景 |

**深色（Dark）** —— 用 Semi 暗色体系（深灰而非死黑，层次更稳）

| 语义 | Token | 值 |
|---|---|---|
| 主色 | `--primary` | `#3C7EFF`（暗底上提亮一档，比 #006AFF 更跳） |
| 主色悬浮 | `--primary-hover` | `#5C92FF` |
| 成功 | `--success` | `#2BC96A` |
| 警告 | `--warning` | `#FF9A2E` |
| 危险 | `--danger` | `#F76965` |
| 文字-主 | `--text-1` | `#F7F8FA` |
| 文字-次 | `--text-2` | `#C9CDD4` |
| 文字-次次 | `--text-3` | `#86909C` |
| 边框 | `--border` | `#2E2E30` |
| 分割线 | `--divider` | `#232324` |
| 填充底 | `--fill` | `#232324` |
| 页面底 | `--bg` | `#17171A` |
| 卡片底 | `--card` | `#232324` |

> 注：你之前认可的深色"纯黑玻璃"这次**不沿用**，改走 Semi 深灰体系——更稳、层次更清楚，也不会有"毛玻璃浅色翻车"的坑。如果你想保留纯黑底，告诉我，我把 `--bg`/`--card` 调回 `#000`/`#161618` 即可。

### 2.2 圆角

| Token | 值 | 用途 |
|---|---|---|
| `--r-sm` | `6px` | 标签、小按钮、输入框 |
| `--r-md` | `10px` | 表格单元格、开关 |
| `--r-lg` | `16px` | 卡片、面板 |
| `--r-xl` | `20px` | 大容器（侧栏、内容区外框，可选） |
| `--r-round` | `999px` | 圆形头像、状态点、分段控件 |

### 2.3 间距（8 进制为主）

`--s-1=4` `--s-2=8` `--s-3=12` `--s-4=16` `--s-5=20` `--s-6=24` `--s-8=32`

卡片内边距统一 `--s-4`（16）或 `--s-5`（20）；区块间距 `--s-6`（24）。

### 2.4 字体

- 字体族：`"PingFang SC","SF Pro SC",-apple-system,"Microsoft YaHei",sans-serif`
- 字号：页头标题 `--fs-title=20px` / 卡片标题 `16px` / 正文 `--fs-body=14px` / 辅助 `13px` / 说明 `12px`
- 字重：标题 `600`，正文 `400`，强调数值 `600`

### 2.5 阴影 / 边框

- 浅色卡片：`box-shadow:0 2px 8px rgba(0,0,0,.04)`（极轻，主要靠 1px 边框立形）
- 深色卡片：`box-shadow:0 2px 8px rgba(0,0,0,.3)` + `border:1px solid var(--border)`
- 悬浮（按钮/卡片 hover）：`0 4px 12px rgba(0,0,0,.08)`

### 2.6 动效

- 过渡：`transition:all .2s ease`
- 主题切换：变量切换瞬时完成（配合 `<head>` 内防闪脚本，避免浅↔深闪白）

---

## 三、组件类草案（积木）

> 全部用 class 实现，替换现有散落的 inline style。以下为关键样式骨架。

### 3.1 布局
```css
.app{display:flex;min-height:100vh}
.sidebar{width:220px;background:var(--card);border-right:1px solid var(--border)}
.content{flex:1;background:var(--bg);padding:var(--s-6)}
.page-head{margin-bottom:var(--s-5)}
.page-title{font-size:var(--fs-title);font-weight:600;color:var(--text-1)}
.page-sub{font-size:13px;color:var(--text-3);margin-top:4px}
```

### 3.2 卡片 `.card`
```css
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--r-lg);
  padding:var(--s-4);box-shadow:0 2px 8px rgba(0,0,0,.04)}
.card-title{font-size:16px;font-weight:600;color:var(--text-1);margin-bottom:var(--s-3)}
```

### 3.3 按钮 `.btn` 系列
```css
.btn{border:none;border-radius:var(--r-sm);padding:7px 16px;font-size:14px;cursor:pointer;transition:.2s}
.btn-primary{background:var(--primary);color:#fff}
.btn-primary:hover{background:var(--primary-hover)}
.btn-outline{background:transparent;border:1px solid var(--border);color:var(--text-2)}
.btn-ghost{background:transparent;color:var(--primary)}
.btn-danger{background:var(--danger);color:#fff}
```

### 3.4 表格 `.table`（Semi 风：表头浅填充、行 hover、底线分隔）
```css
.table{width:100%;border-collapse:collapse;font-size:14px}
.table th{background:var(--fill);color:var(--text-3);font-weight:500;text-align:left;
  padding:10px 12px;border-bottom:1px solid var(--border)}
.table td{padding:10px 12px;border-bottom:1px solid var(--divider);color:var(--text-2)}
.table tbody tr:hover{background:var(--fill)}
```

### 3.5 开关 `.switch`
```css
.switch{width:40px;height:22px;border-radius:var(--r-round);background:var(--border);position:relative;transition:.2s}
.switch.on{background:var(--primary)}
.switch::after{content:"";position:absolute;width:18px;height:18px;border-radius:50%;background:#fff;
  top:2px;left:2px;transition:.2s}
.switch.on::after{left:20px}
```

### 3.6 滑块 `.slider`
```css
.slider{-webkit-appearance:none;height:4px;border-radius:var(--r-round);background:var(--border);outline:none}
.slider::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:50%;background:var(--primary);cursor:pointer}
```

### 3.7 标签 / 状态徽章 `.tag`
```css
.tag{display:inline-block;padding:1px 8px;border-radius:var(--r-sm);font-size:12px}
.tag-success{background:rgba(0,180,42,.12);color:#0A8C20}
.tag-warning{background:rgba(255,125,0,.12);color:#C85A00}
.tag-danger{background:rgba(245,63,63,.12);color:#C7392F}
.tag-info{background:rgba(0,106,255,.12);color:var(--primary)}
```
> 深色下 `.tag-*` 用 `color` 提亮一档（如 `#2BC96A`/`#FF9A2E`），背景保持 12% 透明。

### 3.8 输入框 `.input`
```css
.input{border:1px solid var(--border);border-radius:var(--r-sm);padding:7px 10px;font-size:14px;
  background:var(--card);color:var(--text-1)}
.input:focus{border-color:var(--primary);outline:none}
```

### 3.9 指标卡 `.stat`（大数字，用于系统资源/温度）
```css
.stat{display:flex;flex-direction:column;gap:4px}
.stat-val{font-size:28px;font-weight:600;color:var(--text-1);line-height:1}
.stat-label{font-size:13px;color:var(--text-3)}
```

### 3.10 进度条 `.progress`
```css
.progress{height:6px;border-radius:var(--r-round);background:var(--border);overflow:hidden}
.progress > i{display:block;height:100%;background:var(--primary);border-radius:var(--r-round)}
```

### 3.11 分隔线 `.divider`
```css
.divider{height:1px;background:var(--divider);margin:var(--s-4) 0}
```

### 3.12 侧栏导航 `.nav-tab`
```css
.nav-tab{display:flex;align-items:center;gap:10px;width:100%;padding:10px 12px;border:none;
  background:transparent;color:var(--text-2);border-radius:var(--r-md);font-size:14px;cursor:pointer;text-align:left}
.nav-tab:hover{background:var(--fill)}
.nav-tab.active{background:rgba(0,106,255,.10);color:var(--primary);font-weight:600}
.nav-tab.active::before{content:"";position:absolute;left:4px;top:8px;bottom:8px;width:3px;background:var(--primary);border-radius:2px}
```

---

## 四、明暗主题机制

- 变量分两套：`[data-theme="light"]`（默认）和 `[data-theme="dark"]` 覆盖同名 Token。
- 切换：JS 只切 `document.documentElement.dataset.theme` + 写 `localStorage`，**所有颜色自动跟随**，不会有"漏掉 3 处绿色"的坑。
- `<head>` 内联防闪脚本：页面加载前读 localStorage 设好主题，避免浅→深闪白。

---

## 五、建议迁移顺序（避免返工）

1. **铺地基**：把第二节 Token 写进 `:root` + `[data-theme="dark"]`，删掉现在散落的 `--bg/--card/--green` 等旧变量。
2. **建组件类**：把第三节 12 个组件类写进 `<style>`，替换 inline style。
3. **逐页迁移**：概览 → 温度墙 → 风扇 → 磁盘/阵列卡 → 设置，每页把散落样式收编进组件类。
4. **补状态样式**：空态、加载中、错误态统一（现在没有）。
5. **自测**：浅/深两主题逐页扫一遍，确认无硬写死的色值残留。

---

## 六、待你拍板的点

- 深色底用 **Semi 深灰 `#17171A`**（推荐）还是保留你认可的 **纯黑 `#000`**？
- 主色用 **飞牛 Semi 蓝 `#006AFF`**（更贴飞牛）还是沿用之前 iOS 的 **`#007AFF`**？
- 圆角 `16px` 大卡片是否合适，还是想更收敛（12px）？
