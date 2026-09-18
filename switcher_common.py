#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""switcher_common.py —— WorkBuddy / Trae 两个切换器共用的基础设施。

抽出这部分是为了避免 wb_ui_server.py 与 tw_ui_server.py 各自维护一份几乎相同的
HTTP 骨架（此前已出现修复一处、漏掉另一处的漂移）。这里只放与业务无关的通用件：

- `_json` / `_same_origin`：响应构造与跨站校验
- `BaseHandler`：HTTP 骨架（GET/POST 分发、405/404/500、同源守卫、参数解析）
- `file_lock`：跨进程文件锁（Windows 用 msvcrt，POSIX 用 fcntl）
- `prune_backups`：备份保留策略（只留最近 N 份）
- `bind_server`：端口占用自动避让

业务差异（路由、账号读写）仍留在各自的 server 里，通过 `api_get` / `api_post` 挂钩。
"""
import base64
import contextlib
import datetime
import json
import os
import re
import secrets
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

_AUDIT_LOCK = threading.Lock()
AUDIT_NAME = "switcher.log"
# 单份审计日志的大小上限（1 MiB）。超过就滚成 <名字>.1（覆盖旧 .1）。
# 本地工具不必上多代轮转，但只追加不封顶会一直吃盘。
AUDIT_MAX_BYTES = 1 << 20


def audit(log_dir, source, action, target="", ok=True, detail=""):
    """写一条操作审计（切号 / 增删账号 / 续期）。日志不落凭据，只记对象与结果。

    本地工具此前对凭据操作零记录，出问题无法回溯；这里用追加写 + 线程锁，
    任何失败都静默忽略（审计不能反过来拖垮主流程）。
    """
    try:
        d = Path(log_dir) if log_dir else None
        if not d:
            return
        d.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = "%s [%s] %-10s %-4s target=%-32s %s\n" % (
            ts, source, action, "OK" if ok else "FAIL",
            (target or "-")[:32], str(detail or "").replace("\n", " ")[:160])
        with _AUDIT_LOCK:
            path = d / AUDIT_NAME
            try:
                # 轮转失败不能影响写入：所以单独 try 包住，失败就继续往原文件追加
                if path.exists() and path.stat().st_size >= AUDIT_MAX_BYTES:
                    path.replace(d / (AUDIT_NAME + ".1"))
            except OSError:
                pass
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        pass


def new_token():
    """生成一次性访问令牌（仅本次进程有效，不落盘）。"""
    return secrets.token_urlsafe(24)


def render_template(raw, ctx):
    """把 HTML 模板里的 {{KEY}} 替换成配置值（两个切换器共用同一份模板）。

    简单字符串替换即可，不上模板引擎 —— 只为了消灭两份几乎相同的前端文件。
    """
    text = raw if isinstance(raw, str) else raw.decode("utf-8")
    for key, val in (ctx or {}).items():
        text = text.replace("{{%s}}" % key, str(val))
    return text.encode("utf-8")


def token_source(token):
    """取 JWT payload 里的 token_source（决定有效期长短的签发通道）。"""
    seg = str(token or "").split(".")
    if len(seg) < 2:
        return ""
    p = seg[1]
    p += "=" * (-len(p) % 4)
    try:
        return str(json.loads(base64.urlsafe_b64decode(p)).get("token_source") or "")
    except Exception:  # noqa: BLE001
        return ""


def resolve_data(base_dir, name):
    """定位随包数据文件（前端页面等）。

    PyInstaller 6 把 datas 放到 `_internal/` 而不是 exe 同级，所以打包后按 exe 同级
    找 html 会 404。这里先找 exe 同级（方便自行替换），再回退到 `_internal`。
    """
    base = Path(base_dir)
    for cand in (base / name, base / "_internal" / name):
        if cand.is_file():
            return cand
    return base / name


_HOME = str(Path.home()).replace("\\", "/")


def scrub(text, limit=200):
    """错误信息脱敏：用户目录替换成 ~，统一分隔符并限长。

    异常里常常带出本机绝对路径，直接抛给前端等于泄露目录结构。
    """
    t = str(text).replace("\\", "/")
    if _HOME and _HOME in t:
        t = t.replace(_HOME, "~")
    return t if len(t) <= limit else t[:limit] + "…"


def err_payload(exc):
    """统一的 500 响应体（脱敏后的异常信息）。"""
    return {"ok": False,
            "message": "服务器错误（%s）：%s" % (type(exc).__name__, scrub(exc))}


def require_module(name, attrs=(), who=""):
    """导入被复用的模块并校验契约，失败时给出可读提示而不是裸 Traceback。

    本工具依赖上级「自动签到」项目的 workbuddy_checkin / trae_work_checkin，
    目录一变或函数改名就会在 import 阶段崩，且报错对使用者毫无信息量。
    """
    import importlib
    try:
        mod = importlib.import_module(name)
    except ImportError as e:
        raise SystemExit(
            "找不到依赖模块 %s（%s）。\n"
            "本工具复用上级「自动签到」项目的解析/续期逻辑，请确认 D:/AI项目/自动签到 存在且包含 %s.py；\n"
            "若目录布局变化，请调整 %s 顶部的 PROJECT_ROOT 定位逻辑。\n原始错误：%s"
            % (name, who or "切换器", name, who or "切换器", e))
    missing = [a for a in attrs if not hasattr(mod, a)]
    if missing:
        raise SystemExit(
            "依赖模块 %s 版本不匹配，缺少本工具需要的成员：%s\n"
            "请同步更新上级「自动签到」项目，或固定一个兼容版本。"
            % (name, ", ".join(missing)))
    return mod

# 仅允许回环地址访问（服务本身也只绑 127.0.0.1，这里是第二道防线）
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

# 完整 accessToken 的最小长度（正常 JWT 都在 1000 字符以上）
MIN_TOKEN_LEN = 200


def token_looks_complete(token):
    """accessToken 是否像完整的 JWT：三段结构且长度正常。

    粘贴/导入时被截断的登录态（实测出现过 4 字符的 "eyJx"）能通过
    startswith("eyJ") 这类宽松校验，实际请求时却必然 401，因此用「段数 + 长度」兜底。
    """
    t = str(token or "")
    return t.count(".") >= 2 and len(t) >= MIN_TOKEN_LEN


def _json(data, code=200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return code, body, "application/json; charset=utf-8"


def same_origin(headers):
    """本地服务的跨站防护：Host 必须是回环地址；带 Origin 时也必须指向回环。

    浏览器对 fetch/XHR 一定会带 Origin，因此外部网页发起的跨站写请求会被拦下；
    curl 等不带 Origin 的直连不受影响（仍受 Host 校验约束）。
    """
    host = (headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
    if host not in LOCAL_HOSTS:
        return False
    origin = headers.get("Origin")
    if not origin:
        return True
    try:
        ohost = (urlparse(origin).hostname or "").strip("[]")
    except Exception:  # noqa: BLE001
        return False
    return ohost in LOCAL_HOSTS


@contextlib.contextmanager
def file_lock(key, lock_dir, timeout=15.0):
    """跨进程文件锁，保护「读-改-写」序列不被并发打断。

    key 用于区分资源（如 "wb-desktop"、"wb-auth-xxx.info"）。锁文件统一放在
    lock_dir（本工具目录下的 .locks/），不污染客户端配置目录。
    超时未拿到锁抛 TimeoutError，避免死锁把界面卡住。
    """
    d = Path(lock_dir)
    d.mkdir(parents=True, exist_ok=True)
    lp = d / ("%s.lock" % re.sub(r"[^0-9A-Za-z_.-]", "_", str(key)))
    # 先确保锁文件存在（"r+b" 不会创建）。不用 "a+b"：Windows 下追加模式打开的句柄
    # 没有读权限，seek/read 会抛 PermissionError。
    if not lp.exists():
        lp.touch()
    fh = open(lp, "r+b")
    locked = False
    deadline = time.time() + timeout
    try:
        # 锁的是第 0 字节，文件必须至少有 1 字节才能上锁。此处只在文件为空时补一个字节，
        # 之后不再写任何内容 —— 早期实现每次加锁都 append 一次 pid，锁文件会随调用次数
        # 无限增长（每次 ~6 字节），而 pid 信息实际无人读取。
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"0")
            fh.flush()
        while True:
            try:
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.time() > deadline:
                    raise TimeoutError("等待文件锁超时（可能被其它切换/续期操作占用）：%s" % lp)
                time.sleep(0.1)
        yield
    finally:
        if locked:
            try:
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except (OSError, ValueError):
                pass
        try:
            fh.close()
        except OSError:
            pass


def prune_backups(directory, pattern, keep=10, exclude=()):
    """把目录下匹配 pattern 的备份文件裁剪到最近 keep 份（按修改时间）。

    备份本身是**有效的登录态**，长期堆积等于把凭据散落在磁盘各处，
    因此每次产生新备份后调用本函数做一次清理。返回删除数量。

    ⚠️ 本函数**绝不允许抛出**：它只是善后清理，删不掉最坏也只是多留几份备份，
    但如果让异常冒出去，调用方（切号）会半途中断 —— 那是真正的数据损坏。
    所以这里连 BaseException 一起吞（只放行 KeyboardInterrupt）。

    为什么必须连 BaseException 一起吞：这台机器的 Python 被注入了 WorkBuddy CLI 的
    「安全删除」shim（sitecustomize），删除会先过一遍批量删除守卫
    （safe-delete-bulk-guard.cjs）。守卫判定 confirmRequired/rejected 时
    process.exit(2/3)，Python 侧随即 `raise SystemExit(1)` —— SystemExit 继承自
    BaseException 而不是 Exception，`except OSError` 和上层 `except Exception`
    都兜不住，结果是请求连响应都不发就断连（浏览器只看到 Failed to fetch），
    审计日志里一条记录都没有，完全查不到原因。实测就是切号偶发"提示失败"。
    """
    d = Path(directory)
    if not d.is_dir():
        return 0
    skip = set(exclude)
    try:
        files = [f for f in d.glob(pattern) if f.is_file() and f.name not in skip]
        if len(files) <= keep:
            return 0
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    removed = 0
    for f in files[keep:]:
        try:
            f.unlink()
            removed += 1
        except KeyboardInterrupt:
            raise
        except BaseException:  # noqa: BLE001  含 shim 的 SystemExit
            pass
    return removed


def safe_unlink(path):
    """尽力删除一个文件，绝不抛出。用于「回滚 / 清理」这类善后动作。

    与 prune_backups 同样的理由：调用方多半正处在"主操作已经失败、需要回滚"的
    路径上，这里再抛一个异常只会把真正的失败原因盖掉。
    """
    try:
        Path(path).unlink()
        return True
    except FileNotFoundError:
        return True
    except KeyboardInterrupt:
        raise
    except BaseException:  # noqa: BLE001
        return False


def safe_rmtree(path):
    """尽力递归删除一个目录，绝不抛出。语义同 safe_unlink。"""
    p = Path(path)
    if not p.exists():
        return True
    try:
        shutil.rmtree(p)
        return True
    except FileNotFoundError:
        return True
    except KeyboardInterrupt:
        raise
    except BaseException:  # noqa: BLE001  含安全删除 shim 的 SystemExit
        return False


def port_in_use(host="127.0.0.1", port=8765, timeout=0.25):
    """端口是否已有服务在监听。Windows 上 SO_REUSEADDR 允许重复 bind，
    所以不能只靠 bind 是否报错来判断，必须先主动连一下。"""
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


class QuietHTTPServer(ThreadingHTTPServer):
    """本地服务用的 HTTP 服务端：客户端中途断开不该在控制台刷 traceback。

    浏览器刷新页面、切换账号会取消尚未完成的请求（积分查询要打若干外部接口，
    耗时可到秒级），服务器随后写响应便会拿到 WinError 10053 ConnectionAborted。
    这是正常现象，但 socketserver 默认把它当错误打到 stderr，看起来像程序出了问题。
    """

    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys as _sys
        exc = _sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, BrokenPipeError, TimeoutError)):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


def bind_server(handler_cls, port, host="127.0.0.1", tries=10):
    """绑定本地服务；端口被占用时自动顺延到下一个可用端口。

    返回 (server, 实际端口)。连续 tries 个端口都不可用则抛 OSError。
    """
    last = None
    for p in range(int(port), int(port) + int(tries)):
        if port_in_use(host, p):
            last = OSError("端口 %d 已被占用" % p)
            continue
        try:
            return QuietHTTPServer((host, p), handler_cls), p
        except OSError as e:
            last = e
    raise OSError("端口 %s 起连续 %d 个端口都不可用：%s" % (port, tries, last))


# ---------------------------------------------------------------------------
# 单实例保护
# ---------------------------------------------------------------------------
# 为什么用 Windows 命名互斥体而不是 PID 锁文件（已实测）：
#   - 第一个实例正常退出 → 内核自动释放
#   - 第一个实例**被强杀 / 崩溃** → 内核同样自动释放（实测 kill -9 后下一个进程
#     拿到 errno=0），不会留下假锁，也不会被 PID 复用误判
#   - 不需要任何清理逻辑
# 命名空间用 Local\：每个登录会话独立，允许多用户 / 多 RDP 会话各跑一份，互不干扰。
# 互斥体名必须带 source（wb / tw），否则两个切换器会互相把对方挤掉。
_MUTEX_HANDLES = []          # 句柄必须活到进程结束，放模块级防止被 GC 回收
ERROR_ALREADY_EXISTS = 183
PORT_TRIES = 10              # 端口顺延范围，与 bind_server 默认一致

# 互斥体名**必须带 source**：wb 与 tw 是两个不同工具（默认端口 8765 / 8766），
# 共用同一个名字会让后启动的那个被误判成"已有实例"而被拒。
MUTEX_NAME_WB = "Local\\WorkBuddySwitcher-wb"
MUTEX_NAME_TW = "Local\\TraeSwitcher-tw"

# /api/ping 里合法的 app 标识。探测方必须按这个白名单校验，不能只看"有没有 app 键"。
OUR_APPS = ("wb_switcher", "tw_switcher")


def source_version(path, tag):
    """用源文件 mtime 生成构建戳。

    用途只有一个：让第二个实例能看出"那个在跑的实例是不是旧代码"
    —— 本会话踩过：8765 上跑着 02:16 启动的旧实例，一直用旧模板服务，
    让人以为"改版没生效"。git 在打包态未必可用，mtime 最稳。

    打包态下 `__file__` 指向 PyInstaller 的临时解包目录（每次启动路径都不同、
    且不保证能 stat），所以再回退到 `sys.executable`（= exe 本身）——
    否则 exe 的版本戳会退化成裸 tag，就失去了比对意义。
    """
    import sys as _sys
    import time as _t
    for cand in (path, getattr(_sys, "executable", None)):
        if not cand:
            continue
        try:
            st = Path(cand).stat()
        except OSError:
            continue
        return "%s-%s" % (tag, _t.strftime("%Y%m%d-%H%M%S", _t.localtime(st.st_mtime)))
    return tag


def acquire_single_instance(name):
    """尝试成为唯一实例。

    返回 (handle, already_running)：
      - already_running=False → 拿到所有权，正常启动
      - already_running=True  → 已有实例，调用方应复用或退出
      - handle 为 None 表示平台不支持或创建失败 → **降级放行**，不阻断启动
    """
    if os.name != "nt":
        return None, False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        h = k32.CreateMutexW(None, False, name)
        err = ctypes.get_last_error()
        if not h:
            return None, False          # 权限等导致创建失败 → 降级
        _MUTEX_HANDLES.append(h)
        return h, (err == ERROR_ALREADY_EXISTS)
    except Exception:  # noqa: BLE001
        return None, False


def probe_instance(port, timeout=0.6, host="127.0.0.1"):
    """探测某个端口上是不是我们自己的实例。

    返回 /api/ping 的 JSON（dict）；端口没服务、或响应不是**我们的**工具，返回 None。
    用来把三种情况区分开：我们的实例 / 别人的程序 / 空闲。

    ⚠️ 必须校验 app 在 OUR_APPS 里，不能只看"有没有 app 键" —— 别的程序也可能有
    /api/ping，只认键会把它们误判成我们的实例（自检抓到过）。
    """
    import urllib.request
    url = "http://%s:%d/api/ping" % (host, int(port))
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if r.status != 200:
                return None
            data = json.loads(r.read().decode("utf-8", "replace"))
        if not isinstance(data, dict) or data.get("app") not in OUR_APPS:
            return None
        return data
    except Exception:  # noqa: BLE001  连不上/不是 JSON/不是我们的 → 一律 None
        return None


def probe_instance_range(port, tries=10, timeout=0.4, host="127.0.0.1"):
    """在 [port, port+tries) 里找我们的实例（第一个实例可能因端口占用顺延过）。"""
    for p in range(int(port), int(port) + int(tries)):
        info = probe_instance(p, timeout=timeout, host=host)
        if info:
            return info
    return None


def single_instance_guard(mutex_name, port, tries=PORT_TRIES, log=print, wait=5.0,
                          default_port=None):
    """启动前的统一入口。返回 (handle, action, existing)。

    action:
      "start" —— 可以正常启动
      "reuse" —— 已有同款实例在跑，调用方应打开浏览器指向它然后退出
      "abort" —— 有实例占着互斥体，但端口上找不到它（异常情况），应报错退出

    **顺序很重要：先探端口，再拿互斥体。** 只靠互斥体会漏掉一个真实场景 ——
    升级前启动的旧实例**不持有互斥体**，新版本照样能拿到，于是又变成两个实例
    （本会话实测过：8765 上跑着 02:16 的旧实例，新起的会顺延到 8766）。
    反过来只探端口也不够：两个实例同时启动时都还没 bind，会双双通过。
    两步合起来才既覆盖过渡期、又挡住竞态。
    """
    def _probe_all(timeout=0.4):
        """在"请求的端口区间"和"默认端口区间"里找我们的实例。

        两个区间都要探：互斥体是**按工具全局**的，与端口无关。若只探请求区间，
        那么"已有实例在默认端口、而这次显式传了别的 --port"就会探不到，
        误报成 abort（实测踩过）。
        """
        for start in ({int(port), int(default_port)} if default_port else {int(port)}):
            info = probe_instance_range(start, tries=tries, timeout=timeout)
            if info and str(info.get("pid")) != str(os.getpid()):
                return info
        return None

    # 1) 端口上已经有我们的实例？（含不持互斥体的旧版本）
    info = _probe_all()
    if info:
        return None, "reuse", info

    # 2) 拿互斥体，挡住"同时启动"的竞态
    handle, already = acquire_single_instance(mutex_name)
    if not already:
        return handle, "start", None

    # 拿到句柄但对象已存在：对方可能刚启动、还没 bind。给它一点时间出现。
    deadline = time.time() + max(0.0, float(wait))
    while True:
        info = _probe_all(timeout=0.3)
        if info:
            return handle, "reuse", info
        if time.time() >= deadline:
            break
        time.sleep(0.25)
    return handle, "abort", None


def report_reuse(info, open_browser, app_version, default_port):
    """已有同款实例在跑：指向它，不再起第二个。返回退出码 0。"""
    url = "http://127.0.0.1:%d/" % info.get("port", default_port)
    print("[提示] 已有实例在运行（pid %s，端口 %s），直接复用，不再启动第二个。"
          % (info.get("pid"), info.get("port")), flush=True)
    if info.get("version") and info["version"] != app_version:
        print("[警告] 那个实例的版本是 %r，当前是 %r —— 它可能在跑旧代码。"
              % (info.get("version"), app_version), flush=True)
        print("       建议先关掉它（在它的窗口按 Ctrl+C）再重新启动。", flush=True)
    print("       页面地址：%s" % url, flush=True)
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    return 0


def report_abort(port, tries=PORT_TRIES):
    """有实例占着互斥体，却在其端口区间探测不到它 —— 异常状态，明确报错。返回 1。"""
    print("[错误] 已有切换器实例在运行，但在 %d~%d 端口上都探测不到它。"
          % (port, int(port) + int(tries) - 1), flush=True)
    print("       可能原因：它启动后卡死；或它的端口被别的程序抢走了。", flush=True)
    print("       请先结束已有的切换器进程（任务管理器里找 python.exe / WorkBuddySwitcher.exe）再重试。", flush=True)
    return 1


class BaseHandler(BaseHTTPRequestHandler):
    """HTTP 骨架。子类只需提供 INDEX_FILE / WRITE_ENDPOINTS 与 api_get / api_post。"""

    INDEX_FILE = None        # 直接指定前端页面路径（可选）
    INDEX_NAME = None        # 或在 BASE_DIR / _internal 下按文件名查找
    BASE_DIR = None          # 查找根目录（打包版会被启动器改成 exe 所在目录）
    UI_CONTEXT = None        # 模板变量（两个切换器共用 ui_template.html）
    WRITE_ENDPOINTS = ()     # 只允许 POST 的路径
    server_version = "SwitcherHTTP/1.0"
    TOKEN = None             # 一次性访问令牌；非空时写操作必须带 X-Switcher-Token
    AUDIT_DIR = None         # 审计日志目录；非空则记录写操作
    SOURCE = "app"           # 审计里的来源标记（wb / tw）
    AUDIT_ACTIONS = {}       # 路径 → 审计动作名
    APP_NAME = "switcher"    # /api/ping 里的身份标识，用于区分"是不是我们"
    APP_VERSION = ""         # 构建戳，用于识别"跑着旧代码的实例"
    STARTED_AT = 0           # 进程启动时间戳

    # --- 基础收发 -------------------------------------------------------
    def log_message(self, fmt, *args):
        pass

    def _send(self, body, code=200, ctype="application/json; charset=utf-8"):
        """写响应。客户端提前断开（刷新页面、取消未完成的请求）是常态，不是故障：
        此时再往上抛只会让 socketserver 打一堆 traceback，所以连接类异常一律静默收尾。
        """
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    def _send_json(self, payload, code=200):
        self._send(_json(payload)[1], code)

    # --- 守卫与参数 -----------------------------------------------------
    def _guard(self):
        if not same_origin(self.headers):
            self._send_json({"ok": False, "message": "跨站请求已被拒绝（仅允许本机页面调用）"}, 403)
            return False
        return True

    def _params(self):
        """解析请求参数：query string 与 JSON body 合并（body 优先）。"""
        data = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return data
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            obj = json.loads(raw)
        except ValueError:
            data.update({k: v[0] for k, v in parse_qs(raw).items()})
            return data
        if isinstance(obj, dict):
            data.update(obj)
        return data

    # --- 路由骨架 -------------------------------------------------------
    def _handle_request_error(self, exc, path="", target=""):
        """把处理过程中冒出来的异常转成一个**能看见**的 JSON 响应 + 审计记录。

        之前的写法是 `except (ConnectionError, BrokenPipeError, TimeoutError):
        self.close_connection = True`，本意只是"客户端中途断开时别刷 traceback"，
        但它顺手把两类**真实故障**也吞了：

        - `TimeoutError` 同时是 `socket.timeout` 和 `file_lock` 超时抛的类型，
          网络卡住/锁被占用都会走到这里；
        - 这台机器注入的 WorkBuddy「安全删除」shim 在批量删除守卫拒绝时抛
          `SystemExit(1)`（BaseException），`except Exception` 兜不住。

        两者的共同后果是：**服务端不发任何响应就断连**，浏览器只看到
        `TypeError: Failed to fetch`，审计日志一条没有 —— 用户报"切换账号提示失败"
        却完全查不到原因。所以这里只把"确实已经写不回去"的连接类异常当断连处理，
        其余一律回一个带原因的响应并记审计。
        """
        if isinstance(exc, (ConnectionError, BrokenPipeError)):
            self.close_connection = True
            return
        if isinstance(exc, TimeoutError):
            # 消息同样要脱敏：file_lock 的超时文案里带锁文件绝对路径
            code = 504
            message = "本地服务处理超时（可能被其它切换/续期占用，请稍后重试）：%s" % scrub(exc)
        else:
            code = 500
            message = err_payload(exc)["message"]
        payload = {"ok": False, "message": message, "error": type(exc).__name__}
        if self.AUDIT_DIR and path:
            audit(self.AUDIT_DIR, self.SOURCE,
                  self.AUDIT_ACTIONS.get(path, path), target, False, message)
        try:
            self._send_json(payload, code)
        except (ConnectionError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    def do_GET(self):
        try:
            u = urlparse(self.path)
            if u.path == "/":
                self._serve_index()
                return
            if u.path == "/api/ping":
                # 身份端点：让第二个实例能判断"这个端口上是不是我们自己"。
                # 放在 _guard() 之前 —— 探测方只带 Host，不需要令牌，也不该被同源策略挡。
                self._send_json({
                    "ok": True, "app": self.APP_NAME, "source": self.SOURCE,
                    "pid": os.getpid(), "port": self.server.server_address[1],
                    "version": self.APP_VERSION, "started_at": self.STARTED_AT,
                }, 200)
                return
            if not self._guard():
                return
            if u.path in self.WRITE_ENDPOINTS:
                self._send_json({"ok": False, "message": "该接口仅支持 POST"}, 405)
                return
            code, payload = self.api_get(u)
            self._send_json(payload, code)
        except KeyboardInterrupt:
            raise
        except BaseException as e:  # noqa: BLE001  含 SystemExit（见 _handle_request_error）
            self._handle_request_error(e)

    def do_POST(self):
        path, target = "", ""
        try:
            u = urlparse(self.path)
            path = u.path
            p = self._params()  # 先读净请求体，避免拒绝时客户端收到连接重置
            target = str(p.get("name") or p.get("file") or "")
            if not self._guard():
                return
            if self.TOKEN and self.headers.get("X-Switcher-Token") != self.TOKEN:
                self._send_json({"ok": False, "message": "缺少或错误的一次性访问令牌"}, 401)
                audit(self.AUDIT_DIR, self.SOURCE, u.path, "", False, "令牌校验失败")
                return
            code, payload = self.api_post(u, p)
            if self.AUDIT_DIR:
                audit(self.AUDIT_DIR, self.SOURCE,
                      self.AUDIT_ACTIONS.get(u.path, u.path),
                      target, bool(payload.get("ok")), payload.get("message"))
            self._send_json(payload, code)
        except KeyboardInterrupt:
            raise
        except BaseException as e:  # noqa: BLE001  含 SystemExit（见 _handle_request_error）
            self._handle_request_error(e, path, target)

    def api_get(self, url):
        raise NotImplementedError

    def api_post(self, url, params):
        raise NotImplementedError

    def _index_path(self):
        if self.INDEX_FILE:
            return Path(self.INDEX_FILE)
        name = self.INDEX_NAME or "index.html"
        return resolve_data(self.BASE_DIR or Path(__file__).resolve().parent, name)

    def _serve_index(self):
        idx = self._index_path()
        if not idx or not idx.is_file():
            self._send(b"index not found", 404, "text/plain; charset=utf-8")
            return
        body = idx.read_bytes()
        if self.UI_CONTEXT:
            body = render_template(body, self.UI_CONTEXT)
        if self.TOKEN:
            # 把一次性令牌注入页面：前端拿到后随写请求回传，
            # 进程外的脚本/程序拿不到（除非也去抓取首页并解析）
            inject = ('<script>window.SWITCHER_TOKEN="%s";</script>' % self.TOKEN).encode("utf-8")
            body = self._inject_head(body, inject)
        self._send(body, 200, "text/html; charset=utf-8")

    @staticmethod
    def _inject_head(body, inject):
        r"""把脚本插到 <head> 之后。

        早期实现按精确的 b"<head>" 定位，一旦模板改成 <head lang="zh"> 这类带属性的写法
        就会静默失配 —— 页面照常打开，但前端拿不到令牌，所有写操作一律 401，且错误信息
        完全指不到这里。因此用正则匹配带属性的 head，head 缺失时依次退回 <body> 与文件头。
        """
        m = re.search(rb"<head\b[^>]*>", body, re.IGNORECASE)
        if m:
            return body[:m.end()] + b"\n" + inject + body[m.end():]
        m = re.search(rb"<body\b[^>]*>", body, re.IGNORECASE)
        if m:
            return body[:m.end()] + b"\n" + inject + body[m.end():]
        return inject + b"\n" + body
