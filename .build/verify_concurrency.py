import os, sys, threading, tempfile, shutil
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)

# 必须在 import core.safety 之前设好，否则它在 import 期就把 data/ 建在项目里。
# （原先只靠 import 后改写 safety.DATA_DIR 等常量来隔离，但 import 那一刻
#   makedirs 已经跑过、audit.log 也可能已经写过一行，仍会留下痕迹。）
import atexit as _atexit
_WORK = tempfile.mkdtemp(prefix="l4d2ms-conc-")
_atexit.register(shutil.rmtree, _WORK, ignore_errors=True)
os.environ["L4D2MS_WORK_ROOT"] = _WORK
from core import safety

tmp = tempfile.mkdtemp(prefix="l4d2-conc-")
safety.DATA_DIR = os.path.join(tmp, "data")
safety.BACKUP_DIR = os.path.join(safety.DATA_DIR, "backup")
safety.STAGING_DIR = os.path.join(safety.DATA_DIR, "staging")
safety.CACHE_DIR = os.path.join(safety.DATA_DIR, "cache")
safety.AUDIT_LOG = os.path.join(safety.DATA_DIR, "audit.log")
for _d in (safety.DATA_DIR, safety.BACKUP_DIR, safety.STAGING_DIR, safety.CACHE_DIR):
    os.makedirs(_d, exist_ok=True)

from core import manager

ok = fail = 0
def chk(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  [OK]   %s" % name)
    else: fail += 1; print("  [FAIL] %s %s" % (name, extra))

def make_addonlist(path, keys):
    lines = ['"AddonList"', "{", "\t// 玩家自己的注释，必须保留"]
    for k in keys: lines.append('\t"%s"\t\t"0"' % k)
    lines.append("}")
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")

try:
    al = os.path.join(tmp, "addonlist.txt")
    keys = ["m%03d.vpk" % i for i in range(200)]

    print("=== A 8 线程并发 set_enabled（写不同 key）===")
    make_addonlist(al, keys)
    errs = []
    def worker(t):
        for i in range(25):
            k = "m%03d.vpk" % (t * 25 + i)
            try:
                r = manager.set_enabled(al, {k: True})
                if not r.get("ok"): errs.append((k, r.get("error")))
            except Exception as ex:
                errs.append((k, "%s: %s" % (type(ex).__name__, ex)))
    ts = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in ts: t.start()
    for t in ts: t.join()
    mapping = manager.scan.read_addonlist(al)
    on = [k for k, v in mapping.items() if v]
    chk("无异常", not errs, str(errs[:3]))
    chk("200 条一条不少", len(mapping) == 200, "实际 %d" % len(mapping))
    chk("200 条全部启用（无丢更新）", len(on) == 200, "启用 %d 条" % len(on))
    chk("玩家注释被保留", "玩家自己的注释" in open(al, encoding="utf-8").read())

    print("=== B 3 线程启用 + 1 线程隔离（10 项，≤ BATCH_LIMIT）并发混跑 ===")
    make_addonlist(al, keys)
    addons = os.path.join(tmp, "addons"); os.makedirs(addons)
    for i in range(10):
        with open(os.path.join(addons, keys[i]), "wb") as fp: fp.write(b"FAKE" + bytes(64))

    def enabler(t):
        for i in range(25):
            manager.set_enabled(al, {"m%03d.vpk" % (100 + t * 25 + i): True})
    iso_res = {}
    def isolator():
        iso_res.update(manager.isolate_mods(
            al, [{"key": k, "addon_path": os.path.join(addons, k)} for k in keys[:10]],
            isolate_repo=False))

    ts = [threading.Thread(target=enabler, args=(t,)) for t in range(3)] + \
         [threading.Thread(target=isolator)]
    for t in ts: t.start()
    for t in ts: t.join()

    mapping = manager.scan.read_addonlist(al)
    on = [k for k, v in mapping.items() if v]
    print("    隔离返回: ok=%r moved=%d failed=%d dangling=%d remaining=%r"
          % (iso_res.get("ok"), len(iso_res.get("moved") or []),
             len(iso_res.get("failed") or []), len(iso_res.get("dangling") or []),
             iso_res.get("remaining")))
    chk("剩 190 条（10 条被隔离摘掉）", len(mapping) == 190, "实际 %d" % len(mapping))
    chk("75 条被启用（3×25，无丢更新）", len(on) == 75, "启用 %d 条" % len(on))
    chk("被隔离的 10 条已从 addonlist 消失",
        all(k not in mapping for k in keys[:10]),
        "仍在: %s" % [k for k in keys[:10] if k in mapping][:5])
    chk("其余 190 条一条不少", all(k in mapping for k in keys[10:]))
    chk("注释仍在", "玩家自己的注释" in open(al, encoding="utf-8").read())
    n_files = sum(len(f) for _, _, f in os.walk(safety.STAGING_DIR))
    chk("隔离区里有 10 个 vpk + 10 个边车", n_files == 20, "实际 %d 个文件" % n_files)
    chk("addons 里那 10 个已搬走",
        all(not os.path.exists(os.path.join(addons, k)) for k in keys[:10]))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n并发结果：通过 %d 项，失败 %d 项" % (ok, fail))
sys.exit(0 if fail == 0 else 1)
