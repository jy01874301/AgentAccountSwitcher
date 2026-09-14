# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（WorkBuddy 账号切换器桌面版）。

构建：
    pyinstaller WorkBuddySwitcher.spec --noconfirm
产物在 dist\\WorkBuddySwitcher\\；把 wb_auth\\ 与 exe 放同级即可识别账号库。

注意：switcher_common 是在 wb_ui_server 里 sys.path.insert 之后动态导入的，
静态分析扫不到，必须列进 hiddenimports，否则打包后启动即 ModuleNotFoundError。
"""
import os

ICON = next((p for p in ['D:/AI项目/自动签到/app.ico', 'app.ico'] if os.path.isfile(p)), None)


a = Analysis(
    ['wb_ui_app.py'],
    pathex=['D:/AI项目/自动签到', 'D:/AI项目/wb_switcher'],
    binaries=[],
    datas=[('ui_template.html', '.'), ('README.md', '.'), ('check_ttl.py', '.')],
    hiddenimports=['webview', 'switcher_common'],
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
