# 飞牛官方 vs nasdash 能力对比报告

> 版本基线：nasdash **v2.0.2**（2026-08-14 发布，当前 158 实跑版本）  
> 对比对象：飞牛 fnOS 原生监控能力（公开监控 API 面）  
> 生成日期：2026-08-14

---

## 一、为什么要做这个对比 / 铁律

用户给定两条铁律，本报告严格遵守：

1. **只对比，不动文档**：本报告仅供对照。只有验证通过、且经用户确认后，才允许修改我们的开发文档（如 `docs/使用手册.md`、`fnos-fpk-dev.md`）。
2. **我们这一侧必须"验证过"**：所有 nasdash 字段均来自 **158 真机实跑的 `/api/system` 实时返回** + 已部署 v2.0.2 的 `app.py` 源码，不凭空列举。

---

## 二、验证方法（两边都取自真机，非想象）

| 一侧 | 取证方式 | 说明 |
|------|----------|------|
| 飞牛官方 | `trim-cli monitor cpu/memory/disk/gpu/net/sys-fan`（连 192.168.50.158 真机） | 飞牛**公开监控 API** 实测返回。这就是"飞牛原生监控能给你的东西"，也是它给第三方应用/网关暴露的监控面。 |
| nasdash | SSH 进 158 → `curl --unix-socket .../app.sock http://localhost/api/system` | 直接打部署在 158 上的 v2.0.2 实例，取真实返回；字段与 `app.py:get_system()` 源码逐一对齐。 |

> ⚠️ 公平说明：飞牛的**网页 UI**（存储页、资源页）能展示的东西比它的"公开监控 API"多（比如 UI 里能看 SMART、RAID）。但本对比只比**监控 API / 应用可拿到的数据面**，也就是 `trim-cli monitor` 实测的那一层——这是和 nasdash 监控 API 同口径的苹果对苹果对比。

---

## 三、核心架构差异（先讲透原理）

这是理解后面所有差异的钥匙：

- **飞牛原生监控**：走 fnOS 自己的 `resmon` 网关，字段是**飞牛官方挑过、限过**的一小撮（CPU/内存/磁盘IO/显卡/网口流量/风扇开关状态）。它**刻意不开放**硬件底层细节给公开 API。
- **nasdash**：是飞牛上的**第三方 fpk 应用**，拥有系统级权限，直接跑 `sensors` / `lspci` / `smartctl` / `storcli` / 读 `sysfs` **自己采集硬件**。飞牛那套"开放 API（文件授权/页面路由/主题/查询授权）"里**根本不含任何硬件监控接口**，所以 nasdash 完全不依赖它，自己读底层。

一句话：**飞牛给你"挑好的摘要"，nasdash 自己"扒开机器读原始数据"**。所以 nasdash 在硬件细节上普遍比飞牛公开监控 API 深得多；但飞牛在"实时磁盘 IO 吞吐"这种它专门做了的摘要项上反而有我们没有的。

---

## 四、逐维度对比总表

图例：**✅ 我们覆盖且更深** ｜ **⚠️ 双方都有/基本对齐** ｜ **🔶 飞牛有、我们暂无** ｜ **🔷 我们独有**

| # | 维度 | 飞牛官方（monitor 实测） | nasdash v2.0.2（实跑 API） | 结论 |
|---|------|--------------------------|----------------------------|------|
| 1 | CPU 使用率 | `busy{all/user/system/iowait/other}` | `cpu_usage`（实时） | ⚠️ 对齐，我们都给 |
| 2 | CPU 型号/拓扑/微架构 | `name` `num`(插槽) `core` `thread` `maxFreq` | `cpu_info`：arch / vendor / family / model / stepping / sockets / cores_per_socket / threads_per_core / NUMA节点 / 地址位宽 / min·max·current 频率 / bogomips / **L1d·L1i·L2·L3 缓存** / 虚拟化 / **指令集 lm·ht·aes·avx·avx2·avx512** | ✅ 我们远超 |
| 3 | 系统负载 | `loadavg{1,5,15}` | `load[1,5,15]` | ⚠️ 对齐 |
| 4 | 运行时间 | 无 | `uptime` | 🔷 我们独有 |
| 5 | 内存用量 | `mem{total/used/available/free/buffers/cached/reserved}` `swap{free/total/used}` | `memory{total/used/available/percent}` `swap{total/used}` | ⚠️ 飞牛多 `buffers/cached/reserved` 细分；我们多下面第6/7项硬件清单 |
| 6 | 内存条硬件清单 | 无 | `memory_modules`：品牌 / 型号 / 插槽占用 / 总容量 | 🔷 我们独有 |
| 7 | 主板信息 | 无 | `board`：厂商 / 型号 / 版本 / BIOS / **芯片组** | 🔷 我们独有 |
| 8 | 温度（板级） | `cpu temp[]`（仅 CPU） | `sensors.temps`：主板 SYSTIN / CPUTIN / **PECI Agent 0** / PCH 芯片组 / ACPI 等 + `cpu_temp` + 每盘 temp + GPU temp + 阵列卡 ROC temp | ✅ 我们远超（全板级） |
| 9 | 电压 | 无 | `sensors.voltages`：in0–in8 / +3.3V / 3VSB / CMOS 电池 | 🔷 我们独有 |
| 10 | 风扇（监控+控速） | `sys-fan` 仅 `isTrim:false`（**公开监控 API 不暴露任何风扇数据/转速/控速**） | `sensors.fans`：名称 / **转速 rpm** / 是否停转 / **模式 PWM-DC** / 占空比% / 是否可控 / hwmon·idx / 规则来源 / 目标占空比 / 是否曲线 / 手动激活 —— 且**可写 PWM 控速** | 🔷 我们独有（监控+控制） |
| 11 | 显卡 | `gpu{busy/device/deviceId/vendor/vendorId/ram{free/total/used}/engine{render/video}/index}` | `gpus`：核显/独显类型 / 名称 / 架构代号 / 厂商 / dev / **PCI地址** / **驱动+版本** / **PCIe 协商代宽 gen·width** / **显存容量·类型·位宽** / **核心·显存频率** / **温度** / **功耗·功耗上限** | ✅ 我们远超 |
| 12 | 网卡 | `ifs[]{bond/ifType/index/name/receive/transmit}`（仅流量计数） | `nics`：名称 / 状态 / **MAC** / 速率 / **IP** / **IPv6** / **实时收/发速率** / **MTU** / **双工** / **驱动** / **总线** / **厂商型号** | ✅ 我们远超 |
| 13 | 磁盘 IO 实时吞吐 | `disk[]{busy/read/write/standby/temp}`（实时 IO） | `/api/metrics` 的 `diskio[]`：**实时 `read_rate`/`write_rate`（字节/秒，2s 采样自 /proc/diskstats 扇区差×512）** + 型号/容量/品牌；`/api/history` 存 30 天 disk_read/disk_write 趋势；temp 在 `get_disks()`（SMART），standby 在调速线程另算 | ✅ 我们也有（给的是真实字节吞吐而非 busy%；缺 busy% 占用率与 standby 标志，见第五节） |
| 14 | 磁盘健康 / SMART | 监控 API 无（UI 另算） | `get_disks()`：health / temp / power_on_hours / type(ata/sas/nvme) / brand / feature / rpm / 是否独立盘 | 🔷 我们独有（在监控 API 内） |
| 15 | 阵列卡 / RAID 控制器 | 无 | `get_raid_card()`：MegaRAID 型号 / 固件 / 序列号 / SAS地址 / **ROC 温度** / CacheVault / 每盘 slot·sn·rpm·品牌 + **双磁臂智能合并** | 🔷 我们独有 |
| 16 | 存储卷拓扑 | 无 | `get_storage()`：mdadm 阵列 / 卷 / lsblk 拓扑 | 🔷 我们独有 |
| 17 | 明暗主题 | 飞牛开放 API 有（语言/主题状态） | nasdash 跟随飞牛主题（浅色/深色/跟随系统） | ⚠️ 对齐（我们借飞牛机制） |
| 18 | 应用开发接口 | 开放 API：文件授权 / 页面路由 / 界面状态 / 后端查询授权 | nasdash 不依赖开放 API，直接读硬件 | — 不同维度（非监控） |

---

## 五、我们这侧的"真短板"（诚实列出）

按铁律二"验证过的才算"，下面两项是**实打实我们当前缺/偏弱**的，不是谦虚（注意：实时磁盘吞吐本身我们已有，详见第 13 行更正）：

1. **磁盘 `busy` 占用率百分比 + `standby` 标志**  
   飞牛 monitor 的 `disk[]` 给每块盘的**设备忙占用率（busy%）**和**待机状态（standby）**；nasdash 的 `diskio[]` 给的是**真实字节速率（read_rate/write_rate）**而非 busy%——两者是同一件事的两种表达（一个看"占用比例"，一个看"实际流量"），我们偏"实际吞吐"。standby 状态我们在调速线程里用 smartctl/diskstats 检测但没放进 diskio 字段。  
   → 若要和飞牛逐字段对齐，补一个 `busy`（由 read_rate+write_rate 估算或读 /proc/diskstats 的 io_ticks 算）和 `standby` 标志即可，工作量小。

2. **内存 buffers/cached/reserved 细分**  
   飞牛 `mem` 把 buffers/cached/reserved 单独列了；我们合并成 used/available + 百分比。影响很小，纯展示精度问题。

其余维度（CPU 微架构、显卡全参数、网卡硬件清单、风扇控速、主板/内存条、阵列卡、温度电压板级、**实时磁盘字节吞吐**）**都是我们更深或独有**。

---

## 六、结论

- **nasdash 在"硬件可见深度"上全面领先飞牛公开监控 API**：板级温度/电压、风扇控速、显卡全参数、网卡硬件清单、阵列卡、内存条/主板清单、**实时磁盘字节吞吐**，全是飞牛监控 API 不暴露、nasdash 自己扒出来的。
- **飞牛仅在"磁盘 busy% 占用率 + standby 标志"这两个细分字段上略多**（它给的是占用比例，我们给的是真实字节速率 read_rate/write_rate，是同一件事的两种表达）；内存 buffers/cached/reserved 细分也是它多列了。这些是展示精度层面的小差异，不是能力缺口。
- 两者定位不同：飞牛是"官方挑好的摘要面"，nasdash 是"第三方应用自己读底层"。所以对比不是"谁抄谁"，而是**能力面天然不同的两层**。

---

## 七、下一步建议（⚠️ 未执行，需你确认后才动文档）

以下是我**建议**后续可做的，但严格遵守铁律一——**现在一个字都没改**：

1. 把本对比的结论（尤其"我们领先/独有"清单）择要写进 `docs/使用手册.md` 的"能力说明"区，让用户在官网/手册一眼看到 nasdash 比系统监控多看了什么。
2. 在 `fnos-fpk-dev.md` 增补一条"飞牛公开监控 API 不含硬件监控接口"的事实记录（避免后人误以为要走飞牛 API）。
3. **（可选优化，非缺口）补全磁盘 `busy`% 与 `standby` 字段**：nasdash 已有实时 read_rate/write_rate 字节吞吐，若要和飞牛逐字段对齐，可在 diskio 里增 `busy`（读 /proc/diskstats 的 io_ticks 算占用率）和 `standby` 标志。工作量小，属精度对齐而非能力补齐。

> 待你确认哪一项要做、以及是否要改文档，我再动手。
