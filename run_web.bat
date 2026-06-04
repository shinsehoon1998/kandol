@echo off
REM Solting Automation - Windows Web Runner
cd /d "%~dp0"

set PORT=8000
set HOST=127.0.0.1

echo ================================================
echo   Solting/KB Automation Web Server (Windows)
echo ================================================

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python is not installed. Please install Python from https://www.python.org and try again.
  pause
  exit /b 1
)

if not exist ".venv\" (
  echo [INSTALL] Creating virtual environment .venv ...
  python -m venv .venv
  call .venv\Scripts\activate.bat
  python -m pip install --upgrade pip
  pip install -r requirements.txt -r requirements-windows.txt
  python -m playwright install chromium
) else (
  call .venv\Scripts\activate.bat
)

if not exist "config.yaml" (
  copy config.example.yaml config.yaml
  echo [INFO] config.yaml created.
)

echo [RUN] Opening browser at http://localhost:%PORT% ...
start "" http://localhost:%PORT%
set HOST=%HOST%
set PORT=%PORT%
python web\app.py

pause
