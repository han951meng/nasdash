# nasdash v1.8.2

## 更新内容
- **性能优化**：切换导航栏只刷新当前板块，后台轮询从"每 30s 全量拉取 /api/all"改为"只刷新当前可见板块"；重型采集（阵列卡 storcli / 磁盘 SMART smartctl）新增 12s TTL 缓存，快速来回切换板块不再重复跑重活，大幅减少全量刷新卡顿。

## 部署说明
- 飞牛应用中心 → 上传本 `nasdash.fpk` 安装即可。
- 已去除安装/卸载向导（wizard），支持 trim-cli 标准部署链路：`app stop → app uninstall → app install-fpk → app start`，版本号由应用中心自动登记（无需手改）。
- 升级时建议先在应用中心卸载再安装；配置（风扇标注/模式/历史趋势）位于 `@appdata`，卸载时选择"保留配置"即可沿用。

## 校验
- fpk md5：`1ddca9e114cf95cbc2a55b0e2030f25f`
- 兼容性：fnOS 主流版本；MegaRAID 阵列卡与 HBA 直通卡 storcli 已随包内置。
