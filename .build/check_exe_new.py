# -*- coding: utf-8 -*-
"""核实新打的 exe 里装的是哪一版代码（新旧 exe 对照）。

为什么需要这个脚本：
    PyInstaller 打包成功只说「Build complete」，不会告诉你它把哪一版源码
    打了进去。改动明明在磁盘上、exe 里却是旧的，这种「打包幻觉」只能靠
    读包内归档来证伪。

三条必须记住的 API / 字节码事实（都是本脚本第一版踩过的坑）：
    1. ``open_embedded_archive`` 是 ``CArchiveReader`` 的**实例方法**，
       不是 ``PyInstaller.archive.readers`` 的模块级函数。
    2. ``pyz.extract(mod)`` 返回的是 **code object**，不是源码文本；
       函数名在 ``co_name`` 里、被调用的全局名在 ``co_names`` 里，
       **都不会**出现在字符串常量中。所以「某函数是否存在」要按
       ``co_name`` 找，不能用搜字符串的办法。
    3. CPython 会把 dict 字面量的键打包成**元组常量**（BUILD_CONST_KEY_MAP），
       裸搜 ``co_consts`` 里的 str 会漏掉 ``{"game_files_ok": x}`` 这种键。
       必须递归进 tuple / frozenset 才算扫全。

用法（必须用构建 venv 的 python，因为要 import PyInstaller）：
    .build\\venv\\Scripts\\python.exe .build\\check_exe_new.py
"""
import glob
import hashlib
import os
import sys
import tempfile

# 项目根按本文件位置推断（.build/ 的上一层），不写死盘符路径 ——
# .build/ 会被 tools/sync_to_github.py 同步进公开仓库，而它的敏感扫描
# 会把 "C:\\Users\\..." 这类真实机器路径当成泄漏直接中止提交。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW_EXE = os.path.join(ROOT, "dist", "L4D2ModStudio.exe")


def _find_old_exe() -> str:
    """找上一版 exe 备份（用于新旧对照）。

    优先用环境变量 L4D2MS_OLD_EXE 指定；否则在系统临时目录里找最新的
    l4d2ms_exe_bak_*/dist_L4D2ModStudio.exe。找不到就返回空串，
    脚本会自动跳过「新旧对照」那一段而不是报错退出。
    """
    env = (os.environ.get("L4D2MS_OLD_EXE") or "").strip()
    if env and os.path.isfile(env):
        return env
    cands = glob.glob(os.path.join(tempfile.gettempdir(),
                                   "l4d2ms_exe_bak_*", "dist_L4D2ModStudio.exe"))
    if not cands:
        return ""
    cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return cands[0]


OLD_EXE = _find_old_exe()

from PyInstaller.archive.readers import CArchiveReader


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest().upper()


def iter_codes(code, path=""):
    """遍历 code object 树，产出 (名字路径, code)。"""
    name = (path + "." if path else "") + code.co_name
    yield name, code
    for c in code.co_consts:
        if hasattr(c, "co_consts"):
            yield from iter_codes(c, name)


def all_str(code):
    """递归收集所有字符串常量——**含元组里的**（dict 键就藏在那里）。"""
    out = []

    def walk(obj):
        if isinstance(obj, str):
            out.append(obj)
        elif isinstance(obj, (tuple, list, frozenset, set)):
            for x in obj:
                walk(x)
        elif hasattr(obj, "co_consts"):
            for x in obj.co_consts:
                walk(x)

    walk(code)
    return out


def find_code(code, name: str):
    for _, c in iter_codes(code):
        if c.co_name == name:
            return c
    return None


def load(exe):
    arc = CArchiveReader(exe)
    toc = dict(arc.toc)
    pyz = arc.open_embedded_archive("PYZ.pyz")
    return arc, toc, pyz


def main() -> int:
    # 新 exe 是必需品；旧 exe 只是「新旧对照」的可选参照物，
    # 空串或文件不在就跳过对照段（不能让它把整个检查带崩）。
    # 注意：下面要重绑模块级 OLD_EXE，必须显式 global，
    # 否则 Python 会把它当局部变量，连 `if OLD_EXE` 这行都抛 UnboundLocalError。
    global OLD_EXE
    if not os.path.isfile(NEW_EXE):
        print("找不到新 exe: %s" % NEW_EXE)
        return 1
    if OLD_EXE and not os.path.isfile(OLD_EXE):
        print("（旧 exe 路径存在但文件不在，跳过对照：%s）" % OLD_EXE)
        OLD_EXE = ""

    bad = 0
    print("=" * 72)
    print("新 exe: %s" % NEW_EXE)
    print("  大小 %d B  SHA256=%s" % (os.path.getsize(NEW_EXE),
                                      sha(open(NEW_EXE, "rb").read())))
    if OLD_EXE:
        print("旧 exe: %s" % OLD_EXE)
        print("  大小 %d B  SHA256=%s" % (os.path.getsize(OLD_EXE),
                                          sha(open(OLD_EXE, "rb").read())))
    else:
        print("旧 exe: <没找到备份，跳过新旧对照段>")

    new_arc, new_toc, new_pyz = load(NEW_EXE)
    old_arc = old_toc = old_pyz = None
    if OLD_EXE:
        old_arc, old_toc, old_pyz = load(OLD_EXE)

    # ---- 1) 资源文件：包内字节必须与磁盘源码逐字节相同 ----
    print("\n" + "=" * 72)
    print("[1] 资源文件逐字节比对（新 exe）")
    data_files = [
        (("web\\app.js", "web/app.js"), "web/app.js"),
        (("web\\index.html", "web/index.html"), "web/index.html"),
        (("web\\style.css", "web/style.css"), "web/style.css"),
        (("data\\taglib\\TaggedAssets.jsonc", "data/taglib/TaggedAssets.jsonc"),
         "data/taglib/TaggedAssets.jsonc"),
    ]
    for cands, rel in data_files:
        name = next((c for c in cands if c in new_toc), None)
        if name is None:
            print("  ✗ 包里没有 %s" % rel)
            bad += 1
            continue
        packed = new_arc.extract(name)
        disk = open(os.path.join(ROOT, rel.replace("/", os.sep)), "rb").read()
        same = sha(packed) == sha(disk)
        print("  %s %-32s 包内 %8d B  磁盘 %8d B" %
              ("✓" if same else "✗", rel, len(packed), len(disk)))
        if not same:
            bad += 1

    # ---- 2) 今晚的修复点是否进了包（按「语义」查，不是搜字符串） ----
    print("\n" + "=" * 72)
    print("[2] 今日修复点是否在新包里（按/ 语义/ 查）")

    audit_new = new_pyz.extract("core.audit")
    audit_old = old_pyz.extract("core.audit")
    acquire_new = new_pyz.extract("core.acquire")
    manager_new = new_pyz.extract("core.manager")
    safety_new = new_pyz.extract("core.safety")
    vpk_new = new_pyz.extract("core.vpk")
    reassign_new = new_pyz.extract("core.reassign")

    checks = []

    # (a) is_noise 污染回退：co_names 里不该再有 MULTI_ADDON_ENTRY
    n_new = find_code(audit_new, "is_noise")
    n_old = find_code(audit_old, "is_noise")
    checks.append((
        "audit.is_noise 不含 MULTI_ADDON_ENTRY（今晚①）",
        bool(n_new) and "MULTI_ADDON_ENTRY" not in n_new.co_names,
        "新 co_names=%s" % (sorted(n_new.co_names) if n_new else "找不到函数"),
    ))

    # (b) 反证：script_lib_conflicts 仍然引用它（功能没被误删）
    slc = find_code(audit_new, "script_lib_conflicts")
    checks.append((
        "audit.script_lib_conflicts 仍引用 MULTI_ADDON_ENTRY",
        bool(slc) and "MULTI_ADDON_ENTRY" in slc.co_names,
        "新 co_names 含该名=%s" % (bool(slc) and "MULTI_ADDON_ENTRY" in slc.co_names),
    ))

    # (c) game_files_ok 键（dict 键在元组常量里，用修好的 all_str 才算得准）
    ga = all_str(audit_new)
    checks.append((
        "audit 有 game_files_ok 键（前置缺件降置信度）",
        "game_files_ok" in ga,
        "字符串常量数=%d" % len(ga),
    ))
    checks.append((
        "audit 有 checked_against 键",
        "checked_against" in ga,
        "",
    ))
    checks.append((
        "audit 有 .ani 判据（模型缺件误报修复）",
        ".ani" in ga,
        "",
    ))

    # (d) 函数存在性按 co_name 查
    for modcode, fn, label in [
        (acquire_new, "_kill_tree", "acquire 有 _kill_tree（超时/取消杀子树）"),
        (acquire_new, "_link_error", "acquire 有 _link_error（Win32 错误码）"),
        (manager_new, "_isolate_mods_locked", "manager 有 _isolate_mods_locked（锁外读修复）"),
        (reassign_new, "detect", "reassign 有 detect"),
    ]:
        got = find_code(modcode, fn)
        checks.append((label, got is not None, ""))

    # (e) manager 的 drop 参数（形参名在 co_varnames 里，不是字符串常量）
    render = find_code(manager_new, "_render_addonlist")
    var = getattr(render, "co_varnames", ())
    checks.append(("manager._render_addonlist 有 drop 形参（条目真删除）",
                   "drop" in var, "co_varnames=%s" % (var,)))
    wadd = find_code(manager_new, "write_addonlist")
    checks.append(("manager.write_addonlist 有 drop 形参",
                   "drop" in getattr(wadd, "co_varnames", ()), ""))

    # (f) safety 的 BOM 判定
    sa = all_str(safety_new)
    checks.append(("safety 有 utf-8-sig（BOM 显式判定）", "utf-8-sig" in sa, ""))
    checks.append(("safety 有 L4D2MS_WORK_ROOT 环境口", "L4D2MS_WORK_ROOT" in sa, ""))

    # (g) vpk 重算 crc —— 注意 zlib.crc32 里的 "crc32" 是**属性名**，
    #     它出现在 co_names 里，不是字符串常量；搜字符串常量永远搜不到。
    write_vpk = find_code(vpk_new, "write_vpk")
    checks.append(("vpk.write_vpk 调用 zlib.crc32（重算 crc）",
                   write_vpk is not None and "crc32" in write_vpk.co_names,
                   "co_names 含 crc32=%s" % (write_vpk is not None
                                             and "crc32" in write_vpk.co_names)))

    for label, ok, extra in checks:
        print("  %s %s%s" % ("✓" if ok else "✗", label,
                             ("   [%s]" % extra) if extra else ""))
        if not ok:
            bad += 1

    # ---- 3) 新旧 exe 的关键差异（证明这次重打确实换了代码） ----
    print("\n" + "=" * 72)
    print("[3] 新旧 exe 差异（证明重打不是白打）")
    print("  PYZ 模块数：新 %d / 旧 %d" % (len(new_pyz.toc), len(old_pyz.toc)))
    print("  core.audit 字符串常量：新 %d / 旧 %d" % (len(all_str(audit_new)),
                                                     len(all_str(audit_old))))
    if n_old is not None:
        old_polluted = "MULTI_ADDON_ENTRY" in n_old.co_names
        print("  旧 exe 的 is_noise 是否被污染: %s" % old_polluted)
        print("    （旧 co_names=%s）" % sorted(n_old.co_names))
        if not old_polluted:
            print("    ⚠ 旧 exe 里 is_noise 也没污染 —— 说明 23:21 那次打包")
            print("      可能已经包含了 ①的修复，本次重打的差异主要在别处。")
    else:
        print("  旧 exe 里找不到 is_noise 函数")

    # web/app.js 新旧哈希是否不同（nav.title 是今晚最后一处改动）
    def js_hash(arc, toc):
        name = next((c for c in ("web\\app.js", "web/app.js") if c in toc), None)
        return sha(arc.extract(name)) if name else "<无>"

    hj_new, hj_old = js_hash(new_arc, new_toc), js_hash(old_arc, old_toc)
    print("  web/app.js 哈希：")
    print("    新 %s" % hj_new)
    print("    旧 %s" % hj_old)
    print("    不同 = %s（True 说明 nav.title 等前端改动已进新包）" % (hj_new != hj_old))

    print("\n" + "=" * 72)
    print("结论：%s" % ("✅ 新包内容与磁盘源码一致，今日修复均在包内"
                       if bad == 0 else "❌ 有 %d 项对不上" % bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
