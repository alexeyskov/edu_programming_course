import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '../components/ui';
import type { MoodleHistoryImportEvent, Submission } from '../types';
import { SubmissionsPage } from './SubmissionsPage';

const mocks = vi.hoisted(() => ({
  getSubmissions: vi.fn(),
  getMoodleHistoryImportEvents: vi.fn(),
  dismissMoodleHistoryWarnings: vi.fn(),
  retryMoodleHistoryImport: vi.fn(),
  claimSubmission: vi.fn(),
}));

vi.mock('../lib/api', () => ({ api: {
  getSubmissions: mocks.getSubmissions,
  getMoodleHistoryImportEvents: mocks.getMoodleHistoryImportEvents,
  dismissMoodleHistoryWarnings: mocks.dismissMoodleHistoryWarnings,
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
  mocks.dismissMoodleHistoryWarnings.mockReset().mockImplementation(async (_assessment, ids) => ids);
  mocks.retryMoodleHistoryImport.mockReset();
  mocks.claimSubmission.mockReset().mockResolvedValue({ id: 'new-claim', ownerId: 'teacher', ownerName: 'Коваленко А.', expiresAt: '2026-08-25T12:00:00Z', mine: true });
});
afterEach(cleanup);

describe('course and work filters', () => {
  const queue: Submission[] = [
    { ...items[0], courseId: 'course-a', courseTitle: 'Программирование на C++', assessmentId: 'essay-1', parentAssessmentId: 'quiz-a', parentAssessmentTitle: 'Контрольная №1' },
    { ...items[1], courseId: 'course-a', courseTitle: 'Программирование на C++', assessmentId: 'quiz-b', assessmentTitle: 'Лабораторная №2' },
    { ...items[2], status: 'UNGRADED', claim: undefined, courseId: 'course-b', courseTitle: 'Программирование на C++', assessmentId: 'quiz-c' },
    { ...items[3], courseId: 'course-a', courseTitle: 'Программирование на C++', assessmentId: 'essay-2', parentAssessmentId: 'quiz-a', parentAssessmentTitle: 'Контрольная №1' },
  ];
  beforeEach(() => mocks.getSubmissions.mockResolvedValue(queue));

  it('disables work selection until a course is chosen and keeps same-named courses separate', async () => {
    renderPage('/submissions?assessment=quiz-a');
    const course = await screen.findByRole('combobox', { name: 'Курс' });
    const work = screen.getByRole('combobox', { name: 'Работа' });
    expect(work).toBeDisabled();
    expect(work).toHaveValue('');
    expect(within(work).getAllByRole('option')).toHaveLength(1);
    expect(within(course).getAllByRole('option')).toHaveLength(3);
    expect(screen.getByText('Никита Орлов')).toBeInTheDocument();

    fireEvent.change(course, { target: { value: 'course-a' } });
    expect(work).toBeEnabled();
    expect(work).toHaveValue('');
    expect(screen.getByTestId('location')).toHaveTextContent('/submissions?course=course-a');
    expect(screen.queryByText('Никита Орлов')).not.toBeInTheDocument();
    expect(screen.getByText('Илья Морозов')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Все сданные\s*3/ })).toBeInTheDocument();
    expect(within(work).getAllByRole('option')).toHaveLength(3);

    fireEvent.change(work, { target: { value: 'quiz-a' } });
    expect(screen.getByText('Мария Воронова')).toBeInTheDocument();
    expect(screen.getByText('Софья Лебедева')).toBeInTheDocument();
    expect(screen.queryByText('Илья Морозов')).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Все сданные\s*2/ })).toBeInTheDocument();
  });

  it('clears the selected work when changing or clearing the course', async () => {
    renderPage('/submissions?course=course-a&assessment=quiz-a');
    const course = await screen.findByRole('combobox', { name: 'Курс' });
    fireEvent.change(course, { target: { value: 'course-b' } });
    const work = screen.getByRole('combobox', { name: 'Работа' });
    expect(work).toHaveValue('');
    expect(within(work).queryByRole('option', { name: 'Лабораторная №2' })).not.toBeInTheDocument();
    expect(screen.getByText('Никита Орлов')).toBeInTheDocument();
    expect(screen.queryByText('Мария Воронова')).not.toBeInTheDocument();
    fireEvent.change(work, { target: { value: 'quiz-c' } });
    fireEvent.change(course, { target: { value: '' } });
    expect(work).toHaveValue('');
    expect(work).toBeDisabled();
    expect(screen.getByText('Мария Воронова')).toBeInTheDocument();
    expect(screen.getByTestId('location').textContent).toBe('/submissions');
  });

  it('combines saved filters with tabs and search, without changing the available options', async () => {
    renderPage('/submissions?course=course-a&assessment=quiz-a&view=reviewed&q=Лебедева');
    expect(await screen.findByText('Софья Лебедева')).toBeInTheDocument();
    expect(screen.queryByText('Мария Воронова')).not.toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Работа' })).toHaveValue('quiz-a');
    expect(within(screen.getByRole('combobox', { name: 'Работа' })).getAllByRole('option')).toHaveLength(3);
    expect(screen.getByRole('tab', { name: /Ожидают проверки\s*1/ })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Проверенные\s*1/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Открыть следующую' })).toBeDisabled();
    fireEvent.click(within(rowFor('Софья Лебедева')).getByRole('button', { name: /Открыть результат/ }));
    expect(screen.getByTestId('location').textContent).toBe('/review/graded?course=course-a&assessment=quiz-a&view=reviewed&q=Лебедева');
  });

  it('opens the next submission only inside the selected course and search', async () => {
    renderPage('/submissions?course=course-b&q=Никита');
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть следующую' }));
    await waitFor(() => expect(mocks.claimSubmission).toHaveBeenCalledWith('other'));
    expect(screen.getByTestId('location').textContent).toBe('/review/other?course=course-b&q=Никита');
  });

  it('keeps a filter with no remaining submissions instead of silently showing other work', async () => {
    renderPage('/submissions?course=course-a&assessment=removed-work');
    expect(await screen.findByText('По выбранным фильтрам работ нет')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Работа' })).toHaveValue('removed-work');
    expect(screen.getByRole('button', { name: 'Открыть следующую' })).toBeDisabled();
    fireEvent.change(screen.getByRole('combobox', { name: 'Работа' }), { target: { value: '' } });
    expect(screen.getByText('Мария Воронова')).toBeInTheDocument();
  });
});

const importWarning: MoodleHistoryImportEvent = {
  id: 'import-1', aggregateId: 'assessment-1', assessmentTitle: 'Самостоятельная №1',
  state: 'PARTIAL', createdAt: '', updatedAt: '', receipt: {},
  errorCode: 'ARCHIVE_SOURCE_OMITTED', lastError: 'Не удалось загрузить исходники.',
  warnings: [{
    id: 'a'.repeat(64), code: 'ARCHIVE_SOURCE_OMITTED', studentName: 'Иван Иванов',
    attemptId: '142195', responseLabel: 'Задание 2', submissionId: 'partial-answer',
    moodleUrl: 'https://moodle.test/mod/quiz/review.php?attempt=142195&cmid=31529',
    message: 'В архиве нет поддерживаемых исходников C/C++.',
  }],
};

describe('source warnings stay inside the individual review', () => {
  it.each(['PARTIAL', 'FAILED', 'BLOCKED'])('does not show per-student %s warnings in the queue', async (state) => {
    mocks.getMoodleHistoryImportEvents.mockResolvedValue([{ ...importWarning, state }]);
    renderPage();
    await screen.findByText('Мария Воронова');
    expect(screen.queryByText('Иван Иванов')).not.toBeInTheDocument();
    expect(screen.queryByText(/ARCHIVE_SOURCE_OMITTED/)).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Открыть в Moodle' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Скрыть предупреждение' })).not.toBeInTheDocument();
    expect(mocks.dismissMoodleHistoryWarnings).not.toHaveBeenCalled();
  });

  it.each(['PARTIAL', 'FAILED'])('does not misreport foreign student problems as an empty-state error (%s)', async (state) => {
    mocks.getSubmissions.mockResolvedValue([]);
    mocks.getMoodleHistoryImportEvents.mockResolvedValue([{ ...importWarning, state, warnings: [], warningsDismissed: false }]);
    renderPage();
    expect(await screen.findByText('Сданных работ пока нет')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('keeps an honest empty state if answers could not be loaded, without student details', async () => {
    mocks.getSubmissions.mockResolvedValue([]);
    mocks.getMoodleHistoryImportEvents.mockResolvedValue([importWarning]);
    renderPage();
    expect(await screen.findByText('Список сдач пока не получен')).toBeInTheDocument();
    expect(screen.queryByText('Сданных работ пока нет')).not.toBeInTheDocument();
    expect(screen.queryByText('Иван Иванов')).not.toBeInTheDocument();
  });
});

describe('student submissions views', () => {
  it('shows and opens imported answers without waiting for the import status request', async () => {
    mocks.getMoodleHistoryImportEvents.mockReturnValue(new Promise(() => {}));
    renderPage();
    expect(await screen.findByText('Мария Воронова')).toBeInTheDocument();
    expect(screen.queryByText('Собираем сданные работы…')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Открыть следующую' }));
    expect(await screen.findByText('Экран проверки')).toBeInTheDocument();
    expect(mocks.claimSubmission).toHaveBeenCalledWith('ungraded');
  });

  it('continues fetching new answers while a status request remains pending', async () => {
    vi.useFakeTimers();
    try {
      mocks.getMoodleHistoryImportEvents.mockReturnValue(new Promise(() => {}));
      mocks.getSubmissions.mockResolvedValueOnce([]).mockResolvedValue(items);
      renderPage();
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      expect(screen.queryByText('Мария Воронова')).not.toBeInTheDocument();
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
      expect(screen.getByText('Мария Воронова')).toBeInTheDocument();
      expect(mocks.getSubmissions).toHaveBeenCalledTimes(2);
      expect(mocks.getMoodleHistoryImportEvents).toHaveBeenCalledTimes(1);
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  });

  it.each(['PROCESSING', 'FAILED'])('lets the teacher check an imported answer while the rest is %s', async (state) => {
    mocks.getSubmissions.mockResolvedValue([items[0]]);
    mocks.getMoodleHistoryImportEvents.mockResolvedValue([{
      id: 'partial-import', aggregateId: items[0].assessmentId, state, createdAt: '', updatedAt: '',
    }]);
    renderPage();
    await screen.findByText('Мария Воронова');
    if (state === 'PROCESSING') expect(screen.getByText(/Уже загруженные сдачи доступны для проверки/)).toBeInTheDocument();
    fireEvent.click(within(rowFor('Мария Воронова')).getByRole('button', { name: /^Открыть$/ }));
    expect(await screen.findByText('Экран проверки')).toBeInTheDocument();
    expect(mocks.claimSubmission).toHaveBeenCalledWith('ungraded');
  });

  it('distinguishes delivered answers with warnings from a failed import and permits review', async () => {
    mocks.getSubmissions.mockResolvedValue([items[0]]);
    mocks.getMoodleHistoryImportEvents.mockResolvedValue([{
      id: 'partial-import', aggregateId: items[0].assessmentId, state: 'PARTIAL',
      errorCode: 'ARTIFACT_OMITTED', lastError: 'Часть файлов ответов не удалось скачать из Moodle.',
      createdAt: '', updatedAt: '',
    }]);
    renderPage();
    await screen.findByText('Мария Воронова');
    expect(screen.queryByText('Синхронизация завершена с предупреждениями')).not.toBeInTheDocument();
    expect(screen.queryByText(/ARTIFACT_OMITTED/)).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Открыть следующую' }));
    expect(await screen.findByText('Экран проверки')).toBeInTheDocument();
  });

  it('makes each newly imported answer reviewable before the manual import finishes', async () => {
    vi.useFakeTimers();
    try {
      mocks.getSubmissions.mockResolvedValueOnce([]).mockResolvedValue([items[0]]);
      mocks.getMoodleHistoryImportEvents.mockResolvedValue([{
        id: 'partial-import', aggregateId: items[0].assessmentId, state: 'PROCESSING', createdAt: '', updatedAt: '',
      }]);
      renderPage();
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      expect(screen.getByText('Сдачи ещё загружаются')).toBeInTheDocument();
      await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
      expect(screen.getByText('Мария Воронова')).toBeInTheDocument();
      expect(screen.getByText('Загружаем прошлые сдачи из Moodle')).toBeInTheDocument();
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Открыть следующую' }));
      });
      expect(screen.getByTestId('location')).toHaveTextContent('/review/ungraded');
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  });

  it('preserves the last loaded answers after a failed refresh and can recover', async () => {
    renderPage();
    await screen.findByText('Мария Воронова');
    mocks.getSubmissions.mockRejectedValueOnce(new Error('Временная ошибка сети'));
    fireEvent(document, new Event('visibilitychange'));
    expect(await screen.findByRole('alert')).toHaveTextContent('Не удалось обновить список работ');
    expect(screen.getByText('Мария Воронова')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Открыть следующую' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Обновить список' }));
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    expect(mocks.getSubmissions).toHaveBeenCalledTimes(3);
  });

  it('keeps refreshing a nonempty list after import finishes and removes deleted rows', async () => {
    vi.useFakeTimers();
    try {
      renderPage();
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      expect(screen.getByText('Мария Воронова')).toBeInTheDocument();
      mocks.getSubmissions.mockResolvedValue([items[1]]);
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
      expect(mocks.getSubmissions).toHaveBeenCalledTimes(2);
      expect(screen.queryByText('Мария Воронова')).not.toBeInTheDocument();
      expect(screen.getByText('Илья Морозов')).toBeInTheDocument();
      mocks.getSubmissions.mockResolvedValue(items);
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
      expect(screen.getByText('Мария Воронова')).toBeInTheDocument();
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  });

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

  it('explains manual per-work Moodle sync and refreshes only the local empty list', async () => {
    mocks.getSubmissions.mockResolvedValue([]);
    renderPage('/submissions?view=reviewed');

    expect(await screen.findByText('Проверенных работ пока нет')).toBeInTheDocument();
    expect(screen.getByText(/вручную синхронизируйте нужную работу/)).toBeInTheDocument();
    expect(screen.queryByText(/автоматически не переносятся/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Обновить список/ }));
    await waitFor(() => expect(mocks.getSubmissions).toHaveBeenCalledTimes(2));
    expect(mocks.retryMoodleHistoryImport).not.toHaveBeenCalled();
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

  it('keeps import progress without exposing individual failures in the queue', async () => {
    mocks.getMoodleHistoryImportEvents.mockResolvedValue([
      { id: 'failed', aggregateId: 'assessment-1', assessmentTitle: 'Лабораторная №3', lastError: 'TIMEOUT: External service timed out', state: 'FAILED', createdAt: '', updatedAt: '' },
      { id: 'active', aggregateId: 'assessment-2', state: 'PROCESSING', createdAt: '', updatedAt: '' },
    ]);
    renderPage();
    expect(await screen.findByText('Загружаем прошлые сдачи из Moodle')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByText('Лабораторная №3')).not.toBeInTheDocument();
  });

  it('explains an unrecognized Assignment table without treating it as no submissions', async () => {
    mocks.getSubmissions.mockResolvedValue([]);
    mocks.getMoodleHistoryImportEvents.mockResolvedValue([{
      id: 'failed', aggregateId: 'assessment-1', assessmentTitle: 'Задание №1',
      lastError: 'INVALID_RESPONSE: ASSIGN_TABLE_NOT_FOUND: Moodle submissions table was not recognized',
      state: 'FAILED', createdAt: '', updatedAt: '',
    }]);
    renderPage();
    expect(await screen.findByText('Список сдач пока не получен')).toBeInTheDocument();
    expect(screen.queryByText(/ASSIGN_TABLE_NOT_FOUND/)).not.toBeInTheDocument();
    expect(screen.queryByText('Сданных работ пока нет')).not.toBeInTheDocument();
  });

  it.each(['FAILED', 'BLOCKED'])('keeps a genuine %s import error visible even with zero submissions', async (state) => {
    mocks.getSubmissions.mockResolvedValue([]);
    mocks.getMoodleHistoryImportEvents.mockResolvedValue([{
      id: 'failed-import', aggregateId: 'assessment-1', state, createdAt: '', updatedAt: '', receipt: {},
    }]);
    renderPage();

    expect(await screen.findByText('Список сдач пока не получен')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByText('Сданных работ пока нет')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'К синхронизации работ' })).toHaveAttribute('href', '/courses');
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

    expect(await screen.findByText('Загружаем прошлые сдачи из Moodle')).toBeInTheDocument();
    expect(screen.getByText('Список сдач пока не получен')).toBeInTheDocument();
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
    expect(await screen.findByText('Список сдач пока не получен')).toBeInTheDocument();
    expect(screen.queryByText(/UNEXPECTED_|LMS_REAUTH_REQUIRED/)).not.toBeInTheDocument();
  });

  it('directs a failed import to explicit per-work synchronization rather than bulk retry', async () => {
    const failed = {
      id: 'event-failed', aggregateId: 'assessment-1', actorKey: 'teacher-1', state: 'FAILED' as const,
      createdAt: '2026-09-03T01:00:00Z', updatedAt: '2026-09-03T01:01:00Z', receipt: {},
    };
    mocks.getSubmissions.mockResolvedValue([]);
    mocks.getMoodleHistoryImportEvents.mockResolvedValue([failed]);
    renderPage('/submissions');

    expect(await screen.findByRole('link', { name: 'К синхронизации работ' })).toHaveAttribute('href', '/courses');
    expect(screen.queryByRole('button', { name: 'Повторить загрузку' })).not.toBeInTheDocument();
    expect(mocks.retryMoodleHistoryImport).not.toHaveBeenCalled();
  });
});
