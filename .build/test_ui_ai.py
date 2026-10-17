# -*- coding: utf-8 -*-
"""临时：驱动真实窗口跑一遍 AI 复核的界面流程（不是只看接口）。
放在 .build 下，验完即删。用 .build/venv 里的 python 跑（需要 pywebview）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = "http://127.0.0.1:17310"
fails = []


def check(label, cond, extra=""):
    print("  [%s] %s %s" % ("OK" if cond else "!!", label, extra))
    if not cond:
        fails.append(label)


def post(path, body):
    req = urllib.request.Request(APP + path, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    procs = []
    mocks = [(18011, "normal"), (18012, "slow"), (18013, "xss")]
    for port, mode in mocks:
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, ".build", "mock_llm.py"),
                              str(port), mode],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
    app = subprocess.Popen([sys.executable, "-u", os.path.join(ROOT, "app", "server.py"),
                            "--no-browser", "--port", "17310"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(app)
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(APP + "/api/status", timeout=2).read()
                break
            except Exception:
                time.sleep(0.3)
        time.sleep(1.0)
        post("/api/ai/config", {"enabled": True, "base_url": "127.0.0.1:18011", "model": "", "batch_cases": 4})

        import webview
        win = webview.create_window("ui-test", APP, width=1500, height=950)
        result = {}

        def drive():
            time.sleep(3.0)

            def js(code):
                return win.evaluate_js(code)

            # --- 等首屏扫完 ---
            for _ in range(40):
                if js("(document.getElementById('navMods')||{}).textContent||''") not in ("", "–"):
                    break
                time.sleep(0.5)
            print("  首屏 mod 数:", js("document.getElementById('navMods').textContent"))

            # --- 打开设置，看 AI 卡片在不在 ---
            js("openSettings()")
            time.sleep(0.8)
            result["setting_fields"] = js(
                "['aiEnabled','aiPreset','aiBase','aiModel','aiKey','aiBatch','aiMax','aiTimeout']"
                ".map(function(i){return i+'='+(document.getElementById(i)?'有':'缺')}).join(' ')")
            result["priv_tag"] = js("(document.getElementById('aiPrivTag')||{}).textContent||'(无)'")
            result["preset_opts"] = js("document.getElementById('aiPreset').options.length")
            js("closeDrawer()")
            time.sleep(0.3)

            # --- 切到冲突页 ---
            js("S.audit=null; S.auditTab='conflicts'; rAudit()")
            for _ in range(60):
                if js("!!document.querySelector('#aiCard')"):
                    break
                time.sleep(0.5)
            result["ai_card"] = js("(document.querySelector('#aiCard h3')||{}).textContent||'(无)'")
            result["has_start_btn"] = js(
                "Array.from(document.querySelectorAll('#aiCard button')).map(function(b){return b.textContent.trim()}).join('|')")

            # --- 点「开始复核」（普通 mock，很快） ---
            js("aiStart()")
            result["running_seen"] = js("(document.querySelector('#aiCard .aibar')?'有进度条':'无进度条')")
            for _ in range(90):
                st = js("(S.ai.job&&S.ai.job.state)||'?'")
                if st in ("done", "error", "cancelled"):
                    break
                time.sleep(0.6)
            result["job_state"] = js("(S.ai.job&&S.ai.job.state)||'?'")
            result["rows"] = js("document.querySelectorAll('#aiCard .airow').length")
            result["risk_pills"] = js(
                "Array.from(document.querySelectorAll('#aiCard .risk')).map(function(e){return e.textContent.trim()}).join(',')")
            result["first_what"] = js(
                "(document.querySelector('#aiCard .airow .d')||{}).textContent||''")
            result["overall"] = js("(function(){var n=document.querySelector('#aiCard .note');return n?n.textContent.slice(0,80):'(无)'})()")

            # --- 换成慢 mock，专门验进度条真的会出现 ---
            js("fetch('/api/ai/config',{method:'POST',headers:{'Content-Type':'application/json'},"
               "body:JSON.stringify({enabled:true,base_url:'127.0.0.1:18012',batch_cases:1})})"
               ".then(function(){S.ai.cfg.base_url='http://127.0.0.1:18012/v1'})")
            time.sleep(1.2)
            js("S.ai.job=null; renderAiCard()")
            time.sleep(0.4)
            js("aiStart()")
            seen_bar, seen_spin = False, False
            for _ in range(24):                      # 最多看 12 秒
                time.sleep(0.5)
                if js("!!document.querySelector('#aiCard .aibar')"):
                    seen_bar = True
                if js("!!document.querySelector('#aiCard .ailog')"):
                    seen_spin = True
                st = js("(S.ai.job&&S.ai.job.state)||'?'")
                if st in ("done", "error", "cancelled"):
                    break
            result["progress_bar"] = seen_bar
            result["log_box"] = seen_spin
            # 顺手把「取消」也验了：慢 mock 每批 3 秒，够时间点取消
            js("aiCancel()")
            for _ in range(40):
                st = js("(S.ai.job&&S.ai.job.state)||'?'")
                if st in ("cancelled", "done", "error"):
                    break
                time.sleep(0.5)
            result["slow_job_state"] = js("(S.ai.job&&S.ai.job.state)||'?'")
            result["cancel_btn_gone"] = js("!Array.from(document.querySelectorAll('#aiCard button'))"
                                           ".some(function(b){return b.textContent.indexOf('取消')>=0})")

            # --- 换成 XSS mock，验模型输出有没有被转义（安全项） ---
            js("fetch('/api/ai/config',{method:'POST',headers:{'Content-Type':'application/json'},"
               "body:JSON.stringify({enabled:true,base_url:'127.0.0.1:18013',batch_cases:4})})"
               ".then(function(){S.ai.cfg.base_url='http://127.0.0.1:18013/v1'})")
            time.sleep(1.2)
            js("S.ai.job=null; renderAiCard()")
            time.sleep(0.4)
            js("aiStart()")
            for _ in range(60):
                st = js("(S.ai.job&&S.ai.job.state)||'?'")
                if st in ("done", "error", "cancelled"):
                    break
                time.sleep(0.5)
            result["xss_state"] = js("(S.ai.job&&S.ai.job.state)||'?'")
            # 关键断言：模型吐的 <img>/<iframe> 不能变成真 DOM 节点，也不能执行
            result["xss_injected_img"] = js("!!document.querySelector('#aiCard .airow .d img')")
            result["xss_injected_iframe"] = js("!!document.querySelector('#aiCard .airow iframe')")
            result["xss_executed"] = js("(typeof window.__XSS__!=='undefined')||(typeof window.__XSS2__!=='undefined')")
            result["xss_shows_text"] = js(
                "(document.querySelector('#aiCard .airow .d')||{}).textContent||''")
            result["xss_innerhtml_has_entity"] = js(
                "(function(){var d=document.querySelector('#aiCard .airow .d');"
                "return d? (d.innerHTML.indexOf('&lt;img')>=0) : false})()")

            # ⚠️ 必须自己关窗：webview.start() 会一直阻塞到窗口被销毁，
            #    忘了这一步整个测试就卡死在这里（刚踩过）。
            try:
                win.destroy()
            except Exception as ex:
                print("关窗失败:", ex)

        try:
            webview.start(drive)
        finally:
            # 结果落盘，不指望管道缓冲
            with open(os.path.join(ROOT, ".build", "ui_ai_result.json"), "w", encoding="utf-8") as fp:
                json.dump(result, fp, ensure_ascii=False, indent=1)

        print()
        print("=" * 62)
        check("设置里有全部 AI 字段", result.get("setting_fields", "").count("=有") == 8,
              result.get("setting_fields", ""))
        check("预设下拉有选项", (result.get("preset_opts") or 0) >= 8, "%s 个" % result.get("preset_opts"))
        check("隐私标识已渲染", "本机" in (result.get("priv_tag") or ""), result.get("priv_tag"))
        check("冲突页有 AI 卡片", "AI 冲突复核" in (result.get("ai_card") or ""), result.get("ai_card"))
        check("有开始复核按钮", "开始复核" in (result.get("has_start_btn") or ""), result.get("has_start_btn"))
        check("任务跑完", result.get("job_state") == "done", str(result.get("job_state")))
        check("渲染出结果行", (result.get("rows") or 0) > 0, "%s 行" % result.get("rows"))
        check("风险 badge 有中文", "高危" in (result.get("risk_pills") or ""), result.get("risk_pills"))
        check("结果正文非空", len(result.get("first_what") or "") > 4, (result.get("first_what") or "")[:70])
        check("整体结论已渲染", "模拟整体结论" in (result.get("overall") or ""), (result.get("overall") or "")[:70])
        check("慢模型时出现进度条", bool(result.get("progress_bar")))
        check("慢模型时出现实时日志框", bool(result.get("log_box")))
        check("点取消后状态变 cancelled", result.get("slow_job_state") == "cancelled",
              str(result.get("slow_job_state")))
        check("取消后按钮变回「重新复核」", result.get("cancel_btn_gone") is True)
        print("  --- 模型输出转义（安全项）---")
        check("模型吐的 <img> 没变成真节点", result.get("xss_injected_img") is False)
        check("模型吐的 <iframe> 没变成真节点", result.get("xss_injected_iframe") is False)
        check("onerror/script 没被执行", result.get("xss_executed") is False)
        check("内联 HTML 被转成实体", result.get("xss_innerhtml_has_entity") is True)
        check("文本照常可见（只是当文本看）",
              "带标签的判定" in (result.get("xss_shows_text") or ""),
              (result.get("xss_shows_text") or "")[:60])
        print("=" * 62)
        print("失败项：", fails if fails else "无，界面流程全通")
        return 1 if fails else 0
    finally:
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=6)
            except Exception:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
