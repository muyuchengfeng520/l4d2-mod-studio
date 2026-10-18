# -*- coding: utf-8 -*-
"""第二轮验证：拿真实语料 + 真实模块跑，专挑静态审查看不出来的地方。

跑法（项目根目录）：
    python .build\\verify_deep.py

覆盖：
  A. addonlist 真实文件往返（含 GBK 编码、注释行、行内多条目、CRLF）——
     重点验证本轮修的 drop 语义：
       * 隔离 → 条目真的从文件里消失（原实现 pop 掉 mapping 后写回，等于没删）
       * 删完之后**别的东西一条不少**（注释/非 vpk 条目/其他 mod）
  B. 行内多条目（同一行两个 "x.vpk" "1"）只删其中一个，另一个必须留着。
  C. drop 掉的 key 不许被 extra 逻辑原样追加回来。
  D. 空文件 / 只有标题行 / 没有收尾大括号的畸形文件，不能崩。
  E. vpk.write_vpk 的 crc 重算：改过 data 后写出的 crc 与内容一致（可被自身校验）。
  F. audit 的 CO-BUG 上限提前判：超大分组不会真去枚举 C(n,2)。
  G. read_body 三个边界：0 长度、非 dict、非法 JSON。
  H. restore_from_staging 的 need_target 分支（边车没有 original_path 时）。
  I. _inside 的路径穿越拦截。

⚠️ 副作用：import core.safety 会建 data/{backup,staging,cache}；本脚本全部
   在临时目录里造数据，除了那几个目录本身不往项目里写东西。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))

# core.safety 在 import 期就把数据目录算死并 makedirs，所以必须在 import 之前
# 把 L4D2MS_WORK_ROOT 指到临时目录；否则本脚本的备份/隔离检查会把产物写进
# 玩家真实的 data/（2026-10-18 那次审查真的污染过：46 个备份 + 26 个隔离项）。
import atexit as _atexit
import shutil as _shutil
import tempfile as _tempfile
_WORK = _tempfile.mkdtemp(prefix="l4d2ms-deep-")
_atexit.register(_shutil.rmtree, _WORK, ignore_errors=True)
os.environ["L4D2MS_WORK_ROOT"] = _WORK

PASSED = []
FAILED = []


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


def rmtree(p):
    try:
        if os.path.islink(p) or os.path.isfile(p):
            os.unlink(p)
        elif os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def main() -> int:
    import core.manager as manager
    import core.safety as safety
    import core.scan as scan
    import core.vpk as vpk
    import core.audit as audit
    import http_util

    tmp = tempfile.mkdtemp(prefix="l4d2ms-deep-")
    al = os.path.join(tmp, "addonlist.txt")

    def write_al(text, encoding="utf-8"):
        with open(al, "w", encoding=encoding, newline="") as fp:
            fp.write(text)

    def read_al():
        with open(al, "r", encoding="utf-8-sig", newline="") as fp:
            return fp.read()

    print("=" * 66)
    print("A) drop 语义（本轮修的 bug）")
    print("=" * 66)

    @check("隔离后条目真的从 addonlist.txt 消失（不只是内存里 pop）")
    def _():
        addons = os.path.join(tmp, "addons")
        os.makedirs(addons, exist_ok=True)
        vp = os.path.join(addons, "gone.vpk")
        with open(vp, "wb") as fp:
            fp.write(b"x")
        write_al('"AddonList"\r\n{\r\n\t"gone.vpk"\t\t"1"\r\n\t"keep.vpk"\t\t"1"\r\n}\r\n')
        r = manager.isolate_mods(al, [{"key": "gone.vpk", "addon_path": vp,
                                       "repo_dir": ""}], isolate_repo=False)
        assert r["ok"] is True, r
        txt = read_al()
        assert '"gone.vpk"' not in txt, txt
        assert '"keep.vpk"' in txt, txt          # 别人一条不少
        assert "AddonList" in txt and txt.rstrip().endswith("}"), txt
        assert "\r\n" in txt, "CRLF 必须保持，不能整文件换成 LF"
        rmtree(r["moved"][0]["moved"])

    @check("注释行 / 非 vpk 条目 / 空行在隔离后原样保留")
    def _():
        addons = os.path.join(tmp, "addons")
        os.makedirs(addons, exist_ok=True)
        vp = os.path.join(addons, "n2.vpk")
        with open(vp, "wb") as fp:
            fp.write(b"x")
        body = ('// 我手工加的注释\r\n'
                '"AddonList"\r\n{\r\n'
                '\t"keep2.vpk"\t\t"0"\r\n'
                '\t"n2.vpk"\t\t"1"\r\n'
                '// 尾注释\r\n'
                '}\r\n')
        write_al(body)
        r = manager.isolate_mods(al, [{"key": "n2.vpk", "addon_path": vp,
                                       "repo_dir": ""}], isolate_repo=False)
        assert r["ok"] is True, r
        txt = read_al()
        assert "// 我手工加的注释" in txt, txt
        assert "// 尾注释" in txt, txt
        assert '"keep2.vpk"' in txt, txt
        assert '"n2.vpk"' not in txt, txt
        rmtree(r["moved"][0]["moved"])

    @check("同一行两个条目：只删命中的那个，另一个留着")
    def _():
        addons = os.path.join(tmp, "addons")
        os.makedirs(addons, exist_ok=True)
        vp = os.path.join(addons, "a1.vpk")
        with open(vp, "wb") as fp:
            fp.write(b"x")
        write_al('"AddonList"\n{\n\t"a1.vpk" "1" "a2.vpk" "0"\n}\n')
        r = manager.isolate_mods(al, [{"key": "a1.vpk", "addon_path": vp,
                                       "repo_dir": ""}], isolate_repo=False)
        assert r["ok"] is True, r
        txt = read_al()
        assert '"a1.vpk"' not in txt, txt
        assert '"a2.vpk"' in txt, txt
        rmtree(r["moved"][0]["moved"])

    @check("drop 掉的 key 不会被 extra 逻辑追加回来")
    def _():
        addons = os.path.join(tmp, "addons")
        os.makedirs(addons, exist_ok=True)
        vp = os.path.join(addons, "ex.vpk")
        with open(vp, "wb") as fp:
            fp.write(b"x")
        write_al('"AddonList"\n{\n}\n')      # 文件里本来没有 ex.vpk
        r = manager.isolate_mods(al, [{"key": "ex.vpk", "addon_path": vp,
                                       "repo_dir": ""}], isolate_repo=False)
        txt = read_al()
        assert '"ex.vpk"' not in txt, txt
        rmtree(r["moved"][0]["moved"])

    @check("GBK 编码的 addonlist.txt：读写都保 GBK，中文名不被改坏")
    def _():
        addons = os.path.join(tmp, "addons")
        os.makedirs(addons, exist_ok=True)
        vp = os.path.join(addons, "中文.vpk")
        with open(vp, "wb") as fp:
            fp.write(b"x")
        # 两个中文名（GBK 都能编码）+ 一个 ASCII 对照
        with open(al, "w", encoding="gbk", newline="") as fp:
            fp.write('"AddonList"\n{\n\t"中文.vpk" "1"\n\t"别的模组.vpk" "1"\n}\n')
        r = manager.isolate_mods(al, [{"key": "中文.vpk", "addon_path": vp,
                                       "repo_dir": ""}], isolate_repo=False)
        raw = open(al, "rb").read()
        # 关键：写回必须还是 GBK（能用 gbk 解码，且不含 U+FFFD）
        txt = raw.decode("gbk")
        assert "\ufffd" not in txt, "GBK 被写坏了"
        assert "中文.vpk" not in txt, txt
        assert "别的模组.vpk" in txt, "没被隔离的中文条目不该丢"
        rmtree(r["moved"][0]["moved"])

    print("=" * 66)
    print("B) 畸形 addonlist 不能崩")
    print("=" * 66)

    @check("空文件：set_enabled 能建出合法表")
    def _():
        write_al("")
        r = manager.set_enabled(al, {"newmod.vpk": True})
        assert r["ok"] is True, r
        txt = read_al()
        assert '"newmod.vpk"' in txt, txt
        assert txt.strip().endswith("}"), txt

    @check("没有收尾大括号：新增条目仍能写进去，不抛异常")
    def _():
        write_al('"AddonList"\n{\n\t"old.vpk" "1"\n')
        r = manager.set_enabled(al, {"brand.vpk": True})
        assert r["ok"] is True, r
        txt = read_al()
        assert '"brand.vpk"' in txt, txt

    @check("非 UTF-8 也不炸：read_addonlist 不抛")
    def _():
        with open(al, "wb") as fp:
            fp.write(b'"AddonList"\n{\n\t"bad\xff\xfe.vpk" "1"\n}\n')
        got = scan.read_addonlist(al)
        assert isinstance(got, dict), got

    print("=" * 66)
    print("C) VPK 写出与校验")
    print("=" * 66)

    @check("write_vpk 自己重算 crc：改过 data 后写出的 crc 与内容一致")
    def _():
        import zlib
        e = vpk.VpkEntry(ext="txt", path="scripts", filename="c", crc=0,
                         preload_len=0, archive_index=vpk.ARCHIVE_IN_DIR,
                         entry_offset=0, entry_length=0, preload=b"", data=b"hello")
        # 故意塞一个错 crc，write_vpk 必须覆盖它
        e.crc = 0xDEADBEEF
        out = os.path.join(tmp, "crc.vpk")
        vpk.write_vpk(out, [e])
        v = vpk.Vpk(out, load_data=True)
        want = zlib.crc32(b"hello") & 0xFFFFFFFF
        assert v.entries[0].crc == want, (hex(v.entries[0].crc), hex(want))

    @check("write_vpk 多条目：entry_offset 单调递增且不重叠")
    def _():
        ents = []
        for i in range(20):
            ents.append(vpk.VpkEntry(
                ext="txt", path="d", filename="e%d" % i, crc=0, preload_len=0,
                archive_index=vpk.ARCHIVE_IN_DIR, entry_offset=0, entry_length=0,
                preload=b"", data=("payload-%d" % i).encode() * (i + 1)))
        out = os.path.join(tmp, "multi.vpk")
        vpk.write_vpk(out, ents)
        v = vpk.Vpk(out, load_data=True)
        offs = [e.entry_offset for e in v.entries]
        assert offs == sorted(offs), offs
        for i, e in enumerate(v.entries):
            assert e.entry_length == len(("payload-%d" % i).encode() * (i + 1)), \
                (e.filename, e.entry_length)
            assert v.read_file("d/e%d.txt" % i) == ("payload-%d" % i).encode() * (i + 1)

    print("=" * 66)
    print("D) audit 上限提前判")
    print("=" * 66)

    @check("CO-BUG 的超大分组不会真的枚举 C(n,2)")
    def _():
        import time as _t
        mods = []
        for i in range(300):
            mods.append({
                "key": "m%d.vpk" % i, "title": "M%d" % i, "enabled": True,
                "content_tags": ["same"], "tags": [], "category": "weapon",
                "auto_category": "weapon", "dirs": ["shared/%d" % i],
                "vpaths": ["shared/%d/x.txt" % i],
            })
        t0 = _t.monotonic()
        res = audit.conflicts(mods, only_enabled=True)
        dt = _t.monotonic() - t0
        assert dt < 10.0, "枚举耗时 %.1fs，上限没提前生效" % dt
        assert "findings" in res or "dir_groups" in res, list(res)[:8]

    print("=" * 66)
    print("E) http_util.read_body 边界")
    print("=" * 66)

    class FakeH:
        """够 http_util.send() 用的最小桩：它要 4 个东西 ——
        send_response / send_header / end_headers / wfile.write。"""
        def __init__(self, body=b"", cl=None, ctype="application/json"):
            self.headers = {}
            if cl is not None:
                self.headers["Content-Length"] = cl
            self.headers["Content-Type"] = ctype
            self.rfile = io.BytesIO(body)
            self.wfile = io.BytesIO()
            self.close_connection = False
            self.command = "POST"
            self._status = None

        def send_response(self, code, message=None):
            self._status = code
            self.wfile.write(("HTTP/1.1 %d X\r\n" % code).encode())

        def send_header(self, k, v):
            self.wfile.write(("%s: %s\r\n" % (k, v)).encode())

        def end_headers(self):
            self.wfile.write(b"\r\n")

    def status_of(h):
        raw = h.wfile.getvalue()
        if not raw:
            return None
        return int(raw.split(b" ", 2)[1])

    @check("Content-Length 非法 → 400（不是 500）")
    def _():
        h = FakeH(b"x", cl="abc")
        try:
            http_util.read_body(h)
        except http_util.BodyTooLarge:
            pass
        assert status_of(h) == 400, status_of(h)

    @check("0 长度 → 返回 {}")
    def _():
        h = FakeH(b"", cl="0")
        assert http_util.read_body(h) == {}, http_util.read_body(h)

    @check("非法 JSON → 返回 {} 不抛")
    def _():
        h = FakeH(b"{bad json", cl="9")
        assert http_util.read_body(h) == {}, http_util.read_body(h)

    @check("顶层是数组 → 返回 {}（不是 list）")
    def _():
        h = FakeH(b"[1,2]", cl="5")
        got = http_util.read_body(h)
        assert isinstance(got, dict) and got == {}, got

    print("=" * 66)
    print("F) manager 路径守卫")
    print("=" * 66)

    @check("_inside 拦截 ../ 穿越")
    def _():
        stage = os.path.abspath(safety.STAGING_DIR)
        assert manager._inside(os.path.join(stage, "a"), stage) is True
        assert manager._inside(os.path.join(stage, "..", "evil"), stage) is False
        assert manager._inside(stage + "_evil" + os.sep + "x", stage) is False

    @check("delete_staging_to_recycle 拒绝隔离区外的路径")
    def _():
        r = manager.delete_staging_to_recycle([os.path.join(tmp, "addonlist.txt")])
        assert r["ok"] is False, r
        assert "拒绝" in r.get("error", ""), r

    @check("restore_from_staging：边车没有 original_path → need_target")
    def _():
        stage = os.path.abspath(safety.STAGING_DIR)
        os.makedirs(stage, exist_ok=True)
        item = os.path.join(stage, "20260101-000000__link__noname.vpk")
        with open(item, "wb") as fp:
            fp.write(b"x")
        r = manager.restore_from_staging(al, item, None)
        assert r.get("ok") is True, r
        assert r.get("need_target") is True, r
        os.unlink(item)

    print("=" * 66)
    print("G) 真实语料（有就用，没有跳过）")
    print("=" * 66)

    real_al = os.path.join(ROOT, "data", "addonlist.txt")

    def _():
        if not os.path.isfile(real_al):
            raise RuntimeError("实际没有这个文件，跳过（不算失败）")
        txt, enc = safety.read_text_auto(real_al)
        mapping = scan.read_addonlist(real_al)
        # 纯函数往返：不改内容，渲染出来的文本必须与原文逐字节相同
        new_text, _keys = manager._render_addonlist(txt, mapping)
        assert new_text == txt, "往返改动了文件内容（%d vs %d 字节）" % (len(new_text), len(txt))
        print("         真实 addonlist.txt：%d 字节，编码 %s，%d 条，往返逐字节相同"
              % (len(txt), enc, len(mapping)))
    try:
        _()
    except RuntimeError as ex:
        print("  [SKIP] 真实 addonlist.txt 往返：%s" % ex)

    rmtree(tmp)
    print()
    print("=" * 66)
    print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    print("=" * 66)
    if FAILED:
        print("失败清单：")
        for n, e in FAILED:
            print("  - %s: %s: %s" % (n, type(e).__name__, e))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
