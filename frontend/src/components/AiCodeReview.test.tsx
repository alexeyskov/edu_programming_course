import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AiCodeReview } from '../types';
import { AiCodeReviewPanel, AiHighlightsToggle, AiReviewFeedback, AiReviewIndicator, AiReviewRecommendation, useAiCodeReview } from './AiCodeReview';

const mocks = vi.hoisted(() => ({ getAiCodeReview: vi.fn(), startAiCodeReview: vi.fn() }));
vi.mock('../lib/api', () => ({ api: mocks }));

const completed: AiCodeReview = {
  enabled: true, state: 'COMPLETED', summary: 'Найдена возможная ошибка границ массива.', model: 'test-model',
  findings: [{ path: 'main.cpp', line: 5, endLine: 7, severity: 'warning', message: 'Возможен выход за границы массива.' }],
};
const report = (state: AiCodeReview['state'], enabled = true): AiCodeReview => ({ enabled, state, summary: '', findings: [] });

beforeEach(() => {
  vi.useFakeTimers();
  vi.resetAllMocks();
  mocks.getAiCodeReview.mockResolvedValue(completed);
  mocks.startAiCodeReview.mockResolvedValue(report('QUEUED'));
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

async function settle() { await act(async () => {}); }
async function advance(milliseconds: number) { await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds); }); }

describe('visible AI review failures and bounded retry progress', () => {
  const props = () => ({ report: report('FAILED'), error: null, starting: false, canRetry: true, onRetry: vi.fn() });
  it('shows the final error without hover and offers an explicit new cycle', () => {
    const callbacks = props();
    render(<AiReviewFeedback {...callbacks} report={{ ...report('FAILED'), attemptCount: 3, maxAttempts: 3, error: 'ИИ вернул некорректный формат отчёта.' }} />);
    expect(screen.getByRole('alert')).toHaveTextContent('ИИ-проверка не выполнена');
    expect(screen.getByRole('alert')).toHaveTextContent('Все попытки исчерпаны. Выполнено: 3 из 3.');
    expect(screen.getByRole('alert')).toHaveTextContent('ИИ вернул некорректный формат отчёта.');
    expect(screen.getByRole('alert')).toHaveTextContent('Оценка и комментарий преподавателя не изменены.');
    expect(callbacks.onRetry).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Повторить ИИ-проверку' }));
    expect(callbacks.onRetry).toHaveBeenCalledOnce();
  });
  it.each(['QUEUED', 'RUNNING'] as const)('shows progress in %s, hides intermediate errors, and cannot relaunch', (state) => {
    render(<AiReviewFeedback {...props()} report={{ ...report(state), attemptCount: 2, maxAttempts: 3, error: 'Скрытая промежуточная ошибка' }} />);
    expect(screen.getByRole('status')).toHaveTextContent(state === 'RUNNING' ? 'Попытка 2 из 3' : 'Готовится повторная попытка 3 из 3');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.queryByText('Скрытая промежуточная ошибка')).not.toBeInTheDocument();
  });
  it('hides a previous failure immediately when starting, and disappears on completion', () => {
    const view = render(<AiReviewFeedback {...props()} starting />);
    expect(screen.getByRole('status')).toHaveTextContent('Отправляем работу на проверку.');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    view.rerender(<AiReviewFeedback {...props()} report={completed} />);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
  it('does not claim three exhausted attempts for legacy or policy failures', () => {
    render(<AiReviewFeedback {...props()} canRetry={false} />);
    expect(screen.getByRole('alert')).not.toHaveTextContent('Все попытки исчерпаны');
    expect(screen.getByRole('button')).toBeDisabled();
  });
});

describe('AI recommendation until explicit review exit', () => {
  const suggestion: AiCodeReview = { ...completed, id: 'review', recommendationStatus: 'PENDING', recommendedGrade: 7.5, recommendedComment: 'Исправьте ввод.' };
  it('explains discard on exit and requires an explicit approval', () => {
    const approve = vi.fn();
    render(<AiReviewRecommendation report={suggestion} maxScore={10} canApprove onApprove={approve} />);
    expect(screen.getByText('7.5/10')).toBeVisible();
    expect(screen.getByText(/Кнопка подставит балл и комментарий в поля формы/)).toBeVisible();
    expect(approve).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Поставить оценку и комментарий от ИИ' }));
    expect(approve).toHaveBeenCalledOnce();
  });
  it('offers an AI comment without assigning points when the maximum is zero', () => {
    const approve = vi.fn();
    render(<AiReviewRecommendation report={{ ...suggestion, recommendedGrade: 0 }} maxScore={0} canApprove onApprove={approve} />);
    expect(screen.getByText('Без балла')).toBeVisible();
    expect(screen.queryByText('0/0')).not.toBeInTheDocument();
    expect(screen.getByText(/Баллы за эту работу не выставляются/)).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Поставить комментарий от ИИ' }));
    expect(approve).toHaveBeenCalledOnce();
  });
  it.each([undefined, null, 3])('does not offer applying an invalid zero-point recommendation (grade=%s)', (recommendedGrade) => {
    render(<AiReviewRecommendation report={{ ...suggestion, recommendedGrade }} maxScore={0} canApprove onApprove={vi.fn()} />);
    expect(screen.getByText('Без балла')).toBeVisible();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
  it('does not apply a comment-only AI recommendation when numeric grading is required', () => {
    render(<AiReviewRecommendation report={{ ...suggestion, recommendedGrade: undefined }} maxScore={10} canApprove onApprove={vi.fn()} />);
    expect(screen.getByText('Без балла')).toBeVisible();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
  it('does not offer a discarded recommendation even if a stale payload contains its score', () => {
    vi.setSystemTime(new Date('2020-01-01T00:00:00Z'));
    render(<AiReviewRecommendation report={{ ...suggestion, recommendationStatus: 'DISCARDED' }} maxScore={10} canApprove onApprove={vi.fn()} />);
    expect(screen.queryByRole('region')).not.toBeInTheDocument();
    expect(screen.queryByText('7.5/10')).not.toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
  it('does not offer duplicate approval for an approved recommendation', () => {
    render(<AiReviewRecommendation report={{ ...suggestion, recommendationStatus: 'APPROVED' }} maxScore={10} canApprove onApprove={vi.fn()} />);
    expect(screen.getByText('7.5/10')).toBeVisible();
    expect(screen.getByText(/На основе рекомендации утверждено решение преподавателя/)).toBeVisible();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});

describe('automatic AI review polling', () => {
  it('does nothing until a submission is available', async () => {
    const { result } = renderHook(() => useAiCodeReview(undefined));
    await settle();
    await advance(60_000);
    expect(result.current.report).toBeNull();
    expect(mocks.getAiCodeReview).not.toHaveBeenCalled();
    expect(mocks.startAiCodeReview).not.toHaveBeenCalled();
  });

  it.each([
    ['DISABLED', false], ['NOT_STARTED', true], ['UNAVAILABLE', true],
  ] as const)('only reads %s state and never launches automatic analysis from the browser', async (state, enabled) => {
    mocks.getAiCodeReview.mockResolvedValue(report(state, enabled));
    const { result } = renderHook(() => useAiCodeReview('submission-1'));
    await settle();
    expect(result.current.report?.state).toBe(state);
    await advance(29_999);
    expect(mocks.getAiCodeReview).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(mocks.getAiCodeReview).toHaveBeenCalledTimes(2);
    expect(mocks.startAiCodeReview).not.toHaveBeenCalled();
  });

  it.each(['QUEUED', 'RUNNING'] as const)('polls %s every two seconds without a new launch', async (state) => {
    mocks.getAiCodeReview.mockResolvedValue(report(state));
    renderHook(() => useAiCodeReview('submission-1'));
    await settle();
    expect(mocks.getAiCodeReview).toHaveBeenCalledWith('submission-1');
    await advance(1999);
    expect(mocks.getAiCodeReview).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(mocks.getAiCodeReview).toHaveBeenCalledTimes(2);
    expect(mocks.startAiCodeReview).not.toHaveBeenCalled();
  });

  it('uses completed cached findings and refreshes slowly without requesting another analysis', async () => {
    const { result } = renderHook(() => useAiCodeReview('submission-1'));
    await settle();
    expect(result.current.report).toEqual(completed);
    await advance(29_999);
    expect(mocks.getAiCodeReview).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(mocks.getAiCodeReview).toHaveBeenCalledTimes(2);
    expect(mocks.startAiCodeReview).not.toHaveBeenCalled();
  });

  it('retries a failed analysis only after the explicit retry action', async () => {
    mocks.getAiCodeReview.mockResolvedValue(report('FAILED'));
    const { result } = renderHook(() => useAiCodeReview('submission-1'));
    await settle();
    await advance(30_000);
    expect(mocks.startAiCodeReview).not.toHaveBeenCalled();
    act(() => result.current.retry());
    await settle();
    expect(mocks.startAiCodeReview).toHaveBeenCalledExactlyOnceWith('submission-1', true);
    expect(result.current.report?.state).toBe('QUEUED');
  });

  it('starts one manual check, blocks double clicks and polls through to completed findings', async () => {
    mocks.getAiCodeReview.mockResolvedValue(report('NOT_STARTED'));
    let finish!: (value: AiCodeReview) => void;
    mocks.startAiCodeReview.mockReturnValue(new Promise<AiCodeReview>((resolve) => { finish = resolve; }));
    const { result } = renderHook(() => useAiCodeReview('submission-1'));
    await settle();
    act(() => { void result.current.start(); void result.current.start(); });
    expect(result.current.starting).toBe(true);
    expect(mocks.startAiCodeReview).toHaveBeenCalledExactlyOnceWith('submission-1', false);
    await advance(30_000);
    expect(mocks.getAiCodeReview).toHaveBeenCalledTimes(1);
    await act(async () => finish(report('QUEUED')));
    expect(result.current.starting).toBe(false);
    expect(result.current.report?.state).toBe('QUEUED');
    await act(async () => { await result.current.start(); });
    mocks.getAiCodeReview.mockResolvedValue(report('RUNNING'));
    await advance(2000);
    expect(result.current.report?.state).toBe('RUNNING');
    await act(async () => { await result.current.start(); });
    mocks.getAiCodeReview.mockResolvedValue(completed);
    await advance(2000);
    expect(result.current.report).toEqual(completed);
    await act(async () => { await result.current.start(); });
    expect(mocks.startAiCodeReview).toHaveBeenCalledTimes(1);
  });

  it('does not let an older poll overwrite a manual queue response', async () => {
    mocks.getAiCodeReview.mockResolvedValueOnce(report('NOT_STARTED'));
    const { result } = renderHook(() => useAiCodeReview('submission-1'));
    await settle();
    let finishOldPoll!: (value: AiCodeReview) => void;
    mocks.getAiCodeReview.mockReturnValueOnce(new Promise<AiCodeReview>((resolve) => { finishOldPoll = resolve; }));
    await advance(30_000);
    await act(async () => { await result.current.start(); });
    expect(result.current.report?.state).toBe('QUEUED');
    await act(async () => finishOldPoll(report('NOT_STARTED')));
    expect(result.current.report?.state).toBe('QUEUED');
  });

  it('recovers a lost start response by reading state without resubmitting', async () => {
    mocks.getAiCodeReview.mockResolvedValue(report('NOT_STARTED'));
    mocks.startAiCodeReview.mockRejectedValue(new Error('Соединение прервано'));
    const { result } = renderHook(() => useAiCodeReview('submission-1'));
    await settle();
    await act(async () => { expect(await result.current.start()).toBe('Соединение прервано'); });
    expect(result.current.error).toBe('Соединение прервано');
    expect(result.current.starting).toBe(false);
    mocks.getAiCodeReview.mockResolvedValue(report('QUEUED'));
    await advance(2000);
    expect(result.current.report?.state).toBe('QUEUED');
    expect(mocks.startAiCodeReview).toHaveBeenCalledTimes(1);
  });

  it('does not carry a pending manual start response into another submission', async () => {
    mocks.getAiCodeReview.mockResolvedValue(report('NOT_STARTED'));
    let finish!: (value: AiCodeReview) => void;
    mocks.startAiCodeReview.mockReturnValue(new Promise<AiCodeReview>((resolve) => { finish = resolve; }));
    const { result, rerender } = renderHook(({ id }) => useAiCodeReview(id), { initialProps: { id: 'old' } });
    await settle();
    act(() => { void result.current.start(); });
    rerender({ id: 'new' });
    await settle();
    await act(async () => finish(report('QUEUED')));
    expect(result.current.report?.state).toBe('NOT_STARTED');
    expect(result.current.starting).toBe(false);
    expect(mocks.startAiCodeReview).toHaveBeenCalledExactlyOnceWith('old', false);
  });

  it('drops old annotations when polling loses access and reports the error without launching analysis', async () => {
    const { result } = renderHook(() => useAiCodeReview('submission-1'));
    await settle();
    mocks.getAiCodeReview.mockRejectedValue(new Error('Доступ к работе закрыт'));
    await advance(30_000);
    expect(result.current.report).toBeNull();
    expect(result.current.error).toBe('Доступ к работе закрыт');
    expect(mocks.startAiCodeReview).not.toHaveBeenCalled();
  });

  it('ignores a delayed response for a different submission', async () => {
    let resolveOld!: (value: AiCodeReview) => void;
    mocks.getAiCodeReview.mockImplementation((id: string) => id === 'old'
      ? new Promise<AiCodeReview>((resolve) => { resolveOld = resolve; })
      : Promise.resolve(completed));
    const { result, rerender } = renderHook(({ id }) => useAiCodeReview(id), { initialProps: { id: 'old' } });
    rerender({ id: 'new' });
    await settle();
    expect(result.current.report).toEqual(completed);
    await act(async () => { resolveOld(report('FAILED')); });
    expect(result.current.report).toEqual(completed);
    expect(result.current.error).toBeNull();
    expect(mocks.startAiCodeReview).not.toHaveBeenCalled();
  });

  it('clears completed findings immediately when switching to a still-loading submission', async () => {
    const { result, rerender } = renderHook(({ id }) => useAiCodeReview(id), { initialProps: { id: 'old' } });
    await settle();
    expect(result.current.report).toEqual(completed);
    mocks.getAiCodeReview.mockReturnValue(new Promise(() => {}));
    rerender({ id: 'new' });
    expect(result.current.report).toBeNull();
  });

  it('does not carry a manual retry over to another failed submission', async () => {
    mocks.getAiCodeReview.mockResolvedValue(report('FAILED'));
    const { result, rerender } = renderHook(({ id }) => useAiCodeReview(id), { initialProps: { id: 'old' } });
    await settle();
    act(() => result.current.retry());
    await settle();
    expect(mocks.startAiCodeReview).toHaveBeenCalledExactlyOnceWith('old', true);
    rerender({ id: 'new' });
    await settle();
    expect(result.current.report?.state).toBe('FAILED');
    expect(mocks.startAiCodeReview).toHaveBeenCalledTimes(1);
  });

  it('clears the previous report when the submission is no longer available', async () => {
    const { result, rerender } = renderHook(({ id }: { id: string | undefined }) => useAiCodeReview(id), { initialProps: { id: 'old' as string | undefined } });
    await settle();
    expect(result.current.report).toEqual(completed);
    rerender({ id: undefined });
    expect(result.current.report).toBeNull();
    await advance(60_000);
    expect(mocks.getAiCodeReview).toHaveBeenCalledTimes(1);
  });

  it('does not schedule polling or launch analysis if a pending request resolves after unmount', async () => {
    let resolveRequest!: (value: AiCodeReview) => void;
    mocks.getAiCodeReview.mockReturnValue(new Promise<AiCodeReview>((resolve) => { resolveRequest = resolve; }));
    const { unmount } = renderHook(() => useAiCodeReview('submission-1'));
    unmount();
    await act(async () => { resolveRequest(report('NOT_STARTED')); });
    await advance(60_000);
    expect(mocks.getAiCodeReview).toHaveBeenCalledTimes(1);
    expect(mocks.startAiCodeReview).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('three-state AI review control', () => {
  const props = () => ({ report: report('NOT_STARTED'), checked: true, onChange: vi.fn(), onStart: vi.fn(), originalVisible: true });

  it('offers a framed inset manual start with an explanatory tooltip', () => {
    const callbacks = props();
    render(<AiHighlightsToggle {...callbacks} />);
    const button = screen.getByRole('button', { name: 'Проверить работу через ИИ' });
    expect(button).toBeEnabled();
    expect(button).toHaveClass('ai-review-launch--ready');
    expect(screen.getByTitle('Работа ещё не проверялась ИИ. Нажмите для проверки через ИИ.')).toContainElement(button);
    fireEvent.click(button);
    expect(callbacks.onStart).toHaveBeenCalledOnce();
    expect(callbacks.onChange).not.toHaveBeenCalled();
  });

  it.each(['QUEUED', 'RUNNING'] as const)('makes %s grey and unclickable', (state) => {
    const callbacks = props();
    render(<AiHighlightsToggle {...callbacks} report={report(state)} />);
    const button = screen.getByRole('button', { name: 'ИИ-проверка выполняется' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('aria-busy', 'true');
    fireEvent.click(button);
    expect(callbacks.onStart).not.toHaveBeenCalled();
  });

  it('disables immediately while the launch request is pending', () => {
    render(<AiHighlightsToggle {...props()} starting />);
    expect(screen.getByRole('button', { name: 'ИИ-проверка выполняется' })).toBeDisabled();
  });

  it('switches to the ordinary highlights toggle after completion, even with no findings', () => {
    const callbacks = props();
    render(<AiHighlightsToggle {...callbacks} report={{ ...completed, findings: [] }} />);
    const toggle = screen.getByRole('switch', { name: 'Подсветка замечаний ИИ' });
    expect(toggle).toBeEnabled();
    expect(toggle).toBeChecked();
    fireEvent.click(toggle);
    expect(callbacks.onChange).toHaveBeenCalledExactlyOnceWith(false);
    expect(callbacks.onStart).not.toHaveBeenCalled();
  });

  it('offers an explicit retry after failure but no start in the sandbox or without a provider', () => {
    const callbacks = props();
    const view = render(<AiHighlightsToggle {...callbacks} report={{ ...report('FAILED'), error: 'Проверка прервалась.' }} />);
    expect(screen.getByTitle(/Проверка прервалась/)).toBeInTheDocument();
    expect(screen.getByRole('button')).toBeEnabled();
    view.rerender(<AiHighlightsToggle {...callbacks} originalVisible={false} />);
    expect(screen.getByRole('button')).toBeDisabled();
    view.rerender(<AiHighlightsToggle {...callbacks} report={{ ...report('UNAVAILABLE'), error: 'ИИ отключён.' }} />);
    expect(screen.getByRole('button')).toBeDisabled();
    expect(screen.getByTitle('ИИ отключён.')).toBeInTheDocument();
  });
});

const panelProps = () => ({
  report: completed as AiCodeReview | null, error: null as string | null, highlights: true,
  onHighlights: vi.fn(), onFinding: vi.fn(), onRetry: vi.fn(), originalVisible: true,
});

describe('AI review recommendations', () => {
  it.each(['QUEUED', 'RUNNING'] as const)('shows a non-blocking %s status', (state) => {
    render(<AiCodeReviewPanel {...panelProps()} report={report(state)} />);
    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(screen.queryByText(/ИИ не указал замечаний/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Повторить ИИ-анализ' })).not.toBeInTheDocument();
  });

  it('shows disabled and unavailable explanations without a launch button', () => {
    const { rerender } = render(<AiCodeReviewPanel {...panelProps()} report={report('DISABLED', false)} />);
    expect(screen.getByText(/Выключена для этой работы/)).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    rerender(<AiCodeReviewPanel {...panelProps()} report={{ ...report('UNAVAILABLE'), error: 'Провайдер не настроен' }} />);
    expect(screen.getByText('Провайдер не настроен')).toBeInTheDocument();
  });

  it('shows the effective disabled reason even when the assignment opted in', () => {
    render(<AiCodeReviewPanel {...panelProps()} report={{
      ...report('DISABLED'), error: 'ИИ-поддержка проверки отключена для работы.',
    }} />);
    expect(screen.getByText('ИИ-поддержка проверки отключена для работы.')).toBeInTheDocument();
    expect(screen.queryByText(/ИИ не указал замечаний/)).not.toBeInTheDocument();
  });

  it('does not describe an unstarted older submission as queued or completed', () => {
    render(<AiCodeReviewPanel {...panelProps()} report={report('NOT_STARTED')} />);
    expect(screen.getByText(/не запускался/)).toBeInTheDocument();
    expect(screen.queryByText(/поставлен в очередь/)).not.toBeInTheDocument();
    expect(screen.queryByText(/ИИ не указал замечаний/)).not.toBeInTheDocument();
  });

  it('renders findings, highlights control and a teacher decision disclaimer', () => {
    const props = panelProps();
    render(<AiCodeReviewPanel {...props} originalVisible={false} />);
    fireEvent.click(screen.getByRole('button', { name: /main.cpp:5–7/ }));
    expect(props.onFinding).toHaveBeenCalledWith(completed.findings[0]);
    const checkbox = screen.getByRole('checkbox', { name: 'Подсвечивать замечания ИИ в коде' });
    expect(checkbox).toBeChecked();
    fireEvent.click(checkbox);
    expect(props.onHighlights).toHaveBeenCalledWith(false);
    expect(screen.getByText(/Подсветка доступна в оригинале сдачи/)).toBeInTheDocument();
    expect(screen.getByText(/Окончательную оценку выставляет преподаватель/)).toBeInTheDocument();
  });

  it('offers an explicit manual retry on failure', () => {
    const props = panelProps();
    render(<AiCodeReviewPanel {...props} report={{ ...report('FAILED'), error: 'Провайдер временно недоступен' }} />);
    expect(screen.getByRole('alert')).toHaveTextContent('Провайдер временно недоступен');
    fireEvent.click(screen.getByRole('button', { name: 'Повторить ИИ-анализ' }));
    expect(props.onRetry).toHaveBeenCalledOnce();
  });

  it('treats model messages as plain text', () => {
    const unsafeMessage = '<img src=x onerror=alert(1)>';
    const { container } = render(<AiCodeReviewPanel {...panelProps()} report={{
      ...completed, summary: unsafeMessage, findings: [{ ...completed.findings[0], message: '<script>run()</script>' }],
    }} />);
    expect(screen.getByText(unsafeMessage)).toBeInTheDocument();
    expect(screen.getByText('<script>run()</script>')).toBeInTheDocument();
    expect(container.querySelector('img, script')).toBeNull();
  });

  it('indicates completed findings and opens the analysis panel', () => {
    const onOpen = vi.fn();
    const { rerender } = render(<AiReviewIndicator report={completed} onOpen={onOpen} />);
    fireEvent.click(screen.getByRole('button', { name: 'ИИ: замечаний 1' }));
    expect(onOpen).toHaveBeenCalledOnce();
    rerender(<AiReviewIndicator report={report('DISABLED', false)} onOpen={onOpen} />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('does not imply active analysis when an older submission was not queued', () => {
    render(<AiReviewIndicator report={report('NOT_STARTED')} onOpen={vi.fn()} />);
    expect(screen.queryByRole('button', { name: 'ИИ: анализ…' })).not.toBeInTheDocument();
  });
});
