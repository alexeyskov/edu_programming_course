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
    return <div data-testid="code-workspace" data-read-only={String(props.readOnly)}>Редактор<button aria-label="Изменить файл" disabled={props.readOnly} onClick={() => props.onChange('main', 'int main() { return 1; }', 'typing')}>edit</button><button aria-label="Внутренняя вставка" onClick={() => props.onChange('main', 'int main() {}int', 'internal_paste', 'receipt-1', { offset: 13, deleteCount: 0 })}>paste</button><button aria-label="Создать файл" onClick={props.onCreateFile}>+</button></div>;
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
  it('saves paste metadata separately from subsequent typing', async () => {
    mocks.saveFile.mockResolvedValueOnce({ revision: 1 }).mockResolvedValueOnce({ revision: 2 });
    renderPage(); await screen.findByTestId('code-workspace');
    fireEvent.click(screen.getByRole('button', { name: 'Внутренняя вставка' }));
    fireEvent.click(screen.getByRole('button', { name: 'Изменить файл' }));
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить и выйти' }));
    await screen.findByText('Вернулись к работе');
    expect(mocks.saveFile).toHaveBeenNthCalledWith(1, 'attempt-1', expect.objectContaining({ content: 'int main() {}int' }), 0, 'internal_paste', 'receipt-1', { offset: 13, deleteCount: 0 });
    expect(mocks.saveFile).toHaveBeenNthCalledWith(2, 'attempt-1', expect.objectContaining({ content: 'int main() { return 1; }' }), 1, 'typing', undefined, undefined);
  });

  it('shows only editing time with a separate Moodle upload reserve', async () => {
    const now = Date.now();
    vi.spyOn(Date, 'now').mockReturnValue(now);
    mocks.getAttempt.mockResolvedValue({
      ...attempt,
      deadlineAt: new Date(now + 600_000).toISOString(),
      expectedEndAt: new Date(now + 900_000).toISOString(),
      moodleSyncTimeoutSeconds: 300,
    });
    renderPage();
    await screen.findByTestId('code-workspace');
    expect(screen.getByText('10:00')).toBeInTheDocument();
    expect(screen.queryByText('15:00')).not.toBeInTheDocument();
    expect(screen.getByRole('note')).toHaveTextContent('Резерв на отправку в Moodle: 300 с. Он уже вычтен из таймера.');
  });

  it('locks at the local deadline and follows server auto-submission without a manual click', async () => {
    vi.useFakeTimers();
    const now = new Date('2026-09-10T10:00:00Z').getTime();
    vi.setSystemTime(now);
    mocks.getAttempt.mockResolvedValue({
      ...attempt, deadlineAt: new Date(now + 10_000).toISOString(),
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
    fireEvent.click(screen.getByRole('button', { name: 'Завершить' }));
    fireEvent.click(screen.getByRole('button', { name: 'Сдать ревизию 0' }));
    await waitFor(() => expect(mocks.submitAttempt).toHaveBeenCalledTimes(1));
  });

  it('does not show an elapsed counter or submission countdown for an untimed lab', async () => {
    mocks.getAttempt.mockResolvedValue({ ...attempt, deadlineAt: undefined, hasTimeLimit: false });
    renderPage();
    await screen.findByTestId('code-workspace');
    expect(screen.getAllByText('Без таймера')).toHaveLength(2);
    expect(screen.queryByText(/В сессии ·/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Завершить' }));
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

  it('saves and leaves without submitting the attempt, waiting for acknowledgement', async () => {
    let acknowledge!: (value: { revision: number }) => void;
    mocks.saveFile.mockReturnValue(new Promise((resolve) => { acknowledge = resolve; }));
    renderPage();
    await screen.findByTestId('code-workspace');
    fireEvent.click(screen.getByRole('button', { name: 'Изменить файл' }));
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить и выйти' }));
    expect(screen.queryByText('Вернулись к работе')).not.toBeInTheDocument();
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-read-only', 'true');
    await act(async () => acknowledge({ revision: 1 }));
    expect(await screen.findByText('Вернулись к работе')).toBeInTheDocument();
    expect(mocks.saveFile).toHaveBeenCalledWith('attempt-1', expect.objectContaining({ content: 'int main() { return 1; }' }), 0, 'typing', undefined, undefined);
    expect(mocks.submitAttempt).not.toHaveBeenCalled();
  });

  it('stays with the code when save-and-exit fails', async () => {
    mocks.saveFile.mockRejectedValue(new Error('Network unavailable'));
    renderPage();
    await screen.findByTestId('code-workspace');
    fireEvent.click(screen.getByRole('button', { name: 'Изменить файл' }));
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить и выйти' }));
    expect(await screen.findByText('Не удалось сохранить работу')).toBeInTheDocument();
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
    fireEvent.click(screen.getByRole('button', { name: 'Скрыть условие' }));
    expect(screen.queryByText('Прочитайте строки до EOF.')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Показать условие' }));
    expect(screen.getByText('Прочитайте строки до EOF.')).toBeInTheDocument();

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

    fireEvent.click(screen.getByRole('button', { name: 'Завершить' }));

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

    fireEvent.click(screen.getByRole('button', { name: 'Завершить' }));
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

    fireEvent.click(screen.getByRole('button', { name: 'Завершить' }));
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

    fireEvent.click(screen.getByRole('button', { name: 'Завершить' }));
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

    fireEvent.click(screen.getByRole('button', { name: 'Завершить' }));
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
