'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';

export default function RootPage() {
  const router = useRouter();

  useEffect(() => {
    router.push('/dashboard');
  }, []);

  return (
    <div className="flex h-screen w-screen items-center justify-center bg-slate-950 text-slate-400 text-sm">
      이동 중...
    </div>
  );
}
