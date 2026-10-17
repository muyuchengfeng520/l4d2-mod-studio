# -*- coding: utf-8 -*-
"""改生还者模型的归属：把「替换 A 角色」的模型改成「替换 B 角色」。

为什么能做
----------
L4D2 的生还者模型是**按固定的内部路径被覆盖**的：谁提供了
`models/survivors/survivor_coach.mdl`，游戏里的 Coach 就长那样。
所以「改归属」= 把这套文件**改个名字**，挪到另一个角色的槽位上。

本模块只改 `models/` 与 `materials/vgui/` 下的槽位文件名，
**绝不碰材质目录** —— 实测这类 mod 的贴图都在自定义目录里
（如 `materials/starfelll/survivor.boukansya/`），跟角色名无关，
所以改名不影响贴图查找，不会出现「改完变紫黑方块」。

映射表不是猜的
--------------
八位生还者的代号由两条独立证据交叉确认：
  ① 游戏本体 `pak01_*.vpk` 里实际存在的 `v_arms_*` 与 `s_panel_*`；
  ② 本机 mod 库里各「Replace XX」mod 提供的文件名（标题与代号一一对应）。
两个不规则处已在注释里标出，别按公式硬套。
"""
from __future__ import annotations

import os
import re
import zlib
from collections import Counter

from .safety import fmt_size
from .vpk import Vpk, VpkError, write_vpk

# --------------------------------------------------------------------------- #
# 八位生还者。arm 一列**不统一**：L4D1 四人用通用名（bill/zoey/francis/louis），
# L4D2 四人用代号 + `_new`（gambler_new/producer_new/coach_new/mechanic_new）。
# lobby 头像只有 L4D2 四人——游戏本体里就没有 s_panel_lobby_namvet 这类文件。
# --------------------------------------------------------------------------- #
SURVIVORS = [
    {"id": "namvet",    "zh": "Bill",     "game": "L4D1", "body": "survivor_namvet",
     "arm": "v_arms_bill",          "panel": "namvet"},
    {"id": "teenangst", "zh": "Zoey",     "game": "L4D1", "body": "survivor_teenangst",
     "arm": "v_arms_zoey",          "panel": "teenangst"},
    {"id": "biker",     "zh": "Francis",  "game": "L4D1", "body": "survivor_biker",
     "arm": "v_arms_francis",       "panel": "biker"},
    {"id": "manager",   "zh": "Louis",    "game": "L4D1", "body": "survivor_manager",
     "arm": "v_arms_louis",         "panel": "manager"},
    {"id": "gambler",   "zh": "Nick",     "game": "L4D2", "body": "survivor_gambler",
     "arm": "v_arms_gambler_new",   "panel": "gambler"},
    {"id": "producer",  "zh": "Rochelle", "game": "L4D2", "body": "survivor_producer",
     "arm": "v_arms_producer_new",  "panel": "producer"},
    {"id": "coach",     "zh": "Coach",    "game": "L4D2", "body": "survivor_coach",
     "arm": "v_arms_coach_new",     "panel": "coach"},
    {"id": "mechanic",  "zh": "Ellis",    "game": "L4D2", "body": "survivor_mechanic",
     "arm": "v_arms_mechanic_new",  "panel": "mechanic"},
]

BY_ID = {s["id"]: s for s in SURVIVORS}
# L4D2 四人才有大厅头像
LOBBY_IDS = {s["id"] for s in SURVIVORS if s["game"] == "L4D2"}

# 槽位文件所在的目录（只有这几处需要改名，材质目录一律不动）
BODY_DIR = "models/survivors"
ARM_DIR = "models/weapons/arms"
PANEL_DIR = "materials/vgui"

# 从 mdl 里抠「内部引用」用的正则：只要指向 models/ 下的路径串
_REF_RX = re.compile(rb"[\x20-\x7e]{6,}")


def _h(n: int) -> str:
    return fmt_size(n)


# --------------------------------------------------------------------------- #
# 1) 检测：这个 mod 现在挂在哪个槽位上？内部又指向谁？
# --------------------------------------------------------------------------- #
def detect(vpk_path: str) -> dict:
    """分析一个 VPK 替换的是哪个生还者。

    除「外部文件名」外，还会读 `.mdl` 内部的 `$includemodel` 引用
    （`anim_<角色>.mdl` / `gestures_<角色>.mdl`）来判断**内部指向**。
    两者不一致 = 这个 mod 的归属被改过（改了文件名、没改内脏）——
    实测 一个 Coach 槽位的 mod 就是这种情况：外部是 coach，内部是 teenangst。
    """
    v = Vpk(vpk_path)
    paths_ = v.list_files()

    body_hit, arm_hit, panel_hit = [], [], []
    for p in paths_:
        d, _, fn = p.rpartition("/")
        if d == BODY_DIR and fn.startswith("survivor_"):
            for s in SURVIVORS:
                if fn.startswith(s["body"] + "."):
                    body_hit.append((s["id"], p))
        elif d == ARM_DIR and fn.startswith("v_arms_"):
            for s in SURVIVORS:
                if fn.startswith(s["arm"] + "."):
                    arm_hit.append((s["id"], p))
        elif d == PANEL_DIR and fn.startswith("s_panel_"):
            for s in SURVIVORS:
                if fn == "s_panel_%s.vtf" % s["panel"] or \
                   fn == "s_panel_%s_incap.vtf" % s["panel"] or \
                   fn == "s_panel_lobby_%s.vtf" % s["panel"]:
                    panel_hit.append((s["id"], p))

    def _major(hits):
        if not hits:
            return "", []
        c = Counter(i for i, _p in hits)
        top, n = c.most_common(1)[0]
        return (top, [p for i, p in hits if i == top]) if n else ("", [])

    cur, cur_files = _major(body_hit or arm_hit or panel_hit)
    if not cur:
        return {"ok": False, "reason": "这个 mod 没有覆盖任何生还者槽位（不是角色替换类 mod）",
                "files": len(paths_)}

    # 内部指向：读本体 mdl，看它 include 了谁的 anim / gestures
    internal, internal_refs = "", []
    body_mdl = "%s/%s.mdl" % (BODY_DIR, BY_ID[cur]["body"])
    if body_mdl in paths_:
        try:
            data = v.read_file(body_mdl) or b""
            for raw in _REF_RX.findall(data):
                s = raw.decode("ascii", "replace")
                low = s.lower()
                for cand in SURVIVORS:
                    if ("anim_%s.mdl" % cand["id"]) in low or ("gestures_%s.mdl" % cand["id"]) in low:
                        if s not in internal_refs:
                            internal_refs.append(s)
                            if not internal:
                                internal = cand["id"]
        except Exception:
            pass

    return {
        "ok": True,
        "vpk": vpk_path,
        "version": v.version,
        "total_files": len(paths_),
        "total_size_h": _h(os.path.getsize(vpk_path)),
        "current": cur,
        "current_zh": BY_ID[cur]["zh"],
        "current_files": sorted(cur_files),
        "internal": internal,
        "internal_zh": BY_ID[internal]["zh"] if internal else "",
        "internal_refs": internal_refs,
        "mismatch": bool(internal and internal != cur),
        "arm_files": sorted(p for _i, p in arm_hit),
        "panel_files": sorted(p for _i, p in panel_hit),
    }


# --------------------------------------------------------------------------- #
# 2) 预演：算出要改哪些名，以及内部引用能不能跟着改（只读）
# --------------------------------------------------------------------------- #
def plan(vpk_path: str, target: str, fix_internal: bool = True) -> dict:
    """只读预演。不写任何文件。"""
    d = detect(vpk_path)
    if not d.get("ok"):
        return {"ok": False, "error": d.get("reason", "无法识别当前槽位")}
    if target not in BY_ID:
        return {"ok": False, "error": "未知的目标角色: %s" % target}
    if target == d["current"]:
        return {"ok": False, "error": "目标角色与当前槽位相同（%s），没有可改的" % BY_ID[target]["zh"]}

    cur, tgt = BY_ID[d["current"]], BY_ID[target]
    v = Vpk(vpk_path)
    paths_ = set(v.list_files())

    renames: list[dict] = []
    drops: list[dict] = []

    for p in sorted(paths_):
        dirname, _, fn = p.rpartition("/")
        new_fn = None
        if dirname == BODY_DIR and fn.startswith(cur["body"] + "."):
            new_fn = fn.replace(cur["body"], tgt["body"], 1)
        elif dirname == ARM_DIR and fn.startswith(cur["arm"] + "."):
            new_fn = fn.replace(cur["arm"], tgt["arm"], 1)
        elif dirname == PANEL_DIR and fn.startswith("s_panel_lobby_%s." % cur["panel"]):
            # 大厅头像：目标角色若没有对应槽位，就没地方安放 —— 丢弃而不是硬凑
            if target in LOBBY_IDS:
                new_fn = fn.replace("s_panel_lobby_%s" % cur["panel"],
                                    "s_panel_lobby_%s" % tgt["panel"], 1)
            else:
                # 游戏本体里根本没有 s_panel_lobby_namvet 这类文件（见 LOBBY_IDS 的注释），
                # 硬造一个也不会被读取，只会平白多占体积 —— 直接丢弃，并在预演里说清原因。
                drops.append({"path": p,
                              "why": "%s 属于 L4D1 角色，游戏没有 s_panel_lobby_%s 这个槽位，"
                                     "硬填不会被读取，故丢弃" % (tgt["zh"], tgt["panel"])})
        elif dirname == PANEL_DIR and fn.startswith("s_panel_%s" % cur["panel"]):
            new_fn = fn.replace("s_panel_%s" % cur["panel"], "s_panel_%s" % tgt["panel"], 1)
        if new_fn and new_fn != fn:
            renames.append({"from": p, "to": dirname + "/" + new_fn})

    # 内部引用：只能**等长**字节替换。MDL 内部有偏移表，长度一变就毁结构。
    internal_plan = []
    body_mdl = "%s/%s.mdl" % (BODY_DIR, cur["body"])
    if fix_internal and body_mdl in paths_:
        try:
            data = v.read_file(body_mdl) or b""
            for cand_id in {d["internal"], d["current"]} - {target}:
                if not cand_id:
                    continue
                for kind, tpl in (("anim", "models/survivors/anim_%s.mdl"),
                                  ("gestures", "models/survivors/gestures_%s.mdl")):
                    old = (tpl % cand_id).encode()
                    new = (tpl % target).encode()
                    if old in data:
                        internal_plan.append({
                            "kind": kind, "from": old.decode(), "to": new.decode(),
                            "applicable": len(old) == len(new),
                            "why": "" if len(old) == len(new)
                                   else "%s(%d) 与 %s(%d) 长度不同，等长替换做不到，跳过"
                                        % (cand_id, len(cand_id), target, len(target))})
        except Exception:
            pass

    return {
        "ok": True,
        "vpk": vpk_path,
        "from": {"id": cur["id"], "zh": cur["zh"]},
        "to": {"id": tgt["id"], "zh": tgt["zh"]},
        "current": d["current"], "current_zh": d["current_zh"],
        "internal": d["internal"], "internal_zh": d["internal_zh"],
        "mismatch": d["mismatch"],
        "total": len(paths_),
        "renames": renames,
        "rename_count": len(renames),
        "drops": drops,
        "drop_count": len(drops),
        "keep_count": len(paths_) - len(drops),
        "internal_plan": internal_plan,
        "src_size_h": _h(os.path.getsize(vpk_path)),
        "note": ("改归属只动槽位文件名与头像，贴图目录一律不动 —— "
                 "这类 mod 的材质都在自定义目录里，改名不影响它找贴图。"),
    }


# --------------------------------------------------------------------------- #
# 3) 执行
# --------------------------------------------------------------------------- #
def apply(vpk_path: str, out_path: str, target: str, fix_internal: bool = True) -> dict:
    """按 plan 改写，产出 out_path。不覆盖源文件（备份与命名由上层负责）。"""
    p = plan(vpk_path, target, fix_internal)
    if not p.get("ok"):
        raise VpkError(p.get("error", "预演失败"))
    if not p["renames"] and not p["drops"]:
        raise VpkError("没有需要改动的条目")

    src = Vpk(vpk_path)
    src.load_all()

    rename_map = {r["from"]: r["to"] for r in p["renames"]}
    drop_set = {d["path"] for d in p["drops"]}

    # 内部引用的等长替换表（只收 applicable 的）
    byte_repl = [(i["from"].encode(), i["to"].encode())
                 for i in p["internal_plan"] if i["applicable"]]
    internal_applied: list[str] = []

    keep = []
    renamed, dropped = [], []
    # 期望集合**在动手之前**就按预演结果算好（改名用 to、其余用原名、丢弃的不算）。
    # ⚠️ 绝不能用 {e.vpath for e in keep} 当期望 —— 那是用「刚被自己改过的对象」
    #    算出来的，改名逻辑一旦写错，错误会同时出现在期望与实际两边而互相抵消，
    #    校验永远「通过」。实测踩过：filename 误带扩展名那次，ok 居然是 True。
    intent = set(rename_map.values()) | (set(src.list_files()) - set(rename_map) - drop_set)
    for e in src.entries:
        vp = e.vpath
        if vp in drop_set:
            dropped.append(vp)
            continue
        new_vp = rename_map.get(vp)
        if new_vp:
            # ⚠️ e.filename **不含扩展名**（vpk.py：fn = filename + "." + ext）。
            #    new_vp 是完整路径，得先把扩展名剥掉再赋给 filename，
            #    否则 write_vpk 会再拼一次 ext，写出 survivor_namvet.dx90.vtx.vtx
            #    —— 这种成品能正常解析（所以元数据自比对查不出来），但游戏根本不认。
            _ndir, _sep, new_fn_with_ext = new_vp.rpartition("/")
            if e.ext and new_fn_with_ext.endswith("." + e.ext):
                new_fn = new_fn_with_ext[: -(len(e.ext) + 1)]
            else:
                new_fn = new_fn_with_ext
            # ⚠️ vpath 是 @cached_property（vpk.py 里刻意做的性能优化）。
            #    改了 filename 必须手动清缓存，否则后续读到的还是旧路径 ——
            #    vpk.py 的注释里明确写了「构造后改写字段会让缓存失效」。
            e.filename = new_fn
            e.__dict__.pop("vpath", None)
            renamed.append("%s → %s" % (vp, new_vp))
            # 写进去重集合用的是**预演算出的目标名**，不是 e.vpath ——
            # 见下面校验那里的说明，别拿自己的输出当自己的期望。
            intent.add(new_vp)
            # 顺手修 mdl 内部的引用（等长替换，不改文件长度）
            if byte_repl and e.ext == "mdl" and e.data:
                new_data = e.data
                for old_b, new_b in byte_repl:
                    if old_b in new_data:
                        new_data = new_data.replace(old_b, new_b)
                        internal_applied.append(old_b.decode())
                if new_data != e.data:
                    e.data = new_data
                    # ⚠️ write_vpk 是直接把 e.crc 写进目录树的（vpk.py 第 524 行），
                    #    **不会重算**。改了内容却留着旧 crc，成品就是「元数据与内容不符」
                    #    的坏文件 —— strip_vpk 从不改 data 所以没这个问题，这里必须补。
                    #    crc 的口径是 full_data（preload + data），与 strip_vpk 的
                    #    期望摘要算法保持一致。
                    e.crc = zlib.crc32(e.full_data) & 0xFFFFFFFF
        keep.append(e)

    write_vpk(out_path, keep, 1)

    # 写完成品立刻重读自校验（跟 strip_vpk 同一条红线）
    out = Vpk(out_path)
    got = set(out.list_files())
    want = intent                      # 预演阶段算好的目标，不依赖刚改过的对象
    missing = sorted(want - got)
    extra = sorted(got - want)
    src_changed_paths = sorted({r["from"] for r in p["renames"]} & got)

    return {
        "ok": not missing and not extra,
        "out": out_path,
        "out_size": os.path.getsize(out_path),
        "out_size_h": _h(os.path.getsize(out_path)),
        "总条目": len(src.entries),
        "改名": len(renamed),
        "丢弃": len(dropped),
        "保留": len(keep),
        "改名明细": renamed[:60],
        "丢弃明细": dropped[:60],
        "内部引用已改": internal_applied,
        "校验": {
            "重读条目数": len(got),
            "期望条目数": len(want),
            "缺失": missing[:20],
            "多余": extra[:20],
            "旧名字是否已消失": not src_changed_paths,
            "残留旧名": src_changed_paths[:20],
        },
    }
