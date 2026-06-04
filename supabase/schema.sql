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
