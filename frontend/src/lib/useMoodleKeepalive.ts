import { useEffect } from 'react';
import { api } from './api';

const INTERVAL_MS = 5 * 60_000;

/** Keep both roles' Moodle sessions warm without touching work or model APIs. */
export function useMoodleKeepalive(principalId?: string, loginRevision = 0) {
  useEffect(() => {
    if (!principalId) return;
    let stopped = false;
    let inFlight = false;
    let lastAttempt = -Infinity;
    let controller: AbortController | undefined;
    const ping = async () => {
      if (stopped || inFlight || document.visibilityState !== 'visible' || navigator.onLine === false
        || Date.now() - lastAttempt < INTERVAL_MS) return;
      lastAttempt = Date.now();
      inFlight = true;
      controller = new AbortController();
      try {
        const response = await api.keepMoodleSessionAlive(controller.signal);
        // A genuine Moodle logout cannot be repaired with a ping. Keep the
        // local session/editor intact and let normal LMS actions explain it.
        if (response.status === 'REAUTH_REQUIRED') stopped = true;
      } catch {
        // An optional heartbeat must not interrupt typing or cause toast spam.
      } finally {
        inFlight = false;
      }
    };
    const resume = () => { void ping(); };
    void ping();
    const timer = window.setInterval(resume, INTERVAL_MS);
    document.addEventListener('visibilitychange', resume);
    window.addEventListener('online', resume);
    return () => {
      stopped = true;
      controller?.abort();
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', resume);
      window.removeEventListener('online', resume);
    };
  }, [principalId, loginRevision]);
}
