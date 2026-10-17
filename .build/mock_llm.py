# -*- coding: utf-8 -*-
"""临时：OpenAI 兼容的模拟模型服务，用来测 AI 复核链路（含各种故障模式）。

启动： python mock_llm.py <port> <mode>
mode: normal | slow | badjson | trace | auth | boom
放在 .build 下，验完即删。
"""
from __future__ import annotations

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = "normal"
PORT = 18001
NEED_KEY = ""


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if MODE == "auth":
            # 真实的 OpenAI 兼容服务在 /models 上也要鉴权，mock 必须一起模拟，
            # 否则「测试连接」会假通过，401 路径根本测不到。
            if (self.headers.get("Authorization") or "") != "Bearer " + NEED_KEY:
                return self._send(401, {"error": {"message": "Incorrect API key provided"}})
        if self.path.endswith("/models"):
            if MODE == "boom":
                return self._send(500, {"error": {"message": "模拟的服务端崩溃"}})
            return self._send(200, {"object": "list", "data": [
                {"id": "mock-small-7b", "object": "model"},
                {"id": "mock-large-32b", "object": "model"},
            ]})
        return self._send(404, {"error": {"message": "not found: %s" % self.path}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        try:
            req = json.loads(raw)
        except Exception:
            req = {}

        if MODE == "auth":
            if (self.headers.get("Authorization") or "") != "Bearer " + NEED_KEY:
                return self._send(401, {"error": {"message": "Incorrect API key provided"}})
        if MODE == "boom":
            return self._send(503, {"error": {"message": "模拟的模型服务不可用"}})

        # 从用户消息里抠出案例 id，保证返回的 id 与输入一一对应
        prompt = ""
        for m in req.get("messages", []):
            prompt += str(m.get("content") or "")
        ids = re.findall(r'"id"\s*:\s*"(c\d+)"', prompt)

        if MODE == "slow":
            time.sleep(3)
        if MODE == "trace":
            # 探测服务端到底收到了什么（隐私边界验证用）
            hits = [w for w in ("E:\\", "C:\\Users", "求生之路", "SteamLibrary")
                    if w.lower() in prompt.lower()]
            return self._send(200, {"choices": [{"message": {"content": json.dumps(
                {"results": [], "overall": "TRACE 命中敏感串: %s" % (hits or "无"),
                 "ids": ids, "prompt_len": len(prompt)}, ensure_ascii=False)}}]})

        if MODE == "badjson":
            content = "好的！以下是我的分析结果（忘了按 JSON 格式输出）：\n这个是脚本冲突，比较严重。"
        elif MODE == "xss":
            # 模拟「模型吐出了 HTML」——前端必须当不可信文本转义后再插进 DOM
            rows = []
            for cid in dict.fromkeys(ids):
                rows.append({
                    "id": cid, "risk": "high",
                    "what": '<img src=x onerror="window.__XSS__=1">带标签的判定 <b>粗体</b>',
                    "lose": "<script>window.__XSS2__=1</script>被吃掉的",
                    "advice": "<iframe src=javascript:1></iframe>建议：别信模型输出的 HTML",
                })
            content = json.dumps({"results": rows, "overall": "<b>整体</b>结论带标签"}, ensure_ascii=False)
        else:
            risk_cycle = ["high", "medium", "low", "none"]
            rows = []
            for i, cid in enumerate(dict.fromkeys(ids)):
                rows.append({
                    "id": cid,
                    "risk": risk_cycle[i % 4],
                    "what": "模拟判定：%s 这组会被覆盖（含 } 和 { 的字符串测试）" % cid,
                    "lose": None if i % 4 == 3 else "MOD-%d 的贴图会失效" % (i + 1),
                    "advice": "模拟建议：保留列表靠后的那个，或者换顺序实测",
                })
            content = json.dumps({"results": rows, "overall": "模拟整体结论：共 %d 组。" % len(rows)},
                                 ensure_ascii=False)

        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: [DONE]\n\n")
            return
        time.sleep(0.15)
        return self._send(200, {
            "id": "chatcmpl-mock", "object": "chat.completion",
            "created": int(time.time()), "model": req.get("model") or "mock-small-7b",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": len(prompt) // 4, "completion_tokens": len(content) // 4},
        })


if __name__ == "__main__":
    PORT = int(sys.argv[1])
    MODE = sys.argv[2] if len(sys.argv) > 2 else "normal"
    NEED_KEY = sys.argv[3] if len(sys.argv) > 3 else "secret-key-abc"
    ThreadingHTTPServer.allow_reuse_address = False
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    srv.serve_forever()
