# -*- coding: utf-8 -*-
"""生成 L4D2 Mod Studio 的应用图标 assets/app.ico。

只用标准库（zlib + struct），不依赖 Pillow。
图形：深色圆角底 + 淡蓝「mod 包」圆角块 + 白色播放三角 —— 简单、在 16px 下也不糊。

用法： python tools/make_icon.py [输出路径]
"""
from __future__ import annotations

import os
import struct
import sys
import zlib

# 配色（深色底 + 家徽淡蓝，跟界面同一套观感）
BG = (26, 36, 51, 255)        # #1a2433
PKG = (127, 196, 255, 255)    # #7fc4ff
PKG_DARK = (86, 152, 214, 255)
TRI = (255, 255, 255, 255)
SS = 4                        # 超采样倍率（抗锯齿）

SIZES = [16, 24, 32, 48, 64, 128, 256]


# --------------------------------------------------------------------------- #
def _rrect(x, y, x0, y0, x1, y1, r):
    """点 (x,y) 是否落在圆角矩形内。"""
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def _tri(x, y, ax, ay, bx, by, cx, cy):
    """点是否在三角形内（用叉积同号判断）。"""
    def cross(ox, oy, px_, py_, qx, qy):
        return (px_ - ox) * (qy - oy) - (py_ - oy) * (qx - ox)
    d1 = cross(ax, ay, bx, by, x, y)
    d2 = cross(bx, by, cx, cy, x, y)
    d3 = cross(cx, cy, ax, ay, x, y)
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos)


def _sample(u, v):
    """在归一化坐标 (u,v) ∈ [0,1]^2 上取一个颜色，返回 RGBA。"""
    # ① 深色圆角底
    if not _rrect(u, v, 0.02, 0.02, 0.98, 0.98, 0.21):
        return (0, 0, 0, 0)
    col = BG

    # ② 淡蓝「mod 包」圆角块
    if _rrect(u, v, 0.20, 0.235, 0.80, 0.775, 0.085):
        col = PKG
        # 顶面一条深色分界，做出一点「盒子」的层次
        if 0.235 <= v <= 0.335:
            col = PKG_DARK
        # ③ 白色播放三角（横向略偏右，视觉重心才正）
        if _tri(u, v, 0.435, 0.415, 0.435, 0.615, 0.625, 0.515):
            col = TRI
    return col


def _render(size: int):
    """渲染一张 size×size 的 RGBA（bytes，逐行 top-down）。"""
    S = size * SS
    rows = []
    inv = 1.0 / S
    for py in range(size):
        row = bytearray()
        for px in range(size):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    u = (px * SS + sx + 0.5) * inv
                    v = (py * SS + sy + 0.5) * inv
                    cr, cg, cb, ca = _sample(u, v)
                    # 预乘后再平均，边缘才不会出现黑边
                    r += cr * ca
                    g += cg * ca
                    b += cb * ca
                    a += ca
            n = SS * SS
            if a:
                row += bytes((round(r / a), round(g / a), round(b / a), round(a / n)))
            else:
                row += b"\0\0\0\0"
        rows.append(bytes(row))
    return rows


def _png(size: int, rows) -> bytes:
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _bmp(size: int, rows) -> bytes:
    """32bpp 的 DIB，给 16~64 这种小尺寸用（兼容性最好）。"""
    # BITMAPINFOHEADER：高度写两倍（颜色位图 + AND 掩码）
    hdr = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                      size * size * 4, 0, 0, 0, 0)
    body = bytearray()
    for y in range(size - 1, -1, -1):          # BMP 是自下而上
        r = rows[y]
        for x in range(size):
            pr, pg, pb, pa = r[x * 4:x * 4 + 4]
            body += bytes((pb, pg, pr, pa))    # BGRA
    # AND 掩码：全 0 即可（透明度靠 alpha 通道）
    mask_row = ((size + 31) // 32) * 4
    body += b"\x00" * (mask_row * size)
    return hdr + bytes(body)


def build(out_path: str) -> None:
    entries = []
    for s in SIZES:
        rows = _render(s)
        data = _png(s, rows) if s >= 128 else _bmp(s, rows)
        entries.append((s, data))
        print("  渲染 %3d×%-3d → %6d 字节 (%s)" % (s, s, len(data), "PNG" if s >= 128 else "BMP"))

    # ICONDIR + ICONDIRENTRY*
    out = bytearray(struct.pack("<HHH", 0, 1, len(entries)))
    offset = 6 + 16 * len(entries)
    for s, data in entries:
        out += struct.pack("<BBBBHHII",
                           0 if s >= 256 else s,   # 256 要写 0
                           0 if s >= 256 else s,
                           0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _s, data in entries:
        out += data

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as fp:
        fp.write(out)
    print("已写出 %s（共 %d 字节，%d 个尺寸）" % (out_path, len(out), len(entries)))


if __name__ == "__main__":
    dst = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "app.ico")
    build(dst)
