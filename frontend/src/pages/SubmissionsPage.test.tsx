import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '../components/ui';
import type { Submission } from '../types';
import { SubmissionsPage } from './SubmissionsPage';

const mocks = vi.hoisted(() => ({
  getSubmissions: vi.fn(),
  getMoodleHistoryImportEvents: vi.fn(),
  retryMoodleHistoryImport: vi.fn(),
  claimSubmission: vi.fn(),
}));

vi.mock('../lib/api', () => ({ api: {
  getSubmissions: mocks.getSubmissions,
  getMoodleHistoryImportEvents: mocks.getMoodleHistoryImportEvents,
  retryMoodleHistoryImport: mocks.retryMoodleHistoryImport,
  claimSubmission: mocks.claimSubmission,
} }));

const base: Omit<Submission, 'id' | 'studentName' | 'status'> = {
  assessmentId: 'assessment-1', assessmentTitle: 'Контрольная №1', studentGroup: '1.1',
  submittedAt: '2026-08-25T10:00:00Z', maxScore: 10, risk: 'UNKNOWN', testsPassed: 0,
  testsTotal: 0, files: [], history: [], decisionHistory: [],
};
const items: Submission[] = [
  { ...base, id: 'ungraded', studentName: 'Мария Воронова', status: 'UNGRADED' },
  { ...base, id: 'mine', studentName: 'Илья Морозов', status: 'CLAIMED', claim: { id: 'claim-mine', ownerId: 'teacher', ownerName: 'Коваленко А.', expiresAt: '2026-08-25T12:00:00Z', mine: true } },
  { ...base, id: 'other', studentName: 'Никита Орлов', status: 'CLAIMED', claim: { id: 'claim-other', ownerId: 'other', ownerName: 'Сергеева Е.', expiresAt: '2026-08-25T12:00:00Z', mine: false } },
  { ...base, id: 'graded', studentName: 'Софья Лебедева', status: 'GRADED', score: 9, latestDecision: { reviewerName: 'Коваленко А.', grade: 9, comment: 'Хорошая работа', revision: 1, reviewedAt: '2026-08-25T11:00:00Z', lmsExportState: 'DELIVERED' }, decisionHistory: [{ reviewerName: 'Коваленко А.', grade: 9, comment: 'Хорошая работа', revision: 1, reviewedAt: '2026-08-25T11:00:00Z', lmsExportState: 'DELIVERED' }] },
];

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}{location.search}</output>;
}

function renderPage(initialEntry = '/submissions') {
  return render(<MemoryRouter initialEntries={[initialEntry]}><ToastProvider><LocationProbe /><Routes>
    <Route path="/submissions" element={<SubmissionsPage />} />
    <Route path="/review/:submissionId" element={<p>Экран проверки</p>} />
  </Routes></ToastProvider></MemoryRouter>);
}

function rowFor(studentName: string) {
  return screen.getByText(studentName).closest('.submission-row') as HTMLElement;
}

beforeEach(() => {
  mocks.getSubmissions.mockReset().mockResolvedValue(items);
  mocks.getMoodleHistoryImportEvents.mockReset().mockResolvedValue([]);
  mocks.retryMoodleHistoryImport.mockReset();
  mocks.claimSubmission.mockReset().mockResolvedValue({ id: 'new-claim', ownerId: 'teacher', ownerName: 'Коваленко А.', expiresAt: '2026-08-25T12:00:00Z', mine: true });
});
afterEach(cleanup);

describe('student submissions views', () => {
  it('groups pending and reviewed work into URL-backed tabs and keeps search in the query', async () => {
    renderPage('/submissions?view=pending');

    expect(await screen.findByRole('heading', { name: 'Работы студентов' })).toBeInTheDocument();
    expect(screen.getByText('Проверки')).toBeInTheDocument();
    expect(screen.queryByText('Свидетельства')).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Все сданные\s*4/ })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Ожидают проверки\s*3/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: /Проверенные\s*1/ })).toBeInTheDocument();
    expect(screen.getByText('Мария Воронова')).toBeInTheDocument();
    expect(screen.getByText('Илья Морозов')).toBeInTheDocument();
    expect(screen.getByText('Никита Орлов')).toBeInTheDocument();
    expect(screen.queryByText('Софья Лебедева')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: /Проверенные/ }));
    expect(await screen.findByText('Софья Лебедева')).toBeInTheDocument();
    expect(screen.queryByText('Мария Воронова')).not.toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/submissions?view=reviewed');

    fireEvent.change(screen.getByLabelText('Поиск по работам'), { target: { value: 'Лебедева' } });
    expect(screen.getByTestId('location')).toHaveTextContent('/submissions?view=reviewed&q=%D0%9B%D0%B5%D0%B1%D0%B5%D0%B4%D0%B5%D0%B2%D0%B0');
    expect(screen.getByText('Софья Лебедева')).toBeInTheDocument();
  });

  it('shows status-specific actions and opens a checked result without claiming it', async () => {
    renderPage('/submissions?view=all');
    await screen.findByText('Софья Лебедева');

    expect(screen.getByRole('button', { name: 'Открыть следующую' })).toBeInTheDocument();
    expect(within(rowFor('Мария Воронова')).getByRole('button', { name: /^Открыть$/ })).toBeInTheDocument();
    expect(within(rowFor('Мария Воронова')).getByText('Не проверено')).toBeInTheDocument();
    expect(screen.queryByText('Свободна')).not.toBeInTheDocument();
    expect(within(rowFor('Илья Морозов')).getByRole('button', { name: /Продолжить/ })).toBeInTheDocument();
    expect(within(rowFor('Никита Орлов')).getByRole('button', { name: /^Открыть$/ })).toBeInTheDocument();
    fireEvent.click(within(rowFor('Софья Лебедева')).getByRole('button', { name: /Открыть результат/ }));

    expect(mocks.claimSubmission).not.toHaveBeenCalled();
    expect(await screen.findByText('Экран проверки')).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/review/graded?view=all');
  });

  it('counts a multi-question Moodle attempt once and opens its pending question', async () => {
    mocks.getSubmissions.mockResolvedValue([{
      ...items[0],
      id: 'question-2',
      assessmentTitle: 'Самостоятельная работа №4',
      reviewGroup: {
        id: 'quiz-attempt-123',
        title: 'Самостоятельная работа №4',
        items: [
          { submissionId: 'question-1', position: 1, title: 'Задание 1', score: 4, maxScore: 5, status: 'GRADED' },
          { submissionId: 'question-2', position: 2, title: 'Задание 2', maxScore: 5, status: 'UNGRADED' },
        ],
      },
    }]);
    renderPage('/submissions?view=pending');

    expect(await screen.findByText('Самостоятельная работа №4')).toBeInTheDocument();
    expect(screen.getByText(/2 задания/)).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Все сданные\s*1/ })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Ожидают проверки\s*1/ })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Проверенные\s*0/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Открыть следующую' }));
    await waitFor(() => expect(mocks.claimSubmission).toHaveBeenCalledWith('question-2'));
    expect(screen.getByTestId('location')).toHaveTextContent('/review/question-2?view=pending');
  });

  it('claims a checked result only through the explicit recheck action', async () => {
    renderPage('/submissions?view=reviewed&q=%D0%A1%D0%BE%D1%84%D1%8C%D1%8F');
    await screen.findByText('Софья Лебедева');
    const row = rowFor('Софья Лебедева');
    fireEvent.click(within(row).getByRole('button', { name: /Перепроверить/ }));

    await waitFor(() => expect(mocks.claimSubmission).toHaveBeenCalledWith('graded'));
    expect(await screen.findByText('Экран проверки')).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/review/graded?view=reviewed&q=%D0%A1%D0%BE%D1%84%D1%8C%D1%8F');
  });

  it('keeps globally visible administrator rows read-only when no teacher scope exists', async () => {
    mocks.getSubmissions.mockResolvedValue([
      { ...items[0], id: 'admin-ungraded', canReview: false },
      { ...items[3], id: 'admin-graded', canReview: false },
    ]);
    renderPage('/submissions?view=all');
    await screen.findByText('Мария Воронова');

    const pending = rowFor('Мария Воронова');
    const reviewed = rowFor('Софья Лебедева');
    expect(within(pending).getByRole('button', { name: /^Открыть$/ })).toBeInTheDocument();
    expect(within(reviewed).queryByRole('button', { name: /Перепроверить/ })).not.toBeInTheDocument();

    fireEvent.click(within(pending).getByRole('button', { name: /^Открыть$/ }));
    expect(mocks.claimSubmission).not.toHaveBeenCalled();
    expect(await screen.findByText('Экран проверки')).toBeInTheDocument();
  });

  it('shows non-graded lab submissions in all work without treating them as pending review', async () => {
    mocks.getSubmissions.mockResolvedValue([{ ...items[0], id: 'lab', assessmentTitle: 'Лабораторная работа', reviewRequired: false, canReview: false }]);
    renderPage('/submissions?view=all');

    await screen.findByText('Мария Воронова');
    const row = rowFor('Мария Воронова');
    expect(screen.getByRole('tab', { name: /Все сданные\s*1/ })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Ожидают проверки\s*0/ })).toBeInTheDocument();
    expect(within(row).getByText('Проверка не требуется')).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: /^Открыть$/ })).toBeInTheDocument();
  });

  it('explains background Moodle history import and refreshes an empty list explicitly', async () => {
    mocks.getSubmissions.mockResolvedValue([]);
    renderPage('/submissions?view=reviewed');

    expect(await screen.findByText('Проверенных работ пока нет')).toBeInTheDocument();
    expect(screen.getByText(/Исторические сдачи и оценки Moodle импортируются в фоне после синхронизации LMS/)).toBeInTheDocument();
    expect(screen.queryByText(/автоматически не переносятся/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Обновить список/ }));
    await waitFor(() => expect(mocks.getSubmissions).toHaveBeenCalledTimes(2));
  });

  it.each([false, true])('does not treat zero submissions as a sync error (completed import: %s)', async (completed) => {
    mocks.getSubmissions.mockResolvedValue([]);
    if (completed) {
      mocks.getMoodleHistoryImportEvents.mockResolvedValue([{
        id: 'empty-import', aggregateId: 'assessment-1', actorKey: 'teacher-1', state: 'DELIVERED',
        createdAt: '2026-09-09T10:00:00Z', updatedAt: '2026-09-09T10:01:00Z',
        receipt: { complete: true, created: 0, updated: 0, unchanged: 0, warning_count: 0 },
      }]);
    }
    renderPage();

    expect(await screen.findByText('Сданных работ пока нет')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Все сданные\s*0/ })).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByText('Загружаем прошлые сдачи из Moodle')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Открыть следующую' })).toBeDisabled();
  });

  it('clears an older import failure after a successful import with zero submissions', async () => {
    mocks.getSubmissions.mockResolvedValue([]);
    mocks.getMoodleHistoryImportEvents.mockResolvedValue([
      { id: 'failed', aggregateId: 'assessment-1', actorKey: 'teacher-1', state: 'FAILED', createdAt: '2026-09-09T09:00:00Z', updatedAt: '2026-09-09T09:01:00Z', receipt: {} },
      { id: 'empty', aggregateId: 'assessment-1', actorKey: 'teacher-1', state: 'DELIVERED', createdAt: '2026-09-09T10:00:00Z', updatedAt: '2026-09-09T10:01:00Z', receipt: { complete: true, created: 0 } },
    ]);
    renderPage();

    expect(await screen.findByText('Сданных работ пока нет')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('does not resurrect an old failed import merely because it was updated late', async () => {
    mocks.getMoodleHistoryImportEvents.mockResolvedValue([
      { id: 'old', aggregateId: 'assessment-1', state: 'FAILED', createdAt: '2026-09-09T08:00:00Z', updatedAt: '2026-09-09T11:00:00Z' },
      { id: 'new', aggregateId: 'assessment-1', state: 'DELIVERED', createdAt: '2026-09-09T09:00:00Z', updatedAt: '2026-09-09T10:00:00Z', receipt: { complete: true, created: 0 } },
    ]);
    renderPage();
    await screen.findByText('Мария Воронова');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('names the work and actual cause rather than assuming every failure requires a login', async () => {
    mocks.getMoodleHistoryImportEvents.mockResolvedValue([
      { id: 'failed', aggregateId: 'assessment-1', assessmentTitle: 'Лабораторная №3', lastError: 'TIMEOUT: External service timed out', state: 'FAILED', createdAt: '', updatedAt: '' },
      { id: 'active', aggregateId: 'assessment-2', state: 'PROCESSING', createdAt: '', updatedAt: '' },
    ]);
    renderPage();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Лабораторная №3');
    expect(alert).toHaveTextContent('Moodle не ответил вовремя');
    expect(alert).toHaveTextContent('Другие работы продолжают загружаться');
    expect(alert).not.toHaveTextContent('Проверьте вход');
  });

  it('explains an unrecognized Assignment table without treating it as no submissions', async () => {
    mocks.getSubmissions.mockResolvedValue([]);
    mocks.getMoodleHistoryImportEvents.mockResolvedValue([{
      id: 'failed', aggregateId: 'assessment-1', assessmentTitle: 'Задание №1',
      lastError: 'INVALID_RESPONSE: ASSIGN_TABLE_NOT_FOUND: Moodle submissions table was not recognized',
      state: 'FAILED', createdAt: '', updatedAt: '',
    }]);
    renderPage();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Задание №1');
    expect(alert).toHaveTextContent('Не распознана таблица сдач Moodle');
    expect(alert).toHaveTextContent('это не означает отсутствие работ');
    expect(screen.queryByText('Сданных работ пока нет')).not.toBeInTheDocument();
  });

  it.each(['FAILED', 'BLOCKED'])('keeps a genuine %s import error visible even with zero submissions', async (state) => {
    mocks.getSubmissions.mockResolvedValue([]);
    mocks.getMoodleHistoryImportEvents.mockResolvedValue([{
      id: 'failed-import', aggregateId: 'assessment-1', state, createdAt: '', updatedAt: '', receipt: {},
    }]);
    renderPage();

    expect(await screen.findByRole('alert')).toHaveTextContent('Часть прошлых сдач не загрузилась');
    expect(screen.getByText('Список сдач пока не получен')).toBeInTheDocument();
    expect(screen.queryByText('Сданных работ пока нет')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Повторить загрузку' })).toBeInTheDocument();
  });

  it('does not hide an unavailable import status behind an empty list, and can recover', async () => {
    mocks.getSubmissions.mockResolvedValue([]);
    mocks.getMoodleHistoryImportEvents.mockRejectedValueOnce(new Error('Status request failed'))
      .mockResolvedValue([]);
    renderPage();

    expect(await screen.findByRole('alert')).toHaveTextContent('Не удалось проверить синхронизацию Moodle');
    expect(screen.getByText('Список сдач пока не получен')).toBeInTheDocument();
    expect(screen.queryByText('Сданных работ пока нет')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Проверить статус' }));

    expect(await screen.findByText('Сданных работ пока нет')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('does not hide a real failure while another assessment is still importing', async () => {
    mocks.getSubmissions.mockResolvedValue([]);
    mocks.getMoodleHistoryImportEvents.mockResolvedValue([
      { id: 'failed', aggregateId: 'assessment-1', state: 'FAILED', createdAt: '', updatedAt: '', receipt: {} },
      { id: 'active', aggregateId: 'assessment-2', state: 'PROCESSING', createdAt: '', updatedAt: '', receipt: {} },
    ]);
    renderPage();

    expect(await screen.findByRole('alert')).toHaveTextContent('Часть прошлых сдач не загрузилась');
    expect(screen.getByText('Загружаем прошлые сдачи из Moodle')).toBeInTheDocument();
    expect(screen.queryByText('Сданных работ пока нет')).not.toBeInTheDocument();
  });

  it('keeps already loaded submissions visible when the import status request fails', async () => {
    mocks.getMoodleHistoryImportEvents.mockRejectedValue(new Error('Status request failed'));
    renderPage();

    expect(await screen.findByText('Мария Воронова')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Все сданные\s*4/ })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Не удалось проверить синхронизацию Moodle');
  });

  it('shows progress and a safe recovery message for Moodle history jobs', async () => {
    mocks.getSubmissions.mockResolvedValue([]);
    mocks.getMoodleHistoryImportEvents.mockResolvedValueOnce([{
      id: 'event-1', aggregateId: 'assessment-1', state: 'PROCESSING', createdAt: '', updatedAt: '', receipt: {},
    }]).mockResolvedValueOnce([{
      id: 'event-2', aggregateId: 'assessment-1', state: 'FAILED', createdAt: '', updatedAt: '', receipt: {},
    }]);
    renderPage('/submissions');

    expect(await screen.findByText('Загружаем прошлые сдачи из Moodle')).toBeInTheDocument();
    expect(screen.getByText('Сдачи ещё загружаются')).toBeInTheDocument();
    expect(screen.queryByText('Сданных работ пока нет')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Обновить список/ }));
    expect(await screen.findByText('Часть прошлых сдач не загрузилась')).toBeInTheDocument();
    expect(screen.queryByText(/UNEXPECTED_|LMS_REAUTH_REQUIRED/)).not.toBeInTheDocument();
  });

  it('restarts a failed Moodle history job instead of only re-reading the same error', async () => {
    const failed = {
      id: 'event-failed', aggregateId: 'assessment-1', actorKey: 'teacher-1', state: 'FAILED' as const,
      createdAt: '2026-09-03T01:00:00Z', updatedAt: '2026-09-03T01:01:00Z', receipt: {},
    };
    const retrying = { ...failed, state: 'RETRY' as const, updatedAt: '2026-09-03T02:00:00Z' };
    mocks.getSubmissions.mockResolvedValue([]);
    mocks.getMoodleHistoryImportEvents.mockResolvedValue([failed]);
    mocks.retryMoodleHistoryImport.mockResolvedValue(retrying);
    renderPage('/submissions');

    fireEvent.click(await screen.findByRole('button', { name: 'Повторить загрузку' }));

    await waitFor(() => expect(mocks.retryMoodleHistoryImport).toHaveBeenCalledWith('event-failed'));
    expect(await screen.findByText('Загружаем прошлые сдачи из Moodle')).toBeInTheDocument();
    expect(screen.queryByText('Часть прошлых сдач не загрузилась')).not.toBeInTheDocument();
  });
});
