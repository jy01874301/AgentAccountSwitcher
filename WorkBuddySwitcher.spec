# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（WorkBuddy 账号切换器桌面版）。

构建：
    pyinstaller WorkBuddySwitcher.spec --noconfirm
产物在 dist\\WorkBuddySwitcher\\；把账号目录 wb_auth\\ 与 exe 放同级即可识别账号库。

两个必须写进 hiddenimports 的模块（静态分析扫不到）：
  - switcher_common：在 wb_ui_server 里 sys.path.insert 之后动态导入；
  - workbuddy_checkin：由 common.require_module() 运行时导入，不捆绑则打包后
    启动即抛 SystemExit（窗口版没有控制台，表现为双击后窗口一闪而过）。
    捆绑后 exe 自带解析/续期逻辑，不再依赖同级 ../自动签到 项目。
    若能找到 ../自动签到/config.json，运行时仍会优先读取它的 endpoint 等配置。
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
    ['wb_ui_app.py'],
    pathex=[SIBLING, HERE],
    binaries=[],
    # check_ttl.py / check_exe_datadir.py 是部署后自检用的小工具，跟着 exe 一起发，
    # 免得用户手上只有 exe 时没法自查（前者看有效期，后者看账号库基准对不对）。
    datas=[('ui_template.html', '.'), ('ui_hub.html', '.'), ('README.md', '.'),
           ('check_ttl.py', '.'), ('check_exe_datadir.py', '.')],
    # account_migration 是顶层 import，静态分析扫得到；显式列出是为了防止
    # 将来被改成条件导入后悄悄漏掉（漏了会表现为"切号正常但没有迁移功能"）。
    hiddenimports=['webview', 'switcher_common', 'workbuddy_checkin', 'account_migration'],
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
    name='WorkBuddySwitcher',
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
    name='WorkBuddySwitcher',
)
