# 切换账号时迁移任务与项目（设计说明）

> 目标：切换 WorkBuddy 账号时，让用户选择是否把**任务与项目**一并带到新账号，
> 并保证**原有对话能连续衔接、上下文不丢失**。
>
> 本文基于 2026-09-18 对本机 `~/.workbuddy-ai/` 的实测勘察，不是凭空设计。

---

## 0. 先纠正一个前提：不需要「搬运」对话

"迁移任务和项目"听起来像要把数据搬过去。实测下来**不需要**，而且硬搬会出问题。

`~/.workbuddy-ai/` 的数据分两类：

| 数据 | 存放位置 | 与账号绑定？ |
|---|---|---|
| **对话正文** | `projects/<项目目录 slug>/<会话ID>.jsonl` | ❌ 按「工作目录 + 会话ID」 |
| 会话元信息 | `projects/<项目目录 slug>/<会话ID>.meta.json` | ❌ 同上 |
| 任务 / 步骤 | `tasks/<会话ID>/N.json` | ❌ 按会话ID |
| 变更索引 | `changes-index/<会话ID>.json` | ❌ 按会话ID |
| 产物索引 | `artifact-index/<会话ID>.json` | ❌ 按会话ID |
| 文件历史 / 文件树快照 | `file-history/<会话ID>/`、`file-tree-manifests/<会话ID>.json` | ❌ 按会话ID |
| 会话工作区 | `workspace/sessions/<会话ID>/` | ❌ 按会话ID |
| **会话归属** | `workbuddy.db` → `sessions.user_id` | ✅ **是** |
| 上下文用量 | `workbuddy.db` → `session_usage` | ❌ 按 session_id |
| **账号记忆** | `memory/<uid>_memory.md` | ✅ **是** |
| **账号级设置** | `storage/user-<uid>-personal/`（`scoped` + `global`） | ✅ **是** |
| UI 偏好 | `local_storage/entry_*.info` | ⚠️ 部分（内含 uid） |
| 全局状态 | `user-state.json`、`workspace-state.json`、`settings.json` | ❌ |

`user_id` 就是登录态里的 `account.uid`（OneID 账号 uuid）。本机实测：

```
workbuddy-desktop-ai.info  -> account.uid = 7aac45de-1d55-436c-b779-0317b093c580  (本次对话所在)
workbuddy-desktop.info     -> account.uid = b592a5dd-3f86-4360-b0cc-4df5b196098b  (切换器管的那个)
```

### 结论

**切号后「对话丢了」的真正原因是：正文还在原地，只是 `sessions.user_id` 仍挂着旧账号，
历史列表按 uid 过滤就查不到了。**

所以「迁移」= **改归属键（re-key）**，不是复制文件。代价是一条 `UPDATE` + 两处目录改名，
量级在毫秒到几十毫秒，风险也远低于搬数据。

**这直接决定了交互设计**：不需要"选择要迁移哪些项目"这种重型选择器，
只需要确认"要不要把这 N 个会话的归属改到新账号"。

---

## 1. 交互流程

### 1.1 触发点

用户点击账号列表里的「切换到该账号」后，**先弹确认框，再执行**。

> ⚠️ 必须在**切换前**弹。切完客户端就按新 uid 读了，旧账号的数据在界面上已经看不见，
> 那时候再弹框，用户没法核对"到底要迁什么"。

### 1.2 弹框内容

```
┌─ 切换到 wtnong ──────────────────────────────────────────┐
│                                                          │
│  当前账号 jaynong 下的本地数据                            │
│                                                          │
│    会话      7 个（其中 2 个属于本项目 wb_switcher）       │
│    账号记忆  1 份（254 B）                                │
│    账号设置  4 项                                         │
│    ⚠️ 1 个会话正在运行（本窗口），将自动跳过               │
│                                                          │
│  ──────────────────────────────────────────────────────  │
│  ☑ 迁移会话与任务（改归属，正文不动）      [推荐]         │
│  ☑ 迁移账号记忆 memory                                    │
│  ☑ 迁移账号级设置 storage                                 │
│                                                          │
│  迁移方式   ● 移动（旧账号不再看到）                      │
│             ○ 复制（两个账号都能看到，会各自写入）         │
│                                                          │
│  ☐ 迁移后在旧账号下保留一份副本                            │
│  ☐ 记住我的选择，以后不再询问                              │
│                                                          │
│  冲突时： [两边都保留 ▾]                                  │
│                                                          │
│            [取消]   [仅切换]   [切换并迁移]                │
└──────────────────────────────────────────────────────────┘
```

要点：

- **先扫描、后弹框**：数据量（会话数 / 记忆 / 设置）必须实时算出来给用户看，
  不能写死"将迁移全部数据"这种空话。
- **三个出口**：`取消`（什么都不做）、`仅切换`（保持现状，等于今天的行为）、
  `切换并迁移`（推荐路径）。默认焦点给 `切换并迁移`。
- **运行中的会话单独标出**：`sessions.status = 'working'` 的会话**不迁**（原因见 §3.5）。
  本窗口正在进行的对话就属于这一类。
- **"复制"要给出代价提示**：复制后两个账号各持一份 `user_id`，
  下次再切回来会撞冲突；所以默认是「移动」。

### 1.3 执行与反馈

```
[切换并迁移]
   ↓
① 前置检查（db 可写、无其它迁移在进行、磁盘空间）
   ↓
② 快照（workbuddy.db 一致性备份 + 待改目录 copy 到 .migration-backup/）
   ↓
③ 切号（复用现有 switch_account：轮换备份 → 写盘 → 写后自校验）
   ↓  失败 → 整体中止，什么都不迁（切号本身已回滚）
④ 迁移（一个 SQLite 事务 + 两次目录改名）
   ↓  失败 → 反向恢复 db 快照与目录，报错并保留现场
⑤ 校验（以新 uid 查 sessions；抽查每个会话的 jsonl 首行/末行可读）
   ↓
toast：已切换到 wtnong，并迁移 6 个会话 / 1 份记忆 / 4 项设置
       （1 个运行中的会话已跳过）
```

- **进度**：迁移本身很快，但快照可能要几秒（`workbuddy.db` 135 KB + 4 MB WAL）。
  超过 1 秒就给进度提示，别让用户以为卡死。
- **结果可追溯**：每次迁移写审计日志（`logs/switcher.log`），
  记录 `会话数 / 记忆 / 设置 / 跳过数 / 耗时`，**不记正文内容**。

---

## 2. 迁移选项的默认值

| 选项 | 默认 | 理由 |
|---|---|---|
| 迁移会话与任务 | **开** | 这是"对话接续"的核心；不迁 = 用户在新账号里看不到任何历史 |
| 迁移账号记忆 `memory` | **开** | 记忆决定"它还记不记得之前聊过什么"，不迁等于人格断层 |
| 迁移账号级设置 `storage` | **开** | 量极小（4 项），不迁会导致 UI 行为不一致 |
| 迁移方式 | **移动** | 复制会让两个账号各持一份、各自写入，下次切回来必然冲突 |
| 在旧账号保留副本 | **关** | 默认不留，避免数据与凭据在磁盘上多份散落 |
| 冲突处理 | **两边都保留** | 宁可多留不可误删（见 §3.4） |
| 记住我的选择 | **关** | 迁移是有副作用的操作，默认每次都问 |
| 运行中的会话 | **跳过** | 不提供"强行迁移"选项（见 §3.5） |

> 关于「记住我的选择」：切号是这个工具的高频操作，每次都弹框确实烦。
> 但迁移会改数据归属，静默重放风险太大。折中方案是：
> **记住的只是"选项组合"，不是"自动迁移"** —— 勾了之后弹框仍会出现，
> 只是默认值变成上次的选择，用户一键确认即可。

---

## 3. 迁移范围与冲突处理

### 3.1 范围分档（引用面为实测结果，不是估算）

用会话 ID 和账号 uid 对整个 `~/.workbuddy-ai/` 做全盘 grep，得到真实的引用面：

```
会话 ID 974defc8-…  出现在 93 个文件
账号 uid 7aac45de-… 出现在 37 个文件
```

按「是不是权威源」重新分档：

| 档 | 内容 | 形态 | 处理 |
|---|---|---|---|
| **A 必须迁（权威源）** | `workbuddy.db` → `sessions.user_id` | SQLite | 不迁则新账号看不到任何会话 |
| **A 必须迁** | `storage/skeleton/account-snapshot.json` | JSON | **客户端缓存的"当前账号"**。不改则客户端可能仍认为登录的是旧账号 |
| **A 必须迁** | `settings.json` → `claw.users.<uid>` | JSON 键 | 不改则新账号读不到自己的渠道配置 |
| **B 建议迁** | `memory/<uid>_memory.md` | 文本 | 跨会话记忆 |
| **B 建议迁** | `storage/user-<uid>-personal/` | 目录 | 账号级 UI / 功能开关 |
| **A 必须迁** | `automations` → `owner_user_id`（+ `automation_delivery_outbox`） | SQLite | 定时任务按 `owner_user_id` 隔离；不迁则用户建的自动化在新账号下消失 |
| **B 可迁（实测已解）** | `connectors/<uid>/connector-states.json` | 目录 + JSON | **uid 不参与密钥派生**，`userIdCheck` 可自行重算（§6.4）。⚠️ 但必须重算 —— 漏做会触发"删主文件 + 重新授权" |
| **B 建议迁** | `storage` 里 user scope 的 `ns=conversations, key=pinned`（置顶会话） | JSON key | 不迁则新账号看不到置顶 |
| **D 不需要动** | `app/session/Local Storage/leveldb/` | 二进制（明文 UTF-8） | 里面的 `codebuddy-conversation-status-snapshot` 映射 **key 是 sid 不是 uid**，sid 不变即自动有效（§6.5） |
| **D 不迁（派生 / 缓存）** | `app/session/Cache/`、`sessions/<pid>.json`、`edge-sync-mapping-v3.db` | 二进制 | 进程租约 / 缓存，靠客户端重建 |
| **D 不迁（本来就无关）** | `projects/<cwd-slug>/<sid>.jsonl` 正文、`tasks/`、`changes-index/`、`changes-detail/`、`artifact-index/`、`file-history/`、`file-tree-manifests/`、`workspace/sessions/` | 按 cwd / 会话ID | 迁了反而产生重复副本 |
| **D 不迁（无意义）** | `traces/<pid>/`、`logs/**`、`audit-log/**` | 遥测 / 日志 | 含 sid / uid 但不影响功能；迁它们只会让备份膨胀 |

> ⚠️ 之前设计文档里"账号级只有三处"的说法**是错的**（漏了 `account-snapshot.json`、
> `settings.json` 的账号键、`connectors/<uid>/`）。以本节实测为准。

### 3.1b 会话 ID 的引用面（93 个文件，实测）

**权威引用（必须保持 id 不变，但不搬）**：
```
projects/<cwd-slug>/<sid>.jsonl              对话正文
projects/<cwd-slug>/<sid>.meta.json
tasks/<sid>/N.json
changes-index/<sid>.json
changes-detail/<sid>/*.json
artifact-index/<sid>.json
file-history/<sid>/
file-tree-manifests/<sid>.json
workspace/sessions/<sid>/  (内含 modify_backup/*.workbuddy.db-wal)
sessions/<pid>.json          ← 进程租约：{pid, sessionId, cwd, lastHeartbeat}
```

**非权威引用（不需要动）**：`traces/<pid>/trace_*.json`、`logs/**`、`audit-log/**`。

**二进制引用（动不了）**：`app/session/Local Storage/leveldb/*.ldb|log`、
`edge-sync-mapping-v3.db-wal`。

**D 档必须在 UI 上讲清楚**，否则用户会以为"没搬对话 = 没迁移成功"。
建议在弹框里加一行说明：

> 对话正文保存在项目目录下（`projects\<项目>\<会话ID>.jsonl`），与账号无关，
> 无需搬运 —— 本次只调整它在数据库里的归属。

### 3.2 为什么 `session_id` 绝对不能改

`session_id` 是所有其它数据的**外键**：

```
projects/<cwd-slug>/<sid>.jsonl          ← 文件名
projects/<cwd-slug>/<sid>.meta.json      ← 文件名
tasks/<sid>/N.json                       ← 目录名
changes-index/<sid>.json                 ← 文件名
artifact-index/<sid>.json                ← 文件名
file-history/<sid>/                      ← 目录名
file-tree-manifests/<sid>.json           ← 文件名
workspace/sessions/<sid>/                ← 目录名
session_usage.session_id                 ← 主键
```

**只要 `session_id` 不变，这 9 处引用全部自动接上，一个都不用动。**
这是整个设计"代价极低"的根本原因。任何试图"重新生成会话ID"的方案都会引发
9 处级联改名，且必然漏掉某一处 —— 不做。

### 3.3 冲突场景

| 场景 | 处理 |
|---|---|
| 目标账号已有同名 `session_id` | 理论上不可能（uuid v4 全局唯一）。若真发生（例如上次用了「复制」模式），**保留目标侧**，源侧会话跳过并在结果里列出，让用户手工决定 |
| `memory/<新uid>_memory.md` 已存在 | 默认**合并**：按日期分节拼接 + 去重同标题段落，写回前先备份。用户可选「保留新的 / 保留旧的 / 合并」 |
| `storage/user-<新uid>-personal/` 已存在 | 按 **key 粒度合并**：目标侧已有的 key 不动（保守），只补目标缺失的 key |
| `local_storage` 里同时存在新旧 uid | 默认不动（见 §3.1 C 档） |
| 同一会话在旧账号下 `status='working'` | 跳过（见 §3.5） |
| 目标账号 uid 与源相同 | 直接跳过迁移，等价于「仅切换」 |

### 3.4 冲突处理的默认倾向

**一律偏向"保留"而不是"覆盖"**：多留一份数据的成本是磁盘，误删的成本是用户的对话历史。
所以默认「两边都保留」，且**任何覆盖动作前都先备份到 `.migration-backup/`**。

### 3.5 运行中的会话为什么必须跳过

`sessions.status = 'working'` 的会话（例如**本窗口正在进行的这个对话**）：

- 它的 `.jsonl` 正被当前进程**持续追加写入**；
- 它的 `user_id` 在客户端内存里也有缓存，改库后可能被下一次写回**覆盖**；
- 强行迁移会造成「库里归属已改、内存里还是旧的」的撕裂状态。

**处理**：跳过它，并在结果里明确告知 ——
「1 个正在运行的会话未迁移，结束该对话后再次切换即可带上」。

> 但要注意：**跳过 ≠ 当前对话会断**。当前窗口是按 `session_id + cwd` 读写 jsonl 的，
> 与 uid 无关，所以**本窗口的对话在切号后仍能继续**，只是它暂时不出现在新账号的历史列表里。
> 这一点必须在 UI 上说清楚，否则用户会以为"当前对话要没了"。

---

## 4. 保证对话接续所需保存与恢复的数据

### 4.1 必须保存的（缺一不可）

| # | 数据 | 位置 | 缺失后果 |
|---|---|---|---|
| 1 | **会话归属** `sessions.user_id` | `workbuddy.db` | 新账号历史列表里**看不到**该会话 —— 这就是"对话丢了"的直接原因 |
| 2 | **`session_id` 本身** | `workbuddy.db` 主键 | 一旦改变，9 处引用全部失联（§3.2）。**必须原样保留** |
| 3 | **`sessions.cwd`** | `workbuddy.db` | 决定去 `projects/<哪个项目目录>/` 找正文。切号不改工作目录，因此**保持不变**即可 |
| 4 | **正文** `<sid>.jsonl` | `projects/<cwd-slug>/` | 对话内容本体。**原地不动**，但迁移后必须校验路径仍可解析 |
| 5 | **会话元信息** `<sid>.meta.json` | `projects/<cwd-slug>/` | 列表展示信息（标题等） |
| 6 | **上下文用量** `session_usage` | `workbuddy.db` | 重置会导致 UI 上"上下文占用"显示错误，可能触发不同的压缩/截断阈值 |
| 7 | **账号记忆** `<uid>_memory.md` | `memory/` | 跨会话记忆丢失 → "它不记得我们之前聊过什么了" |
| 8 | **账号级设置** `user-<uid>-personal/` | `storage/` | UI 行为/功能开关回到默认 |

### 4.2 恢复顺序（原子序列）

```
0. 前置
   - 结束或排除 status='working' 的会话
   - 确认没有其它迁移/切号在进行（复用 .locks/wb-desktop.lock）
   - 校验磁盘可用空间 ≥ 待备份数据量的 2 倍

1. 快照（可回滚的前提）
   - workbuddy.db：用 SQLite Backup API 做一致性备份（不要直接 copy，
     有 WAL 时裸拷贝会得到损坏的快照）
   - memory/<旧uid>_memory.md、storage/user-<旧uid>-personal/
     → copy 到 ~/.workbuddy-ai/.migration-backup/<时间戳>/

2. 切号
   - 复用现有 switch_account()（轮换备份 → 写盘 → 写后自校验 → 裁剪）
   - 失败 → 整体中止，什么都不迁

3. 迁移（尽量短，db 部分放一个事务）
   a) BEGIN IMMEDIATE;
      UPDATE sessions SET user_id = <新uid> WHERE user_id = <旧uid>
        AND status <> 'working';                        -- 移动模式
        -- 或 SET user_id = ''                          -- 共享模式（§8.1）
      UPDATE automations SET owner_user_id = <新uid>
        WHERE owner_user_id = <旧uid>;                  -- 实测新增（§6.8）
      UPDATE automation_delivery_outbox SET owner_user_id = <新uid>
        WHERE owner_user_id = <旧uid>;
      COMMIT;
   b) memory：<旧uid>_memory.md → <新uid>_memory.md（冲突则合并）
   c) storage：user-<旧uid>-<type> → user-<新uid>-<type>
      （<type> 是 enterpriseId 或 'personal'，**不能写死**，见 §6.9；按键合并）
      含 ns=conversations/key=pinned 的置顶数据
   d) connectors/<旧uid>/ → connectors/<新uid>/
      + accountIdentityKey 改新 uid
      + **重算 userIdCheck = base64(sha256(新uid + salt)[:16])**（§6.4）
   e) settings.json → claw.users 的键改名（读-改-写）
   f) storage/skeleton/account-snapshot.json → uid / nickname / savedAt 更新

4. 校验
   - 以 <新uid> 查 sessions，会话数应等于预期
   - 抽查每个会话：<sid>.jsonl 存在、首行与末行都能 json.loads
   - 读回 memory / storage 确认新路径可访问

5. 失败回滚
   - db：从步骤 1 的快照还原
   - 目录：.migration-backup/ 里的副本改回原名
   - 切号：复用 _restore_backup() 回滚到切换前的登录态
   - 全程保留现场，不删 .migration-backup/，报错里给出路径
```

### 4.3 幂等与重试

迁移必须**可重入**：

- 若上次迁到一半失败，`UPDATE ... WHERE user_id = <旧uid>` 天然幂等 —— 重跑只会处理剩下的；
- 目录改名用 `os.replace`，已改过的直接跳过；
- 重跑前先检测 `.migration-backup/` 是否残留，残留则提示用户"上次迁移未完成，是否继续/回滚"。

### 4.4 不需要保存/恢复的（避免过度设计）

- **对话正文的副本**：不复制。复制会产生两份 `jsonl`，
  两端各自追加写入 → 内容分叉，比不迁更糟。
- **`local_storage` 的 uuid 替换**：不做（§3.1 C 档）。
- **`projects/` 目录本身**：按 cwd 存，切号不影响。

---

## 5. 落地时的技术约束（实测踩点）

1. **`workbuddy.db` 有 4 MB WAL**，且客户端随时可能写。
   迁移必须在客户端**空闲**时做。实测（§6.1）会话查询是**惰性实时读**的，
   所以改完库**不需要重启**就能看到；但 `account-snapshot.json` 是**冷启动**读的，
   那一份要重启才刷新 —— 所以**建议迁移后重启一次**，让快照与库对齐。
2. **本机 Python 被注入 WorkBuddy 的安全删除 shim**，删除会抛 `SystemExit`
   （`BaseException`）。迁移里的所有"删旧目录/清备份"动作必须走
   `switcher_common.safe_unlink` / `prune_backups`（已修好）。
3. **文件锁**：迁移与切号共用 `wb-desktop.lock`，避免与自动续期/另一路切号交叉。
4. **本工具目前只接管 `workbuddy-desktop.info`**；`workbuddy-desktop-ai.info`
   （本窗口所在的那个账号）不在管理范围内。若将来要支持，`uid` 的取值口径要一并扩展。

---

## 6. 实测结论（5/5 已完成，2026-09-18）

方法：**静态分析客户端 bundle**（`C:\Program Files\WorkBuddyAI\resources\app.asar`，
这个构建带可读源码与注释）+ 只读 db schema + LevelDB 物理格式判断。
**全程零数据变更**。5 项里 **3 项推翻了先前的保守判断**。

### 6.1 改 `sessions.user_id` 后需要重启客户端吗？—— **不需要**

```js
/** 当前用户 id 惰性读取，保证账号切换后查询立刻生效。 */
currentUserId() { return this.context.accountProvider?.()?.uid?.trim() || void 0; }
```

可见性谓词是**每次查询实时拼 SQL** 执行的，不是启动时缓存结果集：

```sql
SELECT ... FROM sessions
WHERE deleted_at IS NULL AND (user_id = ? OR user_id = '')
ORDER BY COALESCE(last_activity_at, updated_at) DESC
```

→ 改完 `user_id`，只要登录态已经切过去（客户端 watcher 会 reconcile），
**刷新列表即可看到**。

**但 `account-snapshot.json` 例外** —— 它的注释写明是「主进程**冷启动**关键路径上同步读盘
并通过 loadFile 的 URL query 注入 renderer」，所以那份快照**要重启才刷新**。

### 6.2 `memory` 按文件名还是按库读？—— **按文件名**

```js
function buildArchiveFileName(uid, legacy = false) {
  const suffix = legacy ? LEGACY_ARCHIVE_SUFFIX : PRIMARY_ARCHIVE_SUFFIX;   // "_memory.md"
  return `${sanitizeFilename(uid)}${suffix}`;
}
```

路径解析器注释：**「纯字符串拼接，零 I/O」**，读取优先级 `memory/` → `memery/`（历史拼写错误），
写入只写 `memory/`。

→ **改文件名即可，不需要动库。** 另注意 `sanitizeFilename()` 会把 `\ / : * ? " < > |`
和控制字符替换成 `_` —— 本机 uid 是纯 uuid，不受影响。

### 6.3 `session_usage` 是否含账号维度？—— **不含，不用迁**

```sql
CREATE TABLE session_usage (
  session_id TEXT PRIMARY KEY, used INTEGER NOT NULL, size INTEGER NOT NULL,
  updated_at INTEGER NOT NULL, credit_json TEXT
);
```

没有 `user_id` 列，**也没有指向 sessions 的外键**。`credit_json` 里是
`{"<modelId>": <数值>}`，同样无账号维度。

### 6.4 `connectors` 的 `userIdCheck` 是 uid 参与密钥派生吗？—— **不是，可迁**

**这是最出乎意料的一项，推翻了我上一版的结论。**

```js
function computeCheck(input, salt) {
  return crypto.createHash("sha256").update(input).update(salt).digest()
         .subarray(0, CHECK_BYTES).toString("base64");
}
function createHeader(userId, masterKey) {
  const salt = crypto.randomBytes(SALT_BYTES);
  return { scheme:"aes-256-gcm", kdf:"hkdf-sha256", salt: salt.toString("base64"),
           userIdCheck: computeCheck(Buffer.from(userId, "utf8"), salt),  // 仅校验值
           keyCheck:    computeCheck(masterKey, salt) };                  // AES key 来源
}
```

- **AES key 来自 `masterKey`，uid 完全不参与密钥派生。**
- `userIdCheck` 只是"这文件属不属于当前账号"的**纯 SHA-256 校验值**，**无密钥、可复现**。

**实测复现（4/4 全部完全一致）：**

```
computeCheck(uid, salt) = base64( sha256(uid_bytes + salt)[:16] )

1271b467-…  stored=AVRScY1lp7vfqZ0HkFEK/Q==  计算=AVRScY1lp7vfqZ0HkFEK/Q==  ✅
7aac45de-…  stored=Faap1plU32jEc9jp7xOWGQ==  计算=Faap1plU32jEc9jp7xOWGQ==  ✅
b0ca8ca7-…  stored=N/NNGfLGPk9Zp7PHR3stDA==  计算=N/NNGfLGPk9Zp7PHR3stDA==  ✅
f19b102d-…  stored=/HUVOJpdLaGijnZPZ2ikZw==  计算=/HUVOJpdLaGijnZPZ2ikZw==  ✅
```

→ **connectors 可以迁移**，三步：
1. 目录改名 `connectors/<旧uid>/` → `connectors/<新uid>/`
2. `accountIdentityKey`: `<旧uid>||<type>` → `<新uid>||<type>`
3. **用新 uid + 原 salt 重算 `userIdCheck`**（算法已验证）
4. `salt` / `keyCheck` **不动**（与 uid 无关）

**但要注意 `verifyHeader` 的失败行为**：`userId-mismatch` 会让调用方
**拒绝解密、删主文件、引导用户重新授权** —— 所以第 3 步漏做 = 用户连接器配置被删。
这是"必须重算"而不是"可选"的原因。

### 6.5 LevelDB 里的 sid / uid 是明文吗？能改吗？—— **明文，但根本不需要改**

`app/session/Local Storage/leveldb/` 下的 `.ldb` / `.log` 里内容**是明文 UTF-8**
（grep 能直接命中，周围是可读 JSON）。实测命中：

```
key:   codebuddy-conversation-status-snapshot
value: {"974defc8-650e-4bfd-b067-42fbd6ea7f5a":"planning"}
```

**关键**：这个映射的 **key 是 sid，不是 uid**。
迁移不改 `session_id` → **该映射自动继续有效，LevelDB 完全不用动。**

uid 只出现在**按账号分的 UI 偏好 key** 里（如 `…-effort:by-model:<uid>`）。
不改的后果仅是"新账号用默认偏好"，**不影响功能、不影响会话可见性**。

→ **上一版把它列为"最大技术障碍 / 高风险不可改"，是错的。** 它其实是"零改动项"。

### 6.6 附带发现：官方自己有迁移框架，但不是账号迁移

bundle 里有 `packages/history-migration/`：

```
PATHS: { CODEBUDDY_BASE: ".codebuddy", SESSIONS_DIR: "projects",
         FILE_HISTORY_DIR: "file-history", BLOBS_DIR: "blobs", TODOS_DIR: "todos",
         TASKS_DIR: "tasks", BACKUP_DIR: "backup",
         MIGRATION_HISTORY_DIR: ".migration-history",
         MIGRATION_STATE_FILE: ".migration-state.json" }
```

它是 **CodeBuddy → WorkBuddy 的目录迁移**（产品改名），带 `sourceHash` 幂等校验。
**官方没有"切号迁移会话"的功能** —— 我们做的是官方没做的事。

可借鉴它的两点做法：
- **状态文件 + 幂等哈希**（`.migration-state.json` + sourceHash）避免重复迁移；
- **回归最小化**：置顶数据迁移时"只碰 `ns=conversations, key=pinned` 这一个 key，
  避免连带搬运同 ns 下的其它未知 key"（原注释）。

### 6.7 附带发现：两处归属语义**相反**，不能一刀切

| 对象 | 空归属的语义 | 出处 |
|---|---|---|
| `sessions.user_id = ''` | **对所有账号可见**（共享） | SQL: `(user_id = ? OR user_id = '')`，注释「迁移遗留空 userId 也归当前用户可见」 |
| `automations.owner_user_id` 空 + `owner_status='legacy_unassigned'` | **被隐藏**（fail-closed） | 注释「无主遗留任务与他人任务一律隐藏，避免个人版遗留自动化在切换到企业账号后仍展示并被自动调度」 |

→ 这直接影响设计：**「置空 user_id 让两个账号都能看到」对 sessions 成立，对 automations 不成立。**

### 6.8 附带发现：`automations` 也是账号级（本机 0 行）

```sql
CREATE TABLE automations (
  ...
  owner_user_id TEXT,
  owner_status TEXT NOT NULL DEFAULT 'legacy_unassigned',
  ...
);
CREATE INDEX idx_automations_owner ON automations(owner_user_id, owner_status, deleted_at);
```

定时任务按 `owner_user_id` 隔离，`automation_delivery_outbox.owner_user_id` 同理。
本机 0 行，但**迁移必须带上**，否则用户建的自动化在新账号下消失。

### 6.9 附带发现：`storage` 目录名的真实规则

`deriveStorageScope(subject)` + `PERSONAL_ENTERPRISE = "personal"` +
`SCOPED_PARENT_DIR = "scoped"` + `GLOBAL_SCOPE_DIR = "global"`。

→ `user-<uid>-personal` 里的 `personal` 是**企业 ID 占位符**（个人账号）。
**企业账号的目录名会是 `user-<uid>-<enterpriseId>`** —— 迁移要按实际目录名处理，
不能写死 `-personal`。

另外：**置顶会话**存在 user scope storage 的 `ns=conversations, key=pinned`
（值 `{id, groupKey}[]`）—— 属账号级，迁移要带上，否则新账号看不到置顶。

---

## 7. 需要修改和优化的部分（改动清单）

> 现状：`switch_account()` 只做「轮换备份 → 写盘 → 自校验 → 裁剪」，
> 完全不碰 `~/.workbuddy-ai/`。迁移是一块**全新能力**，且必须与切号在同一个临界区里。

| # | 改动点 | 落在哪 | 为什么必须 | 不改的后果 |
|---|---|---|---|---|
| 1 | 新增 `account_migration.py` | 新文件 | 迁移逻辑（扫描 / 快照 / 改键 / 校验 / 回滚）与切号解耦，便于单独测 | 塞进 `wb_ui_server.py` 会让它更难维护，且无法独立回归 |
| 2 | `switch_account()` 增加 `migrate` 参数 | `wb_ui_server.py` | 迁移必须与切号共用同一把 `wb-desktop.lock`、同一失败回滚序列 | 两段操作之间被另一路切号/续期插入 → 撕裂 |
| 3 | 新增 `GET /api/migrate-preview` | `wb_ui_server.py` Handler | 弹框前要**实时扫描**待迁数据量 | 用户盲选，弹框只能写"将迁移全部数据"这种空话 |
| 4 | `POST /api/switch` 接受 `migrate` 选项 | 同上 | 把用户的勾选传下来 | 无法按用户意愿迁移 |
| 5 | **客户端进程检测**（`WorkBuddyAI.exe` 是否在跑） | `account_migration.py` | **这是硬前提**：客户端在跑时 LevelDB 被占用、租约与快照会被写回（§9） | 迁移会静默半成功，甚至写坏 LevelDB |
| 6 | `sqlite_backup()` | `switcher_common.py` | 有 4 MB WAL，**裸 copy 会得到损坏快照** | 回滚不可能 —— 一旦迁移出错就是不可逆的数据损坏 |
| 7 | 弹框 UI + 选项 + 结果反馈 | `ui_template.html` | 交互主体 | — |
| 8 | 新增 `{{MIGRATE}}` 占位符 | **两个** `UI_CONTEXT` | 模板共用；tw 侧要隐藏 | 模板残留 `{{MIGRATE}}` → 自检第 12 段直接 FAIL |
| 9 | 回归断言（临时 db + 临时目录） | `smoke_test.py` | 迁移碰的是**真实对话数据**，没有回归就等于裸奔 | 一次写坏 = 用户所有历史对话 |
| 10 | 重新打包两个 exe | — | `dist\` 不跟随源码 | 桌面版没有这个功能 |
| 11 | README / 本文档同步 | — | 迁移是不可逆操作，必须让用户看懂前提 | 用户不知道要先退客户端 |

**改动量估计**：新增约 400–600 行（`account_migration.py` + UI），
`wb_ui_server.py` 改动约 60 行，`switcher_common.py` 约 30 行。

---

## 8. 归属问题与兼容性问题

### 8.1 归属是**零和的**：一个会话只能属于一个账号

`sessions.user_id` 是**单值外键**，不是多对多。所以：

- **移动** = 改键 → 旧账号立刻失去这些会话；
- **复制** = 必须**新建 session_id** + 复制正文 → 之后两个账号各自往自己那份追加，
  **内容必然分叉**；而且 93 处引用要复制两遍。

> **结论：应砍掉「复制」选项。** 上一版设计里给的"复制（两个账号都能看到）"在数据模型上
> 不成立 —— 它要么分叉，要么只是"移动 + 旧账号保留一份只读快照"。
> 如果确实要保留旧账号可见性，正确做法是**迁移前整包备份**，而不是让两个账号共享会话。

**但实测发现了第三条路（§6.7）**：`sessions` 的可见性谓词是
`(user_id = ? OR user_id = '')`，注释写「迁移遗留空 userId 也归当前用户可见」。
即 **`user_id = ''` 是官方认可的"公共会话"语义 —— 置空后所有账号都能看到同一份会话，
且能继续写同一份 jsonl**。这恰好就是用户想要的"两个账号无缝继续同一个任务"。

| 模式 | 实现 | 旧账号 | 新账号 | 正文 |
|---|---|---|---|---|
| 移动（默认） | `user_id` 旧→新 | 看不到 | 看得到 | 同一份 |
| **共享（可选）** | `user_id` → `''` | 看得到 | 看得到 | 同一份，**不会分叉** |
| ~~复制~~ | 新建 sid + 复制正文 | 看得到 | 看得到 | **两份，必然分叉** |

→ 建议把「复制」换成**「共享（置空归属）」**。代价：两个账号都能看到并可写同一会话，
需要提示"同时只用一边"；收益：真正做到"无缝继续"。

> ⚠️ 该语义只对 `sessions` 成立。`automations` 的 `owner_user_id` 置空 + `legacy_unassigned`
> 是 **fail-closed 隐藏**（§6.7），语义相反，不能照搬。

**所以"无缝继续"的正确语义是「归属转移」**：会话本体（正文 + 所有派生索引）原地不动，
只把归属从旧账号改到新账号。新账号打开它时，读到的是同一份 jsonl、同一个上下文用量，
所以进度天然连续 —— 这正是这个方案能做到"无缝"的原因。

### 8.2 `cwd` 是项目身份，跨机器会断链

`projects/<cwd-slug>/` 的目录名是**工作目录的 slug**（如 `d-AI项目-wb_switcher`）。
`session_id` 不变的前提下，只要 `cwd` 不变就能找到正文。

- **同机切号**：`cwd` 不变 → 安全。
- **换盘符 / 换机器 / 改项目路径**：`cwd` 变了 → 去新 slug 目录找不到 jsonl → 会话"空壳"。

**必须做**：迁移后校验 `projects/<slug>/<sid>.jsonl` 真实存在；
不存在就把该会话标记为"正文缺失"并在结果里列出，**不要**静默成功。

### 8.3 连接器状态：**可以迁**，但必须重算 `userIdCheck`（实测已推翻旧结论）

> **本节的旧结论（"uid 参与密钥派生，迁过去会解不开"）已被 §6.4 的实测推翻。**
> `computeCheck` 是纯 `sha256(uid + salt)[:16]`，**没有密钥**；AES key 来自 `masterKey`。

真正的机制是 `verifyHeader`：

```js
if (computeCheck(Buffer.from(userId,"utf8"), salt) !== header.userIdCheck)
    return { ok:false, reason:"userId-mismatch" };   // 调用方：拒绝解密、删主文件、引导重新授权
```

所以**风险不是"解不开"，而是"校验不过 → 配置被删"**。
迁移时只要**用新 uid + 原 salt 重算 `userIdCheck`** 就完全正常（算法已验证，4/4 复现一致）。

→ **改为可迁**。但这一步是**强制项不是可选项** —— 漏做等于把用户的连接器配置删掉。
建议迁移后立刻调一次 `verifyHeader` 等价校验（我们自己在 Python 侧算一遍比对）再落盘。

### 8.4 `account-snapshot.json` 不改会"认错人"

```json
{ "primary": { "uid": "7aac45de-…", "nickname": "jaynong", "type": "personal",
               "editionType": "free", "isPro": false, "savedAt": 1789668359625 } }
```

这是客户端**启动时读的当前账号快照**。切号后不更新，客户端可能仍按旧账号的
`editionType` / `isPro` 走功能分支 → 表现为"切过去了但能力还是旧账号的"。
**必须与 `sessions.user_id` 一起改**，改完 `savedAt` 刷新。

### 8.5 `settings.json` 的账号键

`settings.json` → `claw.users.<uid>` 是**按 uid 分键**的渠道配置（每个账号一套）。
迁移时要把它改名到新 uid，否则新账号读不到自己的配置、旧账号的配置还挂着。

注意这是**全局文件**，改它要**读-改-写**（不能整文件覆盖），且要防并发。

### 8.6 记忆合并是**语义**问题，不只是技术问题

两个账号各自积累的 `memory.md` 拼在一起，可能出现互相矛盾的条目
（比如对同一个项目的不同结论）。**默认应该让用户确认**，而不是静默合并。
建议默认给「保留新的 / 保留旧的 / 合并（分节去重）」三选一，默认**合并**但把结果摘要展示出来。

---

## 9. 风险点

| # | 风险 | 触发条件 | 影响 | 缓解 |
|---|---|---|---|---|
| 1 | **客户端在运行** | 没退出就迁 | LevelDB 被占用改不了；`sessions/<pid>.json` 租约、`account-snapshot.json` 被内存态写回；`working` 会话撕裂 | **硬前提：检测到客户端进程就拒绝迁移**，提示先退出 |
| 2 | **db 快照损坏** | 直接 copy 带 WAL 的 db | 回滚不可能 → 不可逆损坏 | 用 SQLite Backup API（`con.backup()`） |
| 3 | **三处改动无法原子** | db + 目录 + 登录态 跨资源 | 中途失败可能"半迁" | 快照 + 反向补偿 + **保留现场** + 报错里给出手工恢复命令 |
| 4 | **锁持有时间过长** | 快照 4 MB WAL + 目录改名 | 自动续期任务的 `file_lock` 15s 超时 → 续期失败 | 迁移用**独立的锁**，或迁移期间跳过续期；并把 `file_lock` 超时调大 |
| 5 | **备份里含完整对话正文** | `.migration-backup/` 复制了 jsonl | 敏感数据在磁盘上多一份 | 明确**不复制正文**（正文不迁）；备份只含 db 快照 + memory + storage，成功后提示清理 |
| 6 | **迁移不可重入** | 中断后重跑 | 重复迁移或漏迁 | `UPDATE ... WHERE user_id=<旧>` 天然幂等；目录改名用 `os.replace`；重跑前检测残留备份 |
| 7 | ~~LevelDB 改写失败~~ **已排除** | — | — | 实测：LevelDB 里是 sid→status 映射，**key 是 sid 不是 uid**，sid 不变即自动有效，**无需改动**（§6.5） |
| 8 | **`connectors` 校验失败导致配置被删** | 迁了目录但**漏做 `userIdCheck` 重算** | `verifyHeader` 返回 `userId-mismatch` → 调用方**删主文件 + 引导重新授权**，用户连接器配置丢失 | 迁移后**必须重算**（§6.4）；落盘前先自校验一遍 |
| 8b | **`automations` 漏迁** | 只迁了 sessions | 用户建的定时任务在新账号下消失（且 `legacy_unassigned` 是隐藏语义，不会自动可见） | 一并 `UPDATE automations SET owner_user_id`，并处理 `owner_status` |
| 9 | **迁移期间用户又点了一次切换** | UI 未禁用按钮 | 两路迁移并发 | 前端禁用按钮 + 后端文件锁；迁移中 `/api/switch` 返回"迁移进行中" |
| 10 | **磁盘空间不足** | 快照 + 备份 | 快照写一半 → 回滚材料不完整 | 前置校验可用空间 ≥ 待备份量 × 2 |

---

## 10. 各项改动对迁移结果的影响

用「迁移完整度 / 风险 / 成本」三个维度看每一项改动的取舍：

| 改动 | 对迁移完整度的影响 | 风险 | 成本 | 建议 |
|---|---|---|---|---|
| 改 `sessions.user_id` | **决定性** —— 不做则新账号看不到任何会话 | 低 | 极低（1 条 UPDATE） | **必做** |
| 改 `account-snapshot.json` | 高 —— 不做则新账号"认错人"、能力分支错误 | 低 | 极低 | **必做** |
| 改 `settings.json` 的账号键 | 中 —— 不做则渠道配置丢失 | 低（读-改-写要防并发） | 低 | **必做** |
| 迁 `memory/<uid>_memory.md` | 高 —— 不做则"不记得之前聊过什么" | 中（合并有语义冲突） | 低 | **必做**，合并需用户确认 |
| 迁 `storage/user-<uid>-personal/` | 中 —— 不做则 UI 开关回默认 | 低 | 低 | 做 |
| **要求客户端退出** | **决定成败** —— 不做则上面几项都可能被内存态覆盖 | — | 需要用户配合 | **必做，作为硬前提** |
| 用 SQLite Backup API 做快照 | 决定**可回滚性** | — | 低 | **必做** |
| 迁移后校验 jsonl 存在 | 中 —— 不做则"空壳会话"静默成功 | 低 | 低 | **必做** |
| 迁 `connectors/<uid>/` | 中 —— 不做则连接器需重新授权 | **低**（实测 uid 不参与密钥派生，重算校验值即可） | 低（十几行） | **做**（§6.4，推翻旧结论） |
| 迁 `automations` 的 `owner_user_id` | 中 —— 不做则定时任务在新账号消失 | 低 | 极低（1 条 UPDATE） | **做** |
| 迁置顶 `ns=conversations, key=pinned` | 低 —— 不做则新账号看不到置顶 | 低 | 低 | 做 |
| 改 Chromium LevelDB | **无** —— 实测不需要改 | **无** | 零 | **不做**（§6.5，推翻旧结论） |
| 迁 `traces/`、`logs/`、`edge-sync-mapping-v3.db` | 无 | 低 | 高（体积大） | **不做** |
| 提供「复制」模式 | 负 —— 会造成内容分叉 | 高 | 高 | **砍掉**，换成「共享（置空 user_id）」（§8.1） |

**一句话**：真正决定"能不能无缝继续"的只有 **`sessions.user_id` 一处**；
其余都是"锦上添花"或"必须避开的坑"。这也意味着**收益/成本比极高** ——
核心功能只要一条 UPDATE。

---

## 10b. 实现状态（2026-09-18）

**阶段 1 已实现并验证。**

| 交付物 | 说明 |
|---|---|
| `account_migration.py`（新，约 560 行） | 目录判定 / 进程检测 / 扫描预览 / 迁移 / 校验 / 回滚 / 备份裁剪 |
| `wb_ui_server.py` | `switch_account(name, migrate)` 返回 3 元组；新增 `current_uid()` / `migrate_preview()`；`GET /api/migrate-preview`；`POST /api/switch` 接受 `migrate`；CLI `--migrate` / `--migrate-mode` / `--migrate-preview` |
| `ui_template.html` | 迁移确认弹框（数据清单 / 冲突 / 警告 / 选项 / 移动-共享 / 三出口）；`{{MIGRATE}}` 占位符（tw 侧为空即隐藏） |
| `smoke_test.py` 第 14 段 | 28 条断言：算法复现、目录判定、进程检测、完整迁移、校验、回滚、接口与模板。**总计 159 项全 PASS** |
| 两个 exe | 已重建，`account_migration` 已确认打进包（接口返回正常 JSON 而非 ImportError） |

### 实现期新发现的两个真 bug（自检抓到的）

1. **`accountIdentityKey` 是三段式** `<uid>|<企业简称>|<账号类型>`。
   最初用 `split("||")[0]` 取 uid —— 企业简称为空时（`uid||enterprise`）碰巧对，
   但**企业简称非空**时（实测有 `<uid>|fwhxtrpramm8|enterprise`）会取到整串，
   算出的 `userIdCheck` 必然不符 → 客户端判 `userId-mismatch` → **删掉用户的连接器配置**。
   已改为 `split("|")[0]` 并原样保留后两段。

2. **`tasklist` 输出是 GBK，用 `text=True` 会在读取线程里抛 `UnicodeDecodeError`**，
   异常不冒到调用方，`stdout` 变空 → **"检测不到进程"被静默当成"没有进程在跑"**。
   实测本机 12 个 `WorkBuddyAI.exe` 在跑却返回 `[]`，等于绕过了迁移的硬前提。
   已改为 `bytes` + `errors="replace"`，并把子串匹配改成 **CSV 第一列精确比对**
   （否则 `workbuddy.exe` 会被 `workbuddyai.exe` 误命中）。

### 与设计的偏差

- **`find_data_root` 以数据自身归属为准，不信任 `WORKBUDDY_CONFIG_DIR`。**
  实测本机有两个结构完全相同的客户端数据目录（`~/.workbuddy` 与 `~/.workbuddy-ai`），
  而该环境变量在切换器进程里未必存在、在别的进程里可能是 `-ai`。
  照抄它会迁移错目录 —— 已改为按 `account-snapshot.json` 的 uid 与库里会话归属判定。
- **迁移放在切号成功之后**（原设计即如此），失败不回滚切号，避免
  "数据已归新账号、人却还登着旧账号"。

### 尚未实现（阶段 2/3）

- `settings.json` 的读-改-写并发保护（目前是单进程串行 + 文件锁，够用）
- 记忆合并的交互式确认（目前默认合并 + 结果里给明细）
- `--dry-run`
- 迁移前对"客户端确实退出"做二次确认（目前是检测到就拒绝）

---

## 11. 建议的落地顺序

**阶段 1（核心，覆盖绝大部分价值）**
1. 客户端进程检测 + 拒绝迁移提示
2. SQLite Backup API 快照
3. 改 `sessions.user_id`（排除 `status='working'`）
4. 改 `automations` / `automation_delivery_outbox` 的 `owner_user_id`
5. 改 `account-snapshot.json`
6. 迁 `memory` + `storage`（含 pinned；目录名按实际 `<type>`，冲突默认保留）
7. 改 `settings.json` 的 `claw.users.<uid>`
8. 校验 + 回滚 + 审计

**阶段 2**：`connectors` 迁移（含 `userIdCheck` 重算 + 落盘前自校验）；
`settings.json` 的读-改-写并发保护；记忆合并的交互确认。

**阶段 3（可选增强）**：提供「共享（置空 `user_id`）」模式（§8.1）；
`--dry-run` 预览。

**明确不做**：`traces/`、`logs/`、`edge-sync-mapping-v3.db`、`app/session/Cache/`、
Chromium LevelDB（实测不需要）、以及任何形式的"复制会话"。

---

## 12. 实测结论对方案的净影响

| 先前的判断 | 实测后 | 净影响 |
|---|---|---|
| 改库后可能要重启 | **不需要**（惰性实时读），仅快照要重启 | 体验更好 |
| `memory` 可能按库读 | **按文件名** | 少改一处 |
| LevelDB 是最大技术障碍、高风险 | **不需要动** | **删掉一个高风险项** |
| `connectors` 不能迁 | **可迁**，重算校验值即可 | 多一项能力，但**漏做会删用户配置**，风险性质变了 |
| 账号级只有 3 处 | **至少 6 处**（+automations / +settings 键 / +snapshot / +pinned） | 工作量增加 |
| 「复制」是备选 | **数据模型不成立**，但发现了「置空 user_id」这条官方语义 | 选项重新设计 |

**结论**：核心路径（一条 UPDATE + 几处改名）依然极简，
但**"完整迁移"的边界比预想的大**，且新增了一个"漏做就删数据"的强制项（`userIdCheck`）。
建议按 §11 阶段 1 先做，把 `connectors` 放阶段 2 单独验证。
