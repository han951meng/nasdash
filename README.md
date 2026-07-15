# nasdash

**当前版本：v1.7.7** · [下载最新 fpk](https://github.com/han951meng/nasdash/releases/latest)

飞牛OS（fnOS）NAS 硬件监控面板 —— FPK 应用包

一个轻量级单文件 Flask Web 应用，通过 `storcli` / `smartctl` / `sensors` / `mdadm` / `lspci` / `ip` 等系统命令采集 NAS 硬件状态，以网页面板展示，免去每次 SSH 敲命令的麻烦。

## 安装

1. 下载 `nasdash.fpk`
2. 在飞牛OS 应用中心上传安装
3. 安装时自动安装依赖：smartmontools / lm-sensors / mdadm / Flask（storcli 可选，非 LSI 阵列卡可忽略）
4. 安装后浏览器访问 `http://NAS_IP:9800`

> 旧版本手动安装过依赖的用户：直接覆盖安装即可，install_callback 会跳过已安装的工具。

## 功能

左侧边栏共七个模块，覆盖 NAS 硬件运维核心场景：

### 🏠 硬件配置检测（首页仪表盘）
单页总览，进入即看全貌：
- 顶部状态栏：实时概览（阵列卡 / 硬盘 / Docker / 系统负载）
- 系统配置：处理器（含型号、核心/线程）、显卡（有则显示）、内存（品牌取自 SPD 直读，区分模组厂与颗粒厂）、主板型号（DMI 为空时支持网页手动标注，如豆希 WB360）、系统信息（含运行时间）、网络（网卡 IP / 速率 / 状态）
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

### 🌀 风扇控制（独立模块）
- 预设档位（默认 / 全速 / 10% … 90%）一键调速，卡片实时显示 RPM 与占空比
- 每个风扇可网页自定义名称（如「CPU 风扇」「机箱风扇」）与供电电压标注（12V / 5V / 未知），按安装实例持久化，硬件无关不写死机型
- 两套独立温控曲线，互不干扰、可分别接管不同风扇：
  - **硬盘温控**（disk_temp）：勾选监控硬盘，设开转/全速温度与占空比，按最热盘温度平滑调速；监控盘全部休眠时停转风扇；可指定只控部分风扇（如机箱风扇 / SYS_FAN2）
  - **主板/CPU 温控**（sys_temp）：按 CPU 封装温度或主板温度调速，与硬盘温控对称配置
  - 两套均带「恢复自动温度」滞回：温度降到该值以下时受控风扇交还主板/内核自动控速，升回开转温度才再次接管，避免临界抖动；全速档即真正满转
- 手动调速下限放宽至 10%；恢复自动优先交还系统风扇服务，否则走 nasdash 自带保守温控曲线（按 CPU 温度、钳制 30~70%）

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

### v1.7.7
- 风扇温控曲线拆分为「两套」对称逻辑：原有「硬盘温控」(disk_temp) 按指定硬盘温度调速；新增「主板/CPU 温控」(sys_temp) 按 CPU 封装温度或主板温度调速
- 两套各自独立配置（开转/全速/恢复自动温度 + 占空比 + 受控风扇），互不干扰，可分别接管不同风扇（如 CPU 风扇交给 sys_temp、机箱风扇交给 disk_temp）
- 均带滞回「恢复自动」（降到恢复自动温度以下交还主板/内核自动控速）与真正满转

### v1.7.6
- 硬盘温控新增「恢复自动温度」滞回控制：盘温降到该温度以下时，受控风扇交还主板/内核自动控速（nasdash 不再接管）；盘温重新升至开转温度才再次接管，中间滞回区避免临界抖动
- 全速温度档（默认 100%）即真正满转；可指定只控部分风扇（如机箱风扇/SYS_FAN2），不接管 CPU 风扇

### v1.7.5
- 修复 NVMe 硬盘 SMART「已用时长」被千分位逗号截断的 Bug：如 smartctl 输出 `Power On Hours: 7,442` 原只读到 `7 小时`，现正确解析并去掉逗号，显示 `7,442 小时`
- 同步修正 ATA 机械盘 `power_on_hours` 潜在的同类问题（防御性去掉千分位逗号）
- 修复 NVMe / ATA 盘型号展示重复：不再把 model 的第一个词重复当 `vendor` 前缀显示，避免「WDC WDC PC SN730」这类重复

### v1.7.4
- 修复风扇「恢复自动」在未安装 FanControlServer 的机器上误判服务在跑、把风扇错误交还主板原生自动控速（SYS_FAN2 等口默认曲线偏激进会狂转）的 Bug：FCS 检测改用 `[f]ancontrolserver` 括号技巧避免 `pgrep` 自匹配假阳性；无 FCS 时「恢复自动」改走 nasdash 自带保守温控曲线（按 CPU 温度、钳制 30~70%）

### v1.7.3
- 修复 NVMe 系统盘在「硬盘 SMART」页显示「未检测到硬盘」：`get_disks()` 原只扫描 `/dev/sd?`，现增量支持 NVMe 命名空间（`/dev/nvme?n?`），正确读取型号/序列号/温度/已用寿命/剩余备用等
- NVMe 盘现在也能在风扇「硬盘温控」面板中勾选参与控温（此前因同样原因勾选不到）

### v1.7.2
- 硬盘温控支持「指定受控风扇」：在风扇控制页的硬盘温控卡片中勾选要接管的风扇（如仅机箱风扇 / SYS_FAN2，不接管 CPU 风扇），未勾选的风扇保持原自动/手动模式
- 优化硬盘温控界面：分区卡片、网格勾选、实时温度与目标占空比更直观

### v1.7.1
- 新增「硬盘温度控制风扇」模式：在风扇控制页勾选监控硬盘，设开转温度/全速温度/占空比，按最热盘温度平滑调速；监控盘全部休眠时自动停转风扇
- 修复标准安装后版本号误报为 v1.6.2 的 Bug：飞牛标准安装时 manifest 在 `/var/apps/<包名>/manifest`，`_app_version()` 改为优先读取该路径

### v1.7.0
- 风扇可编辑标注：每个风扇可在网页自定义名称（如「CPU 风扇」「机箱风扇」）并标注供电电压（12V/5V/未知），按安装实例持久化，硬件无关不写死机型

### v1.6.9
- 修复硬盘识别问题：个别 SATA 机械盘被误识别为 SSD 且容量显示 0G（如西数 WD20EZRZ 2TB）。lsblk 不可靠时改为从 smartctl 的 User Capacity 与 Rotation Rate 兜底取值，容量与 HDD/SSD 类型均更准确

### v1.6.8
- 新增「检测新版本」功能：面板自动检测 GitHub 最新 Release（带 6 小时缓存与 5 秒超时，不卡页面），发现新版本时顶部弹出更新横幅并附下载链接；右上角新增「检查更新」按钮可手动刷新

### v1.6.7
- 飞牛桌面窗口型集成：点飞牛桌面图标直接在应用窗口内渲染（系统端口服务模式），不再跳转外部浏览器，体验与系统应用一致

