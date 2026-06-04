@echo off
REM ── (선택) 솔팅 1단계가 자체 브라우저를 띄우는 경우 1회 실행 ──
REM KB 2단계는 attach 모드라 이 단계가 필요 없습니다.
cd /d "%~dp0"
SoltingAuto.exe --install-browser
pause
