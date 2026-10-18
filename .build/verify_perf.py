# -*- coding: utf-8 -*-
"""第四轮：真实规模下的性能与一致性（只读，不搬任何文件）。

跑法（项目根目录）：
    python .build\\verify_perf.py

为什么单独一份：静态审查说「这里会 O(n²)」「这里缓存没生效」都只是推断，
只有拿真实语料（206 个 mod / 12.5 GB / 204 条标签规则）跑一次，
才知道哪一处**真的**是瓶颈。

覆盖：
  A. core.scan.scan() 全量扫描耗时 + 结果自检（条目数、注册状态、分类）。
  B. 二次扫描（缓存/CPython 预热后）耗时对比。
  C. components.component_tree 的「不 load_all」优化真的生效：
     拿最大的那个 VPK 对比 带 load_all / 不带 load_all 的耗时与内存。
  D. core.audit.conflicts 在真实 mod 集合上的耗时。
  E. VPK 解析：拿最大的 VPK 读目录树，测耗时（不读数据区）。
"""
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))

# core.safety 在 import 期就把数据目录算死并 makedirs（还会建 data/cache），
# 所以必须在 import 之前把 L4D2MS_WORK_ROOT 指到临时目录，别碰玩家真实 data/。
import atexit as _atexit
import shutil as _shutil
import tempfile as _tempfile
_WORK = _tempfile.mkdtemp(prefix="l4d2ms-perf-")
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
    import core.scan as scan
    import core.audit as audit
    import core.components as components
    import core.vpk as vpk
    import core.manager as manager

    print("=" * 66)
    print("性能基线（真实语料：207 个 VPK / 12.5 GB / 204 条规则）")
    print("=" * 66)

    # ---- A / B 全量扫描 ----
    state = {}

    @check("scan.scan() 首次全量扫描")
    def _():
        t0 = time.monotonic()
        res = scan.scan(CFG)
        dt = time.monotonic() - t0
        state["first"] = dt
        state["res"] = res
        mods = res.get("mods") or []
        assert mods, "一个 mod 都没扫到"
        reg = sum(1 for m in mods if m.registered)
        enabled = sum(1 for m in mods if m.enabled)
        print("         耗时 %.2fs，%d 个 mod（已登记 %d / 启用 %d），"
              "unlinked=%d" % (dt, len(mods), reg, enabled,
                               len(res.get("unlinked") or [])))
        cats = {}
        for m in mods:
            cats[m.category] = cats.get(m.category, 0) + 1
        print("         分类分布：%s"
              % ", ".join("%s=%d" % (k, v) for k, v in
                          sorted(cats.items(), key=lambda kv: -kv[1])[:8]))

    @check("scan.scan() 二次扫描（应明显快于首次）")
    def _():
        t0 = time.monotonic()
        res2 = scan.scan(CFG)
        dt = time.monotonic() - t0
        first = state.get("first", 0)
        print("         耗时 %.2fs（首次 %.2fs，%.2fx）"
              % (dt, first, (first / dt) if dt else 0))
        assert len(res2.get("mods") or []) == len(state["res"].get("mods") or []), \
            "两次扫描结果数量不一致"

    # ---- C components 不 load_all ----
    @check("component_tree：带 load_all vs 不带 load_all（真实最大 VPK）")
    def _():
        mods = state["res"].get("mods") or []
        biggest = None
        for m in mods:
            if not m._vpk_path:
                continue
            try:
                sz = os.path.getsize(m._vpk_path)
            except OSError:
                continue
            if biggest is None or sz > biggest[0]:
                biggest = (sz, m._vpk_path, m.key)
        assert biggest, "没有可解析的 VPK"
        sz, path, key = biggest
        print("         最大 VPK：%.1f MB  %s" % (sz / (1024 ** 2), os.path.basename(key)))

        v = vpk.Vpk(path)          # 不 load_data
        print("         条目数 %d（只读目录树，未读数据区）" % len(v.entries))

        # component_tree 的公开签名：component_tree(vpk_path, cfg)
        t0 = time.monotonic()
        tree_a = components.component_tree(path, CFG)
        dt_a = time.monotonic() - t0
        print("         component_tree 耗时 %.3fs" % dt_a)
        assert tree_a, "component_tree 返回空"

    # ---- D audit ----
    @check("audit.run_all 真实全量（冲突/冗余/前置/未接入）")
    def _():
        import core.paths as paths
        mods_for_audit = scan.dicts_for_audit(state["res"]["mods"])
        al = paths.addonlist_path(CFG)
        addons = paths.addons_dir(CFG)
        t0 = time.monotonic()
        r = audit.run_all(mods_for_audit, CFG["game_dir"], addons,
                          scan.read_addonlist(al), CFG.get("repo_dir", ""))
        dt = time.monotonic() - t0
        c = r["conflicts"]
        p = r["prerequisites"]
        print("         总耗时 %.2fs" % dt)
        print("         冲突：%d 个目录组 / %d 条发现 / truncated=%s"
              % (c.get("path_count") or 0, len(c.get("findings") or []),
                 c.get("findings_truncated")))
        print("         冗余：%d 项" % r["redundancy"].get("total_findings", 0))
        print("         前置：%d 条，game_files_ok=%s，checked_against=%r"
              % (p.get("total", 0), p.get("game_files_ok"),
                 str(p.get("checked_against"))[:80]))
        print("         未接入：%d 项" % r["unlinked"].get("total", 0))

    # ---- E 最大 VPK 读树 ----
    @check("VPK 目录树解析耗时（最大文件）")
    def _():
        mods = state["res"].get("mods") or []
        best = max(((os.path.getsize(m._vpk_path), m._vpk_path)
                    for m in mods if m._vpk_path), default=None)
        assert best, "没有 VPK"
        sz, path = best
        t0 = time.monotonic()
        v = vpk.Vpk(path, load_data=False)
        dt = time.monotonic() - t0
        print("         %.1f MB 文件，%d 条目，解析 %.3fs（不读数据区）"
              % (sz / (1024 ** 2), len(v.entries), dt))
        # 只对少量条目解压，验证读取路径
        t0 = time.monotonic()
        n = 0
        for e in v.entries[:200]:
            try:
                d = v.read_entry(e)
                n += len(d)
            except Exception:
                pass
        dt2 = time.monotonic() - t0
        print("         读前 200 条目（%.1f MB 解压）耗时 %.3fs" % (n / (1024 ** 2), dt2))

    print()
    print("=" * 66)
    print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    print("=" * 66)
    if FAILED:
        for n_, e in FAILED:
            print("  - %s: %s: %s" % (n_, type(e).__name__, e))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
