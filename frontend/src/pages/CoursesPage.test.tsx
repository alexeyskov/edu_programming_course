import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '../components/ui';
import { CoursesPage } from './CoursesPage';

const mocks = vi.hoisted(() => ({
  getCourses: vi.fn(),
  getAssessments: vi.fn(),
  syncCourse: vi.fn(),
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
});

afterEach(cleanup);

describe('teacher courses page', () => {
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

    expect(await screen.findByRole('link', { name: /Самостоятельная работа №1/ }))
      .toHaveAttribute('href', '/assessments/assessment-1');
  });
});
