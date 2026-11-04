/** Pure view rules for one v2 episode. No network. */

export const FORCE_NEW_COPY = '新建会产生新的一笔厂商生成（可能再次扣费）';
export const OFFLINE_BADGE = '已记账·未调用厂商';

const RESERVATION_KINDS = new Set([
  'character',
  'scene',
  'setting',
  'keyframe',
  'image',
  'tts',
]);

export const VIDEO_KINDS = new Set([
  'shot_segment',
  'draft_video',
  'final_video',
  'segment',
  'video',
]);

const STATUS_LABEL: Record<string, string> = {
  pending: '待运行',
  running: '运行中',
  awaiting_user: '等待确认',
  done: '已完成',
  stale: '已过期',
  failed: '失败',
  skipped: '已跳过',
  ready: '预算已通过，准备继续',
  blocked: '被前置节点挡住',
  unavailable: '报价不可用',
  unsupported: '节点类型暂不支持',
};

export interface QuoteView {
  available?: boolean;
  min?: number | null;
  max?: number | null;
  currency?: string | null;
  reason?: string;
}

export interface NodeView {
  id: string;
  kind: string;
  status?: string;
  node_state?: string | null;
  label?: string;
  metadata?: Record<string, unknown>;
  quote?: QuoteView | null;
  budget_hold?: boolean;
  entry_id?: string | null;
  provider_called?: boolean;
}

export function statusLabel(status: string | null | undefined): string {
  if (!status) return '未知';
  return STATUS_LABEL[status] || status;
}

export function isVideoNode(node: { kind?: string }): boolean {
  return VIDEO_KINDS.has(node.kind || '');
}

export function nodeLabel(node: { id: string; label?: string; kind?: string }): string {
  if (node.label && node.label.trim()) return node.label.trim();
  return node.kind || node.id;
}

export function needsVideoRetry(node: NodeView): boolean {
  if (!isVideoNode(node) || node.status !== 'awaiting_user') return false;
  const meta = node.metadata || {};
  const task = meta.vendor_task_id;
  const hasTask = typeof task === 'string' && task.trim().length > 0;
  return Boolean(meta.submit_dispatched || hasTask || meta.force_new_pending);
}

export function formatQuote(quote: QuoteView | null | undefined): string {
  if (!quote) return '没有报价';
  if (quote.available === false) return quote.reason || '报价不可用';
  const currency = quote.currency || '';
  if (typeof quote.min === 'number' && typeof quote.max === 'number' && quote.min !== quote.max) {
    return `报价 ${quote.min} – ${quote.max} ${currency}`.trim();
  }
  if (typeof quote.max === 'number') return `报价上限 ${quote.max} ${currency}`.trim();
  return '报价不可用';
}

/** Fields the executor already stores on the node. A run sends these back. */
export function runPayload(node: NodeView): Record<string, unknown> {
  const meta = node.metadata || {};
  const body: Record<string, unknown> = {};
  for (const key of [
    'model_id',
    'resolution',
    'text',
    'quantity',
    'unit',
    'completion_tokens',
    'includes_video_input',
    'shot_duration',
  ]) {
    const value = meta[key];
    if (value === undefined || value === null || value === '') continue;
    body[key] = value;
  }
  return body;
}

/** Text for the stop button. Refusals stay on the error line; a settled task is not described as stopped. */
export function stopFeedback(result: {
  message: string;
  stopped: string[];
  refused: Array<{ node_id: string; reason: string }>;
  settled?: Array<{ node_id: string; reason: string }>;
}): { error: string; notice: string } {
  const refused = result.refused
    .map(item => `${item.node_id}：${item.reason}`)
    .filter(part => part.trim())
    .join('；');
  const settled = (result.settled || [])
    .map(item => `${item.node_id}：${item.reason}`)
    .filter(part => part.trim())
    .join('；');
  const notice = [result.message, settled].filter(part => part && part.trim()).join('。');
  const error = refused ? (result.stopped.length ? `未停止：${refused}` : refused) : '';
  return { error, notice };
}

/** Plain confirm never carries force_new. */
export function confirmRequest(): Record<string, never> {
  return {};
}

/** force_new is sent only after the second confirmation. */
export function retryRequest(forceNew: boolean, secondConfirmed: boolean): { force_new: boolean } {
  if (forceNew && !secondConfirmed) {
    throw new Error('新建需要第二次确认');
  }
  return { force_new: Boolean(forceNew && secondConfirmed) };
}

export function offlineUncalled(node: NodeView): boolean {
  if (node.status !== 'done') return false;
  if (node.provider_called === false) return true;
  const result = node.metadata?.result;
  if (!result || typeof result !== 'object') return false;
  return (result as { provider_called?: unknown }).provider_called === false;
}

export function showsReservation(node: { kind?: string }): boolean {
  return RESERVATION_KINDS.has(node.kind || '');
}

export function reservationCopy(
  node: NodeView,
  ledger: Array<Record<string, unknown>> | undefined,
): string {
  if (!showsReservation(node)) return '';
  const rows = (ledger || []).filter(row => row.node_id === node.id);
  const row = [...rows].reverse().find(item => item.status === 'spent' || item.status === 'quoted');
  if (!row || typeof row.quote_amount !== 'number') return '';
  const currency = typeof row.currency === 'string' && row.currency ? ` ${row.currency}` : '';
  const ceiling = `预留上限 ${row.quote_amount}${currency}`;
  if (row.status === 'spent' || typeof row.actual_amount === 'number') {
    return `${ceiling}。按上限入账。`;
  }
  return ceiling;
}

export type ModelDrafts = {
  llm_model: string;
  image_model: string;
  video_model: string;
  tts_model: string;
};

const MODEL_DRAFT_KEYS = ['llm_model', 'image_model', 'video_model', 'tts_model'] as const;

/** Keep a model field the user edited. A field that still matches the last server snapshot takes the new one. */
export function mergeModelDrafts(
  current: ModelDrafts,
  baseline: ModelDrafts,
  incoming: ModelDrafts,
): { drafts: ModelDrafts; baseline: ModelDrafts } {
  const drafts = { ...current };
  const nextBaseline = { ...baseline };
  for (const key of MODEL_DRAFT_KEYS) {
    if (current[key] === baseline[key]) {
      drafts[key] = incoming[key];
      nextBaseline[key] = incoming[key];
    }
  }
  return { drafts, baseline: nextBaseline };
}

/** Reload even when confirm fails, so the server status (retry or confirm) is visible. */
export async function confirmThenReload<T>(
  confirm: () => Promise<T>,
  reload: () => Promise<unknown>,
): Promise<T> {
  try {
    return await confirm();
  } finally {
    await reload();
  }
}

export function canConfirmVideo(node: NodeView): boolean {
  if (!isVideoNode(node)) return false;
  if (needsVideoRetry(node)) return false;
  if (node.budget_hold) return false;
  const waiting = node.status === 'awaiting_user' || node.node_state === 'ready';
  if (!waiting) return false;
  if (node.quote && node.quote.available === false) return false;
  return Boolean(node.quote);
}
