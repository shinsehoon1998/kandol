#!/usr/bin/env python3
"""OZ 리포트 뷰어 → PDF 저장 단독 테스트. (Windows 전용)

OZ 뷰어 PDF 저장은 OS 버전/언어/OZ 버전에 따라 대화상자 명칭이 달라 튜닝이 필요합니다.
이 스크립트로 '동의서 한 건이 OZ 뷰어에 떠 있는 상태'에서 PDF 저장만 따로 시험하세요.

사용법(Windows, KB에서 동의서 1건 출력해 OZ 뷰어를 띄운 뒤):
  .venv\\Scripts\\python tools\\test_oz_pdf.py output\\consent_pdfs\\테스트.pdf
"""

import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solting_auto.config import load_config
from solting_auto.oz_viewer import save_as_pdf


def main():
    dest = sys.argv[1] if len(sys.argv) > 1 else "output/consent_pdfs/test_oz.pdf"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    log = logging.getLogger("oz-test")

    cfg = load_config("config.yaml")
    oz_cfg = cfg.get("insurance", {}).get("oz", {})
    log.info(f"OZ 설정: {oz_cfg}")
    log.info("OZ 뷰어에 보고서가 떠 있는지 확인하세요. 3초 후 시작...")
    import time; time.sleep(3)

    path = save_as_pdf(dest, oz_cfg, log)
    log.info(f"완료: {path}")


if __name__ == "__main__":
    main()
