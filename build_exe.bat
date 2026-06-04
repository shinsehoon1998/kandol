@echo off
REM ── exe 빌드 (Windows 빌드용 PC 에서 실행) ──────────────────
REM 결과물: dist\SoltingAuto\  (배포 대상 폴더, 안의 SoltingAuto.exe 실행)
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [오류] python 이 필요합니다(빌드 PC 한정). https://www.python.org
  pause & exit /b 1
)

if not exist ".venv\" (
  python -m venv .venv
)
call .venv\Scripts\activate.bat

echo [빌드] 의존성 설치...
python -m pip install --upgrade pip
pip install -r requirements.txt -r requirements-windows.txt
pip install pyinstaller
python -m playwright install chromium

echo [빌드] PyInstaller 실행...
pyinstaller --noconfirm solting.spec

echo.
echo [완료] dist\SoltingAuto\ 폴더가 생성되었습니다.
echo   - 배포: 이 폴더 전체를 사용할 PC에 복사
echo   - 실행: 폴더 안 SoltingAuto.exe 더블클릭
echo   - (솔팅 1단계가 자체 브라우저를 띄우면) 사용 PC에서 install_browser.bat 1회 실행
pause
