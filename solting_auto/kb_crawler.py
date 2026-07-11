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


# WebSquare 그리드 데이터모델 직접 추출 — 가장 확실/완전한 방법.
# 그리드는 전체 행(getRowCount)을 데이터모델에 보유하고 화면엔 일부만 가상 렌더한다.
# $p.getComponentById(id).getCellData(r,c) 로 전체를 즉시 읽는다(스크롤 불필요).
_WEBSQUARE_GRID_JS = r"""() => {
  let gel=null, header=[];
  for(const t of document.querySelectorAll('table')){
    const ths=[...t.querySelectorAll('th')].map(c=>(c.innerText||'').trim());
    const j=ths.join('|');
    if(j.includes('월보험료') && j.includes('생년월일')){ gel=t.closest('.w2grid'); header=ths; break; }
  }
  if(!gel) return null;
  if(!window.$p || typeof window.$p.getComponentById!=='function') return null;
  let g=null;
  try{ g=window.$p.getComponentById(gel.id); }catch(e){ return null; }
  if(!g || typeof g.getRowCount!=='function' || typeof g.getCellData!=='function') return null;
  let n=0; try{ n=g.getRowCount(); }catch(e){ return null; }
  if(!n || n<1) return null;
  let cols=header.length;
  try{ if(typeof g.getColumnCount==='function'){ const c=g.getColumnCount(); if(c) cols=c; } }catch(e){}
  const rows=[];
  for(let r=0;r<n;r++){
    const row=[];
    for(let c=0;c<cols;c++){ let v=''; try{ v=g.getCellData(r,c); }catch(e){} row.push(v==null?'':String(v)); }
    rows.push(row);
  }
  return {header, rows, total:n};
}"""


def _extract_via_websquare(page, logger=None):
    """WebSquare 그리드 데이터모델에서 전체 행을 직접 추출. 반환 (mapped_rows, score) 또는 None."""
    frames = []
    try:
        frames = list(page.frames)
    except Exception:
        frames = [page]
    best = None
    best_total = 0
    for fr in frames:
        try:
            res = fr.evaluate(_WEBSQUARE_GRID_JS)
        except Exception:
            res = None
        if not res:
            continue
        rows = res.get("rows") or []
        header = res.get("header") or []
        total = res.get("total", 0) or 0
        if rows and total > best_total:
            best_total = total
            best = (header, rows)
    if not best:
        return None
    header, rows = best
    mapped = _map_grid_table(header, rows)
    if logger:
        logger.info(f"[수집] WebSquare 데이터모델 직접추출: 전체 {len(rows)}행(getRowCount={best_total})")
    return mapped, _score_customer_array(mapped)


# ─────────────────────────────────────────────────────────────────────────
# 고객 상세(가입현황) 수집 — 더블클릭으로 진입한 상세 화면에서 담보별 보장/계약현황/
# 보장현황 요약/월보험료/보유계약리스트를 읽는다. 그리드는 데이터모델(getCellData)로
# 전체를 즉시 읽고, 보유계약(카드형)·요약(미가입/부족/충분)은 DOM 텍스트로 파싱한다.
# (라이브 검증: grd_bcvrInscoBcsfcGuarntInfo 37행, grd_contInfo, 보유계약 5건, 월보험료.)
# ─────────────────────────────────────────────────────────────────────────

# 상세 그리드가 로드된 프레임인지 판별하는 키 그리드 접미사
_DETAIL_KEY_SUFFIX = "grd_bcvrInscoBcsfcGuarntInfo"

# '모두펼치기' 토글을 ON으로 만든다(멱등: 이미 펼쳐져 있으면 건드리지 않음).
# 토글 라벨 span(class ch_cmmn_swctxt, text '모두펼치기')을 클릭. 펼침 여부는
# 담보 아래 개별상품(보험사명) 행이 렌더되는지로 판단하기 어려워, 상태 텍스트로 근사.
# '모두펼치기'는 chk_granTypeAll 체크박스(라이브 검증). 라벨 클릭으로 ON(checked=true,
# 화면상 노란 토글). 이미 ON이면 건드리지 않는다. 반환: 'on'(켜짐)/'no-toggle'.
_EXPAND_ALL_JS = r"""() => {
  const findInp=()=>document.querySelector('input.w2checkbox_input[id*="chk_granTypeAll"]')
    || [...document.querySelectorAll('input.w2checkbox_input')].find(i=>/granTypeAll/.test((i.id||'')+(i.name||'')));
  let inp=findInp();
  if(!inp){
    const txt=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&(e.innerText||'').trim()==='모두펼치기');
    if(txt){let sc=txt;for(let i=0;i<4;i++)if(sc.parentElement)sc=sc.parentElement;inp=sc.querySelector('input.w2checkbox_input');}
  }
  if(!inp) return 'no-toggle';
  if(!inp.checked){
    const lab=inp.id?document.querySelector('label[for="'+inp.id+'"]'):null;
    try{ (lab||inp).click(); }catch(e){}
  }
  return inp.checked ? 'on' : 'clicked';
}"""

# 모두펼치기(chk_granTypeAll) 가 실제로 켜졌는지(checked) 확인하는 JS.
_EXPAND_STATE_JS = (r"""()=>{const inp=document.querySelector('input.w2checkbox_input[id*="chk_granTypeAll"]')"""
                    r"""||[...document.querySelectorAll('input.w2checkbox_input')].find(i=>/granTypeAll/.test((i.id||'')+(i.name||'')));"""
                    r"""return inp?!!inp.checked:null;}""")

# 담보별 개별상품(accinbox)이 렌더/파싱 가능한지 확인하는 JS.
_ACCIN_READY_JS = ("()=>[...document.querySelectorAll('.ch_pc_cm_accinbox .ch_pc_dl_listbox')]"
                   ".some(e=>(e.innerText||'').trim().length>5)")

# 상세의 '가입현황' 서브탭을 활성화한다(보유계약리스트·전체보장현황은 이 탭에서만 렌더).
# 사용자 플로우: 더블클릭 → '가입현황' 클릭 → '모두펼치기' ON.
_ACTIVATE_GAIP_JS = r"""() => {
  // 상세 서브탭 바(w2tabcontrol_li)에서 '가입현황' 탭을 찾아 그 앵커를 클릭한다.
  // (라이브 검증: 전역 최소자식 요소 클릭은 오작동, 탭 LI의 <a> 클릭이 정확히 전환됨.)
  const lis=[...document.querySelectorAll('[class*=w2tabcontrol_li]')]
    .filter(li=>li.offsetParent && (li.innerText||'').trim()==='가입현황');
  if(lis.length){
    const li=lis[0];
    if(/active|selected/.test(li.className||'')) return 'already-on';
    const a=li.querySelector('a')||li;
    try{ a.click(); }catch(e){}
    try{ a.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true})); }catch(e){}
    return 'clicked-tab';
  }
  // 폴백: 텍스트가 정확히 '가입현황'인 앵커/버튼
  const alt=[...document.querySelectorAll('a,button,li,span')]
    .filter(e=>e.offsetParent && (e.innerText||'').trim()==='가입현황');
  if(alt.length){ try{ alt[0].click(); }catch(e){} return 'clicked-alt'; }
  return 'no-tab';
}"""

# 현재 열린 상세 화면의 모든 데이터를 읽어 구조화 객체로 반환.
_DETAIL_JS = r"""() => {
  if(!window.$p || typeof $p.getComponentById!=='function') return null;
  const bySuffix = suf => [...document.querySelectorAll('.w2grid')].find(g=>g.id.endsWith(suf));
  const readGrid = (suf) => {
    const el=bySuffix(suf); if(!el) return null;
    let c; try{ c=$p.getComponentById(el.id); }catch(e){ return null; }
    if(!c || typeof c.getRowCount!=='function') return null;
    let n=0,k=0; try{ n=c.getRowCount(); k=c.getColumnCount(); }catch(e){ return null; }
    const th=[...el.querySelectorAll('th')].map(x=>(x.innerText||'').trim()).filter(Boolean).slice(0,k);
    const rows=[];
    for(let r=0;r<n;r++){
      const o={};
      for(let j=0;j<k;j++){ let v=''; try{ v=c.getCellData(r,j); }catch(e){}
        const key=(th[j]&&th[j].trim())?th[j].trim():('col'+j);
        o[key]= v==null?'':String(v).replace(/<br\/?>/g,'/').trim(); }
      rows.push(o);
    }
    return {header:th, rows};
  };

  const bt=document.body.innerText||'';
  const num = re => { const m=bt.match(re); return m?m[1]:null; };

  // 담보별 전체보장현황(37) / 가입담보상세 / 계약현황(정상·실효)
  const cover = readGrid('grd_bcvrInscoBcsfcGuarntInfo');
  const cvrDetail = readGrid('grd_cvrDetailInfo');
  const cont = readGrid('grd_contInfo');

  // 모두펼치기 ON 시 담보별 개별상품(어느 보험사·상품이 그 담보를 보장하는지).
  // 구조(라이브 검증): .ch_pc_dl_listbox 의 leaf = [담보명, 담보합계, {보험사,상품,회사담보,금액}*N].
  let byProduct = [];
  document.querySelectorAll('.ch_pc_cm_accinbox .ch_pc_dl_listbox').forEach(it => {
    const lv = [...it.querySelectorAll('*')]
      .filter(e => e.children.length === 0 && (e.innerText || '').trim())
      .map(e => (e.innerText || '').trim());
    if (lv.length < 6) return;
    const dambo = lv[0];
    for (let i = 2; i + 4 <= lv.length; i += 4) {
      byProduct.push({담보: dambo, 보험사: lv[i], 상품: lv[i + 1],
                      회사담보: lv[i + 2], 가입금액: lv[i + 3]});
    }
  });

  // 보장현황 요약: 미가입/부족/충분
  const summary = {
    미가입: num(/미가입[\s\S]{0,6}?([0-9]+)\s*건/),
    부족:   num(/부족[\s\S]{0,6}?([0-9]+)\s*건/),
    충분:   num(/충분[\s\S]{0,6}?([0-9]+)/),
  };

  // 월보험료
  const premium = num(/월보험료[\s:]*([0-9,]+)\s*원/);

  // 보유계약리스트(카드형): 각 계약카드는 class 'ch_pc_cmlst_item'(라이브 검증).
  // 헤딩 부모체인엔 카드가 없어(형제 서브트리) 카드를 직접 선택해 파싱한다.
  let contracts=[];
  const items=[...document.querySelectorAll('[class*=cmlst_item]')]
    .filter(e=>e.offsetParent && /보험기간/.test(e.innerText||''));
  for(const it of items){
    const lines=(it.innerText||'').split('\n').map(s=>s.trim()).filter(Boolean);
    let period='',product='',cond='',monthly='';
    for(let i=0;i<lines.length;i++){
      if(/^보험기간/.test(lines[i])){
        period=lines[i].replace(/^보험기간/,'').trim();
        product=lines[i+1]||'';
        cond=lines[i+2]||'';
      }
      const m=(lines[i]||'').match(/([0-9]{1,3}(?:,[0-9]{3})+)\s*원/);
      if(m) monthly=m[1];
    }
    if(product && !/^총|^정상|^실효/.test(product))
      contracts.push({period, product, cond, monthly});
  }

  // 상세 화면의 고객 식별(교차오염 검증용):
  //  ① 탭/프로필의 '{이름}고객님 {주민7}' 우선(가장 안정적)
  //  ② 보유계약 조건의 (피)이름 보조
  let insured=null, insured_birth=null;
  const pm=bt.match(/([가-힣]{2,4})\s*고객님[\s\S]{0,12}?(\d{6})\d?/);
  if(pm){ insured=pm[1]; insured_birth=pm[2]; }
  if(!insured){ for(const c of contracts){ const m=(c.cond||'').match(/\(피\)\s*([가-힣]{2,4})/); if(m){ insured=m[1]; break; } } }

  return {
    insured,                 // 상세 화면 고객명 — 대조용
    insured_birth,           // 상세 화면 생년월일6 — 대조 보조
    monthly_premium: premium,
    coverage_summary: summary,
    contract_status: cont ? cont.rows : null,
    coverage_detail: cover ? {header:cover.header, rows:cover.rows,
                              detail: cvrDetail?cvrDetail.rows:null,
                              byProduct: byProduct} : null,
    contracts,
  };
}"""


def _find_detail_frame(page):
    """상세 그리드(grd_bcvrInscoBcsfcGuarntInfo)가 로드된 프레임을 찾는다."""
    frames = []
    try:
        frames = list(page.frames)
    except Exception:
        frames = [page]
    for fr in frames:
        try:
            has = fr.evaluate(
                "(suf)=>!![...document.querySelectorAll('.w2grid')].find(g=>g.id.endsWith(suf))",
                _DETAIL_KEY_SUFFIX)
            if has:
                return fr
        except Exception:
            continue
    return None


def _detail_customer_name(page):
    """상세 화면의 고객명('{이름}고객님')만 값싸게 읽는다(로드 대기용, 탭 활성화 없이)."""
    fr = _find_detail_frame(page)
    if not fr:
        return None
    try:
        return fr.evaluate("()=>{const m=(document.body.innerText||'').match(/([가-힣]{2,4})\\s*고객님/);"
                           "return m?m[1]:null;}")
    except Exception:
        return None


def _read_open_detail(page, logger=None, expand=True):
    """현재 열린 상세 화면을 읽어 구조화 dict 반환(없으면 None).
    expand=True 면 '모두펼치기'를 ON으로 만든 뒤 읽는다."""
    fr = _find_detail_frame(page)
    if not fr:
        return None
    # '가입현황' 서브탭 활성화(보유계약리스트·전체보장현황 렌더 선행조건)
    def _cmlst_ready():
        try:
            return fr.evaluate("()=>[...document.querySelectorAll('[class*=cmlst_item]')]"
                               ".some(e=>/보험기간/.test(e.innerText||''))")
        except Exception:
            return False
    try:
        fr.evaluate(_ACTIVATE_GAIP_JS)
        for _ in range(10):                 # 렌더까지 최대 ~2.5초
            page.wait_for_timeout(250)
            if _cmlst_ready():
                break
        # 아직 보유계약이 안 떴으면 탭을 강제 리로드(전체요약→가입현황) 후 재대기
        if not _cmlst_ready():
            fr.evaluate("()=>{const t=[...document.querySelectorAll('[class*=w2tabcontrol_li]')]"
                        ".find(li=>li.offsetParent&&(li.innerText||'').trim()==='전체요약');"
                        "if(t){(t.querySelector('a')||t).click();}}")
            page.wait_for_timeout(500)
            fr.evaluate(_ACTIVATE_GAIP_JS)
            for _ in range(10):
                page.wait_for_timeout(250)
                if _cmlst_ready():
                    break
    except Exception:
        pass
    if expand:
        # 모두펼치기(chk_granTypeAll)를 ON으로 켜고, 담보별 개별상품(accinbox)이 파싱
        # 가능해질 때까지 대기. (커버리지 없는 고객은 끝내 안 뜰 수 있어 시도 제한.)
        try:
            for _attempt in range(3):
                fr.evaluate(_EXPAND_ALL_JS)      # 토글 ON(이미 ON이면 무동작)
                ok = False
                for _ in range(7):
                    page.wait_for_timeout(250)
                    if fr.evaluate(_ACCIN_READY_JS):
                        ok = True
                        break
                if ok:
                    break
        except Exception:
            pass
    try:
        return fr.evaluate(_DETAIL_JS)
    except Exception as e:
        if logger:
            logger.info(f"[상세] 읽기 실패(무시): {e}")
        return None


def _merge_detail_into_record(rec, detail):
    """읽은 상세(detail)를 정규화 레코드(rec)에 병합. 보유계약은 raw.contracts 에 보관
    (마이그레이션 없이 기존 JSONB 컬럼만 사용)."""
    if not detail:
        return rec
    if detail.get("coverage_summary") and any(detail["coverage_summary"].values()):
        rec["coverage_summary"] = detail["coverage_summary"]
    if detail.get("contract_status"):
        rec["contract_status"] = detail["contract_status"]
    if detail.get("coverage_detail"):
        rec["coverage_detail"] = detail["coverage_detail"]
    # 월보험료: 상세값이 있으면 목록값보다 우선(정확)
    prem = detail.get("monthly_premium")
    if prem:
        rec["monthly_premium"] = re.sub(r"[^0-9]", "", str(prem)) or rec.get("monthly_premium")
    # 보유계약 + 상세수집 표식은 raw 안에 저장(스키마 변경 불필요)
    raw = rec.get("raw")
    if not isinstance(raw, dict):
        raw = {"_list": raw}
    if detail.get("contracts"):
        raw["contracts"] = detail["contracts"]
    raw["detail_collected"] = True
    rec["raw"] = raw
    return rec


def _iframe_chain_offset(frame):
    """중첩 iframe 체인의 뷰포트 오프셋(x,y) 합산 — 프레임 내 좌표를 페이지 좌표로 변환."""
    try:
        return frame.evaluate(
            "()=>{let x=0,y=0,w=window;try{while(w!==w.parent){const fe=w.frameElement;"
            "const r=fe.getBoundingClientRect();x+=r.x;y+=r.y;w=w.parent;}}catch(e){}return{x,y};}")
    except Exception:
        return {"x": 0, "y": 0}


def _visible_list_frame_grid(page):
    """화면에 '보이는' 보장분석 고객 목록 그리드를 (frame, gid)로 반환.
    (라이브 검증: 목록 그리드 grdSmtgranHistInfo, 헤더 월보험료+생년월일)."""
    for fr in list(page.frames):
        try:
            g = fr.evaluate(
                "()=>{const el=[...document.querySelectorAll('.w2grid')].find(g=>{"
                "const th=[...g.querySelectorAll('th')].map(x=>x.innerText||'').join('|');"
                "return th.includes('월보험료')&&th.includes('생년월일')&&g.offsetParent;});"
                "return el?el.id:null;}")
            if g:
                return fr, g
        except Exception:
            continue
    return None, None


def _return_to_list(page):
    """상세 화면에서 '(차세대)보장분석 메인' 탭을 눌러 목록으로 복귀.
    (라이브 검증: 이 탭 클릭 시 목록이 다시 보이고 상세가 닫힌다.)"""
    for fr in list(page.frames):
        try:
            ok = fr.evaluate(
                "()=>{const lis=[...document.querySelectorAll('[class*=w2tabcontrol_li]')]"
                ".filter(li=>li.offsetParent&&/보장분석 메인/.test(li.innerText||''));"
                "if(!lis.length)return 0;(lis[0].querySelector('a')||lis[0]).click();return 1;}")
            if ok:
                page.wait_for_timeout(900)
                return True
        except Exception:
            continue
    return False


def _list_rows_via_model(frame, gid):
    """목록 데이터모델에서 (name, birth6) 순서대로 전체 반환(col1=custNm, col2=생년월일)."""
    try:
        return frame.evaluate(
            "(g)=>{const c=$p.getComponentById(g);const n=c.getRowCount();const o=[];"
            "for(let i=0;i<n;i++){o.push({name:String(c.getCellData(i,1)||'').trim(),"
            "birth:String(c.getCellData(i,2)||'').replace(/[^0-9]/g,'').slice(0,6)});}return o;}", gid)
    except Exception:
        return []


def _grid_rowcount(frame, gid):
    try:
        return frame.evaluate("(g)=>{try{return $p.getComponentById(g).getRowCount()}catch(e){return -1}}", gid)
    except Exception:
        return -1


def _load_full_list(page, frame, gid, logger=None, max_iters=120):
    """지연로딩(스크롤 시 서버에서 계속 추가) 목록을 바닥까지 반복 스크롤해 전체 행을
    로드한다. 화면의 '{N}명' 표기를 목표치로 삼고, 실제 마우스 휠 + 스크롤바 이동을
    함께 써서 KB의 추가 로딩을 유도한다. getRowCount 가 목표 도달 또는 연속으로 안 늘
    때까지 반복. 로드 후 맨 위로 복귀."""
    # 목표 인원(있으면): 사이드바 '최근 보장분석 고객 N명' 등
    target = 0
    try:
        m = frame.evaluate("()=>{const t=(document.body.innerText||'').match(/([0-9]{1,5})\\s*명/g)||[];"
                           "let mx=0;for(const s of t){const v=parseInt(s);if(v>mx)mx=v;}return mx;}")
        target = int(m or 0)
    except Exception:
        target = 0
    # 그리드 중앙 페이지 좌표(실제 휠용)
    try:
        box = frame.evaluate("(g)=>{const el=document.getElementById(g);const r=el.getBoundingClientRect();"
                             "return {x:r.x+r.width/2,y:r.y+r.height/2};}", gid)
        off = _iframe_chain_offset(frame)
        wx, wy = box["x"] + off["x"], box["y"] + off["y"]
    except Exception:
        wx = wy = None

    last = _grid_rowcount(frame, gid)
    idle = 0
    for _ in range(max_iters):
        try:
            frame.evaluate(
                "(g)=>{const sy=document.getElementById(g).querySelector('[class*=w2grid_scrollY]');"
                "if(sy){sy.scrollTop=sy.scrollHeight;sy.dispatchEvent(new Event('scroll',{bubbles:true}));}}", gid)
        except Exception:
            pass
        if wx is not None:
            try:
                page.mouse.move(wx, wy)
                page.mouse.wheel(0, 2400)
            except Exception:
                pass
        page.wait_for_timeout(750)   # 서버 추가 로딩 대기
        n = _grid_rowcount(frame, gid)
        if n > last:
            last = n
            idle = 0
        else:
            idle += 1
        if target and last >= target:   # 표기 인원 도달 → 완료
            break
        if idle >= 8:                   # 8회 연속 안 늘면 수렴
            break
    try:
        frame.evaluate(
            "(g)=>{const sy=document.getElementById(g).querySelector('[class*=w2grid_scrollY]');"
            "if(sy){sy.scrollTop=0;sy.dispatchEvent(new Event('scroll',{bubbles:true}));}}", gid)
    except Exception:
        pass
    page.wait_for_timeout(400)
    if logger:
        tgt = f" / 표기 {target}명" if target else ""
        logger.info(f"[상세] 목록 전체 로드: {last}명{tgt}(지연로딩 스크롤).")
    return last


def _dblclick_customer_by_name(page, frame, gid, name, idx=None, total=None):
    """custNm==name 행을 찾아 실제 마우스 더블클릭. 대형 목록(수백명)에서는 5구간으론
    중간 고객을 놓치므로, 인덱스 비례 스크롤(라이브 검증: 정확)을 우선하고 촘촘한
    구간 스크롤로 보완한다."""
    try:
        mx = frame.evaluate(
            "(g)=>{const sy=document.getElementById(g).querySelector('[class*=w2grid_scrollY]');"
            "return sy?sy.scrollHeight-sy.clientHeight:0;}", gid) or 0
    except Exception:
        mx = 0
    off = _iframe_chain_offset(frame)
    # 후보 스크롤 위치: 인덱스 비례(±약간) 우선 + 촘촘한 전역 격자(0~1, 0.05 간격)
    fracs = []
    if idx is not None and total and total > 1:
        base = idx / (total - 1)
        fracs += [base, max(0.0, base - 0.03), min(1.0, base + 0.03)]
    fracs += [i / 20.0 for i in range(21)]
    seen = set()
    for frac in fracs:
        key = round(frac, 3)
        if key in seen:
            continue
        seen.add(key)
        try:
            frame.evaluate(
                "([g,st])=>{const sy=document.getElementById(g).querySelector('[class*=w2grid_scrollY]');"
                "if(sy){sy.scrollTop=st;sy.dispatchEvent(new Event('scroll',{bubbles:true}));}}",
                [gid, int(mx * frac)])
        except Exception:
            pass
        page.wait_for_timeout(300)
        # 이름이 보이면 그 행을 화면 중앙으로 스크롤(가장자리 잘림 → 더블클릭 미스 방지) 후 클릭
        try:
            found = frame.evaluate(
                "([g,nm])=>{const root=document.getElementById(g);"
                "for(const tr of [...root.querySelectorAll('tr')].filter(r=>r.offsetParent)){"
                "const td=tr.querySelector('td[col_id=custNm]');"
                "if(td&&td.innerText.trim()===nm){td.scrollIntoView({block:'center'});return true;}}"
                "return false;}", [gid, name])
        except Exception:
            found = False
        if not found:
            continue
        page.wait_for_timeout(350)
        try:
            r = frame.evaluate(
                "([g,nm])=>{const root=document.getElementById(g);"
                "for(const tr of [...root.querySelectorAll('tr')].filter(r=>r.offsetParent)){"
                "const td=tr.querySelector('td[col_id=custNm]');"
                "if(td&&td.innerText.trim()===nm){const rc=td.getBoundingClientRect();"
                "if(rc.height<6)return null;"
                "return{x:rc.x+rc.width/2,y:rc.y+rc.height/2};}}return null;}", [gid, name])
        except Exception:
            r = None
        if r:
            try:
                page.mouse.dblclick(r["x"] + off["x"], r["y"] + off["y"])
                return True
            except Exception:
                return False
    return False


def _norm_key(name, birth):
    return ((name or "").strip(), re.sub(r"[^0-9]", "", str(birth or ""))[:6])


def _collect_details(page, results, logger=None, progress_cb=None, stop_cb=None,
                     detail_limit=None, skip_keys=None, batch_cap=300):
    """목록에서 각 고객을 더블클릭해 상세를 수집, results 각 레코드에 병합.
    라이브 검증 플로우: (보장분석 메인 탭 복귀) → 이름매칭 더블클릭 → 상세 폴링·대조 →
    모두펼치기 ON 읽기 → 복귀 → 다음.
    - skip_keys: 이미 상세 수집된 (name,birth6) 집합 → 증분(재실행=이어받기).
    - detail_limit/batch_cap: 한 세션 상한(세션 누적한계 방어).
    - 고객명 대조로 교차오염 차단, 행별 최대 2회 재시도, 실패는 개별 skip."""
    skip_keys = skip_keys or set()
    _return_to_list(page)          # 상세가 열려 있었다면 목록으로
    page.wait_for_timeout(600)
    fr, gid = _visible_list_frame_grid(page)
    if not fr:
        if logger:
            logger.info("[상세] 보장분석 목록이 화면에 보이지 않습니다. '보장분석' 고객목록 화면을 띄워주세요.")
        return 0
    # 지연로딩 목록을 바닥까지 스크롤해 전체 로드(스크롤 시 서버에서 계속 추가됨).
    _load_full_list(page, fr, gid, logger)
    list_rows = _list_rows_via_model(fr, gid)
    if not list_rows:
        if logger:
            logger.info("[상세] 목록 데이터모델을 읽지 못했습니다.")
        return 0
    list_total = len(list_rows)
    idx_of = {(_norm_key(lr["name"], lr["birth"])): i for i, lr in enumerate(list_rows)}

    by_key = {}
    for rec in results:
        by_key.setdefault(_norm_key(rec["customer_name"], rec["birth"]), rec)

    cap = min(batch_cap, detail_limit or batch_cap)
    targets = [lr for lr in list_rows
               if _norm_key(lr["name"], lr["birth"])[0]
               and _norm_key(lr["name"], lr["birth"]) not in skip_keys
               and (by_key.get(_norm_key(lr["name"], lr["birth"]))
                    or by_key.get((_norm_key(lr["name"], lr["birth"])[0], "")))]
    total_targets = min(len(targets), cap)
    if logger:
        logger.info(f"[상세] 대상 {total_targets}명(전체 목록 {len(list_rows)} / 기수집 skip {len(skip_keys)}).")

    done = 0
    for lr in targets:
        if stop_cb and stop_cb():
            break
        if done >= cap:
            if logger:
                logger.info(f"[상세] 배치 상한({cap}) 도달 — 나머지는 다음 실행에서 이어받기.")
            break
        key = _norm_key(lr["name"], lr["birth"])
        rec = by_key.get(key) or by_key.get((key[0], ""))
        got = None
        for attempt in range(2):
            fr, gid = _visible_list_frame_grid(page)
            if not fr:
                _return_to_list(page)
                page.wait_for_timeout(700)
                fr, gid = _visible_list_frame_grid(page)
            if not fr:
                break
            if not _dblclick_customer_by_name(page, fr, gid, lr["name"],
                                              idx=idx_of.get(key), total=list_total):
                if logger and attempt == 0:
                    logger.info(f"[상세] {key[0]} 목록에서 행을 못 찾음 — 재시도.")
                _return_to_list(page)
                page.wait_for_timeout(600)
                continue
            # (1) 대상 고객으로 상세가 뜰 때까지 '값싼 이름확인'만 폴링(탭 재사용이라 이전 고객이
            #     잠시 보일 수 있음). 무거운 읽기를 반복하지 않아 빠르다(최대 12초).
            page.wait_for_timeout(1500)
            loaded = False
            wdl = time.time() + 12
            while time.time() < wdl:
                nm = _detail_customer_name(page)
                if nm and (nm == key[0] or not key[0]):
                    loaded = True
                    break
                page.wait_for_timeout(500)
            if not loaded:
                _return_to_list(page)
                page.wait_for_timeout(700)
                continue
            # (2) 로드 확인됨 → 상세 읽기(가입현황 활성 포함). 월보험료가 있는데 보유계약이
            #     아직 0이면 몇 번 더 읽어 렌더를 기다린다(최대 3회).
            got = None
            for _ in range(3):
                det = _read_open_detail(page, logger, expand=True)
                if det and det.get("insured") and (
                        det["insured"] == key[0] or (key[1] and det.get("insured_birth", "")[:6] == key[1])):
                    got = det
                    prem = re.sub(r"[^0-9]", "", str(det.get("monthly_premium") or ""))
                    if det.get("contracts") or not prem or prem == "0":
                        break
                page.wait_for_timeout(600)
            if got:
                gprem = re.sub(r"[^0-9]", "", str(got.get("monthly_premium") or ""))
                if got.get("contracts") or not gprem or gprem == "0":
                    break   # 완전 수집 → 확정
            got = None       # 불완전(월보험료 있는데 계약 0) → 재시도
            _return_to_list(page)
            page.wait_for_timeout(700)
        if got:
            _merge_detail_into_record(rec, got)
            done += 1
            if logger:
                logger.info(f"[상세] {key[0]} 완료 (보유계약 {len(got.get('contracts') or [])}건) {done}/{total_targets}")
        elif logger:
            logger.info(f"[상세] {key[0]} 진입 실패 — 건너뜀(다음 실행에서 재시도).")
        if progress_cb:
            try:
                progress_cb(done, total_targets, f"상세 {key[0]}")
            except Exception:
                pass
        _return_to_list(page)
        page.wait_for_timeout(600)
    if logger:
        logger.info(f"[상세] 상세 수집 완료: {done}건 병합.")
    return done


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
    """① WebSquare 데이터모델 직접추출(전체·즉시)을 우선 시도하고, 실패 시
    ② 가상스크롤 누적(멀티패스)으로 폴백한다. 반환 (rows, score)."""
    # ① 데이터모델 직접추출 — 전체 행을 한 번에(가장 확실)
    try:
        via = _extract_via_websquare(page, logger)
        if via and via[0] and via[1] >= 3:
            return via
    except Exception as e:
        if logger:
            logger.info(f"[수집] WebSquare 직접추출 불가, 스크롤로 전환: {e}")

    # ② 폴백: 스크롤 누적(멀티패스)
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
                    wait_secs=40, contact_excel_paths=None,
                    collect_detail=False, detail_limit=None, skip_detail_keys=None):
    """KB 보장분석 고객 데이터를 수집해 정규화 dict 리스트로 반환.

    progress_cb(done, total, msg) / stop_cb()->bool / dump_path: 원본 덤프 경로.
    collect_detail: True 면 각 고객을 더블클릭해 가입현황 상세(담보별 보장/계약현황/
      보유계약/보장요약)까지 수집(느림·모두펼치기 ON). skip_detail_keys: 이미 상세
      수집한 (name,birth6) 집합(증분). detail_limit: 이번 실행 상세 상한.
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

        # 상세(가입현황) 수집 — 옵트인. 각 고객 더블클릭→상세탭→모두펼치기 ON→읽기→닫기.
        if collect_detail and results and not stopped():
            log("[상세] 가입현황 상세 수집 시작(느림). 목록 화면을 그대로 두세요.")
            try:
                n = _collect_details(page, results, logger=logger,
                                     progress_cb=progress_cb, stop_cb=stopped,
                                     detail_limit=detail_limit,
                                     skip_keys=skip_detail_keys)
                log(f"[상세] 총 {n}명 상세 병합 완료.")
            except Exception as de:
                log(f"[상세] 상세 수집 중 오류(목록 데이터는 정상 반환): {de}")

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
