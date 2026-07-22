-- ============================================================
-- 상세수집 '이어받기'(증분) — 이미 상세수집된 고객 (이름,생년월일) 조회 RPC
--
-- ⚠️ 적용 대상: 운영 Supabase (ref eryswnijlvkzpeamjtqu) SQL Editor 에 붙여넣고 RUN.
--
-- 문제: 에이전트는 기기PIN 인증(유저 세션 없음)이라 RLS 걸린 customer_records 를 직접
--       SELECT 하면 0건 → skip 목록이 비어 매 실행 전 고객 재수집.
-- 해결: SECURITY DEFINER RPC 로 raw.detail_collected=true 인 고객의 (name,birth)만 반환.
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
     AND (raw->>'detail_collected') = 'true';
  RETURN v_result;
END;
$$;

GRANT EXECUTE ON FUNCTION get_detail_done_keys_via_device(UUID, UUID) TO anon, authenticated;
