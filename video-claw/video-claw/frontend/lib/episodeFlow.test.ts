import { describe, expect, it } from 'vitest';
import {
  FORCE_NEW_COPY,
  canConfirmVideo,
  confirmRequest,
  formatQuote,
  needsVideoRetry,
  retryRequest,
  runPayload,
  statusLabel,
} from './episodeFlow';

describe('episode view', () => {
  it('labels ready without treating it as done', () => {
    expect(statusLabel('ready')).toBe('预算已通过，准备继续');
    expect(statusLabel('ready')).not.toBe(statusLabel('done'));
    expect(statusLabel('mystery-status')).toBe('mystery-status');
  });

  it('shows a retry entry only for a held video submit', () => {
    const waiting = {
      id: 'video',
      kind: 'shot_segment',
      status: 'awaiting_user',
      metadata: { submit_dispatched: true, vendor_task_id: 'task-1' },
    };
    expect(needsVideoRetry(waiting)).toBe(true);
    expect(needsVideoRetry({ ...waiting, metadata: { force_new_pending: true } })).toBe(true);
    expect(needsVideoRetry({ ...waiting, metadata: {} })).toBe(false);
    expect(needsVideoRetry({ ...waiting, status: 'done' })).toBe(false);
    expect(needsVideoRetry({ ...waiting, kind: 'script' })).toBe(false);
  });

  it('requires a quote before confirm and never sends force_new', () => {
    expect(confirmRequest()).toEqual({});
    expect(JSON.stringify(confirmRequest())).not.toContain('force_new');
    const node = {
      id: 'video',
      kind: 'shot_segment',
      status: 'awaiting_user',
      quote: { available: true, max: 70, currency: 'CNY' },
    };
    expect(canConfirmVideo(node)).toBe(true);
    expect(canConfirmVideo({ ...node, quote: { available: false, reason: '无价' } })).toBe(false);
    expect(canConfirmVideo({ ...node, metadata: { submit_dispatched: true } })).toBe(false);
    expect(canConfirmVideo({ ...node, node_state: 'ready', status: 'awaiting_user' })).toBe(true);
    expect(formatQuote(node.quote)).toBe('报价上限 70 CNY');
  });

  it('sends stored prompt fields and keeps force_new off until the second confirm', () => {
    expect(runPayload({
      id: 'tts',
      kind: 'tts',
      metadata: { model_id: 'tts-1', quantity: 10000, unit: 'per_10k_characters', text: '林夏' },
    })).toEqual({
      model_id: 'tts-1',
      quantity: 10000,
      unit: 'per_10k_characters',
      text: '林夏',
    });
    expect(retryRequest(false, false)).toEqual({ force_new: false });
    expect(() => retryRequest(true, false)).toThrow(/第二次确认/);
    expect(retryRequest(true, true)).toEqual({ force_new: true });
    expect(FORCE_NEW_COPY).toBe('新建会产生新的一笔厂商生成（可能再次扣费）');
  });
});
