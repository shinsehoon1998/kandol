-- ============================================================
-- 전화번호 표기 통일 (010-1234-5678) + 엑셀에서 날아간 앞자리 0 복원
--
-- ⚠️ 운영 Supabase (ref eryswnijlvkzpeamjtqu) 적용 완료(2026-08-17).
--
-- ▣ 배경
--   고객DB 전화번호가 세 가지 형식으로 섞여 있었다.
--     010-1234-5678  7,506건  (표준)
--     01012345678    2,005건  (하이픈 없음)
--     1012345678     1,690건  (앞자리 0 누락)
--   앞 0 누락은 엑셀에서 전화번호가 '숫자' 셀로 저장돼 선행 0이 사라진 것으로,
--   그대로는 전화를 걸 수 없고 중복 판정·매칭도 전부 어긋난다.
--   ※ 이 중 589건은 이미 보픽플래너로 잘못된 번호 상태로 전송됐다(별도 통보 필요).
--
-- ▣ 처리
--   1) format_phone_kr(): 숫자만 추출 → 국가코드 82 제거 → 앞 0 복원 →
--      자릿수에 맞춰 하이픈 삽입. 자릿수가 규칙에 안 맞으면 원형을 유지한다
--      (013-525-8400 같은 비정상 번호 2건은 손대지 않고 수동 검토 대상).
--   2) 기존 11,201건 일괄 정규화(실제 변경 3,695건). 변경 전 값은
--      _bak_phone_20260817 에 보존.
--   3) 저장 시점 정규화는 20260817b_ghost_row_guard.sql 의 upsert RPC 에서 수행.
--
-- ▣ 되돌리기
--   UPDATE public.customer_records c SET phone = b.phone_before
--     FROM public._bak_phone_20260817 b WHERE b.id = c.id;
-- ============================================================

CREATE OR REPLACE FUNCTION public.format_phone_kr(t TEXT) RETURNS TEXT
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE x TEXT; n INT;
BEGIN
  x := regexp_replace(COALESCE(t,''), '[^0-9]', '', 'g');
  IF x = '' THEN RETURN NULL; END IF;
  -- 국가코드 82 제거
  IF left(x,2) = '82' AND length(x) >= 11 THEN x := '0' || substr(x,3); END IF;
  -- 엑셀 숫자셀로 읽혀 날아간 앞자리 0 복원
  IF left(x,1) <> '0' THEN x := '0' || x; END IF;
  n := length(x);
  IF left(x,2) = '02' THEN                                   -- 서울 유선
    IF n = 10 THEN RETURN substr(x,1,2)||'-'||substr(x,3,4)||'-'||substr(x,7,4); END IF;
    IF n = 9  THEN RETURN substr(x,1,2)||'-'||substr(x,3,3)||'-'||substr(x,6,4); END IF;
    RETURN x;                                                -- 자릿수 이상 → 원형 유지
  END IF;
  IF n = 11 THEN RETURN substr(x,1,3)||'-'||substr(x,4,4)||'-'||substr(x,8,4); END IF;
  IF n = 10 THEN RETURN substr(x,1,3)||'-'||substr(x,4,3)||'-'||substr(x,7,4); END IF;
  RETURN x;                                                  -- 자릿수 이상 → 원형 유지
END; $$;

-- 이름 정규화(공백·전각공백 제거) — 유령행 가드의 동일인 판정에 사용
CREATE OR REPLACE FUNCTION public.norm_person_name(t TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
  SELECT regexp_replace(COALESCE(t,''), '[[:space:]　]', '', 'g');
$$;

-- 전화번호 비교용 숫자열(정규화 규칙은 format_phone_kr 에 일원화)
CREATE OR REPLACE FUNCTION public.norm_phone_digits(t TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
  SELECT regexp_replace(COALESCE(public.format_phone_kr(t), ''), '[^0-9]', '', 'g');
$$;


-- 변경 전 원본 보존(복구용). 안정화 후 DROP.
DROP TABLE IF EXISTS public._bak_phone_20260817;
CREATE TABLE public._bak_phone_20260817 AS
SELECT id, customer_name, birth, phone AS phone_before,
       public.format_phone_kr(phone) AS phone_after, registered_at, now() AS backed_up_at
  FROM public.customer_records
 WHERE COALESCE(phone,'') <> ''
   AND phone IS DISTINCT FROM public.format_phone_kr(phone);
ALTER TABLE public._bak_phone_20260817 ENABLE ROW LEVEL SECURITY;

UPDATE public.customer_records
   SET phone = public.format_phone_kr(phone),
       updated_at = timezone('utc'::text, now())
 WHERE COALESCE(phone,'') <> ''
   AND phone IS DISTINCT FROM public.format_phone_kr(phone);
