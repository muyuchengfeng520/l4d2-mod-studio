# -*- coding: utf-8 -*-
"""安全层：备份、回收站、原子写、审计日志。

红线（不可绕过）：
  1. 任何破坏性动作前先备份，备份路径写进返回值并报给用户；
  2. 删除一律走回收站（SHFileOperationW），绝不用 rm/-rf；
  3. 写文件一律「临时文件 + os.replace」原子替换，中途失败不留半成品；
  4. 所有动作留痕到 data/audit.log。
"""
from __future__ import annotations

import codecs
import collections
import ctypes
import json
import locale
import os
import re
import shutil
import sys
import threading
import time
from ctypes import wintypes

def _frozen() -> bool:
    """是不是被 PyInstaller 打包成 exe 在跑。"""
    return bool(getattr(sys, "frozen", False))


def _app_root() -> str:
    """应用自身的**只读资源**根目录。

    - 源码模式：项目的 L4D2ModStudio 目录
    - exe 模式：PyInstaller 解包出来的临时目录（sys._MEIPASS），
      web/ 这类静态资源被塞在里面，**不可写、且每次启动都会重建**。
    """
    if _frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _writable(path: str) -> bool:
    """真的落一次盘试试，别信 os.access（在 Windows 上不准）。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_probe")
        with open(probe, "w", encoding="utf-8") as fp:
            fp.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def _work_root() -> str:
    """**可写**的数据根目录。

    exe 模式优先放在 exe 同级（便携：配置、备份、隔离区跟着 exe 走，
    换台机器拷过去就还在）；万一 exe 被放进 Program Files 这类没写权限的
    地方，就退到 %LOCALAPPDATA%\\L4D2ModStudio。

    环境变量 L4D2MS_WORK_ROOT 可覆盖整个数据根目录（配置/备份/隔离区/日志
    一起搬走）。加这个口子是为了**测试**：本模块在 import 期就把下面那几个
    常量算死并 makedirs，于是任何 `import core.safety` 的测试脚本都会把备份
    和隔离产物写进玩家真实的 data/ —— 想测「备份→还原」就得先污染真实目录，
    这本身就是隐患。有了这个变量，脚本在 import 之前设成临时目录即可隔离。
    生产路径不受影响：不设这个变量时行为与原来逐字节相同。
    """
    override = (os.environ.get("L4D2MS_WORK_ROOT") or "").strip()
    if override:
        return os.path.abspath(override)
    if _frozen():
        cands = [os.path.dirname(os.path.abspath(sys.executable))]
        la = os.environ.get("LOCALAPPDATA")
        if la:
            cands.append(os.path.join(la, "L4D2ModStudio"))
        for c in cands:
            if _writable(c):
                return c
        return cands[0]
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ⚠️ 下面这组常量在 **import 期**就固定下来，并在最后 makedirs 一次。
#    也就是说：任何 `import core.safety` 的脚本都会在 import 那一刻就
#    「认下」一个数据目录并建出来。测试脚本必须先设 L4D2MS_WORK_ROOT
#    再 import，否则备份/隔离产物会落进玩家真实的 data/（见 _work_root）。
APP_ROOT = _app_root()          # 只读资源（web/ 等）
WORK_ROOT = _work_root()        # 可写数据（配置/备份/隔离区/日志）
DATA_DIR = os.path.join(WORK_ROOT, "data")
BACKUP_DIR = os.path.join(DATA_DIR, "backup")
STAGING_DIR = os.path.join(DATA_DIR, "staging")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
AUDIT_LOG = os.path.join(DATA_DIR, "audit.log")

# 审计日志轮转上限。原实现只 append 从不轮转，日志无限增长：
# 每次扫描/审计/启用/隔离都写好几行，长期用下来 audit.log 会到几十 MB，
# 而 recent_log() 的 deque 虽已不再整体读入内存，文件本身仍会一直长。
# 8 MB × (当前 + 3 个历史) ≈ 32 MB 封顶，对「排查最近发生了什么」完全够用。
LOG_MAX_BYTES = 8 * 1024 * 1024
LOG_KEEP = 3
_LOG_LOCK = threading.Lock()

for _d in (DATA_DIR, BACKUP_DIR, STAGING_DIR, CACHE_DIR):
    os.makedirs(_d, exist_ok=True)


def resource(*parts: str) -> str:
    """拼一个**只读资源**的绝对路径（web/style.css 这种）。"""
    return os.path.join(APP_ROOT, *parts)


# --------------------------------------------------------------------------- #
# 时间戳 / 日志
# --------------------------------------------------------------------------- #
def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def human_time(ts: float | int | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _rotate_if_needed() -> None:
    """audit.log 超过 LOG_MAX_BYTES 就滚动一次，最多留 LOG_KEEP 份历史。

    命名 audit.log.1（最新）→ audit.log.N（最旧），与常见日志轮转一致。
    只做「重命名 + 丢弃最旧」，不压缩、不重写内容，代价是几次 rename。
    """
    try:
        if os.path.getsize(AUDIT_LOG) < LOG_MAX_BYTES:
            return
    except OSError:
        return
    try:
        # 先把最旧的丢掉（audit.log.LOG_KEEP 会被新的 .LOG_KEEP-1 顶掉）
        oldest = "%s.%d" % (AUDIT_LOG, LOG_KEEP)
        if os.path.isfile(oldest):
            os.unlink(oldest)
        for i in range(LOG_KEEP - 1, 0, -1):
            src = "%s.%d" % (AUDIT_LOG, i)
            if os.path.isfile(src):
                os.replace(src, "%s.%d" % (AUDIT_LOG, i + 1))
        os.replace(AUDIT_LOG, AUDIT_LOG + ".1")
    except OSError:
        # 轮转失败绝不能影响记日志本身（比如杀毒软件锁住文件）
        pass


def log(action: str, detail: str, level: str = "INFO", ok: bool = True) -> None:
    line = "[%s] %-5s %-22s %s | %s\n" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), level, action,
        "OK" if ok else "FAIL", detail)
    try:
        with _LOG_LOCK:
            # 线程安全：ThreadingHTTPServer 下多个请求会并发写同一文件，
            # 没有锁时两次 log 的 rename 可能交错，把日志切碎。
            _rotate_if_needed()
            with open(AUDIT_LOG, "a", encoding="utf-8") as fp:
                fp.write(line)
    except Exception:
        pass


def recent_log(n: int = 200) -> list[str]:
    if n <= 0:
        return []
    # 跨轮转文件读：按时间从旧到新依次喂进 deque（maxlen=n），
    # 最终留下的是「全部日志里最新的 n 行」。只读存在的文件，
    # 且 deque 的迭代在 C 层完成，不会把整份日志读进内存。
    dq: collections.deque = collections.deque(maxlen=n)
    paths = ["%s.%d" % (AUDIT_LOG, i) for i in range(LOG_KEEP, 0, -1)]
    paths.append(AUDIT_LOG)
    any_file = False
    for p in paths:
        if not os.path.isfile(p):
            continue
        any_file = True
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fp:
                dq.extend(fp)
        except OSError:
            continue
    if not any_file:
        return []
    # 保持原有顺序：最新的一行在最前面
    return list(reversed(dq))


# --------------------------------------------------------------------------- #
# 备份
# --------------------------------------------------------------------------- #
def backup_file(src: str, tag: str = "") -> str:
    """把单个文件复制进备份区，返回备份文件路径。同名冲突自动加时间戳。"""
    if not os.path.isfile(src):
        raise FileNotFoundError(src)
    name = os.path.basename(src)
    if tag:
        name = "%s__%s" % (tag, name)
    dst = os.path.join(BACKUP_DIR, "%s__%s" % (stamp(), name))
    i = 1
    while os.path.exists(dst):
        dst = os.path.join(BACKUP_DIR, "%s__%s__%d" % (stamp(), name, i))
        i += 1
    shutil.copy2(src, dst)
    log("backup", "%s -> %s" % (src, dst))
    return dst


def backup_file_to(src: str, rel_dir: str) -> str:
    """按相对目录归档备份（用于游戏文件替换，保持可追溯的目录结构）。"""
    if not os.path.isfile(src):
        raise FileNotFoundError(src)
    safe = rel_dir.replace("/", "__").replace("\\", "__")
    d = os.path.join(BACKUP_DIR, "gamefiles", safe)
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, "%s__%s" % (stamp(), os.path.basename(src)))
    i = 1
    while os.path.exists(dst):
        dst = os.path.join(d, "%s__%s__%d" % (stamp(), os.path.basename(src), i))
        i += 1
    shutil.copy2(src, dst)
    log("backup", "%s -> %s" % (src, dst))
    return dst


def list_backups() -> list[dict]:
    """列出全部备份文件，按修改时间倒序。

    ⚠️ 用 os.scandir 而不是 os.walk + getsize + getmtime：
    Windows 上每次 getsize/getmtime 都是一次独立的 stat 系统调用，
    备份目录攒到几千个文件时，光这两项就是几百毫秒的纯开销。
    scandir 的 DirEntry 自带 stat 缓存（entry.stat() 命中缓存），
    每个文件少 2 次系统调用。语义与 os.walk 一致：
    目录不跟随符号链接（followlinks=False 的默认行为）。
    """
    out = []
    if not os.path.isdir(BACKUP_DIR):
        return out
    stack = [BACKUP_DIR]
    while stack:
        try:
            it = os.scandir(stack.pop())
        except OSError:
            continue                      # 目录被删/无权限，跳过即可
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                        continue
                    st = e.stat()         # 跟随链接，与 getsize/getmtime 同语义
                except OSError:
                    continue              # 扫描途中文件消失，跳过
                out.append({
                    "name": e.name,
                    "path": e.path,
                    "rel": os.path.relpath(e.path, BACKUP_DIR).replace("\\", "/"),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


# --------------------------------------------------------------------------- #
# 原子写
# --------------------------------------------------------------------------- #
def atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    """先写 .tmp 再 os.replace —— 任何时刻断电/崩溃，目标文件要么是旧的完整版，
    要么是新的完整版，不会出现半截内容。

    ⚠️ 失败时要把 .tmp 清掉：否则写盘中途出错（磁盘满、权限、编码）会在
    目标旁边留一个同名 .tmp，下次调用时被静默覆盖 —— 看起来「没事」，
    实际上用户的项目目录里会攒下一堆垃圾文件。用 try/finally 保证清理。
    """
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding=encoding, newline="\n") as fp:
            fp.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_copy(src: str, dst: str) -> None:
    """与 atomic_write_text 同理：失败不留 .tmp 残骸。"""
    tmp = dst + ".tmp"
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# 编码自适应文本读写（addonlist.txt 等玩家手改过的文本文件）
# --------------------------------------------------------------------------- #
def encoding_candidates() -> list[str]:
    """文本文件的编码探测顺序：utf-8 → 本地编码（GBK 等）。

    为什么需要探测：`addonlist.txt` 是玩家手工可编辑的文件，中文 Windows 上
    不少玩家的这份文件是 **GBK** 存的。原实现用 `encoding="utf-8", errors="replace"`
    硬读 —— 遇到中文 mod 文件名会解出 U+FFFD（替换字符），**而扫描结果又会带着
    这个坏名字写回去**，等于把玩家的文件名原地改坏。这与「先看数据再动手」相悖：
    读到的必须能原样写回，否则宁可报错。

    本机实测：`locale.getpreferredencoding(False)` 在中文 Windows 返回 "cp936"
    （GBK 的别名），所以它排在 gbk 之前；两者 codecs 规范化后是同一个名字，
    会被去重掉，不会重复尝试。

    ⚠️ 这里**故意不含 "utf-8-sig"**。BOM 必须由 read_text_auto 单独判断，原因：
    `utf-8-sig` 解码器对「开头没有 BOM 的 UTF-8」也能解码成功（只是不剥字节），
    所以只要把它排进候选表，**任何普通 UTF-8 文件都会被判定成 utf-8-sig**；
    而写入端用 encoding="utf-8-sig" 时 Python 会**主动在文件最前面加一个 BOM**。
    读的时候没剥、写的时候硬加 —— 对「BOM 本来就在开头」的文件恰好无损，但对
    「本来没有 BOM」的文件就是每写一次凭空长 3 字节。
    真实事故：玩家的 addonlist.txt 表头从 `"AddonList"` 变成了 `"\\ufeff"`，
    字节从 `22 41 64 64 6F 6E 4C 69 73 74 22` 变成 `22 EF BB BF 22`，
    而本程序随后每写一次还会再长 3 字节（实测 5763 → 5766）。
    """
    cands = ["utf-8"]
    for enc in (locale.getpreferredencoding(False), "gbk", "mbcs"):
        if not enc:
            continue
        try:
            enc = codecs.lookup(enc).name
        except LookupError:
            continue
        if enc not in cands:
            cands.append(enc)
    return cands


def read_text_auto(path: str) -> tuple[str, str]:
    """读文本文件，自动探测编码。返回 (文本, 实际使用的编码)。

    文件不存在返回 `("", "utf-8")` —— 调用方据此走「新建」分支（新建文件一律
    写 UTF-8 无 BOM，这是跨平台最稳的默认值；**不要**拿候选表最后一项当默认，
    在中文 Windows 上那是 mbcs/cp936，会把「文件还没建」跟「文件是 GBK」混为一谈）。

    编码探测全部失败时退回 fallback + `errors="replace"` 保证不抛，
    但会写一条 WARN 审计日志：这种情况写回去一定会损坏内容，
    必须留下痕迹而不是悄悄进行。
    """
    cands = encoding_candidates()
    fallback = cands[-1] if cands else "utf-8"
    if not os.path.isfile(path):
        return "", "utf-8"
    with open(path, "rb") as fp:
        raw = fp.read()
    # 空文件或纯 ASCII：utf-8 即可，不必动本地编码（也避免给纯英文文件
    # 附上 gbk 标签，导致后续写回时多此一举地换编码）
    if not raw:
        return "", "utf-8"
    # BOM 单独判：只有真的以 EF BB BF 开头，才用 utf-8-sig 读、并把
    # "utf-8-sig" 作为编码告诉写入端（写入端会**自动补回** BOM，于是逐字节往返）。
    # 这就是 encoding_candidates 里不能放 utf-8-sig 的原因：它对无 BOM 的
    # UTF-8 也解码成功，一旦进候选表，所有普通 UTF-8 文件都会被误判成
    # 「有 BOM」，写回时长出 3 字节 —— 正是玩家 addonlist.txt 被写坏的那种模式。
    if raw.startswith(codecs.BOM_UTF8):
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            # BOM 后面不是合法 UTF-8：落入下面的候选表按「坏文件」处理，
            # 让 WARN 日志留下痕迹，而不是在这里悄悄吞掉。
            pass
    for enc in cands:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    log("text-decode", "%s 编码探测全部失败，按 %s + replace 读取（回写会损坏内容）"
        % (path, fallback), level="WARN", ok=False)
    return raw.decode(fallback, errors="replace"), fallback


def write_text_auto(path: str, text: str, encoding: str = "utf-8") -> None:
    """按指定编码原子写回。编码应来自 read_text_auto 的返回值。

    单独包一层是为了让「读用什么编码、写就用什么编码」在调用点一眼可见 ——
    这两个动作必须成对，否则 GBK 文件会被转成 UTF-8（或反之），
    对玩家而言就是文件被改了。
    """
    atomic_write_text(path, text, encoding=encoding)


# --------------------------------------------------------------------------- #
# 回收站（Windows SHFileOperationW）
# --------------------------------------------------------------------------- #
class _SHFILEOPSTRUCTW(ctypes.Structure):
    # 注意：pFrom / pTo 必须声明成 c_void_p。
    # 若按直觉声明成 LPCWSTR(c_wchar_p)，再赋值 create_unicode_buffer(...) 会被
    # ctypes 类型检查直接拒绝：
    #     TypeError: incompatible types, c_wchar_Array_N instance instead of c_wchar_p
    # 而 SHFileOperationW 要求路径串以 \0\0 双重终止（支持一次传多个路径），
    # 所以必须用 buffer + cast 的方式传。
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", ctypes.c_void_p),
        ("pTo", ctypes.c_void_p),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_void_p),
    ]


FO_DELETE = 3
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMATION = 0x0010
FOF_NOERRORUI = 0x0400
FOF_SILENT = 0x0004


def to_recycle_bin(paths: list[str], fallback_isolate: bool = False) -> dict:
    """把路径移入 Windows 回收站。

    返回 {ok:[路径], failed:[(路径, 原因)], fallback:[(原路径, 隔离后路径)]}
    fallback_isolate=True 时，回收站不可用的条目会**降级为移入隔离区**，
    保证「绝不因为系统接口失败就把文件删不掉或删掉」。
    """
    ok_paths, failed, fell = [], [], []
    for p in paths:
        p = os.path.abspath(p)
        if not os.path.exists(p):
            failed.append((p, "路径不存在"))
            continue
        try:
            buf = ctypes.create_unicode_buffer(p + "\x00\x00")
            op = _SHFILEOPSTRUCTW()
            op.hwnd = None
            op.wFunc = FO_DELETE
            op.pFrom = ctypes.cast(buf, ctypes.c_void_p)
            op.pTo = None
            op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
            res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
            _ = buf            # 保持缓冲存活到调用结束
            aborted = bool(op.fAnyOperationsAborted)
            # 成功判据（按可靠性从高到低排）：
            #   ① res == 0 且未被中止 —— Windows 文档里的标准成功
            #   ② res != 0 但**调用后文件已不在原位** —— 实测 SHFileOperationW 在
            #      中文/特殊字符路径上有时返回非 0（最常见 2 = ERROR_FILE_NOT_FOUND）
            #      但操作其实生效了：仓库目录已经清空、原件没动、隔离区空。
            #      原 if 只看 res，把这种情况判成失败并抛 OSError，导致 fallback_isolate
            #      误启动、用户在审计日志里看到「失败」的红色条目——而他实际上没事。
            # 这条兜底**必须**在「调用前 if not os.path.exists」之后才有意义：
            # 否则「调前就不存在」的也会被它误判成成功，但那种已经在上面 continue 了。
            # 同步性：SHFileOperationW 在 Vista 之后默认是同步的（除非显式带异步 flag，
            # 本实现的 fFlags 里没有），所以调用返回后再 stat 是可靠的。
            file_gone = not os.path.exists(p)
            if (res == 0 and not aborted) or file_gone:
                ok_paths.append(p)
                if res == 0:
                    log("recycle", p)
                else:
                    # 真返回非 0 但文件实际已经被移走 —— 记 WARN 便于日后排查
                    log("recycle", "%s 调用返回 %d（中止=%s）但文件已不在原位，视为成功"
                        % (p, res, aborted), level="WARN")
            else:
                raise OSError("SHFileOperationW 返回 %d%s（文件仍在原位，操作真失败）"
                              % (res, "（操作被中止）" if aborted else ""))
        except Exception as ex:
            if fallback_isolate:
                try:
                    dst = isolate(p, tag="recycled")
                    fell.append((p, dst))
                    log("recycle-fallback", "回收站不可用（%s），已改放隔离区: %s" % (ex, dst),
                        level="WARN", ok=False)
                    continue
                except Exception as ex2:
                    failed.append((p, "%s；隔离降级也失败: %s" % (ex, ex2)))
                    continue
            failed.append((p, str(ex)))
            log("recycle", "%s 失败: %s" % (p, ex), level="ERROR", ok=False)
    return {"ok": ok_paths, "failed": failed, "fallback": fell}


# --------------------------------------------------------------------------- #
# 隔离区（卸载 = 移出视野，不销毁）
# --------------------------------------------------------------------------- #
def _origin_path(item_path: str) -> str:
    return item_path + ".origin.json"


def isolate(path: str, tag: str = "", origin: str | None = None) -> str:
    """把文件/目录移进隔离区，返回新路径。绝不销毁。

    同时写一个 `<条目>.origin.json` 边车，记录它的**原始位置**。
    靠这个名字来还原是踩过坑的：隔离前的名字会被加上「时间戳__标签__」前缀，
    只用正则往回剥，遇到本来就带下划线的文件名就会剥错（实测 fake.vpk 还原成了
    link__fake.vpk）。记在边车里才是可靠的。
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    src_abs = os.path.abspath(path)
    name = os.path.basename(src_abs.rstrip("\\/"))
    if tag:
        name = "%s__%s" % (tag, name)
    dst = os.path.join(STAGING_DIR, "%s__%s" % (stamp(), name))
    i = 1
    while os.path.exists(dst) or os.path.exists(_origin_path(dst)):
        dst = os.path.join(STAGING_DIR, "%s__%s__%d" % (stamp(), name, i))
        i += 1
    shutil.move(src_abs, dst)
    try:
        with open(_origin_path(dst), "w", encoding="utf-8") as fp:
            json.dump({"original_path": origin or src_abs,
                       "original_name": os.path.basename((origin or src_abs).rstrip("\\/")),
                       "isolated_at": time.time(),
                       "tag": tag}, fp, ensure_ascii=False, indent=2)
    except Exception as ex:
        log("isolate", "写边车失败: %s" % ex, level="WARN", ok=False)
    log("isolate", "%s -> %s" % (src_abs, dst))
    return dst


def read_origin(item_path: str) -> dict:
    p = _origin_path(item_path)
    if os.path.isfile(p):
        try:
            # 必须用 with：裸 open(...).read() 依赖 CPython 引用计数回收句柄，
            # 在其他实现或异常路径下会泄漏文件句柄。
            with open(p, "r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception as ex:
            # 边车损坏不该让整个列表崩掉，但必须留痕（原来这里是静默 pass）
            log("origin-read", "边车读取失败 %s: %s" % (p, ex), level="WARN", ok=False)
    # 边车缺失时的兜底：按命名规则尽力剥离
    name = os.path.basename(item_path)
    name = re.sub(r"^\d{8}-\d{6}__", "", name)
    name = re.sub(r"^(?:mod|link|dir|recycled)__", "", name)
    name = re.sub(r"__\d+$", "", name)
    return {"original_path": "", "original_name": name, "isolated_at": 0, "tag": ""}


_STAGING_SIZE_CACHE = "staging_sizes.json"


def _cached_dir_size(name: str, full: str, mtime: float, cache: dict) -> int:
    """隔离区条目的体积：目录走「按 mtime 失效」的缓存。

    隔离区里可能是几百 MB 的 mod 目录，每次 list_staging() 都全量 walk
    一遍纯属浪费；目录 mtime 没变就直接复用上次算好的值，
    内容一变（mtime 变了）立刻重算。
    """
    hit = cache.get(name)
    if isinstance(hit, dict):
        try:
            if float(hit.get("mtime", -1)) == float(mtime):
                return int(hit.get("size", 0))
        except (TypeError, ValueError):
            pass
    size = dir_size(full)
    cache[name] = {"size": size, "mtime": mtime, "computed_at": time.time()}
    return size


def list_staging() -> list[dict]:
    if not os.path.isdir(STAGING_DIR):
        return []
    raw_cache = cache_read(_STAGING_SIZE_CACHE, default=None)
    # 必须拷贝一份：下面 _cached_dir_size() 会就地更新 size_cache，
    # 若直接引用 raw_cache，末尾的 fresh_cache != raw_cache 就永远为假，
    # 缓存文件再也不会被更新（每次调用都白 walk 一遍）。
    size_cache = dict(raw_cache) if isinstance(raw_cache, dict) else {}
    fresh_cache: dict = {}
    out = []
    for fn in os.listdir(STAGING_DIR):
        if fn.endswith(".origin.json"):
            continue
        full = os.path.join(STAGING_DIR, fn)
        is_dir = os.path.isdir(full)
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            mtime = 0.0
        if is_dir:
            size = _cached_dir_size(fn, full, mtime, size_cache)
        else:
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
        # 回写当前状态（含未变动的条目），顺带把已消失的历史条目挤掉
        hit = size_cache.get(fn)
        if not isinstance(hit, dict) or hit.get("size") != size or hit.get("mtime") != mtime:
            hit = {"size": size, "mtime": mtime, "computed_at": time.time()}
        fresh_cache[fn] = hit
        org = read_origin(full)
        out.append({
            "name": fn,
            "path": full,
            "is_dir": is_dir,
            "size": size,
            "mtime": mtime,
            "original_path": org.get("original_path", ""),
            "original_name": org.get("original_name", ""),
            "original_dir": os.path.dirname(org.get("original_path", "")) if org.get("original_path") else "",
            "isolated_at_h": human_time(org.get("isolated_at", 0)),
            "origin_recorded": bool(org.get("original_path")),
        })
    # 只保留当前仍在隔离区的条目，缓存文件不会随历史无限膨胀
    if fresh_cache != raw_cache:
        cache_write(_STAGING_SIZE_CACHE, fresh_cache)
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def dir_size(path: str) -> int:
    total = 0
    for root, _d, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return "%.2f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return "%.2f GB" % n


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #
def cache_write(name: str, obj) -> None:
    try:
        with open(os.path.join(CACHE_DIR, name), "w", encoding="utf-8") as fp:
            json.dump(obj, fp, ensure_ascii=False)
    except Exception as ex:
        log("cache-write", "%s: %s" % (name, ex), level="WARN", ok=False)


def cache_read(name: str, default=None):
    p = os.path.join(CACHE_DIR, name)
    if not os.path.isfile(p):
        return default
    try:
        with open(p, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return default


def is_windows() -> bool:
    return sys.platform.startswith("win")
