# -*- coding: utf-8 -*-
"""移植前的语义核对：逐块打印「我的改动」与「主项目对应位置」两边的上下文。

port_to_main.py 只检查行号重叠，查不出「主项目已经把同一个 bug 用另一种方式修了」
这种语义重复。这个脚本把每一块的两边上下文都打出来，供人工过目。

用法：
    python .build/inspect_port.py                # 只列有漂移、需要看的文件
    python .build/inspect_port.py --file app/api.py
"""
from __future__ import annotations

import argparse
import difflib
import io
import os
import subprocess
import sys

SHELL_ROOT = r"E:\L4D2ModStudio\L4D2ModStudio-github"
MAIN_ROOT = r"E:\L4D2ModStudio\L4D2ModStudio"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import port_to_main as P  # noqa: E402


def show(rel: str, ctx: int = 4) -> None:
    plan = P.plan_file(rel)
    main = plan["main"]
    base = P.shell_head_text(rel).splitlines(keepends=True)
    print("=" * 100)
    print("%s   基线 %d 行 / 主项目 %d 行 / 改动 %d 块 / 漂移 %d 行" % (
        rel, plan["base_len"], plan["main_len"], len(plan["hunks"]), plan["drift_lines"]))
    print("=" * 100)

    # 先把「主项目的漂移段」列出来，方便判断它是不是已经修过同一个问题
    b2m, drift_sm = P.build_line_map(base, main)
    print("\n[主项目相对基线的漂移段]")
    for tag, i1, i2, j1, j2 in drift_sm.get_opcodes():
        if tag == "equal":
            continue
        print("  %-7s base[%d:%d] -> main[%d:%d]  删%d/加%d" % (
            tag, i1 + 1, i2, j1 + 1, j2, i2 - i1, j2 - j1))
        for ln in base[i1:i2][:3]:
            print("      - " + ln.rstrip()[:100])
        for ln in main[j1:j2][:8]:
            print("      + " + ln.rstrip()[:100])
        if (j2 - j1) > 8:
            print("      ... 还有 %d 行" % (j2 - j1 - 8))

    print("\n[我的每块改动 + 主项目落点上下文]")
    for n, h in enumerate(plan["hunks"], 1):
        m1, m2 = h["main_range"]
        print("\n  ---- 块 %d/%d  %s  base[%d:%d] -> main[%d:%d]  %s" % (
            n, len(plan["hunks"]), h["tag"], h["base_range"][0] + 1, h["base_range"][1],
            m1 + 1, m2, "可干净落地" if h["clean"] else "★冲突★"))
        for ln in base[max(0, h["base_range"][0] - ctx):h["base_range"][0]]:
            print("     ctx- " + ln.rstrip()[:104])
        for ln in base[h["base_range"][0]:h["base_range"][1]]:
            print("     old- " + ln.rstrip()[:104])
        for ln in h["new_block"]:
            print("     new+ " + ln.rstrip()[:104])
        if h["base_range"][1] < len(base):
            for ln in base[h["base_range"][1]:h["base_range"][1] + ctx]:
                print("     ctx+ " + ln.rstrip()[:104])
        print("     --- 主项目落点：")
        for ln in main[max(0, m1 - ctx):m1]:
            print("     mctx- " + ln.rstrip()[:104])
        for ln in main[m1:m2]:
            print("     mold- " + ln.rstrip()[:104])
        for ln in main[m2:m2 + ctx]:
            print("     mctx+ " + ln.rstrip()[:104])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="", help="只看这个文件")
    ap.add_argument("--drift-only", action="store_true", help="只列有漂移的文件")
    args = ap.parse_args()

    files = subprocess.run(
        ["git", "-C", SHELL_ROOT, "diff", "--name-only", "HEAD"],
        capture_output=True, check=True).stdout.decode("utf-8").split()
    if args.file:
        files = [f for f in files if f == args.file]
    assert files, "没有匹配的文件"
    for rel in files:
        plan = P.plan_file(rel)
        if args.drift_only and plan["drift_lines"] == 0:
            continue
        show(rel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
