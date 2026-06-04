'use client';

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { supabase } from '@/lib/supabase';

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [name, setName] = useState('');
  const [isSignUp, setIsSignUp] = useState(false);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState('');

  // Tenants lists for signup
  const [tenants, setTenants] = useState<{ id: string; name: string }[]>([]);
  const [selectedTenant, setSelectedTenant] = useState('');
  const [newTenantName, setNewTenantName] = useState('');
  const [createOption, setCreateOption] = useState('join'); // 'join' or 'create'

  useEffect(() => {
    fetchTenants();
  }, []);

  async function fetchTenants() {
    try {
      const { data, error } = await supabase.from('tenants').select('id, name');
      if (!error && data) {
        setTenants(data);
        if (data.length > 0) {
          setSelectedTenant(data[0].id);
        }
      }
    } catch (err) {
      console.error(err);
    }
  }

  async function handleAuth(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setMessage('');

    try {
      if (isSignUp) {
        // 1. Sign up user via Auth
        const { data: authData, error: signUpError } = await supabase.auth.signUp({
          email,
          password,
          options: {
            data: { name },
          },
        });

        if (signUpError) throw signUpError;
        if (!authData.user) throw new Error('회원가입에 실패했습니다.');

        let tenantId = selectedTenant;

        // 2. Create tenant if 'create' option is selected
        if (createOption === 'create') {
          if (!newTenantName.trim()) {
            throw new Error('새 회사 이름을 입력해 주세요.');
          }
          const { data: tenantData, error: tenantError } = await supabase
            .from('tenants')
            .insert({ name: newTenantName.trim() })
            .select()
            .single();

          if (tenantError) throw tenantError;
          tenantId = tenantData.id;

          // Also insert a default macro config for the new tenant
          await supabase.from('macro_configs').insert({
            tenant_id: tenantId,
            offsets: {
              image_add_x: 1117, image_add_y: 252, select_all_x: 373, select_all_y: 272,
              send_x: 1185, send_y: 252, tab_local_pdf_x: 666, tab_local_pdf_y: 38,
              folder_docs_x: 578, folder_docs_y: 92, search_input_x: 883, search_input_y: 345,
              search_btn_x: 964, search_btn_y: 345, confirm_btn_x: 899, confirm_btn_y: 617,
              pop_send_btn_x: 254, pop_send_btn_y: 277, fallback_pop_send_x: 923, fallback_pop_send_y: 692
            },
            delays: {
              dialog_open_wait: 1.0, tab_click_wait: 1.0, folder_expand_wait: 1.5,
              search_wait: 2.0, image_load_wait: 4.0, select_all_wait: 1.0,
              send_confirm_wait: 0.5, success_alert_wait: 15.0
            },
            ratios: {
              pop_send_x: 0.693989, pop_send_y: 0.873817
            }
          });
        }

        // 3. Link profile to tenant
        // A trigger on auth.users automatically inserts the profile.
        // We will wait a brief moment and update the profile row with the selected/created tenant_id.
        await new Promise((r) => setTimeout(r, 1500)); // wait for trigger
        const { error: profileError } = await supabase
          .from('profiles')
          .update({ 
            tenant_id: tenantId,
            role: createOption === 'create' ? 'tenant_admin' : 'user' // Creator is tenant_admin
          })
          .eq('id', authData.user.id);

        if (profileError) throw profileError;

        setMessage('회원가입이 완료되었습니다! 이메일 인증이 필요할 수 있습니다.');
        setIsSignUp(false);
      } else {
        // Sign in
        const { error: signInError } = await supabase.auth.signInWithPassword({
          email,
          password,
        });

        if (signInError) throw signInError;
        router.push('/dashboard');
      }
    } catch (err: any) {
      setMessage(`오류: ${err.message || '알 수 없는 에러가 발생했습니다.'}`);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-900 px-4 text-white">
      <div className="w-full max-w-md rounded-2xl bg-slate-800 p-8 shadow-xl border border-slate-700">
        <h2 className="text-center text-3xl font-extrabold text-blue-500">깐돌이 어드민 콘솔</h2>
        <p className="mt-2 text-center text-sm text-slate-400">
          {isSignUp ? '기업 회원가입 및 테넌트 등록' : 'B2B 전산등록 자동화 관리 포털'}
        </p>

        <form className="mt-8 space-y-6" onSubmit={handleAuth}>
          {message && (
            <div className={`rounded-md p-3 text-sm ${message.startsWith('오류') ? 'bg-red-500/20 text-red-400' : 'bg-green-500/20 text-green-400'}`}>
              {message}
            </div>
          )}

          <div className="space-y-4 rounded-md shadow-sm">
            {isSignUp && (
              <div>
                <label className="text-xs font-semibold text-slate-400">이름</label>
                <input
                  type="text"
                  required
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  className="mt-1 w-full rounded-lg bg-slate-700 p-3 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 border border-slate-600"
                  placeholder="홍길동"
                />
              </div>
            )}

            <div>
              <label className="text-xs font-semibold text-slate-400">이메일 주소</label>
              <input
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="mt-1 w-full rounded-lg bg-slate-700 p-3 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 border border-slate-600"
                placeholder="email@company.com"
              />
            </div>

            <div>
              <label className="text-xs font-semibold text-slate-400">비밀번호</label>
              <input
                type="password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="mt-1 w-full rounded-lg bg-slate-700 p-3 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 border border-slate-600"
                placeholder="••••••••"
              />
            </div>

            {isSignUp && (
              <div className="space-y-4 pt-2 border-t border-slate-700">
                <div className="flex gap-4">
                  <label className="flex items-center text-sm gap-2 cursor-pointer">
                    <input
                      type="radio"
                      name="tenantOption"
                      checked={createOption === 'join'}
                      onChange={() => setCreateOption('join')}
                    />
                    기존 회사 합류
                  </label>
                  <label className="flex items-center text-sm gap-2 cursor-pointer">
                    <input
                      type="radio"
                      name="tenantOption"
                      checked={createOption === 'create'}
                      onChange={() => setCreateOption('create')}
                    />
                    새 회사 생성
                  </label>
                </div>

                {createOption === 'join' ? (
                  <div>
                    <label className="text-xs font-semibold text-slate-400">합류할 회사 선택</label>
                    {tenants.length > 0 ? (
                      <select
                        value={selectedTenant}
                        onChange={(e) => setSelectedTenant(e.target.value)}
                        className="mt-1 w-full rounded-lg bg-slate-700 p-3 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 border border-slate-600 text-white"
                      >
                        {tenants.map((t) => (
                          <option key={t.id} value={t.id}>
                            {t.name}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <div className="text-sm text-yellow-500 mt-1">
                        등록된 회사가 없습니다. 새 회사 생성을 선택해 주세요.
                      </div>
                    )}
                  </div>
                ) : (
                  <div>
                    <label className="text-xs font-semibold text-slate-400">새 회사 이름</label>
                    <input
                      type="text"
                      required
                      value={newTenantName}
                      onChange={(e) => setNewTenantName(e.target.value)}
                      className="mt-1 w-full rounded-lg bg-slate-700 p-3 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 border border-slate-600"
                      placeholder="깐돌이 대리점"
                    />
                  </div>
                )}
              </div>
            )}
          </div>

          <div>
            <button
              type="submit"
              disabled={loading}
              className="w-full rounded-lg bg-blue-600 p-3 text-sm font-semibold text-white hover:bg-blue-500 disabled:opacity-50 transition-colors"
            >
              {loading ? '처리 중...' : isSignUp ? '회원가입 완료' : '🔐 로그인'}
            </button>
          </div>
        </form>

        <div className="mt-6 text-center">
          <button
            onClick={() => setIsSignUp(!isSignUp)}
            className="text-sm text-blue-400 hover:underline"
          >
            {isSignUp ? '이미 계정이 있으신가요? 로그인하기' : '신규 계정 만들기 (B2B 등록)'}
          </button>
        </div>
      </div>
    </div>
  );
}
