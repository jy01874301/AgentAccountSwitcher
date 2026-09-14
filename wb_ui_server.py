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
        if not common.token_looks_complete((raw.get("auth") or {}).get("accessToken")):
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


def switch_account(target_name):
    """把 wb_auth\\<target_name>.info 切换为桌面端正式登录态。

    - 校验目标文件存在且可解析；
    - 桌面目录不存在则创建；
    - 现有正式文件改名为带时间戳的备份（与客户端 clean() 一致），保留其可用登录态；
    - 复制目标文件内容为 workbuddy-desktop.info；
    - 清理 workbuddy-desktop.info.logged-out 登出标记（若有）。
    返回 (ok, message)。
    """
    src = AUTH_DIR / target_name
    if not src.is_file():
        return False, "目标账号文件不存在：%s" % target_name
    if not src.name.endswith(".info"):
        return False, "仅支持 .info 格式的账号文件"

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

        # 1. 现有正式文件轮换为备份
        if DESKTOP_INFO.exists():
            backup = DESKTOP_DIR / ("workbuddy-desktop.%s.%d.%s.info" % (ts, pid, marker))
            try:
                DESKTOP_INFO.replace(backup)
            except OSError as e:
                return False, "轮换旧登录态失败：%s" % e
            pruned = common.prune_backups(
                DESKTOP_DIR, "workbuddy-desktop.*.info",
                keep=DESKTOP_BACKUP_KEEP, exclude={"workbuddy-desktop.info"})
        else:
            pruned = 0

        # 2. 写入目标账号
        tmp = DESKTOP_INFO.with_suffix(".info.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(DESKTOP_INFO)
        except OSError as e:
            return False, "写入新登录态失败：%s" % e

        # 3. 清理登出标记
        marker_path = DESKTOP_DIR / ("workbuddy-desktop.info" + LOGOUT_SUFFIX)
        if marker_path.exists():
            try:
                marker_path.unlink()
            except OSError:
                pass

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


def refresh_account_file(file_name):
    r"""切换后对目标账号执行真正的 HTTP 续期，并把新有效期写回 wb_auth\<file>。

    与「把桌面端到期时间复制回素材」不同，本函数直接拿目标文件的 refreshToken
    向 WorkBuddy 服务器续期，返回全新 accessToken / expiresAt，剩余时间即刻更新。
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
        ok, msg, kind = wb.refresh_account(acc, SCRIPT_DIR, cfg, _NULLLOG, force=True)
        if kind == "refreshed" and ok:
            _recalc_expiry(target)
            return True, "已续期并更新 %s 的到期时间" % base
    return ok, msg


class Handler(common.BaseHandler):
    """WorkBuddy 切换器的路由；HTTP 骨架与跨站校验见 switcher_common.BaseHandler。"""

    INDEX_FILE = _BIN_DIR / "wb_ui_index.html"
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
    args = ap.parse_args()

    if args.prune is not None:
        n = common.prune_backups(DESKTOP_DIR, "workbuddy-desktop.*.info",
                                 keep=max(0, int(args.prune)),
                                 exclude={"workbuddy-desktop.info"})
        print("已清理 %d 份桌面端备份（保留最近 %d 份）" % (n, args.prune))
        common.audit(Handler.AUDIT_DIR, Handler.SOURCE, "prune",
                     "keep=%s" % args.prune, True, "清理 %d 份" % n)
        return 0

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