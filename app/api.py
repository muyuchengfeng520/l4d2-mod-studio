# -*- coding: utf-8 -*-
"""API 实现：原来挂在 Handler 上的 20 个 api_* 方法，全部搬到这里变成模块级函数。

设计约定
--------
每个处理函数的签名统一是 ``def api_xxx(ctx) -> dict``：

* ``ctx`` 是**一次请求**的上下文（见 `Ctx`），携带两类东西：
    - 进程级依赖：``get_cfg / do_scan / mod_index / payload_scan / state / lock / port``
    - 本次请求入参：``query / body / params``（路径参数）
* 本模块**不 import server**，也不持有任何模块级可变状态 ——
  依赖全部由 server.py 在每次请求时注入。这样避免了循环 import，
  也让这些函数可以直接在测试里被调用（塞一个假的 ctx 就行）。

没有用任何依赖注入框架，就是一个普通对象 + 属性访问。

关于原 Handler 上的 `_body()`：搬到 http_util.read_body() 之后，
`ctx.body` 已经是解析好的 dict，处理函数不用再关心请求体怎么读。
"""
from __future__ import annotations

import base64
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import (ai, audit, components, gamefiles, manager, paths, reassign,  # noqa: E402
                  safety, scan, vpk)
from core import acquire


class Ctx:
    """一次请求的上下文。由 server.py 构造，处理函数只读它、不改它的依赖字段。

    ⚠️ 刻意**不用**全局单例：``state`` 是 server 的 STATE 字典引用（同一个对象），
    ``port`` 是构造那一刻读到的 PORT —— 所以 server.main() 改了 PORT 之后，
    下一个请求构造出来的 ctx 自然带新值，不会读到旧值。
    """

    __slots__ = ("h", "web_dir", "get_cfg", "do_scan", "mod_index", "payload_scan",
                 "state", "lock", "port", "query", "params",
                 "_read_body", "_body", "_body_done")

    def __init__(self, h=None, web_dir="", get_cfg=None, do_scan=None, mod_index=None,
                 payload_scan=None, state=None, lock=None, port=0,
                 query=None, body=None, params=None, read_body=None):
        self.h = h
        self.web_dir = web_dir
        self.get_cfg = get_cfg
        self.do_scan = do_scan
        self.mod_index = mod_index
        self.payload_scan = payload_scan
        self.state = state
        self.lock = lock
        self.port = port
        self.query = query or {}
        self.params = params or {}
        self._read_body = read_body
        self._body = body if body is not None else {}
        # 测试里直接塞 body，或者没接 HTTP（read_body 为空）时，就当已经读好了
        self._body_done = body is not None or read_body is None

    # -- 便捷访问器 -------------------------------------------------------- #
    def q(self, name: str, default: str = "") -> str:
        """取 query 里的单值。原代码是 ``q.get(name, [""])[0]``，语义保持一致。

        parse_qs 默认丢弃空值，所以 ``?key=`` 拿到的是 default。
        """
        v = self.query.get(name)
        return v[0] if v else default

    @property
    def cfg(self) -> dict:
        return self.get_cfg()

    @property
    def body(self) -> dict:
        """请求体。**第一次访问时才真的去 rfile 读**。

        ⚠️ 必须保持惰性：原代码里只有确实要用 body 的分支才调 self._body()，
        `/api/scan`、`/api/gamefiles/restore-all` 这两条 POST 是**不读**请求体的。
        改成一进 do_POST 就无脑读，会让「声明了 Content-Length 却没发体」的
        客户端把工作线程挂在 read 上。
        """
        if not self._body_done:
            self._body = self._read_body(self.h)
            self._body_done = True
        return self._body


# --------------------------------------------------------------------------- #
# 状态 / 引导
# --------------------------------------------------------------------------- #
def api_status(ctx: Ctx) -> dict:
    cfg = ctx.get_cfg()
    v = paths.validate_game_dir(cfg.get("game_dir", ""))
    tl_ = ctx.state["taglib"]
    return {
        # 身份签名：新实例启动前靠它判断「端口上跑的是不是自己人」
        "app": "L4D2 Mod Studio",
        "pid": os.getpid(),
        "port": ctx.port,
        "config": {k: val for k, val in cfg.items() if not k.startswith("_")},
        "game_valid": v,
        "taglib": {"source": tl_.path, "tags": len(tl_.tags),
                   "rules": tl_.rule_count, "error": tl_.error},
        "scanned": bool(ctx.state.get("last_scan")),
        "policy": {
            "batch_limit": manager.BATCH_LIMIT,
            "delete_goes_to": "Windows 回收站",
            "backup_dir": safety.BACKUP_DIR,
            "staging_dir": safety.STAGING_DIR,
        },
    }


def api_bootstrap(ctx: Ctx) -> dict:
    ctx.get_cfg()
    return {
        "config": {k: val for k, val in ctx.state["cfg"].items() if not k.startswith("_")},
        "evidence": ctx.state.get("boot_evidence", {}),
        "candidates": paths.find_candidates(),
        "taglib": {"source": ctx.state["taglib"].path, "tags": len(ctx.state["taglib"].tags),
                   "rules": ctx.state["taglib"].rule_count,
                   "error": ctx.state["taglib"].error},
    }


def api_candidates(ctx: Ctx) -> dict:
    return {"candidates": paths.find_candidates()}


def api_set_config(ctx: Ctx) -> dict:
    b = ctx.body
    ok = {}
    if b.get("game_dir"):
        v = paths.validate_game_dir(b["game_dir"])
        if not v["ok"]:
            return {"ok": False, "error": "该目录不像 L4D2 的 left4dead2 目录",
                    "checks": v["checks"]}
        ok["game_dir"] = os.path.abspath(b["game_dir"])
    for k in ("repo_dir", "taglib"):
        if b.get(k) is not None:
            val = b[k].strip()
            if val and not os.path.exists(val):
                return {"ok": False, "error": "%s 路径不存在: %s" % (k, val)}
            ok[k] = val
    paths.save(ok)
    ctx.get_cfg(force=True)
    ctx.state["taglib"] = None
    ctx.get_cfg()
    ctx.state["last_scan"] = None
    ctx.state["mods"] = None
    return {"ok": True, "config": {k: v for k, v in ctx.state["cfg"].items()
                                   if not k.startswith("_")}}


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #
def api_scan(ctx: Ctx) -> dict:
    return ctx.payload_scan()


def api_rescan(ctx: Ctx) -> dict:
    t0 = time.time()
    ctx.do_scan(force=True)
    d = ctx.payload_scan()
    d["took"] = round(time.time() - t0, 2)
    return d


def api_mod(ctx: Ctx) -> dict:
    key = ctx.q("key")
    if not key:
        return {"ok": False, "error": "缺少 key"}
    idx = ctx.mod_index()
    m = idx.get(key)
    if not m:
        return {"ok": False, "error": "找不到 mod: %s" % key}
    d = m.to_json()
    d["evidence"] = m.evidence
    tree = {}
    if m._vpk_path and os.path.isfile(m._vpk_path):
        try:
            tree = components.component_tree(m._vpk_path, ctx.get_cfg())
        except Exception as ex:
            tree = {"error": "组件解析失败: %s" % ex}
    return {"ok": True, "mod": d, "components": tree}


def api_audit(ctx: Ctx) -> dict:
    cfg = ctx.get_cfg()
    if ctx.state.get("mods_for_audit") is None:
        ctx.do_scan()
    al = paths.addonlist_path(cfg)
    # 用 paths.addons_dir() 而不是手拼 —— 上一行刚用了 paths.addonlist_path()，
    # 同一个函数里两种写法容易在将来改目录结构时只改一边。
    addons = paths.addons_dir(cfg)
    res = audit.run_all(ctx.state["mods_for_audit"], cfg["game_dir"], addons,
                        scan.read_addonlist(al), cfg.get("repo_dir", ""))
    res["ok"] = True
    return res


# --------------------------------------------------------------------------- #
# 开关（addonlist）
# --------------------------------------------------------------------------- #
def api_enable(ctx: Ctx) -> dict:
    """三种用法：{changes:{k:bool}} / {keys:[..],enable:bool} / {category:..,enable:bool}。

    ⚠️ 三个分支都必须回写内存状态（踩坑记录）：
       原先只有 changes 分支同步了 STATE["mods"] 各记录的 enabled 与
       last_scan["mods_json"]，keys / category 分支改完 addonlist.txt 就返回了 ——
       结果是「文件改了、内存没改」，界面刷新后开关状态又跳回旧值，
       直到下一次 /api/scan 才对齐。这里统一算出一份 `wanted`（key → 目标状态），
       三个分支共用同一段回写逻辑。
    """
    cfg = ctx.get_cfg()
    b = ctx.body
    al = paths.addonlist_path(cfg)
    wanted: dict = {}
    if b.get("changes"):
        changes = {k: bool(v) for k, v in b["changes"].items()}
        if len(changes) > manager.BATCH_LIMIT:
            return {"ok": False, "error": "单批最多 %d 项（本批 %d 项），请分批操作"
                                          % (manager.BATCH_LIMIT, len(changes)),
                    "need_confirm": True, "count": len(changes)}
        res = manager.set_enabled(al, changes)
        wanted = dict(changes)
    elif b.get("keys"):
        enable = bool(b.get("enable"))
        res = manager.batch_by_mods(al, [{"key": k} for k in b["keys"]], enable)
        wanted = {k: enable for k in b["keys"]}
    elif b.get("category"):
        enable = bool(b.get("enable"))
        mods = [m.to_json() for m in (ctx.state.get("mods") or [])]
        res = manager.batch_by_category(al, mods, b["category"], enable)
        wanted = {m["key"]: enable for m in mods if m["category"] == b["category"]}
    else:
        return {"ok": False, "error": "参数不足"}
    # 开关状态变了 → 刷新内存中的记录（三个分支共用）
    if res.get("ok") and wanted:
        for m in ctx.state.get("mods") or []:
            if m.key in wanted:
                m.enabled = bool(wanted[m.key])
        for d in (ctx.state.get("mods_for_audit") or []):
            if d["key"] in wanted:
                d["enabled"] = bool(wanted[d["key"]])
        if ctx.state.get("last_scan"):
            ctx.state["last_scan"]["mods_json"] = [x.to_json() for x in ctx.state["mods"]]
    return res


def api_isolate(ctx: Ctx) -> dict:
    b = ctx.body
    cfg = ctx.get_cfg()
    al = paths.addonlist_path(cfg)
    keys = b.get("keys") or []
    if not keys:
        return {"ok": False, "error": "未选择任何 mod"}
    if len(keys) > manager.BATCH_LIMIT:
        return {"ok": False, "error": "单批最多 %d 项（本批 %d 项），请分批操作"
                                      % (manager.BATCH_LIMIT, len(keys)),
                "need_confirm": True, "count": len(keys)}
    if not b.get("confirm"):
        # 先出清单，等他点头
        idx = ctx.mod_index()
        preview = []
        for k in keys:
            m = idx.get(k)
            preview.append({"key": k, "title": m.title if m else "",
                            "size_h": m.to_json()["size_h"] if m else "",
                            "repo_dir": (m.repo_dir if m else "")})
        return {"ok": False, "need_confirm": True, "count": len(keys),
                "preview": preview,
                "warning": "⚠ 将把这些 mod 移入隔离区（可随时还原，不会销毁）。"
                           "同时会从 addonlist.txt 里摘除对应条目。",
                "dest": safety.STAGING_DIR}
    idx = ctx.mod_index()
    mods = [idx[k].to_json() for k in keys if k in idx]
    if not mods:
        return {"ok": False, "error": "选中的 mod 不存在"}
    return manager.isolate_mods(al, mods, isolate_repo=bool(b.get("isolate_repo", True)))


# --------------------------------------------------------------------------- #
# 隔离区
# --------------------------------------------------------------------------- #
def api_staging(ctx: Ctx) -> dict:
    return {"items": manager.list_staging()}


def api_staging_restore(ctx: Ctx) -> dict:
    b = ctx.body
    cfg = ctx.get_cfg()
    al = paths.addonlist_path(cfg)
    return manager.restore_from_staging(al, b.get("path", ""), b.get("target_dir") or None)


def api_staging_recycle(ctx: Ctx) -> dict:
    b = ctx.body
    paths_ = b.get("paths") or []
    if not paths_:
        return {"ok": False, "error": "未选择条目"}
    if not b.get("confirm"):
        return {"ok": False, "need_confirm": True, "count": len(paths_),
                "warning": "⚠⚠ 这一步会把隔离区里的内容丢进 Windows 回收站。"
                           "仍然可以在回收站里找回，但请确认你不再需要它们。",
                "items": paths_}
    return manager.delete_staging_to_recycle(paths_)


# --------------------------------------------------------------------------- #
# VPK 组件编辑
# --------------------------------------------------------------------------- #
def api_vpk_plan(ctx: Ctx) -> dict:
    b = ctx.body
    key = b.get("key", "")
    drop = b.get("drop") or []
    idx = ctx.mod_index()
    m = idx.get(key)
    if not m or not m._vpk_path:
        return {"ok": False, "error": "找不到可编辑的 VPK"}
    if not drop:
        return {"ok": False, "error": "没有勾选任何要剔除的部件"}
    return {"ok": True, "plan": components.plan_strip(m._vpk_path, drop),
            "mod": {"key": m.key, "title": m.title},
            "warn": "剔除会改变 mod 内容；本工具会在动手前自动备份原件，"
                    "并默认另存新文件，不覆盖原件。"}


def api_vpk_apply(ctx: Ctx) -> dict:
    b = ctx.body
    key = b.get("key", "")
    drop = b.get("drop") or []
    mode = b.get("mode", "new")            # new | replace
    idx = ctx.mod_index()
    m = idx.get(key)
    if not m or not m._vpk_path:
        return {"ok": False, "error": "找不到可编辑的 VPK"}
    if not drop:
        return {"ok": False, "error": "没有勾选任何要剔除的部件"}
    src = m._vpk_path
    if not b.get("confirm"):
        return {"ok": False, "need_confirm": True,
                "plan": components.plan_strip(src, drop),
                "warning": "⚠ 即将改写 VPK：会先备份原件到 %s，"
                           "完成后会重新读取成品做完整性校验（目标必须消失、其余文件一个不少）。"
                           % safety.BACKUP_DIR,
                "mode": mode}
    # 1) 备份原件
    bkp = safety.backup_file(src, tag="vpk-original")
    # 2) 写成品
    #    注意：字节级完整性校验已经内联在 vpk.strip_vpk() 里（写完成品立刻流式
    #    读回复核，不过关就自己把成品删掉并回 ok=False）。这里不再单独调
    #    verify_strip —— 红线放在核心层才绕过不了，调用方只负责看结论。
    if mode == "replace":
        out = src
        tmp = src + ".stripped"
        res = vpk.strip_vpk(src, tmp, set(drop))
        ver = res.get("校验") or {}
        if not res.get("ok"):
            try:
                os.unlink(tmp)              # strip_vpk 已删过；这里兜底
            except OSError:
                pass
            safety.log("vpk-apply", "校验失败，已放弃写入: %s" % ver, level="ERROR", ok=False)
            return {"ok": False, "error": "完整性校验未通过，已放弃改动（原件未变）",
                    "verify": ver, "backup": bkp}
        # replace 会换 inode：先记下指向该实体的 addons 条目，写完重建硬链接
        # （符号链接不用管，它跟路径自动走；硬链接断了自己重建）
        _addons = paths.addons_dir(ctx.get_cfg())
        _links = acquire.link_entries(_addons, src)
        os.replace(tmp, out)
        if _links:
            _fixed = acquire.refresh_links(_addons, out, _links)
            safety.log("vpk-apply", "重建硬链接 %d 条：%s"
                       % (len([x for x in _fixed if x[1]]),
                          ", ".join(n for n, _ok in _fixed)))
    else:
        stem, ext = os.path.splitext(src)
        out = "%s_stripped%s" % (stem, ext)
        i = 1
        while os.path.exists(out):
            out = "%s_stripped(%d)%s" % (stem, i, ext)
            i += 1
        res = vpk.strip_vpk(src, out, set(drop))
        ver = res.get("校验") or {}
        if not res.get("ok"):
            # 另存模式：坏成品已被 strip_vpk 删掉，不留残留文件
            safety.log("vpk-apply", "另存校验失败，未产出文件: %s" % ver, level="ERROR", ok=False)
            return {"ok": False, "error": "完整性校验未通过，未产出新文件（原件未变）",
                    "verify": ver, "backup": bkp}
    fresh = os.path.getsize(out)
    safety.log("vpk-apply", "%s 剔除 %d 项 -> %s（%s，校验 %s）"
               % (m.key, len(drop), out, safety.fmt_size(fresh),
                  "通过" if ver["ok"] else "未通过"))
    # 同步内存记录
    m.size = fresh
    stale = False           # 必须在 try 之前初始化：try 成功路径下原本没有这句，
                            # return 里的 "stale_entries": stale 会直接 NameError，
                            # 每次 strip 成功都回 500（已实测复现）。
    try:
        m._vpaths = vpk.Vpk(out).list_files()
        m.entry_count = len(m._vpaths)
    except Exception as ex:
        # 到这里文件**已经真的改了**（上面 os.replace 成功）。重读成品只更新
        # 内存里的条目缓存，失败不影响磁盘结果 —— 所以不能报错打断流程。
        # 但原先静默 pass 的结果是：界面继续显示**剔除前的条目数与文件列表**，
        # 用户看着旧数字以为没生效。留一条日志，并在返回值里如实标注。
        safety.log("vpk-apply", "成品重读失败，界面上的条目数可能仍是剔除前的旧值: %s: %s"
                   % (type(ex).__name__, ex), level="WARN", ok=False)
        stale = True
    ctx.state["last_scan"]["mods_json"] = [x.to_json() for x in ctx.state["mods"]]
    ctx.state["mods_for_audit"] = scan.dicts_for_audit(ctx.state["mods"])
    return {"ok": True, "stripped": res, "verify": ver, "out": out,
            "out_size_h": safety.fmt_size(fresh), "backup": bkp,
            "stale_entries": stale,
            "mode": mode, "verify_passed": ver["ok"]}


def api_vpk_extract(ctx: Ctx) -> dict:
    b = ctx.body
    key = b.get("key", "")
    idx = ctx.mod_index()
    m = idx.get(key)
    if not m or not m._vpk_path:
        return {"ok": False, "error": "找不到可操作的 VPK"}
    out_dir = b.get("out_dir") or os.path.join(
        safety.DATA_DIR, "extracted", os.path.splitext(m.key)[0])
    if not b.get("confirm"):
        return {"ok": False, "need_confirm": True,
                "preview": {"count": m.entry_count,
                            "size_h": safety.fmt_size(m.size),
                            "out_dir": out_dir}}
    v = vpk.Vpk(m._vpk_path)
    n = v.extract_all(out_dir)
    safety.log("vpk-extract", "%s -> %s（%d 个文件）" % (m.key, out_dir, n))
    return {"ok": True, "count": n, "out_dir": out_dir}


# --------------------------------------------------------------------------- #
# 游戏文件
# --------------------------------------------------------------------------- #
def api_gamefiles(ctx: Ctx) -> dict:
    return gamefiles.inventory(ctx.get_cfg())


def api_gf_read(ctx: Ctx) -> dict:
    return gamefiles.read_current(ctx.get_cfg(), ctx.q("rel"))


def api_gf_backups(ctx: Ctx) -> dict:
    return {"backups": gamefiles.backup_list(ctx.get_cfg()),
            "dir": gamefiles.safety.BACKUP_DIR}


def api_gf_replace(ctx: Ctx) -> dict:
    b = ctx.body
    cfg = ctx.get_cfg()
    rel = b.get("rel", "")
    src = b.get("src_path", "")
    data_b64 = b.get("data_b64")
    if not rel:
        return {"ok": False, "error": "缺少 rel"}
    tmp = ""
    if data_b64:
        # 小文件走上传（大文件请用「选择文件」直接给路径，避免几十 MB 的 base64 搬运）
        # ⚠️ 这条路径就是 http_util.MAX_BODY 取 160 MB 的原因：
        #    这里放行 96 MB 原始文件，base64 之后约 128 MB，上限必须留足。
        raw = base64.b64decode(data_b64)
        if len(raw) > 96 * 1024 * 1024:
            return {"ok": False, "error": "上传文件超过 96 MB，请改用「选择文件」按钮指定本机路径"}
        tmp = os.path.join(safety.DATA_DIR, "cache", "upload__" + os.path.basename(rel))
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        with open(tmp, "wb") as fp:
            fp.write(raw)
        src = tmp
    if not src or not os.path.isfile(src):
        return {"ok": False, "error": "源文件无效：%s" % src}
    res = gamefiles.replace(cfg, rel, src)
    if tmp:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return res


def api_gf_restore(ctx: Ctx) -> dict:
    b = ctx.body
    return gamefiles.restore(ctx.get_cfg(), b.get("rel", ""), b.get("which", "baseline"))


def api_gf_restore_all(ctx: Ctx) -> dict:
    return gamefiles.restore_all(ctx.get_cfg())


def api_gf_write(ctx: Ctx) -> dict:
    b = ctx.body
    return gamefiles.write_text(ctx.get_cfg(), b.get("rel", ""), b.get("text", ""))


# --------------------------------------------------------------------------- #
# AI 冲突复核
# --------------------------------------------------------------------------- #
def api_ai_config_get(ctx: Ctx) -> dict:
    return {"config": ai.public_cfg(ai.load()),
            "presets": ai.PRESETS,
            "risks": [{"k": k, "zh": ai.RISK_ZH[k]}
                      for k in ("high", "medium", "low", "none")]}


def api_ai_config(ctx: Ctx) -> dict:
    """保存 AI 配置。

    ⚠️ 密钥单独走：明文从界面进来，**DPAPI 加密后**才落盘，
    `ai.json` 里永远只有密文；界面只看得到「设没设」，看不到值。
    """
    b = ctx.body
    patch: dict = {}
    for k in ("enabled", "preset", "base_url", "model", "timeout",
              "temperature", "batch_cases", "max_cases", "synthesize"):
        if k in b:
            patch[k] = b[k]
    if "base_url" in patch:
        patch["base_url"] = ai.norm_base(str(patch["base_url"] or ""))
        # 回传保护：界面拿到的 base_url 是 public_cfg() 脱敏过的（query 被剥掉，
        # 免得密钥显示在屏幕上）。用户只是「打开设置 → 直接保存」而没动地址时，
        # 前端会把那个脱敏值原样回传 —— 照单全收就会把磁盘上带 `?key=...` 的
        # 真实地址静默改掉（密钥随 query 一起消失，用户下次复核才发现连不上）。
        # 这里用「新值 == 脱下敏后的旧值」这个精确签名识别出「没改过」：
        # 字符串相等才还原，用户真改了地址时两者不等，照常采纳新值。
        _stored = ai.load()
        _stored_base = str(_stored.get("base_url") or "")
        _shown = str(ai.public_cfg(_stored).get("base_url") or "")
        if patch["base_url"] and patch["base_url"] == _shown != _stored_base:
            patch["base_url"] = _stored_base
    for k, lo, hi in (("timeout", 10, 600), ("batch_cases", 1, 20),
                      ("max_cases", 1, 60)):
        if k in patch:
            try:
                patch[k] = max(lo, min(hi, int(patch[k])))
            except Exception:
                patch.pop(k, None)
    if "temperature" in patch:
        try:
            patch["temperature"] = max(0.0, min(2.0, float(patch["temperature"])))
        except Exception:
            patch.pop("temperature", None)

    if b.get("clear_key"):
        patch["api_key_enc"] = ""
    elif b.get("api_key"):
        patch["api_key_enc"] = ai.encrypt_key(str(b["api_key"]).strip())

    ai.save(patch)
    return {"ok": True, "config": ai.public_cfg(ai.load())}


def api_ai_test(ctx: Ctx) -> dict:
    """连通性自检。允许用「界面上还没保存的值」直接测，省得测一次存一次。"""
    b = ctx.body
    merged = ai.load()
    for k in ("base_url", "model", "timeout", "temperature"):
        if b.get(k) not in (None, ""):
            merged[k] = b[k]
    merged["base_url"] = ai.norm_base(str(merged.get("base_url") or ""))
    if b.get("api_key"):
        merged["api_key_enc"] = ai.encrypt_key(str(b["api_key"]).strip())
    return ai.test(merged)


def api_ai_analyze(ctx: Ctx) -> dict:
    """把冲突交给模型复核。真正的调用在后台线程，这里立刻返回 job id。"""
    b = ctx.body
    cfg = ai.load()
    if not cfg.get("enabled"):
        return {"ok": False,
                "error": "AI 复核还没启用 —— 先到「设置 → AI 冲突复核」里打开并测通连接"}
    base = ai.norm_base(cfg.get("base_url") or "")
    if not base:
        return {"ok": False, "error": "没有配置 AI 接口地址"}

    # 远程端点：mod 名称与内部资源路径会离开本机，必须先明确确认一次
    if not ai.is_local(base) and not b.get("ack_remote"):
        return {"ok": False, "need_remote_ack": True, "host": ai._host_of(base),
                "error": ("AI 接口是远程服务（%s），复核时会把 mod 标题、分类标签和 "
                          "VPK 内部资源路径发送过去（不含文件内容、不含你的磁盘路径）。"
                          "确认后才开始。") % ai._host_of(base)}

    g = ctx.get_cfg()
    if ctx.state.get("mods_for_audit") is None:
        ctx.do_scan()
    mods = ctx.state.get("mods_for_audit") or []
    if not mods:
        return {"ok": False, "error": "还没有扫描结果，先点「重新扫描」"}

    conf = audit.conflicts(mods, only_enabled=True)
    if not conf.get("dir_groups"):
        return {"ok": False, "error": "当前没有冲突可复核"}

    mods_by_key = {}
    for m in (ctx.state.get("mods") or []):
        mods_by_key[m.key] = {"title": m.title, "category": m.category,
                              "auto_category": m.auto_category, "tags": m.tags,
                              "content_tags": m.content_tags, "enabled": m.enabled}
    order_map = {}
    try:
        listed = scan.read_addonlist(paths.addonlist_path(g))
        order_map = {k: i + 1 for i, k in enumerate(listed.keys())}
    except Exception as ex:
        # 加载顺序缺失会让 AI 的复核结论失去一个重要维度（「哪个 mod 生效」
        # 直接取决于顺序）。原来这里是静默 pass，用户只会看到结论里没有顺序信息，
        # 不知道是「真没顺序」还是「读失败了」。
        safety.log("ai-analyze", "读取 addonlist 得到加载顺序失败：%s: %s"
                   % (type(ex).__name__, ex), level="WARN", ok=False)

    try:
        limit = int(b.get("limit") or cfg.get("max_cases") or 12)
    except Exception:
        limit = 12
    limit = max(1, min(60, limit))

    # 指定单个目录时，要先把案例池放大再筛 ——
    # 否则目标目录排在 limit 之外就直接筛空了。
    pool = 200 if b.get("only_dir") else limit
    cases = ai.build_cases(conf, mods_by_key, order_map, limit=pool)
    if b.get("only_dir"):
        cases = [c for c in cases if c.get("dir") == b["only_dir"]]
        if not cases:
            return {"ok": False, "error": "这个目录不在当前冲突列表里"}

    res = ai.start_analysis(cfg, cases, synthesize=cfg.get("synthesize", True))
    if res.get("ok"):
        # 端点走 _safe_url 剥 query：base_url 是用户自由输入的，有人会把密钥
        # 塞进 query（`https://gw.example.com/v1?key=sk-xxx`）。这行会写进
        # data/audit.log，而 /api/log 又把审计尾部整段回前端 —— 不剥就等于
        # 把密钥落到磁盘上还显示在界面上。（is_local 仍用完整 base 判定，
        # 主机名不受 query 影响。）
        safety.log("ai-analyze", "复核 %d 组冲突 / %d 批 / 端点 %s（%s）"
                   % (res["cases"], res["batches"], ai._safe_url(base),
                      "本机" if ai.is_local(base) else "远程"))
    return res


def api_ai_job(ctx: Ctx) -> dict:
    return ai.job_state(ctx.q("id"))


def api_ai_cancel(ctx: Ctx) -> dict:
    return ai.cancel_job(ctx.body.get("id", ""))


# --------------------------------------------------------------------------- #
# 日志 / 备份 / 系统交互
# --------------------------------------------------------------------------- #
def api_backups(ctx: Ctx) -> dict:
    return {"items": safety.list_backups()[:300],
            "dir": safety.BACKUP_DIR}


def api_log(ctx: Ctx) -> dict:
    return {"lines": safety.recent_log(300),
            "path": safety.AUDIT_LOG}


def api_pick_file(ctx: Ctx) -> dict:
    """用系统原生文件对话框选文件，避免把大文件搬来搬去。"""
    ext = ctx.q("ext", "bik")
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as ex:
        return {"ok": False, "error": "系统对话框不可用（%s），请手动填写路径" % ex}
    result = {}

    def run():
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            f = filedialog.askopenfilename(
                title="选择要使用的文件",
                filetypes=[("匹配的素材", "*.%s" % ext), ("所有文件", "*.*")])
            root.destroy()
            result["path"] = f or ""
        except Exception as ex:
            result["error"] = str(ex)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=180)
    if "error" in result:
        return {"ok": False, "error": result["error"]}
    if not result.get("path"):
        return {"ok": False, "error": "未选择文件（或对话框超时）"}
    return {"ok": True, "path": result["path"]}


def api_open_folder(ctx: Ctx) -> dict:
    p = ctx.body.get("path", "")
    if not os.path.exists(p):
        return {"ok": False, "error": "路径不存在: %s" % p}
    try:
        os.startfile(p)          # noqa: S606  —— 本地工具，打开资源管理器
        return {"ok": True}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


# --------------------------------------------------------------------------- #
# 改生还者模型归属
# --------------------------------------------------------------------------- #
def api_reassign_detect(ctx: Ctx) -> dict:
    """这个 mod 现在挂在哪个生还者槽位上？内部又指向谁？

    后者是重点：外部文件名与内部 `$includemodel` 引用不一致，
    说明它的归属被改过（改了名字、没改内脏）—— 这种情况要在界面上明确标出来。
    """
    key = ctx.q("key")
    if not key:
        return {"ok": False, "error": "缺少 key"}
    idx = ctx.mod_index()
    m = idx.get(key)
    if not m:
        return {"ok": False, "error": "找不到 mod: %s" % key}
    if not m._vpk_path or not os.path.isfile(m._vpk_path):
        return {"ok": False, "error": "这个 mod 没有可操作的 VPK"}
    try:
        d = reassign.detect(m._vpk_path)
    except Exception as ex:
        return {"ok": False, "error": "解析失败：%s: %s" % (type(ex).__name__, ex)}
    d["mod"] = {"key": m.key, "title": m.title}
    d["targets"] = [{"id": s["id"], "zh": s["zh"], "game": s["game"]}
                    for s in reassign.SURVIVORS]
    return d


def api_reassign_plan(ctx: Ctx) -> dict:
    """只读预演：算出要改哪些名、丢弃哪些、内部引用能不能跟着改。"""
    b = ctx.body
    key = b.get("key", "")
    target = b.get("target", "")
    idx = ctx.mod_index()
    m = idx.get(key)
    if not m or not m._vpk_path:
        return {"ok": False, "error": "找不到可操作的 VPK"}
    if not target:
        return {"ok": False, "error": "没有选择目标角色"}
    try:
        p = reassign.plan(m._vpk_path, target, b.get("fix_internal", True))
    except Exception as ex:
        return {"ok": False, "error": "预演失败：%s: %s" % (type(ex).__name__, ex)}
    if not p.get("ok"):
        return p
    p["mod"] = {"key": m.key, "title": m.title}
    p["warning"] = ("改归属会另存为一个新的 VPK。新文件**不会自动生效** —— "
                    "addons 里的是指向原文件的符号链接、addonlist 登记的也是原链接名，"
                    "想立刻在游戏里看到效果请用「原地替换」（改的是实体文件，链接自动跟着变）。")
    return p


def api_reassign_apply(ctx: Ctx) -> dict:
    """执行改写。两步确认：第一次返回预演，第二次带 confirm=True 才真写。

    与组件精修保持同一套规矩：动手前自动备份原件、默认另存而不覆盖。
    """
    b = ctx.body
    key = b.get("key", "")
    target = b.get("target", "")
    mode = b.get("mode", "new")            # new | replace
    fix_internal = b.get("fix_internal", True)
    idx = ctx.mod_index()
    m = idx.get(key)
    if not m or not m._vpk_path:
        return {"ok": False, "error": "找不到可操作的 VPK"}
    if not target:
        return {"ok": False, "error": "没有选择目标角色"}

    src = m._vpk_path
    try:
        pv = reassign.plan(src, target, fix_internal)
    except Exception as ex:
        return {"ok": False, "error": "预演失败：%s: %s" % (type(ex).__name__, ex)}
    if not pv.get("ok"):
        return pv

    if not b.get("confirm"):
        # ⚠️ 这里**不能**回 "plan" 字段：前端 openConfirm 的 plan 分支是按 strip_vpk 的
        #    字段渲染的（total / src_size_h / will_drop / drop_size_h / will_keep），
        #    改归属没有 will_drop 这些，会渲染出 NaN。改归属的语义是「改名」不是「剔除」，
        #    所以走 count + items 那条分支，把改名清单逐条列出来。
        return {"ok": False, "need_confirm": True, "detail": pv, "mode": mode,
                "count": pv.get("rename_count", 0),
                "items": ["%s  →  %s" % (r["from"], r["to"])
                          for r in pv.get("renames", [])][:24],
                "warning": "⚠ 即将改写 VPK：会先备份原件到 %s，完成后重读成品校验（改名后的条目"
                           "必须都在、旧名字必须都消失）。当前槽位 %s → 目标 %s。"
                           % (safety.BACKUP_DIR, pv.get("current_zh"), pv.get("to", {}).get("zh"))}

    # 1) 备份原件 —— 与 strip 同一条红线，先备份再动刀
    bkp = safety.backup_file(src, tag="vpk-before-reassign")

    stem, ext = os.path.splitext(src)
    if mode == "replace":
        out = src
        tmp = src + ".reassigned"
        try:
            res = reassign.apply(src, tmp, target, fix_internal)
            if not res.get("ok"):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                safety.log("reassign-apply", "校验失败，已放弃写入: %s"
                           % (res.get("校验") or {}), level="ERROR", ok=False)
                return {"ok": False, "error": "完整性校验未通过，已放弃改动（原件未变）",
                        "verify": res.get("校验"), "backup": bkp}
            _addons2 = paths.addons_dir(ctx.get_cfg())
            _links2 = acquire.link_entries(_addons2, src)
            os.replace(tmp, out)
            if _links2:
                _fixed2 = acquire.refresh_links(_addons2, out, _links2)
                safety.log("reassign-apply", "重建硬链接 %d 条"
                           % len([x for x in _fixed2 if x[1]]))
        except Exception as ex:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            safety.log("reassign-apply", "执行失败: %s: %s" % (type(ex).__name__, ex),
                       level="ERROR", ok=False)
            return {"ok": False, "error": "改写失败：%s（原件未变）" % ex, "backup": bkp}
    else:
        # 名字里带上目标角色，一眼能看出这份是改给谁的
        out = "%s__%s%s" % (stem, target, ext)
        i = 1
        while os.path.exists(out):
            out = "%s__%s(%d)%s" % (stem, target, i, ext)
            i += 1
        try:
            res = reassign.apply(src, out, target, fix_internal)
        except Exception as ex:
            safety.log("reassign-apply", "执行失败: %s: %s" % (type(ex).__name__, ex),
                       level="ERROR", ok=False)
            return {"ok": False, "error": "改写失败：%s（原件未变）" % ex, "backup": bkp}
        if not res.get("ok"):
            safety.log("reassign-apply", "另存校验失败，未产出文件: %s"
                       % (res.get("校验") or {}), level="ERROR", ok=False)
            return {"ok": False, "error": "完整性校验未通过，未产出新文件（原件未变）",
                    "verify": res.get("校验"), "backup": bkp}

    fresh = os.path.getsize(out)
    ver = res.get("校验") or {}
    safety.log("reassign-apply", "%s 改归属 %s → %s（%s，改名 %d / 丢弃 %d，校验 %s）"
               % (m.key, pv.get("current"), target, safety.fmt_size(fresh),
                  res.get("改名", 0), res.get("丢弃", 0), "通过" if ver.get("ok") else "未通过"))

    # 同步内存记录。这里文件**已经真的改了**，重读失败不许打断流程，
    # 但要留日志并在返回值里如实标注，免得界面继续显示旧数字。
    stale = False
    if mode == "replace":
        m.size = fresh
        try:
            m._vpaths = vpk.Vpk(out).list_files()
            m.entry_count = len(m._vpaths)
        except Exception as ex:
            safety.log("reassign-apply", "成品重读失败，条目数可能仍是旧值: %s: %s"
                       % (type(ex).__name__, ex), level="WARN", ok=False)
            stale = True
        ctx.state["last_scan"]["mods_json"] = [x.to_json() for x in ctx.state["mods"]]
        ctx.state["mods_for_audit"] = scan.dicts_for_audit(ctx.state["mods"])

    return {"ok": True, "result": res, "verify": ver, "out": out,
            "out_size_h": safety.fmt_size(fresh), "backup": bkp,
            "stale_entries": stale, "mode": mode,
            "verify_passed": bool(ver.get("ok"))}


# --------------------------------------------------------------------------- #
# 获取 mod：一键入库 + 创意工坊订阅下载（core/acquire.py）
# --------------------------------------------------------------------------- #
def api_acquire_steamcmd(ctx: Ctx) -> dict:
    """SteamCMD 探测结果（没装时给出指引，前端展示一键安装）。"""
    p = acquire.detect_steamcmd()
    return {"ok": True, "found": bool(p), "path": p,
            "hint": ("SteamCMD 是 Valve 官方的命令行工具，创意工坊下载靠它。"
                     "点「一键安装」从 Valve 官网自动下载安装（约 3 MB，"
                     "首次运行自动更新到约 100 MB）。") if not p else ""}


def api_acquire_install(ctx: Ctx) -> dict:
    """一键安装 SteamCMD（从 Valve 官方 CDN 代下载）。ack=False 先要知情确认。"""
    return acquire.install_steamcmd(ack=bool(ctx.body.get("ack")))


def api_mod_move_category(ctx: Ctx) -> dict:
    """把 mod 的仓库分类目录移到另一个分类（两步确认）。"""
    b = ctx.body
    key = b.get("key", "")
    cat = b.get("category", "")
    idx = ctx.mod_index()
    m = idx.get(key)
    if not m:
        return {"ok": False, "error": "找不到这个 mod"}
    if not m.real_path:
        return {"ok": False, "error": "这个 mod 没有实体文件路径"}
    if not cat:
        return {"ok": False, "error": "没指定目标分类"}
    r = acquire.move_category(ctx.get_cfg(), m.real_path, cat,
                              confirm=bool(b.get("confirm")))
    if r.get("ok"):
        try:
            ctx.do_scan(force=True)      # 立刻反映到界面
        except Exception:
            pass
    return r


def api_acquire_linkprobe(ctx: Ctx) -> dict:
    """链接能力自检：当前进程能否建符号链接 / 硬链接（排障用）。"""
    return acquire.probe_link_support()


def api_acquire_env(ctx: Ctx) -> dict:
    """网络环境探测：SteamCMD 通道可不可用（hosts 劫持 / PAC 状态）。"""
    return acquire.detect_network_env()


def api_acquire_browser(ctx: Ctx) -> dict:
    """浏览器代下载：打开创意工坊页面（浏览器读 PAC，绕开 hosts 劫持）。"""
    item = acquire.parse_workshop_input(ctx.body.get("input", ""))
    if not item:
        return {"ok": False, "error": "看不懂这个输入——请贴创意工坊链接或纯数字 item id"}
    return acquire.browser_download(item)


def api_acquire_plan(ctx: Ctx) -> dict:
    """入库预演（只读）：判定分类、算出落点、检查冲突。"""
    src = ctx.body.get("src_path", "").strip()
    if not src:
        return {"ok": False, "error": "缺少 src_path（要入库的 VPK 文件路径）"}
    p = acquire.plan_ingest(ctx.get_cfg(), src)
    return p


def api_acquire_ingest(ctx: Ctx) -> dict:
    """入库执行。两步确认：第一次返回预演，第二次 confirm=True 才真写。"""
    b = ctx.body
    src = b.get("src_path", "").strip()
    mode = b.get("mode", "move")            # move（源文件进回收站）| copy（保留）
    if not src:
        return {"ok": False, "error": "缺少 src_path"}
    if not b.get("confirm"):
        p = acquire.plan_ingest(ctx.get_cfg(), src)
        if not p.get("ok"):
            return p
        return {"ok": False, "need_confirm": True, "plan": p, "mode": mode,
                "warning": "⚠ 将把该 VPK 入库：建 .workshop 目录 → 写元数据 → "
                           "addons 建链接 → addonlist.txt 登记。mode=%s。" % mode,
                "count": 1,
                "items": ["%s  →  %s" % (os.path.basename(src), p.get("dst_name", ""))]}
    # 执行前重新校验一次（预演之后文件可能被移走）
    if not os.path.isfile(src):
        return {"ok": False, "error": "文件不见了（可能已被移动/删除）: %s" % src}
    r = acquire.ingest(ctx.get_cfg(), src, mode)
    if r.get("ok"):
        # 同步内存扫描状态，让界面立刻看到新 mod
        try:
            ctx.do_scan(force=True)
        except Exception:
            pass                                  # 重扫失败不影响入库结果
    return r


def api_acquire_workshop(ctx: Ctx) -> dict:
    """创意工坊订阅：启动后台下载任务，立刻返回 job id。"""
    b = ctx.body
    item = acquire.parse_workshop_input(b.get("input", ""))
    if not item:
        return {"ok": False, "error": "看不懂这个输入——请贴创意工坊链接或纯数字 item id"}
    return acquire.workshop_download(ctx.get_cfg(), item,
                                     b.get("steamcmd", ""))


def api_acquire_job(ctx: Ctx) -> dict:
    return acquire.job_state(ctx.q("id"))


def api_acquire_cancel(ctx: Ctx) -> dict:
    return acquire.cancel_job(ctx.body.get("id", ""))
