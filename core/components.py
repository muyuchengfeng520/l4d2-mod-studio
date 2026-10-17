# -*- coding: utf-8 -*-
"""组件拆分：把一个 VPK 内部的文件按「部件」归类，用于单独启用/停用。

NekoVpk 的招牌能力是「禁用 addon 内部特定部件」，它的粒度是标签级。
实测发现标签级太粗 —— v0.1.4 把 zoey_light / bill_deathpose / francis_light
合并进了父角色标签，按标签删会把整个角色模型端掉。
所以这里再切一层：**先按标签分组，再往下按单条 file pattern 分组**，
让用户能精确到「只去掉 Bill 的死亡姿势」这种粒度。
"""
from __future__ import annotations

import os
import re
from collections import defaultdict

from . import taglib as tl
from .safety import fmt_size
from .vpk import Vpk, VpkError

# 可安全移除的「附属部件」白名单（相对父标签而言是附加物，删掉不会毁主体）
SAFE_PARTS = [
    {"id": "bill.deathpose", "tag": "Bill", "pattern": "models/survivors/namvet/namvet_deathpose.*",
     "zh": "Bill 死亡姿势", "why": "独立的死亡姿势模型，移除不影响 Bill 本体"},
    {"id": "zoey.light", "tag": "Zoey", "pattern": "models/survivors/survivor_teenangst_light.*",
     "zh": "Zoey 手电/光照变体", "why": "光照变体模型，移除后用游戏默认光照"},
    {"id": "francis.light", "tag": "Francis", "pattern": "models/survivors/survivor_biker_light.*",
     "zh": "Francis 手电/光照变体", "why": "光照变体模型，移除后用游戏默认光照"},
]

# SAFE_PARTS 是模块级常量，pattern 永远不会变 —— 正则在这里编译一次就够。
# 原先在循环体里 re.compile()，每点开一个 mod 都白编译 3 条。
# re 模块自带缓存所以开销不大，但常量正则写在模块层是更清楚的意图表达。
_SAFE_RES = [(sp, re.compile(tl.glob_to_regex(sp["pattern"]))) for sp in SAFE_PARTS]


def component_tree(vpk_path: str, cfg: dict) -> dict:
    """把一个 VPK 的内部文件按部件归类，返回给界面做勾选。"""
    lib: tl.TagLib | None = cfg.get("_taglib")
    v = Vpk(vpk_path)
    # ⚠️ 这里**不要**调 v.load_all()。下面只用 e.vpath / e.size / e.preload_len，
    #    三个值全都来自目录树（parse() 已经读完了），载荷一个字节都不需要。
    #    实测一个 827 MB 的 mod：带 load_all() 是 2.242s / 峰值 832 MB，
    #    去掉后 1.521s / 峰值 5 MB，输出逐字节相同；全语料 194 个 mod 从 20.32s 降到 1.61s。
    #    而界面每次点开一个 mod 都要跑一遍（app/api.py 的组件接口），
    #    服务端又是 ThreadingHTTPServer + daemon_threads，并发点开两个就是 1.6 GB 瞬时分配。
    files = []
    for e in v.entries:
        files.append({"path": e.vpath, "size": e.size,
                      "size_h": _h(e.size), "preload": e.preload_len})
    vpaths = [f["path"] for f in files]
    size_of = {f["path"]: f["size"] for f in files}

    # 1) 标签命中
    hits = lib.match(vpaths) if (lib and lib.loaded) else {}
    used: set[str] = set()
    groups: list[dict] = []
    for tag, paths in sorted(hits.items(), key=lambda x: -len(x[1])):
        spec = (lib.tags.get(tag) or {}) if lib else {}
        uniq = sorted(set(paths))
        if not uniq:
            continue
        total = sum(size_of.get(p, 0) for p in uniq)      # 只算一次，下面复用
        groups.append({
            "id": "tag:" + tag,
            "kind": "标签",
            "name": tag,
            "types": spec.get("type", []),
            "files": uniq,
            "count": len(uniq),
            "size": total,
            "size_h": _h(total),
            "removable": _removable(tag),
            "note": _tag_note(tag),
        })
        used.update(uniq)

    # 2) 剩下没被标签覆盖的，按顶层目录归类
    rest: dict[str, list[str]] = defaultdict(list)
    for p in vpaths:
        if p in used:
            continue
        rest[p.split("/")[0] if "/" in p else "(根目录)"].append(p)
    for d, ps in sorted(rest.items(), key=lambda x: -len(x[1])):
        ps = sorted(ps)
        total = sum(size_of.get(p, 0) for p in ps)        # 同上，只算一次
        groups.append({
            "id": "dir:" + d, "kind": "目录", "name": d, "types": [],
            "files": ps, "count": len(ps),
            "size": total,
            "size_h": _h(total),
            "removable": False,
            "note": "未命中任何标签，按顶层目录归类",
        })

    # 3) 可安全移除的附加部件（单独列出来，方便精修）
    safe_hits = []
    for sp, rx in _SAFE_RES:
        got = [p for p in vpaths if rx.match(p)]
        if got:
            safe_hits.append(dict(sp, files=got, count=len(got),
                                  size_h=_h(sum(size_of.get(p, 0) for p in got))))

    return {
        "vpk": vpk_path,
        "version": v.version,
        "total_files": len(files),
        "total_size_h": _h(sum(f["size"] for f in files)),
        "groups": groups,
        "safe_parts": safe_hits,
        "files": files,
        "caution": ("组件级剔除会改变 VPK 内容：核心部件（主体模型 / 必需脚本）删掉会让 mod 失效。"
                    "标了「可安全移除」的才是真正无副作用的附加物，其余请自行判断。"),
    }


def _removable(tag: str) -> bool:
    return any(sp["tag"] == tag for sp in SAFE_PARTS)


def _tag_note(tag: str) -> str:
    for sp in SAFE_PARTS:
        if sp["tag"] == tag:
            return "该标签下的 %s 可单独移除" % sp["zh"]
    return "核心内容，移除后 mod 多半会失效"


def _h(n: float) -> str:
    return fmt_size(n)


def plan_strip(vpk_path: str, drop: list[str]) -> dict:
    """只读预演：算出剔除后的条目数与体积变化，不写任何文件。"""
    v = Vpk(vpk_path)
    # ⚠️ 归一化必须与 vpk.strip_vpk() 完全一致（那里是
    #    d.strip().lower().replace("\\", "/")）。少了反斜杠替换的话，
    #    drop 里带 "\" 时预览会显示「将剔除 0 项」，真正执行却把文件删了 ——
    #    预览与执行对不上，是这个工具最不能出的一种错。
    dropn = {d.strip().lower().replace("\\", "/") for d in drop}
    kept, gone, gone_size = [], [], 0
    for e in v.entries:
        if e.vpath in dropn:
            gone.append(e.vpath)
            gone_size += e.size
        else:
            kept.append(e.vpath)
    bad, why = v.compression_suspect()
    # os.path.getsize 只取一次。原先这里连调三次（src_size / src_size_h / estimated），
    # 万一在这几行之间文件被改写，同一份预览里就会出现互相矛盾的体积数字 ——
    # 对「点确认前唯一的依据」来说，宁可只读一次快照。
    src_size = os.path.getsize(vpk_path)
    return {
        "src": vpk_path,
        "src_size": src_size,
        "src_size_h": _h(src_size),
        "total": len(v.entries),
        "will_drop": len(gone),
        "will_keep": len(kept),
        "drop_size_h": _h(gone_size),
        "drop_files": gone[:200],
        "drop_truncated": len(gone) > 200,
        "blocked": bad,
        "blocked_reason": why,
        "estimated_size_h": _h(max(0, src_size - gone_size)),
    }
