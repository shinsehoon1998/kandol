-- ============================================================
-- 고객DB 마이그레이션 — customer_records (KB 보장분석 수집)
--
-- ⚠️ 적용 대상 프로젝트: eryswnijlvkzpeamjtqu (깐돌이 운영 Supabase)
--    Supabase Dashboard → SQL Editor 에 아래 전체를 붙여넣고 RUN.
--    (MCP는 다른 프로젝트에 연결되어 있어 자동 적용 불가 → 수동 적용 필요)
-- ============================================================

CREATE TABLE IF NOT EXISTS public.customer_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES public.tenants(id) ON DELETE CASCADE NOT NULL,
    device_id UUID REFERENCES public.devices(id) ON DELETE SET NULL,
    customer_name TEXT NOT NULL,            -- 고객명
    birth TEXT NOT NULL DEFAULT '',         -- 생년월일
    age INTEGER,                            -- 나이
    gender TEXT,                            -- 성별
    analysis_date TEXT,                     -- 분석일자
    policy_count INTEGER,                   -- 가입건수
    monthly_premium BIGINT,                 -- 월보험료(원)
    consent_end_date TEXT,                  -- 동의종료일(D-day)
    contract_status JSONB,                  -- 계약현황
    coverage_summary JSONB,                 -- 보장현황(미가입/부족/충분)
    coverage_detail JSONB,                  -- 가입현황 상세(담보별 금액표, 한국어 키)
    raw JSONB,                              -- 수집 원본
    crawled_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    CONSTRAINT uq_customer_records UNIQUE (tenant_id, customer_name, birth)
);

CREATE INDEX IF NOT EXISTS idx_customer_records_tenant ON public.customer_records (tenant_id);
CREATE INDEX IF NOT EXISTS idx_customer_records_tenant_name ON public.customer_records (tenant_id, customer_name);

ALTER TABLE public.customer_records ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "회사별 고객DB 조회/수정 정책" ON public.customer_records;
CREATE POLICY "회사별 고객DB 조회/수정 정책" ON public.customer_records
    FOR ALL USING (
        tenant_id = (SELECT tenant_id FROM public.profiles WHERE id = auth.uid())
        OR
        (SELECT role FROM public.profiles WHERE id = auth.uid()) = 'super_admin'
    );

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
BEGIN
  IF p_records IS NULL OR jsonb_typeof(p_records) <> 'array' THEN
    RETURN 0;
  END IF;

  FOR v_rec IN SELECT * FROM jsonb_array_elements(p_records)
  LOOP
    IF COALESCE(v_rec->>'customer_name', '') = '' THEN
      CONTINUE;
    END IF;

    INSERT INTO public.customer_records (
      tenant_id, device_id, customer_name, birth, age, gender,
      analysis_date, policy_count, monthly_premium, consent_end_date,
      contract_status, coverage_summary, coverage_detail, raw, crawled_at, updated_at
    )
    VALUES (
      p_tenant_id,
      p_device_id,
      v_rec->>'customer_name',
      COALESCE(v_rec->>'birth', ''),
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
      timezone('utc'::text, now()),
      timezone('utc'::text, now())
    )
    ON CONFLICT (tenant_id, customer_name, birth) DO UPDATE
    SET device_id = EXCLUDED.device_id,
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
        crawled_at = EXCLUDED.crawled_at,
        updated_at = timezone('utc'::text, now());

    v_count := v_count + 1;
  END LOOP;

  RETURN v_count;
END;
$$;
