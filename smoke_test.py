#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smoke_test.py —— 本地切换器的回归自检（无需第三方依赖）。

覆盖：备份裁剪 / 文件锁 / 端口避让 / 两个切换器的接口行为 / 令牌完整性校验 / 增删闭环。
不改动真实登录态：切号这类会写客户端配置的操作一律用临时目录或不存在的账号名来触发。

用法：
    python smoke_test.py
退出码 0 表示全部通过，1 表示有失败项。
"""
import ast
import http.client
import io
import inspect
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


# 启动器**不许**自己赋的路径名。用白名单式列举而不是"任何 srv.X ="：
# 启动器合法地要写 `srv.Handler.TOKEN = ...`（一次性令牌），一刀切会假失败。
PATH_NAMES = ("_BIN_DIR", "SCRIPT_DIR", "LOCK_DIR", "TW_AUTH_DIR", "AUTH_DIR",
              "DESKTOP_DIR", "DESKTOP_INFO", "DESKTOP_ROOT_ID")


def assigns_paths(src):
    """启动器里是否出现"自己给路径赋值"（应改成调用 srv.rebind()）。"""
    code = code_only(src)
    for n in PATH_NAMES:
        if re.search(r"^\s*srv\.%s\s*=" % re.escape(n), code, re.M):
            return True
    return bool(re.search(r"^\s*srv\.Handler\.(BASE_DIR|AUDIT_DIR)\s*=", code, re.M))


def code_only(src):
    """剥掉注释行，只留代码 —— 断言"源码里没有 X"时必须先过一遍它。

    这个假失败本会话已经踩过**三次**：说明性注释里提到 `srv.AUTH_DIR = ...`、
    `import ast`、`srv.X = ...`，全文匹配就把注释当成代码。
    比对 spec 里"没写死绝对路径"那条同理（那条还要额外剥模块 docstring）。
    """
    return "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())


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
    bad_file = wb.CHANNELS["wb"].auth_dir / "workbuddy-__bad.info"
    try:
        s_wb, port_wb = common.bind_server(wb.Handler, 8911)
        s_tw, port_tw = common.bind_server(tw.Handler, 8912)
        for s in (s_wb, s_tw):
            threading.Thread(target=s.serve_forever, daemon=True).start()
            servers.append(s)
        time.sleep(0.3)

        # 入口页 / 两个视图的关系本身就是断言对象：
        #   /      → 统一入口页（侧边导航栏，两个管理入口）
        #   /wb    → WorkBuddy（国服）账号管理视图
        #   /wbai  → WorkBuddyAI（国际服）账号管理视图
        st, raw = call(port_wb, "/")
        hub = raw.decode("utf-8", "replace")
        check("[wb] GET / 返回统一入口页", st == 200 and "统一入口" in hub, st)
        check("[wb] 入口页含侧边导航栏（side-nav）", 'class="side-nav"' in hub, "")
        check("[wb] 侧边栏两个管理入口齐全",
              'data-view="wb"' in hub and 'data-view="wbai"' in hub, "")
        check("[wb] 入口名称区分 WorkBuddy / WorkBuddyAI",
              "WorkBuddy账号管理" in hub and "WorkBuddyAI账号管理" in hub, "")
        check("[wb] 入口标注国服 / 国际服", "国服" in hub and "国际服" in hub, "")
        check("[wb] 两个入口指向各自独立视图",
              'data-src="/wb"' in hub and 'data-src="/wbai"' in hub, "")
        check("[wb] 入口页无残留占位符", "{{" not in hub, hub[:60])
        # 入口页必须能自证"不是从服务打开的"。曾经只在侧边栏写一句「读取失败」，
        # 用户双击 html / 静态托管时看到它完全无从判断 —— 是页面坏了还是服务没起。
        check("[wb] 入口页含离线提示条与离线占位",
              'id="offBar"' in hub and 'id="viewOff"' in hub, "")
        check("[wb] 入口页离线时区分「未连接到本地服务」与「读取失败」",
              "未连接到本地服务" in hub and "'读取失败 ' + e.message.slice(5)" in hub, "")
        check("[wb] 入口页离线时不加载 iframe（show() 带 online 门卫）",
              "if(k===key && online && !v.el.getAttribute('src'))" in hub, "")

        st, raw = call(port_wb, "/wbai")
        wbai_html = raw.decode("utf-8", "replace")
        check("[wb] GET /wbai 返回国际服视图",
              st == 200 and "WorkBuddyAI 账号管理" in wbai_html, st)
        # 2026-09-21 起国际服也开放了迁移（migrate: "1" → 页面上会弹迁移确认框）；
        # 签到仍然关闭（接口 active=false）。
        check("[wb] 国际服视图显示积分/续期/迁移、不显示签到",
              'credits: "1"' in wbai_html and 'refresh: "1"' in wbai_html
              and 'migrate: "1"' in wbai_html and 'checkin: ""' in wbai_html, "")
        # 两个通道的计费网关不是同一个：拿国际服的 accessToken 去打国服网关恒 401，
        # 页面上就是每行「积分接口 HTTP 401」。endpoint 必须按通道给（见 channel_cfg）。
        check("[wb] 两个通道的网关配置各自独立",
              wb.channel_cfg("wb")["endpoint"] != wb.channel_cfg("wbai")["endpoint"]
              and "workbuddy.ai" in wb.channel_cfg("wbai")["endpoint"],
              (wb.channel_cfg("wb")["endpoint"], wb.channel_cfg("wbai")["endpoint"]))
        check("[wb] 国际服视图接口前缀指向 /api/wbai/",
              "'/api/wbai/accounts'" in wbai_html, "")
        st, raw = call(port_wb, "/api/wbai/accounts")
        check("[wb] GET /api/wbai/accounts 可用",
              st == 200 and json.loads(raw).get("ok"), st)
        st, raw = call(port_wb, "/api/wbai/current")
        wbai_cur = json.loads(raw).get("current") or []
        check("[wb] /api/wbai/current 只认 workbuddy-desktop-ai*",
              st == 200 and all(e["file"].startswith("workbuddy-desktop-ai") for e in wbai_cur),
              [e["file"] for e in wbai_cur][:3])
        st, raw = call(port_wb, "/api/wbai/switch", "GET")
        check("[wb] 国际服写接口同样只收 POST（405）", st == 405, st)
        # 2026-09-21 起国际服**自己管**自己的客户端：进程名按通道区分后，
        # /api/wbai/open-client 拉起来的就是 WorkBuddyAI.exe，不会再误开国服客户端。
        st, raw = call(port_wb, "/api/wbai/open-client", "POST", {})
        check("[wb] /api/wbai/open-client 可用（开的是国际服自己的客户端）",
              st == 200 and json.loads(raw).get("ok") is True,
              (st, raw[:90]))
        st, raw = call(port_wb, "/api/wbai/client-status")
        j_ai_cs = json.loads(raw)
        check("[wb] /api/wbai/client-status 报国际服进程（不谎报国服的）",
              st == 200 and isinstance(j_ai_cs.get("client"), dict)
              and "unsupported" not in j_ai_cs
              and "WorkBuddyAI.exe" in str(j_ai_cs.get("client", {}).get("exe", "")),
              raw[:120])
        st, raw = call(port_wb, "/api/client-status")
        check("[wb] 国服 /api/client-status 仍返回真实进程状态",
              st == 200 and isinstance(json.loads(raw).get("client"), dict), raw[:70])

        for label, port in (("[wb]", port_wb), ("[tw]", port_tw)):
            path = "/wb" if label == "[wb]" else "/"
            st, raw = call(port, path)
            html = raw.decode("utf-8", "replace")
            check(label + " GET %s 返回视图页" % path,
                  st == 200 and ("账号管理" in html or "切换器" in html), st)
            check(label + " 模板已渲染（无残留占位符）", "{{" not in html, html[:60])
            # 三个工具按钮都默认隐藏，由 loadCredits / loadCheckin 按后端能力点亮
            check(label + " 工具条含三个按钮",
                  'id="credBtn"' in html and 'id="ckBtn"' in html and 'id="rfBtn"' in html, st)
            check(label + " 一键续期按钮已接上",
                  "doRefreshAll()" in html and "/api/refresh-all" in html, st)
            if label == "[wb]":
                check("[wb] 国服视图沿用历史接口前缀 /api/",
                      "'/api/accounts'" in html and "'/api/wbai/" not in html, "")
                check("[wb] 视图在 iframe 中会隐藏自身导航（嵌入模式）",
                      "window.self!==window.top" in html and "classList.add('embed')" in html, "")
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
            (wb.CHANNELS["wb"].auth_dir / "workbuddy-__smoke.info").unlink(missing_ok=True)

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
            tmp_file = wb.CHANNELS["wb"].auth_dir / "workbuddy-__smoke.info"
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

    print("\n== 5b. 切号落盘与失败回滚（桌面端目录重定向到临时目录）==")
    # ⚠️ 真实桌面端登录态绝不能碰，所以必须把**通道实例**的 desktop_dir 指到临时目录。
    # 早期版本改的是模块级 wb.DESKTOP_DIR / wb.DESKTOP_INFO —— 重构引入通道之后
    # 这两个名字不再是生效路径，重定向静默失效，自检于是真的切了本机的登录态。
    # 现在统一走 Channel.redirect()，重定向失败会直接写错目录、断言立刻失败，不会再静默。
    ch_wb = wb.CHANNELS["wb"]
    usable = [a for a in wb.list_accounts() if a.get("ok")]
    if not usable:
        check("[wb] 无可用账号素材，跳过落盘/回滚用例", True, "wb_auth 下没有可解析的 .info")
    else:
        target_name = Path(usable[0]["path"]).name
        target_uid = usable[0]["uid"]
        original = '{"account": {"uid": "orig-uid", "nickname": "原账号"}, "auth": {}}'
        orig_dir, orig_name = ch_wb.desktop_dir, ch_wb.info_name
        try:
            # --- 成功路径：正式文件应变成目标账号，原内容进备份 ---
            with tempfile.TemporaryDirectory() as td:
                desk = Path(td)
                info = desk / "workbuddy-desktop.info"
                info.write_text(original, encoding="utf-8")
                ch_wb.redirect(desk)
                check("[wb] 桌面端目录已重定向到临时目录（真实登录态不受影响）",
                      ch_wb.desktop_info == info, str(ch_wb.desktop_info))

                # --- 账号库路径必须是「派生值」而不是「导入时的快照」 ---
                # 冻结态 exe 会在 import 之后把 wb._BIN_DIR 指到 exe 所在目录，好让
                # 「账号库与 exe 同级」这个部署约定成立。若 Channel.auth_dir 是
                # __init__ 里存下的快照，那次改写就落不到已建好的通道上 ——
                # 表现是打包后的 exe 跑去 _internal\wb_auth\ 找账号、页面恒显示 0 个，
                # 而源码运行一切正常（双通道重构时踩过）。
                _bin_save = wb._BIN_DIR
                try:
                    wb._BIN_DIR = Path(td) / "exe_dir"
                    check("[wb] 账号库路径派生自 _BIN_DIR（不是导入时快照）",
                          wb.channel("wb").auth_dir == Path(td) / "exe_dir" / "wb_auth",
                          str(wb.channel("wb").auth_dir))
                    check("[wb] 国际服账号库同样跟随 _BIN_DIR",
                          wb.channel("wbai").auth_dir == Path(td) / "exe_dir" / "wbai_auth",
                          str(wb.channel("wbai").auth_dir))
                finally:
                    wb._BIN_DIR = _bin_save
                check("[wb] 还原 _BIN_DIR 后账号库回到源码目录",
                      wb.channel("wb").auth_dir == _bin_save / "wb_auth",
                      str(wb.channel("wb").auth_dir))

                # 模块级路径常量一个都不能复活：它们全是导入时的快照，
                # 而冻结态 exe 恰恰要在导入之后改基准目录 —— 留一个就留一个坑。
                for _dead in ("AUTH_DIR", "DESKTOP_DIR", "DESKTOP_INFO", "DESKTOP_ROOT_ID"):
                    check("wb_ui_server 不再暴露模块级路径常量 %s" % _dead,
                          not hasattr(wb, _dead), getattr(wb, _dead, None))
                _app_src = (Path(__file__).resolve().parent / "ui_app.py").read_text(encoding="utf-8")
                # 只匹配**真实赋值**，不扫全文：说明性注释里会提到 srv.AUTH_DIR，
                # 全文匹配会把注释当成代码（这个假失败本会话已经踩过好几次）。
                # 现在更进一步：启动器**一行都不许自己赋**，必须走 srv.rebind() ——
                # 逐个赋 5 行正是"漏改一个"的来源（AUTH_DIR 就是这么退化成快照别名的）。
                check("ui_app 只走 srv.rebind()，不逐个赋路径",
                      "srv.rebind(" in _app_src and not assigns_paths(_app_src), "")
                # exe 是桌面启动器：一次性 CLI 动作必须转交后端 main()，
                # 否则会被静默忽略并**弹出一个窗口**（脚本/计划任务还当它成功了）。
                check("ui_app 把一次性 CLI 动作转交后端（不开窗口）",
                      "_wants_server_cli(argv," in _app_src and "return srv.main()" in _app_src, "")
                # 修饰符单独出现时不该被当成动作（`exe --channel wbai` 仍应开原生窗口）
                check("ui_app 不把 --channel 这类修饰符当动作",
                      '"--channel"' not in _app_src.split('"cli_actions": (')[1].split(")")[0], "")
                # 行为验证用「抽出来 exec」而不是 import ui_app：
                # 后者在 import 时会把 srv._BIN_DIR 重置回项目目录，
                # 而本段正处在临时目录重定向里 —— import 会把重定向冲掉，
                # 后面的断言就会去动真实数据目录（这个坑本会话踩过）。
                _ns = {}
                exec(compile(ast.Module(body=[
                    _n for _n in ast.parse(_app_src).body
                    if (isinstance(_n, ast.Assign) and any(
                            getattr(_t, "id", "") == "_PRODUCTS" for _t in _n.targets))
                    or (isinstance(_n, ast.FunctionDef) and _n.name == "_wants_server_cli")
                ], type_ignores=[]), "<ui_app-cli>", "exec"), _ns)
                _wants = _ns["_wants_server_cli"]
                _wb_acts = _ns["_PRODUCTS"]["wb"]["cli_actions"]
                check("ui_app._wants_server_cli 认得动作与 = 写法",
                      _wants(["--refresh-all"], _wb_acts) is True
                      and _wants(["--prune=3"], _wb_acts) is True
                      and _wants(["--list"], _wb_acts) is True
                      and _wants(["--channel", "wbai", "--switch", "x.info"], _wb_acts) is True
                      and _wants(["--serve", "--port", "8790"], _wb_acts) is False
                      and _wants(["--channel", "wbai"], _wb_acts) is False
                      and _wants(["--force"], _wb_acts) is False
                      and _wants([], _wb_acts) is False, "")

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
                ch_wb.redirect(desk)
                ok, msg, _mig = wb.switch_account(target_name)
                check("写入失败时返回失败", ok is False, msg[:80])
                check("失败信息含回滚说明", "已回滚为原登录态" in msg, msg[:110])
                check("正式文件已恢复原内容",
                      info.is_file() and info.read_text(encoding="utf-8") == original,
                      info.read_text(encoding="utf-8")[:60] if info.is_file() else "文件不存在")
                check("回滚后无残留备份",
                      [p.name for p in desk.glob("workbuddy-desktop.*.info")] == [],
                      [p.name for p in desk.glob("*.info")])

            # --- P1-3 行为级回归：开了迁移 → 客户端被我们关掉后，
            #     失败路径也**必须**把它放回去（以前只有成功路径会重开）。
            with tempfile.TemporaryDirectory() as td:
                desk = Path(td)
                info = desk / "workbuddy-desktop.info"
                info.write_text(original, encoding="utf-8")
                info.with_suffix(".info.tmp").mkdir()      # 让写盘必失败
                ch_wb.redirect(desk)
                _oc = []
                _real_close, _real_open = wb.close_client, wb.open_client
                # 假装「客户端本来在跑、我们把它关掉了」
                wb.close_client = lambda c, **kw: (True, "已关闭客户端", True)
                wb.open_client = lambda c, **kw: (_oc.append(1), (True, "已重新打开客户端"))[1]
                try:
                    ok_p13, msg_p13, _mig_p13 = wb.switch_account(
                        target_name, migrate={"enabled": True, "close_client": True,
                                              "reopen_client": True})
                finally:
                    wb.close_client, wb.open_client = _real_close, _real_open
                    ch_wb.redirect(orig_dir, orig_name)
                check("P1-3 切号失败后客户端仍被放回去（不是只写在成功路径上）",
                      ok_p13 is False and len(_oc) == 1, (ok_p13, len(_oc), msg_p13[:70]))
        finally:
            ch_wb.redirect(orig_dir, orig_name)

    print("\n== 5c. 国际服（WorkBuddyAI）写路径：切号 / 增删 / 两通道互不误伤 ==")
    # 国际服是本次新增的通道。读路径（/api/wbai/*）已有覆盖，但**写路径**此前一条用例
    # 都没有 —— 而它恰恰是唯一会动 workbuddy-desktop-ai.info 的代码，也是本次新增配置
    # （info_name / backup_glob / account_prefix / lock_key）最该被钉住的地方。
    # 全程在临时目录：账号库走 _BIN_DIR 派生，桌面端走 redirect。
    ch_wbai = wb.CHANNELS["wbai"]
    usable_ai = [a for a in wb.list_accounts() if a.get("ok")]
    if not usable_ai:
        check("[wbai] 无可用账号素材，跳过写路径用例", True, "wb_auth 下没有可解析的 .info")
    else:
        good_ai = Path(usable_ai[0]["path"]).read_text(encoding="utf-8")
        target_ai = Path(usable_ai[0]["path"]).name
        target_ai_uid = usable_ai[0]["uid"]
        o_dir_ai, o_name_ai = ch_wbai.desktop_dir, ch_wbai.info_name
        o_bin_ai = wb._BIN_DIR
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            desk = root / "desk"
            desk.mkdir()
            wb._BIN_DIR = root                      # 账号库跟着基准目录走
            ch_wbai.redirect(desk)
            try:
                auth = wb.channel("wbai").auth_dir
                check("[wbai] 账号库指向临时目录（随 _BIN_DIR 派生）",
                      auth == root / "wbai_auth", str(auth))

                # --- 1) 新增账号：文件名前缀必须是 workbuddyai-，不能是国服的 workbuddy- ---
                ok_add, msg_add = wb.add_account("__ai", good_ai, "wbai")
                added = sorted(p.name for p in auth.glob("*.info"))
                check("[wbai] add 落盘用 workbuddyai- 前缀（不是 workbuddy-）",
                      ok_add and added == ["workbuddyai-__ai.info"], "%s %s" % (ok_add, added))

                # --- 2) 切号：正式文件必须是 workbuddy-desktop-ai.info ---
                # 同一个临时桌面目录里**同时**放国服的正式文件：切国际服绝不能碰它。
                wb_info = desk / "workbuddy-desktop.info"
                wb_info.write_text('{"account": {"uid": "wb-keep", "nickname": "国服原账号"}, "auth": {}}',
                                   encoding="utf-8")
                wb_orig = wb_info.read_text(encoding="utf-8")
                ai_info = desk / "workbuddy-desktop-ai.info"
                ai_orig = '{"account": {"uid": "ai-orig", "nickname": "国际服原账号"}, "auth": {}}'
                ai_info.write_text(ai_orig, encoding="utf-8")

                ok_sw, msg_sw, _mig_ai = wb.switch_account("workbuddyai-__ai.info", ch="wbai")
                acc_ai = wb.wb.session_from_info_file(ai_info) if ai_info.is_file() else None
                baks_ai = sorted(p.name for p in desk.glob("workbuddy-desktop-ai.*.info"))
                check("[wbai] 切号成功（临时目录）", ok_sw is True, msg_sw[:80])
                check("[wbai] 正式文件已变成目标账号",
                      bool(acc_ai) and acc_ai["uid"] == target_ai_uid,
                      acc_ai and acc_ai["uid"])
                check("[wbai] 备份前缀是 workbuddy-desktop-ai.*",
                      len(baks_ai) == 1, baks_ai)
                check("[wbai] 备份内容是切换前的国际服登录态",
                      baks_ai and (desk / baks_ai[0]).read_text(encoding="utf-8") == ai_orig, baks_ai)
                # 关键：国服的正式文件与备份一个都不能被国际服切号碰到
                check("[wbai] 切国际服不动国服的正式文件",
                      wb_info.is_file() and wb_info.read_text(encoding="utf-8") == wb_orig, "")
                check("[wbai] 切国际服不为国服产生备份",
                      [p.name for p in desk.glob("workbuddy-desktop.*.info")] == [], 
                      [p.name for p in desk.glob("*.info")])

                # --- 3) 不能拿国服的文件当国际服目标（跨通道误切）---
                ok_cross, msg_cross, _ = wb.switch_account(target_ai, ch="wbai")
                check("[wbai] 拒绝切到只在国服账号库里存在的文件",
                      ok_cross is False, msg_cross[:80])

                # --- 3b) 无迁移能力的通道上给了 migrate：必须**说出来**，不能静默丢掉 ---
                # 以前直接 `migrate = None`，返回 ok=true + migration=null，
                # 用户会以为数据也迁过去了（实际只切了号）。
                #
                # ⚠️ 2026-09-21：国际服**已开放**迁移（A 项），所以这里必须先把能力
                # 临时关掉再测 —— 否则这条用例会走真流程：关掉你正在跑的
                # WorkBuddyAI.exe（最多等 33 秒）并去扫真实数据目录。
                # 能力开关是"这条路径走不走"的唯一闸门，只能在这里扳。
                _ch_ai = wb.channel("wbai")
                _saved_mig = _ch_ai.features["migrate"]
                _ch_ai.features["migrate"] = False
                try:
                    ok_sw2, msg_sw2, mig2 = wb.switch_account(
                        "workbuddyai-__ai.info", {"enabled": True}, "wbai")
                finally:
                    _ch_ai.features["migrate"] = _saved_mig
                check("[wbai] 带 migrate 切号时明说「仅切换」（不静默丢弃）",
                      ok_sw2 is True and mig2 is None
                      and "不支持本地数据迁移，本次仅切换" in msg_sw2, msg_sw2[:120])
                check("[wbai] 3b 用完后迁移能力已还原",
                      wb.channel("wbai").features["migrate"] == _saved_mig, "")

                # --- 4) 删除闭环 ---
                ok_rm, msg_rm = wb.remove_account("workbuddyai-__ai.info", "wbai")
                check("[wbai] remove 生效",
                      ok_rm and not (auth / "workbuddyai-__ai.info").exists(), msg_rm[:80])
            finally:
                wb._BIN_DIR = o_bin_ai
                ch_wbai.redirect(o_dir_ai, o_name_ai)
        check("[wbai] 收尾后账号库回到源码目录",
              wb.channel("wbai").auth_dir == o_bin_ai / "wbai_auth",
              str(wb.channel("wbai").auth_dir))

    print("\n== 5d. 数据目录基准：tw 侧改成派生值 + rebind() 一次改全 ==")
    # tw 侧此前是半新半旧：`TW_AUTH_DIR` / `LOCK_DIR` 是导入时快照，
    # 而 `tw_backups` 是调用时现算 —— 同一个文件里两种写法，漏改一个就是
    # 「账号库在一个目录、备份在另一个目录」的静默分家。
    # 现在三个目录都是函数（按 `_BIN_DIR` 现算），启动器只调 `rebind()`。
    for _dead in ("TW_AUTH_DIR", "LOCK_DIR"):
        check("tw_ui_server 不再暴露模块级路径常量 %s" % _dead,
              not hasattr(tw, _dead), getattr(tw, _dead, None))
    for _fn in ("auth_dir", "lock_dir", "backup_dir", "rebind"):
        check("tw_ui_server 提供 %s()" % _fn, callable(getattr(tw, _fn, None)), "")
    _twapp_src = (BIN / "ui_app.py").read_text(encoding="utf-8")
    check("ui_app 只走 srv.rebind()，不逐个赋路径（trae 产品同样只有一个改写入口）",
          "srv.rebind(" in _twapp_src and not assigns_paths(_twapp_src), "")
    # 行为验证：rebind 到临时目录后，**每一个**入口都得跟着走
    with tempfile.TemporaryDirectory() as _td5d:
        _root5d = Path(_td5d)
        _save_tw = (tw._BIN_DIR, tw.Handler.BASE_DIR, tw.Handler.AUDIT_DIR)
        try:
            tw.rebind(_root5d)
            check("tw rebind 后账号库/锁/备份都指向新基准",
                  tw.auth_dir() == _root5d / "tw_auth"
                  and tw.lock_dir() == _root5d / ".locks"
                  and tw.backup_dir() == _root5d / "tw_backups", str(tw.auth_dir()))
            check("tw rebind 后页面目录与审计目录也指向新基准",
                  tw.Handler.BASE_DIR == _root5d and tw.Handler.AUDIT_DIR == _root5d / "logs",
                  str(tw.Handler.AUDIT_DIR))
        finally:
            tw._BIN_DIR, tw.Handler.BASE_DIR, tw.Handler.AUDIT_DIR = _save_tw
        check("tw 还原后账号库回到源码目录",
              tw.auth_dir() == _save_tw[0] / "tw_auth", str(tw.auth_dir()))
    # wb 侧同样验一遍 —— `rebind` 是 ui_app 唯一的改写入口
    with tempfile.TemporaryDirectory() as _td5d:
        _root5d = Path(_td5d)
        _save_wb = (wb._BIN_DIR, wb.SCRIPT_DIR, wb.LOCK_DIR,
                    wb.Handler.BASE_DIR, wb.Handler.AUDIT_DIR)
        try:
            wb.rebind(_root5d, _root5d / "script")
            check("wb rebind 后基准/锁/页面/审计全指向新目录",
                  wb._BIN_DIR == _root5d and wb.SCRIPT_DIR == _root5d / "script"
                  and wb.LOCK_DIR == _root5d / ".locks"
                  and wb.Handler.BASE_DIR == _root5d
                  and wb.Handler.AUDIT_DIR == _root5d / "logs", str(wb.LOCK_DIR))
            check("wb rebind 后两个通道账号库跟着走",
                  wb.channel("wb").auth_dir == _root5d / "wb_auth"
                  and wb.channel("wbai").auth_dir == _root5d / "wbai_auth", "")
        finally:
            (wb._BIN_DIR, wb.SCRIPT_DIR, wb.LOCK_DIR,
             wb.Handler.BASE_DIR, wb.Handler.AUDIT_DIR) = _save_wb

    print("\n== 5e. Trae 侧写路径：切号 / 备份 / 删除（全程临时目录）==")
    # `tw.switch_account` 是本工具里**唯一**会重写客户端登录态的代码，
    # 而它此前一条自动化用例都没有（wb 侧有 §5b/§5c，tw 侧一直空着）——
    # 上面那个"局部名遮蔽模块函数"的 UnboundLocalError 就落在这条路径上，
    # 没有用例的话只能等用户切号时才发现。
    # 全程重定向：账号库走 rebind()，客户端 storage.json 走 _appdata_dir()，
    # 用的是**伪造的**本机登录态，真实 Trae 登录态一个字节都不碰。
    _tw_usable = [a for a in tw.list_accounts() if a.get("ok")]
    if not _tw_usable:
        check("[tw] 无可用素材，跳过写路径用例", True, "tw_auth 下没有可切换的 storage.json")
    else:
        _tw_target = _tw_usable[0]["file"]
        _tw_asset = (BIN / "tw_auth" / _tw_target).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as _td5e:
            _r5e = Path(_td5e)
            _save5e = (tw._BIN_DIR, tw.Handler.BASE_DIR, tw.Handler.AUDIT_DIR, tw._appdata_dir)
            try:
                tw.rebind(_r5e)
                tw.auth_dir().mkdir(parents=True, exist_ok=True)
                (tw.auth_dir() / _tw_target).write_text(_tw_asset, encoding="utf-8")
                # 伪造本机登录态：不含任何真实凭据，只求结构能过解析
                _cand = tw.tw.CANDIDATE_DIRS[0]
                _sp = _r5e / "appdata" / _cand / "User" / "globalStorage" / "storage.json"
                _sp.parent.mkdir(parents=True, exist_ok=True)
                _fake = {tw.AUTH_KEY: "FAKE-ENC-NOT-A-REAL-TOKEN", "editor.fontSize": 14}
                _sp.write_text(json.dumps(_fake), encoding="utf-8")
                tw._appdata_dir = lambda: str(_r5e / "appdata")

                check("[tw] 本机登录态已重定向到临时目录（真实 Trae 登录态不受影响）",
                      tw._desktop_storage() == _sp, str(tw._desktop_storage()))
                ok_sw, msg_sw = tw.switch_account(_tw_target)
                after = json.loads(_sp.read_text(encoding="utf-8"))
                baks = sorted(p.name for p in tw.backup_dir().glob("storage.*.json"))
                check("[tw] 切号成功（临时目录）", ok_sw is True, msg_sw[:90])
                check("[tw] 备份落在 tw_backups，内容 = 切换前的本机登录态",
                      len(baks) == 1 and json.loads(
                          (tw.backup_dir() / baks[0]).read_text(encoding="utf-8")) == _fake,
                      baks)
                check("[tw] 正式文件已换成目标账号密文，且保留本机其它键",
                      bool(after.get(tw.AUTH_KEY)) and after.get(tw.AUTH_KEY) != _fake[tw.AUTH_KEY]
                      and after.get("editor.fontSize") == 14,
                      str(after.get(tw.AUTH_KEY))[:40])
                check("[tw] remove 闭环",
                      tw.remove_account(_tw_target)[0]
                      and not (tw.auth_dir() / _tw_target).exists(), "")
            finally:
                (tw._BIN_DIR, tw.Handler.BASE_DIR, tw.Handler.AUDIT_DIR, tw._appdata_dir) = _save5e

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

        def boom(*a, **k):
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
    # ⚠️ 这里曾经有一句函数内 `import ast`。Python 里函数内的 import 会让 `ast`
    # 在整个 main() 里变成**局部变量**，于是前面 §5b 里用到的 `ast.Module(...)`
    # 直接 UnboundLocalError —— 报错点在 400 行开外，根因却在 710 行。
    # ast 已在模块顶部导入，不要在这里再写一次。
    # ⚠️ 合并后只剩一个 spec —— 它必须同时覆盖**两个产品**的动态导入依赖
    #    （`--product trae` 靠 importlib 动态导入 tw_ui_server / trae_work_checkin，
    #      静态分析扫不到，漏登记就是打包后 Trae 侧起不来）。
    spec_name = "AgentAccountSwitcher.spec"
    spec = BIN / spec_name
    text = spec.read_text(encoding="utf-8") if spec.is_file() else ""
    check("%s 存在（Trae 侧已合并进来，不再有独立 spec）" % spec_name, bool(text), spec)
    for dep in ("switcher_common", "workbuddy_checkin", "account_migration",
                "wb_ui_server", "tw_ui_server", "trae_work_checkin"):
        check("%s 的 hiddenimports 含 %s" % (spec_name, dep),
              "'%s'" % dep in text, text[:0])
    check("%s 用 SPEC 变量做相对定位" % spec_name, "SPEC" in text, text[:0])
    check("TraeSwitcher.spec 已删除（构建配置合并为一个）",
          not (BIN / "TraeSwitcher.spec").is_file(), "")
    # 「是否写死绝对路径」只看代码：注释和模块 docstring 里提到旧路径是允许的
    # （否则说明性注释反而会把断言逼成假阳性）。
    try:
        code = text.replace(ast.get_docstring(ast.parse(text)) or "", "")
    except SyntaxError:
        code = text
    code = "\n".join(ln.split("#", 1)[0] for ln in code.splitlines())
    check("%s 代码中未写死 D:/AI项目 绝对路径" % spec_name,
          "D:/AI项目" not in code and "D:\\AI项目" not in code, text[:0])
    # 部署后自检脚本要跟着 exe 一起发，且本身得能编译 ——
    # 它平时不参与运行，写错了只有在用户手上才会暴露。
    check("AgentAccountSwitcher.spec 带上部署自检脚本 check_exe_datadir.py",
          "check_exe_datadir.py" in (BIN / "AgentAccountSwitcher.spec").read_text(encoding="utf-8"),
          "")
    check("check_exe_datadir.py 语法正确",
          bool(ast.parse((BIN / "check_exe_datadir.py").read_text(encoding="utf-8"))), "")

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
    finally:
        os.environ.pop("LOCALAPPDATA", None)
        if saved_local is not None:
            os.environ["LOCALAPPDATA"] = saved_local
    check("LOCALAPPDATA 为空串时回退到 ~/AppData/Local",
          bool(got_local) and "AppData" in got_local and "Local" in got_local, repr(got_local))
    # 断言**真实常量**，而不是把表达式重写一遍 —— 后者在常量本身写错时照样通过，
    # 等于测了个副本。（该常量在 import 时算好，改环境变量影响不到它，
    # 所以"环境变量为空"的行为由上一条直接测函数来覆盖。）
    check("DESKTOP_DIR_BASE 是绝对路径且指向 CodeBuddyExtension（不会落到当前目录）",
          wb.DESKTOP_DIR_BASE.is_absolute()
          and "CodeBuddyExtension" in str(wb.DESKTOP_DIR_BASE),
          str(wb.DESKTOP_DIR_BASE))
    # 通道的桌面端目录必须是**派生**的，不能是另一份独立快照
    check("通道 desktop_dir 派生自 DESKTOP_DIR_BASE",
          wb.CHANNELS["wb"].desktop_dir == wb.DESKTOP_DIR_BASE,
          str(wb.CHANNELS["wb"].desktop_dir))

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
    for label, ctx in (("[wb]", wb.Handler.UI_CONTEXT), ("[tw]", tw.Handler.UI_CONTEXT),
                       ("[wbai]", wb.Handler.PAGES["/wbai"][1])):
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
        orig_dir, orig_name = ch_wb.desktop_dir, ch_wb.info_name
        real_prune = common.prune_backups

        def _boom_prune(*a, **k):
            raise SystemExit(1)

        try:
            with tempfile.TemporaryDirectory() as td:
                desk = Path(td)
                info = desk / "workbuddy-desktop.info"
                info.write_text('{"account": {"uid": "orig", "nickname": "原账号"}, "auth": {}}',
                                encoding="utf-8")
                ch_wb.redirect(desk)
                common.prune_backups = _boom_prune
                try:
                    ok, msg, _mig = wb.switch_account(Path(usable2[0]["path"]).name)
                finally:
                    common.prune_backups = real_prune
                check("裁剪备份抛 SystemExit 时切号仍然成功", ok is True, msg[:80])
                check("正式登录态文件存在（不会因裁剪失败而丢失）", info.is_file(), str(info))
        finally:
            ch_wb.redirect(orig_dir, orig_name)

    # current_account：必须只认本工具接管的那一份
    orig_dir, orig_name = ch_wb.desktop_dir, ch_wb.info_name
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
            ch_wb.redirect(desk)
            names = [e["file"] for e in wb.current_account()]
            check("当前账号只认 workbuddy-desktop*，不认 workbuddy-desktop-ai*",
                  names == ["workbuddy-desktop.info"], names)
            # 同一份素材放进国际服通道，结论必须反过来 —— 两个通道各看各的文件
            ch_wbai = wb.CHANNELS["wbai"]
            o_dir2, o_name2 = ch_wbai.desktop_dir, ch_wbai.info_name
            try:
                ch_wbai.redirect(desk)
                names_ai = [e["file"] for e in wb.current_account("wbai")]
                check("国际服通道只认 workbuddy-desktop-ai*",
                      names_ai == ["workbuddy-desktop-ai.info"], names_ai)
            finally:
                ch_wbai.redirect(o_dir2, o_name2)
    finally:
        ch_wb.redirect(orig_dir, orig_name)

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
    ai_root = mig.find_data_root("66666666-6666-4666-8666-666666666666")
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
        st, raw = call(port3, "/wb")     # 迁移弹框属于国服视图，入口页里没有
        html = raw.decode("utf-8", "replace")
        check("[wb] 迁移弹框已注入", 'id="migModal"' in html and 'migrate: "1"' in html, st)
        check("[wb] 迁移预览接口已接上", "/api/migrate-preview" in html, "")
        check("[wb] 「仅切换」出口保留", "migConfirm(false)" in html, "")
        # 行为变了：客户端在跑**不再**禁用按钮 —— 后端会在迁移前自动关掉它。
        # 弹框改为显示一条说明（migClientNote），按钮始终可点。
        check("[wb] 客户端在跑时不再禁用「切换并迁移」",
              "go.disabled = false" in html and "go.disabled = blocked" not in html, "")
        check("[wb] 弹框有「会自动关闭客户端」的说明块",
              'id="migClientNote"' in html and "pv.client_note" in html, "")
    finally:
        srv3.shutdown()
        srv3.server_close()
    tpl = (BIN / "ui_template.html").read_text(encoding="utf-8")
    for label, ctx in (("[wb]", wb.Handler.UI_CONTEXT), ("[tw]", tw.Handler.UI_CONTEXT),
                       ("[wbai]", wb.Handler.PAGES["/wbai"][1])):
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
    # 合并启动器后只有一份，两个产品共用同一段复用/启动逻辑
    s = (BIN / "ui_app.py").read_text(encoding="utf-8")
    # 只看复用分支：正常启动路径当然还是要开窗口的
    i = s.find('if action == "reuse":')
    j = s.find('if action == "abort":', i + 1)
    blk = s[i:j] if (i >= 0 and j > i) else ""
    check("ui_app 复用分支改为弹提示、不再开新窗口",
          bool(blk) and "notify_user" in blk and "_show_window" not in blk, blk[:70])
    check("ui_app 正常启动路径仍会开窗口", "_show_window(url," in s, "")
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
    # 白字 + 红底。底色必须是**深红**：--red(#ff5c5c) 上放白字对比度只有 2.99:1，
    # 低于 WCAG AA 的 4.5:1，用户会看不清。
    check("danger 是白字红底",
          ".btn.danger { background:var(--red-deep); border-color:var(--red-deep); color:#fff; }" in tpl17, "")
    check("danger 的底色用主题变量 --red-deep（不是硬编码）",
          "--red-deep:" in tpl17 and ".btn.danger:hover { background:#b71c1c;" in tpl17, "")
    def _lum(hexs):
        hexs = hexs.lstrip("#")
        ch = [int(hexs[i:i+2], 16) / 255 for i in (0, 2, 4)]
        ch = [(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4) for c in ch]
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
    _c = 1.05 / (_lum("#c62828") + 0.05)
    check("白字在红底上的对比度达到 WCAG AA（>=4.5:1）", _c >= 4.5, "%.2f:1" % _c)
    check("对比度：旧底色 --red 反而不达标（说明为什么换深红）",
          1.05 / (_lum("#ff5c5c") + 0.05) < 4.5, "%.2f:1" % (1.05 / (_lum("#ff5c5c") + 0.05)))

    # --- 17c. 列表更窄更矮 ---
    for needle, why in ((".wrap { max-width: 860px;", "整体更窄且贴合行内容"),
                        (".list { display:flex; flex-direction:column; gap:7px; }", "行间距更小"),
                        # 248 = 最宽的「昵称 + [当前] + [长期 55天]」约 198px
                        # + 头像 32 + 间距 9 + 9 余量。定小了名字会折成两行。
                        (".who { flex:0 0 248px;", "身份列容得下名字+两个标签一行"),
                        (".acts { flex:0 0 110px;", "操作列更窄"),
                        (".ci-bar { flex:1 1 auto; min-width:60px; height:4px;", "进度条更细且吃满整行")):
        check("紧凑化：%s（%s）" % (needle.split("{")[0].strip(), why), needle in tpl17, "")
    # --- 17c2. 身份列不再"串行"：meta 与 exp 各占独立位置 ---
    check("meta 不再重复 [当前] 标签已说明的「桌面端正在使用」",
          "' · 桌面端正在使用'" not in tpl17, "")
    check("exp 只留总剩余积分（登录态剩余已删）",
          ".exp-total { white-space:nowrap; }" in tpl17
          and "totalRemainHtml(o.file)" in tpl17 and "exp-ttl" not in tpl17, "")
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
          "max-width:240px" not in tpl17
          and ".ci-line { display:flex; align-items:center; position:relative; min-height:18px;" in tpl17, "")
    check("积分块把「档位名/时间/用量」压到同一行（省两行高度）",
          'h+=\'<div class="ci">\'\n      +\'<div class="ci-info">\'\n      +\'<span class="ci-name">\'' in tpl17, "")

    # --- 17d. 加载：三件事并发 + 启动预热 ---
    check("首屏三个请求并发发起（不再等 load 回来才拉积分/签到）",
          "load();\nloadCredits();\nloadCheckin();" in tpl17, "")
    check("服务端启动时预热积分/签到缓存",
          hasattr(wb, "warmup_async") and "warmup_async()" in (BIN / "wb_ui_server.py").read_text(encoding="utf-8"), "")
    # 缓存按通道分区，预热也必须逐通道 —— 只热国服的话国际服首屏仍要现查 ~0.7s。
    # 断言"按通道遍历 + 按能力过滤"，别扫全文中文（会命中注释）。
    _warm_src = (BIN / "wb_ui_server.py").read_text(encoding="utf-8")
    check("预热覆盖每个通道（按 supports 过滤能力）",
          "for key in CHANNELS:" in _warm_src
          and 'jobs.append(("%s·积分" % key' in _warm_src
          and '_ch.supports("credits")' in _warm_src, "")

    # --- 17e. 打开客户端 ---
    # 客户端进程名/exe 名已改为**按通道**取（国服 WorkBuddy.exe / 国际服 WorkBuddyAI.exe），
    # 这几个接口都要求显式传 ch —— 早先的模块级单值常量会让国服去关国际服的客户端。
    _CH_WB = wb.channel("wb")
    _CH_WBAI = wb.channel("wbai")
    exe = wb.find_client_exe(_CH_WB)
    check("能定位到 WorkBuddy 客户端可执行文件", exe is None or exe.is_file(), str(exe))
    cs = wb.client_status(_CH_WB)
    check("client_status 返回可 JSON 序列化的 dict（Path 会让响应 500）",
          isinstance(cs, dict) and isinstance(cs.get("pids"), list)
          and isinstance(cs.get("exe"), str) and isinstance(cs.get("running"), bool), cs)
    check("当前用户有客户端在运行时应能检出", cs["running"] is True, cs["pids"])
    before = len(cs["pids"])
    # allow_restart=False：自检不去重启用户正在用的客户端（窗口收在托盘时的重启
    # 是给用户点按钮用的，见 ⑤f）；这里只验证"客户端在跑时不重复启动"这条路径。
    ok, msg = wb.open_client(_CH_WB, allow_restart=False)
    pids2 = wb.client_status(_CH_WB)["pids"]
    check("客户端已在运行时 open_client 不会重复启动", ok is True and len(pids2) <= before,
          "%d -> %d" % (before, len(pids2)))
    # open_client 有三条分支：①已切到前台 ②窗口收在托盘里→已显示出来
    # ③枚举不到主窗口→提示从任务栏/托盘点开。三条都是"可操作"的措辞，
    # 断言只认前两条的话，客户端窗口状态一变（收托盘 / 枚举不到）就会假失败。
    check("open_client 的返回消息可操作（说明是切前台还是已启动）",
          ("前台" in msg) or ("已启动" in msg) or ("显示出来" in msg)
          or ("点开" in msg), msg[:80])

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

    print("\n== 18. 迁移前自动关闭客户端 ==")
    # ⚠️ 全程用 mock：真实环境下 WorkBuddyAI.exe 就是托管本会话的客户端，
    # 真去关它会直接把会话干掉。所以这里只替换 common 的进程接口来验证逻辑。
    src_wb = (BIN / "wb_ui_server.py").read_text(encoding="utf-8")
    check("switch_account 有「迁移前先关客户端」这一步（且按通道关）",
          "close_client(ch)" in src_wb and "client_was_running" in src_wb, "")
    check("关闭失败时整体中止（连切号也不做）",
          'return False, "迁移前无法关闭%s客户端：%s" % (ch.server, msg_c), None' in src_wb, "")
    # ⚠️ P1-3（AUDIT_2026-09-24.md）：以前重开客户端的代码只写在 switch_account
    #    末尾的**成功路径**上，注释却自称「无论迁移成败都恢复」—— 那 4 条失败早退
    #    路径（轮换失败 / 写入失败 / 写后校验失败 / migrate 抛异常）全都走不到它。
    #    现在恢复逻辑提到 `_restore_client()`，由 `finally` 兜底。
    #    这里只做源码结构检查，**真正的行为验证在 4b 段**（用临时目录 + 桩真跑）。
    _sw_src = src_wb.split("\ndef switch_account")[1].split("\ndef ")[0]
    check("流程结束后按原状恢复客户端（无论迁移成败）",
          "def _restore_client()" in _sw_src and _sw_src.count("_restore_client()") >= 2, "")
    check("客户端恢复挂在 finally 分支上（失败/异常路径也走到）",
          "finally:" in _sw_src and "if not switched_ok:" in _sw_src, "")

    real_lp = common.list_processes
    real_cw = common.close_windows_of
    real_tp = common.terminate_processes
    try:
        # 1) 本来就没在跑
        common.list_processes = lambda: {"explorer.exe": [1]}
        ok, msg, was = wb.close_client(_CH_WB, graceful_wait=0.2, total_wait=0.5)
        check("close_client：客户端没在跑 → 成功且 was_running=False",
              ok is True and was is False, msg)

        # 2) 枚举不出来 ≠ 已经关掉了
        common.list_processes = lambda: None
        ok, msg, was = wb.close_client(_CH_WB, graceful_wait=0.2, total_wait=0.5)
        check("close_client：枚举失败时如实报错（不当成已关闭）",
              ok is False and was is False, msg[:70])

        # 3) 一直关不掉 → 先礼后兵，最后如实报失败并给出进程号
        calls = []
        common.list_processes = lambda: {_CH_WB.client_processes[0]: [12345]}
        common.close_windows_of = lambda pids: (calls.append(("close", list(pids))), 1)[1]
        common.terminate_processes = lambda pids: (calls.append(("kill", list(pids))), len(pids))[1]
        ok, msg, was = wb.close_client(_CH_WB, graceful_wait=0.4, total_wait=0.9)
        check("close_client：关不掉时如实报失败（含进程号）",
              ok is False and was is True and "12345" in msg, msg[:80])
        check("close_client：先发 WM_CLOSE 再强杀（先礼后兵）",
              [c[0] for c in calls] == ["close", "kill"], calls)

        # 4) 关得掉 → 成功
        state = {"n": 0}

        def _vanish():
            state["n"] += 1
            return {_CH_WB.client_processes[0]: [999]} if state["n"] <= 1 else {}

        common.list_processes = _vanish
        common.close_windows_of = lambda pids: 1
        common.terminate_processes = lambda pids: 0
        ok, msg, was = wb.close_client(_CH_WB, graceful_wait=1.2, total_wait=2.0)
        check("close_client：进程消失后返回成功", ok is True and was is True, msg)
    finally:
        common.list_processes = real_lp
        common.close_windows_of = real_cw
        common.terminate_processes = real_tp

    # 5) 预览把「会自动关闭」作为说明而不是拦阻
    with tempfile.TemporaryDirectory() as td:
        mig.ROOT_OVERRIDE = Path(td)
        try:
            pv = mig.scan("aaaaaaaa-0000-0000-0000-000000000001",
                          "bbbbbbbb-0000-0000-0000-000000000002")
            check("scan 返回 client_note 字段（供弹框说明用）", "client_note" in pv, sorted(pv)[:8])
            check("客户端在跑时给出「会自动关闭」的说明，而不是塞进 warnings",
                  (pv["client_note"] and "自动关闭" in pv["client_note"])
                  or pv["client_running"] is None,
                  (pv.get("client_note") or "")[:70])
        finally:
            mig.ROOT_OVERRIDE = None

    print("\n== 19. 审计修复回归（AUDIT_2026-09-19.md）==")
    src_wb19 = (BIN / "wb_ui_server.py").read_text(encoding="utf-8")
    src_cm19 = (BIN / "switcher_common.py").read_text(encoding="utf-8")

    # --- BUG A：current_account 的 stat 必须有保护 ---
    check("BUG A：_mtime_with_retry 存在", "def _mtime_with_retry(" in src_wb19, "")
    check("BUG A：current_account 的循环包了 try/except OSError",
          "mtime = _mtime_with_retry(f)" in src_wb19 and "except OSError:" in src_wb19, "")
    # 只看**代码行**：注释里正好写了"不能用 float('inf')"来说明原因，
    # 直接全串匹配会命中注释（这个断言第一版就是这么假失败的）
    _ca19 = [l for l in src_wb19.split("def current_account")[1].split("def ")[0].splitlines()
             if not l.strip().startswith("#")]
    check("BUG A：兜底 mtime 不用 inf（Infinity 不是合法 JSON）",
          not any('float("inf")' in l for l in _ca19), "")
    _real_stat19 = Path.stat

    def _flaky_stat(fail_times):
        st19 = {"n": 0}

        def _f(self, *a, **k):
            if self.name == "workbuddy-desktop.info":
                st19["n"] += 1
                if st19["n"] <= fail_times:
                    raise PermissionError(5, "拒绝访问")
            return _real_stat19(self, *a, **k)
        return _f, st19
    try:
        Path.stat, _st19 = _flaky_stat(999)
        _base19 = wb.current_account()
        check("BUG A：stat 一直失败时 current_account 不抛异常", True, "%d 个条目" % len(_base19))
        check("BUG A：正式文件仍在列表里（不会被旧备份顶掉）",
              any(e["file"] == "workbuddy-desktop.info" for e in _base19), "")
        check("BUG A：current 仍是正式文件",
              [e for e in _base19 if e.get("current")][0]["file"] == "workbuddy-desktop.info", "")
        _json19 = json.dumps(_base19)
        check("BUG A：结果仍是合法 JSON（无 Infinity/NaN）",
              "Infinity" not in _json19 and "NaN" not in _json19, "")
        Path.stat, _st19 = _flaky_stat(2)
        _r19 = wb.current_account()
        check("BUG A：重试生效（前 2 次失败后仍拿到真实 mtime）", _st19["n"] == 3, "尝试 %d 次" % _st19["n"])
    finally:
        Path.stat = _real_stat19

    # --- BUG B：审计动作名走映射 ---
    check("BUG B：令牌校验失败的审计用映射后的动作名",
          "self.AUDIT_ACTIONS.get(u.path, u.path), \"\", False, \"令牌校验失败\"" in src_cm19, "")

    # --- 建议 3：两个快照字段对齐 ---
    _ck19 = wb.checkin_snapshot(False)
    _cr19 = wb.credits_snapshot(False)
    check("建议3：checkin 也有 queried（与 credits 对齐）",
          "queried" in _ck19 and "queried" in _cr19, sorted(set(_ck19) & set(_cr19)))
    check("建议3：checkin.queried = 成功查询数",
          _ck19["queried"] == sum(1 for a in _ck19["accounts"] if a.get("ok")), _ck19["queried"])
    check("建议3：checked（已签到数）语义独立保留",
          "checked" in _ck19 and _ck19["checked"] == sum(1 for a in _ck19["accounts"] if a.get("checked_in")),
          _ck19["checked"])

    # --- 建议 4：改名重试统一到 common ---
    check("建议4：common.replace_with_retry 存在",
          "def replace_with_retry(" in src_cm19, "")
    check("建议4：account_migration._replace 委托给它",
          "return common.replace_with_retry(src, dst" in (BIN / "account_migration.py").read_text(encoding="utf-8"), "")
    # ⚠️ 抽取必须按**列 0 的 def** 切，不能 `split("def ")` —— switch_account 里现在
    #    有一个嵌套的 `def _restore_client()`，按 "def " 切会把函数体截断在那，
    #    于是下面的 `count(...) == 2` 恒为 1，变成假失败。
    _sw19 = src_wb19.split("\ndef switch_account")[1].split("\ndef ")[0]
    check("建议4：switch_account 的 3 处改名都用带重试版本",
          _sw19.count("common.replace_with_retry(") == 2 and "_restore_backup" in src_wb19, "")
    with tempfile.TemporaryDirectory() as _td19:
        _t19 = Path(_td19)
        (_t19 / "a.txt").write_text("x")
        common.replace_with_retry(_t19 / "a.txt", _t19 / "b.txt")
        check("建议4：正常改名可用", (_t19 / "b.txt").is_file(), "")

    # --- 建议 5（已撤销 2026-09-21）：空态文案"exe 同级"提示随 TIP 区块一起删掉 ---
    check("建议5（撤销）：UI_CONTEXT 不再含 EMPTY_HINT 字段",
          "EMPTY_HINT" not in wb.Handler.UI_CONTEXT
          and "EMPTY_HINT" not in tw.Handler.UI_CONTEXT
          and "SUBTITLE" not in wb.Handler.UI_CONTEXT
          and "TIP" not in wb.Handler.UI_CONTEXT, "")

    # --- 建议 6：needed 只有一个计算点 ---
    check("建议6：preview() 自己带 needed",
          's["needed"] = bool(' in (BIN / "account_migration.py").read_text(encoding="utf-8"), "")
    check("建议6：migrate_preview 不再重算 needed",
          'data["needed"] = bool(' not in src_wb19, "")

    # --- 19d. 能力守卫必须在函数里，而不是只在 HTTP 层 ---
    # 迁移扫描的是国服客户端的本地数据目录。守卫一度只写在 api_get 里，
    # 于是 `--channel wbai --migrate-preview` 能绕过它，拿到一份国服数据的预览
    # （root / sessions / automations 全是国服的），看着像"国际服也要迁移"。
    # 守卫写进 migrate_preview() 本身，HTTP 与 CLI 两个入口才只有一个真相来源。
    _mig_src = src_wb19.split("def migrate_preview(")[1].split("\ndef ")[0]
    check("19d：migrate_preview 自带能力守卫（CLI 也挡得住）",
          'supports("migrate")' in _mig_src, "")
    _ai_target = None
    for _a in wb.list_accounts():
        if _a.get("ok"):
            _ai_target = Path(_a["path"]).name
            break
    if _ai_target:
        # ⚠️ 2026-09-21：国际服**已开放**迁移，所以不能再用它验证"能力被拒"这条路径 ——
        # 真跑下去会去扫真实数据目录。这里把能力临时关掉，测的仍是同一段守卫代码。
        _ch_ai19 = wb.channel("wbai")
        _saved19 = _ch_ai19.features["migrate"]
        _ch_ai19.features["migrate"] = False
        try:
            _pv_ai = wb.migrate_preview(_ai_target, "wbai")
        finally:
            _ch_ai19.features["migrate"] = _saved19
        check("[wbai] migrate_preview 拒绝且不泄露国服数据",
              _pv_ai.get("ok") is False and _pv_ai.get("needed") is False
              and "不支持本地数据迁移" in (_pv_ai.get("message") or "")
              and "root" not in _pv_ai and "sessions" not in _pv_ai,
              _pv_ai)
        # 拒绝时还要带 unsupported 标记，与 /api/credits、/api/checkin-status、
        # /api/client-status 同款 —— CLI 靠它区分「通道没这能力」与「这次扫描失败」。
        check("[wbai] migrate_preview 拒绝时带 unsupported 标记",
              _pv_ai.get("unsupported") is True, _pv_ai)
        _pv_wb = wb.migrate_preview(_ai_target, "wb")
        check("[wb] migrate_preview 仍返回真实扫描结果",
              "root" in _pv_wb and "sessions" in _pv_wb, list(_pv_wb)[:6])
        check("[wb] migrate_preview 不带 unsupported 标记",
              "unsupported" not in _pv_wb, list(_pv_wb)[:6])
    else:
        check("[wbai] 无可用账号素材，跳过 migrate_preview 守卫用例", True, "")

    # --- 19e. CLI 的退出码要说真话 ---
    # `--migrate-preview` 曾恒 return 0：能力被拒时 JSON 里写着 ok=false，
    # 但脚本用 `$?` 判断会当成成功。--switch / --refresh-all 都会给非零退出码，
    # 它没有理由例外。
    _main_src = src_wb19.split("def main(")[1]
    check("19e：--migrate-preview 按 unsupported 给退出码",
          'return 1 if out.get("unsupported") else 0' in _main_src, "")
    # `--migrate` 在无迁移能力的通道上被静默丢弃过（只切号，却回 ok=true + migration=null）
    _sw_src = src_wb19.split("def switch_account(")[1].split("\ndef ")[0]
    check("19e：switch_account 记录被丢弃的 migrate 并写进返回消息",
          "migrate_dropped" in _sw_src and "不支持本地数据迁移，本次仅切换" in _sw_src, "")
    # 真跑一遍 CLI（不只扫源码）。
    # ⚠️ 2026-09-21：国际服已开放迁移，"能力被拒 → rc=1" 这条路径**没法再用真 CLI 验**
    #    —— 子进程会重新加载配置，在本进程里临时关掉能力对它无效。
    #    所以这里改成验"新现实"：国际服现在能给真实预览，且扫描的是**国际服自己的**
    #    数据目录（.workbuddy-ai），不串到国服的 .workbuddy —— 正是开放迁移的关键前提。
    #    "被拒 → rc=1" 的语义由上面那条源码断言守住。
    _ai_real = None
    for _a in wb.list_accounts("wbai"):
        if _a.get("ok"):
            _ai_real = Path(_a["path"]).name
            break
    if _ai_real:
        _r_ai = subprocess.run(
            [sys.executable, str(BIN / "wb_ui_server.py"),
             "--channel", "wbai", "--migrate-preview", _ai_real],
            capture_output=True, text=True, cwd=str(BIN))
        _ai_out = json.loads(_r_ai.stdout or "{}")
        check("19e：[CLI] 国际服迁移预览不再被拒（无 unsupported）",
              _r_ai.returncode == 0 and not _ai_out.get("unsupported"),
              (_r_ai.returncode, _r_ai.stdout.strip()[:90]))
        check("19e：[CLI] 国际服预览扫的是 .workbuddy-ai（不是国服的 .workbuddy）",
              ".workbuddy-ai" in str(_ai_out.get("root", "")),
              _ai_out.get("root", ""))

    # --- 19f. 积分/签到快照必须按通道取数，且缓存按通道分区 ---
    # `credits_snapshot` / `checkin_snapshot` / `checkin_account_file` 原来内部写死
    # `list_accounts()` + `channel(DEFAULT_CHANNEL).auth_dir`，和 checkin_all 同一类隐患：
    # 国服是目前唯一有这些能力的通道，所以现网没症状；能力一开放就会静默读错账号库。
    # 更隐蔽的是**缓存**：`_CHECKIN_CACHE` 是模块级的，不分区就会把国服那份结果喂给国际服。
    for _fn in (wb.credits_snapshot, wb.checkin_snapshot, wb.checkin_account_file,
                wb.checkin_all, wb.refresh_all_ui, wb.migrate_preview):
        check("%s 显式收通道 ch" % _fn.__name__,
              "ch" in inspect.signature(_fn).parameters,
              list(inspect.signature(_fn).parameters))
    check("积分/签到缓存按通道 key 分区（不再是扁平单槽）",
          "_CREDITS_CACHE[ch.key]" in src_wb19 and "_CHECKIN_CACHE[ch.key]" in src_wb19
          and '_CREDITS_CACHE = {}' in src_wb19 and '_CHECKIN_CACHE = {}' in src_wb19, "")
    # 往国际服那条缓存里塞一份带标记的假结果：真按通道分区的话，这次调用会
    # **命中缓存直接返回**（不发任何网络请求），并且带回标记。
    _saved_ck = dict(wb._CHECKIN_CACHE)
    try:
        wb._CHECKIN_CACHE["wbai"] = {"ts": time.time(),
                                     "payload": {"ok": True, "accounts": [], "marker": "ai"}}
        _ai_ck = wb.checkin_snapshot(False, "wbai")
        check("[wbai] 签到快照命中自己那条缓存（没串到国服）",
              _ai_ck.get("marker") == "ai" and _ai_ck.get("cached") is True, _ai_ck)
        wb._CHECKIN_CACHE["wb"] = {"ts": 1.0, "payload": {"ok": True, "accounts": []}}
        wb._invalidate_checkin_cache("wbai")
        check("签到缓存失效只清该通道（另一个通道的结果留着）",
              "wbai" not in wb._CHECKIN_CACHE and "wb" in wb._CHECKIN_CACHE,
              sorted(wb._CHECKIN_CACHE))
    finally:
        wb._CHECKIN_CACHE.clear()
        wb._CHECKIN_CACHE.update(_saved_ck)

    # --- 19b. 2026-09-20 的 5 项界面调整 ---
    check("① exp 里不再显示具体到期日期", "fmtDate(o.expires_at)" not in tpl17, "")
    check("② 总剩余积分移到身份列（挂在 exp 那一行）",
          "totalRemainHtml(o.file)" in tpl17 and 'class="exp-total"' in tpl17, "")
    # 用类名而不是中文文本：中文会出现在注释里（这个断言第一版就是这么假失败的）
    check("③ 积分块标题行整行删掉（credits-title / credits-total 全无）",
          "credits-title" not in tpl17 and "credits-total" not in tpl17, "")
    check("④ 已使用/剩余默认隐藏，悬停进度条才显示",
          "opacity:0; transition:opacity .12s" in tpl17
          and ".ci-bar:hover ~ .ci-used { opacity:1; }" in tpl17, "")
    check("④ 浮层绝对定位（不占位 → 进度条吃满整行、不留空档）",
          ".ci-used { position:absolute; right:0;" in tpl17, "")
    check("④ pointer-events:none（否则鼠标移到浮层上会一闪一闪）",
          "pointer-events:none" in tpl17.split(".ci-used {")[1].split("}")[0], "")
    check("④ 进度条命中区上下各扩 7px（4px 的条子太难点中）",
          'content:""; position:absolute; left:0; right:0; top:-7px; bottom:-7px;' in tpl17, "")
    check("④ 去掉 overflow:hidden 后由 <i> 自带圆角（否则填充会露直角）",
          "overflow:hidden" not in tpl17.split(".ci-bar {")[1].split("}")[0]
          and ".ci-bar i { display:block; height:100%; border-radius:2px;" in tpl17, "")
    # 导航栏那个（OPEN_CLIENT）：国服/国际服仍不显示，Trae 显示 —— 用户此前明确要求去掉。
    check("⑤ 导航栏「打开客户端」wb 侧仍置空",
          wb.Handler.UI_CONTEXT.get("OPEN_CLIENT") == "", repr(wb.Handler.UI_CONTEXT.get("OPEN_CLIENT")))
    check("⑤ 导航栏「打开客户端」tw 侧保留",
          tw.Handler.UI_CONTEXT.get("OPEN_CLIENT") == "1", repr(tw.Handler.UI_CONTEXT.get("OPEN_CLIENT")))
    check("⑤ 导航栏按钮按 UI.openClient 隐藏",
          "if(UI.openClient!=='1')" in tpl17 and "openClientBtn" in tpl17, "")
    # 2026-09-21：**当前账号行**那个按钮换成独立开关 OPEN_CLIENT_ROW，两个通道都要显示。
    check("⑤ 当前账号行按钮走独立开关 UI.openClientRow",
          "UI.openClientRow==='1'" in tpl17 and "openClientRow:" in tpl17, "")
    check("⑤ 国服当前行显示「打开客户端」",
          wb.Handler.UI_CONTEXT.get("OPEN_CLIENT_ROW") == "1",
          repr(wb.Handler.UI_CONTEXT.get("OPEN_CLIENT_ROW")))
    check("⑤ 国际服当前行显示「打开客户端」",
          wb.Handler.PAGES["/wbai"][1].get("OPEN_CLIENT_ROW") == "1",
          repr(wb.Handler.PAGES["/wbai"][1].get("OPEN_CLIENT_ROW")))
    check("⑤ Trae 侧当前行也显示（保持原行为）",
          tw.Handler.UI_CONTEXT.get("OPEN_CLIENT_ROW") == "1",
          repr(tw.Handler.UI_CONTEXT.get("OPEN_CLIENT_ROW")))
    check("⑤ doOpenClient 只对可见按钮做反馈（别把「打开中…」写进隐藏的导航按钮）",
          "b.style.display!=='none'" in tpl17, "")
    check("⑤ 没有该按钮时仍保留空的 .acts（三列网格与下面的行对齐）",
          '\'<div class="acts"></div>\'' in tpl17, "")

    # --- 19c. 2026-09-20 第二轮：删登录态剩余 + 布局优化 ---
    check("① 渲染逻辑里不再有「登录态」字样",
          "登录态 ' +" not in tpl17 and ">登录态<" not in tpl17, "")
    check("① 不再渲染剩余天数（原 exp-ttl 已删）",
          "exp-ttl" not in tpl17, "")
    check("① 总剩余积分保留在身份列", 'class="exp-total"' in tpl17, "")
    check("② 快过期信号转到通道标签上（soon 态 + title 带天数）",
          ".tag-ttl.soon { background:rgba(255,92,92,.15); color:var(--red); }" in tpl17
          and "rd<3" in tpl17 and "登录态剩余 " in tpl17, "")
    check("② 通道标签的 55天/30天 仍在（那是通道有效期，不是剩余天数）",
          "长期 " in tpl17 and "短期 " in tpl17, "")
    check("③ 「· 」前缀去掉（只剩一项时不该留分隔符）",
          '>· 总剩余积分 ' not in tpl17, "")

    # --- 19d. 2026-09-21 审计修复的回归钉 ---
    # 这三条都是"只在 exe / 计划任务里暴露、源码态永远测不出来"的坑，
    # 必须钉住，否则改回去时 smoke_test 一片绿、用户那边却静默炸掉。

    # ① GBK 控制台下 CLI 不能崩：windowed exe 的 stdout 走系统 ANSI 代码页，
    #    昵称里的 emoji 会让裸 print(json.dumps(..., ensure_ascii=False)) 抛
    #    UnicodeEncodeError；更糟的是窗口版没有控制台，PyInstaller 会把未捕获
    #    异常弹成 "Unhandled exception in script" 模态框，**进程阻塞等用户点确定**
    #    （计划任务里 = 永久挂起，实测 rc=124 超时）。
    check("① CLI 输出统一走 common.print_json（有 GBK 兜底）",
          hasattr(common, "print_json")
          and "UnicodeEncodeError" in inspect.getsource(common.print_json)
          and "reconfigure" in inspect.getsource(common.print_json), "")
    _bad = "丹怡Helia \U0001f331"      # 本机真实账号昵称，正是当初的触发者
    _buf = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict",
                            newline="", write_through=True)
    try:
        common.print_json({"nickname": _bad}, stream=_buf)
        _gbk_ok = True
    except Exception as _e:            # noqa: BLE001
        _gbk_ok = False
        _gbk_err = _e
    check("① GBK 流下输出 emoji 昵称不抛异常", _gbk_ok,
          "" if _gbk_ok else "%s: %s" % (type(_gbk_err).__name__, _gbk_err))
    check("① wb 侧 main() 不再裸 print(json.dumps(...))",
          not re.search(r"print\(json\.dumps\(", inspect.getsource(wb.main)), "")

    # ② Trae 侧同样要把一次性 CLI 动作转交后端（合并启动器后由同一个 ui_app 负责）。
    #    以前 Trae 的启动器没有这套转发，`--list` 会开窗口并一直阻塞
    #    （日志里冒 CEF 的 Chrome_WidgetWin_0 报错），与 WorkBuddy 侧行为不一致。
    _tw_app = (Path(__file__).resolve().parent / "ui_app.py").read_text(encoding="utf-8")
    check("② ui_app 把一次性 CLI 动作转交后端（不开窗口）",
          "_wants_server_cli(argv," in _tw_app and "return srv.main()" in _tw_app, "")
    check("② ui_app 的 trae 产品 cli_actions 覆盖 --list/--current/--switch/--refresh-all",
          all(v in _tw_app.split('"trae": {')[1].split("},")[0]
              for v in ("--list", "--current", "--switch", "--refresh-all")), "")

    # ③ 迁移「校验」步骤不能因正文 jsonl 缺失而判失败：
    #    那样 ok=False 但**不回滚**（数据其实已迁移），用户看到"迁移失败"，
    #    且 `if result["ok"]: prune` 被绕过 → 每轮新增一份完整对话库快照（实测 14 份）。
    _am = __import__("account_migration")
    _mig_src = inspect.getsource(_am.migrate)
    check("③ 迁移校验步骤恒定判成功（正文缺失只进 warnings）",
          'add_step("校验", True,' in _mig_src, "")
    check("③ 迁移备份按「未回滚」裁剪，而不是按 ok",
          "if not result.get(\"rolled_back\"):" in _mig_src
          and "if result[\"ok\"]:" not in _mig_src, "")

    # ④ 部署：账号目录由代码创建（dist 每次重建都会丢，不能靠打包带上）
    check("④ wb 侧有 ensure_auth_dirs 且 serve 时调用",
          hasattr(wb, "ensure_auth_dirs")
          and "ensure_auth_dirs()" in inspect.getsource(wb.serve), "")
    check("④ tw 侧有 ensure_auth_dir 且 serve 时调用",
          hasattr(tw, "ensure_auth_dir")
          and "ensure_auth_dir()" in inspect.getsource(tw.serve), "")

    # ⑤ 客户端进程名必须**按通道**取（2026-09-21 A+B）
    #    本机两个客户端都装着、都在跑：
    #      国服  C:\Program Files\WorkBuddy\WorkBuddy.exe      (workbuddy.exe)
    #      国际服 C:\Program Files\WorkBuddyAI\WorkBuddyAI.exe  (workbuddyai.exe)
    #    早先是模块级单值 `CLIENT_PROCESS = "workbuddyai.exe"`，于是国服的迁移流程
    #    会去关/开**国际服**客户端，而国服客户端全程没人管、迁移时仍持有自己的
    #    workbuddy.db —— "看起来成功、实际动错程序"，必须钉死。
    _ch_wb, _ch_wbai = wb.channel("wb"), wb.channel("wbai")
    check("⑤ 模块级 CLIENT_PROCESS / CLIENT_EXE_NAME 已删除",
          not hasattr(wb, "CLIENT_PROCESS") and not hasattr(wb, "CLIENT_EXE_NAME"), "")
    check("⑤ 国服客户端进程名是 workbuddy.exe（不是 workbuddyai.exe）",
          "workbuddy.exe" in _ch_wb.client_processes
          and "workbuddyai.exe" not in _ch_wb.client_processes, _ch_wb.client_processes)
    check("⑤ 国际服客户端进程名是 workbuddyai.exe",
          "workbuddyai.exe" in _ch_wbai.client_processes, _ch_wbai.client_processes)
    check("⑤ 两个通道的 exe 名不同（WorkBuddy.exe / WorkBuddyAI.exe）",
          _ch_wb.client_exe != _ch_wbai.client_exe
          and _ch_wb.client_exe == "WorkBuddy.exe"
          and _ch_wbai.client_exe == "WorkBuddyAI.exe",
          (_ch_wb.client_exe, _ch_wbai.client_exe))
    check("⑤ find_client_exe 按通道返回各自的 exe",
          (lambda a, b: (a is None or a.name.lower() == "workbuddy.exe")
                        and (b is None or b.name.lower() == "workbuddyai.exe"))(
              wb.find_client_exe(_ch_wb), wb.find_client_exe(_ch_wbai)),
          (str(wb.find_client_exe(_ch_wb)), str(wb.find_client_exe(_ch_wbai))))

    # ⑤b close/open/client_status 都显式收 ch（不靠默认通道兜底）
    import inspect as _ins
    for _nm, _fn in (("close_client", wb.close_client), ("open_client", wb.open_client),
                     ("client_status", wb.client_status),
                     ("find_client_exe", wb.find_client_exe)):
        check("⑤b %s 显式收 ch 参数" % _nm,
              (_ins.signature(_fn).parameters.get("ch") is not None)
              and "channel(ch)" in _ins.getsource(_fn)
              and "CLIENT_PROCESS" not in _ins.getsource(_fn), "")

    # ⑤c 客户端状态缓存按通道分区（单槽会让国际服拿到国服那份结果）
    _cs_src = _ins.getsource(wb.client_status)
    check("⑤c client_status 缓存按通道 key 分区",
          "_CLIENT_STATUS_CACHE[ch.key]" in _cs_src
          and "_CLIENT_STATUS_CACHE.get(ch.key)" in _cs_src, "")
    _real_lp = common.list_processes
    try:
        common.list_processes = lambda: {"workbuddy.exe": [11, 12]}
        wb.invalidate_client_status()
        _a = wb.client_status(_ch_wb)
        common.list_processes = lambda: {"workbuddyai.exe": [21]}
        _b = wb.client_status(_ch_wbai)          # 缓存分区 → 不应命中 wb 那份
        check("⑤c 国际服不会拿到国服那份缓存", _a["pids"] == [11, 12] and _b["pids"] == [21],
              (_a["pids"], _b["pids"]))
        _c = wb.client_status(_ch_wb, fresh=True)   # fresh 绕过缓存，拿到当前真实进程
        check("⑤c fresh=True 拿到当下真实状态", _c["pids"] == [] and _c["running"] is False,
              _c["pids"])
    finally:
        common.list_processes = _real_lp
        wb.invalidate_client_status()

    # ⑤d close_client 只碰本通道的进程（不能把另一个客户端一起关掉）
    _real_lp2, _real_cw2, _real_tp2 = (common.list_processes, common.close_windows_of,
                                       common.terminate_processes)
    try:
        _seen = []
        common.list_processes = lambda: {"workbuddy.exe": [101], "workbuddyai.exe": [202]}
        common.close_windows_of = lambda pids: (_seen.append(list(pids)), 1)[1]
        common.terminate_processes = lambda pids: (_seen.append(list(pids)), len(pids))[1]
        _ok, _msg, _was = wb.close_client(_ch_wbai, graceful_wait=0.3, total_wait=0.5)
        check("⑤d 关国际服客户端时不动国服进程",
              _seen and all(p == [202] for p in _seen), _seen)
    finally:
        common.list_processes = _real_lp2
        common.close_windows_of = _real_cw2
        common.terminate_processes = _real_tp2

    # ⑤e 「只剩托盘图标、点不出来」—— 客户端关闭时走 win.hide()，主窗口隐藏但进程还在。
    #     早先 focus_windows_of 只收 IsWindowVisible 为真的窗口 → 一条都找不到 →
    #     本工具的「打开客户端」恒回"系统不允许切到前台，请从任务栏点开"，点了没反应。
    check("⑤e common.client_main_windows 存在（能认出被隐藏的主窗口）",
          hasattr(common, "client_main_windows"),
          "")
    _src_cmw = _ins.getsource(common.client_main_windows)
    check("⑤e 主窗口按 Electron 类名 + 面积认定（不会误挑 0x0 的 helper）",
          common.ELECTRON_MAIN_CLASS == "Chrome_WidgetWin_1"
          and "ELECTRON_MAIN_CLASS" in _src_cmw
          and "_MIN_MAIN_W" in _src_cmw
          and "include_hidden" in _ins.signature(common.client_main_windows).parameters,
          "")
    _src_focus = _ins.getsource(common.focus_windows_of)
    check("⑤e focus_windows_of 走 client_main_windows（不再只收可见窗口）",
          "client_main_windows(pids)" in _src_focus
          and "IsWindowVisible(hwnd)" not in _src_focus.replace(
              'u32.IsWindowVisible(win["hwnd"])', ""),
          "")
    check("⑤e 隐藏窗口唤出时保留最大化状态（不把最大化还原成小窗）",
          'SW_SHOWMAXIMIZED if win["maximized"] else SW_SHOWNORMAL' in _src_focus
          or "SW_SHOWMAXIMIZED if win['maximized'] else SW_SHOWNORMAL" in _src_focus,
          "")
    _src_cw = _ins.getsource(common.close_windows_of)
    check("⑤e close_windows_of 也能礼貌关闭藏在托盘里的客户端",
          "client_main_windows(pids)" in _src_cw, "")
    check("⑤e open_client 对「收在托盘」给出准确措辞",
          "系统托盘" in src_wb, "")

    # ⑤f 「唤出后窗口看得见却点不动」—— Electron 客户端被**外部** ShowWindow 从托盘
    #     唤出后，Chromium 内部仍认为窗口隐藏/被遮挡，输入事件进不了渲染进程；
    #     窗口有内容、Win32 属性全正常，但鼠标键盘全无反应（用户实测）。
    #     所以唤出后必须**确认拿到前台**，失败且窗口原本在托盘里时直接重启客户端。
    check("⑤f common.activate_windows_of 存在（唤出后确认前台，而不只看可见）",
          hasattr(common, "activate_windows_of"), "")
    _src_act = _ins.getsource(common.activate_windows_of)
    check("⑤f activate_windows_of 会比对 GetForegroundWindow 确认前台",
          "GetForegroundWindow" in _src_act and "fg_ok" in _src_act, "")
    check("⑤f 抢前台带 Alt 键解锁（解除 Windows 前台锁的常规手法）",
          "keybd_event" in _src_act and "VK_MENU" in _src_act
          and "AllowSetForegroundWindow" in _src_act, "")
    check("⑤f 读不到前台状态时判为未知（不当成失败）",
          "fg == 0" in _src_act and "fg_ok = None" in _src_act, "")
    _src_oc2 = _ins.getsource(wb.open_client)
    check("⑤f open_client 改用 activate_windows_of（不再用只看可见的 focus_windows_of）",
          "activate_windows_of(pids)" in _src_oc2
          and "focus_windows_of(pids)" not in _src_oc2, "")
    check("⑤f 窗口收在托盘 → 直接重启客户端（不依赖任何外部'可交互'判据）",
          "close_client(ch, graceful_wait=4.0, log=log)" in _src_oc2
          and "_spawn_client(ch)" in _src_oc2, "")
    check("⑤f 自检可关掉重启副作用（allow_restart 参数）",
          "allow_restart" in _ins.signature(wb.open_client).parameters
          and "allow_restart" in _src_oc2, "")
    check("⑤f 启动后等窗口就绪再置前（不再启动完立刻返回）",
          hasattr(wb, "_wait_client_window")
          and "client_main_windows" in _ins.getsource(wb._wait_client_window)
          and "activate_windows_of" in _ins.getsource(wb._wait_client_window), "")
    _guard = (BIN / "window_state_guard.py")
    check("⑤e window_state_guard 有 revive 命令（手动救急唤出窗口）",
          _guard.exists() and 'add_parser("revive"' in _guard.read_text(encoding="utf-8"),
          str(_guard))

    # ⑥ A：国际服开放迁移弹窗（数据目录 find_data_root 本就认 .workbuddy-ai）
    check("⑥ 国际服 migrate 能力已开启（页面会弹迁移确认框）",
          _ch_wbai.supports("migrate") is True, _ch_wbai.features)
    check("⑥ 国际服签到仍关闭（接口 active=false）",
          _ch_wbai.supports("checkin") is False, "")
    check("⑥ 国际服 open-client 已开放（当前账号行的「打开客户端」要用它）",
          _ch_wbai.supports("open_client") is True, _ch_wbai.features)
    check("⑥ /wbai 视图渲染出 MIGRATE=1",
          wb.Handler.PAGES["/wbai"][1].get("MIGRATE") == "1",
          wb.Handler.PAGES["/wbai"][1].get("MIGRATE"))
    check("⑥ migrate_preview 只报本通道的客户端进程",
          "client_names=ch.client_processes" in _ins.getsource(wb.migrate_preview), "")
    check("⑥ account_migration.preview 接受 client_names 透传",
          "client_names" in _ins.getsource(_am.preview)
          and "client_names" in _ins.getsource(_am.scan), "")

    # ⑦ 「长期/短期」标签不能只认 token_source（2026-09-21）
    #    实测：国服 8 个账号里的「account-e」、国际服 2 个账号全部，JWT payload 里
    #    **没有 token_source 字段** → 前端 ttlTag() 首行 `if(!src) return ''`
    #    让它们的标签整块消失。改用 exp-iat（服务端签发的权威时长）兜底。
    check("⑦ common.jwt_ttl_days 存在（按 exp-iat 取签发天数）",
          hasattr(common, "jwt_ttl_days")
          and "exp - iat" in _ins.getsource(common.jwt_ttl_days), "")
    check("⑦ ttlTag 在 token_source 缺失时用 ttl_days 兜底",
          "byDays = !src && ttlDays!=null" in tpl17
          and "ttlTag(o.token_source, o.expires_at, rd, o.ttl_days)" in tpl17, "")
    _acc_by_file = {}
    for _k in ("wb", "wbai"):
        for _a in wb.list_accounts(_k):
            _acc_by_file[(_k, _a.get("file"))] = _a
    check("⑦ list_accounts 每个条目都带 ttl_days 字段",
          all("ttl_days" in _a for _a in _acc_by_file.values()),
          sorted({tuple(sorted(_a)) for _a in _acc_by_file.values()})[:1])
    # 真实素材验证：找到"没 token_source 但有 ttl_days"的账号才算数（没有就跳过）
    _naked = [(_k, _f, _a) for (_k, _f), _a in _acc_by_file.items()
              if _a.get("ok") and not _a.get("token_source") and _a.get("ttl_days")]
    if _naked:
        check("⑦ 无 token_source 的账号也能算出签发天数（不再显示为空）",
              all(isinstance(_a["ttl_days"], int) and _a["ttl_days"] > 0 for _k, _f, _a in _naked),
              [(_f, _a.get("ttl_days")) for _k, _f, _a in _naked])
    else:
        check("⑦ 本机无「缺 token_source」的账号，跳过实数据用例", True, "")
    # 反向：打了标的那些必须仍然优先用 token_source（不能被 exp-iat 顶掉）
    _tagged = [(_k, _f, _a) for (_k, _f), _a in _acc_by_file.items()
               if _a.get("ok") and _a.get("token_source")]
    check("⑦ 有 token_source 的账号仍走原判定（不改用 exp-iat）",
          "src ? " in tpl17 and bool(_tagged), len(_tagged))
    # ⑦b 光有 ttl_days 不够 —— rowHtml 的两个调用点曾经手写字段白名单，
    #     把 ttl_days 漏在门外，o.ttl_days 恒为 undefined，兜底分支永远进不去
    #     （account-e + 国际服全部账号因此整块掉标签）。必须透传接口对象。
    _bases = re.findall(r"rowHtml\(Object\.assign\(\{\},\s*(\w+),", tpl17)
    check("⑦b rowHtml 的调用点透传接口对象（不再手写白名单漏 ttl_days）",
          sorted(_bases) == ["a", "c"] and tpl17.count("function rowHtml(") == 1,
          "透传基座=%s" % _bases)
    check("⑦b rowHtml 读取 ttl_days 且调用点不再硬编码字段清单",
          "o.ttl_days" in tpl17 and "rowHtml({\n" not in tpl17,
          "")

    # ⑦c 网络层失败要翻译成人话。浏览器原生 TypeError("Failed to fetch") 的真实含义是
    #     "本地服务没起/端口变了"，直接抛给用户只会让人以为功能坏了（2026-09-22 实测）。
    check("⑦c 模板把 Failed to fetch 翻译成「连不上本地服务」",
          "function netFail(" in tpl17 and "连不上本地服务" in tpl17,
          "")
    check("⑦c req / post 都包了 fetch 的 try-catch",
          tpl17.count("catch(e){ throw netFail(e); }") == 2,
          tpl17.count("throw netFail(e)"))

    # ⑦d 客户端 5.6.2+（2026-09-23）启用了「静态字段保护」：登录态里
    #     accessToken / refreshToken / nickname 不再是明文字符串，而是
    #     {"$wbEncrypted":1,"envelope":"<base64>"}。session_from_info_file 因此
    #     返回 None，页面显示「桌面端当前未登录」—— 而客户端明明登录着（实报）。
    #     密钥由客户端运行时注入、本地拿不到，解不开；但 account.uid 仍是明文，
    #     可以拿它在账号库里反查昵称，至少把"是谁"答出来。
    print("\n== 7d. 加密登录态（客户端字段保护）的降级识别 ==")
    _enc_field = {"$wbEncrypted": 1, "envelope": "eyJzdWl0ZSI6MX0="}
    _enc_raw = {
        "account": {"uid": "uid-enc-1", "nickname": _enc_field, "uin": "123"},
        "auth": {"accessToken": _enc_field, "refreshToken": _enc_field},
    }
    _plain_raw = {"account": {"uid": "u-plain", "nickname": "张三"},
                  "auth": {"accessToken": "eyJhbGciOiJIUzI1NiJ9.abc.def"}}
    check("⑦d 能认出字段保护包装（$wbEncrypted）",
          wb.is_encrypted_field(_enc_field) is True, _enc_field)
    check("⑦d 明文 token 不会被误判为加密",
          wb.is_encrypted_field("eyJhbGciOi") is False
          and wb.is_encrypted_field(None) is False, "")
    check("⑦d 能从加密登录态里取到明文 uid",
          wb.plain_uid(_enc_raw) == "uid-enc-1", wb.plain_uid(_enc_raw))
    check("⑦d info_is_encrypted 认得出凭据已加密",
          wb.info_is_encrypted(_enc_raw) is True
          and wb.info_is_encrypted(_plain_raw) is False, "")
    check("⑦d 前端对加密登录态显示昵称而不是「未登录」",
          "c.encrypted" in tpl17 and "当前账号：" in tpl17, "")

    # 加密登录态的 current_account —— **必须重定向到临时目录**：
    # 这一步会写/读登录态目录，真实桌面端登录态绝不能碰（见 5b 的说明）。
    ch_wb = wb.channel("wb")
    _o_dir, _o_name = ch_wb.desktop_dir, ch_wb.info_name
    _o_bin = wb._BIN_DIR
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib = root / "lib"
            (lib / "wb_auth").mkdir(parents=True)
            # 账号库里放一份同 uid 的**明文**素材，作为昵称反查源
            # （uid 必须与加密登录态一致，否则反查不到）
            (lib / "wb_auth" / "workbuddy-张三.info").write_text(
                json.dumps({"account": {"uid": "uid-enc-1", "nickname": "张三"},
                            "auth": {"accessToken": "eyJhbGciOiJIUzI1NiJ9.abc.def"}},
                           ensure_ascii=False), encoding="utf-8")
            wb._BIN_DIR = lib
            ch_wb.redirect(root / "desk")
            ch_wb.desktop_dir.mkdir(parents=True, exist_ok=True)
            ch_wb.desktop_info.write_text(
                json.dumps(_enc_raw, ensure_ascii=False), encoding="utf-8")

            check("⑦d 重定向已生效（真实登录态不受影响）",
                  ch_wb.desktop_info.parent == root / "desk"
                  and ch_wb.auth_dir == lib / "wb_auth", str(ch_wb.desktop_info))
            cur = wb.current_account("wb")
            top = cur[0] if cur else {}
            check("⑦d 加密登录态仍被标为「当前」",
                  len(cur) == 1 and top.get("current") is True, cur)
            check("⑦d 加密登录态 ok=False 但 encrypted=True",
                  top.get("ok") is False and top.get("encrypted") is True, top)
            check("⑦d 用明文 uid 在账号库里反查出昵称（不是文件名）",
                  top.get("nickname") == "张三" and top.get("uid") == "uid-enc-1",
                  (top.get("nickname"), top.get("uid")))
            check("⑦d 加密条目带 reason，前端不会再说「未登录」",
                  bool(top.get("reason")) and "加密" in top.get("reason", ""),
                  top.get("reason"))
            # 账号库里没有对应 uid 时退化成 uid 前 8 位，而不是退回文件名
            ch_wb.desktop_info.write_text(json.dumps({
                "account": {"uid": "abcdefgh-9999", "nickname": _enc_field},
                "auth": {"accessToken": _enc_field},
            }, ensure_ascii=False), encoding="utf-8")
            _c2 = wb.current_account("wb")
            check("⑦d 反查不到时退化成 uid 前 8 位（不显示文件名）",
                  _c2[0].get("nickname") == "abcdefgh", _c2[0].get("nickname"))
    finally:
        ch_wb.redirect(_o_dir, _o_name)
        wb._BIN_DIR = _o_bin
    check("⑦d 还原后登录态目录回到真实位置",
          ch_wb.desktop_dir == _o_dir and wb._BIN_DIR == _o_bin,
          str(ch_wb.desktop_dir))

    # ⑦e ⑦d 只做到"读不到 token 也能把是谁答出来"，但昵称和剩余天数还是缺的。
    #     真正的解法：客户端自己要拿**明文** token 调接口 → 进程内存里必有一份。
    #     live_token.py 就是只读地去那儿取。这里只钉接口形状与安全边界，
    #     **不真扫内存**（慢，且依赖客户端正在跑）。
    print("\n== 7e. 从客户端进程内存读凭据（live_token）==")
    import base64 as _b64
    import ctypes as _ct
    import datetime as _dt

    lt = getattr(wb, "live_token", None)
    check("⑦e wb_ui_server 已接入 live_token 模块", lt is not None, "")

    if lt is not None:
        def _jwt(payload):
            seg = _b64.urlsafe_b64encode(
                json.dumps(payload).encode()).decode().rstrip("=")
            return "eyJhbGciOiJSUzI1NiJ9." + seg + "." + "x" * 48

        _acc = _jwt({"typ": "Bearer", "aud": "account", "sub": "u1", "exp": 2000000000})
        _ref = _jwt({"typ": "Offline", "aud": "https://x/realms/y", "sub": "u1"})
        _oth = _jwt({"typ": "Whatever"})
        check("⑦e 按 typ=Bearer 认出 accessToken",
              lt._classify(_acc)[0] == "access", lt._classify(_acc))
        check("⑦e 按 typ=Offline 认出 refreshToken",
              lt._classify(_ref)[0] == "refresh", lt._classify(_ref))
        check("⑦e 既不是 access 也不是 refresh 的 JWT 被丢掉",
              lt._classify(_oth) is None, lt._classify(_oth))
        check("⑦e exp 转成带时区的 UTC 时间",
              getattr(lt._expiry({"exp": 2000000000}), "tzinfo", None) is not None, "")
        check("⑦e 没有 exp 时不编造过期时间", lt._expiry({}) is None, "")

        # 缓存必须按通道分区（与 _CREDITS_CACHE 同一个理由：不分区会让国际服拿到国服那份）
        lt._CACHE.clear()
        lt._CACHE["wb"] = {"ts": time.time(), "session": {"uid": "a"}}
        lt._CACHE["wbai"] = {"ts": time.time(), "session": {"uid": "b"}}
        lt.invalidate_cache(wb.channel("wb"))
        check("⑦e 失效缓存只清该通道（国际服那份不受影响）",
              "wb" not in lt._CACHE and "wbai" in lt._CACHE, dict(lt._CACHE))
        lt.invalidate_cache()
        check("⑦e 不传通道时全清", lt._CACHE == {}, dict(lt._CACHE))

        _lt_src = (BIN / "live_token.py").read_text(encoding="utf-8")
        check("⑦e 内存访问是只读的（不写对方进程内存）",
              "ReadProcessMemory" in _lt_src
              and "WriteProcessMemory" not in _lt_src
              and "VirtualProtectEx" not in _lt_src, "")
        check("⑦e 会话按 JWT 的 sub 分组（切号残留的旧 token 归它自己的 uid，不会串号）",
              'uid = str(payload.get("sub") or "")' in _lt_src
              and "return sessions.get(expect_uid)" in _lt_src, "")

        # ⑦e 性能不变量（09-24）。两条各对应一次实测到的 0.3s 级浪费，别再改回去。
        # 1) `buf.raw[:n]` 会**先复制整个 1MB 缓冲区**再切片 → 单进程 1600 多块就是
        #    1.6GB 无谓 memcpy，占掉整次扫描 0.43s 里的 0.30s（真正读内存只要 0.028s）。
        check("⑦e 取内存块走 memoryview 零拷贝（不是 buf.raw 整块复制）",
              "view = memoryview(buf)" in _lt_src
              and "_JWT_RE.finditer(view[" in _lt_src
              and "= buf.raw" not in _lt_src, "")
        # 2) tasklist 要起子进程，本机 95 个进程实测 0.30s；Toolhelp32 是 ~2ms。
        _common_src = (BIN / "switcher_common.py").read_text(encoding="utf-8")
        check("⑦e 进程枚举主路径是原生 Toolhelp32（tasklist 只兜底）",
              "_list_processes_native" in _common_src
              and "CreateToolhelp32Snapshot" in _common_src
              and _common_src.index("def _list_processes_native")
              < _common_src.index('subprocess.run(["tasklist"'), "")

        # ⑦e 上面那些是字符串检查，下面做**行为级**验证：桩掉 scan_process / client_pids，
        # 用两个账号的假 token 真跑一遍 read_live_session。钉住 09-23 性能优化的三条核心语义：
        #   ① expect_uid 只取自己那份  ② 一次扫描同时回答多个 uid（不重扫）
        #   ③ 内存里没有的 uid 走负命中（不重扫）—— 切号备份就靠这条不再白扫 3.5 秒
        import base64 as _b64

        def _mk(uid, exp, typ="Bearer", nick="n"):
            """造一个长度落在 _MIN/_MAX_JWT_LEN 之间的假 JWT（pad 撑长度）。"""
            pl = {"sub": uid, "exp": exp, "typ": typ, "nickname": nick, "pad": "x" * 420}
            body = _b64.urlsafe_b64encode(
                json.dumps(pl).encode()).decode().rstrip("=")
            return "eyJhbGciOiJSUzI1NiJ9." + body + ".c2ln"

        _uidA = "aaaa1111-0000-0000-0000-000000000001"
        _uidB = "bbbb2222-0000-0000-0000-000000000002"
        _uidC = "cccc3333-0000-0000-0000-000000000003"
        _t0 = int(time.time())
        # 按 pid 分离：**第一个进程只有 access，refresh 只在第二个进程里**
        # （实测就是这个形状 —— 国服 pid 6212 有 access 无 refresh，pid 16972 才有）。
        # 这样「默认档不为 refresh 多扫」才能被真正验证：默认档扫完第一个进程就停。
        _by_pid = {111: {_mk(_uidA, _t0 + 1000, "Bearer", "accA"): 1,
                         _mk(_uidB, _t0 + 1500, "Bearer", "accB"): 1},
                   222: {_mk(_uidA, _t0 + 2000, "Offline", "accA"): 1}}
        _saved = (lt.scan_process, lt.client_pids)
        _scans = []
        lt.scan_process = lambda pid, budget=None, types=None: (
            _scans.append((pid, types)) or dict(_by_pid.get(pid, {})))
        lt.client_pids = lambda ch: [111, 222]
        lt.invalidate_cache()
        try:
            _sA = lt.read_live_session(wb.channel("wb"), expect_uid=_uidA)
            check("⑦e 行为：expect_uid 只取自己那份会话",
                  (_sA or {}).get("uid") == _uidA, (_sA or {}).get("uid"))
            check("⑦e 行为：默认档拿到 access 就停（不为 refresh 扫第二个进程）",
                  len(_scans) == 1 and (_sA or {}).get("refresh_token") == "",
                  "scans=%d refresh=%r" % (len(_scans), (_sA or {}).get("refresh_token")))
            _n1 = len(_scans)
            _sB = lt.read_live_session(wb.channel("wb"), expect_uid=_uidB)
            check("⑦e 行为：一次扫描同时回答了另一个 uid（没有重扫）",
                  (_sB or {}).get("uid") == _uidB and len(_scans) == _n1,
                  "%s / scans=%d" % ((_sB or {}).get("uid"), len(_scans)))
            _sC = lt.read_live_session(wb.channel("wb"), expect_uid=_uidC)
            check("⑦e 行为：内存里没有的 uid 走负命中（不重扫）—— 切号备份靠这条不白扫",
                  _sC is None and len(_scans) == _n1, "scans=%d" % len(_scans))
            _sAll = lt.read_live_session(wb.channel("wb"))
            check("⑦e 行为：不带 expect_uid 时取 exp 最晚的那个",
                  (_sAll or {}).get("uid") == _uidB, (_sAll or {}).get("uid"))
            lt.invalidate_cache()
            _scans.clear()
            _sR = lt.read_live_session(wb.channel("wb"), expect_uid=_uidA, want_refresh=True)
            check("⑦e 行为：want_refresh=True 才继续扫到 refresh 所在的进程",
                  bool((_sR or {}).get("refresh_token")) and len(_scans) == 2,
                  "scans=%d refresh=%r" % (len(_scans), bool((_sR or {}).get("refresh_token"))))

            # --- P2-3（AUDIT_2026-09-24.md）：缓存命中分支以前**不看 want_refresh** ---
            # 60 秒内先有功能路径调用（只扫到 access、refresh_token 为空），随后
            # `/api/live-token` 传 want_refresh=True 会命中同一份缓存 → 拿到空
            # refresh_token，误报「该账号没有 refreshToken」，结论随调用顺序漂移。
            lt.invalidate_cache()
            _scans.clear()
            _sNoR = lt.read_live_session(wb.channel("wb"), expect_uid=_uidA)   # 功能档先来
            _scans.clear()
            _sR2 = lt.read_live_session(wb.channel("wb"), expect_uid=_uidA, want_refresh=True)
            check("P2-3 行为：功能档写过缓存后，诊断档仍能拿到 refresh_token",
                  (_sNoR or {}).get("refresh_token") == ""
                  and bool((_sR2 or {}).get("refresh_token")),
                  "先=%r 后=%r scans=%d" % ((_sNoR or {}).get("refresh_token"),
                                            bool((_sR2 or {}).get("refresh_token")), len(_scans)))
            # 反向：诊断档写下的缓存对功能档是**够用**的，不该反过来浪费一次全扫
            _scans.clear()
            _sNoR2 = lt.read_live_session(wb.channel("wb"), expect_uid=_uidA)
            check("P2-3 行为：诊断档的缓存对功能档仍可复用（不反向浪费一次扫描）",
                  bool((_sNoR2 or {}).get("access_token")) and len(_scans) == 0,
                  "scans=%d" % len(_scans))
        finally:
            lt.scan_process, lt.client_pids = _saved
            lt.invalidate_cache()
        check("⑦e 当前解释器是 64 位（扫描按 x64 结构写死）",
              _ct.sizeof(_ct.c_void_p) == 8, _ct.sizeof(_ct.c_void_p))

        # 进程枚举只能有一份实现：必须复用 common.list_processes()。
        # 它已处理 GBK 输出解码、CREATE_NO_WINDOW、以及「枚举失败(None)」与
        # 「真的没有进程」的区分；自己再起一份 tasklist 就是第二份等价逻辑。
        check("⑦e 进程枚举复用 common.list_processes（不重复实现）",
              "common.list_processes()" in _lt_src and "subprocess" not in _lt_src, "")
        # 附带记录的真实坑：list_processes 把 GBK 输出按 UTF-8 解码，
        # 非 ASCII 进程名会变成乱码键（本机 WorkBuddy小工具.exe → workbuddyс????.exe）。
        # 所以按名字匹配的进程名**必须全是 ASCII**，否则永远匹配不上。
        _procs_tbl = common.list_processes() or {}
        check("⑦e 按名字匹配的客户端进程名全是 ASCII（GBK 乱码键不会命中）",
              bool(_procs_tbl) and all(
                  n.isascii() for k in ("wb", "wbai") for n in wb.channel(k).client_processes),
              [n for k in ("wb", "wbai") for n in wb.channel(k).client_processes])
        # 原生枚举的**行为**验证（上面那条只是字符串检查）：非空、键是小写名、值是 int 列表。
        # 与 tasklist 的 pid 集合已人工对拍过一次（09-24，完全一致；差异只有 pid 0 的名字
        # `[system process]` vs `system idle process`，以及 tasklist 会把自己列进去）。
        _native_tbl = common._list_processes_native()
        check("⑦e 原生进程枚举可用（Toolhelp32：非空、键小写、值为 int 列表）",
              bool(_native_tbl) and all(
                  k == k.lower() and v and all(isinstance(p, int) for p in v)
                  for k, v in _native_tbl.items()),
              len(_native_tbl or {}))

        # ⑦e 只钉到接口层的 JSON 字段；「页面上真的显示出来」由独立脚本验证
        # （要起无头浏览器，不适合塞进本自检）。这里钉它存在 + 判别式没被改掉，
        # 免得那个脚本悄悄烂掉。
        _ui_chk = BIN / "check_live_token_ui.py"
        check("⑦e 页面渲染验证脚本存在（check_live_token_ui.py）", _ui_chk.is_file(), "")
        if _ui_chk.is_file():
            _ui_src = _ui_chk.read_text(encoding="utf-8")
            check("⑦e 渲染验证先剥掉 <script>/<style> 再断言（JS 兜底文案会污染）",
                  "def visible_dom(" in _ui_src and "visible_dom(dom)" in _ui_src, "")
            check("⑦e 渲染验证钉住「页面不出现实现细节文案」",
                  "wb.LIVE_REASON not in vis" in _ui_src, "")
            check("⑦e 渲染验证有三种模式（桩 / --no-live / --real）",
                  '"--real"' in _ui_src and '"--no-live"' in _ui_src, "")

        # ⑦e 前端：内存读到凭据时，界面必须和明文登录态**没有任何可见差别** ——
        # 「凭据读自客户端进程内存」是本工具的实现细节，不该出现在页面上。
        check("⑦e 前端在 live 时走正常渲染路径（不再单独显示加密说明）",
              "if(!c.live)" in tpl17, "")
        # currentFile() 不能因为 ok===false 就返回 ''：加密登录态下 ok 恒为 false，
        # 但 uid 仍是明文，照样能映射回账号库文件。返回 '' 有两个可见后果：
        # ①「当前账号」行丢积分 ② 账号列表里重复出现当前账号。
        check("⑦e currentFile 不再因 ok===false 提前返回空（否则列表重复 + 丢积分）",
              "c.ok===false || !c.uid" not in tpl17
              and "if(!c || !c.uid) return '';" in tpl17, "")

        # ⑦e ⚠️ **统一入口页的侧边栏是另一份前端**（ui_hub.html）。只改 ui_template.html
        # 会让导航栏继续把 `c.reason`（= LIVE_REASON）显示出来 —— 用户 09-23 报的正是这里：
        # 「当前账号已经读出信息了，但导航栏上显示：凭据读自客户端进程内存（…）」。
        # 两份前端必须**同一套判据**：`ok===false` 在加密登录态下是常态，
        # 只有 `live` 也为假才降级。
        _hub_src = (BIN / "ui_hub.html").read_text(encoding="utf-8")
        check("⑦e 入口页侧边栏 live 时也走正常渲染路径（导航栏不再显示加密说明）",
              "c.ok === false && !c.live" in _hub_src, "")
        check("⑦e 入口页侧边栏不再把 ok===false 单独当不可用",
              "if(!c || c.ok === false){" not in _hub_src, "")
        # 反向钉住：两份前端都不许出现「LIVE_REASON 的字面量被写进渲染路径」。
        # 用渲染路径的特征串判定，不扫全文（注释里允许出现这段中文做解释）。
        check("⑦e 两份前端都不把 LIVE_REASON 当静态兜底文案",
              "c.reason||'凭据读自客户端进程内存" not in tpl17
              and "c.reason||'凭据读自客户端进程内存" not in _hub_src, "")

        # ⑦e 内存扫描的性能不变量（09-23 优化，用户报「账号读取很慢」）。
        # 每一项都对应一个实测有效的优化，改坏了会静默退化成秒级卡顿，
        # 所以在这里钉住；具体数据见 live_token.read_live_session 的文档串。
        check("⑦e 扫描支持按区域类型过滤（跳过只读镜像段，占可读内存 ~60%）",
              "def scan_process(pid, budget=DEFAULT_BUDGET, types=None)" in _lt_src
              and "MEM_PRIVATE = 0x20000" in _lt_src
              and "types is None or mbi.Type in types" in _lt_src, "")
        check("⑦e 主路径先扫 MEM_PRIVATE，未命中才退到 MAPPED+IMAGE",
              "{MEM_PRIVATE}" in _lt_src
              and "{MEM_MAPPED, MEM_IMAGE}" in _lt_src, "")
        check("⑦e 命中即停（拿到 access 就不再扫剩余进程）",
              "def _stop_on_access(" in _lt_src and "if stop(by_uid):" in _lt_src, "")
        check("⑦e refresh 只在 want_refresh=True 时才等（诊断接口专用）",
              "want_refresh=False" in _lt_src and "_stop_on_both if want_refresh" in _lt_src
              and "want_refresh=True" in (BIN / "wb_ui_server.py").read_text(encoding="utf-8"), "")
        check("⑦e 缓存按 uid 存会话（切号备份的 uid 缺失即负命中，不重扫）",
              '"sessions"' in _lt_src and '"best_uid"' in _lt_src
              and "return sessions.get(expect_uid)" in _lt_src, "")
        # 并行扫描实测无效（跨进程读内存有全局竞争，workers=7 比串行还慢）→ 必须没有它，
        # 否则既多线程开销又没收益。
        check("⑦e 不再使用线程池并行扫描（实测无效，已撤）",
              "ThreadPoolExecutor" not in _lt_src, "")
        _procs = common.list_processes()
        if _procs and any("workbuddy.exe" in n for n in _procs):
            _pids_live = lt.client_pids(wb.channel("wb"))
            check("⑦e 客户端在跑时必须枚举到 pid（国服 workbuddy.exe）",
                  len(_pids_live) > 0, _pids_live)
            check("⑦e 国际服通道只枚举 workbuddyai.exe（不串通道）",
                  all(p in (_procs.get("workbuddyai.exe") or [])
                      for p in lt.client_pids(wb.channel("wbai"))),
                  lt.client_pids(wb.channel("wbai")))
        else:
            check("⑦e 本机国服客户端没在跑，跳过实进程枚举用例", True, "")

    # 脱敏：CLI / HTTP 会把这份 dict 交给前端和审计日志，不能带完整凭据
    _now = _dt.datetime(2026, 11, 17, 12, 44, 32, tzinfo=_dt.timezone.utc)
    _sess = {"access_token": "A" * 100, "refresh_token": "B" * 50, "uid": "u1",
             "nickname": "account-e", "expires_at": _now, "refresh_expires_at": None,
             "token_source": "", "source_pid": 1, "scanned_pids": [1, 2]}
    _red = wb.redact_session(_sess)
    check("⑦e 脱敏后不含完整 token（只留前缀与长度）",
          "A" * 100 not in json.dumps(_red, ensure_ascii=False)
          and _red["access_token"] == "A" * 16 + "…（100 字符）", _red["access_token"])
    check("⑦e 脱敏保留 uid / 昵称 / 来源 pid",
          (_red.get("uid"), _red.get("nickname"), _red.get("source_pid")) == ("u1", "account-e", 1),
          _red)
    check("⑦e 脱敏把 datetime 转成 isoformat（JSON 可序列化）",
          _red["expires_at"] == "2026-11-17T12:44:32+00:00", _red["expires_at"])
    check("⑦e 脱敏对空输入返回 None", wb.redact_session(None) is None, "")
    check("⑦e 空 token 不编出假摘要",
          wb.redact_session({"access_token": ""})["access_token"] == "",
          wb.redact_session({"access_token": ""}))

    _srv_src = (BIN / "wb_ui_server.py").read_text(encoding="utf-8")
    _app_src = (BIN / "ui_app.py").read_text(encoding="utf-8")
    check("⑦e 只读接口 /api/live-token 存在且返回脱敏结果",
          '"/api/live-token"' in _srv_src and "redact_session(session)" in _srv_src, "")
    check("⑦e --live-token 在启动器的 CLI 转发表里（否则 exe 会当「开窗口」阻塞）",
          '"--live-token"' in _app_src, "")
    check("⑦e CLI --live-token 读不到凭据时退出码为 1",
          "return 1" in _srv_src.split("if args.live_token:")[1].split("if args.switch")[0], "")
    check("⑦e 前端读 c.live 显示真实昵称与剩余天数",
          "c.live" in tpl17 and "c.encrypted" in tpl17, "")

    # ⑦e-2 内存读到凭据时，current_account 应该用内存里的昵称/到期时间，
    #       并把 reason 从「读不到」换成 LIVE_REASON。用桩替换 live_session，
    #       既不真扫内存，也不碰真实登录态（同 ⑦d 的重定向纪律）。
    ch_wb2 = wb.channel("wb")
    _o2_dir, _o2_name = ch_wb2.desktop_dir, ch_wb2.info_name
    _o2_bin, _o2_live = wb._BIN_DIR, wb.live_session
    try:
        with tempfile.TemporaryDirectory() as td2:
            root2 = Path(td2)
            lib2 = root2 / "lib"
            (lib2 / "wb_auth").mkdir(parents=True)
            wb._BIN_DIR = lib2
            ch_wb2.redirect(root2 / "desk")
            ch_wb2.desktop_dir.mkdir(parents=True, exist_ok=True)
            ch_wb2.desktop_info.write_text(json.dumps({
                "account": {"uid": "uid-live-1", "nickname": _enc_field},
                "auth": {"accessToken": _enc_field},
            }, ensure_ascii=False), encoding="utf-8")

            wb.live_session = lambda ch, uid=None: {
                "access_token": "eyJhbGciOiJSUzI1NiJ9.mem.sig",
                "refresh_token": "eyJhbGciOiJSUzI1NiJ9.mem.sig",
                "uid": "uid-live-1", "nickname": "内存昵称",
                "expires_at": _now, "refresh_expires_at": None,
                "token_source": "oneid_login", "source_pid": 999, "scanned_pids": [999]}
            _c3 = wb.current_account("wb")
            _t3 = _c3[0] if _c3 else {}
            check("⑦e 内存读到凭据时 live=True（且仍是 encrypted）",
                  _t3.get("live") is True and _t3.get("encrypted") is True, _t3)
            check("⑦e 内存读到凭据时用内存里的昵称（不再退化成 uid 前缀）",
                  _t3.get("nickname") == "内存昵称", _t3.get("nickname"))
            check("⑦e 内存读到凭据时到期时间取内存里的 exp",
                  str(_t3.get("expires_at") or "").startswith("2026-11-17"), _t3.get("expires_at"))
            check("⑦e reason 换成 LIVE_REASON（不再说「读不到」）",
                  _t3.get("reason") == wb.LIVE_REASON, _t3.get("reason"))

            wb.live_session = lambda ch, uid=None: None
            _c4 = wb.current_account("wb")
            check("⑦e 内存也读不到时退回 ENCRYPTED_REASON 且 live=False",
                  _c4[0].get("live") is False
                  and _c4[0].get("reason") == wb.ENCRYPTED_REASON, _c4[0])
    finally:
        wb.live_session = _o2_live
        ch_wb2.redirect(_o2_dir, _o2_name)
        wb._BIN_DIR = _o2_bin
    check("⑦e 还原后登录态目录与 live_session 回到原状",
          ch_wb2.desktop_dir == _o2_dir and wb._BIN_DIR == _o2_bin
          and wb.live_session is _o2_live, str(ch_wb2.desktop_dir))

    print("\n== 20. 审计修复回归（AUDIT_2026-09-24.md）==")
    src_mig20 = (BIN / "account_migration.py").read_text(encoding="utf-8")
    src_cm20 = (BIN / "switcher_common.py").read_text(encoding="utf-8")
    src_wb20 = (BIN / "wb_ui_server.py").read_text(encoding="utf-8")
    src_lt20 = (BIN / "live_token.py").read_text(encoding="utf-8")
    src_tpl20 = (BIN / "ui_template.html").read_text(encoding="utf-8")
    src_hub20 = (BIN / "ui_hub.html").read_text(encoding="utf-8")

    # --- P0-1：迁移引用了一个**全仓不存在**的函数 ---
    # ⚠️ 只匹配「调用」（带左括号），不能匹配裸函数名 —— 上面的说明性注释里
    #    正写着 `` `common.safe_unlink_tree` ``（同一类假失败本会话踩过好几次）。
    check("P0-1 迁移不再调用不存在的 common.safe_unlink_tree",
          "common.safe_unlink_tree(" not in src_mig20
          and not hasattr(common, "safe_unlink_tree"), "")
    check("P0-1 合并分支改成把旧目录移进备份并登记 renames（可回滚）",
          'parked = journal.backup / "storage-merged"' in src_mig20
          and "journal.renames.append((d, parked))" in src_mig20, "")

    # --- P1-1：回滚要能还原「合并」掉的目录 + 清掉合并出来的副本 ---
    check("P1-1 _merge_storage_dir 返回本次新建的文件列表",
          "created.append(target)" in src_mig20 and "return created" in src_mig20, "")
    check("P1-1 回滚清理合并残留（journal.merged_files）",
          "self.merged_files = []" in src_mig20
          and 'for p in getattr(journal, "merged_files", [])' in src_mig20, "")

    # --- P1-2：manifest 增量落盘 ---
    check("P1-2 manifest 每步增量落盘（不再只在全部成功后写一次）",
          "def flush(self)" in src_mig20 and "self.flush()" in src_mig20, "")

    # --- P1-4：切号与续期/签到/删除必须互斥 ---
    _sw20 = src_wb20.split("\ndef switch_account")[1].split("\ndef ")[0]
    check("P1-4 切号同时拿通道锁 + 目标账号锁",
          "ch.lock_key" in _sw20
          and 'account_lock_key = ch.key + "-auth-" + norm' in _sw20
          and _sw20.count("common.file_lock(") == 2, "")
    check("P1-4 续期/签到/删除用的是同一把账号锁（不是各写各的）",
          src_wb20.count('common.file_lock(ch.key + "-auth-" + base') >= 3, "")

    # --- P1-6：三处「当前账号」判据必须同源 ---
    check("P1-6 currentMatches 也认 c.live（与另两处判据一致）",
          "(c.ok!==false || c.live)" in src_tpl20, "")
    check("P1-6 三处判据都出现 c.live",
          src_tpl20.count("c.live") >= 2 and "c.ok === false && !c.live" in src_hub20, "")

    # --- P2-1：请求体上限 ---
    check("P2-1 _params 校验 Content-Length（非法/负数/超大 → 400 而非 500）",
          "MAX_BODY_BYTES" in src_cm20 and "class BadRequest" in src_cm20
          and "isinstance(exc, BadRequest)" in src_cm20, "")

    # --- P2-2：页面路由必须在同源守卫之后 ---
    _get20 = src_cm20.split("def do_GET")[1].split("def do_POST")[0]
    check("P2-2 页面路由移到 _guard() 之后",
          _get20.index("if not self._guard():") < _get20.index('if u.path == "/":'), "")
    check("P2-2 /api/ping 仍有意留在守卫之前（单实例探测要跨源）",
          _get20.index('if u.path == "/api/ping":') < _get20.index("if not self._guard():"), "")

    # --- P2-3 / P2-4：live_token 的缓存语义与三态 ---
    check("P2-3 缓存记下 want_refresh，诊断档不吃功能档那份缓存",
          'hit.get("want_refresh") or not want_refresh' in src_lt20
          and '"want_refresh": bool(want_refresh)' in src_lt20, "")
    check("P2-4 枚举失败返回 None（≠ 空列表）",
          "if table is None:\n        return None" in src_lt20, "")
    check("P2-4 枚举失败时不写负缓存",
          "if pids is None:" in src_lt20, "")

    _real_lp20 = common.list_processes
    try:
        common.list_processes = lambda: None
        check("P2-4 行为：枚举失败 → client_pids 返回 None（不是 []）",
              lt.client_pids(wb.channel("wb")) is None, "")
        lt.invalidate_cache()
        _r20 = lt.read_live_session(wb.channel("wb"))
        check("P2-4 行为：枚举失败时 read_live_session 返回 None 且不写缓存",
              _r20 is None and not lt._CACHE, dict(lt._CACHE))
        common.list_processes = lambda: {"explorer.exe": [1]}
        check("P2-4 行为：枚举成功但客户端没跑 → 返回 []",
              lt.client_pids(wb.channel("wb")) == [], "")
    finally:
        common.list_processes = _real_lp20
        lt.invalidate_cache()

    print("\n== 21. 第二轮修复回归（AUDIT_2026-09-24.md 未修项 A/B/D/E 组）==")
    src_wsg21 = (BIN / "window_state_guard.py").read_text(encoding="utf-8")
    src_it21 = (BIN / "import_token.py").read_text(encoding="utf-8")
    src_spec21 = (BIN / "AgentAccountSwitcher.spec").read_text(encoding="utf-8")
    src_hub21 = (BIN / "ui_hub.html").read_text(encoding="utf-8")
    src_tpl21 = (BIN / "ui_template.html").read_text(encoding="utf-8")

    def _code21(src):
        """剥掉**所有** docstring（含函数内）与 `#` 注释，再做匹配。

        ⚠️ 这个项目反复踩的坑：断言挂在自己的注释/文档上。本次修复的注释与 docstring 里
        **特意写了**被修掉的旧写法（`tmp.replace(path)` / `read_state(ch) or {}` /
        `out.write_text(...)`）作为说明 —— 只剥 `#` 注释是不够的，
        函数 docstring 里同样会有（实测栽在 `_atomic_write_text` 的 docstring 上）。
        """
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.FunctionDef,
                                     ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(getattr(body[0], "value", None), ast.Constant) and \
                    isinstance(body[0].value.value, str):
                for i in range(body[0].lineno - 1, min(body[0].end_lineno, len(lines))):
                    lines[i] = ""
        return "\n".join(ln.split("#", 1)[0] for ln in lines)

    _cm21 = _code21((BIN / "switcher_common.py").read_text(encoding="utf-8"))
    _wb21 = _code21((BIN / "wb_ui_server.py").read_text(encoding="utf-8"))

    # --- P2-8：read_state 的损坏态兜底 ---
    check("P2-8 对 read_state 结果做 dict 兜底（损坏时返回字符串 'CORRUPT'，`or {}` 兜不住）",
          "if not isinstance(st, dict):\n        st = {}" in _code21(src_wsg21), "")

    # --- P2-9：ui_hub 侧边栏转义 ---
    check("P2-9 ui_hub 侧边栏的 reason 补上 esc()（同函数下一行本来就有）",
          "esc((c && c.reason) || '桌面端当前未登录')" in src_hub21, "")

    # --- P2-5：续期写回走重试 + 失败降级 ---
    _rec21 = _wb21.split("def _recalc_expiry")[1].split("\ndef ")[0]
    check("P2-5 _recalc_expiry 改用 common.replace_with_retry（不再裸 tmp.replace）",
          "common.replace_with_retry(tmp, path)" in _rec21
          and "tmp.replace(path)" not in _rec21, "")
    check("P2-5 _recalc_expiry 写回失败降级为 return False（不让请求变 500）",
          "return False" in _rec21 and "return True" in _rec21, "")
    check("P2-5 调用方按写回结果如实提示，不误报「已更新到期时间」",
          "已续期 %s（有效期显示没能写回" in _wb21, "")

    # --- P3-1 / P3-2 / P3-6：switcher_common ---
    check("P3-1 错误审计用 audit_source(path)（否则 /api/wbai/* 出错记成 [wb]）",
          "audit(self.AUDIT_DIR, self.audit_source(path)," in _cm21, "")
    check("P3-2 do_GET 异常把 path 传给 _handle_request_error（否则 GET 侧零审计）",
          "self._handle_request_error(e, path)" in _cm21, "")
    check("P3-6 replace_with_retry 的 tries<=0 不再 raise None（TypeError）",
          "for i in range(max(1, int(tries))):" in _cm21, "")

    # --- P3-16：spec 带上 window_state_guard.py ---
    check("P3-16 spec 的 datas 含 window_state_guard.py（exe 用户也要能跑它）",
          "('window_state_guard.py', '.')" in src_spec21, "")

    # --- P2-10：首屏就绪守卫 ---
    check("P2-10 renderAll 加就绪守卫（数据没到不渲染，避免首屏闪假空态）",
          "if(!lastCurrent && !(lastList && lastList.length)) return;" in src_tpl21, "")

    # --- P2-12：import_token 原子落盘 ---
    _it21c = _code21(src_it21)
    check("P2-12 import_token 用临时文件 + os.replace 原子落盘",
          "def _atomic_write_text(" in _it21c and "os.replace(tmp, path)" in _it21c
          and "out.write_text(" not in _it21c, "")

    # --- P2-12 / P3-6 行为验证（真跑，不只看源码） ---
    import import_token as _it21
    with tempfile.TemporaryDirectory() as _td21:
        _p21 = Path(_td21) / "cfg.json"
        _p21.write_text('{"old": 1}', encoding="utf-8")
        _it21._atomic_write_text(_p21, '{"new": 2}\n')
        check("P2-12 行为：_atomic_write_text 覆盖成功且内容正确",
              json.loads(_p21.read_text(encoding="utf-8")) == {"new": 2}, "")
        _left21 = sorted(p.name for p in Path(_td21).iterdir())
        check("P2-12 行为：写完不留临时文件", _left21 == ["cfg.json"], str(_left21))

        # 源文件不存在 → Path.replace 必抛 OSError，用来验 tries<=0 的异常类型
        try:
            common.replace_with_retry(Path(_td21) / "nope.txt", Path(_td21) / "dst.txt",
                                      tries=0, delay=0)
            _exc21 = None
        except BaseException as _e21:  # noqa: BLE001
            _exc21 = _e21
        check("P3-6 行为：tries=0 抛的是 OSError（修复前 raise None → TypeError）",
              isinstance(_exc21, OSError), repr(_exc21))

    print("\n== 22. 第三轮修复回归（AUDIT_2026-09-24.md 未修项 C/D/F 组）==")
    _cm22 = _code21((BIN / "switcher_common.py").read_text(encoding="utf-8"))
    _wb22 = _code21((BIN / "wb_ui_server.py").read_text(encoding="utf-8"))
    _tw22 = _code21((BIN / "tw_ui_server.py").read_text(encoding="utf-8"))
    src_tpl22 = (BIN / "ui_template.html").read_text(encoding="utf-8")
    src_hub22 = (BIN / "ui_hub.html").read_text(encoding="utf-8")

    # --- P1-5：Trae 切号先清旧设备键 ---
    check("P1-5 Trae 切号前先清掉本机旧设备键（否则设备凭据残留 → device not match）",
          'for k in [k for k in cur if str(k).startswith("iCubeAuthInfo://icube-dc")]:'
          in _tw22, "")

    # --- P2-6：回源 single-flight ---
    check("P2-6 积分/签到回源按通道串行化（同一通道只让一个真去扇出）",
          "def _fetch_lock(" in _wb22
          and '_fetch_lock("checkin", ch.key)' in _wb22
          and '_fetch_lock("credits", ch.key)' in _wb22, "")

    # --- P2-7：目录签名缓存 ---
    check("P2-7 list_accounts / uid_nickname_map 按目录签名缓存（消除重复读盘）",
          "def _auth_dir_sig(" in _wb22 and "_LIST_ACCOUNTS_CACHE" in _wb22
          and "_UID_NICK_CACHE" in _wb22, "")

    # --- P3-3 / P3-4 / P3-5 ---
    check("P3-3 端口区间探测只剩一份实现（_probe_all 复用 probe_instance_range）",
          "return probe_instance_range(" in _cm22 and "exclude_pid" in _cm22, "")
    _op22 = _cm22.split("def open_page")[1].split("\ndef ")[0]
    check("P3-4 open_page 先 open 成功、再 mark_page_opened（失败不占 30s 宽限期）",
          _op22.index("webbrowser.open(url)") < _op22.index("mark_page_opened()"), "")
    check("P3-5 tasklist 兜底用 csv.reader（进程名含逗号不再整行丢弃）",
          "for row in csv.reader(text.splitlines()):" in _cm22
          and "import csv" in _cm22, "")

    # --- P3-7 / P3-8 / P3-9 ---
    check("P3-7 winreg 句柄全部 CloseKey（不再只开不关）",
          _wb22.count("winreg.CloseKey(") >= 2, "")
    check("P3-8 _NullLog 只丢弃不缓存（内存不再只增不减）",
          "self._buf" not in _wb22, "")
    check("P3-9 build_ui_context 用 ch.title，不再硬编码标题",
          'common_ctx["TITLE"] = ch.title' in _wb22, "")

    # --- P3-10 ~ P3-13（Trae 侧） ---
    check("P3-10 Trae 进程枚举复用 common.list_processes（不再自己跑 tasklist）",
          "table = common.list_processes()" in _tw22, "")
    check("P3-11 Trae load_config 包进 try（两份 config 都不在时不再 500/整批中断）",
          "读取 config.json 失败，无法续期" in _tw22, "")
    check("P3-12 Trae read_bytes 包进 try（被占用时给可读错误而非 500）",
          "读取本机登录态失败（可能被 Trae 占用）" in _tw22, "")
    check("P3-13 Trae target_uid 为空时降级比对密文（不再假阴性误报「未采纳」）",
          "密文不一致" in _tw22, "")

    # --- P2-13：脚本编码与退出码 ---
    check("P2-13 两个启动器脚本末尾显式 exit /b（不再恒返回 0）",
          (BIN / "workbuddy_switcher.cmd").read_text(encoding="utf-8").rstrip()
          .endswith("exit /b %RC%")
          and (BIN / "trae_switcher.cmd").read_text(encoding="utf-8").rstrip()
          .endswith("exit /b %RC%"), "")
    check("P2-13 含中文的 cmd 加 chcp 65001（GBK 控制台不再乱码）",
          "chcp 65001" in (BIN / "refresh_all.cmd").read_text(encoding="utf-8")
          and "chcp 65001" in (BIN / "trae_refresh_all.cmd").read_text(encoding="utf-8"), "")
    check("P2-13 install_refresh_task.ps1 带 UTF-8 BOM（PowerShell 5.1 才按 UTF-8 解）",
          (BIN / "install_refresh_task.ps1").read_bytes().startswith(b"\xef\xbb\xbf"), "")

    # --- P3-17 / P3-19 / P3-20 / P3-21（前端，只用正向断言避开注释） ---
    check("P3-17 ttlTag 兜底不再写死天数（拿不到就说「长期」）",
          "const tail = days ? days+'天' : (byDays ? ttlDays+'天' : '');" in src_tpl22, "")
    check("P3-17 toast 调用处不再二次 esc()（toast 内部用的是 textContent）",
          "toast('迁移预览不可用，将只切换账号：'+(pv.message||''),'err')" in src_tpl22, "")
    check("P3-19 ui_hub 离线提示地址用 location.origin 填（端口顺延后不再指错）",
          "['offUrl', 'viewOffUrl'].forEach" in src_hub22, "")
    check("P3-20 renderAll 用 rAF 合并同一帧内的多次全量渲染",
          "_renderPending" in src_tpl22 and "requestAnimationFrame" in src_tpl22, "")
    check("P3-21 积分查询失败时标记陈旧并显式提示",
          "creditsStale=true;" in src_tpl22 and "以下为上次查询结果" in src_tpl22, "")

    # --- P2-7 行为：签名不变时命中缓存 ---
    _ch22 = wb.channel("wb")
    wb._LIST_ACCOUNTS_CACHE.clear()
    _a1_22 = wb.list_accounts(_ch22)
    check("P2-7 行为：list_accounts 首跑会填缓存",
          _ch22.key in wb._LIST_ACCOUNTS_CACHE, str(list(wb._LIST_ACCOUNTS_CACHE)))
    check("P2-7 行为：签名未变时命中缓存且内容一致",
          wb.list_accounts(_ch22) == _a1_22, "")
    wb._LIST_ACCOUNTS_CACHE.clear()

    print("\n== 23. 补漏修复回归（P2-11 / P3-15）==")
    src_wsg23 = (BIN / "window_state_guard.py").read_text(encoding="utf-8")
    src_chk23 = (BIN / "check_exe_datadir.py").read_text(encoding="utf-8")
    check("P2-11 window_state_backups 有保留上限（不再只增不删）",
          "def _prune_backups(" in src_wsg23 and "_prune_backups()" in src_wsg23
          and "BACKUP_KEEP" in src_wsg23, "")
    check("P2-11 独立分发的脚本不依赖 switcher_common（自己实现裁剪）",
          "import switcher_common" not in _code21(src_wsg23), "")
    check("P3-15 check_exe_datadir 只结束本次 Popen 的进程树（不再按镜像名全杀）",
          '"/PID", str(proc.pid), "/T", "/F"' in src_chk23
          and '"/IM"' not in _code21(src_chk23), "")

    # --- P2-11 行为：裁剪保留最近 keep 份 ---
    import window_state_guard as _wsg23
    with tempfile.TemporaryDirectory() as _td23:
        _old_bdir23 = _wsg23.BACKUP_DIR
        _wsg23.BACKUP_DIR = Path(_td23)
        try:
            for _i23 in range(5):
                _p23 = Path(_td23) / ("wb.window-state.2026010%d-x.json" % _i23)
                _p23.write_text("{}", encoding="utf-8")
                os.utime(_p23, (1000 + _i23, 1000 + _i23))     # mtime 递增
            _rm23 = _wsg23._prune_backups(keep=2)
            _left23 = sorted(p.name for p in Path(_td23).iterdir())
            check("P2-11 行为：裁剪到最近 keep 份（删 3 留 2）",
                  _rm23 == 3 and len(_left23) == 2,
                  "删除 %d，剩 %s" % (_rm23, _left23))
        finally:
            _wsg23.BACKUP_DIR = _old_bdir23

    print("\n== 24. 重复实现防漂移（P3-18 / P3-14）==")
    src_tpl24 = (BIN / "ui_template.html").read_text(encoding="utf-8")
    src_hub24 = (BIN / "ui_hub.html").read_text(encoding="utf-8")
    # 合并启动器后只剩一份，产品差异集中在 _PRODUCTS 表里
    _app24 = _code21((BIN / "ui_app.py").read_text(encoding="utf-8"))

    # --- P3-14：_port_alive 死代码已删（全文只有定义、零调用） ---
    check("P3-14 启动器里的 _port_alive 死代码已删",
          "def _port_alive" not in _app24, "")
    check("P3-14 真正要保留的 _wants_server_cli 仍在（别删过头）",
          "def _wants_server_cli" in _app24, "")
    check("P3-14 产品表里的 CLI 动作表都在（不再是两份重复实现）",
          _app24.count('"cli_actions"') >= 2, "")

    # --- P3-18：两份前端里「必须一致」的实现，逐特征钉住 ---
    # ⚠️ 只钉**安全语义**，不钉空格/var-const 这类无意义差异：
    #    esc() 的转义规则一旦漂移就是注入面（P2-9 正是同一函数里两处写法不一致）。
    check("P3-18 两份前端的 esc() 用同一套转义正则",
          ".replace(/[&<>\"']/g" in src_tpl24 and ".replace(/[&<>\"']/g" in src_hub24, "")
    for _pair24 in ("'&':'&amp;'", "'<':'&lt;'", "'>':'&gt;'", "'&quot;'", "'&#39;'"):
        check("P3-18 两份前端的 esc() 映射一致：%s" % _pair24,
              _pair24 in src_tpl24 and _pair24 in src_hub24, "")

    # closeThisTab()：用户可见的失败文案与兜底结构必须一致，否则两处提示会两样
    for _need24 in ("window.close()", "document.getElementById('dupBar')",
                    ".dup-txt span",
                    "浏览器不允许脚本关闭这个标签页，请手动关闭（Ctrl+W）。"):
        check("P3-18 两份前端的 closeThisTab() 一致：%s" % _need24,
              _need24 in src_tpl24 and _need24 in src_hub24, "")

    # 心跳：URL **有意不同**（入口页写死 /api/page-alive，视图层用 API.pageAlive 常量），
    # 所以只钉「间隔」和「可见性补拍」这两条真正该一致的行为。
    check("P3-18 两份前端的心跳间隔都是 5 秒",
          "setInterval(beat, 5000)" in src_tpl24 and "setInterval(beat, 5000)" in src_hub24, "")
    check("P3-18 两份前端都在 visibilitychange 时补拍心跳",
          "visibilitychange" in src_tpl24 and "visibilitychange" in src_hub24, "")

    # --- 频道名是**有意不同**（入口页 / 独立打开的国服视图 / 独立打开的国际服视图），
    #     钉住"三个频道各管各的"，避免将来有人把它们"统一"掉。 ---
    check("P3-18 重复页面探测的三个频道名保持各管各的（有意设计，不是漂移）",
          "'wb_switcher_hub'" in src_hub24
          and "new BroadcastChannel('wb_switcher_page_'+(UI.view||'wb'))" in src_tpl24
          and "if(window.self!==window.top) return;" in src_tpl24, "")

    print("\n== 25. P2-6 回源锁：行为验证（single-flight + 等锁超时）==")
    _ch25 = wb.channel("wb")
    _calls25 = []
    _wbc25 = wb.wb          # wb_ui_server 里的 workbuddy_checkin
    _real25 = (wb.channel_cfg, wb.list_accounts, wb.query_credits,
               _wbc25.session_from_info_file)
    wb._CREDITS_CACHE.pop(_ch25.key, None)
    wb.channel_cfg = lambda c: {"endpoint": "http://127.0.0.1:1"}     # 桩掉网关地址
    wb.list_accounts = lambda c: [
        {"file": "a.info", "uid": "u1", "nickname": "A", "ok": True},
        {"file": "b.info", "uid": "u2", "nickname": "B", "ok": True},
    ]
    _wbc25.session_from_info_file = lambda p: {"stub": True}

    def _fake_credits25(acc, cfg):
        _calls25.append(1)
        time.sleep(0.3)                 # 模拟一次外部请求的耗时
        return {"ok": True, "total_remain": 1.0, "items": []}

    wb.query_credits = _fake_credits25
    try:
        # ① 并发两次 force=True：single-flight 应让底层查询只跑**一轮**
        #    （2 个账号 = 2 次；若失效会是 4 次）
        _out25 = [None, None]
        _ths25 = [threading.Thread(target=lambda i=i: _out25.__setitem__(
            i, wb.credits_snapshot(force=True, ch=_ch25))) for i in (0, 1)]
        for _t25 in _ths25:
            _t25.start()
        for _t25 in _ths25:
            _t25.join(timeout=30)
        check("P2-6 行为：同通道并发回源只扇出一次（2 账号 → 2 次查询，不是 4 次）",
              len(_calls25) == 2, "query_credits 实际调用 %d 次" % len(_calls25))
        check("P2-6 行为：两个并发调用都拿到了结果",
              all(r is not None and r.get("ok") for r in _out25), str(_out25))

        # ② 等锁超时：手动占住锁（模拟"第一个请求还卡在慢网关上"），
        #    验证第二个请求**不会被无限挂住**，而是超时后自己扇出。
        wb._CREDITS_CACHE.pop(_ch25.key, None)
        _calls25.clear()
        _old_to25 = wb.FETCH_LOCK_TIMEOUT
        wb.FETCH_LOCK_TIMEOUT = 0.3
        _hold25 = wb._fetch_lock("credits", _ch25.key)
        _hold25.acquire()
        try:
            _t0_25 = time.time()
            _r25 = wb.credits_snapshot(force=True, ch=_ch25)
            _el25 = time.time() - _t0_25
        finally:
            _hold25.release()
            wb.FETCH_LOCK_TIMEOUT = _old_to25
        check("P2-6 行为：等锁超时后不再干等，退化为自己扇出（不挂死）",
              bool(_r25.get("ok")) and _el25 < 5,
              "耗时 %.2fs（等锁上限 0.3s + 扇出 ~0.3s）" % _el25)
        check("P2-6 行为：超时路径确实自己扇出了（2 次查询）",
              len(_calls25) == 2, "query_credits 实际调用 %d 次" % len(_calls25))
    finally:
        (wb.channel_cfg, wb.list_accounts, wb.query_credits,
         _wbc25.session_from_info_file) = _real25
        wb._CREDITS_CACHE.pop(_ch25.key, None)

    print("\n== 26. 统一启动器一次性令牌（第二十章新发现）==")
    _app26 = _code21((BIN / "ui_app.py").read_text(encoding="utf-8"))
    _TOK26 = "srv.Handler.TOKEN = common.new_token()"
    check("统一启动器设了一次性令牌（wb 原先漏了 → 写端点只剩 Host 守卫）",
          _TOK26 in _app26, "")
    check("令牌必须在 bind_server 之前设（否则服务一起就漏保护）",
          _app26.index(_TOK26) < _app26.index("common.bind_server("), "")
    check("启动器不走 serve()，所以不能指望 serve() 里的 use_token 逻辑",
          "common.bind_server(" in _app26, "")
    # 自检工具必须能另起实例：守卫探「请求端口区间 ∪ 默认端口区间」，用户开着切换器时
    # 传 --port 也躲不开 → 需要一个显式的跳过开关（实测踩到 check_exe_datadir 假 FAIL）。
    check("启动器提供自检用的 --no-single-instance 开关",
          "--no-single-instance" in _app26 and "skip_guard" in _app26, "")
    check("check_exe_datadir 起探针实例时带上了该开关",
          "--no-single-instance" in (BIN / "check_exe_datadir.py").read_text(encoding="utf-8"), "")

    print("\n== 27. 版本戳覆盖启动器（第二十章 P3）==")
    check("source_version 支持 extra_paths",
          "extra_paths" in _code21((BIN / "switcher_common.py").read_text(encoding="utf-8")), "")
    for _f27 in ("wb_ui_server.py", "tw_ui_server.py"):
        _src27 = _code21((BIN / _f27).read_text(encoding="utf-8"))
        check("%s 把统一启动器 ui_app.py 传进 extra_paths" % _f27,
              'extra_paths=(str(Path(__file__).with_name("ui_app.py")),)' in _src27, "")

    # 行为：extra_paths 参与取最大 mtime
    with tempfile.TemporaryDirectory() as _td27:
        _a27 = Path(_td27) / "srv.py"
        _b27 = Path(_td27) / "app.py"
        _a27.write_text("", encoding="utf-8")
        _b27.write_text("", encoding="utf-8")
        _t0_27 = 1700000000                       # 固定时间戳，避免时区/epoch 差异
        os.utime(_a27, (_t0_27, _t0_27))
        os.utime(_b27, (_t0_27 + 100, _t0_27 + 100))     # 启动器晚 100 秒
        _v_only27 = common.source_version(str(_a27), "t")
        _v_extra27 = common.source_version(str(_a27), "t", extra_paths=(str(_b27),))
        _v_b27 = common.source_version(str(_b27), "t")
        check("P3 行为：extra_paths 参与取最大 mtime（改启动器能被版本戳看见）",
              _v_extra27 == _v_b27 and _v_extra27 != _v_only27,
              "only=%s extra=%s" % (_v_only27, _v_extra27))
        check("P3 行为：extra_paths 里的文件不存在时跳过，不报错、结果不变",
              common.source_version(str(_a27), "t",
                                    extra_paths=(str(Path(_td27) / "nope.py"),))
              == _v_only27, "")
        check("P3 行为：主文件也不存在时回退（不抛异常）",
              isinstance(common.source_version(str(Path(_td27) / "nope2.py"), "t"), str), "")

    print("\n失败项：%s" % (FAIL or "无"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
