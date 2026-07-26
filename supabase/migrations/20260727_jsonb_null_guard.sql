-- ============================================================
-- [치명적 수정] upsert RPC: JSONB 'null' 이 typed 컬럼을 덮어써 전체보장현황 소실
--
-- ⚠️ 운영 Supabase (ref eryswnijlvkzpeamjtqu) 적용 대상.
--
-- ▣ 문제
--   상세수집 재실행 시작의 '기본정보 flush'(목록 전체 upsert)는 레코드에
--   coverage_detail / coverage_summary / contract_status 를 JSON null 로 담아 보낸다.
--   RPC 는 이를 v_rec->'coverage_detail' 로 꺼내는데, JSON 값이 null 이면
--   결과는 SQL NULL 이 아니라 **JSONB 스칼라 'null'** 이다.
--   따라서 COALESCE(EXCLUDED.coverage_detail, old) 가 전혀 보호하지 못하고
--   (COALESCE 는 SQL NULL 만 건너뜀) 기존에 수집한 값을 'null' 로 덮어쓴다.
--
--   실측(2026-07-27): 상세완료 3094명 중 3075명의 coverage_detail/coverage_summary/
--   contract_status 가 모두 jsonb 'null' (동일 3075/19 분포). 반면 raw.contracts 는
--   2861명 생존 — 앞서 raw 만 깊은병합으로 고쳤기 때문. 트랜잭션 재현으로 확정.
--
--   → 상담에 필요한 '전체보장현황(담보별 보장 표)'이 사실상 전부 소실.
--
-- ▣ 해결
--   JSONB 추출 시 NULLIF(x, 'null'::jsonb) 로 JSON null → SQL NULL 로 정규화한다.
--   그러면 COALESCE 가 정상 동작해 기존 값이 보존되고, 실제 값이 올 때만 갱신된다.
--   INSERT VALUES 에도 적용해 신규 행에 jsonb 'null' 이 저장되지 않게 한다.
--   (phone/address 등 ->> 로 뽑는 text 컬럼은 JSON null 이 SQL NULL 이 되므로 영향 없음)
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
      -- ▼ JSON null → SQL NULL 정규화(신규행에 jsonb 'null' 저장 방지)
      NULLIF(v_rec->'contract_status',  'null'::jsonb),
      NULLIF(v_rec->'coverage_summary', 'null'::jsonb),
      NULLIF(v_rec->'coverage_detail',  'null'::jsonb),
      NULLIF(v_rec->'raw',              'null'::jsonb),
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
        -- ▼▼▼ 핵심 수정: EXCLUDED 가 이미 NULLIF 로 정규화되어 COALESCE 가 정상 보호 ▼▼▼
        contract_status  = COALESCE(EXCLUDED.contract_status,  public.customer_records.contract_status),
        coverage_summary = COALESCE(EXCLUDED.coverage_summary, public.customer_records.coverage_summary),
        coverage_detail  = COALESCE(EXCLUDED.coverage_detail,  public.customer_records.coverage_detail),
        -- ▲▲▲ 핵심 수정 끝 ▲▲▲
        raw = CASE
                WHEN EXCLUDED.raw IS NULL THEN public.customer_records.raw
                WHEN jsonb_typeof(EXCLUDED.raw) = 'object'
                     AND jsonb_typeof(COALESCE(public.customer_records.raw, '{}'::jsonb)) = 'object'
                  THEN COALESCE(public.customer_records.raw, '{}'::jsonb) || EXCLUDED.raw
                ELSE EXCLUDED.raw
              END,
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
