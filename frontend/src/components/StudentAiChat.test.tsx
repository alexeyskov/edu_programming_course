import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AiStreamEvent } from '../lib/aiStream';
import { composeTeacherAiMessage, parseTeacherAiMessage } from '../lib/teacherAiMessage';
import { StudentAiChat } from './StudentAiChat';

const mocks = vi.hoisted(() => ({ createStudentAiThread: vi.fn(), sendAiMessageStream: vi.fn() }));
vi.mock('../lib/api', () => ({ api: mocks }));
const props = { open: true, onClose: vi.fn(), attemptId: 'attempt-1', courseId: 'course-1', canSend: true, prepareContext: vi.fn() };
const attachment = { path: 'main.cpp', startLine: 4, endLine: 6, text: 'int x = 0;\nstd::cin >> x;' };

beforeEach(() => {
  vi.resetAllMocks();
  props.prepareContext.mockResolvedValue(7);
  mocks.createStudentAiThread.mockResolvedValue({ id: 'student-thread' });
  mocks.sendAiMessageStream.mockResolvedValue({ content: '**Проверьте** границы массива.', citations: [] });
});
afterEach(cleanup);

function ask(question = 'Объясни выделение') {
  fireEvent.change(screen.getByRole('textbox', { name: 'Вопрос ИИ' }), { target: { value: question } });
  fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
}

describe('student AI chat', () => {
  it('matches the review chat while keeping the student no-ready-solution policy and opt-in selected code', async () => {
    render(<StudentAiChat {...props} draft={{ id: 1, attachment }} />);
    expect(screen.getByRole('complementary', { name: 'Учебный помощник' })).toBeInTheDocument();
    expect(screen.getByText('Условие и код уже в контексте')).toBeVisible();
    expect(screen.getByText(/Объяснения и подсказки без готового решения/)).toBeVisible();
    expect(screen.getByRole('textbox')).toHaveValue('');
    expect(screen.getByRole('textbox')).toHaveFocus();
    expect(screen.getByRole('complementary', { name: 'Прикреплённый код' })).toHaveTextContent('main.cpp · строки 4–6');
    expect(mocks.createStudentAiThread).not.toHaveBeenCalled();
    expect(mocks.sendAiMessageStream).not.toHaveBeenCalled();
    ask();
    await waitFor(() => expect(mocks.sendAiMessageStream).toHaveBeenCalledWith('student-thread', composeTeacherAiMessage('Объясни выделение', attachment), { revision: 7 }, expect.anything()));
    expect(mocks.createStudentAiThread).toHaveBeenCalledExactlyOnceWith('attempt-1', 'course-1', 7);
    expect(screen.queryByRole('complementary', { name: 'Прикреплённый код' })).not.toBeInTheDocument();
    expect(screen.getByRole('complementary', { name: 'Фрагмент кода в сообщении' }).querySelector('code')?.textContent).toBe(attachment.text);
    expect((await screen.findByText('Проверьте')).tagName).toBe('STRONG');
  });

  it('saves all code before sending each question and keeps one student thread', async () => {
    let acknowledge!: (revision: number) => void;
    props.prepareContext.mockReturnValueOnce(new Promise((resolve) => { acknowledge = resolve; }));
    render(<StudentAiChat {...props} />);
    ask('Первый вопрос');
    expect(screen.getByRole('status')).toHaveTextContent('Сохраняем код');
    expect(mocks.createStudentAiThread).not.toHaveBeenCalled();
    expect(mocks.sendAiMessageStream).not.toHaveBeenCalled();
    await act(async () => acknowledge(8));
    await waitFor(() => expect(mocks.sendAiMessageStream).toHaveBeenCalledWith('student-thread', 'Первый вопрос', { revision: 8 }, expect.anything()));
    props.prepareContext.mockResolvedValueOnce(10);
    ask('Второй вопрос');
    await waitFor(() => expect(mocks.sendAiMessageStream).toHaveBeenLastCalledWith('student-thread', 'Второй вопрос', { revision: 10 }, expect.anything()));
    expect(props.prepareContext).toHaveBeenCalledTimes(2);
    expect(mocks.createStudentAiThread).toHaveBeenCalledTimes(1);
  });

  it('streams safe Markdown and links without rendering HTML or requesting model-controlled images', async () => {
    let event!: (event: AiStreamEvent) => void;
    let finish!: (result: unknown) => void;
    mocks.sendAiMessageStream.mockImplementation((_id, _text, _context, options) => {
      event = options.onEvent;
      return new Promise((resolve) => { finish = resolve; });
    });
    const { container } = render(<StudentAiChat {...props} />);
    ask();
    await waitFor(() => expect(event).toBeDefined());
    expect(screen.getByRole('status', { name: 'Thinking' })).toBeInTheDocument();
    act(() => event({ type: 'delta', text: '**Важно**\n\n- Проверьте индекс\n\n`size()`\n\n![tracker](https://tracker.invalid/pixel)\n\n<img src="https://tracker.invalid/x">\n\n[bad](javascript:alert(1))\n\n[Справочник](https://en.cppreference.com/w/cpp/container/vector)' }));
    expect(screen.getByText('Важно').tagName).toBe('STRONG');
    expect(screen.getByRole('listitem')).toHaveTextContent('Проверьте индекс');
    expect(screen.getByText('size()').tagName).toBe('CODE');
    expect(screen.getByRole('link', { name: 'Справочник' })).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.queryByRole('link', { name: 'bad' })).not.toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
    expect(screen.queryByRole('status', { name: 'Thinking' })).not.toBeInTheDocument();
    await act(async () => finish({ content: '**Готово**', citations: [
      { title: 'Безопасно', url: 'https://en.cppreference.com/' },
      { title: 'Нельзя', url: 'javascript:alert(1)' },
    ] }));
    expect(screen.getByText('Готово').tagName).toBe('STRONG');
    expect(screen.getByRole('link', { name: 'Безопасно' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Нельзя' })).not.toBeInTheDocument();
  });

  it('never duplicates a request or reuses a fragment after failure; a new draft is preserved', async () => {
    let reject!: (error: Error) => void;
    mocks.sendAiMessageStream.mockImplementation((_id, _text, _context, options) => {
      options.onEvent({ type: 'delta', text: 'Начало ответа' });
      return new Promise((_resolve, fail) => { reject = fail; });
    });
    const view = render(<StudentAiChat {...props} draft={{ id: 1, attachment }} />);
    ask();
    await waitFor(() => expect(reject).toBeDefined());
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });
    expect(mocks.sendAiMessageStream).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Следующий вопрос' } });
    view.rerender(<StudentAiChat {...props} draft={{ id: 2, attachment: { ...attachment, text: 'return x;' } }} />);
    await act(async () => reject(new Error('Сеть недоступна')));
    expect(screen.getByRole('alert')).toHaveTextContent('Сеть недоступна');
    expect(screen.getByText('Неполный ответ · не сохранён')).toBeInTheDocument();
    expect(screen.getByRole('textbox')).toHaveValue('Следующий вопрос');
    expect(screen.getByRole('complementary', { name: 'Прикреплённый код' })).toHaveTextContent('return x;');
    expect(parseTeacherAiMessage(mocks.sendAiMessageStream.mock.calls[0][1]).attachment).toEqual(attachment);
  });

  it('clears the sent fragment on failure without automatically retrying or contacting the model on save failure', async () => {
    props.prepareContext.mockRejectedValueOnce(new Error('Не удалось сохранить код'));
    render(<StudentAiChat {...props} draft={{ id: 1, attachment }} />);
    ask();
    expect(await screen.findByRole('alert')).toHaveTextContent('Не удалось сохранить код');
    expect(screen.getByRole('textbox')).toHaveValue('Объясни выделение');
    expect(screen.queryByRole('complementary', { name: 'Прикреплённый код' })).not.toBeInTheDocument();
    expect(mocks.createStudentAiThread).not.toHaveBeenCalled();
    expect(mocks.sendAiMessageStream).not.toHaveBeenCalled();
  });

  it('keeps messages and a draft when closing/reopening or expanding the drawer', async () => {
    const view = render(<StudentAiChat {...props} draft={{ id: 1, attachment }} />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Неотправленный вопрос' } });
    fireEvent.click(screen.getByRole('button', { name: 'Развернуть чат' }));
    expect(screen.getByRole('button', { name: 'Свернуть чат' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: 'Закрыть помощник' }));
    expect(props.onClose).toHaveBeenCalledOnce();
    view.rerender(<StudentAiChat {...props} open={false} />);
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    view.rerender(<StudentAiChat {...props} />);
    expect(screen.getByRole('textbox')).toHaveValue('Неотправленный вопрос');
    expect(screen.getByRole('complementary', { name: 'Прикреплённый код' }).querySelector('code')?.textContent).toBe(attachment.text);
    expect(mocks.sendAiMessageStream).not.toHaveBeenCalled();
  });

  it('cancels a stream when leaving the task and ignores its late answer', async () => {
    let finish!: (result: unknown) => void;
    mocks.sendAiMessageStream.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    const view = render(<StudentAiChat {...props} />);
    ask();
    await waitFor(() => expect(mocks.sendAiMessageStream).toHaveBeenCalledOnce());
    const signal = mocks.sendAiMessageStream.mock.calls[0][3].signal as AbortSignal;
    view.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => finish({ content: 'Старый ответ', citations: [] }));
    render(<StudentAiChat {...props} attemptId="other-task" />);
    expect(screen.queryByText('Старый ответ')).not.toBeInTheDocument();
  });

  it('does not send when AI is disabled or when the full attachment exceeds the request limit', async () => {
    const view = render(<StudentAiChat {...props} canSend={false} />);
    expect(screen.getByRole('textbox')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Отправить' })).toBeDisabled();
    view.rerender(<StudentAiChat {...props} draft={{ id: 1, attachment: { ...attachment, text: 'x'.repeat(20_000) } }} />);
    ask();
    expect(screen.getByRole('alert')).toHaveTextContent('Вопрос вместе с фрагментом слишком длинный');
    expect(screen.getByRole('complementary', { name: 'Прикреплённый код' })).toBeInTheDocument();
    expect(mocks.sendAiMessageStream).not.toHaveBeenCalled();
  });

  it('waits for course metadata without discarding the selected code or sending a paid request', () => {
    const view = render(<StudentAiChat {...props} courseId="" draft={{ id: 1, attachment }} />);
    expect(screen.getByRole('status')).toHaveTextContent('Загружаем данные курса');
    expect(screen.getByRole('textbox')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Отправить' })).toBeDisabled();
    expect(mocks.sendAiMessageStream).not.toHaveBeenCalled();
    view.rerender(<StudentAiChat {...props} />);
    expect(screen.getByRole('textbox')).toBeEnabled();
    expect(screen.getByRole('textbox')).toHaveFocus();
    expect(screen.getByRole('complementary', { name: 'Прикреплённый код' })).toBeInTheDocument();
  });
});
