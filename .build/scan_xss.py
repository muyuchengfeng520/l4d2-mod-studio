import io, os, re, sys
# 项目根按脚本位置推断（.build 的上一级），不写死某份副本的绝对路径
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8").read()
lines = src.splitlines()

# 找出所有 HTML 模板插值 ${...}，检查里面的表达式是否被 esc() 包裹
hits = []
for i, ln in enumerate(lines, 1):
    for m in re.finditer(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", ln):
        expr = m.group(1)
        if "esc(" in expr or "fmtSize" in expr or "num(" in expr:
            continue
        # 纯数字/布尔/固定字符串表达式通常安全
        if re.fullmatch(r"\s*[A-Za-z_$][\w$.]*\s*", expr):
            hits.append((i, expr.strip(), ln.strip()[:130]))

print("未被 esc() 包裹的模板插值共 %d 处：" % len(hits))
seen = {}
for i, expr, ctx in hits:
    seen.setdefault(expr, []).append(i)
for expr, ls in sorted(seen.items(), key=lambda kv: -len(kv[1])):
    print("  %-42s 行 %s" % (expr[:42], ls[:8]))
