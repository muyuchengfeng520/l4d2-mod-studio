# -*- coding: utf-8 -*-
"""审计引擎：冲突 / 多余 VPK / 前置缺失。

只读，不改任何文件。所有结论都带可复核的依据（路径、数量、文件名）。
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from itertools import combinations

from . import safety
from .vpk import Vpk, VpkError

# 资源路径的重要度分级（用于冲突排序：越靠前越可能"改了但没生效"）
PATH_WEIGHT = [
    (re.compile(r"^models/(survivors|infected|weapons|w_models|v_models)"), 100, "角色/武器模型"),
    (re.compile(r"^maps/"), 95, "地图"),
    (re.compile(r"^scripts/"), 85, "脚本"),
    (re.compile(r"^materials/models/"), 80, "模型贴图"),
    (re.compile(r"^materials/vgui/"), 70, "界面贴图"),
    (re.compile(r"^resource/"), 60, "界面定义"),
    (re.compile(r"^sound/"), 55, "音效"),
    (re.compile(r"^particles/"), 50, "粒子"),
    (re.compile(r"^materials/"), 40, "材质"),
]

# 这些文件几乎所有 mod 都会带一份，互相覆盖**不会**造成功能冲突，必须排除，
# 否则冲突列表会被 73 个 addonimage.jpg 这类噪声淹没（实测踩坑）。
CONFLICT_IGNORE = re.compile(
    r"^(addonimage|addoninfo|addonpicture|addon_banner)"
    r"|\.cache$|^sound/sound\.cache$|^\.|/\.DS_Store$"
    r"|^materials/vgui/.*\.(jpg|jpeg|png)$",
    re.I)

# 只有 .mdl + .ani 的纯动画模型，本来就不带 .vvd/.vtx —— 别当成缺件误报
ANIM_ONLY = re.compile(r"(^|/)(anim_|gestures?_|wip_|p_|z_)|_anims?\.", re.I)

# 官方设计允许「每个 addon 各带一份」的入口脚本：引擎会遍历所有 addon 依次调用，
# 重复不是冲突。不排除的话，冲突列表会被 director_base_addon.nut 这类噪声淹没
# （2026-10-18 实测：本机 16 个 mod 各带一份，scriptedmode_addon 3 份）。
#
# ⚠️ 这条规则**只属于 script_lib_conflicts()**（脚本库整包冲突的判据），
#    不许放进 is_noise() —— 见下面 is_noise 的说明。
MULTI_ADDON_ENTRY = re.compile(
    r"(^|/)(director_base_addon|scriptedmode_addon|mapspawn_addon)\.nut$", re.I)

# 同一脚本库目录下至少这么多 .nut 被同一组 mod 同时提供，才判为「库整包冲突」。
# 设成 2 的依据：单文件覆盖只是谁生效的问题，>=2 才说明是两套库被拼在一起。
LIB_CONFLICT_MIN_FILES = 2


def is_noise(p: str) -> bool:
    """conflicts() 专用的噪声过滤：只排除「人人都有、覆盖也无害」的资源。

    ⚠️ 不许把别的功能的判据塞进来（踩坑，2026-10-18）：
       曾把 MULTI_ADDON_ENTRY（脚本库检测用的「入口脚本排除」）加在这里，
       而 conflicts() 与本函数共用 —— 结果覆盖冲突数从 455 掉到 453，
       对用户就是「之前有的 2 条冲突凭空消失了」，还很难看出原因。
       各功能的过滤规则必须待在自己的函数里：共用函数的输出不允许被顺手改。
       入口脚本的排除已下沉到 script_lib_conflicts() 内部（见该函数）。
    """
    return bool(CONFLICT_IGNORE.search(p))


def _path_weight(p: str) -> tuple[int, str]:
    for rx, w, label in PATH_WEIGHT:
        if rx.match(p):
            return w, label
    return 10, "其他"


# --------------------------------------------------------------------------- #
# 1) 冲突
# --------------------------------------------------------------------------- #
def conflicts(mods: list[dict], only_enabled: bool = True) -> dict:
    """同一内部资源被多个 mod 同时提供 → 只会有一个生效，其余白装。

    mods 为 scan 产出的 mods_json；需要其中的 _vpaths（由 scan 内部附加）。
    """
    index: dict[str, list[str]] = defaultdict(list)
    for m in mods:
        if only_enabled and not m.get("enabled"):
            continue
        if not m.get("_vpaths"):
            continue
        for p in m["_vpaths"]:
            if is_noise(p):
                continue
            index[p].append(m["key"])

    groups = {p: ks for p, ks in index.items() if len(ks) > 1}
    by_weight = defaultdict(list)
    for p, ks in groups.items():
        w, label = _path_weight(p)
        by_weight[label].append((w, p, ks))

    items = []
    for label, rows in by_weight.items():
        rows.sort(key=lambda x: -x[0])
        for w, p, ks in rows:
            items.append({"path": p, "weight": w, "kind": label, "mods": ks, "mod_count": len(ks)})
    items.sort(key=lambda x: (-x["mod_count"], -x["weight"], x["path"]))

    # mod 两两之间的重叠度（想找出"到底谁和谁打架"）
    # 语义与原先的双重 for 循环完全一致（含 ks 里出现重复 key 时产生的自配对），
    # 只是把内层循环换成 itertools.combinations —— 同样是按索引取组合，
    # 但省掉 Python 层的下标运算与边界判断。
    # 上限保护：极端情况下同一个文件被几十个 mod 覆盖，(a,b) 组合数会爆炸，
    # 到顶就停下并如实标记，不静默吞掉。
    PAIR_CAP = 200000
    ITEM_CAP, DIR_CAP, PAIR_OUT_CAP = 300, 120, 80
    pair: dict[tuple[str, str], set] = defaultdict(set)
    truncated = False
    for p, ks in groups.items():
        # ⚠️ 上限要在**每处理一组之前**判，而不是处理完一整组之后：原写法
        #    只在外层循环开头查一次，一个组里若有 1000 个 mod 提供同一文件，
        #    光这一组就是 C(1000,2)=499500 个组合 —— 判到的时候钱已经花了。
        #    改成「本组组合数 + 已有数」超限就直接停，并且如实标记 truncated。
        n_new = len(ks) * (len(ks) - 1) // 2
        if n_new and len(pair) + n_new > PAIR_CAP:
            truncated = True
            break
        for a, b in combinations(ks, 2):
            if a > b:
                a, b = b, a
            pair[(a, b)].add(p)
    pairs = [{"a": a, "b": b, "shared": len(ps), "sample": sorted(ps)[:5]}
             for (a, b), ps in pair.items()]
    pairs.sort(key=lambda x: -x["shared"])
    # 截断必须对调用方可见（踩坑记录）：原实现直接 items[:300]，界面照自己的
    # 数组长度显示「共 N 组冲突」，用户以为看到了全部 —— 而这里少了 100+ 条。
    # 对「据此决定删哪个 mod」的工具，静默截断比报错更危险。
    # 返回里新增 *_total 与 truncated 字段，原键名与内容一律不变（前端只读不写）。

    # 按目录聚合：465 条逐文件冲突读不动，压成「某目录下 N 个文件被 X/Y/Z 同时提供」
    dagg: dict[str, dict] = {}
    for it in items:
        d = it["path"].rsplit("/", 1)[0] if "/" in it["path"] else "(根目录)"
        e = dagg.setdefault(d, {"dir": d, "kind": it["kind"], "weight": it["weight"],
                                "files": 0, "mods": set(), "sample": []})
        e["files"] += 1
        e["mods"].update(it["mods"])
        if len(e["sample"]) < 5:
            e["sample"].append(it["path"])
    dir_groups = []
    for d, e in dagg.items():
        dir_groups.append({"dir": d, "kind": e["kind"], "files": e["files"],
                           "mod_count": len(e["mods"]),
                           "mods": sorted(e["mods"])[:8],
                           "sample": e["sample"]})
    dir_groups.sort(key=lambda x: (-x["mod_count"], -x["files"]))

    return {
        "path_count": len(groups),
        "mods_involved": len({k for ks in groups.values() for k in ks}),
        "items": items[:ITEM_CAP],
        "dir_groups": dir_groups[:DIR_CAP],
        "pairs": pairs[:PAIR_OUT_CAP],
        "pairs_truncated": truncated,
        # ↓ 新增：总量与实际返回量，供界面如实显示「还有 N 条没列出」。
        #   原有的 items / dir_groups / pairs 三个键名与内容一字未改。
        "items_total": len(items),
        "dir_groups_total": len(dir_groups),
        "pairs_total": len(pairs),
        "items_truncated": len(items) > ITEM_CAP,
        "dir_groups_truncated": len(dir_groups) > DIR_CAP,
        "pairs_output_truncated": len(pairs) > PAIR_OUT_CAP,
        "note": ("同一条内部资源被多个 mod 提供时，游戏只会用其中一个，"
                 "具体哪个生效取决于加载顺序 —— 没生效的那个就是白装。"
                 "已自动排除 addonimage/sound.cache 这类「人人都有、覆盖也无害」的文件。"),
    }


# --------------------------------------------------------------------------- #
# 1.5) 脚本库整包冲突（致命级，会崩游戏）
# --------------------------------------------------------------------------- #
def script_lib_conflicts(mods: list[dict], only_enabled: bool = True) -> dict:
    """两套同名脚本库同时挂载 → 版本打架 → 进图即崩。

    与 conflicts() 的区别（2026-10-18 实测留档，本机真事故）：
      单文件覆盖只是「哪个生效」的问题，最坏是白装；
      但同一**脚本库目录**下多个 .nut 被同一组 mod 同时提供，意味着两个不同版本的
      库被拼在一起跑 —— 引擎只加载其中一份，其余脚本按另一份的语义调用，
      极易形成无限递归，把 VScript 的调用栈撑爆。
      崩溃点常见为 `vscript.dll+…`，内存先耗尽时表现为 `tier0.dll+…`（同一根因）。

    事故原型：Thirdperson 与 VSLib 本体同时提供 scripts/vscripts/vslib 下 10 个 .nut
    （含主入口 vslib.nut），`::Update` 与 `ModeUpdate` 互相调用 → 70 万层栈 → 进图必崩。
    """
    owners: dict[str, set] = defaultdict(set)
    for m in mods:
        if only_enabled and not m.get("enabled"):
            continue
        for p in m.get("_vpaths") or ():
            if p.endswith(".nut") and not MULTI_ADDON_ENTRY.search(p):
                owners[p].add(m["key"])

    shared = {p: ks for p, ks in owners.items() if len(ks) > 1}

    # 按「目录 + 共同提供的那组 mod」聚合；不同 mod 组合算不同的冲突，
    # 免得 A/B 冲突和 A/C 冲突被混成一条说不清。
    agg: dict[tuple, dict] = {}
    for p, ks in shared.items():
        d = p.rsplit("/", 1)[0] if "/" in p else "(根目录)"
        e = agg.setdefault((d, frozenset(ks)), {"dir": d, "mods": set(ks), "files": []})
        e["files"].append(p)

    groups = []
    for e in agg.values():          # 这里只用 e；agg 的键 (目录, mod 组合) 只做去重
        if len(e["files"]) < LIB_CONFLICT_MIN_FILES:
            continue
        groups.append({
            "dir": e["dir"],        # 目录名存在 e 里，别再依赖外层循环变量
            "files": sorted(e["files"]),
            "file_count": len(e["files"]),
            "mods": sorted(e["mods"]),
            "severity": "致命",
            "why": ("同一脚本库目录下 %d 个 .nut 被 %d 个 mod 同时提供 —— 两个版本的库"
                    "被拼在一起用，进图加载脚本即崩（vscript.dll / tier0.dll）"
                    % (len(e["files"]), len(e["mods"]))),
            "fix": "这几项二选一：只保留提供该库的那一个，其余禁用"
                   "（改 addons 里符号链接后缀为 .vpk.disabled，可逆）",
        })
    groups.sort(key=lambda x: -x["file_count"])

    return {
        "groups": groups,
        "total": len(groups),
        "note": ("只统计同一目录下被同一组 mod 同时提供的 .nut。"
                 "director_base_addon / scriptedmode_addon / mapspawn_addon "
                 "是官方允许每个 addon 各带一份的入口脚本，已排除。"),
    }


# --------------------------------------------------------------------------- #
# 2) 多余 / 异常 VPK
# --------------------------------------------------------------------------- #
def redundancy(mods: list[dict], addons_dir: str, addonlist_map: dict[str, bool]) -> dict:
    """找出多余、失效、重复、可疑的 VPK 条目。"""
    out: dict[str, list] = {
        "broken_link": [],      # 断链：目标是空的
        "unregistered": [],     # 在 addons 里但 addonlist 未登记 → 游戏不会加载
        "dead_entry": [],       # addonlist 登记了但文件不存在
        "not_vpk": [],          # 扩展名是 .vpk 但内容不是 VPK（常见：ZIP/RAR 改后缀）
        "empty": [],            # 0 字节或目录为空
        "zero_entries": [],     # VPK 结构正常但没有任何文件
        "duplicate_content": [],# 内部文件清单与总大小完全一致 → 大概率重复订阅
        "duplicate_target": [], # 多个链接指向同一个实体文件
        "multi_vpk_dir": [],    # 一个 .workshop 目录里有多个 VPK（旧版本残留）
        "outdated": [],         # 文件名里带 OUTDATED / 旧版标记
    }

    # 磁盘上真实存在的东西 vs addonlist 登记的东西
    on_disk = [f for f in os.listdir(addons_dir)] if os.path.isdir(addons_dir) else []
    on_disk_vpk = sorted(n for n in on_disk if n.lower().endswith(".vpk"))
    listed_keys = set(addonlist_map.keys())

    for n in on_disk_vpk:
        p = os.path.join(addons_dir, n)
        if not os.path.exists(p):
            out["broken_link"].append({"key": n, "target": _readlink(p)})
        if n not in listed_keys:
            out["unregistered"].append({"key": n})

    real_keys = {m["key"] for m in mods}
    for k in sorted(listed_keys - real_keys):
        out["dead_entry"].append({"key": k, "note": "addonlist 里登记了，但 addons 目录里找不到它"})

    for m in mods:
        if m.get("parse_error"):
            e = m["parse_error"]
            if "ZIP" in e or "RAR" in e or "不是 VPK" in e:
                out["not_vpk"].append({"key": m["key"], "title": m["title"], "error": e})
            elif not m.get("link_ok"):
                pass                      # 断链已单列，不再重复计入
            else:
                out["empty"].append({"key": m["key"], "title": m["title"], "error": e})
        elif m.get("entry_count") == 0 and m.get("link_ok"):
            out["zero_entries"].append({"key": m["key"], "title": m["title"],
                                        "note": "VPK 结构正常但内部一个文件都没有"})
        if "OUTDATED" in m["key"].upper() or "outdated" in m["title"].lower():
            out["outdated"].append({"key": m["key"], "title": m["title"],
                                    "size_h": m["size_h"]})

    # 内容重复：签名相同 = 内部文件清单 + 总大小一致
    sig = defaultdict(list)
    real = defaultdict(list)
    for m in mods:
        if m.get("signature") and m.get("entry_count"):
            sig[m["signature"]].append(m)
        if m.get("real_path"):
            real[os.path.normcase(m["real_path"])].append(m)
    for s, ms in sig.items():
        if len(ms) > 1:
            out["duplicate_content"].append({
                "signature": s, "mods": [x["key"] for x in ms],
                "titles": [x["title"][:46] for x in ms],
                "size_h": ms[0]["size_h"],
                "note": "内部文件清单与总大小完全一致"})
    for t, ms in real.items():
        if len(ms) > 1:
            out["duplicate_target"].append({
                "target": t, "mods": [x["key"] for x in ms],
                "titles": [x["title"][:46] for x in ms],
                "note": "多个 addons 条目指向同一个实体 VPK"})

    # 一个 .workshop 目录里塞了多个 VPK
    dirs = defaultdict(list)
    for m in mods:
        if m.get("repo_dir"):
            dirs[os.path.normcase(m["repo_dir"])].append(m["key"])
    for d, ks in dirs.items():
        if len(ks) > 1:
            out["multi_vpk_dir"].append({"dir": d, "mods": ks,
                                         "note": "同一 workshop 目录内多个 VPK，游戏通常只加载一个"})

    # 统计口径：`total_findings` 是「需要用户处理的发现总数」。
    # `duplicate_content`（内部文件清单与大小完全相同的重复 mod）刻意不计入 ——
    # 它是「信息性提示」而非「故障」：玩家装了两个完全一样的 mod 只是浪费空间，
    # 不影响游戏运行。其余 9 类都是真问题，必须计入。
    # 注意 `counts` 是**逐类原始计数**、包含 duplicate_content，两个数字本来就不等，
    # 各自有各自的用途（counts 给界面分栏显示，total_findings 给总览数字）。
    total = sum(len(v) for k, v in out.items() if k != "duplicate_content")
    return {"groups": out,
            "counts": {k: len(v) for k, v in out.items()},
            "total_findings": total,
            "total_findings_note": "不含 duplicate_content（重复 mod 属信息性提示）"}


def _readlink(p: str) -> str:
    try:
        return os.path.realpath(p)
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# 3) 前置缺失
# --------------------------------------------------------------------------- #
NUT_INCLUDE = re.compile(r'(?:IncludeScript|include|require)\s*\(\s*["\']([^"\']+\.nut)["\']',
                         re.I)


def prerequisites(mods: list[dict], game_dir: str, max_scan_bytes: int = 8 * 1024 * 1024) -> dict:
    """检查前置/依赖是否缺失。

    覆盖 4 类可判定的缺件：
      A 模型缺件     —— 有 .mdl 却缺配套的 .vvd / .vtx（纯动画模型不算，见 ANIM_ONLY）
      B 脚本依赖缺失 —— .nut 里 IncludeScript 引用的脚本，任何地方都找不到
      C 皮肤缺底模   —— 只含贴图但明显是改角色/武器 → 大概率需要别人提供模型
      D 运行环境依赖 —— 含 VScript 需要游戏/服务器支持
    """
    findings: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def emit(kind: str, m: dict, detail: str, confidence: str = "高"):
        # 游戏本体没读成时，A（模型缺件）与 B（脚本依赖缺失）这两类判据的
        # 对照基准是残缺的 —— 结论必然偏严，一律降级为「低」，别让用户按
        # 高置信度去删 mod。
        if kind in ("模型缺件", "脚本依赖缺失") and not game_ok:
            confidence = "低"
            detail += "（⚠ 本次没能读到游戏自带资源，可能只是游戏提供了这一件）"
        sig = (m["key"], kind + detail.split("——")[0][:60])
        if sig in seen:
            return
        seen.add(sig)
        findings.append({"kind": kind, "key": m["key"], "title": m["title"],
                         "detail": detail, "confidence": confidence})

    # 收集全局可用的内部资源（游戏自带 + 所有已启用 mod）
    game_paths: set[str] = set()
    pak = os.path.join(game_dir, "pak01_dir.vpk")
    if os.path.isfile(pak):
        try:
            game_paths = set(Vpk(pak).list_files())
        except VpkError as ex:
            # 原先这里是 `except VpkError: pass` —— 游戏本体解析失败时 game_paths
            # 静默变成空集，于是**所有**「模型缺件 / 脚本依赖缺失」的判据都拿不到
            # 游戏自带资源做对照，凭空多出一堆假警告（或者反过来漏报）。
            # 这行日志是用户唯一能看出「这次的审计结果不可信」的线索。
            safety.log("audit-gamefiles", "游戏本体 pak01_dir.vpk 解析失败，"
                       "本次审计**没有**把游戏自带资源纳入对照，结论可能偏严：%s: %s"
                       % (type(ex).__name__, ex), level="ERROR", ok=False)
        except OSError as ex:
            safety.log("audit-gamefiles", "读取游戏本体 pak01_dir.vpk 失败：%s: %s"
                       % (type(ex).__name__, ex), level="ERROR", ok=False)
    mod_paths: dict[str, set[str]] = {}
    all_mod_paths: set[str] = set()
    for m in mods:
        ps = set(m.get("_vpaths") or [])
        mod_paths[m["key"]] = ps
        if m.get("enabled"):
            all_mod_paths |= ps
    available = game_paths | all_mod_paths
    # ⚠️ 游戏本体解析失败（game_paths 为空集）时，「缺件」判据就失去了对照基准：
    #    所有 .nut 依赖、所有模型配套件都会被判成缺失。此时必须给结论打折。
    game_ok = os.path.isfile(pak) and bool(game_paths)
    # 关键：按「无扩展名的 stem」建索引，才能判断 .mdl 有没有配套的 .vvd/.vtx
    avail_stems = {re.sub(r"\.(dx90|dx80|dx70|sw)$", "", p.rsplit(".", 1)[0].lower())
                   for p in available}
    # 这两份集合原先写在 for m in mods 循环体里，每个 mod 都要把
    # 几万条 available 重新筛一遍 —— 纯粹是白跑，提到循环外只算一次。
    available_nuts = {p for p in available if p.endswith(".nut")}

    for m in mods:
        if not m.get("enabled") or not m.get("_vpk_path"):
            continue
        ps = mod_paths[m["key"]]
        if not ps:
            continue

        # ---- A 模型缺件 ----
        # 原则性判据：整个 mod 里连一个 .vvd/.vtx 都没有 → 属于纯动画包 / 纯贴图包，
        # 模型几何数据本来就该由底模提供，逐模型报缺件只会刷出 80+ 条误报（实测踩坑）。
        has_mesh = any(p.endswith((".vvd", ".vtx", ".dx90.vtx", ".dx80.vtx")) for p in ps)
        if has_mesh:
            # ⚠️ 必须 sorted(ps)：ps 是 set，迭代顺序随哈希随机化每进程都不同，
            #    直接迭代会导致「同一个 mod 报哪个模型缺件」每次刷新都在变
            #    （实测：同代码连跑两次分别报 bill_slot_023 / coach_slot_023），
            #    与本模块「所有结论都要可复核」的承诺冲突。
            for p in sorted(ps):
                if not p.startswith("models/") or not p.endswith(".mdl"):
                    continue
                stem = p[:-4]
                if ANIM_ONLY.search(stem):
                    continue      # 纯动画骨架模型，本来就只有 .mdl + .ani
                # 结构性判据：同目录存在**同名 .ani** 的 .mdl 一定是纯动画模型。
                # 动画 mod 的实际布局是「xxx.mdl + xxx.ani」，.mdl 只是个挂载壳，
                # 几何数据本来就不该有 —— 拿 .vvd/.vtx 去要求它必然是误报。
                # 为什么不能只靠上面的正则：真实语料（206 mod / 当前 62 条前置发现）
                # 实测 16 条「模型缺件」里 6 条是这种壳（bill/coach/ellis/nick/
                # rochelle/zoey 的 slot_023，路径形如 models/xdreanims/xxx.mdl，
                # 既不含 anim_ 也不含 _anims，正则抓不到）。
                # 游戏本体同样印证：3333 个 .mdl 里有 31 个没有 .vvd，其中 30 个
                # 都带 .ani —— 也就是说「缺 .vvd」本身不代表坏件。
                # .ani 查全局（本 mod + 游戏 + 其他已启用 mod）：动画壳与 .ani
                # 被拆进两个 mod 的情况也存在，只看本 mod 会漏。
                if (stem.lower() + ".ani") in available:
                    continue
                bare = stem.lower()
                has_vvd = (bare + ".vvd") in avail_stems or (bare + ".vvd") in available
                has_vtx = any((bare + e) in available for e in
                              (".vtx", ".dx90.vtx", ".dx80.vtx")) or (bare + ".vtx") in avail_stems
                if not has_vvd:
                    emit("模型缺件", m, "有 %s.mdl 但全盘都找不到配套 .vvd（模型点数据）——"
                                        "模型会显示错误或不可见" % os.path.basename(stem))
                elif not has_vtx:
                    emit("模型缺件", m, "有 %s.mdl 但全盘都找不到配套 .vtx（几何数据）"
                          % os.path.basename(stem))
        # ---- B 脚本依赖 ----
        nuts = [p for p in ps if p.endswith(".nut")]
        if nuts:
            try:
                v = Vpk(m["_vpk_path"])
                total = sum(e.entry_length for e in v.entries)
                if total <= max_scan_bytes:
                    for np_ in nuts:
                        raw = v.read_file(np_)
                        if not raw:
                            continue
                        text = raw.decode("utf-8", "replace")
                        for ref in NUT_INCLUDE.findall(text):
                            r = ref.strip().lstrip("./").lower()
                            cands = {r, "scripts/vscripts/" + r,
                                     os.path.dirname(np_) + "/" + r}
                            if not any(c.lower() in available_nuts for c in cands):
                                emit("脚本依赖缺失", m,
                                     "%s 引用了 %s，但在本 mod、其他已启用 mod 与游戏本体里"
                                     "都找不到" % (np_.split("/")[-1], ref))
            except Exception as ex:
                # 早先这里是裸 `except Exception: pass` —— 脚本依赖检查整体失败时
                # 用户看到的是「没有任何依赖问题」，而真相是「这一项根本没检查成」。
                # 对会据此决定要不要删 mod 的工具来说，「静默的不确定」比「明确的报错」危险得多。
                safety.log("audit-nut", "检查 %s 的脚本依赖时失败：%s: %s"
                           % (m.get("key", "?"), type(ex).__name__, ex),
                           level="WARN", ok=False)
            emit("运行环境依赖", m,
                 "含 %d 个 VScript(.nut) 文件，需要游戏/服务器启用 VScript 支持"
                 "（本地通常已支持，进服前请确认）" % len(nuts), confidence="低")

        # ---- C 皮肤缺底模 ----
        has_model = any(p.startswith("models/") for p in ps)
        tag_hits = m.get("tags") or []
        characterish = [t for t in tag_hits if t in (
            "Zoey", "Bill", "Francis", "Louis", "Coach", "Ellis", "Rochelle", "Nick",
            "Hunter", "Smoker", "Boomer", "Tank", "Witch", "Jockey", "Charger", "Spitter",
            "步枪类", "狙击枪类", "霰弹枪类", "冲锋枪类", "手枪类", "近战", "载具")]
        if not has_mesh and (characterish or has_model):
            hint = ("内容不含模型几何数据（无 .vvd/.vtx），只提供动画或贴图"
                    + ("，标签指向「%s」" % "、".join(characterish[:4]) if characterish else ""))
            emit("疑似需底模", m,
                 hint + " —— 这类 mod 需要对应的底模提供模型本体，否则进游戏看不到效果",
                 confidence="中")
        elif not has_model and characterish:
            emit("疑似需底模", m,
                 "内容只含贴图/材质，但标签指向「%s」——模型本体不在本 mod 内，"
                 "通常需要一个底模 mod 才能正常显示" % "、".join(characterish[:4]),
                 confidence="中")

    FINDING_CAP = 400
    return {
        "findings": findings[:FINDING_CAP],
        "counts": {k: sum(1 for f in findings if f["kind"] == k)
                   for k in sorted({f["kind"] for f in findings})},
        "total": len(findings),
        # counts 与 total 都按**完整** findings 统计（未截断），这里再多给一个
        # 显式标志，省得调用方自己去比 len(findings) 与 total。
        "findings_truncated": len(findings) > FINDING_CAP,
        "checked_against": "游戏本体 %d 个资源 + 已启用 mod %d 个资源"
                           % (len(game_paths), len(all_mod_paths)),
        # 给界面用：false 表示这次「缺件」类结论不可全信（游戏本体没读成）。
        "game_files_ok": game_ok,
    }


def unlinked_repo_items(repo_dir: str, mods: list[dict]) -> dict:
    """仓库里有、但没接进 addons 的 mod（漏装 / 下载未完成）。"""
    if not repo_dir or not os.path.isdir(repo_dir):
        return {"items": [], "total": 0}
    linked = {os.path.normcase(m["real_path"]) for m in mods if m.get("real_path")}
    items = []
    for root, dirs, files in os.walk(repo_dir):
        if os.path.basename(root).endswith(".workshop"):
            dirs[:] = []
            vs = [f for f in files if f.lower().endswith(".vpk")]
            if not vs:
                items.append({"dir": root, "title": os.path.basename(root)[:-9],
                              "reason": "目录内没有 VPK（可能下载未完成）",
                              "category": os.path.basename(os.path.dirname(root))})
            elif not any(os.path.normcase(os.path.join(root, x)) in linked for x in vs):
                items.append({"dir": root, "title": os.path.basename(root)[:-9],
                              "reason": "未链接进 addons（游戏加载不到）",
                              "category": os.path.basename(os.path.dirname(root)),
                              "vpk": vs[0]})
    return {"items": items, "total": len(items)}


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def run_all(mods: list[dict], game_dir: str, addons_dir: str,
            addonlist_map: dict[str, bool], repo_dir: str = "") -> dict:
    c = conflicts(mods, only_enabled=True)
    sl = script_lib_conflicts(mods, only_enabled=True)
    r = redundancy(mods, addons_dir, addonlist_map)
    p = prerequisites(mods, game_dir)
    u = unlinked_repo_items(repo_dir, mods)
    if sl["total"]:
        # 这类会直接崩游戏，日志里必须单独喊一声，不能混在普通冲突数里
        safety.log("audit", "⚠ 脚本库整包冲突 %d 处，进图可能直接崩：%s"
                   % (sl["total"], "、".join(g["dir"] for g in sl["groups"][:3])),
                   level="WARN", ok=False)
    safety.log("audit", "冲突 %d 组 / 脚本库冲突 %d 处 / 冗余 %d 项 / 前置 %d 条 / 未接入 %d"
               % (c["path_count"], sl["total"], r["total_findings"], p["total"], u["total"]))
    # 新增 script_lib 键；原有 4 个键名与内容一字未改，前端不读就不受影响。
    return {"conflicts": c, "script_lib": sl,
            "redundancy": r, "prerequisites": p, "unlinked": u}
