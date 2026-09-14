# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（Trae 账号切换器桌面版）。

构建：
    pyinstaller TraeSwitcher.spec --noconfirm
产物在 dist\\TraeSwitcher\\；把 tw_auth\\ 与 exe 放同级即可识别账号素材。

同样要注意 switcher_common 是动态导入，必须列进 hiddenimports。
"""
import os

ICON = next((p for p in ['D:/AI项目/自动签到/app.ico', 'app.ico'] if os.path.isfile(p)), None)


a = Analysis(
    ['tw_ui_app.py'],
    pathex=['D:/AI项目/自动签到', 'D:/AI项目/wb_switcher'],
    binaries=[],
    datas=[('tw_ui_index.html', '.'), ('README_Trae.md', '.')],
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
    name='TraeSwitcher',
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
    name='TraeSwitcher',
)
