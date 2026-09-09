import { describe, expect, it, vi } from 'vitest';
import { createUuid } from './uuid';

describe('createUuid', () => {
  it('uses the native implementation when it is available', () => {
    const randomUUID = vi.fn(() => '00000000-0000-4000-8000-000000000001');
    expect(createUuid({ randomUUID })).toBe('00000000-0000-4000-8000-000000000001');
    expect(randomUUID).toHaveBeenCalledOnce();
  });

  it('builds a standards-compatible UUID from secure random bytes', () => {
    const getRandomValues = vi.fn((bytes: Uint8Array) => {
      bytes.set(Array.from({ length: 16 }, (_, index) => index));
      return bytes;
    });
    expect(createUuid({ getRandomValues })).toBe('00010203-0405-4607-8809-0a0b0c0d0e0f');
    expect(getRandomValues).toHaveBeenCalledOnce();
  });

  it('does not fall back to an insecure pseudo-random generator', () => {
    expect(() => createUuid({})).toThrow('Безопасный генератор случайных чисел недоступен');
  });
});
