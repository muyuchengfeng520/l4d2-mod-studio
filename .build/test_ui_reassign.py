# -*- coding: utf-8 -*-
"""临时：在真实 pywebview 窗口里驱动「模型归属」全流程。放在 .build 下。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
APP = "http://127.0.0.1:17360"
fails = []
procs = []


def check(label, cond, extra=""):
    print("  [%s] %s %s" % ("OK" if cond else "!!", label, str(extra)[:110]))
    if not cond:
        fails.append(label)


def main():
    procs.append(subprocess.Popen([PY, "-u", os.path.join(ROOT, "app", "server.py"),
                                   "--no-browser", "--port", "17360"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(APP + "/api/status", timeout=2).read()
                break
            except Exception:
                time.sleep(0.3)
        time.sleep(1.0)

        import webview
        win = webview.create_window("L4D2 Mod Studio", APP, width=1400, height=900)
        R = {}

        def drive():
            time.sleep(3)

            def js(c):
                return win.evaluate_js(c)

            for _ in range(40):
                if js("(document.getElementById('navMods')||{}).textContent||''") not in ("", "–"):
                    break
                time.sleep(0.5)

            # 从 mod 详情跳过来（顺便验证抽屉入口）
            # key 动态取已扫到的第一个 mod，不写死某个真实 mod 名 ——
            # .build/ 会被同步进公开仓库，写死会让敏感扫描中止同步
            js("S.re.key=(window.S.scan&&S.scan.mods&&S.scan.mods[0]&&S.scan.mods[0].key)||''; switchTab('reassign')")
            for _ in range(60):
                if js("!!document.querySelector('#reBox .card')"):
                    break
                time.sleep(0.5)
            time.sleep(0.8)

            R["slot"] = js("(document.querySelector('#reBox .kv .pill')||{}).textContent||'(无)'")
            R["body_text"] = js("(document.querySelector('#reBox')||{}).textContent||''")
            R["chips"] = js("Array.from(document.querySelectorAll('#reBox [data-act=reTarget]')).map(function(e){return e.dataset.id}).join(',')")

            # 选 Bill
            js("rePickTarget('namvet')")
            for _ in range(60):
                if js("document.querySelectorAll('#rePlanBox .f').length>0"):
                    break
                time.sleep(0.5)
            time.sleep(0.6)
            R["plan_text"] = js("(document.getElementById('rePlanBox')||{}).textContent||''")
            R["rename_rows"] = js("document.querySelectorAll('#rePlanBox .grp .gb .f').length")
            R["has_buttons"] = js("Array.from(document.querySelectorAll('#rePlanBox button')).map(function(b){return b.textContent.trim()}).join('|')")

            # 执行：另存为新 VPK
            js("reApply('new')")
            time.sleep(1.2)
            R["confirm_shown"] = js("(document.getElementById('cfmOk')?'有确认框':'无确认框')")
            R["confirm_items"] = js("document.querySelectorAll('#drawerBody .f').length")
            js("cfmOk()")
            for _ in range(90):
                t = js("(document.getElementById('drawerTitle')||{}).textContent||''")
                if t == '改归属结果':
                    break
                time.sleep(0.6)
            time.sleep(1.0)
            R["result_title"] = js("(document.getElementById('drawerTitle')||{}).textContent||''")
            R["result_text"] = js("(document.getElementById('drawerBody')||{}).textContent||''")
            try:
                win.destroy()
            except Exception as ex:
                print("关窗失败:", ex)

        try:
            webview.start(drive)
        finally:
            with open(os.path.join(ROOT, ".build", "ui_reassign_result.json"), "w", encoding="utf-8") as fp:
                json.dump(R, fp, ensure_ascii=False, indent=1)
    finally:
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=6)
            except Exception:
                p.kill()

    print()
    print("=" * 60)
    check("当前槽位渲染出来了", R.get("slot") == "Coach", R.get("slot"))
    check("内部指向渲染出来了", "Zoey" in (R.get("body_text") or ""), "")
    check("mismatch 警告出现", "外部与内部不一致" in (R.get("body_text") or ""), "")
    check("目标 chip 有 7 个（排除自己）", len((R.get("chips") or "").split(",")) == 7, R.get("chips"))
    check("预演显示出改名清单", (R.get("rename_rows") or 0) >= 8, "%s 行" % R.get("rename_rows"))
    check("预演提到丢弃 lobby 头像", "丢弃" in (R.get("plan_text") or ""), "")
    check("预演说明内部引用不可改", "跳过" in (R.get("plan_text") or ""), "")
    check("有另存/原地两个按钮", "另存" in (R.get("has_buttons") or "") and "原地" in (R.get("has_buttons") or ""), R.get("has_buttons"))
    check("弹出确认框", R.get("confirm_shown") == "有确认框", R.get("confirm_shown"))
    check("确认框列出改名项", (R.get("confirm_items") or 0) > 0, "%s 项" % R.get("confirm_items"))
    check("执行后打开结果框", R.get("result_title") == "改归属结果", R.get("result_title"))
    check("结果里校验通过", "通过" in (R.get("result_text") or ""), (R.get("result_text") or "")[:150])
    print("=" * 60)
    print("失败项：", fails if fails else "无")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
