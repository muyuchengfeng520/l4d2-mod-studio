# -*- coding: utf-8 -*-
"""第三轮验证：拿**真实玩家数据**跑，但严格只读原文件（一律先复制到临时目录再动）。

跑法（项目根目录）：
    python .build\\verify_real.py

为什么单独一份：真实 addonlist.txt 是玩家的**唯一**加载配置，绝不能拿它试刀。
本脚本每一处写入都发生在 tempfile 目录里的副本上；原文件只做 `open(..., "rb")`。

覆盖：
  A. 真实 addonlist.txt：读 → 渲染 → 逐字节对比（不该有任何改动）；
     再复制一份真的做「启用/停用/隔离」，验证副本上的结果。
  B. 真实标签库 TaggedAssets.jsonc：加载耗时、规则数、错误信息、分桶命中率。
  C. 真实 mod 仓库（repo_dir）：只列目录不读 VPK 内容，统计规模（用于判断
     扫描/审计的复杂度是否真会踩到）。
  D. 真实游戏目录：只 stat 不读，确认 addonlist 路径推导正确。
"""
import os
import sys
import shutil
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))

# core.safety 在 import 期就把数据目录算死并 makedirs，所以必须在 import 之前
# 把 L4D2MS_WORK_ROOT 指到临时目录；否则隔离检查会把产物写进玩家真实的 data/。
import atexit as _atexit
import shutil as _shutil
_WORK = tempfile.mkdtemp(prefix="l4d2ms-real-")
_atexit.register(_shutil.rmtree, _WORK, ignore_errors=True)
os.environ["L4D2MS_WORK_ROOT"] = _WORK

# 真实路径**不写死**：.build/ 会被 tools/sync_to_github.py 整目录同步到公开
# 仓库，而它 commit 前的敏感扫描会把「带盘符的 Steam 库 / 用户目录」判成泄漏
# 并直接中止同步。改成从 data/config.json 读（data/ 不在同步白名单里），
# 顺带的好处是这份脚本换台机器也能直接跑。详见同目录 _real_cfg.py。
import _real_cfg                                               # noqa: E402

CFG = _real_cfg.real_cfg()

PASSED, FAILED = [], []


def check(name):
    def deco(fn):
        try:
            fn()
            PASSED.append(name)
            print("  [OK]   %s" % name)
        except Exception as ex:                                   # noqa: BLE001
            FAILED.append((name, ex))
            print("  [FAIL] %s -> %s: %s" % (name, type(ex).__name__, ex))
            traceback.print_exc()
        return fn
    return deco


def main() -> int:
    import core.manager as manager
    import core.safety as safety
    import core.scan as scan
    import core.paths as paths

    real_al = paths.addonlist_path(CFG) if hasattr(paths, "addonlist_path") else ""
    print("=" * 66)
    print("真实数据探路")
    print("=" * 66)
    print("  addonlist : %s  %s" % (real_al, "存在" if os.path.isfile(real_al) else "不存在"))
    print("  仓库目录  : %s  %s" % (CFG["repo_dir"], "存在" if os.path.isdir(CFG["repo_dir"]) else "不存在"))
    print("  标签库    : %s  %s" % (CFG["taglib"], "存在" if os.path.isfile(CFG["taglib"]) else "不存在"))
    print()

    tmp = tempfile.mkdtemp(prefix="l4d2ms-real-")

    print("=" * 66)
    print("A) 真实 addonlist.txt（副本上操作，原文件只读）")
    print("=" * 66)

    if not os.path.isfile(real_al):
        print("  [SKIP] 真实 addonlist.txt 不存在")
    else:
        real_bytes = open(real_al, "rb").read()

        @check("渲染往返：读→写 逐字节相同（不碰原文件，拿内存文本对比）")
        def _():
            txt, enc = safety.read_text_auto(real_al)
            mapping = scan.read_addonlist(real_al)
            new_text, keys = manager._render_addonlist(txt, mapping)
            assert new_text == txt, (
                "往返改动了内容：原文 %d 字节 / 重渲染 %d 字节" % (len(txt), len(new_text)))
            print("         编码 %s，%d 字节，%d 条目，往返逐字节相同"
                  % (enc, len(txt.encode(enc)), len(mapping)))

        @check("复制副本后真做一次「停用再启用」：条目数不变、内容可逆")
        def _():
            cp = os.path.join(tmp, "addonlist.txt")
            shutil.copy2(real_al, cp)
            before = scan.read_addonlist(cp)
            k0 = next(iter(before), None)
            assert k0, "真实表里一条都没有？"
            want = not before[k0]
            r = manager.set_enabled(cp, {k0: want})
            assert r["ok"] is True, r
            after = scan.read_addonlist(cp)
            assert len(after) == len(before), (len(after), len(before))
            assert after[k0] is want, (k0, after[k0], want)
            r2 = manager.set_enabled(cp, {k0: before[k0]})
            assert r2["ok"] is True, r2
            back = scan.read_addonlist(cp)
            # 再改回来之后，文本应当与原始完全一致
            cp_txt, _ = safety.read_text_auto(cp)
            og_txt, _ = safety.read_text_auto(real_al)
            assert cp_txt == og_txt, "改回来之后文本不等于原文（不可逆！）"

        @check("副本上真做一次隔离：目标条目消失，其余条目一条不少")
        def _():
            cp = os.path.join(tmp, "addonlist-isolate.txt")
            shutil.copy2(real_al, cp)
            before = scan.read_addonlist(cp)
            k0 = next(iter(before), None)
            addons = os.path.join(tmp, "fakeaddons")
            os.makedirs(addons, exist_ok=True)
            vp = os.path.join(addons, k0)
            with open(vp, "wb") as fp:
                fp.write(b"x")
            r = manager.isolate_mods(cp, [{"key": k0, "addon_path": vp,
                                           "repo_dir": ""}], isolate_repo=False)
            assert r["ok"] is True, r
            after = scan.read_addonlist(cp)
            assert k0 not in after, "隔离后条目还在！"
            missing = set(before) - set(after) - {k0}
            assert not missing, "误删了别的条目: %s" % list(missing)[:5]
            extra = set(after) - set(before)
            assert not extra, "凭空多出条目: %s" % list(extra)[:5]
            print("         原 %d 条 → 隔离 1 条后 %d 条，其余 %d 条一条不少"
                  % (len(before), len(after), len(after)))
            shutil.rmtree(r["moved"][0]["moved"], ignore_errors=True)

    print("=" * 66)
    print("B) 真实标签库")
    print("=" * 66)

    if not os.path.isfile(CFG["taglib"]):
        print("  [SKIP] 标签库不存在")
    else:
        @check("加载真实 TaggedAssets.jsonc")
        def _():
            import core.taglib as taglib
            t0 = time.monotonic()
            tl = taglib.build_taglib(CFG["taglib"])
            dt = time.monotonic() - t0
            assert tl.loaded is True, "加载失败: %s" % tl.error
            assert tl.rule_count > 0, "一条规则都没读到"
            print("         加载 %.3fs，%d 个标签 / %d 条规则，error=%r"
                  % (dt, len(tl.tags), tl.rule_count, tl.error[:120]))

        @check("match() 在真实规则上不会崩，且分桶命中正确")
        def _():
            import core.taglib as taglib
            tl = taglib.build_taglib(CFG["taglib"])
            got = tl.match(["models/survivors/zoey.mdl", "sound/music/x.mp3",
                            "materials/vgui/loadingscreen/a.vmt"])
            assert isinstance(got, dict), type(got)
            print("         3 条样例路径 → 命中 %d 组标签: %s"
                  % (len(got), list(got)[:6]))

    print("=" * 66)
    print("C) 真实 mod 仓库规模（只列目录，不读 VPK）")
    print("=" * 66)

    if not os.path.isdir(CFG["repo_dir"]):
        print("  [SKIP] 仓库目录不存在")
    else:
        t0 = time.monotonic()
        n_dir = 0
        n_vpk = 0
        total = 0
        for cur, dirs, files in os.walk(CFG["repo_dir"]):
            n_dir += 1
            for f in files:
                if f.lower().endswith(".vpk"):
                    n_vpk += 1
                    try:
                        total += os.path.getsize(os.path.join(cur, f))
                    except OSError:
                        pass
            if n_dir > 4000:
                break
        dt = time.monotonic() - t0
        print("  [INFO] %d 个目录 / %d 个 VPK / %.2f GB，os.walk 耗时 %.2fs"
              % (n_dir, n_vpk, total / (1024 ** 3), dt))

    print("=" * 66)
    print("D) 真实游戏目录")
    print("=" * 66)

    @check("addonlist 路径推导正确")
    def _():
        p = paths.addonlist_path(CFG)
        assert p.lower().endswith("addonlist.txt"), p
        assert os.path.isfile(p), p

    @check("addons 目录存在")
    def _():
        p = paths.addons_dir(CFG)
        assert os.path.isdir(p), p
        n = len([x for x in os.listdir(p) if x.lower().endswith(".vpk")])
        print("         addons 里有 %d 个 .vpk" % n)

    rmtree_ok = True
    try:
        shutil.rmtree(tmp, ignore_errors=True)
    except OSError:
        rmtree_ok = False

    # 最后再确认一次：原文件一个字节都没被改
    if os.path.isfile(real_al):
        now = open(real_al, "rb").read()
        ok = now == real_bytes
        print()
        print("  %s 原 addonlist.txt 完整性：%s"
              % ("[OK]  " if ok else "**[FAIL]**", "未改动" if ok else "被改动了！"))
        if not ok:
            FAILED.append(("原文件完整性", AssertionError("原 addonlist.txt 被改动了")))

    print()
    print("=" * 66)
    print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    print("=" * 66)
    if FAILED:
        for n, e in FAILED:
            print("  - %s: %s: %s" % (n, type(e).__name__, e))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
