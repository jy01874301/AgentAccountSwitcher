#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_live_token_ui.py —— 验证「加密登录态 + 内存凭据」这条链路在**页面上**显示正确。

## 为什么需要它

WorkBuddy 5.6.2+ 把 `.info` 里的 token 加密了，本工具改为从**客户端进程内存**读明文
（`live_token.py`）。这条链路要成立，得同时满足：

  1. `current_account()` 在加密登录态下走 live 分支，用内存里的昵称/到期时间；
  2. 前端 `renderCurrent()` 读 `c.live`，显示「当前账号：<昵称>」+ 剩余天数；
  3. **统一入口页 `ui_hub.html` 的侧边栏**也读 `c.live` —— 它是独立的一份前端，
     只验 `/wb` 会漏掉用户实际看到的导航栏（09-23 就漏了）。

`smoke_test.py` 的 ⑦e 组只钉到第 1 步（接口层的 JSON 字段）。这里补第 2、3 步：
**真的把两个页面都渲染出来**，断言 DOM 里出现昵称、且不出现实现细节文案。

## 怎么做到不依赖客户端

只把 `wb_ui_server.live_session` 换成桩，其余全走真实代码路径 ——
真实 `/api/current`、真实 `ui_template.html`、真实前端 JS、真实无头浏览器。
所以国服客户端没在跑时也能验证（客户端只在"真读内存"时才需要）。

## 用法

    python check_live_token_ui.py           # 桩模式：不依赖客户端，验前端分支
    python check_live_token_ui.py --real    # 真模式：真读客户端进程内存再渲染
                                            #（客户端没在跑会跳过）
退出码 0 = 通过，1 = 有失败项。
"""
import datetime
import json
import re
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import switcher_common as common          # noqa: E402
import wb_ui_server as wb                 # noqa: E402

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

# 桩：与真实内存读到的那份同形状（见 MEMORY.md「静态字段保护」一节）
FAKE = {
    "access_token": "eyJhbGciOiJSUzI1NiJ9.stub.stub",
    "refresh_token": "eyJhbGciOiJSUzI1NiJ9.stub.stub",
    "uid": "55555555-5555-4555-8555-555555555555",
    "nickname": "account-e",
    "expires_at": datetime.datetime(2026, 11, 17, 12, 44, 32, tzinfo=datetime.timezone.utc),
    "refresh_expires_at": None,
    "token_source": "oneid_login",
    "source_pid": 4242,
    "scanned_pids": [4242],
}

FAIL = []


def visible_dom(dom):
    """去掉 <script> / <style> 块，只留**会渲染出来的**部分。

    ⚠️ 这一步不能省。前端把降级文案写成 JS 兜底字面量
    （`esc(c.reason||'凭据已被客户端加密，本工具读不到 token')`），
    这些字符串**永远躺在 <script> 里**，所以「页面不该出现 X」这类断言直接查
    `dom` 会恒假 —— 本会话为此踩了两次（先是「桌面端当前未登录」，再是
    「凭据已被客户端加密」）。查 `visible_dom(dom)` 才是查用户真能看到的东西。
    """
    return re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", dom, flags=re.S | re.I)


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> %s" % (extra,) if extra != "" else ""))
    if not cond:
        FAIL.append(name)


def find_edge():
    for p in EDGE_CANDIDATES:
        if Path(p).is_file():
            return p
    return None


def main():
    real_mode = "--real" in sys.argv[1:]
    # --no-live：把 live_session 桩成"读不到"（客户端没在跑的情形），
    # 验证降级显示仍然说得出"是谁"，而不是报「桌面端当前未登录」。
    no_live = "--no-live" in sys.argv[1:]
    mode = "真实内存" if real_mode else ("降级（内存读不到）" if no_live else "桩")
    print("== 加密登录态 + 内存凭据：页面渲染验证（%s）==" % mode)
    edge = find_edge()
    if not edge:
        print("跳过：本机没找到 Edge，无法做无头渲染验证")
        return 0

    # 桩替换，其余全真。`live_session` 是模块级名字，不还原会让后续同进程调用
    # 拿到假凭据，所以恢复动作放 finally。
    real_live = wb.live_session
    if no_live:
        wb.live_session = lambda ch, expect_uid=None: None
    elif not real_mode:
        wb.live_session = lambda ch, expect_uid=None: FAKE
    elif not wb.live_session(wb.channel("wb")):
        # --real：客户端没在跑就没有可验证的真实凭据
        print("跳过：国服客户端没在跑，读不到真实内存凭据（先启动客户端再试）")
        return 0
    wb.Handler.TOKEN = None          # 关掉一次性令牌，方便本机自查
    srv, port = common.bind_server(wb.Handler, 8799)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/current" % port, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        cur = (data.get("current") or [{}])[0]
        check("/api/current 标记 encrypted=True（登录态确实被加密）",
              cur.get("encrypted") is True, cur.get("reason"))
        check("/api/current 的 live 标记与预期一致",
              cur.get("live") is (not no_live), cur.get("live"))
        if real_mode:
            # 真模式：昵称必须与客户端登录的账号一致，且 ttl_days 算得出来
            check("真实凭据的 uid 是明文 uid（与文件里的 uid 一致）",
                  bool(cur.get("uid")) and cur.get("uid") == wb.plain_uid(
                      wb._read_info(wb.channel("wb").desktop_info) or {}),
                  cur.get("uid"))
            check("真实凭据能算出签发天数（token_source 缺失时靠 exp-iat 兜底）",
                  bool(cur.get("ttl_days")), cur.get("ttl_days"))
        elif no_live:
            # 内存也读不到 → 昵称只能靠「明文 uid 在账号库里反查」。
            # ⚠️ **不要断言它等于某个具体昵称**：这台机器上当前登录的是哪个账号会变
            # （09-23 就从「account-e」切到了「account-c」，把原来钉死 'account-e' 的断言弄挂了 ——
            # 那不是代码 bug，是断言依赖了环境）。这里断言不变量：有昵称、且它
            # 既不是 uid 前缀也不是文件名，说明确实反查到了账号库里的真昵称。
            _n = cur.get("nickname") or ""
            _u = cur.get("uid") or ""
            check("/api/current 的昵称取自账号库反查（不是 uid 前缀、不是文件名）",
                  bool(_n) and _n != _u[:8] and not _n.endswith(".info"),
                  "%s（uid=%s…）" % (_n, _u[:8]))
        else:
            # 桩模式：昵称必须来自「内存凭据」那份（FAKE），不是账号库反查
            check("/api/current 的昵称取自内存凭据（不是 uid 前缀、不是文件名）",
                  cur.get("nickname") == FAKE["nickname"], cur.get("nickname"))
        check("/api/current 的 reason 说明凭据来路",
              cur.get("reason") == (wb.ENCRYPTED_REASON if no_live else wb.LIVE_REASON),
              cur.get("reason"))

        dom = subprocess.run(
            [edge, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-proxy-server",
             "--virtual-time-budget=15000", "--dump-dom",
             "http://127.0.0.1:%d/wb" % port],
            capture_output=True, timeout=180).stdout.decode("utf-8", "replace")
        check("无头渲染拿到了页面（DOM 非空）", len(dom) > 5000, "len=%d" % len(dom))
        vis = visible_dom(dom)          # 只查会渲染出来的部分，见 visible_dom 的注释

        if no_live:
            # 内存也读不到 → **应该**把降级原因说出来，否则用户以为客户端掉线了
            check("降级时显示「当前账号：<昵称>」而不是「桌面端当前未登录」",
                  '<div class="empty">当前账号：' in vis
                  and "桌面端当前未登录" not in vis, "")
            check("降级时把原因说出来（登录态被加密、读不到 token）",
                  "凭据已被客户端加密" in vis, "")
            check("降级时不显示「当前」标签行（没有有效期就不编数据）",
                  '<div class="name">%s ' % cur.get("nickname") not in vis, "")
        else:
            # 关键：内存读到凭据时，界面必须和明文登录态**没有任何可见差别** ——
            # 「凭据读自客户端进程内存」这类实现细节不该出现在页面上。
            check("当前账号行照常渲染（昵称 + 「当前」标签，不是降级空态）",
                  ('<div class="name">%s ' % cur.get("nickname")) in vis
                  and "tag-active" in vis
                  and '<div class="empty">当前账号：' not in vis,
                  cur.get("nickname"))
            check("页面不出现「读自客户端进程内存」这类实现细节文案",
                  wb.LIVE_REASON not in vis, "")
            check("页面不出现「凭据已被客户端加密」的降级区块",
                  "凭据已被客户端加密" not in vis, "")
            check("当前账号行显示总剩余积分（uid→file 映射没断）",
                  "总剩余积分" in vis, "")

        # 统一入口页（`/` → ui_hub.html）的侧边栏是**另一份前端代码**，它同样会把
        # `c.reason` 直接显示出来。只验 `/wb` 会漏掉用户真正看到的导航栏
        # —— 09-23 用户报的就是这里：「当前账号已经读出信息了，但导航栏上显示
        # 凭据读自客户端进程内存（…）」。
        hub = subprocess.run(
            [edge, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-proxy-server",
             "--virtual-time-budget=15000", "--dump-dom",
             "http://127.0.0.1:%d/" % port],
            capture_output=True, timeout=180).stdout.decode("utf-8", "replace")
        check("入口页无头渲染拿到了页面（DOM 非空）", len(hub) > 5000, "len=%d" % len(hub))
        hvis = visible_dom(hub)
        if no_live:
            # 内存也读不到 → 侧边栏**应该**把原因说出来（和 /wb 页同一口径）。
            # ⚠️ 这条分支下不能再断言「不出现加密文案」—— 那时它正是该出现的东西。
            check("降级时入口页侧边栏说明原因（不是假装已登录）",
                  wb.ENCRYPTED_REASON in hvis, "")
            check("降级时入口页侧边栏不出现内存实现细节文案",
                  wb.LIVE_REASON not in hvis, "")
        else:
            check("入口页侧边栏不出现实现细节文案", wb.LIVE_REASON not in hvis, "")
            check("入口页侧边栏不出现「凭据已被客户端加密」的降级文案",
                  "凭据已被客户端加密" not in hvis, "")
            check("入口页侧边栏显示「当前：<昵称>」（与 /wb 页口径一致）",
                  "当前：<b>%s</b>" % cur.get("nickname") in hvis, cur.get("nickname"))

        # 当前账号的 uid 必须能映射回账号库文件：
        #   ① 提示里显示「账号库中：xxx」（降级态提示被清空，跳过这条）
        #   ② 列表里不再重复出现它 —— **降级态也必须成立**，否则列表会多一条重复行
        cur_file = next((a.get("file") for a in wb.list_accounts("wb")
                         if a.get("uid") and a.get("uid") == cur.get("uid")), "")
        check("当前账号的明文 uid 能在账号库里定位到文件（uid→file 映射生效）",
              bool(cur_file), cur_file)
        if cur_file:
            if not no_live:
                check("提示里显示「账号库中：<文件>」",
                      "（账号库中：%s）" % cur_file in vis, cur_file)
            check("账号列表里不重复出现当前账号（filter 生效）",
                  'data-file="%s"' % cur_file not in vis, cur_file)
    finally:
        srv.shutdown()
        srv.server_close()
        wb.live_session = real_live

    print("\n失败项：%s" % (FAIL or "无"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
