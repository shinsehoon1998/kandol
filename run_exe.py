#!/usr/bin/env python3
"""exe 빌드용 진입점.

PyInstaller 로 묶으면 더블클릭 시:
  1) exe 옆 폴더를 작업 폴더로 설정(config.yaml/output/uploads 가 여기 생성)
  2) config.yaml 이 없으면 번들된 config.example.yaml 로 생성
  3) Playwright 브라우저 경로를 exe 옆 ms-playwright 로 고정
  4) 웹 서버 기동 + 기본 브라우저 자동 열기
"""

import os
import sys
import shutil
import threading
import time
import webbrowser
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
RES_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent


def _prepare():
    # 모든 상대경로(output/, uploads/, config.yaml, .env)를 exe 옆 폴더 기준으로
    os.chdir(APP_DIR)

    # config.yaml 없으면 번들 예시에서 생성
    cfg = APP_DIR / "config.yaml"
    if not cfg.exists():
        example = RES_DIR / "config.example.yaml"
        if example.exists():
            shutil.copy(example, cfg)
            print(f"[안내] config.yaml 생성됨 → {cfg}")

    # Playwright 브라우저를 exe 옆 폴더에 보관(설치/탐색 위치 고정)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(APP_DIR / "ms-playwright"))

    (APP_DIR / "output").mkdir(exist_ok=True)
    (APP_DIR / "input").mkdir(exist_ok=True)


def _ensure_browser():
    """번들된 Playwright 드라이버로 Chromium 설치(1단계 솔팅이 자체 브라우저를
    띄우는 경우 1회 필요). install_browser.bat 에서 호출."""
    import subprocess
    try:
        from playwright._impl._driver import compute_driver_executable
        drv = compute_driver_executable()
        cmd = list(drv) if isinstance(drv, (list, tuple)) else [drv]
        print("[설치] Chromium 다운로드 중...")
        subprocess.run(cmd + ["install", "chromium"], check=False)
        print("[설치] 완료. 위치:", os.environ.get("PLAYWRIGHT_BROWSERS_PATH"))
    except Exception as e:
        print("[설치] 브라우저 자동설치 실패:", e)
        print("       KB 2단계는 attach 모드라 브라우저 없이도 동작합니다.")


def main():
    _prepare()

    # install_browser.bat → "SoltingAuto.exe --install-browser"
    if "--install-browser" in sys.argv:
        _ensure_browser()
        return

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))

    # web/app.py 의 Flask 앱 재사용
    sys.path.insert(0, str(RES_DIR))
    from web.app import app

    url = f"http://localhost:{port}"
    print(f"\n  솔팅/KB 전산등록 자동화 → {url}\n  (종료: 이 창을 닫기)\n")

    def _open():
        time.sleep(1.5)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
