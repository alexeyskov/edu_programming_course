import { describe, expect, it } from 'vitest';
import { formatEvidenceIssue } from './ReviewPage';

describe('review evidence messages', () => {
  it('replaces runner codes and English details with a Russian explanation', () => {
    expect(formatEvidenceIssue('OUTPUT_MISMATCH', 'fallback')).toBe('Вывод программы не совпал с ожидаемым.');
    expect(formatEvidenceIssue('RUNNER_INTEGRATION_FAILED', 'fallback')).toBe('Сервис запуска не смог сформировать результат для этого теста.');
  });

  it('does not expose an unknown backend code or message', () => {
    expect(formatEvidenceIssue('FUTURE_INTERNAL_FAILURE', 'Сервис обнаружил проблему при выполнении теста.'))
      .toBe('Сервис обнаружил проблему при выполнении теста.');
  });
});
