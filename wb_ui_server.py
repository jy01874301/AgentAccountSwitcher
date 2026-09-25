"""wb_ui_server.py —— WorkBuddy 账号切换小工具的本地后端。

一个进程同时服务**两个客户端通道**（详见 Channel 类）：

| 通道   | 客户端      | 服别   | 登录态文件                  | 账号库        | 接口前缀      |
|--------|-------------|--------|-----------------------------|---------------|---------------|
| `wb`   | WorkBuddy   | 国服   | `workbuddy-desktop.info`    | `wb_auth\\`   | `/api/*`      |
| `wbai` | WorkBuddyAI | 国际服 | `workbuddy-desktop-ai.info` | `wbai_auth\\` | `/api/wbai/*` |

两个登录态文件放在**同一个目录**、格式相同，只有文件名与域名不同，因此每处读写都要
按 `root_id` 过滤（见 Channel.root_id）。

提供两个职责：
1. 纯函数：列出账号库里的可用账号、把选中账号切换为该通道的桌面端正式登录态
   （`%LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\` 下）。
2. 极简 HTTP 服务：供前端 HTML 页面调用（仅绑定 127.0.0.1，局域网不可访问）。
   三个页面：`/`（统一入口页，侧边栏两个管理入口）、`/wb`、`/wbai`。

复用 workbuddy_checkin.py 的解析函数（session_from_info_file / split_info_name）。

用法：
  python wb_ui_server.py --list          # 列出 wb_auth 可用账号（JSON）
  python wb_ui_server.py --current       # 查看桌面端当前账号（JSON）
  python wb_ui_server.py --switch NAME   # 切换为 wb_auth\\NAME.info 账号
  python wb_ui_server.py --serve [--port 8765]  # 启动本地 HTTP 服务（端口占用自动顺延）
  python wb_ui_server.py --prune [N]    # 清理桌面端切号备份，只留最近 N 份（默认 10）

  以上命令都接受 `--channel wbai` 指到国际服通道（默认 wb = 国服），例如：
  python wb_ui_server.py --channel wbai --list
  python wb_ui_server.py --channel wbai --switch workbuddyai-xxx.info

  国际服没有的能力（`--checkin` / 迁移）会明确拒绝，而不是拿国服的逻辑去动国际服的数据。
  `--refresh-all` 两个通道都支持 —— 但**网关不同**（国服 copilot.tencent.com /
  国际服 www.workbuddy.ai），由 `channel_cfg(ch)` 按通道给。
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs
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
import account_migration as migration  # 切号时把本地数据改归属到新账号（见 DESIGN_account_migration.md）
import live_token  # 登录态文件被字段加密时，从客户端进程内存读取当前凭据（见 live_token.py）

# 复用上级项目的解析/续期逻辑：先声明需要的成员，缺失时给出可读提示而不是裸 Traceback
wb = common.require_module(
    "workbuddy_checkin",
    ("session_from_info_file", "split_info_name", "refresh_account", "load_config",
     "parse_jwt_payload", "AUTH_REL_DIR", "LOGOUT_MARKER_SUFFIX"),
    who="wb_ui_server.py")

# 账号库目录固定为本工具所在目录（本工具独立存放账号配置），
# 与 PROJECT_ROOT（复用自动签到项目的解析/续期逻辑）解耦，避免指向兄弟项目。
SCRIPT_DIR = PROJECT_ROOT
def _localappdata_dir():
    r"""%LOCALAPPDATA% 目录；变量缺失**或为空串**时回退到 ~/AppData/Local。

    注意必须用 `or` 而不是 `os.environ.get(k, default)`：环境变量存在但为空串时
    get() 返回 ""，Path("") 会解析成当前工作目录，于是去 ./CodeBuddyExtension/... 找
    登录态 —— 表现为「当前账号为空」而不报任何错。Trae 侧的 %APPDATA% 踩过同一个坑。
    """
    return os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")


LOGOUT_SUFFIX = wb.LOGOUT_MARKER_SUFFIX  # ".logged-out"
LOCK_DIR = _BIN_DIR / ".locks"      # 跨进程锁文件（不放进客户端配置目录）
DESKTOP_BACKUP_KEEP = 10            # 桌面端切号备份保留份数（备份本身是有效登录态）
DEFAULT_PORT = 8765
# 两个客户端共用**同一个**登录态目录（%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth），
# 只是文件名不同 —— 这正是必须按 root_id 过滤的原因。
DESKTOP_DIR_BASE = Path(_localappdata_dir()) / wb.AUTH_REL_DIR


class Channel:
    """一个「客户端通道」：一套登录态文件 + 一个账号库 + 一份界面文案 + 一组可用能力。

    本机同时装着两个客户端，登录态文件**格式相同、目录相同**，只是文件名与域名不同：

    | 通道   | 客户端      | 服别   | 登录态文件                  | 账号库        |
    |--------|-------------|--------|-----------------------------|---------------|
    | `wb`   | WorkBuddy   | 国服   | `workbuddy-desktop.info`    | `wb_auth\\`   |
    | `wbai` | WorkBuddyAI | 国际服 | `workbuddy-desktop-ai.info` | `wbai_auth\\` |

    读写逻辑完全共用，只在下面这几个常量上分叉 —— 于是「加一个通道」= 加一份配置，
    而不是复制一份 switch_account。
    """

    def __init__(self, key, title, server, domain, auth_dir, info_name,
                 account_prefix, lock_key, source, features, endpoint=None,
                 client_processes=(), client_exe="", client_dirs=()):
        self.key = key
        self.title = title            # 页面标题 / 侧边栏入口名
        self.server = server          # 服别文案：国服 / 国际服
        self.domain = domain          # 该客户端登录态里的 auth.domain
        self.auth_dir_name = Path(auth_dir).name   # 只存目录名，路径见 auth_dir 属性
        self.info_name = info_name    # 桌面端正式登录态文件名
        self.account_prefix = account_prefix   # 新增账号时的文件名前缀
        self.lock_key = lock_key      # 切号用的文件锁名（两个通道必须不同）
        self.source = source          # 审计日志来源标记
        self.features = features      # 能力开关：credits / checkin / migrate / refresh
        # 客户端进程名 / 主 exe 名 / 常见安装目录名 —— **必须按通道给**。
        # 两个客户端是**两个不同的程序**，本机都装着、都在跑：
        #   国服  C:\Program Files\WorkBuddy\WorkBuddy.exe      (workbuddy.exe)
        #   国际服 C:\Program Files\WorkBuddyAI\WorkBuddyAI.exe  (workbuddyai.exe)
        # 早先这里是模块级单值常量 `CLIENT_PROCESS = "workbuddyai.exe"`，于是
        # 国服的迁移流程会去关/开**国际服**客户端，而国服客户端全程没人管、
        # 迁移时仍持有自己的 workbuddy.db —— 属于"看起来成功、实际动错程序"的一类。
        self.client_processes = tuple(client_processes)   # 进程名（小写），可多个（含历史名）
        self.client_exe = client_exe                      # 启动用的主 exe 名
        self.client_dirs = tuple(client_dirs)             # 安装目录候选名
        # 计费/续期网关。None = 沿用上级 config.json 里的默认值（国服 copilot.tencent.com）。
        # **两个通道不是同一个网关**：拿国际服的 accessToken 去打国服网关会得到 HTTP 401
        # （实测），页面上就是每行「积分接口 HTTP 401」。见 channel_cfg()。
        self.endpoint = endpoint
        self.ui_context = {}          # 由 build_ui_context() 填充
        # 桌面端目录是**实例属性**而不是模块常量：自检必须能把它重定向到临时目录，
        # 否则它会真的去切用户本机的登录态（重构时踩过一次，见 smoke_test 的说明）。
        self.desktop_dir = DESKTOP_DIR_BASE

    def redirect(self, desktop_dir, info_name=None):
        """把该通道的桌面端目录指到别处（自检 / 特殊部署用）。

        只改这一个通道的实例状态，不碰模块级常量，也不影响另一个通道。
        """
        self.desktop_dir = Path(desktop_dir)
        if info_name:
            self.info_name = info_name
        return self

    # --- 路径 -----------------------------------------------------------
    @property
    def auth_dir(self):
        """账号库目录。**每次访问按模块级 `_BIN_DIR` 现算，不在这里存快照。**

        冻结态（PyInstaller 打包）启动时，`ui_app` 会把 `_BIN_DIR` 改指到
        exe 所在目录，好让「账号库与 exe 同级」这个部署约定成立。若在 `__init__`
        里把路径存成快照，那次改写就落不到已经建好的 Channel 上 ——
        表现是**打包后的 exe 跑去 `_internal\\wb_auth\\` 找账号，页面恒显示 0 个，
        而源码运行一切正常**（双通道重构时踩过，见 smoke_test 的回归断言）。
        """
        return _BIN_DIR / self.auth_dir_name

    @property
    def desktop_info(self):
        return self.desktop_dir / self.info_name

    @property
    def root_id(self):
        """正式登录态的 stem，用来把**另一个通道**的文件排除在外。

        同目录下还躺着对方的 *.info；不按 root_id 过滤的话，「当前账号」取的是
        mtime 最新的一个，对方一写文件就会把这一侧显示的账号顶掉 ——
        页面显示成别人的账号、切换按钮永远变不成"当前账号"。
        """
        return Path(self.info_name).stem

    @property
    def backup_glob(self):
        return "%s.*.info" % self.root_id

    @property
    def backup_prefix(self):
        """切号备份的文件名前缀（与客户端 clean() 的命名一致）。"""
        return self.root_id + "."

    @property
    def logout_marker_name(self):
        return self.info_name + LOGOUT_SUFFIX

    @property
    def api_base(self):
        """该通道在 HTTP 上的接口前缀。国服沿用历史路径 /api/*，其余走 /api/<key>/*。"""
        return "/api/" if self.key == DEFAULT_CHANNEL else "/api/%s/" % self.key

    def supports(self, feature):
        return bool(self.features.get(feature))


DEFAULT_CHANNEL = "wb"

CHANNELS = {
    "wb": Channel(
        key="wb",
        title="WorkBuddy 账号管理",
        server="国服",
        domain="www.workbuddy.cn",
        auth_dir=_BIN_DIR / "wb_auth",
        info_name="workbuddy-desktop.info",
        account_prefix="workbuddy-",
        lock_key="wb-desktop",
        source="wb",
        # open_client 指的是 **/api/<key>/open-client 接口**与「打开客户端」按钮是否可用。
        # 页面上有两处按钮，各自独立控制（见 build_ui_context）：
        #   导航栏那个 → OPEN_CLIENT（国服/国际服按用户要求都不显示，Trae 显示）
        #   当前账号行那个 → OPEN_CLIENT_ROW（2026-09-21 起两个通道都显示）
        # 迁移流程内部直接调 open_client(ch) **函数**，不受这些开关影响。
        features={"credits": True, "checkin": True, "migrate": True, "refresh": True,
                  "open_client": True},
        # None = 沿用上级 config.json 的 endpoint（国服 copilot.tencent.com）
        endpoint=None,
        # 国服客户端的进程/exe 名。codebuddy.exe 是它的历史进程名，一并认。
        client_processes=("workbuddy.exe", "codebuddy.exe"),
        client_exe="WorkBuddy.exe",
        client_dirs=("WorkBuddy",),
    ),
    "wbai": Channel(
        key="wbai",
        title="WorkBuddyAI 账号管理",
        server="国际服",
        domain="www.workbuddy.ai",
        auth_dir=_BIN_DIR / "wbai_auth",
        info_name="workbuddy-desktop-ai.info",
        account_prefix="workbuddyai-",
        lock_key="wbai-desktop",
        source="wbai",
        # 国际服与国服**不是同一个计费网关**：国际服的积分/续期/签到接口在
        # `https://www.workbuddy.ai`（实测拿国际服 accessToken 打国服的
        # copilot.tencent.com 恒返回 HTTP 401；换成国际服域名后
        # /v2/billing/meter/get-user-resource 正常返回积分明细）。
        # 因此 endpoint 必须按通道给，否则页面上每行都是「积分接口 HTTP 401」。
        #
        # credits / refresh：接口已验证可用，与国服一致。
        # checkin：接口**可达**但活动未开启（实测 active=false「签到活动未开启」），
        #   且用户明确要求不迁移签到 → 保持关闭，页面上不出现签到入口。
        # migrate：2026-09-21 起**开放**。数据目录不是障碍 ——
        #   account_migration.find_data_root() 的候选里本就有 `.workbuddy-ai`，
        #   且能按 uid 挑对；之前的障碍是客户端进程名不分通道（见上一条注释），
        #   现在 client_processes 按通道给，国际服关/开的就是国际服客户端。
        # open_client：2026-09-21 起**开放**。进程名已经按通道区分（client_processes），
        #   /api/wbai/open-client 拉起来的就是 WorkBuddyAI.exe 自己，不会再误开国服客户端
        #   —— 当初把它关掉的唯一理由已经不存在。当前账号行的「打开客户端」按钮靠它。
        features={"credits": True, "checkin": False, "migrate": True, "refresh": True,
                  "open_client": True},
        endpoint="https://www.workbuddy.ai",
        client_processes=("workbuddyai.exe",),
        client_exe="WorkBuddyAI.exe",
        client_dirs=("WorkBuddyAI",),
    ),
}


def channel(key=None):
    """按 key 取通道。未知 key 回落到国服 —— HTTP 路由层只放行白名单里的 key。

    允许直接传 Channel 对象，这样内部函数可以互相传递，不必到处做类型判断。
    """
    if isinstance(key, Channel):
        return key
    return CHANNELS.get(key) or CHANNELS[DEFAULT_CHANNEL]


def channel_cfg(ch=None):
    """按通道取计费/续期网关配置：endpoint 由通道决定，其余沿用上级 config.json。

    ⚠️ 这是**唯一**该读网关配置的入口。四个调用点（积分、签到状态、签到、续期）
    以前各写一行 `wb.load_config(SCRIPT_DIR / "config.json")` —— 那样拿到的永远是
    国服网关，国际服的 accessToken 打上去恒 401。加通道时最容易漏改的也正是这种
    「每处各写一遍」的取配置方式，所以统一收敛到这里。

    不直接改 `os.environ["WORKBUDDY_ENDPOINT"]`：那是进程级的，两个通道在一个进程里
    同时服务，设了就会把国服也一起改掉。
    """
    ch = channel(ch)
    cfg = dict(wb.load_config(SCRIPT_DIR / "config.json") or {})
    if ch.endpoint:
        cfg["endpoint"] = ch.endpoint
    return cfg


# 模块级路径常量一个都不留（AUTH_DIR / DESKTOP_DIR / DESKTOP_INFO / DESKTOP_ROOT_ID
# 全部已删除）：它们都是**导入时的快照**，而冻结态 exe 恰恰要在导入之后改基准目录。
# 留一个这样的名字，就等于留一个「改了不生效」的坑 ——
# 前两轮分别因此误切过本机登录态、以及让打包后的 exe 找不到账号库。
# 一律走 `channel(key).auth_dir` / `channel(key).desktop_info` 取活路径。


def _read_info(path):
    """读取一个 .info 文件并解析，返回 dict；结构不符返回 None。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ensure_auth_dirs():
    """确保**每个通道**的账号目录都存在，并在空目录里放一份说明文件。

    为什么要在代码里建目录而不是靠打包带上：`dist/` 是构建产物，每次重新打包都会
    被整个重建，塞进去的空目录必然丢失；而部署时用户只拷 exe，不会手动建目录。
    结果是页面显示「0 个账号」，用户不知道该往哪放文件 —— 看着像工具坏了。

    放在 `list_accounts` / `serve` 的入口调用：只读命令（`--list`）也能顺手把
    目录建出来，用户拷走 exe 跑一次就能看到该往哪放。
    """
    for key in CHANNELS:
        ch = CHANNELS[key]
        try:
            ch.auth_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        hint = ch.auth_dir / "把账号文件放这里.txt"
        if not any(ch.auth_dir.glob("*")):
            try:
                hint.write_text(
                    "把 %s（%s）的账号登录态文件放进本目录。\n"
                    "\n"
                    "  文件名示例：%s张三.info\n"
                    "\n"
                    "导出方式：在客户端登录该账号后，把它的 %s\n"
                    "复制进本目录、改个名字即可。\n"
                    "\n"
                    "本文件只是占位提示，账号放进来之后可以删掉。\n"
                    % (ch.title, ch.server, ch.account_prefix, ch.info_name),
                    encoding="utf-8")
            except OSError:
                pass


# `list_accounts` 的进程内缓存：**签名 = 目录里所有 .info 的 (名, mtime_ns, 大小)**，
# 签名没变就复用上次的解析结果。这是热路径 —— /api/accounts、credits_snapshot、
# checkin_snapshot、uid_nickname_map 每一轮刷新都会调它，而每个账号要读盘 + 解析
# **两次**（session_from_info_file + _read_info），N 个账号就是 2N 次
# （见 AUDIT_2026-09-24.md P2-7）。
_LIST_ACCOUNTS_CACHE = {}
_LIST_ACCOUNTS_LOCK = threading.Lock()


def _auth_dir_sig(d):
    """目录签名：内容一变签名就不同。取不到（目录不存在 / 被占用）返回 None → 不走缓存。"""
    try:
        return tuple(sorted((f.name, f.stat().st_mtime_ns, f.stat().st_size)
                            for f in d.glob("*.info")))
    except OSError:
        return None


def list_accounts(ch=None):
    """列出该通道账号库目录下的所有可用账号。

    优先返回 .info 文件（含切号备份的文件）。每个条目带文件路径与令牌脱敏摘要；
    accessToken 不完整的（如粘贴时被截断）标记为不可用，避免切过去才发现是废凭据。

    结果按「目录签名」在进程内复用（见上面的 _LIST_ACCOUNTS_CACHE）——
    签名由文件名 + mtime_ns + 大小组成，账号库有任何增删改都会立刻失效。
    """
    ch = channel(ch) if not isinstance(ch, Channel) else ch
    out = []
    ensure_auth_dirs()
    if not ch.auth_dir.is_dir():
        return out

    sig = _auth_dir_sig(ch.auth_dir)
    if sig is not None:
        with _LIST_ACCOUNTS_LOCK:
            hit = _LIST_ACCOUNTS_CACHE.get(ch.key)
        if hit and hit["sig"] == sig:
            return [dict(x) for x in hit["items"]]      # 浅拷贝：别让调用方改脏缓存

    for f in sorted(ch.auth_dir.glob("*.info")):
        acc = wb.session_from_info_file(f)
        raw = _read_info(f) or {}
        if not acc:
            # 无法解析的 .info 仍列出，标注不可用
            account = raw.get("account") if isinstance(raw, dict) else None
            name = account.get("nickname") if isinstance(account, dict) else None
            if not isinstance(name, str) or not name:
                # 昵称本身也被加密了（5.6.2+ 字段保护）→ 退到明文 uid 的前 8 位
                name = plain_uid(raw)[:8] or None
            encrypted = info_is_encrypted(raw)
            out.append({
                "file": f.name,
                "path": str(f),
                "ok": False,
                "encrypted": bool(encrypted),
                "label": name or f.stem,
                "nickname": name or f.stem,
                "uid": plain_uid(raw),
                "expires_at": None,
                "refresh_expires_at": None,
                "token_source": "",   # 字段必须齐：前端 ttlTag() 会读它
                "ttl_days": None,    # 同上：token_source 缺失时用它兜底
                "reason": ENCRYPTED_REASON if encrypted
                          else "登录态无法解析（内容可能不全或被加密）",
            })
            continue
        entry = {
            "file": f.name,
            "path": str(f),
            "ok": True,
            "encrypted": False,
            "label": acc["label"],
            "nickname": acc["nickname"],
            "uid": acc["uid"],
            "expires_at": acc["expires_at"].isoformat() if acc["expires_at"] else None,
            "refresh_expires_at": acc["refresh_expires_at"].isoformat() if acc["refresh_expires_at"] else None,
            "auth_id": acc["auth_id"],
        }
        at = (raw.get("auth") or {}).get("accessToken")
        # 签发通道决定有效期长短：oneid_login 55 天，enterprise_switch 30 天
        entry["token_source"] = common.token_source(at)
        # 兜底：签发方不打 token_source 标时（国服部分账号、国际服全部账号），
        # 用 exp-iat 给前端一个真实的签发天数，否则那些账号的标签会整块消失。
        entry["ttl_days"] = common.jwt_ttl_days(at)
        if not common.token_looks_complete(at):
            entry["ok"] = False
            entry["reason"] = "accessToken 不完整（疑似粘贴截断），切过去会 401，请重新导出该账号文件"
        out.append(entry)
    if sig is not None:
        with _LIST_ACCOUNTS_LOCK:
            _LIST_ACCOUNTS_CACHE[ch.key] = {"sig": sig, "items": [dict(x) for x in out]}
    return out


def _mtime_with_retry(path, tries=3, delay=0.05):
    """取文件 mtime，被杀软/索引器短暂占用时重试几次。

    Windows 上刚写完的文件可能被以独占方式持有句柄，此时 `stat()` 抛
    `PermissionError [WinError 5]`。这与 account_migration._replace 是同一类坑
    —— 那边靠重试解决，这边也一样。全部失败返回 None，由调用方决定怎么办。
    """
    for i in range(int(tries)):
        try:
            return Path(path).stat().st_mtime
        except OSError:
            if i + 1 < int(tries):
                time.sleep(delay * (i + 1))
    return None


# --- 客户端「静态字段保护」兼容层（2026-09-23 新增） ------------------------
# WorkBuddy 5.6.2 起启用了 at-rest 字段保护（ProtectedJsonFields），登录态文件里
# accessToken / refreshToken / nickname / phoneNumber 等敏感字段不再是明文字符串，
# 而是被替换成包装对象：
#     {"$wbEncrypted": 1, "envelope": "<base64>"}
# envelope 解开后是 {"suite":1,"keyId":...,"nonce":...,"authTag":...,"ciphertext":...}
# 的 JSON，内容用 AES-256-GCM 加密。密钥（at-rest key）由客户端运行时注入：
# app.asar 里只有算法和 keyblob 读写逻辑，**没有密钥载荷**，本地也没有第二份副本，
# 所以本工具**无法解密这些字段**。
#
# 能做的两件事：
#   1. account.uid / account.uin 仍是明文 → 可据此在账号库里反查昵称，
#      让「当前账号」至少答得出"是谁"，而不是一句"未登录"（见 uid_nickname_map）。
#   2. 客户端**读取时兼容明文**（明文只会被标记为待迁移并重新加密，不影响使用）
#      → 本工具写回明文登录态来切号依然有效，切号功能不受影响。
#
# 症状背景：加密生效后，session_from_info_file() 因为 accessToken 不是 "eyJ" 开头的
# 字符串而返回 None，页面就显示「桌面端当前未登录」，即使客户端明明登录着。

def is_encrypted_field(value):
    """是否为客户端字段保护包装（{"$wbEncrypted":1,"envelope":...}）。"""
    return isinstance(value, dict) and value.get("$wbEncrypted") == 1


def info_is_encrypted(raw):
    """登录态里是否出现字段保护包装（说明凭据已无法直接读取）。"""
    if not isinstance(raw, dict):
        return False
    checks = (((raw.get("auth") or {}), ("accessToken", "refreshToken")),
              ((raw.get("account") or {}), ("nickname", "phoneNumber")))
    for holder, keys in checks:
        if isinstance(holder, dict):
            for k in keys:
                if is_encrypted_field(holder.get(k)):
                    return True
    return False


def plain_uid(raw):
    """取出登录态里的明文 uid（字段保护不覆盖 uid），取不到返回 ""。"""
    account = raw.get("account") if isinstance(raw, dict) else None
    uid = account.get("uid") if isinstance(account, dict) else None
    return uid if isinstance(uid, str) else ""


_UID_NICK_CACHE = {}          # ch.key -> {"sigs": (auth_sig, desktop_sig), "map": {...}}
_UID_NICK_LOCK = threading.Lock()


def uid_nickname_map(ch):
    """建 uid -> 昵称 映射，供加密登录态降级显示用。

    加密文件自身读不出昵称，只能反查。数据源按可信度排序：
    账号库（本工具维护，明文）优先，桌面端目录里的明文备份兜底 ——
    切号会留下明文备份，通常正好是被加密那一份的旧副本。

    ⚠️ 它会把**两个目录的全部 .info 各读一遍**，而调用方是每次刷新都要跑的渲染路径，
    所以结果同样按「两个目录的签名」在进程内复用（见 AUDIT_2026-09-24.md P2-7）。
    """
    sigs = (_auth_dir_sig(ch.auth_dir), _auth_dir_sig(ch.desktop_dir))
    if all(s is not None for s in sigs):
        with _UID_NICK_LOCK:
            hit = _UID_NICK_CACHE.get(ch.key)
        if hit and hit["sigs"] == sigs:
            return dict(hit["map"])

    mapping = {}
    for src in (ch.auth_dir, ch.desktop_dir):
        try:
            files = sorted(src.glob("*.info"))
        except OSError:
            continue
        for f in files:
            raw = _read_info(f)
            if not isinstance(raw, dict):
                continue
            uid = plain_uid(raw)
            if not uid or uid in mapping:
                continue
            account = raw.get("account") or {}
            nick = account.get("nickname") if isinstance(account, dict) else None
            if isinstance(nick, str) and nick:
                mapping[uid] = nick
    if all(s is not None for s in sigs):
        with _UID_NICK_LOCK:
            _UID_NICK_CACHE[ch.key] = {"sigs": sigs, "map": dict(mapping)}
    return mapping


ENCRYPTED_REASON = "凭据已被客户端加密（WorkBuddy 5.6.2+ 字段保护），本工具读不到 token"
LIVE_REASON = "凭据读自客户端进程内存（登录态文件已被加密，内存里有客户端在用的明文 token）"


def live_session(ch, expect_uid=None, want_refresh=False):
    """尝试从**客户端进程内存**读出当前登录凭据。

    文件里的 token 被字段加密读不出来，但客户端自己要用明文 token 调接口 ——
    所以进程内存里一定有一份（实测 accessToken 长度/payload 与账号库明文完全一致）。
    只读、不落盘。任何失败（客户端没跑 / 权限不足 / 扫不到）都返回 None，调用方降级。

    `want_refresh=False`（默认）时扫描**拿到 access 就停** —— 昵称/到期时间/签发天数/
    积分/签到全都只需要 access，不必为 refresh 多扫几个进程。只有诊断接口
    `/api/live-token` 与 CLI `--live-token` 传 True（它们要把 refresh 一并展示）。
    """
    try:
        return live_token.read_live_session(ch, expect_uid=expect_uid or None,
                                           want_refresh=want_refresh)
    except Exception:
        # 内存扫描全是 Win32 调用，绝不让它冒到 HTTP 层
        return None


def redact_session(session):
    """把 live session 的 token 换成摘要，供 CLI / HTTP 返回（别把凭据写进日志）。"""
    if not session:
        return None
    out = {k: v for k, v in session.items()
           if k not in ("access_token", "refresh_token")}
    for k in ("access_token", "refresh_token"):
        t = session.get(k) or ""
        out[k] = ("%s…（%d 字符）" % (t[:16], len(t))) if t else ""
    for k in ("expires_at", "refresh_expires_at"):
        v = session.get(k)
        if v is not None:
            out[k] = v.isoformat()
    return out


def current_account(ch=None):
    """识别该通道客户端当前使用的账号。

    桌面目录可能只有正式文件，也可能因切号遗留 <root>.<时间戳>.<pid>.<uuid>.info 备份
    （备份本身是有效登录态，客户端平滑切号时用它恢复会话）。因此这里把所有 .info 都解析出来，
    去掉登出标记的，并按修改时间降序，把最新会话标记为 current=true 作为当前账号。
    """
    ch = channel(ch)
    entries = []
    nick_map = None   # 惰性构建：只有真的遇到加密登录态才去扫账号库
    for f in ch.desktop_dir.glob("*.info"):
      # 整个条目包在 try 里：一个文件读不到（被杀软/索引器/客户端 watcher 占用）
      # 不该让整张表、乃至整个切号流程 500。以前 f.stat() 是裸的，
      # 实测因此偶发 PermissionError [WinError 5]（2 天 11 次，见 AUDIT_2026-09-19.md）。
      try:
        root_id, is_backup = wb.split_info_name(f.name)
        if not root_id:
            continue
        # 只认本通道接管的那一份（workbuddy-desktop + 它的切号备份）。
        # 同目录下还有**另一个通道**的文件（如国服侧会看到 workbuddy-desktop-ai.*），
        # 那是另一个客户端的登录态，由它自己的通道去管，本通道只读自己的。
        # 此前不过滤，而"当前账号"取的是 mtime 最新的一个，于是对方一写文件
        # 就会把当前账号顶掉：页面显示成别人的账号，切换按钮也永远变不成
        # "当前账号"，看起来像切换失败。
        if root_id != ch.root_id:
            continue
        if (ch.desktop_dir / ch.logout_marker_name).exists():
            continue  # 该 id 会话已登出
        acc = wb.session_from_info_file(f)
        raw = _read_info(f) or {}
        # 加密登录态（5.6.2+ 字段保护）：凭据读不出来，但 uid 还是明文，
        # 拿它在账号库里反查昵称 —— 否则这一行会退化成文件名。
        encrypted = acc is None and info_is_encrypted(raw)
        live = None
        if encrypted:
            if nick_map is None:
                nick_map = uid_nickname_map(ch)
            # 文件里读不出 token，但客户端自己要用明文 token 调接口，
            # 进程内存里必然有一份 —— 只读地取来，好让这一行仍有昵称与剩余天数。
            # （live_token 内部有 60 秒缓存，不会每次轮询都扫一遍内存。）
            live = live_session(ch, plain_uid(raw))
        # token_source 让「当前账号」也能显示通道标签，和账号列表里的一致
        raw_at = (raw.get("auth") or {}).get("accessToken")
        mtime = _mtime_with_retry(f)
        if mtime is None:
            # 一直被占用。**不能直接丢掉**：正式文件正常情况下就是最新的那个，
            # 丢掉它会让一个旧备份顶上来当"当前账号"，页面上就显示成别人的账号了。
            # 所以给正式文件一个"现在"的兜底 mtime（必然大于所有已有文件）让它保持第一，
            # 备份则给 0 排到最后。
            # 注意不能用 float("inf")：mtime 会进 JSON，Infinity 不是合法 JSON，
            # 浏览器 JSON.parse 会直接抛错。
            mtime = time.time() if f.name == ch.info_name else 0.0
        at = raw_at if acc else ((live or {}).get("access_token") or "")
        if acc:
            nick, uid_val, exp = acc["nickname"], acc["uid"], acc["expires_at"]
        elif encrypted and live:
            # 内存里读到了明文凭据：昵称、有效期都能给全，只差"文件本身读不出来"这件事
            uid_val = live.get("uid") or plain_uid(raw)
            nick = (live.get("nickname") or (nick_map or {}).get(uid_val)
                    or (uid_val[:8] if uid_val else root_id))
            exp = live.get("expires_at")
        elif encrypted:
            uid_val = plain_uid(raw)
            nick = (nick_map or {}).get(uid_val) or (uid_val[:8] if uid_val else root_id)
            exp = None
        else:
            nick, uid_val, exp = root_id, "", None
        entries.append({
            "file": f.name,
            "ok": bool(acc),
            "encrypted": bool(encrypted),
            "live": bool(live),
            "nickname": nick,
            "uid": uid_val,
            "expires_at": exp.isoformat() if exp else None,
            "token_source": common.token_source(at) if at else "",
            "ttl_days": common.jwt_ttl_days(at) if at else None,
            "is_backup": bool(is_backup),
            "mtime": mtime,
        })
        if encrypted:
            entries[-1]["reason"] = LIVE_REASON if live else ENCRYPTED_REASON
      except OSError:
        # 任何一步的文件层失败都只跳过这一个条目，绝不冒到 HTTP 层
        continue
    # 最新修改的会话视为当前（正式写在备份之后，通常就是最新）
    entries.sort(key=lambda x: x["mtime"], reverse=True)
    for i, e in enumerate(entries):
        e["current"] = (i == 0)
    return entries


def _restore_backup(backup, ch):
    """写入失败时把已轮换出去的备份改回正式文件名，避免桌面端陷入"无登录态"。

    switch_account 的顺序是「旧文件改名 → 写新文件」，中间那一步失败的话
    正式登录态文件就不存在了，客户端会认为没有登录态。备份本身是有效
    登录态，改回来即可恢复原状。

    返回追加给用户看的说明（成功 / 失败都给出可执行的下一步），无备份可恢复时返回空串。
    """
    if not backup:
        return ""
    src = Path(backup)
    if not src.exists():
        return ""
    try:
        common.replace_with_retry(src, ch.desktop_info)
        return "；已回滚为原登录态"
    except OSError:
        return ("；回滚也失败，原登录态仍在备份 %s，可手工改名回 %s"
                % (src.name, ch.info_name))


def current_uid(ch=None):
    """当前桌面端账号的 uid（取不到返回空串）。"""
    try:
        entries = current_account(ch)
    except Exception:  # noqa: BLE001
        return ""
    for e in entries:
        if e.get("current") and e.get("uid"):
            return e["uid"]
    return ""


def migrate_preview(target_name, ch=None):
    """扫描「切到 target_name 后需要迁移多少数据」，供前端弹框展示。

    ⚠️ 能力守卫放在**这里**，不放在调用方：迁移扫描的是国服客户端的本地数据目录，
    国际服通道跑它只会拿到一份国服数据的预览（看着像"国际服也要迁移"）。
    HTTP 与 CLI 两个入口都要挡，写在函数里才只有一个真相来源 ——
    只写在 HTTP 层时，`--channel wbai --migrate-preview` 就能绕过去。
    """
    ch = channel(ch)
    if not ch.supports("migrate"):
        # `unsupported` 与 /api/credits、/api/checkin-status、/api/client-status 保持同款：
        # 调用方（尤其 CLI）要能区分「这个通道压根没这能力」与「这次扫描本身失败」，
        # 只看 ok=false 两者长得一模一样。
        return {"ok": False, "needed": False, "unsupported": True,
                "message": "%s不支持本地数据迁移" % ch.server}
    norm = os.path.basename((target_name or "").replace("\\", "/"))
    src = ch.auth_dir / norm
    if not norm.endswith(".info") or not src.is_file():
        return {"ok": False, "message": "目标账号文件不存在：%s" % (target_name or "")}
    new_uid = ""
    try:
        acc = wb.session_from_info_file(src)
        new_uid = (acc or {}).get("uid") or ""
    except Exception:  # noqa: BLE001
        pass
    if not new_uid:
        return {"ok": False, "message": "目标账号文件解析不出 uid，无法迁移"}
    old_uid = current_uid(ch)
    # 只报本通道那个客户端：提示里写的是"迁移前会自动关闭它"，
    # 把另一个通道（本次不会去关）的客户端列进来就是假话。
    data = migration.preview(old_uid, new_uid, client_names=ch.client_processes)
    data["target_file"] = norm
    data["target_nickname"] = (wb.session_from_info_file(src) or {}).get("nickname") or norm
    # needed 由 migration.preview() 给出（见那边的注释），这里不再重算 ——
    # 同一个值算两遍，将来改了一处忘了另一处就会不一致。
    return data


def find_client_exe(ch, procs=None):
    """定位**该通道的**客户端可执行文件；找不到返回 None。

    ⚠️ `ch` 是必填的：国服与国际服是两个不同的程序（WorkBuddy.exe /
    WorkBuddyAI.exe），早先这里用模块级单值常量，国服的迁移流程会去关/开
    **国际服**客户端 —— 进程名必须跟着通道走。

    只从**固定候选位置**和**正在运行的进程路径**里找，不接受任何外部输入 ——
    这个结果会被拿去启动程序。

    `procs` 允许调用方传入已经枚举好的进程表：`list_processes()` 内部要起一次
    `tasklist` 子进程（本机实测 **284 ms**），`client_status()` 原本因为它和
    `find_client_exe()` **各跑一次**而白花一倍时间。传进来即可复用。
    """
    ch = channel(ch)
    cands = []

    # 1) 正在运行的进程：最可靠，那就是用户实际在用的那一份
    if procs is None:
        procs = common.list_processes() or {}
    for name in ch.client_processes:
        for pid in procs.get(name, []):
            p = common.process_image_path(pid)
            if p and os.path.basename(p).lower() == ch.client_exe.lower():
                cands.append(Path(p))

    # 2) 常见安装位置
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        for d in ch.client_dirs:
            cands.append(Path(base) / d / ch.client_exe)
            cands.append(Path(base) / "Programs" / d / ch.client_exe)

    # 3) 注册表卸载项里的 DisplayIcon / InstallLocation（尽力而为，失败不影响）
    # ⚠️ 每个 OpenKey 都必须 CloseKey：`find_client_exe` 会被反复调用（/api/client-status
    #    的 TTL 一过就走一次），句柄只开不关会一直累积（见 AUDIT_2026-09-24.md P3-7）。
    try:
        import winreg
        for hive, sub in ((winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                          (winreg.HKEY_CURRENT_USER,
                           r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")):
            try:
                root = winreg.OpenKey(hive, sub)
            except OSError:
                continue
            try:
                n = winreg.QueryInfoKey(root)[0]
                for i in range(n):
                    try:
                        name = winreg.EnumKey(root, i)
                    except OSError:
                        continue
                    sk = None
                    try:
                        sk = winreg.OpenKey(root, name)
                        disp = str(winreg.QueryValueEx(sk, "DisplayName")[0] or "")
                        if "workbuddy" not in disp.lower():
                            continue
                        for val in ("DisplayIcon", "InstallLocation"):
                            try:
                                raw = str(winreg.QueryValueEx(sk, val)[0] or "").strip('"')
                            except OSError:
                                continue
                            if not raw:
                                continue
                            p = Path(raw)
                            # 注册表里可能同时有国服/国际服两条卸载项，只收本通道那个 exe 名
                            cand = p if p.suffix.lower() == ".exe" else p / ch.client_exe
                            if cand.name.lower() == ch.client_exe.lower():
                                cands.append(cand)
                    except OSError:
                        continue
                    finally:
                        if sk is not None:
                            try:
                                winreg.CloseKey(sk)
                            except OSError:
                                pass
            finally:
                try:
                    winreg.CloseKey(root)
                except OSError:
                    pass
    except Exception:  # noqa: BLE001
        pass

    for c in cands:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# 客户端状态缓存
#
# `client_status()` 要枚举进程（内部起 `tasklist` 子进程，本机实测 284 ms）
# 并调用 `find_client_exe()`（304 ms），合计 ≈ 600 ms。而 `/api/client-status`
# 会被前端反复轮询、`/api/open-client` 也要先查一遍状态，每次都重跑全量枚举纯属浪费。
#
# 缓存 4 秒：足够覆盖一轮页面交互，又短到用户手动启停客户端后不会看到过时状态。
# ⚠️ 只给**只读**的 `/api/client-status` 吃缓存；`open_client()` 是动作接口，
#    用户点「打开客户端」时就是要看当下的真实状态，必须传 `fresh=True` 绕过。
# ⚠️ **按通道分区**：两个通道查的是两个不同程序，共用单槽缓存会让国际服
#    拿到国服那份结果（与 credits / checkin 缓存同款坑）。
# ---------------------------------------------------------------------------
_CLIENT_STATUS_CACHE = {}      # {通道 key: {"ts": float, "payload": dict}}
CLIENT_STATUS_TTL = 4.0


def invalidate_client_status(ch=None):
    """丢掉客户端状态缓存。`ch=None` 表示全部通道。

    任何会改变客户端进程状态的操作（启停、切号时的关闭）都要调一次，
    否则后续几秒内读到的还是动作之前的快照。
    """
    if ch is None:
        _CLIENT_STATUS_CACHE.clear()
        return
    _CLIENT_STATUS_CACHE.pop(channel(ch).key, None)


def _client_pids(procs, ch):
    """从**已枚举好的**进程表里取出该通道客户端的 pid。

    进程名可能有历史别名（国服的 `codebuddy.exe`），所以是遍历而不是单次 get。
    传已枚举的表进来，调用方就不必为取 pid 再跑一次 `tasklist`（+284 ms）。
    """
    if not procs:
        return []
    out = []
    for name in ch.client_processes:
        out.extend(procs.get(name, []))
    return sorted(set(out))


def client_status(ch, fresh=False):
    """**该通道**客户端的状态。返回 dict 而不是 tuple —— 这个结果会直接进
    JSON 响应，tuple 里的 Path 序列化不了（会 500）。路径统一转成字符串。

    `fresh=True` 跳过缓存（动作接口用）。
    """
    ch = channel(ch)
    now = time.time()
    cached = _CLIENT_STATUS_CACHE.get(ch.key)
    if not fresh and cached and cached.get("payload") is not None:
        if now - cached["ts"] < CLIENT_STATUS_TTL:
            return cached["payload"]
    procs = common.list_processes()
    pids = _client_pids(procs, ch)
    # 把已枚举的进程表传下去，避免 find_client_exe 内部再跑一次 tasklist
    exe = find_client_exe(ch, procs)
    out = {
        "running": bool(pids),
        "pids": pids,
        "exe": str(exe) if exe else "",
        "detectable": procs is not None,
    }
    _CLIENT_STATUS_CACHE[ch.key] = {"ts": now, "payload": out}
    return out


def open_client(ch):
    """打开**该通道的**客户端；已在运行则把它切到前台，不重复启动。

    切号之后客户端需要重新读取登录态 —— 但它自己不一定在跑，
    所以这里给一个一键入口，而不是让用户去开始菜单找。
    返回 (ok, message)。
    """
    ch = channel(ch)
    label = "%s客户端" % ch.server
    procs = common.list_processes()
    if procs is None:
        return False, "无法枚举进程（tasklist 不可用），请手动打开%s" % label
    pids = _client_pids(procs, ch)
    if pids:
        # 客户端「关闭到托盘」时主窗口是隐藏的（进程在、图标在、窗口看不见）。
        # 这种情况必须走"唤出"而不是"切前台"，否则点了没反应。
        hidden = [w for w in common.client_main_windows(pids)
                  if not w["visible"]]
        if common.focus_windows_of(pids):
            how = "窗口原先收在系统托盘里，已把它显示出来" if hidden \
                else "已切到前台"
            return True, "%s已在运行（pid %s），%s" % (label, pids[0], how)
        return True, ("%s已在运行（pid %s），但没找到它的主窗口，"
                      "请从任务栏/托盘点开" % (label, pids[0]))

    # 复用上面已枚举的 procs：否则 find_client_exe 内部会再跑一次 tasklist（+284 ms）
    exe = find_client_exe(ch, procs)
    if not exe:
        return False, ("找不到%s（%s）。"
                       "如果你装在了非标准位置，请手动启动一次。" % (label, ch.client_exe))
    try:
        # DETACHED_PROCESS：客户端不随本工具退出而结束，也不继承控制台
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(exe)], cwd=str(exe.parent), close_fds=True,
                         creationflags=flags)
    except OSError as e:
        return False, "启动客户端失败：%s" % common.scrub(e)
    invalidate_client_status(ch)
    return True, "已启动%s：%s" % (label, exe)


def close_client(ch, graceful_wait=8.0, total_wait=25.0, log=None):
    """关闭**该通道的**客户端。**先礼后兵**：先发 WM_CLOSE 让它正常退出，
    超时再强杀。返回 (ok, message, was_running)。

    ⚠️ 只关本通道那一个程序：国服与世界服是两个不同的 exe，切号时把对方关掉
    既没用（对方不持有这次要动的库）又打扰用户。

    - `was_running` 供调用方在流程结束后**恢复原状**（原来在跑就重新打开）。
    - 检测不出进程时返回 ok=False —— 不能把"不知道"当成"已经关掉了"。
    """
    ch = channel(ch)
    log = log or (lambda *a: None)

    def _pids():
        return _client_pids(common.list_processes(), ch)

    # 只枚举一次：tasklist 一次约 284 ms，重复枚举是白花一倍时间
    procs0 = common.list_processes()
    if procs0 is None:
        return False, "无法枚举进程（tasklist 不可用），请手动退出客户端", False
    pids = _client_pids(procs0, ch)
    if not pids:
        return True, "客户端本来就没在运行", False

    log("       客户端在运行（%d 个进程），先请它正常退出…" % len(pids))
    common.close_windows_of(pids)
    deadline = time.time() + graceful_wait
    while time.time() < deadline:
        time.sleep(0.4)
        if not _pids():
            invalidate_client_status(ch)
            return True, "客户端已正常退出", True

    left = _pids()
    log("       正常退出超时，强制结束 %d 个进程…" % len(left))
    common.terminate_processes(left)
    deadline = time.time() + max(0.0, total_wait - graceful_wait)
    while time.time() < deadline:
        time.sleep(0.4)
        if not _pids():
            invalidate_client_status(ch)
            return True, "客户端已强制关闭", True

    still = _pids()
    invalidate_client_status(ch)
    return (False,
            "无法关闭客户端（仍有 %d 个进程：%s）。可能被其它用户会话占用或权限不足，"
            "请手动退出后重试" % (len(still), ", ".join(str(p) for p in still[:3])),
            True)


def switch_account(target_name, migrate=None, ch=None):
    r"""把该通道账号库里的 <target_name> 切换为桌面端正式登录态。

    - 校验目标文件存在且可解析；
    - 桌面目录不存在则创建；
    - 现有正式文件改名为带时间戳的备份（与客户端 clean() 一致），保留其可用登录态；
    - 复制目标文件内容为正式登录态文件；
    - 清理 <正式文件>.logged-out 登出标记（若有）；
    - 若给了 migrate 选项，**切换成功后**再把旧账号名下的本地数据改归属到新账号。
      顺序是「先切后迁」：迁移失败时用户至少已经在新账号上，不会出现
      "数据已归新账号、人却还登着旧账号"的更糟状态。只有国服通道支持迁移。
    返回 (ok, message, migration_result)。
    """
    ch = channel(ch)
    # 只接受文件名，与 remove/refresh 保持一致。此前直接拼账号库目录，
    # 传 "../xxx.info" 会解析到账号库之外的文件（实测可命中 ..\..\自动签到\wb_auth\*.info），
    # 等于允许把任意路径的 .info 写进桌面端登录态。
    norm = (target_name or "").replace("\\", "/")
    if not norm or os.path.basename(norm) != norm:
        return False, "非法的账号文件名：%s" % (target_name or ""), None
    if not norm.endswith(".info"):
        return False, "仅支持 .info 格式的账号文件", None
    src = ch.auth_dir / norm
    if not src.is_file():
        return False, "目标账号文件不存在：%s" % target_name, None
    # 该通道没有迁移能力时忽略 migrate：宁可只切号，也不能拿国服的迁移逻辑去动国际服的数据目录。
    # 但**被忽略这件事必须说出来**：以前直接 `migrate = None` 就完事，于是
    # `--channel wbai --switch x --migrate` 会返回 ok=true + migration=null，
    # 用户会以为数据也迁过去了，实际只切了号。
    migrate_dropped = bool(migrate) and not ch.supports("migrate")
    if migrate_dropped:
        migrate = None

    # 切换前的 uid 必须**在轮换之前**取：一旦旧文件被改名走，current_account()
    # 看到的就是备份，语义会变。
    old_uid = current_uid(ch)
    mig_result = None

    # 「备份 + 写入 + 清标记」必须串行，否则与续期/另一次切号交叉会写坏登录态。
    # 带迁移时放宽锁超时：迁移要做 db 快照（本机 WAL 有 4 MB），15s 可能不够，
    # 而超时会让并发的续期任务直接失败。
    lock_timeout = 120.0 if migrate else 15.0
    # ⚠️ 锁必须**两把**。以前只加通道锁，而续期 / 签到 / 删除 / 新增用的是
    #    `<通道>-auth-<文件名>`（见 refresh_account_file / checkin_account_file /
    #    remove_account / add_account）—— 两把锁不同名，上面注释宣称的「与续期互斥」
    #    实际不成立：切号读 src 的同时，续期可以写同一个文件、删除可以把它删掉。
    #    加锁顺序恒为 **通道 → 账号**（其它路径只拿账号锁，不会反向），所以不会死锁。
    account_lock_key = ch.key + "-auth-" + norm

    # ⚠️ 客户端恢复状态必须提到锁外：只要 close_client 成功关掉了它，
    #    之后**任何**退出路径（下面 4 条失败早退、以及 migrate 抛异常）都得把它放回去。
    #    以前重开代码只写在函数末尾的成功路径上（注释却自称「无论迁移成败都恢复」），
    #    于是那 4 条早退路径全部走不到 —— 客户端被静默关掉，用户得自己手动开回来。
    client_was_running = False
    _reopen_done = [False]

    def _restore_client():
        """把被我们关掉的客户端放回去；幂等，返回可拼接进消息的提示串。"""
        if _reopen_done[0] or not client_was_running:
            return ""
        if not (migrate and migrate.get("reopen_client", True)):
            return ""
        _reopen_done[0] = True
        try:
            _ok, _msg = open_client(ch)
        except BaseException as e:  # noqa: BLE001  含 SystemExit（安全删除 shim）
            return "；重新打开%s客户端失败：%s" % (ch.server, common.scrub(e))
        return "；%s" % _msg

    switched_ok = False
    try:
        with common.file_lock(ch.lock_key, LOCK_DIR, timeout=lock_timeout), \
                common.file_lock(account_lock_key, LOCK_DIR, timeout=lock_timeout):
            acc = wb.session_from_info_file(src)
        if not acc:
            return False, "目标账号文件无法解析（内容可能不全或被加密）：%s" % target_name, None
        data = _read_info(src)
        if data is None:
            return False, "目标账号文件不是合法 JSON：%s" % target_name, None
        if not common.token_looks_complete((data.get("auth") or {}).get("accessToken")):
            return False, ("目标账号的 accessToken 不完整（疑似粘贴时被截断），切换后必然 401：%s。"
                           "请重新从客户端导出完整的 %s" % (target_name, ch.info_name)), None

        # 0. 要迁移就先关掉客户端。
        #    放在校验之后、轮换之前：校验失败时不必白关一次；
        #    关不掉就**整体中止**（连切号也不做）—— 用户点的是"切换并迁移"，
        #    只切一半会让人以为迁移成功了。
        client_was_running = False
        if migrate and migrate.get("enabled", True) and migrate.get("close_client", True):
            ok_c, msg_c, client_was_running = close_client(ch)
            if not ok_c:
                return False, "迁移前无法关闭%s客户端：%s" % (ch.server, msg_c), None
            if client_was_running:
                print("[提示] %s" % msg_c, flush=True)

        ch.desktop_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now(datetime.timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H%M%SZ")
        pid = os.getpid()
        marker = "%d" % now.microsecond

        # 1. 现有正式文件轮换为备份（写入失败时用它回滚）
        backup = None
        if ch.desktop_info.exists():
            backup = ch.desktop_dir / ("%s%s.%d.%s.info" % (ch.backup_prefix, ts, pid, marker))
            try:
                common.replace_with_retry(ch.desktop_info, backup)
            except OSError as e:
                return False, "轮换旧登录态失败：%s" % common.scrub(e), None

        # 2. 写入目标账号。这一步失败必须回滚：否则正式文件已被改名走，
        #    桌面端会处于"没有登录态"的状态（此前只报失败、不回滚）。
        tmp = ch.desktop_info.with_suffix(".info.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            common.replace_with_retry(tmp, ch.desktop_info)
        except OSError as e:
            try:
                tmp.unlink()
            except OSError:
                pass
            # OSError 文本会带出本机绝对路径（如桌面端 auth 目录），先脱敏再回给前端
            return False, "写入新登录态失败：%s%s" % (common.scrub(e),
                                                     _restore_backup(backup, ch)), None

        # 3. 清理登出标记
        marker_path = ch.desktop_dir / ch.logout_marker_name
        if marker_path.exists():
            try:
                marker_path.unlink()
            except OSError:
                pass

        # 4. 写后自校验（与 Trae 切换器一致）：确认桌面端文件里确实是目标账号。
        #    客户端 watcher 未采纳或被其它进程覆盖时立刻暴露，而不是报成功却没生效。
        after = _read_info(ch.desktop_info)
        acc_after = wb.session_from_info_file(ch.desktop_info) if after is not None else None
        if not acc_after or acc_after["uid"] != acc["uid"]:
            raw_acct = (after or {}).get("account")
            cur_nick = (raw_acct or {}).get("nickname") if isinstance(raw_acct, dict) else None
            if acc_after:
                cur_nick = acc_after["nickname"]
            return False, ("写入后校验未通过：桌面端当前登录态仍为「%s」，切换可能未生效。"
                           "请确认客户端未占用该文件后重试" % (cur_nick or "未知")), None

        # 5. 裁剪旧备份：纯善后，必须放在**新登录态写好且校验通过之后**。
        #    此前它夹在「旧文件已改名走」和「新文件还没写」之间 —— 一旦这里抛异常
        #    （本机的安全删除 shim 在批量删除守卫拒绝时会抛 SystemExit），
        #    正式文件就整个不存在了，客户端会认为没有登录态。
        #    prune_backups 内部已吞掉包括 SystemExit 在内的一切异常，这里再兜一层。
        try:
            pruned = common.prune_backups(
                ch.desktop_dir, ch.backup_glob,
                keep=DESKTOP_BACKUP_KEEP, exclude={ch.info_name})
        except BaseException:  # noqa: BLE001
            pruned = 0

        # 6. 迁移旧账号名下的本地数据（可选）。
        #    放在**切换成功且写后校验通过之后**：迁移失败时用户至少已经在新账号上，
        #    不会出现"数据已归新账号、人却还登着旧账号"这种更糟的状态。
        #    迁移失败**不回滚切号** —— 否则用户会同时失去登录态和迁移结果。
        if migrate and migrate.get("enabled", True):
            from_uid = str(migrate.get("from_uid") or old_uid or "")
            if not from_uid or from_uid == acc["uid"]:
                mig_result = {"ok": True, "message": "无需迁移（新旧账号相同或取不到原 uid）",
                              "steps": [], "warnings": []}
            else:
                mig_result = migration.migrate(from_uid, acc["uid"], migrate)
                common.audit(Handler.AUDIT_DIR, ch.source, "migrate",
                             "%s -> %s" % (from_uid[:8], acc["uid"][:8]),
                             bool(mig_result.get("ok")), mig_result.get("message"))

        switched_ok = True      # 走到这里 = 切换 + 迁移全部完成，客户端留给成功路径重开
    finally:
        # 失败 / 异常路径：把被我们关掉的客户端放回去。
        # 成功路径不会走到这里生效（switched_ok 为真），它自己在下面拼提示。
        if not switched_ok:
            _restore_client()

    msg = "已切换为 %s（%s），桌面端将自动采纳新会话" % (acc["nickname"], target_name)
    if pruned:
        msg += "；已清理 %d 份旧备份（保留最近 %d 份）" % (pruned, DESKTOP_BACKUP_KEEP)
    if migrate_dropped:
        msg += "；%s不支持本地数据迁移，本次仅切换" % ch.server
    if mig_result is not None:
        if mig_result.get("ok"):
            moved = mig_result.get("moved_sessions")
            extra = "（%d 个会话）" % moved if moved else ""
            msg += "；数据迁移完成%s" % extra
        else:
            msg += "；⚠️ 但数据迁移失败：%s" % mig_result.get("message", "")

    # 恢复客户端原状：原来在跑就重新打开（迁移成功时它会直接读新账号的登录态）。
    # 失败/异常路径由上面的 finally 兜底；这里只负责把重开提示拼进成功消息。
    msg += _restore_client()
    return True, msg, mig_result


def safe_info_name(name, prefix="workbuddy-"):
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
    # 前缀区分通道：国服 workbuddy-、国际服 workbuddyai-，
    # 两个账号库即使被放到同一个目录也不会撞名。
    return "%s%s.info" % (prefix, safe), None


def add_account(name, content, ch=None):
    r"""新增一个账号配置文件到该通道的账号库目录（内容为前端粘贴的 info 或上传文件）。"""
    ch = channel(ch)
    fname, err = safe_info_name(name, ch.account_prefix)
    if err:
        return False, err
    src = ch.auth_dir / fname
    if src.exists():
        return False, "已存在同名账号文件：%s" % fname
    # 先校验是合法 JSON
    try:
        data = json.loads(content)
    except ValueError:
        return False, "内容不是合法 JSON，无法解析为账号文件"

    ch.auth_dir.mkdir(parents=True, exist_ok=True)
    with common.file_lock(ch.key + "-auth-" + fname, LOCK_DIR):
        if src.exists():
            return False, "已存在同名账号文件：%s" % fname
        try:
            src.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            return False, "写入账号文件失败：%s" % e

        # 用现有脚本的解析器校验：只有能解析成有效登录态才保留，否则回滚删除。
        # 回滚用 safe_unlink：这台机器的安全删除 shim 会抛 SystemExit（BaseException），
        # 裸 unlink 会让"拒绝保存"变成"请求直接断连"，用户看不到任何提示。
        acc = wb.session_from_info_file(src)
        if not acc:
            common.safe_unlink(src)
            return False, "内容不是有效的登录态信息（缺少字段或已加密），已回滚，请粘贴正确的账号文件内容"
        if not common.token_looks_complete((data.get("auth") or {}).get("accessToken")):
            common.safe_unlink(src)
            return False, ("accessToken 不完整（疑似粘贴时被截断），已回滚。"
                           "请整份复制客户端的 %s 后重新添加" % ch.info_name)
    return True, "已新增账号 %s（%s）" % (acc["nickname"], fname)


def remove_account(file_name, ch=None):
    """删除该通道账号库下的一个账号配置文件（防止路径穿越）。"""
    ch = channel(ch)
    # 限定文件名，不得包含路径分隔符或上一级
    base = os.path.basename((file_name or "").replace("\\", "/"))
    if base != base.replace("..", "") or base != (file_name or "").replace("\\", "/").split("/")[-1]:
        return False, "非法的文件名"
    if not base.endswith(".info"):
        return False, "只能删除 .info 账号文件"
    target = ch.auth_dir / base
    if not target.is_file():
        return False, "账号文件不存在：%s" % base
    with common.file_lock(ch.key + "-auth-" + base, LOCK_DIR):
        if not common.safe_unlink(target) and target.exists():
            return False, "删除失败：文件仍存在（可能被占用，或本机安全删除策略拒绝了本次删除）"
    return True, "已删除账号文件 %s" % base


class _NullLog:
    """极简日志壳：续期日志不进配置文件目录，避免污染签到日志。

    ⚠️ 只**丢弃**，不缓存 —— 原先把每条都 append 到 `self._buf` 且从不清空，
    常驻服务 + 每次续期若干条，内存只增不减（见 AUDIT_2026-09-24.md P3-8）。
    """
    def __call__(self, msg):
        return None


_NULLLOG = _NullLog()


def _recalc_expiry(path):
    """续期后用 JWT 的 exp 声明重算 expiresAt / refreshExpiresAt。

    workbuddy_checkin.merge_auth_token 只在 expiresAt 缺失时才重算，旧值存在时不覆盖，
    导致续期后 wb_auth 文件里 expiresAt 仍为旧值、剩余天数不更新。
    JWT 的 exp 是服务端设置的权威到期时间，比 lastRefreshTime+expiresIn 更准确。

    返回 True = 有效期已是正确的（含"本来就无需改"）；False = 没能写回。
    调用方据此如实提示 —— 续期本身早已成功，不该因为这一步失败就报"续期失败"。
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    auth = data.get("auth")
    if not isinstance(auth, dict):
        return False
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
        return True          # 值本来就是对的，无需写盘
    data["auth"] = auth
    tmp = Path(path).with_suffix(".info.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        # 同文件其它写盘点都走 replace_with_retry —— rename 会被杀软/客户端间歇性拒绝，
        # 这里原先用的是裸 `tmp.replace(path)`，见 AUDIT_2026-09-24.md P2-5。
        common.replace_with_retry(tmp, path)
    except OSError as e:
        # 续期**已经成功**（调用方已判 kind=="refreshed" and ok），这里只是把新有效期写回素材。
        # 写不进去不该让整个请求变 500 —— 否则用户看到"续期失败"，其实已经续上了。
        # 降级为返回 False，由调用方如实提示"有效期显示未刷新"。
        try:
            common.safe_unlink(str(tmp))
        except BaseException as e2:  # noqa: BLE001  含 SystemExit（安全删除 shim 会抛）
            print("[警告] 清理 %s 失败：%s" % (tmp.name, common.scrub(e2)))
        print("[警告] 写回 %s 的到期时间失败：%s" % (Path(path).name, common.scrub(e)))
        return False
    return True


def refresh_account_file(file_name, force=True, ch=None):
    r"""切换后对目标账号执行真正的 HTTP 续期，并把新有效期写回账号库里的文件。

    与「把桌面端到期时间复制回素材」不同，本函数直接拿目标文件的 refreshToken
    向服务器续期，返回全新 accessToken / expiresAt，剩余时间即刻更新。

    force=True（默认，UI 上手动点「续期」用）：无条件刷新。
    force=False（计划任务用）：交给上级 workbuddy_checkin.refresh_account 判断 ——
      剩余天数 ≥ REFRESH_THRESHOLD_DAYS（3 天）或距上次实际续期不足
      refresh_min_interval_hours（默认 24 小时）就跳过，返回 kind="skipped"。
      与上级 refresh_guard.py / --refresh 的门卫语义一致，避免每天无谓地重写 .info。

    只对声明了 refresh 能力的通道开放。两个通道的续期网关**不同**（国服
    copilot.tencent.com / 国际服 www.workbuddy.ai），由 channel_cfg(ch) 按通道给，
    否则会拿国际服的 refreshToken 去打国服网关、恒 401。
    """
    ch = channel(ch)
    if not ch.supports("refresh"):
        return False, "%s暂不支持一键续期" % ch.server
    base = os.path.basename((file_name or "").replace("\\", "/"))
    if os.path.dirname((file_name or "").replace("\\", "/")):
        return False, "非法的文件路径"
    if not base.endswith(".info"):
        return False, "只能续期 .info 账号文件"
    target = ch.auth_dir / base
    if not target.is_file():
        return False, "%s 中不存在账号文件：%s" % (ch.auth_dir.name, base)
    acc = wb.session_from_info_file(target)
    if not acc:
        return False, "账号文件无法解析，无法续期"
    cfg = channel_cfg(ch)
    # 续期是「换 token + 写回文件」，与切号/同步互斥，否则会互相覆盖
    with common.file_lock(ch.key + "-auth-" + base, LOCK_DIR):
        ok, msg, kind = wb.refresh_account(acc, SCRIPT_DIR, cfg, _NULLLOG, force=force)
        if kind == "refreshed" and ok:
            # 续期本身已成功 —— 写回有效期失败只降级提示，不能报成"续期失败"。
            if _recalc_expiry(target):
                return True, "已续期并更新 %s 的到期时间" % base
            return True, "已续期 %s（有效期显示没能写回，下次刷新会重算）" % base
    return ok, msg


def _refresh_all_collect(force, ch=None):
    """遍历账号库跑一遍续期，返回 [(file, ok, msg)]。CLI 与 UI 共用这一份遍历。"""
    ch = channel(ch)
    results = []
    for a in list_accounts(ch):
        if not a.get("ok"):
            results.append((a["file"], False, a.get("reason") or "不可用，跳过"))
            continue
        ok, msg = refresh_account_file(a["file"], force=force, ch=ch)
        results.append((a["file"], ok, msg))
        common.audit(Handler.AUDIT_DIR, ch.source, "refresh-all", a["file"], ok, msg)
    return results


def _results_payload(results, label):
    """把 [(file, ok, msg)] 整理成前端要的统一结构。"""
    ok_n = sum(1 for r in results if r[1])
    return {
        "ok": ok_n == len(results),
        "message": "%s：%d/%d 成功" % (label, ok_n, len(results)),
        "ok_count": ok_n,
        "total": len(results),
        "results": [{"file": f, "ok": ok, "message": msg} for f, ok, msg in results],
    }


def refresh_all(force=False, ch=None):
    """对账号库下全部账号跑一遍续期，供计划任务调用。返回退出码。

    默认 force=False —— 走上级的门卫（剩余 < 3 天且距上次 ≥ 24 小时才真刷），
    所以「每天跑一次」的实际开销通常只有几次本地文件读取，不产生任何写盘。
    只有 --refresh-all --force 才无条件全刷。

    为什么要门卫：现在新登账号一律是 enterprise_switch（access 30 天），
    access 到期前续一次就不会掉线；而无条件续期会重写全部 .info，
    客户端在跑时属于没必要的写冲突风险。与上级 refresh_guard.py 的语义一致。

    ⚠️ 能力守卫放这里（而不是只在 HTTP 层）：`--channel wbai --refresh-all`
    走的是本函数，HTTP 层那道 400 拦不住它 —— 没有这道守卫就会逐个账号刷屏
    同一句"暂不支持"，让人误以为是真的尝试过并失败了。
    """
    ch = channel(ch)
    if not ch.supports("refresh"):
        print("FAIL %s暂不支持一键续期" % ch.server)
        return 1
    results = _refresh_all_collect(force, ch)
    for f, ok, msg in results:
        print("%-4s %-32s %s" % ("OK" if ok else "FAIL", f, msg))
    print("---- %s 续期完成：%d/%d 成功" % (ch.title, sum(1 for r in results if r[1]), len(results)))
    return 0 if all(r[1] for r in results) else 1


# ---------------------------------------------------------------------------
# 每日签到（只读查询 + 一键领取）
# ---------------------------------------------------------------------------
# 签到接口与计费接口同一个网关、同一套请求头，只是路径不同：
#   POST {endpoint}/v2/billing/meter/checkin-activity-status  ← 只读查状态
#   POST {endpoint}/v2/billing/meter/daily-checkin            ← 真正领取
# 查询走 _billing_post（UA 有要求）；领取直接复用上级 checkin_account 的实现，
# 免得两处各写一份业务码判断（BIZ_ALREADY_CLAIMED / NOT_ELIGIBLE / EVENT_ENDED）。
CHECKIN_STATUS_PATH = getattr(wb, "STATUS_PATH", "/v2/billing/meter/checkin-activity-status")
CHECKIN_TTL = 300.0       # 缓存秒数：和积分一样，一次查询要按账号数发请求

# 每通道一把「回源锁」：同一通道的并发请求只让一个真去扇出，其余等它把缓存填好后
# 直接命中（double-checked）。否则「启动预热线程 vs 首个请求」、或「多个 ?force=1」
# 会各自跑完整扇出 —— 每账号一次外部请求，页面卡顿 + 网关限流风险
# （见 AUDIT_2026-09-24.md P2-6）。
_FETCH_LOCKS = {}
_FETCH_LOCKS_GUARD = threading.Lock()

# 等锁上限（秒）。**必须有上限**：锁的持有者在扇出时可能卡在慢网关上
# （`_billing_post` 单账号 timeout=20 × retries=2 → 最坏 40s/账号；
#   20 个账号 6 并发最坏可达 ~160s）。没有上限的话，后来的请求会被一直挂住，
# 直到前端 fetch 超时。等不到就**退化为自己扇出**（即改造前的并发行为）——
# 宁可多打一次网关，也不把用户卡死（见 AUDIT_2026-09-24.md P2-6）。
FETCH_LOCK_TIMEOUT = 30.0


def _fetch_lock(kind, key):
    """按「用途 + 通道」取回源锁。kind 取 'checkin' / 'credits'。"""
    with _FETCH_LOCKS_GUARD:
        lk = _FETCH_LOCKS.get((kind, key))
        if lk is None:
            lk = _FETCH_LOCKS[(kind, key)] = threading.Lock()
        return lk


_CHECKIN_LOCK = threading.Lock()
# 缓存**按通道分区**：`{通道 key: {"ts": ..., "payload": ...}}`。
# 不分区的话，两个通道会共用同一份结果 —— 两个通道的账号列表与网关都不同，
# 混用会直接把国服的签到/积分显示到国际服视图上（反之亦然）。
_CHECKIN_CACHE = {}


def query_checkin(acc, cfg):
    """只读查询单个账号的签到状态（不发领取请求）。"""
    headers = wb.build_headers(acc)
    code, body = _billing_post(cfg["endpoint"] + CHECKIN_STATUS_PATH, headers)
    if code in (401, 403):
        return {"ok": False, "reason": "登录态无效或已过期（HTTP %d），请重新登录该账号" % code}
    if code != 200:
        return {"ok": False, "reason": "签到状态接口 HTTP %s" % code}
    if not isinstance(body, dict) or body.get("code") not in (None, 0):
        return {"ok": False, "reason": "签到状态接口业务错误（code=%s）"
                % (body or {}).get("code")}
    st = body.get("data")
    if not isinstance(st, dict):
        return {"ok": False, "reason": "签到状态接口结构异常"}

    if st.get("active") is False:
        return {"ok": True, "active": False, "checked_in": False,
                "streak_days": None, "total_credits": None, "daily_credit": None,
                "msg": "签到活动未开启"}

    checked = bool(st.get("today_checked_in"))
    streak = st.get("streak_days")
    total = st.get("total_credits")
    daily = st.get("daily_credit")
    if checked:
        msg = "今日已签到"
        if streak is not None:
            msg += " · 连续 %s 天" % streak
    else:
        msg = "今日未签到"
        if daily is not None:
            msg += " · 可领 %s 积分" % daily
    return {"ok": True, "active": True, "checked_in": checked,
            "streak_days": streak, "total_credits": total, "daily_credit": daily,
            "msg": msg}


def checkin_snapshot(force=False, ch=None):
    """汇总**该通道**账号库全部账号的签到状态（并发查询 + 进程内缓存）。

    ⚠️ 必须显式收通道，不能靠默认值兜底 —— 原来它内部写死 `list_accounts()` +
    `channel(DEFAULT_CHANNEL).auth_dir`，等于"只签国服"。
    两个通道的账号库与网关都不同，读错通道会静默把国服的签到状态显示到国际服视图上。
    """
    ch = channel(ch)
    t0 = time.time()
    with _CHECKIN_LOCK:
        _c = _CHECKIN_CACHE.get(ch.key) or {}
        cached, ts = _c.get("payload"), _c.get("ts", 0.0)
        if not force and cached and t0 - ts < CHECKIN_TTL:
            return dict(cached, cached=True)

    # 回源串行化（见 _fetch_lock）：拿到锁后再查一次缓存 —— 等锁期间可能已经有别的
    # 请求把结果填好了，那就直接复用，别再扇一遍。`ts >= t0` 说明就是**本次等待期间**
    # 别人刚填的，所以即便 force=True 也可以安全复用（避免并发 force 重复扇出）。
    _lk = _fetch_lock("checkin", ch.key)
    _got = _lk.acquire(timeout=FETCH_LOCK_TIMEOUT)
    try:
        now = time.time()
        with _CHECKIN_LOCK:
            _c = _CHECKIN_CACHE.get(ch.key) or {}
            cached, ts = _c.get("payload"), _c.get("ts", 0.0)
            if cached and (ts >= t0 or (not force and now - ts < CHECKIN_TTL)):
                return dict(cached, cached=True)

        cfg = channel_cfg(ch)
        accounts = list_accounts(ch)

        def _one(a):
            entry = {"file": a["file"], "uid": a.get("uid") or "",
                     "nickname": a.get("nickname") or a["file"], "ok": False}
            if not a.get("ok"):
                entry["reason"] = a.get("reason") or "账号不可用"
                return entry
            acc = wb.session_from_info_file(ch.auth_dir / a["file"])
            if not acc:
                entry["reason"] = "登录态无法解析"
                return entry
            try:
                entry.update(query_checkin(acc, cfg))
            except Exception as e:  # noqa: BLE001  单个账号失败不能拖垮整张表
                entry["reason"] = "签到查询异常：%s" % common.scrub(e, 80)
            return entry

        workers = min(6, max(1, len(accounts)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            items = list(pool.map(_one, accounts))
        payload = {
            "ok": True,
            "accounts": items,
            # checked = 今日**已签到**数；queried = **成功查询**数。
            # 两者语义不同，都要有。补 queried 是为了与 credits_snapshot 对齐
            # —— 那个接口一直有 queried，checkin 缺了它（见 AUDIT_2026-09-19.md 建议 3）。
            "checked": sum(1 for i in items if i.get("checked_in")),
            "queried": sum(1 for i in items if i.get("ok")),
            "total": len(items),
            "ts": datetime.datetime.now().strftime("%H:%M:%S"),
            "cached": False,
        }
        with _CHECKIN_LOCK:
            _CHECKIN_CACHE[ch.key] = {"ts": time.time(), "payload": payload}
        return payload
    finally:
        if _got:
            _lk.release()


def checkin_account_file(file_name, ch=None):
    """对**该通道**账号库里的单个账号执行签到（会真正领取）。返回 (ok, msg)。

    与 refresh_account_file 同款：只接受文件名、加文件锁、成功后清签到缓存。
    这里不复用上级的 history 记录 —— 那是 main() 的职责，UI 侧只负责把奖励领到。

    同样显式收通道：它被 checkin_all(ch) 逐个账号调用，写死默认通道的话
    一旦有别的通道支持签到，就会去签国服的账号（见 checkin_snapshot 的注释）。
    """
    ch = channel(ch)
    base = os.path.basename((file_name or "").replace("\\", "/"))
    if os.path.dirname((file_name or "").replace("\\", "/")):
        return False, "非法的文件路径"
    if not base.endswith(".info"):
        return False, "只能对 .info 账号文件签到"
    target = ch.auth_dir / base
    if not target.is_file():
        return False, "%s 中不存在账号文件：%s" % (ch.auth_dir.name, base)
    if not hasattr(wb, "checkin_account"):
        return False, "上级模块缺少 checkin_account，无法签到"
    acc = wb.session_from_info_file(target)
    if not acc:
        return False, "账号文件无法解析，无法签到"
    cfg = channel_cfg(ch)
    # 签到会读账号文件（并可能触发续期写回），与切号/续期互斥。
    # 锁名带通道 key：两个通道的账号库是不同目录，同名文件不该互相阻塞。
    with common.file_lock(ch.key + "-auth-" + base, LOCK_DIR):
        ok, msg, kind = wb.checkin_account(acc, cfg, False, _NULLLOG)
    if kind == "claimed":
        _invalidate_checkin_cache(ch)
    return ok, msg


def _invalidate_checkin_cache(ch=None):
    """让**该通道**的签到缓存失效（缓存按通道分区，见 checkin_snapshot）。

    只清一个通道：签到成功的是哪个通道，就只有那个通道的结果过期了。
    """
    ch = channel(ch)
    with _CHECKIN_LOCK:
        _CHECKIN_CACHE.pop(ch.key, None)


def checkin_all(ch=None):
    """一键签到：对账号库下全部可用账号各签一次。返回统一结构。

    ⚠️ 必须显式收通道：原来不传参、直接 `list_accounts()` + `Handler.SOURCE`，
    等于硬编码走默认（国服）通道。HTTP 层那道 400 只挡住了 /api/wbai/checkin，
    任何别处调用它都会去签国服的账号。
    """
    ch = channel(ch)
    if not ch.supports("checkin"):
        return {"ok": False, "message": "%s不支持一键签到" % ch.server}
    results = []
    for a in list_accounts(ch):
        if not a.get("ok"):
            results.append((a["file"], False, a.get("reason") or "不可用，跳过"))
            continue
        ok, msg = checkin_account_file(a["file"], ch)
        results.append((a["file"], ok, msg))
        common.audit(Handler.AUDIT_DIR, ch.source, "checkin", a["file"], ok, msg)
    _invalidate_checkin_cache(ch)
    return _results_payload(results, "一键签到完成")


def refresh_all_ui(ch=None):
    """一键续期（UI 按钮）：用户显式点了就是要刷，所以跳过门卫。

    与 checkin_all 同理：必须显式收通道，不能默认走国服。
    """
    ch = channel(ch)
    if not ch.supports("refresh"):
        return {"ok": False, "message": "%s暂不支持一键续期" % ch.server}
    return _results_payload(_refresh_all_collect(True, ch), "一键续期完成")


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
# 同 _CHECKIN_CACHE：按通道分区（`{通道 key: {"ts":..., "payload":...}}`）
_CREDITS_CACHE = {}


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


def credits_snapshot(force=False, ch=None):
    """汇总**该通道**账号库全部账号的积分明细（总剩余 + 套餐基础 / 平台奖励）。

    结果在进程内缓存 CREDITS_TTL 秒（前端每次刷新页面都会拉一次，不缓存等于放大
    N 倍请求）。force=True 强制回源（标题栏的「刷新积分」）。

    ⚠️ 与 checkin_snapshot 同理：必须显式收通道，且缓存按通道分区。
    原来内部写死 `list_accounts()` + `channel(DEFAULT_CHANNEL).auth_dir`。
    """
    ch = channel(ch)
    t0 = time.time()
    with _CREDITS_LOCK:
        _c = _CREDITS_CACHE.get(ch.key) or {}
        cached, ts = _c.get("payload"), _c.get("ts", 0.0)
        if not force and cached and t0 - ts < CREDITS_TTL:
            return dict(cached, cached=True)

    # 回源串行化（同 checkin_snapshot，见 _fetch_lock）。等锁同样**有上限**，
    # 超时就退化为自己扇出，不把用户挂死。
    _lk = _fetch_lock("credits", ch.key)
    _got = _lk.acquire(timeout=FETCH_LOCK_TIMEOUT)
    try:
        now = time.time()
        with _CREDITS_LOCK:
            _c = _CREDITS_CACHE.get(ch.key) or {}
            cached, ts = _c.get("payload"), _c.get("ts", 0.0)
            if cached and (ts >= t0 or (not force and now - ts < CREDITS_TTL)):
                return dict(cached, cached=True)

        cfg = channel_cfg(ch)
        accounts = list_accounts(ch)

        def _one(a):
            # uid 是前端把「当前桌面端账号」映射到这条积分记录的唯一键：
            # 当前账号可能不在账号库里（手动登录的），只有 uid 能对上。
            entry = {"file": a["file"], "uid": a.get("uid") or "",
                     "nickname": a.get("nickname") or a["file"], "ok": False}
            if not a.get("ok"):
                entry["reason"] = a.get("reason") or "账号不可用"
                return entry
            acc = wb.session_from_info_file(ch.auth_dir / a["file"])
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
            _CREDITS_CACHE[ch.key] = {"ts": time.time(), "payload": payload}
        return payload
    finally:
        if _got:
            _lk.release()


def build_ui_context(ch):
    """把通道配置翻译成前端模板的占位符。

    两个视图（/wb 国服、/wbai 国际服）共用同一份 ui_template.html，差别全在这里：
    标题、账号库名、提示文案，以及**能力开关**（CREDITS / CHECKIN / MIGRATE / REFRESH /
    OPEN_CLIENT）—— 置空即隐藏对应区块与按钮，前端不会去调不存在的接口。

    ⚠️ 模板里出现的每个 {{KEY}} 都必须在**所有**调用方补齐（wb / wbai / Trae）。
    漏一个，页面上就会留一个裸的 {{KEY}}，自检里有一条专门断言这件事。
    """
    ch = channel(ch)
    common_ctx = {
        "AUTH_DIR": ch.auth_dir.name,
        "ACCEPT": ".info,application/json",
        "FILE_LABEL": "账号配置文件（.info）",
        "CMD": "workbuddy_switcher.cmd",
        "API_BASE": ch.api_base,
        "VIEW": ch.key,
        # 能力开关，按通道的 features 逐项映射到前端（置空即隐藏对应入口）。
        # 国际服现在 credits / refresh / migrate / open_client 都开着；
        # checkin 仍为空 —— 签到活动未开启（接口 active=false）。
        "CREDITS": "1" if ch.supports("credits") else "",
        "CHECKIN": "1" if ch.supports("checkin") else "",
        "MIGRATE": "1" if ch.supports("migrate") else "",
        "REFRESH": "1" if ch.supports("refresh") else "",
        # 「打开客户端」有**两处**按钮，分别控制：
        #   OPEN_CLIENT     = 导航栏那个。国服/国际服按用户要求**不显示**（Trae 侧显示），
        #                     所以这里恒空，不看 features。
        #   OPEN_CLIENT_ROW = 「当前账号行」那个。2026-09-21 起两个通道都要显示 ——
        #                     进程名已按通道区分，点了开的就是自己那个客户端。
        "OPEN_CLIENT": "",
        "OPEN_CLIENT_ROW": "1" if ch.supports("open_client") else "",
    }
    # TITLE 直接用通道自己的 `title` —— 原先在这里又硬编码一份 if/else，
    # 与 `Channel(title=...)` 重复维护，改一处漏一处（见 AUDIT_2026-09-24.md P3-9）。
    # LOGO 只在模板里有意义、Channel 不持有它，所以按通道映射；
    # 用 dict.get 而不是 if/else，将来加通道时不会悄悄落进"else = 国际服"那个坑。
    common_ctx["TITLE"] = ch.title
    common_ctx["LOGO"] = {"wb": "&#128172;", "wbai": "&#127760;"}.get(ch.key, "&#128172;")
    return common_ctx


class Handler(common.BaseHandler):
    """WorkBuddy 切换器的路由；HTTP 骨架与跨站校验见 switcher_common.BaseHandler。

    一个进程对外提供三个页面：`/` 统一入口页（侧边导航栏）、`/wb` 国服账号管理视图、
    `/wbai` 国际服账号管理视图。接口按前缀分通道：国服沿用历史的 `/api/*`，
    国际服走 `/api/wbai/*`。
    """

    INDEX_NAME = "ui_hub.html"        # 首页 = 统一入口页
    BASE_DIR = _BIN_DIR
    UI_CONTEXT = build_ui_context(DEFAULT_CHANNEL)   # 国服视图的模板变量
    PAGES = {
        "/wb": ("ui_template.html", UI_CONTEXT),
        "/wbai": ("ui_template.html", build_ui_context("wbai")),
    }
    # 历史路径归国服；其余通道按 /api/<key>/ 前缀展开 —— 两个前缀都要进白名单，
    # 否则 do_GET 不会对它们返回 405，写接口会被 GET 摸到。
    _BASE_WRITE = ("/api/switch", "/api/remove", "/api/refresh", "/api/add",
                   "/api/checkin", "/api/refresh-all", "/api/open-client")
    WRITE_ENDPOINTS = _BASE_WRITE + tuple("/api/wbai" + p[len("/api"):] for p in _BASE_WRITE)
    SOURCE = "wb"
    AUDIT_DIR = _BIN_DIR / "logs"
    _BASE_ACTIONS = {"/api/switch": "switch", "/api/remove": "remove",
                     "/api/refresh": "refresh", "/api/add": "add",
                     "/api/checkin": "checkin", "/api/refresh-all": "refresh-all",
                     "/api/open-client": "open-client"}
    AUDIT_ACTIONS = dict(_BASE_ACTIONS)
    AUDIT_ACTIONS.update({"/api/wbai" + p[len("/api"):]: a for p, a in _BASE_ACTIONS.items()})
    APP_NAME = "wb_switcher"
    # 把启动器也算进版本戳：它不 import 本模块，但改动同样影响行为 ——
    # 只改启动器时版本戳不变的话，stale 检测（"在跑的实例是不是旧代码"）会漏判
    # （实测：给启动器加一次性令牌，重启后 version 仍是旧值，只能靠行为验证 ——
    #  见 AUDIT_2026-09-24.md 第二十章）。打包态下这个路径 stat 不到，
    # source_version 会自己回退到 exe 自身。
    APP_VERSION = common.source_version(
        __file__, "wb",
        extra_paths=(str(Path(__file__).with_name("ui_app.py")),))
    STARTED_AT = int(time.time())

    # --- 通道路由 -------------------------------------------------------
    @staticmethod
    def _route(path):
        """把 /api/wbai/xxx 拆成 (wbai 通道, "/api/xxx")；其余原样归国服。

        白名单式匹配：只有注册在 CHANNELS 里的 key 才会被认出来，
        `/api/whatever/switch` 不会被误当成某个通道。
        """
        for key in CHANNELS:
            if key != DEFAULT_CHANNEL and path.startswith("/api/%s/" % key):
                return channel(key), "/api/" + path[len("/api/%s/" % key):]
        return CHANNELS[DEFAULT_CHANNEL], path

    def audit_source(self, path=""):
        return self._route(path)[0].source

    def api_get(self, u):
        ch, path = self._route(u.path)
        if path == "/api/accounts":
            return 200, {"ok": True, "accounts": list_accounts(ch)}
        if path == "/api/current":
            return 200, {"ok": True, "current": current_account(ch)}
        if path == "/api/live-token":
            # 登录态文件被字段加密（5.6.2+）时的降级读取：从客户端进程内存里取
            # 当前凭据。只读、不落盘；返回**脱敏**摘要（token 只给前缀和长度），
            # 免得凭据进了前端状态或审计日志。
            # want_refresh=True：这是诊断接口，用户要看 refresh 的签发/到期情况，
            # 所以扫到 access + refresh 都拿到为止（功能路径不这么做，见 live_session 注释）。
            session = live_session(ch, want_refresh=True)
            if not session:
                return 200, {"ok": False,
                             "reason": "读不到客户端进程里的凭据"
                                       "（客户端没在跑，或它以更高权限运行）"}
            return 200, {"ok": True, "session": redact_session(session)}
        if path == "/api/credits":
            # ?force=1 强制回源，否则走 CREDITS_TTL 秒的进程内缓存
            if not ch.supports("credits"):
                return 200, {"ok": True, "accounts": [], "ts": "", "unsupported": True}
            return 200, credits_snapshot(force="force=1" in (u.query or ""), ch=ch)
        if path == "/api/checkin-status":
            # 只读查签到状态。必须与 POST /api/checkin 分开：
            # BaseHandler 对 WRITE_ENDPOINTS 里的路径一律拒绝 GET（405）。
            if not ch.supports("checkin"):
                return 200, {"ok": True, "accounts": [], "ts": "", "unsupported": True}
            return 200, checkin_snapshot(force="force=1" in (u.query or ""), ch=ch)
        if path == "/api/client-status":
            # 只读状态必须与写接口分开：/api/open-client 在 WRITE_ENDPOINTS 里，
            # do_GET 对它的路径一律返回 405（和 /api/checkin 同一个坑）。
            # 国际服不代管客户端进程 → 不能把国服的进程状态当成它的返回（与 credits 同款处理）。
            if not ch.supports("open_client"):
                return 200, {"ok": True, "client": None, "unsupported": True}
            return 200, {"ok": True, "client": client_status(ch)}
        if path == "/api/migrate-preview":
            # 只读扫描：切到这个账号需要迁移多少数据（供弹框展示）。
            # 国际服的拒绝由 migrate_preview() 自己给出 —— 守卫写在函数里，
            # 这样 CLI（--migrate-preview）与这里行为一致，不会一边挡一边漏。
            return 200, migrate_preview(dict(parse_qs(u.query or "")).get("name", [""])[0], ch)
        return 404, {"ok": False, "message": "404"}

    def api_post(self, u, p):
        ch, path = self._route(u.path)
        if path == "/api/open-client":
            # 进程名已按通道区分（Channel.client_processes），这里不会再拉错程序。
            # 国际服仍拒：页面上的「打开客户端」按钮两个通道都不显示，接口没必要对外。
            # （迁移流程内部直接调 open_client(ch) 函数，不走这个接口，不受影响。）
            if not ch.supports("open_client"):
                return 400, {"ok": False, "message": "%s不代管客户端进程" % ch.server}
            ok, msg = open_client(ch)
            # fresh=True：这是动作接口，用户刚点过「打开客户端」，必须给当下真实状态
            return 200, {"ok": ok, "message": msg, "client": client_status(ch, fresh=True)}
        if path == "/api/switch":
            mig = p.get("migrate")
            if not isinstance(mig, dict):
                mig = None            # 不传 = 仅切换（保持旧行为）
            ok, msg, mig_result = switch_account(str(p.get("name") or ""), mig, ch)
            return 200, {"ok": ok, "message": msg, "migration": mig_result}
        elif path == "/api/remove":
            ok, msg = remove_account(str(p.get("file") or ""), ch)
        elif path == "/api/refresh":
            ok, msg = refresh_account_file(str(p.get("file") or ""), True, ch)
        elif path == "/api/add":
            ok, msg = add_account(str(p.get("name") or ""), str(p.get("content") or ""), ch)
        elif path == "/api/checkin":
            # 一键签到：对账号库全部可用账号各签一次。
            # 这里的 400 是**接口契约**（前端按状态码判断），函数内部的守卫是
            # 非 HTTP 调用方的兜底 —— 两处条件相同，但返回形态不同，都要留。
            if not ch.supports("checkin"):
                return 400, {"ok": False, "message": "%s不支持一键签到" % ch.server}
            return 200, checkin_all(ch)
        elif path == "/api/refresh-all":
            # 一键续期：跳过门卫，无条件全刷（同上：400 是契约，函数内另有守卫）
            if not ch.supports("refresh"):
                return 400, {"ok": False, "message": "%s暂不支持一键续期" % ch.server}
            return 200, refresh_all_ui(ch)
        else:
            return 404, {"ok": False, "message": "404"}
        return 200, {"ok": ok, "message": msg}


def rebind(base_dir, script_dir=None):
    """把**所有**数据目录基准一次性指到 base_dir（冻结态启动器调用）。

    ⚠️ 只能走这一个入口，不要在启动器里逐个写 `srv.X = ...`。
    本模块的数据目录基准散在 4 个名字上（`_BIN_DIR` / `SCRIPT_DIR` / `LOCK_DIR` /
    `Handler.BASE_DIR` / `Handler.AUDIT_DIR`），逐个赋值的写法漏一个就是
    「账号库在一个地方、锁和备份在另一个地方」这类**静默分家** ——
    而冻结态 exe 恰恰是最难复现的场景（源码运行永远正常）。
    以前启动器各写 5 行赋值，那 5 行就是漏改的来源。

    账号库/桌面端登录态本身已改成按 `_BIN_DIR` 现算（`Channel.auth_dir` 属性），
    这里只需管那些"必须落在模块/类属性上"的名字。
    """
    global _BIN_DIR, SCRIPT_DIR, LOCK_DIR
    _BIN_DIR = Path(base_dir)
    if script_dir is not None:
        SCRIPT_DIR = Path(script_dir)
    LOCK_DIR = _BIN_DIR / ".locks"
    Handler.BASE_DIR = _BIN_DIR          # 前端页面按 exe 目录 → _internal 依次查找
    Handler.AUDIT_DIR = _BIN_DIR / "logs"


_WARMED = threading.Event()


def warmup_async():
    """后台预热「积分」与「签到」缓存。

    首屏"完全加载"的耗时几乎全在这两个查询上（实测积分 ~680ms、签到 ~430ms，
    前端已经并行，所以首屏要等 ~700ms）。服务在浏览器打开之前就起来了 ——
    这里抢在用户点开页面之前把它们跑完，用户看到页面时缓存已热，两项都是 0ms。

    两个查询各自内部已并发；这里再分成两个线程，让它们同时开始，
    这样 ~0.7s 后两个缓存都热了（串行要 ~1.1s，会慢过用户打开页面）。
    失败只打印，不影响服务本身。
    """
    if _WARMED.is_set():
        return
    _WARMED.set()

    def _one(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001  预热失败不该影响任何功能
            print("[预热] %s 失败（不影响使用）：%s" % (name, common.scrub(e, 120)), flush=True)

    # 预热要覆盖**每个通道**：两个通道的网关与账号库都不同，缓存也是按通道分区的
    # （见 _CREDITS_CACHE / _CHECKIN_CACHE），只热国服的话国际服首屏仍要现查 ~0.7s。
    # 只对**声明了该能力**的通道发请求 —— 没能力的（如国际服签到）发了也是白跑。
    jobs = []
    for key in CHANNELS:
        _ch = CHANNELS[key]
        if _ch.supports("credits"):
            jobs.append(("%s·积分" % key, lambda c=_ch: credits_snapshot(False, c)))
        if _ch.supports("checkin"):
            jobs.append(("%s·签到" % key, lambda c=_ch: checkin_snapshot(False, c)))
    for name, fn in jobs:
        threading.Thread(target=_one, args=(name, fn), name="warmup-" + name,
                         daemon=True).start()


def serve(port=DEFAULT_PORT, open_browser=True, use_token=True):
    # 部署用：把账号目录建出来（含占位提示），用户拷走 exe 双击就能看到该往哪放文件。
    ensure_auth_dirs()
    # 单实例保护：拿到互斥体才允许启动；已有实例则复用或报错退出，**绝不静默另起一个**。
    # 见 DESIGN_single_instance.md。
    handle, action, info = common.single_instance_guard(
        common.MUTEX_NAME_WB, port, tries=common.PORT_TRIES, log=print,
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
        # 只在"默认端口被**别人的程序**占用"时才会走到这里（自己的实例已被上面的
        # 守卫拦下），所以必须把实际端口打显眼 —— 否则用户不知道页面在哪。
        print("[提示] 默认端口 %d 被其它程序占用，已改用 %d（本实例唯一）" % (DEFAULT_PORT, port), flush=True)
    url = "http://127.0.0.1:%d/" % port
    print("本地服务已启动：%s  (Ctrl+C 停止)" % url, flush=True)
    # 抢在浏览器打开之前把积分/签到缓存跑热，首屏就不用等那 ~700ms
    warmup_async()
    if open_browser:
        # 用 open_page 而不是裸 webbrowser.open：只有"当前没有页面开着"才开，
        # 否则每次运行都会多一个指向同一地址的标签页。
        threading.Timer(0.6, lambda: common.open_page(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0




def _print_json(obj):
    """CLI 的 JSON 输出统一出口；实现在 switcher_common，两个切换器共用。

    GBK 控制台下 emoji 昵称会让裸 `print(json.dumps(..., ensure_ascii=False))`
    直接抛 UnicodeEncodeError（详见 common.print_json 的注释）。
    """
    common.print_json(obj)


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 账号切换器后端")
    ap.add_argument("--channel", choices=sorted(CHANNELS), default=DEFAULT_CHANNEL,
                    help="目标通道：wb=WorkBuddy 国服（默认），wbai=WorkBuddyAI 国际服")
    ap.add_argument("--list", action="store_true", help="列出账号库可用账号")
    ap.add_argument("--current", action="store_true", help="查看桌面端当前账号")
    ap.add_argument("--live-token", action="store_true",
                    help="从客户端进程内存读取当前凭据（登录态文件被字段加密时的降级路径；"
                         "输出已脱敏，只给前缀与长度）")
    ap.add_argument("--switch", metavar="NAME", help="切换为该通道账号库里的 NAME.info")
    ap.add_argument("--migrate", action="store_true",
                    help="配合 --switch：切换后把旧账号的本地数据改归属到新账号（仅国服）")
    ap.add_argument("--migrate-mode", choices=("move", "share"), default="move",
                    help="move=改归属到新账号（默认）；share=置空 user_id，两边都能看到")
    ap.add_argument("--migrate-preview", metavar="NAME",
                    help="只读扫描：切到该账号需要迁移多少数据（JSON，仅国服）")
    ap.add_argument("--serve", action="store_true", help="启动本地 HTTP 服务")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="HTTP 服务端口（默认 %d，被占用时自动顺延）" % DEFAULT_PORT)
    ap.add_argument("--prune", metavar="N", type=int, nargs="?", const=DESKTOP_BACKUP_KEEP,
                    help="清理桌面端切号备份，只保留最近 N 份（默认 %d）" % DESKTOP_BACKUP_KEEP)
    ap.add_argument("--no-open", action="store_true",
                    help="只跑服务，不打开浏览器（脚本/测试用；正常双击启动脚本不需要）")
    ap.add_argument("--no-auth", action="store_true",
                    help="关闭一次性访问令牌（写操作将只依赖回环 + 同源校验）")
    ap.add_argument("--refresh-all", action="store_true",
                    help="对账号库全部账号跑一遍续期（供计划任务调用；默认走门卫，见 --force）")
    ap.add_argument("--force", action="store_true",
                    help="配合 --refresh-all：跳过门卫（剩余天数阈值 + 冷却），无条件全刷")
    args = ap.parse_args()
    ch = channel(args.channel)

    if args.prune is not None:
        n = common.prune_backups(ch.desktop_dir, ch.backup_glob,
                                 keep=max(0, int(args.prune)),
                                 exclude={ch.info_name})
        print("已清理 %d 份 %s 桌面端备份（保留最近 %d 份）" % (n, ch.title, args.prune))
        common.audit(Handler.AUDIT_DIR, ch.source, "prune",
                     "keep=%s" % args.prune, True, "清理 %d 份" % n)
        return 0

    if args.refresh_all:
        return refresh_all(force=args.force, ch=ch)

    if args.list:
        _print_json({"accounts": list_accounts(ch)})
        return 0
    if args.current:
        _print_json({"current": current_account(ch)})
        return 0
    if args.live_token:
        session = live_session(ch, want_refresh=True)
        if not session:
            _print_json({"ok": False,
                         "reason": "读不到客户端进程里的凭据（客户端没在跑，或它以更高权限运行）"})
            return 1
        _print_json({"ok": True, "session": redact_session(session)})
        return 0
    if args.switch:
        mig = {"enabled": True, "mode": args.migrate_mode} if args.migrate else None
        ok, msg, mig_result = switch_account(args.switch, mig, ch)
        common.audit(Handler.AUDIT_DIR, ch.source, "switch", args.switch, ok, msg)
        _print_json({"ok": ok, "message": msg, "migration": mig_result})
        return 0 if ok else 1
    if args.migrate_preview:
        out = migrate_preview(args.migrate_preview, ch)
        _print_json(out)
        # 能力不支持 = **命令级失败**（与 --refresh-all 同款），必须给出非零退出码：
        # 以前这里恒 return 0，脚本用 `$?` 判断时会以为这次扫描成功。
        # 账号不存在 / 新旧账号相同这类仍回 0 —— JSON 里的 ok/message 已足够表达，
        # 它们不是「用错了通道」这种硬错误。
        return 1 if out.get("unsupported") else 0
    return serve(port=args.port, use_token=not args.no_auth,
                 open_browser=not args.no_open) or 0


if __name__ == "__main__":
    sys.exit(main())