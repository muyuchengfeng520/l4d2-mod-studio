import subprocess, sys
raw = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                     capture_output=True, timeout=8,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
print("bytes len:", len(raw))
# 用不同编码解码，看哪种能正常读出 LISTENING 行
for enc in ("utf-8", "gbk", "cp936", "mbcs"):
    try:
        txt = raw.decode(enc, errors="replace")
        n = sum(1 for ln in txt.splitlines() if "LISTENING" in ln)
        bad = txt.count("\ufffd")
        print("  %-8s LISTENING 行数=%-4d 替换字符=%d" % (enc, n, bad))
    except Exception as ex:
        print("  %-8s 解码失败: %s" % (enc, ex))
# 找出监听 17246 的 PID
txt = raw.decode("utf-8", errors="replace")
pids = set()
for line in txt.splitlines():
    parts = line.split()
    if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(":17246"):
        pids.add(parts[4])
print("17246 监听 PID (utf-8 路径):", sorted(pids))
