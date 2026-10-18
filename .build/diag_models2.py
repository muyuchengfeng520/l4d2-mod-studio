# -*- coding: utf-8 -*-
"""诊断 3（修正版）：那 16 条「模型缺件」里，哪些是真的坏、哪些是动画模型的误报。

只读。

⚠️ 上一版踩的坑（记录在此，别再犯）：
    VpkEntry.path 是**目录名**（未归一化，可能是 " "），
    完整内部路径要用 cached_property `vpath`；
    数据长度用 `entry_length`（不含 preload），总长用 `size`。

判据设想：
  1. 同一个 mod 里同 stem 有 .ani 兄弟文件 → 纯动画模型（.mdl 只是挂载壳），不需要 .vvd/.vtx；
  2. .mdl 体积远小于同目录其它完整模型 → 可能也是壳；
  3. 拿游戏本体做基线：游戏里「有 .mdl 无同路径 .vvd」的究竟有多少个，
     如果游戏自身也大量缺，那这条判据本身就过严。
"""
import os
import statistics
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


def by_vpath(v):
    """vpath -> entry 的字典。vpath 已是小写归一化，可直接比较。"""
    return {e.vpath: e for e in v.entries}


def main() -> int:
    import core.scan as scan
    import core.audit as audit
    from core.vpk import Vpk

    print("扫描…")
    res = scan.scan(CFG)
    mods = {m["key"]: m for m in scan.dicts_for_audit(res["mods"])}
    p = audit.prerequisites(list(mods.values()), CFG["game_dir"])
    metal = [f for f in p["findings"] if f["kind"] == "模型缺件"]
    print("模型缺件 %d 条\n" % len(metal))

    rows = []
    for f in metal:
        m = mods.get(f["key"])
        if not m:
            continue
        detail = f["detail"]
        base = detail.split("有 ")[-1].split(".mdl")[0] if "有 " in detail else "?"
        v = Vpk(m["_vpk_path"])
        tbl = by_vpath(v)
        # 按 basename 找那个 .mdl
        mdl = None
        for vp in sorted(tbl):
            if vp.startswith("models/") and vp.endswith(".mdl") and \
                    os.path.basename(vp)[:-4] == os.path.basename(base).lower():
                mdl = vp
                break
        if mdl is None:
            print("  ? 找不到 %s（mod=%s）" % (base, m["title"][:40]))
            continue
        stem = mdl[:-4]
        has_ani = (stem + ".ani") in tbl
        vvd = [x for x in tbl if x.startswith(stem) and ".vvd" in x]
        vtx = [x for x in tbl if x.startswith(stem) and ".vtx" in x]
        siblings = sorted(x for x in tbl if x.rsplit(".", 1)[0] == stem)
        rows.append({
            "stem": os.path.basename(stem),
            "mdl": mdl,
            "mdl_size": tbl[mdl].size,
            "has_ani": has_ani,
            "vvd": vvd, "vtx": vtx,
            "siblings": siblings,
            "title": m["title"],
            "mod_bytes": sum(e.size for e in v.entries),
        })

    print("%-3s %-26s %9s %6s %-5s %-30s" %
          ("#", "缺件 stem", ".mdl字节", "有ani", "兄弟数", "同 stem 兄弟文件"))
    print("-" * 100)
    for i, r in enumerate(rows, 1):
        print("%-3d %-26s %9d %6s %-5d %s" %
              (i, r["stem"], r["mdl_size"], "是" if r["has_ani"] else "否",
               len(r["siblings"]),
               [os.path.basename(x) for x in r["siblings"]][:5]))

    print("\n" + "=" * 100)
    print("汇总：按「有没有 .ani 兄弟」分组")
    print("=" * 100)
    with_ani = [r for r in rows if r["has_ani"]]
    no_ani = [r for r in rows if not r["has_ani"]]
    print("有 .ani 兄弟 → 纯动画模型，**不该报缺件**：%d 条" % len(with_ani))
    for r in with_ani:
        print("   %-24s .mdl=%7d 字节  兄弟=%s" %
              (r["stem"], r["mdl_size"], [os.path.basename(x) for x in r["siblings"]]))
    print("\n没 .ani 兄弟 → 疑似真缺件：%d 条" % len(no_ani))
    for r in no_ani:
        print("   %-24s .mdl=%7d 字节  mod总=%d  兄弟=%s" %
              (r["stem"], r["mdl_size"], r["mod_bytes"], r["siblings"]))

    # ---- 基线：游戏本体 ----
    print("\n" + "=" * 100)
    print("基线：游戏本体（pak01_dir.vpk）")
    print("=" * 100)
    gp = Vpk(os.path.join(CFG["game_dir"], "pak01_dir.vpk"))
    gnames = {e.vpath for e in gp.entries}
    gm = [(e.vpath, e.size) for e in gp.entries
          if e.vpath.startswith("models/") and e.vpath.endswith(".mdl")]
    print("游戏本体 .mdl 共 %d 个" % len(gm))
    if gm:
        vals = [s for _, s in gm]
        print("  .mdl 体积：最小 %d / 中位 %d / 最大 %d 字节"
              % (vals[0], int(statistics.median(vals)), vals[-1]))
        small = [(n, s) for n, s in gm if s < 32768]
        print("  小于 32KB 的 %d 个（%.1f%%）" % (len(small), 100.0 * len(small) / len(gm)))
        print("  样例: %s" % small[:5])

    missing_vvd = []
    missing_vtx = []
    for vp, _sz in gm:
        stem = vp[:-4]
        if (stem + ".vvd") not in gnames:
            missing_vvd.append(stem)
        if not any((stem + e) in gnames for e in (".vtx", ".dx90.vtx", ".dx80.vtx")):
            missing_vtx.append(stem)
    print("\n游戏本体里「有 .mdl 但无同路径 .vvd」：%d 个" % len(missing_vvd))
    for s in missing_vvd[:10]:
        print("   %s" % s)
    print("游戏本体里「有 .mdl 但无 .vtx」：%d 个" % len(missing_vtx))
    for s in missing_vtx[:10]:
        print("   %s" % s)
    # 这些缺 vvd 的 .mdl 是否配了 .ani
    with_ani_g = [s for s in missing_vvd if (s + ".ani") in gnames]
    print("其中配了 .ani 的：%d 个 → %s" % (len(with_ani_g), with_ani_g[:8]))

    print("\n" + "=" * 100)
    print("结论建议")
    print("=" * 100)
    if with_ani:
        print("· %d 条「模型缺件」其实是纯动画模型（同目录有 .ani），"
              "ANIM_ONLY 正则没覆盖这类命名，属于**误报**。" % len(with_ani))
    if no_ani:
        print("· %d 条没有 .ani 兄弟，需要人工再看（可能是真的缺件）：%s"
              % (len(no_ani), [r["stem"] for r in no_ani]))
    print("· 游戏本体自身也有 %d 个 .mdl 缺 .vvd —— 说明「缺 .vvd」不等于坏，"
          "判据只能当提示不能当结论。" % len(missing_vvd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
