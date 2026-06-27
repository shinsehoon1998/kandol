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
import xml.etree.ElementTree as ET


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

# KB 그리드의 비고객 플레이스홀더 이름(수집 제외)
_NON_CUSTOMER_NAMES = {"미등록", "미지정", "합계", "소계", "총계"}

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
        "phone": None,
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


def _xml_to_obj(elem):
    """XML 엘리먼트를 dict/list 구조로 변환. 같은 태그가 반복되면 리스트로 묶어
    (= 고객 행 배열) _iter_arrays 가 탐지할 수 있게 한다. 네임스페이스는 제거."""
    children = list(elem)
    if not children:
        return (elem.text or "").strip()
    d = {}
    for c in children:
        tag = c.tag.split('}')[-1]
        val = _xml_to_obj(c)
        if tag in d:
            if not isinstance(d[tag], list):
                d[tag] = [d[tag]]
            d[tag].append(val)
        else:
            d[tag] = val
    return d


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
            # KB(wsserver) 또는 데이터성 content-type 응답을 폭넓게 보관(정적 자원만 제외).
            # 보정 덤프에 실제 구조가 반드시 남도록 필터를 느슨하게.
            if not ("json" in ct or "xml" in ct or "text" in ct
                    or "wsserver" in url.lower()):
                return
            try:
                body = resp.text()
            except Exception:
                return
            if not body or not body.strip():
                return
            self.responses.append({"url": url, "status": resp.status,
                                   "ct": ct, "body": body})
        except Exception:
            pass

    def parsed_data(self):
        """보관된 응답을 JSON·XML 모두 파싱해 (응답메타, 파이썬객체) 리스트로 반환."""
        out = []
        for r in self.responses:
            b = (r["body"] or "").strip()
            if not b:
                continue
            if b[:1] in ("{", "["):
                try:
                    out.append((r, json.loads(b)))
                    continue
                except Exception:
                    pass
            if b[:1] == "<":
                try:
                    out.append((r, _xml_to_obj(ET.fromstring(b))))
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
    """'조회' 버튼을 후보 셀렉터로 시도 클릭. 보장분석은 iframe 안에 있으므로 프레임까지 탐색."""
    targets = [page]
    try:
        targets = list(page.frames)
    except Exception:
        pass
    for fr in targets:
        # 고객 목록과 무관한 프레임(빈/차트)에서의 오클릭 최소화: 고객 키워드 있는 프레임 우선
        for sel in INQUIRY_BUTTON_SELECTORS:
            try:
                loc = fr.locator(sel)
                if loc.count() > 0:
                    loc.first.click(timeout=2500)
                    if logger:
                        logger.info(f"[수집] '조회' 자동 클릭 성공: {sel}")
                    return True
            except Exception:
                continue
    return False


# 화면(DOM)의 모든 표를 {header:[th...], rows:[[td...]...]} 형태로 추출하는 JS.
# (보장분석 그리드는 헤더가 같은 table 안의 th 행에 있고, 본문은 td 행. 후보를 모두 넘겨
#  파이썬에서 헤더 키워드로 올바른 표를 고른다 — 행 수만으로 고르면 엉뚱한 표가 잡힘.)
_DOM_GRID_JS = r"""() => {
  const out=[];
  for (const t of [...document.querySelectorAll('table')]){
    const trs=[...t.querySelectorAll('tr')];
    const dataRows=trs.filter(r=>r.querySelectorAll('td').length>=3);
    if(dataRows.length<2) continue;
    const thRow=trs.find(r=>r.querySelector('th'));
    const header=thRow?[...thRow.querySelectorAll('th')].map(c=>(c.innerText||'').trim()):[];
    const rows=dataRows.map(r=>[...r.querySelectorAll('td')].map(c=>(c.innerText||'').trim()));
    out.push({header, rows});
  }
  return out;
}"""

# 고객 목록 그리드를 식별하는 헤더 키워드(보장분석 표 헤더 기준)
_GRID_HEADER_KEYS = ["고객명", "생년월일", "성별", "월보험료", "가입건수", "동의", "분석일자", "나이"]


def _score_grid(header, rows):
    """표가 보장분석 고객 목록일 가능성 점수 — 헤더 키워드 매칭 우선."""
    htxt = "".join(header)
    kw = sum(1 for k in _GRID_HEADER_KEYS if k in htxt)
    return kw * 10 + min(len(rows), 50) * 0.1


def _map_grid_table(header, rows):
    """헤더(th)와 본문(td)을 정렬해 dict 배열로 변환.
    본문 셀이 헤더보다 많으면(맨 앞 체크박스/순번 컬럼) 앞쪽 여분을 잘라 우측 정렬한다."""
    H = len(header)
    out = []
    for cells in rows:
        c = cells
        if H and len(c) > H:
            c = c[len(c) - H:]   # 앞쪽 여분(체크박스 등) 제거 → 헤더와 우측 정렬
        o = {}
        for i, val in enumerate(c):
            key = header[i].strip() if (i < H and header[i].strip()) else f"col{i}"
            if key not in o:
                o[key] = val
        out.append(o)
    return out


# 가상 스크롤 그리드 대응: 헤더 키워드로 올바른 표를 고른 뒤, 스크롤 컨테이너를 찾아
# 끝까지 스크롤하며 보이는 행을 누적·중복제거한다(가상 그리드는 화면에 보이는 ~10여 행만
# DOM에 두고 스크롤 시 내용이 교체되므로, 한 번만 읽으면 일부만 잡힌다).
_DOM_SCROLL_GRID_JS = r"""async () => {
  const KEYS = ["고객명","생년월일","성별","월보험료","가입건수","동의","분석일자","나이"];
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const headerOf = t => {
    for(const r of t.querySelectorAll('tr')){
      const ths=[...r.querySelectorAll('th')];
      if(ths.length) return ths.map(c=>(c.innerText||'').trim());
    }
    return [];
  };
  const dataRows = t => [...t.querySelectorAll('tr')].filter(r=>r.querySelectorAll('td').length>=3).length;

  let best=null, bestScore=0, bestHeader=[];
  for(const t of document.querySelectorAll('table')){
    if(dataRows(t)<1) continue;
    const h=headerOf(t); const htxt=h.join('');
    let kw=0; for(const k of KEYS) if(htxt.includes(k)) kw++;
    const sc=kw*10 + Math.min(dataRows(t),50)*0.1;
    if(kw>=2 && sc>bestScore){ bestScore=sc; best=t; bestHeader=h; }
  }
  if(!best) return null;

  function findScroller(start){
    // WebSquare w2grid 전용 가상 스크롤러(.w2grid_scrollY) 최우선 — 그리드 본문을 가상 렌더링한다.
    const gridRoot=start.closest('.w2grid');
    if(gridRoot){
      const sy=gridRoot.querySelector('[class*=w2grid_scrollY]');
      if(sy && sy.scrollHeight>sy.clientHeight+5) return sy;
    }
    let el=start.parentElement;
    while(el && el!==document.body){
      const st=getComputedStyle(el);
      if((/(auto|scroll)/).test(st.overflowY+st.overflow) && el.scrollHeight>el.clientHeight+5) return el;
      el=el.parentElement;
    }
    const wrap=start.closest('[class*=grid],[class*=Grid],[id*=grid],[id*=Grid]')||start.parentElement;
    if(wrap){
      for(const d of wrap.querySelectorAll('*')){
        const st=getComputedStyle(d);
        if((/(auto|scroll)/).test(st.overflowY) && d.scrollHeight>d.clientHeight+5) return d;
      }
    }
    return null;
  }
  const cont=findScroller(best);

  const seen=new Map();
  const harvest=()=>{
    for(const r of best.querySelectorAll('tr')){
      const tds=[...r.querySelectorAll('td')];
      if(tds.length<3) continue;
      const cells=tds.map(c=>(c.innerText||'').trim());
      const key=cells.join('');
      if(!seen.has(key)) seen.set(key, cells);
    }
  };
  harvest();
  if(cont){
    // KB 그리드는 서버에서 조금씩 추가 로딩(lazy)한다. 작은 보폭으로 내려가되, 바닥에 닿으면
    // 추가 로딩을 충분히(길게) 기다린다. 행 수 '또는' 스크롤 높이가 늘면 계속 진행하고,
    // 둘 다 연속 10회 안 늘 때(또는 시간예산 초과) 종료 → 매우 긴 목록도 끝까지 로드.
    const t0 = Date.now();
    let lastSize=-1, lastSH=-1, idle=0;
    for(let i=0;i<5000 && idle<10 && (Date.now()-t0)<90000;i++){
      const atBottom = cont.scrollTop + cont.clientHeight >= cont.scrollHeight - 2;
      if(atBottom){
        await sleep(650);                 // 바닥: 서버 추가 로딩 대기
      } else {
        cont.scrollTop = Math.min(cont.scrollTop + Math.max(cont.clientHeight*0.6, 60), cont.scrollHeight);
        cont.dispatchEvent(new Event('scroll',{bubbles:true}));
        await sleep(170);
      }
      harvest();
      const grew = (seen.size !== lastSize) || (cont.scrollHeight !== lastSH);
      if(grew){ idle=0; lastSize=seen.size; lastSH=cont.scrollHeight; }
      else { idle++; }
    }
    try{ cont.scrollTop=0; cont.dispatchEvent(new Event('scroll',{bubbles:true})); }catch(e){}
  }
  return {header: bestHeader, rows:[...seen.values()], scrolled: !!cont};
}"""


def _scrape_dom_grid_once(page):
    """프레임들에서 한 번의 스크롤-수확 패스. (header, rows, scrolled) 또는 None."""
    frames = []
    try:
        frames = list(page.frames)
    except Exception:
        frames = [page]
    best = None
    best_grid_score = 0
    best_scrolled = False
    for fr in frames:
        try:
            res = fr.evaluate(_DOM_SCROLL_GRID_JS)
        except Exception:
            res = None
        if not res:
            continue
        header = res.get("header") or []
        rows = res.get("rows") or []
        if len(rows) < 1:
            continue
        sc = _score_grid(header, rows)
        if sc > best_grid_score:
            best_grid_score = sc
            best = (header, rows)
            best_scrolled = bool(res.get("scrolled"))
    if not best:
        return None
    return best[0], best[1], best_scrolled


def _scrape_dom_grid(page, logger=None, stop_cb=None):
    """그리드를 스크롤·수확하되, KB가 패스마다 추가 로딩하므로 행 수가 더 안 늘 때까지
    여러 패스 반복(최대 6회)해 전체 목록을 확보한다. 반환 (rows, score)."""
    best_header, best_rows = None, []
    prev = -1
    for attempt in range(12):
        if stop_cb and stop_cb():
            break
        got = _scrape_dom_grid_once(page)
        if not got:
            break
        header, rows, scrolled = got
        if len(rows) >= len(best_rows):
            best_header, best_rows = header, rows
        if logger:
            logger.info(f"[수집] DOM 패스{attempt + 1}: {len(rows)}행 (누적 최대 {len(best_rows)}행)")
        if len(rows) <= prev:   # 더 안 늘면 종료(전체 로드 완료)
            break
        prev = len(rows)
    if not best_rows:
        return None, 0
    mapped = _map_grid_table(best_header, best_rows)
    if logger:
        logger.info(f"[수집] DOM 그리드 최종 {len(best_rows)}행, 헤더={best_header[:14]}")
    return mapped, _score_customer_array(mapped)


def _attach_crawl_filelog(logger, dump_path):
    """크롤러 로거에 디스크 파일 핸들러(crawl.log, 주민번호 마스킹)를 부착.
    인앱 콘솔이 닫혀도 진단 로그가 남도록 한다."""
    if not logger or not dump_path:
        return None
    try:
        import logging
        import os
        try:
            from solting_auto.logger import MaskingFilter
        except Exception:
            MaskingFilter = None
        log_path = os.path.join(os.path.dirname(dump_path), "crawl.log")
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        if MaskingFilter:
            fh.addFilter(MaskingFilter())
        logger.addHandler(fh)
        return fh
    except Exception:
        return None


def _detach_crawl_filelog(logger, fh):
    if logger and fh:
        try:
            logger.removeHandler(fh)
            fh.close()
        except Exception:
            pass


def crawl_customers(cdp_url="http://localhost:9222", logger=None,
                    progress_cb=None, stop_cb=None, dump_path=None,
                    wait_secs=40, contact_excel_paths=None):
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
    log_fh = _attach_crawl_filelog(logger, dump_path)
    with sync_playwright() as p:
        log("[수집] KB 브라우저(CDP) 접속 중...")
        browser = p.chromium.connect_over_cdp(cdp_url)

        page = _find_kb_page(browser, logger)
        if page is None:
            _detach_crawl_filelog(logger, log_fh)
            raise RuntimeError("KB 전산 페이지를 찾지 못했습니다. KB에 로그인된 디버그 브라우저가 떠 있는지 확인하세요.")

        cap = _Capture(logger, dump_path)
        # 기존 페이지 + 이후 생성되는 새 창/팝업까지 응답 리스너 부착
        for ctx in browser.contexts:
            for pg in ctx.pages:
                try:
                    pg.on("response", cap.on_response)
                except Exception:
                    pass
            try:
                ctx.on("page", lambda pg: pg.on("response", cap.on_response))
            except Exception:
                pass

        best_arr = None
        best_score = 0
        try:
            # 1) DOM 우선: 사용자가 이미 '조회'로 목록을 띄워둔 경우가 일반적이다.
            #    화면에 이미 보이는 목록은 아무것도 클릭하지 않고 그대로 수집한다
            #    (자동 클릭은 화면 상태를 바꿔 떠 있던 목록을 날릴 수 있어 위험).
            prog(0, 0, "화면의 보장분석 목록 확인 중...")
            try:
                dom_arr, dom_score = _scrape_dom_grid(page, logger, stop_cb=stopped)
                if dom_arr and dom_score >= 3:
                    best_arr, best_score = dom_arr, dom_score
                    log(f"[수집] 화면 목록 {len(best_arr)}건 즉시 수집(점수 {dom_score:.1f}).")
                    prog(0, len(best_arr), f"화면에서 {len(best_arr)}건 발견")
            except Exception as dom_err:
                log(f"[수집] DOM 1차 확인 실패(무시): {dom_err}")

            # 2) 화면에 목록이 없을 때만 '조회' 자동 클릭 후 네트워크/DOM 대기.
            #    ⚠️ Playwright sync API 에서 time.sleep 은 이벤트를 펌프하지 않아 on("response")
            #    콜백이 발화하지 않는다 → page.wait_for_timeout 으로 펌프해야 한다.
            if not best_arr and not stopped():
                log("[수집] 화면에 목록이 없어 '조회' 자동 클릭 시도(없으면 사용자가 직접 눌러도 됨).")
                _try_click_inquiry(page, logger)
                deadline = time.time() + wait_secs
                while time.time() < deadline:
                    if stopped():
                        log("[수집] 사용자 중단 요청.")
                        break
                    for r, obj in cap.parsed_data():
                        for arr in _iter_arrays(obj):
                            s = _score_customer_array(arr)
                            if s > best_score:
                                best_score, best_arr = s, arr
                    # 네트워크가 비어도 매 회 DOM 재확인(조회 결과가 화면에 렌더됐을 수 있음)
                    try:
                        d_arr, d_sc = _scrape_dom_grid(page, logger)
                        if d_arr and d_sc > best_score:
                            best_arr, best_score = d_arr, d_sc
                    except Exception:
                        pass
                    if best_arr and best_score >= 3:
                        prog(0, len(best_arr), f"고객 목록 {len(best_arr)}건 포착(점수 {best_score:.1f}).")
                        break
                    prog(0, 0, "목록 응답 대기 중... (KB에서 '조회'를 눌러주세요)")
                    try:
                        page.wait_for_timeout(1500)   # 대기 + 이벤트 펌프
                    except Exception:
                        time.sleep(1.5)
        finally:
            cap.dump()  # 예외/중단 시에도 진단 덤프 보존

        if not best_arr:
            log(f"[수집] 목록을 포착하지 못했습니다(응답 {len(cap.responses)}건 캡처). "
                f"조회 미실행 또는 구조 불일치 — output 덤프 확인.")
            _detach_crawl_filelog(logger, log_fh)
            return results

        total = len(best_arr)
        log(f"[수집] 고객 목록 {total}건 정규화 시작.")
        for i, row in enumerate(best_arr):
            if stopped():
                break
            if not isinstance(row, dict):
                continue
            rec = _normalize_row(row)
            name = rec["customer_name"]
            # KB 그리드의 비고객 플레이스홀더 행 제외(예: '미등록')
            if not name or name in _NON_CUSTOMER_NAMES:
                continue
            results.append(rec)
            prog(i + 1, total, f"{name} 처리")
        log(f"[수집] 정규화 완료: 유효 {len(results)}건 / 전체 {total}건.")

    # 전화번호 매칭(선택): 동의서 진행 엑셀(들)에서 (이름+생년월일6) → 전화번호 채움
    if contact_excel_paths:
        try:
            contacts = _read_excel_contacts(contact_excel_paths, logger)
            pmap = build_phone_map(contacts)
            matched = 0
            for rec in results:
                ph = pmap.get((rec["customer_name"], rec["birth"])) or pmap.get((rec["customer_name"], ""))
                if ph:
                    rec["phone"] = ph
                    matched += 1
            log(f"[수집] 전화번호 매칭: {matched}/{len(results)}건 (엑셀 연락처 {len(contacts)}건)")
        except Exception as ph_err:
            log(f"[수집] 전화번호 매칭 실패(무시): {ph_err}")

    _detach_crawl_filelog(logger, log_fh)
    return results


# ── 전화번호 매칭: 동의서 진행 엑셀에서 (이름/생년월일/전화) 추출 ──────────
_XL_NAME_HINTS = ["성명", "이름", "고객명", "고객", "name"]
_XL_JUMIN_HINTS = ["주민", "생년", "주민번호", "주민등록"]
_XL_PHONE_HINTS = ["휴대폰", "휴대전화", "핸드폰", "전화", "연락처", "phone", "mobile", "hp"]


def _read_excel_contacts(paths, logger=None):
    """동의서 진행 엑셀(들)에서 (성명, 생년월일6, 전화번호) 추출.
    반환 list[{customer_name, birth, phone}]. 헤더는 키워드로 유연 탐지."""
    import openpyxl
    if isinstance(paths, str):
        paths = [paths]
    out = []
    for path in paths or []:
        try:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        except Exception as e:
            if logger:
                logger.warning(f"[전화매칭] 엑셀 열기 실패: {path} ({e})")
            continue
        try:
            for ws in wb.worksheets:
                data = list(ws.iter_rows(values_only=True))
                hi, idx = None, {}
                for ri, row in enumerate(data[:12]):
                    cells = [str(c).replace(" ", "") if c is not None else "" for c in row]
                    tmp = {}
                    for i, h in enumerate(cells):
                        if "name" not in tmp and any(k in h for k in _XL_NAME_HINTS):
                            tmp["name"] = i
                        if "jumin" not in tmp and any(k in h for k in _XL_JUMIN_HINTS):
                            tmp["jumin"] = i
                        if "phone" not in tmp and any(k in h for k in _XL_PHONE_HINTS):
                            tmp["phone"] = i
                    if "name" in tmp and "phone" in tmp:
                        hi, idx = ri, tmp
                        break
                if hi is None:
                    continue
                for row in data[hi + 1:]:
                    def g(k):
                        j = idx.get(k)
                        return row[j] if (j is not None and j < len(row)) else None
                    name, phone, jumin = g("name"), g("phone"), g("jumin")
                    if not name or not phone:
                        continue
                    name = str(name).strip()
                    birth = ""
                    if jumin is not None:
                        d = re.sub(r"[^0-9]", "", str(jumin))
                        if len(d) >= 6:
                            birth = d[:6]
                    phone = re.sub(r"\s+", "", str(phone)).strip()
                    if name and phone:
                        out.append({"customer_name": name, "birth": birth, "phone": phone})
        finally:
            try:
                wb.close()
            except Exception:
                pass
    return out


def build_phone_map(contacts):
    """연락처 리스트 → {(이름, 생년월일6): 전화} 맵. 생년월일 없는 항목은 (이름,'')로도 보조 등록."""
    m = {}
    for c in contacts or []:
        nm, bt, ph = c.get("customer_name"), c.get("birth") or "", c.get("phone")
        if not nm or not ph:
            continue
        m[(nm, bt)] = ph
        m.setdefault((nm, ""), ph)
    return m
