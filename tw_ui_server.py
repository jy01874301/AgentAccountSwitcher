"""tw_ui_server.py —— Trae Work CN 账号切换小工具的本地后端。

职责（与 wb_ui_server.py 的 WorkBuddy 切换器对齐）：
1. 纯函数：列出 tw_auth/ 下的可用账号、查看本机 Trae 当前账号、
   把选中账号切换为本机 Trae 客户端的正式登录态。
2. 极简 HTTP 服务：供前端 HTML 页面调用（仅绑定 127.0.0.1，局域网不可访问）。

原理（与 WorkBuddy 的明文 .info 不同）：
   Trae 客户端的登录态保存在
     %APPDATA%\\<Trae 目录>\\User\\globalStorage\\storage.json
   的键 iCubeAuthInfo://icube.cloudide，值是客户端用 AES-128-CBC + SHA-512 密钥
   派生加密后的 base64 密文（明文为 {token, userRegion, ...}）。设备密钥
   iCubeAuthInfo://icube-dc:<id> 是账号绑定的设备凭据，切换账号时应一并替换，
   否则客户端会因设备凭据与登录态不匹配而拒绝/丢弃新登录态。

"切换" = 把目标账号素材里已加密的 icube.cloudide 密文 + 其附属 icube-dc 设备密钥
整体搬运到本机 storage.json（素材本身是该账号登录时客户端原生生成的合法密文，
无需重加密）。其它键（usertag、IDE 窗口配置等）保持不变。

写回后执行「写后自校验」：重新读取本机 storage.json，确认登录态对应的 uid 确实
变成了目标账号；若仍为旧账号，说明 Trae 客户端正在运行并锁定了配置文件（或设备
校验不通过），此时如实返回失败并提示用户先退出 Trae 客户端。

复用自动签到项目的 trae_work_checkin.py 的函数（discover_storages /
account_from_storage / decrypt_auth / extract_token / refresh_account 等）。

用法（与 wb 切换器一致）：
  python tw_ui_server.py --list          # 列出 tw_auth 可用账号（JSON）
  python tw_ui_server.py --current       # 查看本机 Trae 当前账号（JSON）
  python tw_ui_server.py --switch NAME   # 切换为 tw_auth\\NAME 账号
  python tw_ui_server.py --serve [--port 8766]  # 启动本地 HTTP 服务（端口占用自动顺延）
  python tw_ui_server.py --prune [N]    # 清理 tw_backups 备份，只留最近 N 份（默认 10）
"""

import argparse
import datetime
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

# 复用自动签到项目脚本的解析/常量
_BIN_DIR = Path(__file__).resolve().parent
project_root_candidate = _BIN_DIR.parent / "自动签到"
if not (project_root_candidate / "trae_work_checkin.py").is_file():
    project_root_candidate = _BIN_DIR.parent
PROJECT_ROOT = project_root_candidate

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(_BIN_DIR))
import switcher_common as common  # 两个切换器共用的 HTTP 骨架 / 文件锁 / 备份裁剪

# 复用上级项目的解析/续期逻辑：先声明需要的成员，缺失时给出可读提示而不是裸 Traceback
tw = common.require_module(
    "trae_work_checkin",
    ("AUTH_KEY", "CANDIDATE_DIRS", "decrypt_auth", "extract_token", "account_uid",
     "account_from_storage", "refresh_account", "load_config"),
    who="tw_ui_server.py")

AUTH_KEY = tw.AUTH_KEY  # "iCubeAuthInfo://icube.cloudide"

# 账号库目录固定为本工具所在目录下的 tw_auth
TW_AUTH_DIR = _BIN_DIR / "tw_auth"
LOCK_DIR = _BIN_DIR / ".locks"   # 跨进程锁文件（不放进客户端配置目录）
TW_BACKUP_KEEP = 10              # tw_backups 保留份数（备份本身是有效登录态）
DEFAULT_PORT = 8766


def _appdata_dir():
    """%APPDATA% 目录；缺失时回退到 ~/AppData/Roaming。

    不能只写 os.environ.get("APPDATA")：某些启动方式（计划任务、精简环境、
    从非登录 shell 拉起）下该变量为空，而这里一旦拿到空串就会静默返回空路径
    列表，界面表现为"未发现 storage.json"——即使文件就在那儿，且切号也会被
    拒。WorkBuddy 侧（wb_ui_server.DESKTOP_DIR 与 workbuddy_checkin.auth_dirs）
    一直带同样的回退，这里补齐对齐。
    """
    return os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")


def desktop_storage_paths():
    """本机 Trae 可能的 storage.json（按候选目录优先级）。"""
    appdata = _appdata_dir()
    res = []
    for d in tw.CANDIDATE_DIRS:
        p = Path(appdata) / d / "User" / "globalStorage" / "storage.json"
        if p.is_file():
            res.append(p)
    return res


def _desktop_storage():
    """本机 Trae 首个存在的 storage.json。"""
    lst = desktop_storage_paths()
    return lst[0] if lst else None


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _parse_storage(data):
    """从 storage.json dict 解析账号信息；未登录/无登录态返回 (None, key_error)。

    返回 (info, err)。info 含 nickname/token/uid/expires/refresh_token/device_id/
    enc_auth（用于写回的密文）。
    """
    enc = data.get(AUTH_KEY)
    if not enc:
        return None, "无登录态（%s）" % AUTH_KEY
    auth = None
    if isinstance(enc, dict):
        auth = enc
    elif isinstance(enc, str):
        try:
            auth = tw.decrypt_auth(enc)
        except Exception as e:
            return None, "登录态解码失败：%s" % e
    else:
        return None, "登录态格式无法识别"
    if not isinstance(auth, dict):
        return None, "登录态结构异常"
    token, region = tw.extract_token(auth)
    device_id = tw.extract_device_id_from_storage(data) if hasattr(
        tw, "extract_device_id_from_storage") else ""
    nickname = ""
    acct = auth.get("account") or {}
    if isinstance(acct, dict):
        nickname = str(acct.get("email") or acct.get("username") or "")
    if not nickname:
        try:
            uid_data = tw.account_uid(token)
            nickname = "id:%s" % uid_data
        except Exception:
            nickname = ""
    expired = auth.get("expiredAt")
    refresh_token = str(auth.get("refreshToken") or "")
    return {
        "nickname": nickname,
        "token": token,
        "region": region or "CN",
        "device_id": device_id,
        "refresh_token": refresh_token,
        "expires_at": expired,
        "enc_auth": enc,
        "auth": auth,
    }, ""


def _uid_of(info):
    """从账号 info 提取用户 uid（u<数字>），用于写后自校验。"""
    if not info or not info.get("token"):
        return ""
    try:
        return tw.account_uid(info["token"])
    except Exception:
        return ""


def _asset_kind(data):
    """判断 tw_auth 下的素材类型：'storage'（含加密登录态，可切换）/
    'tokens'（签到用 token 配置，仅展示）/ 'unknown'。"""
    if isinstance(data, dict) and data.get(AUTH_KEY):
        return "storage"
    if isinstance(data, dict) and isinstance(data.get("tokens"), list) and data["tokens"]:
        return "tokens"
    if isinstance(data, dict) and data.get("token"):
        return "tokens"
    return "unknown"


def _asset_token_entries(data):
    """从 tokens 类型素材提取 token 列表（用于展示，不可切换）。"""
    if isinstance(data, dict) and isinstance(data.get("tokens"), list):
        return data["tokens"]
    if isinstance(data, dict) and data.get("token"):
        return [data]
    return []


def list_accounts():
    """列出 tw_auth 目录下所有账号素材。

    每条带 file/ok/label/nickname/uid/expires_at/type。**uid 必须给**：
    两个切换器共用同一份前端模板，模板靠 uid 把「当前账号」映射回账号库里的文件
    （既用来显示积分/签到，也用来把当前账号从列表里滤掉）。wb 侧一直有 uid，
    tw 侧曾经没有 —— 结果是 Trae 页面里当前账号永远显示"不在账号库中"，
    并且列表里还会和当前账号重复出现一条。
    """
    out = []
    if not TW_AUTH_DIR.is_dir():
        return out
    for f in sorted(TW_AUTH_DIR.glob("*.json")):
        data = _read_json(f)
        if not isinstance(data, dict):
            out.append({"file": f.name, "path": str(f), "ok": False, "uid": "",
                        "type": "unknown", "label": f.stem, "nickname": f.stem,
                        "expires_at": None, "token_source": "",
                        "reason": "不是合法 JSON"})
            continue
        kind = _asset_kind(data)
        if kind == "storage":
            info, err = _parse_storage(data)
            if info is None:
                out.append({"file": f.name, "path": str(f), "ok": False, "uid": "",
                            "type": "storage", "label": f.stem, "nickname": f.stem,
                            "expires_at": None, "token_source": "", "reason": err})
                continue
            nick = info["nickname"] or f.stem
            ok = common.token_looks_complete(info["token"])
            out.append({
                "file": f.name, "path": str(f), "ok": ok, "type": "storage",
                "label": nick, "nickname": nick,
                "uid": _uid_of(info),
                "expires_at": info["expires_at"],
                "region": info["region"],
                "has_refresh": bool(info["refresh_token"]),
                "token_source": common.token_source(info["token"]),
                "reason": "" if ok else "登录态里的 token 不完整（疑似粘贴截断），切过去会 401",
            })
        elif kind == "tokens":
            entries = _asset_token_entries(data)
            if not entries:
                continue
            e = entries[0]
            label = str(e.get("label") or f.stem) if isinstance(e, dict) else f.stem
            out.append({
                "file": f.name, "path": str(f), "ok": False, "uid": "",
                "type": "tokens", "label": label, "nickname": label,
                "expires_at": None, "token_source": "",
                "reason": "签到用 token 配置，无加密登录态，不可切换；请放入该账号的 storage.json",
            })
        else:
            out.append({"file": f.name, "path": str(f), "ok": False, "uid": "",
                        "type": "unknown", "label": f.stem, "nickname": f.stem,
                        "expires_at": None, "reason": "无法识别素材格式"})
    return out


def current_account():
    """识别本机 Trae 当前账号。"""
    sp = _desktop_storage()
    if sp is None:
        return [{"ok": False, "nickname": "未发现 storage.json", "uid": "", "expires_at": None,
                 "reason": "未在本机找到 Trae 的 storage.json，请先登录 Trae 客户端"}]
    data = _read_json(sp)
    if not isinstance(data, dict):
        return [{"ok": False, "nickname": "无法读取", "uid": "", "expires_at": None,
                 "reason": "%s 不是合法 JSON，无法解析" % sp.name}]
    info, err = _parse_storage(data)
    if info is None:
        return [{"ok": False, "nickname": "未登录", "uid": "", "expires_at": None,
                 "reason": err}]
    ok = common.token_looks_complete(info["token"])
    # uid 与 wb 侧对齐（此前恒为空串，前端若按 uid 比对当前账号会拿不到值）
    return [{"file": sp.name, "ok": ok, "nickname": info["nickname"] or "未知",
             "uid": _uid_of(info), "expires_at": info["expires_at"], "region": info["region"],
             "reason": "" if ok else "当前登录态的 token 不完整，客户端可能会要求重新登录"}]


# 进程名里只要含 trae 就算命中（客户端改名/换版本后依然有效）；
# 排除本工具自身与 python，避免把自己当成 Trae
_TRAE_EXCLUDE = ("switcher", "python")


def _trae_running():
    """检测本机是否有 Trae 客户端进程在运行。返回命中的进程名列表。

    只用于给提示文案加一句「检测到客户端正在运行」，真正的判断靠写后自校验；
    所以这里宁可漏报也不误报，命令拿不到就返回空列表。
    """
    try:
        import subprocess
        # 中文 Windows 的 tasklist 输出是 GBK，不指定 errors 会在解码阶段直接炸，
        # 而且 stdout 会变成 None，后续 splitlines() 再抛 AttributeError
        res = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=10,
                             encoding="utf-8", errors="replace")
        out = res.stdout or ""
    except Exception:  # noqa: BLE001
        return []
    hits = []
    for line in out.splitlines():
        name = line.split(",")[0].strip('"')
        low = name.lower()
        if "trae" in low and not any(x in low for x in _TRAE_EXCLUDE):
            hits.append(name)
    return hits


def switch_account(file_name):
    """把 tw_auth\\<file> 的登录态切换为本机 Trae 正式登录态。

    读取目标素材的 icube.cloudide 密文及其附属 icube-dc 设备密钥，
    备份本机 storage.json 后整体写入；写后自校验是否真正生效。
    返回 (ok, message)。
    """
    base = os.path.basename((file_name or "").replace("\\", "/"))
    src = TW_AUTH_DIR / base
    if not src.is_file():
        return False, "目标账号文件不存在：%s" % base
    if not base.endswith(".json"):
        return False, "仅支持 .json 账号素材文件"
    data = _read_json(src)
    if not isinstance(data, dict) or not data.get(AUTH_KEY):
        return False, "素材缺少加密登录态（%s），无法切换；请放入该账号的 storage.json" % AUTH_KEY
    info, err = _parse_storage(data)
    if info is None:
        return False, "素材登录态无法解析：%s" % err
    if not common.token_looks_complete(info["token"]):
        return False, "素材登录态里的 token 不完整（疑似粘贴截断），切换后必然 401，请重新导出该账号的 storage.json"
    target_enc = info["enc_auth"]
    # 素材附属的设备密钥（可能有多个 icube-dc 键），切换时一并应用
    target_dc = {str(k): v for k, v in data.items()
                 if str(k).startswith("iCubeAuthInfo://icube-dc")}

    sp = _desktop_storage()
    if sp is None:
        return False, "未在本机发现 Trae storage.json，请先登录 Trae 客户端"

    # 「备份 + 覆盖写 + 自校验」必须串行，否则与另一次切号交叉会写坏 storage.json
    with common.file_lock("tw-storage", LOCK_DIR):
        cur = _read_json(sp)
        if not isinstance(cur, dict):
            return False, "本机 storage.json 无法读取，无法切换"
        # 当前登录态的 uid，用于写后自校验
        cur_info, _ = _parse_storage(cur)
        cur_uid = _uid_of(cur_info)
        target_uid = _uid_of(info)

        # 若 Trae 正在运行，明确提示（写后自校验仍会兜底）
        running = _trae_running()
        run_hint = ""
        if running:
            run_hint = "（检测到 Trae 客户端正在运行）"

        # 备份本机正式文件到独立备份目录（避免在原目录 rename，Trae 运行时可能拒绝 rename）
        now = datetime.datetime.now(datetime.timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H%M%SZ")
        backup_dir = _BIN_DIR / "tw_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / ("storage.%s.%d.json" % (ts, os.getpid()))
        raw = sp.read_bytes()
        try:
            backup.write_bytes(raw)
        except OSError as e:
            return False, "备份本机登录态失败：%s" % common.scrub(e)
        pruned = common.prune_backups(backup_dir, "storage.*.json", keep=TW_BACKUP_KEEP)

        # 写入目标账号登录态 + 附属设备密钥（其余键保留本机的）
        cur[AUTH_KEY] = target_enc
        for k, v in target_dc.items():
            cur[k] = v
        try:
            tmp = sp.with_name("storage.json.twtmp")
            tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=4), encoding="utf-8")
            # 优先原子替换；若 Trae 运行时拒绝 rename，退化为原位覆盖写原路径
            try:
                common.replace_with_retry(tmp, sp)
            except OSError:
                sp.write_bytes(tmp.read_bytes())
                tmp.unlink(missing_ok=True)
        except OSError as e:
            return False, "写入新登录态失败：%s" % common.scrub(e)

        # 写后自校验：确认登录态确实已落到文件并变成目标账号
        after = _read_json(sp)
        a_info, _ = _parse_storage(after) if after else (None, "")
        a_uid = _uid_of(a_info)
        if not a_uid or a_uid != target_uid:
            cur_nick = (a_info or {}).get("nickname") or "未知"
            return (False,
                    "本机 storage.json 未采纳新登录态（当前仍为 %s）%s。"
                    "Trae 客户端正在运行并锁定了配置文件，已中止切换。"
                    "请【完全退出 Trae 客户端】后重新点击切换。" % (cur_nick, run_hint))
        if cur_uid == target_uid:
            return True, "本机账号已是指定账号，无需切换"

    msg = "已切换为 %s%s。请【完全退出并重启 Trae 客户端】后生效" % (info["nickname"] or base, run_hint)
    if pruned:
        msg += "；已清理 %d 份旧备份（保留最近 %d 份）" % (pruned, TW_BACKUP_KEEP)
    return True, msg


def safe_info_name(name):
    name = (name or "").strip()
    if not name:
        return None, "缺少账号标识"
    if name.lower().endswith(".json"):
        name = name[:-5]
    name = re.sub(r"\s+", " ", name).strip()
    safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._ -]", "", name).strip()
    if not safe:
        return None, "账号名只能包含中英文、数字、空格、点和下划线"
    return "trae-%s.json" % safe, None


def add_account(name, content):
    """新增一个账号素材到 tw_auth\\（内容为前端粘贴的 storage.json 或上传文件）。"""
    fname, err = safe_info_name(name)
    if err:
        return False, err
    src = TW_AUTH_DIR / fname
    if src.exists():
        return False, "已存在同名账号文件：%s" % fname
    try:
        data = json.loads(content)
    except ValueError:
        return False, "内容不是合法 JSON，无法解析为账号文件"
    kind = _asset_kind(data)
    if kind != "storage":
        return False, "内容不是含加密登录态的 storage.json（%s），无法用于切换" % AUTH_KEY
    info, perr = _parse_storage(data)
    if info is None:
        return False, "内容不是有效的 Trae 登录态：%s" % perr
    if not common.token_looks_complete(info["token"]):
        return False, "登录态里的 token 不完整（疑似粘贴截断），请整份复制该账号的 storage.json 后重新添加"
    TW_AUTH_DIR.mkdir(parents=True, exist_ok=True)
    with common.file_lock("tw-auth-" + fname, LOCK_DIR):
        if src.exists():
            return False, "已存在同名账号文件：%s" % fname
        try:
            src.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
        except OSError as e:
            return False, "写入账号文件失败：%s" % e
    return True, "已新增账号 %s（%s）" % (info["nickname"] or name, fname)


def remove_account(file_name):
    base = os.path.basename((file_name or "").replace("\\", "/"))
    if base != base.replace("..", "") or base != (file_name or "").replace("\\", "/").split("/")[-1]:
        return False, "非法的文件名"
    if not base.endswith(".json"):
        return False, "只能删除 .json 账号素材文件"
    target = TW_AUTH_DIR / base
    if not target.is_file():
        return False, "账号文件不存在：%s" % base
    with common.file_lock("tw-auth-" + base, LOCK_DIR):
        try:
            target.unlink()
        except OSError as e:
            return False, "删除失败：%s" % e
    return True, "已删除账号素材 %s" % base


class _NullLog:
    def __init__(self):
        self._buf = []

    def __call__(self, msg):
        try:
            self._buf.append(str(msg))
        except Exception:
            pass


_NULLLOG = _NullLog()


def refresh_account_file(file_name):
    """对 tw_auth\\<file> 素材账号执行纯 HTTP 续期，更新 token 缓存。

    Trae 续期走 icube-dc 设备私钥 + refreshToken（见 trae_work_checkin.refresh_account）。
    续期结果写入 token_cache.json（脚本侧），不重写素材密文。
    """
    base = os.path.basename((file_name or "").replace("\\", "/"))
    if os.path.dirname((file_name or "").replace("\\", "/")):
        return False, "非法的文件路径"
    if not base.endswith(".json"):
        return False, "只能续期 .json 账号素材文件"
    target = TW_AUTH_DIR / base
    if not target.is_file():
        return False, "tw_auth 中不存在账号文件：%s" % base
    data = _read_json(target)
    if not isinstance(data, dict) or not data.get(AUTH_KEY):
        return False, "素材缺少加密登录态，无法续期"
    # 复用自动签到脚本的账号发现逻辑构造 account 结构
    acc = tw.account_from_storage(target)
    cfg = tw.load_config(Path(target.parent.parent) / "config.json") \
        if (Path(target.parent.parent) / "config.json").is_file() else tw.load_config(PROJECT_ROOT / "config.json")
    # script_dir 必须用 PROJECT_ROOT（自动签到）：trae_work_checkin 会把轮换后的
    # refreshToken 写进 script_dir/token_cache.json，此前传素材目录的父级（wb_switcher）
    # 等于另起一份缓存，与签到脚本各持一代 refreshToken —— 一方轮换后另一方的旧凭据即失效。
    with common.file_lock("tw-auth-" + base, LOCK_DIR):
        try:
            ok, msg, kind = tw.refresh_account(acc, PROJECT_ROOT, cfg, _NULLLOG)
        except Exception as e:
            return False, "续期异常：%s" % e
    return ok, msg


def _refresh_all_collect():
    """遍历素材目录各续期一次，返回 [(file, ok, msg)]。CLI 与 UI 共用。"""
    results = []
    for a in list_accounts():
        if not a.get("ok"):
            results.append((a["file"], False, a.get("reason") or "不可用，跳过"))
            continue
        ok, msg = refresh_account_file(a["file"])
        results.append((a["file"], ok, msg))
        common.audit(Handler.AUDIT_DIR, Handler.SOURCE, "refresh-all", a["file"], ok, msg)
    return results


def refresh_all():
    """对 tw_auth 下全部素材各续期一次，供计划任务调用。返回退出码。"""
    results = _refresh_all_collect()
    for f, ok, msg in results:
        print("%-4s %-32s %s" % ("OK" if ok else "FAIL", f, msg))
    print("---- 续期完成：%d/%d 成功" % (sum(1 for r in results if r[1]), len(results)))
    return 0 if all(r[1] for r in results) else 1


def refresh_all_ui():
    """一键续期（UI 按钮）。返回前端要的统一结构。"""
    results = _refresh_all_collect()
    ok_n = sum(1 for r in results if r[1])
    return {
        "ok": ok_n == len(results),
        "message": "一键续期完成：%d/%d 成功" % (ok_n, len(results)),
        "ok_count": ok_n,
        "total": len(results),
        "results": [{"file": f, "ok": ok, "message": msg} for f, ok, msg in results],
    }


class Handler(common.BaseHandler):
    """Trae 切换器的路由；HTTP 骨架与跨站校验见 switcher_common.BaseHandler。"""

    INDEX_NAME = "ui_template.html"   # 与 WorkBuddy 切换器共用同一份模板
    BASE_DIR = _BIN_DIR
    UI_CONTEXT = {
        "TITLE": "Trae 账号切换器",
        "LOGO": "&#10022;",
        "SUBTITLE": "一键切换桌面端登录账号 · 同机共用各账号 Trae 积分",
        "TIP": ("账号素材存放在 <b>tw_auth\\</b> 目录，每个文件是一份 <b>storage.json</b>"
                "（含该账号的加密登录态 <code>iCubeAuthInfo://icube.cloudide</code>）。"
                "切换时替换登录态键与其附属设备密钥，其它 IDE 配置保持不变。"),
        "AUTH_DIR": "tw_auth",
        "ACCEPT": ".json,application/json",
        "FILE_LABEL": "账号的 storage.json（含该账号加密登录态）",
        "ADD_HINT": "点击展开，选择文件或粘贴该账号的 storage.json 内容",
        "EMPTY_HINT": ("请放入该账号的 <code>storage.json</code>。"
                       "直接运行 exe 时，账号目录要放在 exe 同级（用 .cmd 启动就是本目录）。"),
        "CMD": "trae_switcher.cmd",
        "CREDITS": "",             # Trae 侧无 /api/credits，置空隐藏积分明细与「刷新积分」
        "CHECKIN": "",             # Trae 侧无 /api/checkin，置空隐藏签到状态与「一键签到」
        "MIGRATE": "",             # Trae 侧无账号数据迁移能力，置空隐藏迁移确认框
    }
    WRITE_ENDPOINTS = ("/api/switch", "/api/remove", "/api/refresh", "/api/add",
                       "/api/refresh-all")
    SOURCE = "tw"
    AUDIT_DIR = _BIN_DIR / "logs"
    AUDIT_ACTIONS = {"/api/switch": "switch", "/api/remove": "remove",
                     "/api/refresh": "refresh", "/api/add": "add",
                     "/api/refresh-all": "refresh-all"}
    APP_NAME = "tw_switcher"
    APP_VERSION = common.source_version(__file__, "tw")
    STARTED_AT = int(time.time())

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
        elif u.path == "/api/refresh-all":
            return 200, refresh_all_ui()
        else:
            return 404, {"ok": False, "message": "404"}
        return 200, {"ok": ok, "message": msg}


def serve(port=DEFAULT_PORT, open_browser=True, use_token=True):
    # 单实例保护：互斥体名带 tw，不会与 WorkBuddy 侧互相挤掉。见 DESIGN_single_instance.md。
    handle, action, info = common.single_instance_guard(
        common.MUTEX_NAME_TW, port, tries=common.PORT_TRIES, log=print,
        default_port=DEFAULT_PORT)
    if action == "reuse":
        return common.report_reuse(info, open_browser, Handler.APP_VERSION, DEFAULT_PORT)
    if action == "abort":
        return common.report_abort(port)

    # 一次性令牌：只存在于本次进程，随首页注入给前端，写操作必须回传
    Handler.TOKEN = common.new_token() if use_token else None
    requested = port          # 记下"请求的端口"，下面要拿它比，不能用默认端口常量
    server, port = common.bind_server(Handler, port)
    if port != requested:
        print("[提示] 默认端口 %d 被其它程序占用，已改用 %d（本实例唯一）" % (DEFAULT_PORT, port), flush=True)
    url = "http://127.0.0.1:%d/" % port
    print("本地服务已启动：%s  (Ctrl+C 停止)" % url, flush=True)
    if open_browser:
        # 用 open_page 而不是裸 webbrowser.open：只有"当前没有页面开着"才开，
        # 否则每次运行都会多一个指向同一地址的标签页。
        threading.Timer(0.6, lambda: common.open_page(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main():
    ap = argparse.ArgumentParser(description="Trae 账号切换器后端")
    ap.add_argument("--list", action="store_true", help="列出 tw_auth 可用账号")
    ap.add_argument("--current", action="store_true", help="查看本机 Trae 当前账号")
    ap.add_argument("--switch", metavar="NAME", help="切换为 tw_auth\\NAME 账号")
    ap.add_argument("--serve", action="store_true", help="启动本地 HTTP 服务")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="HTTP 服务端口（默认 %d，被占用时自动顺延）" % DEFAULT_PORT)
    ap.add_argument("--prune", metavar="N", type=int, nargs="?", const=TW_BACKUP_KEEP,
                    help="清理 tw_backups 备份，只保留最近 N 份（默认 %d）" % TW_BACKUP_KEEP)
    ap.add_argument("--no-open", action="store_true",
                    help="只跑服务，不打开浏览器（脚本/测试用；正常双击启动脚本不需要）")
    ap.add_argument("--no-auth", action="store_true",
                    help="关闭一次性访问令牌（写操作将只依赖回环 + 同源校验）")
    ap.add_argument("--refresh-all", action="store_true",
                    help="对 tw_auth 下全部素材各续期一次（供计划任务调用）")
    args = ap.parse_args()

    if args.prune is not None:
        n = common.prune_backups(_BIN_DIR / "tw_backups", "storage.*.json",
                                 keep=max(0, int(args.prune)))
        print("已清理 %d 份 tw_backups 备份（保留最近 %d 份）" % (n, args.prune))
        common.audit(Handler.AUDIT_DIR, Handler.SOURCE, "prune",
                     "keep=%s" % args.prune, True, "清理 %d 份" % n)
        return 0

    if args.refresh_all:
        return refresh_all()

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
    return serve(port=args.port, use_token=not args.no_auth,
                 open_browser=not args.no_open) or 0


if __name__ == "__main__":
    sys.exit(main())