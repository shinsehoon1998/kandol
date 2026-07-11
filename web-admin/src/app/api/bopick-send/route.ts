// 고객DB → 보픽플래너(보픽 리드 인입 API) 전송 프록시 (서버 라우트).
// 브라우저에서 직접 부르면 CORS·API키 노출 문제가 있어 서버에서 대신 전송한다.
//
// 보픽 인입 스펙: POST .../functions/v1/ingest-kandori , 헤더 x-api-key, 멱등(전화번호 중복 skip).
//
// 환경변수(Vercel: kandol Project → Settings → Environment Variables):
//   BOPICK_API_KEY  : 보픽 인입 API 키 (필수, 서버 전용 — 절대 코드/클라이언트에 하드코딩 금지)
//   BOPICK_API_URL  : (선택) 인입 엔드포인트. 미설정 시 아래 기본값 사용.

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

  // 보픽 스펙 형식으로 정리 — 모든 컬럼 보존(부가정보는 보픽이 metadata로 저장)
  const payload = {
    source: 'kandori-customer-db',
    count: customers.length,
    customers: customers.map((c) => ({
      customer_name: c.customer_name ?? null,
      phone: c.phone ?? null,
      address: c.address ?? null,
      birth: c.birth ?? null,
      age: c.age ?? null,
      gender: c.gender ?? null,
      monthly_premium: c.monthly_premium ?? null,
      policy_count: c.policy_count ?? null,
      consent_end_date: c.consent_end_date ?? null,
      registered_at: c.registered_at ?? null,
      analysis_date: c.analysis_date ?? null,
      contract_status: c.contract_status ?? null,
      coverage_summary: c.coverage_summary ?? null,
      coverage_detail: c.coverage_detail ?? null,
      device_name: c.devices?.device_name ?? null,
      crawled_at: c.crawled_at ?? null,
      raw: c.raw ?? null,
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
