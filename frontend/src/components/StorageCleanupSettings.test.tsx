import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { StorageCleanupResult, StorageUsage } from '../types';
import { StorageCleanupSettings } from './StorageCleanupSettings';
import { ToastProvider } from './ui';

const mocks = vi.hoisted(() => ({ getStorageUsage: vi.fn(), cleanUpStorage: vi.fn() }));
vi.mock('../lib/api', () => ({ api: mocks }));
const gib = 1024 ** 3;
const preview: StorageUsage = {
  storageLimitGb: 128, usedBytes: 130 * gib, databaseBytes: 150 * gib,
  eligibleBefore: '2026-10-19T09:00:00Z', lastCleanupAt: null, warnings: [],
  categories: [
    { category: 'edit_history', records: 13, usedBytes: 4 * gib, eligibleRecords: 10, eligibleBytes: 3 * gib },
    { category: 'teacher_ai_chats', records: 5, usedBytes: gib, eligibleRecords: 4, eligibleBytes: gib / 2 },
    { category: 'synced_submissions', records: 20, usedBytes: 3 * gib, eligibleRecords: 7, eligibleBytes: gib },
  ],
};
const removed: StorageCleanupResult = {
  usedBytesBefore: 130 * gib, usedBytesAfter: 127 * gib, reclaimedBytes: 3 * gib, overLimit: false,
  removedRecords: 10, categories: [{ category: 'edit_history', removedRecords: 10, reclaimedBytes: 3 * gib }], warnings: [],
};
const accessError = vi.fn(() => false);

function renderStorage(refreshKey = 0) {
  return render(<ToastProvider><StorageCleanupSettings onAccessError={accessError} refreshKey={refreshKey} /></ToastProvider>);
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
async function confirmAction(name = 'Очистить историю написания') {
  const button = await screen.findByRole('button', { name });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  return screen.findByRole('dialog', { name: 'Очистить данные хранилища?' });
}
beforeEach(() => {
  vi.resetAllMocks(); accessError.mockReturnValue(false);
  mocks.getStorageUsage.mockResolvedValue(preview);
  mocks.cleanUpStorage.mockResolvedValue(removed);
});
afterEach(cleanup);

describe('administrator stored-data cleanup', () => {
  it('shows the logical-data budget and disk size separately without deleting anything', async () => {
    renderStorage();
    const summary = await screen.findByLabelText('Объём хранимых данных');
    expect(within(summary).getByText('130 ГиБ')).toBeInTheDocument();
    expect(within(summary).getByText('128 ГиБ')).toBeInTheDocument();
    expect(within(summary).getByText('150 ГиБ')).toBeInTheDocument();
    expect(screen.getByText(/не к оперативной памяти/)).toBeInTheDocument();
    expect(screen.getByText(/Файлы Docker и журналы PostgreSQL/)).toBeInTheDocument();
    expect(mocks.cleanUpStorage).not.toHaveBeenCalled();
    const dialog = await confirmAction();
    expect(within(dialog).getByText(/Историю написания и чаты восстановить нельзя/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Отмена' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mocks.cleanUpStorage).not.toHaveBeenCalled();
  });

  it('refreshes the confirmation cutoff and sends one explicit destructive request', async () => {
    const pending = deferred<StorageCleanupResult>();
    const refreshed = { ...preview, eligibleBefore: '2026-10-19T09:05:00Z' };
    mocks.getStorageUsage.mockResolvedValueOnce(preview).mockResolvedValueOnce(refreshed).mockResolvedValue({ ...preview, usedBytes: 127 * gib });
    mocks.cleanUpStorage.mockReturnValue(pending.promise);
    renderStorage();
    const dialog = await confirmAction('Очистить архив ответов');
    expect(within(dialog).getByText(/Для архива ответов потребуется повторный импорт/)).toBeInTheDocument();
    expect(within(dialog).getByRole('list')).toHaveTextContent('Архив ответов и комментариев: 7');
    const confirm = within(dialog).getByRole('button', { name: 'Удалить безвозвратно' });
    fireEvent.click(confirm); fireEvent.click(confirm);
    expect(confirm).toBeDisabled();
    expect(within(dialog).getByRole('button', { name: 'Отмена' })).toBeDisabled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Закрыть' }));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(mocks.cleanUpStorage).toHaveBeenCalledExactlyOnceWith('synced_submissions', refreshed.eligibleBefore);
    await act(async () => pending.resolve(removed));
    expect(await screen.findByText('Удалено записей: 10. Освобождено: 3 ГиБ.')).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Очистить до лимита' })).toBeDisabled());
  });

  it('offers a quota cleanup only above the saved limit and lists the category priority', async () => {
    renderStorage();
    const dialog = await confirmAction('Очистить до лимита');
    expect(within(dialog).getByText(/Очистка остановится после достижения лимита/)).toBeInTheDocument();
    const items = within(dialog).getAllByRole('listitem');
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent('История написания кода');
    expect(items[1]).toHaveTextContent('Чаты преподавателей с ИИ');
    expect(items[2]).toHaveTextContent('Архив ответов и комментариев');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Удалить безвозвратно' }));
    await waitFor(() => expect(mocks.cleanUpStorage).toHaveBeenCalledExactlyOnceWith('quota', preview.eligibleBefore));
  });

  it('disables deletion when no records are safely eligible, even if the budget is exceeded', async () => {
    mocks.getStorageUsage.mockResolvedValue({ ...preview, categories: preview.categories.map((item) => ({ ...item, eligibleRecords: 0, eligibleBytes: 0 })) });
    renderStorage();
    await screen.findByLabelText('Объём хранимых данных');
    for (const name of ['Очистить историю написания', 'Очистить чаты преподавателей', 'Очистить архив ответов', 'Очистить до лимита']) {
      expect(screen.getByRole('button', { name })).toBeDisabled();
    }
    expect(mocks.cleanUpStorage).not.toHaveBeenCalled();
  });

  it('invalidates stale confirmation if its refresh fails and never deletes from old counts', async () => {
    mocks.getStorageUsage.mockResolvedValueOnce(preview).mockRejectedValueOnce(new Error('Объём недоступен'));
    renderStorage();
    const button = await screen.findByRole('button', { name: 'Очистить историю написания' });
    fireEvent.click(button);
    expect(await screen.findByRole('alert')).toHaveTextContent('Объём недоступен');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Очистить историю написания' })).not.toBeInTheDocument();
    expect(mocks.cleanUpStorage).not.toHaveBeenCalled();
  });

  it('does not auto-repeat a failed POST and requires a fresh preview and confirmation', async () => {
    mocks.cleanUpStorage.mockRejectedValue(new Error('Ответ сервера потерян'));
    renderStorage();
    const dialog = await confirmAction();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Удалить безвозвратно' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Ответ сервера потерян');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Очистить до лимита' })).toBeDisabled();
    expect(mocks.cleanUpStorage).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Обновить объём данных' }));
    expect(await screen.findByRole('button', { name: 'Очистить историю написания' })).toBeEnabled();
    expect(mocks.cleanUpStorage).toHaveBeenCalledTimes(1);
  });

  it('invalidates administrative access without presenting stale destructive controls', async () => {
    const denied = new Error('SYSTEM_SETTINGS required');
    mocks.getStorageUsage.mockRejectedValue(denied); accessError.mockReturnValue(true);
    renderStorage();
    await waitFor(() => expect(accessError).toHaveBeenCalledWith(denied));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Очистить до лимита' })).toBeDisabled();
  });

  it('ignores a preview returned after leaving settings', async () => {
    const pending = deferred<StorageUsage>(); mocks.getStorageUsage.mockReturnValue(pending.promise);
    const view = renderStorage(); view.unmount();
    await act(async () => pending.resolve(preview));
    expect(accessError).not.toHaveBeenCalled();
    expect(mocks.cleanUpStorage).not.toHaveBeenCalled();
  });
});
