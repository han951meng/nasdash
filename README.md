# nasdash

飞牛OS NAS硬件监控面板 FPK应用包

## 功能

- 阵列卡状态（型号/固件/缓存/CacheVault/物理盘列表）
- 所有硬盘SMART（健康/温度/坏块/错误计数，异常盘标红）
- 系统资源（CPU/内存/负载/温度/运行时长）
- 存储卷（mdadm RAID阵列状态/成员盘/挂载点容量）

## 安装方法

1. 在飞牛OS应用中心上传 nasdash.fpk 安装
2. 或通过SSH: appcenter-cli install-fpk /path/to/nasdash.fpk
3. 安装后浏览器访问 http://NAS_IP:9800

## 技术栈

- Python 3 + Flask
- storcli / smartctl / sensors / mdadm 系统命令采集
- 单文件应用，无外部依赖（仅需Flask）
