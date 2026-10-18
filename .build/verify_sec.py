# -*- coding: utf-8 -*-
"""对抗性测试：本地来源校验 / 静态文件穿越 / 路由编译 / 配置损坏 / 链接刷新。只读+临时目录。"""
import io, json, os, sys, tempfile, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# core.safety 在 import 期就把数据目录算死并 makedirs，所以必须在 import 之前
# 把 L4D2MS_WORK_ROOT 指到临时目录；否则配置损坏/链接刷新检查会把审计日志
# 和备份写进玩家真实的 data/（本脚本最初就是这样污染了 177 行 audit.log）。
import atexit as _atexit
import shutil as _shutil
_WORK = tempfile.mkdtemp(prefix="l4d2ms-sec-")
_atexit.register(_shutil.rmtree, _WORK, ignore_errors=True)
os.environ["L4D2MS_WORK_ROOT"] = _WORK

PASS = FAIL = 0
FAILS = []

def chk(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        FAILS.append((name, detail))
        print("  [FAIL] %s   %s" % (name, detail))

class FakeH:
    """最小 HTTP handler 桩：够 http_util 用。"""
    def __init__(self, path="/", headers=None, port=17246):
        self.path = path
        self.headers = headers or {}
        self.wfile = io.BytesIO()
        self.code = None
        self.sent = b""
        self.close_connection = False
        self.server = type("S", (), {"server_address": ("127.0.0.1", port)})()
    def send_response(self, code, message=None):
        self.code = code
    def send_header(self, k, v):
        pass
    def end_headers(self):
        pass

from app import http_util, routes

print("\n=== A 本地来源校验 check_request ===")
base = {"Host": "127.0.0.1:17246"}
# 1 正常放行
h = FakeH(headers=dict(base))
chk("Host 与端口都正确 → 放行", http_util.check_request(h, 17246) == "")
# 2 端口不符
h = FakeH(headers={"Host": "127.0.0.1:9999"})
chk("Host 端口不一致 → 拒绝", bool(http_util.check_request(h, 17246)))
# 3 陌生 hostname（DNS rebinding）
h = FakeH(headers={"Host": "evil.example.com:17246"})
chk("Host 是外部域名 → 拒绝", bool(http_util.check_request(h, 17246)))
# 4 跨站请求
h = FakeH(headers=dict(base, **{"Sec-Fetch-Site": "cross-site"}))
chk("Sec-Fetch-Site: cross-site → 拒绝", bool(http_util.check_request(h, 17246)))
# 5 Origin null
h = FakeH(headers=dict(base, Origin="null"))
chk("Origin: null → 拒绝", bool(http_util.check_request(h, 17246)))
# 6 Origin 是外部站
h = FakeH(headers=dict(base, Origin="http://evil.com"))
chk("Origin 外部站点 → 拒绝", bool(http_util.check_request(h, 17246)))
# 7 Origin 同源
h = FakeH(headers=dict(base, Origin="http://127.0.0.1:17246"))
chk("Origin 同源 → 放行", http_util.check_request(h, 17246) == "")
# 8 IPv6 回环
h = FakeH(headers={"Host": "[::1]:17246"})
chk("IPv6 回环 Host → 放行", http_util.check_request(h, 17246) == "")
# 9 缺 Host（HTTP/1.0）
h = FakeH(headers={})
chk("缺 Host 头 → 拒绝", bool(http_util.check_request(h, 17246)))
# 10 POST Content-Type 必须 json
h = FakeH(headers={"Content-Type": "text/plain"})
chk("POST Content-Type 非 json → 拒绝", bool(http_util.check_json_post(h)))
h = FakeH(headers={"Content-Type": "application/json; charset=utf-8"})
chk("POST Content-Type json（带 charset）→ 放行", http_util.check_json_post(h) == "")
h = FakeH(headers={"Content-Type": "application/x-www-form-urlencoded"})
chk("POST 表单类型 → 拒绝", bool(http_util.check_json_post(h)))

print("\n=== B 静态文件路径穿越 serve_static ===")
web = os.path.join(ROOT, "web")
h = FakeH("/")
http_util.serve_static(h, "/index.html", web)
chk("正常读 index.html", h.code == 200 and b"L4D2" in h.wfile.getvalue())
h = FakeH("/")
http_util.serve_static(h, "/../app/server.py", web)
chk(".. 穿越被拦（非 200）", h.code != 200)
h = FakeH("/")
http_util.serve_static(h, "/..%2f..%2fapp%2fserver.py", web)
chk("URL 编码穿越被拦（非 200）", h.code != 200)
h = FakeH("/")
http_util.serve_static(h, "/%2e%2e/%2e%2e/core/safety.py", web)
chk("编码 .. 穿越被拦（非 200）", h.code != 200)
h = FakeH("/")
http_util.serve_static(h, "/../data/config.json", web)
chk("读 data/config.json 被拦（非 200）", h.code != 200)
h = FakeH("/")
http_util.serve_static(h, "/nonexistent.js", web)
chk("不存在的文件 → 404", h.code == 404)

print("\n=== C 路由编译与匹配 ===")
r = routes.Route("/api/mod/{key}", lambda c: None)
chk("路径参数匹配", r.match("/api/mod/abc") == {"key": "abc"})
chk("路径参数不跨斜杠", r.match("/api/mod/a/b") is None)
r2 = routes.Route("/api/vpk/plan/{rest:path}", lambda c: None)
chk("{x:path} 跨斜杠", r2.match("/api/vpk/plan/a/b/c") == {"rest": "a/b/c"})
r3 = routes.Route("/", lambda c: None)
chk("根路径匹配", r3.match("/") == {})
# 每条 GET/POST 路由都能编译且 match 自己 literal 化后的样例
bad = []
for tbl in (routes.GET_ROUTES, routes.POST_ROUTES):
    for rt in tbl:
        try:
            rt.regex
        except Exception as ex:
            bad.append((rt.pattern, ex))
chk("全部 %d 条路由正则都能编译" % (len(routes.GET_ROUTES) + len(routes.POST_ROUTES)), not bad, str(bad[:3]))
# 查重复 pattern
for _name, _tbl in (("GET", routes.GET_ROUTES), ("POST", routes.POST_ROUTES)):
    _pats = [rt.pattern for rt in _tbl]
    chk("%s 表内无重复 pattern" % _name, len(_pats) == len(set(_pats)),
        str(sorted({p for p in _pats if _pats.count(p) > 1})))
# GET 与 POST 允许同一路径各登记一次（GET 查询 / POST 修改），
# 但两边必须是不同的处理函数 —— 同一个函数挂两种方法说明写错了。
gp = {rt.pattern for rt in routes.GET_ROUTES}
pp = {rt.pattern for rt in routes.POST_ROUTES}
_bad = []
for _p in sorted(gp & pp):
    _g = [rt.handler.__name__ for rt in routes.GET_ROUTES if rt.pattern == _p]
    _q = [rt.handler.__name__ for rt in routes.POST_ROUTES if rt.pattern == _p]
    if _g == _q:
        _bad.append((_p, _g))
chk("GET/POST 同名路径必须是不同处理函数", not _bad, str(_bad))

print("\n=== D routes.lookup 命中与未命中 ===")
h_, params = routes.lookup(routes.GET_ROUTES, "/api/status")
chk("lookup /api/status 命中", h_ is not None)
h_, params = routes.lookup(routes.GET_ROUTES, "/api/nonexistent")
chk("lookup 未登记路径 → None", h_ is None)
h_, params = routes.lookup(routes.POST_ROUTES, "/api/enable")
chk("lookup POST /api/enable 命中", h_ is not None)

print("\n=== E 配置损坏保护 paths.load ===")
tmpd = tempfile.mkdtemp(prefix="l4d2_cfg_")
from core import safety, paths
old_cfg = paths.CONFIG_PATH
# 改成临时目录里的 config.json
paths.CONFIG_PATH = os.path.join(tmpd, "config.json")
try:
    # 1 损坏 JSON
    with open(paths.CONFIG_PATH, "w", encoding="utf-8") as fp:
        fp.write('{"game_dir": "E:/game", ')     # 截断
    cfg = paths.load()
    chk("损坏 JSON → 回退默认值", cfg["game_dir"] == "")
    chk("损坏 JSON → 留下 .bad 备份", os.path.isfile(paths.CONFIG_PATH + ".bad"))
    # 2 顶层不是对象
    with open(paths.CONFIG_PATH, "w", encoding="utf-8") as fp:
        fp.write('["a", "b"]')
    cfg = paths.load()
    chk("顶层是数组 → 回退默认值", cfg["game_dir"] == "")
    # 3 值不是字符串
    with open(paths.CONFIG_PATH, "w", encoding="utf-8") as fp:
        json.dump({"game_dir": None, "repo_dir": 123, "taglib": "x"}, fp)
    cfg = paths.load()
    chk("null/数字值被忽略，只留字符串", cfg["game_dir"] == "" and cfg["repo_dir"] == "" and cfg["taglib"] == "x")
    # 4 正常
    with open(paths.CONFIG_PATH, "w", encoding="utf-8") as fp:
        json.dump({"game_dir": "E:/g"}, fp)
    chk("正常配置能读出", paths.load()["game_dir"] == "E:/g")
    # 5 bootstrap 遇损坏不写回
    with open(paths.CONFIG_PATH, "w", encoding="utf-8") as fp:
        fp.write("{broken")
    before = open(paths.CONFIG_PATH, "rb").read()
    boot = paths.bootstrap()
    after = open(paths.CONFIG_PATH, "rb").read()
    chk("bootstrap 遇损坏不覆盖原文件", before == after)
    chk("bootstrap 在 evidence 里标注 config_corrupt", "config_corrupt" in boot["evidence"])
finally:
    paths.CONFIG_PATH = old_cfg
    import shutil; shutil.rmtree(tmpd, ignore_errors=True)

print("\n=== F 链接刷新 acquire.link_entries / refresh_links ===")
from core import acquire
tmpa = tempfile.mkdtemp(prefix="l4d2_lnk_")
try:
    body = os.path.join(tmpa, "real.vpk")
    with open(body, "wb") as fp:
        fp.write(b"x" * 128)
    # 造一个指向它的符号链接
    lnk = os.path.join(tmpa, "link.vpk")
    made = "none"
    try:
        os.symlink(body, lnk)
        made = "symlink"
    except OSError:
        try:
            os.link(body, lnk)
            made = "hardlink"
        except OSError:
            made = "none"
    ents = acquire.link_entries(tmpa, body)
    if made == "none":
        print("  [SKIP] 本机无法创建任何链接，跳过（不算失败）")
    else:
        chk("link_entries 找到 %s" % made, bool(ents), str(ents))
        # 换 inode 之后刷新
        with open(body, "wb") as fp:
            fp.write(b"y" * 128)
        fixed = acquire.refresh_links(tmpa, body, ents)
        chk("refresh_links 返回结果", isinstance(fixed, list))
        if made == "hardlink":
            chk("硬链接刷新后仍可读且内容为新",
                os.path.isfile(lnk) and open(lnk, "rb").read() == b"y" * 128)
finally:
    import shutil; shutil.rmtree(tmpa, ignore_errors=True)

print("\n" + "=" * 70)
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
if FAILS:
    print("失败明细：")
    for n, d in FAILS:
        print("  - %s   %s" % (n, d))
print("=" * 70)
sys.exit(0 if FAIL == 0 else 1)
