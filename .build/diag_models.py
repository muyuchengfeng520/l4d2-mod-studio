# -*- coding: utf-8 -*-
"""诊断 2：模型缺件判据是不是真的在误报。

只读。跑法：python .build\\diag_models.py

核心怀疑：audit.prerequisites 的 A 类（模型缺件）用
    bare = p[:-4].lower()  →  在 avail_stems 里找 bare + ".vvd"
其中 avail_stems 是用
    re.sub(r"\\.(dx90|dx80|dx70|sw)$", "", p.rsplit(".", 1)[0].lower())
对**每个路径**去掉一层扩展名生成的。

问题：对 `models/x/y.dx90.vtx` → rsplit(".",1)[0] = `models/x/y.dx90`
→ re.sub 去掉 .dx90 → `models/x/y` ✔
但对 `models/x/y.mdl` → rsplit = `models/x/y` → re.sub 不变 → `models/x/y` ✔

看起来对。那为什么真实数据里 16 条「有 .mdl 但找不到 .vvd」？
本脚本直接对其中几个 mod 把 VPK 里的真实路径打出来看。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))

# 真实路径不写死，统一从 data/config.json 读（见同目录 _real_cfg.py）；
# .build/ 会被同步进公开仓库，写死路径会让他的敏感扫描直接中止同步。
import _real_cfg                                               # noqa: E402

CFG = _real_cfg.real_cfg()
if not CFG["game_dir"]:
    sys.exit("没有 game_dir，跑不了；先配好 data/config.json 或设 L4D2MS_CONFIG")

TARGET_KEYS = [
    "bill_slot_023.mdl",
    "v_pumpshotgunt.mdl",
    "w_crowbar0.mdl",
    "petridish01b.mdl",
]


def main() -> int:
    import core.scan as scan
    import core.audit as audit
    from core.vpk import Vpk

    print("扫描…")
    res = scan.scan(CFG)
    mods = scan.dicts_for_audit(res["mods"])

    p = audit.prerequisites(mods, CFG["game_dir"])
    metal = [f for f in p["findings"] if f["kind"] == "模型缺件"]
    print("模型缺件 %d 条" % len(metal))

    # 按 mod key 分组
    by_key = {}
    for f in metal:
        by_key.setdefault(f["key"], []).append(f)
    print("涉及 %d 个 mod\n" % len(by_key))

    for modkey, fs in by_key.items():
        m = next((x for x in mods if x["key"] == modkey), None)
        if not m:
            continue
        print("=" * 70)
        print("mod: %s" % m["title"][:60])
        print("vpk: %s" % m.get("_vpk_path"))
        v = Vpk(m["_vpk_path"])
        paths = v.list_files()
        print("内部 %d 个文件" % len(paths))
        # 打所有 models/ 与 .vvd/.vtx
        models = [x for x in paths if x.startswith("models/")]
        vvds = [x for x in paths if x.lower().endswith(".vvd")]
        vtxs = [x for x in paths if ".vtx" in x.lower()]
        print("  models/ 文件 %d 个（前 12）：" % len(models))
        for x in sorted(models)[:12]:
            print("     %s" % x)
        if len(models) > 12:
            print("     …共 %d 个" % len(models))
        print("  .vvd 文件 %d 个" % len(vvds))
        for x in sorted(vvds)[:12]:
            print("     %s" % x)
        print("  .vtx 文件 %d 个" % len(vtxs))
        for x in sorted(vtxs)[:12]:
            print("     %s" % x)

        # 针对报缺件的那个 stem，看本 mod 里有没有近似文件
        for f in fs[:3]:
            detail = f["detail"]
            base = detail.split("有 ")[-1].split(".mdl")[0] if "有 " in detail else "?"
            print("  报缺件 stem=%r" % base)
            bn = os.path.basename(base).lower()
            near = [x for x in paths if bn in x.lower()]
            print("    本 mod 内 basename 含该串的文件 %d 个: %s" % (len(near), near[:6]))

    print("\n=== 再看 avail_stems 构造有没有问题 ===")
    pak = os.path.join(CFG["game_dir"], "pak01_dir.vpk")
    gp = set(Vpk(pak).list_files())
    sample = [x for x in gp if x.startswith("models/")][:8]
    import re as _re
    for s in sample:
        stem = _re.sub(r"\.(dx90|dx80|dx70|sw)$", "", s.rsplit(".", 1)[0].lower())
        print("  %-50s -> %s" % (s, stem))

    print("\n=== 游戏 pak 里 zoey_slot_023 系列存在吗 ===")
    for pat in ("zoey_slot_023", "v_pumpshotgunt", "w_crowbar0"):
        hits = [x for x in gp if pat in x.lower()]
        print("  %-20s 命中 %d: %s" % (pat, len(hits), hits[:4]))

    print("\n=== 这些 mod 之间的 vvd 是否由其他 mod 提供 ===")
    all_mod_paths = set()
    for mm in mods:
        if mm.get("enabled"):
            all_mod_paths |= set(mm.get("_vpaths") or [])
    for pat in ("zoey_slot_023", "v_pumpshotgunt", "w_crowbar0", "petridish01b"):
        hits = [x for x in all_mod_paths if pat in x.lower()]
        print("  %-20s 全部已启用 mod 里命中 %d: %s" % (pat, len(hits), hits[:5]))

    return 0


if __name__ == "__main__":
    sys.exit(main())
