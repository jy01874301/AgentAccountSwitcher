@echo off
title Trae - refresh all accounts
cd /d "%~dp0"

echo ============================================================
echo   Trae Refresh All
echo   对 tw_auth\ 下全部素材各续期一次
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found. Please install Python.
    pause
    exit /b 1
)

python tw_ui_server.py --refresh-all
set RC=%ERRORLEVEL%
echo.
if "%RC%"=="0" (echo All accounts refreshed.) else (echo Some accounts failed, see above.)
if /i not "%1"=="/nopause" pause
exit /b %RC%
