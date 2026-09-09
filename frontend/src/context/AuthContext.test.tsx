import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { loadRememberedTokens } from '../lib/rememberedTokens';
import type { Session } from '../types';
import { AuthProvider, useAuth } from './AuthContext';

const mocks = vi.hoisted(() => ({
  getSession: vi.fn(),
  elevate: vi.fn(),
  moodleCredentialLogin: vi.fn(),
  devLogin: vi.fn(),
  logout: vi.fn(),
  dropElevation: vi.fn(),
}));

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return { ...actual, api: mocks };
});

const teacherSession: Session = {
  id: 'teacher-1',
  displayName: 'Коваленко А.',
  providerName: 'MMCS Moodle',
  memberships: [{ courseId: 'cpp', courseName: 'C++', role: 'TEACHER' }],
  capabilities: ['course.view'],
};

function AuthProbe() {
  const auth = useAuth();
  return <div data-testid="auth-probe">
    <span>{auth.loading ? 'loading' : 'ready'}</span>
    <span>{auth.session?.capabilities.includes('SYSTEM_SETTINGS') ? 'elevated' : 'regular'}</span>
    <button onClick={() => void auth.elevate('admin-secret')}>Elevate</button>
    <button onClick={auth.invalidateElevation}>Invalidate</button>
  </div>;
}

beforeEach(() => {
  localStorage.clear();
  Object.values(mocks).forEach((mock) => mock.mockReset());
  mocks.getSession.mockResolvedValue(teacherSession);
});

describe('AuthProvider system elevation', () => {
  it('remembers a successful token and invalidates only the elevation capability in place', async () => {
    mocks.elevate.mockResolvedValue({
      ...teacherSession,
      capabilities: ['course.view', 'SYSTEM_SETTINGS'],
      adminElevationExpiresAt: '2026-08-25T20:00:00Z',
    });
    render(<AuthProvider><AuthProbe /></AuthProvider>);

    expect(await screen.findByText('ready')).toBeInTheDocument();
    expect(screen.getByText('regular')).toBeInTheDocument();
    const probeNode = screen.getByTestId('auth-probe');

    fireEvent.click(screen.getByRole('button', { name: 'Elevate' }));

    expect(await screen.findByText('elevated')).toBeInTheDocument();
    expect(mocks.elevate).toHaveBeenCalledWith('admin-secret');
    expect(loadRememberedTokens().adminToken).toBe('admin-secret');

    fireEvent.click(screen.getByRole('button', { name: 'Invalidate' }));

    await waitFor(() => expect(screen.getByText('regular')).toBeInTheDocument());
    expect(screen.getByText('ready')).toBeInTheDocument();
    expect(screen.getByTestId('auth-probe')).toBe(probeNode);
    expect(mocks.getSession).toHaveBeenCalledTimes(1);
  });
});

