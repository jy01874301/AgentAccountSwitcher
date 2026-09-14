# Trae 账号切换器（tw_switcher）

一个本地小工具，用于在**同一台机器**上的多个 Trae Work CN 账号之间一键切换登录态。
通过替换 Trae 客户端的登录态配置实现账号切换，无需重复扫码登录，从而让多个账号
共用在这台机器上的 Trae 额度 / 积分。

> 配套：本工具只负责**切换账号**。Trae 每日签到 / 签到状态查询与续期的协议逻辑在
> 上级项目的 [`自动签到`](../自动签到/trae_work_checkin.py) 中，
> 本工具以只读方式复用其解密 / 账号发现 / 续期函数。

---

## 目录结构

```
wb_switcher/（switcher 系列工具所在目录）
├── tw_ui_server.py        # Trae 后端：账号读写/切换 + 本地 HTTP 服务（127.0.0.1:8766）
├── switcher_common.py     # 两个切换器共用的 HTTP 骨架 / 文件锁 / 备份裁剪 / 端口避让
├── tw_ui_index.html       # Trae 前端页面
├── trae_switcher.cmd      # Trae 启动脚本（双击即用）
├── tw_auth/               # 切换用账号素材库，每账号放一份 storage.json
│   ├── trae-wtnong.json
│   ├── trae-wtnong1.json
│   ├── trae-星空880.json
│   └── _trae_tokens.json  # import_token.py 产出的明文凭据（可读、不可切换）
├── tw_backups/            # 切号时自动备份的登录态（gitignore 排除，超 10 份自动裁剪）
├── TraeSwitcher.spec      # 桌面版打包配置：pyinstaller TraeSwitcher.spec
├── import_token.py        # 凭据导入（别机 tokens / storage.json → config.json）
└── wb_ui_*.py / wb_ui_index.html / workbuddy_switcher.cmd   # 同目录下的 WorkBuddy 切换器（姊妹工具）
```

---

## 工作原理

与 WorkBuddy 不同，Trae 的登录态**不是**明文的独立 `.info` 文件，而是存放在：

```
%APPDATA%\TRAE SOLO CN\User\globalStorage\storage.json
```

关键点：

- **登录态**：键 `iCubeAuthInfo://icube.cloudide` 存的是客户端用
  **AES-128-CBC + SHA-512 密钥派生**加密后的 base64 密文，明文为 `{token, userRegion, ...}`。
  客户端通过**文件 watcher 采纳外部内容变更**。
- **设备密钥**：键 `iCubeAuthInfo://icube-dc:<id>` 与登录态绑定。切换时**连同目标素材里的
  icube-dc 一并写入**本机 storage.json（设备凭据与登录态不匹配时，客户端会丢弃新登录态）。
- **切换本质**：把目标账号素材里**已加密的** `icube.cloudide` 密文 + 其附属 icube-dc 设备密钥
  整体搬运到本机 storage.json（素材本身是该账号登录时客户端原生生成的合法密文，
  无需重加密、无 HMAC 风险），其余键（usertag 及 IDE 其它配置）保持不变。

一次**切换**（`switch_account`）的步骤：

1. 校验 `tw_auth\<目标>.json` 是含加密登录态的 storage.json，且可解密；
2. 备份本机 storage.json 到 `tw_backups\storage.<时间戳>.json`；
3. 读取本机 storage.json，把 `iCubeAuthInfo://icube.cloudide` 替换为目标账号的密文，
   并覆盖写入其附属的 `icube-dc` 设备密钥；
4. 原位覆盖写回**同一路径**（保持文件名不变）。优先原子替换，若 Trae 运行时拒绝
   rename 则退化为直接覆盖写原路径；Trae 的 watcher 会采纳新登录态。

---

## 素材准备（重要）

`tw_auth\` 下每个文件必须是**该账号登录后的完整 storage.json**（含加密登录态）。

获取方式（每个想切换的账号做一次）：

1. 在目标账号的设备上登录 Trae，找到 `%APPDATA%\TRAE SOLO CN\User\globalStorage\storage.json`；
2. 复制该文件到本机 `wb_switcher\tw_auth\trae-<昵称>.json`，或用前端"添加账号"导入该文件；
3. 也可以在未登录该账号时，先手动把文件放进去，再由程序校验。

> ⚠️ `自动签到\tw_auth\config-*.json` 是**签到用的 token 配置**（明文 refresh_token，
> 无设备私钥、无加密登录态），**不能**用于切换 —— 后端会把它们识别为 `tokens` 类型并标为
> "不可切换"，但可正常展示。

---

## 使用方法

### 启动（推荐）

双击 `trae_switcher.cmd`，自动启动本地服务并打开浏览器。

> 切换前请先关闭正在运行的 Trae 客户端进程（或确保 storage.json 可写），
> 否则 Windows/Trae 可能拒绝外部改写。

### 命令行

```bash
python tw_ui_server.py --list          # 列出 tw_auth 可用账号（JSON）
python tw_ui_server.py --current       # 查看本机 Trae 当前账号（JSON）
python tw_ui_server.py --switch NAME   # 切换为 tw_auth\NAME 账号
python tw_ui_server.py --serve --port 8766   # 启动本地 HTTP 服务（默认 8766，被占用自动顺延）
python tw_ui_server.py --prune [N]           # 清理 tw_backups 备份，只留最近 N 份（默认 10）
```

### 并发与备份策略

- **文件锁**：切号/续期/增删素材的「读-改-写」全程持 `common.file_lock`，锁文件在本工具目录
  `.locks\`，不写进 Trae 配置目录。
- **备份保留**：每次切号后把 `tw_backups\` 裁剪到最近 `TW_BACKUP_KEEP`（默认 10）份。
- **端口避让**：默认 8766 被占用时自动顺延。

### 前端操作

- **查看当前桌面端账号**：顶部"当前桌面端账号"卡片。
- **切换账号**：点击账号卡片上的"切换到该账号"，切换后尝试续期并刷新剩余天数。
- **添加账号**：展开"＋ 添加账号"，选择该账号的 storage.json 并填标识后保存。
- **删除账号**：点击卡片"删除"，确认后从 `tw_auth\` 移除素材。

---

## HTTP API

服务仅绑定 `127.0.0.1`（默认端口 `8766`），局域网不可访问。

> 所有会改动登录态 / 账号库的接口**只接受 POST**（参数放 JSON body，也兼容 query string），
> 用 GET 调用会返回 405。服务还会校验 `Host` / `Origin` 必须是回环地址，阻断外部网页的跨站调用。

| 方法 | 路径 | 参数 | 说明 |
|------|------|------|------|
| GET  | `/` | — | 返回前端页面 |
| GET  | `/api/accounts` | — | 列出 `tw_auth` 可用账号 |
| GET  | `/api/current` | — | 本机 Trae 当前账号 |
| POST | `/api/switch` | `{name: "<文件名>"}` | 切换账号 |
| POST | `/api/remove` | `{file: "<文件名>"}` | 从 `tw_auth` 删除素材 |
| POST | `/api/refresh` | `{file: "<文件名>"}` | 对目标账号纯 HTTP 续期（走 icube-dc 设备私钥 + refreshToken） |
| POST | `/api/add` | `{name, content}` | 新增账号素材 |

写操作还需要一次性令牌（见下）。

---

## 与 WorkBuddy 切换器的差异

| 对比 | WorkBuddy（wb_*） | Trae（tw_*） |
|------|-------------------|--------------|
| 登录态位置 | `%LOCALAPPDATA%\...\auth\workbuddy-desktop.info`（明文） | `%APPDATA%\TRAE SOLO CN\...\storage.json`（AES 加密） |
| 切换素材 | `wb_auth\*.info`（明文 JSON） | `tw_auth\*.json`（完整 storage.json） |
| 是否重加密 | 否，直接替换 | 否，搬运客户端原生密文 |
| 设备密钥 | 账号内携带 | `icube-dc` 本机共享，保留 |
| 端口 | 8765 | 8766 |

---

## 安全提示

- storage.json 内含**加密登录态**（可解密出 accessToken/refreshToken）；`tw_auth/_trae_tokens.json`
  与签到用 `config-*.json` 是**明文 refresh_token**，均等同长期凭据。**禁止**提交到
  Git / 上传公网 / 转发他人。`.gitignore` 已排除 `tw_auth/`、`tw_backups/`、`output/`。
- 本工具只在本机 `127.0.0.1` 提供服务，不要修改为对外监听。
- 服务对 `Host` / `Origin` 做回环校验，且写操作仅接受 POST，可阻断外部网页的跨站调用；
  但这只是第二道防线，仍不要把端口暴露到局域网。
- **一次性访问令牌**：启动时随机生成、只存在于进程内存，随首页注入前端，写操作必须回传
  `X-Switcher-Token`，否则 401；需要裸接口调试时加 `--no-auth`。
- **审计日志**：切号 / 增删 / 续期追加到 `logs\switcher.log`（只记对象与结果，不含凭据）。
- 切号只在本机客户端生效；账号归属与使用请以官方规则为准。

---

## 依赖

- Python 3（纯标准库，无第三方依赖）。
- 复用上级项目 `自动签到\trae_work_checkin.py` 的
  `decrypt_auth / extract_token / discover_storages / account_from_storage /
  extract_device_id_from_storage / account_uid / refresh_account / load_config` 等。
  若该文件缺失或路径布局变化，请调整 `tw_ui_server.py` 顶部的 `PROJECT_ROOT` 定位逻辑。