// 보픽 전송용 — 상세수집 데이터(중첩 JSON)를 사람이 읽기 좋은 텍스트로 변환한다.
// 보픽 화면이 객체를 그대로 렌더하면 '[object Object]' 로 보이므로, 여기서 문자열로 만든다.

function won(v: any): string {
  const s = v == null ? '' : String(v).trim();
  return s;
}

// 보유계약: "상품A (48,000원) · 상품B (34,240원) ..."
export function fmtContracts(raw: any): string {
  const arr = Array.isArray(raw?.contracts) ? raw.contracts : [];
  if (!arr.length) return '';
  return arr
    .map((x: any) => {
      const p = (x?.product || '').trim();
      const m = (x?.monthly || '').toString().trim();
      if (!p) return '';
      return m ? `${p} (${m}원)` : p;
    })
    .filter(Boolean)
    .join(' · ');
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
