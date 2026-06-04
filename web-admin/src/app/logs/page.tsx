'use client';

import { useState, useEffect } from 'react';
import { supabase } from '@/lib/supabase';

export default function LogsPage() {
  const [profile, setProfile] = useState<any>(null);
  const [logs, setLogs] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  
  // Image Viewer Modal state
  const [activeScreenshotUrl, setActiveScreenshotUrl] = useState<string | null>(null);

  useEffect(() => {
    loadUserAndLogs();
  }, []);

  async function loadUserAndLogs() {
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
        await fetchLogs(prof.tenant_id, prof.role);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }

  async function fetchLogs(tenantId: string, role: string) {
    let query = supabase
      .from('execution_logs')
      .select('*, profiles(name), devices(device_name)')
      .order('started_at', { ascending: false });

    if (role !== 'super_admin') {
      query = query.eq('tenant_id', tenantId);
    }

    const { data } = await query;
    if (data) {
      setLogs(data);
    }
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-3xl font-black tracking-tight text-white">전산 감사 로그</h1>
        <p className="text-sm text-slate-400 mt-1">깐돌이 솔루션을 통해 구동된 전체 전산 등록 이력 및 정합성 검증 원격 트랙</p>
      </div>

      <div className="rounded-xl bg-slate-900 border border-slate-800 p-6 shadow-sm">
        {logs.length === 0 ? (
          <div className="text-center py-10 border border-dashed border-slate-800 rounded-lg text-slate-500 text-sm">
            아직 실행된 전산 자동화 작업 로그가 없습니다.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="border-b border-slate-800 text-xs font-bold text-slate-400 uppercase tracking-wider">
                  <th className="py-3 px-4">가동 일시</th>
                  <th className="py-3 px-4">작업 구분</th>
                  <th className="py-3 px-4">파일명</th>
                  <th className="py-3 px-4">가동 기기</th>
                  <th className="py-3 px-4">진행률</th>
                  <th className="py-3 px-4">작업 상태</th>
                  <th className="py-3 px-4">오류/중단 사유</th>
                  <th className="py-3 px-4 text-center">자료</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800 text-sm">
                {logs.map((log) => {
                  const duration = log.ended_at 
                    ? `${Math.round((new Date(log.ended_at).getTime() - new Date(log.started_at).getTime()) / 1000)}초`
                    : '-';

                  return (
                    <tr key={log.id} className="hover:bg-slate-800/30">
                      <td className="py-4 px-4 whitespace-nowrap">
                        <div className="font-bold text-slate-200">{new Date(log.started_at).toLocaleDateString()}</div>
                        <div className="text-xs text-slate-500">{new Date(log.started_at).toLocaleTimeString()} ({duration})</div>
                      </td>
                      <td className="py-4 px-4">
                        <span className={`px-2 py-0.5 rounded text-[11px] font-bold ${
                          log.job_type === 'excel_processing' ? 'bg-blue-500/20 text-blue-400' : 'bg-pink-500/20 text-pink-400'
                        }`}>
                          {log.job_type === 'excel_processing' ? '엑셀전산등록' : 'EDMS일괄업로드'}
                        </span>
                      </td>
                      <td className="py-4 px-4 text-slate-200 font-medium max-w-[180px] truncate" title={log.filename}>
                        {log.filename}
                      </td>
                      <td className="py-4 px-4">
                        <div className="font-semibold text-slate-300">{log.devices?.device_name || '대기'}</div>
                        <div className="text-[10px] text-slate-500">{log.profiles?.name || '사용자 정보 없음'}</div>
                      </td>
                      <td className="py-4 px-4 font-mono text-xs text-slate-400">
                        {log.progress_done} / {log.progress_total}행
                      </td>
                      <td className="py-4 px-4">
                        <span className={`px-2.5 py-1 rounded text-xs font-bold ${
                          log.status === 'success' ? 'bg-green-500/10 text-green-400 border border-green-500/20' :
                          log.status === 'failed' ? 'bg-red-500/10 text-red-400 border border-red-500/20' :
                          log.status === 'stopped' ? 'bg-slate-500/10 text-slate-400 border border-slate-500/20' :
                          'bg-yellow-500/10 text-yellow-400 border border-yellow-500/20'
                        }`}>
                          {log.status === 'success' ? '완료' : 
                           log.status === 'failed' ? '실패' : 
                           log.status === 'stopped' ? '중단됨' : '진행중'}
                        </span>
                      </td>
                      <td className="py-4 px-4 text-xs text-slate-400 max-w-[200px] truncate" title={log.error_reason || ''}>
                        {log.error_reason || '-'}
                      </td>
                      <td className="py-4 px-4">
                        <div className="flex gap-2 justify-center">
                          {log.error_screenshot_url && (
                            <button
                              onClick={() => setActiveScreenshotUrl(log.error_screenshot_url)}
                              className="px-2.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-yellow-400 text-xs font-bold rounded-lg transition-colors border border-slate-700 whitespace-nowrap"
                            >
                              🖼️ 에러캡처
                            </button>
                          )}
                          {log.report_file_url && (
                            <a
                              href={log.report_file_url}
                              target="_blank"
                              rel="noreferrer"
                              className="px-2.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-green-400 text-xs font-bold rounded-lg transition-colors border border-slate-700 whitespace-nowrap"
                            >
                              📥 엑셀결과
                            </a>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Screenshot Viewer Modal */}
      {activeScreenshotUrl && (
        <div 
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4"
          onClick={() => setActiveScreenshotUrl(null)}
        >
          <div 
            className="relative max-w-4xl w-full bg-slate-900 rounded-2xl overflow-hidden border border-slate-800 p-2 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex justify-between items-center px-4 py-2 border-b border-slate-800">
              <span className="text-sm font-bold text-slate-300">자동화 에러 순간의 화면 스크린샷 뷰어</span>
              <button 
                onClick={() => setActiveScreenshotUrl(null)}
                className="text-slate-400 hover:text-white text-lg font-bold"
              >
                ✕
              </button>
            </div>
            <div className="p-2 flex items-center justify-center">
              <img 
                src={activeScreenshotUrl} 
                alt="Error Screen Capture" 
                className="max-h-[70vh] rounded-lg object-contain border border-slate-800"
              />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
