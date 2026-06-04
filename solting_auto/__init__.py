"""솔팅프로그램 전산등록 자동화 매크로 (PoC).

PRD: PRD_전산등록_자동화매크로.md
"""

__version__ = "0.1.0"

_stop_check_cb = None

def register_stop_check(cb):
    global _stop_check_cb
    _stop_check_cb = cb

def is_stop_requested():
    if _stop_check_cb and _stop_check_cb():
        return True
    return False

def check_stop():
    if is_stop_requested():
        raise RuntimeError("사용자 중단 요청")

