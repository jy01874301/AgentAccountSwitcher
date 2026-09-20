# 客户端主窗口「自动还原」问题诊断报告

- 日期：2026-09-20
- 对象：WorkBuddyAI（国际服）桌面客户端主窗口，兼及 WorkBuddy（国服）
- 客户端版本：appVersion `5.5.2` / Electron `37.10.3` / Chrome `138.0.7204.251`
- 触发描述（用户原话）：「每次运行任务时，WorkBuddyAI 桌面客户端的窗口都会自动还原（重置窗口大小与位置）」
- 交付物：`window_state_guard.py`（诊断 + 修复工具）

---

## 一、结论（TL;DR）

1. **窗口几何只有一个持久化文件**，位于客户端自己的 userData 下：

   | 通道 | 产品 | 状态文件 |
   | --- | --- | --- |
   | `wb` | WorkBuddy（国服） | `%USERPROFILE%\.workbuddy\app\window-state.json` |
   | `wbai` | WorkBuddyAI（国际服） | `%USERPROFILE%\.workbuddy-ai\app\window-state.json` |

2. 该文件里的 `bounds` **不是当前窗口尺寸**，而是 `win.getNormalBounds()` —— **最大化之前的「还原尺寸」**。窗口恢复顺序是「按 `bounds` 建窗（隐藏）→ `ready-to-show` 时 `maximize()` → `show()`」。

3. **因此 `bounds` 就是「窗口被还原后的大小与位置」的唯一来源。** 修复前它被记成了一个偏小的陈旧值：

   - `wbai`：**812×607 @ (148,92)**，`isMaximized:true`
   - `wb`：**720×544 @ (40,36)**，`isMaximized:true`（已低于客户端最小尺寸 800×600）

   **已修复为 `1600×1000 @ (224,56)` + 保持最大化，两个通道的文件与运行中窗口都已对齐并通过复核（`check` rc=0）。**

4. **这不是本项目（wb_switcher）造成的**，本项目代码不读不写该文件、也不重启客户端（已 grep 确认）。

5. **纯粹「跑任务」本身不会触发**：75 秒采样中，窗口矩形与文件 mtime 全程零变化（见 §三·证据 4）。真正会触发的是**窗口离开最大化状态**的那些时刻 —— 拖标题栏 / 双击标题栏 / `Win+↓` / 贴边 / 应用重启后 `show()` 的那一瞬。此时窗口落到 812×607 @ (148,92)，观感就是「被自动还原」。

6. **根因性质**：客户端「只记录、不校验」的持久化设计 —— `bounds` 一旦被记成小值就永久钉死，因为用户平时总让窗口保持最大化，**永远不会在「非最大化」状态下产生新的 `resize`/`move` 事件**，那个小值就再也没有机会被更新。

---

## 二、机制（源码级，来自客户端 `app.asar` 内 `src/main/window/window-state.ts`）

### 2.1 写入方（全应用唯一）

```js
function saveWindowState(win, persistFullscreen = false) {
    if (win.isDestroyed()) return;
    try {
        const bounds = win.getNormalBounds();          // ← 还原尺寸，不是当前尺寸
        let isFullScreen = win.isFullScreen();
        if (!persistFullscreen) try {
            const raw = fs.readFileSync(stateFilePath(), "utf-8");
            const existing = JSON.parse(raw);
            if (existing.version === 2) isFullScreen = existing.isFullScreen;
        } catch {}
        const state = { version: 2, bounds, isMaximized: win.isMaximized(), isFullScreen };
        fs.mkdirSync(path.dirname(stateFilePath()), { recursive: true });
        fs.writeFileSync(stateFilePath(), JSON.stringify(state));
    } catch {}
}
```

- 触发点：`mainWindow.on("resize", scheduleSaveState)` 与 `mainWindow.on("move", scheduleSaveState)`，**500ms 去抖**。
- `stateFilePath()` = `path.join(electron.app.getPath("userData"), "window-state.json")`。
- 整个 asar 中 `window-state.json` 只有这一个写入方（字符串出现 4 次，其中 1 次写、1 次读）。

### 2.2 读取方（只在建窗时）

```js
function loadWindowState() {
    const fallback = { bounds: centerOnPrimary({width:1200, height:800}),
                       isMaximized: false, isFullScreen: false };
    try {
        const state = JSON.parse(fs.readFileSync(stateFilePath(), "utf-8"));
        if (state.version !== 2 || !state.bounds) return fallback;
        if (!isVisibleOnAnyDisplay(state.bounds)) { /* 居中到主屏 */ }
        return { bounds: state.bounds, isMaximized: state.isMaximized, isFullScreen: state.isFullScreen };
    } catch { return fallback; }
}
```

### 2.3 恢复顺序

```js
// createWindow()
const savedState = loadWindowState();
this.mainWindow = new BrowserWindow(buildMainWindowOptions({ savedState, isMac, isWindows }));
//   buildMainWindowOptions: { ...savedState.bounds, minWidth: 800, minHeight: 600, show: false, ... }

// ready-to-show
if (ctx.savedState.isFullScreen) this.mainWindow?.setFullScreen(true);
else if (ctx.savedState.isMaximized) this.mainWindow?.maximize();
this.mainWindow?.show();
```

**注意顺序**：窗口先以 `bounds`（= 812×607）创建并保持隐藏，等 `ready-to-show`（本次实测约 **+1.5s**）才 `maximize()` + `show()`。这段窗口期内，Windows 侧的还原矩形就已经是 `bounds`。

### 2.4 客户端**不会**主动改窗口几何（已逐条排除）

| 可能的改动点 | 实际情况 |
| --- | --- |
| `setBounds(` | 全 asar 仅 **1** 处：`display-metrics-changed` 的越界钳制，且前置 `if (isMaximized() \|\| isFullScreen() \|\| isMinimized()) return;` |
| `setPosition(` | 仅 `WindowDragController.tick()`；`start()` 前置 `if (win.isFullScreen() \|\| win.isMaximized()) return;` → 最大化时不会拖 |
| `setMinimumSize` | 1 处，同在 `display-metrics-changed` 分支 |
| `maximize()` | 3 处：建窗恢复、`WindowManager.maximize()`（**是 toggle**：已最大化则 `unmaximize()`）、渲染层标题栏按钮 |
| `setFullScreen` | 仅由恢复逻辑 / 渲染层 RPC 触发 |

---

## 三、实测证据

### 证据 1：状态文件与 Windows 自己的记录完全一致（说明持久化链路是「忠实」的）

```
window-state.json (wbai) : {"version":2,"bounds":{"x":148,"y":92,"width":812,"height":607},
                            "isMaximized":true,"isFullScreen":false}

Win32 GetWindowPlacement(WorkBuddy AI):
  showCmd     = 3            (maximized)
  rcNormalPos = x=148 y=92 812x606      ← 与文件一致（607/606 为 DIP 舍入）
  rcCurrent   = x=-7 y=-7 2062x1126
```

### 证据 2：环境与单位

```
物理分辨率 2560x1440 ；DPI-unaware 虚拟化后 2048x1152 ；workArea 2048x1112
主显示器缩放比 = 1.25   →  bounds 单位是 DIP，812x607 DIP = 1015x759 物理像素
```

### 证据 3：国服同样是小值，且已低于最小尺寸

```
wb  : 720x544 @ (40,36)   isMaximized:true   ← 低于客户端 minWidth/minHeight = 800x600
wbai: 812x607 @ (148,92)  isMaximized:true
```

### 证据 4：任务执行期间窗口零变化（未能复现「跑任务即还原」）

75 秒采样（间隔 200ms），期间正常执行本会话的任务：

```
   0.00s  rect=[-7,-7,2062,1126] showCmd=3 normal=[148,92,812,606]
          state_mtime=15:26:45.745  {"...812x607...isMaximized":true...}
=== 结束，共 1 条变化 ===        ← 全程只有首帧，无任何变化
```

### 证据 5：文件确实会被反复重写（写入时机与「加载本地预览页」同步）

`window-state.json` mtime = `2026-09-20 15:26:45.745`，而同一时刻的 `main.log`：

```
07:26:45.486 (UTC) file-watch-d [WorkbuddyAdapter] subscribeFileChange begin/disposed  D:/AI项目/wb_switcher/AUDIT_2026-09-20.md
07:26:45.530 main [FirstScreen] [MainProxy] resolveProxy MISS origin=https://www.workbuddy.ai
07:26:47.196 main [FirstScreen] [MainProxy] resolveProxy MISS origin=http://127.0.0.1:59048   ← 内建浏览器预览面板
07:26:47.407 main [FirstScreen] [MainProxy] resolveProxy MISS origin=http://127.0.0.1:59040
```

→ 客户端**每次 `resize`/`move` 都会重写该文件**，所以任何一次布局/窗口扰动都会刷新它。

### 证据 6：渲染层存在尺寸反馈环

`logs/Crash-Log/crash-report-main-22072-20260920T133710.json` 中反复出现（13:56–14:52 共 10 次、成对）：

```json
{"type":"renderer_js_error","errorName":"RendererJSError",
 "errorMessage":"ResizeObserver loop completed with undelivered notifications."}
```

说明渲染层有 `ResizeObserver` 触发的布局回环。它本身不直接改窗口几何，但会让 `resize` 事件更频繁，从而更频繁地触发状态落盘。

### 证据 7：客户端今日只启动过 2 次，无自动重启 / 无自动更新

```
startup/2026-09-20/14984-122126.log  12:21:26 冷启动，13:36:34 before-quit(isQuitting=true, userConfirmed=true) ← 用户主动退出
startup/2026-09-20/22072-133710.log  13:37:10 重启
logs/update/update-20260920.log      「No update available (HTTP 204)」×4
```

---

## 四、已排除的原因

| 假设 | 排除依据 |
| --- | --- |
| 本项目代码改了窗口状态 | 全项目 grep `window-state` / `isMaximized` / `last-launch` **零命中**；只 kill 自己的 `WorkBuddySwitcher.exe` |
| 本项目重启了客户端 | 同上；今日 2 次启动中第 2 次是 `userConfirmed=true` 的用户主动退出 |
| GPU 崩溃重建窗口 | `main.log` 中 `GpuCrash` / `gpu-crash` / `rebuild` **零命中** |
| 崩溃守卫进安全模式 | `.window-crash-guard` marker **不存在**（`crashCount=0`） |
| 自动更新触发重启 | 更新服务只报 `HTTP 204 No update`，无下载/安装/relaunch |
| 应用主动 `setBounds` | 全 asar 仅 1 处，且在 `display-metrics-changed` 里对「最大化/全屏/最小化」提前 return |
| 拖拽控制器改位置 | `start()` 在最大化时直接 return |
| 渲染层调 `window:maximize` IPC | `main.log` 中该通道 **零命中**（只有建窗恢复那一处调用） |
| 「跑任务」本身触发 | 75 秒任务期采样，窗口矩形 / showCmd / 还原矩形 / 文件 mtime 全部不变 |

---

## 五、为什么表现为「自动还原」

窗口当前**确实是最大化状态**（`showCmd=3`），所以不是「一直没恢复最大化」。用户看到的「还原」= 窗口从最大化态**掉回 812×607 @ (148,92)** 这个小矩形。能造成这一跳的路径：

1. **拖拽最大化窗口的标题栏**（Windows 原生 Aero Snap 行为：按下拖动即取消最大化，并把窗口按还原矩形跟随鼠标）。
2. **双击标题栏**（无边框窗口由 Electron 原生实现，等价于切换最大化）。
3. `Win+↓` / `Win+←` / `Win+→`（贴边同样先取消最大化）。
4. **应用内标题栏的最大化按钮** —— 它是 **toggle**（`WindowManager.maximize()` 里 `isMaximized() ? unmaximize() : maximize()`），点一下即取消最大化。
5. **客户端重启后的建窗窗口期** —— 先以 `bounds`（812×607）建窗并隐藏约 1.5s，再 `maximize()` + `show()`。若 `maximize()` 在隐藏窗口上未生效或延迟，用户会先看到一个 812×607 的小窗。

而它**永远停在 812×607** 的原因是第 6 点：`bounds` 只在 `resize`/`move` 时更新，而用户长期让窗口保持最大化 → 不产生新的非最大化几何 → 陈旧的小值被永久钉死。

---

## 六、解决方案

### 第 0 层：立即修复（把「想要的大小/位置」写进去）—— **已执行**

用交付的 `window_state_guard.py`：

```bash
cd D:\AI项目\wb_switcher

# 1) 看清现状
python window_state_guard.py show

# 2) 方式 A —— 手动摆好后固化（推荐）
#    先把窗口取消最大化、拖成想要的大小与位置，然后：
python window_state_guard.py capture --channel wbai

#    方式 B —— 直接指定，并**就地生效**（不用重启客户端）
python window_state_guard.py pin --channel wbai --size 1600x1000 --maximized --apply --live
python window_state_guard.py pin --channel wb   --size 1600x1000 --maximized --apply --live

# 3) 复核
python window_state_guard.py check
```

> `--apply` / `--fix` 会先把原文件备份到 `window_state_backups/`，再原子写入（tmp + `os.replace`）。

**本次已按 `1600×1000 @ (224,56)` + 保持最大化落地，两个通道都通过复核：**

```
[wb]   OK  文件 1600x1000 @ (224,56) isMaximized=True / 运行中还原矩形（窗口不可见）
[wbai] OK  文件 1600x1000 @ (224,56) isMaximized=True / 运行中还原矩形 1600x1000 @ (224,56)
rc=0
```

#### ⚠️ `--live` 为什么必要，以及**操作顺序不能反**

客户端只在建窗时读状态文件，所以光改文件对**已经在运行**的窗口无效。好在 Windows 自己
持有同一个「还原矩形」（`WINDOWPLACEMENT.rcNormalPosition`），用 `SetWindowPlacement`
改掉它即可 —— 且**保持 `showCmd` 不变**，窗口当前的最大化状态不受影响，用户看不到跳变。

实测确认它**稳定生效**（DPI-unaware 进程、跨进程调用，返回 `TRUE`）：

```
设置前    : (showCmd=3, (148, 92, 812, 606))
SetWindowPlacement 返回 : True  GetLastError = 0
设置后立即: (showCmd=3, (224, 56, 1600, 1000))
1.0s 后   : (showCmd=3, (224, 56, 1600, 1000))
3.0s 后   : (showCmd=3, (224, 56, 1600, 1000))
```

**但顺序必须「先改窗口、再写文件」**，否则会被覆盖 —— 这是实测踩到的坑：

```
16:27:22  写文件 = 1600x1000
16:27:5x  SetWindowPlacement → 窗口还原矩形 = 1600x1000
          ↑ 这一步触发了窗口的 resize/move
16:28:07  客户端 500ms 去抖落盘 → 文件被写回 812x607   ← 刚写的值被冲掉
```

原因：`SetWindowPlacement` 会触发 `resize`/`move`，客户端去抖 500ms 后把**当时**的
`getNormalBounds()` 落盘。所以正确做法是**先改窗口、等 ~1.2s 让客户端那次落盘发生、再写文件**。
`apply_all()` 已按这个顺序实现并加了等待。

改完之后：**文件 = 运行中窗口的还原矩形 = 1600×1000**，此后客户端任何一次
`resize`/`move` 落盘都只会写入同一个值，不会再把小值写回来。

### 第 1 层：守卫（防再次被改回小值）

```bash
# 校验（只报告；同时比对状态文件与运行中窗口）
python window_state_guard.py check

# 校验并回写（漂移才动，先备份；--live 同时修正运行中的窗口）
python window_state_guard.py check --fix --live
```

建议在**登录时、客户端启动前**跑一次 `check --fix`，这样每次冷启动建窗读到的都是 pinned 值。
注册计划任务（本机 `schtasks.exe` 在当前会话被安全策略拦截，请自行在管理员 PowerShell 里执行）：

```powershell
$py  = "C:\Program Files\Python313\python.exe"   # 或任意已装 Python
$arg = "D:\AI项目\wb_switcher\window_state_guard.py check --fix"
schtasks /Create /TN "wb_switcher\window-state-guard" /SC ONLOGON /RL LIMITED ^
         /TR "\"$py\" \"$arg\""
```

### 第 2 层：定位「谁在什么时候改的」

若再出现，用采样器抓现场：

```bash
python window_state_guard.py watch --channel wbai --seconds 120
```

它只在变化时输出，能看到 `rect` / `showCmd` / `normal` / `state_mtime` 的每一次跳变与先后顺序。

### 第 3 层：反馈给客户端团队（发现的真实缺陷）

**`resize` / `move` 路径把 Electron 事件对象当 `persistFullscreen` 传了进去，使文档里声称的保护失效。**

```js
// window-events.ts
mainWindow.on("resize", scheduleSaveState);   // ← 事件对象成为第 1 个实参
mainWindow.on("move",   scheduleSaveState);

// window-manager.ts
scheduleSaveState(persistFullscreen = false) {  // persistFullscreen = Event（真值！）
    ...
    saveWindowState(this.mainWindow, persistFullscreen);
}
```

`saveWindowState` 里 `if (!persistFullscreen) { ...保留磁盘上的 isFullScreen... }` 因此**被跳过**，`isFullScreen` 改为直接读实时窗口。而建窗时窗口是「最大化而非全屏」（注释明说这是为了避免动画卡顿），所以**第一次 resize 触发的落盘就会把 `isFullScreen` 写成 false**，永久丢掉用户的全屏偏好 —— 正是源码注释里声明要防的那个 bug。

建议改成显式包一层：

```js
mainWindow.on("resize", () => scheduleSaveState());
mainWindow.on("move",   () => scheduleSaveState());
```

另外建议：`loadWindowState()` 里对 `bounds` 增加「小于 `MIN_WINDOW_*` 视为异常 → 用默认值居中」的校验，避免陈旧小值被永久钉死。

---

## 七、未验证 / 存疑

1. **未能复现「跑任务即还原」**。75 秒采样期间窗口零变化。若用户的「运行任务」指的是一次具体的、可重复的操作序列，请用 `watch` 抓一次现场，可确定是「Windows 原生行为」还是「客户端建窗窗口期」。
2. **`maximize()` 作用在隐藏窗口上的可靠性**未实测（需重启客户端）。若实测发现它偶发不生效，则 §五·5 就是主因。
3. 国服 `wb` 窗口当前最小化到托盘，`GetWindowPlacement` 未能取到实时值（`list` 里不可见）。

---

## 八、附：本次新增/改动文件

| 文件 | 说明 |
| --- | --- |
| `window_state_guard.py` | **新增**。诊断 + 修复工具，纯标准库；子命令 `list` / `show` / `capture` / `pin` / `check` / `watch`，`--live` 可免重启就地生效 |
| `window_state_golden.json` | **新增**。每通道的「期望状态」，当前 = `1600x1000 @ (224,56)` + 最大化 |
| `window_state_backups/` | **新增**。每次回写前的自动备份（已含 `wb` / `wbai` 各 1~2 份） |
| `AUDIT_window_state_2026-09-20.md` | 本报告 |

> 说明：`window_state_guard.py` 是独立 CLI，**未**加入 `WorkBuddySwitcher.spec` 的 `datas`，因此不需要重打 exe。
