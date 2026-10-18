# -*- coding: utf-8 -*-
"""验证/诊断脚本共用的「真实环境配置」读取口。

为什么单独抽一个模块、而不在每份脚本里写死路径：
  * 主项目的 `.build/` 会被 tools/sync_to_github.py 整目录同步到公开仓库
    （SYNC_MAP 里有 `(".build", ".build")`），而它 commit 前的敏感扫描会把
    「带盘符的 Steam 库路径 / 用户目录 / 真实 mod 名」判成泄漏并**中止同步**；
    写死路径就等于每次同步都要人工放行。
  * 顺带的好处：脚本换台机器也能直接跑，不用改代码。

取值优先级（全部是**只读**，本模块不写任何文件）：
  1. 环境变量 `L4D2MS_CONFIG` 指向的 JSON（临时换环境用）
  2. 本项目的 data/config.json（data/ 不在同步白名单里）
  3. 同级目录的 data/config.json —— 真实壳仓库里没有 data/，
     靠这条回落到主项目的配置，验证脚本在两边跑结果一致
  4. 都没有就返回空串，调用方自己 SKIP（绝不猜路径）
"""
import json
import os

BUILD_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BUILD_DIR)
KEYS = ("game_dir", "repo_dir", "taglib")


def _read_json(path):
    """读 JSON 对象；文件不存在 / 坏掉 / 不是对象一律给空 dict（不抛）。"""
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def config_candidates():
    """按优先级列出要试的 config.json 路径。"""
    out = []
    env = (os.environ.get("L4D2MS_CONFIG") or "").strip()
    if env:
        out.append(env)
    out.append(os.path.join(ROOT, "data", "config.json"))
    # 同级目录：壳仓库 <-> 主项目 互为兄弟，谁的 data/ 在就用谁的
    parent = os.path.dirname(ROOT)
    try:
        for name in sorted(os.listdir(parent)):
            cand = os.path.join(parent, name, "data", "config.json")
            if os.path.isfile(cand):
                out.append(cand)
    except OSError:
        pass
    seen, uniq = set(), []
    for p in out:
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def real_cfg(require=True):
    """返回 {"game_dir","repo_dir","taglib"}。

    三项各自独立回落：哪个键在第一个有该键的配置里非空就用它。
    require=True 时，三个键**全空**会打印一条可读的提示（不是异常），
    让调用方照常走 SKIP 分支，不静默跑出「0 条发现」这种假结论。
    """
    out = {k: "" for k in KEYS}
    used = ""
    for path in config_candidates():
        cfg = _read_json(path)
        got = False
        for k in KEYS:
            if not out[k]:
                v = cfg.get(k)
                if isinstance(v, str) and v.strip():
                    out[k] = v.strip()
                    got = True
        if got:
            used = path
        if all(out[k] for k in KEYS):
            break
    out["_from"] = used
    if require and not out["game_dir"]:
        print("  [提示] 没读到 data/config.json 里的 game_dir，真实环境相关检查会 SKIP。")
        print("         想临时指定：set L4D2MS_CONFIG=<某份 config.json 的路径>")
    return out


def game_sub(cfg, *parts):
    """在游戏目录下拼子路径；game_dir 为空则返回空串（与 core.paths.game_sub 同义）。"""
    g = (cfg or {}).get("game_dir") or ""
    return os.path.join(g, *parts) if g else ""


def addons_dir(cfg):
    return game_sub(cfg, "addons")


def addonlist_path(cfg):
    return game_sub(cfg, "addonlist.txt")
