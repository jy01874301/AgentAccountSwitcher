"""WorkBuddy / WorkBuddyAI 客户端主窗口状态守卫（诊断 + 修复）。

## 结论摘要（依据客户端 app.asar 内 `src/main/window/window-state.ts` 源码）

两个客户端各把主窗口几何持久化在**自己 userData 下**的 `window-state.json`：

| 通道 | 产品 | 状态文件 |
| --- | --- | --- |
| `wb`   | WorkBuddy（国服）   | `%USERPROFILE%\\.workbuddy\\app\\window-state.json` |
| `wbai` | WorkBuddyAI（国际服） | `%USERPROFILE%\\.workbuddy-ai\\app\\window-state.json` |

schema：

    {"version":2,"bounds":{"x":..,"y":..,"width":..,"height":..},
     "isMaximized":bool,"isFullScreen":bool}

- `bounds` = `win.getNormalBounds()` —— **最大化之前的「还原尺寸」**，单位 DIP（缩放比 1.25 时
  1 DIP = 1.25 物理像素）。它**不是**当前窗口尺寸。
- 写时机：`resize` / `move` 事件后 **500ms 去抖**，全应用只有这一个写入方。
- 读时机：**只在建窗时**（冷启动、崩溃重建）。恢复顺序是
  「按 `bounds` 建窗（隐藏）→ `ready-to-show` 时 `maximize()` / `setFullScreen(true)` → `show()`」。

**所以 `bounds` 就是「窗口还原后的大小与位置」的唯一来源。** 一旦它被记成一个偏小的陈旧值，
窗口每次离开最大化状态（拖标题栏、双击标题栏、Win+↓、贴边、或应用重启后那一瞬）都会缩到
那个小尺寸 —— 用户观感就是「窗口被自动还原（重置大小与位置）」。

## 本工具的命令

    list                列出所有可见顶层窗口（窗口标题 + 进程 + 矩形），用于定位
    show                打印两个通道的状态文件 / 实时矩形 / Windows 还原矩形 / 缩放比 / 判定
    capture             把**当前实时窗口的还原矩形**固化为 golden
                        （用法：先手动把窗口摆成想要的大小与位置，再 capture）
    pin                 手工指定 golden 并立即生效：
                        --size 1600x1000 [--pos 200,120] [--maximized|--no-maximized]
                        [--apply] [--live]
    check               比对状态文件**与运行中窗口**是否等于 golden；
                        漂移则报告（加 --fix 才回写，带备份；加 --live 就地修正运行中窗口）
    watch               采样窗口矩形 + 状态文件 mtime，用来抓「谁在什么时候改的」

## ⚠️ DPI 约定

本脚本**故意不调用** `SetProcessDpiAwareness`，保持 DPI-unaware。这样 Win32 返回的是系统
虚拟化后的坐标（= DIP），与 Electron 的 `bounds` 可直接逐位比对。
"""
import argparse
import ctypes
import ctypes.wintypes as w
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- 常量

HERE = Path(__file__).resolve().parent
GOLDEN_PATH = HERE / "window_state_golden.json"
BACKUP_DIR = HERE / "window_state_backups"

# 客户端可执行文件名（用于把窗口归属到通道）
EXE_HINTS = {
    "wb": "\\workbuddy\\workbuddy.exe",
    "wbai": "\\workbuddyai\\workbuddyai.exe",
}

USERPROFILE = Path(os.environ.get("USERPROFILE") or Path.home())

# 状态文件所在目录（userData/app），按通道
STATE_DIRS = {
    "wb": USERPROFILE / ".workbuddy" / "app",
    "wbai": USERPROFILE / ".workbuddy-ai" / "app",
}

# 客户端内建默认值（源码 DEFAULTS / MIN_WINDOW_*），仅用于判定提示
CLIENT_DEFAULT_SIZE = (1200, 800)
CLIENT_MIN_SIZE = (800, 600)

# 认为「偏小得可疑」的下限（DIP）。仅作提示，不参与自动判定。
SUSPICIOUS_SIZE = (900, 640)

# ---------------------------------------------------------------- Win32


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("showCmd", ctypes.c_uint), ("ptMinPosition", POINT),
                ("ptMaxPosition", POINT), ("rcNormalPosition", RECT)]


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel32.QueryFullProcessImageNameW.restype = w.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    ctypes.c_void_p, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = w.DWORD

user32.GetWindowPlacement.argtypes = [w.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
user32.GetWindowPlacement.restype = w.BOOL
user32.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [w.HWND]
user32.AttachThreadInput.argtypes = [w.DWORD, w.DWORD, w.BOOL]
user32.SetWindowPlacement.argtypes = [w.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
user32.SetWindowPlacement.restype = w.BOOL

ENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)

SHOWCMD = {1: "normal", 2: "minimized", 3: "maximized", 4: "maxshow",
           5: "maxhide", 6: "minimize", 7: "minimized-noactivate",
           8: "show-noactivate", 9: "restore", 10: "showdefault",
           11: "forceminimize"}

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def proc_image_path(pid):
    """取进程可执行文件全路径；失败返回空串。"""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = w.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        kernel32.CloseHandle(h)


def enum_windows(include_hidden=False):
    """枚举有标题的顶层窗口，返回 dict 列表。

    默认只收可见窗口（`cmd_list` / `find_window` 要的是"看得见的那个"）。
    `include_hidden=True` 时连**隐藏**窗口一起收 —— 客户端收进托盘后主窗口就是
    隐藏的，只有这样才能找到它（见 `cmd_revive`）。
    """
    out = []

    def cb(hwnd, _lparam):
        if not include_hidden and not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        r = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        out.append({
            "hwnd": hwnd,
            "title": buf.value,
            "pid": pid.value,
            "cls": cls.value,
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "exe": proc_image_path(pid.value),
            "rect": [r.left, r.top, r.right - r.left, r.bottom - r.top],
            "show_cmd": wp.showCmd,
            "normal": [wp.rcNormalPosition.left, wp.rcNormalPosition.top,
                       wp.rcNormalPosition.right - wp.rcNormalPosition.left,
                       wp.rcNormalPosition.bottom - wp.rcNormalPosition.top],
        })
        return True

    user32.EnumWindows(ENUMPROC(cb), 0)
    return out


def find_window(channel):
    """按可执行文件路径把窗口归属到通道。找不到返回 None。"""
    hint = EXE_HINTS[channel]
    for win in enum_windows():
        if win["exe"].lower().endswith(hint):
            return win
    return None


class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", w.WCHAR * 32), ("dmSpecVersion", w.WORD),
        ("dmDriverVersion", w.WORD), ("dmSize", w.WORD), ("dmDriverExtra", w.WORD),
        ("dmFields", w.DWORD), ("dmOrientation", ctypes.c_short),
        ("dmPaperSize", ctypes.c_short), ("dmPaperLength", ctypes.c_short),
        ("dmPaperWidth", ctypes.c_short), ("dmScale", ctypes.c_short),
        ("dmCopies", ctypes.c_short), ("dmDefaultSource", ctypes.c_short),
        ("dmPrintQuality", ctypes.c_short), ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
        ("dmFormName", w.WCHAR * 32), ("dmLogPixels", w.WORD),
        ("dmBitsPerPel", w.DWORD), ("dmPelsWidth", w.DWORD),
        ("dmPelsHeight", w.DWORD), ("dmDisplayFlags", w.DWORD),
        ("dmDisplayFrequency", w.DWORD), ("dmICMMethod", w.DWORD),
        ("dmICMIntent", w.DWORD), ("dmMediaType", w.DWORD),
        ("dmDitherType", w.DWORD), ("dmReserved1", w.DWORD),
        ("dmReserved2", w.DWORD), ("dmPanningWidth", w.DWORD),
        ("dmPanningHeight", w.DWORD),
    ]


def primary_scale_factor():
    """主显示器缩放比（如 1.25）。

    ⚠️ 本进程 DPI-unaware，`GetDpiForMonitor` 只会返回 96，不可用。
    改用「真实分辨率 ÷ 虚拟化后的分辨率」：DPI-unaware 下
    `GetSystemMetrics(SM_CXSCREEN)` 给的是 DIP，而 `EnumDisplaySettingsW`
    给的是物理像素，两者之比即缩放比。
    """
    try:
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        # ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
        if user32.EnumDisplaySettingsW(None, 0xFFFFFFFF, ctypes.byref(dm)):
            virt = user32.GetSystemMetrics(0)
            if virt:
                return round(dm.dmPelsWidth / float(virt), 4)
    except Exception:
        pass
    return 1.0


# 状态文件与 Windows 还原矩形之间允许的像素容差（DIP 舍入会造成 ±1~2）
BOUNDS_TOLERANCE = 2


def bounds_close(a, b, tol=BOUNDS_TOLERANCE):
    if a is None or b is None:
        return False
    return all(abs(x - y) <= tol for x, y in zip(a, b))


# ---------------------------------------------------------------- 状态文件


def state_path(channel):
    return STATE_DIRS[channel] / "window-state.json"


def read_state(channel):
    p = state_path(channel)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return None
    except ValueError:
        return "CORRUPT"


BACKUP_KEEP = 20      # window_state_backups/ 只保留最近多少份


def _prune_backups(keep=BACKUP_KEEP):
    """把 window_state_backups/ 裁剪到最近 keep 份，返回删除数量。

    ⚠️ 本脚本是**独立分发**的（随 exe 放在 `_internal/` 下，用户直接 `python` 跑），
    **不能** import `switcher_common` —— 它的代码在 exe 的 PYZ 归档里，独立脚本取不到。
    所以这里自己实现一份最小的裁剪，与 `common.prune_backups` 语义一致。
    任何异常都吞掉：裁剪只是善后，删不掉最坏就是多留几份（见 AUDIT_2026-09-24.md P2-11）。
    """
    try:
        files = sorted((p for p in BACKUP_DIR.glob("*.window-state.*.json") if p.is_file()),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    removed = 0
    for p in files[int(keep):]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def backup_state(channel, reason):
    """把当前状态文件复制到 window_state_backups/，返回备份路径（失败返回 None）。

    写完就裁剪到最近 `BACKUP_KEEP` 份 —— 原先只增不删，长期堆积既占空间，
    也把窗口几何历史一直留在磁盘上（见 AUDIT_2026-09-24.md P2-11）。
    """
    src = state_path(channel)
    if not src.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = BACKUP_DIR / ("%s.window-state.%s.%s.json" % (channel, ts, reason))
    try:
        shutil.copy2(src, dst)
    except BaseException as exc:  # noqa: BLE001 - 备份失败不应中断主流程
        print("  ! 备份失败：%s" % exc)
        return None
    _prune_backups()
    return dst


def write_state(channel, state):
    p = state_path(channel)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(tmp, p)


def load_golden():
    try:
        with open(GOLDEN_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return {}
    except ValueError:
        return {}


def save_golden(data):
    with open(GOLDEN_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


def norm_bounds(b):
    """把 bounds 规整成 4 元组 int；非法返回 None。"""
    if not isinstance(b, dict):
        return None
    try:
        return (int(b["x"]), int(b["y"]), int(b["width"]), int(b["height"]))
    except (KeyError, TypeError, ValueError):
        return None


def fmt_bounds(b):
    return "%dx%d @ (%d,%d)" % (b[2], b[3], b[0], b[1])


# ---------------------------------------------------------------- 判定


def verdict(channel, state):
    """给出人类可读的判定行列表。"""
    lines = []
    if state is None:
        return ["状态文件不存在（客户端首次启动后才会生成）"]
    if state == "CORRUPT":
        return ["!! 状态文件 JSON 解析失败（客户端会回退到内建默认值 1200x800 居中）"]

    if state.get("version") != 2:
        lines.append("!! version=%r（客户端只认 2，否则回退默认值）" % state.get("version"))
    b = norm_bounds(state.get("bounds"))
    if b is None:
        lines.append("!! bounds 缺失或非法 → 客户端回退默认值")
        return lines

    lines.append("bounds = %s  isMaximized=%s  isFullScreen=%s"
                 % (fmt_bounds(b), state.get("isMaximized"), state.get("isFullScreen")))

    if b[2] < SUSPICIOUS_SIZE[0] or b[3] < SUSPICIOUS_SIZE[1]:
        lines.append("!! 还原尺寸偏小（< %dx%d）—— 取消最大化后窗口会缩到这个小尺寸"
                     % SUSPICIOUS_SIZE)
    if b[2] < CLIENT_MIN_SIZE[0] or b[3] < CLIENT_MIN_SIZE[1]:
        lines.append("!! 还原尺寸低于客户端最小尺寸 %dx%d，会被强制放大到最小值"
                     % CLIENT_MIN_SIZE)

    # 是否落在任一显示器工作区内
    win = find_window(channel)
    if win:
        rc = win["rect"]
        if not bounds_close(b, tuple(win["normal"])):
            lines.append("!! 状态文件与 Windows 记录的还原矩形不一致：文件 %s / Windows %s"
                         % (fmt_bounds(b), fmt_bounds(tuple(win["normal"]))))
        else:
            lines.append("OK 与 Windows 记录的还原矩形一致（容差 %dpx）" % BOUNDS_TOLERANCE)
        lines.append("实时窗口：%s  showCmd=%s(%s)"
                     % (fmt_bounds(tuple(rc)), win["show_cmd"],
                        SHOWCMD.get(win["show_cmd"], "?")))
    else:
        lines.append("-- 当前未找到该通道的可见窗口（可能已最小化到托盘）")
    return lines


# ---------------------------------------------------------------- 命令


def cmd_revive(args):
    """把「只剩托盘图标、点不出来」的客户端主窗口唤出来。

    成因：Electron 客户端关闭时走的是 `win.hide()` 而不是退出 —— 进程还在、
    托盘图标还在，但主窗口 `IsWindowVisible=0`。此时点托盘图标，应用按
    toggle 逻辑可能又执行一次 hide，于是永远出不来。

    办法：直接对主窗口 `ShowWindow`（保持它原来的最大化/还原状态），
    再尝试置前。这里**不改** window-state.json。
    """
    channels = [args.channel] if args.channel else ["wb", "wbai"]
    wins = enum_windows(include_hidden=True)
    for ch in channels:
        hint = EXE_HINTS[ch]
        mains = [x for x in wins
                 if hint.lower() in (x["exe"] or "").lower()
                 and x["cls"] == "Chrome_WidgetWin_1"
                 and x["rect"][2] >= 200 and x["rect"][3] >= 100]
        # 可见的排前面：已经看得见就没必要动它
        mains.sort(key=lambda x: (not x["visible"], -x["rect"][2] * x["rect"][3]))
        print("=" * 78)
        print("[%s] %s" % (ch, EXE_HINTS[ch]))
        if not mains:
            print("  没找到主窗口（客户端可能没在运行）")
            continue
        for x in mains:
            print("  hwnd=%-8d vis=%-5s %dx%d @(%d,%d) title=%r"
                  % (x["hwnd"], x["visible"], x["rect"][2], x["rect"][3],
                     x["rect"][0], x["rect"][1], x["title"][:40]))
        top = mains[0]
        if top["visible"]:
            print("  -> 窗口本来就看得见，无需唤出")
            continue
        cmd = 3 if top["show_cmd"] == 3 else 1   # SW_SHOWMAXIMIZED / SW_SHOWNORMAL
        user32.ShowWindow(top["hwnd"], cmd)
        # 前台切换可能被系统拒绝（前台锁定），失败不影响"看得见"
        fg = user32.GetForegroundWindow()
        tid_cur = kernel32.GetCurrentThreadId()
        tid_fg = user32.GetWindowThreadProcessId(fg, 0) if fg else 0
        attached = bool(tid_fg and tid_fg != tid_cur
                        and user32.AttachThreadInput(tid_cur, tid_fg, True))
        try:
            ok = bool(user32.SetForegroundWindow(top["hwnd"]))
        finally:
            if attached:
                user32.AttachThreadInput(tid_cur, tid_fg, False)
        now_vis = bool(user32.IsWindowVisible(top["hwnd"]))
        print("  -> ShowWindow(%d) 已执行；现在可见=%s，置前=%s"
              % (cmd, now_vis, ok))
    return 0


def cmd_list(_args):
    wins = enum_windows()
    wins.sort(key=lambda x: (x["exe"].lower(), x["title"]))
    print("%-42s %8s %-22s %s" % ("标题", "pid", "矩形", "可执行文件"))
    print("-" * 130)
    for x in wins:
        print("%-42s %8d %-22s %s"
              % (x["title"][:40], x["pid"], fmt_bounds(tuple(x["rect"])), x["exe"]))
    print("\n共 %d 个可见窗口。" % len(wins))
    print("缩放比（主显示器）= %.2f" % primary_scale_factor())


def cmd_show(args):
    channels = [args.channel] if args.channel else ["wb", "wbai"]
    for ch in channels:
        print("=" * 78)
        print("[%s] %s" % (ch, state_path(ch)))
        print("-" * 78)
        st = read_state(ch)
        for line in verdict(ch, st):
            print("  " + line)
        if st not in (None, "CORRUPT"):
            print("  原始内容：%s" % json.dumps(st, ensure_ascii=False, separators=(",", ":")))
    print("=" * 78)
    print("缩放比（主显示器）= %.2f —— 状态文件里的 bounds 是 DIP，乘以它才是物理像素"
          % primary_scale_factor())
    print("golden：%s%s" % (GOLDEN_PATH,
                            "" if GOLDEN_PATH.exists() else "（尚未创建）"))
    return 0


def _golden_entry(bounds, maximized, fullscreen):
    return {"bounds": {"x": bounds[0], "y": bounds[1],
                       "width": bounds[2], "height": bounds[3]},
            "isMaximized": bool(maximized), "isFullScreen": bool(fullscreen)}


def cmd_capture(args):
    ch = args.channel
    win = find_window(ch)
    if not win:
        print("找不到 [%s] 的可见窗口。请先把客户端窗口显示出来（从托盘唤出），再重试。" % ch)
        print("提示：用 `list` 看当前有哪些窗口。")
        return 2
    if win["show_cmd"] in (2, 7, 11):
        print("窗口当前是最小化状态，无法取到可信的还原矩形。请先还原窗口。")
        return 2

    b = tuple(win["normal"])
    maximized = win["show_cmd"] == 3
    st = read_state(ch)
    # ⚠️ `read_state(ch) or {}` **兜不住损坏态**：状态文件坏掉时它返回字符串 "CORRUPT"，
    #    而非空字符串是真值，`or {}` 不生效 → 下一行 st.get(...) 直接 AttributeError，
    #    capture 崩掉而不是降级。同文件 :543/:617 都用 `in (None, "CORRUPT")` 判，
    #    这里原先漏了 —— 见 AUDIT_2026-09-24.md P2-8。
    if not isinstance(st, dict):
        st = {}
    entry = _golden_entry(b, maximized, st.get("isFullScreen", False))

    golden = load_golden()
    golden[ch] = entry
    save_golden(golden)
    print("已固化 golden[%s] = %s  isMaximized=%s"
          % (ch, fmt_bounds(b), entry["isMaximized"]))
    if not maximized:
        print("  （窗口当前不是最大化状态，取到的就是它现在的真实尺寸）")
    else:
        print("  （窗口当前最大化，取到的是 Windows 记录的还原矩形）")
        print("  想换成别的大小：先取消最大化、拖成想要的样子，再跑一次 capture。")
    return 0


def _parse_size(text):
    for sep in ("x", "X", "*", ","):
        if sep in text:
            a, b = text.split(sep, 1)
            return int(a), int(b)
    raise ValueError("尺寸格式应为 WxH，例如 1600x1000")


def cmd_pin(args):
    ch = args.channel
    golden = load_golden()
    cur = golden.get(ch, {})
    cur_b = norm_bounds(cur.get("bounds")) or (0, 0) + CLIENT_DEFAULT_SIZE

    if args.size:
        width, height = _parse_size(args.size)
    else:
        width, height = cur_b[2], cur_b[3]

    if args.pos:
        x, y = (int(v) for v in args.pos.replace(" ", "").split(","))
    else:
        # 没给位置就居中到主显示器工作区
        r = RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
        x = r.left + max(0, ((r.right - r.left) - width) // 2)
        y = r.top + max(0, ((r.bottom - r.top) - height) // 2)

    maximized = cur.get("isMaximized", True) if args.maximized is None else args.maximized
    entry = _golden_entry((x, y, width, height), maximized, False)
    golden[ch] = entry
    save_golden(golden)
    print("已固化 golden[%s] = %s  isMaximized=%s"
          % (ch, fmt_bounds((x, y, width, height)), maximized))

    if args.apply:
        rc = apply_all(ch, entry, args.live, reason="pin")
        if not args.live:
            print("  ⚠️ 客户端只在**建窗时**读这个文件 —— 要让新值生效，需重启客户端，"
                  "或加 --live 直接改运行中窗口的还原矩形。")
        else:
            print("  提示：文件与运行中窗口都已对齐，无需重启。")
        return rc
    return 0


def _apply(ch, entry, reason):
    """把 golden 写进客户端状态文件（先备份）。"""
    bk = backup_state(ch, reason)
    if bk:
        print("  已备份原文件 → %s" % bk)
    st = read_state(ch)
    if st in (None, "CORRUPT"):
        st = {"version": 2}
    st["version"] = 2
    st["bounds"] = dict(entry["bounds"])
    st["isMaximized"] = entry["isMaximized"]
    st["isFullScreen"] = entry.get("isFullScreen", False)
    write_state(ch, st)
    print("  已写入 %s" % state_path(ch))
    return 0


def apply_all(ch, entry, live, reason):
    """按**正确顺序**落地 golden。

    ⚠️ 顺序很关键（实测踩过）：必须**先**改运行中窗口的还原矩形，**再**写状态文件。
    反过来的话，`SetWindowPlacement` 会触发窗口的 `resize`/`move`，客户端 500ms 去抖后
    会把**当时**的 `getNormalBounds()` 落盘，正好覆盖掉我们刚写进去的值 —— 实测就是这样
    被改回 812x607 的。
    """
    if live:
        apply_live(ch, entry)
        # 等客户端的 500ms 去抖落盘，之后我们写的值才是最终值
        time.sleep(1.2)
    return _apply(ch, entry, reason)


def apply_live(channel, entry):
    """把新的「还原矩形」直接写进**正在运行**的窗口，无需重启客户端。

    原理：客户端只在建窗时读 `window-state.json`，所以改文件对已运行的窗口无效。
    但 Windows 自己持有同一个「还原矩形」（`WINDOWPLACEMENT.rcNormalPosition`），
    用 `SetWindowPlacement` 改掉它即可 —— 且**保持 `showCmd` 不变**，所以窗口当前的
    最大化/还原状态不受影响，用户看不到任何跳变。

    之后客户端任何一次 `resize`/`move` 都会把 `getNormalBounds()`（= 新值）落盘，
    与 golden 自然对齐。
    """
    win = find_window(channel)
    if not win:
        print("  --live：当前找不到 [%s] 的可见窗口（可能已最小化到托盘），跳过。"
              "下次启动会读到新值。" % channel)
        return False
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(WINDOWPLACEMENT)
    if not user32.GetWindowPlacement(win["hwnd"], ctypes.byref(wp)):
        print("  --live：GetWindowPlacement 失败，跳过。")
        return False
    n = wp.rcNormalPosition
    before = (n.left, n.top, n.right - n.left, n.bottom - n.top)
    b = entry["bounds"]
    n.left, n.top = b["x"], b["y"]
    n.right, n.bottom = b["x"] + b["width"], b["y"] + b["height"]
    # showCmd / flags 原样保留 —— 不改变窗口当前的最大化或还原状态
    if not user32.SetWindowPlacement(win["hwnd"], ctypes.byref(wp)):
        print("  --live：SetWindowPlacement 被系统拒绝，跳过（重启客户端即可生效）。")
        return False
    after = (n.left, n.top, n.right - n.left, n.bottom - n.top)
    print("  --live：运行中窗口的还原矩形 %s → %s（showCmd=%s 保持不变，无可见跳变）"
          % (fmt_bounds(before), fmt_bounds(after),
             SHOWCMD.get(wp.showCmd, wp.showCmd)))
    return True


def cmd_check(args):
    golden = load_golden()
    channels = [args.channel] if args.channel else ["wb", "wbai"]
    drifted = 0
    for ch in channels:
        want = golden.get(ch)
        if not want:
            print("[%s] 跳过：golden 里没有该通道。先跑 capture 或 pin。" % ch)
            continue
        want_b = norm_bounds(want.get("bounds"))
        st = read_state(ch)
        if st in (None, "CORRUPT"):
            print("[%s] 状态文件缺失/损坏 → 需要修复" % ch)
            if args.fix:
                apply_all(ch, want, args.live, reason="check")
            drifted += 1
            continue
        got_b = norm_bounds(st.get("bounds"))
        win = find_window(ch)
        live_b = tuple(win["normal"]) if win else None

        file_ok = (bounds_close(got_b, want_b)
                   and bool(st.get("isMaximized")) == bool(want.get("isMaximized")))
        live_ok = (live_b is None) or bounds_close(live_b, want_b)

        if file_ok and live_ok:
            print("[%s] OK  文件 %s isMaximized=%s / 运行中还原矩形 %s"
                  % (ch, fmt_bounds(got_b), st.get("isMaximized"),
                     fmt_bounds(live_b) if live_b else "（窗口不可见）"))
            continue

        drifted += 1
        if not file_ok:
            print("[%s] 文件漂移！期望 %s isMaximized=%s / 实际 %s isMaximized=%s"
                  % (ch, fmt_bounds(want_b) if want_b else "?", want.get("isMaximized"),
                     fmt_bounds(got_b) if got_b else "?", st.get("isMaximized")))
        if not live_ok:
            print("[%s] 运行中窗口漂移！期望还原矩形 %s / 实际 %s（需 --live 才能就地修正）"
                  % (ch, fmt_bounds(want_b) if want_b else "?", fmt_bounds(live_b)))

        if args.fix:
            if not live_ok and args.live:
                # 先改窗口、等去抖落盘，再写文件 —— 顺序不能反（见 apply_all 注释）
                apply_all(ch, want, True, reason="check")
                print("  已处理（窗口 + 文件）。")
            else:
                if not file_ok:
                    _apply(ch, want, reason="check")
                if not live_ok:
                    print("  提示：加 --live 可同时修正运行中的窗口。")
                print("  已处理。")
        else:
            print("  （加 --fix 才会回写）")
    return 1 if drifted else 0


def cmd_watch(args):
    ch = args.channel
    deadline = time.time() + args.seconds
    print("[%s] 采样 %d 秒，间隔 %.2fs —— 只在变化时输出" % (ch, args.seconds, args.interval))
    print("  提示：保持窗口可见；期间去客户端里复现「窗口被还原」的操作。")
    last = None
    count = 0
    while time.time() < deadline:
        win = find_window(ch)
        try:
            mt = state_path(ch).stat().st_mtime
        except OSError:
            mt = 0.0
        if win:
            cur = (tuple(win["rect"]), win["show_cmd"], tuple(win["normal"]),
                   round(mt, 3))
        else:
            cur = None
        if cur != last:
            count += 1
            stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            if win:
                print("%s  rect=%s showCmd=%s normal=%s  state_mtime=%s"
                      % (stamp, fmt_bounds(tuple(win["rect"])),
                         SHOWCMD.get(win["show_cmd"], win["show_cmd"]),
                         fmt_bounds(tuple(win["normal"])),
                         datetime.fromtimestamp(mt).strftime("%H:%M:%S.%f")[:-3] if mt else "-"))
            else:
                print("%s  <窗口不可见>" % stamp)
            last = cur
        time.sleep(args.interval)
    print("=== 结束，共 %d 条变化 ===" % count)
    return 0


# ---------------------------------------------------------------- 入口


def build_parser():
    p = argparse.ArgumentParser(
        description="WorkBuddy / WorkBuddyAI 主窗口状态守卫",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("## 本工具的命令")[1] if "## 本工具的命令" in __doc__ else "")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_channel(sp, required=True):
        sp.add_argument("--channel", choices=["wb", "wbai"], required=required,
                        help="wb=WorkBuddy（国服） wbai=WorkBuddyAI（国际服）")

    sp = sub.add_parser("list", help="列出所有可见顶层窗口")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("show", help="打印状态文件与实时窗口几何")
    add_channel(sp, required=False)
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("capture", help="把当前实时窗口的还原矩形固化为 golden")
    add_channel(sp)
    sp.set_defaults(func=cmd_capture)

    sp = sub.add_parser("pin", help="手工指定 golden，可立即生效")
    add_channel(sp)
    sp.add_argument("--size", help="尺寸 WxH，例如 1600x1000")
    sp.add_argument("--pos", help="位置 X,Y；不填则居中")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--maximized", dest="maximized", action="store_true", default=None)
    g.add_argument("--no-maximized", dest="maximized", action="store_false")
    sp.add_argument("--apply", action="store_true", help="立即写入客户端状态文件")
    sp.add_argument("--live", action="store_true",
                    help="配合 --apply：同时改运行中窗口的还原矩形（无需重启客户端）")
    sp.set_defaults(func=cmd_pin)

    sp = sub.add_parser("check", help="比对状态文件与 golden")
    add_channel(sp, required=False)
    sp.add_argument("--fix", action="store_true", help="漂移时回写（先备份）")
    sp.add_argument("--live", action="store_true", help="配合 --fix：同时改运行中窗口")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("revive",
                        help="客户端只剩托盘图标时，把隐藏的主窗口唤出来（不改状态文件）")
    add_channel(sp, required=False)
    sp.set_defaults(func=cmd_revive)

    sp = sub.add_parser("watch", help="采样窗口几何变化")
    add_channel(sp)
    sp.add_argument("--seconds", type=int, default=60)
    sp.add_argument("--interval", type=float, default=0.2)
    sp.set_defaults(func=cmd_watch)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
