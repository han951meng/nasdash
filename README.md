# nasdash

飞牛OS（fnOS）NAS 硬件监控面板 —— FPK 应用包

一个轻量级单文件 Flask Web 应用，通过 storcli / smartctl / sensors / mdadm 等系统命令采集 NAS 硬件状态，以网页面板展示，免去每次 SSH 敲命令的麻烦。

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
- **风扇转速**：通过 `sensors -j` 读取转速 + sysfs `pwmN` 读取占空比，0 RPM 停转风扇标红显示。控制模式优先从 FanControlServer 配置读取（曲线温控等），未安装时自动回退到 sysfs `pwmN_enable`（0=关闭 / 1=手动控制 / 2=自动温控）
- **电压**：+3.3V / 3VSB 待机 / CMOS 电池等，中文标注
- **显卡**：lspci 解析 VGA/3D/Display 控制器
- **网卡**：物理网卡和 bond 接口，显示 IP / 速率 / 状态 / MAC，自动过滤 docker/虚拟网桥
- 自动刷新开关（30 秒间隔，状态持久化到 localStorage）

### 🗄️ 存储卷
- mdadm RAID 阵列状态（级别 / 成员盘 / 容量）
- 挂载点容量（总大小 / 已用 / 可用 / 使用率 / 文件系统类型）
- 自动过滤 docker overlay / tmpfs 等非存储卷

## 安装方法

1. 下载 `nasdash.fpk`
2. 在飞牛OS应用中心上传安装
3. 或通过 SSH：`appcenter-cli install-fpk /path/to/nasdash.fpk`
4. 安装后浏览器访问 `http://NAS_IP:9800`

## 技术栈

- Python 3 + Flask（单文件应用，仅需 Flask 依赖）
- storcli / smartctl / sensors / lspci / ip / mdadm 系统命令采集
- 内联 HTML/CSS/JS 前端，无构建步骤
- 标准 fnOS FPK 应用包格式

## 数据来源

| 数据 | 命令 |
|------|------|
| 阵列卡 | `storcli /c0 show` |
| 硬盘 SMART | `smartctl -a /dev/sdX` |
| 温度/风扇/电压 | `sensors -j`（JSON 输出分类解析） |
| 风扇转速 | `sensors -j` + sysfs `pwmN_enable` / `pwmN` |
| 风扇名称/模式（可选） | FanControlServer 配置（未安装时用 sysfs 模式兜底） |
| 显卡 | `lspci` |
| 网卡 | `ip -o link/addr` + `/sys/class/net/` |
| RAID 阵列 | `cat /proc/mdstat` |
| 挂载点 | `df -h` |
| CPU | `lscpu` |
| 内存 | `cat /proc/meminfo` |

## API

- `GET /` — 面板页面
- `GET /api/all` — 全部数据 JSON（阵列卡 + 硬盘 + 系统 + 存储）

## 注意事项

- 需要 storcli 安装在 `/usr/local/bin/storcli`
- smartctl 需要 sudo 免密权限（飞牛OS 应用框架默认提供）
- FanControlServer 为可选依赖——未安装时风扇转速正常显示，仅缺少名称和模式标签
- 端口 9800（飞牛OS 应用框架分配）
