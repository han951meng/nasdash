#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键发版准备：改版本号 + 同步 manifest(desc/changelog) + README + 重建 fpk + 校验。

用法:
    ./release.py 1.7.9 "本次更新的一句话要点"

脚本会:
  1. 校验新版本号大于当前版本号
  2. 更新 manifest: version / changelog(开头插入) / desc(插入新的「更新亮点」块到最前)
  3. 更新 README.md: 当前版本行 + 更新日志插入 ### vX.Y.Z 小节
  4. 调用 build.sh 重建 fpk 并跑 verify.sh
发版后的 commit/tag/push/Release 由你手动执行（见末尾提示）。
"""
import re
import subprocess
import sys

NEW_RAW = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lstrip("vV")
HEAD = sys.argv[2] if len(sys.argv) > 2 else None
if not NEW_RAW or not HEAD:
    print("用法: ./release.py <新版本号> <更新要点一句话>")
    print("示例: ./release.py 1.7.9 \"新增 XX 功能\"")
    sys.exit(1)

NEW = NEW_RAW          # 无 v，用于 manifest
NEWV = "v" + NEW_RAW   # 带 v，用于 README / desc

MAN = "manifest"
RD = "README.md"

# 更新日志保留条数（避免每次发版无限堆积）
DESC_KEEP = 3       # manifest desc「更新亮点」块（应用中心应用介绍）
CHANGELOG_KEEP = 3  # manifest changelog 单行条目
README_KEEP = 5     # README「## 更新日志」的 ### vX.Y.Z 小节

s = open(MAN, encoding="utf-8").read()
cur = re.search(r"^version\s*=\s*(\S+)", s, re.M).group(1)
cur_t = tuple(int(x) for x in cur.split("."))
new_t = tuple(int(x) for x in NEW.split("."))
print(f"当前版本 {cur} -> 新版本 {NEW}")
assert new_t > cur_t, f"新版本 {NEW} 必须大于当前版本 {cur}"

# 1) manifest version（保留原对齐空格）
s = re.sub(r"^version\s*=\s*\S+", f"version               = {NEW}", s, flags=re.M)

# 2) manifest changelog：在开头插入 NEW:
s = re.sub(r"^changelog\s*=\s*", f"changelog             = {NEW}: {HEAD} ", s, flags=re.M)

# 3) manifest desc：在第一个「更新亮点」块前插入新块
new_block = f"<p><b>{NEWV} 更新亮点</b></p><ul><li>{HEAD}</li></ul>"
s = re.sub(r"<p><b>v[^<]*?更新亮点</b></p>", new_block + r"\g<0>", s, count=1)

# 3a) 裁剪 desc：只保留最近 DESC_KEEP 个「更新亮点」块（连续位于 desc 末尾）
_blocks = list(re.finditer(r"<p><b>v[\d.]+\s*更新亮点</b></p><ul>.*?</ul>", s))
if len(_blocks) > DESC_KEEP:
    for m in reversed(_blocks[DESC_KEEP:]):
        s = s[:m.start()] + s[m.end():]

# 3b) 裁剪 changelog：单行只保留最近 CHANGELOG_KEEP 条（按 "X.Y.Z:" 版本标记切分）
_cl = re.search(r"^(changelog\s*=\s*)(.*)$", s, re.M)
if _cl:
    _val = _cl.group(2)
    _marks = list(re.finditer(r"\d+\.\d+\.\d+:", _val))
    if len(_marks) > CHANGELOG_KEEP:
        _val = _val[: _marks[CHANGELOG_KEEP].start()].rstrip()
        s = s[: _cl.start()] + _cl.group(1) + _val + s[_cl.end():]

open(MAN, "w", encoding="utf-8").write(s)

# 4) README：当前版本行 + 更新日志插入小节
r = open(RD, encoding="utf-8").read()
r = re.sub(r"当前版本：v[\d.]+", f"当前版本：{NEWV}", r, count=1)
new_sec = f"### {NEWV}\n- {HEAD}\n\n"
r = r.replace("## 更新日志\n", f"## 更新日志\n\n{new_sec}", 1)

# 4a) 裁剪 README：只保留最近 README_KEEP 个 ### vX.Y.Z 小节
_secs = list(re.finditer(r"^### v[\d.]+.*?(?=^### v|\Z)", r, re.M | re.S))
if len(_secs) > README_KEEP:
    for m in reversed(_secs[README_KEEP:]):
        r = r[:m.start()] + r[m.end():]
    r = r.rstrip() + "\n"

open(RD, "w", encoding="utf-8").write(r)

print("manifest / README 已更新，开始重建 fpk ...")
subprocess.check_call(["bash", "build.sh"])

print(f"\n发版准备完成：{NEWV}")
print("后续手动步骤：")
print(f"  git add -A && git commit -m 'release: {NEWV}'")
print(f"  git tag {NEWV} && git push && git push --tags")
print(f"  gh release upload {NEWV} nasdash.fpk --clobber")
