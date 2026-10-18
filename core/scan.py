# -*- coding: utf-8 -*-
"""扫描 addons 目录 / FireAxe 仓库，建立 mod 记录并完成分类。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field, fields

from . import paths, safety, taglib as tl
from .vpk import Vpk, VpkError


# --------------------------------------------------------------------------- #
# 预编译的正则。原先在 classify() 里每个 mod 都重新 re.compile 一遍
# （21 条路径规则 + 12 条标题规则 × 几百个 mod），现在只在导入时编译一次。
# 这些规则是 taglib 里的**静态**常量，不会随配置变化，所以可以安全地缓存。
_PATH_RX: list[tuple] = [(re.compile(rule), cat, human) for rule, cat, human in tl.PATH_RULES]
_NAME_RX: list[tuple] = [(re.compile(rule, re.I), cat, human)
                         for rule, cat, human in (tl.SHORTHAND + tl.NAME_RULES)]
# addoncontent_xxx 的值里可能用引号指明替换目标，如 '1 "Rochelle"'
_QUOTED_RX = re.compile(r'["\']([^"\']{1,24})["\']')
@dataclass
class ModRecord:
    key: str = ""                 # addons 目录下的文件名（卸载/开关都以它为准）
    title: str = ""               # 人类可读标题
    kind: str = "vpk"             # vpk | link | dir
    addon_path: str = ""
    real_path: str = ""           # 链接解析后的真实 VPK
    repo_dir: str = ""            # FireAxe 的 <标题>.workshop
    repo_category: str = ""       # 仓库分类目录名，如 01-幸存者
    published_id: str = ""
    time_updated: int = 0
    size: int = 0
    mtime: float = 0.0
    enabled: bool = False
    registered: bool = True
    link_ok: bool = True
    parse_error: str = ""
    vpk_version: int = 0
    entry_count: int = 0
    data_start: int = 0
    top_dirs: list = field(default_factory=list)
    # 分类结果
    category: str = "misc"          # 主分类（仓库已人工归类时以仓库为准）
    auto_category: str = "misc"     # 纯按内容判定出的分类
    category_source: str = ""       # 主分类依据来源
    mismatch: bool = False          # 仓库归类与内容判定不一致
    tags: list = field(default_factory=list)          # 素材级标签
    content_tags: list = field(default_factory=list)  # addoninfo 官方内容标签
    evidence: list = field(default_factory=list)
    signature: str = ""
    # 内部路径缓存（不进 JSON，太重）
    _vpaths: list = field(default_factory=list, repr=False, compare=False)
    _vpk_path: str = ""
    # scan_one 顺手解析出的 addoninfo，供 classify() 复用，避免二次解析目录树。
    # None = 尚未解析过；{} = 解析过但没有 addoninfo.txt（两者语义不同，必须区分）。
    _addoninfo: dict | None = field(default=None, repr=False, compare=False)

    def to_json(self) -> dict:
        # 注意：这里**不能**用 dataclasses.asdict()。
        # asdict 会递归深拷贝所有字段，包括 _vpaths —— 一个 pak 类 mod 的
        # _vpaths 可能有上万个字符串，拷完立刻被 pop 掉，纯属白烧 CPU 和内存。
        # 实测几百个 mod 时这一处就是秒级开销，因此改成手工挑字段。
        d = {f.name: getattr(self, f.name) for f in fields(self)
             if not f.name.startswith("_")}
        d["size_h"] = safety.fmt_size(self.size)
        d["updated_h"] = safety.human_time(self.time_updated)
        d["mtime_h"] = safety.human_time(self.mtime)
        d["category_zh"] = tl.CATEGORIES.get(self.category, {}).get("zh", self.category)
        d["color"] = tl.CATEGORIES.get(self.category, {}).get("color", "#888")
        if self.published_id:
            d["workshop_url"] = ("https://steamcommunity.com/sharedfiles/filedetails/?id="
                                 + self.published_id)
        return d


# --------------------------------------------------------------------------- #
def read_addonlist(path: str) -> dict[str, bool]:
    """读 addonlist.txt -> {文件名: 是否启用}。容错：文件缺失返回空表。"""
    out: dict[str, bool] = {}
    if not path:
        return out
    # 编码探测交给共享层 safety.read_text_auto。原先这里是
    #     open(path, "r", encoding="utf-8-sig", errors="replace")
    # —— 碰上一份 GBK 编码、文件名带中文的 addonlist.txt 时，GBK 字节会被
    # errors="replace" 变成 U+FFFD（不可逆的 替换字符）。玩家那边看到的是
    # 「扫描认不出我的 mod」，更糟的是 manager.write_addonlist 写回时若沿用
    # 这条路径，就会把 U+FFFD **写回磁盘**，中文文件名被永久改坏。
    # 现在读走 safety.read_text_auto（它先看文件是否真以 EF BB BF 开头来决定
    # 要不要用 utf-8-sig，没有 BOM 就按 utf-8 读，探测到的编码原样交给 manager
    # 写回用），读到的名字和写回的名字保证一致。
    # 实测本机 addonlist.txt：探测结果 utf-8（该文件没有前置 BOM），条目数与
    # 启用数与旧实现逐键相等 —— 即这次改动在真实语料上零差异。
    txt, _enc = safety.read_text_auto(path)
    for m in re.finditer(r'"([^"]+\.vpk)"\s*"([01])"', txt):
        out[m.group(1)] = (m.group(2) == "1")
    return out


def dicts_for_audit(recs: list[ModRecord]) -> list[dict]:
    """给审计引擎用的字典：额外带上内部路径与 VPK 实体路径（不下发给浏览器）。"""
    out = []
    for m in recs:
        d = m.to_json()
        d["_vpaths"] = m._vpaths
        d["_vpk_path"] = m._vpk_path
        out.append(d)
    return out


def _read_workshop_meta(ws_dir: str) -> dict:
    p = os.path.join(ws_dir, ".workshop")
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "rb") as fp:
            return json.loads(fp.read().decode("utf-8-sig", "replace"))
    except Exception:
        return {}


def _norm_paths(vpk: Vpk) -> list[str]:
    return [e.vpath for e in vpk.entries]


def _signature(vpaths: list[str], size: int) -> str:
    h = hashlib.sha1()
    for p in sorted(vpaths):
        h.update(p.encode("utf-8", "replace"))
        h.update(b"\n")
    h.update(str(size).encode())
    return h.hexdigest()[:16]


def scan_one(addon_name: str, addon_path: str, enabled: bool, registered: bool) -> ModRecord:
    rec = ModRecord(key=addon_name, title=os.path.splitext(addon_name)[0],
                    addon_path=addon_path, enabled=enabled, registered=registered)
    # 1) 解析链接
    real = addon_path
    if os.path.islink(addon_path):
        rec.kind = "link"
        try:
            real = os.path.realpath(addon_path)
        except Exception:
            real = ""
        if not real or not os.path.isfile(real):
            rec.link_ok = False
            rec.real_path = real
            rec.evidence.append("链接断链：指向 %s（目标不存在）" % (real or "未知"))
            return rec
    elif os.path.isdir(addon_path):
        rec.kind = "dir"
        vs = [f for f in os.listdir(addon_path) if f.lower().endswith(".vpk")]
        if not vs:
            rec.parse_error = "目录内没有 VPK"
            return rec
        real = os.path.join(addon_path, vs[0])
    rec.real_path = real
    # 断链兜底：只认「链接对象本身还在、跟过去的目标没了」这一种。
    # 为什么上面 islink 分支不够：os.path.islink() 在 Windows 上**只认符号链接**
    # （重解析点里 IO_REPARSE_TAG_SYMLINK 那一种），对目录联接 junction
    # （TAG_MOUNT_POINT）一律返回 False。于是「指向一个已经被删/改名目标的
    # junction」会这样一路漏过去：islink→False（跳过断链分支）、
    # isdir→False（isdir 跟随链接）、最后撞死在 os.path.getsize(real)：
    #     FileNotFoundError: [WinError 2] 系统找不到指定的文件
    # scan() 是**整批循环**调用 scan_one 的（下面的 for i, n in enumerate(vpks)），
    # 这里一抛，207 个 mod 一个都扫不出来 —— 玩家看到的是「扫描失败」，
    # 而不是「某个 mod 断链了」，连病因都不提。
    # 本机实测（_coreperf_jtest）：断链 junction 的 islink=False / isdir=False /
    # exists=False / lexists=True，直接调 scan_one() 必抛 FileNotFoundError。
    # 用 lexists(exists(real) 的取反) 判定而非 islink，正是为了覆盖 junction；
    # 判据写成 addon_path 侧（不是 real 侧）是因为 real 是 realpath 结果，
    # 断链时它本身就是个不存在的路径，lexists(real) 也会是 False。
    if not real or (not os.path.exists(real) and os.path.lexists(addon_path)):
        rec.kind = rec.kind or "link"
        rec.link_ok = False
        rec.evidence.append("链接断链：指向 %s（目标不存在）" % (real or "未知"))
        return rec
    rec.size = os.path.getsize(real)
    rec.mtime = os.path.getmtime(real)

    # 2) FireAxe 元数据
    parent = os.path.dirname(real)
    if os.path.basename(parent).endswith(".workshop"):
        rec.repo_dir = parent
        meta = _read_workshop_meta(parent)
        rec.published_id = str(meta.get("PublishedFileId", "") or "")
        rec.time_updated = int(meta.get("TimeUpdated", 0) or 0)
        gp = os.path.dirname(parent)
        if gp and gp != parent:
            rec.repo_category = os.path.basename(gp)
        rec.title = os.path.basename(parent)[:-len(".workshop")]

    # 3) 解析 VPK
    try:
        v = Vpk(real)
        rec.vpk_version = v.version
        rec.entry_count = len(v.entries)
        rec.data_start = v.data_start
        rec._vpk_path = real
        rec._vpaths = _norm_paths(v)
        cnt: dict[str, int] = {}
        for p in rec._vpaths:
            head = p.split("/")[0]
            cnt[head] = cnt.get(head, 0) + 1
        rec.top_dirs = [k for k, _v in sorted(cnt.items(), key=lambda x: -x[1])][:8]
        rec.signature = _signature(rec._vpaths, rec.size)
        # 趁句柄还在手上，顺手把 addoninfo 读出来存成**纯字典**。
        # 这样 classify() 不必重新 Vpk() 解析一遍目录树，也不会把
        # 几万条目的 VpkEntry 对象一直钉在内存里（只留解析结果）。
        try:
            rec._addoninfo = v.addoninfo()
        except Exception:
            rec._addoninfo = {}
    except VpkError as ex:
        rec.parse_error = str(ex)
    except Exception as ex:
        rec.parse_error = "解析异常: %s" % ex
    return rec


# --------------------------------------------------------------------------- #
def _linked_realpaths(addons: str, vpks: list[str]) -> set[str]:
    """把 addons 下的 VPK 名字换算成「链接目标真实路径」集合。

    单独提成函数的原因：这段判断决定了仓库侧「未链接进 addons」的结论对不对，
    但它原先写成 scan() 里的一个推导式，没法单独测 —— 而它是本文件里唯一
    区分 exists / lexists 的地方，测不到就等于没验证。
    """
    # 用 os.path.lexists 而非 os.path.exists：addons 下的 .vpk 绝大多数是符号
    # 链接（本机 207/207 全是链接）。exists 会**跟随链接**判断，链接指向的目标
    # 一旦不存在（玩家删了仓库里的 mod、或盘符/路径变了），exists 返回 False，
    # 该条目就从集合里消失。lexists 只看链接对象本身在不在，于是断链条目也会
    # 被保留 —— 它是否可用由 scan_one 判定（会标 link_ok=False + 记「链接断链」
    # 证据），而不是在这里被静默丢掉。
    # ⚠️ 实测说明：任务书写「exists=195 vs lexists=206，11 个断链 mod 被静默
    # 排除」——**本机语料复现不出来**：207 个 .vpk 全部可解析，exists 与 lexists
    # 都是 207，差 0。另外要澄清一点：真正决定「哪些 mod 进入扫描」的是上面的
    # os.listdir（断链条目照样进），这里的集合只用于**仓库侧**「未链接」的比对。
    # 所以这条改动的价值是语义正确（别把「链接对象存在」判成「不存在」）
    # 与断链时的防御，而不是修一个当场可复现的 mod 丢失。
    return {os.path.realpath(os.path.join(addons, n)) for n in vpks
            if os.path.lexists(os.path.join(addons, n))}


def scan(cfg: dict, progress=None) -> dict:
    """全量扫描。返回 {mods, summary, evidence, elapsed}"""
    t0 = time.time()
    # 用 paths.addons_dir / paths.addonlist_path 而不是手拼 os.path.join。
    # 手拼版（原来是 os.path.join(cfg.get("game_dir",""), "addons")）在 game_dir
    # 为空/None 时会拼出 **"addons" / "addonlist.txt" 这样的相对路径**，它指向哪
    # 里取决于进程 cwd —— 对一个「按配置去改玩家磁盘文件」的工具来说，这是把
    # 「配置没填」静默变成「去某个不确定的目录里找/写文件」，属于最不该有的失败
    # 模式。共享层的 game_sub() 直接返回空串，语义是「没配置就没有这个路径」。
    # 行为变化：game_dir 为空时 addons 从 "addons" 变成 ""，下面
    # `if not os.path.isdir(addons)` 会安全地走进「addons 目录不存在」分支并把
    # 空路径原样写进错误消息。真实语料下 game_dir 非空，结果完全一致
    # （verify_perf.py 的 H 组断言 + 黄金回归 mods 数不变）。
    addons = paths.addons_dir(cfg)
    listed = read_addonlist(paths.addonlist_path(cfg))
    evidence: list[str] = []
    mods: list[ModRecord] = []

    if not os.path.isdir(addons):
        return {"mods": [], "error": "addons 目录不存在: %s" % addons, "evidence": evidence}

    names = sorted(os.listdir(addons))
    vpks = [n for n in names if n.lower().endswith(".vpk")]
    other = [n for n in names if not n.lower().endswith(".vpk")]
    evidence.append("addons 目录: %s" % addons)
    evidence.append("实体 %d 项 = VPK %d 个 + 非 VPK %d 项 %s"
                    % (len(names), len(vpks), len(other),
                       ("（%s）" % ", ".join(other[:5])) if other else ""))
    evidence.append("addonlist.txt 登记 %d 条（启用 %d / 停用 %d）"
                    % (len(listed), sum(1 for v in listed.values() if v),
                       sum(1 for v in listed.values() if not v)))

    for i, n in enumerate(vpks):
        rec = scan_one(n, os.path.join(addons, n), listed.get(n, False), n in listed)
        classify(rec, cfg)
        mods.append(rec)
        if progress and (i % 10 == 0 or i == len(vpks) - 1):
            progress(i + 1, len(vpks), n)

    # 仓库里存在但未链接进 addons 的 mod（潜在遗漏）
    linked = _linked_realpaths(addons, vpks)
    repo_dir = cfg.get("repo_dir", "")
    unlinked: list[dict] = []
    if repo_dir and os.path.isdir(repo_dir):
        for root, dirs, files in os.walk(repo_dir):
            if os.path.basename(root).endswith(".workshop"):
                dirs[:] = []
                vs = [f for f in files if f.lower().endswith(".vpk")]
                if not vs:
                    unlinked.append({"dir": root, "reason": "目录内无 VPK（可能未下载完）"})
                    continue
                if not any(os.path.realpath(os.path.join(root, x)) in linked for x in vs):
                    unlinked.append({"dir": root, "reason": "未链接进 addons"})
        evidence.append("仓库: %s（未接入 addons 的 %d 个）" % (repo_dir, len(unlinked)))

    summary = {
        "total": len(mods),
        "enabled": sum(1 for m in mods if m.enabled),
        "disabled": sum(1 for m in mods if not m.enabled),
        "unregistered": sum(1 for m in mods if not m.registered),
        "broken": sum(1 for m in mods if not m.link_ok),
        "parse_error": sum(1 for m in mods if m.parse_error),
        "mismatch": sum(1 for m in mods if m.mismatch),
        "by_category": {},
        "by_auto_category": {},
        "total_size": sum(m.size for m in mods),
    }
    for m in mods:
        summary["by_category"][m.category] = summary["by_category"].get(m.category, 0) + 1
        summary["by_auto_category"][m.auto_category] = \
            summary["by_auto_category"].get(m.auto_category, 0) + 1
    summary["total_size_h"] = safety.fmt_size(summary["total_size"])

    result = {
        "mods": mods,
        "mods_json": [m.to_json() for m in mods],
        "summary": summary,
        "evidence": evidence,
        "unlinked": unlinked,
        "elapsed": round(time.time() - t0, 2),
        "scanned_at": time.time(),
    }
    # 这里原先有一句 safety.cache_write("scan_meta.json", {...})，已删除。
    # 理由（选「删」而不是「接上缓存」，逐条给依据）：
    #   1) 写进去的东西**不足以支撑任何一次真正的复用**。落盘内容只有
    #      scanned_at / summary / evidence 三个键，**没有 mods / mods_json**
    #      —— 而 scan() 的返回值里承载全部信息量的正是 mods。想靠它「跳过扫描」
    #      根本做不到，最多只能提前显示一句「上次扫描于 X，共 N 个 mod」。
    #   2) 这个唯一的用途**已经被内存缓存覆盖**。app/server.py:52 有
    #      STATE["last_scan"]，do_scan() 把 scan() 结果整个存在里面，
    #      payload_scan()（server.py:84-94）优先读内存、没有再扫。
    #      进程内复用比磁盘缓存快得多，且不存在失效问题。
    #   3) 要「正确」失效，成本几乎等于重扫一遍。缓存键必须覆盖
    #      (game_dir, repo_dir) + addons 目录 mtime + repo_dir 目录 mtime +
    #      addonlist.txt mtime/大小；而 addons 下 207 个条目全是符号链接，
    #      链接目标的 mtime 变化不会反映到 addons 目录的 mtime 上 —— 也就是说
    #      玩家换掉仓库里某个 mod 的内容后，「目录 mtime 未变」这条判据会
    #      放行一份过期缓存。要真正可靠就得逐个链接 stat，那时省下的时间
    #      已经所剩无几。
    #   4) 这是会**改写玩家游戏文件**的工具，过期数据比慢更危险：一个显示
    #      「207 个 mod」的陈旧摘要会让人以为状态没变，而磁盘上早已不同。
    #   5) 全仓库确认零读取方（本副本 + _baseline + old-snapshot + 原项目
    #      一起 grep "scan_meta"）：只有 scan.py 的写入点，以及 graphflow
    #      自动生成的索引和一份 _BACKUP_MANIFEST.txt 的文本命中，都不是调用。
    #      safety.cache_read 本身没被删 —— safety.list_staging() 还在用它读
    #      staging_sizes.json（那种是纯派生的体积缓存，用 mtime 做键是恰当的）。
    return result


# --------------------------------------------------------------------------- #
# 分类
# --------------------------------------------------------------------------- #
def _content_truthy(val: str) -> bool:
    """判断 addoncontent_* 的值是不是「真」。

    实测取值形态：
        '1'                        -> True
        '1 "Rochelle"'             -> True（并指明替换的是哪个角色）
        '0'                        -> False
        '0 //Provides new props,'  -> False（注释已在 vpk.addoninfo 里剥掉）
        ''                         -> False

    大小写不敏感：VPK 里的 addoninfo.txt 是作者手写的，」FALSE」/「False 」
    /「NO」都真的出现过（不同作者不同习惯）。原先那句
        if head in ("0", "false", "no", "False"):
    只列了 "False" 这一种大写写法，于是 "FALSE"、"False "（先 strip 了所以
    尾空格没事）、"NO"、"No" 会被判成真 —— 即「作者明明写了不提供这个内容，
    工具却当成提供了」，进而给出错误的分类。
    改成 head.lower() 后四种写法全覆盖；数字 "0" 不受影响。
    实测（真实语料 206 个 mod / 902 个 addoncontent_* 字段）：首 token 分布
    {'0': 715, '1': 181, '(empty)': 5, 'Francis': 1}，**会翻转的字段数 = 0**
    —— 即这条是纯 latent 修复，不改变本机任何 mod 的分类结果。
    （'Francis' 这种非数字非布尔串走 else 分支，大小写化后仍判真，行为不变。）
    """
    v = (val or "").strip().strip('"').strip()
    if v == "":
        return False
    head = v.split()[0].strip('"').strip()
    if head.lower() in ("0", "false", "no"):
        return False
    return True


def classify(rec: ModRecord, cfg: dict | None = None) -> None:
    """就地写入 rec 的分类字段。

    两条线并行，谁也不覆盖谁：
      * 主分类 category        —— 仓库已人工归类时以仓库为准（尊重既有整理）
      * 内容分类 auto_category —— 多层**加权打分**后的最高分

    为什么用加权打分而不是「按优先级一拍到底」：
      实测发现大量皮肤 mod 的 VPK 里**只有贴图**（模型在别的 mod 里），
      纯按路径会判成「材质」，纯按官方 skin 标签也会判成「材质」；
      但只要 NekoVpk 素材标签指出它改的是 Tank / Charger / Zoey，
      角色这一信号的权重就该压过贴图。打分制正好能表达这种"谁更具体谁说了算"。
    """
    cfg = cfg or {}
    vpaths = rec._vpaths
    hits: dict[str, list[str]] = {}
    scores: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}

    def add(cat: str, score: float, why: str) -> None:
        scores[cat] = scores.get(cat, 0) + score
        reasons.setdefault(cat, []).append(why)

    # ---- 第 0 层：FireAxe 仓库目录（人工整理过）----
    repo_cat = ""
    if rec.repo_category:
        for kw, cat in tl.REPO_DIR_MAP.items():
            if kw in rec.repo_category:
                repo_cat = cat
                rec.category_source = "仓库目录「%s」" % rec.repo_category
                rec.evidence.append("仓库目录「%s」→ %s（人工整理，作主分类）"
                                    % (rec.repo_category, tl.CATEGORIES[cat]["zh"]))
                break

    lib: tl.TagLib | None = cfg.get("_taglib")
    if lib and lib.loaded and vpaths:
        hits = lib.match(vpaths)
        rec.tags = sorted(hits.keys())

    # ---- 第 1 层：addoninfo.txt 官方内容标签 ----
    auto_why = ""
    # 优先复用 scan_one 顺手解析出的 addoninfo：原先这里无条件 Vpk(vpk_path)
    # 重新解析一遍目录树，等于每个 mod 白解析两次（pak 类 mod 有几万条目）。
    # 注意用 `is not None` 判断 —— {} 表示「解析过，但确实没有 addoninfo.txt」，
    # 与 None（没解析过，需要回退）语义不同。
    ai: dict = rec._addoninfo
    if ai is None:
        if rec._vpk_path and os.path.isfile(rec._vpk_path):
            try:
                ai = Vpk(rec._vpk_path).addoninfo()
            except Exception:
                ai = {}
        else:
            ai = {}
    if ai:
        rec.evidence.append("addoninfo.txt: title=%r author=%r"
                            % (ai.get("addontitle", ""), ai.get("addonauthor", "")))
        declared: dict[str, str] = {}
        for k, val in ai.items():
            if k.startswith("addoncontent_"):
                suffix = k[len("addoncontent_"):].strip().lower()
                if _content_truthy(val):
                    declared[suffix] = str(val)
        rec.content_tags = sorted(declared.keys())
        if declared:
            rec.evidence.append("官方内容标签(真值): " + "、".join(
                "%s=%s" % (k, v) for k, v in declared.items()))
            for suffix, raw_val in declared.items():
                cat = tl.CONTENT_TAG_MAP.get(suffix)
                if not cat:
                    continue
                w = tl.CONTENT_WEIGHT.get(suffix, 20)
                why = "官方标签 addoncontent_%s" % suffix
                m = _QUOTED_RX.search(raw_val)
                if m:
                    why += "（目标: %s）" % m.group(1)
                add(cat, w, why)

    # ---- 第 2 层：素材级标签（NekoVpk 75 标签 / 204 条路径规则）----
    if hits and lib:
        for tag, paths in hits.items():
            spec = lib.tags.get(tag) or {}
            for tp in spec.get("type", []):
                cat = tl.NEKO_TYPE_MAP.get(str(tp).strip().lower())
                if cat:
                    add(cat, tl.NEKO_TYPE_WEIGHT.get(str(tp).strip().lower(), 10),
                        "素材标签「%s」(type=%s) 命中 %d 个文件" % (tag, tp, len(paths)))
        top = sorted(hits.items(), key=lambda x: -len(x[1]))[:3]

    # ---- 第 3 层：内部路径启发式 ----
    if vpaths:
        game_map = any(p.startswith("maps/") and p.endswith(".bsp") for p in vpaths)
        if game_map:
            add("campaign", 130, "含 .bsp 地图文件（硬证据）")
        tally: dict[str, int] = {}
        for rx, cat, human in _PATH_RX:
            n = sum(1 for p in vpaths if rx.match(p))
            if n:
                tally[cat] = tally.get(cat, 0) + n
        for cat, n in tally.items():
            share = n / max(1, len(vpaths))
            add(cat, 25 + min(n, 15) + (20 if share > 0.8 else 0),
                "内部路径 %d/%d 个文件落在该类的目录下" % (n, len(vpaths)))

    # ---- 第 4 层：文件名 / 标题关键词 ----
    hay = (rec.title + " " + rec.key + " " + rec.repo_category).lower()
    for rx, cat, human in _NAME_RX:
        if rx.search(hay):
            add(cat, 20, "标题关键词 %s（/%s/）" % (human, rx.pattern))
            break

    # ---- 裁决 ----
    if scores:
        best_cat = max(scores.items(), key=lambda kv: (kv[1], -tl.CATEGORIES[kv[0]]["order"]))[0]
        rec.auto_category = best_cat
        best_score = scores[best_cat]
        auto_why = "；".join(reasons[best_cat][:3])
        rec.evidence.append("内容判定 → %s（得分 %.0f）：%s"
                            % (tl.CATEGORIES[best_cat]["zh"], best_score, auto_why))
        runner = sorted((c for c in scores if c != best_cat), key=lambda c: -scores[c])[:2]
        if runner:
            rec.evidence.append("  次选: " + "、".join(
                "%s(%.0f)" % (tl.CATEGORIES[c]["zh"], scores[c]) for c in runner))
    else:
        rec.auto_category = "misc"
        best_score = 0.0
        rec.evidence.append("无任何内容信号命中，归入「其他/未分类」")

    # ---- 主分类裁决 ----
    if repo_cat:
        rec.category = repo_cat
        if rec.auto_category != "misc" and rec.auto_category != repo_cat:
            if best_score >= tl.STRONG_SCORE:
                rec.mismatch = True
                rec.evidence.append(
                    "⚠ 强证据冲突：仓库放在「%s」（%s），但内容有强证据指向「%s」（%.0f 分）——建议人工复核"
                    % (rec.repo_category, tl.CATEGORIES[repo_cat]["zh"],
                       tl.CATEGORIES[rec.auto_category]["zh"], best_score))
            else:
                rec.evidence.append(
                    "内容倾向「%s」（仅 %.0f 分，弱信号，不足以推翻仓库归类）"
                    % (tl.CATEGORIES[rec.auto_category]["zh"], best_score))
    else:
        rec.category = rec.auto_category
        rec.category_source = auto_why or "内容判定"

    # ---- 去重 + 兜底 ----
    rec.tags = sorted(set(rec.tags))
    if not rec.evidence:
        rec.evidence.append("无任何规则命中，归入「其他/未分类」")
    if not rec.registered:
        rec.evidence.append("⚠ 不在 addonlist.txt 中（游戏不会加载它）")
    if not rec.link_ok:
        rec.evidence.append("⚠ 链接断链")
