-- ============================================================
-- 고객DB 등록완료 상태 — customer_records.registered_at
--
-- ⚠️ 적용 대상: 운영 Supabase(shinsehoon1998's Org 의 kandol / ref eryswnijlvkzpeamjtqu)
--    SQL Editor 에 붙여넣고 RUN. (customer_records 마이그레이션을 먼저 적용한 뒤 실행)
--
-- 동의서(3단계) 등록 성공 시 서버에 '등록완료 시각'을 기록 → 콘솔 고객DB에서 등록여부
-- 중앙 확인 + 여러 PC 간 공유 가능(로컬 파일과 별개의 서버 기준).
-- ============================================================

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
      tenant_id, device_id, customer_name, birth, phone, age, gender,
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
        -- 등록완료: registered=true 일 때만 최초 시각 기록(이후 수집으로 지워지지 않음)
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
