import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { forwardRef, useImperativeHandle } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '../components/ui';
import type { Attempt, AttemptStatus, InteractiveRun, WorkspaceFile } from '../types';
import { IdePage } from './IdePage';

const mocks = vi.hoisted(() => ({
  getAttempt: vi.fn(), getAttemptStatus: vi.fn(), getHistory: vi.fn(), resolveCourseId: vi.fn(),
  startAttempt: vi.fn(), saveFile: vi.fn(),
  startInteractiveAttempt: vi.fn(), getInteractiveAttempt: vi.fn(), stopInteractiveAttempt: vi.fn(),
  submitAttempt: vi.fn(), retryAttemptSubmission: vi.fn(),
  createClipboardReceipt: vi.fn(), workspaceProps: vi.fn(),
}));

vi.mock('../lib/api', () => ({
  api: mocks,
  ApiError: class extends Error { constructor(public status: number, public code: string, message: string) { super(message); } },
}));
vi.mock('../components/CodeWorkspace', () => ({
  CodeWorkspace: forwardRef((props: any, ref) => {
    mocks.workspaceProps(props);
    useImperativeHandle(ref, () => ({ openDiagnostic: vi.fn(), focus: vi.fn() }));
    const active = props.files.find((file: WorkspaceFile) => file.id === props.activeFileId) ?? props.files[0];
    return <div data-testid="code-workspace" data-scope={props.scopeId}>
      {props.files.map((file: WorkspaceFile) => <button key={file.id} onClick={() => props.onActiveFile(file.id)}>{file.path}</button>)}
      <textarea aria-label="Код решения" value={active?.content ?? ''} readOnly={props.readOnly} onChange={(event) => props.onChange(active.id, event.target.value, 'typing')} />
    </div>;
  }),
}));

const quizSession: Attempt['quizSession'] = {
  id: 'quiz-session-1', rootAttemptId: 'question-1',
  questions: [
    { attemptId: 'question-1', slot: '1', title: 'Строки', position: 1 },
    { attemptId: 'question-2', slot: '2', title: 'Массивы', position: 2 },
  ],
};

function questionAttempt(position: number): Attempt {
  return {
    id: `question-${position}`, assessmentId: `assessment-${position}`, title: `Задание ${position}`,
    statement: `Условие задачи ${position}`, status: 'ACTIVE', revision: 0, acknowledgedRevision: 0,
    startedAt: '2026-09-10T10:00:00Z', deadlineAt: '2099-09-10T12:00:00Z', checkpointStatus: 'SYNCED',
    pastePolicy: 'ALLOW', aiEnabled: false, fileMode: 'MULTI', quizSession,
    files: [
      { id: `main-${position}`, path: 'main.cpp', content: `// Решение ${position}`, language: 'cpp' },
      { id: `helper-${position}`, path: 'helper.cpp', content: `// Функции ${position}`, language: 'cpp' },
    ],
  };
}

function runningSession(attemptId: string): InteractiveRun {
  return {
    sessionId: `run-${attemptId}`, status: 'RUNNING', terminal: false, durationMs: 0,
    stdout: `Вывод ${attemptId}`, stderr: '', outputTruncated: false, diagnostics: [],
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

let attempts: Map<string, Attempt>;

function saveToServer(attemptId: string, file: WorkspaceFile, revision: number) {
  const current = attempts.get(attemptId)!;
  expect(revision).toBe(current.revision);
  current.files = current.files.map((item) => item.id === file.id ? { ...file } : item);
  current.revision += 1;
  current.acknowledgedRevision = current.revision;
  return { revision: current.revision };
}

function renderQuiz(attemptId = 'question-1') {
  return render(<MemoryRouter initialEntries={[`/ide/${attemptId}`]}><ToastProvider><Routes>
    <Route path="/ide/:attemptId" element={<IdePage />} />
    <Route path="/" element={<div>Список работ</div>} />
  </Routes></ToastProvider></MemoryRouter>);
}

beforeEach(() => {
  window.sessionStorage.clear();
  Object.values(mocks).forEach((mock) => mock.mockReset());
  attempts = new Map([1, 2].map((position) => {
    const attempt = questionAttempt(position);
    return [attempt.id, attempt];
  }));
  mocks.getAttempt.mockImplementation(async (id: string) => structuredClone(attempts.get(id)));
  mocks.getAttemptStatus.mockImplementation(async (id: string) => {
    const current = attempts.get(id)!;
    return { id, status: current.status, checkpointStatus: current.checkpointStatus };
  });
  mocks.getHistory.mockResolvedValue([]);
  mocks.createClipboardReceipt.mockResolvedValue({ id: 'clipboard-receipt' });
  mocks.resolveCourseId.mockResolvedValue('course-1');
  mocks.saveFile.mockImplementation(async (...args: Parameters<typeof saveToServer>) => saveToServer(...args));
  mocks.startInteractiveAttempt.mockImplementation(async (id: string) => runningSession(id));
  mocks.getInteractiveAttempt.mockImplementation(async (id: string) => runningSession(id));
  mocks.stopInteractiveAttempt.mockImplementation(async (id: string) => ({ ...runningSession(id), status: 'STOPPED', terminal: true }));
  mocks.submitAttempt.mockImplementation(async () => {
    for (const current of attempts.values()) { current.status = 'SUBMITTED'; current.checkpointStatus = 'PENDING'; }
    return { receipt_id: 'quiz-receipt' };
  });
});

afterEach(() => { cleanup(); window.sessionStorage.clear(); vi.restoreAllMocks(); });

describe('student Moodle quiz question workspaces', () => {
  it('uses a simple filename label and example when creating a C++ file', async () => {
    renderQuiz(); await screen.findByLabelText('Код решения');
    act(() => mocks.workspaceProps.mock.calls.at(-1)![0].onCreateFile());
    expect(screen.getByText('Напишите имя файла')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('solution.cpp')).toBeInTheDocument();
    expect(screen.queryByText('Относительный путь')).not.toBeInTheDocument();
  });

  it('keeps a shared clipboard across question remounts without sharing editor models', async () => {
    for (const current of attempts.values()) current.pastePolicy = 'STRICT';
    renderQuiz(); await screen.findByLabelText('Код решения');
    const first = mocks.workspaceProps.mock.calls.at(-1)![0];
    expect(first.scopeId).toBe('question-1');
    expect(first.clipboardScopeId).toBe('question-1');
    expect(first.strictPaste).toBe(true);
    fireEvent.click(screen.getByRole('tab', { name: 'Задача 2 Массивы' }));
    await screen.findByText('Условие задачи 2');
    const second = mocks.workspaceProps.mock.calls.at(-1)![0];
    expect(second.scopeId).toBe('question-2');
    expect(second.clipboardScopeId).toBe('question-1');
    expect(second.clipboardSession).toBe(first.clipboardSession);
    expect(second.strictPaste).toBe(true);
  });

  it('renews a sibling copy using its saved revision, after saving the current question', async () => {
    renderQuiz(); await screen.findByLabelText('Код решения');
    fireEvent.change(screen.getByLabelText('Код решения'), { target: { value: 'int helper() {}' } });
    fireEvent.click(screen.getByRole('tab', { name: 'Задача 2 Массивы' }));
    await screen.findByText('Условие задачи 2');
    fireEvent.change(screen.getByLabelText('Код решения'), { target: { value: '// own second code' } });
    const current = mocks.workspaceProps.mock.calls.at(-1)![0];
    await act(async () => {
      expect(await current.onInternalCopy('main-1', 'int helper()', 'question-1')).toBe('clipboard-receipt');
    });
    expect(attempts.get('question-2')!.files[0].content).toBe('// own second code');
    expect(mocks.createClipboardReceipt).toHaveBeenCalledWith('question-1', 'main-1', 'int helper()', 1);
    expect(screen.getByLabelText('Код решения')).toHaveValue('// own second code');
  });

  it('does not request a receipt from outside the current quiz attempt', async () => {
    renderQuiz('question-2'); await screen.findByLabelText('Код решения');
    const current = mocks.workspaceProps.mock.calls.at(-1)![0];
    await act(async () => {
      expect(await current.onInternalCopy('foreign', 'outside', 'unrelated-attempt')).toBeNull();
    });
    expect(mocks.getAttempt).not.toHaveBeenCalledWith('unrelated-attempt');
    attempts.get('question-1')!.quizSession = { ...quizSession!, rootAttemptId: 'another-quiz' };
    await act(async () => {
      expect(await current.onInternalCopy('main-1', '// Решение 1', 'question-1')).toBeNull();
    });
    expect(mocks.createClipboardReceipt).not.toHaveBeenCalled();
  });

  it('keeps the original controls for a single-question attempt', async () => {
    attempts.get('question-1')!.quizSession = undefined;
    renderQuiz();
    await screen.findByLabelText('Код решения');
    expect(screen.queryByRole('tablist', { name: 'Выбор задачи' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Завершить' })).toBeEnabled();
  });

  it('does not repeat standard question titles in the tabs', async () => {
    attempts.get('question-1')!.quizSession = {
      ...quizSession!,
      questions: quizSession!.questions.map((question, index) => ({
        ...question, title: index === 0 ? 'Задание 1' : 'Задача №2',
      })),
    };
    renderQuiz();
    const firstTab = await screen.findByRole('tab', { name: 'Задача 1' });
    const secondTab = screen.getByRole('tab', { name: 'Задача 2' });
    expect(firstTab.querySelector('span')).toBeNull();
    expect(secondTab.querySelector('span')).toBeNull();
  });

  it('keeps custom question titles as tab subtitles', async () => {
    renderQuiz();
    const firstTab = await screen.findByRole('tab', { name: 'Задача 1 Строки' });
    const secondTab = screen.getByRole('tab', { name: 'Задача 2 Массивы' });
    expect(firstTab.querySelector('span')).toHaveTextContent('Строки');
    expect(secondTab.querySelector('span')).toHaveTextContent('Массивы');
  });

  it('saves separate solutions before switching and restores them when returning', async () => {
    renderQuiz();
    expect(await screen.findByLabelText('Код решения')).toHaveValue('// Решение 1');
    fireEvent.change(screen.getByLabelText('Код решения'), { target: { value: '// Код первой задачи' } });
    fireEvent.click(screen.getByRole('tab', { name: 'Задача 2 Массивы' }));

    expect(await screen.findByText('Условие задачи 2')).toBeInTheDocument();
    expect(screen.getByLabelText('Код решения')).toHaveValue('// Решение 2');
    expect(mocks.saveFile).toHaveBeenCalledWith('question-1', expect.objectContaining({ id: 'main-1', content: '// Код первой задачи' }), 0, 'typing', undefined, undefined);
    fireEvent.change(screen.getByLabelText('Код решения'), { target: { value: '// Код второй задачи' } });
    fireEvent.click(screen.getByRole('tab', { name: 'Задача 1 Строки' }));

    await screen.findByText('Условие задачи 1');
    expect(screen.getByLabelText('Код решения')).toHaveValue('// Код первой задачи');
    expect(attempts.get('question-2')!.files[0].content).toBe('// Код второй задачи');
    expect(screen.getByTestId('code-workspace')).toHaveAttribute('data-scope', 'question-1');
    expect(mocks.startAttempt).not.toHaveBeenCalled();
  });

  it('remembers the selected file independently for each question and after reloading', async () => {
    const view = renderQuiz();
    await screen.findByLabelText('Код решения');
    fireEvent.click(screen.getByRole('button', { name: 'helper.cpp' }));
    fireEvent.click(screen.getByRole('tab', { name: 'Задача 2 Массивы' }));
    await screen.findByText('Условие задачи 2');
    expect(screen.getByLabelText('Код решения')).toHaveValue('// Решение 2');
    fireEvent.click(screen.getByRole('tab', { name: 'Задача 1 Строки' }));
    await screen.findByText('Условие задачи 1');
    expect(screen.getByLabelText('Код решения')).toHaveValue('// Функции 1');
    view.unmount();
    renderQuiz();
    expect(await screen.findByLabelText('Код решения')).toHaveValue('// Функции 1');
  });

  it('does not navigate or lose the code if saving fails, and can retry the same queue', async () => {
    mocks.saveFile.mockRejectedValueOnce(new Error('Нет связи с сервером'));
    renderQuiz();
    await screen.findByLabelText('Код решения');
    fireEvent.change(screen.getByLabelText('Код решения'), { target: { value: '// Несохранённый код' } });
    fireEvent.click(screen.getByRole('tab', { name: 'Задача 2 Массивы' }));

    await screen.findByText('Не удалось переключить задачу');
    expect(screen.getByLabelText('Код решения')).toHaveValue('// Несохранённый код');
    expect(screen.getByLabelText('Код решения')).not.toHaveAttribute('readonly');
    expect(mocks.getAttempt).not.toHaveBeenCalledWith('question-2');
    fireEvent.click(screen.getByRole('tab', { name: 'Задача 2 Массивы' }));
    await screen.findByText('Условие задачи 2');
    expect(attempts.get('question-1')!.files[0].content).toBe('// Несохранённый код');
    expect(mocks.saveFile).toHaveBeenCalledTimes(2);
  });

  it('waits for the server acknowledgement before disposing the current editor', async () => {
    const saved = deferred<{ revision: number }>();
    mocks.saveFile.mockImplementationOnce(() => saved.promise);
    renderQuiz();
    await screen.findByLabelText('Код решения');
    fireEvent.change(screen.getByLabelText('Код решения'), { target: { value: '// Ожидает подтверждения' } });
    fireEvent.click(screen.getByRole('tab', { name: 'Задача 2 Массивы' }));
    await waitFor(() => expect(mocks.saveFile).toHaveBeenCalledOnce());
    expect(screen.getByLabelText('Код решения')).toHaveValue('// Ожидает подтверждения');
    expect(screen.getByLabelText('Код решения')).toHaveAttribute('readonly');
    expect(screen.getByRole('tab', { name: 'Задача 1 Строки' })).toBeDisabled();
    expect(mocks.getAttempt).not.toHaveBeenCalledWith('question-2');
    await act(async () => { saved.resolve({ revision: 1 }); });
    await screen.findByText('Условие задачи 2');
  });

  it('stops the previous program and runs only the selected question workspace', async () => {
    renderQuiz();
    await screen.findByLabelText('Код решения');
    fireEvent.click(screen.getByRole('button', { name: 'Запустить' }));
    await screen.findByText('Вывод question-1');
    fireEvent.click(screen.getByRole('tab', { name: 'Задача 2 Массивы' }));
    await screen.findByText('Условие задачи 2');
    expect(mocks.stopInteractiveAttempt).toHaveBeenCalledWith('question-1', 'run-question-1');
    expect(screen.queryByText('Вывод question-1')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Запустить' }));
    await screen.findByText('Вывод question-2');
    expect(mocks.startInteractiveAttempt).toHaveBeenLastCalledWith('question-2', 0);
  });

  it('stays on the current question if stopping its program fails', async () => {
    mocks.stopInteractiveAttempt.mockRejectedValue(new Error('Не удалось остановить программу'));
    renderQuiz();
    await screen.findByLabelText('Код решения');
    fireEvent.click(screen.getByRole('button', { name: 'Запустить' }));
    await screen.findByText('Вывод question-1');
    fireEvent.click(screen.getByRole('tab', { name: 'Задача 2 Массивы' }));
    await screen.findByText('Не удалось переключить задачу');
    expect(screen.getByText('Условие задачи 1')).toBeInTheDocument();
    expect(mocks.getAttempt).not.toHaveBeenCalledWith('question-2');
  });

  it('explicitly submits all questions from a non-root question after saving its code', async () => {
    renderQuiz('question-2');
    await screen.findByLabelText('Код решения');
    fireEvent.change(screen.getByLabelText('Код решения'), { target: { value: '// Финальный код второй задачи' } });
    fireEvent.click(screen.getByRole('button', { name: 'Завершить работу' }));
    expect(screen.getByText(/Будут сданы сохранённые решения всех задач/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Сдать все задачи' }));
    await waitFor(() => expect(mocks.submitAttempt).toHaveBeenCalledWith('question-2', 1));
    expect(attempts.get('question-2')!.files[0].content).toBe('// Финальный код второй задачи');
    expect(mocks.submitAttempt).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText('Код решения')).toHaveAttribute('readonly');
  });

  it('ignores a delayed status response from the previous question', async () => {
    const oldStatus = deferred<AttemptStatus>();
    mocks.getAttemptStatus.mockImplementationOnce(() => oldStatus.promise);
    renderQuiz();
    await screen.findByLabelText('Код решения');
    await waitFor(() => expect(mocks.getAttemptStatus).toHaveBeenCalledWith('question-1'));
    fireEvent.click(screen.getByRole('tab', { name: 'Задача 2 Массивы' }));
    await screen.findByText('Условие задачи 2');
    await act(async () => { oldStatus.resolve({ id: 'question-1', status: 'LOCKED', checkpointStatus: 'ERROR', closureReason: 'LMS_ATTEMPT_FINALIZED' }); });
    expect(screen.getByText('Условие задачи 2')).toBeInTheDocument();
    expect(screen.getByLabelText('Код решения')).not.toHaveAttribute('readonly');
    expect(screen.queryByText('Сеанс работы завершён через Moodle')).not.toBeInTheDocument();
  });
});
