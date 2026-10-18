import json, os, sys, threading, time, urllib.request, urllib.error
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)

# 起真服务会把审计日志写到 data/，必须在 import 之前把数据根指到临时目录。
import atexit as _atexit
import shutil as _shutil
import tempfile as _tempfile
_WORK = _tempfile.mkdtemp(prefix="l4d2ms-e2e-")
_atexit.register(_shutil.rmtree, _WORK, ignore_errors=True)
os.environ["L4D2MS_WORK_ROOT"] = _WORK

from app import server

# 换一个空闲端口 + 独立 STATE，绝不碰用户真正的 17246 实例
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()

srv = server.bind_server(PORT, "127.0.0.1")
th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
time.sleep(0.6)
base = "http://127.0.0.1:%d" % PORT

def call(path, method="GET", body=None):
    req = urllib.request.Request(base + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

ok = fail = 0
def chk(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  [OK]   %s" % name)
    else: fail += 1; print("  [FAIL] %s %s" % (name, extra))

print("=== A 静态资源 ===")
c, b = call("/")
chk("GET / 返回 200 且是 index.html", c == 200 and b"<!DOCTYPE html>" in b, "code=%s len=%d" % (c, len(b)))
c, b = call("/static/app.js")
chk("GET /static/app.js 200", c == 200 and b"API_TIMEOUT" in b, "code=%s" % c)
c, b = call("/static/../app/server.py")
chk("路径穿越被拦（非 200）", c != 200, "code=%s" % c)

print("=== B 只读接口 ===")
for path, key in [("/api/status", "app"), ("/api/bootstrap", None),
                  ("/api/acquire/env", None), ("/api/staging", None),
                  ("/api/backups", None), ("/api/gamefiles", "groups")]:
    c, b = call(path)
    try: j = json.loads(b.decode("utf-8"))
    except Exception: j = {}
    good = c == 200 and (key is None or key in j)
    chk("GET %s" % path, good, "code=%s keys=%s" % (c, list(j)[:6]))

print("=== C 安全闸门 ===")
req = urllib.request.Request(base + "/api/enable", method="POST",
                             data=b'{"changes":{}}')
req.add_header("Content-Type", "text/plain")
try:
    urllib.request.urlopen(req, timeout=20); code = 200
except urllib.error.HTTPError as e: code = e.code
chk("POST 非 JSON Content-Type 被拒", code >= 400, "code=%s" % code)

req = urllib.request.Request(base + "/api/status")
req.add_header("Host", "evil.example:80")
try:
    urllib.request.urlopen(req, timeout=20); code = 200
except urllib.error.HTTPError as e: code = e.code
chk("Host 非回环被拒", code >= 400, "code=%s" % code)

print("=== D 未登记路由 ===")
c, b = call("/api/does-not-exist")
chk("404", c == 404, "code=%s" % c)

srv.shutdown(); srv.server_close()
print("\n端到端结果：通过 %d 项，失败 %d 项" % (ok, fail))
sys.exit(0 if fail == 0 else 1)

