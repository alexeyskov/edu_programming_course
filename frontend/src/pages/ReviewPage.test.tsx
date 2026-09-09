import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { forwardRef, useImperativeHandle } from 'react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '../components/ui';
import { ThemeProvider } from '../context/ThemeContext';
import type { Submission } from '../types';
import { ReviewPage } from './ReviewPage';

const mocks = vi.hoisted(() => ({
  getSubmission: vi.fn(), getAssessment: vi.fn(), getReviewDraft: vi.fn(), claimSubmission: vi.fn(),
  heartbeatClaim: vi.fn(), releaseClaim: vi.fn(), saveReview: vi.fn(), getEvidenceRuns: vi.fn(),
  createExperiment: vi.fn(), runExperiment: vi.fn(), getRun: vi.fn(), saveExperimentFile: vi.fn(),
  startInteractiveExperiment: vi.fn(), getInteractiveExperiment: vi.fn(), sendInteractiveInput: vi.fn(), eofInteractiveExperiment: vi.fn(), stopInteractiveExperiment: vi.fn(),
  resetExperiment: vi.fn(), deleteExperiment: vi.fn(),
  getTeacherAiThreads: vi.fn(), getAiMessages: vi.fn(), createTeacherAiThread: vi.fn(), sendAiMessage: vi.fn(),
}));

vi.mock('../lib/api', () => ({ api: mocks }));
vi.mock('@monaco-editor/react', () => ({
  DiffEditor: ({ original, modified }: { original: string; modified: string }) => (
    <div data-testid="diff-editor" data-original={original} data-modified={modified}>Diff editor</div>
  ),
}));
vi.mock('../components/CodeWorkspace', () => ({
  CodeWorkspace: forwardRef(({ explorerVisible = true, onExplorerCollapse }: { explorerVisible?: boolean; onExplorerCollapse?(): void }, ref) => {
    useImperativeHandle(ref, () => ({ openDiagnostic: vi.fn(), focus: vi.fn() }));
    return <div data-testid="code-workspace">Код работы{explorerVisible && onExplorerCollapse && <aside className="file-explorer"><button type="button" aria-label="Скрыть файлы" onClick={onExplorerCollapse}>Скрыть</button></aside>}</div>;
  }),
}));

const decision = {
  id: 'decision-2', reviewerName: 'Коваленко А.', grade: 9, comment: 'Хорошая работа',
  revision: 2, reviewedAt: '2026-08-25T11:00:00Z', lmsExportState: 'DELIVERED',
};
const previousDecision = {
  id: 'decision-1', reviewerName: 'Сергеева Е.', grade: 8, comment: 'Первоначальная проверка',
  revision: 1, reviewedAt: '2026-08-25T10:30:00Z', lmsExportState: 'SUPERSEDED',
};
const submission: Submission = {
  id: 'graded', assessmentId: 'assessment-1', assessmentTitle: 'Самостоятельная работа',
  studentName: 'Софья Лебедева', studentGroup: '1.1', submittedAt: '2026-08-25T10:00:00Z',
  status: 'GRADED', score: 9, maxScore: 10, risk: 'LOW', testsPassed: 5, testsTotal: 5,
  files: [{ id: 'main', path: 'main.cpp', content: 'int main() {}', language: 'cpp' }], history: [],
  latestDecision: decision, decisionHistory: [decision, previousDecision],
};

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}{location.search}</output>;
}

function renderPage(initialEntry = '/review/graded?view=reviewed') {
  return render(<MemoryRouter initialEntries={[initialEntry]}><ThemeProvider><ToastProvider><LocationProbe /><Routes>
    <Route path="/review/:submissionId" element={<ReviewPage />} />
    <Route path="/submissions" element={<p>Список работ</p>} />
  </Routes></ToastProvider></ThemeProvider></MemoryRouter>);
}

beforeEach(() => {
  window.sessionStorage.clear();
  Object.values(mocks).forEach((mock) => mock.mockReset());
  mocks.getSubmission.mockResolvedValue(submission);
  mocks.getAssessment.mockResolvedValue({ id: 'assessment-1', courseId: 'course-1', reviewRequired: true, decisionSupportEnabled: true });
  mocks.getReviewDraft.mockResolvedValue(null);
  mocks.getEvidenceRuns.mockResolvedValue([]);
  mocks.getTeacherAiThreads.mockResolvedValue([]);
  mocks.getAiMessages.mockResolvedValue([]);
  mocks.createTeacherAiThread.mockResolvedValue({ id: 'teacher-thread-1' });
  mocks.sendAiMessage.mockResolvedValue({ content: 'Проверьте граничные значения.', citations: [] });
  mocks.releaseClaim.mockResolvedValue(undefined);
  mocks.saveReview.mockResolvedValue({ grade: 8, comment: '', revision: 1 });
  mocks.createExperiment.mockResolvedValue({
    id: 'experiment-1', submissionId: 'graded', revision: 0, changed: false,
    files: submission.files, createdAt: '2026-08-25T12:00:00Z',
  });
  mocks.runExperiment.mockResolvedValue({
    id: 'run-1', state: 'COMPLETED', revision: 0, exitCode: 0, stdout: 'ok\n', stderr: '',
    diagnostics: [], createdAt: '2026-08-25T12:01:00Z',
  });
  mocks.startInteractiveExperiment.mockResolvedValue({
    sessionId: 'a'.repeat(32), status: 'SUCCESS', terminal: true, exitCode: 0,
    durationMs: 1, stdout: 'ok\n', stderr: '', outputTruncated: false, diagnostics: [],
  });
  mocks.getInteractiveExperiment.mockResolvedValue({
    sessionId: 'a'.repeat(32), status: 'SUCCESS', terminal: true, exitCode: 0,
    durationMs: 1, stdout: 'ok\n', stderr: '', outputTruncated: false, diagnostics: [],
  });
  mocks.sendInteractiveInput.mockResolvedValue({
    sessionId: 'a'.repeat(32), status: 'RUNNING', terminal: false,
    durationMs: 1, stdout: '', stderr: '', outputTruncated: false, diagnostics: [],
  });
  mocks.eofInteractiveExperiment.mockResolvedValue({
    sessionId: 'a'.repeat(32), status: 'SUCCESS', terminal: true, exitCode: 0,
    durationMs: 1, stdout: '', stderr: '', outputTruncated: false, inputClosed: true, diagnostics: [],
  });
  mocks.stopInteractiveExperiment.mockResolvedValue({
    sessionId: 'a'.repeat(32), status: 'STOPPED', terminal: true,
    durationMs: 1, stdout: '', stderr: '', outputTruncated: false, diagnostics: [],
  });
  mocks.resetExperiment.mockResolvedValue({
    id: 'experiment-1', submissionId: 'graded', revision: 1, changed: false,
    files: submission.files, createdAt: '2026-08-25T12:00:00Z',
  });
  mocks.deleteExperiment.mockResolvedValue(undefined);
  const claim = { id: 'recheck-claim', ownerId: 'teacher-1', ownerName: 'Коваленко А.', expiresAt: '2026-08-25T12:00:00Z', mine: true };
  mocks.claimSubmission.mockResolvedValue(claim);
  mocks.heartbeatClaim.mockResolvedValue(claim);
});
afterEach(() => { cleanup(); window.sessionStorage.clear(); });

describe('reviewed submission', () => {
  it('shows the network and browser audit context in writing history', async () => {
    mocks.getSubmission.mockResolvedValue({
      ...submission,
      history: [{
        id: 'edit-1', type: 'edit', label: 'Изменение файла', detail: 'REPLACE_CONTENT',
        at: '2026-08-25T09:45:00Z', revision: 1,
        client: {
          ipAddress: '203.0.113.42', browser: 'Google Chrome', browserVersion: '140.0',
          operatingSystem: 'Windows 10/11', deviceType: 'DESKTOP',
        },
      }],
    });

    renderPage();
    await screen.findByText('Работа проверена');
    fireEvent.click(screen.getByRole('button', { name: 'История' }));

    expect(screen.getByText(/IP: 203\.0\.113\.42 · Google Chrome 140\.0/)).toBeInTheDocument();
    expect(screen.getByText(/Windows 10\/11 · компьютер/)).toBeInTheDocument();
  });

  it('switches between Moodle Quiz questions without returning to the review queue', async () => {
    const reviewGroup = {
      id: 'quiz-response-81', title: 'Самостоятельная работа №3',
      items: [
        { submissionId: 'question-1', position: 1, title: 'Строки', score: 3, maxScore: 3, status: 'GRADED' as const },
        { submissionId: 'question-2', position: 2, title: 'Массивы', score: 4, maxScore: 5, status: 'GRADED' as const },
      ],
    };
    const first = { ...submission, id: 'question-1', assessmentTitle: 'Строки', score: 3, maxScore: 3, reviewGroup };
    const second = {
      ...submission, id: 'question-2', assessmentTitle: 'Массивы', score: 4, maxScore: 5, reviewGroup,
      files: [{ id: 'arrays-main', path: 'main.cpp', content: 'int values[3];', language: 'cpp' }],
    };
    mocks.getSubmission.mockImplementation(async (id: string) => id === 'question-2' ? second : first);

    renderPage('/review/question-1?view=reviewed&q=quiz');

    const switcher = await screen.findByRole('navigation', { name: 'Задания в ответе студента' });
    expect(within(switcher).getByText('Самостоятельная работа №3')).toBeInTheDocument();
    expect(within(switcher).getByText('Задание 1 из 2')).toBeInTheDocument();
    expect(within(switcher).getByRole('tab', { name: /№1 Строки Балл: 3 \/ 3 Проверено/ })).toHaveAttribute('aria-selected', 'true');
    expect(within(switcher).getByRole('tab', { name: /№2 Массивы Балл: 4 \/ 5 Проверено/ })).toHaveAttribute('aria-selected', 'false');

    fireEvent.click(within(switcher).getByRole('tab', { name: /№2 Массивы/ }));

    await waitFor(() => expect(mocks.getSubmission).toHaveBeenLastCalledWith('question-2'));
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/review/question-2?view=reviewed&q=quiz'));
    const nextSwitcher = await screen.findByRole('navigation', { name: 'Задания в ответе студента' });
    expect(within(nextSwitcher).getByText('Задание 2 из 2')).toBeInTheDocument();
    expect(within(nextSwitcher).getByRole('tab', { name: /№2 Массивы/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByText('Список работ')).not.toBeInTheDocument();
  });

  it('saves a changed review and reserves the destination before releasing the current question', async () => {
    const currentClaim = {
      id: 'claim-question-1', ownerId: 'teacher-1', ownerName: 'Коваленко А.',
      expiresAt: '2026-08-25T12:00:00Z', mine: true,
    };
    const reviewGroup = {
      id: 'quiz-attempt-136460', title: 'Самостоятельная работа №4',
      items: [
        { submissionId: 'question-1', position: 1, title: 'Шаблон Point2D', maxScore: 5, status: 'CLAIMED' as const },
        { submissionId: 'question-2', position: 2, title: 'Контейнеры STL', maxScore: 5, status: 'UNGRADED' as const },
      ],
    };
    const first: Submission = {
      ...submission, id: 'question-1', courseId: 'course-1', assessmentTitle: 'Шаблон Point2D',
      status: 'CLAIMED', score: undefined, maxScore: 5, latestDecision: undefined, decisionHistory: [],
      claim: currentClaim, reviewRequired: true, canReview: true, reviewGroup,
    };
    const second: Submission = {
      ...submission, id: 'question-2', courseId: 'course-1', assessmentTitle: 'Контейнеры STL',
      status: 'UNGRADED', score: undefined, maxScore: 5, latestDecision: undefined, decisionHistory: [],
      claim: undefined, reviewRequired: true, canReview: true, reviewGroup,
    };
    const targetClaim = {
      id: 'claim-question-2', ownerId: 'teacher-1', ownerName: 'Коваленко А.',
      expiresAt: '2026-08-25T12:05:00Z', mine: true,
    };
    mocks.getSubmission.mockImplementation(async (id: string) => id === second.id ? second : first);
    mocks.getReviewDraft.mockResolvedValue(null);
    mocks.claimSubmission.mockResolvedValue(targetClaim);
    mocks.heartbeatClaim.mockResolvedValue(currentClaim);

    renderPage('/review/question-1?view=all&q=quiz');

    const switcher = await screen.findByRole('navigation', { name: 'Задания в ответе студента' });
    fireEvent.change(screen.getByLabelText(/Итоговый балл/), { target: { value: '4' } });
    fireEvent.change(screen.getByLabelText(/^Комментарий студенту/), { target: { value: 'Исправьте обработку границы.' } });
    fireEvent.click(within(switcher).getByRole('tab', { name: /№2 Контейнеры STL/ }));

    await waitFor(() => expect(mocks.saveReview).toHaveBeenCalledWith('question-1', 4, 'Исправьте обработку границы.'));
    await waitFor(() => expect(mocks.claimSubmission).toHaveBeenCalledWith('question-2'));
    await waitFor(() => expect(mocks.releaseClaim).toHaveBeenCalledWith('claim-question-1'));
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/review/question-2?view=all&q=quiz'));
    expect(mocks.getSubmission.mock.calls.filter(([id]) => id === 'question-2')).toHaveLength(1);
    expect(mocks.saveReview.mock.invocationCallOrder[0]).toBeLessThan(mocks.claimSubmission.mock.invocationCallOrder[0]);
    expect(mocks.claimSubmission.mock.invocationCallOrder[0]).toBeLessThan(mocks.releaseClaim.mock.invocationCallOrder[0]);
  });

  it('keeps the current question reserved when the destination cannot be opened', async () => {
    const currentClaim = {
      id: 'claim-question-1', ownerId: 'teacher-1', ownerName: 'Коваленко А.',
      expiresAt: '2026-08-25T12:00:00Z', mine: true,
    };
    const reviewGroup = {
      id: 'quiz-attempt-136460', title: 'Самостоятельная работа №4',
      items: [
        { submissionId: 'question-1', position: 1, title: 'Шаблон Point2D', maxScore: 5, status: 'CLAIMED' as const },
        { submissionId: 'question-2', position: 2, title: 'Контейнеры STL', maxScore: 5, status: 'UNGRADED' as const },
      ],
    };
    const first: Submission = {
      ...submission, id: 'question-1', courseId: 'course-1', assessmentTitle: 'Шаблон Point2D',
      status: 'CLAIMED', score: undefined, maxScore: 5, latestDecision: undefined, decisionHistory: [],
      claim: currentClaim, reviewRequired: true, canReview: true, reviewGroup,
    };
    mocks.getSubmission.mockImplementation(async (id: string) => {
      if (id === 'question-2') throw new Error('Moodle-вопрос временно недоступен');
      return first;
    });

    renderPage('/review/question-1?view=all');

    const switcher = await screen.findByRole('navigation', { name: 'Задания в ответе студента' });
    fireEvent.change(screen.getByLabelText(/Итоговый балл/), { target: { value: '4' } });
    fireEvent.click(within(switcher).getByRole('tab', { name: /№2 Контейнеры STL/ }));

    await waitFor(() => expect(mocks.saveReview).toHaveBeenCalledWith('question-1', 4, ''));
    expect(await screen.findByText('Задание не открыто')).toBeInTheDocument();
    expect(screen.getByText('Moodle-вопрос временно недоступен')).toBeInTheDocument();
    expect(mocks.releaseClaim).not.toHaveBeenCalled();
    expect(screen.getByTestId('location')).toHaveTextContent('/review/question-1?view=all');
  });

  it('does not abandon an invalid unsaved grade while switching questions', async () => {
    const currentClaim = {
      id: 'claim-question-1', ownerId: 'teacher-1', ownerName: 'Коваленко А.',
      expiresAt: '2026-08-25T12:00:00Z', mine: true,
    };
    const reviewGroup = {
      id: 'quiz-attempt-136460', title: 'Самостоятельная работа №4',
      items: [
        { submissionId: 'question-1', position: 1, title: 'Шаблон Point2D', maxScore: 5, status: 'CLAIMED' as const },
        { submissionId: 'question-2', position: 2, title: 'Контейнеры STL', maxScore: 5, status: 'UNGRADED' as const },
      ],
    };
    mocks.getSubmission.mockResolvedValue({
      ...submission, id: 'question-1', courseId: 'course-1', assessmentTitle: 'Шаблон Point2D',
      status: 'CLAIMED', score: undefined, maxScore: 5, latestDecision: undefined, decisionHistory: [],
      claim: currentClaim, reviewRequired: true, canReview: true, reviewGroup,
    });

    renderPage('/review/question-1?view=all');

    const switcher = await screen.findByRole('navigation', { name: 'Задания в ответе студента' });
    fireEvent.change(screen.getByLabelText(/Итоговый балл/), { target: { value: '9' } });
    fireEvent.click(within(switcher).getByRole('tab', { name: /№2 Контейнеры STL/ }));

    expect(await screen.findByText('Изменения не сохранены')).toBeInTheDocument();
    expect(screen.getByText('Укажите балл от 0 до 5, прежде чем переходить к другому заданию.')).toBeInTheDocument();
    expect(mocks.saveReview).not.toHaveBeenCalled();
    expect(mocks.releaseClaim).not.toHaveBeenCalled();
    expect(mocks.getSubmission).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('location')).toHaveTextContent('/review/question-1?view=all');
  });

  it('explains the limits of a historical Moodle submission and localizes its decision status', async () => {
    mocks.getSubmission.mockResolvedValue({
      ...submission,
      source: 'MOODLE_IMPORT',
      latestDecision: { ...decision, lmsExportState: 'IMPORTED' },
      decisionHistory: [{ ...decision, lmsExportState: 'IMPORTED' }],
    });

    const view = renderPage();

    expect(await screen.findByText('Работа проверена')).toBeInTheDocument();
    const notice = document.querySelector('.toast');
    expect(notice).toHaveTextContent('Импортировано из Moodle');
    expect(notice).toHaveTextContent('Доступен итоговый код и решение; истории набора в Moodle нет.');
    expect(document.querySelector('.moodle-import-banner')).toBeNull();
    expect(screen.queryByText('LMS: IMPORTED')).not.toBeInTheDocument();

    view.unmount();
    renderPage();
    await screen.findByText('Работа проверена');
    expect(document.querySelector('.toast')).toBeNull();
    expect(window.sessionStorage.getItem('eduprog:moodle-import-notice:graded')).toBe('1');
  });

  it('renders the final decision read-only and obtains a claim only after explicit recheck', async () => {
    renderPage();

    expect(await screen.findByText('Работа проверена')).toBeInTheDocument();
    expect(mocks.claimSubmission).not.toHaveBeenCalled();
    expect(screen.getByLabelText(/Итоговый балл/)).toBeDisabled();
    expect(screen.getByLabelText(/Финальный комментарий студенту/)).toBeDisabled();
    expect(screen.getByLabelText(/Финальный комментарий студенту/)).toHaveValue('Хорошая работа');
    expect(screen.getByLabelText(/Финальный комментарий студенту/)).toHaveAttribute('rows', '8');
    expect(screen.getAllByText('Коваленко А.').length).toBeGreaterThan(0);
    expect(screen.getByText(/версия 2/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'История' }));
    expect(screen.getByText('История решений')).toBeInTheDocument();
    expect(screen.getByText('Первоначальная проверка')).toBeInTheDocument();
    expect(screen.getByText('Сергеева Е.')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Перепроверить/ }));

    await waitFor(() => expect(mocks.claimSubmission).toHaveBeenCalledWith('graded'));
    expect(await screen.findByText('Вы проверяете работу')).toBeInTheDocument();
    expect(screen.getByLabelText(/Итоговый балл/)).not.toBeDisabled();
    expect(screen.getByLabelText(/^Комментарий студенту/)).toHaveValue('Хорошая работа');
  });

  it('runs a private copy of an already decided submission without starting a recheck', async () => {
    renderPage();
    await screen.findByText('Работа проверена');

    fireEvent.click(screen.getByRole('button', { name: 'Компилировать и запустить код' }));

    await waitFor(() => expect(mocks.createExperiment).toHaveBeenCalledWith('graded'));
    await waitFor(() => expect(mocks.startInteractiveExperiment).toHaveBeenCalledWith('experiment-1', 0));
    expect(mocks.claimSubmission).not.toHaveBeenCalled();
    expect(screen.queryByText('Преподавательская песочница')).not.toBeInTheDocument();
    expect(screen.getByText('Консоль программы')).toBeInTheDocument();
    expect(screen.getByText('ok')).toBeInTheDocument();
    expect(screen.getByLabelText(/Финальный комментарий студенту/)).toBeDisabled();
    expect(screen.getByLabelText(/Финальный комментарий студенту/)).toHaveValue('Хорошая работа');

    fireEvent.click(screen.getByRole('button', { name: 'Закрыть' }));
    await waitFor(() => expect(screen.queryByText('Консоль программы')).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Открыть преподавательскую песочницу' })).toBeInTheDocument();
    expect(mocks.deleteExperiment).not.toHaveBeenCalled();
  });

  it('keeps a private sandbox available while another teacher owns the recheck reservation', async () => {
    mocks.getSubmission.mockResolvedValue({
      ...submission,
      status: 'CLAIMED',
      claim: {
        id: 'other-recheck', ownerId: 'teacher-2', ownerName: 'Сергеева Е.',
        expiresAt: '2026-08-25T12:15:00Z', mine: false,
      },
    });

    renderPage();

    expect(await screen.findByText('Проверяет Сергеева Е.')).toBeInTheDocument();
    expect(screen.getByText(/официальная перепроверка заблокирована/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Перепроверить' })).toBeDisabled();
    expect(screen.getByLabelText(/Финальный комментарий студенту/)).toBeDisabled();
    expect(screen.getByLabelText(/Финальный комментарий студенту/)).toHaveValue('Хорошая работа');

    fireEvent.click(screen.getByRole('button', { name: 'Компилировать и запустить код' }));
    await waitFor(() => expect(mocks.createExperiment).toHaveBeenCalledWith('graded'));
    await waitFor(() => expect(mocks.startInteractiveExperiment).toHaveBeenCalledWith('experiment-1', 0));
    expect(mocks.claimSubmission).not.toHaveBeenCalled();
  });

  it('compares the active experiment file with the original file having the same path', async () => {
    const originalFiles = [
      { id: 'snapshot-main', path: 'main.cpp', content: 'student main', language: 'cpp' },
      { id: 'snapshot-utils', path: 'utils.cpp', content: 'student utils', language: 'cpp' },
    ];
    const experimentFiles = [
      { id: 'experiment-utils', path: 'utils.cpp', content: 'teacher utils', language: 'cpp' },
      { id: 'experiment-main', path: 'main.cpp', content: 'teacher main', language: 'cpp' },
    ];
    mocks.getSubmission.mockResolvedValue({ ...submission, files: originalFiles });
    mocks.createExperiment.mockResolvedValue({
      id: 'experiment-1', submissionId: 'graded', revision: 0, changed: false,
      files: experimentFiles, createdAt: '2026-08-25T12:00:00Z',
    });

    renderPage();
    await screen.findByText('Работа проверена');
    fireEvent.click(screen.getByRole('button', { name: 'Открыть преподавательскую песочницу' }));
    await screen.findByRole('button', { name: 'Закрыть преподавательскую песочницу' });
    fireEvent.click(screen.getByRole('button', { name: /Сравнить/ }));

    const diff = screen.getByTestId('diff-editor');
    expect(diff).toHaveAttribute('data-original', 'student utils');
    expect(diff).toHaveAttribute('data-modified', 'teacher utils');
    expect(screen.getAllByText(/utils\.cpp/)).toHaveLength(2);
  });

  it('collapses both side panels and leaves small restoration controls', async () => {
    renderPage();
    await screen.findByText('Работа проверена');

    const hideFiles = screen.getByRole('button', { name: 'Скрыть файлы' });
    expect(hideFiles.closest('.file-explorer')).not.toBeNull();
    fireEvent.click(hideFiles);
    expect(screen.getByRole('button', { name: 'Показать файлы' })).toBeInTheDocument();
    expect(screen.getByLabelText('Панель файлов скрыта')).toBeInTheDocument();

    const hideReview = screen.getByRole('button', { name: 'Скрыть панель проверки' });
    expect(hideReview.closest('.review-side__top')).not.toBeNull();
    fireEvent.click(hideReview);
    expect(screen.getByRole('button', { name: 'Показать панель проверки' })).toBeInTheDocument();
    expect(screen.getByLabelText('Панель проверки скрыта')).toBeInTheDocument();
    expect(screen.queryByText('Оценка и комментарий')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Показать файлы' }));
    fireEvent.click(screen.getByRole('button', { name: 'Показать панель проверки' }));
    expect(screen.getByRole('button', { name: 'Скрыть файлы' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Скрыть панель проверки' })).toBeInTheDocument();
  });

  it('labels the similarity evidence tab as plagiarism', async () => {
    renderPage();
    await screen.findByText('Работа проверена');

    expect(screen.getByRole('button', { name: 'Плагиат' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Целостность' })).not.toBeInTheDocument();
  });

  it('enters and closes the sandbox without deleting its private copy', async () => {
    renderPage();
    await screen.findByText('Работа проверена');

    const open = screen.getByRole('button', { name: 'Открыть преподавательскую песочницу' });
    expect(open).toHaveAttribute('title', 'Изменения не затрагивают сдачу студента, историю авторства и проверку на плагиат');
    fireEvent.click(open);

    expect(await screen.findByRole('button', { name: 'Закрыть преподавательскую песочницу' })).toHaveTextContent('Закрыть песочницу');
    expect(document.querySelector('.experiment-banner')).not.toBeInTheDocument();
    expect(screen.queryByText('Преподавательская песочница')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Закрыть преподавательскую песочницу' }));

    await waitFor(() => expect(screen.getByRole('button', { name: 'Открыть преподавательскую песочницу' })).toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Открыть преподавательскую песочницу' })).toBeInTheDocument();
    expect(screen.getByText('Оригинал')).toBeInTheDocument();
    expect(mocks.deleteExperiment).not.toHaveBeenCalled();
  });

  it('stops an ordinary interactive run before closing its console', async () => {
    const runningSession = {
      sessionId: 'e'.repeat(32), status: 'RUNNING', terminal: false, durationMs: 1,
      stdout: 'Введите число: ', stderr: '', outputTruncated: false, diagnostics: [],
    };
    mocks.startInteractiveExperiment.mockResolvedValue(runningSession);
    mocks.getInteractiveExperiment.mockResolvedValue(runningSession);
    mocks.stopInteractiveExperiment.mockResolvedValue({ ...runningSession, status: 'STOPPED', terminal: true });
    renderPage();
    await screen.findByText('Работа проверена');

    fireEvent.click(screen.getByRole('button', { name: 'Компилировать и запустить код' }));
    await waitFor(() => expect(screen.getByText('Консоль программы')).toBeInTheDocument());
    expect(screen.queryByText('Преподавательская песочница')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Остановить программу' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Закрыть' }));

    await waitFor(() => expect(mocks.stopInteractiveExperiment).toHaveBeenCalledWith('experiment-1', runningSession.sessionId));
    await waitFor(() => expect(screen.queryByText('Консоль программы')).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Компилировать и запустить код' })).toBeInTheDocument();
  });

  it('does not start a second process when Run is pressed repeatedly', async () => {
    let resolveStart!: (value: {
      sessionId: string; status: string; terminal: boolean; durationMs: number;
      stdout: string; stderr: string; outputTruncated: boolean; diagnostics: never[];
    }) => void;
    const pendingStart = new Promise<{
      sessionId: string; status: string; terminal: boolean; durationMs: number;
      stdout: string; stderr: string; outputTruncated: boolean; diagnostics: never[];
    }>((resolve) => { resolveStart = resolve; });
    mocks.startInteractiveExperiment.mockReturnValue(pendingStart);
    renderPage();
    await screen.findByText('Работа проверена');

    const run = screen.getByRole('button', { name: 'Компилировать и запустить код' });
    fireEvent.click(run);
    fireEvent.click(run);

    await waitFor(() => expect(mocks.startInteractiveExperiment).toHaveBeenCalledTimes(1));
    await act(async () => {
      resolveStart({
        sessionId: 'f'.repeat(32), status: 'SUCCESS', terminal: true, durationMs: 1,
        stdout: 'ok\n', stderr: '', outputTruncated: false, diagnostics: [],
      });
      await pendingStart;
    });
    expect(mocks.createExperiment).toHaveBeenCalledTimes(1);
  });

  it('restores an edited private copy before an ordinary run outside the sandbox', async () => {
    mocks.createExperiment.mockResolvedValue({
      id: 'experiment-1', submissionId: 'graded', revision: 7, changed: true,
      files: [{ ...submission.files[0], content: 'int main() { return 7; }' }],
      createdAt: '2026-08-25T12:00:00Z',
    });
    mocks.resetExperiment.mockResolvedValue({
      id: 'experiment-1', submissionId: 'graded', revision: 8, changed: false,
      files: submission.files, createdAt: '2026-08-25T12:00:00Z',
    });
    renderPage();
    await screen.findByText('Работа проверена');

    fireEvent.click(screen.getByRole('button', { name: 'Компилировать и запустить код' }));

    await waitFor(() => expect(mocks.resetExperiment).toHaveBeenCalledWith('experiment-1', 7));
    await waitFor(() => expect(mocks.startInteractiveExperiment).toHaveBeenCalledWith('experiment-1', 8));
    expect(screen.queryByText('Преподавательская песочница')).not.toBeInTheDocument();
  });

  it('opens a sandbox and sends stdin to a running process one line at a time', async () => {
    const runningSession = {
      sessionId: 'b'.repeat(32), status: 'RUNNING', terminal: false, durationMs: 1,
      stdout: 'Введите число: ', stderr: '', outputTruncated: false, diagnostics: [],
    };
    mocks.startInteractiveExperiment.mockResolvedValue(runningSession);
    mocks.getInteractiveExperiment.mockResolvedValue(runningSession);
    mocks.sendInteractiveInput.mockResolvedValue(runningSession);
    renderPage();
    await screen.findByText('Работа проверена');

    fireEvent.click(screen.getByRole('button', { name: 'Открыть преподавательскую песочницу' }));

    expect(await screen.findByRole('button', { name: 'Закрыть преподавательскую песочницу' })).toBeInTheDocument();
    expect(mocks.createExperiment).toHaveBeenCalledWith('graded');
    expect(mocks.startInteractiveExperiment).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Компилировать и запустить код' }));
    await waitFor(() => expect(mocks.startInteractiveExperiment).toHaveBeenCalledWith('experiment-1', 0));
    await waitFor(() => expect(window.sessionStorage.getItem('eduprog:interactive-review:graded')).toBe(JSON.stringify({ experimentId: 'experiment-1', sessionId: 'b'.repeat(32), sandboxMode: true })));
    const stdin = screen.getByLabelText('Ввод программы');
    fireEvent.change(stdin, { target: { value: '3' } });
    fireEvent.submit(stdin.closest('form')!);
    await waitFor(() => expect(mocks.sendInteractiveInput).toHaveBeenCalledWith('experiment-1', 'b'.repeat(32), '3'));
    expect(screen.queryByText('› 3')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Передать строку программе' })).toHaveTextContent('Отправить');
    expect(screen.queryByRole('button', { name: /EOF/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Остановить программу' }));
    await waitFor(() => expect(mocks.stopInteractiveExperiment).toHaveBeenCalledWith('experiment-1', 'b'.repeat(32)));
    await waitFor(() => expect(window.sessionStorage.getItem('eduprog:interactive-review:graded')).toBeNull());

    mocks.startInteractiveExperiment.mockResolvedValue(runningSession);
    fireEvent.click(screen.getByRole('button', { name: 'Компилировать и запустить код' }));
    await waitFor(() => expect(mocks.startInteractiveExperiment).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(window.sessionStorage.getItem('eduprog:interactive-review:graded')).not.toBeNull());
    fireEvent.click(screen.getAllByRole('button', { name: 'Остановить программу' }).at(-1)!);
    await waitFor(() => expect(mocks.stopInteractiveExperiment).toHaveBeenCalledWith('experiment-1', 'b'.repeat(32)));
    await waitFor(() => expect(window.sessionStorage.getItem('eduprog:interactive-review:graded')).toBeNull());
  });

  it('reconnects to a live teacher session after reload without stopping it on unmount', async () => {
    const runningSession = {
      sessionId: 'c'.repeat(32), status: 'RUNNING', terminal: false, durationMs: 5,
      stdout: 'Ожидание ввода', stderr: '', outputTruncated: false, diagnostics: [],
    };
    window.sessionStorage.setItem('eduprog:interactive-review:graded', JSON.stringify({
      experimentId: 'experiment-1', sessionId: runningSession.sessionId,
    }));
    mocks.getInteractiveExperiment.mockResolvedValue(runningSession);

    const view = renderPage();

    await waitFor(() => expect(mocks.createExperiment).toHaveBeenCalledWith('graded'));
    await waitFor(() => expect(mocks.getInteractiveExperiment).toHaveBeenCalledWith('experiment-1', runningSession.sessionId));
    expect(await screen.findByRole('button', { name: 'Закрыть преподавательскую песочницу' })).toBeInTheDocument();
    expect(screen.getByText('Ожидание ввода')).toBeInTheDocument();
    expect(mocks.startInteractiveExperiment).not.toHaveBeenCalled();

    view.unmount();
    expect(mocks.stopInteractiveExperiment).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem('eduprog:interactive-review:graded')).not.toBeNull();
  });

  it('stops and forgets a live teacher session when the sandbox is closed', async () => {
    const runningSession = {
      sessionId: 'd'.repeat(32), status: 'RUNNING', terminal: false, durationMs: 5,
      stdout: '', stderr: '', outputTruncated: false, diagnostics: [],
    };
    mocks.startInteractiveExperiment.mockResolvedValue(runningSession);
    renderPage();
    await screen.findByText('Работа проверена');
    fireEvent.click(screen.getByRole('button', { name: 'Открыть преподавательскую песочницу' }));
    await screen.findByRole('button', { name: 'Закрыть преподавательскую песочницу' });
    fireEvent.click(screen.getByRole('button', { name: 'Компилировать и запустить код' }));
    await waitFor(() => expect(window.sessionStorage.getItem('eduprog:interactive-review:graded')).not.toBeNull());

    fireEvent.click(screen.getByRole('button', { name: 'Закрыть преподавательскую песочницу' }));
    await waitFor(() => expect(mocks.stopInteractiveExperiment).toHaveBeenCalledWith('experiment-1', runningSession.sessionId));
    await waitFor(() => expect(window.sessionStorage.getItem('eduprog:interactive-review:graded')).toBeNull());
    expect(screen.getByRole('button', { name: 'Открыть преподавательскую песочницу' })).toBeInTheDocument();
  });

  it('returns to the queue with its view and search query intact', async () => {
    renderPage('/review/graded?view=reviewed&q=%D0%9B%D0%B5%D0%B1%D0%B5%D0%B4%D0%B5%D0%B2%D0%B0');
    await screen.findByText('Работа проверена');

    fireEvent.click(screen.getByRole('button', { name: 'Вернуться к работам' }));

    expect(await screen.findByText('Список работ')).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/submissions?view=reviewed&q=%D0%9B%D0%B5%D0%B1%D0%B5%D0%B4%D0%B5%D0%B2%D0%B0');
    expect(mocks.releaseClaim).not.toHaveBeenCalled();
  });

  it('allows teacher AI questions on an approved visible submission without a recheck claim', async () => {
    renderPage();
    await screen.findByText('Работа проверена');

    fireEvent.click(screen.getByRole('button', { name: 'ИИ' }));
    expect(screen.getByText('Спросить о работе')).toBeInTheDocument();
    expect(screen.getByText(/Для вопросов закреплять работу за собой не нужно/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Итоговый балл/)).toBeDisabled();
    expect(mocks.claimSubmission).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Открыть чат' }));
    const input = await screen.findByPlaceholderText('Вопрос о текущем коде…');
    fireEvent.change(input, { target: { value: 'Что проверить вручную?' } });
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));

    await waitFor(() => expect(mocks.createTeacherAiThread).toHaveBeenCalledWith('graded', 'course-1'));
    await waitFor(() => expect(mocks.sendAiMessage).toHaveBeenCalledWith('teacher-thread-1', 'Что проверить вручную?'));
    expect(await screen.findByText('Проверьте граничные значения.')).toBeInTheDocument();
    expect(mocks.claimSubmission).not.toHaveBeenCalled();
    expect(screen.getByLabelText(/Итоговый балл/)).toBeDisabled();
  });
});
