# -*- coding: utf-8 -*-
"""KB 보장분석 화면의 XHR(데이터) 응답 구조 디스커버리 (CDP attach).

사용법:
  1) KB 전산에 로그인된 디버그 Edge(:9222) 가 떠 있는 상태에서
  2) 이 스크립트를 실행하면 N초간 네트워크 응답을 감시
  3) 그 사이 KB에서:  보장분석 메뉴 → '조회' → 고객 더블클릭 → '가입현황' 탭
  을 차례로 클릭하면, 목록/상세/가입현황 JSON 응답이 scratch/bojang_capture.log 에 덤프됨.

데이터(고객/보장) 응답만 추리기 위해 wsserver 하위 + JSON/XML/텍스트 응답 위주로 기록.
정적 자원(js/css/img/font)은 제외.
"""
import sys, time, logging

DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 120

log = logging.getLogger("bojang")
log.setLevel(logging.INFO)
fh = logging.FileHandler("scratch/bojang_capture.log", encoding="utf-8", mode="w")
fh.setFormatter(logging.Formatter("%(asctime)s %(message)s")); log.addHandler(fh)
sh = logging.StreamHandler(); sh.setFormatter(logging.Formatter("%(message)s")); log.addHandler(sh)

from playwright.sync_api import sync_playwright

ASSET_EXT = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".woff", ".woff2",
             ".ttf", ".ico", ".map")
DATA_HINTS = ["보장", "고객", "계약", "담보", "월보험료", "custNm", "custList", "result",
              "Cust", "Cntr", "Cvr", "list", "grid", "data"]


def on_resp(resp):
    try:
        url = resp.url or ""
        low = url.split("?")[0].lower()
        if low.endswith(ASSET_EXT):
            return
        ct = (resp.headers or {}).get("content-type", "").lower()
        is_data = ("json" in ct or "xml" in ct or "text/plain" in ct
                   or "wsserver" in url.lower())
        if not is_data:
            return
        try:
            body = resp.text()
        except Exception:
            body = ""
        # 데이터로 보이는 응답만(키워드 또는 JSON/XML 형태)
        looks = any(h in body for h in DATA_HINTS) or body[:1].strip() in ("{", "[", "<")
        if not looks or not body.strip():
            return
        log.info("=" * 80)
        log.info(f"[RESP] {resp.request.method} {resp.status} ct={ct}")
        log.info(f"  URL: {url[:160]}")
        # 요청 페이로드도(상세 재요청 재현용)
        try:
            pd = resp.request.post_data
            if pd:
                log.info(f"  REQ-BODY(앞 600): {pd[:600]}")
        except Exception:
            pass
        log.info(f"  RES-BODY(앞 1800): {body[:1800]}")
    except Exception:
        pass


def attach(pg):
    try:
        pg.on("response", on_resp)
    except Exception:
        pass


def main():
    with sync_playwright() as p:
        br = p.chromium.connect_over_cdp("http://localhost:9222")
        for ctx in br.contexts:
            for pg in ctx.pages:
                attach(pg)
            ctx.on("page", lambda pg: attach(pg))
        log.info(f"=== 보장분석 네트워크 감시 시작 ({DURATION}s). "
                 f"지금 KB에서 보장분석→조회→고객 더블클릭→가입현황 을 눌러주세요. ===")
        for _ in range(DURATION):
            time.sleep(1)
        log.info("=== 감시 종료 ===")


if __name__ == "__main__":
    main()
