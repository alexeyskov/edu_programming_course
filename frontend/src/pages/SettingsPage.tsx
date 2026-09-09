import {
  Activity, Bot, BookOpen, Check, Copy, Database, ExternalLink, Eye, EyeOff, KeyRound, Pencil, Plus,
  RefreshCcw, Save, Server, ShieldCheck, Trash2, TriangleAlert,
} from 'lucide-react';
import { type FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import { Badge, Button, Card, Field, InlineError, Modal, PageLoader, Toggle, useToast } from '../components/ui';
import { LmsSyncErrorDialog } from '../components/LmsSyncErrorDialog';
import { useAuth } from '../context/AuthContext';
import { api, ApiError } from '../lib/api';
import { loadRememberedTokens } from '../lib/rememberedTokens';
import type {
  CourseCatalogEntry, CourseImportJob, SystemHealth, SystemSettings, TeacherAccessToken, TeacherAccessTokenIssued,
} from '../types';

const syncLabel: Record<CourseCatalogEntry['syncStatus'], string> = {
  SYNCED: 'Актуален',
  SYNCING: 'Синхронизируется',
  STALE: 'Ожидает синхронизации',
  ERROR: 'Ошибка синхронизации',
};

function validateCourseUrl(value: string): string | null {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return 'Введите полную ссылку на курс Moodle';
  }
  if (!['http:', 'https:'].includes(parsed.protocol)) return 'Ссылка должна начинаться с http:// или https://';
  if (!/\/course\/view\.php\/?$/.test(parsed.pathname) || !parsed.searchParams.get('id')?.trim()) {
    return 'Ожидается ссылка вида …/course/view.php?id=549';
  }
  return null;
}

function courseUrlIdentity(value: string): string | null {
  try {
    const parsed = new URL(value);
    const externalId = parsed.searchParams.get('id')?.trim();
    if (!externalId) return null;
    return `${parsed.origin}${parsed.pathname.replace(/\/$/, '')}?id=${encodeURIComponent(externalId)}`;
  } catch {
    return null;
  }
}

function isSameCatalogCourse(left: CourseCatalogEntry, right: CourseCatalogEntry): boolean {
  if (left.id && right.id && left.id === right.id) return true;
  if (
    left.connectionId && right.connectionId
    && left.connectionId === right.connectionId
    && left.externalId === right.externalId
  ) return true;
  if (
    left.externalId && left.externalId === right.externalId
    && (!left.connectionId || !right.connectionId)
  ) return true;
  const leftUrl = courseUrlIdentity(left.externalUrl);
  return leftUrl !== null && leftUrl === courseUrlIdentity(right.externalUrl);
}

function upsertCatalogEntry(
  entries: CourseCatalogEntry[],
  optimistic: CourseCatalogEntry,
): CourseCatalogEntry[] {
  const matchingIndex = entries.findIndex((entry) => isSameCatalogCourse(entry, optimistic));
  if (matchingIndex === -1) return [...entries, optimistic];

  // A catalog response is authoritative when it already contains the course.
  // Collapse any duplicate identities while retaining its richer connection/sync fields.
  return entries.reduce<CourseCatalogEntry[]>((result, entry, index) => {
    if (!isSameCatalogCourse(entry, optimistic)) {
      result.push(entry);
    } else if (index === matchingIndex) {
      result.push({ ...optimistic, ...entry });
    }
    return result;
  }, []);
}

function previewText(preview: Record<string, unknown>, field: string): string {
  const value = preview[field];
  return typeof value === 'string' ? value.trim() : '';
}

function optimisticCatalogEntry(
  discovered: CourseImportJob,
  confirmed: CourseImportJob,
  externalUrl: string,
  currentCatalog: CourseCatalogEntry[],
): CourseCatalogEntry | null {
  if (!confirmed.confirmedCourse) return null;

  const preview = { ...discovered.preview, ...confirmed.preview };
  const parsedExternalId = (() => {
    try {
      return new URL(externalUrl).searchParams.get('id')?.trim() ?? '';
    } catch {
      return '';
    }
  })();
  const externalId = confirmed.externalCourseId || discovered.externalCourseId || parsedExternalId;
  if (!externalId) return null;

  const identity = courseUrlIdentity(externalUrl);
  const existing = currentCatalog.find((entry) => (
    entry.id === confirmed.confirmedCourse
    || (identity !== null && courseUrlIdentity(entry.externalUrl) === identity)
  ));
  let sameOrigin: CourseCatalogEntry | undefined;
  try {
    const origin = new URL(externalUrl).origin;
    sameOrigin = currentCatalog.find((entry) => {
      try {
        return new URL(entry.externalUrl).origin === origin;
      } catch {
        return false;
      }
    });
  } catch {
    // The URL was validated before discovery; retain a defensive fallback for tests/adapters.
  }
  const connection = existing ?? sameOrigin;

  return {
    id: confirmed.confirmedCourse,
    connectionId: connection?.connectionId ?? '',
    connectionName: connection?.connectionName ?? 'Moodle',
    externalId,
    title: previewText(preview, 'title') || existing?.title || `Курс ${externalId}`,
    shortName: previewText(preview, 'short_name') || existing?.shortName || '',
    externalUrl,
    syncStatus: existing?.syncStatus ?? 'STALE',
    addedAt: existing?.addedAt ?? new Date().toISOString(),
  };
}

interface LoadCatalogOptions {
  background?: boolean;
  preserve?: CourseCatalogEntry;
}

async function copyText(text: string): Promise<void> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
  } catch {
    // Clipboard API may be blocked by permissions even in a secure context.
    // The synchronous fallback below still works in browsers served over HTTP.
  }

  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.readOnly = true;
  textarea.setAttribute('aria-hidden', 'true');
  textarea.style.position = 'fixed';
  textarea.style.inset = '0 auto auto 0';
  textarea.style.opacity = '0';
  textarea.style.pointerEvents = 'none';

  const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  document.body.appendChild(textarea);
  try {
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, text.length);
    if (typeof document.execCommand !== 'function' || !document.execCommand('copy')) {
      throw new Error('Буфер обмена недоступен');
    }
  } finally {
    textarea.remove();
    previouslyFocused?.focus();
  }
}

type TeacherTokenCopyStatus = 'idle' | 'copying' | 'success' | 'error';

function validateTeacherTokenValue(value: string): string | null {
  if (!value) return 'Введите новое значение токена';
  if (!/^[A-Za-z0-9]{8}$/.test(value)) {
    return 'Токен должен состоять ровно из 8 латинских букв или цифр';
  }
  return null;
}

function isSystemSettingsAccessError(caught: unknown): caught is ApiError {
  return caught instanceof ApiError
    && caught.status === 403
    && (
      ['CAPABILITY_REQUIRED', 'SYSTEM_SETTINGS_REQUIRED', 'SYSTEM_SCOPE_REQUIRED'].includes(caught.code)
      || caught.message.includes('SYSTEM_SETTINGS')
    );
}

export function SettingsPage() {
  const { session, dropElevation, elevate, invalidateElevation, refresh } = useAuth();
  const [settings, setSettings] = useState<SystemSettings | null>(null);
  const [health, setHealth] = useState<SystemHealth | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [catalog, setCatalog] = useState<CourseCatalogEntry[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [courseUrl, setCourseUrl] = useState('');
  const [courseUrlError, setCourseUrlError] = useState<string | null>(null);
  const [courseAdding, setCourseAdding] = useState(false);
  const [courseToRemove, setCourseToRemove] = useState<CourseCatalogEntry | null>(null);
  const [removingCourseId, setRemovingCourseId] = useState<string | null>(null);
  const [syncErrorCourse, setSyncErrorCourse] = useState<CourseCatalogEntry | null>(null);
  const [syncRetrying, setSyncRetrying] = useState(false);
  const [teacherTokens, setTeacherTokens] = useState<TeacherAccessToken[]>([]);
  const [teacherTokensLoading, setTeacherTokensLoading] = useState(true);
  const [teacherTokensError, setTeacherTokensError] = useState<string | null>(null);
  const [teacherTokenLabel, setTeacherTokenLabel] = useState('');
  const [teacherTokenCreating, setTeacherTokenCreating] = useState(false);
  const [issuedTeacherToken, setIssuedTeacherToken] = useState<TeacherAccessTokenIssued | null>(null);
  const [teacherTokenCopyStatus, setTeacherTokenCopyStatus] = useState<TeacherTokenCopyStatus>('idle');
  const [revealedTeacherTokens, setRevealedTeacherTokens] = useState<Record<string, string>>({});
  const [revealingTeacherTokenId, setRevealingTeacherTokenId] = useState<string | null>(null);
  const [teacherTokenToEdit, setTeacherTokenToEdit] = useState<TeacherAccessToken | null>(null);
  const [teacherTokenDraft, setTeacherTokenDraft] = useState('');
  const [teacherTokenEditError, setTeacherTokenEditError] = useState<string | null>(null);
  const [updatingTeacherTokenId, setUpdatingTeacherTokenId] = useState<string | null>(null);
  const [teacherTokenToRemove, setTeacherTokenToRemove] = useState<TeacherAccessToken | null>(null);
  const [removingTeacherTokenId, setRemovingTeacherTokenId] = useState<string | null>(null);
  const [elevationRequired, setElevationRequired] = useState(false);
  const [elevationError, setElevationError] = useState<string | null>(null);
  const [elevating, setElevating] = useState(false);
  const [adminToken, setAdminToken] = useState(() => loadRememberedTokens().adminToken);
  const [accessGeneration, setAccessGeneration] = useState(0);
  const accessEpochRef = useRef(0);
  const catalogRequestRef = useRef(0);
  const settingsLoadedRef = useRef(false);
  const catalogLoadedRef = useRef(false);
  const teacherTokensLoadedRef = useRef(false);
  const issuedTeacherTokenValueRef = useRef<HTMLElement>(null);
  const toast = useToast();
  const elevated = session?.capabilities.includes('SYSTEM_SETTINGS') ?? false;

  const requireElevation = useCallback((caught: unknown, requestEpoch: number): boolean => {
    if (!isSystemSettingsAccessError(caught)) return false;
    if (requestEpoch !== accessEpochRef.current) return true;
    // Retire every request that started under the expired capability at once.
    // Only the first 403 from a parallel batch may invalidate the live session.
    accessEpochRef.current += 1;
    invalidateElevation();
    setElevationRequired(true);
    setElevationError(null);
    setError(null);
    setCatalogError(null);
    setTeacherTokensError(null);
    setRevealedTeacherTokens({});
    setTeacherTokenToEdit(null);
    setTeacherTokenDraft('');
    setTeacherTokenEditError(null);
    return true;
  }, [invalidateElevation]);

  const load = useCallback(async (requestEpoch = accessEpochRef.current) => {
    if (requestEpoch === accessEpochRef.current) setLoading(true);
    try {
      const nextSettings = await api.getSettings();
      if (requestEpoch !== accessEpochRef.current) return;
      setSettings(nextSettings);
      settingsLoadedRef.current = true;
      setError(null);
      try {
        const nextHealth = await api.getHealth();
        if (requestEpoch === accessEpochRef.current) setHealth(nextHealth);
      } catch {
        if (requestEpoch === accessEpochRef.current) setHealth(null);
      }
    } catch (caught) {
      const accessError = requireElevation(caught, requestEpoch);
      if (!accessError && requestEpoch === accessEpochRef.current) {
        setError(caught instanceof Error ? caught.message : 'Ошибка');
      }
    } finally {
      if (requestEpoch === accessEpochRef.current) setLoading(false);
    }
  }, [requireElevation]);

  const loadCatalog = useCallback(async (
    requestEpoch = accessEpochRef.current,
    options: LoadCatalogOptions = {},
  ) => {
    const requestId = catalogRequestRef.current + 1;
    catalogRequestRef.current = requestId;
    const isCurrentRequest = () => (
      requestEpoch === accessEpochRef.current && requestId === catalogRequestRef.current
    );
    if (isCurrentRequest() && !options.background) setCatalogLoading(true);
    try {
      const nextCatalog = await api.getCourseCatalog();
      if (!isCurrentRequest()) return;
      setCatalog(options.preserve ? upsertCatalogEntry(nextCatalog, options.preserve) : nextCatalog);
      catalogLoadedRef.current = true;
      setCatalogError(null);
    } catch (caught) {
      if (!isCurrentRequest()) return;
      const accessError = requireElevation(caught, requestEpoch);
      if (!accessError && isCurrentRequest() && !options.background) {
        setCatalogError(caught instanceof Error ? caught.message : 'Не удалось загрузить каталог курсов');
      }
    } finally {
      if (isCurrentRequest() && !options.background) setCatalogLoading(false);
    }
  }, [requireElevation]);

  const loadTeacherTokens = useCallback(async (requestEpoch = accessEpochRef.current) => {
    if (requestEpoch === accessEpochRef.current) setTeacherTokensLoading(true);
    try {
      const nextTeacherTokens = await api.getTeacherTokens();
      if (requestEpoch !== accessEpochRef.current) return;
      setTeacherTokens(nextTeacherTokens);
      setRevealedTeacherTokens((revealed) => Object.fromEntries(
        nextTeacherTokens
          .filter((token) => token.canReveal && revealed[token.id])
          .map((token) => [token.id, revealed[token.id]]),
      ));
      teacherTokensLoadedRef.current = true;
      setTeacherTokensError(null);
    } catch (caught) {
      const accessError = requireElevation(caught, requestEpoch);
      if (!accessError && requestEpoch === accessEpochRef.current) {
        setTeacherTokensError(caught instanceof Error ? caught.message : 'Не удалось загрузить токены преподавателей');
      }
    } finally {
      if (requestEpoch === accessEpochRef.current) setTeacherTokensLoading(false);
    }
  }, [requireElevation]);

  useEffect(() => {
    if (!elevated || elevationRequired) return;
    const requestEpoch = accessEpochRef.current;
    if (!settingsLoadedRef.current) void load(requestEpoch);
    if (!catalogLoadedRef.current) void loadCatalog(requestEpoch);
    if (!teacherTokensLoadedRef.current) void loadTeacherTokens(requestEpoch);
  }, [accessGeneration, elevated, elevationRequired, load, loadCatalog, loadTeacherTokens]);

  const syncingCatalogKey = catalog
    .filter((course) => course.syncStatus === 'SYNCING')
    .map((course) => course.id)
    .sort()
    .join(',');
  useEffect(() => {
    if (!elevated || elevationRequired || !syncingCatalogKey) return undefined;
    const requestEpoch = accessEpochRef.current;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      await loadCatalog(requestEpoch, { background: true });
      if (!cancelled) timer = window.setTimeout(() => { void poll(); }, 2_000);
    };
    timer = window.setTimeout(() => { void poll(); }, 1_500);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [elevated, elevationRequired, loadCatalog, syncingCatalogKey]);

  if (elevationRequired || !elevated) return <div className="content-width settings-page">
    <div className="page-heading">
      <div>
        <span className="eyebrow">SYSTEM_SETTINGS</span>
        <h1>Возобновление доступа</h1>
        <p>{elevationRequired
          ? 'Время повышенного доступа истекло. Введите административный токен ещё раз, чтобы продолжить работу с настройками.'
          : 'Введите административный токен, чтобы открыть глобальные настройки без повторного входа в Moodle.'}</p>
      </div>
    </div>
    <Card className="settings-card compact">
      <header>
        <span><ShieldCheck /></span>
        <div>
          <h2>Системный доступ приостановлен</h2>
          <p>Учётная запись Moodle остаётся активной — повторный вход в LMS не требуется.</p>
        </div>
      </header>
      {elevationError && <InlineError title="Доступ не восстановлен" message={elevationError} />}
      <form className="moodle-credential-login" onSubmit={(event) => {
        event.preventDefault();
        const token = adminToken;
        if (!token.trim() || elevating) return;
        setElevating(true);
        setElevationError(null);
        void elevate(token)
          .then(() => {
            accessEpochRef.current += 1;
            setAdminToken(token);
            if (!settingsLoadedRef.current) setLoading(true);
            if (!catalogLoadedRef.current) setCatalogLoading(true);
            if (!teacherTokensLoadedRef.current) setTeacherTokensLoading(true);
            setElevationRequired(false);
            setAccessGeneration(accessEpochRef.current);
            toast.push('success', 'Системный доступ возобновлён', 'Можно продолжить работу с настройками.');
          })
          .catch((caught) => setElevationError(caught instanceof Error ? caught.message : 'Не удалось проверить токен'))
          .finally(() => setElevating(false));
      }}>
        <Field
          label="Токен администратора"
          hint="Сохранённый токен остаётся скрытым в поле и используется только для повторного повышения доступа."
        >
          <div className="input-with-icon">
            <KeyRound size={18} />
            <input
              type="password"
              aria-label="Токен администратора"
              autoComplete="off"
              data-1p-ignore
              maxLength={1024}
              spellCheck={false}
              value={adminToken}
              onChange={(event) => setAdminToken(event.target.value)}
              placeholder="Введите токен администратора"
              autoFocus
            />
          </div>
        </Field>
        <Button type="submit" loading={elevating} disabled={!adminToken.trim()}>
          <ShieldCheck size={16} /> Возобновить доступ
        </Button>
      </form>
    </Card>
  </div>;
  if (loading) return <PageLoader label="Получаем системные настройки…" />;
  if (error || !settings) return <InlineError message={error ?? 'Настройки не найдены'} retry={() => void load()} />;

  const patch = (next: Partial<SystemSettings>) => setSettings({ ...settings, ...next });

  async function save() {
    if (!settings) return;
    const requestEpoch = accessEpochRef.current;
    setSaving(true);
    try {
      const updated = await api.updateSettings(settings);
      setSettings(updated);
      settingsLoadedRef.current = true;
      toast.push('success', 'Настройки сохранены', 'Изменение политик зафиксировано в аудите.');
    } catch (caught) {
      if (!requireElevation(caught, requestEpoch) && requestEpoch === accessEpochRef.current) {
        toast.push('error', 'Настройки не сохранены', caught instanceof Error ? caught.message : undefined);
      }
    } finally {
      setSaving(false);
    }
  }

  async function addCourse(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const requestEpoch = accessEpochRef.current;
    const url = courseUrl.trim();
    const validationError = validateCourseUrl(url);
    if (validationError) {
      setCourseUrlError(validationError);
      return;
    }
    setCourseAdding(true);
    setCourseUrlError(null);
    try {
      const job = await api.createCourseImport(url);
      const confirmed = await api.confirmCourseImport(job.id);
      const title = String(job.preview.title ?? `Курс ${confirmed.externalCourseId}`);
      const optimistic = optimisticCatalogEntry(job, confirmed, url, catalog);
      if (optimistic) {
        setCatalog((items) => upsertCatalogEntry(items, optimistic));
        setCatalogLoading(false);
        setCatalogError(null);
      }
      setCourseUrl('');
      toast.push('success', 'Курс добавлен в систему', `${title} теперь учитывается при входе пользователей.`);
      await loadCatalog(requestEpoch, { background: Boolean(optimistic), preserve: optimistic ?? undefined });
      await refresh();
    } catch (caught) {
      if (!requireElevation(caught, requestEpoch) && requestEpoch === accessEpochRef.current) {
        toast.push('error', 'Курс не добавлен', caught instanceof Error ? caught.message : undefined);
      }
    } finally {
      setCourseAdding(false);
    }
  }

  async function removeCourse() {
    if (!courseToRemove) return;
    if (removingCourseId) {
      setCourseToRemove(null);
      return;
    }
    const course = courseToRemove;
    const requestEpoch = accessEpochRef.current;
    setRemovingCourseId(course.id);
    // Confirmation is a one-shot action: close it immediately so a slow
    // network response cannot result in repeated DELETE requests.
    setCourseToRemove(null);
    try {
      await api.deleteCourseCatalogEntry(course.id);
      setCatalog((items) => items.filter((item) => item.id !== course.id));
      toast.push('success', 'Курс убран из системы', 'История работ и сдач сохранена.');
      await refresh();
    } catch (caught) {
      if (!requireElevation(caught, requestEpoch) && requestEpoch === accessEpochRef.current) {
        toast.push('error', 'Курс не удалён', caught instanceof Error ? caught.message : undefined);
      }
    } finally {
      setRemovingCourseId(null);
    }
  }

  async function retryCourseSync() {
    if (!syncErrorCourse || syncRetrying) return;
    setSyncRetrying(true);
    try {
      await api.syncCourse(syncErrorCourse.id);
      await loadCatalog(accessEpochRef.current, { background: true });
      setSyncErrorCourse(null);
      toast.push('success', 'Синхронизация запущена', syncErrorCourse.title);
    } catch (caught) {
      const latest = await api.getCourseCatalog().catch(() => null);
      if (latest) {
        setCatalog(latest);
        setSyncErrorCourse(latest.find((course) => course.id === syncErrorCourse.id) ?? syncErrorCourse);
      }
      toast.push('error', 'Синхронизация не завершена', caught instanceof Error ? caught.message : undefined);
    } finally {
      setSyncRetrying(false);
    }
  }

  async function createTeacherToken(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const requestEpoch = accessEpochRef.current;
    const label = teacherTokenLabel.trim();
    if (!label) return;
    setTeacherTokenCreating(true);
    try {
      const issued = await api.createTeacherToken(label);
      setTeacherTokenCopyStatus('idle');
      setIssuedTeacherToken(issued);
      setRevealedTeacherTokens((tokens) => ({ ...tokens, [issued.id]: issued.token }));
      setTeacherTokenLabel('');
      await loadTeacherTokens();
      toast.push('success', 'Токен преподавателя создан', 'Значение доступно администраторам в настройках системы.');
    } catch (caught) {
      if (!requireElevation(caught, requestEpoch) && requestEpoch === accessEpochRef.current) {
        toast.push('error', 'Токен не создан', caught instanceof Error ? caught.message : undefined);
      }
    } finally {
      setTeacherTokenCreating(false);
    }
  }

  async function revealTeacherToken(token: TeacherAccessToken) {
    if (!token.canReveal || revealingTeacherTokenId) return;
    const requestEpoch = accessEpochRef.current;
    setRevealingTeacherTokenId(token.id);
    try {
      const revealed = await api.revealTeacherToken(token.id);
      if (!revealed.token) throw new Error('Сервер не вернул значение токена');
      setRevealedTeacherTokens((tokens) => ({ ...tokens, [token.id]: revealed.token }));
    } catch (caught) {
      if (!requireElevation(caught, requestEpoch) && requestEpoch === accessEpochRef.current) {
        if (caught instanceof ApiError && ['TEACHER_TOKEN_SECRET_UNAVAILABLE', 'TOKEN_SECRET_UNAVAILABLE'].includes(caught.code)) {
          setTeacherTokens((tokens) => tokens.map((item) => item.id === token.id ? { ...item, canReveal: false } : item));
        }
        toast.push('error', 'Токен не показан', caught instanceof Error ? caught.message : undefined);
      }
    } finally {
      setRevealingTeacherTokenId(null);
    }
  }

  function openTeacherTokenEditor(token: TeacherAccessToken) {
    setTeacherTokenToEdit(token);
    setTeacherTokenDraft(revealedTeacherTokens[token.id] ?? '');
    setTeacherTokenEditError(null);
  }

  function closeTeacherTokenEditor() {
    if (updatingTeacherTokenId) return;
    setTeacherTokenToEdit(null);
    setTeacherTokenDraft('');
    setTeacherTokenEditError(null);
  }

  async function updateTeacherToken(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!teacherTokenToEdit) return;
    const validationError = validateTeacherTokenValue(teacherTokenDraft);
    if (validationError) {
      setTeacherTokenEditError(validationError);
      return;
    }
    const requestEpoch = accessEpochRef.current;
    setUpdatingTeacherTokenId(teacherTokenToEdit.id);
    setTeacherTokenEditError(null);
    try {
      const updated = await api.updateTeacherToken(teacherTokenToEdit.id, teacherTokenDraft);
      setTeacherTokens((tokens) => tokens.map((item) => item.id === updated.id ? updated : item));
      setRevealedTeacherTokens((tokens) => ({ ...tokens, [updated.id]: teacherTokenDraft }));
      setTeacherTokenToEdit(null);
      setTeacherTokenDraft('');
      toast.push('success', 'Токен преподавателя изменён', 'Новое значение действует сразу; прежнее больше не подходит для входа.');
    } catch (caught) {
      if (!requireElevation(caught, requestEpoch) && requestEpoch === accessEpochRef.current) {
        setTeacherTokenEditError(caught instanceof Error ? caught.message : 'Не удалось изменить токен');
      }
    } finally {
      setUpdatingTeacherTokenId(null);
    }
  }

  async function copyTeacherToken(token: TeacherAccessToken) {
    const value = revealedTeacherTokens[token.id];
    if (!value) return;
    try {
      await copyText(value);
      toast.push('success', 'Токен скопирован');
    } catch {
      toast.push('error', 'Не удалось скопировать токен', 'Выделите значение и скопируйте его вручную.');
    }
  }

  async function removeTeacherToken() {
    if (!teacherTokenToRemove) return;
    const requestEpoch = accessEpochRef.current;
    setRemovingTeacherTokenId(teacherTokenToRemove.id);
    try {
      await api.deleteTeacherToken(teacherTokenToRemove.id);
      setTeacherTokens((items) => items.filter((item) => item.id !== teacherTokenToRemove.id));
      setRevealedTeacherTokens((tokens) => {
        const next = { ...tokens };
        delete next[teacherTokenToRemove.id];
        return next;
      });
      setTeacherTokenToRemove(null);
      await refresh();
      toast.push('success', 'Токен отозван', 'Связанный пользователь продолжит работу с ролью студента.');
    } catch (caught) {
      if (!requireElevation(caught, requestEpoch) && requestEpoch === accessEpochRef.current) {
        toast.push('error', 'Токен не отозван', caught instanceof Error ? caught.message : undefined);
      }
    } finally {
      setRemovingTeacherTokenId(null);
    }
  }

  async function copyIssuedTeacherToken() {
    if (!issuedTeacherToken) return;
    setTeacherTokenCopyStatus('copying');
    try {
      await copyText(issuedTeacherToken.token);
      setTeacherTokenCopyStatus('success');
      toast.push('success', 'Токен скопирован');
    } catch {
      setTeacherTokenCopyStatus('error');
      const selection = window.getSelection();
      if (selection && issuedTeacherTokenValueRef.current) {
        const range = document.createRange();
        range.selectNodeContents(issuedTeacherTokenValueRef.current);
        selection.removeAllRanges();
        selection.addRange(range);
      }
      toast.push('error', 'Не удалось скопировать автоматически', 'Токен выделен: нажмите Ctrl+C или ⌘C.');
    }
  }

  return <div className="content-width settings-page">
    <div className="page-heading">
      <div>
        <span className="eyebrow">SYSTEM_SETTINGS</span>
        <h1>Настройки системы</h1>
        <p>Повышенный доступ действует только в текущей сессии и не расширяет права на курсы.</p>
      </div>
      <div>
        <Button variant="ghost" onClick={() => {
          setRevealedTeacherTokens({});
          setTeacherTokenToEdit(null);
          setTeacherTokenDraft('');
          void dropElevation();
        }}>Снять доступ</Button>
        <Button loading={saving} onClick={() => void save()}><Save size={16} /> Сохранить</Button>
      </div>
    </div>

    <div className="elevation-notice">
      <ShieldCheck />
      <div>
        <strong>Системный доступ активен</strong>
        <p>Все изменения журналируются. Токен остаётся скрытым и хранится только в этом браузере до явного удаления на экране входа.</p>
      </div>
      <Badge tone="purple">{session?.adminElevationExpiresAt ? 'продлевается при работе' : 'текущая сессия'}</Badge>
    </div>

    <div className="settings-grid">
      <section>
        <Card className="settings-card system-course-card">
          <header>
            <span><BookOpen /></span>
            <div>
              <h2>Глобальный каталог курсов</h2>
              <p>Только эти курсы учитываются при входе преподавателей и студентов.</p>
            </div>
            <Badge tone="info" className="settings-card__badge">{catalog.length}</Badge>
          </header>

          <form className="system-course-form" onSubmit={(event) => void addCourse(event)}>
            <label className="system-course-form__label" htmlFor="system-course-url">Ссылка на курс Moodle</label>
            <input
              id="system-course-url"
              className="system-course-form__input"
              type="url"
              value={courseUrl}
              disabled={courseAdding}
              aria-describedby="system-course-url-message"
              aria-invalid={Boolean(courseUrlError)}
              onChange={(event) => {
                setCourseUrl(event.target.value);
                if (courseUrlError) setCourseUrlError(null);
              }}
              placeholder="https://edu.mmcs.sfedu.ru/course/view.php?id=549"
              autoComplete="off"
            />
            <Button type="submit" loading={courseAdding} disabled={!courseUrl.trim()}>
              <Plus size={16} /> Добавить курс
            </Button>
            <span
              id="system-course-url-message"
              className={courseUrlError ? 'system-course-form__message field__error' : 'system-course-form__message field__hint'}
              role={courseUrlError ? 'alert' : undefined}
            >
              {courseUrlError ?? 'Курс будет проанализирован через вашу текущую LMS-сессию'}
            </span>
          </form>

          {catalogLoading
            ? <div className="system-course-loading" role="status"><RefreshCcw className="spin" size={17} /> Загружаем каталог…</div>
            : catalogError
              ? <InlineError title="Каталог курсов недоступен" message={catalogError} retry={() => void loadCatalog()} />
              : catalog.length
                ? <div className="system-course-list">
                  {catalog.map((course) => <div className="system-course-row" key={course.id}>
                    <div className="system-course-copy">
                      <div className="system-course-title">
                        <strong>{course.title}</strong>
                        {course.shortName && <small>{course.shortName}</small>}
                      </div>
                      <div className="system-course-meta">
                        <span>{course.connectionName}</span>
                        <code>ID в LMS: {course.externalId}</code>
                        {course.syncStatus === 'ERROR'
                          ? <button type="button" className="sync-error-trigger" onClick={() => setSyncErrorCourse(course)} aria-label={`Показать ошибку синхронизации курса ${course.title}`}><Badge tone="danger">{syncLabel.ERROR}</Badge></button>
                          : <Badge tone={course.syncStatus === 'SYNCED' ? 'success' : course.syncStatus === 'SYNCING' ? 'info' : 'warning'}>{syncLabel[course.syncStatus]}</Badge>}
                      </div>
                    </div>
                    <div className="system-course-actions">
                      <a href={course.externalUrl} target="_blank" rel="noreferrer" aria-label={`Открыть курс ${course.title} в Moodle`}>
                        <ExternalLink size={16} />
                      </a>
                      <Button
                        variant="danger"
                        size="icon"
                        loading={removingCourseId === course.id}
                        disabled={Boolean(removingCourseId)}
                        aria-label={`Убрать курс ${course.title} из системы`}
                        onClick={() => setCourseToRemove(course)}
                      >
                        <Trash2 size={16} />
                      </Button>
                    </div>
                  </div>)}
                </div>
                : <div className="system-course-empty">
                  <BookOpen size={22} />
                  <strong>Курсы пока не добавлены</strong>
                  <p>Войдя в систему, пользователи не увидят курсы LMS, пока администратор не добавит их сюда.</p>
                </div>}
        </Card>

        <Card className="settings-card teacher-token-card">
          <header>
            <span><KeyRound /></span>
            <div>
              <h2>Токены преподавателей</h2>
              <p>Глобальная роль преподавателя, независимая от распознавания интерфейса Moodle.</p>
            </div>
            <Badge tone="purple" className="settings-card__badge">{teacherTokens.length}</Badge>
          </header>

          <form className="system-course-form" onSubmit={(event) => void createTeacherToken(event)}>
            <label className="system-course-form__label" htmlFor="teacher-token-label">Название токена</label>
            <input
              id="teacher-token-label"
              className="system-course-form__input"
              aria-label="Название токена преподавателя"
              aria-describedby="teacher-token-label-hint"
              maxLength={120}
              value={teacherTokenLabel}
              disabled={teacherTokenCreating}
              onChange={(event) => setTeacherTokenLabel(event.target.value)}
              placeholder="Кому выдаётся токен"
              autoComplete="off"
            />
            <Button type="submit" loading={teacherTokenCreating} disabled={!teacherTokenLabel.trim()}>
              <Plus size={16} /> Создать токен
            </Button>
            <span id="teacher-token-label-hint" className="system-course-form__message field__hint">
              Например: Коваленко А. · осенний семестр
            </span>
          </form>

          <div className="teacher-token-explanation">
            <ShieldCheck size={17} />
            <p>Для проверки входа используется Argon2id-хэш. Администратор может показать или заменить значение нового токена. Значения ранее созданных токенов восстановить нельзя — их можно заменить без удаления привязки к Moodle.</p>
          </div>

          {teacherTokensLoading
            ? <div className="system-course-loading" role="status"><RefreshCcw className="spin" size={17} /> Загружаем токены…</div>
            : teacherTokensError
              ? <InlineError title="Токены недоступны" message={teacherTokensError} retry={() => void loadTeacherTokens()} />
              : teacherTokens.length
                ? <div className="system-course-list teacher-token-list">
                  {teacherTokens.map((token) => {
                    const revealedValue = revealedTeacherTokens[token.id];
                    return <div className="system-course-row" key={token.id}>
                      <div className="system-course-copy">
                        <div className="system-course-title">
                          <strong>{token.label}</strong>
                          <small>{token.boundDisplayName ? `Привязан: ${token.boundDisplayName}` : 'Ещё не использован'}</small>
                        </div>
                        <div className="system-course-meta">
                          <code>ID: {token.publicId}</code>
                          <code>Хэш: {token.hashFingerprint}</code>
                          <Badge tone={token.boundDisplayName ? 'success' : 'neutral'}>{token.boundDisplayName ? 'Привязан' : 'Ожидает входа'}</Badge>
                        </div>
                        <div className={`teacher-token-secret${!token.canReveal ? ' teacher-token-secret--legacy' : ''}`}>
                          <span>Токен:</span>
                          {revealedValue
                            ? <code aria-label={`Значение токена ${token.label}`}>{revealedValue}</code>
                            : token.canReveal
                              ? <code aria-label={`Значение токена ${token.label} скрыто`}>••••••••</code>
                              : <small>Значение старого токена недоступно — замените его</small>}
                        </div>
                      </div>
                      <div className="system-course-actions">
                        {token.canReveal && <Button
                          variant="secondary"
                          size="icon"
                          loading={revealingTeacherTokenId === token.id}
                          disabled={Boolean(revealingTeacherTokenId) && revealingTeacherTokenId !== token.id}
                          aria-label={`${revealedValue ? 'Скрыть' : 'Показать'} токен ${token.label}`}
                          onClick={() => {
                            if (revealedValue) {
                              setRevealedTeacherTokens((tokens) => {
                                const next = { ...tokens };
                                delete next[token.id];
                                return next;
                              });
                            } else {
                              void revealTeacherToken(token);
                            }
                          }}
                        >
                          {revealedValue ? <EyeOff size={16} /> : <Eye size={16} />}
                        </Button>}
                        {revealedValue && <Button
                          variant="secondary"
                          size="icon"
                          aria-label={`Скопировать токен ${token.label}`}
                          onClick={() => void copyTeacherToken(token)}
                        >
                          <Copy size={16} />
                        </Button>}
                        <Button
                          variant="secondary"
                          size="icon"
                          aria-label={`Изменить токен ${token.label}`}
                          onClick={() => openTeacherTokenEditor(token)}
                        >
                          <Pencil size={16} />
                        </Button>
                        <Button
                          variant="danger"
                          size="icon"
                          aria-label={`Удалить токен ${token.label}`}
                          onClick={() => setTeacherTokenToRemove(token)}
                        >
                          <Trash2 size={16} />
                        </Button>
                      </div>
                    </div>;
                  })}
                </div>
                : <div className="system-course-empty">
                  <KeyRound size={22} />
                  <strong>Преподавательских токенов пока нет</strong>
                  <p>Без привязанного токена пользователи входят как студенты, даже если Moodle показывает им элементы управления курсом.</p>
                </div>}
        </Card>

        <Card className="settings-card">
          <header><span><Bot /></span><div><h2>Искусственный интеллект</h2><p>Глобальные ограничения поверх настроек курсов.</p></div></header>
          <Toggle checked={settings.aiEnabled} onChange={(value) => patch({ aiEnabled: value })} label="ИИ-функции системы" description="Анализ для преподавателя и учебный помощник" />
          <Toggle checked={settings.studentAiEnabled} disabled={!settings.aiEnabled} onChange={(value) => patch({ studentAiEnabled: value })} label="Учебный помощник студентам" description="Курс может дополнительно отключить его для конкретной работы" />
          <small className="muted">Провайдер, модель и бюджет задаются конфигурацией сервера и не выдаются этим API.</small>
        </Card>

        <Card className="settings-card">
          <header><span><Server /></span><div><h2>Компиляция и запуск</h2><p>Верхние границы ресурсов отдельного runner-контейнера.</p></div></header>
          <Toggle checked={settings.runnerEnabled} onChange={(value) => patch({ runnerEnabled: value })} label="Разрешить запуски" description="Отключение не мешает писать и сдавать код" />
          <div className="setting-inline">
            <Field label="Время процессора"><div className="unit-input"><input type="number" value={settings.runnerCpuSeconds} onChange={(event) => patch({ runnerCpuSeconds: Number(event.target.value) })} /><span>сек</span></div></Field>
            <Field label="Память"><div className="unit-input"><input type="number" value={settings.runnerMemoryMb} onChange={(event) => patch({ runnerMemoryMb: Number(event.target.value) })} /><span>МБ</span></div></Field>
          </div>
          <div className="risk-note"><TriangleAlert /><p><strong>Временный режим без песочницы</strong>Код выполняется обычным процессом внутри runner-контейнера. Файловая и сетевая изоляция отдельных запусков отключена; сохраняются лимиты времени, памяти и вывода.</p></div>
        </Card>

        <Card className="settings-card">
          <header><span><Database /></span><div><h2>Хранение</h2><p>История, снимки, эксперименты и аудит.</p></div></header>
          <Field label="Срок хранения учебных записей"><div className="unit-input"><input type="number" value={settings.retentionDays} onChange={(event) => patch({ retentionDays: Number(event.target.value) })} /><span>дней</span></div></Field>
          <Field className="settings-card__spaced-field" label="Системное объявление"><textarea rows={3} value={settings.incidentBanner} onChange={(event) => patch({ incidentBanner: event.target.value })} placeholder="Пусто — объявление не показывается" /></Field>
        </Card>
      </section>

      <aside>
        <Card className="health-card">
          <header><div><span><Activity /></span><div><h2>Состояние backend</h2><p>Ответ `/system/health`</p></div></div><button onClick={() => void load()} aria-label="Обновить состояние"><RefreshCcw /></button></header>
          {health ? <>
            <div><span className={`status-dot status-dot--${health.status === 'ok' ? 'ok' : health.status === 'degraded' ? 'warning' : 'danger'}`} /><span><strong>Основной API</strong><small>build {health.build}</small></span><Badge tone={health.status === 'ok' ? 'success' : health.status === 'degraded' ? 'warning' : 'danger'}>{health.status}</Badge></div>
            <div><span className={`status-dot status-dot--${health.database === 'ok' ? 'ok' : 'danger'}`} /><span><strong>База данных</strong><small>проверка соединения backend</small></span><Badge tone={health.database === 'ok' ? 'success' : 'danger'}>{health.database}</Badge></div>
          </> : <p className="health-unknown">Диагностика недоступна. Состояние runner, LMS и ИИ не предполагается по значениям переключателей.</p>}
        </Card>

        <Card className="settings-card compact">
          <header><span><KeyRound /></span><div><h2>Административный токен</h2><p>Управляется конфигурацией сервера</p></div></header>
          <small className="muted">API текущей версии позволяет открыть и снять повышенный доступ, но не поддерживает просмотр или ротацию ключа из интерфейса.</small>
        </Card>

        <Card className="settings-card compact">
          <header><span><ShieldCheck /></span><div><h2>Дополнительный allow-list LMS</h2><p>Ограничение поверх включённых подключений</p></div></header>
          {settings.allowedLmsOrigins.length
            ? settings.allowedLmsOrigins.map((origin) => <div className="origin-row" key={origin}><span className="status-dot status-dot--ok" /><code>{origin}</code></div>)
            : <small className="muted">Список пуст — импорт разрешён с origin включённых подключений LMS. Непустой список дополнительно ограничит их.</small>}
        </Card>
      </aside>
    </div>

    <LmsSyncErrorDialog
      open={Boolean(syncErrorCourse)}
      courseTitle={syncErrorCourse?.title ?? ''}
      diagnostic={syncErrorCourse?.syncError}
      retrying={syncRetrying}
      onClose={() => setSyncErrorCourse(null)}
      onRetry={() => void retryCourseSync()}
    />

    <Modal
      open={Boolean(courseToRemove)}
      title="Убрать курс из системы?"
      onClose={() => setCourseToRemove(null)}
      footer={<>
        <Button variant="ghost" disabled={Boolean(removingCourseId)} onClick={() => setCourseToRemove(null)}>Отмена</Button>
        <Button variant="danger" loading={Boolean(removingCourseId)} onClick={() => void removeCourse()}><Trash2 size={16} /> Убрать из системы</Button>
      </>}
    >
      <div className="course-removal-confirmation">
        <TriangleAlert />
        <div>
          <strong>{courseToRemove?.title}</strong>
          <p>Курс исчезнет у всех пользователей. Существующие работы, сдачи и история останутся в базе данных.</p>
        </div>
      </div>
    </Modal>

    <Modal
      open={Boolean(issuedTeacherToken)}
      title="Сохраните токен преподавателя"
      onClose={() => { setIssuedTeacherToken(null); setTeacherTokenCopyStatus('idle'); }}
      footer={<>
        <Button
          variant="secondary"
          loading={teacherTokenCopyStatus === 'copying'}
          onClick={() => void copyIssuedTeacherToken()}
        >
          {teacherTokenCopyStatus === 'success' ? <Check size={16} /> : teacherTokenCopyStatus !== 'copying' ? <Copy size={16} /> : null}
          {teacherTokenCopyStatus === 'success' ? 'Скопировано' : teacherTokenCopyStatus === 'error' ? 'Повторить копирование' : 'Скопировать'}
        </Button>
        <Button onClick={() => { setIssuedTeacherToken(null); setTeacherTokenCopyStatus('idle'); }}>Я сохранил токен</Button>
      </>}
    >
      <div className="teacher-token-issued">
        <TriangleAlert />
        <div>
          <strong>{issuedTeacherToken?.label}</strong>
          <p>Передайте токен преподавателю по защищённому каналу. Администратор сможет снова показать или заменить его в настройках системы.</p>
          <code ref={issuedTeacherTokenValueRef}>{issuedTeacherToken?.token}</code>
          {teacherTokenCopyStatus === 'success' && <p className="teacher-token-copy-status teacher-token-copy-status--success" role="status">Токен скопирован в буфер обмена.</p>}
          {teacherTokenCopyStatus === 'error' && <p className="teacher-token-copy-status teacher-token-copy-status--error" role="alert">Автокопирование недоступно. Токен выделен — нажмите Ctrl+C или ⌘C.</p>}
        </div>
      </div>
    </Modal>

    <Modal
      open={Boolean(teacherTokenToEdit)}
      title="Изменить токен преподавателя"
      onClose={closeTeacherTokenEditor}
      footer={<>
        <Button variant="ghost" disabled={Boolean(updatingTeacherTokenId)} onClick={closeTeacherTokenEditor}>Отмена</Button>
        <Button
          loading={Boolean(updatingTeacherTokenId)}
          disabled={Boolean(updatingTeacherTokenId)}
          onClick={() => {
            const form = document.getElementById('teacher-token-edit-form');
            if (form instanceof HTMLFormElement) form.requestSubmit();
          }}
        >
          <Save size={16} /> Сохранить токен
        </Button>
      </>}
    >
      <form id="teacher-token-edit-form" className="teacher-token-edit-form" noValidate onSubmit={(event) => void updateTeacherToken(event)}>
        <p>
          Токен <strong>{teacherTokenToEdit?.label}</strong> будет заменён. Привязка к учётной записи Moodle сохранится,
          но прежнее значение сразу перестанет подходить для нового входа.
        </p>
        <Field
          label="Новое значение токена"
          hint="Ровно 8 символов: латинские буквы A–Z, a–z и цифры 0–9."
          error={teacherTokenEditError ?? undefined}
        >
          <div className="input-with-icon">
            <KeyRound size={18} />
            <input
              aria-label="Новое значение токена преподавателя"
              type="text"
              autoComplete="off"
              data-1p-ignore
              aria-invalid={Boolean(teacherTokenEditError)}
              maxLength={8}
              pattern="[A-Za-z0-9]{8}"
              spellCheck={false}
              value={teacherTokenDraft}
              disabled={Boolean(updatingTeacherTokenId)}
              onChange={(event) => {
                setTeacherTokenDraft(event.target.value);
                if (teacherTokenEditError) setTeacherTokenEditError(null);
              }}
              placeholder="Ab12Cd34"
              autoFocus
            />
          </div>
        </Field>
      </form>
    </Modal>

    <Modal
      open={Boolean(teacherTokenToRemove)}
      title="Отозвать токен преподавателя?"
      onClose={() => { if (!removingTeacherTokenId) setTeacherTokenToRemove(null); }}
      footer={<>
        <Button variant="ghost" disabled={Boolean(removingTeacherTokenId)} onClick={() => setTeacherTokenToRemove(null)}>Отмена</Button>
        <Button variant="danger" loading={Boolean(removingTeacherTokenId)} onClick={() => void removeTeacherToken()}><Trash2 size={16} /> Отозвать токен</Button>
      </>}
    >
      <div className="course-removal-confirmation">
        <TriangleAlert />
        <div>
          <strong>{teacherTokenToRemove?.label}</strong>
          <p>Хэш будет удалён. Если токен уже был привязан, пользователь немедленно потеряет роль преподавателя и продолжит работу как студент.</p>
        </div>
      </div>
    </Modal>
  </div>;
}
