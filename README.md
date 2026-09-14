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
├── ui_template.html       # 前端模板（两份切换器共用，后端按 UI_CONTEXT 渲染后返回）
├── wb_ui_app.py           # 桌面版启动器（pywebview 窗口，可打包 exe）
├── workbuddy_switcher.cmd # Windows 启动脚本（双击即用）
├── wb_auth/               # 切换用的账号配置库，放置 *.info 登录态文件
│   ├── workbuddy-Maggie ya.info
│   ├── workbuddy-wtnong.info
│   ├── workbuddy-wtnong1.info
│   └── workbuddy-星空.info
├── tw_ui_server.py / tw_ui_app.py / trae_switcher.cmd  # Trae 姊妹工具，见 README_Trae.md
├── TraeSwitcher.spec      # Trae 桌面版打包配置（pyinstaller TraeSwitcher.spec）
├── import_token.py        # Trae 凭据导入（别机 tokens / storage.json → config.json）
├── check_ttl.py           # 登录态体检：签发通道 / 剩余天数 / accessToken 完整性（只读）
├── smoke_test.py          # 回归自检：裁剪 / 文件锁 / 端口 / 接口 / 令牌校验（不碰真实登录态）
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
4. 把目标账号内容写入为新的 `workbuddy-desktop.info`；
5. 清理残留的 `.logged-out` 登出标记。

**切换后的续期**：前端切换成功后会调用 `/api/refresh`，用目标账号的 `refreshToken` 向 WorkBuddy
服务器真正续期（`POST /v2/plugin/auth/token/refresh`），拿到全新 `accessToken / expiresAt`，
并把新有效期写回 `wb_auth\<目标>.info`，使列表里展示的剩余天数即刻更新。

> 切号生成的备份文件本身仍是服务端有效的登录态。识别当前账号时会扫描该目录下全部 `.info`，
> 跳过已登出（带 `.logged-out` 标记）的，其余按修改时间降序，最新的标记为当前账号。

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

### 前端操作

- **查看当前桌面端账号**：顶部的“当前桌面端账号”卡片。
- **切换账号**：点击账号卡片上的“切换到该账号”，切换成功后自动续期并刷新剩余天数。
- **添加账号**：展开"＋ 添加账号"，选择 `.info` 配置文件（或粘贴内容），填标识后保存。
  内容会写入 `wb_auth\workbuddy-<标识>.info`，并以解析器校验有效性，无效则回滚删除。
  若 `accessToken` 不完整（例如粘贴时被截断，只剩 `eyJx` 这种几个字符），会直接拒绝 ——
  这类残缺登录态能存进去但一请求就 401，列表里也会标为不可用。
- **删除账号**：点击卡片上的“删除”，弹窗确认后从 `wb_auth\` 移除。

---

## HTTP API

服务仅绑定 `127.0.0.1`（默认端口 `8765`），局域网不可访问。

> 所有会改动登录态 / 账号库的接口**只接受 POST**（参数放 JSON body，也兼容 query string），
> 用 GET 调用会返回 405。服务还会校验 `Host` / `Origin` 必须是回环地址，阻断外部网页的跨站调用。

| 方法 | 路径 | 参数 | 说明 |
|------|------|------|------|
| GET  | `/` | — | 返回前端页面 |
| GET  | `/api/accounts` | — | 列出 `wb_auth` 可用账号 |
| GET  | `/api/current` | — | 桌面端当前账号 |
| POST | `/api/switch` | `{name: "<文件名>"}` | 切换账号 |
| POST | `/api/remove` | `{file: "<文件名>"}` | 从 `wb_auth` 删除账号 |
| POST | `/api/refresh` | `{file: "<文件名>"}` | 对目标账号真正 HTTP 续期并写回 |
| POST | `/api/add` | `{name, content}` | 新增账号 |

写操作还需要一次性令牌（见下）。

---

## 账号库说明（当前 `wb_auth\`）

| 文件 | 昵称 | uid | uin | 手机号（尾号） |
|------|------|-----|-----|----------------|
| `workbuddy-Maggie ya.info` | Maggie ya | a2b31245-7d42-4207-af9b-8162fee183e3 | 330118970606 | 2679 |
| `workbuddy-wtnong.info` | wtnong | b592a5dd-3f86-4360-b0cc-4df5b196098b | 330116013866 | 1461 |
| `workbuddy-wtnong1.info` | wtnong1 | ae528206-0a31-494f-9a3a-4e19363edf55 | 330116014385 | 7608 |
| `workbuddy-星空.info` | 星空 | ba575b06-7a96-46ff-b18f-d7f959c74f0f | 330107846428 | 7575 |

---

## 登录态有效期与签发通道

同一个账号、同一台机器，剩余天数也可能和别的账号差 20 倍 —— 有效期由服务端按
**登录通道**决定，写死在 JWT 的 `token_source` 里，续期只会沿用同一通道：

| `token_source` | access | refresh | 产生方式 |
|---|---|---|---|
| `oneid_login` | 60 天 | 90 天 | 常规 OneID 手机号登录 |
| `enterprise_switch` | 3 天 | 7 天 | 客户端切到企业空间的会话 |
| （无该字段，早期令牌） | 60 天 | 90 天 | 早期签发 |

- **改本地文件/配置没用**：`accessToken` 是 RS256（服务端私钥签名）、`refreshToken` 是 HS512，
  改任何字符都会验签失败；`.info` 里的 `expiresAt` / `expiresIn` 只是本地镜像，改了只骗自己。
- **改续期请求头也没用**：实测 `X-Auth-Refresh-Source` 取 plugin / desktop / console /
  oneid_login / 不带该头，返回恒为同一 TTL，`sessionState` 也不变 —— TTL 绑在服务端会话上。
- **唯一有效办法**：退出企业空间 → 退出登录 → 用手机号验证码重新登录 → 整份复制
  `workbuddy-desktop.info` 覆盖素材。
- 短期账号不会因续期变长期，但 **refresh 会滚动 refreshToken**（每次续期后 RT 也重置 7 天），
  所以每 ≤3 天续一次可以一直不掉线。

体检命令：

```bash
python check_ttl.py              # 扫 wb_auth 与 ..\自动签到\wb_auth
python check_ttl.py wb_auth      # 只扫指定目录
```

输出每个账号的 `token_source`、剩余天数、续期窗口，以及 `accessToken` 是否完整。

---

## 自动续期

短期通道（`enterprise_switch`）的账号 3 天就到期，靠人工点续期不现实：

```bash
python wb_ui_server.py --refresh-all      # 对 wb_auth 全部账号各续期一次，可挂计划任务
```

refresh 会**滚动 refreshToken**（每次续期后 RT 也复位为 7 天），所以每 ≤3 天跑一次可以一直
不掉线。上级「自动签到」项目的 `config.json` 里 `wb_auth_dirs` 已加入本目录：

```json
"wb_auth_dirs": ["D:\\AI项目\\自动签到\\wb_auth", "D:\\AI项目\\wb_switcher\\wb_auth"]
```

`discover_accounts()` 按 uid 全局去重，两个目录放着相同账号也只处理一次，不会重复签到。

前端账号卡上会显示签发通道标签：**长期 60 天**（`oneid_login`）或**短期 3 天**
（`enterprise_switch`，橙色告警）。

---

## 打包桌面版

```bash
"C:\Program Files\Python313\python.exe" -m PyInstaller WorkBuddySwitcher.spec --noconfirm
"C:\Program Files\Python313\python.exe" -m PyInstaller TraeSwitcher.spec --noconfirm
```

产物在 `dist\WorkBuddySwitcher\`、`dist\TraeSwitcher\`。把账号目录（`wb_auth\`、`tw_auth\`）
放到 exe 同级即可识别；未安装 `pywebview` 时会自动退回系统浏览器打开。

---

## 自检

```bash
python smoke_test.py
```

46 项断言，覆盖：

- 备份裁剪、文件锁（超时/串行）、端口避让
- 两个切换器的接口行为：200 / 401 / 403 / 404 / 405 / 同源放行
- 一次性令牌：首页注入、缺令牌 401、错令牌 401、正确令牌放行、读接口不受影响
- 审计日志：记录切号、记录令牌失败、**不含凭据**
- 令牌完整性校验（add 拒绝、列表标记、拒绝切换）、增删闭环
- 错误脱敏（500 响应不含用户目录）、依赖契约（缺模块/缺成员的可读报错）
- 前端模板渲染（无残留占位符）、账号带 token_source 字段

不改动真实登录态（切号用不存在的账号名触发，增删用临时文件后清理）。
改完代码建议跑一遍，退出码非 0 即有失败项。

---

## 安全提示

- `.info` 文件内含**有效的 `accessToken` 与 `refreshToken`**，等同长期登录凭据。
  禁止提交到 Git / 上传公网 / 转发他人。`wb_auth\`、`token_cache_workbuddy.json` 等
  均应在 `.gitignore` 中排除。
- 本工具只在本机 `127.0.0.1` 提供服务，不要修改为对外监听。
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