# -*- coding: utf-8 -*-
"""VPK (Valve Pack) 读写内核 —— 纯 Python 标准库实现，零依赖。

覆盖 L4D2 addon 的真实形态（已实测）：
  * version 1 单文件 VPK，全部条目 archiveIndex = 0x7FFF（数据内嵌在 _dir.vpk 自己里）
  * preload 字节（preloadBytes > 0 时数据前段直接存在目录树里）
  * FireAxe 下载产物会把 addoninfo.txt 塞在名为 " "（单个空格）的目录下 —— 必须归一化
  * 多分卷（pak01_dir.vpk + pak01_000.vpk）读取支持

写入策略：**逐字节搬运**。裁剪时把保留条目的原始字节原样拷进新文件，
不重新编码、不碰 CRC，因此对压缩/未知格式也安全。
"""
from __future__ import annotations

import contextlib
import os
import struct
import zlib
from dataclasses import dataclass, field
from functools import cached_property

VPK_SIG = 0x55AA1234
ARCHIVE_IN_DIR = 0x7FFF          # 数据在本文件内
TERMINATOR = 0xFFFF


class VpkError(Exception):
    """VPK 结构异常。"""


def _strip_inline_comment(line: str) -> str:
    """去掉 // 行内注释，但不动引号里的 //。"""
    out, i, q = [], 0, None
    while i < len(line):
        c = line[i]
        if q:
            out.append(c)
            if c == q:
                q = None
        elif c in "\"'":
            q = c
            out.append(c)
        elif c == "/" and i + 1 < len(line) and line[i + 1] == "/":
            break
        else:
            out.append(c)
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------- #
# 条目
# --------------------------------------------------------------------------- #
@dataclass
class VpkEntry:
    ext: str
    path: str            # 目录，未归一化（FireAxe 产物可能是 " "）
    filename: str
    crc: int
    preload_len: int
    archive_index: int
    entry_offset: int
    entry_length: int
    preload: bytes = b""
    data: bytes = b""    # 读入后缓存的实体数据（不含 preload）

    # 性能：vpath 原本是普通 @property，每次访问都要重新拼字符串。
    # 真实语料实测（206 个 mod / 51320 个条目）：全量访问一遍 0.0183s，
    # 裸拼接 100 万次 0.413s（单次 413 ns）—— 单看一次不贵，但这东西挂在
    # scan / taglib 的热路径上会被反复读（_index 建索引、list_files、
    # audit 比对、components 归一化各走一遍），条目数上万时就是白烧 CPU。
    # 改成 cached_property：算一次存进 __dict__，之后是普通属性读取。
    # 安全性：VpkEntry 是 @dataclass 且无 __slots__（有 __dict__ 才能缓存），
    # 全仓库也没有任何代码在构造后改写 VpkEntry 的 path/ext/filename/vpath
    # —— 一改就会拿到过期缓存，这一点是缓存成立的前提。
    @cached_property
    def vpath(self) -> str:
        """归一化后的内部路径：小写、正斜杠、无首尾多余空白与斜杠。"""
        d = (self.path or "").strip().replace("\\", "/").strip("/")
        fn = self.filename + ("." + self.ext if self.ext else "")
        return (d + "/" + fn).lower() if d else fn.lower()

    @property
    def full_data(self) -> bytes:
        return self.preload + self.data

    @property
    def size(self) -> int:
        return self.preload_len + self.entry_length


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
class Vpk:
    """只读句柄：解析目录树，按需取文件字节。"""

    def __init__(self, path: str, load_data: bool = False):
        self.path = os.path.abspath(path)
        self.version = 0
        self.tree_size = 0
        self.header_size = 0
        self.data_start = 0
        self.entries: list[VpkEntry] = []
        self._data_loaded = False
        self._chunks: dict[int, str] = {}
        # 内部路径 -> 条目 的索引。pak01_dir.vpk 有约 5 万条目，
        # 而 read_file() 会被反复调用（addoninfo / 冲突检测 / 文件树），
        # 线性遍历是 O(n)，累计起来非常可观 —— 建一次索引把查找降到 O(1)。
        self._index: dict[str, VpkEntry] = {}
        self.parse()
        if load_data:
            self.load_all()

    # -- 解析 ------------------------------------------------------------- #
    def parse(self) -> None:
        if not os.path.isfile(self.path):
            raise VpkError("文件不存在: %s" % self.path)
        size = os.path.getsize(self.path)
        with open(self.path, "rb") as fp:
            # 注意：v1 头部只有 12 字节、v2 是 28 字节。
            # 绝不能一口气 read(28) —— 会在 v1 上多吃掉 16 字节，导致整棵目录树
            # 从错误的偏移读起（实测踩坑：206 个 mod 里 162 个报「目录树意外结束」）。
            head = fp.read(12)
            if len(head) < 12:
                raise VpkError("文件过小（%d 字节），不是有效 VPK" % size)
            sig, ver = struct.unpack("<II", head[:8])
            if sig != VPK_SIG:
                hint = ""
                if sig == 0x04034B50:
                    hint = "；实为 ZIP 压缩包，扩展名被改成了 .vpk"
                elif sig == 0x21726152:
                    hint = "；实为 RAR 压缩包"
                # 这里原本还有一条 `elif sig == 0x04034B50 ^ 0x10000:`，已删除。
                # 原因：Python 里 `==` 的优先级高于 `^`，那行实际被解析成
                #     (sig == 0x04034B50) ^ 0x10000
                # 而不是作者以为的 sig == (0x04034B50 ^ 0x10000)。于是：
                #   * 走到这个 elif 说明 sig != 0x04034B50（上面第一个 if 已拦掉），
                #     所以是 (False) ^ 0x10000 = 65536 —— 真值，**恒为真**；
                #   * 而它的函数体只是 hint = ""，而 hint 进来时就初始化成 ""，
                #     且上面两个分支都没命中 —— 所以是个纯空操作。
                # 实测：hex(0x04034B50 ^ 0x10000) = 0x4024b50，该值也不是任何已知
                # 归档签名，无法反推作者想认哪个格式，所以不做「猜测式修正」，
                # 直接删掉这条死分支（删前删后行为逐字节一致，黄金回归可证）。
                raise VpkError("不是 VPK 文件（签名 0x%08X%s）" % (sig, hint))
            if ver == 1:
                (tree_size,) = struct.unpack("<I", head[8:12])
                hdr = 12
            elif ver == 2:
                more = fp.read(16)
                if len(more) < 16:
                    raise VpkError("v2 头部被截断")
                (tree_size,) = struct.unpack("<I", head[8:12])
                hdr = 28
            else:
                raise VpkError("不支持的 VPK 版本 %d" % ver)

            if hdr + tree_size > size:
                raise VpkError("目录树大小越界（%d + %d > %d，文件可能被截断）"
                               % (hdr, tree_size, size))

            self.version = ver
            self.tree_size = tree_size
            self.header_size = hdr
            self.data_start = hdr + tree_size

            tree = fp.read(tree_size)
            if len(tree) != tree_size:
                raise VpkError("目录树读取不完整（期望 %d 实得 %d）" % (tree_size, len(tree)))
            self.entries = self._parse_tree(tree)
            self._check_entry_bounds(size)

        # 建查找索引。同名条目（打包器产生的重复项）以**第一个**为准，
        # 与原先线性遍历 read_file() 的语义完全一致。
        for e in self.entries:
            self._index.setdefault(e.vpath, e)

    def _check_entry_bounds(self, file_size: int) -> None:
        """校验「主文件内」的条目偏移没有越界，越界即报错。

        ⚠️ 原先 parse() 里唯一的边界检查只管目录树自身，条目偏移从不校验，
        而 read_entry() 的 fp.read(n) 越 EOF 是**静默短读**。实测（
        一个 354 MB / 150 条目的 VPK 构造副本）：把数据区截到只剩 1000 字节
        → 解析成功、entries=150、不报错，read_entry 返回 0 字节也不抛异常；
        把某个 offset 补丁成 0xFFFFFF00 → 同样解析成功、同样返回 0 字节。
        后果：extract()/extract_all() 会把一堆空文件写盘并 return True，
        用户以为导出成功了，拿到的全是 0 字节。

        分卷条目（archive_index != 0x7FFF）这里不校验 —— 它们的数据在
        pak01_000.vpk 之类的分卷里，要 stat 另一个文件；那部分交给 read_entry
        在真正读取时把关（缺分卷 / 短读都会报错）。
        """
        avail = file_size - self.data_start
        bad = [e for e in self.entries
               if e.archive_index == ARCHIVE_IN_DIR
               and e.entry_offset + e.entry_length > avail]
        if bad:
            e = bad[0]
            raise VpkError(
                "有 %d 个条目的数据超出文件末尾（文件疑似被截断）："
                "首个是 %s，需要到偏移 %d 但数据区只有 %d 字节"
                % (len(bad), e.vpath, e.entry_offset + e.entry_length, avail))

    @staticmethod
    def _parse_tree(tree: bytes) -> list[VpkEntry]:
        p = 0
        n = len(tree)
        out: list[VpkEntry] = []

        def rdstr(pos: int) -> tuple[str, int]:
            if pos >= n:
                raise VpkError("目录树意外结束")
            e = tree.find(b"\x00", pos)
            if e < 0:
                raise VpkError("目录树字符串未终止")
            return tree[pos:e].decode("utf-8", "replace"), e + 1

        while True:
            ext, p = rdstr(p)
            if ext == "":
                break
            while True:
                path, p = rdstr(p)
                if path == "":
                    break
                while True:
                    name, p = rdstr(p)
                    if name == "":
                        break
                    if p + 18 > n:
                        raise VpkError("条目记录被截断")
                    crc, preload, arch, off, length = struct.unpack("<IHHII", tree[p:p + 16])
                    p += 16
                    (term,) = struct.unpack("<H", tree[p:p + 2])
                    p += 2
                    pre = b""
                    if preload:
                        pre = tree[p:p + preload]
                        p += preload
                    # 宽容处理：某些打包器的终止符不是 0xFFFF，只告警不中止
                    _ = term
                    out.append(VpkEntry(ext, path, name, crc, preload, arch, off, length, preload=pre))
        return out

    # -- 数据 ------------------------------------------------------------- #
    def _chunk_path(self, index: int) -> str:
        """分卷文件路径：<base>_dir.vpk -> <base>_%03d.vpk。"""
        if index in self._chunks:
            return self._chunks[index]
        d = os.path.dirname(self.path)
        b = os.path.basename(self.path)
        stem = b[:-8] if b.lower().endswith("_dir.vpk") else os.path.splitext(b)[0]
        cand = os.path.join(d, "%s_%03d.vpk" % (stem, index))
        self._chunks[index] = cand
        return cand

    @staticmethod
    def _read_exact(fp, offset: int, length: int, e: VpkEntry, where: str) -> bytes:
        """从 fp 的 offset 处读满 length 字节，短读即报错。

        ⚠️ 原先直接用 fp.read(n)：越过 EOF 时 Python 只返回剩余字节、**不抛异常**，
        于是 0 字节的「空文件」被当成正常内容一路传下去。配合上面的
        _check_entry_bounds（管主文件），这里负责分卷与读取期的兜底。
        """
        fp.seek(offset)
        data = fp.read(length)
        if len(data) != length:
            raise VpkError(
                "读取 %s 时数据不足：需要 %d 字节，实得 %d 字节（%s 疑似被截断或分卷缺失）"
                % (e.vpath, length, len(data), where))
        return data

    def load_all(self) -> None:
        if self._data_loaded:
            return
        # 分卷句柄复用：原先分卷分支写在循环体里 `with open(cp, "rb") as cf:`，
        # 一个分卷里有 N 个条目就 open() N 次。合成 fixture 实测（1200 条目 /
        # 3 个分卷）改前 open() 共 1202 次（主文件 2 次：parse + load_all；
        # 分卷 1200 次），改后 5 次（主文件 2 + 3 个分卷各 1 次）。
        # 真实语料里 206 个 mod **没有一个用分卷**，所以这条在玩家机器上省不到
        # 什么；它防的是别人真要读分卷 mod 时被 N 次 open/close 拖垮。
        #
        # 异常安全：用 ExitStack 统一持有句柄，不管是正常走完还是中途抛
        # VpkError（缺分卷 / 数据不足）或任何其它异常，退出 with 时都会关闭，
        # 不需要手工写 try/finally，也不会因为半路抛异常而漏掉已开的分卷。
        with contextlib.ExitStack() as stack:
            chunk_fps: dict[int, object] = {}

            def chunk_fp(index: int):
                """取某个分卷的句柄，首次访问时打开，之后复用同一个。"""
                cf = chunk_fps.get(index)
                if cf is None:
                    cp = self._chunk_path(index)
                    if not os.path.isfile(cp):
                        raise VpkError("缺少分卷 %s" % os.path.basename(cp))
                    cf = stack.enter_context(open(cp, "rb"))
                    chunk_fps[index] = cf
                return cf

            with open(self.path, "rb") as fp:
                for e in self.entries:
                    if e.archive_index == ARCHIVE_IN_DIR:
                        e.data = self._read_exact(
                            fp, self.data_start + e.entry_offset, e.entry_length,
                            e, os.path.basename(self.path))
                    else:
                        e.data = self._read_exact(
                            chunk_fp(e.archive_index), e.entry_offset, e.entry_length,
                            e, os.path.basename(self._chunk_path(e.archive_index)))
        self._data_loaded = True

    def read_entry(self, e: VpkEntry) -> bytes:
        # 与 load_all 的区别，以及为什么这里**不**做句柄复用（实测后决定的）：
        # read_entry 一次调用只读一个条目，所以它的 open() 次数本来就是 1 ——
        # 没有「同一个分卷被反复重开」的浪费可省。要再往下省，只能跨调用把
        # 句柄存起来（比如缓存住最后一次用过的分卷），但那意味着一个 Vpk 实例
        # 会在不确定长的时间里占着一个 fd。Windows 上打开中的文件默认带共享锁，
        # 这个工具干的活恰恰是**改写玩家磁盘上的游戏文件**（删/换 addon、
        # strip_vpk 后 os.replace 成品）—— 残留句柄会让删除或替换目标失败，
        # 甚至报出「文件被另一个程序占用」这种玩家无法理解的错。
        # 省一次 open（微秒级）换这个风险不划算，所以只在 load_all 那条
        # 「真·批量」路径上做复用（那里一次调用要读 N 个条目，收益是 N:1）。
        # 调用方若真需要批量读，走 load_all 而不是循环调 read_entry。
        if e.data or e.entry_length == 0:
            return e.full_data
        if e.archive_index == ARCHIVE_IN_DIR:
            with open(self.path, "rb") as fp:
                e.data = self._read_exact(
                    fp, self.data_start + e.entry_offset, e.entry_length,
                    e, os.path.basename(self.path))
        else:
            cp = self._chunk_path(e.archive_index)
            # 原先这里没有 os.path.isfile 检查：缺分卷时 open() 抛的是
            # FileNotFoundError，而调用方普遍只捕 VpkError（load_all 有检查，
            # 行为不一致）—— 于是「文件读不出来」变成了 500 而不是一句人话。
            if not os.path.isfile(cp):
                raise VpkError("缺少分卷 %s" % os.path.basename(cp))
            with open(cp, "rb") as cf:
                e.data = self._read_exact(
                    cf, e.entry_offset, e.entry_length, e, os.path.basename(cp))
        return e.full_data

    def read_file(self, vpath: str) -> bytes | None:
        want = vpath.strip().lower().replace("\\", "/")
        e = self._index.get(want)
        if e is None:
            return None
        return self.read_entry(e)

    def crc32_entry(self, e: VpkEntry, chunk: int = 1 << 20) -> int:
        """流式计算条目的 crc32（含 preload 段），**不把整条读进内存**。

        verify_strip 要做逐条内容比对；若先 load_all() 再算，一个 827 MB 的
        条目就要 827 MB 常驻（实测语料最大单文件 827.0 MB）。这里按 1 MB
        分块喂 zlib.crc32，峰值只与 chunk 有关，与条目本身大小无关。

        注：crc32(preload) 与 crc32(data) 顺序累积 == crc32(preload + data)，
        已用真实语料验证（8059/8059 条目与目录树里的 crc 字段一致）。
        """
        crc = zlib.crc32(e.preload) if e.preload else 0
        if e.entry_length <= 0:
            return crc & 0xFFFFFFFF
        if e.archive_index == ARCHIVE_IN_DIR:
            path, base = self.path, self.data_start + e.entry_offset
        else:
            path = self._chunk_path(e.archive_index)
            if not os.path.isfile(path):
                raise VpkError("缺少分卷 %s" % os.path.basename(path))
            base = e.entry_offset
        left = e.entry_length
        with open(path, "rb") as fp:
            fp.seek(base)
            while left > 0:
                buf = fp.read(min(chunk, left))
                if not buf:
                    raise VpkError(
                        "读取 %s 时数据不足：还差 %d 字节（%s 疑似被截断）"
                        % (e.vpath, left, os.path.basename(path)))
                crc = zlib.crc32(buf, crc)
                left -= len(buf)
        return crc & 0xFFFFFFFF

    # -- 工具 ------------------------------------------------------------- #
    def list_files(self) -> list[str]:
        return [e.vpath for e in self.entries]

    @property
    def is_single_archive(self) -> bool:
        return all(e.archive_index == ARCHIVE_IN_DIR for e in self.entries)

    def compression_suspect(self) -> tuple[bool, str]:
        """判断文件是否疑似被压缩/异常 —— 这种情况禁止重写。

        依据：LZ4/LZMA 压缩时「条目长度总和」会明显小于「文件实际数据区大小」。
        """
        if not self.entries:
            return False, ""
        if not self.is_single_archive:
            return True, "存在分卷，重写会丢失分卷结构"
        total = sum(e.entry_length for e in self.entries)
        actual = os.path.getsize(self.path) - self.data_start
        if abs(actual - total) > max(4096, actual * 0.02):
            return True, ("数据区 %d 字节与条目总和 %d 字节不符，疑似压缩或含附加段"
                          % (actual, total))
        return False, ""

    def addoninfo(self) -> dict:
        """读取并解析 addoninfo.txt（容忍 FireAxe 的路径畸形与行内注释）。

        实测：官方的 addoninfo 模板把说明写成了行内注释，值长这样 ——
            addoncontent_prop      "0 //This Add-on provides new props,"
        如果不剥注释，"0" 会被当成真值 → 87 个 mod 里有 36 个被误判成「地图」。
        因此这里统一做：去行内注释 → 去引号 → 归一化。
        """
        raw = self.read_file("addoninfo.txt")
        if raw is None:
            return {}
        try:
            text = raw.decode("utf-8-sig", "replace")
        except Exception:
            return {}
        out: dict = {}
        for line in text.splitlines():
            line = _strip_inline_comment(line).strip()
            if not line or line in "{}":
                continue
            parts = line.split(None, 1)
            k = parts[0].strip().strip('"').lower()
            v = parts[1].strip() if len(parts) == 2 else ""
            v = v.strip().strip('"').strip()
            # 处理 0" 这种引号错位
            if v.endswith('"') or v.endswith("'"):
                v = v[:-1].strip()
            out[k] = v
        return out

    # -- 提取 ------------------------------------------------------------- #
    def extract(self, vpath: str, out_path: str) -> bool:
        data = self.read_file(vpath)
        if data is None:
            return False
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as fp:
            fp.write(data)
        return True

    def extract_all(self, out_dir: str, progress=None) -> int:
        self.load_all()
        cnt = 0
        for e in self.entries:
            dst = os.path.join(out_dir, e.vpath.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as fp:
                fp.write(e.full_data)
            cnt += 1
            if progress:
                progress(cnt, len(self.entries), e.vpath)
        return cnt


# --------------------------------------------------------------------------- #
# 写入 / 裁剪
# --------------------------------------------------------------------------- #
def write_vpk(out_path: str, entries: list[VpkEntry], version: int = 1) -> None:
    """把（已带 data 的）条目列表写成单文件 VPK v1。

    ⚠️ 调用前必须确保条目**已经读到了数据**（Vpk(path, load_data=True) 或
    load_all()）。下面那句 `e.entry_length = len(e.data)` 会把没读数据的条目
    静默写成 0 字节 —— 而目录树里 offset/length 也随之归零，成品能正常解析、
    只是所有文件都空了。踩坑记录：verify_p05.py 的 G 组第一版就是拿
    `Vpk(src)`（没 load_data）去 write_vpk，结果 8 个条目全变成 0 字节，
    却因为「成品仍可解析」而一路通过。这种失败模式太安静，必须显式拦住。
    """
    # 只支持 v1 写出，v2 显式拒绝（原先不检查，version 参数被原样塞进头部）。
    # 原因：v2 的头部是 28 字节（12 字节基础头 + 16 字节 extra：条目数 /
    # 归档总大小 / 归档区大小 / 1 字节 + 保留），而下面落盘那句是
    # `struct.pack("<III", VPK_SIG, version, len(tree))` —— 只写 12 字节，
    # 然后把目录树紧跟在后面。于是文件自称 ver=2，但读者的 parse() 会按
    # hdr=28 去算 tree_size，把目录树的前 16 字节当成 extra 吞掉：
    # 实测 write_vpk(version=2) 写出 69 字节，重读直接抛
    #     VpkError: 目录树大小越界（28 + 49 > 69，文件可能被截断）
    # 即产出一个自己都读不回来的坏文件。坏文件对玩家就是「mod 打不开了」，
    # 这种时候宁可当场报中文错误、不落任何文件。
    # 注意：现有调用链（app/api.py 两处 strip_vpk）都走默认 version=1，
    # 所以这条拒绝不会影响任何在用功能。
    if version != 1:
        raise VpkError(
            "不支持写出 VPK v%d：本实现只会写 v1 头部（12 字节），写 v2 会得到"
            "一个连自己都读不回来的畸形文件（重读报「目录树大小越界」）。"
            "请传 version=1。" % version)
    # 按 扩展名 -> 目录 -> 文件名 的层级聚合，保持输入顺序
    groups: dict[str, dict[str, list[VpkEntry]]] = {}
    for e in entries:
        groups.setdefault(e.ext, {}).setdefault(e.path, []).append(e)

    # 只有 preload 没有实体数据的条目，只有在 entry_length == 0 时才是合法的
    # （即「数据全部存在目录树的 preload 段里」）。若 entry_length > 0 而
    # data 为空，说明漏掉了一步 load —— 此时写出去就是静默毁文件。
    lifeless = [e for e in entries if e.entry_length > 0 and not e.data]
    if lifeless:
        e0 = lifeless[0]
        raise VpkError(
            "有 %d 个条目尚未读入数据，直接写出会把它们变成 0 字节空文件："
            "首个是 %s（应载入 %d 字节）。请先用 Vpk(..., load_data=True) "
            "或 load_all() 读取。" % (len(lifeless), e0.vpath, e0.entry_length))

    # 数据区拼接：**先累加长度算偏移，再一次性 join**。
    # 原写法 `data_blob = bytearray(); data_blob += e.data` 在 CPython 上看似摊还
    # 便宜，但每个条目都要把已累积的全部字节重新分配/拷贝一次（真正的增量是
    # 条目大小，而发生次数是 O(条目数)）——对 800MB 级、几万个条目的 mod 意味着
    # 十几 GB 量级的无谓拷贝。改成 b"".join 只需两趟线性扫描，峰值内存只多出
    # 「所有数据的一份副本」（与原实现下 bytearray 的最终大小相当）。
    total = 0
    for e in entries:
        e.entry_offset = total
        # ⚠️ 必须用 len(e.data)，不能写 len(e.full_data)：entry_length 描述的是
        #    「数据区里的字节数」，preload 字节是**存在目录树里**的，不在数据区。
        #    用 full_data 会让每个条目的长度多出 preload_len，偏移虽自洽，
        #    成品却比真实数据整体偏长，读到最后一个条目时直接越过 EOF。
        #    （crc 按完整内容（preload + data）重算，见下方那句 —— 不能沿用旧的。）
        e.entry_length = len(e.data)
        # ⚠️ crc 必须按**这次真正写进去的内容**重算（preload + data）。
        #    前一句注释说「crc 仍按完整内容算，那是对的」—— 那只在「调用方
        #    刚解析完同一个文件、byte 级原样重写」时成立。而 strip_vpk 会
        #    先 load_all 再原样重写、components 那条路径也会改条目集合；
        #    任何修改过 e.data 的场景，沿用的旧 crc 都会与写出的字节不符，
        #    成品「能解析、能读」，校验却对不上 —— 属于最难查的那类问题。
        #    反过来，输入来自真实 VPK 时重算结果与旧值一致（全仓库无改 crc
        #    的写入点），所以对正常路径零影响。
        e.crc = zlib.crc32(e.preload, zlib.crc32(e.data)) & 0xFFFFFFFF
        total += e.entry_length
    data_blob = b"".join(e.data for e in entries)
    for e in entries:
        e.archive_index = ARCHIVE_IN_DIR

    def s(x: str) -> bytes:
        return x.encode("utf-8") + b"\x00"

    tree = bytearray()
    for ext, paths in groups.items():
        tree += s(ext)
        for path, files in paths.items():
            tree += s(path)
            for e in files:
                tree += s(e.filename)
                tree += struct.pack("<IHHII", e.crc, len(e.preload), ARCHIVE_IN_DIR,
                                    e.entry_offset, e.entry_length)
                tree += struct.pack("<H", TERMINATOR)
                tree += e.preload
            tree += b"\x00"
        tree += b"\x00"
    tree += b"\x00"

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as fp:
        fp.write(struct.pack("<III", VPK_SIG, version, len(tree)))
        fp.write(tree)
        fp.write(data_blob)
    os.replace(tmp, out_path)


def strip_vpk(src: str, out_path: str, drop: set[str], version: int = 1) -> dict:
    """从 src 生成 out_path，剔除内部路径命中 drop 的条目。

    drop 中的路径会被归一化（小写、正斜杠）后比对。
    返回统计信息；不做备份、不覆盖源文件 —— 备份由上层 safety 负责。

    ⚠️ 自校验红线（本次加固）：写完成品后**立刻在这里做字节级复核**，
    不合格就把成品删掉再返回 ok=False。

    原先复核只发生在 app/api.py 的调用方，且只比 e.size（而 size =
    preload_len + entry_length，两个数**都来自目录树**）——纯元数据比对：
      * 同长度不同内容 100% 漏过；
      * 源文件在读取期间被 Steam 改写 → 成品是混合版本，照样 ok=True。
    调用方一旦忘了复核（或新写一个调用方），红线就没了。现在复核内联在
    核心层，绕过不了 —— 而且期望摘要是**边写边从实际字节算**的，
    不依赖目录树里的 crc 字段是否可信。
    """
    src_vpk = Vpk(src)
    bad, why = src_vpk.compression_suspect()
    if bad:
        raise VpkError("拒绝重写：%s" % why)
    dropn = {d.strip().lower().replace("\\", "/") for d in drop}
    # 读之前记下源文件的身份：目录树在 T0 解析、数据在 T1 读取、成品在 T2 写出。
    # 若 T0~T2 之间 Steam 更新了这个 mod（或别的工具动了它），我们手里就是
    # 「半新半旧」的字节 —— 写出去的成品会是混合版本，而单看读到的字节
    # 永远自洽、查不出来。所以拿 (size, mtime_ns) 当外部锚点。
    st_before = os.stat(src)
    src_vpk.load_all()
    keep: list[VpkEntry] = []
    dropped: list[str] = []
    for e in src_vpk.entries:
        if e.vpath in dropn:
            dropped.append(e.vpath)
        else:
            keep.append(e)
    if not dropped:
        raise VpkError("没有匹配到任何要剔除的条目（drop=%d 条）" % len(dropn))
    # 期望摘要取自**实际读到的字节**（此刻仍在内存里，所以是免费的），
    # 顺便比对目录树里的 crc 字段：不一致说明这个 mod 被别的工具改过、
    # 目录树没同步 —— 实测全语料 51320 条里只有 1 条不符
    # （一张贴图），所以**只报警不拦截**：
    # 数据是原样搬运的，用户照样能裁，但界面上该告诉他这文件已经不对劲了。
    expected: dict[str, int] = {}
    tree_crc_bad: list[str] = []
    for e in keep:
        c = zlib.crc32(e.full_data) & 0xFFFFFFFF
        expected[e.vpath] = c
        if e.crc and e.crc != c:
            tree_crc_bad.append(e.vpath)
    write_vpk(out_path, keep, version)
    st_after = os.stat(src)
    src_changed = ((st_before.st_size, st_before.st_mtime_ns)
                   != (st_after.st_size, st_after.st_mtime_ns))
    report = verify_strip(src, out_path, drop, expected=expected,
                          src_changed=src_changed, tree_crc_bad=tree_crc_bad)
    if not report["ok"]:
        # 成品是坏的就不许留在磁盘上：调用方即使忘了看 report 也拿不到坏文件。
        try:
            os.unlink(out_path)
        except OSError:
            pass
    return {
        "源条目": len(src_vpk.entries),
        "剔除": len(dropped),
        "保留": len(keep),
        "剔除明细": dropped[:200],
        "大小变化": os.path.getsize(out_path) - os.path.getsize(src) if os.path.exists(out_path) else 0,
        "ok": report["ok"],
        "校验": report,
    }


def verify_strip(original: str, result: str, dropped: set[str],
                 expected: dict[str, int] | None = None,
                 src_changed: bool = False,
                 tree_crc_bad: list[str] | None = None) -> dict:
    """重读成品做完整性校验：目标必须消失，非目标一个不能少。

    expected 给定时（vpath -> crc32），额外做**逐条字节级**比对：
    每条按 1 MB 流式读回算 crc32，峰值内存与条目大小无关（真实语料里
    单个 mod 最大 827.0 MB，整条读进来复核会直接吃掉一个 GB）。

    src_changed / tree_crc_bad 由 strip_vpk 传入，分别代表「源在我们眼皮底下
    被改过」与「目录树 crc 与实际字节不符」；后者只警告不判失败（见 strip_vpk
    注释里 51320 : 1 的实测依据）。
    """
    o = Vpk(original)
    r = Vpk(result)
    oset = {e.vpath: e.size for e in o.entries}
    rset = {e.vpath: e.size for e in r.entries}
    dn = {d.strip().lower().replace("\\", "/") for d in dropped}
    still = sorted(dn & set(rset))
    missing = sorted(set(oset) - dn - set(rset))
    size_bad = sorted(p for p in (set(oset) - dn) & set(rset) if oset[p] != rset[p])

    content_bad: list[str] = []
    checked = 0
    if expected:
        rmap = {e.vpath: e for e in r.entries}
        for p in sorted(set(expected) & set(rmap)):
            checked += 1
            if r.crc32_entry(rmap[p]) != expected[p]:
                content_bad.append(p)
    # 进门查源、出门也查成品：原先只对源调 compression_suspect()，
    # 成品写完没人看一眼 —— 万一写出个数据区长度对不上的文件，
    # 之前会一路 ok=True 交给用户。
    suspect, suspect_why = r.compression_suspect()
    return {
        "ok": (not still and not missing and not size_bad and not content_bad
               and not suspect and not src_changed),
        "未删净": still[:50],
        "误删": missing[:50],
        "大小不符": size_bad[:50],
        # 新增键（前端只读上面四个，这里加键不影响它）
        "内容不符": content_bad[:50],
        "已校验字节": checked,
        "成品异常": suspect_why,
        # 源在读写期间变动：唯一的「硬」信号，无假阳性
        "源已变动": bool(src_changed),
        # 目录树 crc 与实际字节不符：只报告，不拦截（实测会误伤 1/206 个正常 mod）
        "源校验警告": list(tree_crc_bad or [])[:20],
    }
