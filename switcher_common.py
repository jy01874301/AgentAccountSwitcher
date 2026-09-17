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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

_AUDIT_LOCK = threading.Lock()
AUDIT_NAME = "switcher.log"


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
            with open(d / AUDIT_NAME, "a", encoding="utf-8") as fh:
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
    """
    d = Path(directory)
    if not d.is_dir():
        return 0
    skip = set(exclude)
    files = [f for f in d.glob(pattern) if f.is_file() and f.name not in skip]
    if len(files) <= keep:
        return 0
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    removed = 0
    for f in files[keep:]:
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    return removed


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
    def do_GET(self):
        try:
            u = urlparse(self.path)
            if u.path == "/":
                self._serve_index()
                return
            if not self._guard():
                return
            if u.path in self.WRITE_ENDPOINTS:
                self._send_json({"ok": False, "message": "该接口仅支持 POST"}, 405)
                return
            code, payload = self.api_get(u)
            self._send_json(payload, code)
        except (ConnectionError, BrokenPipeError, TimeoutError):
            # 客户端已断开：无处回送，直接收尾，别再去写 500（那会二次抛错）
            self.close_connection = True
        except Exception as e:  # noqa: BLE001
            self._send_json(err_payload(e), 500)

    def do_POST(self):
        try:
            u = urlparse(self.path)
            p = self._params()  # 先读净请求体，避免拒绝时客户端收到连接重置
            if not self._guard():
                return
            if self.TOKEN and self.headers.get("X-Switcher-Token") != self.TOKEN:
                self._send_json({"ok": False, "message": "缺少或错误的一次性访问令牌"}, 401)
                audit(self.AUDIT_DIR, self.SOURCE, u.path, "", False, "令牌校验失败")
                return
            code, payload = self.api_post(u, p)
            if self.AUDIT_DIR:
                target = str(p.get("name") or p.get("file") or "")
                audit(self.AUDIT_DIR, self.SOURCE,
                      self.AUDIT_ACTIONS.get(u.path, u.path),
                      target, bool(payload.get("ok")), payload.get("message"))
            self._send_json(payload, code)
        except (ConnectionError, BrokenPipeError, TimeoutError):
            self.close_connection = True
        except Exception as e:  # noqa: BLE001
            self._send_json(err_payload(e), 500)

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
