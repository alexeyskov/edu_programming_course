import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AiMarkdown, TeacherAiChat, ThinkingIndicator } from './TeacherAiChat';
import type { AiStreamEvent } from '../lib/aiStream';
import { composeTeacherAiMessage, parseTeacherAiMessage } from '../lib/teacherAiMessage';

const mocks = vi.hoisted(() => ({ getTeacherAiThreads: vi.fn(), getAiMessages: vi.fn(), createTeacherAiThread: vi.fn(), resetTeacherAiThread: vi.fn(), sendAiMessageStream: vi.fn() }));
vi.mock('../lib/api', () => ({ api: mocks }));
const props = { submissionId: 'sub-1', courseId: 'course-1', canSend: true, teacherComment: '', expanded: false, onExpanded: vi.fn() };
const attachment = { path: 'main.cpp', startLine: 26, endLine: 26, text: 'new char[10]{"abc defg"};' };
beforeEach(() => {
  vi.clearAllMocks();
  mocks.getTeacherAiThreads.mockResolvedValue([]);
  mocks.getAiMessages.mockResolvedValue([]);
  mocks.createTeacherAiThread.mockResolvedValue({ id: 'thread-1' });
  mocks.resetTeacherAiThread.mockResolvedValue({ id: 'thread-new' });
  mocks.sendAiMessageStream.mockResolvedValue({ id: 'reply', content: 'Ответ', citations: [] });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('teacher AI chat', () => {
  it('attaches selected code above an empty input and sends only the teacher’s own question on demand', async () => {
    const { container } = render(<TeacherAiChat {...props} draft={{ id: 1, attachment }} />);
    expect(screen.getByRole('region', { name: 'Помощник проверки' })).toBeInTheDocument();
    expect(screen.getByText('Условие и код уже в контексте')).toBeVisible();
    const input = screen.getByRole('textbox', { name: 'Вопрос ИИ' });
    expect(input).toHaveValue('');
    expect(input).toHaveFocus();
    const preview = screen.getByRole('complementary', { name: 'Прикреплённый код' });
    expect(preview).toHaveTextContent('main.cpp · строка 26');
    expect(preview).toHaveTextContent(attachment.text);
    expect(container.querySelector('form')?.firstElementChild).toBe(preview);
    expect(screen.getByRole('button', { name: 'Отправить' })).toBeDisabled();
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(mocks.createTeacherAiThread).not.toHaveBeenCalled();
    expect(mocks.sendAiMessageStream).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: 'Объясни выделение' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Отправить' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(mocks.sendAiMessageStream).toHaveBeenCalledWith('thread-1', composeTeacherAiMessage('Объясни выделение', attachment), { teacherComment: '' }, expect.anything()));
    expect(screen.queryByRole('complementary', { name: 'Прикреплённый код' })).not.toBeInTheDocument();
    expect(screen.getByRole('complementary', { name: 'Фрагмент кода в сообщении' })).toHaveTextContent(attachment.text);
    expect(screen.getByText('Объясни выделение')).toBeInTheDocument();
  });

  it('replacing or removing a fragment never overwrites a typed question or calls the model', async () => {
    const view = render(<TeacherAiChat {...props} />);
    const input = screen.getByRole('textbox');
    fireEvent.change(input, { target: { value: 'Мой вопрос' } });
    view.rerender(<TeacherAiChat {...props} draft={{ id: 1, attachment }} />);
    expect(input).toHaveValue('Мой вопрос');
    view.rerender(<TeacherAiChat {...props} draft={{ id: 2, attachment: { ...attachment, text: 'return 0;' } }} />);
    expect(screen.getByRole('complementary', { name: 'Прикреплённый код' })).toHaveTextContent('return 0;');
    expect(input).toHaveValue('Мой вопрос');
    fireEvent.click(screen.getByRole('button', { name: 'Убрать фрагмент кода' }));
    expect(screen.queryByRole('complementary', { name: 'Прикреплённый код' })).not.toBeInTheDocument();
    expect(input).toHaveValue('Мой вопрос');
    expect(input).toHaveFocus();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Отправить' })).toBeEnabled());
    expect(mocks.sendAiMessageStream).not.toHaveBeenCalled();
    expect(mocks.createTeacherAiThread).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(mocks.sendAiMessageStream).toHaveBeenCalledWith('thread-1', 'Мой вопрос', { teacherComment: '' }, expect.anything()));
  });

  it('shows a compact preview but sends the whole selection', async () => {
    const selected = { ...attachment, text: 'int n = 1;\n'.repeat(150) };
    render(<TeacherAiChat {...props} draft={{ id: 1, attachment: selected }} />);
    const preview = screen.getByRole('complementary', { name: 'Прикреплённый код' });
    expect(preview.querySelector('code')?.textContent).toHaveLength(801);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Вопрос' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Отправить' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(mocks.sendAiMessageStream).toHaveBeenCalledTimes(1));
    expect(parseTeacherAiMessage(mocks.sendAiMessageStream.mock.calls[0][1]).attachment).toEqual(selected);
  });

  it('restores attachment cards from history and leaves old messages readable', async () => {
    mocks.getTeacherAiThreads.mockResolvedValue([{ id: 'thread-1', submissionId: 'sub-1', status: 'OPEN' }]);
    mocks.getAiMessages.mockResolvedValue([
      { from: 'user', content: composeTeacherAiMessage('Зачем здесь 10?', attachment) },
      { from: 'user', content: 'Старый вопрос без вложений' },
    ]);
    render(<TeacherAiChat {...props} />);
    expect(await screen.findByRole('complementary', { name: 'Фрагмент кода в сообщении' })).toHaveTextContent(attachment.text);
    expect(screen.getByText('Зачем здесь 10?')).toBeInTheDocument();
    expect(screen.getByText('Старый вопрос без вложений')).toBeInTheDocument();
    expect(screen.getByRole('textbox')).toHaveValue('');
  });

  it('reports an oversized combined request without silently truncating code or sending it', async () => {
    const selected = { ...attachment, text: 'a'.repeat(20000) };
    render(<TeacherAiChat {...props} draft={{ id: 1, attachment: selected }} />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Вопрос' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Отправить' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Вопрос вместе с фрагментом слишком длинный');
    expect(screen.getByRole('textbox')).toHaveValue('Вопрос');
    expect(screen.getByRole('complementary', { name: 'Прикреплённый код' })).toBeInTheDocument();
    expect(mocks.createTeacherAiThread).not.toHaveBeenCalled();
    expect(mocks.sendAiMessageStream).not.toHaveBeenCalled();
  });

  it('renders Markdown tokens before completion and replaces Thinking with the answer', async () => {
    let onEvent!: (event: AiStreamEvent) => void;
    let finish!: (result: unknown) => void;
    mocks.sendAiMessageStream.mockImplementation((_id, _text, _context, options) => {
      onEvent = options.onEvent;
      return new Promise((resolve) => { finish = resolve; });
    });
    render(<TeacherAiChat {...props} />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Проверь' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Отправить' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(onEvent).toBeDefined());
    expect(screen.getByRole('status', { name: 'Thinking' })).toHaveTextContent('Thinking.');
    act(() => onEvent({ type: 'thinking', summary: 'указатели и память' }));
    expect(screen.getByRole('status', { name: 'Thinking' })).toHaveTextContent('Тема анализа: указатели и память');
    act(() => onEvent({ type: 'thinking', summary: 'индексы и границы массива' }));
    expect(screen.getByRole('status', { name: 'Thinking' })).toHaveTextContent('Тема анализа: индексы и границы массива');
    expect(screen.queryByText('Тема анализа: указатели и память')).not.toBeInTheDocument();
    act(() => onEvent({ type: 'delta', text: '**Важно**\n\n- проверь границы\n' }));
    expect(screen.getByText('Важно').tagName).toBe('STRONG');
    expect(screen.getByRole('listitem')).toHaveTextContent('проверь границы');
    expect(screen.queryByRole('status', { name: 'Thinking' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Отправить' })).toBeDisabled();
    await act(async () => finish({ id: 'reply', content: '**Готово**', citations: [] }));
    expect(screen.getByText('Готово').tagName).toBe('STRONG');
  });

  it('cycles one, two and three thinking dots and clears its timer', () => {
    vi.useFakeTimers();
    const view = render(<ThinkingIndicator />);
    expect(screen.getByRole('status')).toHaveTextContent('Thinking.');
    act(() => vi.advanceTimersByTime(450));
    expect(screen.getByRole('status')).toHaveTextContent('Thinking..');
    act(() => vi.advanceTimersByTime(450));
    expect(screen.getByRole('status')).toHaveTextContent('Thinking...');
    act(() => vi.advanceTimersByTime(450));
    expect(screen.getByRole('status').textContent).toBe('Thinking.');
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('keeps drafts and history while expanding and shows an active collapse icon', async () => {
    const draft = { id: 1, attachment };
    const view = render(<TeacherAiChat {...props} draft={draft} />);
    const transcript = screen.getByLabelText('Переписка с ИИ');
    expect(transcript).toContainElement(screen.getByText('Помощник проверки'));
    expect(transcript).not.toContainElement(screen.getByRole('button', { name: 'Развернуть чат' }));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Неотправленный вопрос' } });
    fireEvent.click(screen.getByRole('button', { name: 'Развернуть чат' }));
    expect(props.onExpanded).toHaveBeenCalledWith(true);
    view.rerender(<TeacherAiChat {...props} draft={draft} expanded />);
    expect(screen.getByRole('button', { name: 'Свернуть чат' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('textbox')).toHaveValue('Неотправленный вопрос');
    expect(screen.getByRole('complementary', { name: 'Прикреплённый код' })).toHaveTextContent(attachment.text);
    fireEvent.click(screen.getByRole('button', { name: 'Свернуть чат' }));
    expect(props.onExpanded).toHaveBeenCalledWith(false);
    await waitFor(() => expect(mocks.getTeacherAiThreads).toHaveBeenCalledTimes(1));
    expect(mocks.sendAiMessageStream).not.toHaveBeenCalled();
  });

  it('aborts an active stream when changing submissions/unmounting', async () => {
    mocks.sendAiMessageStream.mockReturnValue(new Promise(() => {}));
    const view = render(<TeacherAiChat {...props} />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Вопрос' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Отправить' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(mocks.sendAiMessageStream).toHaveBeenCalledTimes(1));
    const signal = mocks.sendAiMessageStream.mock.calls[0][3].signal as AbortSignal;
    view.unmount();
    expect(signal.aborted).toBe(true);
  });

  it('marks partial failures explicitly and never automatically resends a paid request', async () => {
    mocks.sendAiMessageStream.mockImplementation(async (_id, _text, _context, { onEvent }) => {
      onEvent({ type: 'delta', text: 'Неполный разбор' });
      throw new Error('Соединение прервано');
    });
    render(<TeacherAiChat {...props} draft={{ id: 1, attachment }} />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Вопрос' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Отправить' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Соединение прервано');
    expect(screen.getByText('Неполный ответ · не сохранён')).toBeInTheDocument();
    expect(screen.getByRole('textbox')).toHaveValue('Вопрос');
    expect(screen.queryByRole('complementary', { name: 'Прикреплённый код' })).not.toBeInTheDocument();
    expect(screen.getByRole('complementary', { name: 'Фрагмент кода в сообщении' })).toHaveTextContent(attachment.text);
    expect(mocks.sendAiMessageStream).toHaveBeenCalledTimes(1);
  });

  it('starts a clean conversation only after confirming and never calls the model to clear it', async () => {
    mocks.getTeacherAiThreads.mockResolvedValue([{ id: 'thread-old', submissionId: 'sub-1', status: 'OPEN' }]);
    mocks.getAiMessages.mockResolvedValue([{ from: 'user', content: 'Старый вопрос' }, { from: 'ai', content: 'Старый ответ' }]);
    const { container } = render(<TeacherAiChat {...props} draft={{ id: 1, attachment }} />);
    await screen.findByText('Старый ответ');
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Черновик' } });
    const clear = screen.getByRole('button', { name: 'Очистить чат' });
    expect(container.querySelector('.teacher-chat-header')?.lastElementChild).toBe(clear);
    fireEvent.click(clear);
    expect(screen.getByRole('dialog', { name: 'Очистить чат?' })).toBeInTheDocument();
    expect(mocks.resetTeacherAiThread).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Отмена' }));
    expect(screen.getByText('Старый ответ')).toBeInTheDocument();
    expect(screen.getByRole('textbox')).toHaveValue('Черновик');
    fireEvent.click(clear);
    fireEvent.click(screen.getByRole('button', { name: 'Очистить' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mocks.resetTeacherAiThread).toHaveBeenCalledExactlyOnceWith('sub-1', 'course-1');
    expect(screen.queryByText('Старый ответ')).not.toBeInTheDocument();
    expect(screen.queryByText('Старый вопрос')).not.toBeInTheDocument();
    expect(screen.getByRole('textbox')).toHaveValue('');
    expect(screen.getByRole('textbox')).toHaveFocus();
    expect(screen.queryByRole('complementary', { name: 'Прикреплённый код' })).not.toBeInTheDocument();
    expect(mocks.sendAiMessageStream).not.toHaveBeenCalled();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Новый вопрос' } });
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(mocks.sendAiMessageStream).toHaveBeenCalledWith('thread-new', 'Новый вопрос', { teacherComment: '' }, expect.anything()));
    expect(mocks.createTeacherAiThread).not.toHaveBeenCalled();
  });

  it('retains the conversation and draft if clearing fails, and prevents duplicate resets or sends', async () => {
    let fail!: (error: Error) => void;
    mocks.resetTeacherAiThread.mockImplementation(() => new Promise((_resolve, reject) => { fail = reject; }));
    render(<TeacherAiChat {...props} draft={{ id: 1, attachment }} />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Черновик' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Очистить чат' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Очистить чат' }));
    const confirm = screen.getByRole('button', { name: 'Очистить' });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    expect(confirm).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Отправить' })).toBeDisabled();
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });
    expect(mocks.sendAiMessageStream).not.toHaveBeenCalled();
    expect(mocks.resetTeacherAiThread).toHaveBeenCalledTimes(1);
    await act(async () => fail(new Error('Сеть недоступна')));
    expect(screen.getByRole('alert')).toHaveTextContent('Сеть недоступна');
    expect(screen.getByRole('textbox')).toHaveValue('Черновик');
    expect(screen.getByRole('complementary', { name: 'Прикреплённый код' })).toHaveTextContent(attachment.text);
    expect(confirm).toBeEnabled();
  });

  it('does not enable clearing a read-only chat, an empty chat or a response in progress', async () => {
    const view = render(<TeacherAiChat {...props} canSend={false} />);
    await waitFor(() => expect(screen.queryByText('Загружаю историю…')).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Очистить чат' })).toBeDisabled();
    view.rerender(<TeacherAiChat {...props} />);
    expect(screen.getByRole('button', { name: 'Очистить чат' })).toBeDisabled();
    mocks.sendAiMessageStream.mockReturnValue(new Promise(() => {}));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Вопрос' } });
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(mocks.sendAiMessageStream).toHaveBeenCalledTimes(1));
    expect(screen.getByRole('button', { name: 'Очистить чат' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Очистить чат' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mocks.resetTeacherAiThread).not.toHaveBeenCalled();
  });

  it('does not restore an old attachment over a newer draft when a stream fails', async () => {
    let fail!: (error: Error) => void;
    mocks.sendAiMessageStream.mockImplementation(() => new Promise((_resolve, reject) => { fail = reject; }));
    const view = render(<TeacherAiChat {...props} draft={{ id: 1, attachment }} />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Первый вопрос' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Отправить' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(fail).toBeDefined());
    view.rerender(<TeacherAiChat {...props} draft={{ id: 2, attachment: { ...attachment, text: 'return 0;' } }} />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Новый вопрос' } });
    await act(async () => fail(new Error('Соединение прервано')));
    expect(screen.getByRole('textbox')).toHaveValue('Новый вопрос');
    const preview = screen.getByRole('complementary', { name: 'Прикреплённый код' });
    expect(within(preview).getByText('return 0;')).toBeInTheDocument();
    expect(preview).not.toHaveTextContent(attachment.text);
    expect(mocks.sendAiMessageStream).toHaveBeenCalledTimes(1);
  });

  it('renders lists/code/tables but not model HTML, scripts, commands or tracking images', () => {
    const { container } = render(<AiMarkdown content={'**Текст**\n\n- пункт\n\n```cpp\nint x = 0;\n```\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n<script>alert(1)</script>\n\n[опасно](javascript:alert) [команда](command:run) ![image](https://tracking.test/i)'} />);
    expect(screen.getByText('Текст').tagName).toBe('STRONG');
    expect(container.querySelector('pre code')).toHaveTextContent('int x = 0;');
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(container.querySelector('script, img, a[href^="javascript:"], a[href^="command:"]')).toBeNull();
  });
});
