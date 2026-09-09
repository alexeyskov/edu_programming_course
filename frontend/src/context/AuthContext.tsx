import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { api, ApiError, AUTH_UNAUTHORIZED_EVENT } from '../lib/api';
import { saveRememberedToken, saveRememberedTokens } from '../lib/rememberedTokens';
import type { Role, Session } from '../types';

interface AuthContextValue {
  session: Session | null;
  loading: boolean;
  error: string | null;
  primaryRole: Role;
  refresh(): Promise<void>;
  moodleLogin(connectionId: string, username: string, password: string, adminToken?: string, teacherToken?: string): Promise<void>;
  devLogin(role: Role, adminToken?: string): Promise<void>;
  logout(): Promise<void>;
  elevate(token: string): Promise<void>;
  invalidateElevation(): void;
  dropElevation(): Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setSession(await api.getSession());
      setError(null);
    } catch (caught) {
      if (!(caught instanceof ApiError && caught.status === 401)) setError(caught instanceof Error ? caught.message : 'Не удалось проверить сессию');
      setSession(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const clearExpiredSession = () => {
      setSession(null);
      setError(null);
      setLoading(false);
    };
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, clearExpiredSession);
    return () => window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, clearExpiredSession);
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const value = useMemo<AuthContextValue>(() => ({
    session, loading, error,
    primaryRole: session?.memberships.some((item) => item.role === 'TEACHER') ? 'TEACHER' : 'STUDENT',
    refresh,
    async moodleLogin(connectionId, username, password, adminToken, teacherToken) {
      const authenticated = await api.moodleCredentialLogin(connectionId, username, password, adminToken, teacherToken);
      saveRememberedTokens({ adminToken, teacherToken });
      setSession(authenticated);
      setError(null);
    },
    async devLogin(role, adminToken) {
      const authenticated = await api.devLogin(role, adminToken);
      if (adminToken) saveRememberedToken('adminToken', adminToken);
      setSession(authenticated);
      setError(null);
    },
    async logout() { await api.logout(); setSession(null); },
    async elevate(token) {
      const elevated = await api.elevate(token);
      saveRememberedToken('adminToken', token);
      setSession(elevated);
      setError(null);
    },
    invalidateElevation() {
      setSession((current) => current ? {
        ...current,
        capabilities: current.capabilities.filter((capability) => capability !== 'SYSTEM_SETTINGS'),
        adminElevationExpiresAt: undefined,
      } : current);
    },
    async dropElevation() { await api.dropElevation(); await refresh(); },
  }), [session, loading, error, refresh]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error('useAuth must be used inside AuthProvider');
  return value;
}
