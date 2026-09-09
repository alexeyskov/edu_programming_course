import { describe, expect, it } from 'vitest';
import { formatPublicationIssues } from './PlaceholderPages';

describe('publication validation messages', () => {
  it('translates and deduplicates imported-draft issue codes', () => {
    expect(formatPublicationIssues([
      {
        field: 'policy',
        code: 'LMS_IMPORT_REQUIRES_CONFIGURATION',
        message: 'Imported Moodle work must be configured',
      },
      {
        field: 'task.ai_policy',
        code: 'LMS_IMPORT_REQUIRES_CONFIGURATION',
        message: 'Imported Moodle placeholder must be replaced',
      },
    ])).toBe('Импортированный черновик создан старой версией системы. Повторите синхронизацию курса.');
  });

  it('never exposes backend identifiers from an unknown validation issue', () => {
    const internalId = '12ce09eb-5483-4087-9d97-1cf7bf9091a1';
    const message = formatPublicationIssues([{
      field: 'items',
      code: 'FUTURE_INTERNAL_POLICY',
      message: `Task version ${internalId} is not published`,
    }]);

    expect(message).toBe('Проверьте параметры работы перед публикацией.');
    expect(message).not.toContain(internalId);
  });

  it('renders a course mismatch without the task-version UUID', () => {
    expect(formatPublicationIssues([{
      field: 'items',
      code: 'TASK_COURSE_MISMATCH',
      message: 'The attached task belongs to another course',
    }])).toBe('Выбранное задание относится к другому курсу.');
  });

  it('translates an unconfirmed Moodle source contract without backend fields', () => {
    expect(formatPublicationIssues([{
      field: 'policy',
      code: 'MOODLE_SOURCE_UNCONFIRMED',
      message: 'Missing statement, schedule, attempt_policy',
    }])).toBe('Moodle не подтвердил название, условие, сроки, лимит времени, максимальный балл или число попыток. Повторите синхронизацию курса.');
  });
});
