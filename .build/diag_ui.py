# -*- coding: utf-8 -*-
"""临时：诊断前端到底加载成什么样。private_mode 避免 Edge 用户数据目录残留干扰。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
APP = "http://127.0.0.1:17380"
OUT = os.path.join(ROOT, ".build", "diag_out.json")


def main():
    p = subprocess.Popen([PY, "-u", os.path.join(ROOT, "app", "server.py"),
                          "--no-browser", "--port", "17380"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out = {}
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(APP + "/api/status", timeout=2).read()
                break
            except Exception:
                time.sleep(0.3)

        import webview
        win = webview.create_window("diag", APP, width=1200, height=800)

        def drive():
            try:
                def js(c):
                    try:
                        return win.evaluate_js(c)
                    except Exception as ex:
                        return "ERR:" + str(ex)[:100]

                # 等首屏扫完
                for _ in range(50):
                    v = js("(document.getElementById('navMods')||{}).textContent||''")
                    if v and v != '–':
                        break
                    time.sleep(0.5)
                out["navMods"] = js("(document.getElementById('navMods')||{}).textContent||'(无)'")
                out["typeof_S_re"] = js("typeof (window.S && S.re)")
                out["typeof_rReassign"] = js("typeof rReassign")
                out["nav_tabs"] = js("Array.from(document.querySelectorAll('nav button[data-tab]')).map(function(b){return b.dataset.tab}).join(',')")
                out["view_len"] = js("(document.getElementById('view')||{}).innerHTML ? document.getElementById('view').innerHTML.length : 0")

                # 切到模型归属
                js("S.re.key=(window.S.mods&&S.mods[0]&&S.mods[0].key)||''")
                js("switchTab('reassign')")
                for _ in range(40):
                    if js("!!document.querySelector('#reBox')"):
                        break
                    time.sleep(0.5)
                time.sleep(1.5)
                out["rebox_exists"] = js("!!document.querySelector('#reBox')")
                out["rebox_len"] = js("(document.getElementById('reBox')||{}).innerHTML ? document.getElementById('reBox').innerHTML.length : 0")
                out["rebox_text"] = (js("(document.getElementById('reBox')||{}).textContent||'(空)'") or '')[:300]
                out["slot_pill"] = js("(document.querySelector('#reBox .kv .pill')||{}).textContent||'(无)'")
            except Exception as ex:
                out["drive_failed"] = "%s: %s" % (type(ex).__name__, ex)
            try:
                win.destroy()
            except Exception:
                pass

        webview.start(drive)
    finally:
        try:
            with open(OUT, "w", encoding="utf-8") as fp:
                json.dump(out, fp, ensure_ascii=False, indent=1)
        except Exception as e:
            print("写结果失败", e)
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
