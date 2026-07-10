v1.6.3 已修复这个问题。

根因：SAS9300-8e（或通过 expander 连接的盘）在 `storcli /c0 show` 的物理盘列表里，部分盘的 model 列会显示为 `-`，导致面板「品牌」「型号」那两列取不到值。但 `storcli /c0/e{e}/s{s} show all` 里通常能拿到 `Model Number` 或 `Inquiry Data`，v1.6.3 改成从这里兜底取型号。

请直接升级到 v1.6.3：
https://github.com/han951meng/nasdash/releases/tag/v1.6.3

升级后如果阵列卡页面还有品牌/型号显示异常，把 `/tmp/nasdash_debug.log` 和下面这条命令的输出发我，我再接着查：

```bash
sudo /usr/local/bin/storcli64 /c0 show
```
