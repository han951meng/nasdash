# nasdash

**当前版本：v1.6.8** · [下载最新 fpk](https://github.com/han951meng/nasdash/releases/latest)

飞牛OS（fnOS）NAS 硬件监控面板 —— FPK 应用包

一个轻量级单文件 Flask Web 应用，通过 `storcli` / `smartctl` / `sensors` / `mdadm` / `lspci` / `ip` 等系统命令采集 NAS 硬件状态，以网页面板展示，免去每次 SSH 敲命令的麻烦。

## 安装

1. 下载 `nasdash.fpk`
2. 在飞牛OS 应用中心上传安装
3. 安装时自动安装依赖：smartmontools / lm-sensors / mdadm / Flask（storcli 可选，非 LSI 阵列卡可忽略）
4. 安装后浏览器访问 `http://NAS_IP:9800`

> 旧版本手动安装过依赖的用户：直接覆盖安装即可，install_callback 会跳过已安装的工具。

## 功能

左侧边栏共六个模块，覆盖 NAS 硬件运维核心场景：

### 🏠 硬件配置检测（首页仪表盘）
单页总览，进入即看全貌：
- 顶部状态栏：实时概览（阵列卡 / 硬盘 / Docker / 系统负载）
- 系统配置：处理器（含型号、核心/线程）、显卡（有则显示）、内存、系统信息（含运行时间）、网络（网卡 IP / 速率 / 状态）
- 阵列卡 & 磁盘：卡型号与物理盘、SMART 健康速览

### 🃏 阵列卡（自动检测卡类型）
通过 `lspci` 自动识别存储控制器类型，分三种情况展示，避免误报：
- **MegaRAID 阵列卡**（如 LSI 9271-8i，IR/RAID 模式）：走 `storcli /c0 show` 显示完整信息——型号 / 序列号 / SAS 地址 / 固件版本 / BIOS 版本 / 驱动版本 / PCI 地址、CacheVault 缓存电池状态、**ROC 芯片温度**、JBOD 物理盘列表（槽位 / 品牌 / 型号(+特性徽章) / 接口 / 容量 / 状态 / 转速）。双磁臂（双执行器）盘显示整盘标称容量（如 14T）并标注每执行器容量
- **HBA 直通卡**（如 LSI 9300-8E，IT 模式）：显示卡型号与「IT 直通模式」说明，阵列卡页**额外显示 HBA 芯片温度**（通过 `storcli /c0 show temperature` 读取 ROC temperature，如 65°C）；每块盘温度仍前往「硬盘 SMART」标签页查看（直通卡盘由系统直接识别为 `/dev/sdX`，由 smartctl 直读）
- **纯 SATA 主板**（无独立阵列卡）：提示无独立阵列卡，温度请见「硬盘 SMART」标签

### 💿 硬盘 SMART
- 同时支持 SAS/SCSI 盘和 SATA/ATA 盘
- 自动识别硬盘品牌与特性（如希捷 / 西数 / 东芝 / 三星，双磁臂 / 双执行器盘带徽章）
- 健康状态（OK/PASSED 标绿，异常标红）
- 温度 / 通电时长 / 坏块计数 / 错误计数
- **机械盘显示真实转速**（来自 smartctl Rotation Rate，如 7200 rpm / 5400 rpm）；SSD 显示「固态(SSD)」
- SATA 盘额外显示 Reallocated / Pending / Uncorrectable / UDMA CRC
- 容量从 smartctl 读取（兼容阵列卡后 lsblk 返回 0 的情况）

### 📊 系统资源
- CPU 型号 / 核心数 / 线程数 / 频率 / 负载
- 内存 / Swap 使用率
- **温度传感器**：分类显示（CPU 各核心 / PCH 芯片组 / 主板 / ACPI），CPU 温度带 max/crit 上限，中文标注
- **风扇转速**：通过 `sensors -j` 读取转速 + sysfs `pwmN` 读取 PWM 占空比，0 RPM 停转风扇标红显示。控制模式从 sysfs `pwmN_enable` 读取（0=关闭 / 1=手动控制 / 2=自动温控）
- **电压**：+3.3V / 3VSB 待机 / CMOS 电池等，中文标注
- 自动刷新开关（30 秒间隔，状态持久化到 localStorage）

### 🗄️ 存储卷
- mdadm RAID 阵列状态（级别 / 成员盘 / 容量）
- 挂载点容量（总大小 / 已用 / 可用 / 使用率 / 文件系统类型）
- 自动过滤 docker overlay / tmpfs 等非存储卷

### 🐳 Docker
- 容器运行状态概览（运行中 / 总数）
- 容器列表：状态 / 名称·镜像 / 占用内存 / **端口映射（自动探测，支持 host 与 bridge 模式）/ 运行时长（中文）**
- 已停止容器标注「容器停止不检测端口」

## 依赖

| 工具 | 用途 | 必需 | 自动安装 |
|------|------|------|----------|
| Python 3 + Flask | Web 框架 | ✅ | ✅ pip/apt |
| smartctl (smartmontools) | 硬盘 SMART | ✅ | ✅ apt |
| sensors (lm-sensors) | 温度/风扇/电压 | ✅ | ✅ apt |
| mdadm | RAID 阵列 | ✅ | ✅ apt |
| storcli | LSI MegaRAID 阵列卡信息 | ❌ | ❌ 需手动 |
| lspci (pciutils) | 显卡 / 阵列卡识别 | ✅ | 系统自带 |
| ip (iproute2) | 网卡 | ✅ | 系统自带 |

**storcli** 不在标准软件源中，LSI 阵列卡（MegaRAID IR/RAID 模式与 HBA IT 模式）用户都需手动从 [Broadcom 官网](https://www.broadcom.com/support/download-search) 下载安装：MegaRAID 卡走 `storcli /c0 show` 读取完整信息（含 ROC 芯片温度），HBA 卡额外走 `storcli /c0 show temperature` 读取芯片温度（HBA 卡的 `/c0 show` 不含温度字段，这是正常现象，并非面板 bug）。纯 SATA 主板（无 LSI 卡）用户可忽略此工具。

## 数据来源

| 数据 | 命令 |
|------|------|
| 阵列卡 | `lspci` 检测卡类型；MegaRAID 卡 `storcli /c0 show`，HBA 卡额外 `storcli /c0 show temperature` 取芯片温度 |
| 硬盘 SMART / 转速 | `smartctl -a/-i /dev/sdX` |
| 温度/风扇/电压 | `sensors -j`（JSON 输出分类解析） |
| 风扇转速 | `sensors -j` + sysfs `pwmN_enable` / `pwmN` |
| 显卡 / 阵列卡 | `lspci` |
| 网卡 | `ip -o link/addr` + `/sys/class/net/` |
| RAID 阵列 | `cat /proc/mdstat` |
| 挂载点 | `df -h` |
| CPU | `lscpu` |
| 内存 | `cat /proc/meminfo` |

## API

- `GET /` — 面板页面
- `GET /api/all` — 全部数据 JSON（硬件配置检测 + 阵列卡 + 硬盘 + 系统 + 存储 + Docker）

## 技术栈

- Python 3 + Flask（单文件应用）
- storcli / smartctl / sensors / lspci / ip / mdadm 系统命令采集
- 内联 HTML/CSS/JS 前端，无构建步骤
- 标准 fnOS FPK 应用包格式

## 注意事项

- 服务端口 9800（飞牛OS 应用框架分配），修改需同步更新 manifest 的 service_port
- smartctl / storcli 需要 sudo 权限（飞牛OS 应用框架默认提供）
- 风扇 PWM 模式显示依赖 it87 / nct6775 等主板传感器驱动
- 双磁臂（双执行器）硬盘：LSI 阵列卡会将其每个执行器作为独立盘暴露给系统（如各 7T），面板在阵列卡页显示整盘标称容量（如 14T）并标注每执行器容量

## 更新日志

### v1.6.8
- 应用介绍页「开发者 / 发布者」改为可点击，点击直接跳转到 GitHub 主页（github.com/han951meng），与项目仓库同名

### v1.6.7
- 飞牛桌面窗口型集成：点飞牛桌面图标直接在应用窗口内渲染（系统端口服务模式），不再跳转外部浏览器，体验与系统应用一致

### v1.6.5
- 风扇控制大改：后端常驻缓变线程，调转速改为「设目标 + 平滑过渡」，不再瞬间拉满导致突然全速；新增轻量 `/api/fan/status` 接口，风扇面板独立 2 秒实时轮询，拖动后能实时看到转速数字慢慢下降；恢复自动时若已有系统风扇服务在运行则优先交还，否则由 nasdash 自带保守温控曲线接管。

### v1.6.4
- HBA 直通卡页面说明优化：明确告知 HBA 芯片无独立温度传感器（不显示卡温属正常现象，并非面板 bug）；并在概览页 HBA 卡片新增「温度：卡无传感器 · 见硬盘 SMART」提示，避免用户误以为面板异常

### v1.6.3
- 修复 SAS9300-8e 等卡（或 expander 下）物理盘品牌/型号显示为“-”的问题：当 `storcli /c0 show` 的 model 列不可靠时，自动从 `/c0/e{e}/s{s} show all` 的 Model Number / Inquiry Data 兜底取型号。

### v1.6.2
- 阵列卡温度兼容性修复：storcli 路径改为动态探测（兼容只装 `storcli64` 的环境，如部分 HBA 9300-8E 用户），不再写死 `/usr/local/bin/storcli`
- 特权执行改为 root 直接运行 + sudo 兜底，不再因免密 sudo 未配置而静默失败（MegaRAID 与 HBA 卡芯片温度均可正常读取）
- 启动自愈：`cmd/main` 的 start 自动重建 `/app/app.py` 软链（飞牛 uninstall 会删该软链而 install 不重建，不补则 start 必报 10500），重装无需手动干预
- 命令执行失败记录 stderr 到 debug.log，便于排查；安装回调新增 `storcli64→storcli` 软链双保险
- UI 版本号与主板芯片组显示：副标题版本号改为从 manifest 动态读取；主板检测新增芯片组显示（DMI 为空时回退 lspci 识别，如 Intel 300 系列）

### v1.6.0
- 主板检测优先读取 `/sys/class/dmi/id`（免 root）；DMI 为空时支持网页手动标注主板型号（如豆希 WB360，写入 `board_override.txt`）并回退 lspci 芯片组识别
- 新增 BIOS 厂商 / 版本 / 日期显示
- 内存品牌改为优先 `decode-dimms` 直读 SPD（区分模组厂与颗粒厂 DRAM，如影驰模组 + 镁光颗粒），识别不出时回退 dmidecode 厂商码映射
- 安装依赖新增 dmidecode 与 i2c-tools

### v1.5.0
- 结构调整：Docker 独立为左侧边栏模块；硬件配置检测移除重复的存储卷表格；显卡移至处理器卡片旁；系统信息 / 网卡移至硬件配置检测
- 硬盘 SMART 页机械盘新增「转速」（真实 RPM，SSD 显示固态）
- 修复 LSI-9300 等 HBA 卡芯片温度无法获取（改用 `storcli /c0 show temperature`）
- 追加主板与内存条品牌型号采集（dmidecode）、风扇网页手动调速与恢复自动控温
