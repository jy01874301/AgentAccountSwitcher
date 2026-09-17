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
            if label == "[wb]":
                check("[wb] 前端含积分明细模板", "积分明细" in html and "credBtn" in html, st)
                # 时间与已使用量同行、两端对齐；两块 nowrap 保证不会"一侧折行一侧单行"
                check("[wb] 积分明细同行布局", "ci-info" in html and "ci-when" in html, st)
            else:
                check("[tw] 无积分接口时不显示刷新按钮",
                      'id="credBtn" style="margin-left:12px;display:none"' in html, st)
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
                ok, msg = wb.switch_account(target_name)
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
                ok, msg = wb.switch_account(target_name)
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

    print("\n失败项：%s" % (FAIL or "无"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
