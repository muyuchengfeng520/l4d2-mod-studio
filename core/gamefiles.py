# -*- coding: utf-8 -*-
"""游戏本体文件替换：启动动画 / 大厅背景动画 / 加载画面 / 启动视频清单。

原理（实测确认）：
  * left4dead2\\media\\*.bik            是**散装文件**（不在 VPK 里），直接覆盖即可
  * left4dead2\\materials\\vgui\\loadingscreen_*.vtf  原本打包在 pak01_dir.vpk 内
    —— 放一个同路径的散装文件到 left4dead2\\ 下，散装优先，即可覆盖（Source 引擎通用行为）
  * left4dead2\\media\\startupvids.txt 控制启动时播哪几段视频，原本也在 pak01 内

安全设计（不可绕过）：
  * 首次替换前，把「原始状态」抽出来存成 BASELINE 备份；
  * 每次替换前，把当前散装文件再备份一份（可回退多步）；
  * 还原：若最新备份是 BASELINE，说明原本没有散装文件 → 直接删掉散装文件即可复原。

⚠ 风险提示：改动游戏本体文件属于「改客户端」。本地单机 / 自建服没问题，
  但连接启用了一致性校验（sv_consistency）的服务器可能被拒或被判定为不一致。
"""
from __future__ import annotations

import json
import os
import shutil

from . import safety
from .vpk import Vpk, VpkError

MANIFEST = os.path.join(safety.BACKUP_DIR, "gamefiles", "_manifest.json")
BASELINE_TAG = "BASELINE"

VIDEO_LABELS = {
    "media/l4d2_intro.bik": "开场动画（点击开始后播放的片头）",
    "media/valve.bik": "Valve 标志动画",
    "media/l4d2_upsell.bik": "推广动画（启动时的宣传片）",
    "media/valve_xbox_legals.bik": "主机版权声明动画",
    "media/l4d2_background01.bik": "大厅背景动画 1",
    "media/l4d2_background02.bik": "大厅背景动画 2",
    "media/l4d2_background03.bik": "大厅背景动画 3",
    "media/l4d2_background04.bik": "大厅背景动画 4",
    "media/l4d2_background05.bik": "大厅背景动画 5",
}


# --------------------------------------------------------------------------- #
def _load_manifest() -> dict:
    """读清单。**损坏时不能静默返回空字典。**

    原实现 except 后直接 `return {}`，后果很隐蔽：清单一旦损坏，
    replace() 会把「已经被替换过的文件」当成原始状态重新抽一份 BASELINE，
    从此用户「还原到最初状态」还原的是 mod 文件，真基线永远丢失。
    所以这里把坏文件另存一份 .bad 并记日志，让人至少能发现出了事。
    """
    if not os.path.isfile(MANIFEST):
        return {}
    try:
        with open(MANIFEST, "r", encoding="utf-8") as fp:
            d = json.load(fp)
        return d if isinstance(d, dict) else {}
    except Exception as ex:
        bad = MANIFEST + ".bad"
        try:
            shutil.copy2(MANIFEST, bad)
        except Exception:
            bad = "(另存失败)"
        safety.log("manifest-corrupt",
                   "清单解析失败，已另存为 %s：%s: %s" % (bad, type(ex).__name__, ex),
                   level="ERROR", ok=False)
        return {}


def _save_manifest(d: dict) -> None:
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    # 必须原子写：manifest 记录的是「游戏原文件的基线备份位置」，
    # 写坏了用户就再也还原不回游戏原文件了（最严重的一处）。
    safety.atomic_write_text(MANIFEST, json.dumps(d, ensure_ascii=False, indent=2))


def _safe(rel: str) -> str:
    return rel.replace("/", "__").replace("\\", "__")


def _backup_dir(rel: str) -> str:
    return os.path.join(safety.BACKUP_DIR, "gamefiles", _safe(rel))


def _backup_stamp(name: str) -> str:
    """从备份文件名里取出 `YYYYMMDD-HHMMSS` 时间戳，取不到返回空串。

    备份文件名形如 `20261016-191302__BASELINE__l4d2_intro.bik`。
    """
    if len(name) >= 15 and name[8] == "-" and name[:8].isdigit() and name[9:15].isdigit():
        return name[:15]
    return ""


def _backups(rel: str) -> list[dict]:
    """列出某个 rel 的历史备份，**按文件名里的时间戳倒序**（最新在前）。

    ⚠️ 这里不能按 mtime 排。备份是用 shutil.copy2 做的，而 copy2 会
    **保留源文件的 mtime** —— 于是「刚备份的这份」带着被备份文件（可能很旧）的
    时间戳，按 mtime 排出来的顺序跟实际备份顺序对不上。
    后果很实在：restore(which="latest") 取 fresh[0] 会回退到**错误的版本**。
    文件名前缀的时间戳才是真正的备份时刻，用它排。
    """
    d = _backup_dir(rel)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in os.listdir(d):
        full = os.path.join(d, fn)
        out.append({"file": full, "name": fn, "size": os.path.getsize(full),
                    "mtime": os.path.getmtime(full),
                    "stamp": _backup_stamp(fn),
                    "is_baseline": fn.startswith(BASELINE_TAG + "__") or ("__" + BASELINE_TAG + "__") in fn})
    # 时间戳倒序；同一秒内的多份按文件名兜底，保证顺序稳定可复现
    out.sort(key=lambda x: (x["stamp"], x["name"]), reverse=True)
    return out


# --------------------------------------------------------------------------- #
# pak01_dir.vpk 目录树缓存
#
# inventory() 和 replace() 都会解析一次 pak01_dir.vpk（约 5 万条目），
# 一次界面操作就是两遍全量解析。这里做进程内单槽缓存：
# 键 = (pak 绝对路径, mtime, size)，游戏更新后 mtime/size 必变，自动失效。
# 注意：返回的是缓存里**同一个 set 对象**，调用方只许读、不许改。
_PAK_TREE_CACHE: dict = {"key": None, "tree": None}


def _pak_tree(game_dir: str) -> set[str]:
    pak = os.path.join(game_dir, "pak01_dir.vpk")
    if not os.path.isfile(pak):
        return set()
    try:
        st = os.stat(pak)
        key = (os.path.abspath(pak), st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None and _PAK_TREE_CACHE.get("key") == key:
        return _PAK_TREE_CACHE["tree"]
    try:
        tree = set(Vpk(pak).list_files())
    except VpkError:
        tree = set()
    if key is not None:
        _PAK_TREE_CACHE["key"] = key
        _PAK_TREE_CACHE["tree"] = tree
    return tree


def _extract_from_pak(game_dir: str, rel: str, dst: str) -> bool:
    pak = os.path.join(game_dir, "pak01_dir.vpk")
    if not os.path.isfile(pak):
        return False
    try:
        v = Vpk(pak)
        data = v.read_file(rel)
        if data is None:
            return False
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as fp:
            fp.write(data)
        return True
    except Exception:
        return False


def inventory(cfg: dict, with_loading: bool = True) -> dict:
    """列出所有可替换的游戏文件及其当前状态。"""
    game_dir = cfg.get("game_dir", "")
    pak_paths = _pak_tree(game_dir)
    man = _load_manifest()
    items: list[dict] = []

    def add_item(rel: str, group: str, label: str):
        disk = os.path.join(game_dir, rel.replace("/", os.sep))
        exists_loose = os.path.isfile(disk)
        in_pak = rel in pak_paths
        bks = _backups(rel)
        entry = man.get(rel, {})
        items.append({
            "rel": rel,
            "group": group,
            "label": label,
            "disk_path": disk,
            "status": ("已替换（散装文件生效）" if exists_loose else
                       ("原始（打包在 pak01 内）" if in_pak else "不存在")),
            "replaced": exists_loose,
            "in_pak": in_pak,
            "size": os.path.getsize(disk) if exists_loose else 0,
            "size_h": safety.fmt_size(os.path.getsize(disk)) if exists_loose else "-",
            "mtime_h": safety.human_time(os.path.getmtime(disk)) if exists_loose else "",
            "backup_count": len(bks),
            "last_backup": bks[0]["name"] if bks else "",
            "last_backup_h": safety.human_time(bks[0]["mtime"]) if bks else "",
            "replaced_at": entry.get("replaced_at", ""),
            "origin_size_h": entry.get("origin_size_h", ""),
        })

    for rel, label in VIDEO_LABELS.items():
        grp = "lobby" if "background" in rel else "startup"
        add_item(rel, grp, label)
    add_item("media/startupvids.txt", "startup",
             "启动视频播放清单（决定启动时播哪几段 / 是否跳过片头）")

    if with_loading:
        for p in sorted(x for x in pak_paths
                        if x.startswith("materials/vgui/loadingscreen")):
            nice = p.split("/")[-1].replace(".vtf", "")
            add_item(p, "loading", "加载画面：%s" % nice)

    groups = {
        "startup": {"zh": "启动动画", "items": [i for i in items if i["group"] == "startup"]},
        "lobby": {"zh": "大厅 / 主菜单背景动画", "items": [i for i in items if i["group"] == "lobby"]},
        "loading": {"zh": "加载画面（战役载入时的图）", "items": [i for i in items if i["group"] == "loading"]},
    }
    return {
        "groups": groups,
        "total": len(items),
        "replaced_count": sum(1 for i in items if i["replaced"]),
        "backup_dir": os.path.join(safety.BACKUP_DIR, "gamefiles"),
        "warning": ("改动游戏本体文件属于改客户端：本地单机与自建服无碍，"
                    "但连接开启一致性校验(sv_consistency)的服务器可能被踢或被提示文件不一致。"
                    "每次替换都会自动备份，随时可一键还原。"),
    }


# --------------------------------------------------------------------------- #
def _allowed_rels(game_dir: str) -> set:
    """允许替换的路径白名单。

    原先是 replace() 里的三行内联代码；抽出来是为了让 write_text() 也能在
    **碰磁盘之前**做同样的判断（见 _reject_bad_rel 的说明）。
    """
    allowed = set(VIDEO_LABELS) | {"media/startupvids.txt"}
    allowed |= {p for p in _pak_tree(game_dir) if p.startswith("materials/vgui/loadingscreen")}
    return allowed


def _reject_bad_rel(cfg: dict, rel: str) -> dict | None:
    """在**任何文件操作之前**校验 rel。不合法时返回错误 dict，合法时返回 None。

    两道关：
      1. 形状关 —— rel 必须是非空、无绝对路径、无 `..` 的相对路径。
         挡的是「空 rel → disk 退化成 game_dir 本身」和「`..\\..` → 逃出游戏目录」。
      2. 白名单关 —— 只允许替换游戏本体的那几个视频/加载画面/启动清单文件。

    顺序很重要：形状关先跑，因为它不依赖 pak 解析（快），
    而且空串/穿越这类输入根本不该走到解析 pak01 那一步。
    """
    game_dir = cfg.get("game_dir", "")
    if not game_dir or not os.path.isdir(game_dir):
        return {"ok": False, "error": "游戏目录无效"}
    if not rel or not rel.strip():
        return {"ok": False, "error": "缺少 rel（要写入的游戏内相对路径）"}
    r = rel.strip().replace("\\", "/")
    if r.startswith("/") or os.path.isabs(r) or (len(r) > 1 and r[1] == ":"):
        return {"ok": False, "error": "rel 必须是游戏目录内的相对路径: %s" % rel}
    if ".." in r.split("/"):
        return {"ok": False, "error": "rel 不允许包含 .. : %s" % rel}
    if r not in _allowed_rels(game_dir):
        return {"ok": False, "error": "路径不在允许替换的白名单内: %s" % rel}
    return None


# --------------------------------------------------------------------------- #
def replace(cfg: dict, rel: str, src_file: str) -> dict:
    """用 src_file 替换游戏内的 rel。自动备份（含 BASELINE 基线）。"""
    game_dir = cfg.get("game_dir", "")
    if not game_dir or not os.path.isdir(game_dir):
        return {"ok": False, "error": "游戏目录无效"}
    if not os.path.isfile(src_file):
        return {"ok": False, "error": "源文件不存在: %s" % src_file}
    # 硬性拦截：只允许替换白名单内的路径，防止被诱导改到任意系统文件
    bad = _reject_bad_rel(cfg, rel)
    if bad:
        return bad

    disk = os.path.join(game_dir, rel.replace("/", os.sep))
    man = _load_manifest()
    entry = man.setdefault(rel, {})
    base_dir = _backup_dir(rel)
    os.makedirs(base_dir, exist_ok=True)

    # 1) 首次替换：建立 BASELINE（原始状态）
    if not entry.get("baseline"):
        if os.path.isfile(disk):
            base = os.path.join(base_dir, "%s__%s" % (BASELINE_TAG, os.path.basename(disk)))
            shutil.copy2(disk, base)
            entry["baseline"] = base
            entry["baseline_kind"] = "loose"
            safety.log("gamefile-baseline", "%s -> %s" % (disk, base))
        else:
            tmp = os.path.join(base_dir, "%s__%s.frompak" % (BASELINE_TAG, os.path.basename(disk)))
            if _extract_from_pak(game_dir, rel, tmp):
                entry["baseline"] = tmp
                entry["baseline_kind"] = "pak"
                entry["origin_size_h"] = safety.fmt_size(os.path.getsize(tmp))
                safety.log("gamefile-baseline", "从 pak01 抽出原件 %s -> %s" % (rel, tmp))
            else:
                entry["baseline"] = ""
                entry["baseline_kind"] = "none"
                safety.log("gamefile-baseline", "%s 无原始文件（游戏内本来就不存在）" % rel,
                           level="WARN")
        _save_manifest(man)

    # 2) 备份当前散装文件（若存在）
    if os.path.isfile(disk):
        safety.backup_file_to(disk, rel)

    # 3) 落盘（原子替换）
    #    这一步会真的改写玩家磁盘文件，也是最容易失败的一步：游戏正开着 →
    #    文件被独占；Steam 正在校验 → 拒绝写入；磁盘满 → OSError。
    #    原先这里没有 try/except，异常一路冒到 HTTP 层变成「服务端异常」500，
    #    用户只看到一句通用错误，audit.log 里**一个字都没有** ——
    #    对于「到底改没改」这件事，沉默是最糟的答复。这里补上明确留痕。
    try:
        os.makedirs(os.path.dirname(disk), exist_ok=True)
        safety.atomic_copy(src_file, disk)
    except Exception as ex:
        safety.log("gamefile-replace", "写入 %s 失败（可能被游戏或 Steam 占用）：%s: %s"
                   % (disk, type(ex).__name__, ex), level="ERROR", ok=False)
        return {"ok": False, "rel": rel, "disk_path": disk,
                "error": "写入失败：%s。如果游戏正在运行，请先退出游戏再试；"
                         "本次没有改动任何游戏文件（原有的备份不受影响）。"
                         % ex}
    entry["replaced_at"] = safety.human_time(os.path.getmtime(disk))
    entry["origin_size_h"] = entry.get("origin_size_h") or "-"
    _save_manifest(man)
    safety.log("gamefile-replace", "%s <- %s（%s）" % (disk, src_file, safety.fmt_size(
        os.path.getsize(disk))))
    return {"ok": True, "rel": rel, "disk_path": disk,
            "baseline": entry.get("baseline", ""), "baseline_kind": entry.get("baseline_kind", ""),
            "size_h": safety.fmt_size(os.path.getsize(disk))}


def write_text(cfg: dict, rel: str, text: str) -> dict:
    """直接写入文本类游戏文件（如 startupvids.txt）。同样走基线备份。

    ⚠️ 这里必须先做路径校验再碰磁盘。
    原实现是「先 os.makedirs + 写 tmp 文件，再调 replace() 过白名单」——
    白名单只挡住了最后那步替换，而目录和临时文件**已经落在游戏目录外面**了。
    实测：rel 传空串 → 往 left4dead2 的同级目录写 `.workbuddy-edit`；
    rel 传 `..\\..\\..\\x.txt` → 在游戏目录外三层落文件。
    replace() 随后会拒绝，但垃圾文件已经留在用户磁盘上（且抛异常变成 HTTP 500）。
    所以白名单判断必须在**任何文件操作之前**完成。
    """
    bad = _reject_bad_rel(cfg, rel)
    if bad:
        return bad
    game_dir = cfg.get("game_dir", "")
    disk = os.path.join(game_dir, rel.replace("/", os.sep))
    tmp = disk + ".workbuddy-edit"
    os.makedirs(os.path.dirname(disk), exist_ok=True)
    with open(tmp, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(text)
    res = replace(cfg, rel, tmp)
    try:
        os.unlink(tmp)
    except OSError as ex:
        # 临时文件落在**游戏目录内部**（和正式文件同一个目录），删不掉就是
        # 在玩家游戏目录里留了个 .workbuddy-edit 垃圾文件。游戏本身不读它，
        # 但用户会看到一个来路不明的文件，而且下次编辑同一文件时会被覆盖/干扰。
        # 原先静默 pass：用户永远不知道自己游戏目录里多了东西。
        # 注意结果 res 已经拿到了（替换可能已经成功），不能因为清理失败就
        # 谎报失败 —— 只把这条残留附在返回值里，让界面能提示。
        safety.log("gamefile-edit", "临时文件清理失败，已残留在游戏目录：%s: %s"
                   % (tmp, ex), level="WARN", ok=False)
        if isinstance(res, dict):
            res = dict(res)
            res["leftover"] = tmp
            res["leftover_note"] = ("有个临时文件没能删掉，残留在游戏目录里：%s。"
                                    "可以手动删除，不影响已生效的修改。" % tmp)
    return res


def read_current(cfg: dict, rel: str, max_bytes: int = 262144) -> dict:
    """读取某文件的当前内容（优先散装，其次从 pak01 抽），用于界面预览/编辑。

    ⚠️ 必须先过 _reject_bad_rel：否则 rel 传 `..\\..\\..\\Windows\\win.ini`
    就能把游戏目录外的任意文本文件读出来（接口是 GET /api/gamefiles/read）。
    """
    bad = _reject_bad_rel(cfg, rel)
    if bad:
        return bad
    game_dir = cfg.get("game_dir", "")
    disk = os.path.join(game_dir, rel.replace("/", os.sep))
    if os.path.isfile(disk) and os.path.getsize(disk) <= max_bytes:
        with open(disk, "rb") as fp:
            raw = fp.read()
        return {"ok": True, "source": "散装文件（当前生效）", "path": disk,
                "text": raw.decode("utf-8", "replace"), "size": len(raw)}
    pak = os.path.join(game_dir, "pak01_dir.vpk")
    if os.path.isfile(pak):
        try:
            data = Vpk(pak).read_file(rel)
            if data is not None and len(data) <= max_bytes:
                return {"ok": True, "source": "pak01_dir.vpk 内部（未被替换）",
                        "path": pak, "text": data.decode("utf-8", "replace"),
                        "size": len(data)}
        except Exception as ex:
            return {"ok": False, "error": str(ex)}
    return {"ok": False, "error": "读取不到内容（散装文件不存在且 pak01 内也没有）"}


def restore(cfg: dict, rel: str, which: str = "latest") -> dict:
    """还原单个文件。which='baseline' 还原到最初状态，'latest' 回退一步。

    ⚠️ 必须先过 _reject_bad_rel：这条路径会调 safety.to_recycle_bin([disk])，
    也就是**删除磁盘上的文件**。原实现用 rel 直接拼路径，
    rel 传 `..\\..\\..\\某个文件` 就能删到游戏目录外面去。
    """
    bad = _reject_bad_rel(cfg, rel)
    if bad:
        return bad
    game_dir = cfg.get("game_dir", "")
    disk = os.path.join(game_dir, rel.replace("/", os.sep))
    man = _load_manifest()
    entry = man.get(rel, {})
    bks = _backups(rel)

    if which == "baseline":
        base = entry.get("baseline", "")
        kind = entry.get("baseline_kind", "")
        if kind == "pak":
            # 原始文件本来在 pak01 里 → 删掉散装文件即恢复原状
            if os.path.isfile(disk):
                r = safety.to_recycle_bin([disk], fallback_isolate=True)
                if r["failed"]:
                    return {"ok": False, "error": "无法移除散装文件: %s" % r["failed"]}
                note = ("已删除散装文件，恢复 pak01 原始内容"
                        + ("（回收站不可用，已改放隔离区）" if r["fallback"] else ""))
                safety.log("gamefile-restore", "%s %s" % (rel, note))
                return {"ok": True, "rel": rel, "mode": note}
            return {"ok": True, "rel": rel, "mode": "没有散装文件，本来就是原始状态"}
        if kind == "loose" and base and os.path.isfile(base):
            if os.path.isfile(disk):
                safety.backup_file_to(disk, rel)
            safety.atomic_copy(base, disk)
            safety.log("gamefile-restore", "%s <- 基线 %s" % (rel, base))
            return {"ok": True, "rel": rel, "mode": "已还原到首次替换前的文件"}
        # ⚠️ 基线文件不见了，但清单说它本来存在（kind == "loose"）。
        #    这跟 kind == "none"（游戏内本来就没有这个文件）是**完全不同**的两件事，
        #    原实现只看「基线文件此刻还在不在」，于是把前者也当成后者，
        #    直接走下面的删除分支 —— 用户点「还原到最初状态」的结果是
        #    **当前文件被删掉，而原始内容早已无从恢复**。
        #    这里必须当成错误返回，让用户自己决定，而不是替他删。
        if kind == "loose":
            return {"ok": False,
                    "error": ("原始文件的基线备份找不到了（清单记录为 %s，文件已不存在）。"
                              "为避免误删你当前的游戏文件，这里不做任何操作。"
                              "如需清掉当前替换，请用「回退一步」，或手动删除 %s。"
                              % (base or "(空)", disk))}
        # 原本就没有这个文件（kind == "none"）：删掉散装副本即恢复原状
        if os.path.isfile(disk):
            safety.to_recycle_bin([disk], fallback_isolate=True)
        return {"ok": True, "rel": rel, "mode": "游戏内本无此文件，已清除散装副本"}
    else:
        fresh = [b for b in bks if not b["is_baseline"]]
        if not fresh:
            return {"ok": False, "error": "没有可回退的历史备份，只能还原到原始状态"}
        # ⚠️ 覆盖前必须先把当前文件备份一份。
        #    baseline 分支有这步，latest 分支原先漏了 —— 于是「回退一步」是
        #    单向的：回退之后当前版本就永久没了，违反项目自己的红线
        #    「任何破坏性操作前先备份」。
        if os.path.isfile(disk):
            safety.backup_file_to(disk, rel)
        safety.atomic_copy(fresh[0]["file"], disk)
        safety.log("gamefile-restore", "%s <- %s（回退一步）" % (rel, fresh[0]["name"]))
        return {"ok": True, "rel": rel, "mode": "已回退一步到 %s" % fresh[0]["name"]}


def restore_all(cfg: dict) -> dict:
    """把所有被动过的游戏文件还原到原始状态。"""
    man = _load_manifest()
    done, failed = [], []
    for rel in list(man.keys()):
        r = restore(cfg, rel, which="baseline")
        (done if r.get("ok") else failed).append({"rel": rel, "mode": r.get("mode") or r.get("error")})
    return {"ok": not failed, "restored": done, "failed": failed}


def backup_list(cfg: dict) -> list[dict]:
    man = _load_manifest()
    out = []
    for rel, entry in man.items():
        for b in _backups(rel):
            b2 = dict(b)
            b2["rel"] = rel
            b2["size_h"] = safety.fmt_size(b["size"])
            b2["mtime_h"] = safety.human_time(b["mtime"])
            out.append(b2)
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out
