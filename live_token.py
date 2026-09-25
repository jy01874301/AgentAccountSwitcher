"""live_token.py —— 从客户端进程内存里读取**当前有效的**登录凭据。

## 为什么需要它

WorkBuddy 5.6.2 起启用了「静态字段保护」（ProtectedJsonFields）：登录态文件
`%LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info`
里的 `accessToken` / `refreshToken` / `nickname` 等被替换成

    {"$wbEncrypted": 1, "envelope": "<base64>"}

envelope 是 AES-256-GCM 密文，密钥（at-rest key）由客户端运行时注入 ——
asar 里只有算法、没有密钥载荷，`~/.workbuddy/keyblob` 的 `wrapped` 本身又是
同格式信封（解它需要同一把钥匙），所以**本地无法解密**。

但客户端自己要用明文 token 调接口，**进程内存里必然躺着明文 JWT**。
本模块就是去那儿取：遍历该通道客户端进程的可读内存，捞出 JWT，
按 payload 特征区分 access / refresh，再用 uid 与预期账号比对。

## 实测依据（2026-09-23，国服「account-e」）

| | 账号库明文 | 内存捞到的 |
|---|---|---|
| accessToken | len=1323, typ=Bearer, aud=account, azp=console | **完全一致** |
| refreshToken | len=700, typ=Offline, aud=<realm> | **完全一致** |

单进程扫描约 1 秒（600MB 预算）。捞到的 accessToken 打
`https://copilot.tencent.com/console/account` 返回 HTTP 200。

## 边界

- **只读**：`OpenProcess(PROCESS_VM_READ)` + `ReadProcessMemory`，不写内存。
- 客户端没在跑、或它以更高权限运行（本进程读不到）→ 返回 None，由调用方降级。
- token 只活在返回的 dict 里，不落盘。
- 只支持 64 位 Windows（PEB / RTL_USER_PROCESS_PARAMETERS 偏移按 x64 写死）。
"""

import base64
import ctypes
import ctypes.wintypes as wt
import datetime
import json
import re
import threading
import time

import switcher_common as common

# --- Win32 ---------------------------------------------------------------
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.VirtualQueryEx.restype = ctypes.c_size_t

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01

# MEMORY_BASIC_INFORMATION.Type：区域是「映射的文件镜像（DLL/EXE 代码段）」、
# 「文件/共享内存映射」还是「私有提交（堆/栈）」。
MEM_IMAGE = 0x1000000
MEM_MAPPED = 0x40000
MEM_PRIVATE = 0x20000
# 可读页：READONLY / READWRITE / WRITECOPY / EXECUTE_READ / EXECUTE_READWRITE / EXECUTE_WRITECOPY
READABLE = 0x02 | 0x04 | 0x08 | 0x20 | 0x40 | 0x80
MAX_USER_ADDR = 0x7FFFFFFFFFFF


class _MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD), ("PartitionId", wt.WORD),
                ("RegionSize", ctypes.c_size_t), ("State", wt.DWORD),
                ("Protect", wt.DWORD), ("Type", wt.DWORD)]


# JWT：三段 base64url。长度下限过滤掉内存里的碎片。
_JWT_RE = re.compile(rb"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")
# 只认「像凭据」的：accessToken 1323 / refreshToken 700 上下，留宽裕余量
_MIN_JWT_LEN = 400
_MAX_JWT_LEN = 8000

DEFAULT_BUDGET = 600 * 1024 * 1024     # 单进程最多读多少字节
CACHE_TTL_SECONDS = 60                 # 进程内缓存：current_account 会被频繁调用

# 缓存按通道分区（与 _CREDITS_CACHE 同一个理由：不分区的模块级缓存会让
# 国际服拿到国服那份结果）。
# 值 = {"ts": float, "sessions": {uid: session}, "best_uid": uid|None}
#   `sessions` —— 上次扫描时内存里**每个** uid 的会话（一次扫描全建好）
#   `best_uid` —— 其中最活跃的那个（exp 最晚）
# 按 uid 存是关键：`current_account()` 会为桌面目录里**每个**加密文件各调一次
# （正式登录态 + 切号备份），备份的 uid 不是当前登录账号，缺失即负命中、
# 直接返回 None，不必重扫 —— 以前这里每次请求都白扫 3.5 秒。
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _decode_payload(token):
    """解 JWT payload（**不验签**，只读字段）。"""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _classify(token):
    """按 payload 特征判定是 accessToken 还是 refreshToken；都不是返回 None。

    判据来自实测的账号库明文结构：
      accessToken  typ=Bearer,  aud=account
      refreshToken typ=Offline, aud=<realm url>
    只认 typ 最稳（aud 在换网关时会变），typ 缺失时退回 aud 判断。
    """
    payload = _decode_payload(token)
    if not payload:
        return None
    typ = str(payload.get("typ") or "")
    aud = payload.get("aud")
    if typ == "Bearer" or aud == "account":
        return "access", payload
    if typ == "Offline" or (isinstance(aud, str) and "/realms/" in aud):
        return "refresh", payload
    return None


def _expiry(payload):
    exp = payload.get("exp")
    if isinstance(exp, (int, float)) and exp > 0:
        try:
            return datetime.datetime.fromtimestamp(exp, datetime.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def client_pids(ch):
    """该通道客户端正在运行的 pid 列表；**枚举失败返回 None**（≠ 空列表）。

    ⚠️ 三态语义，调用方必须分开处理：
      - `None` = 枚举失败（`list_processes()` 返回 None）—— 说明「不知道」，
        不能据此断定客户端没在跑，更**不能写负缓存**；
      - `[]`   = 枚举成功且确实没有该通道的客户端进程；
      - `[pid…]` = 正常。
    以前这里把 `if not table` 和「没进程」合并成 `return []`，与本函数的
    docstring 自相矛盾 —— 偶发枚举失败会让 60 秒内所有请求误报「桌面端未登录」。

    ⚠️ **进程枚举统一走 `common.list_processes()`，别在这里再实现一份**：
    它已经处理好了「枚举失败返回 None」与「真的没有进程」的区分。
    （附带记录：`list_processes()` 主路径已是原生 Toolhelp32；tasklist 只在兜底路径上
    跑，那条路径才把 GBK 输出按 UTF-8 解码，**非 ASCII 的进程名会变成乱码键** ——
    本机实测 `WorkBuddy小工具.exe` → `workbuddyс????.exe`。
    纯 ASCII 的 `workbuddy.exe` / `workbuddyai.exe` 不受影响。）
    """
    names = [n.lower() for n in (ch.client_processes or ())]
    if not names:
        return []
    table = common.list_processes()          # 失败返回 None（≠ 没进程）
    if table is None:
        return None
    pids = []
    for n in names:
        for pid in (table.get(n) or ()):
            if pid not in pids:
                pids.append(pid)
    return pids


def scan_process(pid, budget=DEFAULT_BUDGET, types=None):
    """扫一个进程的可读内存，返回 {jwt: 出现次数}。

    只做只读访问；任何一步失败（权限、被保护区域）都跳过该区域，
    不让整次扫描失败 —— 客户端以管理员身份运行时我们会一个区域都读不到，
    那时返回空 dict，由调用方降级。

    `types` 限定只扫哪些区域类型（`MEM_PRIVATE` / `MEM_MAPPED` / `MEM_IMAGE` 的集合）；
    None = 全扫。**过滤掉 `MEM_IMAGE` 是最大的单点优化**：Electron 客户端每个进程的
    只读镜像段（DLL/EXE 代码与只读数据）就有 240~340 MB，占全部可读内存的 ~60%，
    而 JWT 是运行时字符串，实测**只出现在 `MEM_PRIVATE`（堆）里**，不可能在只读段中。

    ⚠️ **取块必须走 `memoryview`（零拷贝）**：`buf.raw[:n]` 会**先把整个 1 MB 缓冲区
    复制成 bytes 再切片** —— 单进程 1600 多个块就是 1.6 GB 的无谓 memcpy，
    实测占掉整次扫描 0.43s 里的 0.30s（真正的 `ReadProcessMemory` 只要 0.028s、
    正则 0.073s）。`re` 接受任何 bytes-like 对象且 `m.group(0)` 仍返回 bytes。
    """
    handle = _k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return {}
    found = {}
    total = 0
    addr = 0
    mbi = _MBI()
    buf = ctypes.create_string_buffer(1 << 20)
    view = memoryview(buf)          # 零拷贝切片，见 docstring
    got = ctypes.c_size_t()
    try:
        while addr < MAX_USER_ADDR and total < budget:
            if not _k32.VirtualQueryEx(ctypes.c_void_p(handle), ctypes.c_void_p(addr),
                                       ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize or 0x1000
            readable = (mbi.State == MEM_COMMIT
                        and (mbi.Protect & READABLE)
                        and not (mbi.Protect & PAGE_GUARD)
                        and not (mbi.Protect & PAGE_NOACCESS)
                        and (types is None or mbi.Type in types))
            if readable:
                off = 0
                while off < size:
                    n = min(1 << 20, size - off)
                    if _k32.ReadProcessMemory(ctypes.c_void_p(handle),
                                              ctypes.c_void_p(base + off), buf, n,
                                              ctypes.byref(got)):
                        total += got.value
                        for m in _JWT_RE.finditer(view[:got.value]):
                            if _MIN_JWT_LEN < len(m.group(0)) < _MAX_JWT_LEN:
                                tok = m.group(0).decode("ascii", "replace")
                                found[tok] = found.get(tok, 0) + 1
                    off += n
            addr = base + size
    except OSError:
        pass
    finally:
        _k32.CloseHandle(ctypes.c_void_p(handle))
    return found


def _collect(pids, budget, types, stop):
    """按顺序扫这批进程，边扫边判停；返回 ({uid: slot}, [实际扫过的 pid])。

    `types` 见 `scan_process`。`stop(by_uid)` 返回真就立刻停下 ——
    这是「命中即停」：token 往往在前几个进程就出现，没必要扫完剩下的。
    """
    by_uid = {}
    scanned = []
    for p in pids:
        scanned.append(p)
        try:
            found = scan_process(p, budget=budget, types=types)
        except Exception:
            # 单个坏进程不能让整次扫描失败
            continue
        for tok in found:
            kind = _classify(tok)
            if not kind:
                continue
            name, payload = kind
            uid = str(payload.get("sub") or "")
            if not uid:
                continue
            by_uid.setdefault(uid, {"access": {}, "refresh": {}})[name][tok] = (payload, p)
        if stop(by_uid):
            break
    return by_uid, scanned


def _has_access(by_uid):
    return any(slot["access"] for slot in by_uid.values())


def _stop_on_access(by_uid):
    return _has_access(by_uid)


def _stop_on_both(by_uid):
    return any(slot["access"] and slot["refresh"] for slot in by_uid.values())


def _build_session(uid, slot, pids):
    """把某个 uid 的 access/refresh 候选整理成 session dict；没有 access 返回 None。

    同一 uid 可能有多份 token（切号残留 / 刷新前后各一份）→ 取 exp 最晚的。
    """
    if not slot["access"]:
        return None
    best_tok, (best_payload, pid) = max(
        slot["access"].items(), key=lambda kv: kv[1][0].get("exp") or 0)

    refresh_tok = ""
    refresh_exp = None
    if slot["refresh"]:
        rt, (rp, _rpid) = max(slot["refresh"].items(),
                              key=lambda kv: kv[1][0].get("exp") or 0)
        refresh_tok = rt
        refresh_exp = _expiry(rp)

    return {
        "access_token": best_tok,
        "refresh_token": refresh_tok,
        "uid": uid,
        "nickname": str(best_payload.get("nickname")
                        or best_payload.get("preferred_username") or ""),
        "expires_at": _expiry(best_payload),
        "refresh_expires_at": refresh_exp,
        "token_source": str(best_payload.get("token_source") or ""),
        "source_pid": pid,
        "scanned_pids": list(pids),
    }


def read_live_session(ch, expect_uid=None, budget=DEFAULT_BUDGET, use_cache=True,
                      want_refresh=False):
    """从该通道客户端进程内存里读出当前登录凭据。

    返回 dict（字段与 `workbuddy_checkin.session_from_info_file` 对齐，
    好让调用方直接喂给现有渲染/接口逻辑）：

        {access_token, refresh_token, uid, nickname, expires_at,
         refresh_expires_at, token_source, source_pid, scanned_pids}

    读不到返回 None（客户端没跑 / 权限不足 / 没找到 token）。
    expect_uid 给了就只认该 uid 的会话（切号后内存里会残留旧账号的 token）。

    `want_refresh=False`（默认，也是所有功能路径用的档）：**拿到 access 就停**，
    不为 refresh_token 多扫几个进程 —— 昵称 / 到期时间 / 签发天数 / 积分 / 签到
    全都只需要 access。`refresh_token` 目前只有诊断接口 `/api/live-token` 会看，
    它显式传 `want_refresh=True`，那时才扫到 access + refresh 都拿到为止。

    ## 性能（2026-09-23 优化，实测国服 7 进程）

    原实现串行全扫每个进程的**全部**可读内存，冷启动 3.5 秒；`current_account()`
    还会为桌面目录里每个加密文件各调一次，而切号备份的 uid 不是当前登录账号，
    于是每次请求都白扫一遍。三处一起改：

    | 改动 | 效果 |
    |---|---|
    | 缓存**按 uid 存会话**（一次扫描建好所有 uid 的会话） | 两次调用合计只扫一次；缺失即负命中 |
    | 跳过 `MEM_IMAGE`（只读镜像段，占可读内存 ~60%） | 读取量减半 |
    | `MEM_PRIVATE` 优先 + **命中即停** | 命中点实测在 44~64 MB 处，前几个进程就结束 |
    | ~~并行扫描~~ | **实测无效已撤**：跨进程读内存有全局竞争，workers=7 比串行还慢 |
    """
    key = ch.key
    now = time.time()
    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        # ⚠️ 命中还要求「这份缓存够用」：`want_refresh=True` 想要 refresh_token，
        #    而功能路径（want_refresh=False）写下的缓存里 refresh_token 是空串。
        #    直接复用会让诊断接口拿到空 refresh_token，误报「该账号没有 refreshToken」，
        #    且结论随调用顺序漂移（先访问页面再诊断 = 假阴性）。
        if (hit and now - hit["ts"] < CACHE_TTL_SECONDS
                and (hit.get("want_refresh") or not want_refresh)):
            sessions = hit.get("sessions") or {}
            if expect_uid:
                # 缺失 = 负命中：上次扫描确认内存里没有这个 uid 的凭据，不必再扫
                return sessions.get(expect_uid)
            best = hit.get("best_uid")
            return sessions.get(best) if best else None

    pids = client_pids(ch)
    if pids is None:
        # 枚举失败（≠ 没有进程）：**不能写缓存** —— 否则接下来 60 秒所有请求
        # 都会被负命中判成「桌面端未登录」，而客户端其实好好跑着。
        return None
    if not pids:
        _remember(key, {}, None, now, use_cache, want_refresh)
        return None

    stop = _stop_on_both if want_refresh else _stop_on_access

    # 第一遍：只扫私有提交区（堆）。实测 JWT 只出现在这里，且命中点很靠前。
    by_uid, scanned = _collect(pids, budget, {MEM_PRIVATE}, stop)
    if not _has_access(by_uid):
        # 兜底：堆里没有（客户端版本变了 / token 被放进了映射区）→ 再扫
        # MAPPED + IMAGE。不重复扫 PRIVATE。
        more, scanned2 = _collect(pids, budget, {MEM_MAPPED, MEM_IMAGE}, stop)
        for uid, slot in more.items():
            dst = by_uid.setdefault(uid, {"access": {}, "refresh": {}})
            dst["access"].update(slot["access"])
            dst["refresh"].update(slot["refresh"])
        for p in scanned2:
            if p not in scanned:
                scanned.append(p)

    sessions = {}
    for uid, slot in by_uid.items():
        s = _build_session(uid, slot, scanned)
        if s:
            sessions[uid] = s
    # 最活跃的 = exp 最晚的那个（客户端同一时刻只登一个账号，正常只有一条）
    best_uid = None
    if sessions:
        def _exp_of(uid):
            return max((p.get("exp") or 0) for p, _ in by_uid[uid]["access"].values())
        best_uid = max(sessions, key=_exp_of)
    _remember(key, sessions, best_uid, now, use_cache, want_refresh)
    if expect_uid:
        return sessions.get(expect_uid)
    return sessions.get(best_uid) if best_uid else None


def _remember(key, sessions, best_uid, now, use_cache, want_refresh=False):
    """写缓存。**成功与失败都要写** —— 失败也要写，负命中才有依据。

    `want_refresh` 一并记下：它决定这份会话里 refresh_token 是否可信
    （False 档扫到 access 就停，refresh_token 一定是空的）。命中时要靠它判断
    够不够用，见 `read_live_session` 的缓存分支。
    """
    if not use_cache:
        return
    with _CACHE_LOCK:
        _CACHE[key] = {"ts": now, "sessions": sessions, "best_uid": best_uid,
                       "want_refresh": bool(want_refresh)}


def invalidate_cache(ch=None):
    """丢掉缓存（切号后调用；ch 为空表示全丢）。"""
    with _CACHE_LOCK:
        if ch is None:
            _CACHE.clear()
        else:
            _CACHE.pop(ch.key, None)
