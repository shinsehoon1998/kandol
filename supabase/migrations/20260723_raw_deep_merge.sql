-- ============================================================
-- [치명적 버그 수정] upsert_customer_records_via_device: raw 컬럼 통째 덮어쓰기 → 깊은 병합
--
-- ⚠️ 적용 대상: 운영 Supabase (ref eryswnijlvkzpeamjtqu) SQL Editor 에 붙여넣고 RUN.
--
-- ▣ 문제(데이터 소실 + 이어받기 무력화):
--   기존 ON CONFLICT 절이  raw = COALESCE(EXCLUDED.raw, old.raw)  로 raw 컬럼을 통째 교체.
--   상세수집 재실행 시작 시 crawl_customers 가 '기본정보 flush'(목록 전체 upsert)를 먼저 하는데,
--   이 기본 레코드의 raw = {목록 컬럼들}(non-null, detail_collected/contracts 없음)이
--   기존에 상세수집된 고객의 raw 를 통째로 덮어써  detail_collected 플래그와 contracts(보유계약)를 삭제.
--   → (1) 수집해 둔 상세가 소실되고 (2) 플래그가 지워져 get_detail_done_keys 가 줄어들어
--      '이어받기(중복 skip)'가 무력화됨(이미 한 고객을 또 수집).
--   실측: 상세수집 완료수가 463 → 373 으로 역행, 오늘 8841명이 기본 flush 로 덮어써짐.
--
-- ▣ 해결: raw 를 '얕은 깊은병합'(object 끼리 ||)으로 변경.
--   - 기본 flush(목록컬럼만) 가 들어와도 기존 detail_collected/contracts 는 보존(신규 raw에 그 키가 없으므로).
--   - 상세 flush(contracts/detail_collected 포함) 가 들어오면 해당 키를 최신값으로 갱신(신규가 우선).
--   - EXCLUDED.raw 가 NULL 이면 기존 유지. object 가 아니면(방어) 신규값 사용.
-- ============================================================

CREATE OR REPLACE FUNCTION public.upsert_customer_records_via_device(
  p_tenant_id uuid, p_device_id uuid, p_records jsonb
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
AS $function$
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
        -- ▼▼▼ 핵심 수정: raw 통째 덮어쓰기 → object 깊은병합(기존 키 보존) ▼▼▼
        raw = CASE
                WHEN EXCLUDED.raw IS NULL THEN public.customer_records.raw
                WHEN jsonb_typeof(EXCLUDED.raw) = 'object'
                     AND jsonb_typeof(COALESCE(public.customer_records.raw, '{}'::jsonb)) = 'object'
                  THEN COALESCE(public.customer_records.raw, '{}'::jsonb) || EXCLUDED.raw
                ELSE EXCLUDED.raw
              END,
        -- ▲▲▲ 핵심 수정 끝 ▲▲▲
        registered_at = CASE WHEN v_reg
                             THEN COALESCE(public.customer_records.registered_at, timezone('utc'::text, now()))
                             ELSE public.customer_records.registered_at END,
        crawled_at = EXCLUDED.crawled_at,
        updated_at = timezone('utc'::text, now());

    v_count := v_count + 1;
  END LOOP;

  RETURN v_count;
END;
$function$;

GRANT EXECUTE ON FUNCTION public.upsert_customer_records_via_device(uuid, uuid, jsonb) TO anon, authenticated;
