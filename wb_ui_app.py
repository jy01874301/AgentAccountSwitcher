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

# 固定数据目录为 exe 所在目录（保证 wb_auth / wbai_auth 与 exe 同级即可被识别）。
#
# ⚠️ 走 `srv.rebind()` 这一个入口，**不要**在这里逐个赋 `srv.X = ...`。
# 以前这里写着 5 行赋值，其中 `srv.AUTH_DIR` 在双通道重构后退化成了导入时的
# 快照别名 —— 写它不再生效，结果是打包后的 exe 跑去 `_internal\wb_auth\` 找账号、
# 页面恒显示 0 个（源码运行却完全正常）。漏改一个名字就是这个下场，
# 所以现在把所有基准的改写收进一个函数，改不全都不可能。
# 账号库本身是按 `_BIN_DIR` 现算的（见 `Channel.auth_dir` 属性），不依赖这里。
srv.rebind(BASE_DIR, PROJECT_ROOT)

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


# wb_ui_server 的一次性 CLI 动词。
#
# exe 是**桌面启动器**（入口是 wb_ui_app），默认只认 --serve / --no-window / --port，
# 其余参数以前被**静默忽略** —— 于是 `WorkBuddySwitcher.exe --refresh-all` 不会续期，
# 而是弹出一个窗口；脚本/计划任务看退出码 0，会以为命令执行了。
# （实测踩到：`WorkBuddySwitcher.exe --channel wbai --migrate-preview x.info`
#   直接开了一个 GUI 窗口，前台 shell 一直等它退出。）
# 这些动词语义明确、且都是"跑完就退"，交给 wb_ui_server.main() 处理才是用户的本意。
# `--serve` 故意不在表里：它由本文件处理（走的是 bind_server + 保活那条路）。
# 只列**动作**动词：`--channel` / `--force` / `--migrate-mode` 这类是修饰符，
# 单独出现时仍然应该走"开窗口"（否则 `exe --channel wbai` 会变成开浏览器，
# 比开原生窗口还怪）。判据是"有没有一个跑完就退的动作"。
_SERVER_ACTIONS = ("--list", "--current", "--switch", "--migrate-preview",
                   "--prune", "--refresh-all")


def _wants_server_cli(argv):
    """命令行里是否出现了 wb_ui_server 的一次性动作（支持 `--prune=3` 这种写法）。"""
    for a in argv:
        for v in _SERVER_ACTIONS:
            if a == v or a.startswith(v + "="):
                return True
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


def _show_window(url):
    """开原生窗口指向 url；没有 WebView 运行库时退回默认浏览器。"""
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
        return True
    except Exception as e:  # noqa: BLE001
        msg = "WebView 启动失败，退回浏览器打开：%s (%s)" % (url, e)
        print(msg)
        _log_startup(msg)
        import webbrowser
        webbrowser.open(url)
        return False


def _log_startup(msg):
    """窗口版没有控制台，print 没有去处 —— 落一份日志便于事后排查。"""
    try:
        log_dir = BASE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "desktop-start.log", "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def main():
    argv = sys.argv[1:]
    # 一次性 CLI 动作（--list / --switch / --refresh-all / --migrate-preview …）→
    # 交给后端处理，绝不开窗口。详见 _SERVER_ACTIONS 的注释。
    # 注意：windowed 构建没有控制台，print 无处可去 —— 这些命令**行为**是对的
    # （文件真的会改），但看不到输出。要看得见的输出请用
    # `python wb_ui_server.py ...`（refresh_all.cmd 走的就是那条路）。
    if _wants_server_cli(argv):
        return srv.main()

    headless, requested = _parse_args(argv)

    # 单实例保护：拿到互斥体才允许起服务。见 DESIGN_single_instance.md。
    handle, action, info = common.single_instance_guard(
        common.MUTEX_NAME_WB, requested, tries=common.PORT_TRIES, log=print,
        default_port=PORT)
    if action == "reuse":
        url = "http://127.0.0.1:%d/" % info.get("port", requested)
        print("[提示] 已有实例在运行（pid %s，端口 %s），直接指向它，不再启动第二个。"
              % (info.get("pid"), info.get("port")))
        stale = bool(info.get("version")) and info["version"] != srv.Handler.APP_VERSION
        if stale:
            print("[警告] 那个实例的版本是 %r，当前是 %r —— 它可能在跑旧代码。"
                  % (info.get("version"), srv.Handler.APP_VERSION))
        if headless:
            print("       页面地址：%s" % url)
            return 0
        # ⚠️ 这里**不再开新窗口** —— 那正是"每次双击多一个同地址页面"的来源。
        # 窗口版没有控制台，print 无处可去，所以弹一个原生提示框。
        msg = ("已有实例在运行（pid %s，端口 %s），未再打开新窗口。\n\n"
               "请切换到已打开的那个窗口（任务栏）。\n\n地址：%s" % (info.get("pid"), info.get("port"), url))
        if stale:
            msg += "\n\n注意：那个实例的版本是 %s，当前是 %s，可能在跑旧代码。" % (
                info.get("version"), srv.Handler.APP_VERSION)
        common.notify_user("WorkBuddy 账号切换器", msg, log=print)
        return 0
    if action == "abort":
        msg = "已有切换器实例在运行，但在 %d 起的 %d 个端口上都探测不到它。" % (requested, common.PORT_TRIES)
        print("[错误] " + msg)
        _log_startup(msg)
        return 1

    server = None
    port = requested
    server, port = common.bind_server(srv.Handler, port)
    # 与"请求的端口"比，而不是与默认端口常量比 —— 否则显式传 --port 时会误报
    if port != requested:
        print("[提示] 端口 %d 被其它程序占用，已改用 %d（本实例唯一）" % (requested, port))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    srv.warmup_async()      # 抢在窗口出现前把积分/签到缓存跑热

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
        if not _show_window(url) and server:
            # 退回浏览器后仍要保活服务，否则窗口/标签页会立刻打不开
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