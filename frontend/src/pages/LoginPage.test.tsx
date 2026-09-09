import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { loadRememberedTokens, saveRememberedTokens } from '../lib/rememberedTokens';
import type { AuthConnection } from '../types';
import { LoginPage } from './LoginPage';

const mocks = vi.hoisted(() => ({
  getConnections: vi.fn(),
  startLogin: vi.fn(),
  moodleLogin: vi.fn(),
  devLogin: vi.fn(),
  navigate: vi.fn(),
}));

vi.mock('../lib/api', () => ({
  api: {
    getConnections: mocks.getConnections,
    startLogin: mocks.startLogin,
  },
  demoMode: 'never',
}));

vi.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    session: null,
    devLogin: mocks.devLogin,
    moodleLogin: mocks.moodleLogin,
  }),
}));

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return { ...actual, useNavigate: () => mocks.navigate };
});

const connection = (loginMode: AuthConnection['loginMode']): AuthConnection => ({
  id: 'moodle-mmcs',
  name: 'Moodle мехмата ЮФУ',
  kind: 'MOODLE',
  enabled: true,
  loginMode,
});

function renderLogin() {
  return render(<MemoryRouter initialEntries={['/login?course_id=549']}><LoginPage /></MemoryRouter>);
}

beforeEach(() => {
  localStorage.clear();
  mocks.getConnections.mockReset();
  mocks.startLogin.mockReset();
  mocks.moodleLogin.mockReset();
  mocks.devLogin.mockReset();
  mocks.navigate.mockReset();
});

afterEach(cleanup);

describe('Moodle login page', () => {
  it('renders credentials only for a pluginless connection', async () => {
    mocks.getConnections.mockResolvedValue([connection('CREDENTIALS')]);
    renderLogin();

    expect(screen.getByRole('heading', { name: 'Среда осмысленного программирования' })).toBeInTheDocument();
    expect(await screen.findByLabelText('Логин Moodle')).toBeInTheDocument();
    expect(screen.getByLabelText('Пароль Moodle')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Войти через Moodle мехмата ЮФУ/ })).toBeDisabled();
  });

  it('keeps the redirect flow free of Moodle password fields', async () => {
    mocks.getConnections.mockResolvedValue([connection('REDIRECT')]);
    renderLogin();

    expect(await screen.findByRole('button', { name: /Войти через Moodle мехмата ЮФУ/ })).toBeEnabled();
    expect(screen.queryByLabelText('Логин Moodle')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Пароль Moodle')).not.toBeInTheDocument();
  });

  it('submits credentials, clears the Moodle password, and remembers access tokens after success', async () => {
    mocks.getConnections.mockResolvedValue([connection('CREDENTIALS')]);
    mocks.moodleLogin.mockResolvedValue(undefined);
    renderLogin();

    const username = await screen.findByLabelText('Логин Moodle');
    const password = screen.getByLabelText('Пароль Moodle');
    const teacherToken = screen.getByLabelText(/^Токен преподавателя/);
    const adminToken = screen.getByLabelText(/^Токен администратора/);
    fireEvent.change(username, { target: { value: 'student' } });
    fireEvent.change(password, { target: { value: 'moodle-password' } });
    fireEvent.change(teacherToken, { target: { value: 'teacher-secret' } });
    fireEvent.change(adminToken, { target: { value: 'admin-secret' } });
    fireEvent.click(screen.getByRole('button', { name: /Войти через Moodle мехмата ЮФУ/ }));

    await waitFor(() => expect(mocks.moodleLogin).toHaveBeenCalledWith(
      'moodle-mmcs', 'student', 'moodle-password', 'admin-secret', 'teacher-secret',
    ));
    await waitFor(() => {
      expect(password).toHaveValue('');
      expect(teacherToken).toHaveValue('teacher-secret');
      expect(adminToken).toHaveValue('admin-secret');
    });
    expect(loadRememberedTokens()).toEqual({ teacherToken: 'teacher-secret', adminToken: 'admin-secret' });
    expect(username).toHaveValue('student');
    expect(mocks.navigate).toHaveBeenCalledWith('/settings');
  });

  it('submits Moodle credentials through the password form on Enter', async () => {
    mocks.getConnections.mockResolvedValue([connection('CREDENTIALS')]);
    mocks.moodleLogin.mockResolvedValue(undefined);
    renderLogin();

    fireEvent.change(await screen.findByLabelText('Логин Moodle'), { target: { value: 'student' } });
    const password = screen.getByLabelText('Пароль Moodle');
    fireEvent.change(password, { target: { value: 'moodle-password' } });
    fireEvent.submit(password.closest('form')!);

    await waitFor(() => expect(mocks.moodleLogin).toHaveBeenCalledWith(
      'moodle-mmcs', 'student', 'moodle-password', '', '',
    ));
    expect(mocks.navigate).toHaveBeenCalledWith('/');
  });

  it('shows the Moodle error, clears only the password, and does not persist rejected tokens', async () => {
    mocks.getConnections.mockResolvedValue([connection('CREDENTIALS')]);
    mocks.moodleLogin.mockRejectedValue(new Error('Неверный логин или пароль Moodle'));
    renderLogin();

    const password = await screen.findByLabelText('Пароль Moodle');
    const teacherToken = screen.getByLabelText(/^Токен преподавателя/);
    const adminToken = screen.getByLabelText(/^Токен администратора/);
    fireEvent.change(screen.getByLabelText('Логин Moodle'), { target: { value: 'student' } });
    fireEvent.change(password, { target: { value: 'wrong-password' } });
    fireEvent.change(teacherToken, { target: { value: 'teacher-secret' } });
    fireEvent.change(adminToken, { target: { value: 'admin-secret' } });
    fireEvent.click(screen.getByRole('button', { name: /Войти через Moodle мехмата ЮФУ/ }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Неверный логин или пароль Moodle');
    expect(password).toHaveValue('');
    expect(teacherToken).toHaveValue('teacher-secret');
    expect(adminToken).toHaveValue('admin-secret');
    expect(loadRememberedTokens()).toEqual({ teacherToken: '', adminToken: '' });
    expect(mocks.navigate).not.toHaveBeenCalled();
  });

  it('prefills remembered tokens as password fields and forgets them only on explicit request', async () => {
    saveRememberedTokens({ teacherToken: 'remembered-teacher', adminToken: 'remembered-admin' });
    mocks.getConnections.mockResolvedValue([connection('CREDENTIALS')]);
    renderLogin();

    const teacherToken = await screen.findByLabelText(/^Токен преподавателя/);
    const adminToken = screen.getByLabelText(/^Токен администратора/);
    expect(teacherToken).toHaveAttribute('type', 'password');
    expect(adminToken).toHaveAttribute('type', 'password');
    expect(teacherToken).toHaveValue('remembered-teacher');
    expect(adminToken).toHaveValue('remembered-admin');

    fireEvent.click(screen.getByRole('button', { name: 'Забыть сохранённые токены' }));

    expect(teacherToken).toHaveValue('');
    expect(adminToken).toHaveValue('');
    expect(loadRememberedTokens()).toEqual({ teacherToken: '', adminToken: '' });
    expect(screen.queryByRole('button', { name: 'Забыть сохранённые токены' })).not.toBeInTheDocument();
  });
});
