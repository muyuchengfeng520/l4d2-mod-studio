# -*- coding: utf-8 -*-
"""AI 冲突复核 —— 接一个 OpenAI 兼容的模型接口，让它判断冲突的实际影响。

**为什么要 AI**：`audit.py` 是规则引擎，它能机械地发现「同一条 VPK 内部路径
被多个 mod 同时提供」（本机 465 条）。但**「路径撞了」不等于「真的会出问题」**：

  - 两个 mod 在同一个目录下各自放**不同文件** → 目录聚合造成的假象，其实没事；
  - 两个 mod 替换**同一个模型**的 `.mdl`/`.vtx`/`.vvd` → 真冲突，只会有一个生效；
  - 两个 mod 覆盖同一个 `.nut` 脚本 → 功能级冲突，最该重视。

要分清这些，得读懂「这个 mod 是干嘛的」——那是语义判断，规则表写不完。
所以让模型来做，而规则引擎负责把**结构化的事实**喂给它。

隐私边界（重要）：
  - 只发送 **mod 标题、分类与标签、VPK 内部的资源路径、加载顺序**；
  - **不发送** VPK 内容、不发送游戏文件、不发送磁盘绝对路径
    （仓库目录、游戏目录、你的用户名都不会进 payload）。
  - 端点默认指向本机（LM Studio / Ollama），换远程服务时界面会明确标出「数据会出网」。

API Key 处理：用 Windows **DPAPI**（当前用户作用域）加密后存进 `data/ai.json`，
**不写明文**；换台机器或换个 Windows 账户都解不开，这是有意的。
"""
from __future__ import annotations

import base64
import ctypes
import datetime
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from ctypes import wintypes
from email.utils import parsedate_to_datetime

from . import safety

AI_CFG_PATH = os.path.join(safety.DATA_DIR, "ai.json")

# --------------------------------------------------------------------------- #
# 服务商预设。全部走 OpenAI 兼容协议，所以一套代码通吃。
# local=True 表示数据不出本机（界面会据此显示隐私标识）。
# --------------------------------------------------------------------------- #
PRESETS = [
    {"id": "lmstudio", "name": "LM Studio（本机）", "base_url": "http://127.0.0.1:1234/v1",
     "local": True, "needs_key": False,
     "hint": "在 LM Studio 里打开 Developer 标签 → Start Server；模型名留空会自动挑第一个已加载的"},
    {"id": "ollama", "name": "Ollama（本机）", "base_url": "http://127.0.0.1:11434/v1",
     "local": True, "needs_key": False,
     "hint": "先 ollama serve，并 ollama pull 一个模型（如 qwen2.5:7b）"},
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
     "local": False, "needs_key": True, "hint": "模型如 deepseek-chat"},
    {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1",
     "local": False, "needs_key": True, "hint": "模型如 gpt-4o-mini"},
    {"id": "moonshot", "name": "Moonshot / Kimi", "base_url": "https://api.moonshot.cn/v1",
     "local": False, "needs_key": True, "hint": "模型如 moonshot-v1-8k"},
    {"id": "zhipu", "name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "local": False, "needs_key": True, "hint": "模型如 glm-4-flash"},
    {"id": "dashscope", "name": "阿里通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "local": False, "needs_key": True, "hint": "模型如 qwen-plus"},
    {"id": "custom", "name": "自定义（OpenAI 兼容）", "base_url": "",
     "local": False, "needs_key": False, "hint": "任何提供 /chat/completions 的服务都能接"},
]

DEFAULT = {
    "enabled": False,
    "preset": "lmstudio",
    "base_url": "http://127.0.0.1:1234/v1",
    "model": "",
    "api_key_enc": "",       # DPAPI 加密 + base64；永远是密文
    "timeout": 120,          # 单次请求超时（秒）
    "temperature": 0.2,
    "batch_cases": 4,        # 一次请求塞几个冲突案例
    "max_cases": 12,         # 一轮分析最多复核几例
    "synthesize": True,      # 多批时是否再要一段整体总结
}

RISK_ORDER = {"high": 0, "medium": 1, "low": 2, "none": 3, "unknown": 4}
RISK_ZH = {"high": "高", "medium": "中", "low": "低", "none": "无", "unknown": "未知"}


# --------------------------------------------------------------------------- #
# 配置读写 + 密钥加密
# --------------------------------------------------------------------------- #
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    """调 Windows DPAPI。作用域是**当前用户**（不带 LOCAL_MACHINE 标志），
    别的账户或别的机器解不开。"""
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _BLOB()
    descr = ctypes.c_wchar_p()
    # ⚠️ 必须用 WinDLL(..., use_last_error=True)，不能再用 ctypes.windll.<dll>：
    #    windll 造出来的实例**没开 use_last_error**，CPython 就不会在每次调用后
    #    把线程错误码存进 ctypes 的内部槽位，于是下面 ctypes.get_last_error()
    #    永远返回 0 —— 报错里的「错误码」毫无诊断价值（真正有用的
    #    0x80070005 权限不足 / 0x8007000D 数据无效全被吞掉）。
    #    WinDLL 同样是 CDLL 子类，调用约定、函数签名都不变：
    #    DPAPI 语义与密文格式（base64(CryptProtectData 输出)）一字不改。
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None,
                                     0x1,  # CRYPTPROTECT_UI_FORBIDDEN
                                     ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), ctypes.byref(descr), None,
                                        None, None,
                                        0x1,  # CRYPTPROTECT_UI_FORBIDDEN
                                        ctypes.byref(blob_out))
    if not ok:
        # 从 WinDLL 实例取，才有真实错误码（见上面 use_last_error 的说明）
        raise OSError("DPAPI 调用失败（错误码 %d）" % ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)
        if descr:
            kernel32.LocalFree(descr)


def encrypt_key(plain: str) -> str:
    """明文 → DPAPI 密文 → base64 字符串。空串原样返回。"""
    if not plain:
        return ""
    return base64.b64encode(_dpapi(plain.encode("utf-8"), True)).decode("ascii")


def decrypt_key(enc: str) -> str:
    """base64 密文 → 明文。解不开（换了机器/账户）就返回空串，不抛。"""
    if not enc:
        return ""
    try:
        return _dpapi(base64.b64decode(enc), False).decode("utf-8")
    except Exception:
        return ""


def _safe_url(url: str) -> str:
    """把 URL 的 query / fragment 剥掉，只留 `scheme://host[:port]/path`。

    为什么要这个：`base_url` 是用户自由输入的，实际有人会把密钥塞进 query
    （如 `https://gw.example.com/v1?key=sk-xxx`）。而 base_url 会进
    `safety.log()` → 写进 `data/audit.log`，`/api/log` 又会把审计日志尾部
    整段回给前端 —— 不剥 query 等于把密钥落到磁盘上、还显示在界面上。
    注意：**只用于日志与界面展示**；真正发请求仍然用完整 URL（见 `_request`），
    带 query 的鉴权地址照常能用。
    """
    u = (url or "").strip()
    if not u:
        return ""
    try:
        p = urllib.parse.urlsplit(u)
    except Exception:
        return u.split("?", 1)[0].split("#", 1)[0]
    if not p.scheme or not p.netloc:
        # 用户可能只填了 "127.0.0.1:1234" 这种没有 scheme 的，退化成手工切
        return u.split("?", 1)[0].split("#", 1)[0]
    return "%s://%s%s" % (p.scheme, p.netloc, p.path or "")


# 最近一次见到过的密文。用途见 load()：坏文件里如果连 api_key_enc 那段都被
# 写烂了（截断、覆盖），正则也救不回来 —— 这时用进程内记忆兜底，
# 总好过用户下次随手保存一下就把密钥彻底抹掉。
_KEY_MEM = {"enc": ""}
_KEY_MEM_LOCK = threading.Lock()
# 密文是 base64（DPAPI 输出再 b64），不含引号也不含转义，所以从坏文本里
# 用正则直接抠这一段是安全的。
_KEY_RE = re.compile(r'"api_key_enc"\s*:\s*"([A-Za-z0-9+/=]+)"')


def _remember_key(enc: str) -> None:
    with _KEY_MEM_LOCK:
        _KEY_MEM["enc"] = enc or ""


def _remembered_key() -> str:
    with _KEY_MEM_LOCK:
        return _KEY_MEM["enc"]


def _salvage_key(text: str) -> str:
    """从「解析不了的配置原文」里抢救 api_key_enc。抠不到就返回空串。"""
    try:
        m = _KEY_RE.search(text or "")
    except Exception:
        return ""
    return m.group(1) if m else ""


def _quarantine_bad_cfg(text: str, err) -> str:
    """坏配置留证：另存为 `ai.json.bad`（已占用就带时间戳/序号，绝不覆盖旧的）。

    为什么不直接删掉重建：用户可能想手工救回来；留一份原文才谈得上追溯。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidates = [AI_CFG_PATH + ".bad",
                  "%s.bad.%s" % (AI_CFG_PATH, stamp)]
    candidates += ["%s.bad.%s.%d" % (AI_CFG_PATH, stamp, i) for i in range(1, 100)]
    for dst in candidates:
        if os.path.exists(dst):
            continue
        try:
            with open(dst, "w", encoding="utf-8") as fp:
                fp.write(text or "")
        except Exception as ex:
            safety.log("ai-config", "配置解析失败（%s），且坏文件另存失败：%s" % (err, ex),
                       level="ERROR", ok=False)
            return ""
        safety.log("ai-config", "配置解析失败（%s），坏文件已另存为 %s" % (err, os.path.basename(dst)),
                   level="ERROR", ok=False)
        return dst
    return ""


def _disk_key() -> str:
    """只为了「保住密钥」而从磁盘原文里抠密文：正常 JSON 走解析，坏文本走正则。"""
    if not os.path.isfile(AI_CFG_PATH):
        return ""
    try:
        with open(AI_CFG_PATH, "rb") as fp:
            text = fp.read().decode("utf-8", "replace")
    except OSError:
        return ""
    try:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("api_key_enc"), str):
            return data["api_key_enc"]
    except Exception:
        pass
    return _salvage_key(text)


def load() -> dict:
    """读配置。

    为什么要区分「文件不存在」与「解析失败」（原实现是一律 `except: pass`
    返回默认值）：`save()` 是「以 load() 为底」合并的 —— 一旦 ai.json 坏一次，
    load() 就静默返回 `api_key_enc=""` 的默认值，用户下次随便改个设置保存，
    磁盘上的密文就被空串覆盖，**API 密钥无声消失**（用户只会觉得「密钥怎么又没了」）。

    现在：文件不存在 = 正常（全新安装，返回默认）；解析失败 = 异常路径 ——
    坏文件另存 `ai.json.bad` 留证 + 写一条 ERROR 审计日志，再从原文里把
    `api_key_enc` 抢救回来，实在抢救不到才退回进程内记忆的密文。
    """
    cfg = dict(DEFAULT)
    if not os.path.isfile(AI_CFG_PATH):
        return cfg
    try:
        with open(AI_CFG_PATH, "rb") as fp:
            text = fp.read().decode("utf-8", "replace")
    except OSError as ex:
        safety.log("ai-config", "配置读取失败：%s" % ex, level="ERROR", ok=False)
        return cfg
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("配置根节点不是对象（%s）" % type(data).__name__)
    except Exception as ex:
        # 异常路径：留证 + 审计 + 抢救密钥，绝不静默当成默认值
        _quarantine_bad_cfg(text, ex)
        rescued = _salvage_key(text) or _remembered_key()
        if rescued:
            cfg["api_key_enc"] = rescued
        _remember_key(cfg["api_key_enc"])
        return cfg
    cfg.update(data)
    _remember_key(cfg.get("api_key_enc") or "")
    return cfg


def save(cfg: dict) -> None:
    """保存配置。

    关于密钥的两条规则（对应上面 load() 说的丢密钥坑）：
    1. 传入的 cfg **没有** `api_key_enc` 这个键 ⇒ 保留磁盘上原有的密文。
       真实调用路径就是这样：`app/api.py` 的 `/api/ai/config` 组出来的 patch
       里平时根本没有这个键（只有用户填了新 key、或点「清除密钥」时才有）。
       patch 里**显式**给 `api_key_enc=""`（= 用户主动清除）则照写空串，
       不能误判成「缺键」而把清除操作吃掉。
    2. `merged = load()` 已经把坏文件里的密文抢救进来了；这里再直读一次
       磁盘原文兜底，保证「密文只会被显式清除，不会被顺手抹掉」。
    """
    merged = load()
    patch = {k: v for k, v in cfg.items() if k in DEFAULT}
    has_key_field = "api_key_enc" in cfg
    merged.update(patch)
    if not has_key_field and not merged.get("api_key_enc"):
        cur = _disk_key()
        if cur:
            merged["api_key_enc"] = cur
    os.makedirs(os.path.dirname(AI_CFG_PATH), exist_ok=True)
    tmp = AI_CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(merged, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, AI_CFG_PATH)
    _remember_key(merged.get("api_key_enc") or "")
    # 审计日志里**只记是否设了密钥**，绝不记密文或明文；
    # base_url 走 _safe_url 剥掉 query —— 用户可能把 key 写在 query 里
    # （如 https://x/v1?key=sk-xxx），整串进 audit.log 就等于把密钥写盘。
    safety.log("ai-config", "base_url=%s model=%s key=%s enabled=%s"
               % (_safe_url(merged.get("base_url")), merged.get("model") or "(自动)",
                  "已设置" if merged.get("api_key_enc") else "无", merged.get("enabled")))


def public_cfg(cfg: dict) -> dict:
    """给界面看的版本：**不含密钥**，只告诉你设没设。

    base_url 同样剥掉 query / fragment：这份 dict 会被 `/api/ai/config`
    原样回给浏览器，也可能被写进任何日志，不能带密钥。
    """
    d = {k: v for k, v in cfg.items() if k != "api_key_enc"}
    if "base_url" in d:
        d["base_url"] = _safe_url(d.get("base_url"))
    d["api_key_set"] = bool(cfg.get("api_key_enc"))
    d["is_local"] = is_local(cfg.get("base_url", ""))
    d["preset_name"] = next((p["name"] for p in PRESETS if p["id"] == cfg.get("preset")), "自定义")
    return d


# --------------------------------------------------------------------------- #
# 地址判定与规整
# --------------------------------------------------------------------------- #
def _host_of(url: str) -> str:
    u = (url or "").split("://", 1)[-1]
    return u.split("/", 1)[0].split("@")[-1].split(":")[0].strip("[]").lower()


def is_local(url: str) -> bool:
    """数据出不出本机。除本机回环外，常见内网段也算「不出网」。"""
    h = _host_of(url)
    if not h:
        return False
    if h in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if h.startswith("192.168.") or h.startswith("10.") or h.startswith("169.254."):
        return True
    if h.startswith("172."):
        try:
            return 16 <= int(h.split(".")[1]) <= 31
        except Exception:
            return False
    return False


def norm_base(url: str) -> str:
    """规整 base_url。

    用户经常只填 `http://127.0.0.1:1234`，缺 `/v1` ——
    本机地址且没有路径时自动补上；远程地址不动（各家路径不一样，猜错更糟）。
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        return u
    if "://" not in u:
        u = "http://" + u
    rest = u.split("://", 1)[1]
    if "/" not in rest and not rest.endswith("/") and is_local(u):
        u += "/v1"
    return u


# --------------------------------------------------------------------------- #
# HTTP 层：总时限 / 读取上限 / 有限重试
# --------------------------------------------------------------------------- #
_MAX_BODY = 4 * 1024 * 1024          # 响应体上限 4 MiB
_CHUNK = 64 * 1024                   # 流式读块大小
_RETRY_MAX = 2                       # 最多重试 2 次（也就是最多打 3 次请求）
_RETRY_BACKOFF = (1.0, 2.0)          # 第 1 次重试等 1s，第 2 次等 2s
_RETRY_CODES = (429, 500, 502, 503, 504)   # 只对这几种状态码重试
_RETRY_AFTER_CAP = 30.0              # Retry-After 最多认 30 秒（见 _retry_after_of）
_FMT_REJECT_CODES = (400, 415, 422)  # 认为「服务端不接受 response_format」的状态码


class _BodyTooLarge(Exception):
    """响应体超过 _MAX_BODY。单独开一个类型：调用方要能和别的失败区分开。"""


def _fmt_mib(n: float) -> str:
    return "%.2f MiB" % (n / 1048576.0)


def _retry_after_of(ex) -> float | None:
    """解析 429/503 响应里的 Retry-After，返回秒数；解析不出来返回 None。

    两种合法格式都要认：
      - 秒数：`Retry-After: 3`
      - HTTP 日期：`Retry-After: Wed, 21 Oct 2026 07:28:00 GMT`（用 email.utils
        的 parsedate_to_datetime，标准库自带，不用手写日期解析）
    拿不到 / 格式不认识 → 返回 None，调用方退回固定退避（1s / 2s），
    绝不能因为解析失败就不重试了。
    上限 _RETRY_AFTER_CAP：服务商完全可以回 `Retry-After: 3600`，
    老老实实等一小时等于把界面钉死（本机模型服务就干得出来），
    等 30 秒已经足够跨过常见的限流窗口。
    """
    try:
        raw = (ex.headers.get("Retry-After") or "").strip()
    except Exception:
        return None
    if not raw:
        return None
    try:
        secs = float(raw)
    except ValueError:
        try:
            dt = parsedate_to_datetime(raw)
            if dt is None:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            secs = (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        except Exception:
            return None
    if secs < 0:
        secs = 0.0
    return min(secs, _RETRY_AFTER_CAP)


def _read_body(r, deadline: float) -> str:
    """流式分块读响应体：带**字节上限**与**总时限**检查。

    为什么不能沿用 `r.read()`：
    1) 字节上限 —— 本机模型服务（LM Studio / Ollama / llama.cpp）在模型跑飞时
       会吐巨长的 JSON，`r.read()` 一口气全拉进内存，轻则界面卡死，重则
       MemoryError 把整个管理器带走。分块累加、超过 4 MiB 立刻中止。
    2) 总时限 —— `urlopen(timeout=N)` 的 N 是**单次 socket 操作**的超时，
       不是整次请求的时限：服务端每 10 秒挤一个字节就能永远拖住我们
       （慢速攻击那套）。这里用 time.monotonic() 卡住墙钟，
       每读一块前检查一次；单块内还有 socket timeout 兜底，两重保险。
    超时抛 TimeoutError，超限抛 _BodyTooLarge，都由 _http_once 转成中文错误。
    """
    chunks = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("总时限内没读完响应（已收 %s）" % _fmt_mib(total))
        blk = r.read(_CHUNK)
        if not blk:
            break
        total += len(blk)
        if total > _MAX_BODY:
            raise _BodyTooLarge("响应体超过 %s 上限（已收 %s），已中止读取。"
                                "多半是模型服务返回了异常大的内容，检查一下模型是不是跑飞了。"
                                % (_fmt_mib(_MAX_BODY), _fmt_mib(total)))
        chunks.append(blk)
    return b"".join(chunks).decode("utf-8", "replace")


def _http_once(cfg: dict, url: str, body, budget: float) -> tuple:
    """真正发一次请求（不含重试）。

    返回 5 元组 (ok, data, error, code, retry_after)：
      - code：HTTP 状态码；非 HTTP 层的失败（连不上 / 超时 / 超限）为 0。
        **这是给 _chat 用的结构化通道** —— 原来 _chat 靠 `"400" in err` 这种
        文本子串猜「是不是 response_format 被拒」，服务商换个措辞就误判，
        现在改成拿状态码判断（见 _chat）。
      - retry_after：Retry-After 解析出来的秒数，没有就是 None。
    """
    req = urllib.request.Request(url, data=body, method="POST" if body else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "L4D2ModStudio/1.0")
    key = decrypt_key(cfg.get("api_key_enc", ""))
    if key:
        req.add_header("Authorization", "Bearer " + key)
    budget = max(0.05, float(budget))
    deadline = time.monotonic() + budget
    try:
        with urllib.request.urlopen(req, timeout=budget) as r:
            status = int(getattr(r, "status", 200) or 200)
            raw = _read_body(r, deadline)
        try:
            return True, json.loads(raw), "", status, None
        except Exception:
            return False, None, "服务返回的不是 JSON：%s" % raw[:200], status, None
    except urllib.error.HTTPError as ex:
        code = int(ex.code)
        detail = ""
        try:
            # 错误正文也要限流：有些网关会回一整页 HTML 错误页
            detail = _read_body(ex, deadline)
            j = json.loads(detail)
            detail = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else \
                     (j.get("error") or j.get("message") or detail)
        except Exception:
            pass
        if code == 401:
            hint = "（API Key 不对或没填）"
        elif code == 403:
            hint = "（服务端拒绝了这个 Key 或这个模型：检查权限 / 余额 / 地区限制）"
        elif code == 404:
            hint = "（地址路径不对，检查是不是少了 /v1）"
        elif code == 400:
            hint = "（请求被拒，可能是模型名不对或该模型不支持 JSON 输出）"
        elif code == 429:
            hint = "（被限流了：等一会儿再试，或把「每组并发」调小）"
        elif code in (500, 502, 503, 504):
            hint = "（服务端出错：本机模型可能没加载好或已经崩了）"
        else:
            hint = ""
        return False, None, "HTTP %d %s%s" % (code, str(detail)[:300], hint), code, _retry_after_of(ex)
    except urllib.error.URLError as ex:
        reason = getattr(ex, "reason", ex)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return False, None, "请求超时（总时限 %.0f 秒，%s）：%s" % (budget, _safe_url(url), reason or "服务端没回应"), 0, None
        return False, None, "连不上 %s：%s" % (_safe_url(url), reason), 0, None
    except _BodyTooLarge as ex:
        return False, None, str(ex), 0, None
    except (socket.timeout, TimeoutError) as ex:
        return False, None, "请求超时（总时限 %.0f 秒，%s）：%s" % (budget, _safe_url(url), ex or "服务端没回应"), 0, None
    except Exception as ex:
        return False, None, "%s: %s" % (type(ex).__name__, ex), 0, None


def _request(cfg: dict, path: str, payload: dict | None = None, timeout: float | None = None):
    """发一次请求（含有限重试）。返回 (ok, data, error_text, http_status)。

    第 4 个返回值 http_status 是本轮新增的结构化通道（0 = 连 HTTP 都没到）：
    _chat 要用它判断「是不是 response_format 被拒」，而不是去 err 里找子串。
    注意返回元组从 3 元变成 4 元了，ai.py 内 3 个调用点已同步更新
    （list_models / _chat 两次）；本模块外没有别的调用点。

    重试策略（原来一次都不重试，本机模型服务加载模型时回 503、云端限流回 429
    都是家常便饭，用户看到的就是「复核失败」）：
      - 只对 429 与 500/502/503/504 重试，最多 _RETRY_MAX 次，退避 1s / 2s；
      - 响应带 Retry-After 就按它等（解析失败退回固定退避）；
      - 401/403 **绝不重试** —— Key 不对重试一百次也还是不对，
        多打两次只会让服务商的风控更不高兴，直接给中文提示让用户去改；
      - 真的重试过才在最终错误里附上「已重试 N 次」，
        没重试过的（401/403、连不上）不加，免得把 401 的提示文案淹掉。
    """
    base = norm_base(cfg.get("base_url", ""))
    if not base:
        return False, None, "没有填 API 地址", 0
    url = base + path          # 发请求用完整 URL（query 里的鉴权参数照常带着）
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    try:
        budget = float(timeout or cfg.get("timeout", 120) or 120)
    except (TypeError, ValueError):
        budget = 120.0
    if budget <= 0:
        budget = 120.0
    tried = 0
    ok, data, err, code, retry_after = False, None, "请求未执行", 0, None
    while True:
        ok, data, err, code, retry_after = _http_once(cfg, url, body, budget)
        if ok:
            return True, data, "", code
        if code not in _RETRY_CODES or tried >= _RETRY_MAX:
            break
        delay = retry_after if retry_after is not None else _RETRY_BACKOFF[min(tried, len(_RETRY_BACKOFF) - 1)]
        tried += 1
        time.sleep(delay)
    if tried:
        err = "%s（已重试 %d 次）" % (err, tried)
    return False, None, err, code


def list_models(cfg: dict) -> dict:
    """拉服务商支持的模型列表。"""
    ok, data, err, _code = _request(cfg, "/models", None, timeout=15)
    if not ok:
        return {"ok": False, "error": err, "models": []}
    items = data.get("data") if isinstance(data, dict) else None
    models = []
    for it in (items or []):
        if isinstance(it, dict) and it.get("id"):
            models.append(str(it["id"]))
        elif isinstance(it, str):
            models.append(it)
    return {"ok": True, "models": sorted(models), "error": ""}


def test(cfg: dict) -> dict:
    """连通性自检：能不能连上、有哪些模型、会挑哪一个。"""
    t0 = time.time()
    r = list_models(cfg)
    took = round(time.time() - t0, 2)
    if not r["ok"]:
        return {"ok": False, "error": r["error"], "latency": took}
    picked = cfg.get("model") or (r["models"][0] if r["models"] else "")
    return {"ok": True, "models": r["models"], "model": picked,
            "model_ok": bool(picked and picked in r["models"]),
            "latency": took, "is_local": is_local(cfg.get("base_url", ""))}


# --------------------------------------------------------------------------- #
# 冲突案例构造
# --------------------------------------------------------------------------- #
def build_cases(conflicts: dict, mods_by_key: dict, order_map: dict | None = None,
                limit: int = 12, paths_per_case: int = 4, max_mods: int = 6) -> list:
    """把 dir_groups 压成给模型看的紧凑案例。

    刻意**不带绝对路径**：只给 VPK 内部路径、mod 标题、分类标签、加载顺序。
    """
    order_map = order_map or {}
    out = []
    for g in conflicts.get("dir_groups", []):
        if len(out) >= limit:
            break
        mods = []
        for k in (g.get("mods") or [])[:max_mods]:
            m = mods_by_key.get(k) or {}
            mods.append({
                "name": (m.get("title") or k)[:70],
                "cat": m.get("category") or "misc",
                "tags": (m.get("tags") or [])[:3] + (m.get("content_tags") or [])[:3],
                "auto": m.get("auto_category") if m.get("auto_category") != m.get("category") else None,
                "order": order_map.get(k),
                "enabled": m.get("enabled", True),
            })
        out.append({
            "id": "c%d" % (len(out) + 1),
            "dir": g.get("dir"),
            "kind": g.get("kind"),
            "files": g.get("files"),
            "paths": (g.get("sample") or [])[:paths_per_case],
            "mods": mods,
        })
    return out


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """你是《求生之路2》(Left 4 Dead 2) 的 MOD 冲突分析专家，熟悉 Source 引擎的 VPK 加载机制。

用户会给你若干组「资源覆盖冲突」：同一条 VPK 内部路径被多个 MOD 同时提供。
一个规则引擎负责发现这些路径相撞，但**路径相撞不等于真的会出问题**——由你判断实际影响。

判断要点：
1. 区分「同一资源被替换」与「目录里各放各的」：
   - 多个 MOD 提供同一个文件的同一条路径 → 真冲突，游戏只会用其中一个，其余白装。
   - 只是目录相同、文件名不同（如各自放不同武器的贴图）→ 聚合造成的假象，判 none。
2. 看 MOD 的性质。纯贴图(Material)替换 / 完整模型(Model)替换 / 音效 / 脚本，后果完全不同：
   - 模型 MOD 常自带同名贴图，会吃掉另一个纯贴图 MOD 的效果。
   - 脚本类(.nut/.lua/.txt 配置) 被覆盖几乎总是真冲突，而且是**功能级**冲突，最该重视。
3. 加载顺序来自 addonlist.txt，顺序会影响谁生效。但**具体哪个方向获胜在 L4D2 里并不总是直观**：
   如果你不能确定谁吃掉谁，就明说「需要在游戏内实测确认」，**不要编造结论**。
4. 粒子/特效/HUD/音效的覆盖通常只影响观感，不致命。
5. MOD 名字里的关键词（人物名、武器名、V2/V3、HD、Reanimation 等）是重要线索，用它判断两者是不是在改同一样东西。

风险等级 risk 只能取这四个值之一（不要造新词）：
- "high"：几乎一定会出问题或丢功能（脚本冲突、模型被整体替换导致另一个 MOD 完全失效、缺失依赖）
- "medium"：可能出问题或部分失效，取决于加载顺序，值得处理
- "low"：影响限于观感，或大概率无害
- "none"：经判断其实不冲突（目录聚合的假象、各放各的文件）

只输出 JSON，不要 markdown 代码块，不要任何解释性文字。"""

USER_TEMPLATE = """下面是 %d 组待复核的冲突。字段说明：
- dir：冲突所在的 VPK 内部目录
- kind：规则引擎给的类型标签
- files：该目录下相撞的文件数量
- paths：相撞路径的样例
- mods：涉及的 MOD。name=标题，cat=分类，tags=内容标签，order=在 addonlist.txt 里的位置（数字越小越靠前），enabled=是否启用

%s

请严格按这个 JSON 结构返回（results 的条数必须与输入组数一致，id 必须一一对应）：
{"results":[{"id":"c1","risk":"high|medium|low|none","what":"一句话说清会发生什么","lose":"哪个 MOD 的什么功能会被吃掉；没有就写 null","advice":"具体怎么处理，1-2 句"}],"overall":"对本批次的整体判断，2-3 句"}"""

SYNTH_TEMPLATE = """你刚才复核了 %d 组冲突，逐条判定如下（JSON）：

%s

请给一段**整体结论**，面向一个普通玩家，说清：
1. 这些冲突里哪几条是真的需要动手处理的（按优先级排序，指出具体是哪个目录/哪几个 MOD）；
2. 哪些其实可以不管；
3. 如果只能做一件事，先做什么。

只输出 JSON：{"overall":"这里写整体结论，不超过 250 字"}"""


# --------------------------------------------------------------------------- #
# 解析模型返回
# --------------------------------------------------------------------------- #
def _extract_json(text: str):
    """从模型输出里挖出 JSON。

    实测各家模型都会包 ```json 代码块，或者前后加一句「好的，以下是结果」，
    甚至有把两个 JSON 拼一起的。处理顺序：
      ① 剥 ``` 围栏 → ② 从第一个 { 开始做**括号深度扫描**（跳过字符串内的括号）
      → ③ 深度归零就截断，尾部再有什么都不影响解析。
    这样比「第一个 { 到最后一个 }」的贪心截取稳得多。
    """
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        # ```json\n...\n```  或  ```\n...\n```
        t = t.split("\n", 1)[-1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
        t = t.strip()
    try:
        return json.loads(t)
    except Exception:
        pass

    def _scan(s: str, opener: str, closer: str):
        start = s.find(opener)
        if start < 0:
            return None
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except Exception:
                        return None
        return None

    return _scan(t, "{", "}") if t.find("{") >= 0 else _scan(t, "[", "]")


def _chat(cfg: dict, messages: list, cancel: threading.Event | None = None) -> tuple:
    """一次对话补全。返回 (ok, data, err)。"""
    if cancel is not None and cancel.is_set():
        return False, None, "已取消"
    payload = {
        "model": cfg.get("model") or _fallback_model(cfg),
        "messages": messages,
        "temperature": cfg.get("temperature", 0.2),
        "stream": False,
    }
    # 想要严格 JSON。部分服务不支持这个参数，被拒就退回不带。
    #
    # 原来这里是 `if not ok and ("400" in err or "response_format" in err
    # or "json_object" in err):` —— 靠错误文本的子串来猜「是不是 response_format
    # 被拒了」。两个坑：① 服务商换个措辞（比如「unsupported parameter」）就猜不中，
    # 于是本该退回重试的没退回，用户白等一场；② 反过来，模型名叫 "json_object-7b"
    # 或者报错里碰巧带了 "400" 字样（如 "HTTP 400" 之外的上下文、端口号、token 数），
    # 明明不是参数问题也会再打一次请求。
    # 现在改成看 _request 返回的结构化状态码/错误：
    #   - code in (400, 415, 422)：明确是「请求本身不被接受」，值得退回重试；
    #   - code == 0：请求根本没到 HTTP 层（连不上 / 超时），不是参数问题，不重试；
    #   - 400 之外的 4xx（401/403/404/429）交给上面的重试与提示逻辑，别在这里瞎试。
    # 只有一次退回机会（第二次仍带 response_format 以外的同样 payload），
    # 不会无限套娃。
    ok, data, err, code = _request(cfg, "/chat/completions",
                                   dict(payload, response_format={"type": "json_object"}))
    if not ok and code in _FMT_REJECT_CODES:
        # 这里不写日志：_chat 会被「测试连接」等前台路径调用，没有 job 可挂；
        # 退回过的事实由调用方（_run_analysis）在需要时记进任务日志。
        ok, data, err, code = _request(cfg, "/chat/completions", payload)
    return ok, data, err


def _fallback_model(cfg: dict) -> str:
    """用户没填模型名时，自动挑服务商给的第一个。"""
    r = list_models(cfg)
    if r["ok"] and r["models"]:
        cfg["model"] = r["models"][0]
        return cfg["model"]
    return ""


def _text_of(data: dict) -> str:
    try:
        return data["choices"][0]["message"]["content"] or ""
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# 后台任务
# --------------------------------------------------------------------------- #
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()
_JOB_KEEP = 6          # 只保留最近几个任务的状态
_JOB_TTL = 1800.0      # 已完成任务最多留 30 分钟（秒）


def _sweep_locked(now: float = 0.0) -> None:
    """清理过期/超量的已完成任务。**调用方必须已持有 _JOBS_LOCK。**

    两条清理规则：
      1. 数量：只保留最近 _JOB_KEEP 个（正在跑的永不删，否则前端轮询会突然 404）。
      2. 时间：已完成且结束超过 _JOB_TTL 的直接删 —— 用户不再开新任务时，
         光靠 _new_job 里那次数量裁剪是永远触发不了的，_JOBS 会一直挂着。
    """
    now = now or time.time()
    done = [(k, v) for k, v in _JOBS.items()
            if v["state"] != "running" and v.get("finished")]
    # 时间优先：过期的先走
    for k, v in done:
        if now - v["finished"] > _JOB_TTL:
            _JOBS.pop(k, None)
    # 数量兜底：按结束时间从旧到新删，直到不超量
    done = [(k, v) for k, v in _JOBS.items()
            if v["state"] != "running" and v.get("finished")]
    if len(_JOBS) > _JOB_KEEP:
        done.sort(key=lambda kv: kv[1]["finished"])
        for k, _v in done:
            if len(_JOBS) <= _JOB_KEEP:
                break
            _JOBS.pop(k, None)


def _new_job(total: int, meta: dict) -> str:
    """无条件建一个任务并返回 job id（旧接口，保留）。

    这是「不加闸门」的版本，只服务于测试与内部调试（`verify_aijobs.py` 直接
    用它批量造任务来验证裁剪规则）。业务入口 `start_analysis` 一律走
    `_new_job_if_idle()`，别在这里加并发判断，否则那套测试就造不出多个任务了。
    """
    jid = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[jid] = {
            "id": jid, "state": "running", "started": time.time(), "elapsed": 0.0,
            "finished": 0.0, "total": total, "done": 0, "results": [], "overall": "",
            "error": "", "log": [], "meta": meta,
            "cancel": threading.Event(),
        }
        _sweep_locked()
    return jid


def _new_job_if_idle(total: int, meta: dict) -> tuple:
    """并发闸门版建任务：已有任务在跑就**拒绝**，不再建新的。

    返回 (jid, busy_jid)：成功 = (新 id, "")；被拒 = ("", 正在跑的那个 id)。

    ⚠️ 为什么要把「查 running」和「建 job」塞进**同一个** with _JOBS_LOCK：
    `_JOBS_LOCK` 是普通 `threading.Lock`（**不可重入**），而 `_new_job()`
    自己会取一次锁。如果写成「先取锁扫一遍 running，再调 _new_job()」，
    两次取锁之间就是竞态窗口 —— 两个请求几乎同时进来，都扫到「没有 running」，
    于是两个任务一起跑，正是闸门想拦的情况（前端点两下「开始复核」、
    或者网页开了两个标签页，都能撞上）。
    所以这里一次持锁把两件事做完：查 + 建。
    这也顺便避开了本文件历史上那个进程级死锁（在持锁期间调 `_finish_cancel()`
    → `_job_log()` → 再取同一把不可重入锁，永久阻塞）。
    本函数在锁内**只碰 _JOBS 字典本身**，不调用任何会取锁的函数。
    """
    jid = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        for k, v in _JOBS.items():
            if v.get("state") == "running":
                return "", k
        _JOBS[jid] = {
            "id": jid, "state": "running", "started": time.time(), "elapsed": 0.0,
            "finished": 0.0, "total": total, "done": 0, "results": [], "overall": "",
            "error": "", "log": [], "meta": meta,
            "cancel": threading.Event(),
        }
        _sweep_locked()
    return jid, ""


def _job_log(jid: str, msg: str) -> None:
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        if j:
            j["log"].append("[%s] %s" % (time.strftime("%H:%M:%S"), msg))
            j["elapsed"] = round(time.time() - j["started"], 1)


def job_state(jid: str) -> dict:
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        if not j:
            return {"ok": False, "error": "任务不存在或已过期"}
        # ⚠️ 这里必须把 results / log 拷一份再返回。
        #    直接返回引用的话，调用方 json.dumps 的同时后台线程还在
        #    append / sort，会读到撕裂的快照（列表长度和内容对不上）。
        d = {k: v for k, v in j.items() if k != "cancel"}
        d["results"] = list(j["results"])
        d["log"] = list(j["log"])
        d["ok"] = True
        return d


def cancel_job(jid: str) -> dict:
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        if not j:
            return {"ok": False, "error": "任务不存在"}
        j["cancel"].set()
    return {"ok": True}


def _finish(jid: str, state: str, error: str = "") -> None:
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        if j:
            j["state"] = state
            j["error"] = error
            j["elapsed"] = round(time.time() - j["started"], 1)
            j["finished"] = time.time()
            # 任务结束时顺手清扫：别等下一次 _new_job 才回收
            _sweep_locked()


def start_analysis(cfg: dict, cases: list, synthesize: bool | None = None) -> dict:
    """开一个后台分析任务，立刻返回 job id。**一次只允许一个任务在跑。**

    以前这里没有任何闸门：连着点几下「开始复核」就起几个后台线程，
    同时对着同一个模型服务猛打 —— 本机模型（LM Studio / Ollama）本来就一次
    只服务一个请求，结果就是全部超时、显存爆掉或者服务直接崩，用户看到的是
    「全都失败了」却不知道为什么。云端更直接：并发打过去就是撞限流。

    现在的行为是**明确拒绝**，而不是静默排队：队列看着美好，实际上用户点完
    就去干别的了，等十分钟才轮到他的任务，中间还没任何反馈 —— 不如立刻告诉他
    「已经在跑了，先等等或先取消」。前端 web/app.js 的 aiStart() 看到 `!r.ok`
    会把 `error` 原样 toast 出来，所以这段中文会直接显示在界面上。
    前端自己也有一层同样意思的本地判断（S.ai.job 还在 running），但那只是
    内存里的状态，刷新页面就失效了，真正的闸门必须在后端。

    ⚠️ 这里必须调 `_new_job_if_idle()`（查 running + 建 job 在一次持锁里完成），
    不能自己取锁查一遍再调 `_new_job()` —— 理由见 `_new_job_if_idle` 的注释。
    """
    if not cases:
        return {"ok": False, "error": "没有可复核的冲突案例（先跑一次审计，或放宽筛选条件）"}
    if not norm_base(cfg.get("base_url", "")):
        return {"ok": False, "error": "没有配置 AI 接口地址"}
    batch = max(1, int(cfg.get("batch_cases", 4) or 4))
    batches = [cases[i:i + batch] for i in range(0, len(cases), batch)]
    # meta 里的 base_url 过一遍 _safe_url：这份 meta 会随 job_state 回给前端、
    # 也可能被打进日志，用户把密钥写在 query 里时不能整串带出去。
    # （发请求仍然用完整 URL，见 _request。）
    jid, busy = _new_job_if_idle(len(cases), {
        "model": cfg.get("model") or "(自动)", "base_url": _safe_url(norm_base(cfg.get("base_url", ""))),
        "is_local": is_local(cfg.get("base_url", "")),
        "batches": len(batches), "cases": len(cases),
    })
    if not jid:
        return {"ok": False, "error": "已经有一个复核任务在跑了（任务 %s）。"
                                     "等它跑完，或者先在界面上点「取消」再重新开始。" % busy,
                "busy_job": busy}
    threading.Thread(target=_run_analysis, name="ai-analyze",
                     args=(jid, cfg, batches, synthesize), daemon=True).start()
    return {"ok": True, "job": jid, "batches": len(batches), "cases": len(cases)}


def _run_analysis(jid: str, cfg: dict, batches: list, synthesize) -> None:
    done_ok = 0
    verdicts = []
    try:
        _job_log(jid, "开始复核 %d 批 / 共 %d 组" % (len(batches), sum(len(b) for b in batches)))
        for bi, batch in enumerate(batches, 1):
            # ⚠️ 取消检查必须在**锁外**做。
            #    这里原来写成「在 with _JOBS_LOCK 内直接调 _finish_cancel(jid)」，
            #    而 _finish_cancel → _job_log → 又要 with _JOBS_LOCK。
            #    _JOBS_LOCK 是普通 threading.Lock（不可重入），同线程二次获取
            #    会永久阻塞 —— worker 抱着锁睡死，之后 job_state / cancel_job /
            #    _new_job 全部挂起，前端每 600ms 轮询一次、线程持续堆积，
            #    只能重启进程。触发条件：点「开始复核」后立刻点「取消」。
            #    所以：锁内只取 cancel 事件对象，判断与收尾都放到锁外。
            with _JOBS_LOCK:
                j = _JOBS.get(jid)
                if not j:
                    return
                cancel = j["cancel"]
            if cancel.is_set():
                _finish_cancel(jid)
                return
            _job_log(jid, "第 %d/%d 批：%d 组，请求中…（模型可能在思考，耐心点）"
                     % (bi, len(batches), len(batch)))
            t0 = time.time()
            payload = json.dumps(batch, ensure_ascii=False, indent=1)
            ok, data, err = _chat(cfg, [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_TEMPLATE % (len(batch), payload)},
            ], cancel)
            if not ok:
                _job_log(jid, "第 %d 批失败：%s" % (bi, err))
                with _JOBS_LOCK:
                    j = _JOBS.get(jid)
                    if j:
                        for c in batch:
                            j["results"].append({"id": c["id"], "risk": "unknown",
                                                 "what": "本批未取得结果：%s" % err[:180],
                                                 "lose": None, "advice": "",
                                                 "dir": c.get("dir"), "kind": c.get("kind"),
                                                 "mods": [m["name"] for m in c.get("mods", [])]})
                            j["done"] += 1
                continue

            _model_text = _text_of(data)
            parsed = _extract_json(_model_text)
            if not isinstance(parsed, dict):
                # 措辞修正：原来这里写「已记录原文片段」，但代码从头到尾
                # **没有记录任何原文**（只把 parsed 置空继续），是句会误导排查的假日志 ——
                # 用户以为日志里能找到模型原话，翻半天什么都没有。
                # 也不打算真去记原文：模型输出可能上千字，还可能夹带用户贴进去的
                # 本地路径 / 密钥，落盘进 audit.log 是新的泄漏面。
                # 改成如实描述 + 保留可诊断性：字符数能区分「模型一个字没回」（0）
                # 和「回了一大段散文/被截断的 JSON」（很长），这对排查已经够用。
                _job_log(jid, "第 %d 批返回的内容不是预期 JSON（模型输出 %d 字符，"
                              "本批按「未取得结果」处理）" % (bi, len(_model_text or "")))
                parsed = {}
            rows = parsed.get("results") if isinstance(parsed.get("results"), list) else []
            by_id = {str(r.get("id")): r for r in rows if isinstance(r, dict)}
            with _JOBS_LOCK:
                j = _JOBS.get(jid)
                if not j:
                    return
                for c in batch:
                    r = by_id.get(c["id"]) or {}
                    risk = str(r.get("risk", "unknown")).lower()
                    if risk not in RISK_ORDER:
                        risk = "unknown"
                    j["results"].append({
                        "id": c["id"], "risk": risk,
                        "what": str(r.get("what") or "").strip() or "（模型没有给出说明）",
                        "lose": r.get("lose") if r.get("lose") not in ("", "null", None) else None,
                        "advice": str(r.get("advice") or "").strip(),
                        "dir": c.get("dir"), "kind": c.get("kind"), "files": c.get("files"),
                        "paths": c.get("paths"),
                        "mods": [m["name"] for m in c.get("mods", [])],
                    })
                    j["done"] += 1
                ov = parsed.get("overall")
                if ov:
                    j["overall"] = (j["overall"] + "\n" + str(ov)).strip() if j["overall"] else str(ov)
            done_ok += 1
            verdicts.extend(parsed.get("results") or [])
            _job_log(jid, "第 %d 批完成，耗时 %.1fs，拿到 %d 条判定" % (bi, time.time() - t0, len(rows)))
            # 每批跑完立刻复查取消：别让用户点了取消还要等下一批的检查点
            if _cancelled(jid):
                _finish_cancel(jid)
                return

        if not done_ok:
            _finish(jid, "error", "所有批次都失败了 —— 检查地址、模型名和 API Key。可点「测试连接」看具体报错。")
            return

        # ⚠️ 这里必须再查一次取消：批次跑完 ≠ 用户没取消过。
        #    （之前漏了这一步：取消中间批次后仍会被标成 done，语义不对。）
        if _cancelled(jid):
            _finish_cancel(jid)
            return

        if synthesize and len(batches) > 1 and verdicts:
            _job_log(jid, "生成整体结论…")
            ok, data, err = _chat(cfg, [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": SYNTH_TEMPLATE
                 % (len(verdicts), json.dumps(verdicts, ensure_ascii=False)[:6000])},
            ], _cancel_of(jid))
            if ok:
                p = _extract_json(_text_of(data)) or {}
                ov = p.get("overall") if isinstance(p, dict) else None
                if ov:
                    with _JOBS_LOCK:
                        if jid in _JOBS:
                            _JOBS[jid]["overall"] = str(ov)
                    _job_log(jid, "整体结论已生成")
            else:
                _job_log(jid, "整体结论生成失败（不影响逐条结果）：%s" % err)

        # 按风险排序，高危在前
        with _JOBS_LOCK:
            j = _JOBS.get(jid)
            if j:
                j["results"].sort(key=lambda r: RISK_ORDER.get(r.get("risk"), 9))
        _finish(jid, "done")
        _job_log(jid, "全部完成")
    except Exception as ex:
        import traceback
        _job_log(jid, "未捕获异常：%s" % ex)
        _finish(jid, "error", "%s\n%s" % (ex, traceback.format_exc()[-1200:]))


def _cancelled(jid: str) -> bool:
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        return bool(j and j["cancel"].is_set())


def _cancel_of(jid: str):
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        return j["cancel"] if j else None


def _finish_cancel(jid: str) -> None:
    _job_log(jid, "已被用户取消")
    _finish(jid, "cancelled")
