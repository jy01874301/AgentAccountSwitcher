# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（Trae 账号切换器桌面版）。

构建：
    pyinstaller TraeSwitcher.spec --noconfirm
产物在 dist\\TraeSwitcher\\；把素材目录 tw_auth\\ 与 exe 放同级即可识别账号素材。

同样必须写进 hiddenimports 的模块（静态分析扫不到）：
  - switcher_common：在 tw_ui_server 里 sys.path.insert 之后动态导入；
  - trae_work_checkin：由 common.require_module() 运行时导入，不捆绑则打包后
    启动即抛 SystemExit（窗口版没有控制台，表现为双击后窗口一闪而过）。
    捆绑后 exe 自带解密/续期逻辑，不再依赖同级 ../自动签到 项目。
    若能找到 ../自动签到/config.json，运行时仍会优先读取它的配置。
"""
import os

# SPEC 由 PyInstaller 注入；相对定位，避免写死绝对路径。
HERE = os.path.dirname(os.path.abspath(SPEC))
PARENT = os.path.dirname(HERE)
SIBLING = os.path.join(PARENT, "自动签到")

ICON = next((p for p in [os.path.join(SIBLING, "app.ico"),
                         os.path.join(HERE, "app.ico")] if os.path.isfile(p)), None)


a = Analysis(
    ['tw_ui_app.py'],
    pathex=[SIBLING, HERE],
    binaries=[],
    datas=[('ui_template.html', '.'), ('README_Trae.md', '.')],
    hiddenimports=['webview', 'switcher_common', 'trae_work_checkin'],
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
