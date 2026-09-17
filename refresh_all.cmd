@echo off
title WorkBuddy - refresh all accounts
cd /d "%~dp0"

echo ============================================================
echo   WorkBuddy Refresh All
echo   对 wb_auth\ 下全部账号跑一遍续期
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

python wb_ui_server.py --refresh-all %FORCEARG%
set RC=%ERRORLEVEL%
echo.
if "%RC%"=="0" (echo Refresh pass done.) else (echo Some accounts failed, see above.)
if /i not "%1"=="/nopause" if /i not "%2"=="/nopause" pause
exit /b %RC%
