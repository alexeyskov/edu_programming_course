import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DashboardPage } from './DashboardPage';

const mocks = vi.hoisted(() => ({
  getCourses: vi.fn(),
  getAssessments: vi.fn(),
  getCourseSyncStatus: vi.fn(),
  syncCourse: vi.fn(),
}));
const auth = vi.hoisted(() => ({
  primaryRole: 'TEACHER' as 'TEACHER' | 'STUDENT',
  session: {
    id: 'teacher-1',
    displayName: 'Преподаватель',
    providerName: 'MMCS Moodle',
    memberships: [{ courseId: 'course-1', courseName: 'C++', role: 'TEACHER' as const }],
    capabilities: ['course.view', 'course.sync'],
  },
}));

vi.mock('../lib/api', () => ({ api: mocks }));

vi.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    session: auth.session,
    primaryRole: auth.primaryRole,
  }),
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((promiseResolve) => { resolve = promiseResolve; });
  return { promise, resolve };
}

const syncingCourse = {
  id: 'course-1', title: 'Программирование на C++', shortName: 'C++',
  role: 'TEACHER', term: '', provider: 'Moodle', syncStatus: 'SYNCING',
} as const;

beforeEach(() => {
  Object.values(mocks).forEach((mock) => mock.mockReset());
  auth.primaryRole = 'TEACHER';
  mocks.getAssessments.mockResolvedValue([]);
});

afterEach(cleanup);

describe('teacher dashboard Moodle synchronization recovery', () => {
  it('shows each course own count even when their titles are identical', async () => {
    mocks.getCourses.mockResolvedValue([
      { ...syncingCourse, id: 'course-1', syncStatus: 'SYNCED', activeCount: 42 },
      { ...syncingCourse, id: 'course-2', syncStatus: 'SYNCED', activeCount: 17 },
    ]);
    render(<MemoryRouter><DashboardPage /></MemoryRouter>);
    const links = await screen.findAllByRole('link', { name: /Программирование на C\+\+/ });
    expect(links).toHaveLength(2);
    expect(within(links[0]).getByText(/42 работ/)).toBeInTheDocument();
    expect(within(links[1]).getByText(/17 работ/)).toBeInTheDocument();
    expect(within(links[0]).queryByText(/17 работ/)).not.toBeInTheDocument();
    expect(within(links[1]).queryByText(/42 работ/)).not.toBeInTheDocument();
  });

  it('reads an existing course job without blocking navigation or launching bulk synchronization', async () => {
    const status = deferred<{
      courseId: string;
      syncStatus: 'ERROR';
      syncError: { code: string; message: string; retryable: boolean };
    }>();
    const failedCourse = {
      ...syncingCourse,
      syncStatus: 'ERROR' as const,
      syncError: {
        code: 'UNAVAILABLE',
        message: 'Moodle browser session is busy',
        retryable: true,
      },
    };
    mocks.getCourses
      .mockResolvedValueOnce([syncingCourse])
      .mockResolvedValueOnce([failedCourse]);
    mocks.getCourseSyncStatus.mockReturnValue(status.promise);

    render(<MemoryRouter><DashboardPage /></MemoryRouter>);

    const link = await screen.findByRole('link', { name: 'Синхронизация курсов и работ' });
    expect(link).toHaveAttribute('href', '/courses');
    expect(link).not.toHaveAttribute('aria-disabled', 'true');
    expect(screen.getByText(/Другие курсы доступны для ручного обновления/)).toBeInTheDocument();
    expect(screen.getByText('Синхронизируется')).toBeInTheDocument();
    expect(screen.queryByText('Нужна синхронизация')).not.toBeInTheDocument();
    await waitFor(() => expect(mocks.getCourseSyncStatus).toHaveBeenCalledWith('course-1'));

    status.resolve({
      courseId: 'course-1',
      syncStatus: 'ERROR',
      syncError: {
        code: 'UNAVAILABLE',
        message: 'Moodle browser session is busy',
        retryable: true,
      },
    });

    await waitFor(() => expect(mocks.getCourses).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/Moodle browser session is busy/)).toBeInTheDocument();
    expect(screen.getByText(/Повторите синхронизацию нужного курса/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Синхронизация курсов и работ' })).toHaveAttribute('href', '/courses');
    expect(screen.queryByRole('button', { name: /Синхронизировать|Повторить/ })).not.toBeInTheDocument();
    expect(mocks.syncCourse).not.toHaveBeenCalled();
  });

  it('offers manual navigation for idle courses without a bulk mutation button', async () => {
    mocks.getCourses.mockResolvedValue([{ ...syncingCourse, syncStatus: 'SYNCED' }]);
    render(<MemoryRouter><DashboardPage /></MemoryRouter>);
    expect(await screen.findByRole('link', { name: 'Синхронизация курсов и работ' })).toHaveAttribute('href', '/courses');
    expect(screen.queryByRole('button', { name: /Синхронизировать/ })).not.toBeInTheDocument();
    expect(mocks.syncCourse).not.toHaveBeenCalled();
    expect(mocks.getCourseSyncStatus).not.toHaveBeenCalled();
  });
});

describe('student dashboard Moodle attempt admission', () => {
  it('routes an active managed work through live Moodle preparation instead of the IDE', async () => {
    auth.primaryRole = 'STUDENT';
    mocks.getCourses.mockResolvedValue([{
      id: 'course-1', title: 'Программирование на C++', shortName: 'C++',
      role: 'STUDENT', term: '', provider: 'Moodle', syncStatus: 'SYNCED',
    }]);
    mocks.getAssessments.mockResolvedValue([{
      id: 'assessment-1', courseId: 'course-1', title: 'Самостоятельная работа №1',
      summary: '', kind: 'INDEPENDENT', status: 'IN_PROGRESS', attemptId: 'attempt-legacy',
      maxScore: 5, fileMode: 'SINGLE', language: 'CPP', standard: 'C++20',
      pastePolicy: 'STRICT', aiEnabled: false, requiresLiveLmsPreparation: true,
    }]);

    render(<MemoryRouter><DashboardPage /></MemoryRouter>);

    expect(await screen.findByRole('link', { name: /Продолжить работу/ }))
      .toHaveAttribute('href', '/assessments/assessment-1');
  });
});
