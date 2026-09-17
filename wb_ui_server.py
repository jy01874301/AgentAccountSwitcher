"""wb_ui_server.py —— WorkBuddy 账号切换小工具的本地后端。

提供两个职责：
1. 纯函数：列出 wb_auth/ 下的可用账号、把选中账号切换为 WorkBuddy 桌面端正式登录态
   （%LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info）。
2. 极简 HTTP 服务：供前端 HTML 页面调用（仅绑定 127.0.0.1，局域网不可访问）。

复用 workbuddy_checkin.py 的解析函数（session_from_info_file / split_info_name）。

用法：
  python wb_ui_server.py --list          # 列出 wb_auth 可用账号（JSON）
  python wb_ui_server.py --current       # 查看桌面端当前账号（JSON）
  python wb_ui_server.py --switch NAME   # 切换为 wb_auth\\NAME.info 账号
  python wb_ui_server.py --serve [--port 8765]  # 启动本地 HTTP 服务（端口占用自动顺延）
  python wb_ui_server.py --prune [N]    # 清理桌面端切号备份，只留最近 N 份（默认 10）
"""

import argparse
import datetime
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 复用现有脚本的解析/常量
# 程序所在目录（wb_switcher），设计位置为 D:\AI项目\wb_switcher，项目根为其同级目录 自动签到
_BIN_DIR = Path(__file__).resolve().parent
project_root_candidate = _BIN_DIR.parent / "自动签到"
if not (project_root_candidate / "workbuddy_checkin.py").is_file():
    # 兼容旧布局（wb_switcher 位于 自动签到\wb_switcher 时，项目根即其上一级）
    project_root_candidate = _BIN_DIR.parent
PROJECT_ROOT = project_root_candidate

# 复用主目录现有脚本的解析/常量（签到/续期逻辑本体在主目录，本工具只读引用）
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(_BIN_DIR))
import switcher_common as common  # 两个切换器共用的 HTTP 骨架 / 文件锁 / 备份裁剪

# 复用上级项目的解析/续期逻辑：先声明需要的成员，缺失时给出可读提示而不是裸 Traceback
wb = common.require_module(
    "workbuddy_checkin",
    ("session_from_info_file", "split_info_name", "refresh_account", "load_config",
     "parse_jwt_payload", "AUTH_REL_DIR", "LOGOUT_MARKER_SUFFIX"),
    who="wb_ui_server.py")

# 账号库目录固定为本工具所在目录（本工具独立存放账号配置），
# 与 PROJECT_ROOT（复用自动签到项目的解析/续期逻辑）解耦，避免指向兄弟项目。
SCRIPT_DIR = PROJECT_ROOT
AUTH_DIR = _BIN_DIR / "wb_auth"
DESKTOP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / wb.AUTH_REL_DIR
DESKTOP_INFO = DESKTOP_DIR / "workbuddy-desktop.info"
LOGOUT_SUFFIX = wb.LOGOUT_MARKER_SUFFIX  # ".logged-out"
LOCK_DIR = _BIN_DIR / ".locks"      # 跨进程锁文件（不放进客户端配置目录）
DESKTOP_BACKUP_KEEP = 10            # 桌面端切号备份保留份数（备份本身是有效登录态）
DEFAULT_PORT = 8765


def _read_info(path):
    """读取一个 .info 文件并解析，返回 dict；结构不符返回 None。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_accounts():
    """列出 wb_auth 目录下所有可用账号。

    优先返回 .info 文件（含切号备份的文件）。每个条目带文件路径与令牌脱敏摘要；
    accessToken 不完整的（如粘贴时被截断）标记为不可用，避免切过去才发现是废凭据。
    """
    out = []
    if not AUTH_DIR.is_dir():
        return out
    for f in sorted(AUTH_DIR.glob("*.info")):
        acc = wb.session_from_info_file(f)
        raw = _read_info(f) or {}
        if not acc:
            # 无法解析的 .info 仍列出，标注不可用
            name = raw.get("account", {}).get("nickname") if isinstance(raw, dict) else None
            out.append({
                "file": f.name,
                "path": str(f),
                "ok": False,
                "label": name or f.stem,
                "nickname": name or f.stem,
                "uid": "",
                "expires_at": None,
                "refresh_expires_at": None,
                "reason": "登录态无法解析（内容可能不全或被加密）",
            })
            continue
        entry = {
            "file": f.name,
            "path": str(f),
            "ok": True,
            "label": acc["label"],
            "nickname": acc["nickname"],
            "uid": acc["uid"],
            "expires_at": acc["expires_at"].isoformat() if acc["expires_at"] else None,
            "refresh_expires_at": acc["refresh_expires_at"].isoformat() if acc["refresh_expires_at"] else None,
            "auth_id": acc["auth_id"],
        }
        at = (raw.get("auth") or {}).get("accessToken")
        # 签发通道决定有效期长短：oneid_login 60 天，enterprise_switch 只有 3 天
        entry["token_source"] = common.token_source(at)
        if not common.token_looks_complete(at):
            entry["ok"] = False
            entry["reason"] = "accessToken 不完整（疑似粘贴截断），切过去会 401，请重新导出该账号文件"
        out.append(entry)
    return out


def current_account():
    """识别 WorkBuddy 桌面端当前账号。

    桌面目录可能只有正式文件 workbuddy-desktop.info，也可能因切号遗留
    workbuddy-desktop.<时间戳>.<pid>.<uuid>.info 备份（备份本身是有效登录态，
    WorkBuddy 平滑切号时用它恢复会话）。因此这里把所有 .info 都解析出来，
    去掉登出标记的，并按修改时间降序，把最新会话标记为 current=true 作为当前账号。
    """
    entries = []
    for f in DESKTOP_DIR.glob("*.info"):
        root_id, is_backup = wb.split_info_name(f.name)
        if not root_id:
            continue
        if (DESKTOP_DIR / ("%s.info%s" % (root_id, LOGOUT_SUFFIX))).exists():
            continue  # 该 id 会话已登出
        acc = wb.session_from_info_file(f)
        entries.append({
            "file": f.name,
            "ok": bool(acc),
            "nickname": acc["nickname"] if acc else (root_id),
            "uid": acc["uid"] if acc else "",
            "expires_at": acc["expires_at"].isoformat() if acc and acc["expires_at"] else None,
            "is_backup": bool(is_backup),
            "mtime": f.stat().st_mtime,
        })
    # 最新修改的会话视为当前（正式写在备份之后，通常就是最新）
    entries.sort(key=lambda x: x["mtime"], reverse=True)
    for i, e in enumerate(entries):
        e["current"] = (i == 0)
    return entries


def _restore_backup(backup):
    """写入失败时把已轮换出去的备份改回正式文件名，避免桌面端陷入"无登录态"。

    switch_account 的顺序是「旧文件改名 → 写新文件」，中间那一步失败的话
    workbuddy-desktop.info 就不存在了，客户端会认为没有登录态。备份本身是有效
    登录态，改回来即可恢复原状。

    返回追加给用户看的说明（成功 / 失败都给出可执行的下一步），无备份可恢复时返回空串。
    """
    if not backup:
        return ""
    src = Path(backup)
    if not src.exists():
        return ""
    try:
        src.replace(DESKTOP_INFO)
        return "；已回滚为原登录态"
    except OSError:
        return ("；回滚也失败，原登录态仍在备份 %s，可手工改名回 workbuddy-desktop.info"
                % src.name)


def switch_account(target_name):
    """把 wb_auth\\<target_name>.info 切换为桌面端正式登录态。

    - 校验目标文件存在且可解析；
    - 桌面目录不存在则创建；
    - 现有正式文件改名为带时间戳的备份（与客户端 clean() 一致），保留其可用登录态；
    - 复制目标文件内容为 workbuddy-desktop.info；
    - 清理 workbuddy-desktop.info.logged-out 登出标记（若有）。
    返回 (ok, message)。
    """
    # 只接受文件名，与 remove/refresh 保持一致。此前直接拼 AUTH_DIR / target_name，
    # 传 "../xxx.info" 会解析到账号库之外的文件（实测可命中 ..\..\自动签到\wb_auth\*.info），
    # 等于允许把任意路径的 .info 写进桌面端登录态。
    norm = (target_name or "").replace("\\", "/")
    if not norm or os.path.basename(norm) != norm:
        return False, "非法的账号文件名：%s" % (target_name or "")
    if not norm.endswith(".info"):
        return False, "仅支持 .info 格式的账号文件"
    src = AUTH_DIR / norm
    if not src.is_file():
        return False, "目标账号文件不存在：%s" % target_name

    # 「备份 + 写入 + 清标记」必须串行，否则与续期/另一次切号交叉会写坏登录态
    with common.file_lock("wb-desktop", LOCK_DIR):
        acc = wb.session_from_info_file(src)
        if not acc:
            return False, "目标账号文件无法解析（内容可能不全或被加密）：%s" % target_name
        data = _read_info(src)
        if data is None:
            return False, "目标账号文件不是合法 JSON：%s" % target_name
        if not common.token_looks_complete((data.get("auth") or {}).get("accessToken")):
            return False, ("目标账号的 accessToken 不完整（疑似粘贴时被截断），切换后必然 401：%s。"
                           "请重新从客户端导出完整的 workbuddy-desktop.info" % target_name)

        DESKTOP_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now(datetime.timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H%M%SZ")
        pid = os.getpid()
        marker = "%d" % now.microsecond

        # 1. 现有正式文件轮换为备份（写入失败时用它回滚）
        backup = None
        if DESKTOP_INFO.exists():
            backup = DESKTOP_DIR / ("workbuddy-desktop.%s.%d.%s.info" % (ts, pid, marker))
            try:
                DESKTOP_INFO.replace(backup)
            except OSError as e:
                return False, "轮换旧登录态失败：%s" % common.scrub(e)
            pruned = common.prune_backups(
                DESKTOP_DIR, "workbuddy-desktop.*.info",
                keep=DESKTOP_BACKUP_KEEP, exclude={"workbuddy-desktop.info"})
        else:
            pruned = 0

        # 2. 写入目标账号。这一步失败必须回滚：否则正式文件已被改名走，
        #    桌面端会处于"没有登录态"的状态（此前只报失败、不回滚）。
        tmp = DESKTOP_INFO.with_suffix(".info.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(DESKTOP_INFO)
        except OSError as e:
            try:
                tmp.unlink()
            except OSError:
                pass
            # OSError 文本会带出本机绝对路径（如桌面端 auth 目录），先脱敏再回给前端
            return False, "写入新登录态失败：%s%s" % (common.scrub(e), _restore_backup(backup))

        # 3. 清理登出标记
        marker_path = DESKTOP_DIR / ("workbuddy-desktop.info" + LOGOUT_SUFFIX)
        if marker_path.exists():
            try:
                marker_path.unlink()
            except OSError:
                pass

        # 4. 写后自校验（与 Trae 切换器一致）：确认桌面端文件里确实是目标账号。
        #    客户端 watcher 未采纳或被其它进程覆盖时立刻暴露，而不是报成功却没生效。
        after = _read_info(DESKTOP_INFO)
        acc_after = wb.session_from_info_file(DESKTOP_INFO) if after is not None else None
        if not acc_after or acc_after["uid"] != acc["uid"]:
            raw_acct = (after or {}).get("account")
            cur_nick = (raw_acct or {}).get("nickname") if isinstance(raw_acct, dict) else None
            if acc_after:
                cur_nick = acc_after["nickname"]
            return False, ("写入后校验未通过：桌面端当前登录态仍为「%s」，切换可能未生效。"
                           "请确认 WorkBuddy 客户端未占用该文件后重试" % (cur_nick or "未知"))

    msg = "已切换为 %s（%s），桌面端将自动采纳新会话" % (acc["nickname"], target_name)
    if pruned:
        msg += "；已清理 %d 份旧备份（保留最近 %d 份）" % (pruned, DESKTOP_BACKUP_KEEP)
    return True, msg


def safe_info_name(name):
    """把用户输入规整成合法的 *.info 文件名，拦截路径穿越/危险字符。返回 (文件名, 错误)。"""
    name = (name or "").strip()
    if not name:
        return None, "缺少账号标识"
    # 去掉可能带上的 .info，统一处理
    if name.lower().endswith(".info"):
        name = name[:-5]
    name = re.sub(r"\s+", " ", name).strip()
    # 仅允许文件名安全字符（含空格），防止路径注入
    safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._ -]", "", name).strip()
    if not safe:
        return None, "账号名只能包含中英文、数字、空格、点和下划线"
    return "workbuddy-%s.info" % safe, None


def add_account(name, content):
    """新增一个账号配置文件到 wb_auth\\（内容为前端粘贴的 info 或上传文件）。"""
    fname, err = safe_info_name(name)
    if err:
        return False, err
    src = AUTH_DIR / fname
    if src.exists():
        return False, "已存在同名账号文件：%s" % fname
    # 先校验是合法 JSON
    try:
        data = json.loads(content)
    except ValueError:
        return False, "内容不是合法 JSON，无法解析为账号文件"

    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    with common.file_lock("wb-auth-" + fname, LOCK_DIR):
        if src.exists():
            return False, "已存在同名账号文件：%s" % fname
        try:
            src.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            return False, "写入账号文件失败：%s" % e

        # 用现有脚本的解析器校验：只有能解析成有效登录态才保留，否则回滚删除
        acc = wb.session_from_info_file(src)
        if not acc:
            try:
                src.unlink()
            except OSError:
                pass
            return False, "内容不是有效的 WorkBuddy 登录态信息（缺少字段或已加密），已回滚，请粘贴正确的账号文件内容"
        if not common.token_looks_complete((data.get("auth") or {}).get("accessToken")):
            try:
                src.unlink()
            except OSError:
                pass
            return False, ("accessToken 不完整（疑似粘贴时被截断），已回滚。"
                           "请整份复制客户端的 workbuddy-desktop.info 后重新添加")
    return True, "已新增账号 %s（%s）" % (acc["nickname"], fname)


def remove_account(file_name):
    """删除 wb_auth\\ 下的一个账号配置文件（防止路径穿越）。"""
    # 限定文件名，不得包含路径分隔符或上一级
    base = os.path.basename((file_name or "").replace("\\", "/"))
    if base != base.replace("..", "") or base != (file_name or "").replace("\\", "/").split("/")[-1]:
        return False, "非法的文件名"
    if not base.endswith(".info"):
        return False, "只能删除 .info 账号文件"
    target = AUTH_DIR / base
    if not target.is_file():
        return False, "账号文件不存在：%s" % base
    with common.file_lock("wb-auth-" + base, LOCK_DIR):
        try:
            target.unlink()
        except OSError as e:
            return False, "删除失败：%s" % e
    return True, "已删除账号文件 %s" % base


class _NullLog:
    """极简日志壳：续期日志不进配置文件目录，避免污染签到日志。"""
    def __init__(self):
        self._buf = []

    def __call__(self, msg):
        try:
            self._buf.append(str(msg))
        except Exception:
            pass


_NULLLOG = _NullLog()


def _recalc_expiry(path):
    """续期后用 JWT 的 exp 声明重算 expiresAt / refreshExpiresAt。

    workbuddy_checkin.merge_auth_token 只在 expiresAt 缺失时才重算，旧值存在时不覆盖，
    导致续期后 wb_auth 文件里 expiresAt 仍为旧值、剩余天数不更新。
    JWT 的 exp 是服务端设置的权威到期时间，比 lastRefreshTime+expiresIn 更准确。
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    auth = data.get("auth")
    if not isinstance(auth, dict):
        return
    changed = False
    atoken = str(auth.get("accessToken") or "")
    if atoken:
        a_exp = wb.parse_jwt_payload(atoken).get("exp")
        if a_exp:
            new_exp = int(a_exp) * 1000
            if auth.get("expiresAt") != new_exp:
                auth["expiresAt"] = new_exp
                changed = True
    rtoken = str(auth.get("refreshToken") or "")
    if rtoken:
        r_exp = wb.parse_jwt_payload(rtoken).get("exp")
        if r_exp:
            new_rexp = int(r_exp) * 1000
            if auth.get("refreshExpiresAt") != new_rexp:
                auth["refreshExpiresAt"] = new_rexp
                changed = True
    if not changed:
        return
    data["auth"] = auth
    tmp = Path(path).with_suffix(".info.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def refresh_account_file(file_name, force=True):
    r"""切换后对目标账号执行真正的 HTTP 续期，并把新有效期写回 wb_auth\<file>。

    与「把桌面端到期时间复制回素材」不同，本函数直接拿目标文件的 refreshToken
    向 WorkBuddy 服务器续期，返回全新 accessToken / expiresAt，剩余时间即刻更新。

    force=True（默认，UI 上手动点「续期」用）：无条件刷新。
    force=False（计划任务用）：交给上级 workbuddy_checkin.refresh_account 判断 ——
      剩余天数 ≥ REFRESH_THRESHOLD_DAYS（3 天）或距上次实际续期不足
      refresh_min_interval_hours（默认 24 小时）就跳过，返回 kind="skipped"。
      与上级 refresh_guard.py / --refresh 的门卫语义一致，避免每天无谓地重写 .info。
    """
    base = os.path.basename((file_name or "").replace("\\", "/"))
    if os.path.dirname((file_name or "").replace("\\", "/")):
        return False, "非法的文件路径"
    if not base.endswith(".info"):
        return False, "只能续期 .info 账号文件"
    target = AUTH_DIR / base
    if not target.is_file():
        return False, "wb_auth 中不存在账号文件：%s" % base
    acc = wb.session_from_info_file(target)
    if not acc:
        return False, "账号文件无法解析，无法续期"
    cfg = wb.load_config(SCRIPT_DIR / "config.json")
    # 续期是「换 token + 写回文件」，与切号/同步互斥，否则会互相覆盖
    with common.file_lock("wb-auth-" + base, LOCK_DIR):
        ok, msg, kind = wb.refresh_account(acc, SCRIPT_DIR, cfg, _NULLLOG, force=force)
        if kind == "refreshed" and ok:
            _recalc_expiry(target)
            return True, "已续期并更新 %s 的到期时间" % base
    return ok, msg


def refresh_all(force=False):
    """对 wb_auth 下全部账号跑一遍续期，供计划任务调用。返回退出码。

    默认 force=False —— 走上级的门卫（剩余 < 3 天且距上次 ≥ 24 小时才真刷），
    所以「每天跑一次」的实际开销通常只有几次本地文件读取，不产生任何写盘。
    只有 --refresh-all --force 才无条件全刷。

    为什么要门卫：现在新登账号一律是 enterprise_switch（access 30 天），
    access 到期前续一次就不会掉线；而无条件续期会重写 5 个 .info，
    客户端在跑时属于没必要的写冲突风险。与上级 refresh_guard.py 的语义一致。
    """
    results = []
    for a in list_accounts():
        if not a.get("ok"):
            results.append((a["file"], False, a.get("reason") or "不可用，跳过"))
            continue
        ok, msg = refresh_account_file(a["file"], force=force)
        results.append((a["file"], ok, msg))
        common.audit(Handler.AUDIT_DIR, Handler.SOURCE, "refresh-all", a["file"], ok, msg)
    ok_n = sum(1 for r in results if r[1])
    for f, ok, msg in results:
        print("%-4s %-32s %s" % ("OK" if ok else "FAIL", f, msg))
    print("---- 续期完成：%d/%d 成功" % (ok_n, len(results)))
    return 0 if ok_n == len(results) else 1


# ---------------------------------------------------------------------------
# 积分明细（只读：查资源包，不消耗任何积分）
# ---------------------------------------------------------------------------
# 积分来自计费网关的资源包接口（客户端「积分明细」用同一份数据）：
#   POST {endpoint}/v2/billing/meter/get-user-resource   ← 有 PackageName 与周期
#   POST {endpoint}/billing/meter/get-user-resource-summary  ← 只有容量，无名称/周期
# 按资源包性质归两类，与客户端展示一致：
#   套餐基础积分：PackageName 如「CodeBuddy个人体验版」，周期按月滚动 → 下次刷新时间 = 周期结束 +1 秒
#   平台奖励积分：PackageName 如「CodeBuddy个人版国内运营裂变包」（赠送包），一包一到期 → 取最近到期
CREDITS_PATH = "/v2/billing/meter/get-user-resource"
CREDITS_TTL = 600.0       # 缓存秒数：查一次要按账号数发请求，不能每次刷新都回源
# 计费网关强制校验 User-Agent，缺省 urllib UA 会被 10085 拒绝（签到接口无此要求）
BILLING_UA = "Mozilla/5.0 WorkBuddy/5.5.6"
# 资源包名里出现这些词即视为「平台奖励积分」，其余归「套餐基础积分」
_BONUS_HINTS = ("赠送", "裂变", "奖励", "bonus", "gift")

_CREDITS_LOCK = threading.Lock()
_CREDITS_CACHE = {"ts": 0.0, "payload": None}


def _as_int(val):
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _num(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _parse_dt(text):
    """解析 "2026-09-30 23:59:59" 这类本地时间字符串（网关不带时区标记）。"""
    s = str(text or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", ""))
    except ValueError:
        return None


def _billing_post(url, headers, timeout=20, retries=2):
    """调用计费网关并解析 JSON，返回 (http_status, dict)。

    不能用 workbuddy_checkin.post_json：它会把 User-Agent 强制改写成脚本自己的
    UA，而计费接口要求 UA 为 BILLING_UA，否则国内网关直接 403 / 业务码 10085。
    """
    import urllib.error
    import urllib.request
    last = ""
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=b"{}", method="POST")
            for k, v in (headers or {}).items():
                req.add_header(k, v)
            req.add_header("User-Agent", BILLING_UA)
            req.add_header("Accept-Language", "zh-CN")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            if e.code >= 500 and attempt < retries:
                last = "HTTP %d: %s" % (e.code, body)
                time.sleep(1.5 * attempt)
                continue
            return e.code, {"_error": body}
        except Exception as e:  # noqa: BLE001
            last = repr(e)
            if attempt < retries:
                time.sleep(1.5 * attempt)
                continue
    return 0, {"_error": "网络请求失败：%s" % last}


def query_credits(acc, cfg):
    """查单个账号的积分明细。返回 dict，失败时 ok=False 并带 reason（不影响其它账号）。

    返回形如：
        {ok, total_remain, unit, items:[{key,label,time_label,total,used,remain,
                                         used_percent,remain_percent,time,packages}]}
    """
    if not hasattr(wb, "build_headers"):
        return {"ok": False, "reason": "上级模块缺少 build_headers，无法查询积分"}
    headers = wb.build_headers(acc)
    headers["Content-Type"] = "application/json"
    code, body = _billing_post(cfg["endpoint"] + CREDITS_PATH, headers)
    if code != 200:
        return {"ok": False, "reason": "积分接口 HTTP %s" % code}
    if not isinstance(body, dict) or body.get("code") not in (None, 0):
        return {"ok": False, "reason": "积分接口业务错误（code=%s）"
                % (body or {}).get("code")}
    accounts = ((((body.get("data") or {}).get("Response") or {}).get("Data") or {})
                .get("Accounts"))
    if accounts is None:
        accounts = []          # 账号名下还没有任何资源包（TotalCount=0），并非错误
    if not isinstance(accounts, list):
        return {"ok": False, "reason": "积分接口结构异常"}

    groups = {}
    for a in accounts:
        if not isinstance(a, dict):
            continue
        # Status != 0 是已失效的资源包（ExpiredTime 有值，实测如 2026-08-18 批次的裂变包）。
        # 不过滤会污染「最近到期时间」，与签到活动残留 end_time 是同一类坑。
        if _as_int(a.get("Status")) != 0:
            continue
        text = "%s %s" % (a.get("PackageName") or "", a.get("SubProductName") or "")
        key = "bonus" if any(h.lower() in text.lower() for h in _BONUS_HINTS) else "plan"
        # 用 Precise 字段：实测 258.45999959 这种小数只在 Precise 里保留
        size = _num(a.get("CycleCapacitySizePrecise") or a.get("CycleCapacitySize"))
        used = _num(a.get("CycleCapacityUsedPrecise") or a.get("CycleCapacityUsed"))
        remain = _num(a.get("CycleCapacityRemainPrecise") or a.get("CycleCapacityRemain"))
        end = _parse_dt(a.get("CycleEndTime"))
        g = groups.setdefault(key, {"size": 0.0, "used": 0.0, "remain": 0.0,
                                    "end": None, "n": 0})
        g["size"] += size
        g["used"] += used
        g["remain"] += remain
        g["n"] += 1
        if end and (g["end"] is None or end < g["end"]):
            g["end"] = end

    items = []
    for key, label, time_label in (("plan", "套餐基础积分", "下次刷新时间"),
                                   ("bonus", "平台奖励积分", "最近到期时间")):
        g = groups.get(key)
        if not g or g["size"] <= 0:
            continue
        # 套餐按周期滚动：界面上写的是"下一次刷新"的时刻，即本周期结束 +1 秒；
        # 奖励包不可刷新，直接用它最近一次的到期时间
        when = (g["end"] + datetime.timedelta(seconds=1) if key == "plan" and g["end"]
                else g["end"])
        items.append({
            "key": key,
            "label": label,
            "time_label": time_label,
            "total": round(g["size"], 2),
            "used": round(g["used"], 2),
            "remain": round(g["remain"], 2),
            "used_percent": round(g["used"] / g["size"] * 100) if g["size"] else 0,
            "remain_percent": round(g["remain"] / g["size"] * 100) if g["size"] else 0,
            "time": when.strftime("%Y/%m/%d %H:%M:%S") if when else "",
            "packages": g["n"],
        })
    return {"ok": True, "unit": "credits", "items": items,
            "total_remain": round(sum(i["remain"] for i in items), 2)}


def credits_snapshot(force=False):
    """汇总 wb_auth 全部账号的积分明细（总剩余 + 套餐基础 / 平台奖励）。

    结果在进程内缓存 CREDITS_TTL 秒（前端每次刷新页面都会拉一次，不缓存等于放大
    N 倍请求）。force=True 强制回源（标题栏的「刷新积分」）。
    """
    now = time.time()
    with _CREDITS_LOCK:
        cached = _CREDITS_CACHE["payload"]
        if not force and cached and now - _CREDITS_CACHE["ts"] < CREDITS_TTL:
            return dict(cached, cached=True)

    cfg = wb.load_config(SCRIPT_DIR / "config.json")
    accounts = list_accounts()

    def _one(a):
        entry = {"file": a["file"], "nickname": a.get("nickname") or a["file"], "ok": False}
        if not a.get("ok"):
            entry["reason"] = a.get("reason") or "账号不可用"
            return entry
        acc = wb.session_from_info_file(AUTH_DIR / a["file"])
        if not acc:
            entry["reason"] = "登录态无法解析"
            return entry
        try:
            entry.update(query_credits(acc, cfg))
        except Exception as e:  # noqa: BLE001  单个账号失败不能拖垮整张表
            entry["reason"] = "积分查询异常：%s" % common.scrub(e, 80)
        return entry

    # 并发查询：每账号一次外部请求，串行累加会让页面等好几秒，等待期间用户一刷新
    # 就取消请求（服务端表现为连接中止）。并发后总耗时约等于最慢的单个账号。
    workers = min(6, max(1, len(accounts)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        items = list(pool.map(_one, accounts))

    ok_items = [i for i in items if i.get("ok")]
    payload = {
        "ok": True,
        "total_remain": round(sum(_num(i.get("total_remain")) for i in ok_items), 2),
        "accounts": items,
        "queried": len(ok_items),
        "total": len(items),
        "ts": datetime.datetime.now().strftime("%H:%M:%S"),
        "cached": False,
    }
    with _CREDITS_LOCK:
        _CREDITS_CACHE["ts"] = time.time()
        _CREDITS_CACHE["payload"] = payload
    return payload


class Handler(common.BaseHandler):
    """WorkBuddy 切换器的路由；HTTP 骨架与跨站校验见 switcher_common.BaseHandler。"""

    INDEX_NAME = "ui_template.html"   # 与 Trae 切换器共用同一份模板
    BASE_DIR = _BIN_DIR
    UI_CONTEXT = {
        "TITLE": "WorkBuddy 账号切换器",
        "LOGO": "&#128172;",
        "SUBTITLE": "一键切换桌面端登录账号 · 免手机验证码 · 同机共用各账号积分",
        "TIP": ("账号配置存放在 <b>wb_auth\\</b> 目录，每个文件是一份 <b>.info</b> 登录态"
                "（<code>%LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\</code> 下导出的那种）。"
                "切换即替换桌面端的 <code>workbuddy-desktop.info</code>，旧会话会自动备份。"),
        "AUTH_DIR": "wb_auth",
        "ACCEPT": ".info,application/json",
        "FILE_LABEL": "账号配置文件（.info）",
        "ADD_HINT": "点击展开，选择或粘贴该账号的 .info 登录态",
        "EMPTY_HINT": "请把 WorkBuddy 账号登录态文件（<code>workbuddy-*.info</code>）放进去。",
        "CMD": "workbuddy_switcher.cmd",
        "CREDITS": "1",            # 账号卡上展示积分明细（Trae 侧无该接口，置空即隐藏）
    }
    WRITE_ENDPOINTS = ("/api/switch", "/api/remove", "/api/refresh", "/api/add")
    SOURCE = "wb"
    AUDIT_DIR = _BIN_DIR / "logs"
    AUDIT_ACTIONS = {"/api/switch": "switch", "/api/remove": "remove",
                     "/api/refresh": "refresh", "/api/add": "add"}

    def api_get(self, u):
        if u.path == "/api/accounts":
            return 200, {"ok": True, "accounts": list_accounts()}
        if u.path == "/api/current":
            return 200, {"ok": True, "current": current_account()}
        if u.path == "/api/credits":
            # ?force=1 强制回源，否则走 CREDITS_TTL 秒的进程内缓存
            return 200, credits_snapshot(force="force=1" in (u.query or ""))
        return 404, {"ok": False, "message": "404"}

    def api_post(self, u, p):
        if u.path == "/api/switch":
            ok, msg = switch_account(str(p.get("name") or ""))
        elif u.path == "/api/remove":
            ok, msg = remove_account(str(p.get("file") or ""))
        elif u.path == "/api/refresh":
            ok, msg = refresh_account_file(str(p.get("file") or ""))
        elif u.path == "/api/add":
            ok, msg = add_account(str(p.get("name") or ""), str(p.get("content") or ""))
        else:
            return 404, {"ok": False, "message": "404"}
        return 200, {"ok": ok, "message": msg}


def serve(port=DEFAULT_PORT, open_browser=True, use_token=True):
    # 一次性令牌：只存在于本次进程，随首页注入给前端，写操作必须回传
    Handler.TOKEN = common.new_token() if use_token else None
    server, port = common.bind_server(Handler, port)
    if port != DEFAULT_PORT:
        print("[提示] 默认端口 %d 被占用，已改用 %d" % (DEFAULT_PORT, port))
    url = "http://127.0.0.1:%d/" % port
    print("本地服务已启动：%s  (Ctrl+C 停止)" % url)
    if open_browser:
        try:
            import webbrowser
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        except Exception:  # noqa: BLE001
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 账号切换器后端")
    ap.add_argument("--list", action="store_true", help="列出 wb_auth 可用账号")
    ap.add_argument("--current", action="store_true", help="查看桌面端当前账号")
    ap.add_argument("--switch", metavar="NAME", help="切换为 wb_auth\\NAME.info")
    ap.add_argument("--serve", action="store_true", help="启动本地 HTTP 服务")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="HTTP 服务端口（默认 %d，被占用时自动顺延）" % DEFAULT_PORT)
    ap.add_argument("--prune", metavar="N", type=int, nargs="?", const=DESKTOP_BACKUP_KEEP,
                    help="清理桌面端切号备份，只保留最近 N 份（默认 %d）" % DESKTOP_BACKUP_KEEP)
    ap.add_argument("--no-auth", action="store_true",
                    help="关闭一次性访问令牌（写操作将只依赖回环 + 同源校验）")
    ap.add_argument("--refresh-all", action="store_true",
                    help="对 wb_auth 下全部账号跑一遍续期（供计划任务调用；默认走门卫，见 --force）")
    ap.add_argument("--force", action="store_true",
                    help="配合 --refresh-all：跳过门卫（剩余天数阈值 + 冷却），无条件全刷")
    args = ap.parse_args()

    if args.prune is not None:
        n = common.prune_backups(DESKTOP_DIR, "workbuddy-desktop.*.info",
                                 keep=max(0, int(args.prune)),
                                 exclude={"workbuddy-desktop.info"})
        print("已清理 %d 份桌面端备份（保留最近 %d 份）" % (n, args.prune))
        common.audit(Handler.AUDIT_DIR, Handler.SOURCE, "prune",
                     "keep=%s" % args.prune, True, "清理 %d 份" % n)
        return 0

    if args.refresh_all:
        return refresh_all(force=args.force)

    if args.list:
        print(json.dumps({"accounts": list_accounts()}, ensure_ascii=False, indent=2))
        return 0
    if args.current:
        print(json.dumps({"current": current_account()}, ensure_ascii=False, indent=2))
        return 0
    if args.switch:
        ok, msg = switch_account(args.switch)
        common.audit(Handler.AUDIT_DIR, Handler.SOURCE, "switch", args.switch, ok, msg)
        print(json.dumps({"ok": ok, "message": msg}, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    serve(port=args.port, use_token=not args.no_auth)
    return 0


if __name__ == "__main__":
    sys.exit(main())