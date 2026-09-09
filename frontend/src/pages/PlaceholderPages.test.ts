import { describe, expect, it } from 'vitest';
import type { HiddenTestCase } from '../types';
import { buildHiddenTestManifest } from './PlaceholderPages';

const testCase = (name: string, patch: Partial<HiddenTestCase> = {}): HiddenTestCase => ({
  name, stdin: '', expected_stdout: 'OK\n', comparison: 'EXACT', ...patch,
});

describe('hidden-test manifest v1 editor contract', () => {
  it('serializes only the strict manifest-v1 fields and trims case names', () => {
    const result = buildHiddenTestManifest(true, [testCase('  sample  ', { comparison: 'TRIM_TRAILING_WHITESPACE' })]);
    expect(result.error).toBeUndefined();
    expect(result.manifest).toEqual({
      schema_version: 1,
      cases: [{ name: 'sample', stdin: '', expected_stdout: 'OK\n', comparison: 'TRIM_TRAILING_WHITESPACE' }],
    });
  });

  it('uses an empty object at the API layer when hidden tests are disabled', () => {
    expect(buildHiddenTestManifest(false, [testCase('ignored')])).toEqual({});
  });

  it('rejects empty and duplicate case names without regard to case', () => {
    expect(buildHiddenTestManifest(true, [testCase('  ')])).toMatchObject({ error: expect.stringContaining('не заполнено') });
    expect(buildHiddenTestManifest(true, [testCase('Boundary'), testCase('boundary')])).toMatchObject({ error: expect.stringContaining('уникальными') });
  });

  it('enforces per-field and aggregate backend limits before submission', () => {
    expect(buildHiddenTestManifest(true, [testCase('large', { stdin: 'x'.repeat(262_145) })])).toMatchObject({ error: expect.stringContaining('262 144') });
    expect(buildHiddenTestManifest(true, [testCase('multibyte', { stdin: 'я'.repeat(131_073) })])).toMatchObject({ error: expect.stringContaining('байта UTF-8') });
    const largeCases = Array.from({ length: 3 }, (_, index) => testCase(`large-${index}`, {
      stdin: 'x'.repeat(262_144), expected_stdout: 'y'.repeat(262_144),
    }));
    expect(buildHiddenTestManifest(true, largeCases)).toMatchObject({ error: expect.stringContaining('1 МиБ') });
  });

  it('accepts no more than twenty cases', () => {
    expect(buildHiddenTestManifest(true, Array.from({ length: 21 }, (_, index) => testCase(`case-${index}`))))
      .toMatchObject({ error: expect.stringContaining('1 до 20') });
  });
});
