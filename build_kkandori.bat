@echo off
REM ── 깐돌이 에이전트 exe 빌드 스크립트 ──────────────────
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [오류] python 이 필요합니다.
  pause & exit /b 1
)

if not exist ".venv\" (
  python -m venv .venv
)
call .venv\Scripts\activate.bat

echo [빌드] PyInstaller 설치 확인 및 에이전트 빌드 준비...
pip install pyinstaller

echo [빌드] PyInstaller 실행...
pyinstaller --noconfirm kkandori.spec

echo.
echo [완료] dist\Kkandori\ 폴더 및 실행파일이 생성되었습니다.
echo   - 배포: dist\Kkandori\ 폴더 전체를 복사하여 사용
echo   - 실행: 폴더 안 Kkandori.exe 실행
pause
