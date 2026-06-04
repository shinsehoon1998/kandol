"""마스킹 적용 로거. (PRD 7.1 - 로그에 민감정보 노출 금지)

주민번호/전화번호 패턴을 로그 메시지에서 자동 마스킹한다.
"""

import logging
import re
from pathlib import Path

_JUMIN_RE = re.compile(r"(\d{6})[-]?(\d{7})")
_PHONE_RE = re.compile(r"(01[016789])[-]?(\d{3,4})[-]?(\d{4})")


class MaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        msg = _JUMIN_RE.sub(lambda m: f"{m.group(1)}-{m.group(2)[0]}******", msg)
        msg = _PHONE_RE.sub(lambda m: f"{m.group(1)}-****-{m.group(3)}", msg)
        record.msg = msg
        record.args = ()
        return True


def get_logger(output_folder: str) -> logging.Logger:
    logger = logging.getLogger("solting_auto")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)

    Path(output_folder).mkdir(parents=True, exist_ok=True)
    log_path = Path(output_folder) / "run.log"

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    mask = MaskingFilter()

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(mask)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.addFilter(mask)
    logger.addHandler(ch)

    return logger
