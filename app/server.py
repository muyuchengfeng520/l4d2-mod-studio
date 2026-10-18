# -*- coding: utf-8 -*-
"""L4D2 Mod Studio 本地服务：纯标准库 HTTP 服务 + JSON API。

不依赖任何第三方包。启动后浏览器打开 http://127.0.0.1:17246 即可使用。

模块分工（原来都挤在这一个文件里，829 行）
----------------------------------------
* `app\\http_util.py` —— HTTP 收发工具（发响应 / 读请求体 / 静态文件）
* `app\\routes.py`    —— 路由表 GET_ROUTES / POST_ROUTES
* `app\\api.py`       —— 20 个 api_* 处理函数（模块级，收 ctx）
* 本文件             —— 进程级状态、Handler、_Server、绑定与启动

⚠️ 对外契约（desktop.py 依赖，改动即破坏兼容）：
    PORT / HOST / Handler / _Server / bind_server / serve_in_thread /
    _parse_port / _probe / main / get_cfg / do_scan / mod_index / payload_scan /
    STATE / LOCK  —— 全部仍可从 app.server 访问，行为不变。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import paths, safety, scan, taglib  # noqa: E402

# server.py 有两种运行方式：包导入（desktop.py 走 app\ 进 sys.path 后 `import server`）
# 与直接执行（START.bat 跑 app\server.py）。兄弟模块的导入写法不同，按 __package__ 分流。
if __package__:
    from . import api, http_util, routes  # noqa: E402
else:
    import api  # noqa: E402
    import http_util  # noqa: E402
    import routes  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ⚠️ 打包成 exe 后，web/ 静态资源在 PyInstaller 的解包目录里，不能用 ROOT 拼。
#    safety.resource() 会自动区分「源码模式 / exe 模式」。
WEB = safety.resource("web")
PORT = 17246
HOST = "127.0.0.1"

STATE: dict = {"cfg": None, "taglib": None, "last_scan": None, "mods": None,
               # 进程启动时刻（秒）：/api/status 里回传，前端据此识别「实例被换了」
               "started_at": time.time()}
LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
def get_cfg(force: bool = False) -> dict:
    if force or STATE["cfg"] is None:
        boot = paths.bootstrap(force=force)
        STATE["cfg"] = boot["config"]
        STATE["boot_evidence"] = boot["evidence"]
    if STATE["taglib"] is None:
        STATE["taglib"] = taglib.build_taglib(STATE["cfg"].get("taglib", ""))
    STATE["cfg"]["_taglib"] = STATE["taglib"]
    return STATE["cfg"]


def do_scan(force: bool = False, progress_cb=None) -> dict:
    cfg = get_cfg(force)
    r = scan.scan(cfg, progress=progress_cb)
    with LOCK:
        STATE["last_scan"] = r
        STATE["mods"] = r["mods"]
        STATE["mods_for_audit"] = scan.dicts_for_audit(r["mods"])
    return r


def mod_index() -> dict:
    if STATE.get("mods") is None:
        do_scan()
    return {m.key: m for m in STATE["mods"]}


def payload_scan() -> dict:
    r = STATE.get("last_scan")
    if not r:
        r = do_scan()
    return {
        "summary": r["summary"],
        "mods": r["mods_json"],
        "evidence": r["evidence"],
        "unlinked": r["unlinked"],
        "elapsed": r["elapsed"],
        "scanned_at": r["scanned_at"],
        "categories": {k: {"zh": v["zh"], "order": v["order"], "color": v["color"]}
                       for k, v in taglib.CATEGORIES.items()},
    }


# --------------------------------------------------------------------------- #
def _bound_port(h, fallback: int = PORT) -> int:
    """取出**这条连接实际绑定的端口**。

    ⚠️ 不能直接用模块级 PORT：`bind_server(port)` 允许调用方绑任意端口
    （冒烟测试就是这么干的），那时 PORT 还是 17246，来源校验会把 `Host:
    127.0.0.1:17299` 判成端口不一致而全部 403。真实端口在 server_address 里。
    """
    try:
        return int(h.server.server_address[1])
    except Exception:                                   # noqa: BLE001
        return fallback


def make_ctx(h) -> api.Ctx:
    """给一次请求装配上下文。

    ⚠️ 每次请求都**现取** PORT / STATE，不用模块级单例缓存 —— main() 里
    `global PORT` 改过端口之后，下一个请求自然带上新值。
    """
    return api.Ctx(
        h=h,
        web_dir=WEB,
        get_cfg=get_cfg,
        do_scan=do_scan,
        mod_index=mod_index,
        payload_scan=payload_scan,
        state=STATE,
        lock=LOCK,
        port=_bound_port(h),
        query=parse_qs(urlparse(h.path).query),
        read_body=http_util.read_body,
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "L4D2ModStudio/1.0"

    def log_message(self, fmt, *args):        # 静音，避免刷屏
        pass

    # -- 基础 -------------------------------------------------------------- #
    # 四个基础方法保留为薄壳，只为兼容「万一有外部代码直接调 Handler._json」；
    # 真正的实现搬到了 http_util（纯函数版）。
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        return http_util.send(self, code, body, ctype)

    def _json(self, obj, code: int = 200):
        return http_util.send_json(self, obj, code)

    def _err(self, msg: str, code: int = 400, detail: str = ""):
        return http_util.send_err(self, msg, code, detail)

    def _body(self) -> dict:
        return http_util.read_body(self)

    # -- 路由 -------------------------------------------------------------- #
    def _guard(self, is_post: bool) -> str:
        """本地来源校验。返回 "" ＝放行，否则是拒绝原因（调用方回 403）。

        见 http_util 顶部那张「为什么需要这一层」的说明：服务监听回环地址
        不等于只有本机能调它，任何网页都能往这里发「简单请求」触发副作用。
        """
        bad = http_util.check_request(self, _bound_port(self))
        if not bad and is_post:
            bad = http_util.check_json_post(self)
        return bad

    def _dispatch(self, table, is_post: bool = False):
        """查表 → 调处理函数 → 发响应。原来的两条 if 链就是被这里替掉的。"""
        p = urlparse(self.path).path
        bad = self._guard(is_post)
        if bad:
            return self._err(bad, http_util.LOCAL_GUARD_CODE)
        ctx = make_ctx(self)
        try:
            handler, params = routes.lookup(table, p)
            if handler is None:
                return self._err("未知接口: %s" % p, 404)
            ctx.params = params or {}
            out = handler(ctx)
            if out is http_util.DONE:
                return None                      # 处理函数自己写完了响应
            return self._json(out)
        except http_util.BodyTooLarge:
            # 413 已经在 read_body 里发回去了，这里只是别让它变成 500
            return None
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 前端超时/关页面时会主动断开（web/app.js 用 AbortController 中止）。
            # ⚠️ 这不是「服务端异常」：原先它会掉进下面的通用分支，把
            #    traceback.format_exc() 几百行写进 audit.log，一天就把轮转文件
            #    刷满，真正该看的错误反被挤掉。断开就是断开，不再回任何东西。
            return None
        except Exception as ex:
            return self._err("服务端异常", 500, "%s\n%s" % (ex, traceback.format_exc()))

    def do_GET(self):
        return self._dispatch(routes.GET_ROUTES, is_post=False)

    def do_POST(self):
        return self._dispatch(routes.POST_ROUTES, is_post=True)

    def do_OPTIONS(self):
        """不发 CORS 预检响应 —— 明确回 501。

        ⚠️ 这是安全设计的一部分，不是没实现：本服务只给本机页面用，**故意**
        不声明任何跨域许可。跨站的 application/json 请求会先发预检，这里回 501
        ⇒ 预检失败 ⇒ 真实请求根本发不出去。别为了「兼容」加上 CORS 头。
        """
        return self._err("本服务不提供跨域访问（OPTIONS 不支持）", 501)

    # -- 静态文件 ---------------------------------------------------------- #
    def _static(self, rel: str):
        return http_util.serve_static(self, rel, WEB)


class _Server(ThreadingHTTPServer):
    """⚠️ 必须把 allow_reuse_address 显式关掉。

    ThreadingHTTPServer 默认是 True（继承自 HTTPServer）。在 Windows 上
    SO_REUSEADDR 的语义与 Unix 不同 —— 它允许**第二个进程也绑上同一端口**，
    于是双击两次 START.bat 就会出现两个服务同时 LISTENING（端口劫持），
    请求落到哪个实例不确定，状态还可能互相踩。关掉后第二次绑定会失败，
    由 main() 的探测逻辑给出人能看懂的提示。
    """
    allow_reuse_address = False
    daemon_threads = True


def _sep():
    print("=" * 62)


def _parse_port(argv: list) -> int:
    """支持 --port 17247 / --port=17247。给端口冲突时留一条退路。"""
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            try:
                return int(argv[i + 1])
            except ValueError:
                return PORT
        if a.startswith("--port="):
            try:
                return int(a.split("=", 1)[1])
            except ValueError:
                return PORT
    return PORT


def _probe(url: str, timeout: float = 2.0):
    """探测端口上有没有人应答。

    返回 ("ours", data) / ("other", None) / (None, None)（连不上 = 端口空着）。
    注意：「能连上但不是本工具」也要判成 other —— 只要对端回了 HTTP，
    就说明端口被占了，不能当成空闲去抢绑。
    """
    try:
        with urllib.request.urlopen(url + "api/status", timeout=timeout) as r:
            body = r.read(65536)
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            return ("other", None)          # 回了 HTTP 但不是我们的 JSON
        if isinstance(data, dict) and data.get("app") == "L4D2 Mod Studio":
            return ("ours", data)
        return ("other", None)
    except urllib.error.HTTPError:
        return ("other", None)              # 对端回了 HTTP 错误码 → 也是别人占着
    except Exception:
        return (None, None)


def _listener_pid(port: int):
    """尽力查出正在监听该端口的 PID（查不到就返回 None）。"""
    try:
        import subprocess
        # ⚠️ 中文 Windows 的 netstat 输出是 GBK；用 text=True 按 UTF-8 解码会抛
        # UnicodeDecodeError。这里直接拿 bytes 再按「替换非法字节」的方式解码，
        # 行内容只认 ASCII 的数字列，所以替换不影响判断。
        # CREATE_NO_WINDOW：exe 是无控制台的，调 netstat 这种控制台程序
        # 不指定的话 Windows 会弹一个黑窗口出来（用户视角就是「闪一下」）
        raw = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, timeout=8,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        out = raw.decode("utf-8", errors="replace")
        pids = set()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(":%d" % port):
                pids.add(parts[4])
        return sorted(pids)
    except Exception:
        return None


def _port_busy(port: int, reason: str = "") -> int:
    """端口被别人占了时统一打一段人能看懂的提示。返回 1（失败码）。"""
    _sep()
    print("  启动失败：端口 %d 已被别的程序占用。" % port)
    if reason:
        print("  系统提示：%s" % reason)
    pids = _listener_pid(port)
    if pids:
        print("  占用该端口的进程 PID: %s" % ", ".join(pids))
    print("  两个办法：")
    print("    ① 关掉占用该端口的程序，然后重新运行本程序；")
    print("    ② 换个端口启动：  START.bat --port %d" % (port + 1))
    _sep()
    return 1


def bind_server(port: int = PORT, host: str = HOST) -> _Server:
    """绑定端口并返回服务对象（**还没有开始接受请求**）。

    端口被占用时抛 OSError —— 调用方自己决定怎么提示，
    这样桌面版和命令行版能给各自的用户界面用最合适的方式报错。
    """
    return _Server((host, port), Handler)


def serve_in_thread(srv: _Server, name: str = "http-serve") -> threading.Thread:
    """在守护线程里开跑。daemon=True 保证主窗口一关进程就能退干净。"""
    t = threading.Thread(target=srv.serve_forever, name=name, daemon=True)
    t.start()
    return t


def main():
    global PORT
    PORT = _parse_port(sys.argv)
    url = "http://%s:%d/" % (HOST, PORT)
    get_cfg()

    # ---- 启动前先看端口上是谁：别让第二个实例悄悄并存（Windows 下
    #      allow_reuse_address=True 允许两个进程绑同一端口 = 端口劫持）----
    kind, data = _probe(url)
    if kind == "ours":
        pid = (data or {}).get("pid")
        _sep()
        print("  L4D2 Mod Studio 已经在运行了（PID %s）" % (pid or "未知"))
        print("  地址: %s" % url)
        print("  本次不再重复启动服务。")
        if pid:
            print("  如果那个窗口已经关掉、只剩后台进程，可以先结束它：")
            print("      taskkill /PID %s /F" % pid)
        _sep()
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)
        # 用专门的退出码 3 表示「没启动，但已有实例在跑」。
        # ⚠️ 原来回 0：START.bat 里紧跟的 `echo Server stopped.` + `pause`
        #    会让用户以为**刚启动的**服务已经停了，而真正在跑的是上一个实例
        #    （PID 在上面那几行里，很容易被滚屏冲掉）。桌面版 desktop.py 也靠
        #    返回值区分「新起的」和「复用的」。
        return 3
    if kind == "other":
        return _port_busy(PORT, "该端口上的服务不响应本工具的 /api/status，不是本程序")

    # ---- 端口空闲，正式绑定。allow_reuse_address=False 防端口劫持 ----
    try:
        srv = _Server((HOST, PORT), Handler)
    except OSError as ex:
        return _port_busy(PORT, str(ex))

    _sep()
    print("  L4D2 Mod Studio 已启动")
    print("  地址: %s" % url)
    print("  数据目录: %s" % safety.DATA_DIR)
    print("  关闭本窗口即退出。")
    _sep()
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
