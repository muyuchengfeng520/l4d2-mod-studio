import io, os, sys
# 项目根按脚本位置推断（.build 的上一级），不写死某份副本的绝对路径
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)
from app import http_util

class FakeH:
    def __init__(self, path="/"):
        self.path = path; self.headers = {}; self.wfile = io.BytesIO()
        self.code = None; self.close_connection = False
        self.server = type("S", (), {"server_address": ("127.0.0.1", 17246)})()
    def send_response(self, c, message=None): self.code = c
    def send_header(self, k, v): pass
    def end_headers(self): pass

web = os.path.join(ROOT, "web")
h = FakeH("/index.html")
r = http_util.serve_static(h, "/index.html", web)
body = h.wfile.getvalue()
print("return =", r, " code =", h.code, " bytes =", len(body))
print("head:", body[:120])
print("contains L4D2:", b"L4D2" in body)
# 现在对照 web/index.html 的真实内容
raw = open(os.path.join(web, "index.html"), "rb").read()
print("index.html 实际字节:", len(raw), " 一致:", raw == body)
print("index.html 里有 L4D2 吗:", b"L4D2" in raw)
