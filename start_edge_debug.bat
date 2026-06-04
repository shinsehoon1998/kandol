@echo off
REM KB Automation Edge Debug Launcher
cd /d "%~dp0"

set PORT=9222
set PROFILE=%USERPROFILE%\kb-edge-debug

echo ================================================
echo   Launching MS Edge in Remote Debugging Mode
echo   Port: %PORT%
echo ================================================
echo Please login to KB Insurance in the opened Edge browser window.
echo Keep that Edge browser window open!

start "" msedge.exe --remote-debugging-port=%PORT% --user-data-dir="%PROFILE%" "https://nsales.kbinsure.co.kr/eus/ch/ch_index.jsp"

echo.
echo [SUCCESS] Edge launched. After logging in, click "Start" on your web dashboard.
pause
