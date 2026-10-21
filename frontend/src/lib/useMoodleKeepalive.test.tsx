import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useMoodleKeepalive } from './useMoodleKeepalive';

const ping = vi.hoisted(() => vi.fn());
vi.mock('./api', () => ({ api: { keepMoodleSessionAlive: ping } }));

const FIVE_MINUTES = 300_000;
let visibility = 'visible';
let online = true;

beforeEach(() => {
  vi.useFakeTimers();
  visibility = 'visible'; online = true;
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility as DocumentVisibilityState);
  vi.spyOn(navigator, 'onLine', 'get').mockImplementation(() => online);
  ping.mockReset().mockResolvedValue({ status: 'ACTIVE' });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

describe('Moodle keepalive for students and teachers', () => {
  it.each(['student', 'teacher'])('renews a %s session every five minutes', async (principal) => {
    renderHook(() => useMoodleKeepalive(principal));
    await act(async () => {});
    expect(ping).toHaveBeenCalledTimes(1);
    await act(async () => { vi.advanceTimersByTime(FIVE_MINUTES - 1); });
    expect(ping).toHaveBeenCalledTimes(1);
    await act(async () => { vi.advanceTimersByTime(1); });
    expect(ping).toHaveBeenCalledTimes(2);
  });

  it('does not renew logged-out, hidden or offline sessions; resumes without a burst', async () => {
    visibility = 'hidden'; online = false;
    const { rerender } = renderHook(({ id }) => useMoodleKeepalive(id), { initialProps: { id: undefined as string | undefined } });
    await act(async () => { vi.advanceTimersByTime(FIVE_MINUTES); });
    expect(ping).not.toHaveBeenCalled();
    rerender({ id: 'student' });
    await act(async () => { vi.advanceTimersByTime(FIVE_MINUTES); });
    expect(ping).not.toHaveBeenCalled();
    visibility = 'visible'; online = true;
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new Event('online'));
    });
    expect(ping).toHaveBeenCalledTimes(1);
  });

  it('never overlaps requests and aborts on logout/unmount', async () => {
    ping.mockImplementation(() => new Promise(() => {}));
    const { rerender, unmount } = renderHook(({ id }) => useMoodleKeepalive(id), { initialProps: { id: 'student' as string | undefined } });
    const signal = ping.mock.calls[0][0] as AbortSignal;
    await act(async () => { vi.advanceTimersByTime(3 * FIVE_MINUTES); });
    expect(ping).toHaveBeenCalledTimes(1);
    rerender({ id: undefined });
    expect(signal.aborted).toBe(true);
    unmount();
    await act(async () => { vi.advanceTimersByTime(FIVE_MINUTES); });
    expect(ping).toHaveBeenCalledTimes(1);
  });

  it('ignores network failure and retries without changing editor/auth state', async () => {
    ping.mockRejectedValueOnce(new Error('network'));
    renderHook(() => useMoodleKeepalive('student'));
    await act(async () => {});
    await act(async () => { vi.advanceTimersByTime(FIVE_MINUTES); });
    expect(ping).toHaveBeenCalledTimes(2);
  });

  it('keeps the local session warm when Moodle uses a non-browser transport', async () => {
    ping.mockResolvedValue({ status: 'SKIPPED' });
    renderHook(() => useMoodleKeepalive('student'));
    await act(async () => {});
    await act(async () => { vi.advanceTimersByTime(FIVE_MINUTES); });
    expect(ping).toHaveBeenCalledTimes(2);
  });

  it('stops trying to revive an actually expired Moodle session', async () => {
    ping.mockResolvedValue({ status: 'REAUTH_REQUIRED' });
    renderHook(() => useMoodleKeepalive('student'));
    await act(async () => {});
    await act(async () => { vi.advanceTimersByTime(2 * FIVE_MINUTES); });
    expect(ping).toHaveBeenCalledTimes(1);
  });

  it('resumes after explicitly logging into Moodle again as the same student', async () => {
    ping.mockResolvedValueOnce({ status: 'REAUTH_REQUIRED' });
    const { rerender } = renderHook(({ revision }) => useMoodleKeepalive('student', revision), {
      initialProps: { revision: 0 },
    });
    await act(async () => {});
    rerender({ revision: 1 });
    await act(async () => {});
    expect(ping).toHaveBeenCalledTimes(2);
    await act(async () => { vi.advanceTimersByTime(FIVE_MINUTES); });
    expect(ping).toHaveBeenCalledTimes(3);
  });
});
