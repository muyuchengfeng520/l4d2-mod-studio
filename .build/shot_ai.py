# -*- coding: utf-8 -*-
"""临时：把 AI 复核跑到出结果的状态，然后把窗口截下来。放在 .build 下，验完即删。"""
from __future__ import annotations

import ctypes
import json
import os
import struct
import subprocess
import sys
import time
import urllib.request
import zlib
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = "http://127.0.0.1:17320"
user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
SRCCOPY, DIB_RGB_COLORS, PW_RENDERFULLCONTENT = 0x00CC0020, 0, 0x2


def png(w, h, bgra):
    raw = bytearray()
    for y in range(h):
        raw += b"\x00"
        row = bgra[y * w * 4:(y + 1) * w * 4]
        raw += bytes(b for i in range(0, len(row), 4)
                     for b in (row[i + 2], row[i + 1], row[i], 255))

    def ch(tag, data):
        return struct.pack(">I", len(data)) + tag + data + \
               struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n"
            + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + ch(b"IDAT", zlib.compress(bytes(raw), 6)) + ch(b"IEND", b""))


def find_win(title_part):
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _l):
        if not user32.IsWindowVisible(h):
            return True
        n = user32.GetWindowTextLengthW(h)
        if n <= 0:
            return True
        b = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, b, n + 1)
        if title_part in b.value:
            found.append(h)
        return True
    user32.EnumWindows(cb, 0)
    return found


def capture(hwnd, out):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    if not user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT):
        gdi32.BitBlt(mdc, 0, 0, w, h, hdc, 0, 0, SRCCOPY)

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]
    bi = BIH()
    bi.biSize = ctypes.sizeof(BIH); bi.biWidth = w; bi.biHeight = -h
    bi.biPlanes = 1; bi.biBitCount = 32; bi.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    got = gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), DIB_RGB_COLORS)
    gdi32.DeleteObject(bmp); gdi32.DeleteDC(mdc); user32.ReleaseDC(hwnd, hdc)
    if not got:
        raise RuntimeError("GetDIBits 失败")
    open(out, "wb").write(png(w, h, buf.raw))
    return w, h


def main():
    procs = []
    for port, mode in [(18021, "normal"), (18022, "slow")]:
        procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, ".build", "mock_llm.py"),
                                       str(port), mode],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    procs.append(subprocess.Popen([sys.executable, "-u", os.path.join(ROOT, "app", "server.py"),
                                   "--no-browser", "--port", "17320"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    shots = []
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(APP + "/api/status", timeout=2).read(); break
            except Exception:
                time.sleep(0.3)
        req = urllib.request.Request(APP + "/api/ai/config",
                                     data=json.dumps({"enabled": True, "base_url": "127.0.0.1:18021",
                                                      "model": "", "batch_cases": 3}).encode(),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=30).read()

        import webview
        win = webview.create_window("L4D2 Mod Studio — 求生之路2 Mod 管理器", APP,
                                    width=1400, height=900)

        def drive():
            time.sleep(3)
            for _ in range(40):
                if win.evaluate_js("(document.getElementById('navMods')||{}).textContent||''") not in ("", "–"):
                    break
                time.sleep(0.5)
            win.evaluate_js("S.audit=null; S.auditTab='conflicts'; rAudit()")
            for _ in range(60):
                if win.evaluate_js("!!document.querySelector('#aiCard')"):
                    break
                time.sleep(0.5)
            time.sleep(1.2)
            win.evaluate_js("aiStart()")
            for _ in range(90):
                if win.evaluate_js("(S.ai.job&&S.ai.job.state)||'?'") in ("done", "error", "cancelled"):
                    break
                time.sleep(0.6)
            time.sleep(1.5)
            hwnd = find_win("L4D2 Mod Studio")[0]
            shots.append(capture(hwnd, os.path.join(ROOT, "assets", "screenshot-ai.png")))
            win.destroy()

        webview.start(drive)
        print("结果页截图:", shots)
    finally:
        for p in procs:
            try:
                p.terminate(); p.wait(timeout=6)
            except Exception:
                p.kill()


if __name__ == "__main__":
    main()
