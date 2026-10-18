# -*- coding: utf-8 -*-
"""把「壳仓库（L4D2ModStudio-github）」里的改动搬回「主项目（L4D2ModStudio）」。

为什么需要这个脚本
------------------
本项目有两份代码：
  * 主项目 L4D2ModStudio/         —— 日常开发与真正在用的那份（有 data/、dist/、真实 mod 名）
  * 壳仓库 L4D2ModStudio-github/  —— 发布用的「壳」，注释里的真实 mod 名被泛化
两份靠 tools/sync_to_github.py 做「主项目 → 壳」的单向同步。

本次代码审查误把改动都做在了壳上，要搬回主项目。**不能整文件覆盖**：
壳里注释被泛化过（真实 mod 名换成了「一个 354 MB 的 VPK」这类描述），
直接复制会把主项目的真实注释冲掉。

所以用三方差分：
  1. 壳 HEAD（= 上次同步时的基线）→ 壳工作区   = 「我的改动」hunks
  2. 壳 HEAD → 主项目                          = 「两份原有漂移」的行号映射
用 2 建立「基线行号 → 主项目行号」的映射，把 1 的每个 hunk 的基线区间
翻译成主项目的行区间，再在主项目里替换成新内容。这样主项目独有的内容
（真实 mod 名注释、它自己后来加的东西）能原样保留。

用法
----
    python .build/port_to_main.py            # dry-run：只报告每个文件能落多少块
    python .build/port_to_main.py --apply    # 真正写入（会先备份到 _port_backup/）
"""
from __future__ import annotations

import argparse
import difflib
import os
import shutil
import subprocess
import sys
import time

SHELL_ROOT = r"E:\L4D2ModStudio\L4D2ModStudio-github"
MAIN_ROOT = r"E:\L4D2ModStudio\L4D2ModStudio"


def read_text(path: str) -> tuple[str, bool]:
    """读文本，返回 (内容, 是否原本带 BOM)。行尾原样保留，不转换。"""
    with open(path, "rb") as f:
        raw = f.read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    if bom:
        raw = raw[3:]
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc), bom
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), bom


def write_text(path: str, text: str, bom: bool) -> None:
    """按原 BOM 状态写回，先写临时文件再 os.replace（原子）。"""
    data = (("\ufeff" if bom else "") + text).encode("utf-8")
    tmp = path + ".porttmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def shell_head_text(rel: str) -> str:
    """取壳仓库 HEAD 里的文件内容（= 上次同步的基线）。"""
    out = subprocess.run(
        ["git", "-C", SHELL_ROOT, "show", "HEAD:" + rel],
        capture_output=True, check=True,
    ).stdout
    if out.startswith(b"\xef\xbb\xbf"):
        out = out[3:]
    return out.decode("utf-8")


def build_line_map(base: list[str], main: list[str]) -> tuple[list[int], object]:
    """建立「基线行号 → 主项目行号」映射。

    difflib 只能给出等长块内的精确对应；两块之间的边界（replace/delete 区段）
    靠 opcode 的边界补上。区间内部的行号是不可信区（因为主项目那边被改写/删掉了）。
    """
    sm = difflib.SequenceMatcher(None, base, main, autojunk=False)
    b2m = [-1] * (len(base) + 1)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                b2m[i1 + k] = j1 + k
        # 不管什么 tag，两端边界总能对上（这点是 difflib opcode 的性质）
        b2m[i1] = j1
        b2m[i2] = j2
    return b2m, sm


def dirty_ranges(drift_sm) -> list[tuple[int, int]]:
    """基线里「与主项目不一致」的行区间（不含 equal 块）。"""
    return [(i1, i2) for tag, i1, i2, j1, j2 in drift_sm.get_opcodes() if tag != "equal"]


def overlaps(a1: int, a2: int, b1: int, b2: int) -> bool:
    """半开区间 [a1,a2) 与 [b1,b2) 是否相交；空区间按点处理。"""
    if a1 == a2:
        return b1 <= a1 < b2
    if b1 == b2:
        return a1 <= b1 < a2
    return a1 < b2 and b1 < a2


def plan_file(rel: str) -> dict:
    """算出一个文件的移植计划（不写盘）。"""
    shell_path = os.path.join(SHELL_ROOT, rel)
    main_path = os.path.join(MAIN_ROOT, rel)
    base_text = shell_head_text(rel)
    new_text, _ = read_text(shell_path)
    main_text, main_bom = read_text(main_path)

    base = base_text.splitlines(keepends=True)
    new = new_text.splitlines(keepends=True)
    main = main_text.splitlines(keepends=True)

    b2m, drift_sm = build_line_map(base, main)
    dirty = dirty_ranges(drift_sm)
    # 漂移规模：基线里有多少行跟主项目不同（只用于报告）
    drift_lines = sum(i2 - i1 for i1, i2 in dirty)

    edit_sm = difflib.SequenceMatcher(None, base, new, autojunk=False)
    hunks = []
    for tag, i1, i2, j1, j2 in edit_sm.get_opcodes():
        if tag == "equal":
            continue
        m1 = b2m[i1]
        m2 = b2m[i2]
        clean = True
        if m1 < 0 or m2 < 0 or m2 < m1:
            clean = False
        else:
            for d1, d2 in dirty:
                if overlaps(i1, i2, d1, d2):
                    clean = False
                    break
        hunks.append({
            "tag": tag,
            "base_range": (i1, i2),
            "new_block": new[j1:j2],
            "main_range": (m1, m2),
            "clean": clean,
        })
    return {
        "rel": rel,
        "hunks": hunks,
        "main_bom": main_bom,
        "main": main,
        "dirty": dirty,
        "drift_lines": drift_lines,
        "base_len": len(base),
        "new_len": len(new),
        "main_len": len(main),
    }


def apply_file(plan: dict) -> tuple[int, list[dict], list[str]]:
    """按计划改写主项目文件，返回 (成功块数, 冲突块列表, 自检失败信息)。"""
    main = list(plan["main"])
    applied = 0
    failed = []
    # 逆序应用，避免前面的替换影响后面 hunk 的行号
    for h in reversed(plan["hunks"]):
        if not h["clean"]:
            failed.append(h)
            continue
        m1, m2 = h["main_range"]
        main[m1:m2] = h["new_block"]
        applied += 1
    result = "".join(main)
    path = os.path.join(MAIN_ROOT, plan["rel"])
    write_text(path, result, plan["main_bom"])

    # 写入后自检：每一块的新内容必须逐字出现在成品里。
    # 行号算对但上下文错位这种事故，只有比对内容才能发现。
    problems = []
    for n, h in enumerate(plan["hunks"], 1):
        if not h["clean"]:
            continue
        blob = "".join(h["new_block"])
        if blob.strip() and blob not in result:
            problems.append("块 %d（base[%d:%d]）的新内容没出现在成品里"
                            % (n, h["base_range"][0] + 1, h["base_range"][1]))
    return applied, failed, problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    ap.add_argument("--only", default="", help="只处理匹配这个前缀的文件")
    args = ap.parse_args()

    files = subprocess.run(
        ["git", "-C", SHELL_ROOT, "diff", "--name-only", "HEAD"],
        capture_output=True, check=True,
    ).stdout.decode("utf-8").split()
    if args.only:
        files = [f for f in files if f.startswith(args.only)]

    if not files:
        print("没有待移植的改动喵。")
        return 0

    # 主项目不是 git 仓库，改动前先整份备份（带时间戳，可整体还原）
    backup_dir = ""
    if args.apply:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = os.path.join(os.path.dirname(MAIN_ROOT), "_port_backup_" + stamp)
        for rel in files:
            src = os.path.join(MAIN_ROOT, rel)
            dst = os.path.join(backup_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        print("已备份主项目原文件到：%s" % backup_dir)

    print("\n%-24s %6s %6s %6s %6s  %s" % ("文件", "改动块", "可落块", "冲突块", "漂移行", "主项目行数"))
    print("-" * 88)
    total_ok = total_bad = 0
    all_failed = []
    all_problems = []
    for rel in files:
        plan = plan_file(rel)
        ok = sum(1 for h in plan["hunks"] if h["clean"])
        bad = len(plan["hunks"]) - ok
        total_ok += ok
        total_bad += bad
        print("%-24s %6d %6d %6d %6d  %d -> %d" % (
            rel, len(plan["hunks"]), ok, bad, plan["drift_lines"],
            plan["base_len"], plan["main_len"]))
        if args.apply:
            _a, failed, problems = apply_file(plan)
            for h in failed:
                all_failed.append((rel, h))
            for msg in problems:
                all_problems.append((rel, msg))
    print("-" * 88)
    print("合计：可干净落地 %d 块，冲突 %d 块" % (total_ok, total_bad))

    if all_failed:
        print("\n以下是需要人工处理的冲突块（基线区间 / 主项目映射区间）：")
        for rel, h in all_failed:
            print("  %s  base%s -> main%s  (%s)" % (
                rel, h["base_range"], h["main_range"], h["tag"]))
    if all_problems:
        print("\n★ 写入后自检失败（内容没落进成品，必须回滚重做）：")
        for rel, msg in all_problems:
            print("  %s  %s" % (rel, msg))
    if not args.apply:
        print("\n（这是 dry-run，没有写任何文件；确认后加 --apply）")
    return 0 if (total_bad == 0 and not all_problems) else 2


if __name__ == "__main__":
    sys.exit(main())
