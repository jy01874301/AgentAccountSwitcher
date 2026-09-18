#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smoke_test.py —— 本地切换器的回归自检（无需第三方依赖）。

覆盖：备份裁剪 / 文件锁 / 端口避让 / 两个切换器的接口行为 / 令牌完整性校验 / 增删闭环。
不改动真实登录态：切号这类会写客户端配置的操作一律用临时目录或不存在的账号名来触发。

用法：
    python smoke_test.py
退出码 0 表示全部通过，1 表示有失败项。
"""
import http.client
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
sys.path.insert(0, str(BIN.parent / "自动签到"))
import switcher_common as common  # noqa: E402
import wb_ui_server as wb  # noqa: E402
import tw_ui_server as tw  # noqa: E402

FAIL = []
LOCK_DIR = BIN / ".locks"


def check(name, cond, extra=""):
    text = str(extra)
    print(("PASS  " if cond else "FAIL  ") + name + (("  -> " + text[:110]) if text else ""))
    if not cond:
        FAIL.append(name)


def call(port, path, method="GET", body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": "127.0.0.1:%d" % port}
    h.update(headers or {})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data:
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    c.close()
    return r.status, raw


def main():
    print("== 1. 备份裁剪 ==")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for i in range(15):
            f = d / ("storage.%02d.json" % i)
            f.write_text("x")
            os.utime(f, (time.time() + i, time.time() + i))
        removed = common.prune_backups(d, "storage.*.json", keep=5)
        check("15 份保留 5 份 → 删 10 份", removed == 10 and len(list(d.glob("*.json"))) == 5, removed)
        kept = sorted(x.name for x in d.glob("*.json"))
        check("保留的是最新的 5 份",
              kept == ["storage.10.json", "storage.11.json", "storage.12.json",
                       "storage.13.json", "storage.14.json"], kept)

    print("\n== 2. 文件锁 ==")
    order = []
    holding = threading.Event()

    def holder():
        with common.file_lock("smoke-test", LOCK_DIR, timeout=5):
            holding.set()
            time.sleep(1.0)

    def worker(tag, hold):
        with common.file_lock("smoke-test", LOCK_DIR, timeout=5):
            order.append("in-" + tag)
            time.sleep(hold)
            order.append("out-" + tag)

    t0 = threading.Thread(target=holder)
    t0.start()
    holding.wait(3)
    got_timeout = False
    try:
        with common.file_lock("smoke-test", LOCK_DIR, timeout=0.5):
            got_timeout = False
    except TimeoutError:
        got_timeout = True
    check("持锁期间再取锁会超时", got_timeout)
    t0.join()

    t1 = threading.Thread(target=worker, args=("A", 0.3))
    t2 = threading.Thread(target=worker, args=("B", 0.0))
    t1.start()
    time.sleep(0.05)
    t2.start()
    t1.join()
    t2.join()
    check("并发串行执行（无交叉）", order == ["in-A", "out-A", "in-B", "out-B"], order)

    # 锁文件此前每次加锁都 append 一次 pid，会随调用次数单调增长
    with tempfile.TemporaryDirectory() as td:
        for _ in range(5):
            with common.file_lock("grow", td, timeout=2):
                pass
        size = (Path(td) / "grow.lock").stat().st_size
        check("锁文件不随加锁次数增长（≤2 字节）", size <= 2, size)

    print("\n== 3. 令牌完整性校验 ==")
    check("完整 JWT 通过", common.token_looks_complete("eyJhbGciOi." + "a" * 300 + "." + "b" * 300))
    check("截断值被拒", not common.token_looks_complete("eyJx"))
    check("空值被拒", not common.token_looks_complete(""))

    # 令牌注入此前只认精确的 b"<head>"，模板加个属性就会静默失配
    head = b'<html><head lang="zh"><title>x</title></head><body>hi</body></html>'
    injected = common.BaseHandler._inject_head(head, b"<script>T</script>")
    at = injected.find(b"<script>T</script>")
    check("带属性的 <head> 也能注入令牌",
          at != -1 and at < injected.find(b"</head>"), injected[:70])
    check("注入后原内容不丢失", b"hi</body>" in injected)

    print("\n== 4. 端口避让 ==")
    srv1, p1 = common.bind_server(wb.Handler, 8901)
    threading.Thread(target=srv1.serve_forever, daemon=True).start()
    time.sleep(0.3)
    check("首次绑定 8901", p1 == 8901, p1)
    srv2, p2 = common.bind_server(wb.Handler, 8901)
    check("占用后顺延到 8902", p2 == 8902, p2)
    srv1.shutdown()
    srv1.server_close()
    srv2.server_close()

    print("\n== 5. 切换器接口 ==")
    servers = []
    bad_file = wb.AUTH_DIR / "workbuddy-__bad.info"
    try:
        s_wb, port_wb = common.bind_server(wb.Handler, 8911)
        s_tw, port_tw = common.bind_server(tw.Handler, 8912)
        for s in (s_wb, s_tw):
            threading.Thread(target=s.serve_forever, daemon=True).start()
            servers.append(s)
        time.sleep(0.3)

        for label, port in (("[wb]", port_wb), ("[tw]", port_tw)):
            st, raw = call(port, "/")
            html = raw.decode("utf-8", "replace")
            check(label + " GET / 返回前端", st == 200 and "切换器" in html, st)
            check(label + " 模板已渲染（无残留占位符）", "{{" not in html, html[:60])
            # 三个工具按钮都默认隐藏，由 loadCredits / loadCheckin 按后端能力点亮
            check(label + " 工具条含三个按钮",
                  'id="credBtn"' in html and 'id="ckBtn"' in html and 'id="rfBtn"' in html, st)
            check(label + " 一键续期按钮已接上",
                  "doRefreshAll()" in html and "/api/refresh-all" in html, st)
            if label == "[wb]":
                check("[wb] 前端含积分明细模板", "积分明细" in html and "credBtn" in html, st)
                # 时间与已使用量同行、两端对齐；两块 nowrap 保证不会"一侧折行一侧单行"
                check("[wb] 积分明细同行布局", "ci-info" in html and "ci-when" in html, st)
                check("[wb] 签到功能开启", 'checkin: "1"' in html and "doCheckinAll()" in html, st)
                check("[wb] 可用账号用列表行渲染（不再是 grid 卡片）",
                      'id="acctList"' in html and 'class="list"' in html and 'id="acctGrid"' not in html, st)
                check("[wb] 当前账号与列表行共用同一套行模板",
                      "function rowHtml" in html and 'class="avatar"' in html, st)
                check("[wb] 列表里过滤掉当前账号",
                      "a.file!==curFile" in html, st)
                # 只读签到状态必须与写接口分开：WRITE_ENDPOINTS 里的路径 GET 一律 405
                st2, raw2 = call(port, "/api/checkin-status")
                check("[wb] GET /api/checkin-status 可用（只读）",
                      st2 == 200 and json.loads(raw2).get("ok"), st2)
                st2, _ = call(port, "/api/checkin", "GET")
                check("[wb] GET /api/checkin 405（写接口只收 POST）", st2 == 405, st2)
                st2, _ = call(port, "/api/refresh-all", "GET")
                check("[wb] GET /api/refresh-all 405", st2 == 405, st2)
                check("[wb] 两个写接口已登记进 WRITE_ENDPOINTS",
                      "/api/checkin" in wb.Handler.WRITE_ENDPOINTS
                      and "/api/refresh-all" in wb.Handler.WRITE_ENDPOINTS, wb.Handler.WRITE_ENDPOINTS)
            else:
                check("[tw] 无积分接口时不显示刷新按钮",
                      'id="credBtn" style="display:none"' in html, st)
                check("[tw] 无签到接口时不显示签到按钮",
                      'id="ckBtn" style="display:none"' in html and 'checkin: ""' in html, st)
                check("[tw] 一键续期接口可用",
                      "/api/refresh-all" in tw.Handler.WRITE_ENDPOINTS, tw.Handler.WRITE_ENDPOINTS)
            st, raw = call(port, "/api/accounts")
            check(label + " GET /api/accounts", st == 200 and json.loads(raw).get("ok"), st)
            accs = json.loads(raw).get("accounts") or []
            check(label + " 账号带 token_source 字段",
                  all("token_source" in a for a in accs), accs[:1])
            # 两个切换器共用同一份前端模板，模板靠 uid 把「当前账号」映射回账号库里的
            # 文件（既用于显示积分/签到，也用于把当前账号从列表里滤掉）。
            # tw 侧曾经不返回 uid，导致 Trae 页面里当前账号永远显示"不在账号库中"、
            # 列表还和当前账号重复一条 —— 这条断言就是防它再次漂移。
            check(label + " 账号条目都带 uid 字段",
                  all("uid" in a for a in accs), accs[:1])
            usable_accs = [a for a in accs if a.get("ok")]
            check(label + " 可用账号解析出非空 uid",
                  all(a.get("uid") for a in usable_accs),
                  [(a["file"], a.get("uid")) for a in usable_accs])
            st, raw = call(port, "/api/nope")
            check(label + " 未知路径 JSON 404", st == 404 and json.loads(raw).get("ok") is False, st)
            st, raw = call(port, "/api/switch", "POST", {"name": "nope"})
            check(label + " POST /api/switch", st == 200 and json.loads(raw).get("ok") is False, st)
            st, _ = call(port, "/api/switch", "POST", {"name": "nope"}, {"Origin": "http://evil.com"})
            check(label + " 跨站 403", st == 403, st)
            st, _ = call(port, "/api/accounts", "GET", None, {"Origin": "http://127.0.0.1:%d" % port})
            check(label + " 同源放行", st == 200, st)

        st, raw = call(port_tw, "/api/current")
        cur = json.loads(raw).get("current") or []
        check("[tw] current 返回 dict（不是裸字符串）",
              bool(cur) and isinstance(cur[0], dict) and "ok" in cur[0], cur[:1])

        # 客户端中途断开（浏览器刷新页面会取消未完成的请求）不应把服务打挂
        check("端口服务用 QuietHTTPServer 承载",
              isinstance(s_wb, common.QuietHTTPServer), type(s_wb).__name__)
        import socket as _socket
        sk = _socket.create_connection(("127.0.0.1", port_wb), timeout=5)
        sk.sendall(b"GET /api/accounts HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        sk.close()
        time.sleep(0.2)
        st, _raw = call(port_wb, "/api/accounts")
        check("客户端断开后服务仍可用", st == 200, st)

        # 取一份可用账号内容当素材。不写死文件名：账号库会增删改名，
        # 写死某个具体账号会让自检在文件变动当天直接崩掉（而非报失败项）。
        usable = [a for a in wb.list_accounts() if a.get("ok")]
        if not usable:
            check("[wb] 无可用账号素材，跳过残缺/增删用例", True,
                  "wb_auth 下没有可解析的 .info")
        else:
            good = Path(usable[0]["path"]).read_text(encoding="utf-8")

            # 先清掉上一轮可能残留的测试文件：本机删除走安全删除 shim，被中断的运行
            # 可能 fail-closed 把文件留在原地，下一次跑就会让"未落盘"断言假失败。
            bad_file.unlink(missing_ok=True)
            (wb.AUTH_DIR / "workbuddy-__smoke.info").unlink(missing_ok=True)

            # 残缺登录态：add 必须拒绝，且不能留下文件
            broken = json.loads(good)
            broken["auth"]["accessToken"] = "eyJx"
            st, raw = call(port_wb, "/api/add", "POST",
                           {"name": "__bad", "content": json.dumps(broken)})
            check("[wb] 残缺 accessToken 被拒", json.loads(raw).get("ok") is False, raw[:90])
            check("[wb] 残缺账号未落盘", not bad_file.exists())

            # 手动放入残缺文件后，列表必须标为不可用
            bad_file.write_text(json.dumps(broken, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                st, raw = call(port_wb, "/api/accounts")
                entry = next((a for a in json.loads(raw)["accounts"] if a["file"] == bad_file.name), None)
                check("[wb] 列表标记残缺账号不可用",
                      entry is not None and entry["ok"] is False and "不完整" in (entry.get("reason") or ""),
                      entry)
                st, raw = call(port_wb, "/api/switch", "POST", {"name": bad_file.name})
                check("[wb] 拒绝切换到残缺账号", json.loads(raw).get("ok") is False, raw[:90])

                # 路径穿越：../ 形式的 name 必须被拒（wb 侧曾直接拼 AUTH_DIR / name）
                st, raw = call(port_wb, "/api/switch", "POST",
                               {"name": "../../自动签到/wb_auth/nope.info"})
                check("[wb] 拒绝路径穿越的 name", json.loads(raw).get("ok") is False, raw[:90])
            finally:
                bad_file.unlink(missing_ok=True)

            # 正常增删闭环
            st, raw = call(port_wb, "/api/add", "POST", {"name": "__smoke", "content": good})
            tmp_file = wb.AUTH_DIR / "workbuddy-__smoke.info"
            check("[wb] add 正常内容", json.loads(raw).get("ok") and tmp_file.is_file(), raw[:90])
            st, raw = call(port_wb, "/api/remove", "POST", {"file": tmp_file.name})
            check("[wb] remove", json.loads(raw).get("ok") and not tmp_file.exists(), raw[:90])
    finally:
        bad_file.unlink(missing_ok=True)
        for s in servers:
            s.shutdown()
            s.server_close()
        wb.Handler.TOKEN = None
        tw.Handler.TOKEN = None

    print("\n== 5b. 切号落盘与失败回滚（DESKTOP_* 重定向到临时目录）==")
    # 真实桌面端登录态绝不能碰，所以把 DESKTOP_DIR / DESKTOP_INFO 指到临时目录，
    # 这样能完整走一遍「轮换备份 → 写新文件 → 写后自校验」的成功与失败路径。
    usable = [a for a in wb.list_accounts() if a.get("ok")]
    if not usable:
        check("[wb] 无可用账号素材，跳过落盘/回滚用例", True, "wb_auth 下没有可解析的 .info")
    else:
        target_name = Path(usable[0]["path"]).name
        target_uid = usable[0]["uid"]
        original = '{"account": {"uid": "orig-uid", "nickname": "原账号"}, "auth": {}}'
        orig_dir, orig_info = wb.DESKTOP_DIR, wb.DESKTOP_INFO
        try:
            # --- 成功路径：正式文件应变成目标账号，原内容进备份 ---
            with tempfile.TemporaryDirectory() as td:
                desk = Path(td)
                info = desk / "workbuddy-desktop.info"
                info.write_text(original, encoding="utf-8")
                wb.DESKTOP_DIR, wb.DESKTOP_INFO = desk, info
                ok, msg, _mig = wb.switch_account(target_name)
                acc_now = wb.wb.session_from_info_file(info) if info.is_file() else None
                backups = [p for p in desk.glob("*.info") if p.name != "workbuddy-desktop.info"]
                check("切号成功（临时目录）", ok is True, msg[:80])
                check("正式文件已变成目标账号",
                      bool(acc_now) and acc_now["uid"] == target_uid, acc_now and acc_now["uid"])
                check("原登录态已轮换为备份",
                      len(backups) == 1 and backups[0].read_text(encoding="utf-8") == original,
                      [p.name for p in backups])

            # --- 失败路径：用同名目录占住临时文件路径，强制写盘抛 OSError ---
            with tempfile.TemporaryDirectory() as td:
                desk = Path(td)
                info = desk / "workbuddy-desktop.info"
                info.write_text(original, encoding="utf-8")
                info.with_suffix(".info.tmp").mkdir()      # 让 tmp.write_text 必失败
                wb.DESKTOP_DIR, wb.DESKTOP_INFO = desk, info
                ok, msg, _mig = wb.switch_account(target_name)
                check("写入失败时返回失败", ok is False, msg[:80])
                check("失败信息含回滚说明", "已回滚为原登录态" in msg, msg[:110])
                check("正式文件已恢复原内容",
                      info.is_file() and info.read_text(encoding="utf-8") == original,
                      info.read_text(encoding="utf-8")[:60] if info.is_file() else "文件不存在")
                check("回滚后无残留备份",
                      [p.name for p in desk.glob("workbuddy-desktop.*.info")] == [],
                      [p.name for p in desk.glob("*.info")])
        finally:
            wb.DESKTOP_DIR, wb.DESKTOP_INFO = orig_dir, orig_info

    print("\n== 6. 一次性令牌 + 审计日志 ==")
    with tempfile.TemporaryDirectory() as td:
        wb.Handler.TOKEN = "test-token-xyz"
        wb.Handler.AUDIT_DIR = Path(td)
        srv, port = common.bind_server(wb.Handler, 8921)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            time.sleep(0.3)
            st, raw = call(port, "/")
            html = raw.decode("utf-8", "replace")
            check("首页注入令牌", 'window.SWITCHER_TOKEN="test-token-xyz"' in html)
            st, raw = call(port, "/api/switch", "POST", {"name": "nope"})
            check("无令牌写请求 401", st == 401, st)
            st, raw = call(port, "/api/switch", "POST", {"name": "nope"},
                           {"X-Switcher-Token": "wrong"})
            check("错令牌写请求 401", st == 401, st)
            st, raw = call(port, "/api/switch", "POST", {"name": "nope"},
                           {"X-Switcher-Token": "test-token-xyz"})
            check("正确令牌放行", st == 200 and json.loads(raw).get("ok") is False, st)
            st, raw = call(port, "/api/accounts")
            check("读接口不受令牌影响", st == 200, st)

            log = Path(td) / common.AUDIT_NAME
            text = log.read_text(encoding="utf-8") if log.is_file() else ""
            check("审计记录了切号", "switch" in text and "nope" in text, text[-90:])
            check("审计记录了令牌失败", "令牌校验失败" in text)
            check("审计不含凭据", "eyJ" not in text)
        finally:
            srv.shutdown()
            srv.server_close()
            wb.Handler.TOKEN = None
            wb.Handler.AUDIT_DIR = BIN / "logs"

    print("\n== 6b. 审计日志轮转 ==")
    # 日志只追加不封顶会一直吃盘；把上限压到很小，几条就该滚出 .1
    with tempfile.TemporaryDirectory() as td:
        orig_max = common.AUDIT_MAX_BYTES
        common.AUDIT_MAX_BYTES = 200
        try:
            for i in range(10):
                common.audit(Path(td), "wb", "switch", "acct-%d" % i, True, "x" * 60)
        finally:
            common.AUDIT_MAX_BYTES = orig_max
        cur = Path(td) / common.AUDIT_NAME
        rolled = Path(td) / (common.AUDIT_NAME + ".1")
        check("超过上限后滚出 .1", rolled.is_file(), [p.name for p in Path(td).iterdir()])
        check("当前日志被压回上限附近", cur.stat().st_size < 500, cur.stat().st_size)
        check("轮转后最新记录仍在当前日志", "acct-9" in cur.read_text(encoding="utf-8"), "")

    print("\n== 7. 错误脱敏与依赖契约 ==")
    check("scrub 抹掉用户目录",
          "Administrator" not in common.scrub("C:/Users/Administrator/secret/x")
          and "~" in common.scrub("C:/Users/Administrator/secret/x"),
          common.scrub("C:/Users/Administrator/secret/x"))
    check("scrub 限长", len(common.scrub("x" * 500)) <= 205, len(common.scrub("x" * 500)))

    import types
    fake = types.ModuleType("fake_dep_module")
    sys.modules["fake_dep_module"] = fake
    raised = ""
    try:
        common.require_module("fake_dep_module", ("does_not_exist",), who="smoke")
    except SystemExit as e:
        raised = str(e)
    check("依赖缺成员 → 可读 SystemExit", "缺少本工具需要的成员" in raised, raised[:70])
    raised = ""
    try:
        common.require_module("no_such_module_xyz")
    except SystemExit as e:
        raised = str(e)
    check("依赖缺失 → 可读 SystemExit", "找不到依赖模块" in raised and "自动签到" in raised, raised[:70])

    check("_trae_running 返回列表且不抛异常", isinstance(tw._trae_running(), list))

    # 触发一次 500，确认响应里不含本机路径
    srv, port = common.bind_server(wb.Handler, 8931)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    orig_list = wb.list_accounts
    try:
        time.sleep(0.3)

        def boom():
            raise ValueError("泄漏路径 C:/Users/Administrator/secret/token.info")

        wb.list_accounts = boom
        st, raw = call(port, "/api/accounts")
        payload = json.loads(raw)
        check("500 响应脱敏", st == 500 and "Administrator" not in payload.get("message", "")
              and "ValueError" in payload.get("message", ""), payload.get("message"))
    finally:
        wb.list_accounts = orig_list
        srv.shutdown()
        srv.server_close()

    print("\n== 8. 积分明细（造资源包响应，不联网）==")
    # 积分来自计费网关的资源包接口，这里替换掉真实请求，只验证归类与汇总
    orig_billing = wb._billing_post

    def _pkg(name, sub, size, used, remain, cycle_end, status=0):
        return {"PackageCode": "X", "PackageName": name, "SubProductName": sub,
                "Status": status, "CycleEndTime": cycle_end,
                "CycleCapacitySizePrecise": str(size),
                "CycleCapacityUsedPrecise": str(used),
                "CycleCapacityRemainPrecise": str(remain)}

    def _resp(pkgs):
        return (200, {"code": 0, "data": {"Response": {"Data": {"Accounts": pkgs}}}})

    try:
        wb._billing_post = lambda url, headers, timeout=20, retries=2: _resp([
            # 套餐基础积分：按月滚动
            _pkg("CodeBuddy个人体验版", "腾讯云代码助手 (IDE)", 500, 500, 0, "2026-09-30 23:59:59"),
            # 平台奖励积分（赠送包）：两个有效包
            _pkg("CodeBuddy个人版国内运营裂变包", "腾讯云代码助手 (IDE) - 赠送包",
                 1500, 258.46, 1241.54, "2026-10-02 21:31:06"),
            _pkg("CodeBuddy个人版国内运营裂变包", "腾讯云代码助手 (IDE) - 赠送包",
                 100, 0, 100, "2026-10-05 00:01:39"),
            # 已失效包（Status=3）：到期更早，但不得成为"最近到期时间"
            _pkg("CodeBuddy个人版国内运营裂变包", "腾讯云代码助手 (IDE) - 赠送包",
                 100, 100, 0, "2026-09-01 00:01:03", status=3),
        ])
        snap = wb.credits_snapshot(force=True)
    finally:
        wb._billing_post = orig_billing

    acc = snap["accounts"][0] if snap["accounts"] else {}
    items = acc.get("items") or []
    check("积分：可用账号查到明细", snap["queried"] > 0 and len(items) == 2, items)
    if len(items) == 2:
        plan, bonus = items[0], items[1]
        check("积分：分为套餐基础 / 平台奖励两档",
              plan["label"] == "套餐基础积分" and bonus["label"] == "平台奖励积分",
              [plan["label"], bonus["label"]])
        # 套餐按月滚动，界面上写"下次刷新时间" = 本周期结束 + 1 秒
        check("积分：套餐显示下次刷新时间（周期结束+1s）",
              plan["time"] == "2026/10/01 00:00:00", plan["time"])
        check("积分：套餐已用 100% / 剩余 0%",
              plan["used_percent"] == 100 and plan["remain_percent"] == 0,
              (plan["used_percent"], plan["remain_percent"]))
        check("积分：奖励取最近到期时间（已失效包被排除）",
              bonus["time"] == "2026/10/02 21:31:06", bonus["time"])
        check("积分：奖励只统计有效包", bonus["packages"] == 2, bonus["packages"])
        check("积分：奖励合计 1500+100、已用 258.46",
              bonus["total"] == 1600 and bonus["used"] == 258.46 and bonus["remain"] == 1341.54,
              (bonus["total"], bonus["used"], bonus["remain"]))
        check("积分：总剩余 = 套餐 + 奖励", acc.get("total_remain") == 1341.54,
              acc.get("total_remain"))

    # 网关报错不能污染整个列表
    try:
        wb._billing_post = lambda url, headers, timeout=20, retries=2: (500, {"_error": "boom"})
        bad = wb.credits_snapshot(force=True)
    finally:
        wb._billing_post = orig_billing
    check("积分：单账号失败不影响整体结构",
          bad["ok"] and bad["queried"] == 0 and all("reason" in a for a in bad["accounts"]),
          bad["queried"])

    print("\n== 9. 打包配置（spec）==")
    # 打包出的 exe 不在自检范围内，但 spec 的静态缺陷可以在这里拦住：
    # hiddenimports 漏项属于「构建成功但双击一闪而过」，跑一次构建才发现，代价高。
    import ast
    for spec_name, dep in (("WorkBuddySwitcher.spec", "workbuddy_checkin"),
                           ("TraeSwitcher.spec", "trae_work_checkin")):
        spec = BIN / spec_name
        text = spec.read_text(encoding="utf-8") if spec.is_file() else ""
        check("%s 存在" % spec_name, bool(text), spec)
        # switcher_common 与 checkin 模块都是运行时动态导入，静态分析扫不到
        check("%s 的 hiddenimports 含 switcher_common 与 %s" % (spec_name, dep),
              "'switcher_common'" in text and ("'%s'" % dep) in text, text[:0])
        check("%s 用 SPEC 变量做相对定位" % spec_name, "SPEC" in text, text[:0])
        # 「是否写死绝对路径」只看代码：注释和模块 docstring 里提到旧路径是允许的
        # （否则说明性注释反而会把断言逼成假阳性）。
        try:
            code = text.replace(ast.get_docstring(ast.parse(text)) or "", "")
        except SyntaxError:
            code = text
        code = "\n".join(ln.split("#", 1)[0] for ln in code.splitlines())
        check("%s 代码中未写死 D:/AI项目 绝对路径" % spec_name,
              "D:/AI项目" not in code and "D:\\AI项目" not in code, text[:0])

    print("\n== 10. Trae 登录态路径的 APPDATA 回退 ==")
    # 此前 tw 侧只写 os.environ.get("APPDATA")，变量为空就静默返回空列表，
    # 表现为「未发现 storage.json」（哪怕文件就在那儿），切号也会被拒。
    real_appdata = Path.home() / "AppData" / "Roaming"
    expect_found = any((real_appdata / d / "User" / "globalStorage" / "storage.json").is_file()
                       for d in tw.tw.CANDIDATE_DIRS)
    saved_appdata = os.environ.pop("APPDATA", None)
    try:
        got_dir = tw._appdata_dir()
        got_paths = tw.desktop_storage_paths()
    finally:
        if saved_appdata is not None:
            os.environ["APPDATA"] = saved_appdata
    check("APPDATA 缺失时回退到 ~/AppData/Roaming",
          bool(got_dir) and "AppData" in got_dir and "Roaming" in got_dir, got_dir)
    check("APPDATA 缺失时仍能找到本机 storage.json",
          (len(got_paths) >= 1) == expect_found,
          "%d 条 / 预期%s" % (len(got_paths), expect_found))

    # wb 侧的 %LOCALAPPDATA% 同款坑：变量"存在但为空串"时 os.environ.get(k, d) 会返回 ""，
    # Path("") 解析成当前目录，于是静默去 ./CodeBuddyExtension/... 找登录态，
    # 表现为「当前账号为空」且不报错。必须用 `or` 回退。
    saved_local = os.environ.pop("LOCALAPPDATA", None)
    try:
        os.environ["LOCALAPPDATA"] = ""            # 关键：置空而不是删除
        got_local = wb._localappdata_dir()
        # 复现模块级表达式：DESKTOP_DIR 在 import 时就算好了，直接读它测不出环境变量变化
        got_desktop = Path(wb._localappdata_dir()) / wb.wb.AUTH_REL_DIR
    finally:
        os.environ.pop("LOCALAPPDATA", None)
        if saved_local is not None:
            os.environ["LOCALAPPDATA"] = saved_local
    check("LOCALAPPDATA 为空串时回退到 ~/AppData/Local",
          bool(got_local) and "AppData" in got_local and "Local" in got_local, repr(got_local))
    check("LOCALAPPDATA 为空串时 DESKTOP_DIR 仍是绝对路径（不会落到当前目录）",
          got_desktop.is_absolute() and "CodeBuddyExtension" in str(got_desktop), str(got_desktop))

    print("\n== 11. 续期门卫（计划任务默认不强制刷新）==")
    # 计划任务每天跑一次 --refresh-all，但默认 force=False，交给上级
    # workbuddy_checkin.refresh_account 按「剩余 < 3 天 + 24 小时冷却」判断，
    # 免得每天无谓地重写 5 个 .info。UI 手动点「续期」才 force=True。
    import inspect
    sig_file = inspect.signature(wb.refresh_account_file).parameters
    sig_all = inspect.signature(wb.refresh_all).parameters
    check("refresh_account_file 默认 force=True（UI 手动续期）",
          sig_file["force"].default is True, sig_file["force"].default)
    check("refresh_all 默认 force=False（计划任务走门卫）",
          sig_all["force"].default is False, sig_all["force"].default)

    captured = []
    real_refresh = wb.wb.refresh_account

    def fake_refresh(acc, script_dir, cfg, log, force=False, threshold=None):
        captured.append(force)
        return True, "fake-skipped", "skipped"

    wb.wb.refresh_account = fake_refresh
    try:
        usable = [a for a in wb.list_accounts() if a.get("ok")]
        if usable:
            wb.refresh_account_file(usable[0]["file"], force=False)
            check("force=False 透传到上级门卫", captured[-1] is False, captured)
            wb.refresh_account_file(usable[0]["file"], force=True)
            check("force=True 透传到上级门卫", captured[-1] is True, captured)
            check("门卫返回 skipped 时不写盘（消息原样回传）",
                  wb.refresh_account_file(usable[0]["file"], force=False)[1] == "fake-skipped")
        else:
            check("[wb] 无可用账号素材，跳过门卫透传用例", True, "wb_auth 下没有可解析的 .info")
    finally:
        wb.wb.refresh_account = real_refresh

    print("\n== 12. 前端模板：占位符与 JS 语法 ==")
    # 模板是两个切换器共用的，任何一边漏配 UI_CONTEXT 都会在页面上留下裸 {{KEY}}；
    # 而脚本里一个语法错误会让整页白屏，且只有打开浏览器才看得出来 —— 这里一并守住。
    tpl = (BIN / "ui_template.html").read_text(encoding="utf-8")
    for label, ctx in (("[wb]", wb.Handler.UI_CONTEXT), ("[tw]", tw.Handler.UI_CONTEXT)):
        rendered = common.render_template(tpl, ctx).decode("utf-8")
        left = sorted(set(re.findall(r"\{\{[A-Za-z0-9_]+\}\}", rendered)))
        check("%s 模板占位符全部有值" % label, not left, left)
    scripts = re.findall(r"<script>(.*?)</script>", rendered, re.S)
    main_js = max(scripts, key=len) if scripts else ""
    check("模板含主脚本（rowHtml / renderAccounts）",
          "function rowHtml" in main_js and "function renderAccounts" in main_js, len(main_js))
    node = shutil.which("node")
    if node:
        with tempfile.TemporaryDirectory() as td:
            js = Path(td) / "tpl.js"
            js.write_text(main_js, encoding="utf-8")
            r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
        check("前端 JS 通过 node --check", r.returncode == 0,
              (r.stderr or r.stdout or "").strip()[-200:])
    else:
        check("跳过前端 JS 语法检查（node 不在 PATH）", True, "")

    print("\n== 13. 删除被拒时不得拖垮请求（安全删除 shim 会抛 SystemExit）==")
    # 这台机器的 Python 被注入了 WorkBuddy CLI 的安全删除 shim：删除先过一遍
    # 批量删除守卫（safe-delete-bulk-guard.cjs），守卫判定 confirmRequired/rejected
    # 时 process.exit(2/3)，Python 侧随即 raise SystemExit(1)。
    # SystemExit 是 BaseException 而非 Exception —— 它曾经穿透 prune_backups 的
    # `except OSError`、再穿透 do_POST 的 `except Exception`，让请求**连响应都不发
    # 就断连**（浏览器只看到 Failed to fetch，审计日志一条没有）。切号偶发"提示失败"
    # 就是这个。下面三条断言分别守住三个防线。
    real_unlink = Path.unlink

    def _boom_unlink(self, *a, **k):
        raise SystemExit(1)

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for i in range(6):
            f = d / ("bk.%02d.info" % i)
            f.write_text("x", encoding="utf-8")
            os.utime(f, (time.time() + i, time.time() + i))
        Path.unlink = _boom_unlink
        try:
            n = common.prune_backups(d, "bk.*.info", keep=2)
            ok_safe = common.safe_unlink(d / "bk.00.info")
        finally:
            Path.unlink = real_unlink
        check("prune_backups 吞掉 SystemExit 并正常返回", n == 0, n)
        check("删除被拒时备份保持原样（没被删掉）",
              len(list(d.glob("bk.*.info"))) == 6, len(list(d.glob("bk.*.info"))))
        check("safe_unlink 删除被拒时返回 False 而不是抛出", ok_safe is False, ok_safe)

    # do_POST：api_post 里冒出 SystemExit / TimeoutError 时必须回一个能看见的 JSON
    with tempfile.TemporaryDirectory() as td:
        class _Boom(wb.Handler):
            AUDIT_DIR = Path(td)

            def api_post(self, u, p):
                raise SystemExit(1)

        class _Slow(wb.Handler):
            AUDIT_DIR = Path(td)

            def api_post(self, u, p):
                raise TimeoutError("等待文件锁超时")

        _Boom.TOKEN = None
        _Slow.TOKEN = None
        for label, cls, want in (("SystemExit", _Boom, 500), ("TimeoutError", _Slow, 504)):
            s, port = common.bind_server(cls, 8941)
            threading.Thread(target=s.serve_forever, daemon=True).start()
            try:
                time.sleep(0.2)
                st, raw = call(port, "/api/switch", "POST", {"name": "x"})
                body = {}
                try:
                    body = json.loads(raw)
                except ValueError:
                    pass
                check("api_post 抛 %s 时仍回 JSON（HTTP %d，不再断连）" % (label, want),
                      st == want and body.get("ok") is False, (st, raw[:90]))
                check("api_post 抛 %s 时错误类型写进响应" % label,
                      body.get("error") == label, body.get("error"))
            finally:
                s.shutdown()
                s.server_close()
        log = (Path(td) / "switcher.log")
        txt = log.read_text(encoding="utf-8") if log.is_file() else ""
        check("失败也写进了审计日志（可追溯）", "switch" in txt and "FAIL" in txt, txt[-90:])

    # switch_account：裁剪备份抛异常也不能让切号失败，更不能留下"没有正式登录态"
    usable2 = [a for a in wb.list_accounts() if a.get("ok")]
    if not usable2:
        check("[wb] 无可用账号素材，跳过裁剪异常用例", True, "")
    else:
        orig_dir, orig_info = wb.DESKTOP_DIR, wb.DESKTOP_INFO
        real_prune = common.prune_backups

        def _boom_prune(*a, **k):
            raise SystemExit(1)

        try:
            with tempfile.TemporaryDirectory() as td:
                desk = Path(td)
                info = desk / "workbuddy-desktop.info"
                info.write_text('{"account": {"uid": "orig", "nickname": "原账号"}, "auth": {}}',
                                encoding="utf-8")
                wb.DESKTOP_DIR, wb.DESKTOP_INFO = desk, info
                common.prune_backups = _boom_prune
                try:
                    ok, msg, _mig = wb.switch_account(Path(usable2[0]["path"]).name)
                finally:
                    common.prune_backups = real_prune
                check("裁剪备份抛 SystemExit 时切号仍然成功", ok is True, msg[:80])
                check("正式登录态文件存在（不会因裁剪失败而丢失）", info.is_file(), str(info))
        finally:
            wb.DESKTOP_DIR, wb.DESKTOP_INFO = orig_dir, orig_info

    # current_account：必须只认本工具接管的那一份
    orig_dir, orig_info = wb.DESKTOP_DIR, wb.DESKTOP_INFO
    try:
        with tempfile.TemporaryDirectory() as td:
            desk = Path(td)
            good = Path(usable2[0]["path"]).read_text(encoding="utf-8") if usable2 else "{}"
            mine = desk / "workbuddy-desktop.info"
            mine.write_text(good, encoding="utf-8")
            ai = desk / "workbuddy-desktop-ai.info"
            ai.write_text(good, encoding="utf-8")
            os.utime(mine, (time.time() - 100, time.time() - 100))   # 让 -ai 更新
            os.utime(ai, (time.time(), time.time()))
            wb.DESKTOP_DIR, wb.DESKTOP_INFO = desk, mine
            names = [e["file"] for e in wb.current_account()]
            check("当前账号只认 workbuddy-desktop*，不认 workbuddy-desktop-ai*",
                  names == ["workbuddy-desktop.info"], names)
    finally:
        wb.DESKTOP_DIR, wb.DESKTOP_INFO = orig_dir, orig_info

    print("\n== 14. 账号数据迁移（全程在临时目录，不碰真实数据）==")
    mig = wb.migration
    # 迁移的前置检查里有"不得有另一个切换器实例在跑"（拒绝路径由 §15e 专门覆盖）。
    # 但自检是在用户的真实环境里跑的，8765 上很可能正跑着切换器 —— 那会把本段
    # 全部用例挡掉。所以这里把探测端口指到一个不监听任何服务的端口，
    # 让本段专注验证迁移本身。§15e 结束时会把 INSTANCE_PORT 还原。
    _saved_inst_port = mig.INSTANCE_PORT
    mig.INSTANCE_PORT = 8999          # 该端口上没有任何服务
    OLD = "aaaaaaaa-1111-2222-3333-444444444444"
    NEW = "bbbbbbbb-5555-6666-7777-888888888888"
    SID1 = "11111111-aaaa-bbbb-cccc-000000000001"
    SID2 = "22222222-aaaa-bbbb-cccc-000000000002"
    SIDW = "33333333-aaaa-bbbb-cccc-000000000003"

    # --- 14a. connectors 校验值算法：必须能复现真实文件里的 userIdCheck ---
    # computeCheck = base64(sha256(uid + salt)[:16])，无密钥。实测 4/4 复现一致。
    import base64 as _b64
    import hashlib as _hash
    _salt = bytes(range(16))
    _expect = _b64.b64encode(_hash.sha256(b"u1" + _salt).digest()[:16]).decode()
    check("computeCheck 与客户端算法一致（sha256(uid+salt)[:16]）",
          mig.compute_check(b"u1", _salt) == _expect, _expect)
    real_conn = mig.find_data_root(wb.current_uid()) / "connectors"
    hit = miss_ = 0
    if real_conn.is_dir():
        for d in real_conn.iterdir():
            f = d / "connector-states.json"
            if not f.is_file():
                continue
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
                enc = j.get("encryption") or {}
                salt = _b64.b64decode(enc["salt"])
                # 三段式：<uid>|<企业简称>|<类型>，取第一段才是 uid
                uid = (j.get("accountIdentityKey") or "").split("|")[0]
                n = len(_b64.b64decode(enc["userIdCheck"])) or 16
                if mig.compute_check(uid.encode(), salt, n) == enc["userIdCheck"]:
                    hit += 1
                else:
                    miss_ += 1
            except Exception:  # noqa: BLE001
                miss_ += 1
    check("真实 connectors 的 userIdCheck 全部可复现（读只）",
          miss_ == 0, "命中 %d / 不一致 %d" % (hit, miss_))

    # --- 14b. 数据目录判定：本机有两个结构相同的客户端目录，必须挑对 ---
    real_root = mig.find_data_root(wb.current_uid())
    check("find_data_root 按 uid 选中正确目录（不是靠环境变量猜）",
          real_root.is_dir() and (real_root / "workbuddy.db").is_file(), str(real_root))
    ai_root = mig.find_data_root("7aac45de-1d55-436c-b779-0317b093c580")
    check("不同 uid 解析到不同数据目录",
          str(ai_root) != str(real_root) or not (ai_root / "workbuddy.db").is_file(),
          "%s vs %s" % (real_root, ai_root))

    # --- 14c. 进程检测不能静默失效 ---
    procs = mig.running_clients()
    check("running_clients 不抛异常且返回 None 或列表",
          procs is None or isinstance(procs, list), procs)
    check("running_clients 精确匹配（本机有客户端时应检出）",
          procs is None or (len(procs) > 0) == (os.name == "nt"), procs)

    # --- 14d. 造一个临时数据目录，跑完整迁移 + 校验 + 回滚 ---
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mig.ROOT_OVERRIDE = tmp
        try:
            con = sqlite3.connect(str(tmp / "workbuddy.db"))
            con.executescript("""
                CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT NOT NULL, user_id TEXT NOT NULL,
                  title TEXT, status TEXT NOT NULL DEFAULT 'Pending', created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL, deleted_at INTEGER);
                CREATE TABLE automations (id TEXT PRIMARY KEY, name TEXT, owner_user_id TEXT,
                  owner_status TEXT NOT NULL DEFAULT 'legacy_unassigned', deleted_at INTEGER);
                CREATE TABLE automation_delivery_outbox (id TEXT PRIMARY KEY, owner_user_id TEXT);
                CREATE TABLE session_usage (session_id TEXT PRIMARY KEY, used INTEGER, size INTEGER);
            """)
            con.executemany("INSERT INTO sessions (id,cwd,user_id,title,status,created_at,updated_at) "
                            "VALUES (?,?,?,?,?,1,1)",
                            [(SID1, r"D:\proj", OLD, "A", "completed"),
                             (SID2, r"D:\proj", OLD, "B", "completed"),
                             (SIDW, r"D:\proj", OLD, "运行中", "working")])
            con.execute("INSERT INTO automations (id,name,owner_user_id) VALUES ('a1','任务',?)", (OLD,))
            con.commit()
            con.close()
            (tmp / "projects/d-proj").mkdir(parents=True)
            for s in (SID1, SID2, SIDW):
                (tmp / "projects/d-proj" / (s + ".jsonl")).write_text('{"t":1}\n', encoding="utf-8")
            (tmp / "memory").mkdir()
            (tmp / "memory" / (OLD + "_memory.md")).write_text("# 1\n旧记忆\n", encoding="utf-8")
            sd = tmp / "storage" / ("user-%s-personal" % OLD) / "scoped" / "x"
            sd.mkdir(parents=True)
            (sd / "kv.json").write_text('{"pinned":[]}', encoding="utf-8")
            (tmp / "storage/skeleton").mkdir(parents=True)
            (tmp / "storage/skeleton/account-snapshot.json").write_text(
                json.dumps({"primary": {"uid": OLD, "nickname": "旧", "savedAt": 1}}), encoding="utf-8")
            cdir = tmp / "connectors" / OLD
            cdir.mkdir(parents=True)
            (cdir / "connector-states.json").write_text(json.dumps({
                "encryption": {"salt": _b64.b64encode(_salt).decode(),
                               "userIdCheck": mig.compute_check(OLD.encode(), _salt),
                               "keyCheck": "AAAAAAAAAAAAAAAAAAAAAA=="},
                "connectors": {"k": "cipher"},
                "accountIdentityKey": OLD + "||enterprise"}, ensure_ascii=False), encoding="utf-8")
            (tmp / "settings.json").write_text(
                json.dumps({"claw": {"users": {OLD: {"channels": {}}}}}), encoding="utf-8")

            # 扫描
            pv = mig.preview(OLD, NEW)
            keys = [i["key"] for i in pv["items"]]
            check("预览列出会话/记忆/设置/连接器/渠道/快照",
                  set(keys) >= {"sessions", "memory", "storage", "connectors", "settings", "snapshot"},
                  keys)
            check("预览标出运行中的会话会跳过",
                  any("正在运行" in (i.get("note") or "") for i in pv["items"]), pv["items"])
            check("预览说明正文不用搬", "无需搬运" in (pv.get("projects_note") or ""), "")

            # 迁移
            res = mig.migrate(OLD, NEW, {"mode": "move", "allow_client_running": True})
            check("迁移成功", res["ok"] is True, res["message"][:110])
            check("迁移后未回滚", res["rolled_back"] is False)
            con = sqlite3.connect("file:%s?mode=ro" % (tmp / "workbuddy.db").as_posix(), uri=True)

            def cnt(sql, *a):
                return con.execute(sql, a).fetchone()[0]

            check("会话归属已改到新账号（2 个）", cnt("SELECT COUNT(*) FROM sessions WHERE user_id=?", NEW) == 2)
            check("运行中的会话仍留在旧账号（不迁）",
                  cnt("SELECT COUNT(*) FROM sessions WHERE user_id=? AND status='working'", OLD) == 1)
            check("定时任务归属已改", cnt("SELECT COUNT(*) FROM automations WHERE owner_user_id=?", NEW) == 1)
            con.close()
            check("记忆已改名到新账号",
                  (tmp / "memory" / (NEW + "_memory.md")).is_file()
                  and not (tmp / "memory" / (OLD + "_memory.md")).is_file())
            check("账号级设置目录已改名", (tmp / "storage" / ("user-%s-personal" % NEW)).is_dir())
            cj = json.loads((tmp / "connectors" / NEW / "connector-states.json").read_text(encoding="utf-8"))
            check("连接器 userIdCheck 已按新 uid 重算（漏做会触发删配置）",
                  cj["encryption"]["userIdCheck"] == mig.compute_check(NEW.encode(), _salt),
                  cj["encryption"]["userIdCheck"])
            check("连接器 keyCheck 未被动（与 uid 无关）",
                  cj["encryption"]["keyCheck"] == "AAAAAAAAAAAAAAAAAAAAAA==")
            check("连接器加密内容原样保留", cj["connectors"] == {"k": "cipher"})
            check("settings.json 账号键已改名",
                  NEW in json.loads((tmp / "settings.json").read_text(encoding="utf-8"))["claw"]["users"])
            check("账号快照 uid 已更新",
                  json.loads((tmp / "storage/skeleton/account-snapshot.json").read_text(
                      encoding="utf-8"))["primary"]["uid"] == NEW)
            check("对话正文仍在原地（不搬）",
                  (tmp / "projects/d-proj" / (SID1 + ".jsonl")).is_file())
            check("迁移前做了 db 快照",
                  (Path(res["backup_dir"]) / "workbuddy.db").is_file())

            # 回滚：注入写 settings 失败
            real_wt = Path.write_text

            def _boom(self, *a, **k):
                if self.name == "settings.json.tmp":
                    raise OSError("注入的写盘失败")
                return real_wt(self, *a, **k)

            Path.write_text = _boom
            try:
                res2 = mig.migrate(NEW, OLD, {"mode": "move", "allow_client_running": True})
            finally:
                Path.write_text = real_wt
            check("中途失败时如实报错且标记已回滚",
                  res2["ok"] is False and res2["rolled_back"] is True, res2["message"][:110])
            con = sqlite3.connect("file:%s?mode=ro" % (tmp / "workbuddy.db").as_posix(), uri=True)
            check("回滚后数据库归属完整还原",
                  con.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (NEW,)).fetchone()[0] == 2
                  and con.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (OLD,)).fetchone()[0] == 1)
            con.close()
            # 回滚的目标是「第 2 次迁移之前」的状态：那时连接器在 NEW 名下（第 1 次迁移搬过去的），
            # 所以回滚后应该是 NEW 在、OLD 不在。
            check("回滚后连接器目录退回原处",
                  (tmp / "connectors" / NEW).is_dir() and not (tmp / "connectors" / OLD).is_dir(),
                  sorted(p.name for p in (tmp / "connectors").iterdir()))

            # 客户端在跑时应拒绝迁移
            real_rc = mig.running_clients
            mig.running_clients = lambda: ["workbuddyai.exe"]
            try:
                res3 = mig.migrate(NEW, OLD, {"mode": "move"})
            finally:
                mig.running_clients = real_rc
            check("检测到客户端在跑时拒绝迁移", res3["ok"] is False and "退出" in res3["message"],
                  res3["message"][:90])
        finally:
            mig.ROOT_OVERRIDE = None

    # --- 14e. 接口与模板 ---
    srv3, port3 = common.bind_server(wb.Handler, 8951)
    threading.Thread(target=srv3.serve_forever, daemon=True).start()
    try:
        time.sleep(0.25)
        st, raw = call(port3, "/api/migrate-preview?name=nope.info")
        check("[wb] GET /api/migrate-preview 可用", st == 200 and json.loads(raw).get("ok") is False, st)
        st, raw = call(port3, "/")
        html = raw.decode("utf-8", "replace")
        check("[wb] 迁移弹框已注入", 'id="migModal"' in html and 'migrate: "1"' in html, st)
        check("[wb] 迁移预览接口已接上", "/api/migrate-preview" in html, "")
        check("[wb] 「仅切换」出口保留", "migConfirm(false)" in html, "")
        # 用语义片段而不是写死的整行：之前断言写死了 "migGo').disabled = blocked"，
        # 后来把代码改成先取变量再赋值，断言就假失败了。
        check("[wb] 客户端在跑时禁用「切换并迁移」按钮",
              "go.disabled = blocked" in html and "const go = document.getElementById('migGo')" in html, "")
        check("[wb] 置灰时给出可操作的原因",
              "请先完全退出 WorkBuddy 客户端再迁移" in html, "")
    finally:
        srv3.shutdown()
        srv3.server_close()
    tpl = (BIN / "ui_template.html").read_text(encoding="utf-8")
    for label, ctx in (("[wb]", wb.Handler.UI_CONTEXT), ("[tw]", tw.Handler.UI_CONTEXT)):
        rendered = common.render_template(tpl, ctx).decode("utf-8")
        left = sorted(set(re.findall(r"\{\{[A-Za-z0-9_]+\}\}", rendered)))
        check("%s MIGRATE 占位符已配（无残留）" % label, not left, left)
    check("[tw] 迁移能力关闭时隐藏弹框",
          'migrate: ""' in common.render_template(tpl, tw.Handler.UI_CONTEXT).decode("utf-8"), "")

    print("\n== 15. 单实例保护（见 DESIGN_single_instance.md）==")
    # --- 15a. 互斥体语义 ---
    check("wb 与 tw 的互斥体名不同（否则两个工具会互相挤掉）",
          common.MUTEX_NAME_WB != common.MUTEX_NAME_TW,
          (common.MUTEX_NAME_WB, common.MUTEX_NAME_TW))
    mname = "Local\\WorkBuddySwitcher-smoketest"
    h1, already1 = common.acquire_single_instance(mname)
    h2, already2 = common.acquire_single_instance(mname)
    check("首次获取互斥体 → 可启动", already1 is False, already1)
    check("同进程再次获取 → 判定已有实例", already2 is True, already2)
    check("互斥体句柄被模块级持有（防止被 GC 提前回收）",
          h1 in common._MUTEX_HANDLES and h2 in common._MUTEX_HANDLES, len(common._MUTEX_HANDLES))

    # --- 15b. 身份探测：三种端口状态要能区分 ---
    # 别人的服务放在前面，我们的紧挨其后 —— 这样"跳过别人的端口找到自己"才可测
    class _Foreign(common.BaseHandler):
        APP_NAME = "not_us"          # 也有 /api/ping，但不是我们

        def api_get(self, u):
            return 404, {"ok": False}

    srv_other, port_other = common.bind_server(_Foreign, 8971)
    threading.Thread(target=srv_other.serve_forever, daemon=True).start()
    srv_ours, port_ours = common.bind_server(wb.Handler, 8972)
    threading.Thread(target=srv_ours.serve_forever, daemon=True).start()
    time.sleep(0.3)
    try:
        info = common.probe_instance(port_ours)
        check("probe_instance 认出我们的服务", bool(info) and info.get("app") == "wb_switcher",
              info and {k: info[k] for k in ("app", "source", "pid", "port")})
        check("/api/ping 带 source / pid / port / version",
              all(k in (info or {}) for k in ("source", "pid", "port", "version", "started_at")),
              sorted((info or {}).keys()))
        check("probe_instance 对空闲端口返回 None", common.probe_instance(8999) is None, "")
        check("probe_instance 不把别人的服务当成自己",
              common.probe_instance(port_other) is None, "")
        got = common.probe_instance_range(port_other, tries=3)
        check("probe_instance_range 会跳过非我们的端口找到自己",
              bool(got) and got.get("port") == port_ours, got and got.get("port"))
        check("OUR_APPS 白名单挡住了别人的 /api/ping",
              common.probe_instance(port_other) is None
              and "not_us" not in common.OUR_APPS, common.OUR_APPS)

        # --- 15c. /api/ping 不需要一次性令牌 ---
        wb.Handler.TOKEN = "secret-token"
        try:
            st, raw = call(port_ours, "/api/ping")
            check("/api/ping 免令牌可访问（探测方拿不到令牌）",
                  st == 200 and json.loads(raw).get("app") == "wb_switcher", st)
            st, raw = call(port_ours, "/api/accounts")
            check("其它 GET 仍受令牌约束之外的正常处理", st == 200, st)
        finally:
            wb.Handler.TOKEN = None

        # --- 15d. guard 的三种结果 ---
        # 同进程起的服务**不该**被当成"另一个实例"（pid 相同），否则自己就把自己挡住
        act0 = common.single_instance_guard("Local\\WorkBuddySwitcher-probe0-" + str(os.getpid()),
                                            port_ours, tries=2, wait=0.2,
                                            default_port=port_ours)[1]
        check("本进程自己的服务不算另一个实例 → action=start", act0 == "start", act0)
        act2 = common.single_instance_guard("Local\\WorkBuddySwitcher-probe2-" + str(os.getpid()),
                                            8990, tries=2, wait=0.2, default_port=8990)[1]
        check("端口全空闲且无实例 → action=start", act2 == "start", act2)
        # reuse 必须在**另一个进程**里才能验：pid 不同的服务才会被认作"已有实例"
        sub_port = 8981
        sub_code = (
            "import sys, threading, time\n"
            "sys.path.insert(0, r'%s'); sys.path.insert(0, r'%s')\n"
            "import switcher_common as c, wb_ui_server as wb\n"
            "s, p = c.bind_server(wb.Handler, %d)\n"
            "threading.Thread(target=s.serve_forever, daemon=True).start()\n"
            "print('up', p, flush=True)\n"
            "time.sleep(90)\n"
        ) % (str(BIN), str(BIN.parent / "自动签到"), sub_port)
        sub = subprocess.Popen([sys.executable, "-c", sub_code],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace")
        try:
            for _ in range(40):
                if common.probe_instance(sub_port):
                    break
                time.sleep(0.25)
            live = common.probe_instance(sub_port)
            check("另一个进程里起了我们的服务（前置条件）",
                  bool(live) and str(live.get("pid")) != str(os.getpid()),
                  live and live.get("pid"))
            act1 = common.single_instance_guard("Local\\WorkBuddySwitcher-probe1-" + str(os.getpid()),
                                                sub_port, tries=2, wait=0.2,
                                                default_port=sub_port)[1]
            check("另一个进程已有我们的实例 → action=reuse", act1 == "reuse", act1)
        finally:
            try:
                sub.terminate()
                sub.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass

        # --- 15e. 迁移：不得与另一个实例同时进行 ---
        check("迁移选项里有 allow_other_instance（仅自检/演练用）",
              "allow_other_instance" in mig.DEFAULT_OPTS, sorted(mig.DEFAULT_OPTS))
        old_port = mig.INSTANCE_PORT
        try:
            # 指向本进程自己的服务 → 不应被当成"另一个实例"
            mig.INSTANCE_PORT = port_ours
            check("other_instance 排除掉自己（pid 相同不算）", mig.other_instance() is None,
                  mig.other_instance())
            # 指向"别人的服务" → 也不算我们的实例
            mig.INSTANCE_PORT = port_other
            check("other_instance 不认别人的服务", mig.other_instance() is None, "")
            # 伪造一个真·另一个实例
            real_oi = mig.other_instance
            mig.other_instance = lambda: {"pid": 999999, "port": 8765, "app": "wb_switcher"}
            try:
                with tempfile.TemporaryDirectory() as td:
                    mig.ROOT_OVERRIDE = Path(td)
                    try:
                        # allow_client_running=True 是为了走到"另一个实例"那道检查 ——
                        # 客户端那道在它之前，不放行就短路了，测不到本用例。
                        r = mig.migrate("aaaaaaaa-0000-0000-0000-000000000009",
                                        "bbbbbbbb-0000-0000-0000-00000000000a",
                                        {"mode": "move", "allow_client_running": True})
                    finally:
                        mig.ROOT_OVERRIDE = None
                    check("检测到另一个实例时拒绝迁移",
                          r["ok"] is False and "另一个切换器实例" in r["message"], r["message"][:90])
                    check("被拒时不做任何写入（连备份目录都没建）",
                          not (Path(td) / ".migration-backup").exists(), "")
            finally:
                mig.other_instance = real_oi
        finally:
            mig.INSTANCE_PORT = old_port
    finally:
        for s in (srv_ours, srv_other):
            s.shutdown()
            s.server_close()

    # --- 15f. 启动脚本 ---
    for f, svc in (("workbuddy_switcher.cmd", "wb_ui_server.py"),
                   ("trae_switcher.cmd", "tw_ui_server.py")):
        txt = (BIN / f).read_text(encoding="utf-8", errors="replace")
        check("%s 不再只用 where python 判断（会命中 Store 占位符）" % f,
              "where python" not in txt and "sys.version_info" in txt, "")
        check("%s 调用的服务脚本正确" % f, svc in txt, "")
        check("%s 是纯 ASCII（避免 OEM 代码页乱码）" % f,
              all(ord(c) < 128 for c in txt), "")
        check("%s 用 CRLF 换行" % f,
              (BIN / f).read_bytes().count(b"\r\n") > 5, "")

    print("\n== 16. 不重复打开同一地址的页面 ==")
    # 旧行为：复用分支无条件 webbrowser.open(url) —— 每双击一次 .cmd 就多一个标签页。
    # 现在由"运行中的实例"通过 /api/ping 的 page_open 回答"有没有页面开着"，
    # 只有它说没有时才开。
    import webbrowser as _wb
    opened = []
    _real_open = _wb.open
    _wb.open = lambda u, *a, **k: (opened.append(u), True)[1]
    try:
        def _reset():
            common._PAGE_STATE.update(seen=0.0, opened=0.0)

        # --- 16a. open_page 的三种情形 ---
        _reset()
        check("无页面时 open_page 会打开", common.open_page("http://x/") is True and len(opened) == 1,
              opened)
        check("刚开过（宽限期内）不再开", common.open_page("http://x/") is False and len(opened) == 1,
              opened)
        common._PAGE_STATE["opened"] = time.time() - (common.PAGE_OPEN_GRACE + 1)
        check("宽限期过后仍未加载出页面 → 允许再开",
              common.open_page("http://x/") is True and len(opened) == 2, opened)

        # --- 16b. 心跳与打开宽限必须分开判（早先用 max 把两者混在一起，自检抓到） ---
        _reset()
        common.mark_page_seen()
        check("有心跳 → 认为页面开着", common.page_is_open() is True, common.page_state())
        common._PAGE_STATE["seen"] = time.time() - (common.PAGE_TTL + 1)
        check("心跳过期(%ds) → 认为页面已关闭" % common.PAGE_TTL,
              common.page_is_open() is False, common.page_state())
        _reset()
        common._PAGE_STATE["opened"] = time.time() - (common.PAGE_OPEN_GRACE + 1)
        check("打开宽限过期(%ds) → 认为页面已关闭" % common.PAGE_OPEN_GRACE,
              common.page_is_open() is False, common.page_state())

        # --- 16c. report_reuse 依运行中实例的 page_open 决定 ---
        opened.clear()
        common.report_reuse({"pid": 1, "port": 9999, "page_open": True}, True, "v", 9999)
        check("复用分支：对方说页面开着 → 不再开标签页", opened == [], opened)
        common.report_reuse({"pid": 1, "port": 9999, "page_open": False}, True, "v", 9999)
        check("复用分支：对方说没有页面 → 才打开", opened == ["http://127.0.0.1:9999/"], opened)
    finally:
        _wb.open = _real_open
        common._PAGE_STATE.update(seen=0.0, opened=0.0)

    # --- 16d. /api/page-alive 与 /api/ping 的 page_open ---
    srv4, port4 = common.bind_server(wb.Handler, 8995)
    threading.Thread(target=srv4.serve_forever, daemon=True).start()
    try:
        time.sleep(0.25)
        st, raw = call(port4, "/api/ping")
        check("/api/ping 带 page_open 字段", "page_open" in json.loads(raw), sorted(json.loads(raw)))
        check("尚未请求首页时 page_open=False", json.loads(raw).get("page_open") is False, "")
        st, _ = call(port4, "/")
        check("GET / 会记一次页面存活", st == 200, st)
        st, raw = call(port4, "/api/ping")
        check("请求过首页后 page_open=True（跨进程可见）",
              json.loads(raw).get("page_open") is True, raw[:90])
        wb.Handler.TOKEN = "secret"
        try:
            st, raw = call(port4, "/api/page-alive")
            check("/api/page-alive 免令牌可访问",
                  st == 200 and json.loads(raw).get("page_open") is True, st)
        finally:
            wb.Handler.TOKEN = None
    finally:
        srv4.shutdown()
        srv4.server_close()

    # --- 16e. 前端与启动器 ---
    tpl16 = (BIN / "ui_template.html").read_text(encoding="utf-8")
    for k in ("id=\"dupBar\"", "BroadcastChannel", "/api/page-alive", "closeThisTab",
              "setInterval(beat, 5000)"):
        check("模板含 %s" % k, k in tpl16, "")
    check("模板占位符未受影响（MIGRATE 仍在）", "{{MIGRATE}}" in tpl16, "")
    for f in ("wb_ui_app.py", "tw_ui_app.py"):
        s = (BIN / f).read_text(encoding="utf-8")
        # 只看复用分支：正常启动路径当然还是要开窗口的
        i = s.find('if action == "reuse":')
        j = s.find('if action == "abort":', i + 1)
        blk = s[i:j] if (i >= 0 and j > i) else ""
        check("%s 复用分支改为弹提示、不再开新窗口" % f,
              bool(blk) and "notify_user" in blk and "_show_window" not in blk, blk[:70])
        check("%s 正常启动路径仍会开窗口" % f, "_show_window(url)" in s, "")
    for f in ("wb_ui_server.py", "tw_ui_server.py"):
        s = (BIN / f).read_text(encoding="utf-8")
        check("%s 启动时用 open_page（不是裸 webbrowser.open）" % f,
              "common.open_page(url)" in s and "webbrowser.open(url)" not in s, "")

    # --- 16f. 探测耗时：串行探 10 个空端口要 4 秒、两个区间 8 秒，.cmd 会像卡死 ---
    t0 = time.time()
    common.probe_instance_range(9001, tries=10, timeout=0.3)
    dt = time.time() - t0
    check("probe_instance_range 是并发的（10 个空端口 < 2s，串行要 4s+）",
          dt < 2.0, "%.2fs" % dt)
    t0 = time.time()
    common.single_instance_guard("Local\\WorkBuddySwitcher-timing-%d" % os.getpid(),
                                 9001, tries=common.PORT_TRIES, default_port=9011,
                                 log=lambda *a: None)
    dt2 = time.time() - t0
    check("冷启动守卫总耗时 < 2.5s（曾因串行探测达到 8.3s）", dt2 < 2.5, "%.2fs" % dt2)

    # --- 16g. --no-open：脚本/测试用的"别开浏览器"开关 ---
    for f in ("wb_ui_server.py", "tw_ui_server.py"):
        r = subprocess.run([sys.executable, str(BIN / f), "--help"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        check("%s 提供 --no-open（否则脚本/测试会意外弹页面）" % f,
              "--no-open" in (r.stdout or ""), "")
        s = (BIN / f).read_text(encoding="utf-8")
        check("%s 的 --no-open 真的接到了 open_browser" % f,
              "open_browser=not args.no_open" in s, "")
        # 端口提示必须与"请求的端口"比：显式传 --port 时曾误报"默认端口被占用"
        check("%s 端口提示不再拿默认端口常量比" % f,
              "requested = port" in s and "if port != requested:" in s, "")

    print("\n== 17. 导航栏 / 紧凑化 / 打开客户端 ==")
    tpl17 = (BIN / "ui_template.html").read_text(encoding="utf-8")

    # --- 17a. 导航栏：位置（sticky 顶部）、入口（3 锚点 + 2 动作）、交互（滚动高亮）---
    check("导航栏存在且 sticky 常驻顶部", '.nav {' in tpl17 and 'position:sticky' in tpl17, "")
    for tgt in ("secCurrent", "secAccounts", "secAdd"):
        check("导航入口指向 %s" % tgt,
              ('data-target="%s"' % tgt) in tpl17 and ('id="%s"' % tgt) in tpl17, "")
    check("导航含「刷新」与「打开客户端」两个动作",
          'onclick="load();loadCredits(true);loadCheckin(true)"' in tpl17
          and 'id="openClientBtn"' in tpl17, "")
    check("导航交互：点击平滑滚动 + 滚动高亮",
          "scrollIntoView" in tpl17 and "IntersectionObserver" in tpl17, "")
    check("导航栏含服务连接状态", 'id="srvDot"' in tpl17 and 'id="srvText"' in tpl17, "")

    # --- 17b. 删除按钮红色，但尺寸/形状沿用 ghost（风格一致）---
    check("删除按钮用 danger 类", 'class="btn ghost danger"' in tpl17, "")
    check("删除按钮判定同步改成 danger", "contains('danger')" in tpl17 and "del-btn" not in tpl17, "")
    check("danger 只改配色、不改尺寸（沿用 .btn.ghost 的 padding/圆角）",
          ".btn.danger { border-color:rgba(255,92,92,.45); color:var(--red); }" in tpl17, "")
    check("danger 用主题里的 --red 变量（不是硬编码色值）",
          ".btn.danger:hover { background:rgba(255,92,92,.12); border-color:var(--red); color:var(--red); }" in tpl17, "")

    # --- 17c. 列表更窄更矮 ---
    for needle, why in ((".wrap { max-width: 860px;", "整体更窄且贴合行内容"),
                        (".list { display:flex; flex-direction:column; gap:7px; }", "行间距更小"),
                        # 254 = 能容下「登录态剩余 · 日期」一行（实测该行 204px，
                        # 加头像 32 + 间距 9）。窄于这个值它就会折成两行。
                        (".who { flex:0 0 254px;", "身份列容得下剩余时间一行"),
                        (".acts { flex:0 0 110px;", "操作列更窄"),
                        (".ci-bar { flex:1 1 60px; min-width:60px; height:4px;", "进度条更细且自适应")):
        check("紧凑化：%s（%s）" % (needle.split("{")[0].strip(), why), needle in tpl17, "")
    # --- 17c2. 身份列不再"串行"：meta 与 exp 各占独立位置 ---
    check("meta 不再重复 [当前] 标签已说明的「桌面端正在使用」",
          "' · 桌面端正在使用'" not in tpl17, "")
    check("exp 拆成两个 nowrap 片段（折行时不会留下孤立的 ·）",
          ".exp-ttl, .exp-when { white-space:nowrap; }" in tpl17
          and '<span class="exp-ttl">' in tpl17 and '<span class="exp-when">' in tpl17, "")
    check("meta / exp 有正常行高与间距（折行后不贴在一起）",
          "line-height:1.4" in tpl17 and ".exp { font-size:12px; color:var(--dim); line-height:1.4; margin-top:4px;" in tpl17, "")
    # 进度条与数字同排，整排宽度 == 上方文字行宽度（左右边缘对齐）。
    # 否则填充比例读不出来：条子 240px、文字行 447px 时，80% 的填充只占整行的 43%。
    check("进度条与数字同排（.ci-line 包裹 bar + used）",
          '<div class="ci-line">' in tpl17 and ">已使用 '" in tpl17.replace('"', "'"), "")
    # 积分块必须**填满** .body：用 fit-content 会让它只占 300px、而 .body 仍占满
    # 剩余宽度，中间空出一大块（用户截图反馈"红框区域太大"）。
    check("积分块填满 .body（不留死区）",
          ".ci { display:flex; flex-direction:column; gap:4px; width:100%; }" in tpl17, "")
    check("进度条自适应剩余宽度（不再写死 max-width）",
          "max-width:240px" not in tpl17 and ".ci-line { display:flex; align-items:center; gap:10px;" in tpl17, "")
    check("积分块把「档位名/时间/用量」压到同一行（省两行高度）",
          'h+=\'<div class="ci">\'\n      +\'<div class="ci-info">\'\n      +\'<span class="ci-name">\'' in tpl17, "")

    # --- 17d. 加载：三件事并发 + 启动预热 ---
    check("首屏三个请求并发发起（不再等 load 回来才拉积分/签到）",
          "load();\nloadCredits();\nloadCheckin();" in tpl17, "")
    check("服务端启动时预热积分/签到缓存",
          hasattr(wb, "warmup_async") and "warmup_async()" in (BIN / "wb_ui_server.py").read_text(encoding="utf-8"), "")

    # --- 17e. 打开客户端 ---
    exe = wb.find_client_exe()
    check("能定位到 WorkBuddy 客户端可执行文件", exe is None or exe.is_file(), str(exe))
    cs = wb.client_status()
    check("client_status 返回可 JSON 序列化的 dict（Path 会让响应 500）",
          isinstance(cs, dict) and isinstance(cs.get("pids"), list)
          and isinstance(cs.get("exe"), str) and isinstance(cs.get("running"), bool), cs)
    check("当前用户有客户端在运行时应能检出", cs["running"] is True, cs["pids"])
    before = len(cs["pids"])
    ok, msg = wb.open_client()
    pids2 = wb.client_status()["pids"]
    check("客户端已在运行时 open_client 不会重复启动", ok is True and len(pids2) <= before,
          "%d -> %d" % (before, len(pids2)))
    check("open_client 的返回消息可操作（说明是切前台还是已启动）",
          ("前台" in msg) or ("已启动" in msg), msg[:80])

    srv5, port5 = common.bind_server(wb.Handler, 8996)
    threading.Thread(target=srv5.serve_forever, daemon=True).start()
    try:
        time.sleep(0.25)
        st, raw = call(port5, "/api/client-status")
        check("[wb] GET /api/client-status 可用", st == 200 and "client" in json.loads(raw), st)
        st, raw = call(port5, "/api/open-client")
        check("[wb] GET /api/open-client 被拒（写接口只收 POST）", st == 405, st)
        st, raw = call(port5, "/api/open-client", "POST", {})
        check("[wb] POST /api/open-client 可用",
              st == 200 and "ok" in json.loads(raw), (st, raw[:80]))
    finally:
        srv5.shutdown()
        srv5.server_close()

    print("\n失败项：%s" % (FAIL or "无"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
