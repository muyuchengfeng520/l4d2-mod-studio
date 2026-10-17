# -*- coding: utf-8 -*-
"""把 mod 弄进系统：一键入库 + 创意工坊订阅下载。

两条入口，一个落地面
------------------
* **一键入库**（ingest）：给一个本地 VPK 文件，自动判定分类 → 建 FireAxe 式
  仓库结构 → 写 .workshop 元数据 → addons 建符号链接 → addonlist.txt 登记。
  不依赖 Steam，任何渠道搞到的 VPK 都能一键进体系。
* **创意工坊订阅**（workshop）：走 SteamCMD 的 `workshop_download_item 550 <id>`
  下载，产物 VPK 喂给同一个 ingest 落地。后台线程执行，进度可查。

仓库结构（实测 FireAxe 仓库反推的规律，见 paths.autodetect_repo）
-----------------------------------------------------------------------------
    <repo>/<NN>-<分类名>/<标题>.workshop/<标题>-time_updated-<ts>.vpk
                                     └─ .workshop       元数据 JSON
    addons/<链接名>.vpk -> 上面那个实体（符号链接）

.nNB-分类名 的序号与 taglib.CATEGORIES 的 order 字段一致（survivor=1 → 01-幸存者，
misc=99 → 99-其他未分类）。目录名**动态探测**（REPO_DIR_PATTERN），不写死——
用户仓库里目录叫别的也能认，缺的目录按需创建。

红线（与 core 其他模块同一套，不可绕过）
--------------------------------------
1. 写操作前先备份（addonlist 改写走 manager.write_addonlist，它内部备份）
2. 原子写：先 tmp 再 os.replace（元数据 JSON、addonlist）
3. 删除一律走回收站（safety.to_recycle_bin），绝不用 unlink 玩家文件
4. 批量 ≤ manager.BATCH_LIMIT，出错立刻停
5. 所有动作留痕到 audit.log
6. **符号链接创建后必须验证**（lexists + islink 双验），不信 API 返回值——
   实测某些环境 CreateSymbolicLinkW 返回成功但文件系统里什么都没有
   （WorkBuddy CLI 的 hook 层会拦）。验证不过 → 降级复制 + 如实告知。
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import threading
import time

from . import manager, paths, safety, scan, taglib as tl
from .vpk import Vpk, VpkError

# 仓库分类目录名格式：NN-名称（01-幸存者 / 99-其他未分类）
REPO_DIR_PATTERN = re.compile(r"^(\d{1,3})[-_\s](.+)$")

# 链接名前缀。用 studio_ 而不是 fireaxe_——那是 FireAxe 的命名空间，
# 混在一起以后分不清哪个工具建的，清理时容易误伤。
LINK_PREFIX = "studio_link"

# SteamCMD 下载 L4D2（App 550）创意工坊内容的产物根目录
STEAM_APP_ID = "550"

# 单个 item 下载超时（秒）。创意工坊 item 从几百 KB 到几百 MB 都有，
# 大 item 给足时间；超时后任务标记失败并保留已下载部分供重试。
DOWNLOAD_TIMEOUT = 900

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _h(n: float) -> str:
    return safety.fmt_size(n)


def _repo_category_dir(repo_dir: str, cat: str) -> str:
    """算出分类在仓库里的目录名（动态探测 → 按需创建）。

    优先用仓库里**已有的**目录：扫一遍 repo_dir，把 `NN-名称` 目录按
    「序号 == CATEGORIES[cat].order」对上号。对不上就按规律造一个
    （`%02d-%s`），ingest 执行时创建。
    """
    order = tl.CATEGORIES.get(cat, tl.CATEGORIES["misc"])["order"]
    zh = tl.CATEGORIES.get(cat, tl.CATEGORIES["misc"])["zh"]
    if os.path.isdir(repo_dir):
        for name in os.listdir(repo_dir):
            m = REPO_DIR_PATTERN.match(name)
            if m and int(m.group(1)) == order and os.path.isdir(os.path.join(repo_dir, name)):
                return name
    return "%02d-%s" % (order, zh)


def _unique_vpk_name(addons_dir: str, base: str) -> str:
    """在 addons 里生成不冲突的 VPK 名：<标题>.vpk / <标题> (2).vpk …

    为什么链接名直接用标题而不是 studio_link_N：符号链接靠 realpath 反推标题，
    硬链接与副本都反推不了，扫描器只能从文件名读 —— 用编号命名会让界面和
    游戏内 addon 列表都只剩一个 studio_link_3 这样的编号。
    """
    name = base + ".vpk"
    n = 2
    while os.path.lexists(os.path.join(addons_dir, name)):
        name = "%s (%d).vpk" % (base, n)
        n += 1
    return name


def _next_link_name(addons_dir: str, taken: set) -> str:
    """生成不冲突的链接名：studio_link_1.vpk / studio_link_2.vpk ..."""
    i = 1
    while True:
        name = "%s_%d.vpk" % (LINK_PREFIX, i)
        if name not in taken and not os.path.lexists(os.path.join(addons_dir, name)):
            return name
        i += 1


def _vpk_title(vpk_path: str, addoninfo: dict, title_hint: str = "") -> str:
    """标题优先级：外部提示（创意工坊 title）> addoninfo.addontitle > 文件名。

    title_hint 是「这个 VPK 从哪来」的权威标题：直下时 Steam Web API 的 title
    比 VPK 内部的 addontitle 更可靠（有些作者不写 addoninfo，或写得跟创意工坊
    标题不一致）。没有外部提示时才退回 VPK 内部与文件名。
    """
    t = (title_hint or "").strip()
    if t:
        return t
    t = (addoninfo or {}).get("addontitle", "").strip()
    if t:
        return t
    base = os.path.basename(vpk_path)
    base = re.sub(r"[-_]time[-_]updated[-_]\d+", "", base)
    base = os.path.splitext(base)[0].strip()
    return base or "unnamed_mod"


def _sanitize_title(title: str) -> str:
    """标题用作目录名，清掉 Windows 文件名非法字符与首尾空白。"""
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad else c for c in title).strip().strip(".")
    return out[:80] or "unnamed_mod"


def _same_content(a: str, b: str, head: int = 65536) -> bool:
    """轻量内容比对：大小 + 头部 head 字节。VPK 头（目录树）相同且大小相同
    基本可判定同一文件；不做全量哈希（几百 MB 的 mod 会跑很久）。
    """
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read(head) == fb.read(head)
    except OSError:
        return False


def _find_dup(addons: str, dst_vpk: str) -> str:
    """在 addons 里找「已经指向同一个 mod」的条目，返回链接名或空串。

    两种场景都要覆盖：
      * 符号链接 —— realpath 相等（快，不读内容）
      * 降级副本 —— realpath 是自身，比大小 + 头部内容（副本场景专用）
    """
    if not os.path.isdir(addons) or not os.path.isfile(dst_vpk):
        return ""
    dst_size = os.path.getsize(dst_vpk)
    dst_real = os.path.realpath(dst_vpk)
    for n in os.listdir(addons):
        if not n.lower().endswith(".vpk"):
            continue
        lp = os.path.join(addons, n)
        if not os.path.lexists(lp):
            continue
        try:
            if os.path.getsize(lp) != dst_size:
                continue
            if os.path.realpath(lp) == dst_real:
                return n
            if not os.path.islink(lp) and _same_content(lp, dst_vpk):
                return n
        except OSError:
            continue
    return ""


# --------------------------------------------------------------------------- #
# 1) 解析 + 分类（入库的第一步：读懂这个 VPK 是什么）
# --------------------------------------------------------------------------- #
def inspect_vpk(vpk_path: str, cfg: dict, title_hint: str = "") -> dict:
    """读 VPK 内部，判定分类与标题。只读，不碰任何文件。"""
    v = Vpk(vpk_path)
    paths_ = v.list_files()
    ai = {}
    try:
        ai = v.addoninfo()
    except Exception:
        ai = {}

    # 复用 scan 的分类打分：造一个临时 ModRecord 喂给 classify
    rec = scan.ModRecord(key=os.path.basename(vpk_path),
                         title=_vpk_title(vpk_path, ai, title_hint),
                         _vpk_path=vpk_path, _vpaths=paths_, _addoninfo=ai)
    scan.classify(rec, cfg)

    return {
        "ok": True,
        "vpk": vpk_path,
        "size": os.path.getsize(vpk_path),
        "size_h": _h(os.path.getsize(vpk_path)),
        "total_files": len(paths_),
        "title": _vpk_title(vpk_path, ai, title_hint),
        "published_id": str(ai.get("publishedfileid", "") or ""),
        "category": rec.category,
        "category_zh": tl.CATEGORIES[rec.category]["zh"],
        "auto_category": rec.auto_category,
        "category_source": rec.category_source,
        "evidence": rec.evidence[:8],
        "tags": rec.tags,
    }


# --------------------------------------------------------------------------- #
# 2) 入库预演（只读）
# --------------------------------------------------------------------------- #
def plan_ingest(cfg: dict, src_vpk: str, meta: dict = None) -> dict:
    """算出入库后的完整落点，检查冲突。不写任何文件。

    meta（可选）：来自下载渠道的元信息 {title, published_id, time_updated}。
    传入时优先于 VPK 内部的 addoninfo —— 创意工坊标题比作者随手写的
    addontitle 更可信，item id 只有下载渠道知道。
    """
    meta = meta or {}
    if not os.path.isfile(src_vpk):
        return {"ok": False, "error": "文件不存在: %s" % src_vpk}
    if not src_vpk.lower().endswith(".vpk"):
        return {"ok": False, "error": "只支持 .vpk 文件（当前: %s）" % os.path.basename(src_vpk)}
    repo_dir = cfg.get("repo_dir", "")
    if not repo_dir:
        return {"ok": False, "error": "还没配置 mod 仓库目录（设置页探测或手动填一个）"}
    if not os.path.isdir(repo_dir):
        # 配置里有路径 = 用户选定或 bootstrap 给的默认位置 → 首次入库直接建出来，
        # 不让用户手动 mkdir。建不出来（盘符不存在/无权限）再如实报错。
        try:
            os.makedirs(repo_dir, exist_ok=True)
        except OSError as ex:
            return {"ok": False,
                    "error": "仓库目录无法创建: %s（%s）" % (repo_dir, ex)}

    try:
        info = inspect_vpk(src_vpk, cfg, meta.get("title", ""))
        # 外部来源的 item id 覆盖 VPK 内部解析结果（内部通常没有这个字段）
        if meta.get("published_id"):
            info["published_id"] = str(meta["published_id"])
        if meta.get("time_updated"):
            info["src_time_updated"] = int(meta["time_updated"])
    except VpkError as ex:
        return {"ok": False, "error": "VPK 解析失败: %s" % ex}
    except Exception as ex:
        return {"ok": False, "error": "读取失败: %s: %s" % (type(ex).__name__, ex)}

    cat_dir = _repo_category_dir(repo_dir, info["category"])
    ws_dir = os.path.join(repo_dir, cat_dir, "%s.workshop" % _sanitize_title(info["title"]))
    # VPK 落库文件名沿用 FireAxe 规律：<标题>-time_updated-<ts>.vpk
    # ts 优先取外部来源的真实更新时间（创意工坊的 time_updated），
    # 拿不到才退回文件修改时间 —— 否则下载时间会污染版本号。
    ts = int(info.get("src_time_updated") or os.path.getmtime(src_vpk))
    dst_name = "%s-time_updated-%d.vpk" % (_sanitize_title(info["title"]), ts)
    dst_vpk = os.path.join(ws_dir, dst_name)

    # 冲突检查：目标 .workshop 目录已存在 / 目标 VPK 已存在 / addons 已有同内容链接
    clash_dir = os.path.isdir(ws_dir)
    clash_vpk = os.path.isfile(dst_vpk)
    addons = paths.addons_dir(cfg)
    existing = [n for n in (os.listdir(addons) if os.path.isdir(addons) else [])
                if n.lower().endswith(".vpk")]
    # 已有一个链接指向同一个实体 = 重复入库
    dup_link = ""
    if os.path.isdir(addons):
        for n in existing:
            try:
                if os.path.realpath(os.path.join(addons, n)) == os.path.realpath(dst_vpk):
                    dup_link = n
                    break
            except OSError:
                continue

    return {
        "ok": True,
        "info": info,
        "repo_dir": repo_dir,
        "cat_dir": cat_dir,
        "ws_dir": ws_dir,
        "dst_vpk": dst_vpk,
        "dst_name": dst_name,
        "clash_dir": clash_dir,
        "clash_vpk": clash_vpk,
        "dup_link": dup_link,
        "note": ("目标路径不存在，将创建；addons 链接与 addonlist 登记会自动完成"
                 if not (clash_dir or clash_vpk or dup_link) else
                 "检测到已存在的内容，执行前请确认覆盖/跳过"),
    }


# --------------------------------------------------------------------------- #
# 3) 符号链接（创建 + 验证 + 降级）
# --------------------------------------------------------------------------- #
def _create_link(link_path: str, target: str) -> tuple[bool, str]:
    """建符号链接，返回 (成功?, 方式说明)。

    ⚠️ 不信 API 返回值：实测（WorkBuddy CLI 环境）CreateSymbolicLinkW 返回 1
    但文件系统里什么都没有，os.symlink 或抛 WinError 2 或静默建成副本。
    所以创建后必须 lexists + islink + samefile 三验，全过才认。
    三验不过 → 返回失败，由调用方降级为复制。
    """
    target = os.path.abspath(target)
    if not os.path.isfile(target):
        return False, "目标文件不存在: %s" % target
    # 先试 Python 标准库（开发者模式/管理员下可用）
    try:
        if not os.path.lexists(link_path):
            os.symlink(target, link_path)
    except OSError:
        pass
    ok = _verify_link(link_path, target)
    if ok:
        return True, "符号链接"
    # 标准库不行 → Windows 原生 API（带 ALLOW_UNPRIVILEGED_CREATE 标志）
    if not os.path.lexists(link_path):
        try:
            ctypes.windll.kernel32.CreateSymbolicLinkW(link_path, target, 0x2)
        except Exception:
            pass
    ok = _verify_link(link_path, target)
    if ok:
        return True, "符号链接"
    # 都失败：清掉可能留下的残骸（半成品链接/空文件），让调用方降级
    if os.path.lexists(link_path):
        try:
            if not os.path.islink(link_path) and os.path.getsize(link_path) == 0:
                os.unlink(link_path)
        except OSError:
            pass
    # ③ 最后试硬链接：Windows 上不需要任何特权，只要同卷 + NTFS。
    #    仓库与 addons 通常在同一盘（本机都在 E:），命中率很高。
    ok2, why2 = _create_hardlink(link_path, target)
    if ok2:
        return True, why2
    return False, "符号链接与硬链接都不可用（%s）；跨卷时需管理员身份或开启 Windows 开发者模式" % why2


def probe_link_support() -> dict:
    """自检：当前进程能否创建符号链接 / 硬链接。

    在 data/cache 下建临时文件试一次，验证语义后立即清理。
    这是给排障用的——「为什么 addons 里是副本」这类问题，一次自检就有答案。
    """
    import tempfile
    out = {"symbolic": False, "hardlink": False, "same_volume": True, "errors": {}}
    cache = os.path.join(safety.DATA_DIR, "cache")
    os.makedirs(cache, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="linkprobe_", dir=cache)
    target = os.path.join(tmp, "target.vpk")
    try:
        with open(target, "wb") as fp:
            fp.write(b"probe" * 100)
        # ① 符号链接
        p1 = os.path.join(tmp, "sym.vpk")
        try:
            os.symlink(target, p1)
            out["symbolic"] = bool(os.path.islink(p1)
                                   and os.path.samefile(p1, target))
            if not out["symbolic"]:
                out["errors"]["symbolic"] = "创建成功但验证失败（islink/samefile 不符）"
        except OSError as ex:
            out["errors"]["symbolic"] = "%s" % ex
        if os.path.lexists(p1):
            try:
                os.unlink(p1)
            except OSError:
                pass
        # ② 硬链接
        p2 = os.path.join(tmp, "hard.vpk")
        ok2, why2 = _create_hardlink(p2, target)
        out["hardlink"] = ok2
        if not ok2:
            out["errors"]["hardlink"] = why2
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)
    # ③ 仓库与 addons 是否同卷（硬链接的前提）
    try:
        cfg = paths.load()
        r = cfg.get("repo_dir", "")
        a = paths.addons_dir(cfg)
        if r and a and os.path.exists(r) and os.path.exists(a):
            out["same_volume"] = (os.stat(r).st_dev == os.stat(a).st_dev)
            out["repo"] = r
            out["addons"] = a
    except Exception:
        pass
    out["note"] = (
        "符号链接可用：新建 mod 会建真链接（FireAxe 同款形态）"
        if out["symbolic"] else
        "符号链接不可用，但同卷硬链接可用：新建 mod 会建硬链接（同一份数据、不占双倍空间，"
        "文件列表里显示为普通文件）。想要带箭头的符号链接：开启 Windows 开发者模式，"
        "或让本程序以管理员身份运行。"
        if out["hardlink"] and out["same_volume"] else
        "链接都不可用：新建 mod 会复制进 addons（占双倍空间）。"
        "把仓库与游戏放在同一分区（硬链接）或开启开发者模式（符号链接）可解决。"
    )
    return out


def _create_hardlink(link_path: str, target: str) -> tuple:
    """建硬链接。返回 (成功?, 说明)。

    **不需要任何特权**（Windows 普通用户即可）——实测 CreateHardLinkW 返回 1。
    Vortex 的默认部署方式就是硬链接；符号链接才需要管理员/开发者模式。
    限制：必须同一卷（同一分区）+ NTFS；只对文件有效。
    """
    if os.path.lexists(link_path):
        return False, "目标已存在"
    try:
        ok = ctypes.windll.kernel32.CreateHardLinkW(link_path, target, None)
    except Exception as ex:
        return False, "CreateHardLinkW 异常: %s" % ex
    if not ok:
        return False, "CreateHardLinkW 失败（错误码 %s）" % ctypes.get_last_error()
    # 验证：必须是同一文件（硬链接的判据是 samefile，不是 islink）
    try:
        if os.path.samefile(link_path, target):
            return True, "硬链接"
    except OSError:
        pass
    return False, "硬链接创建后校验失败"


def link_entries(addons_dir: str, vault_path: str) -> list:
    """列出 addons 里当前指向 vault_path 的条目名（符号链接或硬链接）。

    必须在**写操作之前**调用：os.replace 换了 inode 之后，硬链接条目就再也
    认不出自己原来指向谁了（samefile 变 False）。
    """
    names = []
    if not os.path.isdir(addons_dir) or not os.path.isfile(vault_path):
        return names
    for n in os.listdir(addons_dir):
        p = os.path.join(addons_dir, n)
        if not os.path.lexists(p):
            continue
        try:
            if os.path.samefile(p, vault_path):
                names.append(n)
        except OSError:
            continue
    return names


def refresh_links(addons_dir: str, vault_path: str, names: list) -> list:
    """写操作（os.replace）之后重建断掉的硬链接。

    符号链接不用管：它指向路径，replace 之后自动跟随新文件。
    硬链接指向 inode，replace 换了 inode 就断了 —— 这里删掉旧条目重新建。
    ⚠️ unlink 硬链接**不是删除数据**：只是减一个引用计数，实体仍在 vault 里，
    所以这里不走回收站（走了反而会把还在用的 vpk 一起搬走）。
    """
    fixed = []
    for n in names:
        p = os.path.join(addons_dir, n)
        if not os.path.lexists(p):
            continue
        if os.path.islink(p):
            continue                      # 符号链接自动跟随，无需处理
        try:
            if os.path.samefile(p, vault_path):
                continue                  # 还是同一个文件，没断
        except OSError:
            pass
        try:
            os.unlink(p)                  # 切断这条旧硬链接（数据不受影响）
        except OSError:
            continue
        ok, _how = _create_hardlink(p, vault_path)
        fixed.append((n, ok))
    return fixed


def _verify_link(link_path: str, target: str) -> bool:
    """链接三验：存在 + 是链接 + 与目标同一文件。"""
    try:
        if not os.path.lexists(link_path) or not os.path.islink(link_path):
            return False
        return os.path.samefile(link_path, target)
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# 4) 入库执行
# --------------------------------------------------------------------------- #
def ingest(cfg: dict, src_vpk: str, mode: str = "move", meta: dict = None) -> dict:
    """执行入库。mode: move（成功后源文件进回收站，默认）| copy（保留源文件）。

    ⚠️ 为什么 move 不是「直接 shutil.move」：move 完源文件就没了，后面任何一步
    失败都没法把文件搬回去 —— 玩家丢文件。改成**先复制进仓库，全部步骤成功后
    才把源文件移回收站**（符合「删除走回收站」红线，且任何一步失败源文件都
    还在原处）。

    返回 {ok, ...}；任何一步失败都已回滚，不留半成品。
    """
    p = plan_ingest(cfg, src_vpk, meta)
    if not p.get("ok"):
        return p
    info = p["info"]
    repo_dir, ws_dir, dst_vpk = p["repo_dir"], p["ws_dir"], p["dst_vpk"]
    addons = paths.addons_dir(cfg)
    log = []

    # 0) 已存在同样内容的链接 = 重复入库，直接拒绝（不产生第二份）
    if p["dup_link"]:
        return {"ok": False, "error": "这个 mod 已经在 addons 里了（%s），无需重复入库"
                % p["dup_link"]}

    done: list[str] = []          # 已完成的步骤（失败时按逆序回滚）
    link_name = ""
    try:
        # 1) 建 .workshop 目录
        os.makedirs(ws_dir, exist_ok=True)
        log.append("创建目录 %s" % ws_dir)

        # 2) 复制 VPK 进仓库（源文件最后才处理，见函数注释）
        if p["clash_vpk"]:
            # 目标已存在：先备份原文件（走备份区，不直接覆盖）
            safety.backup_file(dst_vpk, tag="ingest-clash")
            log.append("目标 VPK 已存在，原件已备份")
        shutil.copy2(src_vpk, dst_vpk)
        done.append("vpk")
        log.append("%s -> %s（%s）" % (os.path.basename(src_vpk), dst_vpk, _h(info["size"])))

        # 2.5) 复入库检测的第二道：VPK 已进仓库，此时可以比内容了。
        #      plan 阶段的 dup_link 只覆盖符号链接场景（realpath 相等）；
        #      降级副本的 realpath 是自身，必须在这里用「同大小+头部内容」补判。
        dup = _find_dup(addons, dst_vpk)
        if dup:
            raise RuntimeError("addons 里已有同一个 mod（%s），重复入库已中止" % dup)
        log.append("重复入库检测通过")

        # 3) 写 .workshop 元数据（原子写：tmp + replace）
        ts = int(info.get("src_time_updated") or os.path.getmtime(dst_vpk))
        meta = {
            "PublishedFileId": int(info["published_id"]) if info.get("published_id", "").isdigit() else 0,
            "TimeUpdated": ts,
            "CurrentFile": os.path.basename(dst_vpk),
        }
        meta_tmp = os.path.join(ws_dir, ".workshop.tmp")
        with open(meta_tmp, "w", encoding="utf-8") as fp:
            json.dump(meta, fp, ensure_ascii=False, indent=2)
        os.replace(meta_tmp, os.path.join(ws_dir, ".workshop"))
        done.append("meta")
        log.append("写入元数据 .workshop")

        # 4) addons 建链接：符号链接 -> 硬链接 -> 副本，名字统一用标题
        link_name = _unique_vpk_name(addons, _sanitize_title(info["title"]))
        link_path = os.path.join(addons, link_name)
        os.makedirs(addons, exist_ok=True)
        link_ok, link_how = _create_link(link_path, dst_vpk)
        if not link_ok:
            # 降级：链接一律建不了才复制（跨卷 + 无特权时才会走到这）
            shutil.copy2(dst_vpk, link_path)
            link_how = "副本（降级）"
            log.append("链接不可用，已降级为复制：%s" % link_name)
        else:
            log.append("链接 %s -> %s" % (link_name, os.path.basename(dst_vpk)))
        done.append("link")

        # 5) addonlist 登记（manager 内部保持格式 + 备份）
        al = paths.addonlist_path(cfg)
        res = manager.set_enabled(al, {link_name: True})
        if not res.get("ok"):
            raise RuntimeError("addonlist 登记失败: %s" % res.get("error", "未知"))
        log.append("addonlist.txt 登记 %s（启用）" % link_name)

    except Exception as ex:
        # 回滚：按已完成步骤逆序清理，绝不留半成品在玩家目录里。
        # 源文件全程没动（步骤 2 是复制），所以玩家的文件安然无恙。
        rolled = _rollback(done, ws_dir, dst_vpk, addons, link_name)
        safety.log("ingest", "入库失败，已回滚（%s）：%s" % (rolled, ex),
                   level="ERROR", ok=False)
        return {"ok": False, "error": "入库失败（已回滚）: %s" % ex, "rolled_back": rolled}

    # 6) 全部成功，move 模式才处理源文件：进回收站（不是 unlink！）
    if mode == "move":
        r = safety.to_recycle_bin([src_vpk])
        if r["ok"]:
            log.append("源文件已移入回收站（可在回收站还原）")
        else:
            log.append("⚠ 源文件移回收站失败（%s），仓库侧已完成，源文件请手动处理"
                       % (r["failed"] or ["未知"]))

    safety.log("ingest", "%s -> %s / %s（%s，%s，%d 文件）"
               % (info["title"], p["cat_dir"], link_name, link_how,
                  _h(info["size"]), info["total_files"]))
    return {
        "ok": True,
        "title": info["title"],
        "category": info["category"],
        "category_zh": info["category_zh"],
        "ws_dir": ws_dir,
        "vpk": dst_vpk,
        "link": link_name,
        "link_mode": link_how,
        "size_h": info["size_h"],
        "total_files": info["total_files"],
        "log": log,
        "note": ("addons 里是符号链接，改仓库实体直接生效"
                 if link_how == "符号链接" else
                 "addons 里是硬链接（同一份数据，不占双倍空间；写操作后工具会自动重建）"
                 if link_how == "硬链接" else
                 "⚠ 链接不可用，addons 里是独立副本（占双倍空间、不随实体更新）。"
                 "同盘环境下正常不该出现；若出现请检查两处是否在同一分区/NTFS。"),
    }


def _rollback(done: list, ws_dir: str, dst_vpk: str, addons: str, link_name: str) -> list:
    """失败回滚。链接/VPK 一律走回收站（不 unlink 玩家文件）。

    源文件不在回滚范围——ingest 是「先复制后处理」的，源文件从头到尾没动过。
    """
    rolled = []
    if "link" in done and link_name:
        lp = os.path.join(addons, link_name)
        if os.path.lexists(lp):
            r = safety.to_recycle_bin([lp])
            if r["ok"]:
                rolled.append("回收链接 %s" % link_name)
            # addonlist 此时还没登记（登记是最后一步，失败即未写盘），无需摘除
    if "meta" in done:
        side = os.path.join(ws_dir, ".workshop")
        if os.path.isfile(side):
            safety.to_recycle_bin([side])
            rolled.append("回收元数据")
    if "vpk" in done:
        if os.path.isfile(dst_vpk):
            safety.to_recycle_bin([dst_vpk])
            rolled.append("回收已复制的 VPK")
    # .workshop 目录空了就一起收走
    if os.path.isdir(ws_dir) and not os.listdir(ws_dir):
        safety.to_recycle_bin([ws_dir])
        rolled.append("回收空目录")
    return rolled


# --------------------------------------------------------------------------- #
# 5) SteamCMD：探测 / 代下载安装 / 订阅下载
# --------------------------------------------------------------------------- #
# Valve 官方 CDN 的 SteamCMD 安装包（developer.valvesoftware.com 提供的固定地址）。
# 为什么是「代下载」而不是把 steamcmd.exe 打进 exe：Valve 的 Steam 安装协议
# 1.B(iii) 允许 repackage 后随其他软件分发，但**禁止以任何方式把 SteamCMD
# 集成进另一个程序**；1.B(v) 还要求附带协议并让终端用户接受。
# 代下载方案里二进制始终从 Valve 官方直达用户机器，本工具只是下载器+解压器
# （性质同 pip 装依赖），完全绕开再分发条款；首次安装前界面展示知情说明。
STEAMCMD_ZIP_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
# 下载超时（秒）。zip 只有几 MB，给 300 秒足够覆盖慢网络。
INSTALL_TIMEOUT = 300


def steamcmd_dir() -> str:
    """SteamCMD 的安装/工作目录。

    ⚠️ **必须纯 ASCII**：Valve 的 bootstrapper 拒绝在含非英文字符的路径下
    运行 —— 实测装在 `E:\\...\\工作\\...\\data\\steamcmd\\` 下，`steamcmd +quit`
    直接 Fatal Error：
        !!! Fatal Error: %appname% cannot run from a folder path that
        includes non-English characters. Please re-install Steam to the
        default folder.
    所以**不能**用 data/（它跟 exe 同级，而项目路径可能含中文，本机就是）。
    改用 %LOCALAPPDATA%\\L4D2ModStudio\\steamcmd\\ —— 用户级持久、纯 ASCII。
    代价：换机器要重装（一键安装，无感）；它不是用户数据，不值得为它
    牺牲「能用」。
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "L4D2ModStudio", "steamcmd")


def detect_steamcmd() -> str:
    """找 SteamCMD：本工具装过的位置优先 → 常见安装位置 → PATH。找不到返回空串。"""
    # ① 本工具 data/steamcmd/（代下载安装的落点）
    own = os.path.join(steamcmd_dir(), "steamcmd.exe")
    if os.path.isfile(own):
        return own
    cands = []
    for d in paths.DRIVES:
        cands += [r"%s:\SteamCMD\steamcmd.exe" % d,
                  r"%s:\Program Files (x86)\SteamCMD\steamcmd.exe" % d,
                  r"%s:\Program Files\SteamCMD\steamcmd.exe" % d]
    cands.append("steamcmd")
    for c in cands:
        if c == "steamcmd":
            try:
                r = subprocess.run(["where", "steamcmd"], capture_output=True,
                                   timeout=10, text=True, errors="replace",
                                   creationflags=_NO_WIN)
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout.splitlines()[0].strip()
            except Exception:
                pass
        elif os.path.isfile(c):
            return c
    return ""


def detect_network_env() -> dict:
    """探测当前网络环境对 SteamCMD 友不友好。

    实测定律（Windows + Steam++ 类代理环境，2026-10）：
      * Steam++ 之流把 Steam 域名劫持进 hosts（127.0.0.1）+ 本地加速服务只接
        它名单里的程序 → SteamCMD 连更新服务器直接 http error 0，六连败；
      * .NET 程序（FireAxe）默认读系统 PAC 代理，能蹭上加速；Python/curl/
        SteamCMD 谁都不读 PAC → 全死。
    所以下载前先探测：hosts 有劫持条目 / PAC 端口不通 → 直接走浏览器路线，
    别让用户对着「进行中」干等两分钟再看报错。
    """
    hijacks = []
    pac_port = 0
    pac_alive = False
    try:
        import winreg
        hosts_path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                  "System32", "drivers", "etc", "hosts")
        if os.path.isfile(hosts_path):
            with open(hosts_path, "r", encoding="utf-8", errors="replace") as fp:
                for line in fp:
                    line = line.strip()
                    if line.startswith("#") or not line:
                        continue
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] in ("127.0.0.1", "0.0.0.0"):
                        host = parts[1].lower()
                        if any(k in host for k in ("steam", "akamai", "steampowered")):
                            hijacks.append(host)
        # 系统 PAC（Steam++ 的本地代理脚本）
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        pac_url, _ = winreg.QueryValueEx(k, "AutoConfigURL")
        winreg.CloseKey(k)
        m = re.search(r":(\d{2,5})", pac_url or "")
        if m:
            pac_port = int(m.group(1))
            import socket
            s = socket.socket()
            s.settimeout(1.5)
            try:
                s.connect(("127.0.0.1", pac_port))
                pac_alive = True
            except OSError:
                pass
            finally:
                s.close()
    except Exception:
        pass
    # 判定：有劫持且 PAC 没活着 → SteamCMD 必死（实测定律）
    blocked = bool(hijacks) and not pac_alive
    return {
        "ok": True,
        "steamcmd_usable": not blocked,
        "hijacked_hosts": hijacks[:8],
        "pac_port": pac_port,
        "pac_alive": pac_alive,
        "reason": ("检测到 hosts 劫持（%d 个 Steam 域名指向本机）且本地加速代理"
                   "（PAC 端口 %s）不在线 —— SteamCMD 的更新/下载通道连不出去，"
                   "这是 Steam++ 类工具的已知半开状态。"
                   % (len(hijacks), pac_port or "未配置")
                   if blocked else ""),
    }




# --------------------------------------------------------------------------- #
# 5.5) 全自动直下（PowerShell 代驾）：拿直链 + 下载 + 入库，用户零操作
#
# 为什么需要它：Python urllib 不读 Windows 的 PAC 自动代理脚本，在 Steam++ 之
# 类把 Steam 域名劫持进 hosts 的环境里必死（实测 http error 0）。而 PowerShell
# 基于 .NET，默认走系统代理（含 PAC）—— 同一台机器上实测：它能拿 Steam Web API
# 的 200 响应，也能从 cdn.steamusercontent.com 下到合法 VPK（文件头 34 12 AA 55）。
# 所以：Python 管业务，PowerShell 管「联网」，两个进程各干各的。
# --------------------------------------------------------------------------- #
STEAM_API_DETAILS = ("https://api.steampowered.com/ISteamRemoteStorage/"
                     "GetPublishedFileDetails/v1/")


# 无控制台的 exe 调控制台子进程时，Windows 会给子进程新开一个黑窗口。
# CREATE_NO_WINDOW（0x08000000）让子进程不分配控制台 —— 所有 subprocess
# 调用都要带它，否则用户会看到 PowerShell/steamcmd 的黑框一闪而过。
_NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _ps_exe() -> str:
    """挑一个 PowerShell 可执行文件：**优先 pwsh 7**。

    ⚠️ 为什么优先 7 而不是系统自带的 5.1（实测踩过）：
      * pwsh 7（.NET Core）会读 HTTP_PROXY / HTTPS_PROXY 环境变量 —— 用户装了
        Clash / v2ray / Steam++ 之类设了环境变量代理时，它能直接用；
      * powershell 5.1（.NET Framework）只认 IE 设置里的 PAC 脚本 —— 而
        Steam++ 的 PAC 服务一关，5.1 就直接裸连撞 hosts 劫持，报「无法连接
        到远程服务器」，同一个网络下 pwsh 7 却能通。
    两条都找不到就退 "powershell"（系统一定有）。
    """
    cands = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "PowerShell", "7", "pwsh.exe"),
        "pwsh",
        "powershell",
    ]
    for c in cands:
        if c in ("pwsh", "powershell"):
            try:
                r = subprocess.run(["where", c], capture_output=True, timeout=5,
                                   text=True, errors="replace",
                                   creationflags=_NO_WIN)
                if r.returncode == 0 and r.stdout.strip():
                    return c
            except Exception:
                pass
        elif os.path.isfile(c):
            return c
    return "powershell"


def _ps_run(script: str, timeout: int = 120) -> tuple:
    """执行一段 PowerShell 脚本（写成 .ps1 临时文件再 -File，避开引号转义地狱）。

    直接 -Command 传长脚本时，引号/反引号/换行会互相掐；落成文件最稳。用完即删。
    """
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".ps1", prefix="l4d2_ps_")
    try:
        # utf-8-sig：PowerShell 5.1 认 BOM，无 BOM 的中文脚本会乱码
        with os.fdopen(fd, "w", encoding="utf-8-sig") as fp:
            fp.write(script)
        r = subprocess.run(
            [_ps_exe(), "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", path],
            capture_output=True, timeout=timeout,
            creationflags=_NO_WIN)
        raw = r.stdout + r.stderr
        # PowerShell 5.1 的管道输出是**本地代码页（GBK）**，按 utf-8 解会乱码
        # （实测「无法连接到远程服务器」变成一堆 ʹ�á�0����）。先试 utf-8，
        # 不行再退 mbcs；两者都错也无所谓，反正是给人看的日志。
        for enc in ("utf-8", "mbcs"):
            try:
                return r.returncode, raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return r.returncode, raw.decode("utf-8", "replace")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def workshop_details(item_id: str) -> dict:
    """用 PowerShell 调 Steam Web API，拿 item 的标题/文件名/大小/直链。

    返回 {ok, filename, file_size, file_url, title} 或 {ok: False, error}。
    """
    if not item_id or not item_id.isdigit():
        return {"ok": False, "error": "item id 不合法: %r" % item_id}
    out_json = os.path.join(safety.DATA_DIR, "cache", "ws_%s.json" % item_id)
    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "try {\n"
        "  $body = 'itemcount=1&publishedfileids[0]=%s'\n"
        "  $r = Invoke-WebRequest -Uri '%s' -Method POST -Body $body "
        "-UseBasicParsing -TimeoutSec 25\n"
        "  [System.IO.File]::WriteAllText('%s', $r.Content, "
        "[System.Text.Encoding]::UTF8)\n"
        "  Write-Output 'OK'\n"
        "} catch {\n"
        "  Write-Output ('ERR ' + $_.Exception.Message)\n"
        "  exit 1\n"
        "}\n"
    ) % (item_id, STEAM_API_DETAILS, out_json)
    rc, out = _ps_run(script, timeout=90)
    if rc != 0 or not out.strip().startswith("OK"):
        return {"ok": False, "error": "Steam API 调用失败：%s" % out.strip()[:200]}
    try:
        with open(out_json, "r", encoding="utf-8-sig") as fp:
            data = json.load(fp)
        det = ((data.get("response") or {}).get("publishedfiledetails") or [{}])[0]
    except Exception as ex:
        return {"ok": False, "error": "解析 API 响应失败: %s" % ex}
    if str(det.get("result")) != "1" or not det.get("file_url"):
        return {"ok": False,
                "error": "这个 item 拿不到下载直链（result=%s）——可能已被删除、"
                         "设为私有，或作者关闭了下载" % det.get("result")}
    return {
        "ok": True,
        "filename": det.get("filename") or ("workshop_%s.vpk" % item_id),
        "file_size": int(det.get("file_size") or 0),
        "file_url": det.get("file_url"),
        "title": det.get("title") or "",
        "time_updated": int(det.get("time_updated") or 0),
    }


def _ps_download(url: str, dest: str, timeout: int = 1800) -> tuple:
    """用 PowerShell 把 url 流式下载到 dest（走系统代理/PAC）。"""
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "try {\n"
        "  $req = [System.Net.HttpWebRequest]::Create('%s')\n"
        "  # HttpWebRequest::Create 不会自动继承系统代理（Proxy 为 null），\n"
        "  # 必须显式取 GetSystemWebProxy —— 它是唯一会读 PAC 的入口。\n"
        "  $req.Proxy = [System.Net.WebRequest]::GetSystemWebProxy()\n"
        "  $req.Proxy.Credentials = [System.Net.CredentialCache]::DefaultCredentials\n"
        "  $req.Timeout = 60000\n"
        "  $req.ReadWriteTimeout = 60000\n"
        "  $req.UserAgent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'\n"
        "  $resp = $req.GetResponse()\n"
        "  $stream = $resp.GetResponseStream()\n"
        "  $fs = [System.IO.File]::Create('%s')\n"
        "  $stream.CopyTo($fs)\n"
        "  $fs.Close(); $stream.Close(); $resp.Close()\n"
        "  Write-Output ('OK ' + (Get-Item '%s').Length)\n"
        "} catch {\n"
        "  Write-Output ('ERR ' + $_.Exception.Message)\n"
        "  exit 1\n"
        "}\n"
    ) % (url, dest, dest)
    rc, out = _ps_run(script, timeout=timeout)
    if rc != 0 or not out.strip().startswith("OK"):
        return False, out.strip()[:300]
    return True, out.strip()


def workshop_download_direct(cfg: dict, item_id: str) -> dict:
    """全自动下载 + 入库：Steam API 拿直链 → PowerShell 下载 → ingest 落地。

    不依赖 SteamCMD（省 100 MB 安装），不弹浏览器（用户零操作）。
    速度受代理转发影响（实测 ~0.14 MB/s），大 mod 挂着等即可。
    """
    if not item_id:
        return {"ok": False, "error": "缺少 item id"}
    job_id = "ug_%d" % int(time.time() * 1000)
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "id": job_id, "kind": "ugc", "item_id": item_id,
            "state": "running", "log": [], "result": None, "percent": 0,
            "started_at": time.time(),
        }

    def run():
        job = _JOBS[job_id]

        def say(msg):
            job["log"].append("[%s] %s" % (time.strftime("%H:%M:%S"), msg))

        tmp_vpk = ""
        try:
            say("取 item 详情（Steam Web API，经 PowerShell 走系统代理）…")
            det = workshop_details(item_id)
            if not det.get("ok"):
                job["state"] = "failed"
                job["result"] = {"ok": False, "error": det.get("error", "取详情失败")}
                return
            size = det["file_size"]
            say("标题：%s" % (det.get("title") or det["filename"]))
            say("大小：%.1f MB | 直链域名：%s"
                % (size / 1048576, det["file_url"].split("/")[2]))
            cache = os.path.join(safety.DATA_DIR, "cache")
            os.makedirs(cache, exist_ok=True)
            tmp_vpk = os.path.join(cache, "ugc_%s.vpk" % item_id)
            if os.path.isfile(tmp_vpk):
                os.remove(tmp_vpk)
            say("开始下载（走系统代理，速度受转发影响）…")
            import threading
            stop = threading.Event()

            def watch():
                last = -1
                while not stop.is_set():
                    try:
                        if os.path.isfile(tmp_vpk) and size:
                            cur = os.path.getsize(tmp_vpk)
                            pct = min(99, int(cur * 100 / size))
                            if pct != last:
                                last = pct
                                job["percent"] = pct
                    except OSError:
                        pass
                    stop.wait(0.8)

            t = threading.Thread(target=watch, daemon=True)
            t.start()
            ok, out = _ps_download(det["file_url"], tmp_vpk, timeout=DOWNLOAD_TIMEOUT)
            stop.set()
            if not ok:
                job["state"] = "failed"
                job["result"] = {"ok": False,
                                 "error": "下载失败（网络/代理）：%s" % out[:200]}
                return
            got = os.path.getsize(tmp_vpk)
            say("下载完成：%.1f MB" % (got / 1048576))
            with open(tmp_vpk, "rb") as fp:
                head = fp.read(4)
            if head != b"\x34\x12\xaa\x55":
                job["state"] = "failed"
                job["result"] = {"ok": False,
                                 "error": "下载到的不是合法 VPK（文件头 %r）——"
                                          "可能是错误页或被代理改写" % head}
                return
            say("VPK 魔数校验通过，开始入库…")
            job["percent"] = 99
            r = ingest(cfg, tmp_vpk, mode="move",
                       meta={"title": det.get("title", ""),
                             "published_id": item_id,
                             "time_updated": det.get("time_updated", 0)})
            if not r.get("ok"):
                job["state"] = "failed"
                job["result"] = {"ok": False, "error": r.get("error", "入库失败")}
                return
            job["percent"] = 100
            job["state"] = "done"
            job["result"] = {"ok": True, "title": r.get("title"),
                             "ingested": [r], "source": "PowerShell 直连 UGC"}
            say("入库完成：%s" % r.get("title"))
        except subprocess.TimeoutExpired:
            job["state"] = "failed"
            job["result"] = {"ok": False,
                             "error": "下载超时（%d 秒），可重试或改用浏览器"
                                      % DOWNLOAD_TIMEOUT}
        except Exception as ex:
            job["state"] = "failed"
            job["result"] = {"ok": False, "error": "%s: %s" % (type(ex).__name__, ex)}
        finally:
            if tmp_vpk and os.path.isfile(tmp_vpk):
                try:
                    os.remove(tmp_vpk)
                except OSError:
                    pass

    threading.Thread(target=run, daemon=True, name="ugc-%s" % item_id).start()
    return {"ok": True, "job": job_id}


def browser_download(item_id: str) -> dict:
    """用系统默认浏览器打开创意工坊页面（浏览器读 PAC，FireAxe 同款通道）。

    不自己下载——把最后一步交给用户：浏览器里点「订阅」或页面上的下载，
    存下 VPK，然后走「一键入库」。比跟 SteamCMD 死磕稳得多。
    """
    if not item_id:
        return {"ok": False, "error": "缺少 item id"}
    url = "https://steamcommunity.com/sharedfiles/filedetails/?id=%s" % item_id
    try:
        import webbrowser
        ok = webbrowser.open(url)
        if not ok:                                   # 某些环境 open 返回 False
            os.startfile(url)                        # type: ignore[attr-defined]
    except Exception:
        try:
            os.startfile(url)                        # type: ignore[attr-defined]
        except Exception as ex:
            return {"ok": False, "error": "打不开浏览器：%s（请手动访问 %s）" % (ex, url)}
    return {
        "ok": True,
        "url": url,
        "hint": "已在浏览器打开创意工坊页面。页面里点「订阅」（免费）或等作者提供"
                "的下载链接，把 .vpk 存到本地后，回到本工具「一键入库」选它即可。",
    }


def parse_workshop_input(text: str) -> str:
    """从用户输入里抠 item id：纯数字 / 创意工坊 URL 都认。"""
    text = (text or "").strip()
    if text.isdigit():
        return text
    m = re.search(r"[?&]id=(\d+)", text)
    if m:
        return m.group(1)
    m = re.search(r"/(\d{6,})(?:/|$)", text)
    if m:
        return m.group(1)
    return ""


def install_steamcmd(ack: bool = False) -> dict:
    """从 Valve 官方 CDN 代下载安装 SteamCMD。ack=False 时要求先知情确认。

    返回 {ok, job} 或 {ok: False, error/need_ack}。装到 data/steamcmd/。
    """
    if not ack:
        return {"ok": False, "need_ack": True,
                "error": "将从 Valve 官方网站（steamcdn-a.akamaihd.net）下载 SteamCMD "
                         "安装包（约 3 MB），解压到 data/steamcmd/；首次运行它会自动 "
                         "更新到约 100 MB。Steam 安装协议适用。确认后开始安装。"}
    if detect_steamcmd():
        return {"ok": True, "already": True, "path": detect_steamcmd()}
    job_id = "sc_%d" % int(time.time() * 1000)
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "id": job_id, "kind": "install", "item_id": "",
            "state": "running", "log": [], "result": None, "percent": 0,
            "started_at": time.time(),
        }

    def run():
        job = _JOBS[job_id]

        def say(msg):
            job["log"].append("[%s] %s" % (time.strftime("%H:%M:%S"), msg))

        import urllib.request
        import zipfile
        dest = steamcmd_dir()
        zip_path = os.path.join(safety.DATA_DIR, "steamcmd_download.zip")
        try:
            os.makedirs(dest, exist_ok=True)
            say("开始下载 %s" % STEAMCMD_ZIP_URL)
            req = urllib.request.Request(STEAMCMD_ZIP_URL,
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=INSTALL_TIMEOUT) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                got = 0
                with open(zip_path, "wb") as fp:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        fp.write(chunk)
                        got += len(chunk)
                        if total:
                            job["percent"] = min(99, int(got * 100 / total))
                            if got % (4 * 1024 * 1024) < len(chunk):
                                say("已下载 %.1f / %.1f MB"
                                    % (got / 1048576, total / 1048576))
            job["percent"] = 99
            say("下载完成（%.2f MB），正在解压…" % (os.path.getsize(zip_path) / 1048576))
            with zipfile.ZipFile(zip_path) as zf:
                # 只解压 steamcmd.exe 与 dll/vdf 等运行必需文件，跳过目录项
                names = [n for n in zf.namelist() if not n.endswith("/")]
                zf.extractall(dest, members=names)
                say("解压 %d 个文件到 %s" % (len(names), dest))
            # 清理下载的 zip（走回收站而不是 unlink）
            safety.to_recycle_bin([zip_path])
            exe = os.path.join(dest, "steamcmd.exe")
            if not os.path.isfile(exe):
                raise RuntimeError("解压完成但没找到 steamcmd.exe（zip 内容结构变了？）")
            job["percent"] = 100
            job["state"] = "done"
            job["result"] = {"ok": True, "path": exe}
            say("安装完成：%s" % exe)
        except Exception as ex:
            job["state"] = "failed"
            job["result"] = {"ok": False,
                             "error": "安装失败：%s: %s（可去 developer.valvesoftware.com "
                                      "手动下载 SteamCMD，或检查网络/代理）"
                                      % (type(ex).__name__, ex)}

    threading.Thread(target=run, daemon=True, name="sc-install").start()
    return {"ok": True, "job": job_id}


def workshop_download(cfg: dict, item_id: str, steamcmd: str = "") -> dict:
    """启动一个后台下载任务，立刻返回 job id。进度轮询 /api/acquire/job。"""
    if not item_id:
        return {"ok": False, "error": "缺少创意工坊 item id"}
    if not steamcmd:
        steamcmd = detect_steamcmd()
    # 环境闸门：hosts 劫持 + PAC 不在线时 SteamCMD 必死（实测定律，见
    # detect_network_env 注释）。别让用户对着「进行中」干等两分钟。
    env = detect_network_env()
    if not env.get("steamcmd_usable") and steamcmd and os.path.realpath(steamcmd).startswith(
            os.path.realpath(steamcmd_dir())):
        # SteamCMD 通道死了，但 PowerShell 能读系统代理（PAC）——直接转全自动直下，
        # 用户无感：不需要装 SteamCMD、不需要开浏览器。直下若也失败（item 私有/
        # 无直链），job 里会如实报错，前端再引导到浏览器兜底。
        return workshop_download_direct(cfg, item_id)
    if not steamcmd:
        return {"ok": False, "need_steamcmd": True,
                "error": "没找到 SteamCMD。它是 Valve 官方的命令行工具（约 100 MB），"
                         "装一个即可；也可以手动指定 steamcmd.exe 的路径。"}
    # SteamCMD 的工作目录决定产物落在哪：<work>/steamapps/workshop/content/550/<id>/
    # work 必须与 steamcmd_dir 同一个纯 ASCII 根（SteamCMD 拒非 ASCII 路径，见函数注释）
    work = steamcmd_dir()
    os.makedirs(work, exist_ok=True)
    job_id = "ws_%d" % int(time.time() * 1000)
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "id": job_id, "kind": "workshop", "item_id": item_id,
            "state": "running", "log": [], "result": None, "percent": 0,
            "started_at": time.time(),
        }

    def run():
        job = _JOBS[job_id]

        def say(msg):
            job["log"].append("[%s] %s" % (time.strftime("%H:%M:%S"), msg))

        try:
            say("启动 SteamCMD 下载 item %s（App %s）..." % (item_id, STEAM_APP_ID))
            # +login anonymous：公开 item 匿名可下。登录态的 item 会失败并如实报错。
            cmd = [steamcmd, "+login", "anonymous",
                   "+workshop_download_item", STEAM_APP_ID, item_id, "+quit"]
            proc = subprocess.Popen(cmd, cwd=work,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace",
                                    creationflags=_NO_WIN)
            out_lines = []
            for line in proc.stdout:
                line = line.rstrip()
                out_lines.append(line)
                if line.strip():
                    say(line.strip()[:120])
            proc.wait(timeout=DOWNLOAD_TIMEOUT)
            # 产物目录：<work>/steamapps/workshop/content/550/<item_id>/
            ugc = os.path.join(work, "steamapps", "workshop", "content",
                               STEAM_APP_ID, item_id)
            vpks = [os.path.join(ugc, f) for f in os.listdir(ugc)
                    if f.lower().endswith(".vpk")] if os.path.isdir(ugc) else []
            if not vpks:
                # 失败归因别猜：把 SteamCMD 原始输出尾部带回去。实测两种典型
                # 失败长得完全不一样——
                #   ① Assertion Failed / Fatal Error → SteamCMD 自身没跑起来
                #      （网络/代理挡了它的更新通道、路径含非 ASCII），跟 item 无关；
                #    ② ERROR! Download item ... failed (Failure) → 真与 item 有关
                #      （私有/已删/需要登录）。
                # 原先统一说「item 可能被删除」把 ① 全误判成 ①② 颠倒，用户
                # 会跑去检查 item 而真正原因是网络。
                raw_tail = [l for l in out_lines if l.strip()][-4:]
                bootstrap_bad = any(("Assertion Failed" in l) or ("Fatal Error" in l)
                                    for l in raw_tail)
                job["state"] = "failed"
                job["result"] = {
                    "ok": False,
                    "bootstrap": bootstrap_bad,
                    "error": ("SteamCMD 自身没能启动（它的输出里有 Assertion/Fatal Error）"
                              "—— 通常是网络或代理挡住了 Steam 的更新通道，"
                              "或安装路径含非 ASCII 字符；这与 item 本身无关。"
                              if bootstrap_bad else
                              "下载完成但没找到 VPK 产物 —— item 可能已被作者删除、"
                              "设为私有，或需要登录 Steam 账号（匿名只能下公开 item）"),
                    "raw": out_lines[-20:],
                }
                return
            say("下载完成：%d 个 VPK" % len(vpks))
            # 多个 VPK 时取最大的（主体通常是最大的那个；附属件 ingest 会一并处理）
            vpks.sort(key=lambda p: -os.path.getsize(p))
            results = []
            for vp in vpks[:manager.BATCH_LIMIT]:
                r = ingest(cfg, vp, mode="copy")   # copy：ugc 产物留给 SteamCMD 管
                r["src"] = vp
                results.append(r)
                if not r.get("ok"):
                    break                            # 出错立刻停（铁律）
            ok_all = all(r.get("ok") for r in results)
            job["state"] = "done" if ok_all else "failed"
            job["result"] = {"ok": ok_all, "ingested": results,
                             "title": results[0].get("title", "") if results else ""}
        except subprocess.TimeoutExpired:
            job["state"] = "failed"
            job["result"] = {"ok": False, "error": "下载超时（%d 秒），可重试"
                             % DOWNLOAD_TIMEOUT}
        except Exception as ex:
            job["state"] = "failed"
            job["result"] = {"ok": False, "error": "%s: %s" % (type(ex).__name__, ex)}

    threading.Thread(target=run, daemon=True, name="ws-dl-%s" % item_id).start()
    return {"ok": True, "job": job_id}


def job_state(job_id: str) -> dict:
    with _JOBS_LOCK:
        j = _JOBS.get(job_id)
        if not j:
            return {"ok": False, "error": "任务不存在: %s" % job_id}
        # 用 j.get 而不是 j[k]：字段是逐步加的（percent 就是后加的），
        # 直接下标取值会让旧任务/缺字段任务整个查询 500（实测踩过）。
        return {"ok": True, "job": {k: j.get(k) for k in
                                    ("id", "kind", "item_id", "state", "log",
                                     "result", "percent")}}


def cancel_job(job_id: str) -> dict:
    with _JOBS_LOCK:
        j = _JOBS.get(job_id)
        if not j:
            return {"ok": False, "error": "任务不存在"}
        if j["state"] == "running":
            j["state"] = "cancelled"
            return {"ok": True}
        return {"ok": False, "error": "任务已结束（%s），无法取消" % j["state"]}
