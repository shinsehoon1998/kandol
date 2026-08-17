-- ============================================================
-- 보픽플래너 전송 이력 추적
--
-- ⚠️ 운영 Supabase (ref eryswnijlvkzpeamjtqu) 적용 완료(2026-08-17).
--
-- ▣ 배경
--   지금까지 '누구를 보픽에 보냈는지' 기록이 전혀 없었다.
--   registered_at 은 동의서 처리 성공(등록완료) 마커이지 전송 이력이 아니다
--   (bopick-send 라우트는 registered_at 을 읽어 넘기기만 하고 쓰지 않는다).
--   보픽 인입 API 가 전화번호 중복을 skip 하므로 사실상 고객당 전송 기회가
--   한 번뿐인데, 무엇을 이미 보냈는지 몰라 매번 전량 재전송할 수밖에 없었다.
--
-- ▣ 처리
--   bopick_sent_at 컬럼 + mark_bopick_sent() RPC 추가.
--   어드민이 청크 전송에 성공할 때마다 해당 고객을 표시한다.
--   RPC 는 호출자(profiles.tenant_id)의 테넌트 레코드만 갱신한다.
--
-- ▣ 한계
--   과거 전송분은 기록이 없어 backfill 이 불가능하다. 적용 시점 기준으로
--   전원이 '미전송' 으로 보인다. 정확한 기준선이 필요하면 보픽 팀에서
--   보유 중인 전화번호 목록을 받아 대조하는 방법뿐이다.
--   또한 보픽 응답은 inserted/skipped 합계만 주고 건별 결과를 주지 않으므로
--   이 컬럼은 '보냈다'(전송 시도 성공)를 뜻하며 '보픽에 적재됐다'와는 다르다.
-- ============================================================

ALTER TABLE public.customer_records
  ADD COLUMN IF NOT EXISTS bopick_sent_at TIMESTAMPTZ;

COMMENT ON COLUMN public.customer_records.bopick_sent_at IS
  '보픽플래너로 전송한 시각. NULL = 미전송. 중복 skip 여부는 알 수 없음(보픽이 건별 결과를 주지 않음).';

CREATE INDEX IF NOT EXISTS ix_customer_records_bopick_sent
  ON public.customer_records (tenant_id, bopick_sent_at);

CREATE OR REPLACE FUNCTION public.mark_bopick_sent(p_ids UUID[])
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_tenant UUID;
  v_role   TEXT;
  n        INTEGER;
BEGIN
  IF p_ids IS NULL OR array_length(p_ids, 1) IS NULL THEN
    RETURN 0;
  END IF;

  SELECT tenant_id, role INTO v_tenant, v_role
    FROM public.profiles WHERE id = auth.uid();
  IF v_tenant IS NULL AND v_role IS DISTINCT FROM 'super_admin' THEN
    RAISE EXCEPTION '권한 없음';
  END IF;

  UPDATE public.customer_records
     SET bopick_sent_at = timezone('utc'::text, now())
   WHERE id = ANY(p_ids)
     AND (v_role = 'super_admin' OR tenant_id = v_tenant);
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$;

GRANT EXECUTE ON FUNCTION public.mark_bopick_sent(UUID[]) TO authenticated;
