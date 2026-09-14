@echo off
title WorkBuddy - refresh all accounts
cd /d "%~dp0"

echo ============================================================
echo   WorkBuddy Refresh All
echo   对 wb_auth\ 下全部账号各续期一次
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found. Please install Python.
    pause
    exit /b 1
)

python wb_ui_server.py --refresh-all
set RC=%ERRORLEVEL%
echo.
if "%RC%"=="0" (echo All accounts refreshed.) else (echo Some accounts failed, see above.)
echo.
pause
exit /b %RC%
