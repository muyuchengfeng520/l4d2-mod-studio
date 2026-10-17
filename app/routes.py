# -*- coding: utf-8 -*-
"""路由表：把「路径模式 → 处理函数」显式登记，取代原来的两条 if 链。

为什么要换成表
--------------
原来 `do_GET` / `do_POST` 各是一条几十个 `if p == "..."` 的长链，
加一个接口要在三个地方改（if 链、api_* 方法、前端），漏一处就静默 404。
现在接口清单是一份**数据**：`GET_ROUTES` / `POST_ROUTES`，
`server.Handler` 只负责「解析请求 → 查表 → 调函数 → 发响应」。

模式语法
--------
* 字面量：`/api/status`
* 路径参数：`/api/mod/{key}` —— 匹配到的段进 `ctx.params["key"]`
* 尾随通配：`/static/{rel:path}` —— 吃下剩余全部路径（含 `/`），进 `ctx.params["rel"]`

⚠️ 兼容性铁律：现有接口**一律保持 query 传参**（`/api/mod?key=x`），
   不为了「RESTful 好看」改成 `/api/mod/x` —— 前端 web/app.js 里写死了
   这些 URL 与参数名，改了就是全站 404。路径参数只是给以后新接口留的能力。
"""
from __future__ import annotations

import re

# server.py 既可能以包的形式被导入（app.server），也可能被当成顶层模块导入
# （desktop.py 里 sys.path 塞了 app\ 之后 `import server`；START.bat 直接
#  `python app\server.py`）。两种情况下兄弟模块的导入写法不一样，
# 所以这里按 __package__ 分流 —— 别改成裸的相对导入，直接跑会 ImportError。
if __package__:
    from . import api, http_util
else:
    import api
    import http_util


class Route:
    """一条路由。handler 收 ctx、返回 dict（会自动 _json）或 http_util.DONE。"""

    __slots__ = ("pattern", "handler", "regex", "names")

    def __init__(self, pattern: str, handler):
        self.pattern = pattern
        self.handler = handler
        self.regex, self.names = _compile(pattern)

    def match(self, path: str):
        """命中返回参数字典，否则返回 None。"""
        m = self.regex.match(path)
        if not m:
            return None
        return m.groupdict()

    def __repr__(self) -> str:
        return "<Route %s>" % self.pattern


def _compile(pattern: str):
    """把 `/a/{b}/c/{d:path}` 编译成正则 + 参数名列表。"""
    names = []
    out = []
    for seg in pattern.split("/"):
        if not seg:
            continue
        if seg.startswith("{") and seg.endswith("}"):
            body = seg[1:-1]
            name, _, kind = body.partition(":")
            names.append(name)
            if kind == "path":
                # 尾随通配：吃下剩余全部（含斜杠）。用 .* 而不是 .+ ——
                # 原实现里 `/static/` 会走到 _static("") 然后报「静态资源不存在」，
                # 用 .+ 的话这条路径直接掉进「未知接口」，提示语就变了。
                out.append(r"(?P<%s>.*)" % name)
            else:
                # 单段：不含斜杠
                out.append(r"(?P<%s>[^/]+)" % name)
        else:
            out.append(re.escape(seg))
    # 字面量根路径（"/"）split 之后是空的 —— 单独兜一下
    rx = "/" + "/".join(out) if out else "/"
    return re.compile("^" + rx + "$"), names


# --------------------------------------------------------------------------- #
# 处理函数：静态资源（路由表里唯一一个「自己写响应」的）
# --------------------------------------------------------------------------- #
def _index(ctx):
    """`/` 与 `/index.html` —— 原代码这两条都走 _static("index.html")。"""
    return http_util.serve_static(ctx.h, "index.html", ctx.web_dir)


def _static(ctx):
    """`/static/*`。

    原来的 `_static(rel)` 只做 `rel.lstrip("/\\\\").replace("..", "")` ——
    见 http_util.serve_static 里的踩坑注释，那里才是真正的防护。
    """
    return http_util.serve_static(ctx.h, ctx.params.get("rel", ""), ctx.web_dir)


# --------------------------------------------------------------------------- #
# 路由表
# --------------------------------------------------------------------------- #
# ⚠️ 顺序有意义：先登记的先用。带通配的 `/static/{rel:path}` 不会和
#    `/api/...` 打架（前缀不同），但新加接口时仍建议把具体路径写在前面。

GET_ROUTES = [
    Route("/", _index),
    Route("/index.html", _index),
    Route("/static/{rel:path}", _static),

    Route("/api/status", api.api_status),
    Route("/api/bootstrap", api.api_bootstrap),
    Route("/api/candidates", api.api_candidates),
    Route("/api/scan", api.api_scan),
    Route("/api/rescan", api.api_rescan),
    Route("/api/mod", api.api_mod),
    Route("/api/audit", api.api_audit),
    Route("/api/gamefiles", api.api_gamefiles),
    Route("/api/gamefiles/read", api.api_gf_read),
    Route("/api/gamefiles/backups", api.api_gf_backups),
    Route("/api/staging", api.api_staging),
    Route("/api/backups", api.api_backups),
    Route("/api/log", api.api_log),
    Route("/api/ai/config", api.api_ai_config_get),
    Route("/api/ai/job", api.api_ai_job),
    Route("/api/pick-file", api.api_pick_file),
    Route("/api/reassign/detect", api.api_reassign_detect),
    Route("/api/acquire/steamcmd", api.api_acquire_steamcmd),
    Route("/api/acquire/env", api.api_acquire_env),
    Route("/api/acquire/job", api.api_acquire_job),
]

POST_ROUTES = [
    Route("/api/config", api.api_set_config),
    Route("/api/scan", api.api_scan),
    Route("/api/enable", api.api_enable),
    Route("/api/isolate", api.api_isolate),
    Route("/api/staging/restore", api.api_staging_restore),
    Route("/api/staging/recycle", api.api_staging_recycle),
    Route("/api/vpk/plan", api.api_vpk_plan),
    Route("/api/vpk/apply", api.api_vpk_apply),
    Route("/api/vpk/extract", api.api_vpk_extract),
    Route("/api/gamefiles/replace", api.api_gf_replace),
    Route("/api/gamefiles/restore", api.api_gf_restore),
    Route("/api/gamefiles/restore-all", api.api_gf_restore_all),
    Route("/api/gamefiles/write", api.api_gf_write),
    Route("/api/ai/config", api.api_ai_config),
    Route("/api/ai/test", api.api_ai_test),
    Route("/api/ai/analyze", api.api_ai_analyze),
    Route("/api/ai/cancel", api.api_ai_cancel),
    Route("/api/open-folder", api.api_open_folder),
    Route("/api/reassign/plan", api.api_reassign_plan),
    Route("/api/reassign/apply", api.api_reassign_apply),
    Route("/api/acquire/plan", api.api_acquire_plan),
    Route("/api/acquire/ingest", api.api_acquire_ingest),
    Route("/api/acquire/workshop", api.api_acquire_workshop),
    # install / browser 都带 JSON body（ack / input），必须登记在 POST 表——
    # 之前误放到 GET 表，前端点「浏览器下载」直接 404（实测踩过）。
    Route("/api/acquire/install", api.api_acquire_install),
    Route("/api/acquire/browser", api.api_acquire_browser),
    Route("/api/acquire/cancel", api.api_acquire_cancel),
]


def lookup(table, path: str):
    """在表里找 path。返回 (handler, params) 或 (None, None)。"""
    for r in table:
        params = r.match(path)
        if params is not None:
            return r.handler, params
    return None, None
