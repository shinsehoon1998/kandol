-- ============================================================
-- 동명이인에게 잘못 붙은 전화·주소 정리 (보픽 FB-0019)
--
-- ⚠️ 적용 대상: 운영 Supabase(kandol / ref eryswnijlvkzpeamjtqu). **대표 확인 뒤에만** SQL Editor 에서 RUN.
-- ⚠️ 순서: (1) 이 저장소 fix/contact-match-namesake 머지 → v1.9.17 빌드·배포
--          (2) 이 SQL — 백업 후 비우기
--          (3) v1.9.17 에이전트에서 원본 동의서 엑셀로 수집(전화번호 매칭) 다시 → 보픽 재전송
--          (2)를 (1)보다 먼저 하면, 옛 에이전트가 다시 수집할 때 이름만 맞춰 또 붙인다.
--
-- ▣ 무슨 일이었나
--   kb_crawler 의 연락처 매칭이 (이름, 생년월일)이 안 맞으면 이름만 같은 첫 연락처를 붙였다.
--   그래서 KB 의 B(동명이인) 기록에 엑셀 A 의 전화·주소가 붙어 보픽까지 갔다.
--   흔적: 같은 이름·같은 번호인데 생년월일이 다른 기록 묶음. 2026-10-28 실측 24묶음 48건.
--
-- ▣ 무엇을 비우나
--   묶음 안에서 동의 등록(registered_at)이 없는 기록의 phone·address 만(실측 34건).
--   등록된 기록은 동의서 엑셀의 주민번호로 정확히 맞춘 것이라 그대로 둔다(실측 14건).
--   묶음 전체가 미등록이면(10묶음 20건) 누가 주인인지 원본 엑셀 없이는 몰라 둘 다 비운다 —
--   (3)에서 고친 매칭이 진짜 주인에게만 다시 붙인다.
--   이 SQL 이 못 찾는 경우(진짜 주인이 KB 에 없어 묶음이 안 생긴 경우)는 보픽 고객 카드의
--   「생년월일 확인」(통화로 확인한 값과 다르면 보장 내역을 가림)이 현장에서 걸러낸다.
--
-- ▣ 되돌리기
--   update public.customer_records c set phone = b.phone, address = b.address
--     from public._bak_namesake_20261028 b where b.id = c.id;
-- ============================================================

create table if not exists public._bak_namesake_20261028 as
select c.id, c.tenant_id, c.customer_name, c.birth, c.phone, c.address, c.registered_at, c.bopick_sent_at,
       now() as backed_up_at
  from public.customer_records c
  join (
    select tenant_id, customer_name, regexp_replace(coalesce(phone, ''), '\D', '', 'g') as ph
      from public.customer_records
     where coalesce(phone, '') <> ''
     group by 1, 2, 3
    having count(distinct birth) >= 2
  ) g on g.tenant_id = c.tenant_id
     and g.customer_name = c.customer_name
     and g.ph = regexp_replace(coalesce(c.phone, ''), '\D', '', 'g')
 where c.registered_at is null;

-- 이름·전화가 담긴 표다. public 에 RLS 없이 두면 공개 키(anon)로 읽힌다 — 정책 없이 잠근다.
alter table public._bak_namesake_20261028 enable row level security;

-- 백업 건수부터 본다(실측 34). 크게 다르면 멈추고 다시 볼 것.
select count(*) as backed_up from public._bak_namesake_20261028;

update public.customer_records c
   set phone = null, address = null, updated_at = timezone('utc'::text, now())
  from public._bak_namesake_20261028 b
 where b.id = c.id;
