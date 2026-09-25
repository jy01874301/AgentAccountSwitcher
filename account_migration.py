"""account_migration.py —— 切换账号时把本地数据「改归属」到新账号。

设计依据与全部实测结论见 DESIGN_account_migration.md。核心事实：

- 对话正文（`projects/<cwd-slug>/<会话ID>.jsonl`）按 **cwd + 会话ID** 存，与账号无关，
  **不需要搬**。所以"迁移"= 改归属键，不是复制文件。
- `session_id` 是所有派生数据的键，**必须原样保留**。
- 真正账号级的只有几处：`sessions.user_id`、`automations.owner_user_id`、
  `memory/<uid>_memory.md`、`storage/user-<uid>-<type>/`、`connectors/<uid>/`、
  `settings.json` 的 `claw.users.<uid>`、`storage/skeleton/account-snapshot.json`。
- 客户端查询是**惰性实时读**的（`currentUserId()` 注释：账号切换后查询立刻生效），
  所以改完库不需要重启；但 `account-snapshot.json` 是冷启动读的，那一份要重启才刷新。

⚠️ 本模块会写用户的真实对话库。所有写操作都遵守：
  1. 动手前先做**一致性快照**（SQLite Backup API，不能裸 copy —— 有 WAL）；
  2. 每一步都记进 manifest，失败可按 manifest 反向回滚；
  3. 回滚失败就**保留现场**并在错误里给出路径，绝不静默吞掉。
"""

import datetime
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import switcher_common as common

# ---------------------------------------------------------------------------
# 数据目录：本机可能有**多个**客户端，必须挑对那一个
# ---------------------------------------------------------------------------
# ⚠️ 这是本模块最容易搞错的地方。本机实测存在两个结构完全相同的客户端数据目录：
#
#   ~/.workbuddy/      account-snapshot.uid = 33333333…（= workbuddy-desktop.info 的账号）
#   ~/.workbuddy-ai/   account-snapshot.uid = 66666666…（= workbuddy-desktop-ai.info，AI 端）
#
# 切换器管的是 `workbuddy-desktop.info`，所以**该迁的是 ~/.workbuddy/**。
# 而 `WORKBUDDY_CONFIG_DIR` 在切换器进程里未必存在、在别的进程里可能是 `-ai`，
# 照抄它就会迁移错目录 —— 那是最坏的一类 bug：看起来成功，实际动了别人的数据。
#
# 所以目录判定以**数据自身的归属标识**为准，不信任环境变量：
#   1) storage/skeleton/account-snapshot.json 的 primary.uid == 目标 uid
#   2) workbuddy.db 里存在该 uid 的会话
#   3) 回退：WORKBUDDY_CONFIG_DIR → ~/.workbuddy → ~/.workbuddy-ai
ROOT_OVERRIDE = None      # 测试用：指向临时目录，绝不碰真实数据
CANDIDATE_DIR_NAMES = (".workbuddy", ".workbuddy-ai")


def candidate_roots():
    """按优先级列出候选数据目录（只保留存在的）。"""
    cands = []
    env = (os.environ.get("WORKBUDDY_CONFIG_DIR") or "").strip()
    if env:
        cands.append(Path(env))
    for name in CANDIDATE_DIR_NAMES:
        p = Path.home() / name
        if p not in cands:
            cands.append(p)
    return [p for p in cands if p.is_dir()]


def snapshot_uid(d):
    """读某个数据目录里 account-snapshot.json 的 uid（读不到返回空串）。"""
    p = Path(d) / "storage" / "skeleton" / "account-snapshot.json"
    if not p.is_file():
        return ""
    try:
        return ((json.loads(p.read_text(encoding="utf-8")).get("primary") or {})
                .get("uid") or "")
    except (OSError, ValueError):
        return ""


def _db_has_uid(db, uid):
    if not uid or not Path(db).is_file():
        return False
    try:
        con = sqlite3.connect("file:%s?mode=ro" % Path(db).as_posix(), uri=True)
        try:
            return bool(con.execute("SELECT 1 FROM sessions WHERE user_id=? LIMIT 1",
                                    (uid,)).fetchone())
        finally:
            con.close()
    except sqlite3.Error:
        return False


def find_data_root(uid=""):
    """找到与 uid 对应的客户端数据目录。

    判定顺序见文件头注释。找不到匹配项时回退到候选里的第一个（并保证它是
    一个真实存在、带 workbuddy.db 的目录），绝不返回一个"看起来对"的空目录。
    """
    if ROOT_OVERRIDE:
        return Path(ROOT_OVERRIDE)
    cands = candidate_roots()
    if not cands:
        return Path.home() / CANDIDATE_DIR_NAMES[0]
    if uid:
        for d in cands:                      # 1) 快照归属
            if snapshot_uid(d) == uid:
                return d
        for d in cands:                      # 2) 库里有没有这个 uid 的会话
            if _db_has_uid(d / "workbuddy.db", uid):
                return d
    for d in cands:                          # 3) 回退：优先带 workbuddy.db 的
        if (d / "workbuddy.db").is_file():
            return d
    return cands[0]


def _db_path(r):
    return Path(r) / "workbuddy.db"


# ---------------------------------------------------------------------------
# 客户端进程检测 —— 迁移的硬前提
# ---------------------------------------------------------------------------
CLIENT_PROCESS_NAMES = ("workbuddyai.exe", "codebuddy.exe", "workbuddy.exe")


INSTANCE_PORT = 8765      # 切换器默认端口；迁移前用它确认没有第二个实例


def other_instance():
    """返回**另一个**切换器实例的 /api/ping 信息；没有则 None。

    为什么迁移要管这个：两个实例的内存态各自独立。A 在迁移时，B 的"当前账号"
    缓存会在迁移后失效，B 继续切号/续期就可能把它的内存态写回，覆盖迁移结果。
    文件锁能串行化单次操作，但锁不住"B 拿着过期状态继续干活"。
    """
    try:
        info = common.probe_instance_range(INSTANCE_PORT, tries=common.PORT_TRIES, timeout=0.4)
    except Exception:  # noqa: BLE001
        return None
    if info and str(info.get("pid")) != str(os.getpid()):
        return info
    return None


def running_clients(names=None):
    """返回正在运行的客户端进程名列表。

    `names` 用来**按通道限定**要查哪些进程名：国服与国际服是两个不同的程序，
    提示里点名一个本次根本不会去关的客户端（"迁移前会自动关闭它"）是假话。
    不传 = 沿用 CLIENT_PROCESS_NAMES 全量（老行为）。

    返回 None 表示**检测不出来**（tasklist 不可用等），调用方应据此提示用户
    自行确认，而不是当成"没在跑"。实现已抽到 `common.list_process_names()`
    —— 那里集中处理了"GBK 输出 + UTF-8 解码会在读取线程里炸"这个坑。
    """
    running = common.list_process_names()
    if running is None:
        return None
    return sorted(n for n in (CLIENT_PROCESS_NAMES if names is None else names)
                  if n in running)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _safe(uid):
    """把 uid 变成可用在 glob / 目录名里的形式（与客户端 sanitizeFilename 同规则）。"""
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(uid or "")).strip()


def _connect(db):
    con = sqlite3.connect(str(db), timeout=15)
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _table_exists(con, name):
    row = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (name,)).fetchone()
    return bool(row)


def _count(con, sql, args=()):
    try:
        return con.execute(sql, args).fetchone()[0]
    except sqlite3.Error:
        return 0


def _now_stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _replace(src, dst, tries=6, delay=0.15):
    """改名/移动带重试。实现已提到 `common.replace_with_retry`
    共用（切号那边也需要），这里保留名字避免改动 7 处调用点。"""
    return common.replace_with_retry(src, dst, tries=tries, delay=delay)


# ---------------------------------------------------------------------------
# connectors：userIdCheck 是纯 sha256(uid + salt) 前 16 字节的 base64
# ---------------------------------------------------------------------------
def compute_check(input_bytes, salt, nbytes=16):
    """复刻客户端 computeCheck：base64( sha256(input + salt)[:nbytes] )。

    实测 4/4 账号复现一致。它**不含密钥**，uid 也不参与 AES 密钥派生
    （AES key 来自 masterKey，由 keyCheck 校验），所以改 uid 后重算即可。
    """
    import base64
    import hashlib
    return base64.b64encode(hashlib.sha256(input_bytes + salt).digest()[:nbytes]).decode()


def _fix_connector_states(path, new_uid):
    """把 connector-states.json 的归属改成 new_uid，并重算 userIdCheck。

    不做这一步的后果不是"解不开"，而是 verifyHeader 返回 userId-mismatch，
    调用方会**删主文件并引导重新授权** —— 等于把用户的连接器配置删掉。
    """
    import base64
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    enc = data.get("encryption") or {}
    salt_b64 = enc.get("salt")
    if not salt_b64:
        return False, "缺少 encryption.salt，无法重算校验值"
    try:
        salt = base64.b64decode(salt_b64)
    except Exception as e:  # noqa: BLE001
        return False, "salt 不是合法 base64：%s" % e
    # accountIdentityKey 是 `<uid>|<企业简称>|<账号类型>` 三段（企业简称为空时形如
    # `<uid>||enterprise`）。**不能用 split("||") 取 uid** —— 企业简称非空时
    # （实测有 `<uid>|fwhxtrpramm8|enterprise`）会取到整串，算出的校验值必然不符，
    # 于是 verifyHeader 判 userId-mismatch，把用户的连接器配置删掉。
    parts = (data.get("accountIdentityKey") or "").split("|")
    rest = parts[1:] if len(parts) > 1 else ["", "personal"]
    data["accountIdentityKey"] = "|".join([new_uid] + rest)
    nbytes = 16
    if enc.get("userIdCheck"):
        try:
            nbytes = len(base64.b64decode(enc["userIdCheck"])) or 16
        except Exception:  # noqa: BLE001
            nbytes = 16
    enc["userIdCheck"] = compute_check(new_uid.encode("utf-8"), salt, nbytes)
    data["encryption"] = enc
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _replace(tmp, path)
    return True, ""


# ---------------------------------------------------------------------------
# 扫描（只读）
# ---------------------------------------------------------------------------
def scan(old_uid, new_uid, client_names=None):
    """扫描旧账号名下待迁数据。返回 dict，供弹框展示与冲突提示。

    `client_names` 透传给 running_clients()：只报**本次会去关的那个**客户端，
    别把另一个通道的客户端算进来（它在跑也跟这次迁移无关）。
    """
    r = find_data_root(old_uid)
    out = {
        "ok": True, "old_uid": old_uid or "", "new_uid": new_uid or "",
        "root": str(r),
        "sessions": 0, "running_sessions": 0, "automations": 0,
        "has_memory": False, "storage_dirs": [], "has_connectors": False,
        "snapshot_is_old": False, "settings_key": False,
        "projects": 0, "client_running": running_clients(client_names),
        "conflicts": [], "warnings": [], "client_note": "",
    }
    if not old_uid or not new_uid:
        out["ok"] = False
        out["warnings"].append("缺少账号 uid，无法迁移")
        return out
    if old_uid == new_uid:
        out["ok"] = False
        out["warnings"].append("目标账号与当前账号相同，无需迁移")
        return out

    db = _db_path(r)
    if db.is_file():
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
            try:
                out["sessions"] = _count(
                    con, "SELECT COUNT(*) FROM sessions WHERE user_id=? AND deleted_at IS NULL",
                    (old_uid,))
                out["running_sessions"] = _count(
                    con, "SELECT COUNT(*) FROM sessions WHERE user_id=? AND deleted_at IS NULL "
                         "AND status='working'", (old_uid,))
                if _table_exists(con, "automations"):
                    out["automations"] = _count(
                        con, "SELECT COUNT(*) FROM automations WHERE owner_user_id=? "
                             "AND deleted_at IS NULL", (old_uid,))
                # 冲突：新账号名下是否已有数据
                n_new = _count(con, "SELECT COUNT(*) FROM sessions WHERE user_id=? "
                                    "AND deleted_at IS NULL", (new_uid,))
                if n_new:
                    out["conflicts"].append("新账号名下已有 %d 个会话（按会话 ID 去重，不会覆盖）" % n_new)
                out["projects"] = _count(con, "SELECT COUNT(DISTINCT cwd) FROM sessions "
                                              "WHERE user_id=? AND deleted_at IS NULL", (old_uid,))
            finally:
                con.close()
        except sqlite3.Error as e:
            out["warnings"].append("读取 workbuddy.db 失败：%s" % e)
    else:
        out["warnings"].append("找不到 %s" % db)

    out["has_memory"] = (r / "memory" / ("%s_memory.md" % _safe(old_uid))).is_file()
    if (r / "memory" / ("%s_memory.md" % _safe(new_uid))).is_file():
        out["conflicts"].append("新账号已有记忆文件（默认按日期分节合并）")

    for d in sorted((r / "storage").glob("user-%s-*" % _safe(old_uid))):
        out["storage_dirs"].append(d.name)
    if out["storage_dirs"]:
        for name in out["storage_dirs"]:
            new_name = name.replace("user-%s-" % _safe(old_uid), "user-%s-" % _safe(new_uid), 1)
            if (r / "storage" / new_name).is_dir():
                out["conflicts"].append("新账号已有 %s（按 key 粒度合并，目标侧优先）" % new_name)

    out["has_connectors"] = (r / "connectors" / _safe(old_uid) / "connector-states.json").is_file()
    if (r / "connectors" / _safe(new_uid) / "connector-states.json").is_file():
        out["conflicts"].append("新账号已有连接器状态（会保留新账号那份）")

    snap = r / "storage" / "skeleton" / "account-snapshot.json"
    if snap.is_file():
        try:
            j = json.loads(snap.read_text(encoding="utf-8"))
            out["snapshot_is_old"] = ((j.get("primary") or {}).get("uid") == old_uid)
        except (OSError, ValueError):
            out["warnings"].append("account-snapshot.json 无法解析")

    settings = r / "settings.json"
    if settings.is_file():
        try:
            j = json.loads(settings.read_text(encoding="utf-8"))
            out["settings_key"] = old_uid in ((j.get("claw") or {}).get("users") or {})
        except (OSError, ValueError):
            out["warnings"].append("settings.json 无法解析")

    if out["client_running"] is None:
        out["warnings"].append("无法检测客户端进程，迁移前请自行确认已退出 WorkBuddy")
    elif out["client_running"]:
        # 不再当成"警告"：迁移前会自动关掉它（见 switch_account 的第 0 步），
        # 所以这是**说明**而不是拦阻。前端据此显示提示、不再禁用按钮。
        out["client_note"] = ("检测到 WorkBuddy 客户端正在运行（%s）。"
                              "迁移前会自动关闭它，完成后重新打开。"
                              % "、".join(out["client_running"]))
    return out


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------
DEFAULT_OPTS = {
    "sessions": True,      # 会话与任务（改归属）
    "memory": True,        # 账号记忆
    "storage": True,       # 账号级设置（含置顶）
    "connectors": True,    # 连接器状态（含 userIdCheck 重算）
    "settings": True,      # settings.json 的 claw.users 键
    "snapshot": True,      # account-snapshot.json
    "mode": "move",        # move=改归属到新账号；share=置空 user_id 让两边都能看到
    "allow_client_running": False,
    "allow_other_instance": False,   # 仅供自检/演练使用，正式路径不要打开
}


def _norm_opts(opts):
    o = dict(DEFAULT_OPTS)
    for k, v in (opts or {}).items():
        if k in o:
            o[k] = v
    if o["mode"] not in ("move", "share"):
        o["mode"] = "move"
    return o


class _Journal:
    """记录每一步做了什么，供回滚使用。"""

    def __init__(self, backup_dir):
        self.backup = Path(backup_dir)
        self.renames = []        # (from, to) 目录改名，回滚 = 反向
        self.restore = []        # (backup_file, target_file) 文件还原
        self.merged_files = []   # 合并 storage 时**新建**的目标文件，回滚 = 删除
        self.db_backup = None
        self.steps = []
        self.notes = []

    def add_step(self, name, ok, detail=""):
        self.steps.append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})
        # 每记一步就刷一次盘 —— 比在每个调用点手动 flush 更不容易漏，
        # 代价是一次 1KB 级的小文件写。见 flush() 的说明。
        self.flush()

    def flush(self):
        """把当前进度增量落盘到 manifest.json。

        ⚠️ 以前 manifest 只在**全部成功之后**（第 9 步）写一次，而文件头宣称
        「每一步都记进 manifest，失败可按 manifest 反向回滚」—— 正常失败还能靠
        内存里的 journal 回滚，但**进程被杀 / 断电 / 被强杀**时磁盘上没有任何
        恢复依据，只剩一份被改了一半的账号数据，比「失败并回滚」更难排查。
        现在每个关键步之后都刷一次。
        """
        try:
            self.backup.mkdir(parents=True, exist_ok=True)
            (self.backup / "manifest.json").write_text(
                json.dumps(self.manifest(), ensure_ascii=False, indent=2), encoding="utf-8")
        except BaseException:  # noqa: BLE001  含 SystemExit（安全删除 shim）
            pass

    def manifest(self):
        return {
            "backup_dir": str(self.backup),
            "db_backup": self.db_backup,
            "renames": [[str(a), str(b)] for a, b in self.renames],
            "restore": [[str(a), str(b)] for a, b in self.restore],
            "merged_files": [str(p) for p in self.merged_files],
            "steps": self.steps,
            "notes": self.notes,
        }


def _backup_db(r, dest):
    """用 SQLite Backup API 做一致性快照。

    有 4 MB WAL 时**裸 copy 会得到损坏的快照**，回滚就无从谈起，所以必须走
    Backup API（它会把 WAL 里的内容一并合进去）。
    """
    src = _db_path(r)
    if not src.is_file():
        return False, "找不到 workbuddy.db"
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(src), timeout=20)
    try:
        out = sqlite3.connect(str(dest))
        try:
            con.backup(out)
            out.commit()
        finally:
            out.close()
    finally:
        con.close()
    return True, str(dest)


def _copy_tree(src, dst):
    if Path(dst).exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)


def _merge_memory(old_file, new_file):
    """两份记忆合并：按日期分节去重。返回 (ok, detail)。"""
    def sections(p):
        text = Path(p).read_text(encoding="utf-8", errors="replace")
        parts, cur = [], []
        for line in text.splitlines():
            if line.startswith("#") and cur:
                parts.append("\n".join(cur))
                cur = [line]
            else:
                cur.append(line)
        if cur:
            parts.append("\n".join(cur))
        return parts

    a = sections(old_file)
    b = sections(new_file) if Path(new_file).is_file() else []
    seen, merged = set(), []
    for sec in a + b:
        key = re.sub(r"\s+", "", sec)[:120]
        if key in seen:
            continue
        seen.add(key)
        merged.append(sec.rstrip())
    Path(new_file).write_text("\n\n".join(merged) + "\n", encoding="utf-8")
    return True, "合并 %d + %d 节 → %d 节" % (len(a), len(b), len(merged))


def _verify_db(r, uid):
    """以只读方式复核某个归属键下的会话数。"""
    con = sqlite3.connect("file:%s?mode=ro" % _db_path(r).as_posix(), uri=True)
    try:
        return _count(con, "SELECT COUNT(*) FROM sessions WHERE user_id=? AND deleted_at IS NULL",
                      (uid,))
    finally:
        con.close()


def _project_jsonl_index(r):
    """扫一遍 projects/*/，建立 sid -> jsonl 路径的索引（不猜 slug 规则）。"""
    idx = {}
    base = Path(r) / "projects"
    if not base.is_dir():
        return idx
    for d in base.iterdir():
        if not d.is_dir():
            continue
        for f in d.glob("*.jsonl"):
            idx[f.stem] = f
    return idx


def migrate(old_uid, new_uid, opts=None):
    """把 old_uid 名下的本地数据改归属到 new_uid。

    返回 dict：{ok, message, steps, warnings, backup_dir, rolled_back}
    """
    o = _norm_opts(opts)
    r = find_data_root(old_uid)
    journal = _Journal(r / ".migration-backup" / _now_stamp())
    result = {"ok": False, "message": "", "steps": [], "warnings": [],
              "backup_dir": str(journal.backup), "rolled_back": False, "scanned": {}}

    if not old_uid or not new_uid:
        result["message"] = "缺少账号 uid"
        return result
    if old_uid == new_uid:
        result["ok"] = True
        result["message"] = "目标账号与当前账号相同，无需迁移"
        return result

    # ---- 0. 前置检查 ----
    procs = running_clients()
    if procs is None:
        result["warnings"].append("无法检测客户端进程，已按「未运行」继续；若异常请先退出 WorkBuddy 重试")
    elif procs and not o["allow_client_running"]:
        result["message"] = ("检测到 WorkBuddy 仍在运行（%s）。迁移会改数据库与本地存储，"
                             "请先完全退出客户端再重试。" % "、".join(procs))
        return result
    other = other_instance()
    if other and not o.get("allow_other_instance"):
        result["message"] = ("检测到另一个切换器实例正在运行（pid %s，端口 %s）。"
                             "迁移会改数据库与本地存储，两个实例同时操作会让状态错乱 —— "
                             "请先关掉它（在它的窗口按 Ctrl+C）再重试。"
                             % (other.get("pid"), other.get("port")))
        return result
    journal.add_step("前置检查", True, "客户端进程：%s；其它切换器实例：%s"
                     % (procs if procs is not None else "未知",
                        other.get("pid") if other else "无"))

    # ---- 1. 快照 ----
    try:
        journal.backup.mkdir(parents=True, exist_ok=True)
        ok, detail = _backup_db(r, journal.backup / "workbuddy.db")
        journal.db_backup = str(journal.backup / "workbuddy.db") if ok else None
        journal.add_step("数据库快照", ok, detail)
        if not ok:
            result["message"] = "数据库快照失败：%s" % detail
            return result

        snap_targets = [
            ("memory", r / "memory" / ("%s_memory.md" % _safe(old_uid))),
            # 新账号那份也要备份：冲突时会与它**合并**，不改回去就等于回滚不彻底
            ("memory_new", r / "memory" / ("%s_memory.md" % _safe(new_uid))),
            ("settings.json", r / "settings.json"),
            ("account-snapshot.json", r / "storage" / "skeleton" / "account-snapshot.json"),
        ]
        for name, p in snap_targets:
            if Path(p).is_file():
                dst = journal.backup / "files" / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
                journal.restore.append((dst, p))
        for d in (r / "storage").glob("user-%s-*" % _safe(old_uid)):
            _copy_tree(d, journal.backup / "storage" / d.name)
        cdir = r / "connectors" / _safe(old_uid)
        if cdir.is_dir():
            _copy_tree(cdir, journal.backup / "connectors" / cdir.name)
        journal.add_step("文件快照", True, "%d 个文件 + storage/connectors 目录" % len(journal.restore))
    except Exception as e:  # noqa: BLE001
        journal.add_step("快照", False, e)
        result["message"] = "迁移前快照失败，已中止（未改动任何数据）：%s" % common.scrub(e)
        return result

    # ---- 2. 改数据库（一个事务） ----
    try:
        con = _connect(_db_path(r))
        try:
            con.execute("BEGIN IMMEDIATE")
            target_uid = "" if o["mode"] == "share" else new_uid
            n_sess = 0
            if o["sessions"]:
                cur = con.execute(
                    "UPDATE sessions SET user_id=? WHERE user_id=? AND deleted_at IS NULL "
                    "AND status<>'working'", (target_uid, old_uid))
                n_sess = cur.rowcount or 0
            n_auto = 0
            if _table_exists(con, "automations"):
                cur = con.execute("UPDATE automations SET owner_user_id=? WHERE owner_user_id=?",
                                  (new_uid, old_uid))
                n_auto = cur.rowcount or 0
                if _table_exists(con, "automation_delivery_outbox"):
                    con.execute("UPDATE automation_delivery_outbox SET owner_user_id=? "
                                "WHERE owner_user_id=?", (new_uid, old_uid))
            con.commit()
        finally:
            con.close()
        journal.add_step("数据库归属", True,
                         "会话 %d 个（模式 %s）、定时任务 %d 个" % (n_sess, o["mode"], n_auto))
        result["moved_sessions"] = n_sess
        result["moved_automations"] = n_auto
    except Exception as e:  # noqa: BLE001
        journal.add_step("数据库归属", False, e)
        return _rollback(r, journal, result, "改数据库失败：%s" % common.scrub(e))

    # ---- 3. 记忆 ----
    if o["memory"]:
        try:
            src = r / "memory" / ("%s_memory.md" % _safe(old_uid))
            dst = r / "memory" / ("%s_memory.md" % _safe(new_uid))
            if src.is_file():
                if dst.is_file():
                    _merge_memory(src, dst)
                    journal.add_step("账号记忆", True, "与新账号已有记忆合并")
                else:
                    journal.renames.append((src, dst))
                    _replace(src, dst)
                    journal.add_step("账号记忆", True, "已改名")
            else:
                journal.add_step("账号记忆", True, "无（跳过）")
        except Exception as e:  # noqa: BLE001
            journal.add_step("账号记忆", False, e)
            return _rollback(r, journal, result, "迁移记忆失败：%s" % common.scrub(e))

    # ---- 4. storage 目录 ----
    if o["storage"]:
        try:
            moved = []
            for d in sorted((r / "storage").glob("user-%s-*" % _safe(old_uid))):
                new_name = d.name.replace("user-%s-" % _safe(old_uid),
                                          "user-%s-" % _safe(new_uid), 1)
                dst = r / "storage" / new_name
                if dst.exists():
                    moved.append("%s → %s（合并）" % (d.name, new_name))
                    # ⚠️ 合并分支**不能只把旧目录删掉**。两处都踩过：
                    #   ① 以前写的是 `common.safe_unlink_tree` —— **那个函数全仓不存在**
                    #      （只有 safe_unlink / safe_rmtree），于是「新账号已有同名 storage
                    #      目录」这条分支（来回切号必现）在 _merge_storage_dir 已经复制完
                    #      之后必抛 AttributeError → 回滚、报「迁移 storage 失败」，
                    #      而内容其实已经并进新目录了。
                    #   ② 就算改成 safe_rmtree 也不够：回滚只认 journal.renames，
                    #      直接删掉等于旧目录永久消失。
                    #    改成**移进备份目录**并登记 renames，回滚就能靠已有的反向改名逻辑
                    #    把它放回去；合并时新建的文件另记一份，回滚时删掉。
                    parked = journal.backup / "storage-merged" / d.name
                    parked.parent.mkdir(parents=True, exist_ok=True)
                    journal.merged_files.extend(_merge_storage_dir(d, dst))
                    _replace(d, parked)
                    journal.renames.append((d, parked))
                else:
                    journal.renames.append((d, dst))
                    _replace(d, dst)
                    moved.append("%s → %s" % (d.name, new_name))
            journal.add_step("账号级设置", True, "；".join(moved) or "无（跳过）")
        except Exception as e:  # noqa: BLE001
            journal.add_step("账号级设置", False, e)
            return _rollback(r, journal, result, "迁移 storage 失败：%s" % common.scrub(e))

    # ---- 5. connectors ----
    if o["connectors"]:
        try:
            cdir = r / "connectors" / _safe(old_uid)
            if cdir.is_dir():
                dst = r / "connectors" / _safe(new_uid)
                if dst.exists():
                    journal.add_step("连接器状态", True, "新账号已有，保留新账号那份（跳过）")
                else:
                    st = cdir / "connector-states.json"
                    if st.is_file():
                        ok, why = _fix_connector_states(st, new_uid)
                        if not ok:
                            journal.add_step("连接器状态", False, why)
                            return _rollback(r, journal, result, "重算连接器校验值失败：%s" % why)
                    journal.renames.append((cdir, dst))
                    _replace(cdir, dst)
                    journal.add_step("连接器状态", True, "已改名并重算 userIdCheck")
            else:
                journal.add_step("连接器状态", True, "无（跳过）")
        except Exception as e:  # noqa: BLE001
            journal.add_step("连接器状态", False, e)
            return _rollback(r, journal, result, "迁移连接器失败：%s" % common.scrub(e))

    # ---- 6. settings.json 的账号键 ----
    if o["settings"]:
        try:
            sp = r / "settings.json"
            if sp.is_file():
                j = json.loads(sp.read_text(encoding="utf-8"))
                users = ((j.get("claw") or {}).get("users") or {})
                if old_uid in users:
                    users[new_uid] = users.pop(old_uid)
                    j.setdefault("claw", {})["users"] = users
                    tmp = sp.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")
                    _replace(tmp, sp)
                    journal.add_step("settings.json 账号键", True, "已改名到新 uid")
                else:
                    journal.add_step("settings.json 账号键", True, "无（跳过）")
            else:
                journal.add_step("settings.json 账号键", True, "文件不存在（跳过）")
        except Exception as e:  # noqa: BLE001
            journal.add_step("settings.json 账号键", False, e)
            return _rollback(r, journal, result, "迁移 settings 失败：%s" % common.scrub(e))

    # ---- 7. 账号快照 ----
    if o["snapshot"]:
        try:
            sp = r / "storage" / "skeleton" / "account-snapshot.json"
            if sp.is_file():
                j = json.loads(sp.read_text(encoding="utf-8"))
                prim = j.get("primary") or {}
                if prim.get("uid") == old_uid:
                    prim["uid"] = new_uid
                    if result.get("new_nickname"):
                        prim["nickname"] = result["new_nickname"]
                    prim["savedAt"] = int(time.time() * 1000)
                    j["primary"] = prim
                    tmp = sp.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")
                    _replace(tmp, sp)
                    journal.add_step("账号快照", True, "uid 已更新")
                else:
                    journal.add_step("账号快照", True, "不是旧账号（跳过）")
            else:
                journal.add_step("账号快照", True, "文件不存在（跳过）")
        except Exception as e:  # noqa: BLE001
            journal.add_step("账号快照", False, e)
            return _rollback(r, journal, result, "更新账号快照失败：%s" % common.scrub(e))

    # ---- 8. 校验 ----
    try:
        uid_check = new_uid if o["mode"] == "move" else ""
        got = _verify_db(r, uid_check) if o["sessions"] else 0
        idx = _project_jsonl_index(r)
        missing = []
        if o["sessions"]:
            con = sqlite3.connect("file:%s?mode=ro" % _db_path(r).as_posix(), uri=True)
            try:
                ids = [r[0] for r in con.execute(
                    "SELECT id FROM sessions WHERE user_id=? AND deleted_at IS NULL",
                    (uid_check,))]
            finally:
                con.close()
            missing = [i for i in ids if i not in idx]
        detail = "会话归属 %d 个" % got
        if missing:
            detail += "；⚠️ %d 个会话找不到正文 jsonl" % len(missing)
            result["warnings"].append(
                "%d 个会话在 projects/ 下找不到正文（cwd 可能变过）：%s"
                % (len(missing), ", ".join(missing[:3])))
        # ⚠️ 正文缺失**只算警告，不算失败**：本步真正要验的是「归属有没有改对」，
        # `_verify_db` 已经给出结论；「projects/ 下有没有对应 jsonl」是另一件事
        # —— cwd 变过、对话正文被清理过都会让正文不在，但归属迁移本身是成功的。
        # 历史上这里写的是 `not missing`，后果有两层（2026-09-21 实测确认）：
        #   1) 所有写操作都已落地且**未回滚**，却对外报 ok=False，
        #      切号 UI 渲染成「但数据迁移失败」，与实际状态相反；
        #   2) 下面 `if result["ok"]` 的裁剪被绕过 → `.migration-backup/` 无限堆积
        #      （每份含**完整对话库快照**），来回切 8 轮就到了 14 份。
        journal.add_step("校验", True, detail)
    except Exception as e:  # noqa: BLE001
        journal.add_step("校验", False, e)
        result["warnings"].append("校验阶段异常：%s" % common.scrub(e))

    # ---- 9. 落盘 manifest ----
    try:
        (journal.backup / "manifest.json").write_text(
            json.dumps(journal.manifest(), ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

    result["ok"] = all(s["ok"] for s in journal.steps)
    result["steps"] = journal.steps
    result["message"] = "迁移完成：%s" % "；".join(
        s["name"] + ("✓" if s["ok"] else "✗") for s in journal.steps)
    # 裁剪的唯一条件是「**没有回滚**」——即当前状态就是迁移后的状态。
    # 不能用 `result["ok"]`：ok 会被纯警告类步骤拉低（见上面「校验」那段），
    # 一旦被拉低就永远不裁剪，每一轮切号都新增一份完整对话库快照。
    # 回滚过了才需要保留现场供恢复；未回滚时备份没有留着的意义。
    if not result.get("rolled_back"):
        try:
            prune_migration_backups(r)
        except BaseException:  # noqa: BLE001
            pass
    return result


def _merge_storage_dir(src, dst):
    """把 src 里的文件按 key 粒度补进 dst（目标已有的不动）。

    **返回本次新建的目标文件列表** —— 合并是「复制」而不是「移动」，回滚时
    光把旧目录放回去还不够，这些多出来的副本也得删掉，否则新账号目录里会残留
    旧账号的设置。目标已存在的文件不动，所以返回的每一项都是「本来不存在」的，
    回滚删掉它们是安全的。
    """
    created = []
    for f in Path(src).rglob("*"):
        if f.is_dir():
            continue
        rel = f.relative_to(src)
        target = Path(dst) / rel
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        created.append(target)
    return created


def _rollback(r, journal, result, message):
    """按 manifest 反向回滚。回滚不了就保留现场并说明。"""
    problems = []
    # 目录改名反向
    for a, b in reversed(journal.renames):
        try:
            if Path(b).exists() and not Path(a).exists():
                _replace(b, a)
        except OSError as e:
            problems.append("还原 %s：%s" % (Path(b).name, common.scrub(e)))
    # 数据库整体还原
    if journal.db_backup and Path(journal.db_backup).is_file():
        try:
            for suffix in ("-wal", "-shm"):
                common.safe_unlink(str(_db_path(r)) + suffix)
            shutil.copy2(journal.db_backup, _db_path(r))
        except OSError as e:
            problems.append("还原数据库：%s" % common.scrub(e))
    # 单文件还原
    for src, dst in journal.restore:
        try:
            if Path(src).is_file():
                shutil.copy2(src, dst)
        except OSError as e:
            problems.append("还原 %s：%s" % (Path(dst).name, common.scrub(e)))
    # 合并 storage 时新建的副本要删掉：合并是「复制」不是「移动」，
    # 只把旧目录（renames 里那条）放回去的话，新账号目录里还会残留旧账号的设置。
    # 这些文件在合并时就是「目标本来不存在」的，删除是安全的。
    for p in getattr(journal, "merged_files", []):
        try:
            if Path(p).is_file():
                common.safe_unlink(str(p))
        except BaseException as e:  # noqa: BLE001  含 SystemExit（安全删除 shim）
            problems.append("清理合并残留 %s：%s" % (Path(p).name, common.scrub(e)))

    result["ok"] = False
    result["steps"] = journal.steps
    result["rolled_back"] = not problems
    if problems:
        result["message"] = ("%s；回滚未完全成功（%s）。现场保留在 %s，可手工恢复。"
                             % (message, "；".join(problems), journal.backup))
    else:
        result["message"] = "%s；已回滚到迁移前状态（备份保留在 %s）" % (message, journal.backup)
    return result


def prune_migration_backups(r, keep=3):
    """只保留最近 keep 份迁移备份。

    `.migration-backup/<时间戳>/workbuddy.db` 是**完整对话库快照**，
    迁移后没有必要长期堆积。

    调用时机：**只要这一轮没有回滚**就应该调用（见 `migrate()` 末尾）——
    此时磁盘状态就是迁移后的状态，备份不含"待恢复现场"的语义。
    注意判据是 `rolled_back`，**不是** `result["ok"]`：`ok` 会被纯警告类步骤
    （例如"个别会话找不到正文 jsonl"）拉低，用 ok 当门会导致备份永不裁剪。
    真正回滚过的那一轮才会 `return` 前保留现场、不走到这里。
    """
    base = Path(r) / ".migration-backup"
    if not base.is_dir():
        return 0
    dirs = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True)
    removed = 0
    for d in dirs[keep:]:
        if common.safe_rmtree(d):
            removed += 1
    return removed


def preview(old_uid, new_uid, client_names=None):
    """给弹框用的精简预览。`client_names` 透传给 scan()，见那里的说明。"""
    s = scan(old_uid, new_uid, client_names)
    items = []
    if s.get("sessions"):
        items.append({"key": "sessions", "label": "会话与任务",
                      "value": "%d 个" % s["sessions"],
                      "note": ("其中 %d 个正在运行，将跳过" % s["running_sessions"])
                              if s.get("running_sessions") else ""})
    if s.get("automations"):
        items.append({"key": "automations", "label": "定时任务", "value": "%d 个" % s["automations"]})
    if s.get("has_memory"):
        items.append({"key": "memory", "label": "账号记忆", "value": "1 份"})
    if s.get("storage_dirs"):
        items.append({"key": "storage", "label": "账号级设置",
                      "value": "%d 项" % len(s["storage_dirs"])})
    if s.get("has_connectors"):
        items.append({"key": "connectors", "label": "连接器状态", "value": "1 份"})
    if s.get("settings_key"):
        items.append({"key": "settings", "label": "渠道配置", "value": "1 组"})
    if s.get("snapshot_is_old"):
        items.append({"key": "snapshot", "label": "账号快照", "value": "需更新"})
    s["items"] = items
    s["projects_note"] = ("对话正文按项目目录存放，与账号无关，无需搬运 —— "
                          "本次只调整它在数据库里的归属")
    # needed 在**这里**算，不留在服务层：否则 preview() 与 migrate_preview()
    # 同名近名、返回结构却不同，调用方很容易调错那个拿不到 needed 的
    # （我自己做验证时就先调错了一次，见 AUDIT_2026-09-19.md 建议 6）。
    s["needed"] = bool(old_uid and new_uid and old_uid != new_uid
                       and (items or s.get("conflicts")))
    return s


if __name__ == "__main__":
    # 只读预览：python account_migration.py --preview <old_uid> <new_uid>
    if len(sys.argv) >= 4 and sys.argv[1] == "--preview":
        print(json.dumps(preview(sys.argv[2], sys.argv[3]), ensure_ascii=False, indent=2))
    else:
        print("用法: python account_migration.py --preview <old_uid> <new_uid>")
