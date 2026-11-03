'use client';

import { useState } from 'react';
import RetryPanel from '@/components/episode/RetryPanel';
import type { GraphNode, NodeActionResult } from '@/lib/episodeApi';
import {
  OFFLINE_BADGE,
  canConfirmVideo,
  formatQuote,
  isVideoNode,
  needsVideoRetry,
  nodeLabel,
  offlineUncalled,
  reservationCopy,
  statusLabel,
} from '@/lib/episodeFlow';

function resultText(node: GraphNode): string {
  const result = node.metadata?.result;
  if (!result || typeof result !== 'object') return '';
  const record = result as Record<string, unknown>;
  if (typeof record.text === 'string' && record.text.trim()) return record.text;
  if (typeof record.result_id === 'string' && record.result_id.trim()) return record.result_id;
  return '';
}

export default function NodeCard({
  node,
  executable,
  busy,
  draft,
  onDraft,
  onSavePrompt,
  onRun,
  onConfirm,
  onDecline,
  onRaise,
  onDeclineBudget,
  onRetry,
  action,
  ledger,
}: {
  node: GraphNode;
  executable: boolean;
  busy: boolean;
  draft: string;
  onDraft: (text: string) => void;
  onSavePrompt: () => void;
  onRun: () => void;
  onConfirm: () => void;
  onDecline: () => void;
  onRaise: (cap: number) => void;
  onDeclineBudget: () => void;
  onRetry: (forceNew: boolean) => void;
  action: NodeActionResult | null;
  ledger?: Array<Record<string, unknown>>;
}) {
  const [cap, setCap] = useState('');
  const quote = node.quote || (action?.node_id === node.id ? action.quote : null);
  const showRetry = needsVideoRetry(node);
  const showConfirm = canConfirmVideo({ ...node, quote: quote || node.quote });
  const showBudget = Boolean(node.budget_hold && node.entry_id);
  const savedText = typeof node.metadata?.text === 'string' ? node.metadata.text : '';
  const output = resultText(node);
  const stateNote = node.node_state && node.node_state !== node.status
    ? `账本状态 ${statusLabel(node.node_state)}`
    : '';
  const offline = offlineUncalled(node);
  const reservation = reservationCopy(node, ledger);

  return (
    <article className="space-y-3 rounded-xl border border-gray-200 bg-white p-4">
      <header className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-gray-900">{nodeLabel(node)}</h3>
        <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-500">{node.kind}</span>
        {offline ? (
          <span className="rounded bg-gray-200 px-1.5 py-0.5 text-[11px] text-gray-800">{OFFLINE_BADGE}</span>
        ) : (
          <span className="rounded bg-blue-50 px-1.5 py-0.5 text-[11px] text-blue-700">{statusLabel(node.status)}</span>
        )}
        {stateNote && <span className="text-[11px] text-gray-500">{stateNote}</span>}
      </header>

      <label className="block text-xs text-gray-500">
        提示词
        <textarea
          value={draft}
          onChange={event => onDraft(event.target.value)}
          rows={3}
          className="mt-1 w-full rounded-lg border border-gray-200 px-2 py-1.5 text-sm text-gray-800 outline-none focus:border-blue-300"
        />
      </label>
      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={busy || draft === savedText}
          onClick={onSavePrompt}
          className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm text-gray-700 disabled:opacity-50"
        >
          保存提示词
        </button>
        {draft === savedText && <span className="text-xs text-gray-400">已与服务器一致</span>}
      </div>

      {output && (
        <p className="whitespace-pre-wrap rounded-lg bg-gray-50 px-3 py-2 text-sm text-gray-700">{output}</p>
      )}
      {reservation && <p className="text-sm text-gray-700">{reservation}</p>}
      {isVideoNode(node) && action?.status === 'done' && action.provider_called === true && (
        <p className="text-sm text-gray-700">厂商已返回结果。</p>
      )}

      {executable && (
        <button
          type="button"
          disabled={busy}
          onClick={onRun}
          className="rounded-lg bg-blue-600 px-3 py-1.5 text-sm text-white disabled:opacity-50"
        >
          运行
        </button>
      )}

      {isVideoNode(node) && (node.status === 'awaiting_user' || node.node_state === 'ready') && (
        <div className="space-y-2 rounded-lg border border-blue-100 bg-blue-50/60 p-3">
          <p className="text-sm text-blue-950">{formatQuote(quote)}</p>
          <p className="text-xs text-blue-800">确认后才会向厂商提交。页面不会自动提交。</p>
          {action?.node_id === node.id && action.message && (
            <p className="text-sm text-gray-700">{action.message}</p>
          )}
          {showConfirm && (
            <div className="flex gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={onConfirm}
                className="rounded-lg bg-blue-700 px-3 py-1.5 text-sm text-white disabled:opacity-50"
              >
                确认提交
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={onDecline}
                className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-700 disabled:opacity-50"
              >
                放弃本次
              </button>
            </div>
          )}
        </div>
      )}

      {showBudget && (
        <div className="space-y-2 rounded-lg border border-orange-200 bg-orange-50 p-3">
          <p className="text-sm text-orange-950">预算不够，调用已暂停。可以提高上限，或放弃这次调用。</p>
          <div className="flex flex-wrap items-center gap-2">
            <input
              type="number"
              min="0"
              step="0.01"
              value={cap}
              onChange={event => setCap(event.target.value)}
              placeholder="新的预算上限"
              className="w-36 rounded-lg border border-orange-200 px-2 py-1.5 text-sm"
            />
            <button
              type="button"
              disabled={busy || cap.trim() === '' || Number.isNaN(Number(cap))}
              onClick={() => onRaise(Number(cap))}
              className="rounded-lg bg-orange-600 px-3 py-1.5 text-sm text-white disabled:opacity-50"
            >
              上调预算
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={onDeclineBudget}
              className="rounded-lg border border-orange-300 bg-white px-3 py-1.5 text-sm text-orange-900 disabled:opacity-50"
            >
              放弃
            </button>
          </div>
        </div>
      )}

      {showRetry && <RetryPanel disabled={busy} onRetry={onRetry} />}
    </article>
  );
}
