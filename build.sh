#!/bin/bash
set -e
cd "$(dirname "$0")"

# 1. 重建 app.tgz（含 app.py / ui / config / vendor）
COPYFILE_DISABLE=1 tar --format gnutar --no-mac-metadata -czf app.tgz app.py ui config bin

# 2. 同步 manifest.checksum（GNU tar 把 mtime 嵌进归档，md5 必漂移，必须重算）
python3 - <<'PY'
import re, subprocess
p = "manifest"
s = open(p).read()
md5 = subprocess.check_output(["md5", "-q", "app.tgz"]).decode().strip()
s = re.sub(r'(?m)^(checksum\s*=\s*)\S+$', lambda m: m.group(1) + md5, s)
open(p, "w").write(s)
print("manifest checksum ->", md5)
PY

# 3. 重建 fpk（图标为已生成的高清版，不进 app.tgz）
COPYFILE_DISABLE=1 tar --format gnutar --no-mac-metadata -czf nasdash.fpk app.tgz cmd config ICON.PNG ICON_256.PNG manifest wizard

ls -la nasdash.fpk app.tgz

# 4. 一致性校验（三处 md5 / desc 单行 / 版本号三处一致）
./verify.sh
