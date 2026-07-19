#!/bin/bash
# 发版前一致性校验：挡掉 10111(manifest 换行) / 校验失败 / 版本号四处不一致等低级错误。
# 由 build.sh 在打包后自动调用，也可单独运行。
set -e
cd "$(dirname "$0")"

# 跨平台 md5：macOS 用 `md5 -q`，Linux 用 `md5sum`
md5_of() {
  if command -v md5 >/dev/null 2>&1; then
    md5 -q "$1"
  else
    md5sum "$1" | awk '{print $1}'
  fi
}

echo "=== verify.sh: 发版前一致性校验 ==="

APP_MD5=$(md5_of app.tgz)
MAN_MD5=$(grep '^checksum' manifest | awk -F'= ' '{print $2}' | tr -d ' ')
FPK_MD5=$(tar -xzOf nasdash.fpk app.tgz 2>/dev/null | md5_of /dev/stdin)
echo "app.tgz md5      = $APP_MD5"
echo "manifest checksum= $MAN_MD5"
echo "fpk inner app.tgz= $FPK_MD5"
if [ "$APP_MD5" = "$MAN_MD5" ] && [ "$APP_MD5" = "$FPK_MD5" ]; then
  echo "OK: 三处 md5 一致"
else
  echo "MISMATCH: md5 不一致！"; exit 1
fi

python3 - <<'PY'
import re, subprocess
# manifest 解析（value 可能跨续行，正确检测 desc 是否含真实换行）
s = open('manifest').read()
cur = None; kv = {}
for line in s.split('\n'):
    m = re.match(r'^(\S+)\s*=\s*(.*)$', line)
    if m:
        cur = m.group(1); kv[cur] = m.group(2)
    elif cur is not None:
        kv[cur] += '\n' + line
assert '\n' not in kv.get('desc', ''), "desc 含换行！应用中心会报 10111"
print("OK: manifest desc 单行通过")

man_ver = re.search(r'^version\s*=\s*(\S+)', s, re.M).group(1)
fpk = subprocess.check_output(['tar', '-xzOf', 'nasdash.fpk', 'manifest']).decode()
fpk_ver = re.search(r'^version\s*=\s*(\S+)', fpk, re.M).group(1)
readme = open('README.md').read()
rm = re.search(r'当前版本：v([\d.]+)', readme)
rm_ver = rm.group(1) if rm else None
print(f"版本号 -> manifest={man_ver}  fpk={fpk_ver}  README={rm_ver}")
assert man_ver == fpk_ver, "fpk 内 manifest 版本与根目录 manifest 不一致"
assert man_ver == rm_ver, "README 当前版本与 manifest 不一致"
print("OK: 版本号三处一致 (manifest / fpk / README)")

import json, os
for f in ('config/privilege', 'config/resource'):
    assert os.path.exists(f), f"{f} 缺失（fnpack 打包检查要求存在）"
    try:
        json.load(open(f))
    except Exception as e:
        raise AssertionError(f"{f} 不是合法 JSON: {e}")
print("OK: config/privilege 与 config/resource 为合法 JSON（fnpack 等价检查）")
PY

echo "=== verify 全部通过 ==="
