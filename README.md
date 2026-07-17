# nasdash

**当前版本：v1.7.13** · [下载最新 fpk](https://github.com/han951meng/nasdash/releases/latest)

飞牛OS（fnOS）NAS 硬件监控面板 —— FPK 应用包

一个轻量级单文件 Flask Web 应用，通过 `storcli` / `smartctl` / `sensors` / `mdadm` / `lspci` / `ip` 等系统命令采集 NAS 硬件状态，以网页面板展示，免去每次 SSH 敲命令的麻烦。

## 安装

1. 下载 `nasdash.fpk`
2. 在飞牛OS 应用中心上传安装
3. 安装时自动安装依赖：smartmontools / lm-sensors / mdadm / Flask / dmidecode / i2c-tools；**storcli 现已随包内置**，LSI 阵列卡用户无需手动下载
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
| storcli | LSI MegaRAID 阵列卡信息 | ❌ | ✅ 随包内置 |
| lspci (pciutils) | 显卡 / 阵列卡识别 | ✅ | 系统自带 |
| ip (iproute2) | 网卡 | ✅ | 系统自带 |

**storcli 现已随包内置**：LSI MegaRAID 阵列卡（IR/RAID 模式）走 `storcli /c0 show` 读取完整信息（含 ROC 芯片温度），HBA 直通卡（IT 模式）额外走 `storcli /c0 show temperature` 读取芯片温度（HBA 卡的 `/c0 show` 不含温度字段，这是正常现象，并非面板 bug）。安装时由 install_callback 自动落地到 /opt/MegaRAID/storcli 并建软链，无需再从 Broadcom 官网手动下载。纯 SATA 主板（无 LSI 卡）用户可忽略此工具。

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

## 实测硬件配置（避坑说明）

本应用目前主要在以下硬件上完成实测验证。飞牛OS / 不同主板 / 阵列卡组合下，风扇接口命名与内核 `hwmonN` 编号可能不同，**以下现象属正常，并非面板 bug**：

### 测试机配置

| 组件 | 配置 |
|------|------|
| 系统 | 飞牛OS（fnOS，基于 Debian 12），应用端口 9800 |
| 主板 | MSI B360M MORTAR（Intel B360 芯片组） |
| CPU | Intel 奔腾 G5400（风冷，散热器接 CPU_FAN1） |
| 阵列卡 | LSI MegaRAID 9271-8i（2× ST14000NM SAS 14T，JBOD 模式），storcli 随包内置 |
| 传感器芯片 | Nuvoton NCT6797D（nct6775 驱动），本机**唯一真实风扇控制器**，暴露 5 个 PWM 通道（pwm1–pwm5）；无 it87 芯片，「it8772 风扇」字样来自旧 FanControlServer 配置残留 |

### 风扇接口接线（已逐一拉满转速肉眼核对）

| 主板接口 | 接的设备 | 测速 | 说明 |
|----------|----------|------|------|
| CPU_FAN1 | CPU 散热器风扇 | ✅ 有 | — |
| SYS_FAN1 | 主板/芯片组散热风扇 | ✅ 有 | — |
| SYS_FAN2 | 阵列卡风扇 | ❌ 无 | 缺测速线，面板显示「无转速信号」，但能正常调速 |
| SYS_FAN3 | 硬盘散热风扇 | ✅ 有 | 一分二转接两把 PWM 风扇 |
| PUMP_FAN1 | 空口 | — | 风冷机未接水冷泵，无风扇，面板可一键隐藏 |

### 常见「看起来像 bug」的正常现象

| 现象 | 说明 |
|------|------|
| 风扇显示「无转速信号」 | 该通道接的风扇没接测速线（3 针/缺 tach），或是空接口（如 PUMP_FAN1）。面板仍能调速，只是读不到 RPM 数字，可点「隐藏」收起。 |
| hwmon 编号跨重启变化 | 内核给风扇控制器分配的 `hwmonN` 路径（如 nct6797 在 hwmon3↔hwmon4 间漂移）不固定。面板已自动校正——调速请求按通道序号(idx)命中真实通道，不受 hwmon 编号变化影响；自定义名称/隐藏也按 idx 兜底，重启后不乱。 |
| 名字显示成「it8772 风扇 N」 | 读取到机器上旧 FanControlServer 配置残留标签。可在风扇卡片「自定义名称」改成你喜欢的（如 CPU 风扇），按安装实例持久化，硬件无关不写死机型。 |
| 手动调速后停在目标值、不再回弹 | 设计行为——拖动滑块即切换为手动控制并平滑过渡到目标占空比，转速数字继续爬升；点「恢复自动」才交还主板/内核自动控温（nasdash 自带保守曲线钳制 30~70%）。 |
| 滑块最低 10% | 手动调速下限为 10%（该硬件 10% 仍可运转），非 0%。 |

> 本应用仅在以上硬件完成完整实测；其他主板/阵列卡组合功能可能不同，欢迎在 Issues 反馈。

## 更新日志

### v1.7.13
- 统一网关鉴权：访问前先经飞牛登录态校验，关闭裸端口 9800；storcli 改用 usr-local-linker 自动软链，安装更稳


### v1.7.12
- 风扇控制全面修复与加固：拖滑块不再无效/回弹自动曲线35%——GUI滑块hwmon与后端控制路径按实时枚举为权威并双保险校正，调速请求精准命中真实通道(CPU_FAN1/SYS_FAN1等)；滑块拖动不被后台刷新打断、手动调速停在目标值且转速数字继续爬升；无转速回读的空通道(一分二分线器副扇/主板未布线该通道)标注「无转速信号」并可一键隐藏/恢复；风扇自定义名称/电压标注改为写入飞牛持久目录(TRIM_PKGVAR)，重装不再清空；README新增实测硬件配置避坑说明


### v1.7.11
- 全模块自动检测加固：风扇枚举移除芯片白名单并支持集线器多通道(fan6+)；CPU/主板温度源兼容非coretemp主板(AMD/华硕等)；磁盘枚举支持多字母盘名(sdaa/sdab)；阵列卡检测移除厂商白名单(任意品牌自动识别)；回归测试扩至29项


### v1.7.10
- 修复风扇温控空转(Bug A)：sys_temp/disk_temp 设 controlled_fans:all 时启动即接管全部风扇，不再因 FAN_TARGETS 为空而失效；主采集 smartctl 加 -n standby 避免每次轮询唤醒休眠 SAS 盘(Bug B)；新增风扇枚举/温控选择回归测试
