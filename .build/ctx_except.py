# 把每处 except Exception: pass 的上下文打出来（前后各 6 行）
import io, os, re, sys
targets = {
 "app/api.py": [963, 1019],
 "core/acquire.py": [395, 809, 863, 927, 1503, 1507],
 "core/ai.py": [238, 494, 707],
 "core/paths.py": [175],
 "core/reassign.py": [132, 216],
 "core/safety.py": [149],
 "desktop.py": [53, 101, 111, 126, 393],
}
for rel, lns in targets.items():
    src = open(rel, "rb").read().decode("utf-8", "replace").splitlines()
    print("="*90)
    print(rel)
    for ln in lns:
        a = max(0, ln-7); b = min(len(src), ln+2)
        print("  --- L%d ---" % ln)
        for i in range(a, b):
            print("   %5d | %s" % (i+1, src[i]))
        print()
