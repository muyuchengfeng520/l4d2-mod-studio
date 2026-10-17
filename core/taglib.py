# -*- coding: utf-8 -*-
"""标签库与分类知识。

三层判定，证据逐条透出（便于人工复核）：

  第一层 游戏官方标签 —— VPK 内 addoninfo.txt 的 addoncontent_* 字段（最权威）
  第二层 素材级标签 —— 内置完整标签库（源自 NekoVpk TaggedAssets.jsonc，
                       75 标签 / 204 条路径规则；外部文件可覆盖，见 BUILTIN_TAGS）
  第三层 内置启发式 —— VPK 内部路径 + 文件名关键词（与第二层合并打分）

外部标签库缺失时自动降级，不中断。
"""
from __future__ import annotations

import copy
import json
import os
import re

# --------------------------------------------------------------------------- #
# 主分类表（对齐 FireAxe 仓库的 10 个目录，方便与既有整理保持一致）
# --------------------------------------------------------------------------- #
CATEGORIES: dict[str, dict] = {
    "campaign": {"zh": "地图 / 战役", "order": 4, "color": "#e0a458"},
    "survivor": {"zh": "幸存者", "order": 1, "color": "#5b9bd5"},
    "infected": {"zh": "感染者", "order": 2, "color": "#a06cd5"},
    "weapon": {"zh": "武器皮肤", "order": 3, "color": "#d96459"},
    "sound": {"zh": "音效 / 音乐", "order": 5, "color": "#5cb85c"},
    "hud": {"zh": "HUD / 界面", "order": 6, "color": "#4aa3a3"},
    "script": {"zh": "脚本 / 插件", "order": 7, "color": "#c9a227"},
    "particle": {"zh": "粒子 / 特效", "order": 8, "color": "#e879b5"},
    "material": {"zh": "材质 / 贴图", "order": 9, "color": "#8f8f8f"},
    "model_other": {"zh": "其他模型", "order": 10, "color": "#7d8ca3"},
    "misc": {"zh": "其他 / 未分类", "order": 99, "color": "#6b7280"},
}

# 仓库目录名 -> 主分类（FireAxe 已有目录直接采信，优先级最高）
REPO_DIR_MAP = {
    "幸存者": "survivor", "感染者": "infected", "武器": "weapon", "地图": "campaign",
    "战役": "campaign", "音效": "sound", "音乐": "sound", "hud": "hud", "界面": "hud",
    "脚本": "script", "插件": "script", "粒子": "particle", "特效": "particle",
    "材质": "material", "贴图": "material", "其他": "misc", "未分类": "misc",
}

# NekoVpk 的 type -> 本工具主分类
NEKO_TYPE_MAP = {
    "survivor": "survivor", "infected": "infected", "weapon": "weapon", "melee": "weapon",
    "shotgun": "weapon", "sniper": "weapon", "rifle": "weapon", "smg": "weapon",
    "pistol": "weapon", "map": "campaign", "campaign": "campaign", "vscript": "script",
    "script": "script", "sound": "sound", "music": "sound", "hud": "hud", "ui": "hud",
    "particle": "particle", "texture": "material", "material": "material",
    "model": "model_other",
}

# addoninfo.txt 的 addoncontent_* 官方标签 -> 主分类（完整覆盖实测出现的 19 个标签）
CONTENT_TAG_MAP = {
    "campaign": "campaign", "map": "campaign", "versus": "campaign",
    "survival": "campaign", "scavenge": "campaign",
    "survivor": "survivor",
    "bossinfected": "infected", "commoninfected": "infected", "infected": "infected",
    "weapon": "weapon", "weaponmodel": "weapon", "melee": "weapon",
    "script": "script", "vscript": "script",
    "backgroundmovie": "hud", "ui": "hud", "hud": "hud",
    "music": "sound", "sound": "sound", "voice": "sound",
    "skin": "material", "texture": "material", "material": "material",
    "prop": "model_other", "prefab": "model_other", "model": "model_other",
    "spray": "misc", "particle": "particle",
}

# NekoVpk 标签 type 的特异性权重
NEKO_TYPE_WEIGHT = {
    "survivor": 100, "infected": 100, "weapon": 95, "melee": 95, "shotgun": 95,
    "sniper": 95, "rifle": 95, "smg": 95, "pistol": 95,
    "map": 90, "campaign": 90, "vscript": 70, "script": 70,
    "hud": 55, "ui": 55, "particle": 50, "sound": 45, "music": 45,
    "texture": 30, "material": 30, "model": 25,
}

# 官方标签的特异性权重（与 NekoVpk 权重同一量纲，便于合并打分）
CONTENT_WEIGHT = {
    "campaign": 100, "map": 100, "versus": 100, "survival": 100, "scavenge": 100,
    "survivor": 95, "bossinfected": 95, "commoninfected": 95, "infected": 95,
    "weapon": 90, "weaponmodel": 90, "melee": 90,
    "script": 75, "vscript": 75,
    "backgroundmovie": 70, "ui": 65, "hud": 65,
    "particle": 60,
    "music": 55, "sound": 55, "voice": 55,
    "skin": 40,
    "prop": 35, "prefab": 30, "model": 30,
    "spray": 20, "material": 35, "texture": 35,
}

# 判定为「强证据冲突」的分数下限 —— 低于此值的差异只算「内容倾向」，不报警
STRONG_SCORE = 70

# --------------------------------------------------------------------------- #
# 第三层：内置启发式规则（内部路径优先，命中即证据）
# --------------------------------------------------------------------------- #
PATH_RULES: list[tuple[str, str, str]] = [
    # (正则, 主分类, 人类可读标签)
    (r"^maps/[^/]+\.bsp$", "campaign", "含地图 BSP"),
    (r"^maps/", "campaign", "maps 目录"),
    (r"^missions/", "campaign", "战役 mission 脚本"),
    (r"^models/survivors/", "survivor", "幸存者模型"),
    (r"^models/infected/", "infected", "感染者模型"),
    (r"^models/weapons/", "weapon", "武器模型"),
    (r"^models/weapons/melee/", "weapon", "近战武器模型"),
    (r"^models/weapons/arms/", "weapon", "手臂模型"),
    (r"^models/props_vehicles/", "model_other", "载具模型"),
    (r"^particles/", "particle", "粒子特效"),
    (r"^sound/", "sound", "音效"),
    (r"^scripts/vscripts/", "script", "VScript 脚本"),
    (r"^scripts/", "script", "脚本"),
    (r"^resource/ui/", "hud", "UI 面板"),
    (r"^resource/", "hud", "resource 界面资源"),
    (r"^panorama/", "hud", "panorama 界面"),
    (r"^materials/vgui/", "hud", "GUI 贴图"),
    (r"^materials/", "material", "材质贴图"),
    (r"^models/", "model_other", "其他模型"),
    (r"^addoninfo\.txt$", "misc", "addon 元数据"),
]

# 文件名关键词（VPK 文件名 / 仓库目录名 / mod 标题），用于无名数字 ID 的兜底
NAME_RULES: list[tuple[str, str, str]] = [
    (r"地图|战役|campaign|map\b", "campaign", "标题含战场关键词"),
    (r"幸存者|survivor|zoey|ellis|coach|nick|rochelle|louis|francis|bill|教练|白毛|女主", "survivor", "标题含幸存者关键词"),
    (r"特感|感染者|infected|hunter|smoker|boomer|jockey|charger|spitter|tank|witch", "infected", "标题含感染者关键词"),
    (r"武器|枪|weapon|gun|ak47|m16|shotgun|sniper|pistol|近战|melee|刀", "weapon", "标题含武器关键词"),
    (r"音效|音乐|sound|music|bgm|语音|voice|配音", "sound", "标题含音效关键词"),
    (r"hud|界面|ui\b|菜单|menu", "hud", "标题含界面关键词"),
    (r"脚本|插件|script|vscript|plugin|nut\b", "script", "标题含脚本关键词"),
    (r"粒子|特效|particle|effect|trail|光束", "particle", "标题含特效关键词"),
    (r"材质|贴图|texture|material|重涂|retexture", "material", "标题含材质关键词"),
    (r"模型|model|mesh|替换模型", "model_other", "标题含模型关键词"),
]

# 已知的特殊缩写 / 圈内黑话
SHORTHAND = [
    (r"\bnpc\b|npc替换|替换npc", "model_other", "NPC 替换"),
    (r"kiyo|anim\b|animation|动作|动画", "survivor", "动作动画类（通常配幸存者）"),
]


# --------------------------------------------------------------------------- #
# 标签库加载
# --------------------------------------------------------------------------- #
class TagLib:
    """NekoVpk 素材级标签库。"""

    def __init__(self, path: str = ""):
        self.path = path
        self.tags: dict[str, dict] = {}
        self._rules: list[tuple[re.Pattern, str, list[str]]] = []
        self.loaded = False
        self.error = ""
        # 规则按「路径首段」分桶的索引，惰性构建（见 _ensure_index）
        self._by_seg: dict[str, list[tuple[re.Pattern, str, list[str]]]] = {}
        self._generic: list[tuple[re.Pattern, str, list[str]]] = []
        self._index_n = -1
        if path:
            self.load(path)

    @staticmethod
    def _strip_jsonc(text: str) -> str:
        """剥掉 JSONC 的注释与尾随逗号，**感知字符串边界**。

        ⚠️ 原实现是三行正则：
            re.sub(r"/\\*.*?\\*/", "", text)      # 块注释
            re.sub(r"(?m)//.*$", "", text)         # 行注释
            re.sub(r",(\\s*[}\\]])", r"\\1", text)  # 尾随逗号
        后两条都**不认字符串**：`{"a": "http://x"}` 会被剥成 `{"a": "http:`，
        整个标签库解析失败 → build_taglib() 静默降级到 29 条内置规则，
        界面上 204 条规则带来的素材标签大面积消失，而用户只看到一句
        「标签库解析失败」。
        实测当前真实标签库（753 行 / 21359 字符）里含「引号内 //」的行数为 **0**，
        所以这是潜伏缺陷 —— 但标签库是**第三方文件**，用户手改加一个 URL
        或一条带引号的注释就会触发。改成逐字符扫描后，字符串内部的
        `//`、`/*`、`,` 一律不受影响。
        """
        out: list[str] = []
        i, n = 0, len(text)
        in_str = False
        while i < n:
            c = text[i]
            if in_str:
                out.append(c)
                if c == "\\" and i + 1 < n:
                    # 转义序列整体搬运，避免 \" 被误判成字符串结束
                    out.append(text[i + 1])
                    i += 2
                    continue
                if c == '"':
                    in_str = False
                i += 1
                continue
            if c == '"':
                in_str = True
                out.append(c)
                i += 1
                continue
            if c == "/" and i + 1 < n and text[i + 1] == "/":
                i = text.find("\n", i)
                if i < 0:
                    break
                continue
            if c == "/" and i + 1 < n and text[i + 1] == "*":
                j = text.find("*/", i + 2)
                i = n if j < 0 else j + 2
                # 块注释被整段删除后可能把前后 token 粘在一起，补一个空格
                if out and out[-1] not in " \t\n\r[{,":
                    out.append(" ")
                continue
            out.append(c)
            i += 1
        # 尾随逗号：只在「字符串外、且后面只跟空白与 } 或 ]」时删。
        # 用同一套扫描重跑一遍，避免上一步产出的文本里字符串内容被误伤。
        res: list[str] = []
        i, n = 0, len(out)
        in_str = False
        while i < n:
            c = out[i]
            if in_str:
                res.append(c)
                if c == "\\" and i + 1 < n:
                    res.append(out[i + 1])
                    i += 2
                    continue
                if c == '"':
                    in_str = False
                i += 1
                continue
            if c == '"':
                in_str = True
                res.append(c)
                i += 1
                continue
            if c == ",":
                j = i + 1
                while j < n and out[j] in " \t\r\n":
                    j += 1
                if j < n and out[j] in "}]":
                    i += 1
                    continue
            res.append(c)
            i += 1
        return "".join(res)

    def load(self, path: str) -> bool:
        try:
            with open(path, "rb") as fp:
                raw = fp.read().decode("utf-8-sig", "replace")
            data = json.loads(self._strip_jsonc(raw))
        except Exception as ex:
            self.error = "标签库解析失败: %s" % ex
            return False
        for tag, spec in (data or {}).items():
            if not isinstance(spec, dict):
                continue
            # "files" 必须逐条是字符串。外部标签库是**第三方文件**（NekoVpk 的
            # TaggedAssets.jsonc），手写时很容易写成 {"files": "sound/**"} ——
            # 字符串是可迭代的，直接 for pat in files: 会逐字符生成 8 条垃圾规则
            # （^s$ / ^o$ / ^u$ / ^n$ / ^d$ / ^/$ / ^*$ / ^*$）。这些规则几乎匹配
            # 不到东西，却会白白增加 8 次 match，而且只字不提哪里写错了。
            raw_files = spec.get("files") or []
            if isinstance(raw_files, str):
                raw_files = [raw_files]
            files = [p for p in raw_files if isinstance(p, str) and p.strip()]
            types = spec.get("type") or []
            if isinstance(types, str):
                types = [types]
            color = spec.get("color", "")
            self.tags[tag] = {"files": files, "type": types,
                              "color": color if isinstance(color, str) else ""}
            for pat in files:
                try:
                    rx = re.compile(glob_to_regex(pat))
                except re.error as ex:
                    # 单条规则写坏不该让整个标签库报废：跳过并记进 error
                    self.error = (self.error + "；" if self.error else "") + \
                        "标签「%s」的规则 %r 无法编译: %s" % (tag, pat, ex)
                    continue
                self._rules.append((rx, tag, types))
        self.loaded = True
        return True

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    @staticmethod
    def _rule_bucket(rx: re.Pattern) -> str:
        """取出规则的「首段字面量」，用于把规则分桶。

        glob_to_regex 生成的模式形如 ^models/survivors/xxx\\.[^/]*$，
        开头到第一个 / 之间一定是纯字面量（re.escape 之后仍可原样取回）。
        这类规则只可能匹配首段等于该字面量的路径，因此可以按首段建桶，
        把 204 条规则的全量比对缩成「同段规则」的小集合。
        返回 "" 表示通配规则（以 * ? [ 开头），必须对所有路径都试。
        """
        pat = rx.pattern
        if not pat.startswith("^"):
            return ""
        end = pat.find("/", 1)
        head = pat[1:] if end < 0 else pat[1:end]
        if not head or "\\" in head or "[" in head or "(" in head or "*" in head:
            return ""
        return head

    def _ensure_index(self) -> None:
        """惰性构建分桶索引。规则数变化（例如 load 之后）会自动重建。"""
        n = len(self._rules)
        if self._index_n == n:
            return
        by_seg: dict[str, list[tuple[re.Pattern, str, list[str]]]] = {}
        generic: list[tuple[re.Pattern, str, list[str]]] = []
        for item in self._rules:
            seg = self._rule_bucket(item[0])
            if seg:
                by_seg.setdefault(seg, []).append(item)
            else:
                generic.append(item)
        self._by_seg = by_seg
        self._generic = generic
        self._index_n = n

    def match(self, vpaths: list[str]) -> dict[str, list[str]]:
        """返回 {标签: [命中的内部路径]} —— **全部命中，不做条数截断**。

        性能：原先对每个文件都跑一遍全部规则（实测 204 条规则 × 数万文件
        ≈ 千万次正则匹配，是整条扫描链上最烫的一处）。现在按路径首段分桶，
        同段路径只与「同段规则 + 通配规则」比对，规则集通常缩到个位数。

        ⚠️ 这里曾经有 `if len(hits[tag]) < 12` 的硬截断，已删除。它不是「少显示
        几行」那么轻：调用方全都把这 12 条当全集在用 ——
        components.component_tree() 拿它算 count/size、拿它标记「已归组」，
        web/app.js 的勾选也只遍历这份列表。后果实测 19 处少报：
        某 mod 的 Skybox 显示 12 条、实际 48 条，
        被藏起来的 36 个文件掉进「目录」组、还被贴上「未命中任何标签」的假标签；
        用户勾上「Skybox」点删除，只删掉 12 个，mod 里还剩 36 个天空盒。
        界面在撒谎，而且撒的是「删干净了」这种最危险的谎。
        """
        if not self._rules:
            return {}
        self._ensure_index()
        by_seg = self._by_seg
        generic = self._generic
        hits: dict[str, list[str]] = {}
        cand_cache: dict[str, list] = {}
        for vp in vpaths:
            i = vp.find("/")
            seg = vp if i < 0 else vp[:i]
            cand = cand_cache.get(seg)
            if cand is None:
                cand = by_seg.get(seg)
                if cand is None:
                    cand = generic
                elif generic:
                    cand = cand + generic
                cand_cache[seg] = cand
            for rx, tag, _t in cand:
                if rx.match(vp):
                    lst = hits.get(tag)
                    if lst is None:
                        hits[tag] = [vp]
                    else:
                        lst.append(vp)
        return hits

    # ⚠️ 这里原本还有一个 category_of(hits) 方法，已删除（2026-10-16）。
    #    它与 scan.py 的分类打分是**两套并存的逻辑**：都是
    #    `spec = tags.get(tag)` → `for tp in spec["type"]` → `NEKO_TYPE_MAP.get(...)`
    #    → `NEKO_TYPE_WEIGHT.get(...)` 打分。实测全仓库引用计数为 1（仅定义处），
    #    即**从未被调用过**，是最大的维护陷阱 —— 读代码的人会以为改分类口径
    #    要动这里，实际上真正生效的是 scan.py 里的那份。
    #    本文件保留 NEKO_TYPE_MAP / NEKO_TYPE_WEIGHT 两个常量，因为 scan.py 在用。


def glob_to_regex(pat: str) -> str:
    """把 VPKEdit 风格的 glob 转成正则：* 不跨目录分隔符，** 跨目录。

    ⚠️ `**` 必须单独处理，否则它会被当成两个 `*` 拼出 `[^/]*[^/]*`，
    等价于单个 `*` —— 规则静默失效，不报错、不留痕。
    真实标签库里有 7 条这样的规则：maps/**、sound/**、particles/**、
    scripts/vscripts/**、materials/skybox/**、models/spb/reanims/**、models/xdreanims/**。
    实测 `sound/**` 匹配不到 `sound/player/survivor/voice/zoey/idle.wav`、
    `maps/**` 匹配不到 `maps/dead/airport/c1m1_hotel.bsp` —— 于是 Sound / Map /
    Particle / Skybox / VScript 这些标签大面积漏报，界面显示「未命中任何标签」。
    修好后 206 个 mod 里有 66 个标签变完整（多出 421 条命中）。

    `**/` 按「零个或多个目录」处理（`a/**/b` 同时匹配 `a/b` 与 `a/x/b`），
    这也是各版本 glob 实现的通行约定。不含 `**` 的模式生成的 regex 与旧版
    逐字符相同（197/197 已核对），改动不会波及其他规则。
    """
    out, i = [], 0
    pat = pat.strip().lower().replace("\\", "/")
    n = len(pat)
    while i < n:
        c = pat[i]
        if c == "*" and i + 1 < n and pat[i + 1] == "*":
            i += 2
            if i < n and pat[i] == "/":
                out.append("(?:.*/)?")      # **/ 可匹配零个目录
                i += 1
            else:
                out.append(".*")            # ** 跨分隔符
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pat.find("]", i)
            if j > 0:
                out.append(pat[i:j + 1])
                i = j
            else:
                out.append(r"\[")
        else:
            out.append(re.escape(c))
        i += 1
    return "^" + "".join(out) + "$"


# --------------------------------------------------------------------------- #
# 内置标签库（NekoVpk TaggedAssets.jsonc 的完整数据，75 标签 / 204 条路径规则）
#
# 数据来源：Starfelll/NekoVpk（GPL-3.0）的 TaggedAssets.jsonc —— 由脚本从该
# jsonc 转成 Python 字面量内联（只保留 load() 用到的 files / type / color 三键），
# 不再依赖任何外部文件：源码模式与 exe 开箱即完整标签覆盖。
#
# 为什么还保留外部文件加载：build_taglib(prefer_path) 仍优先读用户放在
# data/taglib/ 下的同名文件 —— 用户可以用更新版的 NekoVpk 标签库覆盖内置版。
# 内置是兜底，不是唯一入口。
#
# ⚠️ 许可提示：本常量的数据是 GPL-3.0 项目的产物，随 MIT 许可的本项目分发
#    （聚合分发：独立数据、非链接）。README「第三方组件与许可」有完整说明。
# --------------------------------------------------------------------------- #
BUILTIN_TAGS: dict[str, dict] = {'AK47': {'files': ['models/w_models/weapons/w_rifle_ak47.*',
                        'models/v_models/v_rifle_AK47.*'],
              'type': ['Weapon', 'Rifle'],
              'color': 'Yellow'},
     'AWP': {'files': ['models/w_models/weapons/w_sniper_awp.*',
                       'models/v_models/v_snip_awp.*'],
             'type': ['Weapon', 'Sniper'],
             'color': 'Yellow'},
     'Adrenaline': {'files': ['models/w_models/weapons/w_eq_adrenaline.*',
                              'models/v_models/v_adrenaline.*'],
                    'type': ['Item'],
                    'color': 'Green'},
     'AutoShotgun': {'files': ['models/w_models/weapons/w_autoshot_m4super.*',
                               'models/v_models/v_autoshotgun.*'],
                     'type': ['Weapon', 'Shotgun'],
                     'color': 'Yellow'},
     'BaseballBat': {'files': ['models/weapons/melee/v_bat.*',
                               'models/weapons/melee/w_bat.*'],
                     'type': ['Weapon', 'Melee'],
                     'color': 'Green'},
     'BileBomb': {'files': ['models/w_models/weapons/w_eq_bile_flask.*',
                            'models/v_models/v_bile_flask.*'],
                  'type': ['Weapon', 'Throwable'],
                  'color': 'Green'},
     'Bill': {'files': ['materials/vgui/s_panel_namvet.vtf',
                        'materials/vgui/s_panel_namvet_incap.vtf',
                        'materials/vgui/select_bill.vtf',
                        'materials/vgui/s_panel_namvet.vmt',
                        'materials/vgui/s_panel_namvet_incap.vmt',
                        'materials/vgui/select_bill.vmt',
                        'models/survivors/survivor_namvet.*',
                        'models/weapons/arms/v_arms_bill.*',
                        'models/survivors/namvet/namvet_deathpose.*'],
              'type': ['Survivor'],
              'color': 'Red'},
     'Boomer': {'files': ['models/infected/boomer.*',
                          'models/infected/boomette.*',
                          'models/infected/boomer_l4d1.*',
                          'models/v_models/weapon/v_claw_boomer.*',
                          'models/v_models/weapon/v_claw_boomer_l4d1.*'],
                'type': ['SpecialInfected'],
                'color': 'Green'},
     'Chainsaw': {'files': ['models/weapons/melee/v_chainsaw.*',
                            'models/weapons/melee/w_chainsaw.*'],
                  'type': ['Weapon', 'Melee'],
                  'color': 'Green'},
     'Charger': {'files': ['models/infected/charger.*',
                           'models/weapons/arms/v_charger_arms.*'],
                 'type': ['SpecialInfected'],
                 'color': 'Green'},
     'ChromeShotgun': {'files': ['models/w_models/weapons/w_pumpshotgun_A.*',
                                 'models/v_models/v_shotgun_chrome.*'],
                       'type': ['Weapon', 'Shotgun'],
                       'color': 'Yellow'},
     'Coach': {'files': ['materials/vgui/s_panel_coach.vtf',
                         'materials/vgui/s_panel_coach_incap.vtf',
                         'materials/vgui/s_panel_lobby_coach.vtf',
                         'materials/vgui/s_panel_coach.vmt',
                         'materials/vgui/s_panel_coach_incap.vmt',
                         'materials/vgui/s_panel_lobby_coach.vmt',
                         'models/survivors/survivor_coach.*',
                         'models/weapons/arms/v_arms_coach_new.*'],
               'type': ['Survivor'],
               'color': 'Pink'},
     'Cola': {'files': ['models/w_models/weapons/w_cola.*', 'models/v_models/v_cola.*'],
              'type': ['Item'],
              'color': 'Green'},
     'CommonInfected': {'files': ['models/infected/common*'], 'type': [], 'color': 'Green'},
     'CricketBat': {'files': ['models/weapons/melee/v_cricket_bat.*',
                              'models/weapons/melee/w_cricket_bat.*'],
                    'type': ['Weapon', 'Melee'],
                    'color': 'Green'},
     'Crowbar': {'files': ['models/weapons/melee/v_crowbar.*',
                           'models/weapons/melee/w_crowbar.*'],
                 'type': ['Weapon', 'Melee'],
                 'color': 'Green'},
     'Deagle': {'files': ['models/w_models/weapons/w_desert_eagle.*',
                          'models/v_models/v_desert_eagle.*'],
                'type': ['Weapon', 'Pistol'],
                'color': 'Yellow'},
     'Defibrillator': {'files': ['models/w_models/weapons/w_eq_defibrillator.*',
                                 'models/v_models/v_defibrillator.*'],
                       'type': ['Item'],
                       'color': 'Green'},
     'ElectricGuitar': {'files': ['models/weapons/melee/v_electric_guitar.*',
                                  'models/weapons/melee/w_electric_guitar.*'],
                        'type': ['Weapon', 'Melee'],
                        'color': 'Green'},
     'Ellis': {'files': ['materials/vgui/s_panel_mechanic.vtf',
                         'materials/vgui/s_panel_mechanic_incap.vtf',
                         'materials/vgui/s_panel_lobby_mechanic.vtf',
                         'materials/vgui/s_panel_mechanic.vmt',
                         'materials/vgui/s_panel_mechanic_incap.vmt',
                         'materials/vgui/s_panel_lobby_mechanic.vmt',
                         'models/survivors/survivor_mechanic.*',
                         'models/weapons/arms/v_arms_mechanic_new.*'],
               'type': ['Survivor'],
               'color': 'Purple'},
     'ExplosiveAmmo': {'files': ['models/w_models/weapons/w_eq_explosive_ammopack.*',
                                 'models/v_models/v_explosive_ammopack.*',
                                 'models/props/terror/exploding_ammo.*'],
                       'type': ['Item'],
                       'color': 'Green'},
     'ExplosiveBarrel': {'files': ['models/props_industrial/barrel_fuel.*',
                                   'models/props_industrial/barrel_fuel_parta.*',
                                   'models/props_industrial/barrel_fuel_partb.*'],
                         'type': ['Item'],
                         'color': 'Green'},
     'Fireaxe': {'files': ['models/weapons/melee/v_fireaxe.*',
                           'models/weapons/melee/w_fireaxe.*'],
                 'type': ['Weapon', 'Melee'],
                 'color': 'Green'},
     'Fireworks': {'files': ['models/props_junk/explosive_box001.*'],
                   'type': ['Item'],
                   'color': 'Green'},
     'Francis': {'files': ['materials/vgui/s_panel_biker.vtf',
                           'materials/vgui/s_panel_biker_incap.vtf',
                           'materials/vgui/select_francis.vtf',
                           'materials/vgui/s_panel_biker.vmt',
                           'materials/vgui/s_panel_biker_incap.vmt',
                           'materials/vgui/select_francis.vmt',
                           'models/survivors/survivor_biker.*',
                           'models/weapons/arms/v_arms_francis.*',
                           'models/survivors/survivor_biker_light.*'],
                 'type': ['Survivor'],
                 'color': 'Violet'},
     'FryingPan': {'files': ['models/weapons/melee/v_frying_pan.*',
                             'models/weapons/melee/w_frying_pan.*'],
                   'type': ['Weapon', 'Melee'],
                   'color': 'Green'},
     'GasCan': {'files': ['models/props_junk/gascan001a.*'],
                'type': ['Item'],
                'color': 'Green'},
     'GnomeChompski': {'files': ['models/props_junk/gnome.*',
                                 'models/weapons/melee/v_gnome.*'],
                       'type': ['Item'],
                       'color': 'Green'},
     'GolfClub': {'files': ['models/weapons/melee/v_golfclub.*',
                            'models/weapons/melee/w_golfclub.*'],
                  'type': ['Weapon', 'Melee'],
                  'color': 'Green'},
     'GrenadeLauncher': {'files': ['models/w_models/weapons/w_grenade_launcher.*',
                                   'models/v_models/v_grenade_launcher.*'],
                         'type': ['Weapon'],
                         'color': 'Yellow'},
     'Hunter': {'files': ['models/infected/hunter.*',
                          'models/infected/hunter_l4d1.*',
                          'models/v_models/weapons/v_claw_hunter.*',
                          'models/v_models/weapons/v_claw_hunter_l4d1.*'],
                'type': ['SpecialInfected'],
                'color': 'Green'},
     'HuntingRifle': {'files': ['models/w_models/weapons/w_sniper_mini14.*',
                                'models/v_models/v_huntingrifle.*'],
                      'type': ['Weapon', 'Sniper'],
                      'color': 'Yellow'},
     'IncendiaryAmmo': {'files': ['models/w_models/weapons/w_eq_incendiary_ammopack.*',
                                  'models/v_models/v_incendiary_ammopack.*',
                                  'models/props/terror/incendiary_ammo.*'],
                        'type': ['Item'],
                        'color': 'Green'},
     'Jockey': {'files': ['models/infected/jockey.*',
                          'models/weapons/arms/v_jockey_arms.*'],
                'type': ['SpecialInfected'],
                'color': 'Green'},
     'Katana': {'files': ['models/weapons/melee/v_katana.*',
                          'models/weapons/melee/w_katana.*'],
                'type': ['Weapon', 'Melee'],
                'color': 'Green'},
     'Knife': {'files': ['models/v_models/v_knife_t.*',
                         'models/w_models/weapons/w_knife_t.*'],
               'type': ['Weapon', 'Melee'],
               'color': 'Green'},
     'LaserSight': {'files': ['models/w_models/weapons/w_laser_sights.*'],
                    'type': ['Item'],
                    'color': 'Green'},
     'Louis': {'files': ['materials/vgui/s_panel_manager.vtf',
                         'materials/vgui/s_panel_manager_incap.vtf',
                         'materials/vgui/select_louis.vtf',
                         'materials/vgui/s_panel_manager.vmt',
                         'materials/vgui/s_panel_manager_incap.vmt',
                         'materials/vgui/select_louis.vmt',
                         'models/survivors/survivor_manager.*',
                         'models/weapons/arms/v_arms_louis.*'],
               'type': ['Survivor'],
               'color': 'Indigo'},
     'M16A1': {'files': ['models/w_models/weapons/w_rifle_m16a2.*',
                         'models/v_models/v_rifle.*'],
               'type': ['Weapon', 'Rifle'],
               'color': 'Yellow'},
     'M60': {'files': ['models/w_models/weapons/w_m60.*', 'models/v_models/v_m60.*'],
             'type': ['Weapon'],
             'color': 'Yellow'},
     'MP5': {'files': ['models/w_models/weapons/w_smg_mp5.*',
                       'models/v_models/v_smg_mp5.*'],
             'type': ['Weapon', 'SMG'],
             'color': 'Yellow'},
     'Mac-10': {'files': ['models/w_models/weapons/w_smg_a.*',
                          'models/v_models/v_silenced_smg.*'],
                'type': ['Weapon', 'SMG'],
                'color': 'Yellow'},
     'Machete': {'files': ['models/weapons/melee/v_machete.*',
                           'models/weapons/melee/w_machete.*'],
                 'type': ['Weapon', 'Melee'],
                 'color': 'Green'},
     'Map': {'files': ['maps/**'], 'type': [], 'color': 'Teal'},
     'Medkit': {'files': ['models/w_models/weapons/w_eq_Medkit.*',
                          'models/v_models/v_medkit.*'],
                'type': ['Item'],
                'color': 'Green'},
     'MilitarySniper': {'files': ['models/w_models/weapons/w_sniper_military.*',
                                  'models/v_models/v_sniper_military.*'],
                        'type': ['Weapon', 'Sniper'],
                        'color': 'Yellow'},
     'Molotov': {'files': ['models/w_models/weapons/w_eq_molotov.*',
                           'models/v_models/v_molotov.*'],
                 'type': ['Weapon', 'Throwable'],
                 'color': 'Green'},
     'Nick': {'files': ['materials/vgui/s_panel_gambler.vtf',
                        'materials/vgui/s_panel_gambler_incap.vtf',
                        'materials/vgui/s_panel_lobby_gambler.vtf',
                        'materials/vgui/s_panel_gambler.vmt',
                        'materials/vgui/s_panel_gambler_incap.vmt',
                        'materials/vgui/s_panel_lobby_gambler.vmt',
                        'models/survivors/survivor_gambler.*',
                        'models/weapons/arms/v_arms_gambler_new.*'],
              'type': ['Survivor'],
              'color': 'Blue'},
     'Nightstick': {'files': ['models/weapons/melee/v_tonfa.*',
                              'models/weapons/melee/w_tonfa.*'],
                    'type': ['Weapon', 'Melee'],
                    'color': 'Green'},
     'OxygenTank': {'files': ['models/props_equipment/oxygentank01.*'],
                    'type': ['Item'],
                    'color': 'Green'},
     'PainPills': {'files': ['models/w_models/weapons/w_eq_painpills.*',
                             'models/v_models/v_painpills.*'],
                   'type': ['Item'],
                   'color': 'Green'},
     'Particle': {'files': ['particles/**'], 'type': [], 'color': 'Orange'},
     'PipeBomb': {'files': ['models/w_models/weapons/w_eq_pipebomb.*',
                            'models/v_models/v_pipebomb.*'],
                  'type': ['Weapon', 'Throwable'],
                  'color': 'Green'},
     'Pistol': {'files': ['models/w_models/weapons/w_pistol_A.*',
                          'models/w_models/weapons/w_pistol_A_dual.*',
                          'models/w_models/weapons/w_pistol_B.*',
                          'models/v_models/v_pistolA.*',
                          'models/v_models/v_dual_pistolA.*'],
                'type': ['Weapon', 'Pistol'],
                'color': 'Yellow'},
     'Pitchfork': {'files': ['models/weapons/melee/v_pitchfork.*',
                             'models/weapons/melee/w_pitchfork.*'],
                   'type': ['Weapon', 'Melee'],
                   'color': 'Green'},
     'PropaneTank': {'files': ['models/props_junk/propanecanister001a.*'],
                     'type': ['Item'],
                     'color': 'Green'},
     'PumpShotgun': {'files': ['models/w_models/weapons/w_shotgun.*',
                               'models/v_models/v_pumpshotgun.*'],
                     'type': ['Weapon', 'Shotgun'],
                     'color': 'Yellow'},
     'Rochelle': {'files': ['materials/vgui/s_panel_producer.vtf',
                            'materials/vgui/s_panel_producer_incap.vtf',
                            'materials/vgui/s_panel_lobby_producer.vtf',
                            'materials/vgui/s_panel_producer.vmt',
                            'materials/vgui/s_panel_producer_incap.vmt',
                            'materials/vgui/s_panel_lobby_producer.vmt',
                            'models/survivors/survivor_producer.*',
                            'models/weapons/arms/v_arms_producer_new.*'],
                  'type': ['Survivor'],
                  'color': 'LightBlue'},
     'SG552': {'files': ['models/w_models/weapons/w_rifle_sg552.*',
                         'models/v_models/v_rif_sg552.*'],
               'type': ['Weapon', 'Rifle'],
               'color': 'Yellow'},
     'SPAS-12': {'files': ['models/w_models/weapons/w_shotgun_spas.*',
                           'models/v_models/v_shotgun_spas.*'],
                 'type': ['Weapon', 'Shotgun'],
                 'color': 'Yellow'},
     'SPR': {'files': ['models/spb/reanims/**'], 'type': ['Animation'], 'color': 'Red'},
     'Scar': {'files': ['models/w_models/weapons/w_desert_rifle.*',
                        'models/v_models/v_desert_rifle.*'],
              'type': ['Weapon', 'Rifle'],
              'color': 'Yellow'},
     'Scout': {'files': ['models/w_models/weapons/w_sniper_scout.*',
                         'models/v_models/v_snip_scout.*'],
               'type': ['Weapon', 'Sniper'],
               'color': 'Yellow'},
     'Shovel': {'files': ['models/weapons/melee/v_shovel.*',
                          'models/weapons/melee/w_shovel.*'],
                'type': ['Weapon', 'Melee'],
                'color': 'Green'},
     'Skybox': {'files': ['materials/skybox/**'], 'type': [], 'color': 'Purple'},
     'Smoker': {'files': ['models/infected/smoker.*',
                          'models/infected/smoker_l4d1.*',
                          'models/v_models/weapons/v_claw_smoker.*',
                          'models/v_models/weapons/v_claw_smoker_l4d1.*'],
                'type': ['SpecialInfected'],
                'color': 'Green'},
     'Sound': {'files': ['sound/**'], 'type': [], 'color': 'Red'},
     'Spitter': {'files': ['models/infected/spitter.*',
                           'models/weapons/arms/v_spitter_arms.*'],
                 'type': ['SpecialInfected'],
                 'color': 'Green'},
     'Spray': {'files': ['scripts/sprays_manifest.txt'], 'type': [], 'color': 'Amber'},
     'Tank': {'files': ['models/infected/hulk.*',
                        'models/infected/hulk_dlc3.*',
                        'models/infected/hulk_l4d1.*',
                        'models/v_models/weapons/v_claw_hulk.*',
                        'models/v_models/weapons/v_claw_hulk_dlc3.*',
                        'models/v_models/weapons/v_claw_hulk_l4d1.*'],
              'type': ['SpecialInfected'],
              'color': 'Green'},
     'Uzi': {'files': ['models/w_models/weapons/w_smg_uzi.*', 'models/v_models/v_smg.*'],
             'type': ['Weapon', 'SMG'],
             'color': 'Yellow'},
     'VScript': {'files': ['scripts/vscripts/**'], 'type': [], 'color': 'Amber'},
     'Witch': {'files': ['models/infected/witch.*', 'models/infected/witch_bride.*'],
               'type': ['SpecialInfected'],
               'color': 'Green'},
     'XDR': {'files': ['models/xdreanims/**'], 'type': ['Animation'], 'color': 'Pink'},
     'Zoey': {'files': ['materials/vgui/s_panel_teenangst.vtf',
                        'materials/vgui/s_panel_teenangst_incap.vtf',
                        'materials/vgui/select_zoey.vtf',
                        'materials/vgui/s_panel_teenangst.vmt',
                        'materials/vgui/s_panel_teenangst_incap.vmt',
                        'materials/vgui/select_zoey.vmt',
                        'models/survivors/survivor_teenangst.*',
                        'models/weapons/arms/v_arms_zoey.*',
                        'models/survivors/survivor_teenangst_light.*'],
              'type': ['Survivor'],
              'color': 'Cyan'}}


def build_taglib(prefer_path: str = "") -> TagLib:
    """优先加载外部标签库；失败则退回内置。返回状态写进 .path/.error 供审计。"""
    if prefer_path and os.path.isfile(prefer_path):
        lib = TagLib(prefer_path)
        if lib.loaded:
            return lib
    lib = TagLib()
    # 深拷贝：原先这里直接 lib.tags = BUILTIN_TAGS，等于把模块级字典本身
    # 交出去。任何调用方（包括将来的调试代码）往 lib.tags 里塞一个标签，
    # 都会永久污染模块级常量 —— 同一进程后续所有 build_taglib() 都带着它。
    # 实测 t1.tags is BUILTIN_TAGS 为 True。
    lib.tags = copy.deepcopy(BUILTIN_TAGS)
    for tag, spec in BUILTIN_TAGS.items():
        for pat in spec["files"]:
            lib._rules.append((re.compile(glob_to_regex(pat)), tag, spec["type"]))
    lib.loaded = True
    lib.path = "(内置完整标签库 %d 条规则，源自 NekoVpk GPL-3.0)" % len(lib._rules)
    if prefer_path:
        lib.error = "外部标签库不可用（%s），已降级为内置" % prefer_path
    return lib
