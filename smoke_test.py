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

    print("\n== 3. 令牌完整性校验 ==")
    check("完整 JWT 通过", common.token_looks_complete("eyJhbGciOi." + "a" * 300 + "." + "b" * 300))
    check("截断值被拒", not common.token_looks_complete("eyJx"))
    check("空值被拒", not common.token_looks_complete(""))

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
            st, raw = call(port, "/api/accounts")
            check(label + " GET /api/accounts", st == 200 and json.loads(raw).get("ok"), st)
            accs = json.loads(raw).get("accounts") or []
            check(label + " 账号带 token_source 字段",
                  all("token_source" in a for a in accs), accs[:1])
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

        # 残缺登录态：add 必须拒绝，且不能留下文件
        good = (wb.AUTH_DIR / "workbuddy-wtnong.info").read_text(encoding="utf-8")
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

    print("\n失败项：%s" % (FAIL or "无"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
