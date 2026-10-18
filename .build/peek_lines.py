import os
# 项目根按脚本位置推断（.build 的上一级），不写死某份副本的绝对路径
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
lines = open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8").read().splitlines()
targets = [729, 777, 779, 813, 821, 907, 916, 1309, 1368, 1383, 1384, 1392, 1393,
           1482, 1490, 1592, 1943, 1955, 2231, 2237, 963, 979, 1046, 1050]
for n in targets:
    print("%4d | %s" % (n, lines[n-1].rstrip()[:200]))
