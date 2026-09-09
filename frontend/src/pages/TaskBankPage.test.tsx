import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '../components/ui';
import { TaskBankPage } from './PlaceholderPages';

const mocks = vi.hoisted(() => ({
  getCourses: vi.fn(),
  getTaskBank: vi.fn(),
}));
const auth = vi.hoisted(() => ({
  session: {
    id: 'teacher-1',
    displayName: 'Преподаватель',
    providerName: 'MMCS Moodle',
    memberships: [],
    capabilities: ['course.view', 'assessment.manage'],
  },
}));

vi.mock('../lib/api', () => ({
  api: {
    getCourses: mocks.getCourses,
    getTaskBank: mocks.getTaskBank,
  },
}));

vi.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    session: auth.session,
  }),
}));

beforeEach(() => {
  Object.values(mocks).forEach((mock) => mock.mockReset());
  mocks.getCourses.mockResolvedValue([]);
  mocks.getTaskBank.mockResolvedValue([{
    id: 'task-1',
    course: 'course-1',
    scope: 'COURSE',
    slug: 'long-imported-task',
    category: 'Лабораторные работы',
    tags: [],
    latestVersion: {
      id: 'version-1',
      itemId: 'task-1',
      number: 1,
      title: 'Лабораторная работа с очень длинным названием, которое должно переноситься независимо от статуса',
      statement: '',
      language: 'CPP',
      languageStandard: 'C++17',
      multiFile: false,
      maxScore: 10,
      status: 'DRAFT',
      aiPolicy: { lms_import_requires_configuration: true },
    },
  }]);
});

afterEach(cleanup);

describe('task bank cards', () => {
  it('treats a legacy unresolved LMS transport marker as a publishable draft', async () => {
    render(<MemoryRouter><ToastProvider><TaskBankPage /></ToastProvider></MemoryRouter>);

    expect((await screen.findByText('Черновик')).closest('.task-card__status')).not.toBeNull();
    expect(screen.getByText('Версия 1')).toHaveClass('task-card__version');
    expect(screen.queryByText('Требует настройки')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Опубликовать' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /Лабораторная работа с очень длинным названием/ })).toBeInTheDocument();
  });
});
