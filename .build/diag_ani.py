import os, sys
# 项目根按脚本位置推断（.build 的上一级），不写死某份副本的绝对路径
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# 真实路径不写死，统一从 data/config.json 读（见同目录 _real_cfg.py）
import _real_cfg                                               # noqa: E402
CFG = _real_cfg.real_cfg()
if not CFG["game_dir"]:
    sys.exit("没有 game_dir，跑不了；先配好 data/config.json 或设 L4D2MS_CONFIG")
import core.scan as scan
from core.vpk import Vpk
res = scan.scan(CFG)
mods = scan.dicts_for_audit(res["mods"])
allp = set()
gp = set(Vpk(os.path.join(CFG["game_dir"], "pak01_dir.vpk")).list_files())
allp |= gp
for m in mods:
    if m.get("enabled"):
        allp |= set(m.get("_vpaths") or [])
anism = {p[:-4] for p in allp if p.endswith(".ani")}
print("game+enabled 里 .ani stem 数:", len(anism))
for s in ["models/xdreanims/bill_slot_023","models/xdreanims/francis_slot_023",
          "models/xdreanims/louis_slot_023","models/xdreanims/zoey_slot_023",
          "models/v_models/v_pumpshotgunt","models/weapons/melee/w_crowbar0",
          "models/props_lab/petridish01b","models/weapons/melee/v_frying_pen"]:
    print("  %-42s ani=%s" % (s, s in anism))
# 游戏本体里 ani 兄弟的 mdl：是不是全都没 vvd
gm = [p for p in gp if p.startswith("models/") and p.endswith(".mdl")]
no_vvd = [p for p in gm if (p[:-4] + ".vvd") not in gp]
with_ani = [p for p in no_vvd if (p[:-4] + ".ani") in gp]
print("\ngame mdl=%d, 无vvd=%d, 其中带ani=%d" % (len(gm), len(no_vvd), len(with_ani)))
print("无vvd又无ani的:", [x for x in no_vvd if x not in with_ani])
