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

# 3) manifest desc：用哨兵注释维护「最近更新」区块（应用中心介绍可见），保留 DESC_KEEP 条。
#    旧版靠正则匹配「更新亮点」块插入，一旦 desc 被手工重写删掉该块，正则永远失配、更新日志再也不会进 desc。
#    现改为在 desc 末尾用 <!--APP_CHANGELOG_START/END--> 哨兵包裹，每次发版由 changelog 字段重算生成，幂等可靠。
CL_START = "<!--APP_CHANGELOG_START-->"
CL_END = "<!--APP_CHANGELOG_END-->"
_cl = re.search(r"^changelog\s*=\s*(.*)$", s, re.M)
_cl_val = _cl.group(1) if _cl else ""
_cl_entries = re.split(r"\s+(?=\d+\.\d+\.\d+:)", _cl_val.strip())[:DESC_KEEP]
_cl_items = "".join(
    f"<li><b>{e.split(':', 1)[0]}</b> {e.split(':', 1)[1].strip()}</li>"
    for e in _cl_entries if e.strip()
)
_cl_block = f"{CL_START}<p><b>最近更新</b></p><ul>{_cl_items}</ul>{CL_END}"
if CL_START in s:
    s = re.sub(re.escape(CL_START) + r".*?" + re.escape(CL_END), _cl_block, s, flags=re.S)
else:
    s = re.sub(r"(^desc\s*=\s*)(.*)$", lambda m: f"{m.group(1)}{m.group(2)}{_cl_block}", s, flags=re.M)
# 防御：清理旧版「更新亮点」块（若存在）
s = re.sub(r"<p><b>v[\d.]+\s*更新亮点</b></p><ul>.*?</ul>", "", s)

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
