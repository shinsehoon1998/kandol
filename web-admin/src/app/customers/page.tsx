'use client';

import { useState, useEffect, useMemo } from 'react';
import { supabase } from '@/lib/supabase';

export default function CustomersPage() {
  const [profile, setProfile] = useState<any>(null);
  const [customers, setCustomers] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');

  // 상세 모달 (가입현황 등)
  const [active, setActive] = useState<any | null>(null);

  useEffect(() => {
    loadUserAndCustomers();
  }, []);

  async function loadUserAndCustomers() {
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
        await fetchCustomers(prof.tenant_id, prof.role);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }

  async function fetchCustomers(tenantId: string, role: string) {
    let query = supabase
      .from('customer_records')
      .select('*, devices(device_name)')
      .order('crawled_at', { ascending: false });

    if (role !== 'super_admin') {
      query = query.eq('tenant_id', tenantId);
    }

    const { data } = await query;
    if (data) {
      setCustomers(data);
    }
  }

  const filtered = useMemo(() => {
    const q = search.trim();
    if (!q) return customers;
    return customers.filter((c) =>
      (c.customer_name || '').includes(q) ||
      (c.birth || '').includes(q) ||
      (c.phone || '').includes(q)
    );
  }, [customers, search]);

  function fmtWon(v: any) {
    if (v === null || v === undefined || v === '') return '-';
    const n = Number(v);
    if (Number.isNaN(n)) return String(v);
    return n.toLocaleString() + '원';
  }

  if (loading) {
    return <div className="text-slate-400">고객DB 조회 중...</div>;
  }

  return (
    <div className="space-y-8">
      <div className="flex items-end justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-3xl font-black tracking-tight text-white">고객DB</h1>
          <p className="text-sm text-slate-400 mt-1">
            깐돌이 에이전트가 KB 보장분석에서 수집한 담당 고객 데이터 (개인신용정보 — 취급 주의)
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-xs text-slate-500">총 {filtered.length}명</span>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="고객명 / 생년월일 / 전화번호 검색"
            className="px-3 py-2 rounded-lg bg-slate-900 border border-slate-700 text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:border-blue-500 w-56"
          />
        </div>
      </div>

      <div className="rounded-xl bg-slate-900 border border-slate-800 p-6 shadow-sm">
        {filtered.length === 0 ? (
          <div className="text-center py-10 border border-dashed border-slate-800 rounded-lg text-slate-500 text-sm">
            수집된 고객 데이터가 없습니다. 깐돌이 에이전트의 「🗂️ 고객DB 수집」 탭에서 수집을 실행해 주세요.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="border-b border-slate-800 text-xs font-bold text-slate-400 uppercase tracking-wider">
                  <th className="py-3 px-4">고객명</th>
                  <th className="py-3 px-4">생년월일</th>
                  <th className="py-3 px-4">전화번호</th>
                  <th className="py-3 px-4">등록완료</th>
                  <th className="py-3 px-4">나이</th>
                  <th className="py-3 px-4">성별</th>
                  <th className="py-3 px-4">월보험료</th>
                  <th className="py-3 px-4">가입건수</th>
                  <th className="py-3 px-4">동의종료일</th>
                  <th className="py-3 px-4">분석일자</th>
                  <th className="py-3 px-4">수집일시</th>
                  <th className="py-3 px-4 text-center">상세</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800 text-sm">
                {filtered.map((c) => (
                  <tr key={c.id} className="hover:bg-slate-800/30">
                    <td className="py-4 px-4 font-bold text-slate-200">{c.customer_name}</td>
                    <td className="py-4 px-4 text-slate-300">{c.birth || '-'}</td>
                    <td className="py-4 px-4 font-mono text-slate-300">{c.phone || '-'}</td>
                    <td className="py-4 px-4">
                      {c.registered_at ? (
                        <span className="px-2 py-0.5 rounded text-[11px] font-bold bg-green-500/10 text-green-400 border border-green-500/20"
                          title={new Date(c.registered_at).toLocaleString()}>
                          ✅ 등록완료
                        </span>
                      ) : (
                        <span className="text-slate-600 text-xs">미등록</span>
                      )}
                    </td>
                    <td className="py-4 px-4 text-slate-300">{c.age ?? '-'}</td>
                    <td className="py-4 px-4 text-slate-300">{c.gender || '-'}</td>
                    <td className="py-4 px-4 font-mono text-slate-200">{fmtWon(c.monthly_premium)}</td>
                    <td className="py-4 px-4 text-slate-300">{c.policy_count ?? '-'}건</td>
                    <td className="py-4 px-4 text-slate-300">{c.consent_end_date || '-'}</td>
                    <td className="py-4 px-4 text-slate-400 text-xs">{c.analysis_date || '-'}</td>
                    <td className="py-4 px-4 text-slate-500 text-xs whitespace-nowrap">
                      {c.crawled_at ? new Date(c.crawled_at).toLocaleString() : '-'}
                    </td>
                    <td className="py-4 px-4 text-center">
                      <button
                        onClick={() => setActive(c)}
                        className="px-2.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-blue-400 text-xs font-bold rounded-lg transition-colors border border-slate-700 whitespace-nowrap"
                      >
                        🔍 보기
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {active && (
        <CustomerDetailModal customer={active} onClose={() => setActive(null)} fmtWon={fmtWon} />
      )}
    </div>
  );
}

/* 가입현황 상세/계약현황/보장현황을 유연하게 렌더 (배열=표, 객체=키:값) */
function JsonBlock({ title, value }: { title: string; value: any }) {
  if (value === null || value === undefined) return null;

  let body: React.ReactNode;

  if (Array.isArray(value) && value.length > 0 && typeof value[0] === 'object') {
    const cols = Array.from(
      value.reduce((set: Set<string>, row: any) => {
        Object.keys(row || {}).forEach((k) => set.add(k));
        return set;
      }, new Set<string>())
    );
    body = (
      <div className="overflow-x-auto">
        <table className="w-full text-left border-collapse text-sm">
          <thead>
            <tr className="border-b border-slate-700 text-xs font-bold text-slate-400">
              {cols.map((c) => (
                <th key={c} className="py-2 px-3">{c}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800">
            {value.map((row: any, i: number) => (
              <tr key={i} className="hover:bg-slate-800/30">
                {cols.map((c) => (
                  <td key={c} className="py-2 px-3 text-slate-300">
                    {row && row[c] !== undefined && row[c] !== null ? String(row[c]) : '-'}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  } else if (typeof value === 'object' && !Array.isArray(value)) {
    const entries = Object.entries(value);
    if (entries.length === 0) {
      body = <div className="text-slate-500 text-sm">데이터 없음</div>;
    } else {
      body = (
        <div className="grid grid-cols-2 gap-2">
          {entries.map(([k, v]) => (
            <div key={k} className="flex justify-between gap-3 bg-slate-800/40 rounded px-3 py-2">
              <span className="text-slate-400 text-xs">{k}</span>
              <span className="text-slate-200 text-sm font-semibold">
                {v !== null && typeof v === 'object' ? JSON.stringify(v) : String(v)}
              </span>
            </div>
          ))}
        </div>
      );
    }
  } else {
    body = <div className="text-slate-300 text-sm">{String(value)}</div>;
  }

  return (
    <div className="space-y-2">
      <h3 className="text-sm font-bold text-slate-300">{title}</h3>
      {body}
    </div>
  );
}

function CustomerDetailModal({
  customer,
  onClose,
  fmtWon,
}: {
  customer: any;
  onClose: () => void;
  fmtWon: (v: any) => string;
}) {
  const c = customer;
  const hasDetail =
    c.contract_status || c.coverage_summary || c.coverage_detail;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4"
      onClick={onClose}
    >
      <div
        className="relative max-w-4xl w-full max-h-[85vh] overflow-y-auto bg-slate-900 rounded-2xl border border-slate-800 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex justify-between items-center px-6 py-4 border-b border-slate-800 sticky top-0 bg-slate-900 z-10">
          <div>
            <span className="text-lg font-black text-white">{c.customer_name}</span>
            <span className="ml-3 text-sm text-slate-400">
              {c.birth} · {c.gender || '-'} · {c.age ?? '-'}세
              {c.phone ? ` · 📞 ${c.phone}` : ''}
            </span>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white text-xl font-bold">
            ✕
          </button>
        </div>

        <div className="p-6 space-y-6">
          {/* 요약 */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Summary label="월보험료" value={fmtWon(c.monthly_premium)} />
            <Summary label="가입건수" value={(c.policy_count ?? '-') + '건'} />
            <Summary label="동의종료일" value={c.consent_end_date || '-'} />
            <Summary label="분석일자" value={c.analysis_date || '-'} />
          </div>

          <JsonBlock title="📑 계약현황" value={c.contract_status} />
          <JsonBlock title="🛡️ 보장현황 (미가입/부족/충분)" value={c.coverage_summary} />
          <JsonBlock title="💰 가입현황 상세 (담보별 금액)" value={c.coverage_detail} />

          {!hasDetail && (
            <div className="text-center py-6 text-slate-500 text-sm border border-dashed border-slate-800 rounded-lg">
              상세 데이터(계약/보장/가입현황)가 수집되지 않았습니다.
            </div>
          )}

          {/* 원본 */}
          {c.raw && (
            <details className="text-xs">
              <summary className="cursor-pointer text-slate-500 hover:text-slate-300">
                원본 데이터(raw) 보기
              </summary>
              <pre className="mt-2 p-3 bg-slate-950 border border-slate-800 rounded-lg overflow-x-auto text-slate-400">
                {JSON.stringify(c.raw, null, 2)}
              </pre>
            </details>
          )}
        </div>
      </div>
    </div>
  );
}

function Summary({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-slate-800/40 rounded-lg px-4 py-3">
      <div className="text-xs text-slate-400">{label}</div>
      <div className="text-base font-bold text-slate-100 mt-1">{value}</div>
    </div>
  );
}
