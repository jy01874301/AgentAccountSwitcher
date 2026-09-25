#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""check_exe_datadir.py —— 冻结态 exe 的数据目录基准自检（双探针法）。

**为什么需要它**：exe 的数据目录基准被钉死在「导入时的快照」上过两次，
两次都表现为「源码运行一切正常、打包后账号库恒为 0 个」：
  - 第一轮：自检真的切了本机登录态；
  - 第二轮：打包后的 exe 跑去 `<exe 同级>\_internal\wb_auth\` 找账号。
这种 bug 在源码态**测不出来**，只能拿真的 exe 验。所以每次重新打包后跑一遍。

做法：在两个候选目录各放一个探针账号文件，看 exe 读的是哪个 ——
  A) `<exe 同级>\wb_auth\`            ← 正确基准
  B) `<exe 同级>\_internal\wb_auth\`  ← 旧 bug 会读这里
国际服（`wbai_auth\`）做同样一遍，确认两个通道都跟着 `_BIN_DIR`。

用法：
    python check_exe_datadir.py [exe 目录]
    # 默认 dist\AgentAccountSwitcher

退出码 0 = 两个通道都读 exe 同级；1 = 有失败项（会打印原因）。
探针文件跑完即删，不碰真实账号库。
"""
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXE_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "dist" / "AgentAccountSwitcher"
EXE = EXE_DIR / "AgentAccountSwitcher.exe"

GOOD = json.dumps({
    "account": {"uid": "probe-uid", "nickname": "探针账号"},
    "auth": {"accessToken": "x" * 300, "refreshToken": "y" * 300,
             "expiresAt": int(time.time() * 1000) + 55 * 86400 * 1000},
}, ensure_ascii=False)

# 每个探针 = (放哪、文件名、属于哪个通道、期望结果)
PROBES = [
    ("wb_auth", "workbuddy-probe-exe.info", "wb", True),
    ("_internal/wb_auth", "workbuddy-probe-internal.info", "wb", False),
    ("wbai_auth", "workbuddyai-probe-exe.info", "wbai", True),
    ("_internal/wbai_auth", "workbuddyai-probe-internal.info", "wbai", False),
]


def free_port():
    """挑一个没人用的端口 —— 写死端口会和别的实例撞上，表现是"探针全都没读到"。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    if not EXE.is_file():
        print("找不到 exe：%s" % EXE)
        return 1
    port = free_port()
    created = []
    for sub, name, _ch, _want in PROBES:
        p = EXE_DIR / sub / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(GOOD, encoding="utf-8")
        created.append(p)

    def get(path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    # exe 是 windowed 构建，起服务靠 --serve（见 README 的「桌面版 exe 的命令行开关」）。
    # ⚠️ 必须带 `--no-single-instance`：单实例守卫探的是「请求端口区间 ∪ **默认端口区间**」，
    #    所以只要用户正开着切换器（它是常驻服务，很常见），这个随机端口的探针实例就会被
    #    判成 reuse 直接退出，表现为"服务没起来"（实测踩到）。
    proc = subprocess.Popen([str(EXE), "--serve", "--port", str(port),
                             "--no-single-instance"],
                            cwd=str(EXE_DIR), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    fails = []
    try:
        alive = False
        for _ in range(40):
            try:
                if get("/api/ping").get("app"):
                    alive = True
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        if not alive:
            print("FAIL 服务没起来（exe 可能启动即退出，看 logs\\desktop-start.log）")
            return 1

        ping = get("/api/ping")
        print("ping.app=%s version=%s" % (ping.get("app"), ping.get("version")))
        seen = {"wb": [a["file"] for a in get("/api/accounts")["accounts"]],
                "wbai": [a["file"] for a in get("/api/wbai/accounts")["accounts"]]}
        for ch in ("wb", "wbai"):
            print("[%s] %s" % (ch, seen[ch]))
        for _sub, name, ch, want in PROBES:
            got = name in seen[ch]
            if got != want:
                fails.append("%s 侧 %s「%s」（期望 %s）"
                             % (ch, "读到了" if got else "没读到", name,
                                "读到" if want else "不该读到"))
    finally:
        # ⚠️ 不要用 `taskkill /IM AgentAccountSwitcher.exe /F` —— 那是**按镜像名全杀**，
        #    会把用户自己正在用的实例一并干掉（本工具是常驻服务，用户很可能同时开着）。
        #    这里只结束**本次 Popen 的那棵树**：PyInstaller 的引导器会另起子进程，
        #    所以光 terminate() 不够，要 `/PID <pid> /T` 连子树一起收
        #    （见 AUDIT_2026-09-24.md P3-15）。
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True)
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        for p in created:
            try:
                p.unlink()
            except OSError:
                pass
        for sub, _name, _ch, _want in PROBES:
            try:
                (EXE_DIR / sub).rmdir()      # 只在空了才删得掉，不会误删真账号库
            except OSError:
                pass

    print("失败项：%s" % ("；".join(fails) if fails else "无"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
