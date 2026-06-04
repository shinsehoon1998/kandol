'use client';

import { useState, useEffect } from 'react';
import { supabase } from '@/lib/supabase';

export default function DashboardPage() {
  const [profile, setProfile] = useState<any>(null);
  const [activeJobs, setActiveJobs] = useState<any[]>([]);
  const [devices, setDevices] = useState<any[]>([]);
  const [stats, setStats] = useState({ activeDevices: 0, totalJobs: 0, successRate: 100 });

  useEffect(() => {
    loadUserAndData();
  }, []);

  async function loadUserAndData() {
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
        fetchDashboardData(prof.tenant_id, prof.role);
        subscribeToRealtime(prof.tenant_id, prof.role);
      }
    } catch (err) {
      console.error(err);
    }
  }

  async function fetchDashboardData(tenantId: string, role: string) {
    try {
      // 1. Fetch active devices
      let deviceQuery = supabase.from('devices').select('*, tenants(name)');
      if (role !== 'super_admin') {
        deviceQuery = deviceQuery.eq('tenant_id', tenantId);
      }
      const { data: deviceList } = await deviceQuery;
      if (deviceList) {
        setDevices(deviceList);
      }

      // 2. Fetch running or queued logs
      let logQuery = supabase
        .from('execution_logs')
        .select('*, profiles(name), devices(device_name)')
        .in('status', ['running', 'queued'])
        .order('started_at', { ascending: false });

      if (role !== 'super_admin') {
        logQuery = logQuery.eq('tenant_id', tenantId);
      }
      const { data: activeList } = await logQuery;
      if (activeList) {
        setActiveJobs(activeList);
      }

      // 3. Fetch stats
      let allLogsQuery = supabase.from('execution_logs').select('status', { count: 'exact', head: false });
      if (role !== 'super_admin') {
        allLogsQuery = allLogsQuery.eq('tenant_id', tenantId);
      }
      const { count: totalLogs, data: logStats } = await allLogsQuery;

      if (totalLogs !== null && logStats) {
        const successes = logStats.filter(l => l.status === 'success').length;
        const rate = totalLogs > 0 ? Math.round((successes / totalLogs) * 100) : 100;

        // Active devices count (heartbeat within 2 minutes)
        const activeCount = deviceList ? deviceList.filter(d => {
          if (!d.last_heartbeat) return false;
          const diff = Date.now() - new Date(d.last_heartbeat).getTime();
          return diff < 120000; // 2 minutes
        }).length : 0;

        setStats({
          activeDevices: activeCount,
          totalJobs: totalLogs,
          successRate: rate
        });
      }
    } catch (err) {
      console.error(err);
    }
  }

  function subscribeToRealtime(tenantId: string, role: string) {
    // Listen to changes in execution_logs and devices to refresh dashboard in real-time
    const logChannel = supabase
      .channel('schema-db-changes')
      .on(
        'postgres_changes',
        { event: '*', schema: 'public', table: 'execution_logs' },
        () => {
          fetchDashboardData(tenantId, role);
        }
      )
      .on(
        'postgres_changes',
        { event: '*', schema: 'public', table: 'devices' },
        () => {
          fetchDashboardData(tenantId, role);
        }
      )
      .subscribe();

    return () => {
      supabase.removeChannel(logChannel);
    };
  }

  async function handleForceStop(logId: string) {
    if (!confirm('정말로 이 작업을 원격 강제 중단하시겠습니까?')) return;
    try {
      const { error } = await supabase
        .from('execution_logs')
        .update({ status: 'stopped', error_reason: '관리자에 의해 웹콘솔에서 강제 중단됨' })
        .eq('id', logId);
      if (error) throw error;
      
      // Update local state immediately
      setActiveJobs(prev => prev.filter(j => j.id !== logId));
    } catch (err: any) {
      alert(`중단 실패: ${err.message}`);
    }
  }

  function isDeviceOnline(lastHeartbeat: string | null) {
    if (!lastHeartbeat) return false;
    const diff = Date.now() - new Date(lastHeartbeat).getTime();
    return diff < 120000; // 2 minutes
  }

  return (
    <div className="space-y-8">
      {/* Upper Title */}
      <div>
        <h1 className="text-3xl font-black tracking-tight text-white">대시보드</h1>
        <p className="text-sm text-slate-400 mt-1">실시간 에이전트 가동 현황 및 시스템 관제 패널</p>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-1 gap-6 sm:grid-cols-3">
        <div className="rounded-xl bg-slate-900 p-6 border border-slate-800 shadow-sm flex flex-col justify-between">
          <span className="text-sm text-slate-400 font-medium">활동 중인 에이전트</span>
          <span className="text-4xl font-extrabold text-blue-500 mt-2">{stats.activeDevices} 대</span>
        </div>
        <div className="rounded-xl bg-slate-900 p-6 border border-slate-800 shadow-sm flex flex-col justify-between">
          <span className="text-sm text-slate-400 font-medium">누적 처리 작업</span>
          <span className="text-4xl font-extrabold text-green-500 mt-2">{stats.totalJobs} 건</span>
        </div>
        <div className="rounded-xl bg-slate-900 p-6 border border-slate-800 shadow-sm flex flex-col justify-between">
          <span className="text-sm text-slate-400 font-medium">작업 성공률</span>
          <span className="text-4xl font-extrabold text-purple-500 mt-2">{stats.successRate}%</span>
        </div>
      </div>

      {/* Section 1: Running Jobs */}
      <div className="rounded-xl bg-slate-900 border border-slate-800 p-6 shadow-sm">
        <h3 className="text-lg font-bold text-white mb-4">🖥️ 실시간 가동 중인 작업 ({activeJobs.length})</h3>
        
        {activeJobs.length === 0 ? (
          <div className="text-center py-10 border border-dashed border-slate-800 rounded-lg text-slate-500 text-sm">
            현재 실행 중인 전산 자동화 작업이 없습니다.
          </div>
        ) : (
          <div className="space-y-6">
            {activeJobs.map((job) => {
              const pct = job.progress_total > 0 ? Math.round((job.progress_done / job.progress_total) * 100) : 0;
              return (
                <div key={job.id} className="rounded-lg bg-slate-800/50 p-5 border border-slate-700/50 flex flex-col md:flex-row md:items-center justify-between gap-6">
                  <div className="space-y-2 flex-1">
                    <div className="flex items-center gap-3">
                      <span className="px-2 py-0.5 rounded text-xs font-semibold bg-blue-500/20 text-blue-400 uppercase">
                        {job.job_type === 'excel_processing' ? '엑셀전산등록' : 'EDMS일괄업로드'}
                      </span>
                      <span className="text-sm font-bold text-slate-200">{job.filename}</span>
                    </div>
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-xs text-slate-400">
                      <div>담당자: <span className="text-slate-300 font-medium">{job.profiles?.name || '대기'}</span></div>
                      <div>기기명: <span className="text-slate-300 font-medium">{job.devices?.device_name || '대기'}</span></div>
                      <div>시작시간: <span className="text-slate-300 font-medium">{new Date(job.started_at).toLocaleTimeString()}</span></div>
                      <div>현재단계: <span className="text-slate-300 font-bold uppercase text-yellow-500">{job.current_stage || '-'}</span></div>
                    </div>
                    
                    {/* Progress Fill */}
                    <div className="space-y-1 pt-2">
                      <div className="flex justify-between text-xs font-medium text-slate-400">
                        <span>진행률: {job.progress_done} / {job.progress_total}행</span>
                        <span>{pct}%</span>
                      </div>
                      <div className="w-full bg-slate-700 h-2 rounded-full overflow-hidden">
                        <div className="bg-blue-500 h-full rounded-full transition-all duration-300" style={{ width: `${pct}%` }}></div>
                      </div>
                    </div>
                    
                    {/* Last message logs */}
                    {job.last_message && (
                      <div className="text-xs text-slate-400 font-mono bg-slate-900/50 p-2 rounded border border-slate-800">
                        Log: <span className="text-slate-300">{job.last_message}</span>
                      </div>
                    )}
                  </div>
                  
                  <div>
                    <button
                      onClick={() => handleForceStop(job.id)}
                      className="px-4 py-2.5 bg-red-600/90 hover:bg-red-500 text-white text-xs font-bold rounded-lg transition-colors shadow-sm"
                    >
                      🛑 원격 강제 중단
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Section 2: Device lists */}
      <div className="rounded-xl bg-slate-900 border border-slate-800 p-6 shadow-sm">
        <h3 className="text-lg font-bold text-white mb-4">💻 깐돌이 에이전트 접속 기기 ({devices.length})</h3>
        
        {devices.length === 0 ? (
          <div className="text-center py-10 border border-dashed border-slate-800 rounded-lg text-slate-500 text-sm">
            연동 요청된 에이전트 기기가 없습니다.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="border-b border-slate-800 text-xs font-bold text-slate-400 uppercase tracking-wider">
                  <th className="py-3 px-4">기기명</th>
                  <th className="py-3 px-4">상태</th>
                  <th className="py-3 px-4">승인 여부</th>
                  <th className="py-3 px-4">하드웨어 식별자(HWID)</th>
                  <th className="py-3 px-4">최근 동기화</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800 text-sm">
                {devices.map((device) => {
                  const online = isDeviceOnline(device.last_heartbeat);
                  return (
                    <tr key={device.id} className="hover:bg-slate-800/30">
                      <td className="py-4 px-4 font-bold text-slate-200">{device.device_name}</td>
                      <td className="py-4 px-4">
                        <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-bold ${online ? 'bg-green-500/10 text-green-400' : 'bg-slate-500/10 text-slate-400'}`}>
                          <span className={`h-1.5 w-1.5 rounded-full ${online ? 'bg-green-500 animate-pulse' : 'bg-slate-500'}`}></span>
                          {online ? 'Online' : 'Offline'}
                        </span>
                      </td>
                      <td className="py-4 px-4">
                        <span className={`px-2 py-0.5 rounded text-xs font-bold ${
                          device.status === 'approved' ? 'bg-green-500/20 text-green-400' :
                          device.status === 'blocked' ? 'bg-red-500/20 text-red-400' :
                          'bg-yellow-500/20 text-yellow-400'
                        }`}>
                          {device.status === 'approved' ? '승인됨' : device.status === 'blocked' ? '차단됨' : '대기중'}
                        </span>
                      </td>
                      <td className="py-4 px-4 font-mono text-xs text-slate-400 truncate max-w-[200px]" title={device.hwid}>
                        {device.hwid}
                      </td>
                      <td className="py-4 px-4 text-xs text-slate-400">
                        {device.last_heartbeat ? new Date(device.last_heartbeat).toLocaleString() : '이력 없음'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
