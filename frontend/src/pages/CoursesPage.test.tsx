import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '../components/ui';
import { CoursesPage } from './CoursesPage';

const mocks = vi.hoisted(() => ({
  getCourses: vi.fn(),
  getAssessments: vi.fn(),
  syncCourse: vi.fn(),
  getCourseSyncStatus: vi.fn(),
  getAssessmentSyncStatuses: vi.fn(),
  syncAssessment: vi.fn(),
}));
const auth = vi.hoisted(() => ({
  primaryRole: 'TEACHER' as 'TEACHER' | 'STUDENT',
  session: {
    id: 'teacher-1', displayName: 'Преподаватель', providerName: 'MMCS Moodle',
    memberships: [], capabilities: ['course.view', 'assessment.manage'],
  },
}));

vi.mock('../lib/api', () => ({
  api: {
    getCourses: mocks.getCourses,
    getAssessments: mocks.getAssessments,
    syncCourse: mocks.syncCourse,
    getCourseSyncStatus: mocks.getCourseSyncStatus,
    getAssessmentSyncStatuses: mocks.getAssessmentSyncStatuses,
    syncAssessment: mocks.syncAssessment,
  },
}));

vi.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    session: auth.session,
    primaryRole: auth.primaryRole,
  }),
}));

beforeEach(() => {
  Object.values(mocks).forEach((mock) => mock.mockReset());
  auth.primaryRole = 'TEACHER';
  mocks.getCourses.mockResolvedValue([]);
  mocks.getAssessments.mockResolvedValue([]);
  mocks.getAssessmentSyncStatuses.mockResolvedValue([]);
  mocks.getCourseSyncStatus.mockImplementation((courseId) => Promise.resolve({ courseId, syncStatus: 'SYNCING' }));
});

afterEach(cleanup);

describe('teacher courses page', () => {
  const course = { id: 'course-1', title: 'C++', shortName: 'C++', syncStatus: 'SYNCED', role: 'TEACHER' };
  const assessment = { id: 'assessment-1', courseId: 'course-1', title: 'Работа 1', kind: 'LAB', status: 'AVAILABLE', standard: 'C++20', fileMode: 'SINGLE' };

  it('labels and filters a published Moodle work with every group revoked as not enabled', async () => {
    mocks.getCourses.mockResolvedValue([course]);
    mocks.getAssessments.mockResolvedValue([{ ...assessment, publicationStatus: 'PUBLISHED', policy: { moodle_metadata_read_only: true }, availabilityRules: [] }]);
    render(<MemoryRouter><ToastProvider><CoursesPage /></ToastProvider></MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: 'Развернуть список работ курса C++' }));
    expect(screen.getByRole('link', { name: /Работа 1.*Не включена/ })).toBeVisible();
    const filter = screen.getAllByRole('combobox')[1];
    fireEvent.change(filter, { target: { value: 'AVAILABLE' } });
    expect(screen.queryByRole('link', { name: /Работа 1/ })).not.toBeInTheDocument();
    fireEvent.change(filter, { target: { value: 'DRAFT' } });
    expect(screen.getByRole('link', { name: /Работа 1.*Не включена/ })).toBeVisible();
  });

  it.each([
    ['IDLE', '', 'Ответы ещё не синхронизировались'],
    ['COMPLETED', 'work-sync-button--success', 'Ответы синхронизированы'],
    ['FAILED', 'work-sync-button--failed', 'Ошибка синхронизации ответов'],
    ['PARTIAL', 'work-sync-button--failed', 'Синхронизировано с предупреждениями'],
    ['SYNCING', '', 'Ответы синхронизируются'],
  ])('shows the last synchronization outcome %s on its own button', async (status, tone, description) => {
    mocks.getCourses.mockResolvedValue([course]);
    mocks.getAssessments.mockResolvedValue([assessment]);
    mocks.getAssessmentSyncStatuses.mockResolvedValue([{
      assessmentId: assessment.id, status,
      lastError: 'Не удалось скачать файл.', errorCode: 'ARTIFACT_OMITTED',
    }]);
    render(<MemoryRouter><ToastProvider><CoursesPage /></ToastProvider></MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: 'Развернуть список работ курса C++' }));
    const button = screen.getByRole('button', { name: 'Синхронизировать ответы: Работа 1' });
    expect(button.title).toContain(description);
    if (tone) expect(button).toHaveClass(tone);
    else expect(button).not.toHaveClass('work-sync-button--success', 'work-sync-button--failed');
    expect(Boolean(button.querySelector('.spin'))).toBe(status === 'SYNCING');
    if (status === 'PARTIAL' || status === 'FAILED') expect(button.title).toContain('ARTIFACT_OMITTED');
  });

  it('shows partial import warnings without treating them as a failed or busy operation', async () => {
    mocks.getCourses.mockResolvedValue([course]);
    mocks.getAssessments.mockResolvedValue([assessment]);
    mocks.getAssessmentSyncStatuses.mockResolvedValue([{
      assessmentId: 'assessment-1', status: 'PARTIAL', errorCode: 'ARTIFACT_OMITTED',
      lastError: 'Часть файлов ответов не удалось скачать из Moodle.',
    }]);
    render(<MemoryRouter><ToastProvider><CoursesPage /></ToastProvider></MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: 'Развернуть список работ курса C++' }));
    const sync = screen.getByRole('button', { name: 'Синхронизировать ответы: Работа 1' });
    expect(sync).toBeEnabled();
    expect(sync).toHaveClass('work-sync-button--failed');
    expect(sync).not.toHaveClass('work-sync-button--success');
    expect(sync.title).toContain('ARTIFACT_OMITTED');
    expect(sync.title).toContain('Загруженные работы можно проверять');
    expect(screen.getByRole('button', { name: 'Синхронизировать курс C++' })).toBeEnabled();
  });

  it('starts all courses collapsed and toggles each independently while keeping header actions available', async () => {
    mocks.getCourses.mockResolvedValue([course, { ...course, id: 'course-2', title: 'Python' }]);
    mocks.getAssessments.mockImplementation((id) => Promise.resolve([
      { ...assessment, id: `assessment-${id}`, courseId: id, title: id === 'course-1' ? 'Работа C++' : 'Работа Python' },
    ]));
    render(<MemoryRouter><ToastProvider><CoursesPage /></ToastProvider></MemoryRouter>);
    const toggle = await screen.findByRole('button', { name: 'Развернуть список работ курса C++' });
    const panel = document.getElementById(toggle.getAttribute('aria-controls')!);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(panel).not.toBeVisible();
    expect(screen.queryByRole('link', { name: /Работа C\+\+/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Синхронизировать ответы: Работа C++' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Работа Python/ })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'C++' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Синхронизировать курс C++' })).toBeEnabled();

    fireEvent.click(toggle);
    expect(toggle).toHaveAccessibleName('Свернуть список работ курса C++');
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(panel).toBeVisible();
    expect(screen.getByRole('link', { name: /Работа C\+\+/ })).toBeVisible();
    expect(screen.queryByRole('link', { name: /Работа Python/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Развернуть список работ курса Python' }));
    fireEvent.click(toggle);
    expect(panel).not.toBeVisible();
    expect(screen.getByRole('link', { name: /Работа Python/ })).toBeVisible();
    expect(mocks.syncCourse).not.toHaveBeenCalled();
    expect(mocks.syncAssessment).not.toHaveBeenCalled();
  });

  it('preserves expanded and collapsed states across refreshes and starts new courses collapsed', async () => {
    vi.useFakeTimers();
    try {
      mocks.getCourses.mockResolvedValue([course]);
      mocks.getAssessments.mockResolvedValue([assessment]);
      render(<MemoryRouter><ToastProvider><CoursesPage /></ToastProvider></MemoryRouter>);
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      mocks.getAssessments.mockResolvedValue([assessment, { ...assessment, id: 'assessment-2', title: 'Новая работа' }]);
      await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
      const toggle = screen.getByRole('button', { name: 'Развернуть список работ курса C++' });
      expect(toggle).toHaveAttribute('aria-expanded', 'false');
      expect(screen.queryByRole('link', { name: /Новая работа/ })).not.toBeInTheDocument();
      fireEvent.click(toggle);
      expect(screen.getByRole('link', { name: /Новая работа/ })).toBeVisible();
      mocks.getCourses.mockResolvedValue([course, { ...course, id: 'course-2', title: 'Python' }]);
      mocks.getAssessments.mockImplementation((id) => Promise.resolve([
        { ...assessment, id: `assessment-${id}`, courseId: id, title: id === 'course-1' ? 'Работа C++' : 'Работа Python' },
      ]));
      await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
      expect(toggle).toHaveAttribute('aria-expanded', 'true');
      expect(screen.getByRole('link', { name: /Работа C\+\+/ })).toBeVisible();
      expect(screen.getByRole('button', { name: 'Развернуть список работ курса Python' })).toHaveAttribute('aria-expanded', 'false');
      expect(screen.queryByRole('link', { name: /Работа Python/ })).not.toBeInTheDocument();
    } finally { cleanup(); vi.useRealTimers(); }
  });

  it('can collapse and expand an empty course too', async () => {
    mocks.getCourses.mockResolvedValue([course]);
    render(<MemoryRouter><ToastProvider><CoursesPage /></ToastProvider></MemoryRouter>);
    const toggle = await screen.findByRole('button', { name: 'Развернуть список работ курса C++' });
    expect(screen.queryByRole('heading', { name: 'В курсе пока нет работ' })).not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(screen.getByRole('heading', { name: 'В курсе пока нет работ' })).toBeVisible();
    fireEvent.click(toggle);
    expect(screen.queryByRole('heading', { name: 'В курсе пока нет работ' })).not.toBeInTheDocument();
  });

  it('offers separate manual course and answer sync without starting either on page load', async () => {
    mocks.getCourses.mockResolvedValue([course]);
    mocks.getAssessments.mockResolvedValue([assessment]);
    render(<MemoryRouter><ToastProvider><CoursesPage /></ToastProvider></MemoryRouter>);
    expect(await screen.findByRole('button', { name: 'Синхронизировать курс C++' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Развернуть список работ курса C++' }));
    const sync = screen.getByRole('button', { name: 'Синхронизировать ответы: Работа 1' });
    expect(sync).toBeEnabled();
    expect(sync.closest('a')).toBeNull();
    expect(mocks.syncCourse).not.toHaveBeenCalled();
    expect(mocks.syncAssessment).not.toHaveBeenCalled();
    expect(screen.getByText(/Ответы студентов загружаются отдельно/)).toBeInTheDocument();
  });

  it('blocks only the matching course during course sync, including duplicate clicks', async () => {
    mocks.getCourses.mockResolvedValue([course, { ...course, id: 'course-2', title: 'Python' }]);
    mocks.getAssessments.mockImplementation((id) => Promise.resolve([{ ...assessment, id: `assessment-${id}`, courseId: id, title: id }]));
    let finish!: (value: unknown) => void;
    mocks.syncCourse.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    render(<MemoryRouter><ToastProvider><CoursesPage /></ToastProvider></MemoryRouter>);
    const button = await screen.findByRole('button', { name: 'Синхронизировать курс C++' });
    fireEvent.click(screen.getByRole('button', { name: 'Развернуть список работ курса C++' }));
    fireEvent.click(screen.getByRole('button', { name: 'Развернуть список работ курса Python' }));
    fireEvent.click(button);
    fireEvent.click(button);
    expect(mocks.syncCourse).toHaveBeenCalledTimes(1);
    expect(button).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Синхронизировать ответы: course-1' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Синхронизировать курс Python' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Синхронизировать ответы: course-2' })).toBeEnabled();
    await act(async () => finish({ ...course, syncStatus: 'SYNCING' }));
    expect(button).toBeDisabled();
    expect(mocks.syncAssessment).not.toHaveBeenCalled();
  });

  it('starts one answer sync, blocks its course but permits another work in that course', async () => {
    mocks.getCourses.mockResolvedValue([course]);
    mocks.getAssessments.mockResolvedValue([assessment, { ...assessment, id: 'assessment-2', title: 'Работа 2' }]);
    mocks.syncAssessment.mockResolvedValue({ assessmentId: 'assessment-1', status: 'SYNCING' });
    render(<MemoryRouter><ToastProvider><CoursesPage /></ToastProvider></MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: 'Развернуть список работ курса C++' }));
    const button = screen.getByRole('button', { name: 'Синхронизировать ответы: Работа 1' });
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(mocks.syncAssessment).toHaveBeenCalledTimes(1));
    expect(button).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Синхронизировать курс C++' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Синхронизировать ответы: Работа 2' })).toBeEnabled();
  });

  it('polls shared task states to reflect another teacher and unlock completed jobs', async () => {
    vi.useFakeTimers();
    try {
      mocks.getCourses.mockResolvedValue([course]);
      mocks.getAssessments.mockResolvedValue([assessment]);
      mocks.getAssessmentSyncStatuses.mockResolvedValue([{ assessmentId: 'assessment-1', status: 'SYNCING' }]);
      render(<MemoryRouter><ToastProvider><CoursesPage /></ToastProvider></MemoryRouter>);
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      fireEvent.click(screen.getByRole('button', { name: 'Развернуть список работ курса C++' }));
      expect(screen.getByRole('button', { name: 'Синхронизировать ответы: Работа 1' })).toBeDisabled();
      mocks.getAssessmentSyncStatuses.mockResolvedValue([{ assessmentId: 'assessment-1', status: 'COMPLETED' }]);
      await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
      expect(screen.getByRole('button', { name: 'Синхронизировать ответы: Работа 1' })).toBeEnabled();
      expect(screen.getByRole('button', { name: 'Синхронизировать курс C++' })).toBeEnabled();
      expect(mocks.syncCourse).not.toHaveBeenCalled();
      expect(mocks.syncAssessment).not.toHaveBeenCalled();
    } finally { cleanup(); vi.useRealTimers(); }
  });

  it('shows course completion while a background work-list read is still pending, ignoring its stale status', async () => {
    vi.useFakeTimers();
    try {
      mocks.getCourses.mockResolvedValue([{ ...course, syncStatus: 'SYNCING' }]);
      mocks.getAssessments.mockResolvedValue([assessment]);
      render(<MemoryRouter><ToastProvider><CoursesPage /></ToastProvider></MemoryRouter>);
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      const sync = screen.getByRole('button', { name: 'Синхронизировать курс C++' });
      expect(sync).toBeDisabled();
      let finishRead!: (value: unknown[]) => void;
      mocks.getAssessments.mockReturnValueOnce(new Promise((resolve) => { finishRead = resolve; }));
      await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
      mocks.getCourseSyncStatus.mockResolvedValue({ courseId: course.id, syncStatus: 'SYNCED' });
      await act(async () => { await vi.advanceTimersByTimeAsync(1_500); });
      expect(sync).toBeEnabled();
      expect(sync.querySelector('.spin')).toBeNull();
      expect(screen.getByText('Список курса загружен')).toBeInTheDocument();
      await act(async () => { finishRead([assessment]); });
      expect(sync).toBeEnabled();
      expect(screen.queryByText('Синхронизируется')).not.toBeInTheDocument();
      expect(mocks.syncCourse).not.toHaveBeenCalled();
    } finally { cleanup(); vi.useRealTimers(); }
  });

  it('tracks a newly started course without waiting for the next full catalog refresh', async () => {
    vi.useFakeTimers();
    try {
      mocks.getCourses.mockResolvedValue([course]);
      mocks.getAssessments.mockResolvedValue([assessment]);
      mocks.syncCourse.mockResolvedValue({ ...course, syncStatus: 'SYNCING' });
      render(<MemoryRouter><ToastProvider><CoursesPage /></ToastProvider></MemoryRouter>);
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      const sync = screen.getByRole('button', { name: 'Синхронизировать курс C++' });
      await act(async () => { fireEvent.click(sync); });
      expect(sync).toBeDisabled();
      mocks.getCourseSyncStatus.mockResolvedValue({ courseId: course.id, syncStatus: 'SYNCED' });
      await act(async () => { await vi.advanceTimersByTimeAsync(1_500); });
      expect(sync).toBeEnabled();
      expect(mocks.syncCourse).toHaveBeenCalledTimes(1);
    } finally { cleanup(); vi.useRealTimers(); }
  });

  it('does not delay a completed course behind a slow status request for another course', async () => {
    vi.useFakeTimers();
    try {
      mocks.getCourses.mockResolvedValueOnce([
        { ...course, syncStatus: 'SYNCING' },
        { ...course, id: 'course-2', title: 'Python', syncStatus: 'SYNCING' },
      ]).mockResolvedValue([
        course,
        { ...course, id: 'course-2', title: 'Python', syncStatus: 'SYNCING' },
      ]);
      mocks.getCourseSyncStatus.mockImplementation((id) => id === course.id
        ? Promise.resolve({ courseId: id, syncStatus: 'SYNCED' })
        : new Promise(() => {}));
      render(<MemoryRouter><ToastProvider><CoursesPage /></ToastProvider></MemoryRouter>);
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      expect(screen.getByRole('button', { name: 'Синхронизировать курс C++' })).toBeEnabled();
      expect(screen.getByRole('button', { name: 'Синхронизировать курс Python' })).toBeDisabled();
    } finally { cleanup(); vi.useRealTimers(); }
  });

  it('retries status reads after a transient error and checks immediately on window focus', async () => {
    vi.useFakeTimers();
    try {
      mocks.getCourses.mockResolvedValueOnce([{ ...course, syncStatus: 'SYNCING' }]).mockResolvedValue([course]);
      mocks.getCourseSyncStatus.mockRejectedValueOnce(new Error('Нет соединения'));
      render(<MemoryRouter><ToastProvider><CoursesPage /></ToastProvider></MemoryRouter>);
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      const sync = screen.getByRole('button', { name: 'Синхронизировать курс C++' });
      expect(sync).toBeDisabled();
      await act(async () => { await vi.advanceTimersByTimeAsync(1_500); });
      expect(mocks.getCourseSyncStatus).toHaveBeenCalledTimes(2);
      mocks.getCourseSyncStatus.mockResolvedValue({ courseId: course.id, syncStatus: 'SYNCED' });
      await act(async () => { window.dispatchEvent(new Event('focus')); });
      expect(sync).toBeEnabled();
      expect(mocks.syncCourse).not.toHaveBeenCalled();
      const calls = mocks.getCourseSyncStatus.mock.calls.length;
      cleanup();
      await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
      expect(mocks.getCourseSyncStatus).toHaveBeenCalledTimes(calls);
    } finally { cleanup(); vi.useRealTimers(); }
  });

  it('refreshes on returning to the tab and shows a failed sync without continuing to spin', async () => {
    vi.useFakeTimers();
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
    try {
      mocks.getCourses.mockResolvedValue([{ ...course, syncStatus: 'SYNCING' }]);
      render(<MemoryRouter><ToastProvider><CoursesPage /></ToastProvider></MemoryRouter>);
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      hidden.mockReturnValue(true);
      await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
      expect(mocks.getCourseSyncStatus).toHaveBeenCalledTimes(1);
      const failed = { ...course, syncStatus: 'ERROR', syncError: { code: 'UNAVAILABLE', message: 'Moodle не ответил', retryable: true } };
      mocks.getCourses.mockResolvedValue([failed]);
      mocks.getCourseSyncStatus.mockResolvedValue({ courseId: course.id, syncStatus: failed.syncStatus, syncError: failed.syncError });
      hidden.mockReturnValue(false);
      await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
      expect(screen.getByRole('button', { name: 'Показать ошибку синхронизации курса C++' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Синхронизировать курс C++' }).querySelector('.spin')).toBeNull();
      expect(mocks.syncCourse).not.toHaveBeenCalled();
    } finally { cleanup(); hidden.mockRestore(); vi.useRealTimers(); }
  });

  it('leaves work links usable after sync status polling fails but disables sync commands', async () => {
    vi.useFakeTimers();
    try {
      mocks.getCourses.mockResolvedValue([course]);
      mocks.getAssessments.mockResolvedValue([assessment]);
      render(<MemoryRouter><ToastProvider><CoursesPage /></ToastProvider></MemoryRouter>);
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      fireEvent.click(screen.getByRole('button', { name: 'Развернуть список работ курса C++' }));
      mocks.getAssessmentSyncStatuses.mockRejectedValue(new Error('Нет соединения'));
      await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
      expect(screen.getByRole('link', { name: /Работа 1/ })).toHaveAttribute('href', '/assessments/assessment-1');
      expect(screen.getByRole('button', { name: 'Синхронизировать ответы: Работа 1' })).toBeDisabled();
      expect(screen.getByText('Нет соединения')).toBeInTheDocument();
    } finally { cleanup(); vi.useRealTimers(); }
  });
  it('does not expose global course import outside system settings', async () => {
    render(<MemoryRouter><ToastProvider><CoursesPage /></ToastProvider></MemoryRouter>);

    expect(await screen.findByRole('heading', { name: 'Курсы и работы' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Добавить курс по ссылке/ })).not.toBeInTheDocument();
    expect(screen.queryByText('Подключение курса')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Создать работу/ })).not.toBeInTheDocument();
  });

  it('opens the persisted Moodle synchronization error from its badge', async () => {
    mocks.getCourses.mockResolvedValue([{
      id: 'course-1', title: 'Программирование на C++', shortName: 'C++',
      syncStatus: 'ERROR', role: 'TEACHER',
      syncError: {
        code: 'UNAVAILABLE', message: 'External HTTP request timed out',
        at: '2026-08-27T18:00:00Z', retryable: true,
      },
    }]);

    render(<MemoryRouter><ToastProvider><CoursesPage /></ToastProvider></MemoryRouter>);
    const badge = await screen.findByRole('button', { name: /Показать ошибку синхронизации/ });
    fireEvent.click(badge);

    expect(screen.getByRole('heading', { name: 'Ошибка синхронизации Moodle' })).toBeInTheDocument();
    expect(screen.getByText('Moodle или браузерный коннектор не ответил')).toBeInTheDocument();
    expect(screen.getByText('External HTTP request timed out')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Повторить синхронизацию/ })).toBeInTheDocument();
  });
});

describe('student courses page Moodle attempt admission', () => {
  it('does not bypass live preparation for a managed active attempt', async () => {
    auth.primaryRole = 'STUDENT';
    mocks.getCourses.mockResolvedValue([{
      id: 'course-1', title: 'Программирование на C++', shortName: 'C++',
      role: 'STUDENT', syncStatus: 'SYNCED',
    }]);
    mocks.getAssessments.mockResolvedValue([{
      id: 'assessment-1', courseId: 'course-1', title: 'Самостоятельная работа №1',
      summary: '', kind: 'INDEPENDENT', status: 'IN_PROGRESS', attemptId: 'attempt-legacy',
      maxScore: 5, fileMode: 'SINGLE', language: 'CPP', standard: 'C++20',
      pastePolicy: 'STRICT', aiEnabled: false, requiresLiveLmsPreparation: true,
    }]);

    render(<MemoryRouter><ToastProvider><CoursesPage /></ToastProvider></MemoryRouter>);

    const toggle = await screen.findByRole('button', { name: 'Развернуть список работ курса Программирование на C++' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('link', { name: /Самостоятельная работа №1/ })).not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(screen.getByRole('link', { name: /Самостоятельная работа №1/ }))
      .toHaveAttribute('href', '/assessments/assessment-1');
    expect(screen.queryByRole('button', { name: /Синхронизировать/ })).not.toBeInTheDocument();
    expect(mocks.getAssessmentSyncStatuses).not.toHaveBeenCalled();
    fireEvent.click(toggle);
    expect(screen.queryByRole('link', { name: /Самостоятельная работа №1/ })).not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(screen.getByRole('link', { name: /Самостоятельная работа №1/ }))
      .toHaveAttribute('href', '/assessments/assessment-1');
  });
});
