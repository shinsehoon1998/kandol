-- ============================================================
-- 고객DB 주소지 — customer_records.address
--
-- ⚠️ 적용 대상: 운영 Supabase(shinsehoon1998's Org 의 kandol / ref eryswnijlvkzpeamjtqu)
--    SQL Editor 에 붙여넣고 RUN. (customer_records 마이그레이션을 먼저 적용한 뒤 실행)
--
-- 동의서(3단계) 엑셀에 '주소' 컬럼을 필수로 받아 고객DB에 함께 저장.
-- 이 스크립트는 address/registered_at 컬럼을 모두 보장하고 RPC를 갱신하므로,
-- 20260701(등록완료) 적용 여부와 무관하게 단독 실행 가능.
-- ============================================================

ALTER TABLE public.customer_records ADD COLUMN IF NOT EXISTS address TEXT;
ALTER TABLE public.customer_records ADD COLUMN IF NOT EXISTS registered_at TIMESTAMP WITH TIME ZONE;

CREATE OR REPLACE FUNCTION upsert_customer_records_via_device(
  p_tenant_id UUID,
  p_device_id UUID,
  p_records JSONB
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_rec JSONB;
  v_count INTEGER := 0;
  v_reg BOOLEAN;
BEGIN
  IF p_records IS NULL OR jsonb_typeof(p_records) <> 'array' THEN
    RETURN 0;
  END IF;

  FOR v_rec IN SELECT * FROM jsonb_array_elements(p_records)
  LOOP
    IF COALESCE(v_rec->>'customer_name', '') = '' THEN
      CONTINUE;
    END IF;
    v_reg := (v_rec->>'registered') = 'true';

    INSERT INTO public.customer_records (
      tenant_id, device_id, customer_name, birth, phone, address, age, gender,
      analysis_date, policy_count, monthly_premium, consent_end_date,
      contract_status, coverage_summary, coverage_detail, raw,
      registered_at, crawled_at, updated_at
    )
    VALUES (
      p_tenant_id,
      p_device_id,
      v_rec->>'customer_name',
      COALESCE(v_rec->>'birth', ''),
      NULLIF(v_rec->>'phone', ''),
      NULLIF(v_rec->>'address', ''),
      NULLIF(regexp_replace(COALESCE(v_rec->>'age',''), '[^0-9]', '', 'g'), '')::INTEGER,
      v_rec->>'gender',
      v_rec->>'analysis_date',
      NULLIF(regexp_replace(COALESCE(v_rec->>'policy_count',''), '[^0-9]', '', 'g'), '')::INTEGER,
      NULLIF(regexp_replace(COALESCE(v_rec->>'monthly_premium',''), '[^0-9]', '', 'g'), '')::BIGINT,
      v_rec->>'consent_end_date',
      v_rec->'contract_status',
      v_rec->'coverage_summary',
      v_rec->'coverage_detail',
      v_rec->'raw',
      CASE WHEN v_reg THEN timezone('utc'::text, now()) ELSE NULL END,
      timezone('utc'::text, now()),
      timezone('utc'::text, now())
    )
    ON CONFLICT (tenant_id, customer_name, birth) DO UPDATE
    SET device_id = EXCLUDED.device_id,
        phone = COALESCE(EXCLUDED.phone, public.customer_records.phone),
        address = COALESCE(EXCLUDED.address, public.customer_records.address),
        age = COALESCE(EXCLUDED.age, public.customer_records.age),
        gender = COALESCE(EXCLUDED.gender, public.customer_records.gender),
        analysis_date = COALESCE(EXCLUDED.analysis_date, public.customer_records.analysis_date),
        policy_count = COALESCE(EXCLUDED.policy_count, public.customer_records.policy_count),
        monthly_premium = COALESCE(EXCLUDED.monthly_premium, public.customer_records.monthly_premium),
        consent_end_date = COALESCE(EXCLUDED.consent_end_date, public.customer_records.consent_end_date),
        contract_status = COALESCE(EXCLUDED.contract_status, public.customer_records.contract_status),
        coverage_summary = COALESCE(EXCLUDED.coverage_summary, public.customer_records.coverage_summary),
        coverage_detail = COALESCE(EXCLUDED.coverage_detail, public.customer_records.coverage_detail),
        raw = COALESCE(EXCLUDED.raw, public.customer_records.raw),
        registered_at = CASE WHEN v_reg
                             THEN COALESCE(public.customer_records.registered_at, timezone('utc'::text, now()))
                             ELSE public.customer_records.registered_at END,
        crawled_at = EXCLUDED.crawled_at,
        updated_at = timezone('utc'::text, now());

    v_count := v_count + 1;
  END LOOP;

  RETURN v_count;
END;
$$;
