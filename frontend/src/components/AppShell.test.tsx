import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { THEME_STORAGE_KEY, ThemeProvider } from '../context/ThemeContext';
import { saveRememberedToken } from '../lib/rememberedTokens';
import type { Session } from '../types';
import { AppShell } from './AppShell';
import { ToastProvider } from './ui';

const mocks = vi.hoisted(() => ({
  elevate: vi.fn(),
  logout: vi.fn(),
}));

const teacherSession: Session = {
  id: 'teacher-1',
  displayName: 'Коваленко А.',
  providerName: 'MMCS Moodle',
  memberships: [{ courseId: 'cpp', courseName: 'C++', role: 'TEACHER' }],
  capabilities: ['course.view'],
};

vi.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    session: teacherSession,
    primaryRole: 'TEACHER',
    logout: mocks.logout,
    elevate: mocks.elevate,
  }),
}));

beforeEach(() => {
  localStorage.clear();
  mocks.elevate.mockReset();
  mocks.logout.mockReset();
});
afterEach(cleanup);

describe('AppShell administrator elevation', () => {
  it('prefills the modal with the remembered password-masked token and submits it', async () => {
    saveRememberedToken('adminToken', 'remembered-admin-secret');
    mocks.elevate.mockResolvedValue(undefined);
    render(<ThemeProvider><MemoryRouter initialEntries={['/']}>
      <ToastProvider><AppShell><p>Содержимое</p></AppShell></ToastProvider>
    </MemoryRouter></ThemeProvider>);

    fireEvent.click(screen.getByRole('button', { name: /Коваленко А\./ }));
    fireEvent.click(screen.getByRole('button', { name: 'Токен администратора' }));

    const tokenInput = screen.getByLabelText('Токен администратора');
    expect(tokenInput).toHaveAttribute('type', 'password');
    expect(tokenInput).toHaveValue('remembered-admin-secret');

    fireEvent.click(screen.getByRole('button', { name: 'Проверить токен' }));

    await waitFor(() => expect(mocks.elevate).toHaveBeenCalledWith('remembered-admin-secret'));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('switches the persisted colour theme and collapses/restores the desktop sidebar', () => {
    render(<ThemeProvider><MemoryRouter initialEntries={['/']}>
      <ToastProvider><AppShell><p>Содержимое</p></AppShell></ToastProvider>
    </MemoryRouter></ThemeProvider>);

    fireEvent.click(screen.getByRole('button', { name: 'Включить тёмную тему' }));
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark');
    expect(screen.getByRole('button', { name: 'Включить светлую тему' })).toBeInTheDocument();

    const hideNavigation = screen.getByRole('button', { name: 'Скрыть основное меню' });
    expect(hideNavigation.closest('.sidebar')).not.toBeNull();
    fireEvent.click(hideNavigation);
    expect(document.querySelector('.app-shell')).toHaveClass('app-shell--sidebar-collapsed');
    expect(screen.getByLabelText('Основное меню скрыто')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Показать основное меню' }));
    expect(document.querySelector('.app-shell')).not.toHaveClass('app-shell--sidebar-collapsed');
  });
});
