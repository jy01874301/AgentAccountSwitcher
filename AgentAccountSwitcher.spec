# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（账号切换器桌面版，多产品单 exe）。

构建：
    pyinstaller AgentAccountSwitcher.spec --noconfirm
产物是 dist\\AgentAccountSwitcher\\AgentAccountSwitcher.exe
（spec 名与产物名都已**产品无关化** —— 它现在同时能起 WorkBuddy 与 Trae，
 叫 WorkBuddySwitcher 已不贴切）。
把账号目录（wb_auth\\ / wbai_auth\\ / tw_auth\\）与 exe 放同级即可识别账号库。

入口是 **ui_app.py**（统一启动器），用 `--product` 选产品：
    AgentAccountSwitcher.exe                  # 默认 wb
    AgentAccountSwitcher.exe --product trae   # Trae

必须写进 hiddenimports 的模块（全是**动态导入**，静态分析扫不到）：
  - switcher_common：在后端模块里 sys.path.insert 之后动态导入；
  - workbuddy_checkin / trae_work_checkin：由 common.require_module() 运行时导入，
    不捆绑则打包后启动即抛 SystemExit（窗口版没有控制台，表现为双击后窗口一闪而过）。
    捆绑后 exe 自带解析/续期逻辑，不再依赖同级 ../自动签到 项目。
    若能找到 ../自动签到/config.json，运行时仍会优先读取它的 endpoint 等配置。
  - tw_ui_server：`--product trae` 时由 ui_app 用 importlib 动态导入 ——
    漏登记的话 Trae 侧起不来（ImportError，窗口版看不到报错）。
"""
import os

# SPEC 由 PyInstaller 注入；用相对定位替代原先写死的 D:/AI项目/... 绝对路径，
# 换机器/换盘符后无需再改 spec。
HERE = os.path.dirname(os.path.abspath(SPEC))
PARENT = os.path.dirname(HERE)
SIBLING = os.path.join(PARENT, "自动签到")

ICON = next((p for p in [os.path.join(SIBLING, "app.ico"),
                         os.path.join(HERE, "app.ico")] if os.path.isfile(p)), None)


a = Analysis(
    ['ui_app.py'],
    pathex=[SIBLING, HERE],
    binaries=[],
    # check_ttl.py / check_exe_datadir.py 是部署后自检用的小工具，跟着 exe 一起发，
    # 免得用户手上只有 exe 时没法自查（前者看有效期，后者看账号库基准对不对）。
    datas=[('ui_template.html', '.'), ('ui_hub.html', '.'), ('README.md', '.'),
           ('check_ttl.py', '.'), ('check_exe_datadir.py', '.'),
           # window_state_guard.py 是「客户端窗口大小/位置被自动还原」那个问题的修复产物，
           # 得让手上只有 exe 的用户也能跑它 —— 原先漏了，见 AUDIT_2026-09-24.md P3-16。
           ('window_state_guard.py', '.')],
    # account_migration 是顶层 import，静态分析扫得到；显式列出是为了防止
    # 将来被改成条件导入后悄悄漏掉（漏了会表现为"切号正常但没有迁移功能"）。
    # ⚠️ 合并启动器后，**两个**后端都是 importlib **动态导入**的（静态分析扫不到）：
    #    wb_ui_server 以前是顶层 import、能自动扫到，改成动态导入后必须显式登记 ——
    #    漏了就是「exe 一起来就 ImportError」（实测：重打后查 PYZ 才发现它没了）。
    #    tw_ui_server / trae_work_checkin 是 `--product trae` 用到的。
    hiddenimports=['webview', 'switcher_common', 'workbuddy_checkin', 'account_migration',
                   'wb_ui_server', 'tw_ui_server', 'trae_work_checkin'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AgentAccountSwitcher',
    icon=ICON,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AgentAccountSwitcher',
)
