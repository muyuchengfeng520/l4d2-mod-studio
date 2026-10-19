import sys, os
sys.path.insert(0, r"E:\L4D2ModStudio\L4D2ModStudio-github")
os.chdir(r"E:\L4D2ModStudio\L4D2ModStudio-github")
sys.path.insert(0, os.path.join(r"E:\L4D2ModStudio\L4D2ModStudio-github", "app"))
from app import server
# 用真正在监听的端口验证 _listener_pid 能不能查出 PID
for port in (61980, 17246):
    print("port %d -> %r" % (port, server._listener_pid(port)))
