#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 账号切换器 —— 桌面版启动器。

以原生窗口（WebView）内嵌 wb_ui_server 的 Web 界面，双击即可运行，不依赖本机 Python。
工作目录约定：exe 所在目录与账号数据（wb_auth）同级；前端模板 ui_template.html
打包后位于 _internal/，由 common.resolve_data() 依次查找。

打包态注意：PyInstaller 会把 __file__ 指向临时解包目录（_MEIPASS），因此这里在导入
wb_ui_server 后显式把数据目录纠正为 exe 所在目录。

界面上的「积分明细」由后端 wb_ui_server 的 /api/credits 提供（只读查询计费网关的资源包，
不消耗积分，结果在进程内缓存 10 分钟），本文件只负责把窗口调到能容下它。
"""
import sys
import threading
import time
from pathlib import Path

_FROZEN = bool(getattr(sys, "frozen", False))
if _FROZEN:
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent


def _find_project_root():
    """顺着 exe 向上找含 workbuddy_checkin.py 的项目根（自动签到）。"""
    for cand in (BASE_DIR.parent / "自动签到", BASE_DIR.parent, BASE_DIR):
        if (cand / "workbuddy_checkin.py").is_file():
            return cand
    return BASE_DIR.parent


PROJECT_ROOT = _find_project_root()
for p in (PROJECT_ROOT, BASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import wb_ui_server as srv        # noqa: E402
import switcher_common as common  # noqa: E402

# 固定数据目录为 exe 所在目录（保证 wb_auth 与 exe 同级即可被识别）
srv._BIN_DIR = BASE_DIR
srv.SCRIPT_DIR = PROJECT_ROOT
srv.AUTH_DIR = BASE_DIR / "wb_auth"
srv.LOCK_DIR = BASE_DIR / ".locks"
srv.Handler.BASE_DIR = BASE_DIR          # 前端页面按 exe 目录 → _internal 依次查找
srv.Handler.AUDIT_DIR = BASE_DIR / "logs"

PORT = srv.DEFAULT_PORT


def _port_alive(port, timeout=0.6):
    import urllib.request
    t0 = time.time()
    url = "http://127.0.0.1:%d/" % port
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    return False


def _parse_args(argv):
    """只认两个开关，够用即可，不引 argparse。

    --serve / --no-window：只跑本地 HTTP 服务，不开窗口。桌面版 exe 是 windowed
      构建（没有控制台），出问题时既看不到报错也拿不到接口响应；带上这个开关就能
      用 curl 直接冒烟，也方便让别的脚本复用这份服务。
    --port N：指定起始端口（被占用时仍会自动顺延）。
    """
    headless = any(a in ("--serve", "--no-window") for a in argv)
    port = PORT
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                pass
    return headless, port


def main():
    headless, requested = _parse_args(sys.argv[1:])
    server = None
    port = requested
    # 若端口已被占用（如旧服务仍在运行），直接复用现有服务；否则自动避让到空闲端口
    if _port_alive(port):
        pass
    else:
        server, port = common.bind_server(srv.Handler, port)
        # 与"请求的端口"比，而不是与默认端口常量比 —— 否则显式传 --port 时会误报
        if port != requested:
            print("[提示] 端口 %d 被占用，已改用 %d" % (requested, port))
        threading.Thread(target=server.serve_forever, daemon=True).start()

    url = "http://127.0.0.1:%d/" % port

    if headless:
        print("本地服务已启动：%s  (Ctrl+C 停止)" % url, flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            if server:
                server.shutdown()
                server.server_close()
        return 0

    try:
        import webview
        webview.create_window(
            "WorkBuddy 账号切换器",
            url,
            width=1180,          # 账号改成列表行（身份 / 积分 / 操作 三段）后需要更宽
            height=1040,         # 当前账号行 + 积分明细 + 签到，比原先的 780 高不少
            min_size=(900, 700),
        )
        webview.start()
    except Exception as e:  # noqa: BLE001  # 无 WebView 运行库时退回默认浏览器
        # 桌面版是 windowed 构建，print 没有任何去处 —— 同时落一份日志，
        # 否则用户只会看到"浏览器突然弹出来"，完全无从判断为什么没开窗口。
        msg = "WebView 启动失败，退回浏览器打开：%s (%s)" % (url, e)
        print(msg)
        try:
            log_dir = BASE_DIR / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_dir / "desktop-start.log", "a", encoding="utf-8") as fh:
                fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        except OSError:
            pass
        import webbrowser
        webbrowser.open(url)
        if server:
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
    finally:
        if server:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    sys.exit(main())