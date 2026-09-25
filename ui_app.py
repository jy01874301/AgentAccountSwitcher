#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号切换器 —— 统一的桌面版启动器（多产品）。

以原生窗口（WebView）内嵌后端的 Web 界面，双击即可运行，不依赖本机 Python。

用法：

    ui_app.py                                   # 默认产品（wb）
    ui_app.py --product trae                    # Trae
    ui_app.py --product trae --port 8800        # 指定起始端口（被占用仍会自动顺延）
    ui_app.py --product trae --serve --no-window  # 只跑本地服务、不开窗口（便于冒烟）

## 为什么是"一个入口 + 产品表"

`wb_ui_app.py` / `tw_ui_app.py` 曾是两份 200 行的逐字重复（只有服务模块、互斥体、
窗口标题尺寸、CLI 动作表这几处不同），改一处漏一处 —— 已经因此漏过东西
（Trae 侧缺一次性令牌、缺 stale 检测）。现在产品差异**全部**集中在下面的 `_PRODUCTS`
表里：**新增一个产品 = 加一条记录**，不必再复制一份启动器。
见 AUDIT_2026-09-24.md P3-14 / 第二十章。

## 打包态注意

PyInstaller 会把 `__file__` 指向临时解包目录（`_MEIPASS`），因此这里在导入后端模块后，
通过它的 `rebind()` 把数据目录纠正为 **exe 所在目录**。

⚠️ 只走 `rebind()` 这一个入口，**不要**在这里逐个赋 `srv.X = ...` —— 以前那样写过，
其中 `srv.AUTH_DIR` 在双通道重构后退化成了导入时的快照别名，写它不再生效，
结果是打包后的 exe 跑去 `_internal\\wb_auth\\` 找账号、页面恒显示 0 个
（源码运行却完全正常）。漏改一个名字就是这个下场。
"""
import importlib
import sys
import threading
import time
from pathlib import Path

_FROZEN = bool(getattr(sys, "frozen", False))
BASE_DIR = (Path(sys.executable).resolve().parent if _FROZEN
            else Path(__file__).resolve().parent)


# ---------------------------------------------------------------------------
# 产品注册表 —— 所有差异都在这里，这是"预留扩展"的落点
# ---------------------------------------------------------------------------
# 字段：
#   title          窗口标题 / 原生提示框标题
#   server_module  后端模块名（**动态导入**，必须在 .spec 的 hiddenimports 里登记）
#   mutex_attr     switcher_common 里的互斥体常量名（按产品隔离，互不挤掉）
#   root_marker    项目根标志文件；顺着 BASE_DIR 往上找含它的目录
#   cli_actions    一次性 CLI 动词（跑完就退，交给后端 main() 处理）
#   window         (width, height, min_width, min_height)
#   rebind_root    `rebind()` 是否要第二个参数（项目根目录）
#   warmup         启动时是否预热积分/签到缓存（抢在窗口出现前跑热）
#   status         ready = 可用；planned = 只占位（启动时**明确报错**，不静默降级）
#   note           planned 的说明：还差什么才能启用
_PRODUCTS = {
    "wb": {
        "title": "WorkBuddy 账号切换器",
        "server_module": "wb_ui_server",
        "mutex_attr": "MUTEX_NAME_WB",
        "root_marker": "workbuddy_checkin.py",
        # exe 是**桌面启动器**，默认只认 --serve / --no-window / --port，
        # 其余参数以前被**静默忽略** —— 于是 `--refresh-all` 不会续期，而是弹出一个窗口；
        # 脚本/计划任务看退出码 0，会以为命令执行了。这些动词语义明确、且都是"跑完就退"，
        # 交给后端 main() 才是用户的本意。
        # `--serve` 故意不在表里：它由本文件处理（走 bind_server + 保活那条路）。
        # 只列**动作**动词：`--channel` / `--force` / `--migrate-mode` 这类是修饰符，
        # 单独出现时仍应走"开窗口"（否则 `--channel wbai` 会变成开浏览器，比开窗口还怪）。
        "cli_actions": ("--list", "--current", "--live-token", "--switch",
                        "--migrate-preview", "--prune", "--refresh-all"),
        "window": (1180, 1040, 900, 700),   # 账号列表 + 积分明细 + 签到，比原先高不少
        "rebind_root": True,
        "warmup": True,
        "status": "ready",
    },
    "trae": {
        "title": "Trae 账号切换器",
        "server_module": "tw_ui_server",
        "mutex_attr": "MUTEX_NAME_TW",
        "root_marker": "trae_work_checkin.py",
        "cli_actions": ("--list", "--current", "--switch", "--refresh-all"),
        "window": (1180, 900, 900, 640),
        "rebind_root": False,
        "warmup": False,
        "status": "ready",
    },

    # --- 以下为**预留**产品：只占位，启动时明确提示"尚未实现" ---
    # 启用步骤见各自的 note；加完把 status 改成 "ready" 即可，本文件不用再动。
    "trae-work": {
        "title": "Trae Work 账号切换器",
        "server_module": "trae_work_ui_server",
        "mutex_attr": "MUTEX_NAME_TRAE_WORK",
        "root_marker": "trae_work_checkin.py",
        "cli_actions": (),
        "window": (1180, 900, 900, 640),
        "rebind_root": False,
        "warmup": False,
        "status": "planned",
        "note": "还差：① 后端 trae_work_ui_server.py；"
                "② 把它登记进 AgentAccountSwitcher.spec 的 hiddenimports；"
                "③ 在 switcher_common 里加 CHANNELS 通道（进程名 / 登录态路径 / 网关）"
                "与 MUTEX_NAME_TRAE_WORK 常量。",
    },
}

DEFAULT_PRODUCT = "wb"


def _parse_args(argv):
    """只认四个开关，够用即可，不引 argparse。

    --serve / --no-window：只跑本地 HTTP 服务、不开窗口。桌面版 exe 是 windowed 构建
      （没有控制台），出问题时既看不到报错也拿不到接口响应；带上它就能用 curl 直接冒烟，
      也方便让别的脚本复用这份服务。
    --port N：指定起始端口（被占用时仍会自动顺延）。
    --product X：选择产品（见 `_PRODUCTS`），默认 `wb`。
    --no-single-instance：**跳过单实例守卫**。只给自检工具用 —— `check_exe_datadir.py`
      必须另起一个实例来验证「数据目录基准 = exe 同级」，而守卫会把它判成 reuse 直接退出
      （它探的是「请求端口区间 ∪ 默认端口区间」，所以**传 `--port` 也躲不开**）。
      日常别用：两个实例同时跑会互相抢端口与账号文件。
    """
    headless = any(a in ("--serve", "--no-window") for a in argv)
    skip_guard = "--no-single-instance" in argv
    product = DEFAULT_PRODUCT
    port = None
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                pass
        elif a == "--product" and i + 1 < len(argv):
            product = argv[i + 1]
        elif a.startswith("--product="):
            product = a.split("=", 1)[1]
    return headless, port, product, skip_guard


def _strip_product_args(argv):
    """把**本启动器自己的**开关摘掉（`--product X` / `--no-single-instance`）。

    后端（`srv.main()`）的 argparse 不认识它们，原样转发会让
    `ui_app.py --product trae --list` 直接报"未知参数"。
    """
    out = []
    skip_next = False
    for a in argv:
        if skip_next:
            skip_next = False
            continue
        if a == "--product":
            skip_next = True
            continue
        if a.startswith("--product=") or a == "--no-single-instance":
            continue
        out.append(a)
    return out


def _wants_server_cli(argv, actions):
    """命令行里是否出现了后端的一次性动作（支持 `--prune=3` 这种写法）。"""
    for a in argv:
        for v in actions:
            if a == v or a.startswith(v + "="):
                return True
    return False


def _setup_paths(root_marker):
    """把项目根与 exe 目录塞进 sys.path，返回项目根。"""
    root = BASE_DIR.parent
    for cand in (BASE_DIR.parent / "自动签到", BASE_DIR.parent, BASE_DIR):
        if (cand / root_marker).is_file():
            root = cand
            break
    for p in (root, BASE_DIR):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    return root


def _load_server(spec):
    """按产品导入后端模块与公共模块，并把数据目录纠正到 exe 所在目录。"""
    root = _setup_paths(spec["root_marker"])
    srv = importlib.import_module(spec["server_module"])
    common = importlib.import_module("switcher_common")
    # ⚠️ 只走 rebind() 这一个入口（见模块 docstring 的说明）。
    if spec["rebind_root"]:
        srv.rebind(BASE_DIR, root)
    else:
        srv.rebind(BASE_DIR)
    return srv, common


def _log_startup(msg):
    """窗口版没有控制台，print 没有去处 —— 落一份日志便于事后排查。"""
    try:
        log_dir = BASE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "desktop-start.log", "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


_DIE_WAIT = 10.0    # 报错弹框最多等多久（秒），见 _die


def _die(msg, code=2):
    """报错并尽量让用户看得见 —— windowed 构建下 print 无处可去，再弹个原生框。

    ⚠️ 弹框是**阻塞**的（等用户点确定），直接调用会让脚本调用者一直挂住
    （实测：`exe --product nope` 在无人值守时永不返回）。所以放后台线程 + 限时等待：
    双击的用户看得见提示，脚本最多等 `_DIE_WAIT` 秒就拿到退出码。
    daemon 线程随进程退出，用户没点也不会留下僵尸。
    """
    print(msg)
    _log_startup(msg)

    def _notify():
        try:
            import switcher_common as _c
            _c.notify_user("账号切换器", msg, log=print)
        except Exception:  # noqa: BLE001  弹框失败就只留日志，别让报错路径再抛
            pass

    t = threading.Thread(target=_notify, daemon=True)
    t.start()
    t.join(timeout=_DIE_WAIT)
    return code


def _show_window(url, title, geom):
    """开原生窗口指向 url；没有 WebView 运行库时退回默认浏览器。"""
    width, height, min_w, min_h = geom
    try:
        import webview
        webview.create_window(title, url, width=width, height=height,
                              min_size=(min_w, min_h))
        webview.start()
        return True
    except Exception as e:  # noqa: BLE001
        msg = "WebView 启动失败，退回浏览器打开：%s (%s)" % (url, e)
        print(msg)
        _log_startup(msg)
        import webbrowser
        webbrowser.open(url)
        return False


def main():
    argv = sys.argv[1:]
    # 先把最基础的路径塞好 —— 后面报错要 import switcher_common 弹提示框
    for p in (BASE_DIR.parent, BASE_DIR):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))

    headless, requested_port, product_key, skip_guard = _parse_args(argv)
    spec = _PRODUCTS.get(product_key)
    if spec is None:
        return _die("未知的产品 %r。可用：%s" % (product_key, "、".join(sorted(_PRODUCTS))))
    if spec["status"] != "ready":
        return _die("%s 尚未实现。\n\n%s" % (spec["title"], spec.get("note", "")))

    # 一次性 CLI 动作（--list / --switch / --refresh-all …）→ 交给后端处理，绝不开窗口。
    # 注意：windowed 构建没有控制台，print 无处可去 —— 这些命令**行为**是对的
    # （文件真的会改），但看不到输出。要看得见的输出请用
    # `python <server_module>.py ...`（refresh_all.cmd 走的就是那条路）。
    if _wants_server_cli(argv, spec["cli_actions"]):
        srv, _common = _load_server(spec)
        sys.argv = [sys.argv[0]] + _strip_product_args(argv)
        return srv.main()

    srv, common = _load_server(spec)
    default_port = srv.DEFAULT_PORT
    port = requested_port if requested_port is not None else default_port

    # 单实例保护：互斥体按产品隔离，两个产品可以同时开，互不挤掉。
    # 见 DESIGN_single_instance.md。
    # ⚠️ `--no-single-instance` 只给自检工具用（它必须另起一个实例来验证数据目录基准），
    #    日常别用 —— 两个实例同时跑会互相抢端口与账号文件。
    if skip_guard:
        print("[提示] 已按 --no-single-instance 跳过单实例守卫（仅供自检）。")
        handle, action, info = None, "start", {}
    else:
        handle, action, info = common.single_instance_guard(
            getattr(common, spec["mutex_attr"]), port,
            tries=common.PORT_TRIES, log=print, default_port=default_port)

    if action == "reuse":
        url = "http://127.0.0.1:%d/" % info.get("port", port)
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
               "请切换到已打开的那个窗口（任务栏）。\n\n地址：%s"
               % (info.get("pid"), info.get("port"), url))
        if stale:
            msg += "\n\n注意：那个实例的版本是 %s，当前是 %s，可能在跑旧代码。" % (
                info.get("version"), srv.Handler.APP_VERSION)
        common.notify_user(spec["title"], msg, log=print)
        return 0

    if action == "abort":
        msg = ("已有切换器实例在运行，但在 %d 起的 %d 个端口上都探测不到它。"
               % (port, common.PORT_TRIES))
        print("[错误] " + msg)
        _log_startup(msg)
        return 1

    server = None
    # 一次性令牌：只存在于本次进程，随首页注入给前端，写操作必须回传。
    # ⚠️ 必须**显式**设 —— 这里走的是 bind_server()，**不经过 srv.serve()**，
    #    而 `Handler.TOKEN = new_token() if use_token else None` 那行是在 serve() 里的。
    #    漏了它 → TOKEN 保持类默认值 None → do_POST 里 `if self.TOKEN and ...` 恒假
    #    → **令牌校验被整段跳过**（只剩 Host+Origin 守卫）。
    #    见 AUDIT_2026-09-24.md 第二十章。
    srv.Handler.TOKEN = common.new_token()
    server, port = common.bind_server(srv.Handler, port)
    # 与"请求的端口"比，而不是与默认端口常量比 —— 否则显式传 --port 时会误报
    if port != (requested_port if requested_port is not None else default_port):
        print("[提示] 端口 %d 被其它程序占用，已改用 %d（本实例唯一）"
              % (requested_port if requested_port is not None else default_port, port))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if spec["warmup"] and hasattr(srv, "warmup_async"):
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
        if not _show_window(url, spec["title"], spec["window"]) and server:
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
