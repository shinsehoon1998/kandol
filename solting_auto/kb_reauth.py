"""KB 재인증 창 감지와 사람 인증 대기 상태 머신.

이 모듈은 PyQt/Playwright/Supabase에 의존하지 않는다. 브라우저 페이지 요약과
로그인 상태를 주입받아 감지·대기·알림 이벤트만 결정한다. 본인인증 버튼 클릭,
문자 코드 입력 등 인증 행위는 의도적으로 포함하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time
from urllib.parse import urlparse
import time as _time
from typing import Any, Callable, Iterable, Optional

KB_HOST_SUFFIX = "kbinsure.co.kr"
AUTH_WINDOW_PATH = "ch_mobile2track"
ENTRANCE_PATH = "/eus/ch/entrance/"
AUTH_WINDOW_TITLE = "인증방법 선택"

STATE_CLEAR = "clear"
AUTH_REQUIRED = "auth_required"
SESSION_EXPIRED = "session_expired"
STATE_UNKNOWN = "unknown"

WAIT_RESUMED = "resumed"
WAIT_STOPPED = "stopped"
WAIT_TIMEOUT = "timeout"


class ReauthRequired(RuntimeError):
    """동의서 처리 대신 사람의 KB 재인증을 기다려야 한다는 신호."""


@dataclass(frozen=True)
class ReauthState:
    state: str
    reauth_window: bool = False
    session_expired: bool = False
    logged_in: bool = False
    pages: tuple[dict[str, str], ...] = ()

    @property
    def required(self) -> bool:
        return self.reauth_window or self.session_expired


@dataclass(frozen=True)
class _Page:
    url: str
    title: str


def _is_kb_url(url: str) -> bool:
    try:
        host = (urlparse(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host == KB_HOST_SUFFIX or host.endswith("." + KB_HOST_SUFFIX)


def _page_values(page: Any) -> _Page:
    if isinstance(page, dict):
        url = str(page.get("url") or "")
        title = str(page.get("title") or "")
        return _Page(url, title)
    url = getattr(page, "url", "")
    if callable(url):
        url = url()
    title = getattr(page, "title", "")
    if callable(title):
        title = title()
    return _Page(str(url or ""), str(title or ""))


def is_reauth_page(url: str, title: str = "") -> bool:
    """KB 호스트의 인증방법 선택 창인지 판정한다."""
    if not _is_kb_url(url):
        return False
    try:
        path = (urlparse(str(url)).path or "").lower()
    except ValueError:
        return False
    return AUTH_WINDOW_PATH in path or AUTH_WINDOW_TITLE in str(title or "")


def is_session_expired_page(url: str) -> bool:
    """KB 메인 탭이 로그인 입구로 돌아온 상태인지 판정한다."""
    if not _is_kb_url(url):
        return False
    try:
        path = (urlparse(str(url)).path or "").lower()
    except ValueError:
        return False
    return path.startswith(ENTRANCE_PATH) or ENTRANCE_PATH in path


def detect_reauth_state(pages: Iterable[Any], logged_in: Optional[bool] = None) -> ReauthState:
    """페이지 목록으로 재인증 창/세션 만료를 판정한다.

    ``logged_in``을 생략하면 URL만으로 로그인 여부를 추정하지 않는다. Playwright
    연결부는 ``kb_session.classify_tabs`` 결과를 명시적으로 주입해야 한다.
    """
    normalized = []
    has_auth = False
    has_expired = False
    for raw_page in pages or ():
        page = _page_values(raw_page)
        normalized.append({"url": page.url, "title": page.title})
        has_auth = has_auth or is_reauth_page(page.url, page.title)
        has_expired = has_expired or is_session_expired_page(page.url)

    is_logged_in = bool(logged_in)
    if has_auth:
        state = AUTH_REQUIRED
    elif has_expired:
        state = SESSION_EXPIRED
    elif is_logged_in:
        state = STATE_CLEAR
    else:
        state = STATE_UNKNOWN
    return ReauthState(
        state=state,
        reauth_window=has_auth,
        session_expired=has_expired,
        logged_in=is_logged_in,
        pages=tuple(normalized),
    )


# 이름을 기억하기 쉬운 별칭도 제공한다.
classify_pages = detect_reauth_state
detect_reauth = detect_reauth_state


def _read_clock(clock: Any) -> Any:
    if callable(clock):
        return clock()
    for attr in ("monotonic", "time", "now"):
        candidate = getattr(clock, attr, None)
        if callable(candidate):
            return candidate()
    raise TypeError("clock는 callable 또는 monotonic/time/now 메서드를 제공해야 합니다.")


def _as_seconds(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, date):
        return datetime.combine(value, datetime_time()).timestamp()
    return float(value)


def _wall_now(clock: Any, now: Optional[Callable[[], Any]]) -> datetime:
    value = now() if now is not None else _read_clock(clock)
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime_time())
    return datetime.now()


def _parse_hhmm(value: Any) -> datetime_time:
    if isinstance(value, datetime_time):
        return value
    if isinstance(value, (int, float)):
        hour = int(value) % 24
        return datetime_time(hour, 0)
    text = str(value or "").strip()
    parts = text.split(":", 1)
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) == 2 else 0
    except (ValueError, TypeError):
        raise ValueError(f"잘못된 조용한 시간: {value!r}")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"잘못된 조용한 시간: {value!r}")
    return datetime_time(hour, minute)


def is_quiet_time(value: datetime, quiet_start: Any = "23:00", quiet_end: Any = "07:00") -> bool:
    start = _parse_hhmm(quiet_start)
    end = _parse_hhmm(quiet_end)
    current = value.time().replace(second=0, microsecond=0)
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _is_stop_requested(stop_requested: Any) -> bool:
    return bool(stop_requested() if callable(stop_requested) else stop_requested)


def _resume_ready(value: Any) -> bool:
    if isinstance(value, ReauthState):
        return value.logged_in and not value.required
    if isinstance(value, dict):
        if "ready" in value:
            return bool(value["ready"])
        return bool(value.get("logged_in")) and not bool(
            value.get("reauth_window") or value.get("session_expired")
        )
    if isinstance(value, (tuple, list)) and value:
        return str(value[0]).lower() in {"logged_in", STATE_CLEAR, WAIT_RESUMED}
    return bool(value)


def _emit_notify(notify: Optional[Callable[..., Any]], kind: str, waited_min: int, escalate_sms: bool = False) -> None:
    if notify is None:
        return
    try:
        notify(kind, escalate_sms=escalate_sms, waited_min=waited_min)
        return
    except TypeError:
        pass
    try:
        notify(kind, escalate_sms=escalate_sms)
        return
    except TypeError:
        pass
    notify(kind)


def wait_for_reauth(
    probe: Callable[[], Any],
    notify: Optional[Callable[..., Any]],
    stop_requested: Any,
    *,
    clock: Any = _time.monotonic,
    sleep: Callable[[float], Any] = _time.sleep,
    now: Optional[Callable[[], Any]] = None,
    poll_interval_sec: float = 5.0,
    resume_interval_sec: Optional[float] = None,
    escalation_after_sec: float = 10 * 60,
    reminder_interval_sec: float = 30 * 60,
    max_wait_sec: float = 24 * 60 * 60,
    quiet_start: Any = "23:00",
    quiet_end: Any = "07:00",
    resume_confirmations: int = 2,
    resume_requested: Any = None,
) -> str:
    """재인증 완료를 기다리고 ``resumed/stopped/timeout`` 중 하나를 반환한다."""
    if poll_interval_sec < 0 or (resume_interval_sec is not None and resume_interval_sec < 0):
        raise ValueError("폴링 간격은 음수일 수 없습니다.")
    if resume_interval_sec is None:
        resume_interval_sec = poll_interval_sec
    confirmations_needed = max(1, int(resume_confirmations))

    started_at = _as_seconds(_read_clock(clock))
    _emit_notify(notify, "auth_required", 0)
    confirmation_count = 0
    escalation_due_sent = False
    next_reminder_at = float(escalation_after_sec) + float(reminder_interval_sec)
    quiet_suppressed = False
    morning_reminder_sent: Optional[date] = None

    while True:
        if _is_stop_requested(stop_requested):
            return WAIT_STOPPED

        elapsed = max(0.0, _as_seconds(_read_clock(clock)) - started_at)
        if elapsed >= max_wait_sec:
            _emit_notify(notify, WAIT_TIMEOUT, int(elapsed // 60))
            return WAIT_TIMEOUT

        ready = _resume_ready(probe())
        if ready:
            confirmation_count += 1
            if confirmation_count >= confirmations_needed:
                _emit_notify(notify, WAIT_RESUMED, int(elapsed // 60))
                return WAIT_RESUMED
        else:
            confirmation_count = 0

        wall = _wall_now(clock, now)
        quiet = is_quiet_time(wall, quiet_start, quiet_end)
        waited_min = int(elapsed // 60)

        if elapsed >= escalation_after_sec and not escalation_due_sent:
            escalation_due_sent = True
            if quiet:
                quiet_suppressed = True
            else:
                _emit_notify(notify, "reminder", waited_min, escalate_sms=True)

        if elapsed >= next_reminder_at:
            if quiet:
                quiet_suppressed = True
            elif not quiet_suppressed:
                _emit_notify(notify, "reminder", waited_min)
                next_reminder_at = elapsed + float(reminder_interval_sec)

        # 조용한 시간 동안 누적된 알림은 조용한 시간이 끝난 첫 폴링에서 한 번만 보낸다.
        if not quiet and quiet_suppressed and morning_reminder_sent != wall.date():
            _emit_notify(notify, "reminder", waited_min)
            morning_reminder_sent = wall.date()
            quiet_suppressed = False
            next_reminder_at = elapsed + float(reminder_interval_sec)

        # 수동 버튼은 인증을 우회하지 않고 다음 상태를 즉시 다시 확인하도록만 한다.
        manual_resume = _is_stop_requested(resume_requested) if resume_requested is not None else False
        delay = float(resume_interval_sec if confirmation_count else poll_interval_sec)
        if manual_resume:
            delay = min(delay, 0.1)
        remaining = float(max_wait_sec) - elapsed
        sleep(max(0.0, min(delay, remaining)))


__all__ = [
    "AUTH_REQUIRED",
    "AUTH_WINDOW_PATH",
    "AUTH_WINDOW_TITLE",
    "ENTRANCE_PATH",
    "KB_HOST_SUFFIX",
    "ReauthRequired",
    "ReauthState",
    "SESSION_EXPIRED",
    "STATE_CLEAR",
    "STATE_UNKNOWN",
    "WAIT_RESUMED",
    "WAIT_STOPPED",
    "WAIT_TIMEOUT",
    "classify_pages",
    "detect_reauth",
    "detect_reauth_state",
    "is_quiet_time",
    "is_reauth_page",
    "is_session_expired_page",
    "wait_for_reauth",
]
