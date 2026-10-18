import re, io, os, sys
p = r".build\verify_sec.py"
s = open(p, "r", encoding="utf-8").read()

# 1) serve_static 写的是 h.wfile，不是 h.sent
s = s.replace(
 'chk("正常读 index.html", h.code == 200 and b"L4D2" in h.sent)',
 'chk("正常读 index.html", h.code == 200 and b"L4D2" in h.wfile.getvalue())')

# 2) /api/scan、/api/ai/config 在 GET 与 POST 同时登记是设计如此（同表无重复才算错）
old = '''pats = [rt.pattern for rt in routes.GET_ROUTES] + [rt.pattern for rt in routes.POST_ROUTES]
chk("无重复路由 pattern", len(pats) == len(set(pats)),
    str([p for p in set(pats) if pats.count(p) > 1][:5]))
# GET 与 POST 不许有交叉（同一路径两种方法会互相遮蔽）
gp = {rt.pattern for rt in routes.GET_ROUTES}
pp = {rt.pattern for rt in routes.POST_ROUTES}
chk("GET/POST 路由无交叉", not (gp & pp), str(gp & pp))'''
new = '''for _name, _tbl in (("GET", routes.GET_ROUTES), ("POST", routes.POST_ROUTES)):
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
chk("GET/POST 同名路径必须是不同处理函数", not _bad, str(_bad))'''
assert old in s, "clip not found"
s = s.replace(old, new)
open(p, "w", encoding="utf-8").write(s)
print("patched", p)
