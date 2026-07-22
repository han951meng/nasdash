#!/bin/bash
set -e
cd "$(dirname "$0")"

# 跨平台 tar 打包参数：
#   macOS 的默认 tar 格式飞牛无法解析，须强制 GNU 格式（--format gnutar + 忽略 macOS 扩展属性）。
#   Linux（CI ubuntu）的 GNU tar 默认即 GNU 格式，直接用即可；--format gnutar 在 GNU tar 上反而报错。
case "$(uname -s)" in
  Darwin) TAR_FMT="--format gnutar --no-mac-metadata"; export COPYFILE_DISABLE=1 ;;
  *)      TAR_FMT="" ;;
esac

# 1. 重建 app.tgz（含 app.py / ui / config / bin / templates / docs 操作手册）
tar $TAR_FMT -czf app.tgz app.py ui config bin templates docs

# 2. 同步 manifest.checksum（GNU tar 把 mtime 嵌进归档，md5 必漂移，必须重算）
python3 - <<'PY'
import re, subprocess, hashlib
p = "manifest"
s = open(p).read()
md5 = hashlib.md5(open("app.tgz", "rb").read()).hexdigest()
s = re.sub(r'(?m)^(checksum\s*=\s*)\S+$', lambda m: m.group(1) + md5, s)
open(p, "w").write(s)
print("manifest checksum ->", md5)
PY

# 3. 重建 fpk（图标为已生成的高清版，不进 app.tgz）
#    双模式（用户 2026-07-20 确认的工作流）：
#      build.sh              → 默认【无向导 / wizard-free】，用于本地开发测试，
#                              可直接走 trim-cli 一键部署 stop→uninstall→install-fpk→start。
#      build.sh --with-wizard → 【带向导】，用于发布到 GitHub，
#                              飞牛应用中心 Web UI 安装时显示安装说明页、卸载时可选保留/删除配置。
#    关键：wizard 只在 fpk 根层（不进 app.tgz），故两种模式的 app.tgz / manifest / checksum
#          完全一致，只是 fpk 根多/少一个 wizard/ 目录——发布版与测试版应用代码逐字节相同。
WITH_WIZARD=0
for _a in "$@"; do
  case "$_a" in
    --with-wizard) WITH_WIZARD=1 ;;
  esac
done

if [ "$WITH_WIZARD" = "1" ]; then
  tar $TAR_FMT -czf nasdash.fpk app.tgz cmd config ICON.PNG ICON_256.PNG manifest wizard
  echo "[build] 含 wizard/（发布版，供 Web UI 安装/卸载向导页）"
else
  tar $TAR_FMT -czf nasdash.fpk app.tgz cmd config ICON.PNG ICON_256.PNG manifest
  echo "[build] 无 wizard/（测试版，可 trim-cli 一键部署）"
fi

ls -la nasdash.fpk app.tgz

# 4. 一致性校验（三处 md5 / desc 单行 / 版本号三处一致）
./verify.sh
