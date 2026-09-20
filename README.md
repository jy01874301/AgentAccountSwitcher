# WorkBuddy 账号切换器（wb_switcher）

一个本地小工具，用于在**同一台机器**上的多个腾讯 WorkBuddy 账号之间一键切换登录态。
通过替换 WorkBuddy 桌面端本地的登录态文件（`.info`）实现账号切换，无需反复输手机号/验证码，
从而让多个账号共用在同机客户端上的签到积分。

> 配套：本工具只负责**切换账号**。每日签到与 token 续期的具体协议逻辑在
> 上级项目的 [`自动签到`](../自动签到/) 中，本工具以只读方式复用其解析/续期函数。

---

## 目录结构

```
wb_switcher/
├── wb_ui_server.py        # 后端：账号读写/切换 + 极简本地 HTTP 服务（127.0.0.1:8765）
├── switcher_common.py     # 两个切换器共用的 HTTP 骨架 / 文件锁 / 备份裁剪 / 端口避让
├── ui_hub.html            # 统一入口页：左侧侧边导航栏，两个管理入口（国服 / 国际服）
├── ui_template.html       # 两个管理视图共用的前端模板（后端按通道渲染后返回）
├── wb_ui_app.py           # 桌面版启动器（pywebview 窗口，可打包 exe）
├── workbuddy_switcher.cmd # Windows 启动脚本（双击即用）
├── wb_auth/               # 国服账号库，放置 *.info 登录态文件（唯一真源，见下）
│   ├── workbuddy-jhan.info
│   ├── workbuddy-Maggie ya.info
│   ├── workbuddy-wtnong.info
│   ├── workbuddy-wtnong1.info
│   └── workbuddy-星空.info
├── wbai_auth/             # 国际服（WorkBuddyAI）账号库，同样放 *.info
├── tw_ui_server.py / tw_ui_app.py / trae_switcher.cmd  # Trae 姊妹工具，见 README_Trae.md
├── WorkBuddySwitcher.spec / TraeSwitcher.spec  # 桌面版打包配置（pyinstaller <spec> --noconfirm）
├── import_token.py        # Trae 凭据导入（别机 tokens / storage.json → config.json）
├── check_ttl.py           # 登录态体检：签发通道 / 剩余天数 / accessToken 完整性（只读）
├── check_exe_datadir.py   # 冻结态 exe 的数据目录基准自检（双探针法，重新打包后跑）
├── window_state_guard.py  # 客户端主窗口「自动还原」诊断与修复（见「窗口状态守卫」一节）
├── smoke_test.py          # 回归自检：裁剪 / 文件锁 / 端口 / 接口 / 令牌校验（不碰真实登录态）
├── refresh_all.cmd / trae_refresh_all.cmd      # 全量续期（计划任务用，支持 /nopause）
├── install_refresh_task.ps1  # 注册「每 N 小时全量续期」计划任务（-Trae / -Remove / -Hours）
└── README.md
```

---

## 工作原理

WorkBuddy 桌面端的本地登录态保存在：

```
%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info
```

这是一个明文 JSON（`account{uid,nickname,...}` + `auth{accessToken,refreshToken,domain,expiresAt,...}`），
桌面端通过文件 watcher 自动采纳外部变更。切换器就是围绕这个文件做“偷梁换柱”。

一次**切换**（`switch_account`）的步骤：

1. 校验 `wb_auth\<目标>.info` 存在且可解析；
2. 确保桌面端 auth 目录存在；
3. 把当前正式文件 `workbuddy-desktop.info` 改名为带时间戳的备份
   （`workbuddy-desktop.<日期>.<pid>.<随机>.info`，与客户端的 `clean()` 命名一致，保留其可用登录态）；
4. 把目标账号内容写入为新的 `workbuddy-desktop.info`；**写盘失败会自动回滚** —— 把第 3 步
   的备份改回正式名，否则桌面端会停在"没有 `workbuddy-desktop.info`"的无登录态；
5. 清理残留的 `.logged-out` 登出标记；
6. **写后自校验**：重新读取桌面端文件，确认其中的 uid 已是目标账号（与 Trae 切换器一致），
   未通过则如实报失败，避免"报成功但实际没生效"；
7. **裁剪旧备份**（`prune_backups`）：纯善后，**必须放在第 6 步之后**。

> ⚠️ 第 7 步的位置很关键。它曾经夹在「旧文件已改名走」和「新文件还没写」之间 ——
> 一旦这里抛异常，`workbuddy-desktop.info` 就整个不存在了，客户端会认为没有登录态。
> 而这里**真的会抛**：见下面「安全删除被拒」一节。

**切换后的续期**：前端切换成功后会调用 `/api/refresh`，用目标账号的 `refreshToken` 向 WorkBuddy
服务器真正续期（`POST /v2/plugin/auth/token/refresh`），拿到全新 `accessToken / expiresAt`，
并把新有效期写回 `wb_auth\<目标>.info`，使列表里展示的剩余天数即刻更新。

> 切号生成的备份文件本身仍是服务端有效的登录态。识别当前账号时会扫描该目录下的 `.info`，
> 跳过已登出（带 `.logged-out` 标记）的，其余按修改时间降序，最新的标记为当前账号。
> **只认 `workbuddy-desktop` 及其切号备份**：同目录下还有 `workbuddy-desktop-ai.*`，
> 那是 WorkBuddy AI 客户端自己的登录态，本工具既不读取也不裁剪 —— 不过滤的话，
> 那边一写文件就会把"当前账号"顶掉，页面显示成别人的账号、切换按钮也永远变不成
> "当前账号"，看起来像切换失败。

### 切号时迁移「任务与项目」（可选）

点「切换到该账号」时，如果当前账号名下**有可迁移的本地数据**，会先弹一个确认框，
让用户决定是否把数据一并带到新账号。完整设计见 `DESIGN_account_migration.md`。

**核心事实：不需要搬对话正文。** 正文按「工作目录 + 会话ID」存在
`<数据目录>/projects/<项目>/<会话ID>.jsonl`，与账号无关；真正账号级的只有几处归属键。
所以迁移 = **改归属**，一条 `UPDATE` 加几处目录改名，毫秒级。

**客户端在跑时不用你手动退** —— 选「切换并迁移」后，流程会**自动关闭它**，迁移完成
再**按原状恢复**（原来在跑就重新打开，它会直接读到新账号的登录态）。

弹框里会有一条说明而不是拦阻，按钮始终可点：

> 检测到 WorkBuddy 客户端正在运行（workbuddy.exe、workbuddyai.exe）。
> 迁移前会自动关闭它，完成后重新打开。

关闭是**先礼后兵**：先给窗口发 `WM_CLOSE` 让它正常退出（避免丢未落盘的状态），
8 秒还没退再强杀，总共等 25 秒。

| 情况 | 处理 |
|---|---|
| 客户端本来没在跑 | 直接迁移，不碰它 |
| 正常退出 / 强杀成功 | 继续迁移 |
| **关不掉**（权限不足 / 被其它会话占用） | **整体中止，连切号也不做** —— 用户点的是"切换并迁移"，只切一半会让人以为迁移成功了 |
| **枚举不出进程** | 同样中止，不把"不知道"当成"已经关掉了" |
| **迁移中途失败** | 回滚到迁移前状态（见下），然后**照样把客户端恢复原状** |
| 迁移成功 | 重新打开客户端，它会读新账号的登录态 |

顺序是 `关客户端 → 切号 → 迁移 → 恢复客户端`，全部在 `wb-desktop` 文件锁内串行。
把关闭放在**切号之前**，是为了让切号也在干净状态下完成；放在校验之后，
是为了校验失败时不必白关一次。

迁移仍然跳过 `status='working'` 的会话，且如果客户端在你操作期间又被打开，
`migrate()` 会再检查一次并拒绝（关掉是尽力而为，不是一劳永逸）。

弹框里的选项与默认值：

| 选项 | 默认 | 说明 |
|---|---|---|
| 会话与任务 | 开 | 改 `sessions.user_id`；`status='working'` 的会话**跳过**（正在写，改了会撕裂） |
| 账号记忆 | 开 | `memory/<uid>_memory.md`；新账号已有则按日期分节**合并** |
| 账号级设置 | 开 | `storage/user-<uid>-<企业简称或个人>-personal/`；按键粒度合并，目标侧优先 |
| 连接器状态 | 开 | `connectors/<uid>/`；**必须重算 `userIdCheck`**，否则客户端会判定"文件不属于当前账号"并**删掉它** |
| 渠道配置 / 账号快照 | 固定开 | `settings.json` 的 `claw.users.<uid>`、`storage/skeleton/account-snapshot.json` |
| 迁移方式 | **移动** | 改归属到新账号；另有「共享」= 置空 `user_id`，两个账号都能看到并继续（见下） |

**「移动」vs「共享」**：`sessions` 的可见性谓词是 `(user_id = ? OR user_id = '')`，
所以**置空 `user_id` 会让所有账号都看得到同一个会话**，且写的是同一份 jsonl —— 不会分叉。
这适合"两个账号都要继续同一个任务"的场景。注意这个语义**只对 `sessions` 成立**；
`automations` 的空归属是 fail-closed 隐藏，语义相反。

**安全设计**：

- 动手前用 **SQLite Backup API** 做一致性快照（有 WAL 时裸 copy 会得到损坏的快照，
  回滚就无从谈起），另有文件快照；
- 每一步记进 `.migration-backup/<时间戳>/manifest.json`，失败按它**反向回滚**；
- 回滚也失败就**保留现场**并在错误里给出路径，不静默吞掉；
- 迁移**先切号后迁移**：迁移失败时用户至少已经在新账号上，不会出现
  "数据已归新账号、人却还登着旧账号"的更糟状态；
- 成功后只保留最近 3 份备份（里面含完整对话库快照）。

> ⚠️ 本机存在**两个结构完全相同的客户端数据目录**：
> `~/.workbuddy/`（= `workbuddy-desktop.info`，切换器管的）与
> `~/.workbuddy-ai/`（= `workbuddy-desktop-ai.info`，AI 端）。
> 迁移**按数据自身的归属标识**选目录（`account-snapshot.json` 的 uid → 库里有没有该 uid 的会话），
> **不信任 `WORKBUDDY_CONFIG_DIR`** —— 照抄它会迁移错目录，那是最坏的一类 bug。

命令行等价入口：

```bash
python wb_ui_server.py --migrate-preview workbuddy-jhan.info   # 只读扫描，看要迁多少
python wb_ui_server.py --switch workbuddy-jhan.info --migrate  # 切号并迁移（默认 move）
python wb_ui_server.py --switch x.info --migrate --migrate-mode share
```

### `--serve` 的开关

```bash
python wb_ui_server.py --serve                    # 起服务并打开页面（.cmd 用的就是这个）
python wb_ui_server.py --serve --no-open          # 只起服务，不开浏览器（脚本/测试用）
python wb_ui_server.py --serve --no-auth          # 关闭一次性令牌
python wb_ui_server.py --serve --port 8998        # 指定端口
```

> `--no-open` 是给脚本和测试用的。**不加它就会开浏览器** —— 之前用 `--serve` 做端到端
> 测试时，测试实例自己弹了一个 `http://127.0.0.1:8996/` 的页面出来。

### 客户端在运行时的行为与排查

**先明确一点：切号本身在客户端运行时是正常的。** 实测（客户端 12 个进程在跑）连续
切 4 次全部 `HTTP 200`、0.01–0.03s，且客户端**不会回写** `workbuddy-desktop.info`
（观察 5 秒 mtime 无变化）。

**被挡住的是「迁移」，不是切号** —— 这是设计使然：

| 环节 | 客户端在跑时 | 为什么 |
|---|---|---|
| 切号 | ✅ 正常 | 只替换一个文件；客户端有 watcher，会 reconcile |
| 续期 | ✅ 正常 | 只动本地账号库文件 |
| **迁移** | ❌ **拒绝** | 见下 |

迁移被拒的四个理由：

1. 客户端持有数据库连接与**内存态**，改完会被写回；
2. `app/session/Local Storage/leveldb/` 被 Chromium **文件锁**占用，改不了；
3. `sessions/<pid>.json` 是**进程租约**，会被覆盖；
4. `status='working'` 的会话正在追加写 `.jsonl`，改了会撕裂。

守卫在**任何写操作之前**中止（实测：连 `.migration-backup/` 目录都不会创建）。

#### 切号真的失败时，按这个顺序排查

| # | 可能原因 | 症状 | 怎么确认 | 怎么解决 |
|---|---|---|---|---|
| 1 | **文件被占用**（杀软 / 索引器 / 客户端持有句柄） | 提示"轮换旧登录态失败"/"写入新登录态失败"，带 `WinError 5/32` | `logs/switcher.log` 的 FAIL 行（异常现在也会写审计） | 退出客户端后重试；把 auth 目录加入杀软白名单 |
| 2 | **写后自校验竞争**：客户端 reconcile 时回写，我们读到旧账号 | 提示"写入后校验未通过：桌面端当前登录态仍为「X」" | 连续切两次看是否复现；看 `workbuddy-desktop.info` 的 mtime 是否被外部改动 | 重试一次；若稳定复现，退出客户端再切 |
| 3 | **accessToken 不完整**（粘贴时被截断） | 提示"accessToken 不完整…切换后必然 401" | 列表里该账号标为"不可用" | 重新从客户端导出完整的 `.info` |
| 4 | **一次性令牌不匹配** | 红 toast「请求失败：HTTP 401」 | 页面是从**另一个实例**加载的（见下） | 关掉多余实例，刷新页面 |
| 5 | **跑着旧代码的实例** | 功能缺失或行为不符预期 | `curl "http://127.0.0.1:8765/api/migrate-preview?name=x"` —— 返回 404 就是旧代码 | 关掉它重新启动 |
| 6 | **端口被别的程序占用**，服务顺延到 8766 | 打开的页面和预期不符 | `netstat -ano \| findstr 876` | 见 `DESIGN_single_instance.md` |

#### 一次快速体检

```bash
curl -s "http://127.0.0.1:8765/api/current"          # 当前账号对不对
curl -s "http://127.0.0.1:8765/api/migrate-preview?name=x"  # 404 = 旧代码
tail -20 logs/switcher.log                            # 失败原因
ls -la "%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth" | grep workbuddy-desktop
```

### 客户端窗口大小/位置被「自动还原」（窗口状态守卫）

**与本工具无关** —— 本项目既不读也不写窗口状态文件，也不重启客户端（已 grep 确认）。

两个客户端各把主窗口几何存一份，位置在**各自 userData 下**：

| 通道 | 产品 | 状态文件 |
|---|---|---|
| `wb` | WorkBuddy（国服） | `%USERPROFILE%\.workbuddy\app\window-state.json` |
| `wbai` | WorkBuddyAI（国际服） | `%USERPROFILE%\.workbuddy-ai\app\window-state.json` |

schema：`{"version":2,"bounds":{x,y,width,height},"isMaximized":bool,"isFullScreen":bool}`

三条关键事实（来自客户端 `app.asar` 内 `src/main/window/window-state.ts`）：

1. `bounds` = `win.getNormalBounds()` —— **最大化之前的「还原尺寸」，不是当前窗口尺寸**，单位 DIP（缩放 1.25 时 ×1.25 才是物理像素）。
2. 写时机：`resize` / `move` 后 **500ms 去抖**，全应用只有这一个写入方。
3. 读时机：**只在建窗时**（冷启动 / 崩溃重建）。恢复顺序 = 按 `bounds` 建窗（隐藏）→ `ready-to-show` 时 `maximize()` → `show()`。

**所以 `bounds` 就是「窗口被还原后的大小与位置」的唯一来源。** 它一旦被记成偏小的陈旧值，
窗口每次离开最大化状态（拖标题栏 / 双击标题栏 / `Win+↓` / 贴边 / 重启后的建窗窗口期）
都会缩到那个小尺寸。而它永远停在那个小值，是因为用户长期保持最大化 →
不产生新的非最大化几何 → `resize`/`move` 不触发 → 小值被永久钉死。

诊断与修复用 `window_state_guard.py`（纯标准库，独立 CLI，**未**打进 exe）：

```bash
python window_state_guard.py show                      # 看两个通道的现状与判定
python window_state_guard.py list                      # 列出可见窗口（定位用）
python window_state_guard.py capture --channel wbai    # 手动摆好后固化（推荐）
python window_state_guard.py pin --channel wbai --size 1600x1000 --maximized --apply --live
python window_state_guard.py check --fix --live        # 漂移则回写 + 就地修正运行中窗口
python window_state_guard.py watch --channel wbai --seconds 120   # 抓「谁什么时候改的」
```

`--apply` / `--fix` 会先备份到 `window_state_backups/`，再 tmp + `os.replace` 原子写入。
期望值存在 `window_state_golden.json`。建议登录时跑一次 `check --fix`，
让每次冷启动建窗读到的都是 pinned 值。

**`--live` 的意义**：客户端只在建窗时读状态文件，光改文件对**已在运行**的窗口无效。
`--live` 用 Win32 `SetWindowPlacement` 直接改窗口的 `rcNormalPosition`（**保持 `showCmd`
不变，不改变当前最大化状态，用户看不到跳变**），无需重启客户端。

> ⚠️ **顺序不能反**（实测踩过）：必须**先改窗口、再写文件**。反过来的话，
> `SetWindowPlacement` 触发的 `resize`/`move` 会让客户端 500ms 去抖后把**当时**的
> `getNormalBounds()` 落盘，正好冲掉刚写的值（实测被改回 812x607）。
> 工具里的 `apply_all()` 已按正确顺序实现并加了 1.2s 等待。

完整诊断过程与证据见 `AUDIT_window_state_2026-09-20.md`。

> ⚠️ DPI 约定：脚本**故意不调用** `SetProcessDpiAwareness`，保持 DPI-unaware，
> 这样 Win32 返回虚拟化坐标（= DIP），可与 Electron 的 `bounds` 直接比对。

### 单实例保护（已实现）

**同一时刻只允许一个实例。** 双击两次不会再起第二个：

```
[提示] 已有实例在运行（pid 21728，端口 8765），直接复用，不再启动第二个。
       页面地址：http://127.0.0.1:8765/
```

机制见 `DESIGN_single_instance.md`，要点：

- **Windows 命名互斥体**（`Local\WorkBuddySwitcher-wb` / `Local\TraeSwitcher-tw`）
  —— 进程正常退出**或被强杀/崩溃**都由内核自动释放，不留假锁。
  互斥体名带 source，所以两个切换器可以同时跑。
- **`GET /api/ping` 身份端点** —— 区分「我们的实例 / 别人的程序 / 空闲」。
  探测方按 `OUR_APPS` 白名单校验，不会把别人家的 `/api/ping` 当成自己。
- **顺序是先探端口、再拿互斥体**：探端口覆盖"升级前启动的旧实例不持互斥体"这个过渡期，
  互斥体挡"同时启动"的竞态。只做任一件都会漏。
- 版本不一致时会额外警告：

  ```
  [警告] 那个实例的版本是 'wb-20260918-140006'，当前是 'wb-20260918-140650'
         —— 它可能在跑旧代码。建议先关掉它再重新启动。
  ```

- 端口只在被**别的程序**占用时才顺延，且一定打印实际地址。
- **迁移的前置条件也包含这一条**：检测到另一个切换器实例时拒绝迁移
  （两个实例的内存态各自独立，同时操作会让状态错乱），且**不做任何写入**。

### 不重复打开同一地址的页面

单实例保护解决了"起两个服务"，但**页面**是另一回事：复用分支以前无条件
`webbrowser.open(url)`，于是每双击一次 `.cmd` 就多一个标签页。现在改成
**只在确实没有页面开着时才开**。

判断"有没有页面开着"靠两件事，且**由运行中的实例来回答**
（判断发生在刚启动的那个进程里，它自己的状态是空的，问本地等于没问）：

| 来源 | 含义 | 有效期 |
|---|---|---|
| 首页被请求 / 前端心跳 | 页面还在（前端每 5 秒打一次 `/api/page-alive`） | `PAGE_TTL` = 20s |
| 刚调过浏览器 | 页面可能还没加载完、还没发第一次心跳 | `PAGE_OPEN_GRACE` = 30s |

两者**分开判**，不取 max —— 早先写成 `(now - max(seen, opened)) < max(TTL, GRACE)`
等于让宽限期覆盖了心跳时限，标签页关掉后要多等 10 秒才认账。

行为：

```
第一次双击 → 起服务 + 打开页面
再双击     → [提示] 已有实例在运行（pid …），直接复用
            [提示] 已有页面在运行，不再新开标签页
            → 不产生新标签页 ✓

标签页被关掉后再双击（心跳超时）→ 重新打开 ✓
```

`/api/ping` 也带上 `page_open` 字段，方便排查。桌面版 exe 同理：
复用分支**不再开新窗口**，改为弹一个原生提示框告知地址（窗口版没有控制台，
只打印等于"双击没反应"）。

**如果还是出现了第二个页面**（例如手动又开了一个），后来者会自己认出来并显示提示条：

> 这个页面已经开着另一个标签页了 —— 为保持只有一个页面实例，建议关掉这一个。
> 数据两边是同一份，不会丢。 ［关闭此页］

实现用 `BroadcastChannel` 互相探测。⚠️ 诚实说明：浏览器**不允许脚本关闭不是它自己
打开的标签页**，所以「关闭此页」按钮会先尝试 `window.close()`，失败则提示按 `Ctrl+W`
手动关闭。**程序侧保证不再主动开第二个页面；手动开的那个无法被强制关掉。**

### ⚠️ 安全删除被拒会让请求"凭空消失"（已修）

这台机器的 Python 被注入了 WorkBuddy CLI 的**安全删除 shim**（`sitecustomize`）：任何删除
先过一遍批量删除守卫（`safe-delete-bulk-guard.cjs`）。守卫判定 `confirmRequired` /
`rejected` 时 `process.exit(2/3)`，Python 侧随即 **`raise SystemExit(1)`**。

`SystemExit` 继承自 **`BaseException` 而不是 `Exception`**，于是它：

1. 穿透 `prune_backups` 的 `except OSError`；
2. 再穿透 `do_POST` 的 `except Exception`（后者当时还把 `TimeoutError` 和连接异常一起
   当"客户端断开"处理，直接 `close_connection`）；
3. 结果：**服务端不发任何响应就断连** → 浏览器只看到 `TypeError: Failed to fetch`
   → 前端红色 toast「请求失败」→ 而**审计日志里一条记录都没有**。

症状就是「切号偶发提示失败，但查不到任何原因」。现在三层都补上了：

| 位置 | 修法 |
|------|------|
| `prune_backups` / `safe_unlink` | 连 `BaseException` 一起吞（只放行 `KeyboardInterrupt`）。裁剪只是善后，删不掉最多多留几份备份，**绝不允许因此让切号失败** |
| `switch_account` | 裁剪挪到写后自校验**之后**，再包一层兜底 |
| `BaseHandler.do_POST` / `do_GET` | 只有真正的连接类异常才算断连；`TimeoutError` 回 **504**、其余回 **500**，都带可读原因**并写审计** |

复现与回归：`smoke_test.py` 第 13 段（用假异常复刻 `SystemExit` / `TimeoutError`，
断言仍回 JSON 且审计有记录）。

> **本工具按「通道」接管登录态，一个进程同时服务两个通道**：
> `workbuddy-desktop.info`（**国服**，`www.workbuddy.cn`）与
> `workbuddy-desktop-ai.info`（**国际服**，`www.workbuddy.ai`）。
> 两者**放在同一个目录**、文件格式完全相同，只是文件名与域名不同 —— 所以每处读写都必须
> 按 `root_id` 过滤（国服只认 `workbuddy-desktop*`、国际服只认 `workbuddy-desktop-ai*`）。
> 不过滤的话，一边写文件就会把另一边的"当前账号"顶掉：页面显示成别人的账号、
> 切换按钮永远变不成"当前账号"，看起来像切换失败。备份裁剪的通配也各自独立
> （`workbuddy-desktop.*.info` 匹配不到 `workbuddy-desktop-ai.*`，因为 `desktop` 后面
> 是连字符而不是点），两族的备份互不干扰 —— 这是预期行为，不是漏清理。
>
> 详见下面的「双通道（国服 / 国际服）」。

### 双通道（国服 / 国际服）

页面入口是 `/`（统一入口页），左侧侧边导航栏有两个管理入口，各自指向一个**独立视图**：

| 入口 | 服别 | 视图 | 接管的登录态 | 账号库 | 接口前缀 |
|---|---|---|---|---|---|
| WorkBuddy账号管理 | 国服 | `/wb` | `workbuddy-desktop.info`（`www.workbuddy.cn`） | `wb_auth\` | `/api/*` |
| WorkBuddyAI账号管理 | 国际服 | `/wbai` | `workbuddy-desktop-ai.info`（`www.workbuddy.ai`） | `wbai_auth\` | `/api/wbai/*` |

两个视图共用同一份 `ui_template.html`，差别全部来自后端注入的通道上下文：

| 能力 | 国服 | 国际服 | 为什么 |
|---|---|---|---|
| 切换 / 增删账号 | ✅ | ✅ | 同一套文件读写逻辑 |
| 积分明细 | ✅ | ✅ | **两个通道不是同一个计费网关**：国服 `copilot.tencent.com` / 国际服 `www.workbuddy.ai`。拿国际服的 accessToken 去打国服网关恒返回 HTTP 401（实测），所以 `endpoint` 必须按通道给 —— 见 `channel_cfg()` |
| 签到状态 / 一键签到 | ✅ | ❌ | 国际服的签到接口**可达但活动未开启**（实测 `active=false`「签到活动未开启」），且按用户要求不迁移签到入口 |
| 一键续期 | ✅ | ✅ | 同 `endpoint` 机制；续期路径 `/v2/plugin/auth/token/refresh`，国际服实测可续 |
| 切号时迁移本地数据 | ✅ | ❌ | 迁移只对国服的数据目录（`~/.workbuddy`）验证过 |
| 打开客户端 | ❌ | ❌ | 国服侧按用户要求去掉按钮（**接口保留**，迁移流程内部仍在用）；国际服在**接口层**也拒掉 —— 进程是按国服 exe 名枚举的，放行等于用 `/api/wbai/open-client` 去开国服客户端 |

> ⚠️ 网关配置**只有 `channel_cfg(ch)` 一个入口**。早期四个调用点（积分、签到状态、
> 签到、续期）各写一行 `wb.load_config(SCRIPT_DIR / "config.json")`，拿到的永远是国服
> 网关 —— 加通道时最容易漏改的就是这种「每处各写一遍」的取配置方式。
> 也**不要**用 `os.environ["WORKBUDDY_ENDPOINT"]` 覆盖：那是进程级的，两个通道在同一
> 进程里同时服务，设了会把国服也一起改掉。

后端实现上，一个通道 = `Channel` 配置对象（`wb_ui_server.py`）：

```python
CHANNELS = {"wb": Channel(...), "wbai": Channel(...)}
```

读写函数都接受 `ch` 参数（默认国服，保持 CLI / 计划任务 / 老调用的行为不变），
内部只读 `ch.auth_dir` / `ch.desktop_info` / `ch.backup_glob` / `ch.lock_key` 这些属性。
**「加一个通道」= 加一份配置，而不是复制一份 `switch_account`。**

> ⚠️ 桌面端目录是**通道实例属性**（`ch.desktop_dir`），不是模块级常量。
> 早期版本有个 `wb.DESKTOP_DIR`，自检靠改它把写盘重定向到临时目录；
> 重构后那个名字不再生效，重定向**静默失效**，自检于是真的切了本机的登录态。
> 现在统一走 `ch.redirect(目录)`，重定向没生效会立刻让断言失败，不会再静默。
> 自检里那条 `[wb] 桌面端目录已重定向到临时目录` 断言就是防这件事复发。


### 并发与备份策略

- **文件锁**：切号/续期/增删账号的「读-改-写」全程持 `common.file_lock`
  （Windows `msvcrt`、POSIX `fcntl`），锁文件放在本工具目录 `.locks\`，不写进客户端配置目录。
  与定时任务同时触发时不会互相覆盖。
- **备份保留**：每次切号后自动把桌面端备份裁剪到最近 `DESKTOP_BACKUP_KEEP`（默认 10）份。
  备份是有效登录态，长期堆积等于把凭据散落磁盘；想立即清理：`python wb_ui_server.py --prune`。
- **端口避让**：默认 8765 被占用时自动顺延（Windows 的 `SO_REUSEADDR` 允许重复 bind，
  所以实现上先主动探测端口是否真有服务在监听，再决定顺延）。

---

## 使用方法

### 启动（推荐）

双击 `workbuddy_switcher.cmd`，或在上层 `自动签到\` 目录双击其同名脚本。
脚本会自动定位到 `wb_switcher`，校验 Python 后启动本地服务并自动打开浏览器。

### 命令行直接调用

```bash
python wb_ui_server.py --list           # 列出 wb_auth 可用账号（JSON）
python wb_ui_server.py --current        # 查看桌面端当前账号（JSON）
python wb_ui_server.py --switch NAME    # 切换为 wb_auth\NAME.info
python wb_ui_server.py --serve [--port 8765]  # 启动本地 HTTP 服务（端口被占用会自动顺延）
python wb_ui_server.py --prune [N]      # 清理桌面端切号备份，只留最近 N 份（默认 10）
```

加 `--channel wbai` 可把上述命令指向**国际服**通道：

```bash
python wb_ui_server.py --channel wbai --list
python wb_ui_server.py --channel wbai --current
python wb_ui_server.py --channel wbai --switch workbuddyai-xxx.info
```

### 页面结构

`/` 是**统一入口页**：左侧侧边导航栏两个管理入口，右侧是选中入口对应的独立视图
（用 iframe 承载，两边 JS/CSS 完全隔离，不必把两个视图的前端揉成一个巨型文件）。

```
┌ 侧边导航栏 ────────┬ 内容区（/wb 或 /wbai）────────────────────┐
│ [logo] 账号管理     │  ● 已连接   ↻ 刷新                        │
│ WorkBuddy 与        │ ────────────────────────────────────────  │
│ WorkBuddyAI         │  提示块（首行是副标题）                    │
│                     │  当前桌面端账号        一行                │
│ 管理入口            │  可用账号（来自 <账号库>） 标题右侧是工具条 │
│ ┃[国服] WorkBuddy   │  ＋ 添加账号            折叠卡片           │
│ ┃      账号管理     │  可用账号列表           每行：身份|积分|操作│
│ ┃      workbuddy-   │                                          │
│ ┃      desktop.info │                                          │
│ ┃      … · 当前：X  │                                          │
│ ┃[国际服] WorkBuddyAI                                          │
│ ┃      账号管理     │                                          │
│ ┃      … · 当前：Y  │                                          │
│ ● 本地服务已连接    │                                          │
└─────────────────────┴──────────────────────────────────────────┘
```

**侧边导航栏**（`ui_hub.html`）：

| 入口 | 名称 | 服别 | 副标题（入口下方两行） | 状态行 |
|---|---|---|---|---|
| `/wb` | WorkBuddy账号管理 | 国服 | `workbuddy-desktop.info` / `www.workbuddy.cn · 账号库 wb_auth\` | 当前：<昵称> |
| `/wbai` | WorkBuddyAI账号管理 | 国际服 | `workbuddy-desktop-ai.info` / `www.workbuddy.ai · 账号库 wbai_auth\` | 当前：<昵称> |

交互与实现要点：

- 入口是**真锚点**（`href="#/wb"` / `href="#/wbai"`），点击时脚本切视图并同步 hash ——
  刷新页面、复制链接都能停在同一个视图；中键/新标签打开也照常可用。
- 当前入口用 `aria-current` + `.on` 高亮；键盘可达（`focus-visible` 有描边）。
- **iframe 懒加载**：没点过的视图不加载，点了才把 `data-src` 写进 `src`；
  切回来不重新加载（两个 iframe 都留在 DOM 里，只是显示/隐藏切换）。
- 入口下方的「当前：X」由入口页自己拉 `/api/current` 与 `/api/wbai/current` 得到
  （都是只读 GET，不需要一次性令牌），每 30 秒自更新 —— 切完号不用手动刷新侧边栏。

> ⚠️ **入口页必须由本地服务提供，不能当普通 html 文件打开。**
> 双击 `ui_hub.html`、或把它丢到静态托管上时，`/api/*` 与两个 iframe 的 `/wb`、`/wbai`
> 全都不可达。此时页面会**顶部弹出红色提示条**说明原因并给出正确地址
> （`http://127.0.0.1:8765/`），侧边栏两个入口显示「未连接到本地服务」，
> 内容区换成「视图不可用」占位 —— 而不是让浏览器甩出一个打不开的图标。
> 正确入口：跑 `WorkBuddySwitcher.exe`（它自己会开窗口），或
> `python wb_ui_server.py --serve` 后访问打印出来的地址。
- 两个视图页在 iframe 里会自己认出被嵌入（`window.self!==window.top`），
  隐藏自身的品牌与锚点导航（外层侧边栏已经有一份了），只保留右侧状态与工具按钮，
  并把这一行降级成无边框工具条。
- 重复页面探测由**入口页**负责（频道 `wb_switcher_hub`）；视图层在嵌入模式下不参与，
  否则同一个入口页里的两个视图会互相把对方当成"重复页面"。

**视图内导航栏**（`ui_template.html`，两个视图共用）：

| 入口 | 位置 | 交互 |
|---|---|---|
| 当前账号 / 可用账号 / 添加账号 | 导航栏中部 | 点击**平滑滚动**到对应区块；滚动时用 `IntersectionObserver` 高亮当前区块（`rootMargin` 上方留出导航栏高度，否则标题会被压在栏下） |
| 刷新 | 导航栏右侧 | 重新拉账号列表 + 强制刷新积分/签到 |
| 打开客户端 | 导航栏右侧、当前账号行 | 见下 |

> 当前账号行也放了「打开客户端」—— 切完号正是需要它重新读取登录态的时候，
> 比让用户回到顶部导航更顺手。列表行则只有切换/删除。
> **本机两个通道都把它关掉了**（`OPEN_CLIENT` 置空），Trae 侧仍保留。

### 「打开客户端」

切号之后客户端需要重新读取登录态，但它不一定在跑 —— 所以给一个一键入口，
而不是让用户去开始菜单找。

- **已在运行** → 尝试把它的窗口切到前台（`EnumWindows` + `SetForegroundWindow`）。
  ⚠️ Windows 有**前台锁定**，`SetForegroundWindow` 可能被系统拒绝；
  这时消息会如实说"系统不允许本程序把它切到前台，请从任务栏点开"，不会假装成功。
- **没在运行** → 定位 exe 并 `DETACHED_PROCESS` 启动（不随本工具退出而结束）。

exe 定位只从**固定候选位置**和**正在运行的进程路径**里找，不接受任何外部输入：
运行中进程的镜像路径 → `%ProgramFiles%` / `%ProgramFiles(x86)%` / `%LOCALAPPDATA%`
下的 `WorkBuddyAI\WorkBuddyAI.exe` → 注册表卸载项的 `DisplayIcon` / `InstallLocation`。

接口：`GET /api/client-status`（只读）、`POST /api/open-client`（写，需令牌）。
两者都按通道分派：国际服分别返回 `{"ok":true,"client":null,"unsupported":true}` 与 `400 国际服不代管客户端进程`，不会谎报国服的进程状态。

### 加载性能

实测首屏时序（本机）：

| 阶段 | 耗时 |
|---|---|
| 账号列表（`/api/accounts` + `/api/current`，前端并行） | **9 ms** |
| 积分 + 签到（`/api/credits` + `/api/checkin-status`，前端并行） | **680 ms** |
| 缓存命中后全部 | **10 ms** |

后端本身不慢（`current_account` 5.4ms、`list_accounts` 1.9ms）。所以优化做在三处：

1. **启动预热**：服务起来后立刻在后台把积分与签到缓存跑热（两个线程并行），
   抢在用户打开页面之前完成 —— 首屏那两个请求直接命中缓存，**680ms → 10ms**。
2. **前端并发**：`load()`、`loadCredits()`、`loadCheckin()` 同时发起。
   原来是 `load().then(()=>Promise.all([...]))`，白白多一个串行段。
3. **启动探测提速**：`single_instance_guard` 从 **0.86s → 0.22s**。
   原因：本机连一个**没人监听的回环端口不会立刻 RST，会一直挂到超时**，
   探测耗时 ≈ 超时值。改成一次并发扫完所有候选端口 + 超时 0.4→0.2s。

### 文件操作的健壮性（2026-09-19 加固）

Windows 上**刚创建/刚写入的文件与目录**可能被杀软或索引器短暂持有句柄，
此时 `rename` 抛 `WinError 5 拒绝访问`、`stat()` 抛 `PermissionError`。
本工具踩过两次，现在统一按下面的方式处理：

| 场景 | 处理 |
|---|---|
| 改名/移动（切号、迁移） | `common.replace_with_retry()`：6 次递增退避重试 |
| `current_account()` 取 mtime | `_mtime_with_retry()`：3 次重试；仍失败则给兜底 mtime |
| 其它读文件 | `except (OSError, ValueError)` 返回 None |

`current_account()` 的兜底 mtime 有个讲究：**正式文件给"现在"、备份给 0**。
不能简单地"读不到就跳过" —— 那会让一个旧备份顶上来当"当前账号"，页面上显示成别人的账号。
也**不能用 `float("inf")`**：mtime 会进 JSON，`Infinity` 不是合法 JSON，浏览器 `JSON.parse` 会抛错。

### 账号行里各块信息的位置

身份列（`.who`）宽 **254px**。这个值是**实测**出来的："登录态剩余 · 日期"那一行
（如 `登录态 剩余 54.9 天 · 2026/11/12 13:45`）实测 **204px**，加上头像 32px
与间距 9px，正好 245px —— 取 254 留 9px 余量。**窄于这个值它就会折成两行。**

> 用布局探针量过：`.who` 210px 时内部只有 169px，而该行需要 204px，必然折行；
> 折行后 `.meta`（当时还带「· 桌面端正在使用」）与它挤在一起，看起来像文字串行。
> 顺带也去掉了那句重复信息 —— 行首的 `[当前]` 标签已经说明同一件事。

`.exp` 里是**两个 `nowrap` 片段**（`.exp-ttl` 登录态剩余 / `.exp-total` 总剩余积分），
万一在极窄窗口下仍要折行，会在两者之间**整体断开**，不会把 `·` 孤零零留在行尾。
两块都有 `line-height:1.4` 与独立上边距。

**这一行现在只有「总剩余积分 Y」**（2026-09-20 第二轮调整）：

- 「登录态 剩余 X 天」整段删除（标签和天数都不再显示）
- 「总剩余积分」保留在这里（上一轮从积分块标题行移过来的）
- 积分块只剩档位明细 —— 标题行整行删掉

> **剩余天数的信息没有丢**：不足 3 天（含已过期）时通道标签 `[长期 55天]` 会**变红**，
> 天数放在它的 `title` 里。这样"快过期"的安全信号还在，但不占版面。
> 标签上的 `55天 / 30天` 是**通道本身的有效期**（签发通道决定），不是剩余天数，所以照常显示。

**已使用 / 剩余积分的数字只在鼠标悬停到进度条时显示**（`.ci-bar:hover ~ .ci-used`）。
三个实现细节：

- 浮层用**绝对定位 + `opacity`**，不是 `visibility` —— 后者"隐形但占位"，
  会让进度条后面永久留一条 ~142px 的空档（实测条子只有 274px，而整行 450px），看着像没画完
- `pointer-events:none` 是必须的：浮层是条子的**兄弟节点**，如果它能接收指针，
  鼠标移到浮层上就会离开 `.ci-bar:hover`，浮层一闪一闪
- 进度条本体只有 4px 高，直接悬停很难点中，所以用 `.ci-bar::before` 把命中区上下各扩 7px。
  **因此 `.ci-bar` 不能再有 `overflow:hidden`**（会把伪元素一起裁掉），改由 `<i>` 自带圆角

### 身份列的宽度由「最宽的那一行」决定

`.who` 定 **248px**。这一列最宽的不是文件名（145px），而是当前账号行的
**「昵称 + [当前] + [长期 55天]」** —— 长昵称如「丹怡Helia 🌱」时约 **198px**。
定 196px 时它就会折成两行、行高多出 25px（实测 `name h=44`）。

### 进度条为什么要和数字同一排

**进度条的宽度必须和它上方那一行文字等宽，否则填充比例读不出来。**

实测过：文字行宽 447px、进度条只有 240px 时，`剩余20%` 对应 80% 的填充，
但 80% × 240 = 192px 只占整行宽度的 **43%** —— 看上去就像"文字说 80%、条子只有一半"。
数据本身没错，是**两者不等宽导致比例无法目视对照**。

现在把「进度条 + 已使用量」放进 `.ci-line` 同一排：

```
套餐基础积分             下次刷新时间 2026/10/01 00:00:00     ← .ci-info
[===== bar =====]        已使用 399.47/500 剩余20%           ← .ci-line（与上面等宽）
```

两排的 `left` / `right` 边缘**完全对齐**（实测 367 → 811）。进度条用
`flex:1 1 60px` 吃掉数字之外的剩余宽度，所以条子既短、比例又能直接读。

### 删除按钮：白字红底

```css
.btn.danger { background:var(--red-deep); border-color:var(--red-deep); color:#fff; }
```

底色用**更深的 `--red-deep`（`#c62828`）而不是 `--red`（`#ff5c5c`）**：
后者太亮，白字压在上面对比度只有 **2.99:1**，低于 WCAG AA 要求的 4.5:1，字会糊；
`#c62828` 是 **5.65:1**，清晰可读。自检里**直接算对比度做断言**（不是靠肉眼判断），
并额外断言"旧底色确实不达标"，把换色的理由钉在测试里。

### ⚠️ 积分块必须**填满** `.body`，行宽也要贴合内容

这里连续踩了两次，两个方向都不行：

| 写法 | 后果 |
|---|---|
| `.ci { max-width:240px }`（只压进度条） | 条子 240px、文字行 447px，比例读不出来 |
| `.ci { width:fit-content }`（整块收缩） | 积分块只占 300px，而 `.body` 仍占满剩余宽度 → **中间空出一大块** |
| `.ci { width:100% }` + `.wrap` 仍是 960px | 没死区了，但 `space-between` 把「档位名」和「时间」拉开 285px，条子也长到 392px |
| **`.ci { width:100% }` + `.wrap` 收窄到 860px** ✅ | 行内容 858px ≈ 行宽 860px，**刚好填满**；条子 292px；两排对齐 |

**根本原因是「行宽 > 内容宽」**：身份列 254 + 积分块内容 ~444 + 操作列 110 + 间隙 24
+ 内边距 26 = 858px。`.wrap` 定 960 时行宽 920，多出的 60px 加上 `fit-content`
留下的空档，就成了截图里那一大块空白。**收窄 `.wrap` 让行宽贴合内容**才是正解 ——
光调进度条或积分块的宽度，只是把空白从一个地方挪到另一个地方。

### 前端操作

页面分三块：**当前桌面端账号**（一行）、**工具条**、**可用账号列表**。

- **当前桌面端账号**：单独一行，与列表行**共用同一套渲染**（同一个头像样式、同一个通道标签、
  同一份积分与签到块），所以观感完全一致。右边多一行 `账号库中：<文件>` ——
  当前账号按 **uid** 反查它在 `wb_auth\` 里的文件，找不到（例如手动登录的账号）就显示
  「不在账号库中，无积分数据」。
- **可用账号**：**列表形式**，一行一个账号，横向三段 ——
  左「身份」（头像 / 昵称 / 通道标签 / 文件名 / 登录态剩余 / 签到）、
  中「积分明细」、右「操作」（切换到该账号 / 删除）。
  窄窗口下三段自动折行。
  **列表里不重复显示当前账号**（它只在上面那一行展示）。
- **工具条**（在"可用账号"标题右侧）：**刷新积分**、**一键签到**、**一键续期**。
  三个按钮按后端能力显示：Trae 侧没有积分 / 签到接口，对应按钮自动隐藏。
- **查看积分**：每个账号都给出**总剩余积分**与**积分明细**，分两档
  （口径与客户端「积分明细」一致，取自计费网关的资源包接口）：

  | 档位 | 含义 | 展示的时间 | 聚合方式 |
  |------|------|-----------|----------|
  | 套餐基础积分 | 套餐额度，按周期滚动 | **下次刷新时间** = 本周期结束 + 1 秒 | 周期内多个包取最早结束 |
  | 平台奖励积分 | 赠送 / 裂变包，一包一到期 | **最近到期时间** = 各包中最早到期 | 全部有效包容量与用量求和 |

  每个档位三行：档位名、时间与已使用量（同一行、两端对齐）、进度条。
  列表行是整行宽度，放得下「时间 + 已使用量」约 350px 的一行；两块文本都设 `nowrap`
  并允许整块换行，因此在窄窗口下退化为整齐的两行，不会出现半边折行的错位。

  进度条长度 = **已使用比例**（与客户端一致）。已失效的资源包（`Status != 0`）不计入明细，
  也不参与「最近到期时间」，否则会显示成早已过去的时间。账号名下没有资源包时显示「暂无可用积分包」。
  数据缓存 10 分钟，工具条「刷新积分」强制回源。各账号**并发**查询（实测 6 个账号 1.96s → 0.38s），
  既省等待，也减少「等太久用户刷新页面把请求取消」的情况。
- **查看签到状态**：每个账号在登录态下面显示一行「今日已签到 · 连续 N 天」（绿点）
  或「今日未签到 · 可领 N 积分」（黄点）。只读查 `checkin-activity-status`，**不会**顺带签到。
  状态缓存 5 分钟。查询失败（如登录态过期）显示原因，不影响其它账号。
- **一键签到**：对 `wb_auth` 全部可用账号各签一次（真正调 `daily-checkin` 领取）。
  已签到 / 活动未开启 / 不可领取都会如实返回，不算失败。签完自动刷新签到状态与积分。
- **一键续期**：对全部账号强制续期一次（等价 `--refresh-all --force`）。
  这是显式点击，所以**跳过门卫**；计划任务那条路仍然走门卫。
- **切换账号**：点击列表行上的“切换到该账号”，切换成功后自动续期并刷新剩余天数。
- **添加账号**：展开"＋ 添加账号"，选择 `.info` 配置文件（或粘贴内容），填标识后保存。
  内容会写入 `wb_auth\workbuddy-<标识>.info`，并以解析器校验有效性，无效则回滚删除。
  若 `accessToken` 不完整（例如粘贴时被截断，只剩 `eyJx` 这种几个字符），会直接拒绝 ——
  这类残缺登录态能存进去但一请求就 401，列表里也会标为不可用。
- **删除账号**：点击列表行上的“删除”，弹窗确认后从 `wb_auth\` 移除。

---

## HTTP API

服务仅绑定 `127.0.0.1`（默认端口 `8765`），局域网不可访问。

> 所有会改动登录态 / 账号库的接口**只接受 POST**（参数放 JSON body，也兼容 query string），
> 用 GET 调用会返回 405。服务还会校验 `Host` / `Origin` 必须是回环地址，阻断外部网页的跨站调用。

| 方法 | 路径 | 参数 | 说明 |
|------|------|------|------|
| GET  | `/` | — | **统一入口页**（侧边导航栏，两个管理入口） |
| GET  | `/wb` | — | **国服**管理视图（WorkBuddy 账号管理） |
| GET  | `/wbai` | — | **国际服**管理视图（WorkBuddyAI 账号管理） |
| GET  | `/api/accounts` | — | 列出 `wb_auth` 可用账号 |
| GET  | `/api/current` | — | 桌面端当前账号 |
| GET  | `/api/credits` | `?force=1` | 积分明细：各账号总剩余积分 + 套餐基础 / 平台奖励明细（缓存 10 分钟，`force` 强制回源） |
| GET  | `/api/checkin-status` | `?force=1` | 各账号签到状态（只读，缓存 5 分钟） |
| POST | `/api/switch` | `{name: "<文件名>"}` | 切换账号 |
| POST | `/api/remove` | `{file: "<文件名>"}` | 从 `wb_auth` 删除账号 |
| POST | `/api/refresh` | `{file: "<文件名>"}` | 对目标账号真正 HTTP 续期并写回 |
| POST | `/api/add` | `{name, content}` | 新增账号 |
| POST | `/api/checkin` | — | 一键签到：账号库全部可用账号各签一次 |
| POST | `/api/refresh-all` | — | 一键续期：全部账号强制续期（跳过门卫） |

**国际服通道**把上表的 `/api/xxx` 换成 `/api/wbai/xxx`（`accounts` / `current` /
`switch` / `remove` / `add` / `credits` / `refresh` / `refresh-all` 都可用，走
`https://www.workbuddy.ai` 网关；`checkin-status` 返回空列表并带 `unsupported: true`；
`checkin` / `refresh-all` 中签到那条返回 400 并说明原因）。

> 路由是**白名单式**的：只有注册在 `CHANNELS` 里的 key 才会被识别，
> `/api/whatever/switch` 不会被误当成某个通道，而是落回国服的 404。
> 两个前缀都登记进了 `WRITE_ENDPOINTS` —— 漏了的话，GET 不会对它们返回 405，
> 写接口就能被 GET 摸到。

> 签到状态是 **GET `/api/checkin-status`**、签到动作是 **POST `/api/checkin`** ——
> 两者必须用不同路径：`WRITE_ENDPOINTS` 里的路径一律拒绝 GET（405），
> 同一个路径没法既只读又写。

写操作还需要一次性令牌（见下）。

---

## 账号库说明（当前 `wb_auth\`）

> 下表为 2026-09-17 快照，账号会增删 —— **实际以 `wb_auth\` 目录与 `check_ttl.py` 输出为准**。

| 文件 | 昵称 | uid | uin | 手机号（尾号） | 签发通道 |
|------|------|-----|-----|----------------|----------|
| `workbuddy-jhan.info` | 13677759422 | c6297850-7a78-4ec5-b46a-8748ffd28a58 | 330119838223 | 9422 | `enterprise_switch`（短期 30 天） |
| `workbuddy-Maggie ya.info` | Maggie ya | a2b31245-7d42-4207-af9b-8162fee183e3 | 330118970606 | 2679 | `enterprise_switch`（短期 30 天） |
| `workbuddy-wtnong.info` | wtnong | b592a5dd-3f86-4360-b0cc-4df5b196098b | 330116013866 | 1461 | `oneid_login`（长期 55 天） |
| `workbuddy-wtnong1.info` | wtnong1 | ae528206-0a31-494f-9a3a-4e19363edf55 | 330116014385 | 7608 | `oneid_login`（长期 55 天） |
| `workbuddy-星空.info` | 星空 | ba575b06-7a96-46ff-b18f-d7f959c74f0f | 330107846428 | 7575 | 早期令牌（无 `token_source`，按 55 天） |

> 其中 `jhan` 与 `Maggie ya` 是 30 天短期通道，靠下面的自动续期保持不掉线。

### 唯一账号库约定（重要）

`wb_switcher\wb_auth\` 是**唯一**的 WorkBuddy 账号库。上级 `自动签到\wb_auth\`
必须**保持为空**，不要把账号文件拷进去 —— 同一账号存在两份副本时，两份凭据会各自
按自己的周期到期（续期只延长被续的那一份），容易出现"一边还能用、另一边已过期"的
错觉，排查成本很高。`自动签到\config.json` 的 `_说明4b` 也记了这条约定。

> 实测补充：服务端**不会**在续期后立刻作废旧 refreshToken（旧 RT 仍返回 200），
> 所以副本不会瞬间互相失效；真正的问题是两份凭据的有效期各自漂移。

---

## 登录态有效期与签发通道

同一个账号、同一台机器，剩余天数也可能和别的账号差近 2 倍 —— 有效期由服务端按
**登录通道**决定，写死在 JWT 的 `token_source` 里，续期只会沿用同一通道。

下表为 **2026-09-17 实测**（取自续期响应 `data.expiresIn`，与 JWT 的 `exp` 一致）：

| `token_source` | access | refresh | 产生方式 |
|---|---|---|---|
| `oneid_login` | 55 天（`expiresIn=4752000`） | 60 天 | **登录流程**直接签发的会话 |
| `enterprise_switch` | 30 天（`expiresIn=2592000`） | 60 天 | **"切换/绑定账号"换来的会话** |
| （无该字段，早期令牌） | 55 天 | 60 天 | 早期签发 |

> 早期文档写的「`oneid_login` 60/90、`enterprise_switch` 3/7」已与现状不符：现在
> 企业空间通道是 **30 天**（不是 3 天），两条通道的 refresh 都是 **60 天**。
> TTL 由服务端下发、可能随时调整，**以 `check_ttl.py` 的实时输出为准**。

### 为什么"同样用手机号验证码登录"，通道却不同

**`token_source` 记录的是"这份会话由哪个接口签发"，不是"账号是什么身份"。**
个人账号同样会被标成 `enterprise_switch` —— 实测用三个账号的 accessToken 调
`GET https://copilot.tencent.com/console/account`，`type` 全是 `personal`，
`isCurrentOneIdEnterprise` / `isCurrentOneIdPersonal` 全为 `false`，`sso.domain`、`idp` 全空。

从桌面客户端 `app.asar` 的 `AuthenticationManager.switchAccount()` 可以直接读到两条路径：

```js
// 普通登录：POST /v2/plugin/auth/token      → 服务端标 token_source = oneid_login
// 切换账号：AuthenticationManager.switchAccount(account) → provider.switchAccount()
async switchAccount(account, authSession, ctx) {
    const { type, enterpriseId } = account;
    const path = type === Edition.Personal
        ? `/v2${this.prefixPath}/login/enterprise`            // ← 个人账号也走这条
        : `/v2${this.prefixPath}/login/enterprise/${enterpriseId}`;
    const { data: { data: newAuthToken } } = await this.restOperations.post(path, {}, {
        headers: {
            ...this.enterpriseHeaders(currentSession.auth),
            "X-Refresh-Token": currentSession.auth.refreshToken,
            Authorization: `Bearer ${currentSession.auth.accessToken}`,
            // 仅企业账号才补 X-Enterprise-Id / X-Tenant-Id
        }
    });
}
```

网页登录页侧有对称的一步。`download.codebuddy.cn/web/login/.../index-*.js` 里，
登录成功后的 `chooseAccount()` **无条件**执行：

```js
const l = g.startSpan("web.login.choose_account", {account_type: o.type, enterprise_id: o.enterpriseId || "", platform: a});
try {
    if (Le) { if (!(await Cs(o.enterpriseId))?.switched) throw new Error("switch enterprise account failed") }
    else { const d = await Qe(); await Rs({enterpriseId: o.enterpriseId, state: h, domain: s, webtoken: d, ...}) }
    ...
    try { await kn(o.enterpriseId) } catch (d) { console.warn("[newLogin.chooseAccount] switchCurrentAccount failed", d) }
    ...
    await wt(o);   // bridge：把 token 交回客户端
}
```

`kn(r)` = `POST /console/account/switch`，`body = r ? {target_enterprise_id: r} : {}`。
**个人账号 `enterpriseId` 为空 → 传空 body，照样调。** 没有任何按账号类型跳过的分支。

### ⚠️ 所以：退出重登改不了（已实测确认）

**现在的登录流程固定会走一次"切换/绑定"，客户端最终拿到的就是 `enterprise_switch` 会话。**
用户实测：退出登录 → 手机号验证码重新登录 → 仍然是 30 天。与代码一致。

时间线也吻合：本机 14 份历史登录态快照里，`oneid_login` 的会话都建于 **08-20 ~ 09-02**，
而 `enterprise_switch` 的两个建于 **09-14 00:46**（Maggie ya）和 **09-16 14:28**（jhan）。
旧会话靠"续期不换通道"一直保持 55 天，新登的一律 30 天 —— 是**登录页改版**的结果，
不是账号属性，也不是操作失误。

**实践结论：30 天就是新常态，别再折腾重登。真正的风险是自动续期没跑起来 ——
见下面「自动续期」章节，务必注册计划任务。**

> ⚠️ 两处曾被误判、已作废：
> ① "登录页有个人/企业身份选择" —— CN 版确实有「个人 / 企业」两个 tab，但默认就是「个人」
> （`useState("personal")`），企业 tab 走企业域名 SSO，与手机号验证码无关。
> ② "只有被手动切换过的账号才是 30 天" —— 实测重登一样是 30 天，与是否手动切换无关。

- **改本地文件/配置没用**：`accessToken` 是 RS256（服务端私钥签名）、`refreshToken` 是 HS512，
  改任何字符都会验签失败；`.info` 里的 `expiresAt` / `expiresIn` 只是本地镜像，改了只骗自己。
- **改续期请求头也没用**：实测 `X-Auth-Refresh-Source` 取 plugin / desktop / console /
  oneid_login / 不带该头，返回恒为同一 TTL，`sessionState` 也不变 —— TTL 绑在服务端会话上。
- **目前没有办法转回 55 天**：登录流程固定走"切换/绑定"，退出重登也无效（已实测）。
  除非官方改回旧流程，否则新登账号一律 30 天。已有的 55 天老会话靠"续期不换通道"保持。
- 短期账号不会因续期变长期（`token_source` 原样保留），但 **refresh 会把 refreshToken
  重置为新的 60 天**，所以只要在 access 到期前续一次就能一直不掉线。
- **旧 refreshToken 不会立刻作废**：实测拿续期前的旧 RT 再请求，仍返回 200。因此
  "续期会踢掉另一份副本"并不成立；副本的真实风险是两份凭据的有效期各自漂移。
- **续期不保证延长**：若服务端本次签发较短 TTL，续期后剩余天数反而可能变少
  （实测 `oneid_login` 从 60 天变 55 天）。要延长先看 `check_ttl.py` 的剩余天数再决定。

体检命令：

```bash
python check_ttl.py              # 扫 wb_auth、wbai_auth 与 ..\自动签到\wb_auth
python check_ttl.py wb_auth      # 只扫指定目录
```

输出每个账号的 `token_source`、剩余天数、续期窗口，以及 `accessToken` 是否完整。
（`..\自动签到\wb_auth` 按约定应保持为空，扫不到文件是正常的。）

---

## 自动续期

**⚠️ 新登账号一律是 `enterprise_switch`（30 天），无法避免（见上一章节）。**
所以自动续期不是可选项，是必需品 —— 没有它，账号每 30 天就会掉线一次。

### 门卫：默认只续快到期的，不做无谓写盘

`--refresh-all` **默认走门卫**（`force=False`），交给上级
`workbuddy_checkin.refresh_account` 判断 —— 只有同时满足两个条件才真正刷新：

| 闸门 | 默认值 | 来自 |
|---|---|---|
| accessToken 剩余天数 | < **3 天** | `REFRESH_THRESHOLD_DAYS` |
| 距上次实际续期 | ≥ **24 小时** | `refresh_min_interval_hours`（config.json 可调） |

不满足就返回 `kind="skipped"`，**一个字节都不写盘**。这与上级
`refresh_guard.py` / `workbuddy_checkin.py --refresh` 的门卫语义一致。

```bash
python wb_ui_server.py --refresh-all            # 走门卫（计划任务用）
python wb_ui_server.py --refresh-all --force    # 跳过门卫，无条件全刷
refresh_all.cmd                                 # 双击跑；加 --force 同上
```

UI 上手动点「续期」按钮走的是 `force=True`（你点了就是要刷，不再判断）。

### 计划任务

**每天 07:30 一次**（对齐上级「自动签到」项目的旧规则：`checkin_task.xml` 每天 00:01、
`refresh_task.xml` 每天 07:30）。因为门卫挡着，日常开销只有几次本地读取，真正刷新
只发生在快到期时 —— 所以不需要更高频次。

```powershell
# 默认每天 07:30；-At 06:00 改时刻，-Hours 12 改成每 12 小时，-Remove 移除
powershell -ExecutionPolicy Bypass -File install_refresh_task.ps1
```

> 本机 PowerShell 的 ExecutionPolicy 禁止直接运行 `.ps1`。要么加
> `-ExecutionPolicy Bypass`，要么把内容读成 scriptblock 执行
> （后者会让 `$MyInvocation.MyCommand.Path` 为空，得手工补 `$root`）。

`refresh_all.cmd` 支持 `/nopause`（计划任务是非交互环境，不加会让任务一直挂住），
`install_refresh_task.ps1` 已自动带上。refresh 会把 refreshToken 重置为新的 60 天，
所以按天级跑一次即可一直不掉线。上级「自动签到」项目的 `config.json` 里
`wb_auth_dirs` 已加入本目录：

```json
"wb_auth_dirs": ["D:\\AI项目\\自动签到\\wb_auth", "D:\\AI项目\\wb_switcher\\wb_auth"]
```

`discover_accounts()` 按 uid 全局去重，两个目录放着相同账号也只处理一次，不会重复签到。
**但不要把账号文件放回 `自动签到\wb_auth`**（见上面的「唯一账号库约定」）。

前端账号卡上会显示签发通道标签：**长期 55 天**（`oneid_login`）或**短期 30 天**
（`enterprise_switch`，橙色告警）。

---

## 打包桌面版

打包用的解释器**必须装了 `pywebview`**，否则 `hiddenimports` 里的 `webview` 会被静默跳过，
打出来的 exe 虽然能跑，但每次启动都退回"打开系统浏览器"。推荐用隔离环境：

```powershell
# 一次性准备（不污染系统 Python）
C:\Users\Administrator\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe -m venv `
  C:\Users\Administrator\.workbuddy-ai\binaries\python\envs\default
$VENV = C:\Users\Administrator\.workbuddy-ai\binaries\python\envs\default
& "$VENV\Scripts\python.exe" -m pip install pywebview pyinstaller

# 打包
& "$VENV\Scripts\python.exe" -m PyInstaller WorkBuddySwitcher.spec --noconfirm
& "$VENV\Scripts\python.exe" -m PyInstaller TraeSwitcher.spec --noconfirm
```

产物在 `dist\WorkBuddySwitcher\`、`dist\TraeSwitcher\`。把账号目录（`wb_auth\`、
`wbai_auth\`、`tw_auth\`）放到 exe 同级即可识别。验证 `webview` 确实打进去了：
`dist\*\ _internal\` 下应能看到 `webview\`、`pythonnet\`、`clr_loader\` 三个目录。

> ⚠️ **exe 的数据目录基准是「exe 同级」，不是 cwd、也不是 `_internal\`。**
> 实现方式是 `wb_ui_app` / `tw_ui_app` 在 `import` 之后把
> `srv._BIN_DIR` 指到 `Path(sys.executable).parent`（冻结态才这么干）。
>
> 因此**通道的账号库路径必须按 `_BIN_DIR` 现算**（`Channel.auth_dir` 是属性，
> 不是 `__init__` 里存的快照），模块级路径常量一个都不能留 —— 它们全是导入时的快照，
> 而这次改写发生在导入**之后**，改不到已经建好的通道上。
> 踩过两次，表现分别是「自检真的切了本机登录态」和
> 「打包后的 exe 跑去 `_internal\wb_auth\` 找账号，页面恒显示 0 个，源码运行却完全正常」。
>
> 部署后自检：把账号目录放到 exe 同级，跑
> `WorkBuddySwitcher.exe --serve --port <冷门端口>`，再请求 `/api/accounts`，
> 条数应与你放进去的文件数一致。
> 或者直接跑 `python check_exe_datadir.py [exe 目录]`（双探针法，自动判两个通道
> 读的是 exe 同级还是 `_internal\`，跑完自动清理探针）。

### 桌面版 exe 的命令行开关

```bat
WorkBuddySwitcher.exe                  :: 正常：开原生窗口
WorkBuddySwitcher.exe --serve          :: 只跑本地 HTTP 服务，不开窗口（便于 curl 冒烟）
WorkBuddySwitcher.exe --serve --port 8790

:: 一次性动作会被转交后端（与 python wb_ui_server.py 同一套实现）
WorkBuddySwitcher.exe --list
WorkBuddySwitcher.exe --channel wbai --list
WorkBuddySwitcher.exe --refresh-all [--force]
WorkBuddySwitcher.exe --channel wbai --migrate-preview x.info
```

> `--channel` / `--force` / `--migrate-mode` 是**修饰符**，单独出现不构成动作 ——
> `WorkBuddySwitcher.exe --channel wbai` 仍然开原生窗口。
> 以前这些动作被**静默忽略**：`WorkBuddySwitcher.exe --refresh-all` 不会续期，
> 而是弹出一个窗口，脚本看退出码 0 还以为成功了。

窗口版没有控制台，出了问题看不到任何提示。所以：

- `--serve` 是唯一的排障入口（能用 curl 直接打接口）。
- 一次性动作在**终端或脚本里**能正常拿到 stdout 与退出码（实测
  `WorkBuddySwitcher.exe --channel wbai --migrate-preview x.info` 会打印 JSON 并返回 1）。
  只有**双击**运行时没有控制台、`print` 无处可去 —— 那种场景请用
  `python wb_ui_server.py ...`（`refresh_all.cmd` 走的就是那条路）。
- 账号目录要放在 exe 同级，否则 `--list` 只会回 `{"accounts": []}`。
- WebView 起不来时会退回系统浏览器，同时把原因写进 exe 同级的
  `logs\desktop-start.log` —— 否则用户只会看到"浏览器突然弹出来"，无从判断。

打包要点（改 spec 前先看）：

- **exe 自带解析/续期逻辑**。`switcher_common` 与 `workbuddy_checkin` / `trae_work_checkin`
  都是运行时动态导入，静态分析扫不到，必须列进 `hiddenimports` —— 漏了不会构建失败，
  而是**打包后双击一闪而过**（窗口版没有控制台，`require_module` 的 SystemExit 提示看不到）。
  排查办法：在 cmd 里直接跑 exe，能看到 `找不到依赖模块 ...` 的字样。
- `pathex` / `ICON` 已改成基于 `SPEC` 的相对定位，换机器或换盘符不必再改 spec。
- 运行时若找得到 `..\自动签到\config.json` 就优先读它的 `endpoint` 等配置；找不到则用
  内置默认值（`https://copilot.tencent.com`），不影响切号与续期。
  ⚠️ 这个默认值只对**国服**成立 —— 国际服的网关是 `https://www.workbuddy.ai`，
  由 `channel_cfg(ch)` 按通道覆盖（`Channel.endpoint`），不依赖 config.json 是否存在。
- **源码改动后必须重新打包**，`dist\` 不会自动跟随源码。
- ⚠️ **重新打包前先手工删掉 `dist\` 与 `build\`，并且先结束正在运行的 exe。**
  本机的 `rm` 是 WorkBuddy CLI 注入的安全删除 shim（走回收站），删大目录会
  `Some operations were aborted` 并 fail-closed；PyInstaller 内部的 `shutil.rmtree`
  同样被拦，表现为 `OSError: [safe-delete] 操作失败`，于是**旧产物没删掉、新产物没生成**，
  留下一个 exe 是旧的、`_internal` 是半新半旧的混合目录（很能骗人）。
  被 exe 占用的 `.pyd` 还会 `Permission denied`，所以顺序是：杀进程 → `rm -rf dist build` → 打包。

---

## 自检

```bash
python smoke_test.py
```

392 项断言，覆盖：

- 备份裁剪、文件锁（超时/串行/**锁文件不随加锁次数增长**）、端口避让
- 令牌注入健壮性（`<head>` 带属性时仍能注入）
- **客户端中途断开后服务仍可用**、服务端使用 `QuietHTTPServer`
- 积分明细（造假响应、**不联网**）：套餐 / 奖励两档归类、**下次刷新时间（周期结束 +1 秒）**、
  最近到期时间、**已失效包被排除**、容量与用量合计、总剩余、单账号失败不污染整体
- 两个切换器的接口行为：200 / 401 / 403 / 404 / 405 / 同源放行
- 一次性令牌：首页注入、缺令牌 401、错令牌 401、正确令牌放行、读接口不受影响
- 审计日志：记录切号、记录令牌失败、**不含凭据**
- 令牌完整性校验（add 拒绝、列表标记、拒绝切换）、**拒绝路径穿越的 name**、增删闭环
- 错误脱敏（500 响应不含用户目录）、依赖契约（缺模块/缺成员的可读报错）
- 前端模板渲染（无残留占位符，**三个上下文 wb / wbai / tw 逐一校验**）、账号带 token_source 字段
- **入口页与两个视图**：`/` 是统一入口页、侧边栏两个入口的名称与服别、
  两个入口指向各自独立视图、国际服视图显示积分与续期但隐藏签到、接口前缀正确
- **入口页的离线自证**：含离线提示条与占位、区分「未连接到本地服务」与「读取失败」、
  离线时不加载 iframe（`show()` 带 `online` 门卫）
- **账号库路径是派生值**：改 `_BIN_DIR` 后两个通道的 `auth_dir` 都跟着变；
  模块级路径常量（`AUTH_DIR` / `DESKTOP_DIR` / `DESKTOP_INFO` / `DESKTOP_ROOT_ID`）一个都不存在
- **国际服写路径**：`add` 用 `workbuddyai-` 前缀、`switch` 写 `workbuddy-desktop-ai.info`、
  备份族是 `workbuddy-desktop-ai.*`，且**不碰**国服的正式文件与备份
- **国际服不代管客户端**：`/api/wbai/open-client` 回 400、`/api/wbai/client-status` 不谎报国服进程
- **两个通道各看各的文件**：国服只认 `workbuddy-desktop*`、国际服只认
  `workbuddy-desktop-ai*`；国际服写接口只收 POST（405）
- **能力守卫在函数内**：`migrate_preview` / `refresh_all` / `checkin_all` 自带守卫，
  CLI 与 HTTP 两个入口行为一致（不靠调用方记得挡）
- **按通道的函数都显式收 `ch`**：`credits_snapshot` / `checkin_snapshot` /
  `checkin_account_file` / `checkin_all` / `refresh_all_ui` / `migrate_preview`
  签名里都有 `ch`，内部不拿默认通道兜底
- **积分/签到缓存按通道分区**：`_CREDITS_CACHE` / `_CHECKIN_CACHE` 是
  `{通道 key: {...}}`；国际服命中自己那条、不串国服；缓存失效只清该通道
- **CLI 退出码说真话**：`--channel wbai --migrate-preview` 非零退出且不输出国服数据；
  无迁移能力的通道上带 `--migrate` 切号时，返回消息里明说「本次仅切换」
- **exe 认 CLI 动作**：`--list` / `--switch` / `--refresh-all` / `--migrate-preview` 等
  会被转交后端处理（以前被静默忽略并**弹出一个窗口**）；`--channel` 这类修饰符单独出现
  时仍走「开原生窗口」
- **数据目录基准只有一个改写入口**：`rebind(base)` 一次改全
  （`_BIN_DIR` / `SCRIPT_DIR` / `LOCK_DIR` / `Handler.BASE_DIR` / `Handler.AUDIT_DIR`），
  启动器里**一行 `srv.X = ...` 都不许有**（逐个赋值就是"漏改一个"的来源）
- **Trae 侧目录也是派生值**：`auth_dir()` / `lock_dir()` / `backup_dir()` 按 `_BIN_DIR` 现算，
  不再是 `TW_AUTH_DIR` / `LOCK_DIR` 这类导入时快照（以前半新半旧，`tw_backups` 是现算的）
- **Trae 侧写路径**：切号 / 备份落 `tw_backups` / `remove` 闭环，全程临时目录 +
  伪造的本机登录态，真实 Trae 登录态一个字节都不碰

不改动真实登录态（切号一律在**重定向到临时目录的通道**上进行，增删用临时文件后清理）。

> ⚠️ 改自检里「切号落盘」相关用例时注意：桌面端目录是**通道实例属性**，
> 必须用 `wb.CHANNELS["wb"].redirect(临时目录)`，并断言重定向确实生效。
> 早期版本改的是模块级 `wb.DESKTOP_DIR`，重构后那个名字不再生效 ——
> 重定向静默失效，自检真的切了本机的登录态（跑完才发现，已从轮换备份还原）。
> 现在有一条 `[wb] 桌面端目录已重定向到临时目录` 的断言把这件事钉住。

改完代码建议跑一遍，退出码非 0 即有失败项。

---

## 安全提示

- `.info` 文件内含**有效的 `accessToken` 与 `refreshToken`**，等同长期登录凭据。
  禁止提交到 Git / 上传公网 / 转发他人。`wb_auth\`、`token_cache_workbuddy.json` 等
  均应在 `.gitignore` 中排除。
- 本工具只在本机 `127.0.0.1` 提供服务，不要修改为对外监听。
- **客户端中途断开属正常现象**：刷新页面、切换账号会取消尚未完成的请求（积分查询要打若干
  外部接口，耗时可到秒级），服务端随后写响应会拿到 `WinError 10053`。控制台**不会**再刷
  traceback —— 写响应做了连接异常兜底、路由层不再二次回写 500，服务端也用 `QuietHTTPServer`
  静默这类错误（真实故障照常打印）。
- 服务对 `Host` / `Origin` 做回环校验，且写操作仅接受 POST，可阻断外部网页的跨站调用；
  但这只是第二道防线，仍不要把端口暴露到局域网。
- **一次性访问令牌**：服务启动时随机生成，只存在于本次进程内存，随首页注入给前端，
  写操作必须回传 `X-Switcher-Token`，否则 401。用途是挡住"顺手调一下接口"的本地程序与
  DNS rebinding；它挡不住铁了心去抓首页解析令牌的本机进程，所以不要当成强鉴权。
  需要裸接口调试时加 `--no-auth` 关闭。
- **审计日志**：切号 / 增删 / 续期会追加到 `logs\switcher.log`（只记对象与结果，不含凭据），
  出问题可回溯"什么时候切过哪个号"。
- 切号只在**本机**客户端生效；若账号已在其他设备登录，请以官方规则为准。

---

## 依赖

- Python 3（纯标准库，无第三方依赖）。
- 复用上级项目 `自动签到\workbuddy_checkin.py` 的
  `session_from_info_file / split_info_name / load_config / refresh_account` 等函数。
  若该文件缺失或路径布局变化，请调整 `wb_ui_server.py` 顶部的 `PROJECT_ROOT` 定位逻辑。