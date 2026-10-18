# -*- coding: utf-8 -*-
"""诊断：真实语料上「前置缺失」的 62 条到底是不是误报。

只读。跑法（项目根目录）：
    python .build\\diag_prereq.py

做的事：
  1. 跑一次真实 scan + audit.prerequisites，把 62 条按 kind / confidence 分组；
  2. 对「模型缺件」逐条回查：缺的 .vvd/.vtx 在游戏 pak 里到底有没有
     （用不同的 key 归一化方式再查一遍，看是不是判据写错了）；
  3. 对「脚本依赖缺失」逐条回查：引用的脚本名在 available_nuts 里
     有没有近似匹配（大小写/路径前缀/扩展名）。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))

# 真实路径不写死，统一从 data/config.json 读（见同目录 _real_cfg.py）；
# .build/ 会被同步进公开仓库，写死路径会让他的敏感扫描直接中止同步。
import _real_cfg                                               # noqa: E402

CFG = _real_cfg.real_cfg()
if not CFG["game_dir"]:
    sys.exit("没有 game_dir，跑不了；先配好 data/config.json 或设 L4D2MS_CONFIG")


def main() -> int:
    import core.scan as scan
    import core.audit as audit
    from core.vpk import Vpk

    print("扫描中…")
    t0 = time.monotonic()
    res = scan.scan(CFG)
    mods = scan.dicts_for_audit(res["mods"])
    print("扫描完成 %.2fs，%d 个 mod" % (time.monotonic() - t0, len(mods)))

    t0 = time.monotonic()
    p = audit.prerequisites(mods, CFG["game_dir"])
    print("prerequisites 完成 %.2fs" % (time.monotonic() - t0))
    print("  total=%d" % p.get("total", 0))
    print("  game_files_ok=%s" % p.get("game_files_ok"))
    print("  checked_against=%r" % p.get("checked_against"))
    items = p.get("findings") or []
    print("  findings=%d" % len(items))

    by_kind = {}
    by_conf = {}
    for f in items:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
        by_conf[f["confidence"]] = by_conf.get(f["confidence"], 0) + 1
    print("\n按类型：")
    for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        print("   %-14s %d" % (k, v))
    print("按置信度：")
    for k, v in sorted(by_conf.items(), key=lambda kv: -kv[1]):
        print("   %-14s %d" % (k, v))

    print("\n全部条目（前 80）：")
    for i, f in enumerate(items[:80], 1):
        print("  %3d. [%s/%s] %s | %s"
              % (i, f["kind"], f["confidence"], f["title"], f["detail"][:110]))

    # ---- 回查「模型缺件」 ----
    metal = [f for f in items if f["kind"] == "模型缺件"]
    if metal:
        print("\n" + "=" * 60)
        print("回查「模型缺件」：游戏 pak 里到底有没有对应的 .vvd/.vtx")
        print("=" * 60)
        pak = os.path.join(CFG["game_dir"], "pak01_dir.vpk")
        gp = set(Vpk(pak).list_files())
        print("游戏 pak 资源数：%d" % len(gp))
        # 游戏 pak 里的 models 路径样例
        samples = [x for x in gp if x.startswith("models/")][:5]
        print("models/ 样例：%s" % samples)

        for f in metal[:15]:
            key = f["key"]
            m = next((x for x in mods if x["key"] == key), None)
            detail = f["detail"]
            # detail 形如「有 xxx.mdl 但全盘都找不到配套 .vvd（…）」
            base = detail.split("有 ")[-1].split(".mdl")[0] if "有 " in detail else "?"
            stem = ("models/" + base) if not base.startswith("models/") else base
            bare = stem.lower()
            hits_vvd = [x for x in gp if x.lower() == bare + ".vvd"]
            hits_any = [x for x in gp if os.path.basename(x).lower() ==
                        os.path.basename(base).lower() + ".vvd"]
            print("  %-40s 游戏pak精确=%d 按basename=%d"
                  % (os.path.basename(base)[:38], len(hits_vvd), len(hits_any)))
            if hits_any[:2]:
                print("        样例: %s" % hits_any[:2])

    # ---- 回查「脚本依赖缺失」 ----
    nutl = [f for f in items if f["kind"] == "脚本依赖缺失"]
    if nutl:
        print("\n" + "=" * 60)
        print("回查「脚本依赖缺失」：引用名在可用 .nut 集合里的近似匹配")
        print("=" * 60)
        # 重建 available 集合（与 audit.prerequisites 同口径）
        all_mod = set()
        for m in mods:
            if m.get("enabled"):
                all_mod |= set(m.get("_vpaths") or [])
        gp = set(Vpk(os.path.join(CFG["game_dir"], "pak01_dir.vpk")).list_files())
        avail = gp | all_mod
        avail_nuts = {x for x in avail if x.endswith(".nut")}
        print("可用 .nut 总数：%d" % len(avail_nuts))
        print("样例：%s" % sorted(avail_nuts)[:6])
        for f in nutl[:15]:
            detail = f["detail"]
            ref = detail.split("引用了 ")[-1].split("，")[0] if "引用了 " in detail else "?"
            r = ref.strip().lstrip("./").lower()
            exact = any(c.lower() in avail_nuts for c in
                        {r, "scripts/vscripts/" + r,
                         os.path.dirname(f.get("detail", "")) + "/" + r})
            base = os.path.basename(r)
            basename_hits = [x for x in avail_nuts if x.lower().endswith("/" + base) or x.lower() == base]
            print("  %-32s 引用=%s 精确=%s basename命中=%d %s"
                  % (f["title"][:30], ref[:36], exact, len(basename_hits),
                     basename_hits[:2]))

    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

