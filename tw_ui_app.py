#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trae 账号切换器 —— 桌面版启动器（与 wb_ui_app.py 对齐）。

以原生窗口（WebView）内嵌 tw_ui_server 的 Web 界面，双击即可运行，不依赖本机 Python。
工作目录约定：exe 所在目录与账号数据（tw_auth）同级。
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
    """顺着 exe 向上找含 trae_work_checkin.py 的项目根（自动签到）。"""
    for cand in (BASE_DIR.parent / "自动签到", BASE_DIR.parent, BASE_DIR):
        if (cand / "trae_work_checkin.py").is_file():
            return cand
    return BASE_DIR.parent


PROJECT_ROOT = _find_project_root()
for p in (PROJECT_ROOT, BASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import tw_ui_server as srv        # noqa: E402
import switcher_common as common  # noqa: E402

# 固定数据目录为 exe 所在目录（保证 tw_auth 与 exe 同级即可被识别）
srv._BIN_DIR = BASE_DIR
srv.TW_AUTH_DIR = BASE_DIR / "tw_auth"
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


def main():
    server = None
    if _port_alive(PORT):
        port = PORT
    else:
        srv.Handler.TOKEN = common.new_token()
        server, port = common.bind_server(srv.Handler, PORT)
        if port != PORT:
            print("[提示] 默认端口 %d 被占用，已改用 %d" % (PORT, port))
        threading.Thread(target=server.serve_forever, daemon=True).start()

    url = "http://127.0.0.1:%d/" % port
    try:
        import webview
        webview.create_window(
            "Trae 账号切换器",
            url,
            width=1120,
            height=780,
            min_size=(900, 600),
        )
        webview.start()
    except Exception as e:  # noqa: BLE001  # 无 WebView 运行库时退回默认浏览器
        print("WebView 启动失败，退回浏览器打开：%s (%s)" % (url, e))
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
