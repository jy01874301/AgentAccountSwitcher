#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_ttl.py —— WorkBuddy 登录态有效期体检（只读，不联网、不写文件）。

服务端按登录通道（JWT 里的 token_source）签发不同有效期。2026-09-17 实测
（取自续期响应 data.expiresIn，与 JWT 的 exp 一致）：
    oneid_login        55 天 access / 60 天 refresh   ← 常规 OneID 登录
    enterprise_switch  30 天 access / 60 天 refresh   ← 客户端切到企业空间的会话
    （无该字段，早期令牌）55 天 / 60 天
续期只是沿用同一 token_source，所以短期会话续多少次都是短期。
TTL 由服务端下发、可能随时调整，本脚本始终以文件里的实际时间戳为准。

用法：
    python check_ttl.py                    # 扫描本目录 wb_auth 与 ../自动签到/wb_auth
    python check_ttl.py 目录1 目录2 ...
"""
import base64
import datetime
import json
import sys
from pathlib import Path

# 已知通道 → 说明
SOURCE_HINT = {
    "oneid_login": "长期（常规 OneID 登录，access 约 55 天）",
    "enterprise_switch": "短期（走「切换/绑定」换来的会话，access 约 30 天；重登也改不回来）",
}

MIN_TOKEN_LEN = 200  # 正常 JWT 远大于此；小于则视为内容被截断
LONG_REMAIN_DAYS = 40  # 无 token_source 时按剩余天数粗判：≥40 天视为长期通道


def jwt_payload(token):
    seg = str(token).split(".")
    if len(seg) < 2:
        return {}
    p = seg[1]
    p += "=" * (-len(p) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:  # noqa: BLE001
        return {}


def utc(sec):
    return datetime.datetime.fromtimestamp(int(sec), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")


def scan(directory):
    d = Path(directory)
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.info")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out.append({"file": f.name, "dir": str(d), "broken": "文件无法解析"})
            continue
        auth = data.get("auth") or {}
        acct = data.get("account") or {}
        at = str(auth.get("accessToken") or "")
        at_pl = jwt_payload(at)
        rt_pl = jwt_payload(auth.get("refreshToken") or "")
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        exp_ms = auth.get("expiresAt")
        rexp_ms = auth.get("refreshExpiresAt")
        out.append({
            "dir": str(d),
            "file": f.name,
            "nickname": acct.get("nickname") or f.stem,
            "source": at_pl.get("token_source") or rt_pl.get("token_source") or "?",
            "at_len": len(at),
            "remain": (exp_ms / 1000 - now) / 86400 if exp_ms else None,
            "rremain": (rexp_ms / 1000 - now) / 86400 if rexp_ms else None,
            "exp_at": utc(exp_ms / 1000) if exp_ms else None,
            "broken": "accessToken 不完整（疑似粘贴截断）" if len(at) < MIN_TOKEN_LEN else "",
        })
    return out


def main():
    args = sys.argv[1:]
    dirs = args or [str(Path(__file__).resolve().parent / "wb_auth"),
                    str(Path(__file__).resolve().parent.parent / "自动签到" / "wb_auth")]
    rows = []
    for d in dirs:
        rows.extend(scan(d))
    if not rows:
        print("未找到任何 .info 登录态文件：%s" % ", ".join(dirs))
        return 1

    print("%-34s %-12s %-18s %6s %6s  %s" % ("文件", "昵称", "token_source", "剩(天)", "续期(天)", "状态"))
    print("-" * 110)
    last_dir = None
    for r in rows:
        if r["dir"] != last_dir:
            print("[%s]" % r["dir"])
            last_dir = r["dir"]
        if r.get("broken") and not r.get("source"):
            print("  %-32s %-12s %-18s %6s %6s  %s" % (r["file"], "-", "-", "-", "-", r["broken"]))
            continue
        if r.get("broken"):
            flag = r["broken"]
        elif r["source"] == "oneid_login":
            flag = SOURCE_HINT["oneid_login"]
        elif r["source"] in SOURCE_HINT:
            flag = "短期通道：%s" % SOURCE_HINT[r["source"]]
        elif (r["remain"] or 0) >= LONG_REMAIN_DAYS:
            flag = "长期（旧版令牌，无 token_source 字段，按剩余天数判定）"
        else:
            flag = "短期（通道未知，剩余不足 %d 天）" % LONG_REMAIN_DAYS
        print("  %-32s %-12s %-18s %6.1f %6.1f  %s"
              % (r["file"], r["nickname"][:12], r["source"],
                 r["remain"] if r["remain"] is not None else -1,
                 r["rremain"] if r["rremain"] is not None else -1,
                 flag))
    print("\n说明：续期只沿用同一通道，短期续多少次还是短期。现在的登录流程固定会走一次"
          "\n      「切换/绑定」，新登账号一律是 enterprise_switch（30 天），退出重登也改不了"
          "\n      （已实测）。所以关键是让自动续期跑起来：python wb_ui_server.py --refresh-all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
