# -*- coding: utf-8 -*-
"""L4D2 Mod Studio 桌面版启动器 —— **原生窗口，不弹浏览器**。

双击 L4D2ModStudio.exe 之后发生的事：

  1. 后台线程里起一个本地 HTTP 服务，**只监听 127.0.0.1**（外网碰不到它）；
  2. 用 pywebview 开一个原生窗口加载界面 —— 没有地址栏、没有标签页、没有收藏夹；
  3. 关掉窗口就等于退出程序，后台服务跟着一起结束。

窗口有三层降级，任何一层出问题都不会让你对着黑屏发呆：
    pywebview 原生窗口  →  Edge/Chrome 的 --app 无边框窗口  →  系统默认浏览器

因为是 windowed exe（没有控制台），诊断信息写进 <数据目录>/logs/desktop.log，
致命错误另外弹一个原生消息框，不会静默死掉。

命令行参数：
    --browser     直接用系统默认浏览器打开（不用原生窗口）
    --port N      指定起始端口（默认 17246，被占会自动往后顺延）
    --console     额外起一个控制台窗口显示日志（排障用）
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback

# --------------------------------------------------------------------------- #
# 启动期准备：先把路径和 stdout 兜住，再做别的
# --------------------------------------------------------------------------- #

def _base_dir() -> str:
    """代码/资源所在目录。exe 模式下是 PyInstaller 的解包目录。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


BASE = _base_dir()
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))

# ⚠️ windowed exe 里 sys.stdout / sys.stderr 是 None，
#    任何一句 print() 都会炸成 AttributeError。先拿 devnull 顶上。
for _name in ("stdout", "stderr"):
    if getattr(sys, _name, None) is None:
        try:
            setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))
        except Exception:
            pass

import server  # noqa: E402  在 sys.path 设置之后才能导入
from core import safety  # noqa: E402

WINDOW_TITLE = "L4D2 Mod Studio — 求生之路 2 Mod 管理器"
WIN_W, WIN_H = 1400, 900
WIN_MIN = (1024, 660)
PORT_TRIES = 12
# 无边框窗口「存活时长」下限：低于这个秒数就退出，判定为「浏览器把请求转交给了
# 已在运行的实例，自己立刻退场」（见 window_app_mode 的说明）。
_MIN_ALIVE = 1.5

_LOG_PATH = None
# desktop.log 的轮转上限（与 core/safety.py 的 audit.log 同一套思路）。
# windowed exe 没有控制台，这个文件是排障的**唯一线索**，所以不能无限长：
# 双击几十次之后一个几十 MB 的日志既打不开也没人看。超限就把当前文件
# 改名为 desktop.log.1（旧的 .1 直接覆盖），保证「最近一次运行」永远留在
# 主文件里、且总量封顶在 2 倍上限。
_DESKTOP_LOG_MAX = 2 * 1024 * 1024


def _rotate_desktop_log(path: str) -> None:
    """超过上限就把当前日志挪成 .1。任何失败都吞掉 —— 日志绝不能拖垮启动。"""
    try:
        if os.path.getsize(path) < _DESKTOP_LOG_MAX:
            return
        os.replace(path, path + ".1")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 日志与报错
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    global _LOG_PATH
    line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        if _LOG_PATH is None:
            d = os.path.join(safety.DATA_DIR, "logs")
            os.makedirs(d, exist_ok=True)
            _LOG_PATH = os.path.join(d, "desktop.log")
        _rotate_desktop_log(_LOG_PATH)
        with open(_LOG_PATH, "a", encoding="utf-8") as fp:
            fp.write(line)
    except Exception:
        pass
    try:
        if sys.stdout is not None and not sys.stdout.closed:
            sys.stdout.write(line)
            sys.stdout.flush()
    except UnicodeEncodeError:
        # 控制台代码页不一定吃得下中文（比如某些 cp437 环境）。
        # 文件里已经是完整 UTF-8 了，这里丢字符不影响留档。
        pass
    except Exception:
        pass


def msgbox(text: str, title: str = "L4D2 Mod Studio", flags: int = 0x40) -> None:
    """原生消息框。flags: 0x40 信息(i) / 0x30 警告(!) / 0x10 错误(x)。

    设了 L4D2MS_NO_DIALOG=1 就只写日志不弹框 —— 自动化测试用，
    免得一个没人点的模态框把流程挂死。
    """
    if os.environ.get("L4D2MS_NO_DIALOG"):
        log("[对话框已抑制] %s：%s" % (title, text.replace("\n", " / ")))
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 起服务
# --------------------------------------------------------------------------- #

class AlreadyRunning(Exception):
    def __init__(self, pid):
        super().__init__("already running")
        self.pid = pid


def start_service(prefer: int):
    """在**已绑定**的服务上返回 (srv, port)。

    先看首选端口上是不是本工具的另一个实例 —— 是就抛 AlreadyRunning
    （两个实例同时写 addonlist 会互相踩，必须拦住）；
    是别的程序占着，就往后顺延端口。
    """
    state, data = server._probe("http://127.0.0.1:%d/" % prefer)
    if state == "ours":
        raise AlreadyRunning((data or {}).get("pid"))

    last_err = None
    for i in range(PORT_TRIES):
        port = prefer + i
        try:
            srv = server.bind_server(port)
            if i:
                log("端口 %d 被占用，已改用 %d" % (prefer, port))
            return srv, port
        except OSError as ex:
            last_err = ex
            log("端口 %d 绑定失败：%s（继续试下一个）" % (port, ex))
    raise RuntimeError("连续 %d 个端口都绑不上，最后一个错误：%s" % (PORT_TRIES, last_err))


# --------------------------------------------------------------------------- #
# 三层窗口
# --------------------------------------------------------------------------- #

def window_native(url: str) -> str:
    """第 1 层：pywebview 原生窗口。返回 'ok' 或抛异常。

    环境变量（排障用，正常使用不需要）：
      L4D2MS_SELFTEST=秒数   开窗后自动关窗，用于自动化验证
      L4D2MS_NO_NATIVE=1     跳过原生窗口，强制走降级链
    """
    import webview
    win = webview.create_window(WINDOW_TITLE, url,
                                width=WIN_W, height=WIN_H, min_size=WIN_MIN,
                                resizable=True)
    log("原生窗口已创建，进入消息循环（关掉窗口即退出）")

    auto = os.environ.get("L4D2MS_SELFTEST")
    if auto:
        def _close_later():
            try:
                time.sleep(max(0.5, float(auto)))
            except ValueError:
                time.sleep(2.0)
            # 关窗前先在窗口里跑一段 JS，回报界面到底渲染成什么样了 ——
            # 「窗口开出来了」不等于「界面是对的」，得看一眼 DOM 才算数。
            try:
                probe = win.evaluate_js(_PROBE_JS)
                log("自检·界面探针: %s" % probe)
            except Exception as ex:
                log("自检·界面探针失败：%s" % ex)
            log("自检模式：自动关窗")
            try:
                win.destroy()
            except Exception as ex:
                log("自动关窗失败：%s" % ex)
        webview.start(_close_later)      # start(func) 会把 func 放到子线程跑
    else:
        webview.start()                  # 阻塞到窗口关闭
    return "ok"


# 自检探针：读几个稳定的 id，看 bootstrap/scan 有没有真的跑完
_PROBE_JS = r"""
(function () {
  function txt(id) {
    var e = document.getElementById(id);
    return e ? (e.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 90) : '(缺 id:' + id + ')';
  }
  var v = document.getElementById('view');
  return JSON.stringify({
    title: document.title,
    ready: document.readyState,
    nodes: document.getElementsByTagName('*').length,
    head: txt('headPath'),
    navMods: txt('navMods'),
    navAudit: txt('navAudit'),
    view: v ? (v.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 260) : '(缺 #view)'
  });
})()
"""


def _browser_exes():
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    return [
        os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(local, r"Microsoft\Edge\Application\msedge.exe"),
    ]


def window_app_mode(url: str) -> bool:
    """第 2 层：Edge/Chrome 的 --app 无边框窗口。

    没有地址栏、没有标签页、任务栏上一个独立图标 —— 比裸开浏览器体面得多。
    用独立的 user-data-dir，免得污染你日常浏览器的配置。

    ⚠️ 返回值必须如实反映「窗口真的开出来了」（2026-10-16 修复）：
        原来无论浏览器怎么退出都 `return True`，于是有两种静默失败：
          ① 浏览器启动失败（profile 被上一次留下的进程锁住、参数不认等）
             会立刻以非 0 退出码结束 → 仍返回 True → main() 认为窗口已关，
             直接停服务退出。用户双击 exe，**什么都没看见，程序就没了**；
          ② 浏览器把请求转交给已在运行的实例后自己立刻退出（退出码 0）→
             同样返回 True → 服务被停掉，而那个浏览器的窗口还开着，
             页面变成「无法连接」。
        现在：非 0 退出码 = 失败，换下一个浏览器；退出码 0 但**存活不足
        _MIN_ALIVE 秒** = 判定为转交/秒退，也算失败并继续降级。
        正常用户关窗口至少要 1.5 秒（得先把界面画出来），不会被误判。
    """
    profile = os.path.join(tempfile.gettempdir(), "L4D2ModStudio-profile")
    for exe in _browser_exes():
        if not os.path.isfile(exe):
            continue
        try:
            log("改用无边框窗口模式：%s" % exe)
            t0 = time.time()
            proc = subprocess.Popen(
                [exe, "--app=" + url,
                 "--window-size=%d,%d" % (WIN_W, WIN_H),
                 "--user-data-dir=" + profile,
                 "--no-first-run", "--no-default-browser-check"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            proc.wait()      # 窗口关掉，进程退出
            alive = time.time() - t0
            if proc.returncode != 0:
                log("%s 退出码 %s（窗口没开成），换下一个浏览器"
                    % (os.path.basename(exe), proc.returncode))
                continue
            if alive < _MIN_ALIVE:
                log("%s 只存活了 %.2fs 就退出（疑似把请求转交给了别的实例），换下一个"
                    % (os.path.basename(exe), alive))
                continue
            return True
        except Exception as ex:
            log("无边框窗口启动失败：%s" % ex)
    return False


def window_browser(url: str) -> None:
    """第 3 层：系统默认浏览器 + 一个「点确定就退出」的对话框。

    这一层没法感知浏览器什么时候被关掉，所以用模态框当「退出开关」，
    既不留下僵尸进程，也不用你去任务管理器里杀。
    """
    import webbrowser
    log("退到默认浏览器模式：%s" % url)
    try:
        webbrowser.open(url)
    except Exception as ex:
        log("打开默认浏览器失败：%s" % ex)
    msgbox("界面已在你的默认浏览器中打开：\n\n%s\n\n"
           "工具会在后台继续运行。\n"
           "想退出，点这个对话框的「确定」即可。" % url,
           "L4D2 Mod Studio 正在运行")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def _has_flag(name: str) -> bool:
    return name in sys.argv


def main() -> int:
    started = time.time()
    log("=" * 60)
    log("启动 L4D2 Mod Studio 桌面版")
    log("  代码资源目录: %s" % safety.APP_ROOT)
    log("  数据目录:     %s" % safety.WORK_ROOT)
    log("  运行模式:     %s" % ("exe（冻结）" if getattr(sys, "frozen", False) else "源码"))

    prefer = server._parse_port(sys.argv)

    # --- 起服务 ---
    srv = None
    try:
        srv, port = start_service(prefer)
    except AlreadyRunning as ex:
        log("检测到已有实例在运行（PID %s），本次退出" % ex.pid)
        msgbox("L4D2 Mod Studio 已经在运行了（PID %s）。\n\n"
               "请直接使用那个已经打开的窗口。\n"
               "如果找不到那个窗口，先结束该进程再启动本程序：\n\n"
               "    taskkill /PID %s /F" % (ex.pid, ex.pid),
               "已经在运行", 0x40)
        return 0
    except Exception as ex:
        log("起服务失败：%s\n%s" % (ex, traceback.format_exc()))
        msgbox("启动失败，端口被占用或系统拒绝了绑定。\n\n%s\n\n"
               "详细日志：\n%s" % (ex, _LOG_PATH or "(尚未生成)"),
               "启动失败", 0x10)
        return 2

    server.serve_in_thread(srv)
    url = "http://127.0.0.1:%d/" % port
    log("服务就绪：%s（耗时 %.2fs）" % (url, time.time() - started))

    # --- 开窗口 ---
    how = "native"
    try:
        if _has_flag("--browser"):
            log("按参数要求，直接走浏览器模式")
            how = "browser"
        elif os.environ.get("L4D2MS_NO_NATIVE"):
            log("按环境变量要求，跳过原生窗口")
            how = "fallback"
        else:
            window_native(url)
            how = "done"
    except Exception as ex:
        log("原生窗口不可用：%s\n%s" % (ex, traceback.format_exc()))
        how = "fallback"

    if how == "fallback":
        if window_app_mode(url):
            how = "done"
        else:
            window_browser(url)
            how = "done"
    elif how == "browser":
        window_browser(url)
        how = "done"

    # --- 收尾 ---
    log("窗口已关闭，正在停止服务…")
    try:
        srv.shutdown()
        srv.server_close()
        log("服务已停止，进程退出。总运行 %.1f 秒" % (time.time() - started))
    except Exception as ex:
        log("停止服务时出错：%s" % ex)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as _ex:
        try:
            log("未捕获异常：%s\n%s" % (_ex, traceback.format_exc()))
        except Exception:
            pass
        msgbox("程序遇到未处理的错误：\n\n%s\n\n日志：\n%s"
               % (_ex, _LOG_PATH or "(尚未生成)"), "L4D2 Mod Studio", 0x10)
        sys.exit(3)
