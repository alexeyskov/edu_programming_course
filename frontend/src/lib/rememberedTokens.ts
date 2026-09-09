export type RememberedTokenKind = 'teacherToken' | 'adminToken';

export interface RememberedTokens {
  teacherToken: string;
  adminToken: string;
}

const STORAGE_KEY = 'eduprog.remembered-access-tokens.v1';
const TOKEN_LIMITS: Record<RememberedTokenKind, number> = {
  teacherToken: 512,
  adminToken: 1024,
};
const EMPTY_TOKENS: RememberedTokens = { teacherToken: '', adminToken: '' };

function browserStorage(): Storage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function validToken(kind: RememberedTokenKind, value: unknown): string {
  return typeof value === 'string' && value.length <= TOKEN_LIMITS[kind] ? value : '';
}

/**
 * These access tokens are deliberately kept in localStorage so a user can regain
 * teacher/system access without finding the token again. This is a UX/security
 * tradeoff: localStorage is readable by any script running on the same origin and
 * therefore does not protect a token from XSS. Never pass an LMS password or any
 * other credential to this helper. Use it only on a trusted browser profile and
 * keep the application's CSP and XSS protections enabled.
 */
export function loadRememberedTokens(storage: Storage | null = browserStorage()): RememberedTokens {
  if (!storage) return { ...EMPTY_TOKENS };
  try {
    const serialized = storage.getItem(STORAGE_KEY);
    if (!serialized) return { ...EMPTY_TOKENS };
    const parsed = JSON.parse(serialized) as Record<string, unknown> | null;
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return { ...EMPTY_TOKENS };
    return {
      teacherToken: validToken('teacherToken', parsed.teacherToken),
      adminToken: validToken('adminToken', parsed.adminToken),
    };
  } catch {
    return { ...EMPTY_TOKENS };
  }
}

/**
 * Merges only non-empty, bounded tokens. In particular, clearing a form field or
 * submitting it empty never silently deletes a previously remembered token.
 */
export function saveRememberedTokens(
  tokens: Partial<RememberedTokens>,
  storage: Storage | null = browserStorage(),
): RememberedTokens {
  const remembered = loadRememberedTokens(storage);
  if (!storage) return remembered;

  const next = { ...remembered };
  (['teacherToken', 'adminToken'] as const).forEach((kind) => {
    const candidate = validToken(kind, tokens[kind]);
    if (candidate.trim()) next[kind] = candidate;
  });

  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(next));
    return loadRememberedTokens(storage);
  } catch {
    // Storage may be disabled, full, or blocked by the browser privacy policy.
    return remembered;
  }
}

export function saveRememberedToken(
  kind: RememberedTokenKind,
  value: string,
  storage: Storage | null = browserStorage(),
): RememberedTokens {
  return saveRememberedTokens({ [kind]: value }, storage);
}

export function clearRememberedTokens(storage: Storage | null = browserStorage()): void {
  if (!storage) return;
  try {
    storage.removeItem(STORAGE_KEY);
  } catch {
    // A blocked localStorage must not make the login page unusable.
  }
}
