'use client';

import { useState } from 'react';
import { FORCE_NEW_COPY } from '@/lib/episodeFlow';

export default function RetryPanel({
  disabled,
  onRetry,
}: {
  disabled?: boolean;
  onRetry: (forceNew: boolean) => void;
}) {
  const [open, setOpen] = useState(false);

  return (
    <div className="space-y-2 rounded-lg border border-amber-200 bg-amber-50 p-3">
      <p className="text-sm text-amber-950">
        上次提交可能已经发给厂商，任务号还留着。重试会沿用原来的任务，不会主动新建一笔生成。
      </p>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          disabled={disabled}
          onClick={() => onRetry(false)}
          className="rounded-lg bg-amber-700 px-3 py-1.5 text-sm text-white disabled:opacity-50"
        >
          重试提交
        </button>
        <button
          type="button"
          disabled={disabled}
          onClick={() => setOpen(true)}
          className="rounded-lg border border-amber-400 bg-white px-3 py-1.5 text-left text-sm text-amber-950 disabled:opacity-50"
        >
          {FORCE_NEW_COPY}
        </button>
      </div>
      {open && (
        <div role="dialog" aria-label="确认新建" className="space-y-2 rounded-lg border border-red-200 bg-white p-3">
          <p className="text-sm font-medium text-red-700">{FORCE_NEW_COPY}</p>
          <p className="text-xs text-gray-500">再点一次才会发出新建请求。取消不会提交。</p>
          <div className="flex gap-2">
            <button
              type="button"
              disabled={disabled}
              onClick={() => {
                setOpen(false);
                onRetry(true);
              }}
              className="rounded-lg bg-red-600 px-3 py-1.5 text-sm text-white disabled:opacity-50"
            >
              确认新建
            </button>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm text-gray-600"
            >
              取消
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
