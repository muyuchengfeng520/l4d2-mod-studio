import re, io, os, sys
# 项目根按脚本位置推断（.build 的上一级），不写死某份副本的绝对路径
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)
from app import routes

for name, tbl in (("GET", routes.GET_ROUTES), ("POST", routes.POST_ROUTES)):
    pats = [rt.pattern for rt in tbl]
    dup = sorted({p for p in pats if pats.count(p) > 1})
    print("%-4s 共 %d 条, 表内重复: %s" % (name, len(pats), dup or "无"))

gp = {rt.pattern for rt in routes.GET_ROUTES}
pp = {rt.pattern for rt in routes.POST_ROUTES}
both = sorted(gp & pp)
print("GET+POST 双登记的路径:", both)
for p in both:
    g = [rt.handler.__name__ for rt in routes.GET_ROUTES if rt.pattern == p]
    q = [rt.handler.__name__ for rt in routes.POST_ROUTES if rt.pattern == p]
    print("  %-16s GET=%-18s POST=%s" % (p, g[0], q[0]))
