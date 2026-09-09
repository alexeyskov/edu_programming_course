import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '../components/ui';
import { ApiError } from '../lib/api';
import { loadRememberedTokens, saveRememberedToken } from '../lib/rememberedTokens';
import type { CourseCatalogEntry, SystemSettings, TeacherAccessToken } from '../types';
import { SettingsPage } from './SettingsPage';

const mocks = vi.hoisted(() => ({
  getSettings: vi.fn(),
  updateSettings: vi.fn(),
  getHealth: vi.fn(),
  getCourseCatalog: vi.fn(),
  createCourseImport: vi.fn(),
  confirmCourseImport: vi.fn(),
  deleteCourseCatalogEntry: vi.fn(),
  syncCourse: vi.fn(),
  getTeacherTokens: vi.fn(),
  createTeacherToken: vi.fn(),
  revealTeacherToken: vi.fn(),
  updateTeacherToken: vi.fn(),
  deleteTeacherToken: vi.fn(),
  dropElevation: vi.fn(),
  elevate: vi.fn(),
  invalidateElevation: vi.fn(),
  refresh: vi.fn(),
}));

const authState = vi.hoisted(() => ({ elevated: true }));

vi.mock('../lib/api', async (importOriginal) => {
  const original = await importOriginal<typeof import('../lib/api')>();
  return {
    ...original,
    api: {
    getSettings: mocks.getSettings,
    updateSettings: mocks.updateSettings,
    getHealth: mocks.getHealth,
    getCourseCatalog: mocks.getCourseCatalog,
    createCourseImport: mocks.createCourseImport,
    confirmCourseImport: mocks.confirmCourseImport,
    deleteCourseCatalogEntry: mocks.deleteCourseCatalogEntry,
    syncCourse: mocks.syncCourse,
    getTeacherTokens: mocks.getTeacherTokens,
    createTeacherToken: mocks.createTeacherToken,
    revealTeacherToken: mocks.revealTeacherToken,
    updateTeacherToken: mocks.updateTeacherToken,
    deleteTeacherToken: mocks.deleteTeacherToken,
    },
  };
});

vi.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    session: {
      id: 'admin-1', displayName: 'Администратор', providerName: 'MMCS Moodle',
      memberships: [], capabilities: authState.elevated ? ['SYSTEM_SETTINGS'] : [],
    },
    dropElevation: mocks.dropElevation,
    elevate: mocks.elevate,
    invalidateElevation: mocks.invalidateElevation,
    refresh: mocks.refresh,
  }),
}));

const settings: SystemSettings = {
  revision: 1,
  aiEnabled: true,
  studentAiEnabled: true,
  runnerEnabled: true,
  runnerCpuSeconds: 4,
  runnerMemoryMb: 256,
  retentionDays: 365,
  allowedLmsOrigins: ['https://edu.mmcs.sfedu.ru'],
  incidentBanner: '',
  services: [],
};

const course: CourseCatalogEntry = {
  id: 'course-1',
  connectionId: 'connection-1',
  connectionName: 'MMCS Moodle',
  externalId: '549',
  title: 'Основы программирования C/C++',
  shortName: 'C/C++',
  externalUrl: 'https://edu.mmcs.sfedu.ru/course/view.php?id=549',
  syncStatus: 'SYNCED',
  addedAt: '2026-08-25T10:00:00Z',
};

const teacherToken: TeacherAccessToken = {
  id: 'teacher-token-1',
  label: 'Коваленко А.',
  publicId: 'a1b2c3d4e5f60708',
  hashFingerprint: '41f6bc2e7832a630',
  canReveal: true,
  boundPrincipalId: 'teacher-1',
  boundDisplayName: 'Коваленко Алексей',
  useCount: 3,
  lastUsedAt: '2026-08-25T12:00:00Z',
  createdAt: '2026-08-25T10:00:00Z',
};

const issuedTeacherToken = {
  ...teacherToken,
  boundPrincipalId: undefined,
  boundDisplayName: undefined,
  useCount: 0,
  token: 'A7dkP2x9',
};

function renderSettings() {
  return render(<MemoryRouter><ToastProvider><SettingsPage /></ToastProvider></MemoryRouter>);
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, resolve, reject };
}

async function openIssuedTeacherTokenDialog() {
  mocks.createTeacherToken.mockResolvedValue(issuedTeacherToken);
  renderSettings();

  const input = await screen.findByLabelText('Название токена преподавателя');
  fireEvent.change(input, { target: { value: teacherToken.label } });
  fireEvent.click(screen.getByRole('button', { name: 'Создать токен' }));

  return screen.findByRole('dialog', { name: 'Сохраните токен преподавателя' });
}

beforeEach(() => {
  window.localStorage.clear();
  authState.elevated = true;
  Object.values(mocks).forEach((mock) => mock.mockReset());
  mocks.getSettings.mockResolvedValue(settings);
  mocks.updateSettings.mockResolvedValue(settings);
  mocks.getHealth.mockResolvedValue({ status: 'ok', database: 'ok', build: 'test' });
  mocks.getCourseCatalog.mockResolvedValue([course]);
  mocks.deleteCourseCatalogEntry.mockResolvedValue(undefined);
  mocks.getTeacherTokens.mockResolvedValue([]);
  mocks.revealTeacherToken.mockResolvedValue({ token: issuedTeacherToken.token });
  mocks.updateTeacherToken.mockImplementation(async (_tokenId: string, _token: string) => ({
    ...teacherToken,
    canReveal: true,
  }));
  mocks.deleteTeacherToken.mockResolvedValue(undefined);
  mocks.dropElevation.mockResolvedValue(undefined);
  mocks.elevate.mockResolvedValue(undefined);
  mocks.refresh.mockResolvedValue(undefined);
});

describe('administrator elevation renewal', () => {
  it('offers a local token step-up when settings are opened without an active elevation', async () => {
    const rememberedToken = 'remembered-admin-token';
    saveRememberedToken('adminToken', rememberedToken);
    authState.elevated = false;

    renderSettings();

    expect(await screen.findByRole('heading', { name: 'Возобновление доступа' })).toBeInTheDocument();
    expect(screen.getByLabelText('Токен администратора')).toHaveValue(rememberedToken);
    expect(mocks.getSettings).not.toHaveBeenCalled();

    fireEvent.submit(screen.getByLabelText('Токен администратора').closest('form')!);
    await waitFor(() => expect(mocks.elevate).toHaveBeenCalledWith(rememberedToken));
  });

  it('replaces a stale capability error with a token form and reloads protected data after elevation', async () => {
    const rememberedToken = 'remembered-admin-token';
    saveRememberedToken('adminToken', rememberedToken);
    mocks.getSettings
      .mockRejectedValueOnce(new ApiError(403, 'CAPABILITY_REQUIRED', 'Missing SYSTEM_SETTINGS'))
      .mockResolvedValue(settings);

    renderSettings();

    expect(await screen.findByRole('heading', { name: 'Возобновление доступа' })).toBeInTheDocument();
    expect(screen.queryByText('Missing SYSTEM_SETTINGS')).not.toBeInTheDocument();
    expect(mocks.invalidateElevation).toHaveBeenCalledTimes(1);
    const tokenInput = screen.getByLabelText('Токен администратора');
    expect(tokenInput).toHaveAttribute('type', 'password');
    expect(tokenInput).toHaveValue(rememberedToken);

    fireEvent.submit(tokenInput.closest('form')!);

    await waitFor(() => expect(mocks.elevate).toHaveBeenCalledWith(rememberedToken));
    await waitFor(() => expect(mocks.getSettings).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole('heading', { name: 'Настройки системы' })).toBeInTheDocument();
    expect(loadRememberedTokens().adminToken).toBe(rememberedToken);
  });

  it('ignores a late capability failure from the previous elevation epoch', async () => {
    const oldCatalogRequest = deferred<CourseCatalogEntry[]>();
    mocks.getSettings
      .mockRejectedValueOnce(new ApiError(403, 'CAPABILITY_REQUIRED', 'Missing SYSTEM_SETTINGS'))
      .mockResolvedValue(settings);
    mocks.getCourseCatalog
      .mockImplementationOnce(() => oldCatalogRequest.promise)
      .mockResolvedValue([course]);
    renderSettings();

    const tokenInput = await screen.findByLabelText('Токен администратора');
    fireEvent.change(tokenInput, { target: { value: 'fresh-admin-token' } });
    fireEvent.submit(tokenInput.closest('form')!);

    await waitFor(() => expect(mocks.getCourseCatalog).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole('heading', { name: 'Настройки системы' })).toBeInTheDocument();

    oldCatalogRequest.reject(new ApiError(403, 'CAPABILITY_REQUIRED', 'Missing SYSTEM_SETTINGS'));

    await waitFor(() => expect(mocks.invalidateElevation).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole('heading', { name: 'Возобновление доступа' })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Настройки системы' })).toBeInTheDocument();
  });

  it('invalidates one expired elevation only once when parallel protected loads all return 403', async () => {
    const expired = new ApiError(403, 'CAPABILITY_REQUIRED', 'Missing SYSTEM_SETTINGS');
    mocks.getSettings.mockRejectedValueOnce(expired);
    mocks.getCourseCatalog.mockRejectedValueOnce(expired);
    mocks.getTeacherTokens.mockRejectedValueOnce(expired);

    renderSettings();

    expect(await screen.findByRole('heading', { name: 'Возобновление доступа' })).toBeInTheDocument();
    await waitFor(() => expect(mocks.invalidateElevation).toHaveBeenCalledTimes(1));
  });

  it('preserves locally edited settings when access expires during save', async () => {
    mocks.updateSettings.mockRejectedValueOnce(
      new ApiError(403, 'SYSTEM_SETTINGS_REQUIRED', 'System settings access is required'),
    );
    renderSettings();

    const banner = await screen.findByLabelText('Системное объявление');
    fireEvent.change(banner, { target: { value: 'Локальное несохранённое объявление' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Сохранить' }));

    expect(await screen.findByRole('heading', { name: 'Возобновление доступа' })).toBeInTheDocument();
    expect(screen.queryByText('Настройки не сохранены')).not.toBeInTheDocument();
    expect(mocks.invalidateElevation).toHaveBeenCalledTimes(1);

    const tokenInput = screen.getByLabelText('Токен администратора');
    fireEvent.change(tokenInput, { target: { value: 'fresh-admin-token' } });
    fireEvent.submit(tokenInput.closest('form')!);

    expect(await screen.findByLabelText('Системное объявление')).toHaveValue('Локальное несохранённое объявление');
    expect(mocks.getSettings).toHaveBeenCalledTimes(1);
  });
});

describe('administrator teacher token pool', () => {
  it('creates a token and shows the open value only in the one-time dialog', async () => {
    mocks.createTeacherToken.mockResolvedValue(issuedTeacherToken);
    renderSettings();

    const input = await screen.findByLabelText('Название токена преподавателя');
    fireEvent.change(input, { target: { value: teacherToken.label } });
    fireEvent.click(screen.getByRole('button', { name: 'Создать токен' }));

    await waitFor(() => expect(mocks.createTeacherToken).toHaveBeenCalledWith(teacherToken.label));
    const dialog = screen.getByRole('dialog', { name: 'Сохраните токен преподавателя' });
    expect(within(dialog).getByText(issuedTeacherToken.token)).toBeInTheDocument();
    expect(input).toHaveValue('');
    expect(mocks.getTeacherTokens).toHaveBeenCalledTimes(2);
  });

  it('copies the one-time token with the Clipboard API and confirms success', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const dialog = await openIssuedTeacherTokenDialog();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Скопировать' }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(issuedTeacherToken.token));
    expect(within(dialog).getByRole('button', { name: 'Скопировано' })).toBeInTheDocument();
    expect(within(dialog).getByRole('status')).toHaveTextContent('Токен скопирован в буфер обмена.');
  });

  it('uses the synchronous fallback when Clipboard API is unavailable over HTTP', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
    const execCommand = vi.fn().mockImplementation(() => {
      expect(document.activeElement).toBeInstanceOf(HTMLTextAreaElement);
      expect((document.activeElement as HTMLTextAreaElement).value).toBe(issuedTeacherToken.token);
      return true;
    });
    Object.defineProperty(document, 'execCommand', { configurable: true, value: execCommand });
    const dialog = await openIssuedTeacherTokenDialog();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Скопировать' }));

    await waitFor(() => expect(execCommand).toHaveBeenCalledWith('copy'));
    expect(within(dialog).getByRole('button', { name: 'Скопировано' })).toBeInTheDocument();
    expect(document.querySelector('textarea[aria-hidden="true"]')).not.toBeInTheDocument();
  });

  it('selects the token and explains manual copying when both methods fail', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('permission denied'));
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    Object.defineProperty(document, 'execCommand', { configurable: true, value: vi.fn().mockReturnValue(false) });
    const dialog = await openIssuedTeacherTokenDialog();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Скопировать' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('нажмите Ctrl+C или ⌘C');
    expect(within(dialog).getByRole('button', { name: 'Повторить копирование' })).toBeInTheDocument();
    expect(window.getSelection()?.toString()).toBe(issuedTeacherToken.token);
    expect(screen.getByText('Не удалось скопировать автоматически')).toBeInTheDocument();
  });

  it('reveals and hides a recoverable token only on administrator request', async () => {
    mocks.getTeacherTokens.mockResolvedValue([teacherToken]);
    renderSettings();

    expect(await screen.findByLabelText(`Значение токена ${teacherToken.label} скрыто`)).toHaveTextContent('••••••••');
    expect(screen.queryByText(issuedTeacherToken.token)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: `Показать токен ${teacherToken.label}` }));

    await waitFor(() => expect(mocks.revealTeacherToken).toHaveBeenCalledWith(teacherToken.id));
    expect(await screen.findByLabelText(`Значение токена ${teacherToken.label}`)).toHaveTextContent(issuedTeacherToken.token);
    fireEvent.click(screen.getByRole('button', { name: `Скрыть токен ${teacherToken.label}` }));
    expect(screen.queryByText(issuedTeacherToken.token)).not.toBeInTheDocument();
    expect(mocks.revealTeacherToken).toHaveBeenCalledTimes(1);
  });

  it('explains that a legacy hash-only token cannot be revealed but can be replaced', async () => {
    mocks.getTeacherTokens.mockResolvedValue([{ ...teacherToken, canReveal: false }]);
    renderSettings();

    expect(await screen.findByText('Значение старого токена недоступно — замените его')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: `Показать токен ${teacherToken.label}` })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: `Изменить токен ${teacherToken.label}` })).toBeInTheDocument();
  });

  it('validates an edited token and replaces it without deleting its Moodle binding', async () => {
    const replacement = 'Zx90Yw12';
    mocks.getTeacherTokens.mockResolvedValue([teacherToken]);
    mocks.updateTeacherToken.mockResolvedValue({
      ...teacherToken,
      canReveal: true,
      hashFingerprint: 'updatedhash00001',
    });
    renderSettings();

    fireEvent.click(await screen.findByRole('button', { name: `Изменить токен ${teacherToken.label}` }));
    const dialog = screen.getByRole('dialog', { name: 'Изменить токен преподавателя' });
    const input = within(dialog).getByLabelText('Новое значение токена преподавателя');
    fireEvent.change(input, { target: { value: 'with-dash' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Сохранить токен' }));

    expect(await within(dialog).findByText(/ровно из 8 латинских букв или цифр/)).toBeInTheDocument();
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(mocks.updateTeacherToken).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: replacement } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Сохранить токен' }));

    await waitFor(() => expect(mocks.updateTeacherToken).toHaveBeenCalledWith(teacherToken.id, replacement));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Изменить токен преподавателя' })).not.toBeInTheDocument());
    expect(screen.getByLabelText(`Значение токена ${teacherToken.label}`)).toHaveTextContent(replacement);
    expect(screen.getByText(`Привязан: ${teacherToken.boundDisplayName}`)).toBeInTheDocument();
  });

  it('requires confirmation before deleting a hash and demoting its user', async () => {
    mocks.getTeacherTokens.mockResolvedValue([teacherToken]);
    renderSettings();

    fireEvent.click(await screen.findByRole('button', { name: `Удалить токен ${teacherToken.label}` }));
    const dialog = screen.getByRole('dialog', { name: 'Отозвать токен преподавателя?' });
    expect(within(dialog).getByText(/пользователь немедленно потеряет роль преподавателя/)).toBeInTheDocument();
    expect(mocks.deleteTeacherToken).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Отозвать токен' }));
    await waitFor(() => expect(mocks.deleteTeacherToken).toHaveBeenCalledWith(teacherToken.id));
    await waitFor(() => expect(screen.queryByText(teacherToken.label)).not.toBeInTheDocument());
    expect(mocks.refresh).toHaveBeenCalledOnce();
  });
});

afterEach(cleanup);

describe('administrator course catalog settings', () => {
  it('shows the global course name, LMS connection and external id', async () => {
    renderSettings();

    expect(await screen.findByText(course.title)).toBeInTheDocument();
    expect(screen.getByText('MMCS Moodle')).toBeInTheDocument();
    expect(screen.getByText('ID в LMS: 549')).toBeInTheDocument();
    expect(screen.getByText('Актуален')).toBeInTheDocument();
  });

  it('labels an active catalog update as synchronization in progress', async () => {
    mocks.getCourseCatalog.mockResolvedValue([{ ...course, syncStatus: 'SYNCING' }]);
    renderSettings();

    expect(await screen.findByText('Синхронизируется')).toBeInTheDocument();
    expect(screen.queryByText('Ожидает синхронизации')).not.toBeInTheDocument();
  });

  it('opens synchronization diagnostics from the catalog badge', async () => {
    mocks.getCourseCatalog.mockResolvedValue([{
      ...course,
      syncStatus: 'ERROR',
      syncError: {
        code: 'TIMEOUT', message: 'course discovery exceeded timeout',
        at: '2026-08-27T18:00:00Z', retryable: true,
      },
    }]);
    renderSettings();

    fireEvent.click(await screen.findByRole('button', { name: /Показать ошибку синхронизации/ }));
    expect(screen.getByRole('heading', { name: 'Ошибка синхронизации Moodle' })).toBeInTheDocument();
    expect(screen.getByText('Moodle отвечал слишком долго')).toBeInTheDocument();
    expect(screen.getByText('course discovery exceeded timeout')).toBeInTheDocument();
  });

  it('discovers and confirms a Moodle URL, reloads the catalog and refreshes the session', async () => {
    let entries = [course];
    const added: CourseCatalogEntry = {
      ...course,
      id: 'course-2',
      externalId: '777',
      title: 'Новый курс C++',
      externalUrl: 'https://edu.mmcs.sfedu.ru/course/view.php?id=777',
      syncStatus: 'STALE',
    };
    mocks.getCourseCatalog.mockImplementation(() => Promise.resolve(entries));
    mocks.createCourseImport.mockResolvedValue({
      id: 'job-1', state: 'DISCOVERED', externalCourseId: '777',
      preview: { title: added.title }, capabilityReport: {},
    });
    mocks.confirmCourseImport.mockImplementation(async () => {
      entries = [...entries, added];
      return { id: 'job-1', state: 'CONFIRMED', externalCourseId: '777', preview: {}, capabilityReport: {}, confirmedCourse: added.id };
    });
    renderSettings();

    const input = await screen.findByLabelText('Ссылка на курс Moodle');
    fireEvent.change(input, { target: { value: added.externalUrl } });
    fireEvent.click(screen.getByRole('button', { name: 'Добавить курс' }));

    await waitFor(() => expect(mocks.createCourseImport).toHaveBeenCalledWith(added.externalUrl));
    expect(mocks.confirmCourseImport).toHaveBeenCalledWith('job-1');
    expect(await screen.findByText(added.title)).toBeInTheDocument();
    expect(screen.getAllByText(added.title)).toHaveLength(1);
    expect(input).toHaveValue('');
    expect(mocks.refresh).toHaveBeenCalledOnce();
  });

  it('keeps a confirmed course visible when the first catalog refresh is stale', async () => {
    const staleCatalogRefresh = deferred<CourseCatalogEntry[]>();
    const added: CourseCatalogEntry = {
      ...course,
      id: 'course-2',
      externalId: '777',
      title: 'Новый курс C++',
      shortName: 'NEW C++',
      externalUrl: 'https://edu.mmcs.sfedu.ru/course/view.php?id=777',
      syncStatus: 'STALE',
    };
    mocks.getCourseCatalog
      .mockResolvedValueOnce([course])
      .mockReturnValueOnce(staleCatalogRefresh.promise);
    mocks.createCourseImport.mockResolvedValue({
      id: 'job-1', state: 'DISCOVERED', externalCourseId: added.externalId,
      preview: { title: added.title, short_name: added.shortName }, capabilityReport: {},
    });
    mocks.confirmCourseImport.mockResolvedValue({
      id: 'job-1', state: 'CONFIRMED', externalCourseId: added.externalId,
      preview: {}, capabilityReport: {}, confirmedCourse: added.id,
    });
    renderSettings();

    const input = await screen.findByLabelText('Ссылка на курс Moodle');
    await screen.findByText(course.title);
    fireEvent.change(input, { target: { value: added.externalUrl } });
    fireEvent.click(screen.getByRole('button', { name: 'Добавить курс' }));

    await waitFor(() => expect(mocks.confirmCourseImport).toHaveBeenCalledWith('job-1'));
    expect(await screen.findByText(added.title)).toBeInTheDocument();
    expect(screen.getByText(added.shortName)).toBeInTheDocument();
    expect(mocks.getCourseCatalog).toHaveBeenCalledTimes(2);

    staleCatalogRefresh.resolve([course]);

    await waitFor(() => expect(input).not.toBeDisabled());
    expect(screen.getAllByText(added.title)).toHaveLength(1);
    expect(screen.getByText(added.title)).toBeInTheDocument();
    expect(mocks.refresh).toHaveBeenCalledOnce();
  });

  it('rejects a URL that is not a Moodle course before calling the API', async () => {
    renderSettings();

    const input = await screen.findByLabelText('Ссылка на курс Moodle');
    fireEvent.change(input, { target: { value: 'https://edu.mmcs.sfedu.ru/user/profile.php?id=549' } });
    fireEvent.click(screen.getByRole('button', { name: 'Добавить курс' }));

    expect(await screen.findByText('Ожидается ссылка вида …/course/view.php?id=549')).toBeInTheDocument();
    expect(mocks.createCourseImport).not.toHaveBeenCalled();
  });

  it('requires confirmation before removing a course from every user', async () => {
    const deletion = deferred<void>();
    mocks.deleteCourseCatalogEntry.mockReturnValueOnce(deletion.promise);
    renderSettings();

    fireEvent.click(await screen.findByRole('button', { name: `Убрать курс ${course.title} из системы` }));
    const dialog = screen.getByRole('dialog', { name: 'Убрать курс из системы?' });
    expect(within(dialog).getByText(/Курс исчезнет у всех пользователей/)).toBeInTheDocument();
    expect(mocks.deleteCourseCatalogEntry).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Убрать из системы' }));
    await waitFor(() => expect(mocks.deleteCourseCatalogEntry).toHaveBeenCalledWith(course.id));
    expect(screen.queryByRole('dialog', { name: 'Убрать курс из системы?' })).not.toBeInTheDocument();
    expect(mocks.deleteCourseCatalogEntry).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: `Убрать курс ${course.title} из системы` })).toBeDisabled();

    deletion.resolve(undefined);
    await waitFor(() => expect(screen.queryByText(course.title)).not.toBeInTheDocument());
    expect(mocks.refresh).toHaveBeenCalledOnce();
  });
});
