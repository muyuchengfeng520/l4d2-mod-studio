# -*- coding: utf-8 -*-
"""audit 引擎的离线自检（构造用例，不需要游戏目录、不联网、不碰玩家数据）。

为什么有这份脚本：`script_lib_conflicts()` 是本机**当前跑出 0 处**的功能
（真实语料里没有两套同名脚本库同时挂着），所以没法用真实数据自证它到底
还对不对；同理，`is_noise()` 被顺手塞进 `MULTI_ADDON_ENTRY` 那次事故
（覆盖冲突 455 -> 453）也不会有任何真实数据能稳定复现。这类「真实语料
给不出证据」的判据，只能靠构造用例把它钉死——一旦有人再犯，脚本立刻红。

跑法（项目根目录）：
    python .build\\verify_audit.py

覆盖：
  A. is_noise 污染防护（入口脚本不许被当成噪声；addonimage 仍照旧排除）；
  B. script_lib_conflicts 的判据（同目录 >=2 个 .nut 才算；入口脚本要排除；
     不同 mod 组合分开算；only_enabled 开关生效）；
  C. 结构守卫（AST 层面：MULTI_ADDON_ENTRY 只能出现在 script_lib_conflicts 里）；
  D. conflicts 的 PAIR_CAP 提前判（组合爆炸不能先跑完再判）；
  E. run_all 的返回键（原有 4 键不许改名）。

退出码 0 = 全过，1 = 有失败。
"""
import ast
import os
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))

# core.safety 在 import 期就算死数据目录并 makedirs，必须先隔离再 import，
# 否则断言里造的备份会落进玩家真实 data/（2026-10-18 污染过 72 个文件）。
import atexit as _atexit
import shutil as _shutil
_WORK = tempfile.mkdtemp(prefix="l4d2ms-audit-")
_atexit.register(_shutil.rmtree, _WORK, ignore_errors=True)
os.environ["L4D2MS_WORK_ROOT"] = _WORK

from core import audit  # noqa: E402

PASSED = []
FAILED = []


def check(name):
    """跑一个检查项；断言失败/异常记入 FAILED，不中断后续检查。"""
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


def mod(key: str, vpaths: list, enabled: bool = True) -> dict:
    """构造一个 scan 风格的 mod 条目（只给 audit 真正读到的键）。"""
    return {"key": key, "title": key, "enabled": enabled,
            "_vpaths": vpaths, "_vpk_path": os.path.join(_WORK, key)}


def entry_refs(tree: ast.AST) -> dict:
    """按作用域收集 MULTI_ADDON_ENTRY 的「读取」引用：{"<模块级>" 或函数名: [行号]}。

    两个刻意的设计：
      1. 只认 ast.Load —— 常量定义处（`MULTI_ADDON_ENTRY = re.compile(...)`）是 Store，
         不该算「引用」，否则守卫会把自己的定义判成越界。
      2. 按作用域分组、且把函数外的引用归到 "<模块级>" —— 旧写法只遍历 FunctionDef，
         模块级引用会被整个漏掉；而一旦漏掉，报错里只会显示 {} ，看起来像
         「这个常量压根没被引用」，把排查方向指反了。
    """
    refs = {}

    def scan(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scan(child, child.name)      # 进入函数，作用域名换成函数名
                continue
            if (isinstance(child, ast.Name) and child.id == "MULTI_ADDON_ENTRY"
                    and isinstance(child.ctx, ast.Load)):
                refs.setdefault(scope, []).append(child.lineno)
            scan(child, scope)

    scan(tree, "<模块级>")
    return refs


def main() -> int:
    # ---------------------------------------------------------------- A
    print("=" * 66)
    print("A) is_noise 污染防护")
    print("=" * 66)

    @check("is_noise：入口脚本 director_base_addon.nut 不是噪声")
    def _():
        # 这是 455 -> 453 那次事故的核心：入口脚本被 is_noise 吃掉，
        # 于是 conflicts() 少报两条；它只该由 script_lib_conflicts() 自己排除。
        assert audit.is_noise("scripts/vscripts/director_base_addon.nut") is False
        assert audit.is_noise("scripts/vscripts/scriptedmode_addon.nut") is False
        assert audit.is_noise("scripts/vscripts/mapspawn_addon.nut") is False

    @check("is_noise：addonimage / sound.cache 仍照旧判为噪声")
    def _():
        assert audit.is_noise("addonimage.jpg") is True
        assert audit.is_noise("sound/sound.cache") is True
        assert audit.is_noise("materials/vgui/x.png") is True

    @check("conflicts：两个 mod 都带 director_base_addon → 仍报 1 条（不许丢）")
    def _():
        ms = [
            {"key": "A", "title": "A", "enabled": True, "_vpk_path": "a",
             "_vpaths": ["scripts/vscripts/director_base_addon.nut", "models/x.mdl"]},
            {"key": "B", "title": "B", "enabled": True, "_vpk_path": "b",
             "_vpaths": ["scripts/vscripts/director_base_addon.nut", "models/x.mdl"]},
        ]
        r = audit.conflicts(ms)
        # 两条资源（入口脚本 + models/x.mdl）都被两个 mod 提供 = 2 条冲突。
        # 如果 is_noise 又把入口脚本吃了，这里会掉成 1 —— 正是要钉住的地方。
        assert r["path_count"] == 2, r["path_count"]
        paths = {it["path"] for it in r["items"]}
        assert "scripts/vscripts/director_base_addon.nut" in paths, paths

    @check("conflicts：只剩噪声资源时不产生冲突")
    def _():
        ms = [
            {"key": "A", "title": "A", "enabled": True, "_vpk_path": "a",
             "_vpaths": ["addonimage.jpg"]},
            {"key": "B", "title": "B", "enabled": True, "_vpk_path": "b",
             "_vpaths": ["addonimage.jpg"]},
        ]
        assert audit.conflicts(ms)["path_count"] == 0

    # ---------------------------------------------------------------- B
    print("=" * 66)
    print("B) script_lib_conflicts 判据")
    print("=" * 66)

    VSLIB = ["scripts/vscripts/vslib/%s.nut" % n
             for n in ("vslib", "utils", "timer", "update")]

    @check("同目录 2 个 .nut 被同组 mod 提供 → 1 组致命冲突")
    def _():
        ms = [
            {"key": "A", "title": "A", "enabled": True, "_vpk_path": "a",
             "_vpaths": VSLIB[:2]},
            {"key": "B", "title": "B", "enabled": True, "_vpk_path": "b",
             "_vpaths": VSLIB[:2]},
        ]
        r = audit.script_lib_conflicts(ms)
        assert r["total"] == 1, r
        g = r["groups"][0]
        assert g["file_count"] == 2, g
        assert g["dir"] == "scripts/vscripts/vslib", g["dir"]
        assert g["severity"] == "致命", g
        assert sorted(g["mods"]) == ["A", "B"], g["mods"]
        assert g["files"] == sorted(VSLIB[:2]), g["files"]

    @check("入口脚本被排除：只有 director_base_addon 共享 → 0 组")
    def _():
        entry = "scripts/vscripts/director_base_addon.nut"
        ms = [
            {"key": "A", "enabled": True, "_vpk_path": "a", "_vpaths": [entry]},
            {"key": "B", "enabled": True, "_vpk_path": "b", "_vpaths": [entry]},
        ]
        r = audit.script_lib_conflicts(ms)
        assert r["total"] == 0, r

    @check("单文件共享不算库冲突（LIB_CONFLICT_MIN_FILES=2）")
    def _():
        ms = [
            {"key": "A", "enabled": True, "_vpk_path": "a", "_vpaths": [VSLIB[0]]},
            {"key": "B", "enabled": True, "_vpk_path": "b", "_vpaths": [VSLIB[0]]},
        ]
        assert audit.script_lib_conflicts(ms)["total"] == 0
        assert audit.LIB_CONFLICT_MIN_FILES == 2

    @check("不同 mod 组合分开算（A/B 与 A/C 不能混成一条）")
    def _():
        d1 = ["scripts/vscripts/lib1/a.nut", "scripts/vscripts/lib1/b.nut"]
        d2 = ["scripts/vscripts/lib2/c.nut", "scripts/vscripts/lib2/d.nut"]
        ms = [
            {"key": "A", "enabled": True, "_vpk_path": "a", "_vpaths": d1 + d2},
            {"key": "B", "enabled": True, "_vpk_path": "b", "_vpaths": d1},
            {"key": "C", "enabled": True, "_vpk_path": "c", "_vpaths": d2},
        ]
        r = audit.script_lib_conflicts(ms)
        assert r["total"] == 2, r
        dirs = sorted(g["dir"] for g in r["groups"])
        assert dirs == ["scripts/vscripts/lib1", "scripts/vscripts/lib2"], dirs

    @check("only_enabled：默认忽略禁用 mod，传 False 才计入")
    def _():
        ms = [
            {"key": "A", "enabled": True, "_vpk_path": "a", "_vpaths": VSLIB[:2]},
            {"key": "B", "enabled": False, "_vpk_path": "b", "_vpaths": VSLIB[:2]},
        ]
        assert audit.script_lib_conflicts(ms)["total"] == 0
        assert audit.script_lib_conflicts(ms, only_enabled=False)["total"] == 1

    @check("groups 按 file_count 倒序、files 有序")
    def _():
        d1 = ["scripts/vscripts/lib1/a.nut", "scripts/vscripts/lib1/b.nut"]
        d2 = ["scripts/vscripts/lib2/%s.nut" % c for c in "abcd"]
        ms = [
            {"key": "A", "enabled": True, "_vpk_path": "a", "_vpaths": d1 + d2},
            {"key": "B", "enabled": True, "_vpk_path": "b", "_vpaths": d1 + d2},
        ]
        gs = audit.script_lib_conflicts(ms)["groups"]
        assert [g["file_count"] for g in gs] == [4, 2], gs
        for g in gs:
            assert g["files"] == sorted(g["files"]), g["files"]

    # ---------------------------------------------------------------- C
    print("=" * 66)
    print("C) 结构守卫：MULTI_ADDON_ENTRY 只能活在 script_lib_conflicts 里")
    print("=" * 66)

    @check("AST：is_noise() 函数体里不出现 MULTI_ADDON_ENTRY")
    def _():
        src = open(audit.__file__, "r", encoding="utf-8").read()
        tree = ast.parse(src)
        fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "is_noise":
                fn = node
                break
        assert fn is not None, "找不到 is_noise 定义"
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        assert "MULTI_ADDON_ENTRY" not in names, \
            "is_noise 又引用了 MULTI_ADDON_ENTRY —— 会再次污染 conflicts()"

    @check("AST：MULTI_ADDON_ENTRY 的全部引用都在 script_lib_conflicts 内")
    def _():
        src = open(audit.__file__, "r", encoding="utf-8").read()
        tree = ast.parse(src)
        refs = entry_refs(tree)          # {作用域: [行号]}，"<模块级>" 也算一个作用域

        # 模块级引用最隐蔽（谁都能拿到这个规则），单独先报，并带上行号；
        # 旧写法只遍历 FunctionDef，这种引用会被漏掉，失败信息还只剩一个 {}。
        assert "<模块级>" not in refs, (
            "MULTI_ADDON_ENTRY 在模块级被读取（第 %s 行）—— 只许在 script_lib_conflicts() "
            "内部使用；模块级引用等于把规则暴露给所有函数"
            % ", ".join(map(str, refs["<模块级>"])))

        bad = {k: v for k, v in refs.items() if k != "script_lib_conflicts"}
        detail = "；".join("%s（第 %s 行）" % (k, ", ".join(map(str, v)))
                          for k, v in sorted(bad.items()))
        assert not bad, (
            "MULTI_ADDON_ENTRY 被这些函数读取：%s；只允许 script_lib_conflicts()" % detail)

        # 反向兜底：常量在 script_lib_conflicts 里必须真的被读到，否则守卫形同虚设
        # （比如有人把规则内联成字面量、把常量删了，这里就该红）。
        assert refs.get("script_lib_conflicts"), (
            "script_lib_conflicts() 里没有读取 MULTI_ADDON_ENTRY —— 守卫已失效，"
            "确认是规则被内联/删除，还是函数被改名")

    # ---------------------------------------------------------------- D
    print("=" * 66)
    print("D) conflicts 的 PAIR_CAP 提前判")
    print("=" * 66)

    @check("700 个 mod 抢同一文件 → pairs_truncated 为 True（耗时只做兜底）")
    def _():
        p = "models/survivors/survivor_x.mdl"
        ms = [{"key": "m%d" % i, "enabled": True, "_vpk_path": "x",
               "_vpaths": [p]} for i in range(700)]
        t0 = time.monotonic()
        r = audit.conflicts(ms)
        dt = time.monotonic() - t0
        # 真判据只有一条：pairs_truncated 为 True，证明没把 C(700,2)=244650 个组合跑完。
        # 耗时只作「病态退化」兜底：阈值给到 30s，因为原来卡 10s 在慢机器/实时杀毒
        # 扫描下会假红（flaky）；而这条要抓的是分钟级退化，用不着卡得那么紧。
        assert r["pairs_truncated"] is True, "组合数 C(700,2)=244650 必须被截断"
        assert dt < 30.0, "耗时 %.1fs，远超兜底线，疑似先把组合跑完才判截断" % dt

    # ---------------------------------------------------------------- E
    print("=" * 66)
    print("E) run_all 的返回键")
    print("=" * 66)

    @check("run_all 返回 5 个键，原有 4 键名未改名")
    def _():
        d = os.path.join(_WORK, "empty")
        os.makedirs(d, exist_ok=True)
        r = audit.run_all([], "", d, {}, "")
        for k in ("conflicts", "script_lib", "redundancy", "prerequisites", "unlinked"):
            assert k in r, "缺键 %s：%s" % (k, sorted(r))
        assert "total" in r["script_lib"] and "groups" in r["script_lib"]

    print("=" * 66)
    print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    print("=" * 66)
    if FAILED:
        print("失败清单：")
        for name, ex in FAILED:
            print("  - %s: %s" % (name, ex))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
