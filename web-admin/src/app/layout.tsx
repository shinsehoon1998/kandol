'use client';

import { useState, useEffect } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import { supabase } from '@/lib/supabase';
import Link from 'next/link';
import './globals.css';

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const [profile, setProfile] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    checkUser();
    
    // Auth state listener
    const { data: { subscription } } = supabase.auth.onAuthStateChange(async (event, session) => {
      if (session) {
        fetchProfile(session.user.id);
      } else {
        setProfile(null);
        if (pathname !== '/login') {
          router.push('/login');
        }
      }
    });

    return () => {
      subscription.unsubscribe();
    };
  }, [pathname]);

  async function checkUser() {
    try {
      const { data: { session } } = await supabase.auth.getSession();
      if (session) {
        await fetchProfile(session.user.id);
      } else {
        setLoading(false);
        if (pathname !== '/login') {
          router.push('/login');
        }
      }
    } catch (err) {
      console.error(err);
      setLoading(false);
    }
  }

  async function fetchProfile(userId: string) {
    try {
      const { data, error } = await supabase
        .from('profiles')
        .select('*, tenants(name)')
        .eq('id', userId)
        .single();
      if (!error && data) {
        setProfile(data);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }

  async function handleLogout() {
    await supabase.auth.signOut();
    router.push('/login');
  }

  const isLoginPage = pathname === '/login';

  return (
    <html lang="ko" className="h-full bg-slate-950 text-slate-100">
      <body className="min-h-full flex flex-col antialiased">
        {loading ? (
          <div className="flex h-screen w-screen items-center justify-center bg-slate-950">
            <div className="text-center">
              <div className="h-10 w-10 animate-spin rounded-full border-4 border-blue-500 border-t-transparent mx-auto"></div>
              <p className="mt-4 text-sm text-slate-400">깐돌이 서버 연결 중...</p>
            </div>
          </div>
        ) : isLoginPage ? (
          children
        ) : (
          <div className="flex min-h-screen">
            {/* Sidebar */}
            <aside className="w-64 bg-slate-900 border-r border-slate-800 flex flex-col">
              <div className="p-6 border-b border-slate-800">
                <Link href="/dashboard" className="flex items-center gap-2 text-lg font-black text-white hover:text-blue-400 transition-colors">
                  <img src="/mascot.png" alt="깐돌이 마스코트" className="h-8 w-8 rounded-lg object-contain bg-slate-800 border border-slate-700" />
                  <span>깐돌이 콘솔</span>
                </Link>
                {profile?.tenants?.name && (
                  <span className="text-xs text-slate-400 mt-2 block pl-1">
                    소속: {profile.tenants.name}
                  </span>
                )}
              </div>
              
              <nav className="flex-1 p-4 space-y-1">
                <Link 
                  href="/dashboard" 
                  className={`flex items-center gap-3 px-4 py-3 rounded-lg text-sm font-semibold transition-colors ${pathname === '/dashboard' ? 'bg-blue-600 text-white' : 'text-slate-400 hover:bg-slate-800 hover:text-white'}`}
                >
                  📊 대시보드 (관제)
                </Link>
                <Link 
                  href="/settings" 
                  className={`flex items-center gap-3 px-4 py-3 rounded-lg text-sm font-semibold transition-colors ${pathname === '/settings' ? 'bg-blue-600 text-white' : 'text-slate-400 hover:bg-slate-800 hover:text-white'}`}
                >
                  🎯 매크로 좌표/딜레이
                </Link>
                <Link 
                  href="/devices" 
                  className={`flex items-center gap-3 px-4 py-3 rounded-lg text-sm font-semibold transition-colors ${pathname === '/devices' ? 'bg-blue-600 text-white' : 'text-slate-400 hover:bg-slate-800 hover:text-white'}`}
                >
                  💻 라이선스 기기 승인
                </Link>
                <Link
                  href="/logs"
                  className={`flex items-center gap-3 px-4 py-3 rounded-lg text-sm font-semibold transition-colors ${pathname === '/logs' ? 'bg-blue-600 text-white' : 'text-slate-400 hover:bg-slate-800 hover:text-white'}`}
                >
                  📜 전산 감사 로그
                </Link>
                <Link
                  href="/customers"
                  className={`flex items-center gap-3 px-4 py-3 rounded-lg text-sm font-semibold transition-colors ${pathname === '/customers' ? 'bg-blue-600 text-white' : 'text-slate-400 hover:bg-slate-800 hover:text-white'}`}
                >
                  👥 고객DB
                </Link>
                <a 
                  href="https://github.com/shinsehoon1998/kandol/releases/download/v1.9.0/Kkandori_2026-07-22.zip" 
                  target="_blank"
                  rel="noreferrer"
                  className="flex items-center gap-3 px-4 py-3 rounded-lg text-sm font-semibold text-slate-400 hover:bg-slate-800 hover:text-white transition-colors"
                >
                  📥 에이전트 다운로드
                </a>
              </nav>

              <div className="p-4 border-t border-slate-800 bg-slate-900/50">
                <div className="text-sm font-medium text-slate-300 truncate">{profile?.name}</div>
                <div className="text-xs text-slate-500 capitalize">{profile?.role}</div>
                <button
                  onClick={handleLogout}
                  className="mt-3 w-full py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-semibold rounded-lg transition-colors border border-slate-700"
                >
                  로그아웃
                </button>
              </div>
            </aside>

            {/* Main Content Area */}
            <main className="flex-1 bg-slate-950 p-8 overflow-y-auto">
              {children}
            </main>
          </div>
        )}
      </body>
    </html>
  );
}
