# -*- coding: utf-8 -*-
"""mod 管理：启用/停用（addonlist.txt）、批量开关、隔离卸载、还原。

纪律（不可绕过）：
  * 改 addonlist.txt 前先备份原件；
  * 删除一律走回收站，绝不用 rm/-rf；
  * 「卸载」默认 = 移出视野（移入隔离区），可一键还原，不销毁内容；
  * 批量操作先出清单，单批不超过 10 项。
"""
from __future__ import annotations

import os
import re
import shutil
import threading

from . import safety, scan

BATCH_LIMIT = 10

# addonlist.txt 的「读 → 改 → 写」必须整体互斥：服务端是 ThreadingHTTPServer，
# 两个请求同时改不同的 key 时会各自读到同一份旧表，后写的把先写的抹掉（丢更新）。
# 用 RLock 而不是普通 Lock：set_enabled / isolate_mods 持锁期间还会调
# write_addonlist，同一线程必须能重入，普通 Lock 会把本线程自己锁死。
_ADDONLIST_LOCK = threading.RLock()

# 只认「"xxx.vpk" "0/1"」这一种条目，与 scan.read_addonlist 的正则保持一致。
# 行内其余内容（缩进、空白、行尾、注释）一律原样保留 —— 玩家的 addonlist.txt
# 里可能有手工加的东西，整文件重建会把它们全吃掉。
_ADDONLIST_ENTRY_RE = re.compile(r'"([^"]+\.vpk)"\s*"([01])"')


# --------------------------------------------------------------------------- #
# addonlist.txt
# --------------------------------------------------------------------------- #
def _read_addonlist_bytes(path: str) -> tuple[str, str]:
    """读 addonlist.txt，返回 (文本, 实际使用的编码)。

    文件不存在时返回 ("", 编码) —— 调用方据此走「新建」分支。

    实现已提到 `safety.read_text_auto`：`scan.py` 也要读同一份文件，
    两边各写一套编码探测的话，一旦候选顺序或兜底策略有出入，就会出现
    「扫描时看到的文件名」与「写回时的文件名」不一致 —— 对玩家而言
    就是文件名被改坏了。这里只做转发，保持原函数名与语义（内部与外部
    调用点都不必改）。
    """
    return safety.read_text_auto(path)


def read_addonlist_text(path: str) -> str:
    """读 addonlist.txt 的原始文本（保持原有签名：只返回 str）。"""
    txt, _enc = _read_addonlist_bytes(path)
    if not txt and not os.path.isfile(path):
        return '"AddonList"\n{\n}\n'
    return txt


def _render_addonlist(old_text: str, mapping: dict[str, bool],
                      drop: "set[str] | frozenset[str]" = frozenset()) -> tuple[str, list[str]]:
    """逐行改写 addonlist.txt：只替换匹配到的条目行，其余行原样保留。

    返回 (新文本, 按出现顺序排列的 .vpk 键列表)。

    为什么要逐行：原实现是「正则扫出所有 .vpk → 重新拼整个文件」，
    于是注释行、非 .vpk 条目、额外 key、空行全被静默丢弃 ——
    玩家手工加进 addonlist.txt 的东西会凭空消失。

    `drop` = 要**整条删除**的 key（隔离/卸载用）。这是必须单独给的一组，
    不能靠「从 mapping 里 pop 掉」来表达删除 —— _render_addonlist 只会
    改写 mapping 里有的条目，不在 mapping 里的条目一律原样保留，
    于是「pop 掉再写回」等于什么都没删（隔离后 addonlist.txt 里那条还在，
    游戏仍会尝试加载一个已经不存在于 addons 的文件）。
    """
    keys: list[str] = []
    seen: set[str] = set()
    # splitlines(True) 保留每行原来的行尾（\r\n / \n / \r 各自保留）
    lines = old_text.splitlines(True)
    out: list[str] = []
    for line in lines:
        ms = list(_ADDONLIST_ENTRY_RE.finditer(line))
        if not ms:
            out.append(line)          # 非条目行：注释 / 空行 / 额外 key —— 原样保留
            continue
        pos = 0
        buf: list[str] = []
        dropped_here = 0
        for m in ms:
            key = m.group(1)
            if key in drop:
                # 整条摘掉：把从本条目起点到终点的整段切走，前后内容保留
                buf.append(line[pos:m.start()])
                pos = m.end()
                dropped_here += 1
                continue
            if key in mapping:
                # 命中要改的 key：只换掉 `"0"` / `"1"` 这一段，缩进与行尾不动
                buf.append(line[pos:m.start(2)])
                buf.append("1" if mapping[key] else "0")
                pos = m.end(2)
            if key not in seen:
                seen.add(key)
                keys.append(key)
        buf.append(line[pos:])
        new_line = "".join(buf)
        # 整行只剩缩进和行尾 = 这行除了被摘掉的条目没别的东西了，一起删掉，
        # 别在文件里留一串空行。
        if dropped_here and not new_line.strip():
            continue
        out.append(new_line)
    # 新增的 key（原文件里没有）：追加到末尾，插在收尾的 `}` 之前。
    # drop 里的 key 绝不能进 extra —— 否则刚删掉又原样追加回来了。
    extra = [k for k in mapping if k not in seen and k not in drop]
    if extra:
        new_lines = ['\t"%s"\t\t"%d"\n' % (k, 1 if mapping[k] else 0) for k in extra]
        insert_at = len(out)
        for i in range(len(out) - 1, -1, -1):
            if out[i].strip() == "}":
                insert_at = i
                break
        out[insert_at:insert_at] = new_lines
        keys.extend(extra)
    return "".join(out), keys


def write_addonlist(path: str, mapping: dict[str, bool], backup: bool = True,
                    drop: "set[str] | frozenset[str]" = frozenset()) -> dict:
    """把 {文件名: 启用} 写回 addonlist.txt。

    保持原有条目顺序（顺序影响加载优先级），新增的追加到末尾；
    非 .vpk 的行（注释、额外 key、空行）逐行原样保留。
    写回时用探测到的编码，避免把 GBK 文件强行转成 UTF-8。

    `drop` = 要从表里**整条删除**的 key（隔离/卸载）。必须显式传进来：
    只把 key 从 mapping 里去掉是删不掉的（见 _render_addonlist 的说明）。
    """
    with _ADDONLIST_LOCK:
        old_text, enc = _read_addonlist_bytes(path)
        # ⚠️ 判据是「读到的文本是空的」，不是「文件不存在」。
        #    原写法 `if not old_text and not os.path.isfile(path)` 只在文件
        #    缺失时补骨架；文件**存在但为空**（0 字节，玩家误删/编辑器存空、
        #    或上次写到一半断电）时既不补骨架、_render_addonlist 又找不到
        #    收尾的 `}` 去插新条目，于是新增条目被追加到文件末尾，写出来是
        #    光秃秃的 `\t"x.vpk"\t\t"1"\n` —— 没有 "AddonList" 标题、没有大括号，
        #    游戏解析不了，玩家看到的是「开了一个 mod，游戏里却一个都没有」。
        if not old_text.strip():
            old_text = '"AddonList"\n{\n}\n'
        new_text, keys = _render_addonlist(old_text, mapping, drop=drop)

        bkp = ""
        if backup and os.path.isfile(path):
            bkp = safety.backup_file(path, tag="addonlist")
        safety.atomic_write_text(path, new_text, encoding=enc)
        safety.log("addonlist-write", "%s（%d 条，启用 %d，删除 %d，编码 %s）备份=%s"
                   % (path, len(keys), sum(1 for v in mapping.values() if v),
                      len(drop), enc, bkp))
        return {"path": path, "backup": bkp, "count": len(keys),
                "enabled": sum(1 for v in mapping.values() if v),
                "dropped": len(drop),
                "text": new_text, "encoding": enc}


def set_enabled(addonlist: str, changes: dict[str, bool]) -> dict:
    """changes = {mod 文件名: 是否启用}。"""
    # ⚠️ 空 changes 直接回失败：下面 `res["ok"] = True` 是无条件的，而
    #    write_addonlist 会把「一个键都没有」的表写回去 —— 等于用空改动
    #    触发一次纯备份 + 重写。上层（app/api.py 的 api_enable）只在这里
    #    拿到 ok=True 才回写内存，空输入根本不该走到那一步。
    if not changes:
        return {"ok": False, "error": "没有需要改的条目（changes 为空）"}
    if len(changes) > BATCH_LIMIT:
        return {"ok": False, "error": "单批最多 %d 项，本批 %d 项；请分批执行"
                                      % (BATCH_LIMIT, len(changes))}
    # 读 → 改 → 写整段持锁：否则两个请求各自读旧表，后写的抹掉先写的（丢更新）
    with _ADDONLIST_LOCK:
        mapping = scan.read_addonlist(addonlist)
        applied = {}
        for k, v in changes.items():
            # 未登记的 mod 也给它登记上（原实现两个分支写得一模一样，
            # 是复制粘贴留下的死分支，行为不变）
            applied[k] = v
            mapping[k] = v
        res = write_addonlist(addonlist, mapping)
    res["ok"] = True
    res["applied"] = applied
    safety.log("enable" if all(changes.values()) else "toggle",
               "改 %d 项: %s" % (len(changes), ", ".join(
                   "%s=%s" % (k, "开" if v else "关") for k, v in list(applied.items())[:10])))
    return res


def batch_by_mods(addonlist: str, mods: list[dict], enable: bool) -> dict:
    """按一批 mod 记录批量开关（自动分批，每批 ≤ BATCH_LIMIT）。

    逐块提交，**不整体回滚** —— 但把「哪几块已经生效」如实标出来，
    避免上层看到 ok=False 就以为整批没动。`applied_blocks` = 已生效块数，
    `pending_blocks` = 因前面失败而未执行的块数。
    """
    results = []
    # ⚠️ 去重：同一个 key 在一批里出现两次（前端把两个勾选项算重）时，
    #    第一次写入成功、第二次再写一次同样的值 —— 表面无害，但 `count`
    #    会虚高（报了 12 项、实际只有 10 个 mod），失败计数也会错位。
    #    调用方本来就不该重复传。
    seen = set()
    keys = [m["key"] for m in mods if not (m["key"] in seen or seen.add(m["key"]))]
    for i in range(0, len(keys), BATCH_LIMIT):
        chunk = keys[i:i + BATCH_LIMIT]
        r = set_enabled(addonlist, {k: enable for k in chunk})
        results.append(r)
        if not r.get("ok"):
            break          # 前面已经改了文件，继续跑只会让状态更乱；如实上报
    applied = sum(1 for r in results if r.get("ok"))
    # ⚠️ keys 为空时循环一次都没跑、results 为空，而 all([]) 恒为 True ——
    #    原写法会把「一个 mod 都没传」报成成功，前端于是弹一句绿色的
    #    「已完成 0 项」。空输入必须当成失败并给出原因。
    total = (len(keys) + BATCH_LIMIT - 1) // BATCH_LIMIT
    out = {"ok": bool(results) and all(r.get("ok") for r in results),
           "batches": len(results), "count": len(keys), "detail": results,
           "applied_blocks": applied, "pending_blocks": total - len(results)}
    if not keys:
        out["error"] = "没有可操作的 mod（列表为空）"
    if not out["ok"] and applied:
        out["partial"] = ("前 %d 块已写入 addonlist.txt 并生效，第 %d 块失败；"
                          "剩余 %d 块未执行（未自动回滚，请核对后重试）"
                          % (applied, applied + 1, out["pending_blocks"]))
    return out


def batch_by_category(addonlist: str, mods: list[dict], category: str, enable: bool) -> dict:
    return batch_by_mods(addonlist, [m for m in mods if m["category"] == category], enable)


# --------------------------------------------------------------------------- #
# 卸载 / 隔离 / 还原
# --------------------------------------------------------------------------- #
def isolate_mods(addonlist: str, mods: list[dict], isolate_repo: bool = True) -> dict:
    """把 mod 移入隔离区。

    两种粒度：
      * isolate_repo=True  —— 连仓库里的 .workshop 目录一起搬走（彻底腾空间）
      * isolate_repo=False —— 只删掉 addons 里的链接（保留仓库素材，随时可再链接）
    无论哪种，都先把 mod 从 addonlist 里摘掉，保证游戏不会残留垃圾条目。

    返回键：ok / moved / failed / partial / dangling / rolled_back / backup / remaining。
    `ok=False` **不代表什么都没动**，调用方必须看 moved 与 failed 分别报数。
    """
    if len(mods) > BATCH_LIMIT:
        return {"ok": False, "error": "单批最多 %d 项，本批 %d 项" % (BATCH_LIMIT, len(mods))}
    # 把「读 addonlist → 逐个搬文件 → 写回 addonlist」整段放进锁里：
    # 原先 read 在锁外、write 时才进锁，于是「隔离」与「开关」并发时会各自
    # 读到同一份旧表，后写的把先写的抹掉（丢更新）。搬文件这一步也一并被锁
    # 覆盖，是为了让 addonlist 的改动顺序与文件的实际去向严格一致 ——
    # 否则「文件已搬走、条目还没摘掉」的中间态会被另一个请求读到并写进表里。
    # RLock 可重入：write_addonlist 内部还要再拿同一把锁。
    with _ADDONLIST_LOCK:
        return _isolate_mods_locked(addonlist, mods, isolate_repo)


def _isolate_mods_locked(addonlist: str, mods: list[dict], isolate_repo: bool) -> dict:
    """isolate_mods 的实现体。**调用方必须已持有 _ADDONLIST_LOCK。**"""
    mapping = scan.read_addonlist(addonlist)
    # dangling：目标已搬走、但 addons 里的链接没能删掉（清理失败）。
    # 这些是留在玩家 addons 目录里的断链，必须回传给界面提示手动处理，
    # 不能像原先那样吞掉 —— 用户会以为「已经隔离干净了」。
    moved, failed, dangling = [], [], []
    removed_keys: set[str] = set()      # 只有**真搬成功**的 key 才配从 addonlist 摘掉
    for m in mods:
        try:
            key = m["key"]
            addon_path = m["addon_path"]
            if os.path.islink(addon_path) or os.path.isfile(addon_path) \
                    or (not os.path.isdir(addon_path) and os.path.lexists(addon_path)):
                # 第三种情况是**断链**：符号链接指向的目标已经不在（Steam 更新
                # 改了路径 / 仓库目录被手动挪走 / 隔离过一次又手动还原了一半），
                # 而链接文件本身还躺在 addons 里 —— 玩家看得见它，游戏却加载
                # 不到。os.path.isfile 对断链为 False、os.path.isdir 也为 False，
                # 旧写法会让它掉进最后的 else 被报成「找不到实体文件」，
                # 于是这种最该清理的东西反倒清不掉。
                if isolate_repo and m.get("repo_dir") and os.path.isdir(m["repo_dir"]):
                    dst = safety.isolate(m["repo_dir"], tag="mod")
                    # 链接会随目标一起失效，顺手清掉。这里**不能**沿用原来的
                    # os.path.islink 判断：目标刚被上一句搬走，此刻链接已经是断链，
                    # islink 在 Windows 上可能返回 False，而真正要问的是
                    # 「这个路径还在不在」—— 那是 lexists。
                    try:
                        if os.path.lexists(addon_path):
                            os.unlink(addon_path)
                    except OSError as ex:
                        # 这里静默 pass 的后果很具体：目标已被搬进隔离区，
                        # 链接却留在 addons 里变成了断链 —— 游戏侧会多出一个
                        # 加载不到的条目，而用户以为自己「已经隔离干净了」。
                        # 不能因此中断整个隔离流程（其它 mod 还是要处理），
                        # 但必须留痕，让用户知道有一个残留链接要手动清。
                        safety.log("isolate", "悬挂链接清理失败（需手动删除）%s: %s"
                                   % (addon_path, ex), level="WARN", ok=False)
                        dangling.append(addon_path)
                    moved.append({"key": key, "moved": dst, "repo_dir": m["repo_dir"],
                                  "original": addon_path})
                else:
                    dst = safety.isolate(addon_path, tag="link")
                    moved.append({"key": key, "moved": dst, "original": addon_path})
            elif os.path.isdir(addon_path):
                dst = safety.isolate(addon_path, tag="dir")
                moved.append({"key": key, "moved": dst, "original": addon_path})
            else:
                # 三支都没命中 = 扫描之后文件已经不在了（游戏更新改了路径 /
                # 玩家自己删了 / 被别的程序挪走）。这**不是**「没有变化」，
                # 也不该安静跳过 —— 用户勾了它、点了隔离，结果只在界面上
                # 看到一句「已隔离 N 项」，少掉的那一个没有任何解释。
                # 记进 failed，让界面按 key 逐个报出来。
                failed.append({"key": key,
                               "error": "找不到实体文件（可能已被移动或删除）：%s"
                                        % addon_path})
                safety.log("isolate", "找不到实体文件，已跳过 %s（%s）"
                           % (key, addon_path), level="WARN", ok=False)
                continue
            # ⚠️ 只有走到这里（三支里有一支确实搬了东西）才算「这个 mod 被搬走了」。
            #    原先这条 pop 无条件执行：既不是链接/文件也不是目录时（文件已被
            #    手动删除、或扫描后被 Steam 更新改了路径），它照样把 key 从
            #    addonlist.txt 里摘掉 —— 条目没了、文件其实还在别处，玩家看到的是
            #    「我明明没隔离它，addonlist 里却消失了」。
            removed_keys.add(key)
        except Exception as ex:
            failed.append({"key": m.get("key"), "error": str(ex)})
            safety.log("isolate", "%s 失败: %s" % (m.get("key"), ex), level="ERROR", ok=False)
    # ⚠️ 这里**不能**只靠「mapping.pop(key) 再写回」来表达删除 ——
    #    _render_addonlist 只改写在 mapping 里的条目，不在 mapping 里的条目
    #    一律原样保留；于是 pop 掉再写回等于什么都没删（隔离后 addonlist.txt
    #    里那条还在，游戏仍会尝试加载一个已经不在 addons 里的文件）。
    #    真正删除必须走 drop 参数，由 _render_addonlist 整段摘除。
    #    mapping 这边也一并 pop：让返回的 count/enabled 反映「剩下多少条」，
    #    否则删掉的条目还会被算进统计里。
    #    写 addonlist 失败时必须回滚 —— 否则 mod 已经搬进隔离区、addonlist 里却
    #    还留着启用条目，玩家看到的是「条目在、文件没了」。原实现直接让异常冲出
    #    函数，隔离区里躺着一堆 mod，界面却什么都没说。
    for key in removed_keys:
        mapping.pop(key, None)
    try:
        res = write_addonlist(addonlist, mapping, drop=removed_keys)
    except Exception as ex:                                   # noqa: BLE001
        rolled_back = _rollback_moved(moved)
        safety.log("isolate", "写 addonlist 失败，已回滚 %d/%d 项: %s"
                   % (len(rolled_back), len(moved), ex), level="ERROR", ok=False)
        return {"ok": False, "moved": moved, "failed": failed,
                "rolled_back": True, "rolled_back_count": len(rolled_back),
                "rollback_failed": [k for k in moved if k["key"] not in
                                    {r["key"] for r in rolled_back}],
                "dangling": dangling,
                "error": "隔离已回滚：写 addonlist.txt 失败（%s）。"
                         "你的 addons 与 addonlist.txt 保持原样。" % ex,
                "backup": "", "remaining": len(mapping)}

    if dangling:
        safety.log("isolate", "有 %d 个链接未能删除，留在 addons 里成了断链：%s"
                   % (len(dangling), "、".join(dangling[:5])), level="WARN", ok=False)
    # 三种结局要能分开看，不能一律压成 ok=False：
    #   ① moved 非空 + failed 空  → 全成功，ok=True
    #   ② moved 非空 + failed 非空 → **部分成功**：这几项真搬进隔离区了、
    #      addonlist 里也真摘掉了。ok=False 只表示「不是全部成功」，
    #      界面必须按 moved.length 提示「已隔离 N 项、M 项失败」，
    #      否则用户看到一句「失败」就以为什么都没动（文件其实已经不在了）。
    #   ③ moved 空   + failed 非空 → 全失败，addonlist 未改动
    # 「传了 keys 却一个都没搬成」也归 ②/③ 而非成功：原先 ok=True 会让界面
    # 弹「N 项已移入隔离区」，而那 N 项压根没找到实体。
    return {"ok": not failed and bool(moved), "moved": moved, "failed": failed,
            "partial": bool(moved) and bool(failed), "rolled_back": False,
            "dangling": dangling,
            "backup": res["backup"], "remaining": res["count"]}


def _rollback_moved(moved: list[dict]) -> list[dict]:
    """把已搬进隔离区的条目搬回原位，返回成功回滚的条目列表。

    只做尽力而为的回滚：任何一条搬不回去都记录下来交给调用方，
    绝不因为回滚失败而吞掉原始错误。
    """
    done = []
    for it in moved:
        repo = it.get("repo_dir")
        src = it.get("moved", "")
        try:
            if repo and not os.path.isdir(repo) and os.path.isdir(src):
                # 整个仓库目录被搬走了：搬回原路径
                os.makedirs(os.path.dirname(repo), exist_ok=True)
                shutil.move(src, repo)
            else:
                # 只搬了链接 / 目录条目本身
                if not os.path.exists(src):
                    continue
                dst = it.get("original") or repo or ""
                if not dst:
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(dst):
                    os.unlink(dst)
                shutil.move(src, dst)
            done.append(it)
        except Exception as ex:                               # noqa: BLE001
            safety.log("isolate-rollback", "%s 回滚失败: %s" % (it.get("key"), ex),
                       level="ERROR", ok=False)
    return done


def list_staging() -> list[dict]:
    out = safety.list_staging()
    for it in out:
        it["size_h"] = safety.fmt_size(it["size"])
        it["mtime_h"] = safety.human_time(it["mtime"])
    return out


def restore_from_staging(addonlist: str, item_path: str, target_dir: str | None = None) -> dict:
    """把隔离区的条目搬回原位。

    目标目录优先用边车记录的**原始位置**；没记录时才需要调用方给 target_dir。
    """
    if not os.path.exists(item_path):
        return {"ok": False, "error": "隔离区条目不存在"}
    org = safety.read_origin(item_path)
    real = org.get("original_name") or "restored_item"
    # ⚠️ 边车（safety.isolate 写的 *.origin.json）里只有 original_path，**没有**
    #    original_dir 这个键 —— 它是 safety.list_staging() 读列表时现算出来给
    #    前端看的。原先这里直接 `org.get("original_dir", "")`，于是从列表里点
    #    「还原」、前端把算好的 original_dir 原样传回来时才不触发 need_target；
    #    任何直接调本函数（或先清空输入框）的路径都会永远拿到「没有留下原始
    #    位置」，明明边车里记着完整路径。改成从 original_path 取 dirname。
    orig = org.get("original_path") or ""
    dst_dir = (target_dir or "").strip() or (os.path.dirname(orig) if orig else "")
    if not dst_dir:
        return {"ok": True, "need_target": True, "name": real,
                "hint": "这条记录没有留下原始位置，请手动指定还原目录"}
    dst = os.path.join(dst_dir, real)
    if os.path.exists(dst):
        return {"ok": False, "error": "目标已存在，请先处理: %s" % dst}
    os.makedirs(dst_dir, exist_ok=True)
    shutil.move(item_path, dst)
    # 边车随之清理，避免隔离区里留下孤儿记录
    side = item_path + ".origin.json"
    if os.path.isfile(side):
        try:
            os.unlink(side)
        except OSError:
            pass
    safety.log("restore", "%s -> %s" % (item_path, dst))
    return {"ok": True, "restored_to": dst, "auto": not bool(target_dir)}


def _inside(path: str, root: str) -> bool:
    """path 是否真的位于 root 目录**之内**（含 root 自身）。

    踩坑：早先用 ap.startswith(stage) 判断，少了 os.sep ——
    `...\\staging_evil\\x` 会被误判成在 `...\\staging` 里而放行，
    本该硬拦截的路径就被送进回收站了。
    现在先 normcase + abspath 规范化，再要求「相等 或 带分隔符前缀」。
    """
    p = os.path.normcase(os.path.abspath(path))
    r = os.path.normcase(os.path.abspath(root))
    return p == r or p.startswith(r + os.sep)


def delete_staging_to_recycle(paths: list[str]) -> dict:
    """把隔离区条目丢进回收站（真正的销毁动作，需二次确认）。

    硬性拦截：只允许操作本工具隔离区内的路径。
    回收站 API 不可用时降级为「移入隔离区」而不是直接失败，避免出现
    「既没删掉、也没保住」的中间态。
    """
    if len(paths) > BATCH_LIMIT:
        return {"ok": False, "error": "单批最多 %d 项" % BATCH_LIMIT}
    stage = os.path.abspath(safety.STAGING_DIR)
    targets = []
    for p in paths:
        ap = os.path.abspath(p)
        if not os.path.exists(ap):
            return {"ok": False, "error": "路径不存在: %s" % p}
        if not _inside(ap, stage):
            return {"ok": False, "error": "拒绝操作隔离区以外的路径: %s" % p}
        targets.append(ap)
        side = ap + ".origin.json"
        if os.path.isfile(side):
            targets.append(side)
    res = safety.to_recycle_bin(targets, fallback_isolate=True)
    return {"ok": not res["failed"], "recycled": res["ok"],
            "fallback": res["fallback"], "failed": res["failed"],
            "note": ("回收站不可用，相关条目已改放隔离区（内容未丢失）"
                     if res["fallback"] else "")}
