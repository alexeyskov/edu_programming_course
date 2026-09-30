import { describe, expect, it } from 'vitest';
import { apiNormalizers } from '../lib/api';

describe('review task and source warnings', () => {
  it('preserves the submitted question statement and its safe Moodle link', () => {
    const item = apiNormalizers.mapSubmission({
      id: 'answer', task_statement: 'Напишите функцию <int>.\nНе используйте циклы.',
      source_warnings: [{ code: 'ARCHIVE_SOURCE_OMITTED', message: 'Нет исходников.', moodle_url: 'https://moodle.test/mod/quiz/review.php?attempt=9' }],
    });
    expect(item.taskStatement).toBe('Напишите функцию <int>.\nНе используйте циклы.');
    expect(item.sourceWarnings?.[0].moodleUrl).toBe('https://moodle.test/mod/quiz/review.php?attempt=9');
  });

  it('does not create executable links from warning metadata', () => {
    const item = apiNormalizers.mapSubmission({
      id: 'answer', source_warnings: [{ code: 'ARTIFACT_OMITTED', message: 'Не загружено.', moodle_url: 'javascript:alert(1)' }],
    });
    expect(item.sourceWarnings?.[0].moodleUrl).toBeUndefined();
    expect(item.taskStatement).toBeUndefined();
  });
});
