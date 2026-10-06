import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { forwardRef, useImperativeHandle } from 'react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '../components/ui';
import { ThemeProvider } from '../context/ThemeContext';
import type { AiCodeFinding, Submission, WorkspaceFile } from '../types';
import type { AiCodeSelection } from '../lib/editorAiSelection';
import { ReviewPage } from './ReviewPage';

const mocks = vi.hoisted(() => ({
  getSubmission: vi.fn(), getAssessment: vi.fn(), getReviewDraft: vi.fn(), claimSubmission: vi.fn(),
  heartbeatClaim: vi.fn(), releaseClaim: vi.fn(), saveReview: vi.fn(), getEvidenceRuns: vi.fn(),
  finalizeReview: vi.fn(),
  createExperiment: vi.fn(), runExperiment: vi.fn(), getRun: vi.fn(), saveExperimentFile: vi.fn(),
  startInteractiveExperiment: vi.fn(), getInteractiveExperiment: vi.fn(), sendInteractiveInput: vi.fn(), eofInteractiveExperiment: vi.fn(), stopInteractiveExperiment: vi.fn(),
  resetExperiment: vi.fn(), deleteExperiment: vi.fn(),
  getTeacherAiThreads: vi.fn(), getAiMessages: vi.fn(), createTeacherAiThread: vi.fn(), sendAiMessageStream: vi.fn(),
  getAiCodeReview: vi.fn(), startAiCodeReview: vi.fn(), discardAiCodeReview: vi.fn(),
  getAuthorshipAnalyses: vi.fn(), getSimilarityAnalyses: vi.fn(),
}));

vi.mock('../lib/api', () => ({ api: mocks }));
vi.mock('@monaco-editor/react', () => ({
  DiffEditor: ({ original, modified }: { original: string; modified: string }) => (
    <div data-testid="diff-editor" data-original={original} data-modified={modified}>Diff editor</div>
  ),
}));
vi.mock('../components/CodeWorkspace', () => ({
  CodeWorkspace: forwardRef(({ files, activeFileId, onActiveFile, onChange, readOnly, explorerVisible = true, explorerId, onExplorerCollapse, aiFindings = [], onAskAi, onAskAiSelection }: {
    files: WorkspaceFile[];
    activeFileId: string;
    onActiveFile(id: string): void;
    onChange(id: string, content: string): void;
    readOnly?: boolean;
    explorerVisible?: boolean;
    explorerId?: string;
    onExplorerCollapse?(): void;
    aiFindings?: unknown[];
    onAskAi?(finding: AiCodeFinding): void;
    onAskAiSelection?(selection: AiCodeSelection): void;
  }, ref) => {
    useImperativeHandle(ref, () => ({ openDiagnostic: vi.fn(), focus: vi.fn() }));
    const activeFile = files.find((file) => file.id === activeFileId) ?? files[0];
    return <div data-testid="code-workspace" data-ai-findings={aiFindings.length}>
      Код работы
      {onAskAi && aiFindings.length > 0 && <button onClick={() => onAskAi(aiFindings[0] as AiCodeFinding)}>Спросить о замечании</button>}
      {onAskAiSelection && activeFile && <button onClick={() => onAskAiSelection({ path: activeFile.path, startLine: 1, endLine: 1, text: activeFile.content })}>Спросить о выделении</button>}
      <aside id={explorerId} className="file-explorer" hidden={!explorerVisible}>
        {onExplorerCollapse && <button type="button" aria-label="Скрыть файлы" onClick={onExplorerCollapse}>Скрыть</button>}
        {files.map((file) => <button key={file.id} type="button" onClick={() => onActiveFile(file.id)}>{file.path}</button>)}
      </aside>
      {activeFile && <textarea aria-label={`Код ${activeFile.path}`} readOnly={readOnly} value={activeFile.content} onChange={(event) => onChange(activeFile.id, event.target.value)} />}
    </div>;
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
  taskStatement: 'Найдите максимум массива. Используйте функцию.',
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

function mockUngradedQuestionGroup() {
  const reviewGroup = {
    id: 'quiz-response', title: 'Два задания',
    items: [1, 2].map((position) => ({
      submissionId: `question-${position}`, position, title: `Задача ${position}`,
      maxScore: 5, status: 'UNGRADED' as const,
    })),
  };
  const questions: Submission[] = reviewGroup.items.map((item) => ({
    ...submission, id: item.submissionId, courseId: 'course-1', assessmentTitle: item.title,
    status: 'UNGRADED', score: undefined, maxScore: 5, latestDecision: undefined, decisionHistory: [],
    claim: undefined, canReview: true, reviewRequired: true, reviewGroup,
    files: [{ ...submission.files[0], content: `int main() { return ${item.position}; }` }],
  }));
  const claim = (id: string) => ({
    id, ownerId: 'teacher-1', ownerName: 'Преподаватель', mine: true,
    expiresAt: '2026-08-25T12:00:00Z',
  });
  mocks.getSubmission.mockImplementation(async (id: string) => questions.find((item) => item.id === id));
  mocks.claimSubmission.mockImplementation(async (id: string) => claim(`claim-${id}`));
  mocks.heartbeatClaim.mockImplementation(async (id: string) => claim(id));
  mocks.createExperiment.mockImplementation(async (id: string) => ({
    id: `experiment-${id}`, submissionId: id, revision: 0, changed: false,
    files: questions.find((item) => item.id === id)!.files, createdAt: '2026-08-25T12:00:00Z',
  }));
  return questions;
}

beforeEach(() => {
  window.sessionStorage.clear();
  window.localStorage.removeItem('eduprog:ai-highlights');
  Object.values(mocks).forEach((mock) => mock.mockReset());
  mocks.getSubmission.mockResolvedValue(submission);
  mocks.getAssessment.mockResolvedValue({ id: 'assessment-1', courseId: 'course-1', reviewRequired: true, decisionSupportEnabled: true });
  mocks.getReviewDraft.mockResolvedValue(null);
  mocks.getEvidenceRuns.mockResolvedValue([]);
  mocks.getAuthorshipAnalyses.mockResolvedValue([]);
  mocks.getSimilarityAnalyses.mockResolvedValue([]);
  mocks.getTeacherAiThreads.mockResolvedValue([]);
  mocks.getAiMessages.mockResolvedValue([]);
  mocks.getAiCodeReview.mockResolvedValue({ enabled: true, state: 'NOT_STARTED', summary: '', findings: [] });
  mocks.startAiCodeReview.mockResolvedValue({ enabled: true, state: 'QUEUED', summary: '', findings: [] });
  mocks.discardAiCodeReview.mockResolvedValue({ enabled: true, state: 'NOT_STARTED', recommendationStatus: 'DISCARDED', summary: '', findings: [] });
  mocks.createTeacherAiThread.mockResolvedValue({ id: 'teacher-thread-1' });
  mocks.sendAiMessageStream.mockResolvedValue({ content: 'Проверьте граничные значения.', citations: [] });
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
afterEach(() => { cleanup(); vi.useRealTimers(); window.sessionStorage.clear(); });

describe('reviewed submission', () => {
  it('shows the exhausted AI error on the task tab without changing Moodle fields', async () => {
    mocks.getAiCodeReview.mockResolvedValue({ id: 'ai-failed', enabled: true, state: 'FAILED', attemptCount: 3, maxAttempts: 3, error: 'ИИ вернул некорректный формат отчёта.', summary: '', findings: [] });
    renderPage();
    await screen.findByText('ИИ-проверка не выполнена');
    expect(screen.getByRole('tab', { name: 'Задание' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('Все попытки исчерпаны. Выполнено: 3 из 3.')).toBeVisible();
    expect(screen.getByText('ИИ вернул некорректный формат отчёта.')).toBeVisible();
    expect(screen.getByLabelText(/Итоговый балл/)).toHaveValue(9);
    expect(screen.getByLabelText('Финальный комментарий студенту')).toHaveValue('Хорошая работа');
    expect(mocks.startAiCodeReview).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Повторить ИИ-проверку' }));
    await screen.findByText('Работа в очереди на проверку.');
    expect(screen.queryByText('ИИ-проверка не выполнена')).not.toBeInTheDocument();
    expect(mocks.startAiCodeReview).toHaveBeenCalledExactlyOnceWith('graded', true);
    expect(mocks.saveReview).not.toHaveBeenCalled();
    expect(mocks.finalizeReview).not.toHaveBeenCalled();
  });

  it('discards the unapproved AI result on explicit exit without changing the Moodle decision', async () => {
    mocks.getAiCodeReview.mockResolvedValue({ id: 'ai-report', recommendationStatus: 'PENDING', enabled: true, state: 'COMPLETED', summary: 'Совет', findings: [], recommendedGrade: 4, recommendedComment: 'Новый совет' });
    renderPage();
    await screen.findByRole('region', { name: 'Ответ ИИ' });
    expect(screen.getByLabelText(/Итоговый балл/)).toHaveValue(9);
    expect(screen.getByLabelText('Финальный комментарий студенту')).toHaveValue('Хорошая работа');
    fireEvent.click(screen.getByRole('button', { name: 'Вернуться к работам' }));
    await screen.findByText('Список работ');
    expect(mocks.discardAiCodeReview).toHaveBeenCalledExactlyOnceWith('graded', 'ai-report');
    expect(mocks.finalizeReview).not.toHaveBeenCalled();
    expect(mocks.startAiCodeReview).not.toHaveBeenCalled();
    expect(mocks.saveReview).not.toHaveBeenCalled();
  });

  it.each(['viewed', 'copied', 'saved'] as const)('keeps an automatic report after exit and reopening when it was %s', async (action) => {
    const report = {
      id: 'automatic-report', automatic: true, enabled: true, recommendationStatus: 'PENDING',
      state: 'COMPLETED', summary: 'Обзор', recommendedGrade: 4, recommendedComment: 'Совет ИИ',
      findings: [{ path: 'main.cpp', line: 1, severity: 'warning', message: 'Проверьте границы' }],
    };
    let savedDraft: { grade: number; comment: string; aiReviewId: string } | null = null;
    mocks.getAiCodeReview.mockResolvedValue(report);
    mocks.getReviewDraft.mockImplementation(async () => savedDraft);
    mocks.claimSubmission.mockResolvedValue({ id: 'claim-1', mine: true, ownerName: 'Teacher' });
    mocks.heartbeatClaim.mockResolvedValue({ id: 'claim-1', mine: true, ownerName: 'Teacher' });
    mocks.saveReview.mockImplementation(async (_id, grade, comment, aiReviewId) => {
      savedDraft = { grade, comment, aiReviewId };
      return { ...savedDraft, revision: 1 };
    });
    // Exit clears the teacher's draft, not the server's cached automatic report.
    mocks.discardAiCodeReview.mockImplementation(async () => { savedDraft = null; return report; });
    const firstVisit = renderPage();
    await screen.findByRole('region', { name: 'Ответ ИИ' });
    expect(screen.getByText(/Ответ автоматической проверки и подсветка замечаний сохраняются/)).toBeVisible();
    if (action !== 'viewed') {
      fireEvent.click(screen.getByRole('button', { name: 'Утвердить рекомендацию ИИ' }));
      await waitFor(() => expect(screen.getByLabelText(/Итоговый балл/)).toHaveValue(4));
      expect(screen.getByLabelText('Комментарий студенту')).toHaveValue('Совет ИИ');
      if (action === 'saved') {
        fireEvent.click(screen.getByRole('button', { name: 'Сохранить' }));
        await waitFor(() => expect(mocks.saveReview).toHaveBeenCalledWith('graded', 4, 'Совет ИИ', 'automatic-report'));
      }
    }
    fireEvent.click(screen.getByRole('button', { name: 'Вернуться к работам' }));
    await screen.findByText('Список работ');
    expect(mocks.discardAiCodeReview).toHaveBeenCalledExactlyOnceWith('graded', 'automatic-report');
    expect(savedDraft).toBeNull();
    firstVisit.unmount();

    renderPage();
    const recommendation = await screen.findByRole('region', { name: 'Ответ ИИ' });
    expect(within(recommendation).getByText('Совет ИИ')).toBeVisible();
    expect(within(recommendation).getByText('4/10')).toBeVisible();
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-ai-findings', '1');
    expect(screen.getByLabelText(/Итоговый балл/)).toHaveValue(9);
    expect(screen.getByLabelText('Финальный комментарий студенту')).toHaveValue('Хорошая работа');
    expect(mocks.startAiCodeReview).not.toHaveBeenCalled();
    expect(mocks.finalizeReview).not.toHaveBeenCalled();
  });

  it('keeps the page, claim and draft if discarding fails, and permits retry', async () => {
    mocks.getSubmission.mockResolvedValue({ ...submission, status: 'CLAIMED', claim: { id: 'claim-1', mine: true, ownerName: 'Teacher' } });
    mocks.heartbeatClaim.mockResolvedValue({ id: 'claim-1', mine: true, ownerName: 'Teacher' });
    mocks.getAiCodeReview.mockResolvedValue({ id: 'ai-report', recommendationStatus: 'PENDING', enabled: true, state: 'COMPLETED', summary: '', findings: [], recommendedGrade: 4, recommendedComment: 'Совет' });
    mocks.discardAiCodeReview.mockRejectedValueOnce(new Error('Сеть недоступна'));
    renderPage();
    await screen.findByRole('region', { name: 'Ответ ИИ' });
    fireEvent.change(screen.getByLabelText(/Итоговый балл/), { target: { value: '6' } });
    fireEvent.change(screen.getByLabelText('Комментарий студенту'), { target: { value: 'Мой черновик' } });
    fireEvent.click(screen.getByRole('button', { name: 'Вернуться к работам' }));
    await screen.findByText('Не удалось завершить просмотр');
    expect(screen.getByTestId('location')).toHaveTextContent('/review/graded');
    expect(mocks.releaseClaim).not.toHaveBeenCalled();
    expect(screen.getByLabelText(/Итоговый балл/)).toHaveValue(6);
    expect(screen.getByLabelText('Комментарий студенту')).toHaveValue('Мой черновик');
    fireEvent.click(screen.getByRole('button', { name: 'Вернуться к работам' }));
    await screen.findByText('Список работ');
    expect(mocks.releaseClaim).toHaveBeenCalledWith('claim-1');
    expect(mocks.finalizeReview).not.toHaveBeenCalled();
  });

  it.each(['APPROVED', 'DISCARDED'] as const)('does not discard an already %s report on exit', async (recommendationStatus) => {
    mocks.getAiCodeReview.mockResolvedValue({ id: 'ai-report', recommendationStatus, enabled: true, state: 'COMPLETED', summary: 'Отчёт', findings: [] });
    renderPage();
    await screen.findByRole('switch', { name: 'Подсветка замечаний ИИ' });
    fireEvent.click(screen.getByRole('button', { name: 'Вернуться к работам' }));
    await screen.findByText('Список работ');
    expect(mocks.discardAiCodeReview).not.toHaveBeenCalled();
    expect(mocks.finalizeReview).not.toHaveBeenCalled();
  });

  it('does not cancel AI when switching questions but discards both viewed reports on exit', async () => {
    mockUngradedQuestionGroup();
    mocks.getAiCodeReview.mockImplementation(async (id: string) => ({ id: `ai-${id}`, recommendationStatus: 'PENDING', enabled: true, state: 'COMPLETED', summary: 'Отчёт', findings: [], recommendedGrade: 4, recommendedComment: 'Совет' }));
    renderPage('/review/question-1');
    await screen.findByRole('region', { name: 'Ответ ИИ' });
    fireEvent.click(screen.getByRole('button', { name: 'Следующее задание' }));
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/review/question-2'));
    await screen.findByRole('region', { name: 'Ответ ИИ' });
    expect(mocks.discardAiCodeReview).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Вернуться к работам' }));
    await screen.findByText('Список работ');
    expect(mocks.discardAiCodeReview.mock.calls).toEqual([
      ['question-1', 'ai-question-1'], ['question-2', 'ai-question-2'],
    ]);
    expect(mocks.finalizeReview).not.toHaveBeenCalled();
  });

  it('waits for cancellation before releasing the claim and prevents double exit while AI runs', async () => {
    mocks.getSubmission.mockResolvedValue({ ...submission, claim: { id: 'claim-1', mine: true, ownerName: 'Teacher' } });
    mocks.heartbeatClaim.mockResolvedValue({ id: 'claim-1', mine: true, ownerName: 'Teacher' });
    mocks.getAiCodeReview.mockResolvedValue({ id: 'ai-running', recommendationStatus: 'NONE', enabled: true, state: 'RUNNING', summary: '', findings: [] });
    let finish!: (value: object) => void;
    mocks.discardAiCodeReview.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    renderPage();
    await screen.findByText('Проверка ИИ 1/3…');
    const exit = screen.getByRole('button', { name: 'Вернуться к работам' });
    fireEvent.click(exit);
    fireEvent.click(exit);
    expect(exit).toBeDisabled();
    expect(mocks.releaseClaim).not.toHaveBeenCalled();
    expect(mocks.discardAiCodeReview).toHaveBeenCalledTimes(1);
    await act(async () => { finish({ recommendationStatus: 'DISCARDED' }); });
    await screen.findByText('Список работ');
    expect(mocks.releaseClaim).toHaveBeenCalledWith('claim-1');
  });

  it.each([false, true])('copies AI values into editable fields and exports only through the main approval button (automatic=%s)', async (automatic) => {
    mocks.getAiCodeReview.mockResolvedValue({ id: 'ai-report', automatic, recommendationStatus: 'PENDING', enabled: true, state: 'COMPLETED', summary: 'Обзор', findings: [], recommendedGrade: 7.5, recommendedComment: 'Исправьте граничный случай.' });
    mocks.claimSubmission.mockResolvedValue({ id: 'ai-claim', mine: true, ownerName: 'Teacher' });
    mocks.finalizeReview.mockResolvedValue({ id: 'new-decision', grade: 7.5, comment: 'Исправьте граничный случай.', lmsExportState: 'PENDING' });
    renderPage();
    const recommendation = await screen.findByRole('region', { name: 'Ответ ИИ' });
    expect(within(recommendation).getByText('7.5/10')).toBeVisible();
    expect(screen.getByLabelText(/Итоговый балл/)).toHaveValue(9);
    expect(screen.getByLabelText('Финальный комментарий студенту')).toHaveValue('Хорошая работа');
    expect(mocks.finalizeReview).not.toHaveBeenCalled();
    expect(mocks.claimSubmission).not.toHaveBeenCalled();
    fireEvent.click(within(recommendation).getByRole('button', { name: 'Утвердить рекомендацию ИИ' }));
    await waitFor(() => expect(screen.getByLabelText(/Итоговый балл/)).toHaveValue(7.5));
    expect(screen.getByLabelText('Комментарий студенту')).toHaveValue('Исправьте граничный случай.');
    expect(screen.getByLabelText('Комментарий студенту')).toBeEnabled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mocks.saveReview).not.toHaveBeenCalled();
    expect(mocks.finalizeReview).not.toHaveBeenCalled();
    fireEvent.click(screen.getAllByRole('button', { name: 'Утвердить' })[0]);
    expect(screen.getByRole('dialog')).toHaveTextContent('7.5/10');
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Утвердить и отправить' }));
    await waitFor(() => expect(mocks.claimSubmission).toHaveBeenCalledWith('graded'));
    await waitFor(() => expect(mocks.finalizeReview).toHaveBeenCalledWith('graded', 7.5, 'Исправьте граничный случай.', 'ai-report'));
    expect(mocks.discardAiCodeReview).not.toHaveBeenCalled();
  });

  it('replaces existing fields with a zero AI grade and keeps subsequent teacher edits after cancelling approval', async () => {
    mocks.getSubmission.mockResolvedValue({ ...submission, status: 'CLAIMED', claim: { id: 'claim-1', mine: true, ownerName: 'Teacher' } });
    mocks.getAiCodeReview.mockResolvedValue({ id: 'ai-zero', recommendationStatus: 'PENDING', enabled: true, state: 'COMPLETED', summary: 'Обзор', findings: [], recommendedGrade: 0, recommendedComment: 'Не соответствует условию.' });
    renderPage();
    await screen.findByRole('region', { name: 'Ответ ИИ' });
    fireEvent.change(screen.getByLabelText(/Итоговый балл/), { target: { value: '6' } });
    fireEvent.change(screen.getByLabelText('Комментарий студенту'), { target: { value: 'Мой черновик' } });
    fireEvent.click(screen.getByRole('button', { name: 'Утвердить рекомендацию ИИ' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Итоговый балл/)).toHaveValue(0);
    expect(screen.getByLabelText('Комментарий студенту')).toHaveValue('Не соответствует условию.');
    fireEvent.change(screen.getByLabelText(/Итоговый балл/), { target: { value: '3' } });
    fireEvent.change(screen.getByLabelText('Комментарий студенту'), { target: { value: 'Уточнение преподавателя' } });
    fireEvent.click(screen.getAllByRole('button', { name: 'Утвердить' })[0]);
    expect(screen.getByRole('dialog')).toHaveTextContent('3/10');
    expect(screen.getByRole('dialog')).toHaveTextContent('Уточнение преподавателя');
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Отмена' }));
    expect(screen.getByLabelText(/Итоговый балл/)).toHaveValue(3);
    expect(screen.getByLabelText('Комментарий студенту')).toHaveValue('Уточнение преподавателя');
    expect(mocks.saveReview).not.toHaveBeenCalled();
    expect(mocks.finalizeReview).not.toHaveBeenCalled();
    mocks.finalizeReview.mockResolvedValue({ id: 'edited-decision', lmsExportState: 'PENDING' });
    fireEvent.click(screen.getAllByRole('button', { name: 'Утвердить' })[0]);
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Утвердить и отправить' }));
    await waitFor(() => expect(mocks.finalizeReview).toHaveBeenCalledWith('graded', 3, 'Уточнение преподавателя', 'ai-zero'));
  });

  it('leaves existing fields intact if an AI draft cannot acquire the claim', async () => {
    mocks.getAiCodeReview.mockResolvedValue({ id: 'ai-report', recommendationStatus: 'PENDING', enabled: true, state: 'COMPLETED', summary: '', findings: [], recommendedGrade: 4, recommendedComment: 'Совет' });
    mocks.claimSubmission.mockRejectedValue(new Error('Работа закреплена за другим преподавателем'));
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Утвердить рекомендацию ИИ' }));
    await screen.findByText('Не удалось подставить рекомендацию');
    expect(screen.getByLabelText(/Итоговый балл/)).toHaveValue(9);
    expect(screen.getByLabelText('Финальный комментарий студенту')).toHaveValue('Хорошая работа');
    expect(mocks.saveReview).not.toHaveBeenCalled();
    expect(mocks.finalizeReview).not.toHaveBeenCalled();
  });

  it('tags a saved AI draft for discard on exit without approving or exporting it', async () => {
    mocks.getAiCodeReview.mockResolvedValue({ id: 'ai-report', recommendationStatus: 'PENDING', enabled: true, state: 'COMPLETED', summary: '', findings: [], recommendedGrade: 4, recommendedComment: 'Совет' });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Утвердить рекомендацию ИИ' }));
    await waitFor(() => expect(screen.getByLabelText(/Итоговый балл/)).toHaveValue(4));
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить' }));
    await waitFor(() => expect(mocks.saveReview).toHaveBeenCalledWith('graded', 4, 'Совет', 'ai-report'));
    fireEvent.click(screen.getByRole('button', { name: 'Вернуться к работам' }));
    await screen.findByText('Список работ');
    expect(mocks.discardAiCodeReview).toHaveBeenCalledWith('graded', 'ai-report');
    expect(mocks.finalizeReview).not.toHaveBeenCalled();
  });

  it('restores a saved AI draft as editable when switching back to an already graded question', async () => {
    mocks.getReviewDraft.mockResolvedValue({ grade: 4, comment: 'Сохранённый ИИ-черновик', aiReviewId: 'ai-report' });
    renderPage();
    await waitFor(() => expect(screen.getByLabelText(/Итоговый балл/)).toHaveValue(4));
    expect(screen.getByLabelText('Комментарий студенту')).toHaveValue('Сохранённый ИИ-черновик');
    expect(screen.getByLabelText('Комментарий студенту')).toBeEnabled();
    expect(mocks.claimSubmission).toHaveBeenCalledWith('graded');
    fireEvent.click(screen.getByRole('button', { name: 'Вернуться к работам' }));
    await screen.findByText('Список работ');
    expect(mocks.discardAiCodeReview).toHaveBeenCalledWith('graded', 'ai-report');
  });

  it('shows comment-only Moodle feedback without manufacturing a grade', async () => {
    mocks.getSubmission.mockResolvedValue({ ...submission, status: 'UNGRADED', score: undefined, latestDecision: undefined, decisionHistory: [], moodleFeedbackComment: 'Алгоритм неверный' });
    mocks.claimSubmission.mockResolvedValue({ id: 'claim-1', mine: true, ownerName: 'Teacher' });
    renderPage();
    const feedback = await screen.findByRole('region', { name: 'Комментарий из Moodle' });
    expect(feedback).toHaveTextContent('Алгоритм неверный');
    expect(screen.getByLabelText(/Итоговый балл/)).toHaveValue(null);
    expect(mocks.startAiCodeReview).not.toHaveBeenCalled();
  });

  it('attaches a finding and its code without inventing a question, calling the model or changing the grade', async () => {
    mocks.getAiCodeReview.mockResolvedValue({ enabled: true, state: 'COMPLETED', summary: '', findings: [{ path: 'main.cpp', line: 1, severity: 'warning', message: 'Проверьте пустой массив.' }] });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Спросить о замечании' }));
    expect(screen.getByRole('tab', { name: 'ИИ' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('textbox', { name: 'Вопрос ИИ' })).toHaveValue('');
    expect(screen.getByRole('complementary', { name: 'Прикреплённый код' })).toHaveTextContent('Проверьте пустой массив.');
    expect(screen.getByRole('complementary', { name: 'Прикреплённый код' })).toHaveTextContent('int main() {}');
    expect(mocks.createTeacherAiThread).not.toHaveBeenCalled();
    expect(mocks.sendAiMessageStream).not.toHaveBeenCalled();
    expect(mocks.saveReview).not.toHaveBeenCalled();
  });

  it('opens selection drafts only for the original and preserves drafts when expanding or switching tabs', async () => {
    const { container } = renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Спросить о выделении' }));
    const input = screen.getByRole('textbox', { name: 'Вопрос ИИ' });
    expect(input).toHaveValue('');
    expect(screen.getByRole('complementary', { name: 'Прикреплённый код' })).toHaveTextContent('main.cpp');
    fireEvent.change(input, { target: { value: 'Мой уточнённый вопрос' } });
    fireEvent.click(screen.getByRole('button', { name: 'Развернуть чат' }));
    expect(container.querySelector('.review-layout')).toHaveClass('review-layout--ai-expanded');
    fireEvent.click(screen.getByRole('button', { name: 'Свернуть чат' }));
    expect(container.querySelector('.review-layout')).not.toHaveClass('review-layout--ai-expanded');
    fireEvent.click(screen.getByRole('tab', { name: 'Задание' }));
    fireEvent.click(screen.getByRole('tab', { name: 'ИИ' }));
    expect(screen.getByRole('textbox', { name: 'Вопрос ИИ' })).toHaveValue('Мой уточнённый вопрос');
    expect(screen.getByRole('complementary', { name: 'Прикреплённый код' })).toHaveTextContent('int main() {}');
    fireEvent.click(screen.getByRole('button', { name: 'Открыть преподавательскую песочницу' }));
    await screen.findByRole('button', { name: 'Закрыть преподавательскую песочницу' });
    expect(screen.queryByRole('button', { name: 'Спросить о выделении' })).not.toBeInTheDocument();
    expect(mocks.sendAiMessageStream).not.toHaveBeenCalled();
  });

  it('offers manual analysis without the old automatic review panel', async () => {
    renderPage();
    const start = await screen.findByRole('button', { name: 'Проверить работу через ИИ' });
    await waitFor(() => expect(start).toBeEnabled());
    expect(mocks.startAiCodeReview).not.toHaveBeenCalled();
    fireEvent.click(start);
    await waitFor(() => expect(mocks.startAiCodeReview).toHaveBeenCalledExactlyOnceWith('graded', false));
    expect(screen.getByRole('button', { name: 'ИИ-проверка выполняется' })).toBeDisabled();
    fireEvent.click(screen.getByRole('tab', { name: 'ИИ' }));
    expect(screen.getByRole('region', { name: 'Помощник проверки' })).toBeInTheDocument();
    expect(screen.queryByText('Автоматическая ИИ-проверка')).not.toBeInTheDocument();
    expect(screen.queryByText(/Администратор может включить/)).not.toBeInTheDocument();
  });

  it('shows an error for a failed manual request without changing a grade', async () => {
    mocks.startAiCodeReview.mockRejectedValue(new Error('ИИ временно недоступен'));
    renderPage();
    const start = await screen.findByRole('button', { name: 'Проверить работу через ИИ' });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    expect(await screen.findByText('ИИ-проверка не запущена')).toBeInTheDocument();
    expect(mocks.saveReview).not.toHaveBeenCalled();
    expect(mocks.finalizeReview).not.toHaveBeenCalled();
  });

  it('shows cached AI recommendations and lets the teacher turn code highlights off', async () => {
    mocks.getAiCodeReview.mockResolvedValue({
      enabled: true, state: 'COMPLETED', summary: 'Проверьте граничные случаи.', model: 'test-model',
      findings: [{ path: 'main.cpp', line: 1, severity: 'warning', message: 'Нет обработки пустого массива.' }],
    });
    renderPage();
    const indicator = await screen.findByRole('switch', { name: 'Подсветка замечаний ИИ' });
    await waitFor(() => expect(indicator).toBeEnabled());
    expect(screen.getByRole('tab', { name: 'Задание' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-ai-findings', '1');
    fireEvent.click(indicator);
    expect(screen.getByRole('tab', { name: 'Задание' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-ai-findings', '0');
    expect(mocks.startAiCodeReview).not.toHaveBeenCalled();
    expect(mocks.saveReview).not.toHaveBeenCalled();
    expect(mocks.finalizeReview).not.toHaveBeenCalled();
  });

  it('keeps code and grading usable while the background AI report is loading', async () => {
    mocks.getAiCodeReview.mockReturnValue(new Promise(() => undefined));
    renderPage();
    expect(await screen.findByLabelText('Код main.cpp')).toBeInTheDocument();
    expect(screen.getByText('Оценка и комментарий')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Компилировать и запустить код' })).toBeEnabled();
    expect(mocks.startAiCodeReview).not.toHaveBeenCalled();
  });

  it('does not transfer delayed AI findings to another question', async () => {
    mockUngradedQuestionGroup();
    let resolveFirst!: (value: unknown) => void;
    mocks.getAiCodeReview.mockImplementation((id: string) => id === 'question-1'
      ? new Promise((resolve) => { resolveFirst = resolve; })
      : Promise.resolve({ enabled: false, state: 'DISABLED', summary: '', findings: [] }));
    renderPage('/review/question-1');
    await screen.findByText('Задание 1 из 2');
    fireEvent.click(screen.getByRole('button', { name: 'Следующее задание' }));
    await screen.findByText('Задание 2 из 2');
    await act(async () => { resolveFirst({ enabled: true, state: 'COMPLETED', summary: 'Старый отчёт', findings: [{ path: 'main.cpp', line: 1, severity: 'warning', message: 'Старая ошибка' }] }); });
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-ai-findings', '0');
    expect(screen.queryByRole('button', { name: 'ИИ: замечаний 1' })).not.toBeInTheDocument();
  });

  it('does not attach code or a question from the previous submission after switching tasks', async () => {
    mockUngradedQuestionGroup();
    renderPage('/review/question-1');
    fireEvent.click(await screen.findByRole('button', { name: 'Спросить о выделении' }));
    expect(screen.getByRole('complementary', { name: 'Прикреплённый код' })).toHaveTextContent('return 1;');
    fireEvent.change(screen.getByRole('textbox', { name: 'Вопрос ИИ' }), { target: { value: 'Вопрос о первом задании' } });
    fireEvent.click(screen.getByRole('button', { name: 'Следующее задание' }));
    await screen.findByText('Задание 2 из 2');
    fireEvent.click(screen.getByRole('tab', { name: 'ИИ' }));
    expect(screen.getByRole('textbox', { name: 'Вопрос ИИ' })).toHaveValue('');
    expect(screen.queryByRole('complementary', { name: 'Прикреплённый код' })).not.toBeInTheDocument();
    expect(mocks.sendAiMessageStream).not.toHaveBeenCalled();
  });

  it('opens the exact task by default without requesting or generating hidden tests', async () => {
    renderPage();
    expect(await screen.findByRole('region', { name: 'Задание студента' })).toHaveTextContent(submission.taskStatement!);
    expect(screen.getByRole('tab', { name: 'Задание' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByRole('tab', { name: 'Тесты' })).not.toBeInTheDocument();
    expect(mocks.getEvidenceRuns).not.toHaveBeenCalled();
  });

  it('shows source warnings for the opened submission, with a safe Moodle link', async () => {
    mocks.getSubmission.mockResolvedValue({ ...submission, sourceWarnings: [{
      code: 'ARCHIVE_SOURCE_OMITTED', message: 'В архиве нет исходников C/C++.',
      moodleUrl: 'https://moodle.test/mod/quiz/review.php?attempt=9',
    }] });
    renderPage();
    expect(await screen.findByRole('alert')).toHaveTextContent('В архиве нет исходников C/C++.');
    expect(screen.getByRole('link', { name: 'Открыть ответ в Moodle' })).toHaveAttribute('rel', 'noopener noreferrer');
    fireEvent.click(screen.getByRole('tab', { name: 'История' }));
    expect(screen.getByRole('alert')).toHaveTextContent('ARCHIVE_SOURCE_OMITTED');
  });

  it('renders task markup as text and honestly reports a missing statement', async () => {
    mocks.getSubmission.mockResolvedValue({ ...submission, taskStatement: '<script>invalid()</script>' });
    const page = renderPage();
    expect(await screen.findByRole('region', { name: 'Задание студента' })).toHaveTextContent('<script>invalid()</script>');
    expect(document.querySelector('.review-task script')).toBeNull();
    page.unmount();
    mocks.getSubmission.mockResolvedValue({ ...submission, taskStatement: '' });
    renderPage();
    expect(await screen.findByRole('region', { name: 'Задание студента' })).toHaveTextContent('Текст задания не был получен из Moodle');
  });

  it.each(['PENDING', 'FAILED'])('refreshes %s delivery without reloading or changing the decision', async (state) => {
    vi.useFakeTimers();
    const pending = { ...decision, lmsExportState: state };
    mocks.getSubmission.mockResolvedValueOnce({ ...submission, latestDecision: pending, decisionHistory: [pending] });
    renderPage();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.queryByText('Оценка передана в Moodle')).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(screen.getByText('Оценка передана в Moodle')).toBeVisible();
    expect(screen.getByLabelText(/Итоговый балл/)).toHaveValue(9);
    expect(screen.getByLabelText('Финальный комментарий студенту')).toHaveValue(decision.comment);
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(mocks.getSubmission).toHaveBeenCalledTimes(2);
    expect(mocks.claimSubmission).not.toHaveBeenCalled();
  });

  it('ignores a late heartbeat failure after successful finalization without releasing twice', async () => {
    let rejectHeartbeat!: (reason: Error) => void;
    mocks.heartbeatClaim.mockReturnValue(new Promise((_resolve, reject) => { rejectHeartbeat = reject; }));
    mocks.finalizeReview.mockResolvedValue({ id: 'new-decision', grade: 9, comment: '', lmsExportState: 'PENDING' });
    mocks.getSubmission.mockResolvedValue({ ...submission, status: 'CLAIMED', claim: { id: 'claim-1', mine: true, ownerName: 'Teacher' } });
    renderPage();
    await screen.findByText('Вы проверяете работу');
    fireEvent.click(screen.getAllByRole('button', { name: 'Утвердить' })[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Утвердить и отправить' }));
    await screen.findByText('Оценка утверждена');
    await act(async () => { rejectHeartbeat(new Error('Резервирование работы больше не действует.')); });
    expect(screen.queryByText('Право на изменение проверки потеряно')).not.toBeInTheDocument();
    expect(mocks.releaseClaim).not.toHaveBeenCalled();
    expect(mocks.finalizeReview).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('location')).toHaveTextContent('/submissions?view=reviewed');
  });

  it('still reports an actual lost reservation during editing', async () => {
    mocks.getSubmission.mockResolvedValue({ ...submission, status: 'CLAIMED', claim: { id: 'claim-1', mine: true, ownerName: 'Teacher' } });
    mocks.heartbeatClaim.mockRejectedValue(new Error('Резервирование работы больше не действует.'));
    renderPage();
    expect(await screen.findByText('Право на изменение проверки потеряно')).toBeVisible();
    expect(screen.getByLabelText(/Итоговый балл/)).toBeDisabled();
  });

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
    fireEvent.click(screen.getByRole('tab', { name: 'История' }));

    expect(screen.getByText(/IP: 203\.0\.113\.42 · Google Chrome 140\.0/)).toBeInTheDocument();
    expect(screen.getByText(/Windows 10\/11 · компьютер/)).toBeInTheDocument();
  });

  it('opens the writing history in the right panel from the integrity card', async () => {
    mocks.getSubmission.mockResolvedValue({ ...submission, history: [{
      id: 'edit-1', type: 'edit', label: 'Изменение файла', filePath: 'src/main.cpp',
      at: '2026-08-25T09:45:12Z', revision: 1,
    }] });
    mocks.getAuthorshipAnalyses.mockResolvedValue([]);
    mocks.getSimilarityAnalyses.mockResolvedValue([]);
    renderPage();
    await screen.findByText('Работа проверена');
    fireEvent.click(screen.getByRole('tab', { name: 'Плагиат' }));
    fireEvent.click(await screen.findByRole('button', { name: /История написания/ }));
    expect(screen.getByRole('tab', { name: 'История' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('src/main.cpp')).toBeVisible();
    expect(screen.getByRole('button', { name: /История написания/ })).toHaveAttribute('aria-expanded', 'true');
  });

  it('switches between Moodle Quiz questions without returning to the review queue', async () => {
    const reviewGroup = {
      id: 'quiz-response-81', title: 'Самостоятельная работа №3',
      items: [
        { submissionId: 'question-1', position: 1, title: 'Строки', score: 3, maxScore: 3, status: 'GRADED' as const },
        { submissionId: 'question-2', position: 2, title: 'Массивы', score: 4, maxScore: 5, status: 'GRADED' as const },
      ],
    };
    const first = { ...submission, id: 'question-1', assessmentTitle: 'Строки', taskStatement: 'Посчитайте буквы.', score: 3, maxScore: 3, reviewGroup,
      sourceWarnings: [{ code: 'ARTIFACT_OMITTED', message: 'Не загружен первый ответ.' }] };
    const second = {
      ...submission, id: 'question-2', assessmentTitle: 'Массивы', score: 4, maxScore: 5, reviewGroup,
      taskStatement: 'Посчитайте элементы массива.',
      files: [{ id: 'arrays-main', path: 'main.cpp', content: 'int values[3];', language: 'cpp' }],
    };
    mocks.getSubmission.mockImplementation(async (id: string) => id === 'question-2' ? second : first);

    renderPage('/review/question-1?view=reviewed&q=quiz');

    const switcher = await screen.findByRole('navigation', { name: 'Задания в ответе студента' });
    expect(within(switcher).getByText('Самостоятельная работа №3')).toBeInTheDocument();
    expect(within(switcher).getByText('Задание 1 из 2')).toBeInTheDocument();
    expect(within(switcher).getByRole('tab', { name: /№1 Строки Балл: 3 \/ 3 Проверено/ })).toHaveAttribute('aria-selected', 'true');
    expect(within(switcher).getByRole('tab', { name: /№2 Массивы Балл: 4 \/ 5 Проверено/ })).toHaveAttribute('aria-selected', 'false');

    expect(screen.getByRole('region', { name: 'Задание студента' })).toHaveTextContent('Посчитайте буквы.');
    expect(screen.getByRole('alert')).toHaveTextContent('Не загружен первый ответ.');
    fireEvent.click(screen.getByRole('tab', { name: 'История' }));
    fireEvent.click(within(switcher).getByRole('tab', { name: /№2 Массивы/ }));

    await waitFor(() => expect(mocks.getSubmission).toHaveBeenLastCalledWith('question-2'));
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/review/question-2?view=reviewed&q=quiz'));
    const nextSwitcher = await screen.findByRole('navigation', { name: 'Задания в ответе студента' });
    expect(within(nextSwitcher).getByText('Задание 2 из 2')).toBeInTheDocument();
    expect(within(nextSwitcher).getByRole('tab', { name: /№2 Массивы/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByText('Список работ')).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Задание' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('region', { name: 'Задание студента' })).toHaveTextContent('Посчитайте элементы массива.');
    expect(screen.queryByText('Не загружен первый ответ.')).not.toBeInTheDocument();
  });

  it('runs each ungraded question in its own sandbox when switching there and back', async () => {
    mockUngradedQuestionGroup();
    mocks.saveExperimentFile.mockResolvedValue(1);
    renderPage('/review/question-1');
    await screen.findByText('Вы проверяете работу');
    fireEvent.click(screen.getByRole('button', { name: 'Открыть преподавательскую песочницу' }));
    await screen.findByRole('button', { name: 'Закрыть преподавательскую песочницу' });
    fireEvent.change(screen.getByLabelText('Код main.cpp'), { target: { value: 'int main() { return 7; }' } });
    fireEvent.click(screen.getByRole('button', { name: 'Компилировать и запустить код' }));
    await waitFor(() => expect(mocks.startInteractiveExperiment).toHaveBeenCalledWith('experiment-question-1', 1));

    fireEvent.click(screen.getByRole('tab', { name: /№2 Задача 2/ }));
    await waitFor(() => expect(screen.getByRole('tab', { name: /№2 Задача 2/ })).toHaveAttribute('aria-selected', 'true'));
    expect(screen.getByLabelText(/Итоговый балл/)).toHaveValue(null);
    expect(screen.getByLabelText('Код main.cpp')).toHaveValue('int main() { return 2; }');
    fireEvent.click(screen.getByRole('button', { name: 'Компилировать и запустить код' }));
    await waitFor(() => expect(mocks.startInteractiveExperiment).toHaveBeenCalledWith('experiment-question-2', 0));
    expect(mocks.resetExperiment).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('tab', { name: /№1 Задача 1/ }));
    await waitFor(() => expect(screen.getByRole('tab', { name: /№1 Задача 1/ })).toHaveAttribute('aria-selected', 'true'));
    expect(screen.getByLabelText(/Итоговый балл/)).toHaveValue(null);
    fireEvent.click(screen.getByRole('button', { name: 'Компилировать и запустить код' }));
    await waitFor(() => expect(mocks.startInteractiveExperiment).toHaveBeenLastCalledWith('experiment-question-1', 0));
    expect(mocks.createExperiment.mock.calls.map(([id]) => id)).toEqual(['question-1', 'question-2', 'question-1']);
    expect(mocks.releaseClaim.mock.calls.map(([id]) => id)).toEqual(['claim-question-1', 'claim-question-2']);
    expect(mocks.saveReview).not.toHaveBeenCalled();
    expect(mocks.finalizeReview).not.toHaveBeenCalled();
    expect(screen.queryByText('Запуск не выполнен')).not.toBeInTheDocument();
  });

  it('saves a comment without assigning a grade and restores the blank grade on return', async () => {
    mockUngradedQuestionGroup();
    const drafts = new Map<string, { grade: number | null; comment: string }>();
    mocks.saveReview.mockImplementation(async (id: string, grade: number | null, comment: string) => {
      drafts.set(id, { grade, comment });
    });
    mocks.getReviewDraft.mockImplementation(async (id: string) => drafts.get(id) ?? null);
    renderPage('/review/question-1');
    await screen.findByText('Вы проверяете работу');
    fireEvent.change(screen.getByLabelText(/^Комментарий студенту/), { target: { value: 'Проверить границы массива.' } });
    expect(screen.getByRole('button', { name: 'Сохранить' })).toBeEnabled();
    fireEvent.click(screen.getByRole('tab', { name: /№2 Задача 2/ }));
    await waitFor(() => expect(screen.getByRole('tab', { name: /№2 Задача 2/ })).toHaveAttribute('aria-selected', 'true'));
    expect(mocks.saveReview).toHaveBeenCalledWith('question-1', null, 'Проверить границы массива.');
    expect(screen.getByLabelText(/^Комментарий студенту/)).toHaveValue('');
    fireEvent.click(screen.getByRole('tab', { name: /№1 Задача 1/ }));
    await waitFor(() => expect(screen.getByRole('tab', { name: /№1 Задача 1/ })).toHaveAttribute('aria-selected', 'true'));
    expect(screen.getByLabelText(/Итоговый балл/)).toHaveValue(null);
    expect(screen.getByLabelText(/^Комментарий студенту/)).toHaveValue('Проверить границы массива.');
    expect(screen.getAllByRole('button', { name: 'Утвердить' }).every((button) => button.hasAttribute('disabled'))).toBe(true);
    expect(mocks.finalizeReview).not.toHaveBeenCalled();
  });

  it('waits for a pending start and stops it before releasing the previous question', async () => {
    const questions = mockUngradedQuestionGroup();
    let resolveCreate!: (value: unknown) => void;
    mocks.createExperiment.mockImplementationOnce(() => new Promise((resolve) => { resolveCreate = resolve; }));
    const runningSession = {
      sessionId: 'b'.repeat(32), status: 'RUNNING', terminal: false, durationMs: 1,
      stdout: '', stderr: '', outputTruncated: false, diagnostics: [],
    };
    mocks.startInteractiveExperiment.mockResolvedValue(runningSession);
    mocks.getInteractiveExperiment.mockResolvedValue(runningSession);
    renderPage('/review/question-1');
    await screen.findByText('Вы проверяете работу');
    fireEvent.click(screen.getByRole('button', { name: 'Компилировать и запустить код' }));
    await waitFor(() => expect(mocks.createExperiment).toHaveBeenCalledWith('question-1'));
    fireEvent.click(screen.getByRole('tab', { name: /№2 Задача 2/ }));
    await act(async () => { await Promise.resolve(); });
    expect(mocks.releaseClaim).not.toHaveBeenCalled();
    expect(mocks.getSubmission).toHaveBeenCalledTimes(1);
    await act(async () => { resolveCreate({
      id: 'experiment-question-1', submissionId: 'question-1', revision: 0, changed: false,
      files: questions[0].files, createdAt: '2026-08-25T12:00:00Z',
    }); });
    await waitFor(() => expect(screen.getByRole('tab', { name: /№2 Задача 2/ })).toHaveAttribute('aria-selected', 'true'));
    expect(mocks.stopInteractiveExperiment).toHaveBeenCalledWith('experiment-question-1', runningSession.sessionId);
    expect(mocks.stopInteractiveExperiment.mock.invocationCallOrder[0]).toBeLessThan(mocks.releaseClaim.mock.invocationCallOrder[0]);
    expect(window.sessionStorage.getItem('eduprog:interactive-review:question-2')).toBeNull();
    expect(screen.queryByText('Консоль программы')).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Итоговый балл/)).toHaveValue(null);
  });

  it('keeps the current claim when its running program cannot be stopped during a switch', async () => {
    mockUngradedQuestionGroup();
    const runningSession = {
      sessionId: 'c'.repeat(32), status: 'RUNNING', terminal: false, durationMs: 1,
      stdout: '', stderr: '', outputTruncated: false, diagnostics: [],
    };
    mocks.startInteractiveExperiment.mockResolvedValue(runningSession);
    mocks.getInteractiveExperiment.mockResolvedValue(runningSession);
    mocks.stopInteractiveExperiment.mockRejectedValue(new Error('Не удалось остановить программу.'));
    renderPage('/review/question-1');
    await screen.findByText('Вы проверяете работу');
    fireEvent.click(screen.getByRole('button', { name: 'Компилировать и запустить код' }));
    await screen.findByRole('button', { name: 'Остановить программу' });
    fireEvent.click(screen.getByRole('tab', { name: /№2 Задача 2/ }));
    expect(await screen.findByText('Задание не открыто')).toBeVisible();
    expect(mocks.releaseClaim).not.toHaveBeenCalled();
    expect(mocks.getSubmission).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('tab', { name: /№1 Задача 1/ })).toHaveAttribute('aria-selected', 'true');
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

    fireEvent.click(screen.getByRole('tab', { name: 'История' }));
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
    const compare = screen.getByRole('button', { name: 'Сравнить' });
    expect(compare).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(compare);
    expect(compare).toHaveAttribute('aria-pressed', 'true');
    expect(compare).toHaveAttribute('title', 'Сравнение включено. Нажмите ещё раз, чтобы вернуться к редактору.');

    const diff = screen.getByTestId('diff-editor');
    expect(diff).toHaveAttribute('data-original', 'student utils');
    expect(diff).toHaveAttribute('data-modified', 'teacher utils');
    expect(screen.getAllByText(/utils\.cpp/)).toHaveLength(2);
    fireEvent.click(compare);
    expect(compare).toHaveAttribute('aria-pressed', 'false');
    expect(screen.queryByTestId('diff-editor')).not.toBeInTheDocument();
    expect(screen.getByTestId('code-workspace')).toBeVisible();
    fireEvent.click(compare);
    fireEvent.click(screen.getByRole('button', { name: 'Закрыть преподавательскую песочницу' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть преподавательскую песочницу' }));
    expect(await screen.findByRole('button', { name: 'Сравнить' })).toHaveAttribute('aria-pressed', 'false');
  });

  it('keeps the file toggle in place and leaves a restoration control for the review panel', async () => {
    renderPage();
    await screen.findByText('Работа проверена');

    const hideFiles = screen.getByRole('button', { name: 'Скрыть файлы' });
    expect(hideFiles.closest('.workspace-files-rail')).not.toBeNull();
    expect(hideFiles).toHaveAttribute('aria-expanded', 'true');
    expect(hideFiles).toHaveAttribute('aria-controls', 'review-file-explorer');
    expect(document.getElementById('review-file-explorer')).toBeVisible();
    hideFiles.focus();
    fireEvent.click(hideFiles);
    expect(screen.getByRole('button', { name: 'Показать файлы' })).toBe(hideFiles);
    expect(hideFiles).toHaveAttribute('aria-expanded', 'false');
    expect(hideFiles).toHaveFocus();
    expect(document.getElementById('review-file-explorer')).not.toBeVisible();
    expect(screen.getByLabelText('Панель файлов')).toBeVisible();

    const hideReview = screen.getByRole('button', { name: 'Скрыть панель проверки' });
    expect(hideReview.closest('.review-side__top')).not.toBeNull();
    fireEvent.click(hideReview);
    expect(screen.getByRole('button', { name: 'Показать панель проверки' })).toBeInTheDocument();
    expect(screen.getByLabelText('Панель проверки скрыта')).toBeInTheDocument();
    expect(screen.queryByText('Оценка и комментарий')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Показать файлы' }));
    fireEvent.click(screen.getByRole('button', { name: 'Показать панель проверки' }));
    expect(screen.getByRole('button', { name: 'Скрыть файлы' })).toBe(hideFiles);
    expect(hideFiles).toHaveAttribute('aria-expanded', 'true');
    expect(document.getElementById('review-file-explorer')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Скрыть панель проверки' })).toBeInTheDocument();
  });

  it('preserves the selected file and its unsaved sandbox content when files are collapsed and restored', async () => {
    const files = [
      ...submission.files,
      { id: 'utils', path: 'utils.cpp', content: 'int answer() { return 41; }', language: 'cpp' },
    ];
    mocks.getSubmission.mockResolvedValue({ ...submission, files });
    mocks.createExperiment.mockResolvedValue({
      id: 'experiment-1', submissionId: 'graded', revision: 0, changed: false,
      files, createdAt: '2026-08-25T12:00:00Z',
    });
    mocks.saveExperimentFile.mockResolvedValue({
      id: 'experiment-1', submissionId: 'graded', revision: 1, changed: true,
      files, createdAt: '2026-08-25T12:00:00Z',
    });

    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть преподавательскую песочницу' }));
    await screen.findByRole('button', { name: 'Закрыть преподавательскую песочницу' });
    fireEvent.click(screen.getByRole('button', { name: 'utils.cpp' }));
    const editor = screen.getByRole('textbox', { name: 'Код utils.cpp' });
    const editedCode = 'int answer() { return 42; }';
    fireEvent.change(editor, { target: { value: editedCode } });

    fireEvent.click(screen.getByRole('button', { name: 'Скрыть файлы' }));
    expect(screen.getByRole('textbox', { name: 'Код utils.cpp' })).toBe(editor);
    expect(editor).toHaveValue(editedCode);
    expect(screen.queryByRole('button', { name: 'utils.cpp' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Показать файлы' }));
    expect(screen.getByRole('textbox', { name: 'Код utils.cpp' })).toBe(editor);
    expect(editor).toHaveValue(editedCode);
    expect(screen.getByRole('button', { name: 'utils.cpp' })).toBeVisible();
    expect(mocks.createExperiment).toHaveBeenCalledTimes(1);
    expect(mocks.resetExperiment).not.toHaveBeenCalled();
    expect(mocks.deleteExperiment).not.toHaveBeenCalled();
  });

  it('labels the similarity evidence tab as plagiarism', async () => {
    renderPage();
    await screen.findByText('Работа проверена');

    expect(screen.getByRole('tab', { name: 'Плагиат' })).toBeInTheDocument();
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
    expect(await screen.findByText('› 3')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Передать строку программе' })).toHaveTextContent('Отправить');
    expect(screen.queryByRole('button', { name: /EOF/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Остановить программу' }));
    await waitFor(() => expect(mocks.stopInteractiveExperiment).toHaveBeenCalledWith('experiment-1', 'b'.repeat(32)));
    await waitFor(() => expect(window.sessionStorage.getItem('eduprog:interactive-review:graded')).toBeNull());

    mocks.startInteractiveExperiment.mockResolvedValue(runningSession);
    fireEvent.click(screen.getByRole('button', { name: 'Компилировать и запустить код' }));
    await waitFor(() => expect(mocks.startInteractiveExperiment).toHaveBeenCalledTimes(2));
    expect(screen.queryByText('› 3')).not.toBeInTheDocument();
    await waitFor(() => expect(window.sessionStorage.getItem('eduprog:interactive-review:graded')).not.toBeNull());
    fireEvent.click(screen.getAllByRole('button', { name: 'Остановить программу' }).at(-1)!);
    await waitFor(() => expect(mocks.stopInteractiveExperiment).toHaveBeenCalledWith('experiment-1', 'b'.repeat(32)));
    await waitFor(() => expect(window.sessionStorage.getItem('eduprog:interactive-review:graded')).toBeNull());
  });

  it('keeps the entered values between output lines after the program finishes', async () => {
    let run = {
      sessionId: 'console-input-run', status: 'RUNNING', terminal: false, durationMs: 1,
      stdout: 'Введите размер массива\n', stderr: '', outputTruncated: false, diagnostics: [],
    };
    mocks.startInteractiveExperiment.mockResolvedValue(run);
    mocks.getInteractiveExperiment.mockImplementation(async () => run);
    mocks.sendInteractiveInput.mockImplementation(async () => {
      run = mocks.sendInteractiveInput.mock.calls.length === 1
        ? { ...run, stdout: run.stdout + 'Введите элементы массива\n' }
        : { ...run, status: 'SUCCESS', terminal: true, stdout: run.stdout + 'Итоговый массив: 1 2 3 4 5\n' };
      return run;
    });
    const { container } = renderPage();
    await screen.findByText('Работа проверена');
    fireEvent.click(screen.getByRole('button', { name: 'Компилировать и запустить код' }));
    await screen.findByText('Введите размер массива');
    const input = screen.getByLabelText('Ввод программы');
    fireEvent.change(input, { target: { value: '5' } });
    fireEvent.submit(input.closest('form')!);
    await screen.findByText('Введите элементы массива');
    fireEvent.change(input, { target: { value: '1 2 3 4 5' } });
    fireEvent.submit(input.closest('form')!);
    await screen.findByText('Итоговый массив: 1 2 3 4 5');
    expect([...container.querySelectorAll('.experiment-console__output pre')].map((entry) => entry.textContent)).toEqual([
      'Введите размер массива', '› 5', 'Введите элементы массива', '› 1 2 3 4 5', 'Итоговый массив: 1 2 3 4 5',
    ]);
    expect(input).toBeDisabled();
  });

  it('echoes only acknowledged input, prevents double sends and preserves the next typed line', async () => {
    const run = {
      sessionId: 'pending-input-run', status: 'RUNNING', terminal: false, durationMs: 1,
      stdout: 'Число: ', stderr: '', outputTruncated: false, diagnostics: [],
    };
    mocks.startInteractiveExperiment.mockResolvedValue(run);
    mocks.getInteractiveExperiment.mockResolvedValue(run);
    mocks.sendInteractiveInput.mockRejectedValueOnce(new Error('Не удалось отправить строку'));
    renderPage();
    await screen.findByText('Работа проверена');
    fireEvent.click(screen.getByRole('button', { name: 'Компилировать и запустить код' }));
    await screen.findByText('Число:');
    const input = screen.getByLabelText('Ввод программы');
    fireEvent.change(input, { target: { value: '3' } });
    fireEvent.submit(input.closest('form')!);
    await screen.findByText('Ввод не передан программе');
    expect(screen.queryByText('› 3')).not.toBeInTheDocument();
    expect(input).toHaveValue('3');
    let resolveInput!: (value: typeof run) => void;
    mocks.sendInteractiveInput.mockImplementation(() => new Promise((resolve) => { resolveInput = resolve; }));
    fireEvent.submit(input.closest('form')!);
    fireEvent.submit(input.closest('form')!);
    expect(mocks.sendInteractiveInput).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: 'Передать строку программе' })).toBeDisabled();
    fireEvent.change(input, { target: { value: '9' } });
    await act(async () => { resolveInput(run); });
    expect(screen.getAllByText('› 3')).toHaveLength(1);
    expect(input).toHaveValue('9');
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

    fireEvent.click(screen.getByRole('tab', { name: 'ИИ' }));
    expect(screen.getByRole('region', { name: 'Помощник проверки' })).toBeInTheDocument();
    expect(screen.queryByText(/Для вопросов закреплять работу за собой не нужно/)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Итоговый балл/)).toBeDisabled();
    expect(mocks.claimSubmission).not.toHaveBeenCalled();

    const input = await screen.findByPlaceholderText('Вопрос о текущем коде…');
    expect(screen.queryByRole('button', { name: 'Почему не проходит тест?' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Что проверить вручную?' })).not.toBeInTheDocument();
    await waitFor(() => expect(input).not.toBeDisabled());
    fireEvent.change(input, { target: { value: 'Что проверить вручную?' } });
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));

    await waitFor(() => expect(mocks.createTeacherAiThread).toHaveBeenCalledWith('graded', 'course-1'));
    await waitFor(() => expect(mocks.sendAiMessageStream).toHaveBeenCalledWith('teacher-thread-1', 'Что проверить вручную?', { teacherComment: 'Хорошая работа' }, expect.objectContaining({ onEvent: expect.any(Function), signal: expect.any(AbortSignal) })));
    expect(await screen.findByText('Проверьте граничные значения.')).toBeInTheDocument();
    expect(mocks.claimSubmission).not.toHaveBeenCalled();
    expect(screen.getByLabelText(/Итоговый балл/)).toBeDisabled();
  });

  it('uses a separate AI thread and comment for each reviewed question', async () => {
    const reviewGroup = {
      id: 'quiz-response-ai', title: 'Самостоятельная работа',
      items: [
        { submissionId: 'question-1', position: 1, title: 'Строки', maxScore: 5, status: 'GRADED' as const },
        { submissionId: 'question-2', position: 2, title: 'Массивы', maxScore: 5, status: 'GRADED' as const },
      ],
    };
    mocks.getSubmission.mockImplementation(async (id: string) => ({
      ...submission, id, reviewGroup,
      latestDecision: { ...decision, comment: `Комментарий ${id}` },
    }));
    mocks.createTeacherAiThread.mockImplementation(async (id: string) => ({ id: `thread-${id}` }));
    let finishFirst!: (value: { content: string; citations: [] }) => void;
    mocks.sendAiMessageStream.mockImplementationOnce(() => new Promise((resolve) => { finishFirst = resolve; }));
    renderPage('/review/question-1');
    await screen.findByRole('navigation', { name: 'Задания в ответе студента' });
    fireEvent.click(screen.getByRole('tab', { name: 'ИИ' }));
    await waitFor(() => expect(screen.getByPlaceholderText('Вопрос о текущем коде…')).not.toBeDisabled());
    fireEvent.change(screen.getByPlaceholderText('Вопрос о текущем коде…'), { target: { value: 'Первый вопрос' } });
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(mocks.sendAiMessageStream).toHaveBeenCalledWith('thread-question-1', 'Первый вопрос', { teacherComment: 'Комментарий question-1' }, expect.objectContaining({ onEvent: expect.any(Function), signal: expect.any(AbortSignal) })));

    fireEvent.click(screen.getByRole('tab', { name: /№2 Массивы/ }));
    await screen.findByText('Задание 2 из 2');
    fireEvent.click(screen.getByRole('tab', { name: 'ИИ' }));
    await waitFor(() => expect(screen.getByPlaceholderText('Вопрос о текущем коде…')).not.toBeDisabled());
    await act(async () => { finishFirst({ content: 'Ответ для первой задачи', citations: [] }); });
    expect(screen.queryByText('Первый вопрос')).not.toBeInTheDocument();
    expect(screen.queryByText('Ответ для первой задачи')).not.toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText('Вопрос о текущем коде…'), { target: { value: 'Второй вопрос' } });
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(mocks.sendAiMessageStream).toHaveBeenLastCalledWith('thread-question-2', 'Второй вопрос', { teacherComment: 'Комментарий question-2' }, expect.objectContaining({ onEvent: expect.any(Function), signal: expect.any(AbortSignal) })));
    expect(mocks.saveReview).not.toHaveBeenCalled();
  });

  it('includes the unsaved comment in chat without saving the review', async () => {
    mocks.getSubmission.mockResolvedValue({
      ...submission, status: 'CLAIMED', latestDecision: undefined, decisionHistory: [],
      claim: { id: 'claim-1', ownerId: 'teacher-1', ownerName: 'Учитель', expiresAt: '2099-01-01T00:00:00Z', mine: true },
    });
    renderPage();
    const comment = await screen.findByLabelText('Комментарий студенту');
    fireEvent.change(comment, { target: { value: 'Проверить пустой ввод' } });
    fireEvent.click(screen.getByRole('tab', { name: 'ИИ' }));
    await waitFor(() => expect(screen.getByPlaceholderText('Вопрос о текущем коде…')).not.toBeDisabled());
    fireEvent.change(screen.getByPlaceholderText('Вопрос о текущем коде…'), { target: { value: 'Верно ли замечание?' } });
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(mocks.sendAiMessageStream).toHaveBeenCalledWith('teacher-thread-1', 'Верно ли замечание?', { teacherComment: 'Проверить пустой ввод' }, expect.objectContaining({ onEvent: expect.any(Function), signal: expect.any(AbortSignal) })));
    expect(mocks.saveReview).not.toHaveBeenCalled();
  });
});
