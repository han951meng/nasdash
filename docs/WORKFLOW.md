# nasdash 开发 / 发版工作流（本软件专属开发文档）

> 本文档只针对 **nasdash**（飞牛 OS 硬件监控面板，包名 `com.dashboard.nasdash`）这一个软件。
> 覆盖：开发/发版工作流、前端架构约定、功能实现方式、数据采集架构、**踩坑与修复记录**、开发守则。
> 目的：改代码前先查文档，避免"修一个 bug 冒一个 bug"。**动手前先看 §5 踩坑记录 与 §6 开发守则。**

基于 `v2.0.1` 基线。所有修改一律从干净基线出发，绝不用旧包 / 旧图标当母版。

## Step 0 · 明确需求（先想清楚再动手）

- 要修的 bug：复现路径、期望行为、影响范围（哪些页面 / 接口 / 硬件）。
- 要加的功能：用户故事、配置项、UI 入口、是否要持久化配置。
- 不确定就先和用户确认，不要凭猜实现。

## Step 0.5 · 查飞牛官方开发者文档（避免绕弯路）

动手改代码 / 配置 / manifest / 图标前，先翻官方文档确认规范，少走弯路：

- 开发者文档总入口：<https://developer.fnnas.com/docs/>
- 应用图标规范：<https://developer.fnnas.com/docs/core-concepts/icon/>
  - 注意：官方写「ICON.PNG=64×64」是**误导**。实测只有位图内容全部 256×256 才清晰（参考 fnos-hermes-agent 做法）。图标统一由 `scripts/generate_icon.py` 生成。
- manifest / fpk 结构、应用中心生命周期（install / start / stop / uninstall）以官方文档为准。

## Step 1 · 基线对齐

```bash
git fetch
git checkout v2.0.1        # 或 git pull 到最新 main（HEAD 即 v2.0.1 发版 commit）
grep '^version' manifest   # 确认 version = 2.0.1
```

## Step 2 · 编码 / 改图标

- 改 `app.py` / `ui` / `config` / `vendor`。
- 改图标**只改 `scripts/generate_icon.py`**（1024 母版下采样），重跑生成 4 个 256×256 文件，不要另起炉灶、不要按官方做真 64×64。

## 前端架构约定（图标 / 主题 / CPU）

> v2.0 视觉焕新后沉淀下来的约定。写新页面 / 加新功能前先过一遍，避免又退回 emoji + 写死色值的老路。

### 图标：统一 SVG 线框（对齐飞牛 file-tools 风格）

- **单一数据源**：所有图标定义在 `templates/index.html` 的 `ICONS` 对象里（`key: '<path .../>'`，`viewBox="0 0 24 24"`、`fill:none`、描边用 `currentColor`、不写死颜色）。
- **雪碧图自动生成**：一段 IIFE 在运行时把每个 key 注入成 `<symbol id="i-<key>">` 挂到 `document.head`；静态 HTML 与 JS 模板字符串共用同一份定义，明暗自动跟随。
- **两种引用方式**：
  - JS 模板里：`iconSvg('key')` → 返回 `<svg class="ico"><use href="#i-key"/></svg>`。
  - 静态 HTML 里：直接写 `<svg class="ico" viewBox="0 0 24 24"><use href="#i-key"/></svg>`。
- **新增图标**：在 `ICONS` 里加一行 `key:'<lucide 风格 path>'`（`stroke` 为主、不填色）即生效，无需改别处。**不要再引入 emoji**——已用 unicode 范围正则兜底校验，emoji 混入会卡 CI。
- `.ico` 默认 16px、`stroke-width:2`、垂直对齐 `-3px`；按钮内 `.icon-btn .ico` 为 15px；需要别的尺寸用 `iconSvg('key','extra-cls')` 传额外 class 或在 `.ico` 基础上加 class。

### 主题：三档模式 + 跟随飞牛系统明暗

- **三档**：`light` / `dark` / `auto`（自动跟随飞牛系统明暗）。`auto` **绝不用**浏览器 `prefers-color-scheme`（fnOS webview 不暴露该接口，会恒浅），而是读飞牛写入的 `localStorage.fnos-theme-mode`（dark/light）。
- **真值只看 `data-theme`**：`document.documentElement.dataset.theme` 是页面真实明暗。JS 判断当前明暗统一用 `currentIsDark()`（只读 `data-theme`），不要自己再算一遍——否则会和页面显示对不上。
- **关键函数**（均在 `templates/index.html`）：
  - `getThemeMode()`：读模式（light/dark/auto）。
  - `fnosThemeMode()`：取飞牛系统明暗（优先级：自身 URL 参数 `?fnos-theme-mode=` → 父框架 URL 参数 → `localStorage`），取不到返回 null。
  - `resolvedDark()`：`auto` 分支用 `fnosThemeMode()`，仍保留 `matchMedia` 作兜底；`applyTheme()` 按模式落 `data-theme`。
  - `cycleTheme()`：单按钮循环 浅 → 深 → 自动。
- **实时跟随**：飞牛切主题时会更新 `localStorage.fnos-theme-mode` 并触发 `storage` 事件；前端在 `auto` 下监听该事件即时 `applyTheme()`。所以**改主题只动变量、不写死色值**——新增组件颜色一律走 Token（`--primary` / `--text-1` / …），详见 `docs/ui-2.0-design-spec.md`。
- **CSS 写法**：`[data-theme="dark"]` 覆盖同名 Token；`<head>` 内防闪脚本在页面渲染前先读 localStorage 设好 `data-theme`，避免浅↔深闪白。

### 真实 CPU 使用率

- `app.py` 的 `get_cpu_usage()` 读 `/proc/stat` 聚合行，与模块级 `_LAST_CPU_STAT` 快照算 idle 差值返回百分比；跨请求保存快照、无阻塞 sleep。首次调用无基准返回 `None`（前端显示「—」），**不要用 load average 冒充使用率**。前端系统资源页用该值。

## 功能清单与实现方式

| 功能 | 实现方式 | 关键接口 |
|---|---|---|
| 硬件配置检测（首页） | `get_system`+`get_board`+`get_memory_modules`+`get_raid_card`+`get_disks` 并行采集 | `/api/all` |
| 阵列卡 | `storcli /c0 show` 等（内置 storcli64） | `/api/raid` |
| 硬盘 SMART | `smartctl -a` 逐盘（并行），健康/温度/寿命 | `/api/disks` |
| 系统资源 | `get_system`（CPU/内存/负载/GPU/网卡） | `/api/system` |
| 历史趋势 | metrics daemon 每 30s 写 SQLite，前端按桶聚合 | `/api/history` |
| 风扇控制 | 逐风扇 hwmon PWM 读写 + 温控规则（线性/曲线/手动） | `/api/fan/*` |
| 风扇温控接管 | 常驻线程 `fan_smooth_loop`（0.6s tick）写 pwm；接管时停系统风扇服务 | — |
| 温度监控 | 统一温度快照（见下节） | `/api/fan/temps` |
| 存储卷 | `mdadm` + `df` | `/api/storage` |
| Docker | `docker ps/stats/inspect`（60s 缓存） | `/api/docker` |
| 控制与自动化 | 自动刷新、告警、硬盘自检（SMART 长自检 / badblocks） | `/api/disks/selftest` 等 |
| 实时数据 | metrics daemon（CPU 使用率/功耗/网速/磁盘 IO） | `/api/metrics` |

## 数据采集架构（核心设计：一次采样、处处共享）

**设计原则：任何数据只有一个"权威采集点"，所有页面/接口从同一份快照或缓存取数，绝不各自重复采集。**

| 数据 | 采集方式 | 缓存 | force（手动强制） |
|---|---|---|---|
| 温度（CPU/主板/测点墙） | `_temp_collect_loop` 每 2s 一次 `sensors -j` | `_TEMP_SNAP` 2s | 无需（本就 2s） |
| 硬盘温度 | `get_disk_temps_cached` | 4s TTL + 非阻塞锁 | 无 |
| 硬盘 SMART | `get_disks` → `_collect_disks_full` | 60s TTL + 锁 | ✅ `?force=1` |
| 系统总览 | `get_system` → `_collect_system_full` | 30s TTL + 锁 | ✅ `?force=1` |
| 阵列卡 | `get_raid_card` | 60s TTL | 无 |
| 主板/内存条 | `get_board` / `get_memory_modules` | 60s TTL | 无 |
| 网卡静态信息 | `get_network_nics` | 60s TTL | 无 |
| 存储卷 | `get_storage` | 60s TTL | 无 |
| Docker | `get_docker` | 60s TTL | 无 |
| CPU 使用率/功耗/网速/磁盘 IO | `metrics_collect_loop` daemon | 每秒写缓存 | 无 |

**force 语义**：用户手动「立即刷新」（前端 `loadData(true)`）→ `/api/all?force=1` → `get_disks(force=True)`/`get_system(force=True)` 强制重采；30s 自动刷新不带 force，走缓存。

**缓存实现要点**（`get_disks`/`get_system` 模板）：
- 模块级 `_XXX_CACHE = {"t","v"}` + `_XXX_LOCK`；TTL 内直接返回；过期非阻塞抢锁，抢不到返回旧值（**绝不阻塞**）。
- `force=True` 时阻塞锁等待后强制重采（低频操作，可等）。

## 踩坑与修复记录（改代码前必读）

> 每条按"现象 → 根因 → 修复方法"记录。**修 bug 前先查这里，避免重复踩、避免修出新 bug。**

### 温度类

| # | 现象 | 根因 | 修复方法 |
|---|---|---|---|
| T1 | CPU 温度虚高（49°C vs 真实 38°C） | `sensors -j` coretemp 的 `temp1_input` 是驱动"虚拟最高点"读数，被 `_fan_read_cpu_temp` 当封装温度取走 | `_parse_cpu_temp`：Intel 取核心(Core N)测点 max 并**排除 temp1_input**；AMD 取 Tdie/Tctl |
| T2 | hero 41 vs 温度墙★最准 39（同一份数据差 2°C） | CPU 温度被两个函数各算一遍（`_parse_cpu_temp` 核心 max vs `_parse_sensors_all` 取 Package 第一个 `_input`）——两套解析语义 | **权威值归口**：温度墙"CPU 封装温度"条目直接引用 `_parse_cpu_temp(j)` 的权威值，不再自己另算 |
| T3 | CPU 温度秒跳（39→47） | 核心温度热容量小，瞬时负载下 1~2s 跳 10°C+ 是物理现象，但显示追尖峰很晃 | `_smooth_cpu_temp` EMA（前值 50%+新值 50%，采集 2s），控速同源转速更稳 |
| T4 | 温度显示带小数点（38.9°C） | EMA/传感器读数本身是小数 | 后端快照写入统一 `int(x+0.5)` 取整（EMA 内部保留 float）；前端平均温度 `Math.round` |
| T5 | 温度墙 30s 才刷新，不及时 | 温度墙读 `/api/all`/`/api/system` 快照（30s 自动刷新才更新） | 温度墙改读统一快照（`/api/fan/temps` 的 `sensors`+`raid_temp`），前端每 5s 就地重绘 |
| T6 | 控速线程每 0.6s 跑一次 `sensors -j` | `fan_smooth_loop`/`get_fan_status` 直接调 `_fan_read_sys_temp`（无缓存） | `_fan_read_sys_temp` 改读统一快照，不再自己跑命令 |

### 缓存 / 高频轮询类（风暴源）

| # | 现象 | 根因 | 修复方法 |
|---|---|---|---|
| C1 | 面板所有接口全卡、转速冻结、连轻量接口都 pending | `/api/fan/temps` 被前端每 5s 轮询，无缓存每次全量 smartctl 扫 6 盘（8s 超时/盘）；且控速线程每 0.6s 读盘温 → 每秒十几发 smartctl 打满磁盘 I/O，把后端与飞牛网关堵死 | `get_disk_temps_cached`（4s TTL + 非阻塞锁：过期抢锁扫描、抢不到返旧值），`_disk_source_state`/`api_fan_temps` 全部改走缓存 |
| C2 | `/api/metrics` 约 2s 才返回 | 每次请求都调 `get_disks()`（全量 smartctl）只为补 model/size/brand/type 静态信息 | `_disks_meta_map`（300s TTL + 非阻塞锁） |
| C3 | 手动刷新拿不到最新 SMART/系统数据 | 缓存无 force，刷新被 TTL 挡住 | `get_disks`/`get_system` 支持 `force`，前端「立即刷新」`loadData(true)` → `?force=1` |
| C4 | 缓存并发重复采集 | `@_ttl_cache` 无锁，并发 miss 各采一遍 | 新缓存统一带 `_LOCK` 非阻塞抢锁 |

### 前端刷新类

| # | 现象 | 根因 | 修复方法 |
|---|---|---|---|
| F1 | 风扇页每 5s 整页闪烁 + 温度显示/消失 | `fetchTemps`（5s 轮询）末尾调 `refreshPanel('fan')` 整页 innerHTML 重建，而温度行初始为空、靠 `_paintTemps` 填 → 每 5s"填上→重建清空" | ① `fetchTemps` 不再整页重建，改就地更新（检测/温度页 hero CPU 温度用 id 就地 textContent）；② 抽出 `_buildTempRow()`，`renderTempOverview` 重渲时直接内嵌 chip，永不留空行 |
| F2 | 30s 自动刷新整页闪（风扇页） | 用户开了自动刷新（30s 整页重载），风扇页数据本是 1s/5s 就地更新 | `renderPanel('fan')` 豁免整页重建（方案 A）；其它页面照常 30s 刷新 |

### 部署 / 工具 / 平台类

| # | 现象 | 根因 | 修复方法 |
|---|---|---|---|
| D1 | 改函数后语法错误，坏文件已上传、应用起不来 | 目标函数原本有装饰器（如 `@_ttl_cache(300)`），插入代码时把装饰器隔断在模块级变量前 → SyntaxError | 改函数前**先查它有没有装饰器**；部署命令 `py_compile && echo OK` 若输出无 OK **严禁继续上传** |
| D2 | trim-cli `file upload` 返回 "Uploaded" 但文件没变 | 同名文件静默不覆盖 | 用 `--overwrite replace`；部署后对比本地/NAS 文件 size 确认（`file size <path>`） |
| D3 | 部署 cmd 生命周期脚本无效 | `app.py/templates/bin` 在 `/vol1/@appcenter/...`（admin 可直写），但 `cmd/main` 在 `/var/apps/.../cmd/`（admin 写不了） | cmd 须 sudo cp 到 `/var/apps/com.dashboard.nasdash/cmd/`；`TRIM_APPDEST` 指向 `/vol1/@appcenter` 与 cmd 不同 |
| D4 | 应用重启后代码没生效 | `app restart` 可能未真正拉起新 python（cmd/main start 在已 running 时不重跑） | 用 `app restart` 后看 `app.sock` mtime 是否更新确认新进程；必要时应用中心停用/启用 |
| D5 | 修改 manifest 不生效 | 飞牛 manifest 安装时读取缓存，运行时改无效 | 必须卸载重装 fpk |
| D6 | 网关会话失效显示 "invalid token" | 应用中心重启后 cgi 令牌变化；直接开 cgi URL 缺桌面上下文 | 从飞牛桌面点图标进（iframe 内正常）；ego 验证也须走桌面 iframe 读 `contentWindow` |
| D7 | 风扇标注被整体覆盖丢失 | 保存接口整体覆盖 fan_labels.json | `api_fan_labels_post` 必须**合并模式**（load 全量 → update 单 key → save）；空标注=删 key |
| D8 | CPU 使用率瞬时 100%/0% 尖刺 | 并发请求各自读 /proc/stat，采样窗口被压成毫秒级 | metrics daemon 固定 1s 窗口采样写 `_CPU_USAGE_CACHE`，请求读缓存 |

### 数据解析 / 展示类

| # | 现象 | 根因 | 修复方法 |
|---|---|---|---|
| P1 | 风扇卡片重名"一张变两张" | 同一物理风扇被 sensors 双芯片/多路径报两遍；或 FCS 配置重复命名 | 风扇卡片唯一来源 `_enumerate_fans()`（只含 pwm 通道 + 芯片前缀命名）；FCS 配置名撞车加 `#fan{idx}` 后缀 |
| P2 | 双磁臂 SAS 盘显示成两块 | 同序列号拆成两个 LUN（sda/sdb，各 7.0T） | 前端按 serial+model+type 同组智能合并为一条（双磁臂徽章、容量合并、温度取最高、健康取最差），**纯前端展示不改存储** |
| P3 | badblocks 进度恒 0% | `badblocks -s` 用退格符刷新进度，buf 堆满历史行，`re.search` 恒命中第一行 0.00% | `re.findall` 取**最后一个**匹配 |
| P4 | webview 下 number 输入框异常 | type=number 在 webview 有 bug | 用 type=text + inputmode=numeric + pattern + 自定义步进 |
| P5 | 网卡 IP 解析不到 | `ip -o addr` 接口名后无冒号；OVS 桥才是真实 IP | 正则 `^\d+:\s+(\S+).*?\binet\s+(\S+)`；link/addr 都 `split("@")[0]`；真实 IP 在 eno1-ovs 桥 |
| P6 | 风扇"停不下来/忽快忽慢"误报 | 0% 已下发但惯性减速中；或 pwm_enable=2（交还主板）读到的 0 非自己下发 | 入场 8s 才提示（等惯性）；`pwm_enable==1` 才算自己控速；退场 1.5s 延迟隐藏防抖 |
| P7 | 温度三页各跳各的 | 各页各自取值 | 前端 `cpuTempUnified()`：优先实时温度快照，回退首屏快照，三页强制同源 |

### 发版 / 打包类（历史已踩，勿再踩）

- `manifest` 的 `desc` / `changelog` 必须单行，换行 → 应用中心 10111。
- `manifest.checksum` 必须等于 `md5(app.tgz)`（GNU tar mtime 漂移，须重建时重算）。
- 随包二进制（如 storcli）必须塞进 `app.tgz` 内，fpk 顶层额外目录飞牛不解包。
- 图标位图全部 256×256，勿信官方「64×64」。
- `pkill -f com.dashboard.nasdash` 经 SSH 自匹配杀会话，只用 `appcenter-cli stop`。
- 发布资产必须是**带向导完整版**（`--with-wizard`）；发布说明/手册禁止出现内部部署话术（build.sh/trim-cli/wizard 等）。

## 开发守则（避免"修一个 bug 冒一个 bug"的强制检查清单）

> 每条坑后面都是真实事故。**动手改代码前逐条过一遍。**

### 改代码前
- [ ] **查调用点**：`grep` 该函数/路由所有调用点，确认改一个不会连带坏别处
- [ ] **查装饰器**：目标函数是否被 `@_ttl_cache` 等装饰（插入代码别隔断装饰器，D1 教训）
- [ ] **查缓存**：被前端高频轮询的接口，后端必须有缓存；加缓存必须考虑 锁/force/陈旧值
- [ ] **查口径**：同一数值（如 CPU 温度）只能有一个权威来源，其他消费方引用它，禁止各算一遍（T2 教训）

### 改代码后 / 部署前
- [ ] 后端：本地 `python3 -m py_compile app.py` **必须输出 OK** 才允许上传（D1 教训）
- [ ] 前端：抽取全部 `<script>` 块 `node --check` 语法全过
- [ ] 改 Flask 路由：`grep` 确认 `@app.route` 归属，前端注入要对准真实读取端点
- [ ] 上传用 `--overwrite replace`，上传后对比本地/NAS 文件 size 确认真的覆盖了（D2 教训）
- [ ] 重启后看 `app.sock` mtime 确认新进程在跑（D4 教训）

### 验证
- [ ] 真机请求确认字段正确（不只比对文件大小）
- [ ] 修完跑一遍相关页面 + 相邻页面（温度改动要验三页一致；缓存改动要验手动刷新 force；风扇改动要验转速）
- [ ] 数值展示：统一口径 + 平滑（防秒跳）+ 取整（防小数点）

### 新增功能
- [ ] **接入统一采集模式**：新数据源进对应采集循环/缓存，页面从快照读，不自己开新采集
- [ ] 新缓存抄 `get_disks` 模板（TTL + 锁 + force）
- [ ] 前端新页面复用 `cpuTempUnified()` 等统一取值函数

## Step 3 · 本地自测（不上真机，早暴露问题）

```bash
./scripts/local_smoke.sh
```

- `py_compile` 语法检查；若有 flask 则本地起服务 curl `/api/version`、`/api/all`。
- 硬件接口在 Mac 上可能 500，属正常，只要服务启动且 `/api/version` 返回 200 即可。

## Step 3.5 · 跑回归测试（守护历史 bug 不复发）

```bash
./scripts/test.sh          # 纯函数 pytest：品牌识别 / 阵列卡温度 / NVMe 通电时长 等 15 个用例
```

- 不依赖硬件，本地 venv 跑；覆盖 v1.7.5(通电时长逗号截断)、v1.7.8(金士顿误判三星) 等历史修复。
- 改了 `app.py` 的解析逻辑后务必跑一遍，再上真机。

## Step 4 · 改版本号 + 同步四处一致（一键）

```bash
./scripts/release.py 1.8.8 "一句话更新要点"
```

自动改 `manifest`(version/desc/changelog) + `README.md`(版本号/更新日志) + 重建 fpk + 校验。
（手动方式见 `scripts/update_manifest.py` 旧脚本。）

## Step 5 · 重建 fpk + 一致性校验（一键）

```bash
./build.sh        # 内含 ./scripts/verify.sh
```

`scripts/verify.sh` 自动挡：三处 md5 一致、manifest desc 单行（否则应用中心报 10111）、版本号三处（manifest / fpk / README）一致。

## Step 6 · 部署到 NAS（真机自测用无向导版）

测试/自测部署用**无向导版**（`bash build.sh` 产物，fpk 根不含 `wizard/`），经 trim-cli 一键部署。
（连接参数固定：`--host 192.168.50.158 --scheme ws --port 5666 --allow-insecure-ws`；须先 `export TRIM_CLI_SESSION_STORAGE=file`）

```bash
# 标准流：stop → uninstall → install-fpk → start
trim-cli ... app stop com.dashboard.nasdash --yes
trim-cli ... app uninstall com.dashboard.nasdash --yes
trim-cli ... app install-fpk /path/nasdash.fpk --volume-id 1 --yes
trim-cli ... app start com.dashboard.nasdash --yes
```

- **若 uninstall 被飞牛拦死**（NAS 上装的是向导/WebUI 版时常见）：改用 cp 兜底——先 `app stop`，再把本地 `app.tgz` 解包覆盖到运行目录 `/vol1/@appcenter/com.dashboard.nasdash/`，并同步 `/var/apps/<appid>/manifest` 版本号，最后 `app start`。
- 部署前先备份 `@appdata`：`sshpass -p hanyuvip ssh ... "tar czf /vol1/1000/nd_appdata_backup_$(date +%Y%m%d_%H%M%S).tar.gz -C /vol1/@appdata com.dashboard.nasdash"`。
- 首启若报 10500（端口竞态），再 `start` 一次自愈。

## Step 7 · 真机验证

```bash
appcenter-cli list                                  # 版本号与 manifest 一致
curl -s http://localhost:9800/api/version          # current == manifest
curl -s http://localhost:9800/api/all              # HTTP 200
ss -ltnp | grep 9800                               # 端口在监听
ps -o user= -p $(pgrep -f com.dashboard.nasdash)   # 运行用户为 root
```

## Step 8 · 发版（发布版必须带向导）

发布资产必须是**带向导完整版**（`bash build.sh --with-wizard` 产物，fpk 根含 `wizard/`），绝不能用无向导测试版当发布物。

```bash
# 1) 构建带向导发布版，并复制成发布资产名
bash build.sh --with-wizard
cp nasdash.fpk nasdash-release-vX.Y.Z.fpk

# 2) 提交源码 + 发布说明（fpk 本身是 Release 资产、不入库）
git add README.md app.py "docs/使用手册.md" manifest templates/index.html release_notes_vX.Y.Z.md
git commit -m "release: vX.Y.Z"
git tag vX.Y.Z && git push origin main && git push origin vX.Y.Z

# 3) 建 Release（发布说明作 -F，fpk 作位置参数放末尾）
gh release create vX.Y.Z -F release_notes_vX.Y.Z.md -t "nasdash vX.Y.Z" --latest nasdash-release-vX.Y.Z.fpk
```

- 发版后改了任何会进包的内容（手册/app.py/模板/配置），必须**重建带向导版 → `gh release delete-asset` 旧资产 → `gh release upload` 新资产**，并比对 GitHub 资产字节数 = 本地 fpk 字节数确认无误。
- 发布说明（`release_notes_*.md`）与操作手册面向最终用户，**禁止出现 build.sh / trim-cli / 无向导版 / 带向导版 / wizard / fnos-fpk-dev.md 等内部部署话术**。

## 附：回滚预案

- 部署前留底：`cp nasdash.fpk /tmp/nasdash_v<上一版>.fpk`。
- 出问题：新机 `uninstall → install-fpk /tmp/nasdash_v<上一版>.fpk --volume 1 → start`。
- git 角度：每个发版 commit 即回滚点，`git checkout v1.7.8` 重建即可。

## 附：大改动用分支

```bash
git checkout -b fix/xxx v1.7.8
# ... 开发验证 ...
git checkout main && git merge fix/xxx && git tag vX.Y.Z
```

## 版本演进速记（v2.0.x）

| 版本 | 关键内容 |
|---|---|
| v2.0.4 | CPU 缓存修复 / 负载两位小数 / 阵列卡温度入温度 tab / 网络卡 5s 刷新 |
| v2.0.5 | 全局告警去硬盘 SMART / 温度 chip 分类对齐 / 移除系统风扇服务状态框 |
| v2.0.6（未发布） | 风扇页防闪烁（F1/F2）/ 统一温度采集（T1-T6）/ 硬盘+系统统一缓存（C1-C4）/ CPU 温度平滑取整（T3/T4） |
