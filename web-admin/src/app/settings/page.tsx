'use client';

import { useState, useEffect } from 'react';
import { supabase } from '@/lib/supabase';

export default function SettingsPage() {
  const [profile, setProfile] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');

  // Config States
  const [offsets, setOffsets] = useState<Record<string, number>>({});
  const [delays, setDelays] = useState<Record<string, number>>({});

  const offsetLabels: Record<string, string> = {
    image_add_x: '동의서 이미지 추가 버튼 X',
    image_add_y: '동의서 이미지 추가 버튼 Y',
    select_all_x: '전체 선택 체크박스 X',
    select_all_y: '전체 선택 체크박스 Y',
    send_x: '전송 버튼 X',
    send_y: '전송 버튼 Y',
    tab_local_pdf_x: '로컬 PDF 탭 X',
    tab_local_pdf_y: '로컬 PDF 탭 Y',
    folder_docs_x: '문서 폴더 확장 X',
    folder_docs_y: '문서 폴더 확장 Y',
    search_input_x: '검색창 클릭 영역 X',
    search_input_y: '검색창 클릭 영역 Y',
    search_btn_x: '검색 실행 버튼 X',
    search_btn_y: '검색 실행 버튼 Y',
    confirm_btn_x: '검색된 항목 확인 클릭 X',
    confirm_btn_y: '검색된 항목 확인 클릭 Y',
    pop_send_btn_x: '전송확인 팝업 내 전송 X',
    pop_send_btn_y: '전송확인 팝업 내 전송 Y',
    fallback_pop_send_x: '전송확인 팝업 폴백 전송 X',
    fallback_pop_send_y: '전송확인 팝업 폴백 전송 Y',
  };

  const delayLabels: Record<string, string> = {
    dialog_open_wait: '동의서 팝업창 오픈 대기 (초)',
    tab_click_wait: '로컬 PDF 탭 클릭 후 대기 (초)',
    folder_expand_wait: '폴더 목록 확장 대기 (초)',
    search_wait: '검색 실행 후 로드 대기 (초)',
    image_load_wait: '이미지 로드 대기 (초)',
    select_all_wait: '전체 선택 클릭 후 대기 (초)',
    send_confirm_wait: '전송 클릭 후 팝업 대기 (초)',
    success_alert_wait: '완료 메시지창 타임아웃 (초)',
  };

  useEffect(() => {
    loadUserAndConfig();
  }, []);

  async function loadUserAndConfig() {
    try {
      const { data: { session } } = await supabase.auth.getSession();
      if (!session) return;

      const { data: prof } = await supabase
        .from('profiles')
        .select('*')
        .eq('id', session.user.id)
        .single();

      if (prof) {
        setProfile(prof);
        await fetchConfig(prof.tenant_id);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }

  async function fetchConfig(tenantId: string) {
    const { data } = await supabase
      .from('macro_configs')
      .select('*')
      .eq('tenant_id', tenantId)
      .single();

    if (data) {
      setOffsets(data.offsets || {});
      setDelays(data.delays || {});
    }
  }

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    if (!profile) return;
    
    // Check role
    if (profile.role !== 'super_admin' && profile.role !== 'tenant_admin') {
      alert('설정 저장 권한이 없습니다. 회사 관리자에게 요청하세요.');
      return;
    }

    setSaving(true);
    setMessage('');

    try {
      const { error } = await supabase
        .from('macro_configs')
        .upsert({
          tenant_id: profile.tenant_id,
          offsets,
          delays,
          ratios: { pop_send_x: 0.693989, pop_send_y: 0.873817 }, // ratio standard config
          updated_at: new Date().toISOString(),
          updated_by: profile.id
        });

      if (error) throw error;
      setMessage('설정이 서버에 성공적으로 저장되었습니다!');
    } catch (err: any) {
      setMessage(`에러: ${err.message}`);
    } finally {
      setSaving(false);
    }
  }

  function handleReset() {
    if (!confirm('설정값을 기본값으로 초기화하시겠습니까? (로컬에만 반영되며 저장을 눌러야 적용됩니다)')) return;
    
    setOffsets({
      image_add_x: 1117, image_add_y: 252, select_all_x: 373, select_all_y: 272,
      send_x: 1185, send_y: 252, tab_local_pdf_x: 666, tab_local_pdf_y: 38,
      folder_docs_x: 578, folder_docs_y: 92, search_input_x: 883, search_input_y: 345,
      search_btn_x: 964, search_btn_y: 345, confirm_btn_x: 899, confirm_btn_y: 617,
      pop_send_btn_x: 254, pop_send_btn_y: 277, fallback_pop_send_x: 923, fallback_pop_send_y: 692
    });

    setDelays({
      dialog_open_wait: 1.0, tab_click_wait: 1.0, folder_expand_wait: 1.5,
      search_wait: 2.0, image_load_wait: 4.0, select_all_wait: 1.0,
      send_confirm_wait: 0.5, success_alert_wait: 15.0
    });
  }

  const isEditable = profile?.role === 'super_admin' || profile?.role === 'tenant_admin';

  if (loading) {
    return <div className="text-slate-400">설정 불러오는 중...</div>;
  }

  return (
    <div className="space-y-8 max-w-5xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-black tracking-tight text-white">매크로 설정 튜닝</h1>
          <p className="text-sm text-slate-400 mt-1">로컬 깐돌이 에이전트가 참조할 마우스 클릭 좌표와 대기시간 설정</p>
        </div>
        
        {isEditable && (
          <button
            type="button"
            onClick={handleReset}
            className="px-4 py-2 border border-slate-700 hover:bg-slate-800 text-slate-300 text-xs font-semibold rounded-lg transition-colors"
          >
            기본값으로 세팅
          </button>
        )}
      </div>

      {message && (
        <div className={`rounded-lg p-4 text-sm ${message.startsWith('에러') ? 'bg-red-500/20 text-red-400' : 'bg-green-500/20 text-green-400'}`}>
          {message}
        </div>
      )}

      <form onSubmit={handleSave} className="space-y-8">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
          
          {/* Coordinates Card */}
          <div className="rounded-xl bg-slate-900 border border-slate-800 p-6 shadow-sm space-y-4">
            <h3 className="text-lg font-bold text-white border-b border-slate-800 pb-2">🎯 마우스 클릭 좌표 (Offsets)</h3>
            <div className="space-y-3 max-h-[500px] overflow-y-auto pr-2">
              {Object.keys(offsetLabels).map((key) => (
                <div key={key} className="flex items-center justify-between gap-4">
                  <label className="text-xs text-slate-400 font-medium">{offsetLabels[key]} <span className="text-[10px] text-slate-600 block font-mono">{key}</span></label>
                  <input
                    type="number"
                    required
                    disabled={!isEditable}
                    value={offsets[key] ?? 0}
                    onChange={(e) => setOffsets({ ...offsets, [key]: parseInt(e.target.value) || 0 })}
                    className="w-24 rounded-lg bg-slate-800 p-2 text-right text-sm focus:outline-none focus:ring-1 focus:ring-blue-500 border border-slate-700 text-white font-mono"
                  />
                </div>
              ))}
            </div>
          </div>

          {/* Delays Card */}
          <div className="rounded-xl bg-slate-900 border border-slate-800 p-6 shadow-sm space-y-4">
            <h3 className="text-lg font-bold text-white border-b border-slate-800 pb-2">⏳ 실행 지연 시간 (Delays)</h3>
            <div className="space-y-3">
              {Object.keys(delayLabels).map((key) => (
                <div key={key} className="flex items-center justify-between gap-4">
                  <label className="text-xs text-slate-400 font-medium">{delayLabels[key]} <span className="text-[10px] text-slate-600 block font-mono">{key}</span></label>
                  <input
                    type="number"
                    step="0.1"
                    required
                    disabled={!isEditable}
                    value={delays[key] ?? 0}
                    onChange={(e) => setDelays({ ...delays, [key]: parseFloat(e.target.value) || 0 })}
                    className="w-24 rounded-lg bg-slate-800 p-2 text-right text-sm focus:outline-none focus:ring-1 focus:ring-blue-500 border border-slate-700 text-white font-mono"
                  />
                </div>
              ))}
            </div>
          </div>

        </div>

        {isEditable ? (
          <div className="flex justify-end pt-4 border-t border-slate-900">
            <button
              type="submit"
              disabled={saving}
              className="px-6 py-3 bg-blue-600 hover:bg-blue-500 text-white text-sm font-semibold rounded-lg shadow transition-colors disabled:opacity-50"
            >
              {saving ? '저장 중...' : '⬆️ 서버에 설정 저장 및 배포'}
            </button>
          </div>
        ) : (
          <div className="text-center text-xs text-slate-500">
            * 일반 사용자 계정은 설정을 수정할 수 없습니다. 어드민에게 문의하세요.
          </div>
        )}
      </form>
    </div>
  );
}
