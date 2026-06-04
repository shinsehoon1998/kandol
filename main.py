#!/usr/bin/env python3
"""솔팅프로그램 전산등록 자동화 - 진입점.

사용 예:
  # 단일 파일 처리 (실제 등록)
  python main.py --file input/test.xlsx

  # 브라우저 없이 검증/중복/리포트만 (셀렉터 없이도 동작)
  python main.py --file input/test.xlsx --dry-run

  # 감시 폴더의 엑셀 전체 처리
  python main.py --watch
"""

import argparse
import shutil
import sys
from pathlib import Path

from solting_auto.config import load_config
from solting_auto.logger import get_logger
from solting_auto.runner import process_file


def _move(src: Path, folder: str, logger):
    try:
        dest_dir = Path(folder)
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest_dir / src.name))
    except Exception as e:
        logger.info(f"파일 이동 실패({src.name}): {e}")


def main():
    ap = argparse.ArgumentParser(description="솔팅프로그램 전산등록 자동화")
    ap.add_argument("--config", default="config.yaml", help="설정 파일 경로")
    ap.add_argument("--file", help="처리할 단일 엑셀 파일")
    ap.add_argument("--watch", action="store_true", help="감시 폴더의 엑셀 전체 처리")
    ap.add_argument("--dry-run", action="store_true", help="브라우저 없이 검증/리포트만")
    args = ap.parse_args()

    cfg = load_config(args.config)
    logger = get_logger(cfg["run"].get("output_folder", "./output"))

    targets = []
    if args.file:
        targets = [Path(args.file)]
    elif args.watch:
        watch = Path(cfg["run"].get("watch_folder", "./input"))
        watch.mkdir(parents=True, exist_ok=True)
        targets = sorted([p for p in watch.glob("*.xlsx") if p.is_file()])
        if not targets:
            logger.info(f"처리할 엑셀이 없습니다: {watch}")
            return 0
    else:
        ap.error("--file 또는 --watch 중 하나를 지정하세요.")

    exit_code = 0
    for path in targets:
        try:
            summary = process_file(str(path), cfg, logger, dry_run=args.dry_run)
            # 감시 모드에서만 파일 이동(FR-1.4)
            if args.watch:
                if summary.fail > 0:
                    _move(path, cfg["run"].get("error_folder", "./input/오류"), logger)
                else:
                    _move(path, cfg["run"].get("done_folder", "./input/완료"), logger)
        except Exception as e:
            logger.info(f"파일 처리 오류({path.name}): {e}")
            exit_code = 1
            if args.watch:
                _move(path, cfg["run"].get("error_folder", "./input/오류"), logger)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
