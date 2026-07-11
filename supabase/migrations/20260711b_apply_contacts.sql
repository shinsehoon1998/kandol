-- ============================================================
-- 엑셀 연락처(전화·주소) → 서버 기존 고객 즉시 매칭 갱신
--
-- ⚠️ 적용 대상: 운영 Supabase(shinsehoon1998's Org 의 kandol / ref eryswnijlvkzpeamjtqu)
--    SQL Editor 에 붙여넣고 RUN. (address 마이그레이션 20260711 을 먼저 적용한 뒤 실행)
--
-- 크롤링 없이, 업로드한 엑셀의 (이름+생년월일)로 서버 customer_records 를 찾아
-- 전화번호·주소만 갱신한다(신규 레코드는 만들지 않음 = '비교 매칭'). 매칭 건수 반환.
-- ============================================================

ALTER TABLE public.customer_records ADD COLUMN IF NOT EXISTS address TEXT;

CREATE OR REPLACE FUNCTION apply_contacts_via_device(
  p_tenant_id UUID,
  p_device_id UUID,
  p_records JSONB
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_rec JSONB;
  v_total INTEGER := 0;
  v_matched INTEGER := 0;   -- 서버 고객과 매칭되어 갱신된 엑셀행 수
  v_nm TEXT;
  v_bt TEXT;
  v_ph TEXT;
  v_ad TEXT;
  v_upd INTEGER;
BEGIN
  IF p_records IS NULL OR jsonb_typeof(p_records) <> 'array' THEN
    RETURN jsonb_build_object('matched', 0, 'total', 0);
  END IF;

  FOR v_rec IN SELECT * FROM jsonb_array_elements(p_records)
  LOOP
    v_nm := COALESCE(v_rec->>'customer_name', '');
    IF v_nm = '' THEN CONTINUE; END IF;
    v_total := v_total + 1;
    v_bt := COALESCE(v_rec->>'birth', '');
    v_ph := NULLIF(v_rec->>'phone', '');
    v_ad := NULLIF(v_rec->>'address', '');

    -- 이름 + (생년월일 있으면 일치) 로 기존 고객 찾아 전화·주소만 갱신
    UPDATE public.customer_records
       SET phone   = COALESCE(v_ph, phone),
           address = COALESCE(v_ad, address),
           updated_at = timezone('utc'::text, now())
     WHERE tenant_id = p_tenant_id
       AND customer_name = v_nm
       AND (v_bt = '' OR birth = v_bt);

    GET DIAGNOSTICS v_upd = ROW_COUNT;
    IF v_upd > 0 THEN
      v_matched := v_matched + 1;
    END IF;
  END LOOP;

  RETURN jsonb_build_object('matched', v_matched, 'total', v_total);
END;
$$;

GRANT EXECUTE ON FUNCTION apply_contacts_via_device(UUID, UUID, JSONB) TO anon, authenticated;
