# -*- coding: utf-8 -*-
"""本轮审查改动的离线自检（不需要游戏目录、不联网、不碰玩家数据）。

为什么有这份脚本：这次改动是在**无法运行 python 的环境**里做的
（ACL 沙箱在授予工作区写权限那一步就失败：SetNamedSecurityInfoW Win32 5），
所以只能先写一份一键可跑的验证，等环境恢复后立刻把「静态看对了」
升级为「实测对了」。

跑法（项目根目录）：
    python .build\\verify_review.py

做这几件事：
  1. 编译 + 导入：core / app 下全部 .py；
  2. addonlist 相关纯函数（去重、空输入、真写入）；
  3. VPK 单文件读写往返（含 entry_length 不把 preload 算进去）；
  4. 隔离流程四种情形：文件不存在 / 真文件 / addons 里的断链 / 还原回原位；
  5. read_body 的两处加固（非法 Content-Length、顶层不是对象）。
全部用临时目录，跑完自动清理。退出码 0 = 全过，1 = 有失败。

⚠️ 副作用提醒：import core.safety 时它会 `os.makedirs(data/…)`，所以本脚本
   会在**项目目录**下建出 data/{backup,staging,cache}。隔离相关检查用的是真
   safety.isolate()（要验证边车格式），产物会落到 data/staging/ 里，脚本
   在每条检查内部用 _rmtree() 删掉自己造的那几项。
"""
import os
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))

# core.safety 在 import 期就把数据目录算死并 makedirs，所以必须在 import 之前
# 把 L4D2MS_WORK_ROOT 指到临时目录；否则本脚本的备份/隔离检查会把产物写进
# 玩家真实的 data/（2026-10-18 那次审查真的污染过，46 个备份 + 26 个隔离项）。
import atexit as _atexit
import shutil as _shutil
_WORK = tempfile.mkdtemp(prefix="l4d2ms-review-")
_atexit.register(_shutil.rmtree, _WORK, ignore_errors=True)
os.environ["L4D2MS_WORK_ROOT"] = _WORK

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


def main() -> int:
    print("=" * 66)
    print("1) 编译 + 导入")
    print("=" * 66)
    import py_compile
    files = []
    for sub in ("core", "app"):
        d = os.path.join(ROOT, sub)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".py"):
                files.append(os.path.join(d, f))
    bad = []
    for f in files:
        try:
            # ⚠️ 不能用 py_compile.compile(f, cfile=os.devnull)：
            #    Windows 上 os.devnull 是 "nul"，Python 3.12 会直接抛
            #    FileExistsError（"nul is a non-regular file ..."），
            #    于是「语法检查」这一步在 Windows 上永远失败。
            #    直接读源码 + compile() 等价且跨平台。
            with open(f, "rb") as fp:
                src = fp.read()
            compile(src, f, "exec")
        except SyntaxError as ex:
            bad.append((f, "%s: 第 %s 行 %s" % (type(ex).__name__, ex.lineno, ex.msg)))
        except (OSError, ValueError) as ex:
            bad.append((f, "%s: %s" % (type(ex).__name__, ex)))
    if bad:
        for f, e in bad:
            FAILED.append((os.path.basename(f), e))
            print("  [FAIL] 语法错误 %s\n%s" % (os.path.basename(f), e))
    else:
        PASSED.append("compile")
        print("  [OK]   编译通过：%d 个文件" % len(files))

    try:
        import core.manager as manager
        import core.safety as safety                                # noqa: F401
        import core.vpk as vpk
        import core.audit as audit                                  # noqa: F401
        import api as api_mod                                       # noqa: F401
        import http_util
        import routes                                               # noqa: F401
        PASSED.append("import")
        print("  [OK]   导入通过：core.* + app.api / http_util / routes")
    except Exception as ex:                                         # noqa: BLE001
        FAILED.append(("import", ex))
        print("  [FAIL] 导入失败：%s: %s" % (type(ex).__name__, ex))
        traceback.print_exc()
        print("\n导入都过不了，后面的行为检查没有意义。")
        return 1

    print("=" * 66)
    print("2) 行为检查（临时目录）")
    print("=" * 66)
    with tempfile.TemporaryDirectory(prefix="l4d2ms-verify-") as tmp:
        al = os.path.join(tmp, "addonlist.txt")

        def write_al(text):
            with open(al, "w", encoding="utf-8") as fp:
                fp.write(text)

        def read_al():
            with open(al, encoding="utf-8") as fp:
                return fp.read()

        def _rmtree(p):
            """隔离区产物（文件或目录）用完就删，别把临时目录越堆越大。"""
            import shutil as _sh
            try:
                if os.path.isdir(p) and not os.path.islink(p):
                    _sh.rmtree(p, ignore_errors=True)
                elif os.path.lexists(p):
                    os.remove(p)
            except OSError:
                pass

        @check("batch_by_mods：空列表 ≠ 成功")
        def _():
            write_al('"a.vpk" "1"\n"b.vpk" "0"\n')
            r = manager.batch_by_mods(al, [], True)
            assert r["ok"] is False, r
            assert r.get("error"), r

        @check("batch_by_mods：重复 key 不虚报 count")
        def _():
            write_al('"a.vpk" "1"\n"b.vpk" "0"\n')
            r = manager.batch_by_mods(al, [{"key": "a.vpk"}, {"key": "a.vpk"},
                                           {"key": "b.vpk"}], False)
            assert r["count"] == 2, r["count"]

        @check("batch_by_mods：真的写进了文件")
        def _():
            write_al('"a.vpk" "1"\n')
            r = manager.batch_by_mods(al, [{"key": "a.vpk"}], False)
            assert r["ok"] is True, r
            assert '"a.vpk" "0"' in read_al(), read_al()

        @check("set_enabled：空 changes 直接失败，不白写一次文件")
        def _():
            write_al('"a.vpk" "1"\n')
            r = manager.set_enabled(al, {})
            assert r["ok"] is False, r
            assert '"a.vpk" "1"' in read_al(), read_al()

        @check("write_vpk：单文件往返，entry_length 不含 preload")
        def _():
            e = vpk.VpkEntry(ext="txt", path="scripts", filename="x", crc=0,
                             preload_len=0, archive_index=vpk.ARCHIVE_IN_DIR,
                             entry_offset=0, entry_length=0, preload=b"", data=b"hello")
            out = os.path.join(tmp, "t.vpk")
            vpk.write_vpk(out, [e])
            v = vpk.Vpk(out, load_data=True)
            got = v.read_file("scripts/x.txt")
            assert got == b"hello", got
            assert v.entries[0].entry_length == 5, v.entries[0].entry_length

        @check("isolate_mods：找不到实体文件的 key 进 failed，且不摘 addonlist 条目")
        def _():
            write_al('"ghost.vpk" "1"\n')
            mods = [{"key": "ghost.vpk",
                     "addon_path": os.path.join(tmp, "addons", "ghost.vpk"),
                     "repo_dir": ""}]
            r = manager.isolate_mods(al, mods, isolate_repo=False)
            assert r["ok"] is False, r
            assert r["moved"] == [], r["moved"]
            assert any(f.get("key") == "ghost.vpk" for f in r["failed"]), r["failed"]
            assert '"ghost.vpk"' in read_al(), read_al()

        @check("isolate_mods：真文件能搬走，条目被摘掉")
        def _():
            addons = os.path.join(tmp, "addons")
            os.makedirs(addons, exist_ok=True)
            vp = os.path.join(addons, "real.vpk")
            with open(vp, "wb") as fp:
                fp.write(b"x")
            write_al('"real.vpk" "1"\n')
            r = manager.isolate_mods(al, [{"key": "real.vpk", "addon_path": vp,
                                           "repo_dir": ""}], isolate_repo=False)
            assert r["ok"] is True, r
            assert len(r["moved"]) == 1, r["moved"]
            assert not os.path.exists(vp), "原文件应当已被搬进隔离区"
            assert '"real.vpk"' not in read_al(), read_al()
            # 隔离区是真的落在磁盘上的，别把临时目录撑大 —— 删掉它
            _rmtree(r["moved"][0]["moved"])

        @check("isolate_mods：addons 里的断链能被清掉，而不是报「找不到实体文件」")
        def _():
            addons = os.path.join(tmp, "addons")
            os.makedirs(addons, exist_ok=True)
            dead_target = os.path.join(tmp, "dead-target.vpk")
            with open(dead_target, "wb") as fp:
                fp.write(b"x")
            link = os.path.join(addons, "broken.vpk")
            try:
                os.symlink(dead_target, link)
            except (OSError, NotImplementedError):
                return      # 没有创建符号链接的权限（Windows 未开发者模式）就跳过
            os.remove(dead_target)          # 目标没了，link 成为断链
            assert not os.path.exists(link) and os.path.lexists(link), "前提不成立"
            write_al('"broken.vpk" "1"\n')
            r = manager.isolate_mods(al, [{"key": "broken.vpk", "addon_path": link,
                                           "repo_dir": ""}], isolate_repo=False)
            assert r["ok"] is True, r
            assert not os.path.lexists(link), "断链应当被清掉"
            _rmtree(r["moved"][0]["moved"])

        @check("restore_from_staging：边车里只有 original_path，也能自动还原回原位")
        def _():
            # 回归测试：restore_from_staging 原先读的是 org["original_dir"]，
            # 而 safety.isolate() 写的边车只有 original_path / original_name /
            # isolated_at / tag —— original_dir 是 list_staging() 现算给前端看的。
            # 于是「不传 target_dir」时永远拿不到目录，白返回 need_target。
            addons = os.path.join(tmp, "addons")
            os.makedirs(addons, exist_ok=True)
            orig = os.path.join(addons, "restorable.vpk")
            with open(orig, "wb") as fp:
                fp.write(b"y")
            staged = safety.isolate(orig, tag="link")
            assert not os.path.exists(orig), "前提：原文件应已被搬进隔离区"
            r = manager.restore_from_staging(al, staged, None)
            assert r.get("ok") is True, r
            assert r.get("need_target") is not True, r
            assert os.path.isfile(orig), "应当自动还原到原路径 %s" % orig
            assert r["restored_to"] == orig, (r["restored_to"], orig)

        @check("http_util.read_body：Content-Length 非法 → 400（不是 500）")
        def _():
            class H:
                close_connection = False
                headers = {"Content-Length": "abc"}

                class rfile:
                    @staticmethod
                    def read(n):
                        return b""

            sent = []
            orig = http_util.send_err
            http_util.send_err = lambda h, msg, code=400, detail="": sent.append(code)
            try:
                got = http_util.read_body(H())
            finally:
                http_util.send_err = orig
            assert sent == [400], sent
            assert got == {}, got

        @check("http_util.read_body：顶层是数组 → 空 dict（不是 list）")
        def _():
            body = b"[1,2]"

            class H:
                close_connection = False
                headers = {"Content-Length": str(len(body))}

                class rfile:
                    @staticmethod
                    def read(n):
                        return body[:n]

            assert http_util.read_body(H()) == {}, "非对象 JSON 必须归一成 {}"

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
