import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { ReviewWritingHistory } from './ReviewWritingHistory';
import type { HistoryEvent } from '../types';

afterEach(cleanup);

const events: HistoryEvent[] = [
  { id: 'delete', type: 'edit', label: 'Изменение файла', detail: 'FILE_DELETE', filePath: 'include/old.h', at: '2026-10-05T10:03:12Z', revision: 3 },
  { id: 'edit', type: 'edit', label: 'Изменение файла', detail: 'REPLACE_CONTENT', fileId: 'main', at: '2026-10-05T10:02:23Z', revision: 2 },
  { id: 'create', type: 'edit', label: 'Изменение файла', detail: 'FILE_CREATE', filePath: 'include/old.h', at: '2026-10-05T10:01:34Z', revision: 1 },
];

describe('review writing history', () => {
  it('reveals exact removed and inserted text, positions and ranges without rendering code as HTML', async () => {
    const { container } = render(<ReviewWritingHistory events={[{
      ...events[1], editDetails: [{ operation: 'replace', startLine: 3, startColumn: 4,
        endLine: 4, endColumn: 2, deleteCount: 5, deletedText: ' old\n', insertText: '<script>new</script>\n' }],
    }]} files={[]} />);
    const summary = screen.getByText('Замена текста · Строка 3, позиция 4');
    expect(screen.queryByLabelText('Удалённый текст')).not.toBeInTheDocument();
    fireEvent.click(summary);
    const removed = await screen.findByLabelText('Удалённый текст');
    expect(removed.querySelector('code')?.textContent).toBe(' old\n');
    expect(screen.getByLabelText('Вставленный текст').querySelector('code')?.textContent).toBe('<script>new</script>\n');
    expect(screen.getByText(/До строки 4, позиции 2/)).toBeInTheDocument();
    expect(container.querySelector('script')).toBeNull();
    fireEvent.click(summary);
  });

  it('shows saved insertion and offset but never invents missing legacy deletion text or coordinates', async () => {
    render(<ReviewWritingHistory events={[{ ...events[1], editDetails: [{
      operation: 'replace', offset: 12, deleteCount: 5, deletedText: null, insertText: 'new',
    }] }]} files={[]} />);
    fireEvent.click(screen.getByText('Замена текста · Смещение от начала файла: 12 символов'));
    await screen.findByText('Удалённый текст не сохранён и не восстановлен по снимкам.');
    expect(screen.getByLabelText('Вставленный текст').querySelector('code')?.textContent).toBe('new');
    expect(screen.queryByText(/Строка 1/)).not.toBeInTheDocument();
  });

  it('shows chronological actions, file paths including deleted files, and exact timestamps', () => {
    render(<ReviewWritingHistory events={events} files={[{ id: 'main', path: 'src/main.cpp', content: '', language: 'cpp' }]} />);
    const rows = screen.getAllByRole('listitem');
    expect(rows.map((row) => row.querySelector('strong')?.textContent)).toEqual(['Создание файла', 'Изменение файла', 'Удаление файла']);
    expect(within(rows[0]).getByText('include/old.h')).toBeInTheDocument();
    expect(within(rows[1]).getByText('src/main.cpp')).toBeInTheDocument();
    expect(within(rows[2]).getByText('include/old.h')).toBeInTheDocument();
    expect(rows[0].querySelector('time')).toHaveAttribute('datetime', '2026-10-05T10:01:34Z');
    expect(rows[0].querySelector('time')?.textContent).toMatch(/:01:34/);
    expect(screen.queryByText('FILE_CREATE')).not.toBeInTheDocument();
    // Sorting must not change the supplied submission history.
    expect(events[0].id).toBe('delete');
  });

  it('opens and closes the list from the writing history heading', () => {
    render(<ReviewWritingHistory events={events} files={[]} />);
    const toggle = screen.getByRole('button', { name: /История написания/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('list')).not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(screen.getAllByRole('listitem')).toHaveLength(3);
  });

  it('keeps long histories compact and reveals the next portion on request', () => {
    render(<ReviewWritingHistory events={Array.from({ length: 205 }, (_, index) => ({ ...events[1], id: String(index), revision: index }))} files={[]} />);
    expect(screen.getAllByRole('listitem')).toHaveLength(100);
    fireEvent.click(screen.getByRole('button', { name: 'Показать ещё (105)' }));
    expect(screen.getAllByRole('listitem')).toHaveLength(200);
    fireEvent.click(screen.getByRole('button', { name: 'Показать ещё (5)' }));
    expect(screen.getAllByRole('listitem')).toHaveLength(205);
    expect(screen.queryByRole('button', { name: /Показать ещё/ })).not.toBeInTheDocument();
  });

  it('does not invent editing events for a direct Moodle import', () => {
    render(<ReviewWritingHistory events={[{ id: 'import', type: 'snapshot', label: 'Импортировано из Moodle', detail: 'История редактирования в Moodle недоступна', at: '2026-10-05T10:00:00Z', revision: 0 }]} files={[]} />);
    expect(screen.getAllByRole('listitem')).toHaveLength(1);
    expect(screen.getByText('История редактирования в Moodle недоступна')).toBeInTheDocument();
    expect(screen.queryByText('Изменение файла')).not.toBeInTheDocument();
  });

  it('explains an empty history without claiming that the student did not work', () => {
    render(<ReviewWritingHistory events={[]} files={[]} />);
    expect(screen.getByText('События написания для этой сдачи не сохранены.')).toBeInTheDocument();
  });
});
