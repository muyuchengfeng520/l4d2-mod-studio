import glob, os, sys
# 项目根按脚本位置推断（.build 的上一级），不写死某份副本的绝对路径
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from core import reassign
# 真实 addons 目录从 data/config.json 推（见同目录 _real_cfg.py）
import _real_cfg                                               # noqa: E402
addons = _real_cfg.addons_dir(_real_cfg.real_cfg())
if not addons or not os.path.isdir(addons):
    sys.exit("没有可用的 addons 目录，跑不了；先配好 data/config.json 或设 L4D2MS_CONFIG")
vpks = sorted(glob.glob(os.path.join(addons, "*.vpk")))
hit = 0
for p in vpks:
    try:
        d = reassign.detect(p)
    except Exception as ex:
        print("EXC", os.path.basename(p), ex); continue
    if d.get("ok"):
        hit += 1
        if hit <= 6:
            print("OK  %-40s cur=%-10s internal=%-10s mismatch=%-5s err=%r"
                  % (os.path.basename(p)[:40], d.get("current"), d.get("internal") or "-",
                     d.get("mismatch"), d.get("internal_error")))
print("---")
print("扫描 %d 个 vpk，识别为生还者替换 %d 个" % (len(vpks), hit))
