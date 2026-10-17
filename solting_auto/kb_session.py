"""KB전산 로그인 상태 감지 (읽기 전용).

디버그 포트(9222)로 열린 Edge 의 탭 목록(`/json/list`)만 읽어서 KB전산에 로그인돼 있는지
판단한다. 화면 조작·페이지 이동·입력은 하지 않는다.

배경: 3단계(동의서 출력)는 원래 1단계(Edge 기동)·2단계(자동 로그인) 버튼을 순서대로 눌러야만
열렸다. 사용자가 디버그 Edge 에서 KB전산에 직접(수동) 로그인해 둔 경우에도 3단계를 쓸 수 있도록
이 모듈로 로그인 상태를 감지한다. 실제 처리 엔진(insurance.py)은 attach 모드 + skip_login 일 때
이미 '수동 로그인 상태'를 전제로 동작하므로, 막혀 있던 것은 화면의 단계 잠금뿐이다.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlparse

KB_HOST_SUFFIX = "kbinsure.co.kr"
# insurance.py 가 '메인 전산 페이지'를 찾을 때 쓰는 URL 키워드와 같은 기준
KB_MAIN_URL_KEYWORDS = ("main", "wsserver", "wq")

STATE_NO_BROWSER = "no_browser"      # 디버그 포트에 응답하는 브라우저 없음
STATE_NO_KB_TAB = "no_kb_tab"        # 브라우저는 있는데 KB전산 탭이 없음
STATE_LOGIN_PAGE = "login_page"      # KB전산 탭은 있으나 로그인 화면 상태
STATE_LOGGED_IN = "logged_in"        # KB전산 메인(로그인 후) 탭 있음

STATE_MESSAGES = {
    STATE_NO_BROWSER: (
        "디버그 모드 Edge(포트 9222)를 찾지 못했어요. "
        "1단계 [Edge 브라우저 기동]으로 연 Edge에서 KB전산에 로그인해 주세요. "
        "(일반 Edge는 연결할 수 없어요)"
    ),
    STATE_NO_KB_TAB: "디버그 Edge는 켜져 있지만 KB전산 탭이 없어요. KB전산에 접속해 로그인해 주세요.",
    STATE_LOGIN_PAGE: "KB전산이 아직 로그인 전 화면이에요. 로그인해서 KB전산 홈까지 들어가 주세요.",
    STATE_LOGGED_IN: "KB전산 로그인 상태를 확인했어요.",
}


def _is_kb_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host == KB_HOST_SUFFIX or host.endswith("." + KB_HOST_SUFFIX)


def classify_tabs(tabs: list[dict]) -> tuple[str, str]:
    """CDP `/json/list` 항목들로 (상태, 대표 URL)을 판정한다."""
    kb_urls = []
    for t in tabs or []:
        if t.get("type") != "page":
            continue
        url = str(t.get("url") or "")
        if _is_kb_url(url):
            kb_urls.append(url)
    for url in kb_urls:
        low = url.lower()
        if any(k in low for k in KB_MAIN_URL_KEYWORDS):
            return STATE_LOGGED_IN, url
    if kb_urls:
        return STATE_LOGIN_PAGE, kb_urls[0]
    return STATE_NO_KB_TAB, ""


def probe_kb_session(cdp_url: str = "http://localhost:9222", timeout: float = 2.0) -> tuple[str, str]:
    """디버그 Edge 의 탭 목록을 읽어 KB전산 로그인 상태를 돌려준다. 예외를 밖으로 던지지 않는다."""
    try:
        with urllib.request.urlopen(cdp_url.rstrip("/") + "/json/list", timeout=timeout) as resp:
            tabs = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return STATE_NO_BROWSER, ""
    if not isinstance(tabs, list):
        return STATE_NO_BROWSER, ""
    return classify_tabs(tabs)
