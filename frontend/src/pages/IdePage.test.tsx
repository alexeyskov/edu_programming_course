import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { forwardRef, useImperativeHandle } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '../components/ui';
import type { Attempt, InteractiveRun } from '../types';
import { IdePage } from './IdePage';

const mocks = vi.hoisted(() => ({
  getAttempt: vi.fn(), getAttemptStatus: vi.fn(), getHistory: vi.fn(), resolveCourseId: vi.fn(),
  startAttempt: vi.fn(),
  startInteractiveAttempt: vi.fn(), getInteractiveAttempt: vi.fn(),
  sendInteractiveAttemptInput: vi.fn(), eofInteractiveAttempt: vi.fn(),
  stopInteractiveAttempt: vi.fn(), createFile: vi.fn(), saveFile: vi.fn(), submitAttempt: vi.fn(),
  retryAttemptSubmission: vi.fn(),
}));

const ApiErrorMock = vi.hoisted(() => class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
});

vi.mock('../lib/api', () => ({ api: mocks, ApiError: ApiErrorMock }));
vi.mock('../components/CodeWorkspace', () => ({
  CodeWorkspace: forwardRef((props: any, ref) => {
    useImperativeHandle(ref, () => ({ openDiagnostic: vi.fn(), focus: vi.fn() }));
    return <div data-testid="code-workspace" data-explorer-visible={String(props.explorerVisible)} data-read-only={String(props.readOnly)}><aside id={props.explorerId} hidden={!props.explorerVisible}>Список файлов</aside><span data-testid="editor-content">{props.files.find((file: any) => file.id === props.activeFileId)?.content}</span>Редактор<button aria-label="Изменить файл" disabled={props.readOnly} onClick={() => props.onChange('main', 'int main() { return 1; }', 'typing')}>edit</button><button aria-label="Внутренняя вставка" onClick={() => props.onChange('main', 'int main() {}int', 'internal_paste', 'receipt-1', { offset: 13, deleteCount: 0 })}>paste</button><button aria-label="Создать файл" onClick={props.onCreateFile}>+</button></div>;
  }),
}));

const attempt: Attempt = {
  id: 'attempt-1', assessmentId: 'assessment-1', title: 'Интерактивный ввод',
  statement: 'Прочитайте строки до EOF.', status: 'ACTIVE', revision: 0,
  acknowledgedRevision: 0, startedAt: '2026-08-26T10:00:00Z',
  deadlineAt: '2099-08-26T11:00:00Z', checkpointStatus: 'SYNCED',
  pastePolicy: 'ALLOW', aiEnabled: false, fileMode: 'SINGLE',
  files: [{ id: 'main', path: 'main.cpp', content: 'int main() {}', language: 'cpp' }],
};

function runningSession(id = 'a'.repeat(32)): InteractiveRun {
  return {
    sessionId: id, status: 'RUNNING', terminal: false, durationMs: 4,
    stdout: 'Введите число: ', stderr: '', outputTruncated: false,
    inputClosed: false, diagnostics: [],
  };
}

function renderPage() {
  return render(<MemoryRouter initialEntries={['/attempts/attempt-1']}><ToastProvider><Routes>
    <Route path="/attempts/:attemptId" element={<IdePage />} />
    <Route path="/assessments/:assessmentId" element={<p>Вернулись к работе</p>} />
  </Routes></ToastProvider></MemoryRouter>);
}

beforeEach(() => {
  window.sessionStorage.clear();
  Object.values(mocks).forEach((mock) => mock.mockReset());
  mocks.getAttempt.mockResolvedValue(attempt);
  mocks.startAttempt.mockResolvedValue(attempt);
  mocks.getAttemptStatus.mockResolvedValue({
    id: attempt.id, status: attempt.status, checkpointStatus: attempt.checkpointStatus,
  });
  mocks.getHistory.mockResolvedValue([]);
  mocks.resolveCourseId.mockResolvedValue('course-1');
  mocks.startInteractiveAttempt.mockResolvedValue(runningSession());
  mocks.getInteractiveAttempt.mockResolvedValue(runningSession());
  mocks.sendInteractiveAttemptInput.mockResolvedValue(runningSession());
  mocks.eofInteractiveAttempt.mockResolvedValue({
    ...runningSession(), inputClosed: true,
  });
  mocks.stopInteractiveAttempt.mockResolvedValue({
    ...runningSession(), status: 'STOPPED', terminal: true,
  });
  mocks.createFile.mockResolvedValue({
    file: { id: 'input', path: 'fixtures/input.txt', content: '', language: 'plaintext' },
    revision: 1,
  });
  mocks.saveFile.mockResolvedValue({ revision: 1 });
  mocks.submitAttempt.mockResolvedValue({ receipt_id: 'receipt-1' });
  mocks.retryAttemptSubmission.mockResolvedValue({ receipt_id: 'receipt-1' });
});

afterEach(() => { cleanup(); window.sessionStorage.clear(); vi.restoreAllMocks(); vi.useRealTimers(); });

describe('student interactive console', () => {
  it.each([true, false])('offers one clear completion action with hasTimeLimit=%s', async (hasTimeLimit) => {
    mocks.getAttempt.mockResolvedValue({ ...attempt, hasTimeLimit });
    renderPage();
    await screen.findByTestId('code-workspace');
    expect(screen.getByRole('button', { name: 'Завершить работу' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Сохранить и выйти' })).not.toBeInTheDocument();
    expect(screen.queryByText(/Сохранить и выйти/)).not.toBeInTheDocument();
    expect(mocks.submitAttempt).not.toHaveBeenCalled();
  });

  it.each([-4, 4])('uses backend time with a %s hour device offset, even after the device clock changes', async (offset) => {
    vi.useFakeTimers();
    const backend = Date.parse('2026-09-30T09:00:00Z');
    vi.setSystemTime(backend + offset * 3600_000);
    mocks.getAttempt.mockResolvedValue({
      ...attempt, serverNow: new Date(backend).toISOString(),
      deadlineAt: new Date(backend + 3600_000).toISOString(),
    });
    renderPage();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText('01:00:00')).toBeInTheDocument();
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-read-only', 'false');
    vi.setSystemTime(backend + 24 * 3600_000);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(screen.getByText('59:00')).toBeInTheDocument();
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-read-only', 'false');
    await act(async () => { await vi.advanceTimersByTimeAsync(59 * 60_000); });
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-read-only', 'true');
  });

  it('refreshes backend time when returning to the browser tab', async () => {
    vi.useFakeTimers();
    mocks.getAttempt.mockResolvedValue({
      ...attempt, serverNow: '2026-09-30T09:00:00Z', deadlineAt: '2026-09-30T10:00:00Z',
    });
    renderPage();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText('01:00:00')).toBeInTheDocument();
    mocks.getAttemptStatus.mockResolvedValue({
      id: attempt.id, status: 'ACTIVE', checkpointStatus: 'SYNCED', serverNow: '2026-09-30T09:30:00Z',
    });
    await act(async () => { fireEvent(document, new Event('visibilitychange')); });
    expect(screen.getByText('30:00')).toBeInTheDocument();
  });

  it('opens code without waiting for ancillary history or course lookups', async () => {
    mocks.getHistory.mockImplementation(() => new Promise(() => undefined));
    mocks.resolveCourseId.mockImplementation(() => new Promise(() => undefined));
    renderPage();
    expect(await screen.findByTestId('code-workspace')).toHaveAttribute('data-read-only', 'false');
    fireEvent.click(screen.getByRole('button', { name: 'Изменить файл' }));
    await waitFor(() => expect(mocks.saveFile).toHaveBeenCalledOnce());
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-read-only', 'false');
  });

  it('keeps the current code when reloading failed ancillary metadata', async () => {
    mocks.getHistory.mockRejectedValueOnce(new Error('history unavailable')).mockResolvedValueOnce([]);
    renderPage();
    await screen.findByTestId('code-workspace');
    fireEvent.click(screen.getByRole('button', { name: 'Изменить файл' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Повторить загрузку дополнительных данных' }));
    await waitFor(() => expect(mocks.getHistory).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(mocks.saveFile).toHaveBeenCalledOnce());
    expect(mocks.getAttempt).toHaveBeenCalledOnce();
    expect(mocks.saveFile).toHaveBeenCalledWith('attempt-1', expect.objectContaining({ content: 'int main() { return 1; }' }), 0, 'typing', undefined, undefined);
  });

  it('does not reopen an auto-submitted attempt when an earlier save acknowledgement arrives late', async () => {
    vi.useFakeTimers();
    let acknowledge!: (value: { revision: number }) => void;
    mocks.saveFile.mockReturnValue(new Promise((resolve) => { acknowledge = resolve; }));
    mocks.getAttemptStatus
      .mockResolvedValueOnce({ id: attempt.id, status: 'ACTIVE', checkpointStatus: 'SYNCED' })
      .mockResolvedValueOnce({ id: attempt.id, status: 'SUBMITTED', checkpointStatus: 'PENDING' })
      .mockImplementation(() => new Promise(() => undefined));
    renderPage();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    fireEvent.click(screen.getByRole('button', { name: 'Изменить файл' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(mocks.saveFile).toHaveBeenCalledOnce();
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-read-only', 'true');

    await act(async () => { acknowledge({ revision: 1 }); });

    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-read-only', 'true');
    expect(screen.getByRole('heading', { name: 'Передаём работу в Moodle…' })).toBeInTheDocument();
    expect(screen.queryByText('Работа сдана')).not.toBeInTheDocument();
  });

  it('does not interpret a stale ACTIVE periodic checkpoint as final delivery', async () => {
    mocks.getAttempt.mockResolvedValue({ ...attempt, status: 'SUBMITTED', checkpointStatus: 'PENDING' });
    mocks.getAttemptStatus.mockResolvedValue({ id: attempt.id, status: 'ACTIVE', checkpointStatus: 'SYNCED' });
    renderPage();
    await screen.findByTestId('code-workspace');
    expect(screen.getByRole('heading', { name: 'Передаём работу в Moodle…' })).toBeInTheDocument();
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-read-only', 'true');
    expect(screen.queryByText('Работа сдана')).not.toBeInTheDocument();
  });

  it('saves paste metadata separately from subsequent typing', async () => {
    mocks.saveFile.mockResolvedValueOnce({ revision: 1 }).mockResolvedValueOnce({ revision: 2 });
    renderPage(); await screen.findByTestId('code-workspace');
    fireEvent.click(screen.getByRole('button', { name: 'Внутренняя вставка' }));
    fireEvent.click(screen.getByRole('button', { name: 'Изменить файл' }));
    await waitFor(() => expect(mocks.saveFile).toHaveBeenCalledTimes(2));
    expect(mocks.saveFile).toHaveBeenNthCalledWith(1, 'attempt-1', expect.objectContaining({ content: 'int main() {}int' }), 0, 'internal_paste', 'receipt-1', { offset: 13, deleteCount: 0 });
    expect(mocks.saveFile).toHaveBeenNthCalledWith(2, 'attempt-1', expect.objectContaining({ content: 'int main() { return 1; }' }), 1, 'typing', undefined, undefined);
  });

  it('shows the reduced editing time without exposing the Moodle reserve notice', async () => {
    const now = Date.now();
    vi.spyOn(Date, 'now').mockReturnValue(now);
    vi.spyOn(performance, 'now').mockReturnValue(0);
    mocks.getAttempt.mockResolvedValue({
      ...attempt,
      serverNow: new Date(now).toISOString(),
      deadlineAt: new Date(now + 3000_000).toISOString(),
      expectedEndAt: new Date(now + 3300_000).toISOString(),
      moodleSyncTimeoutSeconds: 300,
    });
    renderPage();
    await screen.findByTestId('code-workspace');
    expect(screen.getByText('50:00')).toBeInTheDocument();
    expect(screen.queryByText('55:00')).not.toBeInTheDocument();
    expect(screen.queryByText('53:00')).not.toBeInTheDocument();
    expect(screen.queryByText(/Резерв на отправку в Moodle/)).not.toBeInTheDocument();
  });

  it('locks at the backend deadline and follows server auto-submission without a manual click', async () => {
    vi.useFakeTimers();
    const now = new Date('2026-09-10T10:00:00Z').getTime();
    vi.setSystemTime(now);
    mocks.getAttempt.mockResolvedValue({
      ...attempt, serverNow: new Date(now).toISOString(), deadlineAt: new Date(now + 10_000).toISOString(),
      expectedEndAt: new Date(now + 310_000).toISOString(), moodleSyncTimeoutSeconds: 300,
    });
    mocks.getAttemptStatus.mockImplementation(async () => ({
      id: attempt.id, status: Date.now() < now + 10_000 ? 'ACTIVE' : 'SUBMITTED',
      checkpointStatus: Date.now() < now + 10_000 ? 'SYNCED' : 'PENDING',
    }));
    renderPage();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-read-only', 'false');
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-read-only', 'true');
    expect(screen.getByRole('dialog')).toHaveTextContent('Передаём работу в Moodle');
    expect(mocks.submitAttempt).not.toHaveBeenCalled();
    expect(screen.queryByText('Работа сдана')).not.toBeInTheDocument();
  });

  it('still permits immediate manual submission before the reserved window', async () => {
    mocks.getAttempt.mockResolvedValue({ ...attempt, moodleSyncTimeoutSeconds: 300 });
    renderPage();
    await screen.findByTestId('code-workspace');
    fireEvent.click(screen.getByRole('button', { name: 'Завершить работу' }));
    fireEvent.click(screen.getByRole('button', { name: 'Сдать ревизию 0' }));
    await waitFor(() => expect(mocks.submitAttempt).toHaveBeenCalledTimes(1));
  });

  it('does not show an elapsed counter or submission countdown for an untimed lab', async () => {
    mocks.getAttempt.mockResolvedValue({ ...attempt, deadlineAt: undefined, hasTimeLimit: false });
    renderPage();
    await screen.findByTestId('code-workspace');
    expect(screen.getAllByText('Без таймера')).toHaveLength(2);
    expect(screen.queryByText(/В сессии ·/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Завершить работу' }));
    expect(screen.queryByText('Время в сессии')).not.toBeInTheDocument();
    expect(screen.queryByText('Осталось времени')).not.toBeInTheDocument();
  });

  it('does not mistake an unknown Moodle timer for an unlimited attempt', async () => {
    mocks.getAttempt.mockResolvedValue({ ...attempt, deadlineAt: undefined, hasTimeLimit: undefined });
    renderPage();
    await screen.findByTestId('code-workspace');
    expect(screen.queryByText('Без таймера')).not.toBeInTheDocument();
    expect(screen.getByText(/В сессии ·/)).toBeInTheDocument();
  });

  it('saves pending edits before completing the work, waiting for acknowledgement', async () => {
    let acknowledge!: (value: { revision: number }) => void;
    mocks.saveFile.mockReturnValue(new Promise((resolve) => { acknowledge = resolve; }));
    renderPage();
    await screen.findByTestId('code-workspace');
    fireEvent.click(screen.getByRole('button', { name: 'Изменить файл' }));
    fireEvent.click(screen.getByRole('button', { name: 'Завершить работу' }));
    fireEvent.click(screen.getByRole('button', { name: 'Сдать ревизию 0' }));
    expect(screen.queryByText('Вернулись к работе')).not.toBeInTheDocument();
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-read-only', 'true');
    expect(mocks.submitAttempt).not.toHaveBeenCalled();
    await act(async () => acknowledge({ revision: 1 }));
    expect(await screen.findByRole('heading', { name: 'Передаём работу в Moodle…' })).toBeInTheDocument();
    expect(mocks.saveFile).toHaveBeenCalledWith('attempt-1', expect.objectContaining({ content: 'int main() { return 1; }' }), 0, 'typing', undefined, undefined);
    expect(mocks.submitAttempt).toHaveBeenCalledWith('attempt-1', 1);
  });

  it('keeps the code and does not submit when saving before completion fails', async () => {
    mocks.saveFile.mockRejectedValue(new Error('Network unavailable'));
    renderPage();
    await screen.findByTestId('code-workspace');
    fireEvent.click(screen.getByRole('button', { name: 'Изменить файл' }));
    fireEvent.click(screen.getByRole('button', { name: 'Завершить работу' }));
    fireEvent.click(screen.getByRole('button', { name: 'Сдать ревизию 0' }));
    expect(await screen.findByText('Не удалось завершить работу')).toBeInTheDocument();
    expect(screen.queryByText('Вернулись к работе')).not.toBeInTheDocument();
    expect(mocks.submitAttempt).not.toHaveBeenCalled();
  });

  it('flushes edits still waiting for debounce when leaving the editor route', async () => {
    const page = renderPage();
    await screen.findByTestId('code-workspace');
    fireEvent.click(screen.getByRole('button', { name: 'Изменить файл' }));
    page.unmount();
    await waitFor(() => expect(mocks.saveFile).toHaveBeenCalledTimes(1));
    expect(mocks.submitAttempt).not.toHaveBeenCalled();
  });

  it('prepares an upgrading Moodle attempt before displaying its statement and editor', async () => {
    const unprepared = {
      ...attempt,
      statement: '',
      requiresLiveLmsPreparation: true,
    };
    const prepared = {
      ...attempt,
      statement: 'Реализуйте обработку массива.',
      requiresLiveLmsPreparation: false,
    };
    mocks.getAttempt
      .mockResolvedValueOnce(unprepared)
      .mockResolvedValueOnce(prepared);
    mocks.startAttempt.mockResolvedValue(prepared);

    renderPage();

    expect(await screen.findByText('Реализуйте обработку массива.')).toBeInTheDocument();
    expect(mocks.startAttempt).toHaveBeenCalledWith('assessment-1');
    expect(mocks.getAttempt).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-read-only', 'false');
  });

  it('warns instead of showing a missing attempt when live Moodle preparation finds it finalized', async () => {
    mocks.getAttempt.mockResolvedValue({
      ...attempt,
      requiresLiveLmsPreparation: true,
    });
    mocks.startAttempt.mockRejectedValue(new ApiErrorMock(
      409,
      'LMS_ATTEMPT_FINALIZED',
      'Attempt was finalized in Moodle',
    ));

    renderPage();

    expect(await screen.findByRole('heading', { name: 'Сеанс работы завершён через Moodle' })).toBeInTheDocument();
    expect(screen.getByText(/система не пыталась перезаписать ответ/i)).toBeInTheDocument();
    expect(screen.queryByText('Попытка не найдена')).not.toBeInTheDocument();
    expect(screen.queryByTestId('code-workspace')).not.toBeInTheDocument();
  });

  it('uses explicit controls to hide and restore the condition and lower panel', async () => {
    renderPage();
    await screen.findByTestId('code-workspace');

    expect(screen.getByText('Прочитайте строки до EOF.')).toBeInTheDocument();
    const toggle = screen.getByRole('button', { name: 'Скрыть условие' });
    toggle.focus();
    fireEvent.click(toggle);
    expect(screen.getByText('Прочитайте строки до EOF.')).not.toBeVisible();
    expect(screen.getByText('Задание')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Показать условие' })).toBe(toggle);
    expect(toggle).toHaveFocus();
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveAttribute('aria-controls', 'attempt-condition');
    fireEvent.click(toggle);
    expect(screen.getByText('Прочитайте строки до EOF.')).toBeVisible();

    expect(screen.getByText('Проблем не найдено')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Закрыть' }));
    expect(screen.queryByText('Проблем не найдено')).not.toBeInTheDocument();
    expect(screen.getByText('Консоль скрыта')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Открыть консоль' }));
    expect(screen.getByText('Проблем не найдено')).toBeInTheDocument();
  });

  it('automatically restores the console when the program starts', async () => {
    renderPage();
    await screen.findByTestId('code-workspace');
    fireEvent.click(screen.getByRole('button', { name: 'Закрыть' }));

    fireEvent.click(screen.getByRole('button', { name: 'Запустить' }));

    expect(await screen.findByText('Введите число:')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Закрыть' })).toHaveAttribute('aria-expanded', 'true');
  });

  it('keeps run at the bottom left of close, separate from submission', async () => {
    renderPage();
    await screen.findByTestId('code-workspace');
    const run = screen.getByRole('button', { name: 'Запустить' });
    const close = screen.getByRole('button', { name: 'Закрыть' });
    expect(run.nextElementSibling).toBe(close);
    expect(run.closest('.bottom-panel')).not.toBeNull();
    expect(run.closest('.ide-toolbar')).toBeNull();
    expect(screen.getByRole('button', { name: 'Завершить работу' }).closest('.ide-toolbar')).not.toBeNull();
    fireEvent.click(close);
    expect(screen.getByRole('button', { name: 'Запустить' })).toBe(run);
  });

  it('hides files without remounting the editor or losing unsaved code', async () => {
    renderPage();
    const editor = await screen.findByTestId('code-workspace');
    fireEvent.click(screen.getByRole('button', { name: 'Изменить файл' }));
    const toggle = screen.getByRole('button', { name: 'Скрыть файлы' });
    toggle.focus();
    fireEvent.click(toggle);
    expect(screen.getByRole('button', { name: 'Показать файлы' })).toBe(toggle);
    expect(toggle).toHaveFocus();
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveAttribute('aria-controls', 'student-file-explorer');
    expect(screen.getByText('Список файлов')).not.toBeVisible();
    expect(screen.getByTestId('code-workspace')).toBe(editor);
    expect(screen.getByTestId('editor-content')).toHaveTextContent('int main() { return 1; }');
    fireEvent.click(toggle);
    expect(screen.getByText('Список файлов')).toBeVisible();
    expect(screen.getByTestId('code-workspace')).toBe(editor);
    expect(screen.getByTestId('editor-content')).toHaveTextContent('int main() { return 1; }');
  });

  it('starts once, sends Enter-delimited lines and supports stop', async () => {
    renderPage();
    expect((await screen.findAllByText('Интерактивный ввод')).length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole('button', { name: 'Запустить' }));
    await waitFor(() => expect(mocks.startInteractiveAttempt).toHaveBeenCalledWith('attempt-1', 0));
    expect(await screen.findByText('Введите число:')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Запустить' })).not.toBeInTheDocument();

    const input = screen.getByLabelText('Ввод программы');
    fireEvent.change(input, { target: { value: '42' } });
    fireEvent.submit(input.closest('form')!);
    await waitFor(() => expect(mocks.sendInteractiveAttemptInput).toHaveBeenCalledWith('attempt-1', 'a'.repeat(32), '42'));
    expect(screen.queryByText('› 42')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Передать строку программе' })).toHaveTextContent('Отправить');
    expect(screen.queryByRole('button', { name: /EOF/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getAllByRole('button', { name: 'Остановить' })[0]);
    await waitFor(() => expect(mocks.stopInteractiveAttempt).toHaveBeenCalledWith('attempt-1', 'a'.repeat(32)));
  });

  it('reconnects to the same live session after a page reload', async () => {
    window.sessionStorage.setItem('eduprog:interactive-attempt:attempt-1', 'b'.repeat(32));
    mocks.getInteractiveAttempt.mockResolvedValue(runningSession('b'.repeat(32)));
    renderPage();

    await waitFor(() => expect(mocks.getInteractiveAttempt).toHaveBeenCalledWith('attempt-1', 'b'.repeat(32)));
    expect((await screen.findAllByRole('button', { name: 'Остановить' })).length).toBeGreaterThan(0);
    expect(mocks.startInteractiveAttempt).not.toHaveBeenCalled();
  });

  it('uses a clear Moodle label for the latest server checkpoint', async () => {
    renderPage();
    expect((await screen.findAllByText('Интерактивный ввод')).length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole('button', { name: 'Завершить работу' }));

    expect(screen.getByText('Последнее сохранение в Moodle')).toBeInTheDocument();
    expect(screen.queryByText('LMS checkpoint')).not.toBeInTheDocument();
  });

  it('does not report success while the final Moodle checkpoint is pending', async () => {
    let submitted = false;
    mocks.submitAttempt.mockImplementation(async () => {
      submitted = true;
      return { receipt_id: 'receipt-1' };
    });
    mocks.getAttemptStatus.mockImplementation(async () => submitted ? {
      id: attempt.id,
      status: 'SUBMITTED',
      checkpointStatus: 'PENDING',
    } : {
      id: attempt.id,
      status: 'ACTIVE',
      checkpointStatus: 'SYNCED',
    });
    renderPage();
    await screen.findByTestId('code-workspace');

    fireEvent.click(screen.getByRole('button', { name: 'Завершить работу' }));
    fireEvent.click(screen.getByRole('button', { name: 'Сдать ревизию 0' }));

    expect(await screen.findByRole('heading', { name: 'Передаём работу в Moodle…' })).toBeInTheDocument();
    expect(screen.getByText(/успех будет показан только после загрузки ответа/i)).toBeInTheDocument();
    expect(screen.queryByText('Работа сдана')).not.toBeInTheDocument();
  });

  it('explains that a slow Moodle confirmation continues safely in the background', async () => {
    let submitted = false;
    const timeoutSpy = vi.spyOn(window, 'setTimeout');
    mocks.submitAttempt.mockImplementation(async () => {
      submitted = true;
      return { receipt_id: 'receipt-1' };
    });
    mocks.getAttemptStatus.mockImplementation(async () => submitted ? {
      id: attempt.id,
      status: 'SUBMITTED',
      checkpointStatus: 'PENDING',
    } : {
      id: attempt.id,
      status: 'ACTIVE',
      checkpointStatus: 'SYNCED',
    });
    renderPage();
    await screen.findByTestId('code-workspace');

    fireEvent.click(screen.getByRole('button', { name: 'Завершить работу' }));
    fireEvent.click(screen.getByRole('button', { name: 'Сдать ревизию 0' }));
    await screen.findByRole('heading', { name: 'Передаём работу в Moodle…' });
    const slowTimer = timeoutSpy.mock.calls.find(([, delay]) => delay === 12_000)?.[0];
    expect(typeof slowTimer).toBe('function');

    act(() => { if (typeof slowTimer === 'function') slowTimer(); });

    expect(screen.getByRole('heading', { name: 'Сдача продолжается в фоне' })).toBeInTheDocument();
    expect(screen.getByText(/финальная версия сохранена в системе/i)).toBeInTheDocument();
    expect(screen.getByText(/можно вернуться к списку/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Вернуться к работам' })).toBeInTheDocument();
  });

  it('reports success only after Moodle confirms the final checkpoint', async () => {
    let submitted = false;
    mocks.submitAttempt.mockImplementation(async () => {
      submitted = true;
      return { receipt_id: 'receipt-1' };
    });
    mocks.getAttemptStatus.mockImplementation(async () => submitted ? {
      id: attempt.id,
      status: 'SUBMITTED',
      checkpointStatus: 'SYNCED',
    } : {
      id: attempt.id,
      status: 'ACTIVE',
      checkpointStatus: 'SYNCED',
    });
    renderPage();
    await screen.findByTestId('code-workspace');

    fireEvent.click(screen.getByRole('button', { name: 'Завершить работу' }));
    fireEvent.click(screen.getByRole('button', { name: 'Сдать ревизию 0' }));

    expect(await screen.findByText('Moodle подтвердил получение ответа и завершение попытки.')).toBeInTheDocument();
    expect(screen.getByText('Работа сдана')).toBeInTheDocument();
  });

  it('offers retry when Moodle rejects the final checkpoint', async () => {
    let submitted = false;
    let retried = false;
    mocks.submitAttempt.mockImplementation(async () => {
      submitted = true;
      return { receipt_id: 'receipt-1' };
    });
    mocks.retryAttemptSubmission.mockImplementation(async () => {
      retried = true;
      return { receipt_id: 'receipt-1' };
    });
    mocks.getAttemptStatus.mockImplementation(async () => submitted ? {
      id: attempt.id,
      status: 'SUBMITTED',
      checkpointStatus: retried ? 'PENDING' : 'ERROR',
    } : {
      id: attempt.id,
      status: 'ACTIVE',
      checkpointStatus: 'SYNCED',
    });
    renderPage();
    await screen.findByTestId('code-workspace');

    fireEvent.click(screen.getByRole('button', { name: 'Завершить работу' }));
    fireEvent.click(screen.getByRole('button', { name: 'Сдать ревизию 0' }));
    expect(await screen.findByRole('heading', { name: 'Moodle не подтвердил сдачу' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Повторить отправку' }));
    await waitFor(() => expect(mocks.retryAttemptSubmission).toHaveBeenCalledWith('attempt-1'));
    expect(await screen.findByRole('heading', { name: 'Передаём работу в Moodle…' })).toBeInTheDocument();
  });

  it('allows adding a text data file in single-source mode', async () => {
    renderPage();
    await screen.findByTestId('code-workspace');

    fireEvent.click(screen.getByRole('button', { name: 'Создать файл' }));
    const path = screen.getByPlaceholderText('input.txt');
    fireEvent.change(path, { target: { value: 'fixtures/input.txt' } });
    fireEvent.click(screen.getByRole('button', { name: 'Создать' }));

    await waitFor(() => expect(mocks.createFile).toHaveBeenCalledWith(
      'attempt-1',
      'fixtures/input.txt',
      0,
    ));
  });

  it.each(['helper.cpp', 'include/value.hpp'])('allows creating %s for an archive answer', async (path) => {
    mocks.getAttempt.mockResolvedValue({ ...attempt, fileMode: 'MULTI' });
    mocks.createFile.mockResolvedValue({ file: { id: 'new-file', path, content: '', language: 'cpp' }, revision: 1 });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Создать файл' }));
    fireEvent.change(screen.getByPlaceholderText('solution.cpp'), { target: { value: path } });
    fireEvent.click(screen.getByRole('button', { name: 'Создать' }));
    await waitFor(() => expect(mocks.createFile).toHaveBeenCalledWith('attempt-1', path, 0));
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Новый файл' })).not.toBeInTheDocument());
  });

  it('does not allow a second source file for an online-text answer', async () => {
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Создать файл' }));
    fireEvent.change(screen.getByPlaceholderText('input.txt'), { target: { value: 'helper.cpp' } });
    fireEvent.click(screen.getByRole('button', { name: 'Создать' }));
    expect(await screen.findByText('В однофайловом режиме можно добавлять только текстовые файлы .txt')).toBeInTheDocument();
    expect(mocks.createFile).not.toHaveBeenCalled();
  });

  it('locks the IDE and warns when status polling reports completion through Moodle', async () => {
    mocks.getAttemptStatus.mockResolvedValue({
      id: attempt.id,
      status: 'LOCKED',
      closureReason: 'LMS_ATTEMPT_FINALIZED',
      checkpointStatus: 'ERROR',
    });

    renderPage();

    expect(await screen.findByRole('heading', { name: 'Сеанс работы завершён через Moodle' })).toBeInTheDocument();
    expect(screen.getByText(/система не пыталась перезаписать ответ/i)).toBeInTheDocument();
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-read-only', 'true');
    expect(mocks.saveFile).not.toHaveBeenCalled();
    expect(mocks.submitAttempt).not.toHaveBeenCalled();
  });

  it('opens a deleted Moodle attempt read-only with no resubmission or runtime recovery', async () => {
    window.sessionStorage.setItem('eduprog:interactive-attempt:attempt-1', 'b'.repeat(32));
    mocks.getAttempt.mockResolvedValue({
      ...attempt, status: 'LOCKED', closureReason: 'LMS_ATTEMPT_DELETED',
      checkpointStatus: 'ERROR',
    });
    renderPage();

    expect(await screen.findByRole('heading', { name: 'Попытка удалена в Moodle' })).toBeInTheDocument();
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-read-only', 'true');
    expect(screen.getByText(/сохранённый код остался в системе/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Повторить отправку' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Вернуться к работам' })).toBeInTheDocument();
    expect(mocks.getInteractiveAttempt).not.toHaveBeenCalled();
    expect(mocks.startAttempt).not.toHaveBeenCalled();
  });

  it('does not interpret deletion during final delivery as a successful submission', async () => {
    mocks.getAttempt.mockResolvedValue({ ...attempt, status: 'SUBMITTED', checkpointStatus: 'PENDING' });
    mocks.getAttemptStatus.mockResolvedValue({
      id: attempt.id, status: 'LOCKED', closureReason: 'LMS_ATTEMPT_DELETED',
      checkpointStatus: 'SYNCED',
    });
    renderPage();

    expect(await screen.findByRole('heading', { name: 'Попытка удалена в Moodle' })).toBeInTheDocument();
    expect(screen.queryByText('Работа сдана')).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Передаём работу в Moodle…' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Повторить отправку' })).not.toBeInTheDocument();
    expect(mocks.retryAttemptSubmission).not.toHaveBeenCalled();
  });

  it('keeps polling failed delivery so a deleted attempt does not offer retry forever', async () => {
    mocks.getAttempt.mockResolvedValue({ ...attempt, status: 'SUBMITTED', checkpointStatus: 'ERROR' });
    mocks.getAttemptStatus.mockResolvedValue({
      id: attempt.id, status: 'LOCKED', closureReason: 'LMS_ATTEMPT_DELETED',
      checkpointStatus: 'ERROR',
    });
    renderPage();

    expect(await screen.findByRole('heading', { name: 'Попытка удалена в Moodle' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Повторить отправку' })).not.toBeInTheDocument();
  });

  it('ignores an old successful status response after retry confirms deletion', async () => {
    let acknowledge!: (value: object) => void;
    mocks.getAttempt.mockResolvedValue({ ...attempt, status: 'SUBMITTED', checkpointStatus: 'ERROR' });
    mocks.getAttemptStatus.mockReturnValue(new Promise((resolve) => { acknowledge = resolve; }));
    mocks.retryAttemptSubmission.mockRejectedValue(new ApiErrorMock(
      409, 'LMS_ATTEMPT_DELETED', 'Attempt deleted in Moodle',
    ));
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Повторить отправку' }));
    await screen.findByRole('heading', { name: 'Попытка удалена в Moodle' });
    await act(async () => {
      acknowledge({ id: attempt.id, status: 'SUBMITTED', checkpointStatus: 'SYNCED' });
    });

    expect(screen.getByRole('heading', { name: 'Попытка удалена в Moodle' })).toBeInTheDocument();
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-read-only', 'true');
    expect(screen.queryByText('Работа сдана')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Повторить отправку' })).not.toBeInTheDocument();
  });

  it('shows deletion reported while loading without a generic reload loop', async () => {
    mocks.getAttempt.mockRejectedValue(new ApiErrorMock(
      409, 'LMS_ATTEMPT_DELETED', 'Attempt deleted in Moodle',
    ));
    renderPage();
    expect(await screen.findByRole('heading', { name: 'Попытка удалена в Moodle' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Вернуться к работам' })).toBeInTheDocument();
    expect(screen.queryByText('Не получилось загрузить данные')).not.toBeInTheDocument();
  });

  it('stops a late runner startup after polling confirms the Moodle attempt was deleted', async () => {
    vi.useFakeTimers();
    let started!: (value: InteractiveRun) => void;
    mocks.startInteractiveAttempt.mockReturnValue(new Promise((resolve) => { started = resolve; }));
    mocks.getAttemptStatus
      .mockResolvedValueOnce({ id: attempt.id, status: 'ACTIVE', checkpointStatus: 'SYNCED' })
      .mockResolvedValue({
        id: attempt.id, status: 'LOCKED', closureReason: 'LMS_ATTEMPT_DELETED',
        checkpointStatus: 'ERROR',
      });
    renderPage();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    fireEvent.click(screen.getByRole('button', { name: 'Запустить' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(screen.getByRole('heading', { name: 'Попытка удалена в Moodle' })).toBeInTheDocument();

    await act(async () => { started(runningSession()); });

    expect(mocks.stopInteractiveAttempt).toHaveBeenCalledWith('attempt-1', 'a'.repeat(32));
    expect(screen.queryByRole('button', { name: 'Остановить' })).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem('eduprog:interactive-attempt:attempt-1')).toBeNull();
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-read-only', 'true');
  });

  it('treats LMS finalization during autosave as terminal instead of a revision conflict', async () => {
    mocks.saveFile.mockRejectedValue(new ApiErrorMock(
      409,
      'LMS_ATTEMPT_FINALIZED',
      'Attempt was finalized in Moodle',
    ));
    renderPage();
    await screen.findByTestId('code-workspace');

    fireEvent.click(screen.getByRole('button', { name: 'Изменить файл' }));

    expect(await screen.findByRole('heading', { name: 'Сеанс работы завершён через Moodle' }, { timeout: 2_000 })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Конфликт ревизии' })).not.toBeInTheDocument();
    expect(mocks.saveFile).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-read-only', 'true');
  });

  it('shows the revision conflict dialog only for REVISION_CONFLICT', async () => {
    mocks.saveFile.mockRejectedValue(new ApiErrorMock(
      409,
      'REVISION_CONFLICT',
      'Workspace revision is stale',
    ));
    renderPage();
    await screen.findByTestId('code-workspace');

    fireEvent.click(screen.getByRole('button', { name: 'Изменить файл' }));

    expect(await screen.findByRole('heading', { name: 'Конфликт ревизии' }, { timeout: 2_000 })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Сеанс работы завершён через Moodle' })).not.toBeInTheDocument();
  });
});
