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
import csv
import datetime
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
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


def replace_with_retry(src, dst, tries=6, delay=0.15):
    """改名/移动，带重试。返回 None，失败抛最后一个 OSError。

    Windows 上**刚创建/刚写入的文件与目录**可能被杀软或索引器短暂持有句柄，
    此时 rename 会间歇性地抛 `WinError 5 拒绝访问` / `WinError 32 正在使用`。
    实测踩过两次：迁移连跑两次第二次偶发失败；切号的写后自校验也偶发 500
    （见 AUDIT_2026-09-19.md）。这类失败等几十毫秒就过去了，
    重试比让整个流程回滚划算得多。

    原先只在 account_migration 里有一份，切号那边没有 —— 现在提到这里共用。
    """
    last = None
    # ⚠️ `tries<=0` 时 range 为空 → 直接 `raise last`，而 last 是 None →
    #    抛的是 TypeError 而不是原始 OSError，把真正的失败原因吃掉。至少试一次。
    for i in range(max(1, int(tries))):
        try:
            Path(src).replace(dst)
            return
        except OSError as e:
            last = e
            time.sleep(delay * (i + 1))
    raise last


def new_token():
    """生成一次性访问令牌（仅本次进程有效，不落盘）。"""
    return secrets.token_urlsafe(24)


def jwt_ttl_days(token):
    """按 JWT 的 `exp - iat` 取**服务端签发的**有效天数；取不到返回 None。

    `token_source` 只是个标签，`exp - iat` 才是权威时长 —— 前者缺失时用它兜底：
      国服 account-e     token_source 为空，但 exp-iat = 55 天（与 oneid_login 一致）
      国际服 两个   token_source 为空，exp-iat = 362 / 364 天（约 1 年，国服那套
                   55/30 的二分法对它根本不适用）
    """
    seg = str(token or "").split(".")
    if len(seg) < 2:
        return None
    p = seg[1]
    p += "=" * (-len(p) % 4)
    try:
        pl = json.loads(base64.urlsafe_b64decode(p))
    except Exception:  # noqa: BLE001
        return None
    try:
        exp, iat = int(pl.get("exp")), int(pl.get("iat"))
    except (TypeError, ValueError):
        return None
    if exp <= iat:
        return None
    return int(round((exp - iat) / 86400.0))


def render_template(raw, ctx):
    """把 HTML 模板里的 {{KEY}} 替换成配置值（两个切换器共用同一份模板）。

    简单字符串替换即可，不上模板引擎 —— 只为了消灭两份几乎相同的前端文件。
    """
    text = raw if isinstance(raw, str) else raw.decode("utf-8")
    for key, val in (ctx or {}).items():
        text = text.replace("{{%s}}" % key, str(val))
    return text.encode("utf-8")


def token_source(token):
    """取 JWT payload 里的 token_source（决定有效期长短的签发通道）。

    ⚠️ **不是每个账号都有这个字段**。实测（2026-09-21）：国服 8 个账号里有 1 个
    （account-e）、国际服 2 个账号全部，payload 里都没有 `token_source` —— 只靠它判断
    「长期 55 天 / 短期 30 天」会让这些账号的标签直接消失。所以另有
    `jwt_ttl_days()` 用 `exp - iat` 兜底（那是服务端签发的权威时长，一定有）。
    """
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


def print_json(obj, stream=None):
    """CLI 的 JSON 输出统一出口 —— 兜住 GBK 控制台下的 UnicodeEncodeError。

    为什么必须有这一层：打包成 windowed exe 后（或从计划任务里调用 exe 时），
    `sys.stdout` 的编码是 **GBK** 而不是 UTF-8。此时
    `print(json.dumps(..., ensure_ascii=False))` 只要遇到 BMP 以外的字符就直接抛
    `UnicodeEncodeError`。2026-09-21 实测：本机 `wb_auth\\workbuddy-hlqin.info`
    的昵称是 `丹怡Helia 🌱`，`wb_ui_server.py --list` / `--current` /
    `--switch` / `--migrate-preview` 四条命令在 `PYTHONIOENCODING=gbk` 下全部
    以退出码 1 崩溃（而 `--channel wbai --list` 正常，只因国际服昵称不含 emoji）。

    这个坑在源码态**测不出来**：Git Bash 与 cmd 交互式的 stdout 都是 UTF-8，
    只有 exe / 计划任务才暴露。

    处理策略：先按原编码试写；失败就整体切到 UTF-8 重写一次。
    切 UTF-8 而不是 `errors="replace"`：宁可让终端编码不匹配，
    也不要静默把昵称换成 `?`，让用户以为账号名变了。
    """
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    out = stream if stream is not None else sys.stdout
    if out is None:            # pythonw / 无控制台：没有可写目标，直接放弃
        return
    try:
        out.write(text + "\n")
        out.flush()
        return
    except UnicodeEncodeError:
        pass
    except ValueError:         # 流已关闭
        return
    except OSError:
        return
    try:
        out.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        # 老版本 Python 或不可重配的流：退化成 ASCII 转义，至少不崩
        try:
            out.write(json.dumps(obj, ensure_ascii=True, indent=2) + "\n")
            out.flush()
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        out.write(text + "\n")
        out.flush()
    except Exception:  # noqa: BLE001
        pass


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

# 请求体上限。本服务的写请求都是小 JSON（切号 / 增删 / 续期参数），
# 1 MB 已经远远够用；设上限是为了不让一个畸形 Content-Length 把工作线程挂住。
MAX_BODY_BYTES = 1 << 20


class BadRequest(ValueError):
    """请求本身不合法（Content-Length 非法 / 请求体过大）→ 回 400 而不是 500。

    500 的语义是「服务端出错了」，而这类情况是**客户端发错了**，
    混在一起会让排障时误以为服务有 bug。
    """

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


def source_version(path, tag, extra_paths=()):
    """用源文件 mtime 生成构建戳。

    用途只有一个：让第二个实例能看出"那个在跑的实例是不是旧代码"
    —— 本会话踩过：8765 上跑着 02:16 启动的旧实例，一直用旧模板服务，
    让人以为"改版没生效"。git 在打包态未必可用，mtime 最稳。

    `extra_paths`：**一起参与取最大 mtime** 的兄弟文件。典型是启动器
    （`ui_app.py`）—— 它们不 import 本模块的常量，但改动同样
    影响行为。不传的话，"只改了启动器"时版本戳不变，`stale` 检测就失效
    （实测：给启动器加一次性令牌，重启后 version 仍是旧值，只能靠行为验证 ——
     见 AUDIT_2026-09-24.md 第二十章）。

    打包态下 `__file__` 指向 PyInstaller 的临时解包目录（每次启动路径都不同、
    且不保证能 stat），此时所有候选都 stat 不到 → 回退到 `sys.executable`
    （= exe 本身），否则 exe 的版本戳会退化成裸 tag，就失去了比对意义。
    """
    import sys as _sys
    import time as _t
    best = None
    for cand in (path,) + tuple(extra_paths):
        if not cand:
            continue
        try:
            mt = Path(cand).stat().st_mtime
        except OSError:
            continue
        if best is None or mt > best:
            best = mt
    if best is None:
        # 候选全都 stat 不到（打包态）→ 退到 exe 自身
        exe = getattr(_sys, "executable", None)
        if exe:
            try:
                best = Path(exe).stat().st_mtime
            except OSError:
                best = None
    if best is None:
        return tag
    return "%s-%s" % (tag, _t.strftime("%Y%m%d-%H%M%S", _t.localtime(best)))


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


def _list_processes_native():
    """用 `CreateToolhelp32Snapshot` 原生枚举进程；失败返回 None。

    为什么不用 `tasklist`：**它要起一个子进程，实测 0.30 秒**（本机 95 个进程），
    而原生 API 是 **~2 毫秒**，快 150 倍。`list_processes()` 是 `client_status()` /
    `live_token.client_pids()` / 迁移前置检查等的必经之路，一次页面加载能调好几次，
    这 0.3 秒是纯浪费。
    附带好处：`Process32NextW` 给的是**真 Unicode 名**，不再有 tasklist 那个
    「GBK 输出按 UTF-8 解码 → 非 ASCII 进程名变乱码键」的坑。
    """
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD),
                        ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t),   # ULONG_PTR
                        ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]

        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == INVALID_HANDLE_VALUE:
            return None
        out = {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                name = (entry.szExeFile or "").lower()
                if name:
                    out.setdefault(name, []).append(int(entry.th32ProcessID))
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
        return out or None
    except Exception:  # noqa: BLE001
        return None


def list_processes():
    """枚举当前进程，返回 {小写进程名: [pid, ...]}。

    **检测不出来返回 None**（≠ 没有进程）—— 调用方必须区分这两者，
    否则"枚举失败"会被当成"程序没在跑"（本会话踩过：tasklist 的 GBK 输出
    让 UTF-8 解码在读取线程里炸掉，stdout 变空，于是 12 个客户端进程被当成 0 个）。

    主路径是原生 `CreateToolhelp32Snapshot`（~2ms）；`tasklist` 只作兜底
    （0.30s，且非 ASCII 名会乱码）—— 两条路都失败才返回 None。
    """
    if os.name != "nt":
        return None
    native = _list_processes_native()
    if native:
        return native
    try:
        # 不能用 text=True：tasklist 在中文 Windows 上输出 GBK，而本机 Python 处于
        # UTF-8 模式，解码会在**读取线程里**抛 UnicodeDecodeError（异常不冒到调用方）。
        # 我们只要 ASCII 的进程名，errors="replace" 就够。
        r = subprocess.run(["tasklist", "/NH", "/FO", "CSV"], capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            return None
        text = (r.stdout or b"").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    out = {}
    # ⚠️ 用 csv.reader 而不是裸 split(",")：进程名理论上可以含逗号，tasklist 会用
    #    引号把它包起来，裸 split 会把这一行拆错、于是整条记录被丢掉
    #    （见 AUDIT_2026-09-24.md P3-5）。csv.reader 会自动去掉外层引号。
    for row in csv.reader(text.splitlines()):
        parts = [c.strip() for c in row]
        if len(parts) < 2 or not parts[0]:
            continue
        name = parts[0].lower()
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        out.setdefault(name, []).append(pid)
    return out


def list_process_names():
    """只要进程名集合；检测不出来返回 None。"""
    procs = list_processes()
    return None if procs is None else set(procs)


def process_image_path(pid):
    """取某个 pid 的可执行文件完整路径；取不到返回空串。"""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                   wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return ""


def _win_placement_struct():
    """`WINDOWPLACEMENT`（ctypes.wintypes 里没有，只能自己定义）。"""
    import ctypes
    from ctypes import wintypes
    if os.name != "nt":
        return None

    class _WP(ctypes.Structure):
        _fields_ = [("length", ctypes.c_uint), ("flags", ctypes.c_uint),
                    ("showCmd", ctypes.c_uint), ("ptMinPosition", wintypes.POINT),
                    ("ptMaxPosition", wintypes.POINT),
                    ("rcNormalPosition", wintypes.RECT)]
    return _WP


_WINDOWPLACEMENT = _win_placement_struct() or type("_WP", (), {})  # 占位，非 Windows 不用


# Electron 主窗口的类名。`Chrome_WidgetWin_0` / `IME` /
# `Electron_NotifyIconHostWindow` / `crashpad_SessionEndWatcher` 等都是 helper，
# 尺寸常常是 0x0 —— 拿它们去 ShowWindow 会显示出一个空的隐形窗口，
# 用户观感就是"点了还是没反应"，所以主窗口必须按类名 + 面积认。
ELECTRON_MAIN_CLASS = "Chrome_WidgetWin_1"
_MIN_MAIN_W, _MIN_MAIN_H = 200, 100

SW_SHOWNORMAL, SW_SHOWMAXIMIZED = 1, 3

# activate_windows_of 用到的常量（抢前台 / SetWindowPos）
ASFW_ANY = 0xFFFFFFFF          # AllowSetForegroundWindow：允许任意进程抢前台
VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x2
SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0040


def client_main_windows(pids, include_hidden=True):
    """挑出属于这些 pid 的 **客户端主窗口**，按可用性排序返回。

    ⚠️ 2026-09-22：客户端「关闭到托盘」时，主窗口是 **隐藏** 的
    （`IsWindowVisible=0`，但 `WINDOWPLACEMENT.showCmd` 仍是最大化）。
    只按"可见"过滤会一条都选不出来 —— 本工具的「打开客户端」于是回
    「系统不允许切到前台，请从任务栏点开」，用户点托盘图标也出不来。

    返回 `[{hwnd, cls, title, visible, maximized, area}]`，顺序为：
      1) 可见的主窗口（正常情况）  2) 隐藏的主窗口（收在托盘里）
      3) 可见的其它窗口（兜底，按面积降序）
    `include_hidden=False` 时只保留第 1 类。
    非 Windows / ctypes 不可用 → 返回 []（不抛异常）。
    """
    if os.name != "nt" or not pids:
        return []
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u32.IsWindowVisible.argtypes = [wintypes.HWND]
        u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(_WINDOWPLACEMENT)]
        want = set(int(p) for p in pids)
        rows = []
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in want:
                return True
            cls = ctypes.create_unicode_buffer(256)
            u32.GetClassNameW(hwnd, cls, 256)
            n = u32.GetWindowTextLengthW(hwnd)
            title = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, title, n + 1)
            r = wintypes.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(r))
            wp = _WINDOWPLACEMENT()
            wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
            u32.GetWindowPlacement(hwnd, ctypes.byref(wp))
            rows.append({
                "hwnd": hwnd, "cls": cls.value, "title": title.value,
                "visible": bool(u32.IsWindowVisible(hwnd)),
                "maximized": wp.showCmd == SW_SHOWMAXIMIZED,
                "area": max(0, r.right - r.left) * max(0, r.bottom - r.top),
                "w": max(0, r.right - r.left), "h": max(0, r.bottom - r.top),
            })
            return True

        u32.EnumWindows(proc(_cb), 0)

        def _is_main(x):
            return x["cls"] == ELECTRON_MAIN_CLASS and \
                x["w"] >= _MIN_MAIN_W and x["h"] >= _MIN_MAIN_H

        mains = [x for x in rows if _is_main(x)]
        vis_main = sorted([x for x in mains if x["visible"]],
                          key=lambda x: -x["area"])
        hid_main = sorted([x for x in mains if not x["visible"]],
                          key=lambda x: -x["area"])
        others = sorted([x for x in rows if x["visible"] and not _is_main(x)],
                        key=lambda x: -x["area"])
        out = vis_main + (hid_main if include_hidden else []) + others
        return out
    except Exception:  # noqa: BLE001
        return []


def focus_windows_of(pids, sw_restore=9):
    """把指定 pid 的主窗口切到前台；**窗口被收进托盘时先把它唤出来**。

    返回 True = 窗口现在**看得见**了（置前可能被 Windows 前台锁定拒绝，
    但那不影响"看得见"，所以不算失败）。一个窗口都找不到才返回 False。

    ⚠️ 早期版本只收 `IsWindowVisible` 为真的窗口，对"关到托盘"的客户端
    恒返回 False —— 表现就是点了「打开客户端」没反应。
    """
    if os.name != "nt" or not pids:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.SetForegroundWindow.argtypes = [wintypes.HWND]
        u32.GetForegroundWindow.restype = wintypes.HWND
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        k32.GetCurrentThreadId.restype = wintypes.DWORD

        cands = client_main_windows(pids)
        if not cands:
            return False
        win = cands[0]
        # 隐藏的窗口要先 ShowWindow 现身；用 showCmd 对应的命令，
        # 免得把"最大化后收进托盘"的窗口给还原成小窗。
        cmd = (SW_SHOWMAXIMIZED if win["maximized"] else SW_SHOWNORMAL) \
            if not win["visible"] else sw_restore
        u32.ShowWindow(win["hwnd"], cmd)

        # 前台切换：Windows 有前台锁定，尽了力仍可能被拒（返回 0）。
        # 用 AttachThreadInput 解除"别的线程持有前台"这一常见拒绝原因。
        fg = u32.GetForegroundWindow()
        tid_cur = k32.GetCurrentThreadId()
        tid_fg = u32.GetWindowThreadProcessId(fg, 0) if fg else 0
        attached = bool(tid_fg and tid_fg != tid_cur and
                        u32.AttachThreadInput(tid_cur, tid_fg, True))
        try:
            ok = bool(u32.SetForegroundWindow(win["hwnd"]))
        finally:
            if attached:
                u32.AttachThreadInput(tid_cur, tid_fg, False)
        # 只要窗口现身就算成功 —— 用户要的是"能看见"
        return bool(u32.IsWindowVisible(win["hwnd"])) or ok
    except Exception:  # noqa: BLE001
        return False


def activate_windows_of(pids, sw_restore=9):
    """唤出客户端主窗口，并**尽量确认它真的被激活**。返回 `(found, fg_ok, hwnd)`。

    - `found` —— 找到了主窗口（含"收在托盘里、已被唤出来"的情况）
    - `fg_ok` —— `True`=确认已拿到前台；`False`=明确没拿到；
                 `None`=读不到前台状态（非交互桌面 / 权限受限），**无法判定**
    - `hwnd`  —— 主窗口句柄（`found=False` 时是 `None`）

    ⚠️ 与 `focus_windows_of` 的分工：那个把「窗口看得见」当成功（调用方要的是"能看见"），
    本函数额外确认「拿到前台」—— 因为 Electron 客户端被**外部** `ShowWindow` 从托盘
    唤出后，窗口可能看得见却收不到输入（Chromium 内部仍认为窗口隐藏/被遮挡，输入事件
    进不了渲染进程），表现正是"内容正常但完全点不动"。

    抢前台顺序：`SetForegroundWindow` → 失败则模拟一次 Alt 键（Windows 认可的
    "用户正在操作"信号，用来解除前台锁）再重试，最多两轮。
    """
    if os.name != "nt" or not pids:
        return False, None, None
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.SetForegroundWindow.argtypes = [wintypes.HWND]
        u32.SetForegroundWindow.restype = wintypes.BOOL
        u32.GetForegroundWindow.restype = wintypes.HWND
        u32.BringWindowToTop.argtypes = [wintypes.HWND]
        u32.SetActiveWindow.argtypes = [wintypes.HWND]
        u32.SetFocus.argtypes = [wintypes.HWND]
        u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     wintypes.UINT]
        u32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
        u32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, wintypes.DWORD,
                                    ctypes.c_void_p]

        cands = client_main_windows(pids)
        if not cands:
            return False, None, None
        win = cands[0]
        hwnd = win["hwnd"]
        # 隐藏的窗口要先 ShowWindow 现身；用 showCmd 对应的命令，
        # 免得把"最大化后收进托盘"的窗口给还原成小窗。
        cmd = (SW_SHOWMAXIMIZED if win["maximized"] else SW_SHOWNORMAL) \
            if not win["visible"] else sw_restore
        u32.ShowWindow(hwnd, cmd)
        u32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                         SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW)

        u32.AllowSetForegroundWindow(ASFW_ANY)
        ok = bool(u32.SetForegroundWindow(hwnd))
        fg = u32.GetForegroundWindow()
        tries = 0
        while fg != hwnd and tries < 2:
            # 前台锁：先模拟一次 Alt 按下/抬起，系统才认可这次抢前台
            u32.keybd_event(VK_MENU, 0, 0, None)
            time.sleep(0.03)
            u32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, None)
            time.sleep(0.03)
            ok = bool(u32.SetForegroundWindow(hwnd))
            u32.BringWindowToTop(hwnd)
            u32.SetActiveWindow(hwnd)
            u32.SetFocus(hwnd)
            time.sleep(0.15)
            fg = u32.GetForegroundWindow()
            tries += 1

        if fg == hwnd:
            fg_ok = True
        elif fg == 0:
            fg_ok = None      # 读不到前台状态 → 不判定为失败（避免误触发重启）
        else:
            fg_ok = False
        return (bool(u32.IsWindowVisible(hwnd)) or ok), fg_ok, hwnd
    except Exception:  # noqa: BLE001
        return False, None, None


def close_windows_of(pids, wm_close=0x0010):
    """给指定 pid 的**主窗口**发 WM_CLOSE（礼貌关闭），返回发出的条数。

    包括被收进托盘的隐藏窗口 —— 以前只发给可见窗口，结果客户端藏在托盘时
    一条都发不出去，`close_client` 只能白白等满 8 秒再强杀。
    优先用它而不是直接强杀：客户端能走正常退出流程，不会丢未落盘的状态。
    """
    if os.name != "nt" or not pids:
        return 0
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM]
        cands = client_main_windows(pids)
        if not cands:
            return 0
        mains = [w for w in cands if w["cls"] == ELECTRON_MAIN_CLASS]
        # 万一直的不是 Electron（没有 Chrome_WidgetWin_1），退回"发给可见窗口"的老行为，
        # 免得一条 WM_CLOSE 都发不出去、白等 8 秒再强杀。
        targets = mains or [w for w in cands if w["visible"]]
        sent = 0
        for w in targets:
            if u32.PostMessageW(w["hwnd"], wm_close, 0, 0):
                sent += 1
        return sent
    except Exception:  # noqa: BLE001
        return 0


def terminate_processes(pids):
    """强杀指定 pid（礼貌关闭超时后的兜底）。返回成功数。"""
    if os.name != "nt" or not pids:
        return 0
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        PROCESS_TERMINATE = 0x0001
        n = 0
        for pid in pids:
            h = k32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
            if not h:
                continue
            try:
                if k32.TerminateProcess(h, 1):
                    n += 1
            finally:
                k32.CloseHandle(h)
        return n
    except Exception:  # noqa: BLE001
        return 0


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


def probe_instance_range(port, tries=10, timeout=0.3, host="127.0.0.1",
                         extra_ports=(), exclude_pid=None):
    """在候选端口里找我们的实例（第一个实例可能因端口占用顺延过）。

    候选 = `[port, port+tries)` ∪ 每个 `extra_ports` 各自的 `tries` 个端口（去重）。
    `extra_ports` 用于「请求端口区间 ∪ 默认端口区间」这种多起点场景 —— 互斥体是
    **按工具全局**的、与端口无关，只探请求区间会在「已有实例在默认端口、这次又显式
    传了别的 --port」时误报。

    `exclude_pid`：跳过指定 pid 的实例（单实例守卫要排除**自己**）。

    命中优先级：`port` > `extra_ports` 顺序 > 端口最小者，保证结果确定。

    ⚠️ **必须并发探**。串行探 10 个空端口时，每个都要等满超时（实测本机 0.42s/个 ——
    那些端口上没人应答，连接一直挂着而不是立刻 RST），单区间 4.1s，
    两个区间加起来启动要 **8.3 秒**，`.cmd` 会像卡死。
    并发之后总耗时 ≈ 单次超时（所以超时必须短：真实实例 20ms 内就应答）。

    本函数是**唯一**的端口区间探测实现 —— `single_instance_guard._probe_all` 直接
    复用它（见 AUDIT_2026-09-24.md P3-3）。
    """
    starts = list(dict.fromkeys([int(port)] + [int(p) for p in extra_ports]))
    cands = []
    for s in starts:
        cands.extend(range(s, s + int(tries)))
    cands = list(dict.fromkeys(cands))
    if not cands:
        return None

    def _keep(info):
        if not info:
            return False
        if exclude_pid is not None and str(info.get("pid")) == str(exclude_pid):
            return False
        return True

    found = {}
    if len(cands) > 1:
        try:
            import concurrent.futures as cf
            with cf.ThreadPoolExecutor(max_workers=len(cands)) as ex:
                futs = {ex.submit(probe_instance, p, timeout, host): p for p in cands}
                for fut in cf.as_completed(futs):
                    try:
                        info = fut.result()
                    except Exception:  # noqa: BLE001
                        info = None
                    if _keep(info):
                        found[futs[fut]] = info
        except Exception:  # noqa: BLE001  并发不可用时退回串行，别因此起不来
            for p in cands:
                info = probe_instance(p, timeout=timeout, host=host)
                if _keep(info):
                    found[p] = info
    else:
        info = probe_instance(cands[0], timeout=timeout, host=host)
        if _keep(info):
            found[cands[0]] = info

    if not found:
        return None
    for p in starts:                  # 起点顺序优先（请求端口 > 默认端口）
        if p in found:
            return found[p]
    return found[min(found)]          # 否则端口最小者，保证结果确定


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
    def _probe_all(timeout=0.2):
        """找我们的实例：**一次并发扫完所有候选端口**。

        候选 = 请求端口区间 ∪ 默认端口区间。默认区间是必要的 —— 互斥体是
        **按工具全局**的、与端口无关，只探请求区间会在"已有实例在默认端口、
        这次显式传了别的 --port"时误报 abort（实测踩过）。

        ⚠️ 两点都是实测踩出来的：
        - **必须并发**：本机连一个没人监听的回环端口**不会立刻 RST，会一直挂到超时**
          （裸 socket 也要 2 秒），串行 10 个端口就是 10 倍（曾达 8.3s）。
        - **超时必须短**（0.2s）：探测总耗时 ≈ 超时值；真实实例 20ms 内就应答，
          0.2s 有 10 倍余量。
        早先的写法是"先串行探两个优先端口、再串行扫两个区间"，冷启动要 0.86s ——
        优先端口没命中时那 0.4s 是白花的。合成一次并发后只剩 0.2s。

        实现已下沉到 `probe_instance_range`（全仓**唯一**一份端口区间探测），
        这里只负责传参 —— 原先两边各写一遍线程池 + 串行兜底 + 最小端口
        （见 AUDIT_2026-09-24.md P3-3）。
        """
        return probe_instance_range(
            port, tries=tries, timeout=timeout,
            extra_ports=[int(default_port)] if default_port else (),
            exclude_pid=os.getpid())

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
        info = _probe_all(timeout=0.2)
        if info:
            return handle, "reuse", info
        if time.time() >= deadline:
            break
        time.sleep(0.25)
    return handle, "abort", None


# ---------------------------------------------------------------------------
# 页面存活状态：避免"每次运行都再开一个指向同一地址的标签页"
# ---------------------------------------------------------------------------
# 旧行为：复用分支无条件 webbrowser.open(url) —— 每双击一次 .cmd 就多一个标签页。
# 但也不能干脆不开：用户可能已经把标签页关了，这时双击就该给他打开。
# 所以要能回答"现在有没有页面开着"：
#   - 首页被请求（_serve_index）→ 记一次"刚开过"
#   - 页面每 5 秒打 /api/page-alive → 持续刷新"还开着"
#   - 标签页被关掉 → 心跳停 → 超过 PAGE_TTL 就认为没有页面了
_PAGE_STATE = {"seen": 0.0, "opened": 0.0}
PAGE_TTL = 20.0          # 心跳多久没来就算页面已关闭
PAGE_OPEN_GRACE = 30.0   # 刚调过浏览器后的宽限期（页面可能还没加载完、还没发心跳）


def mark_page_opened():
    _PAGE_STATE["opened"] = time.time()


def mark_page_seen():
    _PAGE_STATE["seen"] = time.time()


def page_is_open():
    """是否（很可能）已经有一个页面开着。

    两个来源要**分开判**，不能取 max 后比 max(时限)：
      - 心跳（seen）：页面还在，按 PAGE_TTL 算
      - 刚调过浏览器（opened）：页面可能还没加载完、还没发第一次心跳，按更宽的
        PAGE_OPEN_GRACE 算
    早先写成 `(now - max(seen, opened)) < max(PAGE_TTL, PAGE_OPEN_GRACE)`，
    等于让宽限期覆盖了心跳时限，标签页关掉后要多等 10 秒才认账（自检抓到）。
    """
    now = time.time()
    if _PAGE_STATE["seen"] and (now - _PAGE_STATE["seen"]) < PAGE_TTL:
        return True
    if _PAGE_STATE["opened"] and (now - _PAGE_STATE["opened"]) < PAGE_OPEN_GRACE:
        return True
    return False


def page_state():
    """给 /api/page-alive 与调试用。"""
    now = time.time()
    return {
        "open": page_is_open(),
        "last_seen_ago": round(now - _PAGE_STATE["seen"], 1) if _PAGE_STATE["seen"] else None,
        "last_opened_ago": round(now - _PAGE_STATE["opened"], 1) if _PAGE_STATE["opened"] else None,
    }


def open_page(url, log=print):
    """只在**没有页面开着**时才打开浏览器。返回是否真的打开了。

    这是"重复打开多个页面"的根治点：把开浏览器这件事从"每次运行都做"
    改成"只在确实没有页面时才做"。
    """
    if page_is_open():
        log("       已有页面在运行，不再新开标签页；若那个标签页已关闭，"
            "请手动打开：%s" % url)
        return False
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        return False
    # ⚠️ mark 必须在**确认打开没抛异常之后**：原先先 mark 再 open，一旦 open 失败，
    #    30 秒宽限期照样被占住，这段时间内的调用会被误判成「已有页面在运行」而不开标签页
    #    （见 AUDIT_2026-09-24.md P3-4）。
    mark_page_opened()
    log("       已打开页面：%s" % url)
    return True


def notify_user(title, message, log=print):
    """给用户一个**可见**的提示。

    窗口版（console=False）没有控制台，print 没有任何去处 —— 复用分支若只打印，
    用户会以为"双击没反应"。这里优先弹一个原生消息框。
    """
    log("%s：%s" % (title, message))
    if os.name != "nt":
        return
    try:
        import ctypes
        # MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x40 | 0x10000 | 0x40000)
    except Exception:  # noqa: BLE001
        pass


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
        # 关键：由**运行中的实例**告诉我们页面是否开着（本进程的 _PAGE_STATE 是空的，
        # 问本地等于没问）。只有它说"没有页面"时才开，避免越点越多标签页。
        if info.get("page_open"):
            print("       已有页面在运行，不再新开标签页；若那个标签页已关闭，"
                  "请手动打开：%s" % url, flush=True)
        else:
            open_page(url)
    return 0


def report_abort(port, tries=PORT_TRIES):
    """有实例占着互斥体，却在其端口区间探测不到它 —— 异常状态，明确报错。返回 1。"""
    print("[错误] 已有切换器实例在运行，但在 %d~%d 端口上都探测不到它。"
          % (port, int(port) + int(tries) - 1), flush=True)
    print("       可能原因：它启动后卡死；或它的端口被别的程序抢走了。", flush=True)
    print("       请先结束已有的切换器进程（任务管理器里找 python.exe / AgentAccountSwitcher.exe）再重试。", flush=True)
    return 1


class BaseHandler(BaseHTTPRequestHandler):
    """HTTP 骨架。子类只需提供 INDEX_FILE / WRITE_ENDPOINTS 与 api_get / api_post。"""

    INDEX_FILE = None        # 直接指定前端页面路径（可选）
    INDEX_NAME = None        # 或在 BASE_DIR / _internal 下按文件名查找
    BASE_DIR = None          # 查找根目录（打包版会被启动器改成 exe 所在目录）
    UI_CONTEXT = None        # 模板变量（两个切换器共用 ui_template.html）
    # 除首页外的额外页面：路径 -> (文件名, 该页的模板变量 dict)。
    # 用途是「一个进程对外提供多个视图」：入口页 / 国服视图 /wb / 国际服视图 /wbai
    # 共用同一套令牌注入与页面心跳逻辑，不必各写一份 Handler。
    PAGES = {}
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
        """解析请求参数：query string 与 JSON body 合并（body 优先）。

        ⚠️ Content-Length 必须**先校验再读**，三种坏值各有后果：
          - 非数字 → `int()` 抛 ValueError → 500（其实是客户端发错了，该回 400）；
          - 负数  → `self.rfile.read(-5)` 会**一直读到连接关闭**，把工作线程挂住；
          - 超大  → 无上限地读进内存。
        本机页面不会这么发，但任何能连到 87xx 的本地进程都可以。
        """
        data = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return data
        try:
            length = int(raw_len)
        except (TypeError, ValueError):
            raise BadRequest("Content-Length 非法：%r" % (raw_len,))
        if length < 0:
            raise BadRequest("Content-Length 不能为负：%d" % length)
        if length > MAX_BODY_BYTES:
            raise BadRequest("请求体过大（%d 字节，上限 %d 字节）" % (length, MAX_BODY_BYTES))
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
    def audit_source(self, path=""):
        """审计日志里的来源标记。默认固定 SOURCE；多通道的 Handler 会按路径改写，
        这样 [wb] 与 [wbai] 的动作在同一份日志里能分辨出来。"""
        return self.SOURCE

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
        elif isinstance(exc, BadRequest):
            # 客户端发错了（Content-Length 非法 / 请求体过大）→ 400，不是 500
            code = 400
            message = str(exc)
        else:
            code = 500
            message = err_payload(exc)["message"]
        payload = {"ok": False, "message": message, "error": type(exc).__name__}
        if self.AUDIT_DIR and path:
            # 用 audit_source(path) 而不是 self.SOURCE：后者会把 /api/wbai/* 的出错
            # 记成 [wb]。do_POST 的成功路径与令牌失败路径都用的 audit_source，这里原先漏了
            # （见 AUDIT_2026-09-24.md P3-1）。
            audit(self.AUDIT_DIR, self.audit_source(path),
                  self.AUDIT_ACTIONS.get(path, path), target, False, message)
        try:
            self._send_json(payload, code)
        except (ConnectionError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    def do_GET(self):
        path = ""
        try:
            u = urlparse(self.path)
            path = u.path
            if u.path == "/api/ping":
                # 身份端点：让第二个实例能判断"这个端口上是不是我们自己"。
                # 放在 _guard() 之前 —— 探测方只带 Host，不需要令牌，也不该被同源策略挡。
                self._send_json({
                    "ok": True, "app": self.APP_NAME, "source": self.SOURCE,
                    "pid": os.getpid(), "port": self.server.server_address[1],
                    "version": self.APP_VERSION, "started_at": self.STARTED_AT,
                    # 页面是否开着必须由**运行中的实例**回答：判断这件事的进程是
                    # 新起的那个，它自己的 _PAGE_STATE 是空的，问本地等于没问。
                    "page_open": page_is_open(),
                }, 200)
                return
            if u.path == "/api/page-alive":
                # 页面心跳：前端每 5 秒打一次。服务端据此知道"还有页面开着"，
                # 从而在下次运行时**不再重复打开标签页**。同样放在 _guard() 之前。
                mark_page_seen()
                st = page_state()
                self._send_json({"ok": True, "page_open": st["open"]}, 200)
                return
            if not self._guard():
                return
            # ⚠️ 页面路由必须在 `_guard()` **之后**：`_serve_page` 会往页面里注入
            #    `window.SWITCHER_TOKEN`（见下面 _serve_page），而 `_guard()` 含 Host 回环
            #    校验。放在守卫之前等于「任何 Host 都能拿到带令牌的页面」，与 README
            #    声明的「Host / Origin 都必须回环」不符。
            #    （不构成直接可利用的漏洞：do_POST 的 Host+Origin 校验与令牌校验是
            #    串联的两道，DNS rebinding 读到令牌也过不了 Host 校验。但这是纵深防御
            #    该补齐的一环 —— 见 AUDIT_2026-09-24.md P2-2。）
            #    只有 `/api/ping` 与 `/api/page-alive` 是**有意**豁免的（单实例探测）。
            if u.path == "/":
                self._serve_index()
                return
            if u.path in self.PAGES:
                name, ctx = self.PAGES[u.path]
                self._serve_page(name, ctx)
                return
            if u.path in self.WRITE_ENDPOINTS:
                self._send_json({"ok": False, "message": "该接口仅支持 POST"}, 405)
                return
            code, payload = self.api_get(u)
            self._send_json(payload, code)
        except KeyboardInterrupt:
            raise
        except BaseException as e:  # noqa: BLE001  含 SystemExit（见 _handle_request_error）
            # 必须把 path 传进去：`_handle_request_error` 里 `if self.AUDIT_DIR and path`
            # 就是审计的开关，不传 → GET 侧异常**零审计**（do_POST 一直传，这里原先漏了，
            # 见 AUDIT_2026-09-24.md P3-2）。
            self._handle_request_error(e, path)

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
                # 用映射后的动作名，别直接写 u.path —— 否则日志里出现的是
                # [wb] /api/switch 而不是 [wb] switch，按动作统计会分成两组
                audit(self.AUDIT_DIR, self.audit_source(u.path),
                      self.AUDIT_ACTIONS.get(u.path, u.path), "", False, "令牌校验失败")
                return
            code, payload = self.api_post(u, p)
            if self.AUDIT_DIR:
                audit(self.AUDIT_DIR, self.audit_source(u.path),
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
        return self._page_path(self.INDEX_NAME or "index.html")

    def _page_path(self, name):
        if self.INDEX_FILE and name == (self.INDEX_NAME or "index.html"):
            return Path(self.INDEX_FILE)
        return resolve_data(self.BASE_DIR or Path(__file__).resolve().parent, name)

    def _serve_index(self):
        self._serve_page(self.INDEX_NAME or "index.html", self.UI_CONTEXT)

    def _serve_page(self, name, ctx=None):
        """渲染并返回一个模板页。

        首页与 PAGES 里的额外页走同一条路径 —— 页面心跳、模板渲染、一次性令牌注入
        只写一份，避免"某个视图拿不到令牌于是所有写操作 401"这类只在子页面出现的坑。
        """
        idx = self._page_path(name)
        if not idx or not idx.is_file():
            self._send(b"page not found", 404, "text/plain; charset=utf-8")
            return
        # 页面被请求 = 刚刚有页面打开（可能是我们开的，也可能是用户手动开的）
        mark_page_seen()
        body = idx.read_bytes()
        if ctx:
            body = render_template(body, ctx)
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
