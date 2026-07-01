-- 1. 테넌트 (기업 고객사) 테이블
CREATE TABLE public.tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- 2. 사용자 프로필 테이블
CREATE TABLE public.profiles (
    id UUID REFERENCES auth.users ON DELETE CASCADE PRIMARY KEY,
    tenant_id UUID REFERENCES public.tenants(id) ON DELETE SET NULL,
    name TEXT,
    role TEXT CHECK (role IN ('super_admin', 'tenant_admin', 'user')) DEFAULT 'user' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- 3. 디바이스 (깐돌이 에이전트 기기 연동) 테이블
CREATE TABLE public.devices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES public.tenants(id) ON DELETE CASCADE NOT NULL,
    hwid TEXT NOT NULL UNIQUE, -- CPU ID + Mainboard UUID
    device_name TEXT NOT NULL,
    status TEXT CHECK (status IN ('pending', 'approved', 'blocked')) DEFAULT 'pending' NOT NULL,
    pin_code VARCHAR(6) UNIQUE, -- 어드민 기기 승인 시 발급해 주는 6자리 간편 인증번호
    last_heartbeat TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- 4. 매크로 좌표 및 딜레이 설정 (회사별 커스텀 값)
CREATE TABLE public.macro_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES public.tenants(id) ON DELETE CASCADE UNIQUE NOT NULL,
    offsets JSONB NOT NULL, -- { "image_add_x": 1117, "send_x": 1185, ... }
    delays JSONB NOT NULL,  -- { "search_wait": 2.0, "dialog_open_wait": 1.0, ... }
    ratios JSONB NOT NULL,  -- { "pop_send_x": 0.693, "pop_send_y": 0.873 }
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    updated_by UUID REFERENCES public.profiles(id)
);

-- 5. 통합 실행 감사 로그 (솔팅 전과정 및 EDMS 업로드 추적)
CREATE TABLE public.execution_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES public.tenants(id) ON DELETE CASCADE NOT NULL,
    user_id UUID REFERENCES public.profiles(id) ON DELETE SET NULL,
    device_id UUID REFERENCES public.devices(id) ON DELETE SET NULL,
    job_type TEXT CHECK (job_type IN ('excel_processing', 'edms_upload')) NOT NULL,
    filename TEXT NOT NULL,
    status TEXT CHECK (status IN ('queued', 'running', 'success', 'failed', 'stopped')) DEFAULT 'queued' NOT NULL,
    progress_done INTEGER DEFAULT 0 NOT NULL,
    progress_total INTEGER DEFAULT 0 NOT NULL,
    current_stage TEXT, -- 'solting', 'insurance', 'stamping', 'kb_scan', 'edms_upload'
    last_message TEXT,
    error_reason TEXT,
    error_screenshot_url TEXT,
    report_file_url TEXT, -- Excel 결과 리포트 다운로드 주소
    started_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    ended_at TIMESTAMP WITH TIME ZONE
);

-- 6. 에이전트 OTA 패치 관리 테이블
CREATE TABLE public.agent_updates (
    version TEXT PRIMARY KEY,
    binary_url TEXT NOT NULL, -- 업데이터 .exe 다운로드 링크
    release_notes TEXT,
    is_mandatory BOOLEAN DEFAULT false NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- --------------------------------------------------
-- Triggers: auth.users 신규 가입 시 profiles 자동 매핑
-- --------------------------------------------------

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger AS $$
BEGIN
  INSERT INTO public.profiles (id, name, role)
  VALUES (
    new.id, 
    COALESCE(new.raw_user_meta_data->>'name', split_part(new.email, '@', 1)), 
    'user'
  );
  RETURN new;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE OR REPLACE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- --------------------------------------------------
-- RLS (Row Level Security) 설정 및 격리 정책
-- --------------------------------------------------

-- 테이블별 RLS 활성화
ALTER TABLE public.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.macro_configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.execution_logs ENABLE ROW LEVEL SECURITY;

-- 1. profiles RLS
CREATE POLICY "전체 사용자 프로필 조회 정책" ON public.profiles
    FOR SELECT USING (true);

CREATE POLICY "본인 프로필 업데이트 정책" ON public.profiles
    FOR UPDATE USING (auth.uid() = id);

-- 2. tenants RLS
CREATE POLICY "본인 회사 조회 정책" ON public.tenants
    FOR SELECT USING (
        id = (SELECT tenant_id FROM public.profiles WHERE id = auth.uid())
        OR 
        (SELECT role FROM public.profiles WHERE id = auth.uid()) = 'super_admin'
    );

-- 3. devices RLS
CREATE POLICY "회사별 기기 조회/수정 정책" ON public.devices
    FOR ALL USING (
        tenant_id = (SELECT tenant_id FROM public.profiles WHERE id = auth.uid())
        OR 
        (SELECT role FROM public.profiles WHERE id = auth.uid()) = 'super_admin'
    );

-- 4. macro_configs RLS
CREATE POLICY "회사별 매크로설정 조회/수정 정책" ON public.macro_configs
    FOR ALL USING (
        tenant_id = (SELECT tenant_id FROM public.profiles WHERE id = auth.uid())
        OR 
        (SELECT role FROM public.profiles WHERE id = auth.uid()) = 'super_admin'
    );

-- 5. execution_logs RLS
CREATE POLICY "회사별 로그 조회/수정 정책" ON public.execution_logs
     FOR ALL USING (
         tenant_id = (SELECT tenant_id FROM public.profiles WHERE id = auth.uid())
         OR 
         (SELECT role FROM public.profiles WHERE id = auth.uid()) = 'super_admin'
     );

-- --------------------------------------------------
-- Phase 2: PIN 로그인 & RPC 보안 함수 (SECURITY DEFINER)
-- --------------------------------------------------

-- 1. 기기 승인 시 6자리 PIN 자동 생성 트리거 함수
CREATE OR REPLACE FUNCTION public.generate_unique_pin_code()
RETURNS trigger AS $$
DECLARE
  v_pin TEXT;
  v_exists BOOLEAN;
BEGIN
  IF NEW.status = 'approved' AND NEW.pin_code IS NULL THEN
    LOOP
      v_pin := lpad(floor(random() * 1000000)::text, 6, '0');
      SELECT EXISTS(SELECT 1 FROM public.devices WHERE pin_code = v_pin) INTO v_exists;
      IF NOT v_exists THEN
        NEW.pin_code := v_pin;
        EXIT;
      END IF;
    END LOOP;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_generate_device_pin
  BEFORE UPDATE ON public.devices
  FOR EACH ROW
  EXECUTE FUNCTION public.generate_unique_pin_code();

-- 2. 테넌트(회사) 목록 조회 RPC (클라이언트가 회원가입 시 선택용)
CREATE OR REPLACE FUNCTION get_tenant_list()
RETURNS TABLE (id UUID, name TEXT)
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
  RETURN QUERY SELECT public.tenants.id, public.tenants.name FROM public.tenants ORDER BY public.tenants.name;
END;
$$;

-- 3. HWID 기반 기기 상태 조회 RPC
CREATE OR REPLACE FUNCTION get_device_status(p_hwid TEXT)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_device RECORD;
  v_tenant RECORD;
BEGIN
  SELECT * INTO v_device FROM public.devices WHERE hwid = p_hwid;
  IF NOT FOUND THEN
    RETURN jsonb_build_object('registered', false);
  END IF;

  SELECT * INTO v_tenant FROM public.tenants WHERE id = v_device.tenant_id;

  RETURN jsonb_build_object(
    'registered', true,
    'id', v_device.id,
    'device_name', v_device.device_name,
    'status', v_device.status,
    'pin_code', v_device.pin_code,
    'tenant_name', COALESCE(v_tenant.name, '')
  );
END;
$$;

-- 4. 기기 등록 신청 RPC (클라이언트에서 호출)
CREATE OR REPLACE FUNCTION register_device_via_client(p_tenant_id UUID, p_hwid TEXT, p_device_name TEXT)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_device RECORD;
BEGIN
  INSERT INTO public.devices (tenant_id, hwid, device_name, status)
  VALUES (p_tenant_id, p_hwid, p_device_name, 'pending')
  ON CONFLICT (hwid) DO UPDATE 
  SET tenant_id = EXCLUDED.tenant_id,
      device_name = EXCLUDED.device_name,
      status = 'pending'
  RETURNING * INTO v_device;

  RETURN jsonb_build_object(
    'success', true,
    'device', jsonb_build_object(
      'id', v_device.id,
      'device_name', v_device.device_name,
      'status', v_device.status
    )
  );
END;
$$;

-- 5. PIN 번호 및 HWID 검증 RPC
CREATE OR REPLACE FUNCTION verify_device_pin(p_pin_code TEXT, p_hwid TEXT)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_device RECORD;
  v_tenant RECORD;
  v_config RECORD;
  v_result JSONB;
BEGIN
  SELECT * INTO v_device FROM public.devices WHERE pin_code = p_pin_code;
  IF NOT FOUND THEN
    RAISE EXCEPTION '유효하지 않은 인증번호입니다.';
  END IF;

  IF v_device.status = 'blocked' THEN
    RAISE EXCEPTION '차단된 기기입니다. 관리자에게 문의하세요.';
  ELSIF v_device.status = 'pending' THEN
    RAISE EXCEPTION '승인 대기 중인 기기입니다. 관리자 승인을 기다려주세요.';
  END IF;

  IF v_device.hwid <> p_hwid THEN
    RAISE EXCEPTION '이 인증번호는 등록된 기기와 매치되지 않습니다.';
  END IF;

  SELECT * INTO v_tenant FROM public.tenants WHERE id = v_device.tenant_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION '소속 회사를 찾을 수 없습니다.';
  END IF;

  SELECT * INTO v_config FROM public.macro_configs WHERE tenant_id = v_device.tenant_id;

  v_result := jsonb_build_object(
    'device', jsonb_build_object(
      'id', v_device.id,
      'device_name', v_device.device_name,
      'hwid', v_device.hwid,
      'status', v_device.status
    ),
    'tenant', jsonb_build_object(
      'id', v_tenant.id,
      'name', v_tenant.name
    ),
    'config', CASE WHEN v_config.id IS NOT NULL THEN jsonb_build_object(
      'offsets', v_config.offsets,
      'delays', v_config.delays,
      'ratios', v_config.ratios
    ) ELSE NULL END
  );

  RETURN v_result;
END;
$$;

-- 6. 실행 로그 생성 RPC (RLS 우회)
CREATE OR REPLACE FUNCTION create_execution_log_via_device(
  p_tenant_id UUID,
  p_device_id UUID,
  p_job_type TEXT,
  p_filename TEXT,
  p_current_stage TEXT
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_log_id UUID;
BEGIN
  INSERT INTO public.execution_logs (
    tenant_id,
    device_id,
    job_type,
    filename,
    status,
    progress_done,
    progress_total,
    current_stage
  )
  VALUES (
    p_tenant_id,
    p_device_id,
    p_job_type,
    p_filename,
    'queued',
    0,
    0,
    p_current_stage
  )
  RETURNING id INTO v_log_id;

  RETURN v_log_id;
END;
$$;

-- 7. 실행 로그 진행률 업데이트 RPC (RLS 우회)
CREATE OR REPLACE FUNCTION update_execution_log_progress_via_device(
  p_log_id UUID,
  p_done INTEGER,
  p_total INTEGER,
  p_last_message TEXT
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
  UPDATE public.execution_logs
  SET progress_done = p_done,
      progress_total = p_total,
      last_message = p_last_message
  WHERE id = p_log_id;
END;
$$;

-- 8. 실행 로그 상태 업데이트 RPC (RLS 우회)
CREATE OR REPLACE FUNCTION update_execution_log_status_via_device(
  p_log_id UUID,
  p_status TEXT,
  p_error_reason TEXT,
  p_error_screenshot_url TEXT,
  p_report_file_url TEXT
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
  UPDATE public.execution_logs
  SET status = p_status,
      error_reason = p_error_reason,
      error_screenshot_url = p_error_screenshot_url,
      report_file_url = p_report_file_url,
      ended_at = CASE WHEN p_status IN ('success', 'failed', 'stopped') THEN timezone('utc'::text, now()) ELSE ended_at END
  WHERE id = p_log_id;
END;
$$;

-- 9. 기기 하트비트 업데이트 RPC
CREATE OR REPLACE FUNCTION heartbeat_device_via_pin(p_device_id UUID)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
  UPDATE public.devices
  SET last_heartbeat = timezone('utc'::text, now())
  WHERE id = p_device_id;
END;
$$;

-- 10. 실행 로그 상태 확인 RPC (원격 중단 감지용)
CREATE OR REPLACE FUNCTION check_execution_log_status(p_log_id UUID)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_status TEXT;
BEGIN
  SELECT status INTO v_status FROM public.execution_logs WHERE id = p_log_id;
  RETURN v_status;
END;
$$;

-- 11. 매크로 설정 저장 RPC
CREATE OR REPLACE FUNCTION save_macro_config_via_device(
  p_tenant_id UUID,
  p_offsets JSONB,
  p_delays JSONB
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
  INSERT INTO public.macro_configs (tenant_id, offsets, delays, ratios, updated_at)
  VALUES (p_tenant_id, p_offsets, p_delays, '{"pop_send_x": 0.693989, "pop_send_y": 0.873817}'::jsonb, timezone('utc'::text, now()))
  ON CONFLICT (tenant_id) DO UPDATE
  SET offsets = EXCLUDED.offsets,
      delays = EXCLUDED.delays,
      ratios = EXCLUDED.ratios,
      updated_at = EXCLUDED.updated_at;
END;
$$;

-- 12. 매크로 설정 조회 RPC
CREATE OR REPLACE FUNCTION get_macro_config_via_device(p_tenant_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_config RECORD;
BEGIN
  SELECT * INTO v_config FROM public.macro_configs WHERE tenant_id = p_tenant_id;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  RETURN jsonb_build_object(
    'offsets', v_config.offsets,
    'delays', v_config.delays,
    'ratios', v_config.ratios
  );
END;
$$;

-- ============================================================
-- 7. 고객DB (KB 보장분석 수집) 테이블 customer_records
--    설계사가 KB전산 '보장분석'에서 보는 본인 담당 고객 데이터를 수집해 저장.
--    컬럼 = 화면 한국어 항목 매핑(고객명/생년월일/나이/성별/분석일자/가입건수/
--    월보험료/동의종료일/계약현황/보장현황/가입현황상세). 원본(raw)도 함께 보관.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.customer_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES public.tenants(id) ON DELETE CASCADE NOT NULL,
    device_id UUID REFERENCES public.devices(id) ON DELETE SET NULL,
    customer_name TEXT NOT NULL,            -- 고객명
    birth TEXT NOT NULL DEFAULT '',         -- 생년월일
    phone TEXT,                             -- 전화번호(동의서 엑셀 매칭)
    age INTEGER,                            -- 나이
    gender TEXT,                            -- 성별
    analysis_date TEXT,                     -- 분석일자
    policy_count INTEGER,                   -- 가입건수
    monthly_premium BIGINT,                 -- 월보험료(원)
    consent_end_date TEXT,                  -- 동의종료일(D-day)
    contract_status JSONB,                  -- 계약현황(정상/실효·해지 등)
    coverage_summary JSONB,                 -- 보장현황(미가입/부족/충분)
    coverage_detail JSONB,                  -- 가입현황 상세(담보별 금액표, 한국어 키)
    raw JSONB,                              -- 수집 원본(가공 전)
    registered_at TIMESTAMP WITH TIME ZONE, -- 동의서 등록완료 시각(NULL=미등록). RPC registered 플래그로 기록(20260701 마이그레이션)
    crawled_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    CONSTRAINT uq_customer_records UNIQUE (tenant_id, customer_name, birth)
);

CREATE INDEX IF NOT EXISTS idx_customer_records_tenant ON public.customer_records (tenant_id);
CREATE INDEX IF NOT EXISTS idx_customer_records_tenant_name ON public.customer_records (tenant_id, customer_name);

ALTER TABLE public.customer_records ENABLE ROW LEVEL SECURITY;

-- RLS: 본인 회사 데이터만(또는 super_admin 전체) — 기존 execution_logs 정책과 동일 패턴
CREATE POLICY "회사별 고객DB 조회/수정 정책" ON public.customer_records
    FOR ALL USING (
        tenant_id = (SELECT tenant_id FROM public.profiles WHERE id = auth.uid())
        OR
        (SELECT role FROM public.profiles WHERE id = auth.uid()) = 'super_admin'
    );

-- 13. 고객DB 일괄 upsert RPC (에이전트가 anon 키로 device 인증 후 호출, RLS 우회)
--     p_records: customer_records 행 배열(JSONB). (tenant, 고객명, 생년월일) 중복 시 갱신.
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
      tenant_id, device_id, customer_name, birth, phone, age, gender,
      analysis_date, policy_count, monthly_premium, consent_end_date,
      contract_status, coverage_summary, coverage_detail, raw, crawled_at, updated_at
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
        crawled_at = EXCLUDED.crawled_at,
        updated_at = timezone('utc'::text, now());

    v_count := v_count + 1;
  END LOOP;

  RETURN v_count;
END;
$$;

