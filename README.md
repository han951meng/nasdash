# nasdash

飞牛OS（fnOS）NAS 硬件监控面板 —— FPK 应用包

一个轻量级单文件 Flask Web 应用，通过 storcli / smartctl / sensors / mdadm 等系统命令采集 NAS 硬件状态，以网页面板展示，免去每次 SSH 敲命令的麻烦。

## 安装

1. 下载 `nasdash.fpk`
2. 在飞牛OS应用中心上传安装
3. 安装时自动安装依赖：smartmontools / lm-sensors / mdadm / Flask（storcli 可选，非 LSI 阵列卡可忽略）
4. 安装后浏览器访问 `http://NAS_IP:9800`

> 旧版本手动安装过依赖的用户：直接覆盖安装即可，install_callback 会跳过已安装的工具。

## 功能

四个标签页，覆盖 NAS 硬件运维核心场景：

### 💾 阵列卡
- 型号 / 序列号 / SAS 地址 / 固件版本 / BIOS 版本 / 驱动版本 / PCI 地址
- CacheVault 缓存电池状态
- JBOD 物理盘列表（槽位 / DID / 状态 / 容量 / 接口 / 型号）

### 💿 硬盘 SMART
- 同时支持 SAS/SCSI 盘和 SATA/ATA 盘
- 健康状态（OK/PASSED 标绿，异常标红）
- 温度 / 通电时长 / 坏块计数 / 错误计数
- SATA 盘额外显示 Reallocated / Pending / Uncorrectable / UDMA CRC
- 容量从 smartctl 读取（兼容阵列卡后 lsblk 返回 0 的情况）

### 📊 系统资源
- CPU 型号 / 核心数 / 线程数 / 频率 / 负载
- 内存 / Swap 使用率
- 运行时长
- **温度传感器**：分类显示（CPU 各核心 / PCH 芯片组 / 主板 / ACPI），CPU 温度带 max/crit 上限，中文标注
- **风扇转速**：通过 `sensors -j` 读取转速 + sysfs `pwmN` 读取 PWM 占空比，0 RPM 停转风扇标红显示。控制模式从 sysfs `pwmN_enable` 读取（0=关闭 / 1=手动控制 / 2=自动温控）
- **电压**：+3.3V / 3VSB 待机 / CMOS 电池等，中文标注
- **显卡**：lspci 解析 VGA/3D/Display 控制器
- **网卡**：物理网卡和 bond 接口，显示 IP / 速率 / 状态 / MAC，自动过滤 docker/虚拟网桥
- 自动刷新开关（30 秒间隔，状态持久化到 localStorage）

### 🗄️ 存储卷
- mdadm RAID 阵列状态（级别 / 成员盘 / 容量）
- 挂载点容量（总大小 / 已用 / 可用 / 使用率 / 文件系统类型）
- 自动过滤 docker overlay / tmpfs 等非存储卷

## 依赖

| 工具 | 用途 | 必需 | 自动安装 |
|------|------|------|----------|
| Python 3 + Flask | Web 框架 | ✅ | ✅ pip/apt |
| smartctl (smartmontools) | 硬盘 SMART | ✅ | ✅ apt |
| sensors (lm-sensors) | 温度/风扇/电压 | ✅ | ✅ apt |
| mdadm | RAID 阵列 | ✅ | ✅ apt |
| storcli | LSI 阵列卡信息 | ❌ | ❌ 需手动 |
| lspci (pciutils) | 显卡 | ✅ | 系统自带 |
| ip (iproute2) | 网卡 | ✅ | 系统自带 |

**storcli** 不在标准软件源中，LSI/MegaRAID 阵列卡用户需手动从 [Broadcom 官网](https://www.broadcom.com/support/download-search) 下载安装。其他类型阵列卡用户可忽略此工具，对应面板会显示"未检测到"。

## 数据来源

| 数据 | 命令 |
|------|------|
| 阵列卡 | `storcli /c0 show` |
| 硬盘 SMART | `smartctl -a /dev/sdX` |
| 温度/风扇/电压 | `sensors -j`（JSON 输出分类解析） |
| 风扇转速 | `sensors -j` + sysfs `pwmN_enable` / `pwmN` |
| 显卡 | `lspci` |
| 网卡 | `ip -o link/addr` + `/sys/class/net/` |
| RAID 阵列 | `cat /proc/mdstat` |
| 挂载点 | `df -h` |
| CPU | `lscpu` |
| 内存 | `cat /proc/meminfo` |

## API

- `GET /` — 面板页面
- `GET /api/all` — 全部数据 JSON（阵列卡 + 硬盘 + 系统 + 存储）

## 技术栈

- Python 3 + Flask（单文件应用）
- storcli / smartctl / sensors / lspci / ip / mdadm 系统命令采集
- 内联 HTML/CSS/JS 前端，无构建步骤
- 标准 fnOS FPK 应用包格式

## 注意事项

- 服务端口 9800（飞牛OS 应用框架分配），修改需同步更新 manifest 的 service_port
- smartctl 需要 sudo 权限（飞牛OS 应用框架默认提供）
- 风扇 PWM 模式显示依赖 it87 / nct6775 等主板传感器驱动
