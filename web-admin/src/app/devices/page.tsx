'use client';

import { useState, useEffect } from 'react';
import { supabase } from '@/lib/supabase';

export default function DevicesPage() {
  const [profile, setProfile] = useState<any>(null);
  const [devices, setDevices] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    loadUserAndDevices();
  }, []);

  async function loadUserAndDevices() {
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
        await fetchDevices(prof.tenant_id, prof.role);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }

  async function fetchDevices(tenantId: string, role: string) {
    let query = supabase.from('devices').select('*, tenants(name)').order('created_at', { ascending: false });
    if (role !== 'super_admin') {
      query = query.eq('tenant_id', tenantId);
    }
    const { data } = await query;
    if (data) {
      setDevices(data);
    }
  }

  async function handleStatusChange(deviceId: string, status: 'approved' | 'blocked' | 'pending') {
    try {
      const { error } = await supabase
        .from('devices')
        .update({ status })
        .eq('id', deviceId);

      if (error) throw error;
      
      // Update local state
      setDevices(prev => prev.map(d => d.id === deviceId ? { ...d, status } : d));
    } catch (err: any) {
      alert(`상태 수정 실패: ${err.message}`);
    }
  }

  async function handleDeleteDevice(deviceId: string) {
    if (!confirm('정말로 이 기기를 기기 목록에서 삭제하시겠습니까? 다시 연결하려면 재로그인 승인이 필요합니다.')) return;
    try {
      const { error } = await supabase
        .from('devices')
        .delete()
        .eq('id', deviceId);

      if (error) throw error;
      setDevices(prev => prev.filter(d => d.id !== deviceId));
    } catch (err: any) {
      alert(`삭제 실패: ${err.message}`);
    }
  }

  const isEditable = profile?.role === 'super_admin' || profile?.role === 'tenant_admin';

  if (loading) {
    return <div className="text-slate-400">에이전트 목록 조회 중...</div>;
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-3xl font-black tracking-tight text-white">기기 라이선스 승인</h1>
        <p className="text-sm text-slate-400 mt-1">로컬 깐돌이 에이전트 구동 허가 기기(HWID) 등록 및 보안 통제</p>
      </div>

      <div className="rounded-xl bg-slate-900 border border-slate-800 p-6 shadow-sm">
        {devices.length === 0 ? (
          <div className="text-center py-10 border border-dashed border-slate-800 rounded-lg text-slate-500 text-sm">
            등록 신청을 보낸 에이전트 기기가 없습니다.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="border-b border-slate-800 text-xs font-bold text-slate-400 uppercase tracking-wider">
                  <th className="py-3 px-4">기기 컴퓨터 이름</th>
                  {profile.role === 'super_admin' && <th className="py-3 px-4">회사명</th>}
                  <th className="py-3 px-4">상태</th>
                  <th className="py-3 px-4">기기 고유키(HWID)</th>
                  <th className="py-3 px-4">신청 일자</th>
                  {isEditable && <th className="py-3 px-4 text-center">액션</th>}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800 text-sm">
                {devices.map((device) => (
                  <tr key={device.id} className="hover:bg-slate-800/30">
                    <td className="py-4 px-4 font-bold text-slate-200">{device.device_name}</td>
                    {profile.role === 'super_admin' && (
                      <td className="py-4 px-4 text-slate-300">{device.tenants?.name}</td>
                    )}
                    <td className="py-4 px-4">
                      <span className={`px-2.5 py-1 rounded text-xs font-bold ${
                        device.status === 'approved' ? 'bg-green-500/10 text-green-400 border border-green-500/20' :
                        device.status === 'blocked' ? 'bg-red-500/10 text-red-400 border border-red-500/20' :
                        'bg-yellow-500/10 text-yellow-400 border border-yellow-500/20'
                      }`}>
                        {device.status === 'approved' ? '승인 완료' : device.status === 'blocked' ? '차단됨' : '승인 대기'}
                      </span>
                    </td>
                    <td className="py-4 px-4 font-mono text-xs text-slate-400 max-w-[220px] truncate" title={device.hwid}>
                      {device.hwid}
                    </td>
                    <td className="py-4 px-4 text-xs text-slate-400">
                      {new Date(device.created_at).toLocaleString()}
                    </td>
                    {isEditable && (
                      <td className="py-4 px-4">
                        <div className="flex gap-2 justify-center">
                          {device.status !== 'approved' && (
                            <button
                              onClick={() => handleStatusChange(device.id, 'approved')}
                              className="px-2.5 py-1.5 bg-green-600 hover:bg-green-500 text-white text-xs font-bold rounded-lg transition-colors"
                            >
                              승인
                            </button>
                          )}
                          {device.status !== 'blocked' && (
                            <button
                              onClick={() => handleStatusChange(device.id, 'blocked')}
                              className="px-2.5 py-1.5 bg-yellow-600 hover:bg-yellow-500 text-white text-xs font-bold rounded-lg transition-colors"
                            >
                              차단
                            </button>
                          )}
                          <button
                            onClick={() => handleDeleteDevice(device.id)}
                            className="px-2.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-red-400 text-xs font-bold rounded-lg transition-colors border border-slate-700"
                          >
                            삭제
                          </button>
                        </div>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
