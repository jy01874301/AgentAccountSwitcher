@echo off
title WorkBuddy - refresh all accounts
cd /d "%~dp0"

echo ============================================================
echo   WorkBuddy Refresh All
echo   对 wb_auth\（国服）与 wbai_auth\（国际服）全部账号各跑一遍续期
echo   两个通道的网关不同，脚本分别调用（endpoint 由通道决定）
echo   默认走门卫：剩余不足 3 天且距上次满 24 小时才真正刷新
echo   加 --force 可跳过门卫，无条件全刷
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found. Please install Python.
    pause
    exit /b 1
)

set FORCEARG=
if /i "%1"=="--force" set FORCEARG=--force
if /i "%2"=="--force" set FORCEARG=--force

set RC=0

echo ---- [国服] wb ----
python wb_ui_server.py --channel wb --refresh-all %FORCEARG%
if errorlevel 1 set RC=1
echo.

echo ---- [国际服] wbai ----
python wb_ui_server.py --channel wbai --refresh-all %FORCEARG%
if errorlevel 1 set RC=1
echo.

if "%RC%"=="0" (echo All refresh passes done.) else (echo Some accounts failed, see above.)
if /i not "%1"=="/nopause" if /i not "%2"=="/nopause" pause
exit /b %RC%
