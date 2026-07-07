// 고객DB → 보픽플래너 CRM 전송 프록시 (서버 라우트).
// 브라우저에서 직접 외부 API를 부르면 CORS·API키 노출 문제가 있어 서버에서 대신 전송한다.
//
// 환경변수(Vercel Project → Settings → Environment Variables 에 설정):
//   BOPICK_API_URL   : 보픽플래너 고객 수신 엔드포인트 URL (필수)
//   BOPICK_API_KEY   : 인증 키 (선택, 있으면 Authorization: Bearer 로 전송)
//   BOPICK_API_AUTH_HEADER : 인증 헤더 이름 (선택, 기본 'Authorization', 값은 'Bearer <KEY>')
//
// ※ 실제 보픽플래너 API 스펙(요청 형식/필드명)을 받으면 아래 payload 매핑만 맞추면 된다.
//    현재는 customer_records 전체 컬럼을 그대로(customers 배열) 전송한다.

export async function POST(request: Request) {
  const url = process.env.BOPICK_API_URL;
  const key = process.env.BOPICK_API_KEY;
  const authHeader = process.env.BOPICK_API_AUTH_HEADER || 'Authorization';

  if (!url) {
    return Response.json(
      { ok: false, error: 'BOPICK_API_URL 미설정 — Vercel 환경변수에 보픽플래너 엔드포인트를 등록하세요.' },
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

  // 전체 컬럼을 정리해 전송 (내부 필드 devices 조인값은 device_name 만 평탄화)
  const payload = {
    source: 'kandori-customer-db',
    count: customers.length,
    customers: customers.map((c) => ({
      customer_name: c.customer_name ?? null,
      birth: c.birth ?? null,
      phone: c.phone ?? null,
      age: c.age ?? null,
      gender: c.gender ?? null,
      analysis_date: c.analysis_date ?? null,
      policy_count: c.policy_count ?? null,
      monthly_premium: c.monthly_premium ?? null,
      consent_end_date: c.consent_end_date ?? null,
      contract_status: c.contract_status ?? null,
      coverage_summary: c.coverage_summary ?? null,
      coverage_detail: c.coverage_detail ?? null,
      registered_at: c.registered_at ?? null,
      crawled_at: c.crawled_at ?? null,
      device_name: c.devices?.device_name ?? null,
      raw: c.raw ?? null,
    })),
  };

  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (key) headers[authHeader] = authHeader === 'Authorization' ? `Bearer ${key}` : key;

  try {
    const upstream = await fetch(url, {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
    });
    const text = await upstream.text();
    return Response.json(
      { ok: upstream.ok, status: upstream.status, response: text.slice(0, 2000) },
      { status: upstream.ok ? 200 : 502 }
    );
  } catch (e: any) {
    return Response.json({ ok: false, error: `보픽플래너 전송 실패: ${e?.message || e}` }, { status: 502 });
  }
}
