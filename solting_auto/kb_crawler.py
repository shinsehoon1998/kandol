# -*- coding: utf-8 -*-
"""KB 보장분석 고객 데이터 수집기 (CDP attach 기반).

설계사 본인이 KB전산에 로그인한 세션(디버그 Edge :9222)에 CDP로 접속하여,
'보장분석' 화면에 표시되는 본인 담당 고객 데이터를 수집한다. 새로운 보안 우회가
아니라, 기존 동의서 자동화와 동일하게 '이미 로그인된 본인 세션의 화면 데이터'를
읽는 방식이다.

핵심 전략 — 네트워크 응답 가로채기(우선) + UI 자동구동(보조):
  WebSquare 는 화면을 XHR(JSON/XML)로 채운다. DOM 을 긁는 대신 page.on("response")
  로 데이터 응답을 가로채 '고객 배열'을 직접 확보한다(프레임워크 구조 변화에 강함).
  - 수동/자동 무관: 에이전트가 '조회'를 자동 클릭하거나, 사용자가 직접 조회를 눌러도
    동일하게 응답을 수확(harvest)한다.
  - 수집과 동시에 원본 응답을 dump_path 에 저장 → 셀렉터/필드 매핑 보정 자료로 사용.

반환: 정규화된 dict 리스트. 각 dict 키는 customer_records 스키마와 동일
  (customer_name, birth, age, gender, analysis_date, policy_count,
   monthly_premium, consent_end_date, contract_status, coverage_summary,
   coverage_detail, raw).
"""
import json
import re
import time


# ── 보장분석 목록/상세 트리거용 후보 셀렉터 (보정 가능) ──────────────
# WebSquare 화면 구조가 확정되면 이 목록을 정밀화하면 된다. 여러 후보를 순차 시도.
INQUIRY_BUTTON_SELECTORS = [
    "input[value='조회']",
    "button:has-text('조회')",
    "a:has-text('조회')",
    "input[id$='btn_search']",
    "input[id$='btnSearch']",
    "[id$='btn_inquiry']",
]

BOJANG_MENU_KEYWORDS = ["보장분석", "스마트보장", "보장 분석"]

# 정적 자원 제외
_ASSET_EXT = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg",
              ".woff", ".woff2", ".ttf", ".ico", ".map")

# 목록 행(dict)을 고객으로 인정하기 위한 키 이름 힌트(한/영)
_NAME_HINTS = ["custnm", "고객명", "고객", "name", "nm", "성명"]
_BIRTH_HINTS = ["birth", "생년", "birthdt", "rrn", "ssn", "주민"]
_AGE_HINTS = ["age", "나이", "연령"]
_GENDER_HINTS = ["gender", "성별", "sex"]
# 분석일자: 'dt' 같은 과도하게 일반적인 토큰은 제외(birthDt 오매칭 방지)
_DATE_HINTS = ["analy", "분석", "일자", "기준일", "basedt", "stddt"]
_COUNT_HINTS = ["cnt", "건수", "count", "가입건", "qty"]
_PREMIUM_HINTS = ["prem", "보험료", "월납", "amt", "amount", "fee"]
_CONSENT_HINTS = ["consent", "동의", "expire", "만료", "종료", "end"]


def _looks_like_name(v):
    return isinstance(v, str) and bool(re.search(r"[가-힣]{2,}", v)) and len(v) <= 20


def _looks_like_birth(v):
    if not isinstance(v, (str, int)):
        return False
    s = re.sub(r"[^0-9]", "", str(v))
    return 6 <= len(s) <= 8


def _key_matches(key, hints):
    k = str(key).lower()
    return any(h in k for h in hints)


def _pick(d, hints, validator=None, claimed=None):
    """dict d 에서 hints 에 맞는 키의 (key, value)를 반환. claimed(이미 사용된 키)는 제외.
    검증기 통과 후보 우선. 매칭 실패 시 (None, None)."""
    claimed = claimed if claimed is not None else set()
    candidates = [(k, v) for k, v in d.items()
                  if k not in claimed and _key_matches(k, hints)]
    if validator:
        for k, v in candidates:
            if validator(v):
                return k, v
    if candidates:
        return candidates[0]
    return None, None


def _iter_arrays(obj, depth=0):
    """JSON 객체를 재귀 순회하며 (dict 들의 배열)을 모두 산출."""
    if depth > 8:
        return
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            yield obj
        for x in obj:
            yield from _iter_arrays(x, depth + 1)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_arrays(v, depth + 1)


def _score_customer_array(arr):
    """배열이 '고객 목록'일 가능성 점수. 이름/생년월일 컬럼 존재 여부 기반."""
    if not arr:
        return 0
    sample = arr[0]
    keys = list(sample.keys())
    if len(keys) < 2:
        return 0
    score = 0
    # 키 이름에 이름/생년 힌트
    if any(_key_matches(k, _NAME_HINTS) for k in keys):
        score += 3
    if any(_key_matches(k, _BIRTH_HINTS) for k in keys):
        score += 2
    # 값에 한글 이름/생년월일 패턴이 실제로 있는지
    name_vals = [v for v in sample.values() if _looks_like_name(v)]
    birth_vals = [v for v in sample.values() if _looks_like_birth(v)]
    if name_vals:
        score += 2
    if birth_vals:
        score += 1
    # 행 수가 많을수록 목록일 확률↑ (과도하지 않게)
    score += min(len(arr), 5) * 0.2
    return score


def _normalize_row(row):
    """KB 목록 행(dict) → customer_records 정규화 dict.
    각 KB 컬럼이 한 필드에만 매핑되도록 사용한 키를 claim 하여 교차오염을 막는다.
    매칭 실패 항목은 raw 로 보존."""
    claimed = set()

    def claim(hints, validator=None):
        k, v = _pick(row, hints, validator, claimed)
        if k is not None:
            claimed.add(k)
        return v

    # 이름/생년월일(가장 특정적) → 성별/나이 → 동의종료/분석일자 → 건수/보험료 순으로 claim
    name = claim(_NAME_HINTS, _looks_like_name)
    if not name:
        for k, v in row.items():
            if k not in claimed and _looks_like_name(v):
                name, _ = v, claimed.add(k)
                break

    birth = claim(_BIRTH_HINTS, _looks_like_birth)
    if not birth:
        for k, v in row.items():
            if k not in claimed and _looks_like_birth(v):
                birth, _ = v, claimed.add(k)
                break
    if birth:
        birth = re.sub(r"[^0-9]", "", str(birth))
        # 주민등록번호 전체(13자리 등)가 잡히면 생년월일(앞 6자리)만 보관 — 민감정보 최소화
        if len(birth) > 8:
            birth = birth[:6]

    gender = _normalize_gender(claim(_GENDER_HINTS))
    age = _to_text(claim(_AGE_HINTS))
    consent = _to_text(claim(_CONSENT_HINTS))
    analysis = _to_text(claim(_DATE_HINTS))
    count = _to_text(claim(_COUNT_HINTS))
    premium = _to_text(claim(_PREMIUM_HINTS))

    return {
        "customer_name": (str(name).strip() if name else ""),
        "birth": (birth or ""),
        "age": age,
        "gender": gender,
        "analysis_date": analysis,
        "policy_count": count,
        "monthly_premium": premium,
        "consent_end_date": consent,
        "contract_status": None,
        "coverage_summary": None,
        "coverage_detail": None,
        "raw": row,
    }


def _to_text(v):
    if v is None:
        return None
    return str(v).strip()


def _normalize_gender(v):
    if v is None:
        return None
    s = str(v).strip()
    mapping = {"1": "남", "2": "여", "M": "남", "F": "여", "남자": "남", "여자": "여"}
    return mapping.get(s, s)


class _Capture:
    """CDP 페이지의 데이터 응답을 모으는 버퍼."""

    def __init__(self, logger=None, dump_path=None):
        self.responses = []   # [{url, status, ct, body}]
        self.logger = logger
        self.dump_path = dump_path

    def on_response(self, resp):
        try:
            url = resp.url or ""
            low = url.split("?")[0].lower()
            if low.endswith(_ASSET_EXT):
                return
            ct = (resp.headers or {}).get("content-type", "").lower()
            if not ("json" in ct or "xml" in ct or "text/plain" in ct
                    or "wsserver" in url.lower()):
                return
            try:
                body = resp.text()
            except Exception:
                return
            if not body or body[:1].strip() not in ("{", "["):
                # JSON 형태만 보관(목록/상세는 JSON 가정; 아니면 스킵)
                if "<" not in body[:1]:
                    return
            self.responses.append({"url": url, "status": resp.status,
                                   "ct": ct, "body": body})
        except Exception:
            pass

    def parsed_json(self):
        out = []
        for r in self.responses:
            b = r["body"].strip()
            if b[:1] in ("{", "["):
                try:
                    out.append((r, json.loads(b)))
                except Exception:
                    continue
        return out

    def dump(self):
        if not self.dump_path:
            return
        try:
            with open(self.dump_path, "w", encoding="utf-8") as f:
                for r in self.responses:
                    f.write("=" * 80 + "\n")
                    f.write(f"{r['status']} {r['ct']}\n{r['url']}\n")
                    # 디버그 덤프에는 주민등록번호(13자리)를 마스킹해 저장
                    body = re.sub(r"(\d{6})[-]?\d{7}", r"\1-*******", r["body"][:4000])
                    f.write(body + "\n")
        except Exception:
            pass


def _find_kb_page(browser, logger=None):
    """CDP 컨텍스트들에서 KB(nsales) 페이지를 찾는다. 보장분석 우선."""
    best = None
    for ctx in browser.contexts:
        for pg in ctx.pages:
            try:
                url = pg.url or ""
            except Exception:
                continue
            if "kbinsure" in url.lower() or "nsales" in url.lower():
                best = best or pg
                # 보장분석 화면이면 즉시 우선
                try:
                    for kw in BOJANG_MENU_KEYWORDS:
                        if pg.locator(f"text={kw}").count() > 0:
                            return pg
                except Exception:
                    pass
    return best


def _try_click_inquiry(page, logger=None):
    """'조회' 버튼을 후보 셀렉터로 시도 클릭. 성공 시 True."""
    for sel in INQUIRY_BUTTON_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.click(timeout=2500)
                if logger:
                    logger.info(f"[수집] '조회' 자동 클릭 성공: {sel}")
                return True
        except Exception:
            continue
    return False


def crawl_customers(cdp_url="http://localhost:9222", logger=None,
                    progress_cb=None, stop_cb=None, dump_path=None,
                    wait_secs=40):
    """KB 보장분석 고객 데이터를 수집해 정규화 dict 리스트로 반환.

    progress_cb(done, total, msg) / stop_cb()->bool / dump_path: 원본 덤프 경로.
    """
    from playwright.sync_api import sync_playwright

    def log(m):
        if logger:
            logger.info(m)

    def prog(d, t, m):
        if progress_cb:
            try:
                progress_cb(d, t, m)
            except Exception:
                pass

    def stopped():
        return bool(stop_cb and stop_cb())

    results = []
    with sync_playwright() as p:
        log("[수집] KB 브라우저(CDP) 접속 중...")
        browser = p.chromium.connect_over_cdp(cdp_url)

        page = _find_kb_page(browser, logger)
        if page is None:
            raise RuntimeError("KB 전산 페이지를 찾지 못했습니다. KB에 로그인된 디버그 브라우저가 떠 있는지 확인하세요.")

        cap = _Capture(logger, dump_path)
        # 모든 페이지에 리스너 부착(상세가 새 창/프레임일 수 있음)
        for ctx in browser.contexts:
            for pg in ctx.pages:
                try:
                    pg.on("response", cap.on_response)
                except Exception:
                    pass

        prog(0, 0, "보장분석 조회를 시도합니다...")
        log("[수집] 보장분석 목록 조회 트리거 시도(자동). 안 되면 사용자가 직접 '조회'를 눌러도 수집됩니다.")
        _try_click_inquiry(page, logger)

        # 목록 응답이 들어올 때까지 폴링(자동/수동 무관 수확)
        deadline = time.time() + wait_secs
        best_arr = None
        best_score = 0
        while time.time() < deadline:
            if stopped():
                log("[수집] 사용자 중단 요청.")
                break
            for r, obj in cap.parsed_json():
                for arr in _iter_arrays(obj):
                    s = _score_customer_array(arr)
                    if s > best_score:
                        best_score, best_arr = s, arr
            if best_arr and best_score >= 3:
                prog(0, len(best_arr), f"고객 목록 {len(best_arr)}건 포착(점수 {best_score:.1f}).")
                break
            prog(0, 0, "목록 응답 대기 중... (KB에서 '조회'를 눌러주세요)")
            time.sleep(1.5)

        cap.dump()

        if not best_arr:
            log("[수집] 목록 응답을 포착하지 못했습니다. (조회 미실행 또는 구조 불일치 — 덤프 확인)")
            return results

        total = len(best_arr)
        log(f"[수집] 고객 목록 {total}건 정규화 시작.")
        for i, row in enumerate(best_arr):
            if stopped():
                break
            rec = _normalize_row(row)
            if rec["customer_name"]:
                results.append(rec)
            prog(i + 1, total, f"{rec['customer_name'] or '(이름미상)'} 처리")
        log(f"[수집] 정규화 완료: 유효 {len(results)}건 / 전체 {total}건.")

    return results
