import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { FORCE_NEW_COPY } from '@/lib/episodeFlow';
import RetryPanel from './RetryPanel';

describe('RetryPanel', () => {
  it('retries in place without a second confirm', () => {
    const onRetry = vi.fn();
    render(<RetryPanel onRetry={onRetry} />);
    fireEvent.click(screen.getByRole('button', { name: '重试提交' }));
    expect(onRetry).toHaveBeenCalledWith(false);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('asks again before a new vendor generation', () => {
    const onRetry = vi.fn();
    render(<RetryPanel onRetry={onRetry} />);
    fireEvent.click(screen.getByRole('button', { name: FORCE_NEW_COPY }));
    expect(onRetry).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog').textContent || '').toContain(FORCE_NEW_COPY);
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(onRetry).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: FORCE_NEW_COPY }));
    fireEvent.click(screen.getByRole('button', { name: '确认新建' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onRetry).toHaveBeenCalledWith(true);
  });
});
