-- ============================================================
-- 생년월일 없는 '유령 고객행' 생성 차단 (재발 방지)
--
-- ⚠️ 운영 Supabase (ref eryswnijlvkzpeamjtqu) 적용 완료(2026-08-17).
-- ⚠️ 선행: 20260817a_phone_format.sql (format_phone_kr / norm_* 함수)
--
-- ▣ 배경
--   고객 유일키가 (tenant_id, customer_name, birth) 이므로 birth 가 빈 문자열이면
--   같은 사람이라도 '별개 고객'으로 새 행이 생긴다.
--   동의서 탭의 연락처 엑셀 업로드(agent_app.upload_customer_db_from_excel)는
--   엑셀에 주민번호 컬럼이 없으면 birth='' 로 보내므로,
--   2026-08-03 05:21 한 번의 업로드로 이름+전화만 있는 중복행 2,019건이 생성됐다.
--   생년월일이 없어 상세수집이 구조적으로 불가능한데도 '미수집'으로 집계되어
--   진행률을 왜곡했다(실측 39% → 정리 후 45%).
--   같은 원인으로 2026-06-28 크롤러가 KB 메뉴 트리를 고객으로 오인 저장한 104건도 존재.
--   → 유령행 2,019 + 검증용 테스트행 2건은 삭제(_bak_customer_records_20260817 에 백업).
--
-- ▣ 처리 — birth 가 비어 있는 레코드는 새 고객으로 만들지 않는다
--   1) 이름+전화번호로 기존 고객이 '유일하게' 특정되면 그 고객에 병합(빈 칸만 채움)
--   2) 실패 시 이름만으로 유일하게 특정되면 그 고객에 병합
--      (동명이인이 2명 이상이면 모호하므로 병합하지 않음)
--   3) 그래도 못 찾고 고객 실체 신호(나이/성별/가입건수/분석일자/동의종료일/raw)도
--      없으면 폐기 — 이름+전화뿐인 행은 고객으로 등록하지 않는다
--   4) 실체 신호가 있으면(= KB 목록에서 생년월일만 못 읽은 진짜 고객) 종전대로 등록
--
--   병합은 덮어쓰지 않고 빈 칸만 채운다. 서버·엑셀 값이 실제로 다른 건은
--   자동 반영하지 않고 수동 검토 대상으로 남긴다.
--
--   추가로 저장 시점에 format_phone_kr() 로 전화번호 표기를 강제해
--   형식이 다시 흐트러지지 않게 한다.
--
-- ▣ 되돌리기
--   아래 "── 유령행 가드" 블록(IF v_birth = '' ... END IF)만 삭제하고 다시 실행하면
--   직전 동작(무조건 INSERT ... ON CONFLICT)으로 복귀한다.
-- ============================================================

CREATE OR REPLACE FUNCTION public.upsert_customer_records_via_device(
  p_tenant_id UUID,
  p_device_id UUID,
  p_records   JSONB
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $function$
DECLARE
  v_rec   JSONB;
  v_count INTEGER := 0;
  v_reg   BOOLEAN;
  v_birth TEXT;
  v_phone TEXT;
  v_ids   UUID[];
BEGIN
  IF p_records IS NULL OR jsonb_typeof(p_records) <> 'array' THEN
    RETURN 0;
  END IF;

  FOR v_rec IN SELECT * FROM jsonb_array_elements(p_records)
  LOOP
    IF COALESCE(v_rec->>'customer_name', '') = '' THEN
      CONTINUE;
    END IF;
    v_reg   := (v_rec->>'registered') = 'true';
    v_birth := COALESCE(v_rec->>'birth', '');

    -- ── 유령행 가드: birth 가 비면 새 고객으로 만들지 않는다 ──────
    IF v_birth = '' THEN
      v_phone := public.norm_phone_digits(v_rec->>'phone');
      v_ids   := NULL;

      -- 1) 이름 + 전화번호로 기존 고객 특정
      IF v_phone <> '' THEN
        SELECT array_agg(id) INTO v_ids
          FROM public.customer_records
         WHERE tenant_id = p_tenant_id
           AND COALESCE(birth,'') <> ''
           AND public.norm_person_name(customer_name)
               = public.norm_person_name(v_rec->>'customer_name')
           AND public.norm_phone_digits(phone) = v_phone;
      END IF;

      -- 2) 실패 시 이름만으로 유일하게 특정되는 경우
      IF v_ids IS NULL OR array_length(v_ids, 1) <> 1 THEN
        SELECT array_agg(id) INTO v_ids
          FROM public.customer_records
         WHERE tenant_id = p_tenant_id
           AND COALESCE(birth,'') <> ''
           AND public.norm_person_name(customer_name)
               = public.norm_person_name(v_rec->>'customer_name');
      END IF;

      -- 유일하게 특정됨 → 빈 칸만 채우고 종료(새 행 생성 안 함)
      IF array_length(v_ids, 1) = 1 THEN
        UPDATE public.customer_records
           SET phone   = COALESCE(phone,   public.format_phone_kr(v_rec->>'phone')),
               address = COALESCE(address, NULLIF(v_rec->>'address', '')),
               registered_at = CASE WHEN v_reg
                                    THEN COALESCE(registered_at, timezone('utc'::text, now()))
                                    ELSE registered_at END,
               updated_at = timezone('utc'::text, now())
         WHERE id = v_ids[1];
        v_count := v_count + 1;
        CONTINUE;
      END IF;

      -- 특정 실패 + 고객 실체 신호도 없음(이름+전화뿐) → 폐기
      IF  (v_rec->>'age')              IS NULL
      AND (v_rec->>'gender')           IS NULL
      AND (v_rec->>'policy_count')     IS NULL
      AND (v_rec->>'analysis_date')    IS NULL
      AND (v_rec->>'consent_end_date') IS NULL
      AND NULLIF(v_rec->'raw', 'null'::jsonb) IS NULL THEN
        CONTINUE;
      END IF;
      -- 실체 신호가 있으면 아래 정상 등록 경로로 진행
    END IF;
    -- ── 유령행 가드 끝 ─────────────────────────────────────────

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
      v_birth,
      public.format_phone_kr(v_rec->>'phone'),      -- 저장 시점 표기 통일
      NULLIF(v_rec->>'address', ''),
      NULLIF(regexp_replace(COALESCE(v_rec->>'age',''), '[^0-9]', '', 'g'), '')::INTEGER,
      v_rec->>'gender',
      v_rec->>'analysis_date',
      NULLIF(regexp_replace(COALESCE(v_rec->>'policy_count',''), '[^0-9]', '', 'g'), '')::INTEGER,
      NULLIF(regexp_replace(COALESCE(v_rec->>'monthly_premium',''), '[^0-9]', '', 'g'), '')::BIGINT,
      v_rec->>'consent_end_date',
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
        contract_status  = COALESCE(EXCLUDED.contract_status,  public.customer_records.contract_status),
        coverage_summary = COALESCE(EXCLUDED.coverage_summary, public.customer_records.coverage_summary),
        coverage_detail  = COALESCE(EXCLUDED.coverage_detail,  public.customer_records.coverage_detail),
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

GRANT EXECUTE ON FUNCTION public.upsert_customer_records_via_device(UUID, UUID, JSONB)
  TO anon, authenticated;
