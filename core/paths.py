# -*- coding: utf-8 -*-
"""路径探测与配置持久化。

优先级：data/config.json 里用户手填的路径 > 自动探测（Steam 库配置 + 常见盘符）。
结论一律带依据（探测命中的文件/注册表项），便于人工复核。
"""
from __future__ import annotations

import json
import os
import re
import shutil

from . import safety

CONFIG_PATH = os.path.join(safety.DATA_DIR, "config.json")

# 上一次 load() 是否因配置损坏而回退到默认值。
# bootstrap() 靠它决定「能不能把自动探测的结果写回磁盘」—— 见 bootstrap 里的说明。
_LAST_LOAD_CORRUPT = False

# Steam 安装根目录候选模板（按盘符展开）
STEAM_ROOT_TEMPLATES = [
    r"{d}:\Program Files (x86)\Steam",
    r"{d}:\Program Files\Steam",
    r"{d}:\Steam",
    r"{d}:\SteamLibrary",
]
DRIVES = "CDEFGH"

DEFAULT = {
    "game_dir": "",           # …\Left 4 Dead 2\left4dead2
    "repo_dir": "",           # FireAxe 素材仓库（可选）
    "taglib": "",             # NekoVpk TaggedAssets.jsonc（可选，留空用内置）
}


def load() -> dict:
    """读配置。文件不存在 → 返回默认值（正常首次运行）；文件损坏 → 备份 + 记 ERROR。

    ⚠️ 原先这里是 `except Exception: pass` —— 文件解析失败与「文件不存在」被混为一谈。
    后果和 ai.json 的密钥丢失是同一类：config.json 一旦写坏，load() 会安安静静地
    返回三个空串，紧接着自动探测可能找到**另一份**游戏目录，save() 就以这份
    「重新探测的结果」为底把它写回磁盘 —— 用户手填的那条路径无声消失，
    而坏文件被原地覆盖，事后无从追查。
    对会改写玩家磁盘文件的工具来说，「静默的不确定」比「明确的报错」危险得多。
    """
    cfg = dict(DEFAULT)
    global _LAST_LOAD_CORRUPT
    _LAST_LOAD_CORRUPT = False
    if not os.path.isfile(CONFIG_PATH):
        return cfg
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception as ex:
        # 保留现场：坏文件另存一份，绝不原地覆盖用户手填的路径。
        bad_path = CONFIG_PATH + ".bad"
        try:
            shutil.copy2(CONFIG_PATH, bad_path)
        except Exception:
            bad_path = "(备份失败)"
        _LAST_LOAD_CORRUPT = True
        safety.log("config-corrupt", "config.json 解析失败，已保留原文件到 %s：%s: %s"
                   % (bad_path, type(ex).__name__, ex), level="ERROR", ok=False)
        return cfg
    if not isinstance(data, dict):
        _LAST_LOAD_CORRUPT = True
        safety.log("config-corrupt", "config.json 顶层不是对象（实际是 %s），已忽略"
                   % type(data).__name__, level="ERROR", ok=False)
        return cfg
    # 只接受已知键，且值必须是字符串：手改配置写成 null / 数字时不该让下游炸掉。
    for k in DEFAULT:
        v = data.get(k)
        if isinstance(v, str):
            cfg[k] = v
    return cfg


def save(cfg: dict) -> None:
    merged = load()
    merged.update({k: v for k, v in cfg.items() if k in DEFAULT})
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    # 原子写：配置文件写到一半断电会毁掉用户手填的路径，必须走「临时文件 + os.replace」。
    safety.atomic_write_text(CONFIG_PATH, json.dumps(merged, ensure_ascii=False, indent=2))
    safety.log("config-save", json.dumps(merged, ensure_ascii=False))


def _steam_roots() -> list[tuple[str, str]]:
    """扫描所有盘符找 Steam 安装根，返回 [(根目录, 依据)]。"""
    out = []
    for d in DRIVES:
        for tpl in STEAM_ROOT_TEMPLATES:
            p = tpl.format(d=d)
            if os.path.isfile(os.path.join(p, "steamapps", "libraryfolders.vdf")):
                out.append((p, "发现 Steam 根 %s/steamapps/libraryfolders.vdf" % p))
    return out


def _library_roots() -> list[tuple[str, str]]:
    """解析 libraryfolders.vdf，拿到所有 Steam 库根目录，附带来源说明。"""
    found: list[tuple[str, str]] = []
    for root, why in _steam_roots():
        found.append((root, why))
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, "r", encoding="utf-8", errors="replace") as fp:
                txt = fp.read()
        except Exception:
            continue
        for m in re.finditer(r'"path"\s*"([^"]+)"', txt):
            p = m.group(1).replace("\\\\", "\\")
            if os.path.isdir(p) and p.lower() != root.lower():
                found.append((p, "libraryfolders.vdf 登记: %s" % vdf))
    # 兜底：常见自定义库位置（怕 libraryfolders 漏登）
    for d in DRIVES:
        for tpl in (r"{d}:\SteamLibrary", r"{d}:\Steam"):
            p = tpl.format(d=d)
            if os.path.isdir(os.path.join(p, "steamapps", "common")):
                found.append((p, "兜底扫描到库目录 %s" % p))
    seen, out = set(), []
    for p, src in found:
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append((p, src))
    return out


def _score_game_dir(path: str) -> tuple[int, list[str]]:
    """给一个候选游戏目录打分。分数高者才可能是真在玩的安装。

    依据（实测教训：D 盘存在一份只剩 addonlist.txt 的残骸安装，
    若按目录存在就选中，会把游戏文件改到废目录里去）：
    """
    score, why = 0, []
    exe = os.path.join(os.path.dirname(path), "left4dead2.exe")
    if os.path.isfile(exe):
        score += 100
        why.append("+100 存在左4死2.exe")
    if os.path.isfile(os.path.join(path, "pak01_000.vpk")):
        score += 100
        why.append("+100 存在 pak01_000.vpk（完整游戏数据）")
    if os.path.isfile(os.path.join(path, "pak01_dir.vpk")):
        score += 50
        why.append("+50 存在 pak01_dir.vpk")
    ad = os.path.join(path, "addons")
    if os.path.isdir(ad):
        n = len([f for f in os.listdir(ad) if f.lower().endswith(".vpk")])
        score += min(n, 60)
        why.append("+%d addons 内有 %d 个 VPK" % (min(n, 60), n))
    else:
        why.append("addons 目录不存在")
    # 同库的 appmanifest 是否标记为完整安装
    lib = os.path.dirname(os.path.dirname(os.path.dirname(path)))
    acf = os.path.join(lib, "steamapps", "appmanifest_550.acf")
    if os.path.isfile(acf):
        try:
            with open(acf, "r", encoding="utf-8", errors="replace") as fp:
                t = fp.read()
            if re.search(r'"StateFlags"\s*"4"', t):
                score += 20
                why.append("+20 appmanifest_550 StateFlags=4（已完整安装）")
            m = re.search(r'"SizeOnDisk"\s*"(\d+)"', t)
            if m:
                why.append("  SizeOnDisk=%.2f GB" % (int(m.group(1)) / 1073741824))
        except Exception:
            pass
    return score, why


def find_candidates() -> list[dict]:
    """列出所有候选游戏目录并按可信度排序。"""
    cands: list[dict] = []
    seen = set()
    pool = [p for p, _ in _library_roots()]
    for d in DRIVES:
        pool += [r"{d}:\Program Files (x86)\Steam\steamapps\common\left 4 dead 2\left4dead2".format(d=d),
                 r"{d}:\SteamLibrary\steamapps\common\left 4 dead 2\left4dead2".format(d=d)]
    for lib in pool:
        common = os.path.join(lib, "steamapps", "common", "Left 4 Dead 2", "left4dead2")
        if lib.lower().endswith("left4dead2"):
            common = lib
        if not os.path.isdir(common) or common.lower() in seen:
            continue
        seen.add(common.lower())
        score, why = _score_game_dir(common)
        cands.append({"path": common, "score": score, "why": why})
    cands.sort(key=lambda x: -x["score"])
    return cands


def autodetect_game() -> tuple[str, list[str]]:
    """返回 (最佳游戏 left4dead2 目录, 探测依据)。找不到时目录为空串。"""
    evidence: list[str] = []
    cands = find_candidates()
    if not cands:
        evidence.append("未自动命中，请手动填写 left4dead2 目录")
        return "", evidence
    for c in cands:
        evidence.append("候选 %s —— 可信度 %d 分" % (c["path"], c["score"]))
        evidence += ["    " + w for w in c["why"]]
    best = cands[0]
    if best["score"] <= 0:
        evidence.append("⚠ 最高分候选为 0 分，可能不是有效安装；请人工确认")
        return "", evidence
    evidence.append("选中最高分目录: %s" % best["path"])
    return best["path"], evidence


def validate_game_dir(path: str) -> dict:
    """校验一个目录是不是 L4D2 的 left4dead2 目录，返回证据。"""
    res = {"ok": False, "path": os.path.abspath(path) if path else "", "checks": []}
    if not path or not os.path.isdir(path):
        res["checks"].append(("目录存在", False))
        return res
    res["checks"].append(("目录存在", True))
    checks = [
        ("pak01_dir.vpk 存在", os.path.isfile(os.path.join(path, "pak01_dir.vpk"))),
        ("addons 目录存在", os.path.isdir(os.path.join(path, "addons"))),
        ("media 目录存在", os.path.isdir(os.path.join(path, "media"))),
        ("scripts 目录存在", os.path.isdir(os.path.join(path, "scripts"))),
    ]
    res["checks"] += checks
    res["ok"] = checks[0][1] and checks[1][1]
    return res


def game_sub(cfg: dict, *parts: str) -> str:
    g = cfg.get("game_dir") or ""
    return os.path.join(g, *parts) if g else ""


def addons_dir(cfg: dict) -> str:
    return game_sub(cfg, "addons")


def addonlist_path(cfg: dict) -> str:
    return game_sub(cfg, "addonlist.txt")


def autodetect_repo() -> tuple[str, list[str]]:
    """探测 FireAxe 素材仓库，返回 (仓库根, 依据)。

    实测结构：<仓库根>\\<分类目录>\\<标题>.workshop\\<标题>-time_updated-<ts>.vpk
    仓库根 = 所有「直接含 *.workshop 的目录」的**最深公共祖先之上的那一层**。
    用公共祖先回溯，比按固定层级猜稳得多（多包一层版本号目录也不会认错）。
    """
    ev: list[str] = []
    roots: list[str] = []
    for d in DRIVES:
        for sub in ("求生之路2mod", "L4D2mod", "l4d2mod", "L4D2Mod", "L4D2", "mod",
                    "Left 4 Dead 2 mod", "求生之路2"):
            roots.append("%s:\\%s" % (d, sub))

    for r in roots:
        if not os.path.isdir(r):
            continue
        ev.append("候选仓库根: %s" % r)
        containers: list[str] = []
        base_depth = r.rstrip("\\/").count(os.sep)
        for root, dirs, _files in os.walk(r):
            if root.count(os.sep) - base_depth > 3:
                dirs[:] = []
                continue
            ws = [x for x in dirs if x.endswith(".workshop")]
            if ws:
                containers.append(root)
                ev.append("  %s 下含 %d 个 .workshop" % (root, len(ws)))
                dirs[:] = [x for x in dirs if not x.endswith(".workshop")]
        if not containers:
            continue
        # 1) 所有 container 的公共祖先
        parts = [os.path.normpath(c).split(os.sep) for c in containers]
        common = parts[0]
        for p in parts[1:]:
            i = 0
            while i < len(common) and i < len(p) and common[i].lower() == p[i].lower():
                i += 1
            common = common[:i]
        cand = os.sep.join(common)
        # 2) 若公共祖先自己长得像分类目录（01-幸存者 这种），再往上一层
        #    （只有一个分类目录时公共祖先是它本身，这里兜住）
        for _ in range(3):
            base = os.path.basename(cand.rstrip("\\/"))
            if re.match(r"^\d{1,3}[-_\s]", base):
                parent = os.path.dirname(cand.rstrip("\\/"))
                if parent and os.path.dirname(parent) != parent:
                    cand = parent
                    continue
            break
        if os.path.isdir(cand):
            ev.append("命中 FireAxe 仓库: %s（%d 个分类容器，公共祖先回溯）"
                      % (cand, len(containers)))
            return cand, ev
    ev.append("未探测到 FireAxe 仓库（可选，不影响主流程）")
    return "", ev


def autodetect_taglib() -> str:
    cands = [
        # ① 工具自带的标签库：exe 模式下在解包目录里，源码模式下就是项目里的 data/taglib
        os.path.join(safety.APP_ROOT, "data", "taglib", "TaggedAssets.jsonc"),
        # ② 可写数据目录下的同名文件（用户想自己换一份放这儿）
        os.path.join(safety.DATA_DIR, "taglib", "TaggedAssets.jsonc"),
        # ③ 本机 NekoVpk 安装位置的原始文件
        *[os.path.join("%s:\\NekoVpk" % d, "TaggedAssets.jsonc") for d in DRIVES],
    ]
    # ④ 开发机上的技能目录（只在源码模式有意义，exe 拷到别的机器就没有了）
    skill = os.path.expanduser(r"~\.workbuddy\skills\l4d2-mod-classifier\references\TaggedAssets.jsonc")
    cands.append(skill)
    for c in cands:
        if os.path.isfile(c):
            return c
    return ""


def bootstrap(force: bool = False) -> dict:
    """确保配置可用：缺失就自动探测一次。返回配置 + 探测依据。"""
    cfg = load()
    # load() 刚跑完就把「是否损坏」记下来：下面调 save() 时它内部还会再 load()
    # 一次而把标志重置，晚读就拿不到了。
    corrupt = _LAST_LOAD_CORRUPT
    ev: dict[str, list[str]] = {}
    changed = False
    if force or not cfg["game_dir"] or not os.path.isdir(cfg["game_dir"]):
        g, e = autodetect_game()
        ev["game_dir"] = e
        if g:
            cfg["game_dir"], changed = g, True
    else:
        ev["game_dir"] = ["沿用已保存配置: %s" % cfg["game_dir"]]
    if force or not cfg["repo_dir"] or not os.path.isdir(cfg["repo_dir"]):
        r, e = autodetect_repo()
        ev["repo_dir"] = e
        if r:
            cfg["repo_dir"], changed = r, True
    else:
        ev["repo_dir"] = ["沿用已保存配置: %s" % cfg["repo_dir"]]
    if not cfg["taglib"] or not os.path.isfile(cfg["taglib"]):
        t = autodetect_taglib()
        if t:
            cfg["taglib"], changed = t, True
    if changed:
        if corrupt:
            # 配置刚被判为损坏：这时候绝对不能把自动探测的结果写回去 ——
            # 那等于用「重新猜出来的路径」原地覆盖用户手填的那份，坏文件被抹掉、
            # 事后无从追查（原文件已另存为 config.json.bad，但用户多半不会去看）。
            # 只把探测结果交出去让本次运行能用，落盘等用户在设置页显式保存。
            ev["config_corrupt"] = [
                "config.json 损坏，本次运行改用自动探测结果，**没有写回磁盘**；"
                "原文件已另存为 config.json.bad。请在设置页确认路径后手动保存。"]
        else:
            save(cfg)
    return {"config": cfg, "evidence": ev}
