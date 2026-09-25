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

REM Checking "python exists" is NOT enough: the PATH may also hold the
REM Microsoft Store stub python.exe (running it opens the Store, which looks
REM like "double-click did nothing"). So actually run it and check the version.
REM (The ">" inside the quoted -c argument is not treated as redirection.)
python -c "import sys;assert sys.version_info[0]==3 and sys.version_info[1]>7" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python 3.8+ not found, or the name resolves to the
    echo         Microsoft Store stub. Check with:  python --version
    echo         If that opens the Store, turn off the App Execution Alias
    echo         for python.exe, or install Python from python.org.
    pause
    exit /b 1
)

REM Single instance is enforced inside the service (named mutex, see
REM DESIGN_single_instance.md). If an instance is already running this will
REM point you at it and exit, instead of silently starting a second one on a
REM shifted port. The port is only shifted when some OTHER program holds it,
REM and the actual address is always printed below.
python wb_ui_server.py --serve
set RC=%ERRORLEVEL%
if "%RC%"=="1" (
    echo.
    echo [ERROR] Could not start the service ^(see the message above^).
    pause
)
echo.
echo Service stopped.
REM Must exit with an explicit code: without it the script's exit status comes
REM from the LAST command (echo), which is always 0 -- scheduled tasks and any
REM caller checking %ERRORLEVEL% would treat a failed start as success.
exit /b %RC%
