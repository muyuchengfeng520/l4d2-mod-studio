# -*- coding: utf-8 -*-
"""诊断：reassign.detect / plan 的字段完整性（含 internal_error）。

只读。跑法（项目根目录）：python .build\\diag_reassign2.py [某个 .vpk 路径]

不给参数时自动挑第一个「能识别成生还者替换」的 vpk 来试，
**不写死具体文件名** —— .build/ 会被同步进公开仓库，写死真实 mod 名
会让 tools/sync_to_github.py 的敏感扫描中止同步。
"""
import glob
import os
import sys

# 项目根按脚本位置推断（.build 的上一级），不写死某份副本的绝对路径
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from core import reassign                                       # noqa: E402
import _real_cfg                                                # noqa: E402


def pick_vpk():
    """命令行给了就直接用；否则在 addons 里找第一个识别成功的。"""
    if len(sys.argv) > 1:
        return sys.argv[1]
    addons = _real_cfg.addons_dir(_real_cfg.real_cfg())
    if not addons or not os.path.isdir(addons):
        sys.exit("没有可用的 addons 目录，跑不了；先配好 data/config.json 或设 L4D2MS_CONFIG")
    for cand in sorted(glob.glob(os.path.join(addons, "*.vpk"))):
        try:
            if reassign.detect(cand).get("ok"):
                return cand
        except Exception:                                        # noqa: BLE001
            continue
    sys.exit("addons 里没找到能识别为生还者替换的 vpk")


p = pick_vpk()
print("目标 vpk:", p)
d = reassign.detect(p)
print("detect keys:", sorted(d.keys()))
print("internal_error =", repr(d.get("internal_error")))

# 目标角色取「当前角色之外」的那个，长度不同才有看头（换不到会写明 why）。
# 注意 SURVIVORS 是 dict 的 list（每项含 id/zh/game/body/arm/panel），
# 不是 id 的集合；按 id 查要用 BY_ID，或者取 it["id"]。
cur = d.get("current") or ""
tgt = next((it["id"] for it in reassign.SURVIVORS if it["id"] != cur), "")
pl = reassign.plan(p, tgt, True)
print("plan(%s -> %s) ok: %s" % (cur, tgt, pl.get("ok")))
print("plan internal_error =", repr(pl.get("internal_error")))
print("internal_plan:")
for it in pl.get("internal_plan", []):
    print("  ", it)
print("rename_count:", pl.get("rename_count"), "drop_count:", pl.get("drop_count"))
