# -*- coding: utf-8 -*-
"""临时：端到端测 AI 复核链路（含故障模式与隐私边界）。放在 .build 下，验完即删。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
APP = "http://127.0.0.1:17300"
MOCKS = {}          # port -> mode
procs = []


def free_port(p):
    raw = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True).stdout
    return not any(l.split()[3] == "LISTENING" and l.split()[1].endswith(":%d" % p)
                   for l in raw.decode("utf-8", "replace").splitlines() if len(l.split()) >= 5)


def post(path, body=None):
    req = urllib.request.Request(APP + path,
                                 data=json.dumps(body or {}).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        return {"ok": False, "http": ex.code, "error": ex.read().decode("utf-8")[:300]}


def get(path):
    try:
        with urllib.request.urlopen(APP + path, timeout=180) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        return {"ok": False, "http": ex.code, "error": ex.read().decode("utf-8")[:300]}


def wait_job(jid, limit=180):
    t0 = time.time()
    while time.time() - t0 < limit:
        st = get("/api/ai/job?id=" + jid)
        if not st.get("ok") or st.get("state") != "running":
            return st
        time.sleep(0.4)
    return {"ok": False, "error": "轮询超时"}


def start_mocks():
    for port, mode in [(18001, "normal"), (18002, "slow"), (18003, "badjson"),
                       (18004, "trace"), (18005, "auth"), (18006, "boom")]:
        if not free_port(port):
            print("  !! 端口 %d 已被占，跳过该 mock" % port)
            continue
        p = subprocess.Popen([PY, "-u", os.path.join(ROOT, ".build", "mock_llm.py"), str(port), mode],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        MOCKS[port] = mode
    time.sleep(1.5)


def set_ai(**kw):
    kw.setdefault("enabled", True)
    return post("/api/ai/config", kw)


def main():
    app = subprocess.Popen([PY, "-u", os.path.join(ROOT, "app", "server.py"),
                            "--no-browser", "--port", "17300"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(app)
    for _ in range(80):
        try:
            urllib.request.urlopen(APP + "/api/status", timeout=2).read()
            break
        except Exception:
            time.sleep(0.3)
    start_mocks()
    fails = []

    def check(label, cond, extra=""):
        print("  [%s] %s %s" % ("OK" if cond else "!!", label, extra))
        if not cond:
            fails.append(label)

    print("=" * 66)
    print("用例 1  保存配置 + 密钥加密落盘")
    r = set_ai(base_url="127.0.0.1:18001", model="", api_key="super-secret-key-XYZ")
    check("保存成功", r.get("ok"), json.dumps(r.get("config", {}), ensure_ascii=False)[:120])
    cfg = r.get("config", {})
    check("地址被规整（补 /v1）", cfg.get("base_url") == "http://127.0.0.1:18001/v1", cfg.get("base_url"))
    check("界面拿不到密钥明文", "api_key" not in cfg and cfg.get("api_key_set") is True)
    check("识别为本机端点", cfg.get("is_local") is True)
    raw = open(os.path.join(ROOT, "data", "ai.json"), "rb").read().decode("utf-8")
    check("落盘文件里没有明文密钥", "super-secret-key-XYZ" not in raw)
    saved = json.loads(raw)
    check("落盘的是密文", bool(saved.get("api_key_enc")) and len(saved["api_key_enc"]) > 40)

    print()
    print("用例 2  连通性自检")
    t = post("/api/ai/test", {})
    check("连通", t.get("ok"), "耗时 %ss" % t.get("latency"))
    check("拿到模型列表", t.get("models") == ["mock-large-32b", "mock-small-7b"], str(t.get("models")))
    check("没填模型名时自动挑一个", bool(t.get("model")), t.get("model"))

    print()
    print("用例 3  正常复核（走真实冲突数据）")
    r = post("/api/ai/analyze", {"limit": 8})
    if not r.get("ok"):
        check("启动任务", False, str(r))
    else:
        print("      任务 %s：%d 组 / %d 批" % (r["job"], r["cases"], r["batches"]))
        st = wait_job(r["job"])
        check("任务完成", st.get("state") == "done", "state=%s err=%s" % (st.get("state"), st.get("error")[:120]))
        res = st.get("results") or []
        check("结果条数与案例数一致", len(res) == r["cases"], "%d vs %d" % (len(res), r["cases"]))
        check("每条都带 dir/kind/mods", all(x.get("dir") and x.get("mods") for x in res))
        risks = [x["risk"] for x in res]
        check("风险等级都在允许集合内", set(risks) <= {"high", "medium", "low", "none", "unknown"}, str(risks))
        check("按风险排过序（高危在前）", risks == sorted(risks, key=lambda k: {"high": 0, "medium": 1, "low": 2, "none": 3, "unknown": 4}[k]))
        check("拿到了整体结论", bool(st.get("overall")), (st.get("overall") or "")[:60])
        check("进度计数归位", st.get("done") == st.get("total") == r["cases"])
        print("      首条示例:", json.dumps(res[0], ensure_ascii=False)[:200] if res else "(空)")

    print()
    print("用例 4  隐私边界：发给模型的 payload 里有没有本机磁盘路径")
    set_ai(base_url="127.0.0.1:18004")
    r = post("/api/ai/analyze", {"limit": 6})
    st = wait_job(r["job"]) if r.get("ok") else {}
    blob = json.dumps(st, ensure_ascii=False)
    check("trace 模式生效（探针有回话）", "TRACE 命中敏感串" in blob, "")
    # 探针把命中项原样带回来了；这里断言的是**磁盘路径类标记一个都不许有**。
    # （'求生之路' 会命中，因为它是本工具 system prompt 里的游戏名，不是用户数据。）
    leaked = [w for w in ("E:\\", "C:\\Users", "SteamLibrary", "最新版") if w in blob]
    check("磁盘路径 / 仓库名 未进入 payload", not leaked, "泄漏项: %s" % (leaked or "无"))
    check("游戏名命中属预期（来自工具自身提示词）", "求生之路" in blob)

    print()
    print("用例 5  模型返回非 JSON 时的降级")
    set_ai(base_url="127.0.0.1:18003")
    r = post("/api/ai/analyze", {"limit": 4})
    st = wait_job(r["job"]) if r.get("ok") else {}
    check("任务仍完成（没崩）", st.get("state") == "done", str(st.get("state")))
    risks = [x["risk"] for x in (st.get("results") or [])]
    check("降级标成 unknown 而非臆造", risks and set(risks) <= {"unknown"}, str(risks))
    print("      降级说明:", (st.get("results") or [{}])[0].get("what", "")[:80])

    print()
    print("用例 6  鉴权失败")
    set_ai(base_url="127.0.0.1:18005", api_key="wrong-key")
    t = post("/api/ai/test", {})
    check("自检报错", not t.get("ok"))
    check("错误里提到 401 且给了人话", "401" in (t.get("error") or "") and "API Key" in (t.get("error") or ""),
          (t.get("error") or "")[:110])

    print()
    print("用例 7  服务不可达")
    set_ai(base_url="127.0.0.1:18999")
    t = post("/api/ai/test", {})
    check("自检报错", not t.get("ok"))
    check("提示连不上", "连不上" in (t.get("error") or ""), (t.get("error") or "")[:110])
    r = post("/api/ai/analyze", {"limit": 3})
    st = wait_job(r["job"]) if r.get("ok") else {}
    check("分析任务以 error 收场并给出人话",
          st.get("state") == "error" and "测试连接" in (st.get("error") or ""),
          (st.get("error") or "")[:110])

    print()
    print("用例 8  远程端点的外发确认闸门")
    set_ai(base_url="https://api.deepseek.com/v1")
    r = post("/api/ai/analyze", {"limit": 3})
    check("未确认时被拦下", r.get("ok") is False and r.get("need_remote_ack") is True)
    check("错误文案说清会发什么", "发送" in (r.get("error") or "") and "资源路径" in (r.get("error") or ""),
          (r.get("error") or "")[:130])
    check("识别出主机名", r.get("host") == "api.deepseek.com", str(r.get("host")))

    print()
    print("用例 9  取消")
    set_ai(base_url="127.0.0.1:18002")     # slow 模式，每批 3 秒
    r = post("/api/ai/analyze", {"limit": 8})
    if r.get("ok"):
        time.sleep(4.5)                     # 等第一批跑完，卡在第二批
        post("/api/ai/cancel", {"id": r["job"]})
        st = wait_job(r["job"], limit=40)
        check("状态为 cancelled", st.get("state") == "cancelled", str(st.get("state")))
        check("保留了已完成的部分", len(st.get("results") or []) > 0,
              "%d 条" % len(st.get("results") or []))
    else:
        check("启动任务", False, str(r))

    print()
    print("用例 10  开关关掉后拒绝复核")
    set_ai(base_url="127.0.0.1:18001", enabled=False)
    r = post("/api/ai/analyze", {"limit": 3})
    check("被拒绝并给出指路", r.get("ok") is False and "设置" in (r.get("error") or ""),
          (r.get("error") or "")[:110])

    print()
    print("=" * 66)
    print("失败项：%s" % (fails if fails else "无，全部通过"))
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=6)
            except Exception:
                p.kill()
    sys.exit(code)
