#!/bin/bash
# 솔팅 전산등록 웹 - 맥 실행 런처 (더블클릭으로 실행)
# 최초 실행 시 자동으로 환경을 구성하고, 이후에는 바로 서버를 띄웁니다.

cd "$(dirname "$0")" || exit 1

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"

echo "================================================"
echo "  솔팅 전산등록 자동화 - 웹 서버"
echo "================================================"

# 1) 파이썬 확인
if ! command -v python3 >/dev/null 2>&1; then
  echo "[오류] python3 가 설치되어 있지 않습니다."
  echo "  → https://www.python.org 에서 Python 3 설치 후 다시 실행하세요."
  read -r -p "엔터를 누르면 종료합니다..."
  exit 1
fi

# 2) 가상환경 + 의존성 (최초 1회)
if [ ! -d ".venv" ]; then
  echo "[설치] 최초 실행 - 환경 구성 중 (수 분 소요)..."
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip >/dev/null
  ./.venv/bin/pip install -r requirements.txt
  echo "[설치] Playwright 브라우저(Chromium) 내려받는 중..."
  ./.venv/bin/playwright install chromium
  echo "[설치] 완료."
fi

# 3) 설정 파일 확인
if [ ! -f "config.yaml" ]; then
  cp config.example.yaml config.yaml
  echo "[안내] config.yaml 을 생성했습니다. 솔팅프로그램 URL/셀렉터를 채워주세요."
fi

# 4) 브라우저 자동 열기 (2초 후)
( sleep 2; open "http://localhost:${PORT}" ) &

# 5) 서버 실행
echo "[실행] 브라우저에서 http://localhost:${PORT} 접속"
echo "       (종료하려면 이 창에서 Ctrl + C)"
echo "------------------------------------------------"
HOST="$HOST" PORT="$PORT" ./.venv/bin/python web/app.py

read -r -p "서버가 종료되었습니다. 엔터를 누르면 창을 닫습니다..."
