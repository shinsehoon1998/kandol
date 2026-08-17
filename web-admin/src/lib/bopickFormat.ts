// 보픽 전송용 — 상세수집 데이터(중첩 JSON)를 사람이 읽기 좋은 텍스트로 변환한다.
// 보픽 화면이 객체를 그대로 렌더하면 '[object Object]' 로 보이므로, 여기서 문자열로 만든다.

function won(v: any): string {
  const s = v == null ? '' : String(v).trim();
  return s;
}

/* v1.9.11 이전 수집분은 계약 조건이 한 덩어리로 뭉쳐 있다.
     예) cond = "월납20년납100세만기(계)박경순(피)박경순"
   KB 화면의 '·' 구분자가 CSS 로 생성되는 것이라 innerText 로 읽으면 사라진 탓이다.
   전체 재수집(약 35시간)이 끝나기 전에도 제대로 보이도록 여기서 되살린다.
   신규 수집분은 구조화 필드를 그대로 쓰므로 이 함수를 타지 않는다. */
export function splitCond(cond: any): {
  pay_cycle: string; pay_term: string; maturity: string;
  contractor: string; insured: string;
} {
  const s = String(cond || '').replace(/\s*·\s*/g, '');
  const pick = (re: RegExp) => (s.match(re)?.[1] || '').trim();
  return {
    pay_cycle:  pick(/(월납|연납|반기납|분기납|일시납)/),
    pay_term:   pick(/(\d+년납|\d+세납|전기납|종신납)/),
    maturity:   pick(/(\d+세만기|\d+년만기|종신(?!납))/),
    contractor: pick(/\(계\)\s*([^()]+)/),
    insured:    pick(/\(피\)\s*([^()]+)/),
  };
}

/* 계약 1건을 '구조화 필드 우선, 없으면 cond 에서 복원' 한 형태로 정규화. */
export function normalizeContract(x: any): any {
  if (!x || typeof x !== 'object') return x;
  const hasStructured = x.contractor || x.insured || x.pay_cycle || x.pay_term || x.maturity;
  if (hasStructured) return x;
  const f = splitCond(x.cond);
  return {
    ...x,
    pay_cycle: f.pay_cycle,
    pay_term: f.pay_term,
    maturity: f.maturity,
    contractor: f.contractor,
    insured: f.insured,
  };
}

// 보유계약: "상품A (48,000원) · 상품B (34,240원) ..."
export function fmtContracts(raw: any): string {
  const arr = (Array.isArray(raw?.contracts) ? raw.contracts : []).map(normalizeContract);
  if (!arr.length) return '';
  return arr
    .map((x: any) => {
      const p = (x?.product || '').trim();
      if (!p) return '';
      const m = (x?.monthly || '').toString().trim();
      // 계약 조건(월납·20년납·100세만기) — 신규 구조화 필드, 없으면 기존 cond 사용
      const terms = [x?.pay_cycle, x?.pay_term, x?.maturity].filter(Boolean).join('/')
        || (x?.cond || '').toString().trim();
      const period = [x?.start, x?.end].filter(Boolean).join('~')
        || (x?.period || '').toString().trim();
      const who = [
        x?.contractor ? `계약자 ${x.contractor}` : '',
        x?.insured ? `피보험자 ${x.insured}` : '',
      ].filter(Boolean).join(' ');
      const seg = [
        p,
        m ? `${m}원` : '',
        terms,
        period ? `보험기간 ${period}` : '',
        who,
      ].filter(Boolean).join(' | ');
      return seg;
    })
    .filter(Boolean)
    .join('\n');
}

/* 보유계약을 보픽 '컬럼 매칭'용 배열로 — 각 필드가 개별 키로 분리되어야
   보픽 화면에서 계약자/피보험자/보험기간/납입조건을 따로 쓸 수 있다. */
export function contractRows(raw: any): any[] {
  const arr = (Array.isArray(raw?.contracts) ? raw.contracts : []).map(normalizeContract);
  return arr
    .filter((x: any) => (x?.product || '').trim())
    .map((x: any) => ({
      상품명: x.product || null,
      보험사코드: x.insurer_code || null,
      월보험료: x.monthly ? `${x.monthly}원` : null,
      보험시작일: x.start || null,
      보험종료일: x.end || null,
      보험기간: [x.start, x.end].filter(Boolean).join('~') || x.period || null,
      납입주기: x.pay_cycle || null,
      납입기간: x.pay_term || null,
      만기: x.maturity || null,
      계약자: x.contractor || null,
      피보험자: x.insured || null,
      납입횟수: x.pay_count || null,
      납입완료보험료: x.paid_amount || null,
      납입예정보험료: x.remain_amount || null,
    }));
}

// 계약현황: contract_status 는 [정상행, 실효해지행]. 각 행의 '컬럼 값' 을 이어붙임.
export function fmtContractStatus(cs: any): string {
  if (!Array.isArray(cs) || !cs.length) return '';
  return cs
    .map((row: any) => {
      if (!row || typeof row !== 'object') return '';
      const parts = Object.entries(row)
        .map(([k, v]) => `${k} ${won(v)}`.trim())
        .filter(Boolean);
      return parts.join(', ');
    })
    .filter(Boolean)
    .join(' / ');
}

// 보장현황: {미가입, 부족, 충분} → "미가입 14 · 부족 11 · 충분 12"
export function fmtCoverageSummary(s: any): string {
  if (!s || typeof s !== 'object') return '';
  const order = ['미가입', '부족', '충분'];
  const parts: string[] = [];
  for (const k of order) {
    if (s[k] != null && String(s[k]).trim() !== '') parts.push(`${k} ${s[k]}`);
  }
  // 그 외 키도 뒤에 붙임
  for (const [k, v] of Object.entries(s)) {
    if (!order.includes(k) && v != null && String(v).trim() !== '') parts.push(`${k} ${v}`);
  }
  return parts.join(' · ');
}

// 담보별 가입상품: coverage_detail.byProduct → 담보별로 그룹핑
//  [상해사망] 한화손보 무배당한아름(8,000만), 삼성생명 다모은(100만)
//  [질병사망] ...
export function fmtByProduct(cd: any): string {
  const bp = Array.isArray(cd?.byProduct) ? cd.byProduct : [];
  if (!bp.length) return '';
  const byD: Record<string, string[]> = {};
  const order: string[] = [];
  for (const x of bp) {
    const d = (x?.담보 || '-').trim();
    if (!(d in byD)) { byD[d] = []; order.push(d); }
    const ins = (x?.보험사 || '').trim();
    const prod = (x?.상품 || '').trim();
    const amt = (x?.가입금액 || '').trim();
    const item = `${ins} ${prod}${amt ? `(${amt})` : ''}`.trim();
    if (item) byD[d].push(item);
  }
  return order.map((d) => `[${d}] ${byD[d].join(', ')}`).join('\n');
}
