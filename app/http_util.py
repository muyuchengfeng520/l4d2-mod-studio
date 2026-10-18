# -*- coding: utf-8 -*-
"""HTTP 收发工具：响应发送、请求体读取、静态文件服务。

这一层只跟「一个 BaseHTTPRequestHandler 实例」打交道 —— 不知道路由，也不碰
任何业务状态。所以这里全是**纯函数**，handler 当第一个参数传进来。

从 server.py 里搬出来的东西：
    _send / _json / _err / _body   —— 原 Handler 的四个基础方法
    静态文件服务                    —— 原 Handler._static

搬运时顺手修掉的两个真实缺陷（原实现见 git 历史）：
    1. _static() 的路径逃逸：原来是 rel.lstrip("/\\").replace("..", "")，
       变形路径（..%2f、....//）拦不住；现在改成 URL 解码 + 规范化 + 前缀校验。
    2. _body() 没有长度上限：Content-Length 多大就 read 多大，一个超大请求
       就能把内存打爆；现在超过 MAX_BODY 直接 413 并且**不读**请求体。
"""
from __future__ import annotations

import json
import os
import urllib.parse

from core import safety

# 请求体上限。⚠️ 不能低于 140 MB：api_gf_replace 允许上传 96 MB 的原始文件，
# base64 之后约 128 MB（4/3 倍），留一点余量取 160 MB。
# 超过就直接回 413 并丢弃请求体 —— 绝不 read 进来再判断，那样内存已经炸了。
MAX_BODY = 160 * 1024 * 1024

# 静态资源的扩展名 → Content-Type。认不出来的按二进制流发。
MIME = {
    "html": "text/html; charset=utf-8",
    "js": "application/javascript; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "svg": "image/svg+xml",
    "json": "application/json; charset=utf-8",
}

# 特殊返回值：处理函数**自己**已经把响应写出去了，分发器不要再发一遍。
# （静态文件、413 这两种情况就是。）
DONE = object()


class BodyTooLarge(Exception):
    """请求体超过 MAX_BODY —— 413 已经发回给客户端了，调用方直接收工。"""


# --------------------------------------------------------------------------- #
# 发送
# --------------------------------------------------------------------------- #
def send(h, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
    h.send_response(code)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    try:
        h.wfile.write(body)
    except (BrokenPipeError, ConnectionAbortedError):
        pass


def send_json(h, obj, code: int = 200) -> None:
    send(h, code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))


def send_err(h, msg: str, code: int = 400, detail: str = "") -> None:
    # ⚠️ detail 可能是 None（调用方显式传了 None）—— 切片前必须兜底，
    #    否则这里先抛 TypeError，把真正的错误信息盖掉，排障时看到的是假异常。
    safety.log("api-error", "%s | %s" % (msg, (detail or "")[:400]), level="ERROR", ok=False)
    send_json(h, {"ok": False, "error": msg, "detail": detail}, code)


# --------------------------------------------------------------------------- #
# 本地来源校验
# --------------------------------------------------------------------------- #
# ⚠️ 为什么需要这一层（真实缺陷，别删）：
#   服务监听 127.0.0.1 **不等于**只有本机能调它 —— 用户在浏览器里打开的
#   任何一个网页，都能 `fetch("http://127.0.0.1:17246/api/...")` 往这里发请求。
#   只要该请求算「简单请求」（Content-Type 是 text/plain / form-urlencoded /
#   multipart），浏览器就不发预检，请求真实抵达、**副作用真实发生**；攻击页面
#   读不到响应，但文件已经没了。可被静默触发的破坏性接口：
#     /api/gamefiles/restore（把游戏原文件送进回收站）
#     /api/staging/recycle  （confirm:true 直接删隔离区条目）
#     /api/isolate          （把 mod 搬出 addons）
#     /api/enable           （改写玩家的 addonlist.txt）
#     /api/config           （改掉 game_dir）
#
#   三道闸门：
#     ① Content-Type 必须是 application/json。这一条**就够了**：application/json
#        不在 CORS 安全名单里，跨站发它必然先发预检，而本服务不实现 do_OPTIONS
#        （BaseHTTPRequestHandler 默认回 501）⇒ 预检失败 ⇒ 真实请求根本发不出去。
#     ② Origin 存在时必须是本机页面（防跨站）；不存在则放行 —— curl 等非浏览器
#        客户端不带 Origin，部分浏览器的同源请求也可能不带。
#     ③ Host 必须指向本机回环。防 **DNS rebinding**：攻击者用自己的域名解析到
#        127.0.0.1，浏览器会认为「同源」，①② 就全都失效了。
#   三道都不动业务逻辑，任何一道单独被绕过，剩下的仍然够用。
_TRUSTED_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}

# 校验失败时统一回这个错误码
LOCAL_GUARD_CODE = 403


def _host_port(value: str) -> tuple:
    """拆 `host:port`，兼容 IPv6 字面量 `[::1]:17246`。返回 (host, port_str)。"""
    v = (value or "").strip()
    if v.startswith("["):
        i = v.find("]")
        if i < 0:
            return v.lower(), ""
        rest = v[i + 1:]
        return v[1:i].lower(), rest[1:] if rest.startswith(":") else ""
    if ":" in v:
        host, _, p = v.rpartition(":")
        return host.lower(), p
    return v.lower(), ""


def check_request(h, port: int) -> str:
    """校验请求来源。返回 "" ＝放行，否则返回给用户看的拒绝原因。"""
    raw_host = h.headers.get("Host") or ""
    host, hport = _host_port(raw_host)
    if host not in _TRUSTED_HOSTNAMES:
        return ("Host 只接受本机回环地址，收到 %r。本工具不对外提供服务。"
                % raw_host)
    if hport and hport != str(port):
        return "Host 里的端口与当前服务端口不一致：%s ≠ %d" % (hport, port)
    # 现代浏览器会带这个头：明确标了跨站就直接拒，比 Origin 更早一步
    if (h.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
        return "跨站请求被拒绝（Sec-Fetch-Site: cross-site）"
    origin = (h.headers.get("Origin") or "").strip()
    if origin:
        if origin.lower() == "null":
            return ("拒绝来自本地文件/沙箱页面的请求（Origin: null）。"
                    "请用 http://127.0.0.1:%d/ 打开本工具。" % port)
        u = urllib.parse.urlparse(origin)
        if u.scheme not in ("http", "https"):
            return "Origin 协议不被接受：%s" % origin
        if (u.hostname or "").lower() not in _TRUSTED_HOSTNAMES:
            return ("Origin 不是本机页面：%s。只有从 http://127.0.0.1:%d/ 打开的"
                    "界面能操作本工具。" % (origin, port))
        if u.port and u.port != port:
            return "Origin 的端口与当前服务端口不一致：%s" % origin
    return ""


def check_json_post(h) -> str:
    """POST 必须声明 application/json。返回 "" ＝放行，否则是拒绝原因。"""
    raw = h.headers.get("Content-Type") or ""
    if raw.split(";")[0].strip().lower() != "application/json":
        return ("只接受 Content-Type: application/json 的请求，收到 %r。"
                "（这一条是为了让跨站的「简单请求」无法抵达本接口）" % (raw or "(空)"))
    return ""


# --------------------------------------------------------------------------- #
# 接收
# --------------------------------------------------------------------------- #
def read_body(h) -> dict:
    # ⚠️ Content-Length 是**客户端给的任意字符串**，不是保证成形的数字。
    #    原写法 `int(h.headers.get("Content-Length") or 0)` 遇到 "abc"、
    #    "10, 10"（多值被拼成逗号串）、"12abc" 都会抛 ValueError，
    #    一路冒到 _dispatch 的 except 变成 500「服务端异常」——
    #    这是**客户端的错**，该回 400，而且 500 会把 traceback 写进日志、
    #    让排障时误以为是服务端 bug。
    #    BodyTooLarge 的 413 分支同理：先把「不是数字」挡在门外。
    try:
        n = int(h.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        # 回 400 而不是静默当空体、更不是抛异常：
        # 这一层的错误语义是「已经自己回好响应了」，调用方（_dispatch）看到
        # BodyTooLarge 只会静默收尾。若在这里 raise，等于把**已经发出的 400**
        # 变成「服务端异常」，traceback 照样进日志 —— 正是要避免的那种误导。
        send_err(h, "Content-Length 不是合法整数", 400)
        h.close_connection = True      # 体没读走，连接不能再复用
        return {}
    if not n:
        return {}
    if n > MAX_BODY:
        # 只回 413，一个字节都不读走 —— 读进来就等于把内存交出去了。
        send_err(h, "请求体过大（%d 字节，上限 %d 字节），请改用「选择文件」按钮指定本机路径"
                 % (n, MAX_BODY), 413)
        # 请求体没读走，这条连接不能再复用，否则残留的字节会被当成下一个请求解析
        h.close_connection = True
        raise BodyTooLarge(n)
    raw = h.rfile.read(n)
    # ⚠️ socket 的 read(n) **不保证读满 n 个字节**（连接中途断开、前端 abort、
    #    代理提前关闭都会只给一半）。原写法直接 json.loads，短读时拿到截断的
    #    JSON → 解析失败 → 静默返回 {} → 接口回「参数不足」，调用方（前端）
    #    完全不知道是传输被截断了，排障时会怀疑自己的参数写错。
    #    这里补读满；补不齐就当成客户端断连，走和 _dispatch 里同样的静默路径。
    if len(raw) < n:
        rest = b""
        try:
            rest = h.rfile.read(n - len(raw))
        except Exception:                                   # noqa: BLE001
            rest = b""
        raw = raw + rest
        if len(raw) < n:
            # 短读 = 客户端断连/前半截没送完。已经发过 4xx 的场景在更上面处理，
            # 这里没有响应可发，只能静默收尾；但 **不能** 抛 BodyTooLarge，
            # 否则 _dispatch 的「静默」分支之外还会多写一条 ERROR 级日志。
            h.close_connection = True
            return {}
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    # ⚠️ 必须确认是 JSON **对象**：`json.loads("[1,2]")` / `json.loads("5")` 都能成功，
    #    返回的却是 list / int。原实现原样交给调用方，接着 `body.get(...)`
    #    抛 AttributeError → 500。前端只会发对象，所以非对象一律按「空体」处理，
    #    各接口既有的「参数不足」分支会正常回 400 级错误。
    return body if isinstance(body, dict) else {}


# --------------------------------------------------------------------------- #
# 静态文件
# --------------------------------------------------------------------------- #
def _inside(full: str, root: str) -> bool:
    """full 是不是确实落在 root 目录里。

    Windows 上两个路径在不同盘符时 commonpath 会抛 ValueError，按「不在里面」处理。
    """
    try:
        return os.path.commonpath([full, root]) == root
    except ValueError:
        return False


def serve_static(h, rel: str, web_dir: str):
    """把 web_dir 下的 rel 发出去；越界或不存在一律 404。返回 DONE。

    ⚠️ 路径逃逸防护（踩坑记录，别改回去）：
       原来只做 rel.replace("..", "")，这拦不住变形攻击 ——
       `/static/....//core/x.py` 替换后仍是 `//core/x.py`，
       `/static/..%2f..%2fcore%2fx.py` 更是原样带着编码穿过去。
       正确顺序是：**先 URL 解码 → 再规范化 → 最后校验前缀**，
       并且校验要拿 commonpath 比对，而不是字符串 startswith
       （`C:\\web-evil` 是 `C:\\web` 的前缀，startswith 会放行）。
    """
    rel = urllib.parse.unquote(rel or "")
    root = os.path.normpath(web_dir)
    full = os.path.normpath(os.path.join(root, rel.lstrip("/\\")))
    if not _inside(full, root) or not os.path.isfile(full):
        return send_err(h, "静态资源不存在: %s" % rel, 404)
    ctype = MIME.get(rel.rsplit(".", 1)[-1].lower(), "application/octet-stream")
    with open(full, "rb") as fp:
        send(h, 200, fp.read(), ctype)
    return DONE
