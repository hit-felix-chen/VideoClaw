'use client';

import { Suspense } from 'react';
import EpisodeStudio from '@/components/episode/EpisodeStudio';

function Loading() {
  return (
    <div className="flex h-screen items-center justify-center bg-gray-50">
      <p className="text-gray-500">加载中...</p>
    </div>
  );
}

export default function Home() {
  return (
    <Suspense fallback={<Loading />}>
      <EpisodeStudio />
    </Suspense>
  );
}
