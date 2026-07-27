-- ============================================================
-- 이어받기 대상에서 '영구 조회불가' 고객 제외 + 기존 데이터 소급 표식
--
-- ⚠️ 운영 Supabase (ref eryswnijlvkzpeamjtqu) 적용 완료(2026-07-27).
--
-- ▣ 배경(실측)
--   상세수집 팝업 스킵 사유 분포:
--     -S0001 프로그램이 정상적으로 수행되지 않았습니다  420명  ← 일시(KB 서버측 순간 장애)
--     사용량이 많아 조회가 지연되고 있습니다               5명  ← 일시(과부하)
--     개인정보노출이 제한된 고객으로 계약정보 조회 불가     6명  ← 영구
--     가입설계동의 미진행                                  1명  ← 영구
--   일시 사유는 재시도하면 성공하므로 계속 대상에 남겨야 하고,
--   영구 사유는 매 실행 재시도하면 시간만 낭비되므로 제외해야 한다.
--
-- ▣ 처리
--   ① 기존 영구불가 고객에 raw.detail_permanent_skip=true 소급 적용
--   ② done_keys RPC 가 '완전수집' OR '영구불가' 를 모두 skip 대상으로 반환
--      (에이전트 v1.9.7 이 신규 영구 사유에 표식을 남긴다)
-- ============================================================

-- ① 소급 표식
UPDATE public.customer_records
   SET raw = COALESCE(raw,'{}'::jsonb) || '{"detail_permanent_skip": true}'::jsonb
 WHERE raw ? 'detail_skip_reason'
   AND (raw->>'detail_skip_reason') ~ '개인정보노출.{0,10}제한|계약정보 조회가 불가|가입설계동의 미진행|마케팅.{0,6}미동의|동의를 진행'
   AND COALESCE((raw->>'detail_permanent_skip')::boolean, false) = false;

-- ② skip 대상 = 완전수집 OR 영구 조회불가
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
           ( (raw->>'detail_collected') = 'true'
             AND jsonb_typeof(coverage_detail) = 'object'
             AND jsonb_typeof(coverage_detail->'rows') = 'array'
             AND jsonb_array_length(coverage_detail->'rows') > 0 )
           OR
           COALESCE((raw->>'detail_permanent_skip')::boolean, false)
         );
  RETURN v_result;
END;
$$;

GRANT EXECUTE ON FUNCTION get_detail_done_keys_via_device(UUID, UUID) TO anon, authenticated;
