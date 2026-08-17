-- ============================================================
-- 이어받기 done 기준에 '보유계약 구조화' 조건 추가 (v1.9.11 대응)
--
-- ⚠️ 운영 Supabase (ref eryswnijlvkzpeamjtqu) 적용 완료(2026-08-07).
--
-- ▣ 배경
--   v1.9.11 이전에는 계약 카드의 회색줄을 innerText 한 줄로 읽어
--   화면의 '·' 구분자(CSS 생성)가 사라진 채 뭉쳐 저장됐다.
--     예) cond = "월납20년납100세만기(계)이관우(피)이관우"
--   → 계약자/피보험자/보험기간/납입조건을 개별 컬럼으로 쓸 수 없어 보픽 매칭 불가.
--   v1.9.11 부터 토큰 단위 파싱으로 contractor/insured/start/end/pay_cycle/
--   pay_term/maturity/pay_count/... 를 구조화해 저장한다.
--
-- ▣ 처리
--   기수집분(구형 구조)을 재수집 대상으로 되돌린다.
--     · 계약 보유 고객 : contracts[0] 에 'contractor' 키가 있어야 done 인정
--     · 무보험 고객    : contracts 가 비어 있는 것이 정상이므로 구조 조건 면제
--     · 영구 조회불가  : 종전대로 skip 유지
--   실측(적용 시점): skip 대상 5,406 → 300 명, 재수집 대기 5,106 명(약 35시간 분량).
--
-- ▣ 되돌리기
--   아래 AND ( NOT (raw ? 'contracts') ... ) 블록만 지우고 다시 실행하면
--   직전 기준(완전수집 OR 영구불가)으로 복귀한다.
-- ============================================================

CREATE OR REPLACE FUNCTION get_detail_done_keys_via_device(
  p_tenant_id UUID,
  p_device_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_result JSONB;
BEGIN
  SELECT COALESCE(
           jsonb_agg(jsonb_build_object('name', customer_name, 'birth', COALESCE(birth, ''))),
           '[]'::jsonb)
    INTO v_result
    FROM public.customer_records
   WHERE tenant_id = p_tenant_id
     AND (
           (
             (raw->>'detail_collected') = 'true'
             AND jsonb_typeof(coverage_detail) = 'object'
             AND jsonb_typeof(coverage_detail->'rows') = 'array'
             AND jsonb_array_length(coverage_detail->'rows') > 0
             AND (
                   NOT (raw ? 'contracts')
                   OR jsonb_typeof(raw->'contracts') <> 'array'
                   OR jsonb_array_length(raw->'contracts') = 0
                   OR ((raw->'contracts'->0) ? 'contractor')
                 )
           )
           OR COALESCE((raw->>'detail_permanent_skip')::boolean, false)
         );
  RETURN v_result;
END;
$$;

GRANT EXECUTE ON FUNCTION get_detail_done_keys_via_device(UUID, UUID) TO anon, authenticated;
