#!/bin/bash
set -e
cd /Users/hanyu/WorkBuddy/2026-07-01-17-37-29/nasdash
# 1. 重建 app.tgz（含品牌修复后的 app.py；vendor 已在其中）
COPYFILE_DISABLE=1 tar --format gnutar --no-mac-metadata -czf app.tgz app.py ui config vendor
# 2. 同步 manifest.checksum（仅替换哈希，保留原对齐）
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
# 4. 三处 md5 一致性校验
APP_MD5=$(md5 -q app.tgz)
MAN_MD5=$(grep '^checksum' manifest | awk -F'= ' '{print $2}' | tr -d ' ')
FPK_MD5=$(tar -xzOf nasdash.fpk app.tgz 2>/dev/null | md5 -q)
echo "app.tgz md5      = $APP_MD5"
echo "manifest checksum= $MAN_MD5"
echo "fpk inner app.tgz= $FPK_MD5"
if [ "$APP_MD5" = "$MAN_MD5" ] && [ "$APP_MD5" = "$FPK_MD5" ]; then
  echo "OK: 三处一致"
else
  echo "MISMATCH!"; exit 1
fi
ls -la nasdash.fpk app.tgz
