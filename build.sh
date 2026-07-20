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

# 1. 重建 app.tgz（含 app.py / ui / config / bin / templates）
tar $TAR_FMT -czf app.tgz app.py ui config bin templates

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
#    注意：不打包 wizard/ —— fnOS 的 trim-cli(app-center) 只要 fpk 含 wizard
#    就拒绝 install/uninstall（报 requires custom wizard parameters），导致 CLI 标准
#    部署流程完全走不通。去掉后 CLI 装/卸全通。安装说明文字请见 README / manifest desc。
tar $TAR_FMT -czf nasdash.fpk app.tgz cmd config ICON.PNG ICON_256.PNG manifest

ls -la nasdash.fpk app.tgz

# 4. 一致性校验（三处 md5 / desc 单行 / 版本号三处一致）
./verify.sh
