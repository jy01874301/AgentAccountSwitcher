@echo off
title WorkBuddy Account Switcher
cd /d "%~dp0"

echo ============================================================
echo   WorkBuddy Account Switcher
echo   Starting local service...
echo   The browser will open automatically.
echo   Press Ctrl+C to stop the service and close this window.
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found. Please install Python.
    pause
    exit /b 1
)

REM Run the service in the foreground.
REM Capturing Ctrl+C ends both the service and this window cleanly.
python wb_ui_server.py --serve
echo.
echo Service stopped.