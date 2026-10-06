import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AiChatCleanupCounts, AiChatCleanupPreview } from '../types';
import { AiChatCleanupSettings } from './AiChatCleanupSettings';
import { ToastProvider } from './ui';

const mocks = vi.hoisted(() => ({ getAiChatCleanup: vi.fn(), cleanUpAiChats: vi.fn() }));
vi.mock('../lib/api', () => ({ api: mocks }));

const preview: AiChatCleanupPreview = {
  threads: 5, teacherThreads: 2, studentThreads: 3, messages: 47,
  closedBefore: '2026-10-05T09:55:00Z', graceSeconds: 300, protectedThreads: 1,
};
const empty: AiChatCleanupPreview = { ...preview, threads: 0, teacherThreads: 0, studentThreads: 0, messages: 0 };
const accessError = vi.fn(() => false);

function renderCleanup() {
  return render(<ToastProvider><AiChatCleanupSettings onAccessError={accessError} /></ToastProvider>);
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

async function openConfirmation() {
  const button = await screen.findByRole('button', { name: 'Очистить закрытые переписки' });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  return screen.findByRole('dialog', { name: 'Удалить закрытые переписки с ИИ?' });
}

beforeEach(() => {
  vi.resetAllMocks();
  accessError.mockReturnValue(false);
  mocks.getAiChatCleanup.mockResolvedValue(preview);
  mocks.cleanUpAiChats.mockResolvedValue({ threads: 5, teacherThreads: 2, studentThreads: 3, messages: 47 });
});
afterEach(cleanup);

describe('administrator AI chat cleanup', () => {
  it('previews both roles without deleting anything, and cancel keeps the history', async () => {
    renderCleanup();
    const counts = await screen.findByLabelText('Доступно для очистки');
    expect(within(counts).getByText('2')).toBeInTheDocument();
    expect(within(counts).getByText('3')).toBeInTheDocument();
    expect(within(counts).getByText('47')).toBeInTheDocument();
    expect(screen.getByText(/Пока сохранены недавно закрытые чаты: 1/)).toBeInTheDocument();
    expect(mocks.cleanUpAiChats).not.toHaveBeenCalled();
    const dialog = await openConfirmation();
    expect(mocks.getAiChatCleanup).toHaveBeenCalledTimes(2);
    expect(within(dialog).getByText(/Отменить удаление/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Отмена' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mocks.cleanUpAiChats).not.toHaveBeenCalled();
  });

  it('confirms the refreshed cutoff, prevents duplicate requests, and displays actual counts', async () => {
    const pending = deferred<AiChatCleanupCounts>();
    mocks.cleanUpAiChats.mockReturnValue(pending.promise);
    const refreshed = { ...preview, closedBefore: '2026-10-05T10:00:00Z' };
    mocks.getAiChatCleanup.mockResolvedValueOnce(preview).mockResolvedValueOnce(refreshed).mockResolvedValue(empty);
    renderCleanup();
    const dialog = await openConfirmation();
    const confirm = within(dialog).getByRole('button', { name: 'Удалить безвозвратно' });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    expect(confirm).toBeDisabled();
    expect(within(dialog).getByRole('button', { name: 'Отмена' })).toBeDisabled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Закрыть' }));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(mocks.cleanUpAiChats).toHaveBeenCalledExactlyOnceWith(refreshed.closedBefore);
    await act(async () => pending.resolve({ threads: 4, teacherThreads: 1, studentThreads: 3, messages: 40 }));
    expect(await screen.findByText('Удалено чатов: 4, сообщений: 40.')).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(await screen.findByText('Нет закрытых переписок, доступных для очистки.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Очистить закрытые переписки' })).toBeDisabled();
  });

  it('does not offer deletion when all remaining closed chats are recent', async () => {
    mocks.getAiChatCleanup.mockResolvedValue(empty);
    renderCleanup();
    await screen.findByText('Нет закрытых переписок, доступных для очистки.');
    expect(screen.getByRole('button', { name: 'Очистить закрытые переписки' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Обновить счётчики' })).toBeEnabled();
    expect(mocks.cleanUpAiChats).not.toHaveBeenCalled();
  });

  it('invalidates stale counts if refreshing for confirmation fails', async () => {
    mocks.getAiChatCleanup.mockResolvedValueOnce(preview).mockRejectedValueOnce(new Error('Сервер недоступен'));
    renderCleanup();
    const button = screen.getByRole('button', { name: 'Очистить закрытые переписки' });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(await screen.findByRole('alert')).toHaveTextContent('Сервер недоступен');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(button).toBeDisabled();
    expect(mocks.cleanUpAiChats).not.toHaveBeenCalled();
  });

  it('shows a failure without auto retry and requires another preview before trying again', async () => {
    mocks.cleanUpAiChats.mockRejectedValue(new Error('Не удалось дождаться ответа сервера'));
    renderCleanup();
    const dialog = await openConfirmation();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Удалить безвозвратно' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Не удалось дождаться ответа сервера');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Очистить закрытые переписки' })).toBeDisabled();
    expect(mocks.cleanUpAiChats).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Обновить счётчики' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Очистить закрытые переписки' })).toBeEnabled());
    expect(mocks.cleanUpAiChats).toHaveBeenCalledTimes(1);
  });

  it('invalidates administrative access on a permission error', async () => {
    const denied = new Error('SYSTEM_SETTINGS required');
    mocks.getAiChatCleanup.mockRejectedValue(denied);
    accessError.mockReturnValue(true);
    renderCleanup();
    await waitFor(() => expect(accessError).toHaveBeenCalledWith(denied));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(mocks.cleanUpAiChats).not.toHaveBeenCalled();
  });

  it('ignores a preview returned after leaving settings', async () => {
    const pending = deferred<AiChatCleanupPreview>();
    mocks.getAiChatCleanup.mockReturnValue(pending.promise);
    const view = renderCleanup();
    view.unmount();
    await act(async () => pending.resolve(preview));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(accessError).not.toHaveBeenCalled();
    expect(mocks.cleanUpAiChats).not.toHaveBeenCalled();
  });
});
