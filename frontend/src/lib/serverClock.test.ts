import { afterEach, describe, expect, it, vi } from 'vitest';
import { createServerClock } from './serverClock';

afterEach(() => vi.restoreAllMocks());

describe('backend-anchored monotonic clock', () => {
  it('ignores wrong device time and subsequent changes to the system clock', () => {
    let tick = 100;
    vi.spyOn(performance, 'now').mockImplementation(() => tick);
    const device = vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2030-01-01T00:00:00Z'));
    const clock = createServerClock();
    const backend = Date.parse('2026-09-30T09:00:00Z');
    clock.sync(new Date(backend).toISOString());
    tick += 60_000;
    expect(clock.now()).toBe(backend + 60_000);
    device.mockReturnValue(0);
    tick += 60_000;
    expect(clock.now()).toBe(backend + 120_000);
  });

  it('accounts for response age, timezone offsets and ignores out-of-order samples', () => {
    vi.spyOn(performance, 'now').mockReturnValue(2500);
    const clock = createServerClock();
    const backend = Date.parse('2026-09-30T09:00:00Z');
    clock.sync('2026-09-30T12:00:00+03:00', 500);
    expect(clock.now()).toBe(backend + 2000);
    clock.sync('2026-09-30T08:00:00Z', 2000);
    clock.sync('2026-09-30T10:00:00Z', 100);
    expect(clock.now()).toBe(backend + 2000);
    clock.sync('2026-09-30T09:30:00Z', 2500);
    expect(clock.now()).toBe(backend + 1800_000);
  });

  it('never falls back to device time if the server sample is missing or ambiguous', () => {
    const clock = createServerClock();
    for (const value of [undefined, 'invalid', '2026-09-30T09:00:00']) {
      clock.sync(value);
      expect(clock.now()).toBeNull();
    }
  });
});
