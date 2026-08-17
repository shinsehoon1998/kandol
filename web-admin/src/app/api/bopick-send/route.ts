// 고객DB → 보픽플래너(보픽 리드 인입 API) 전송 프록시 (서버 라우트).
// 브라우저에서 직접 부르면 CORS·API키 노출 문제가 있어 서버에서 대신 전송한다.
//
// 보픽 인입 스펙(2026-08-17 개정, Edge Function v8):
//   POST .../functions/v1/ingest-kandori , 헤더 x-api-key
//   · mode="upsert" → 기존 고객이면 갱신. **생략하면 skip 이라 보강분이 반영되지 않는다.**
//   · 중복 판정: external_id(1순위) → phone(2순위, 숫자만 비교)
//   · 갱신 대상: metadata 전체(병합) · name · phone · birth_date
//     status·배정 담당자·방문일정·배차비용은 건드리지 않으므로 상담 중인 리드에 재전송해도 안전.
//   · 응답: { ok, mode, received, inserted, updated, skipped, errors }
//
// 환경변수(Vercel: kandol Project → Settings → Environment Variables):
//   BOPICK_API_KEY  : 보픽 인입 API 키 (필수, 서버 전용 — 절대 코드/클라이언트에 하드코딩 금지)
//   BOPICK_API_URL  : (선택) 인입 엔드포인트. 미설정 시 아래 기본값 사용.

import { parseRegion } from '@/lib/region';
import { fmtContracts, fmtContractStatus, fmtCoverageSummary, fmtByProduct, contractRows } from '@/lib/bopickFormat';

const DEFAULT_URL = 'https://aftallfjjwzfphqeuwuc.supabase.co/functions/v1/ingest-kandori';

export async function POST(request: Request) {
  const url = process.env.BOPICK_API_URL || DEFAULT_URL;
  const key = process.env.BOPICK_API_KEY;

  if (!key) {
    return Response.json(
      { ok: false, error: 'BOPICK_API_KEY 미설정 — Vercel 환경변수에 보픽 인입 API 키(x-api-key)를 등록하세요.' },
      { status: 500 }
    );
  }

  let body: any = {};
  try {
    body = await request.json();
  } catch {
    return Response.json({ ok: false, error: '잘못된 요청(JSON 파싱 실패)' }, { status: 400 });
  }

  const customers: any[] = Array.isArray(body?.customers) ? body.customers : [];
  if (customers.length === 0) {
    return Response.json({ ok: false, error: '전송할 고객이 없습니다.' }, { status: 400 });
  }
  if (customers.length > 5000) {
    return Response.json({ ok: false, error: `한 번에 최대 5,000건까지 전송 가능합니다(요청 ${customers.length}건). 나눠서 보내주세요.` }, { status: 400 });
  }

  // 보픽 스펙 형식으로 정리.
  // ⚠️ 상세수집 데이터(계약현황·보장상세 등)를 중첩 JSON 그대로 보내면 보픽 화면이
  //    '[object Object]' 로 표시되므로, 사람이 읽기 좋은 '텍스트'로 변환해 보낸다.
  const payload = {
    // 생략하면 보픽이 skip 처리해 상세수집 보강분이 반영되지 않는다.
    mode: 'upsert',
    source: 'kandori-customer-db',
    count: customers.length,
    customers: customers.map((c) => ({
      // 중복 판정 1순위. 전화번호가 정정돼도 같은 사람으로 이어붙도록 고객 UUID 를 보낸다
      // (전화번호만으로 매칭하면 번호 정정 시 한 사람이 두 건으로 늘어난다).
      external_id: c.id ?? null,
      customer_name: c.customer_name ?? null,
      phone: c.phone ?? null,
      address: c.address ?? null,
      region: parseRegion(c.address).sido || null,        // 시/도(지역 필터용)
      region_sigungu: parseRegion(c.address).sigungu || null,  // 시/군/구
      birth: c.birth ?? null,
      age: c.age ?? null,
      gender: c.gender ?? null,
      monthly_premium: c.monthly_premium ?? null,
      policy_count: c.policy_count ?? null,
      consent_end_date: c.consent_end_date ?? null,
      registered_at: c.registered_at ?? null,
      analysis_date: c.analysis_date ?? null,
      device_name: c.devices?.device_name ?? null,
      crawled_at: c.crawled_at ?? null,
      // ── 읽기 좋은 상세 요약(보픽 표시용) ──────────────────────────
      보유계약: fmtContracts(c.raw) || null,
      // 컬럼 매칭용 — 계약별로 계약자·피보험자·보험기간·납입주기/기간/만기가 개별 키로 분리
      보유계약목록: contractRows(c.raw),
      계약현황: fmtContractStatus(c.contract_status) || null,
      보장현황: fmtCoverageSummary(c.coverage_summary) || null,
      담보별가입상품: fmtByProduct(c.coverage_detail) || null,
      // 원본 구조(프로그램 연동용) — 보픽이 필요 시 파싱. 표시는 위 텍스트 사용.
      raw_detail: {
        contract_status: c.contract_status ?? null,
        coverage_summary: c.coverage_summary ?? null,
        coverage_detail: c.coverage_detail ?? null,
        contracts: c.raw?.contracts ?? null,
      },
    })),
  };

  try {
    const upstream = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'x-api-key': key },
      body: JSON.stringify(payload),
    });
    const text = await upstream.text();
    let result: any = text;
    try { result = JSON.parse(text); } catch { /* keep text */ }
    return Response.json(
      { ok: upstream.ok && (result?.ok !== false), status: upstream.status, result },
      { status: upstream.ok ? 200 : 502 }
    );
  } catch (e: any) {
    return Response.json({ ok: false, error: `보픽 전송 실패: ${e?.message || e}` }, { status: 502 });
  }
}
