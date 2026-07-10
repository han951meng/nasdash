【回复】阵列卡温度读不到的问题已修复（v1.6.2）

感谢反馈！根因找到了，是两个环境差异导致的：

1. 旧版把 storcli 路径写死了（/usr/local/bin/storcli），但部分机器（比如 9300-8E 这类 HBA 卡）实际只装了 `storcli64`，命令找不到就静默失败；
2. 旧版用 `sudo -n`（免密 sudo）去跑，如果系统没配免密 sudo，同样静默失败，温度自然取不到。

v1.6.2 已经修好：
- storcli 路径改成**自动探测**：优先找 `storcli64`，找不到再回退 `storcli`；
- 应用本身是飞牛以 **root** 拉起的，已改成直接执行，**不再依赖免密 sudo**；
- 同时兼容 **MegaRAID**（读 ROC 温度）和 **HBA 直通卡**（/c0 show temperature 兜底）的温度。

我也在「只有 storcli64、无免密 sudo」的环境里实测过，温度能正常取到（61°C 左右），所以其他用户的机器应该也都没问题了。

最新版（含 .fpk 安装包，直接升级即可）：
https://github.com/han951meng/nasdash/releases/tag/v1.6.2

升级后如果还有温度读不到的情况，把页面报错或 /tmp/nasdash_debug.log 发我，我再接着查。
