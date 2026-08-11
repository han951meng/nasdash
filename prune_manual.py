#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prune_manual.py — 维护使用手册里「往期版本已修复」段的长度。

为什么需要它：
  nasdash 操作手册顶部有「当前版本新增」块，下面紧跟「往期版本已修复」块。
  每次升版本号，当前版本块应当沉入往期，而往期越积越长、翻起来很累。
  本脚本把「裁剪往期」和「升版本号时自动搬运」两件事做成一条命令，
  避免手工操作遗漏（人总会忘）。

用法：
  # 仅裁剪（不升版本号）：往期保留最近 --keep 条（默认 3）
  python3 prune_manual.py
  python3 prune_manual.py --keep 3

  # 升版本号：把「当前版本新增」块整体移入往期、更新版本号、再裁剪
  # 同步更新：manifest 版本号 + README 当前版本 + 手册版本声明/当前块标题
  python3 prune_manual.py --bump 2.0.1

  # 只看会怎么改，不落盘
  python3 prune_manual.py --keep 3 --dry-run

判定规则：
  - 往期段 = 「**往期版本已修复...**」之后、下一个「---」之前的内容。
  - 一条「历史」= 一个顶格（0 缩进）的 **...** 标题 + 其后续行，直到下一个顶格 **...** 或段尾。
  - 带 X.Y.Z 版本号的按版本号降序排序；不带版本号的视为最旧，优先被裁掉。
  - --bump 时，当前版本块（「本版本新增了什么（vX 新增）」到其后「---」之间）整体移入往期，
    标题改为「X 版修复」，并把块内顶格 **...** 子标题缩进成列表项，
    避免下次解析被错误地拆成多条历史。
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MANUAL = os.path.join(HERE, "docs", "使用手册.md")
MANIFEST = os.path.join(HERE, "manifest")
README = os.path.join(HERE, "README.md")

HEADER_CURRENT = re.compile(r"本版本新增了什么")
HEADER_WANGQI = re.compile(r"往期版本已修复")
SEP = re.compile(r"^---\s*$")
BOLD_TOP = re.compile(r"^\*\*(.+?)\*\*\s*$")
VER = re.compile(r"(\d+)\.(\d+)\.(\d+)")
VERSION_LINE = re.compile(r'^version\s*=\s*([\d.]+)\s*$', re.I)


def read_lines(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.readlines()


def write_lines(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def strip_blanks(lines):
    out = list(lines)
    while out and out[0].strip() == "":
        out.pop(0)
    while out and out[-1].strip() == "":
        out.pop()
    return out


def ver_of(text):
    m = VER.search(text)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def parse_entries(block_lines):
    """把一段文本拆成若干「条目」（每个条目含标题行 + 后续行）。"""
    entries = []
    cur = None
    for line in block_lines:
        if line.strip() == "":
            if cur is not None:
                cur["lines"].append(line)
            continue
        if BOLD_TOP.match(line):
            if cur is not None:
                entries.append(cur)
            cur = {"header": line.rstrip("\n"), "lines": [line]}
        else:
            if cur is None:
                # 段首的游离行：归到一个无名条目里，避免丢失
                cur = {"header": None, "lines": []}
            cur["lines"].append(line)
    if cur is not None:
        entries.append(cur)
    # 去掉每个条目首尾的空行，便于重排
    for e in entries:
        e["lines"] = strip_blanks(e["lines"])
    return entries


def render_entries(entries):
    out = []
    for i, e in enumerate(entries):
        if i > 0:
            out.append("\n")
        out.extend(e["lines"])
        out.append("\n")
    return out


def prune_now(keep):
    lines = read_lines(MANUAL)
    # 定位分隔与标题
    idx_cur = next((i for i, l in enumerate(lines) if HEADER_CURRENT.search(l)), None)
    if idx_cur is None:
        print("找不到「本版本新增了什么」块，退出。")
        return False
    idx_sep1 = next((i for i in range(idx_cur, len(lines)) if SEP.match(lines[i])), None)
    idx_wq = next((i for i in range(idx_sep1 + 1, len(lines)) if HEADER_WANGQI.search(lines[i])), None)
    if idx_wq is None:
        print("找不到「往期版本已修复」块，退出。")
        return False
    idx_sep2 = next((i for i in range(idx_wq + 1, len(lines)) if SEP.match(lines[i])), None)
    if idx_sep2 is None:
        print("找不到往期段结束的「---」，退出。")
        return False

    wq_body = lines[idx_wq + 1: idx_sep2]
    entries = parse_entries(wq_body)
    # 按版本号降序；无版本号的排最后（优先被裁）
    entries_sorted = sorted(entries, key=lambda e: ver_of(e["header"] or ""), reverse=True)
    before = len(entries_sorted)
    kept = entries_sorted[:keep]
    dropped = entries_sorted[keep:]

    new_wq = render_entries(kept)
    new_lines = lines[: idx_wq + 1] + new_wq + lines[idx_sep2:]

    print(f"往期条目：原 {before} 条 → 保留 {len(kept)} 条（--keep {keep}）")
    print("保留：")
    for e in kept:
        print("  +", (e["header"] or "(无名)").strip())
    if dropped:
        print("裁掉：")
        for e in dropped:
            print("  -", (e["header"] or "(无名)").strip())
    return new_lines


def bump_version(newver, dry_run=False):
    lines = read_lines(MANUAL)
    idx_cur = next((i for i, l in enumerate(lines) if HEADER_CURRENT.search(l)), None)
    if idx_cur is None:
        print("找不到「本版本新增了什么」块，无法 bump。")
        return None
    idx_sep1 = next((i for i in range(idx_cur, len(lines)) if SEP.match(lines[i])), None)
    idx_wq = next((i for i in range(idx_sep1 + 1, len(lines)) if HEADER_WANGQI.search(lines[i])), None)
    if idx_wq is None:
        print("找不到「往期版本已修复」块，无法 bump。")
        return None
    idx_sep2 = next((i for i in range(idx_wq + 1, len(lines)) if SEP.match(lines[i])), None)

    # 旧版本号：优先用 manifest，否则从当前块标题里取
    oldver = read_manifest_version()
    m = VER.search(lines[idx_cur])
    if not oldver and m:
        oldver = m.group(0)
    if not oldver:
        print("无法确定旧版本号，请检查 manifest 或当前块标题。")
        return None

    # 当前版本块（标题 + 正文，不含末尾「---」）
    cur_block = lines[idx_cur: idx_sep1]
    # 正文 = 去掉首行标题后的部分
    cur_body = cur_block[1:]
    # 把正文里顶格 **...** 子标题缩进成列表项，避免下次被拆成多条
    new_entry_lines = ["**%s 版修复**\n" % oldver]
    for bl in cur_body:
        if BOLD_TOP.match(bl):
            new_entry_lines.append("  " + bl)
        else:
            new_entry_lines.append(bl)
    # 规整：去掉首尾空行
    new_entry = {"header": "**%s 版修复**" % oldver, "lines": strip_blanks(new_entry_lines)}

    # 现有往期条目 + 新条目，按版本降序后裁剪
    wq_body = lines[idx_wq + 1: idx_sep2]
    entries = parse_entries(wq_body)
    entries.append(new_entry)
    entries_sorted = sorted(entries, key=lambda e: ver_of(e["header"] or ""), reverse=True)
    kept = entries_sorted[:3]

    # 重写当前版本块：标题换新版号，正文留占位提示
    new_cur_header = "**本版本新增了什么（v%s 新增）**\n" % newver
    new_cur_body = ["\n", "（请在此填写 v%s 的本版本新增 / 修复内容，写法和往期一致。）\n" % newver, "\n"]

    # 手册版本声明 / 当前块标题里的 vOLD -> vNEW
    lines = [re.sub(r"v" + re.escape(oldver) + r"\b", "v" + newver, l) for l in lines]

    new_wq = render_entries(kept)
    new_lines = (
        lines[:idx_cur]
        + [new_cur_header] + new_cur_body
        + lines[idx_sep1: idx_wq + 1]
        + new_wq
        + lines[idx_sep2:]
    )

    # 同步 manifest / README 版本号（dry-run 不写）
    readme_out, readme_changed = bump_readme_version(oldver, newver)
    if dry_run:
        if readme_changed:
            print(f"[--dry-run] README.md 版本 v{oldver} → v{newver}（将更新）")
    else:
        if readme_changed:
            write_lines(README, readme_out)
        set_manifest_version(newver)

    print(f"版本号 {oldver} → {newver}")
    print(f"往期条目：原 {len(entries_sorted)-1} 条 + 当前块 → 保留 {len(kept)} 条")
    for e in kept:
        print("  +", (e["header"] or "(无名)").strip())
    return new_lines


def read_manifest_version():
    if not os.path.exists(MANIFEST):
        return None
    for l in read_lines(MANIFEST):
        m = VERSION_LINE.match(l)
        if m:
            return m.group(1)
    return None


def set_manifest_version(newver):
    if not os.path.exists(MANIFEST):
        return
    lines = read_lines(MANIFEST)
    for i, l in enumerate(lines):
        if VERSION_LINE.match(l):
            lines[i] = "version               = %s\n" % newver
            break
    write_lines(MANIFEST, lines)


def bump_readme_version(oldver, newver):
    """返回 (新行列表, 是否改动)。写盘由调用方按 dry-run 决定。"""
    if not os.path.exists(README):
        return None, False
    lines = read_lines(README)
    out = []
    changed = False
    for l in lines:
        nl = re.sub(r"v" + re.escape(oldver) + r"\b", "v" + newver, l)
        if nl != l:
            changed = True
        out.append(nl)
    return out, changed


def main():
    ap = argparse.ArgumentParser(description="裁剪 / 升版本号时自动维护使用手册往期段")
    ap.add_argument("--keep", type=int, default=3, help="往期保留条数（默认 3）")
    ap.add_argument("--bump", metavar="NEWVER", help="升版本号，如 2.0.1")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = ap.parse_args()

    if args.bump:
        new_lines = bump_version(args.bump, dry_run=args.dry_run)
    else:
        new_lines = prune_now(args.keep)

    if new_lines is None:
        sys.exit(1)

    if args.dry_run:
        print("\n[--dry-run] 以下为改动后的往期段预览：")
        print("".join(new_lines))
        return

    write_lines(MANUAL, new_lines)
    print("\n已写入 %s" % MANUAL)
    if args.bump:
        print("已同步 manifest 版本号 → %s" % args.bump)


if __name__ == "__main__":
    main()
