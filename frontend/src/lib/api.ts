import {
  cloneDemo, demoAssessments, demoAttempt, demoConnections, demoCourseCatalog, demoCourses, demoExperiment,
  demoHistory, demoSettings, demoSubmissions, failedRun, studentCourse, studentSession,
  teacherCourse, teacherSession,
} from './demo';
import { languageForPath, unwrapList } from './utils';
import { createUuid } from './uuid';
import type {
  Assessment, AssessmentPublicationTargets, Attempt, AttemptStatus, AuthConnection, AuthorshipAnalysis, AvailabilityRule, Course, CourseCatalogEntry, CourseGroup, CourseSyncStatus, Diagnostic, EvidenceReport, HiddenTestManifestV1, HistoryEvent, InteractiveRun, LmsActivity, Role, RunResult,
  CourseImportJob, MoodleHistoryImportEvent, Session, SimilarityAnalysis, SimilarityComparison, SimilarityMatch, Submission, SystemHealth, SystemSettings, TaskBankItem, TaskVersion, TeacherAccessToken, TeacherAccessTokenIssued, TeacherExperiment, WorkspaceFile,
} from '../types';

type DemoMode = 'always' | 'auto' | 'never';
const envMode = import.meta.env.VITE_DEMO_MODE;
export const demoMode: DemoMode = envMode ?? (import.meta.env.DEV ? 'auto' : 'never');
export const apiBase = (import.meta.env.VITE_API_BASE ?? '/api/v1').replace(/\/$/, '');
const demoDelay = (ms = 180) => new Promise((resolve) => setTimeout(resolve, ms));

let csrfToken: string | undefined;
let csrfPromise: Promise<string> | null = null;
let demoSession: Session | null = null;
let attemptState = cloneDemo(demoAttempt);
let settingsState = cloneDemo(demoSettings);
let submissionsState = cloneDemo(demoSubmissions);
let experimentState = cloneDemo(demoExperiment);
let taskBankState: TaskBankItem[] = [];
let assessmentState = cloneDemo(demoAssessments);
let evidenceState: EvidenceReport[] = [];
let courseCatalogState = cloneDemo(demoCourseCatalog);
let teacherTokenState: TeacherAccessToken[] = [];
const courseImportState = new Map<string, { job: CourseImportJob; url: string }>();

export const AUTH_UNAUTHORIZED_EVENT = 'eduprog:auth-required';

const LOCALIZED_API_ERROR_MESSAGES: Readonly<Record<string, string>> = {
  ACTIVE_REVIEW_CLAIM_REQUIRED: 'Сначала закрепите работу за собой.',
  CLAIM_NOT_ACTIVE: 'Резервирование работы больше не действует. Откройте работу повторно.',
  CLAIM_NOT_OWNED: 'Работа закреплена за другим преподавателем.',
  CLAIM_EXPIRED: 'Срок резервирования работы истёк. Откройте работу повторно.',
  CLAIM_NOT_FOUND: 'Резервирование работы не найдено. Откройте работу повторно.',
  INTEGRATION_ERROR: 'Не удалось выполнить обмен с внешним сервисом.',
  NOT_CONFIGURED: 'Подключение к внешнему сервису не настроено.',
  TIMEOUT: 'Внешний сервис не ответил вовремя. Повторите попытку.',
  BROWSER_BUSY: 'Браузерный коннектор Moodle занят. Повторите синхронизацию через несколько секунд.',
  UNAVAILABLE: 'Внешний сервис временно недоступен. Повторите попытку позже.',
  INVALID_RESPONSE: 'Внешний сервис вернул неожиданный ответ.',
  RESPONSE_TOO_LARGE: 'Ответ внешнего сервиса слишком большой.',
  LMS_IMPORT_REQUIRES_CONFIGURATION: 'Импортированная работа ещё не готова. Синхронизируйте курс с Moodle.',
  MOODLE_SOURCE_UNCONFIRMED: 'Moodle не подтвердил все параметры работы. Повторите синхронизацию курса.',
  MOODLE_ANSWER_TRANSPORT_UNSUPPORTED: 'Moodle не подтвердил поддерживаемый способ отправки программного ответа.',
  LMS_DELIVERY_PROFILE_UNRESOLVED: 'Moodle ещё не подтвердил формат ответа. Синхронизируйте курс и повторите запуск попытки.',
  MOODLE_ONLINE_TEXT_SINGLE_TRANSLATION_UNIT_REQUIRED: 'Это задание Moodle принимает только один основной файл C/C++; вспомогательные текстовые файлы в ответ не отправляются.',
  MOODLE_PUBLICATION_TARGETS_REQUIRED: 'Выберите хотя бы одну группу преподавателя.',
  INDIVIDUAL_PUBLICATION_UNSUPPORTED: 'Открывать Moodle-работу можно только группе; индивидуальный доступ Moodle проверит при запуске.',
  MOODLE_ASSESSMENT_UNAVAILABLE: 'Работа сейчас недоступна для вашей учётной записи Moodle.',
  MOODLE_REAUTHENTICATION_REQUIRED: 'Войдите в Moodle заново и повторите запуск работы.',
  MOODLE_SESSION_BUSY: 'Сессия Moodle занята. Повторите запуск через несколько секунд.',
  MOODLE_RUNTIME_MAPPING_UNAVAILABLE: 'Не удалось однозначно сопоставить работу с заданием Moodle. Попросите преподавателя синхронизировать курс.',
  MOODLE_RUNTIME_PREPARATION_REQUIRED: 'Перед открытием редактора нужно проверить попытку в Moodle. Вернитесь к работе и откройте её ещё раз.',
  MOODLE_RUNTIME_PREPARATION_STALE: 'Сопоставление с Moodle устарело. Попросите преподавателя синхронизировать курс и повторите запуск.',
  MOODLE_RUNTIME_PREPARATION_FAILED: 'Moodle не подтвердил запуск попытки. Повторите открытие работы; если ошибка сохранится, сообщите преподавателю.',
  MOODLE_RUNTIME_PREPARATION_UNAVAILABLE: 'Moodle временно недоступен. Попробуйте открыть работу ещё раз через несколько секунд.',
  MOODLE_RUNTIME_PREPARATION_MISMATCH: 'Moodle открыл другую работу. Попытка не была запущена; сообщите преподавателю.',
  MOODLE_ATTEMPT_BINDING_CONFLICT: 'Открытая попытка не совпадает с попыткой Moodle. Работа не изменена; обновите страницу и повторите вход.',
  MOODLE_ATTEMPT_STILL_FINALIZING: 'Предыдущая попытка ещё сохраняется в Moodle. Подождите несколько секунд и откройте работу снова.',
  INVALID_MOODLE_PREPARATION: 'Moodle вернул неполные параметры задания. Попытка не была запущена; сообщите преподавателю.',
  ATTEMPT_READ_ONLY: 'Эта попытка уже завершена и доступна только для чтения.',
  DEADLINE_PASSED: 'Время выполнения работы закончилось. Редактирование и запуск программы недоступны.',
  ATTEMPT_LIMIT_REACHED: 'Доступное число попыток исчерпано.',
  MOODLE_ATTEMPT_SUPERSEDED: 'Эта попытка уже не последняя в Moodle. Проверять и оценивать нужно последнюю попытку после её завершения.',
  STUDENT_SCOPE_REQUIRED: 'Можно открывать работу только своим студентам.',
  ASSESSMENT_NOT_ASSIGNED: 'Работа не назначена вашей группе.',
  LMS_ATTEMPT_FINALIZED: 'Сеанс работы завершён через Moodle.',
};

const LOCALIZED_API_MESSAGE_TEXT: Readonly<Record<string, string>> = {
  'LMS synchronization failed': 'Не удалось синхронизировать курс с Moodle.',
  'Internal server error': 'Внутренняя ошибка сервера. Повторите попытку позже.',
};

function localizedApiErrorMessage(code: string, message: string): string {
  return LOCALIZED_API_ERROR_MESSAGES[code] ?? LOCALIZED_API_MESSAGE_TEXT[message] ?? message;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public traceId?: string,
    public issues: Array<{ field?: string; code?: string; message: string }> = [],
  ) {
    super(localizedApiErrorMessage(code, message));
    this.name = 'ApiError';
  }
}

async function ensureCsrf(force = false): Promise<string> {
  if (force) csrfToken = undefined;
  if (csrfToken && !force) return csrfToken;
  if (!csrfPromise) {
    csrfPromise = fetch(`${apiBase}/auth/csrf`, { credentials: 'include', headers: { Accept: 'application/json' } })
      .then(async (response) => {
        if (!response.ok) throw new ApiError(response.status, 'CSRF_FAILED', 'Не удалось подготовить защищённый запрос');
        const body = await response.json() as { csrf_token: string };
        if (!body.csrf_token) throw new ApiError(response.status, 'CSRF_FAILED', 'Сервер не вернул CSRF-токен');
        csrfToken = body.csrf_token;
        return body.csrf_token;
      }).finally(() => { csrfPromise = null; });
  }
  return csrfPromise;
}

async function request<T>(path: string, init: RequestInit = {}, retryCsrf = true): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase();
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) await ensureCsrf();
  const headers = new Headers(init.headers);
  headers.set('Accept', 'application/json');
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  if (csrfToken && !['GET', 'HEAD', 'OPTIONS'].includes(method)) headers.set('X-CSRFToken', csrfToken);
  const response = await fetch(`${apiBase}${path}`, { ...init, headers, credentials: 'include' });
  const returnedCsrf = response.headers.get('X-CSRFToken');
  if (returnedCsrf) csrfToken = returnedCsrf;
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as Record<string, unknown>;
    const error = apiError(response.status, body);
    if (response.status === 403 && error.code === 'CSRF_FAILED' && retryCsrf && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      await ensureCsrf(true);
      return request<T>(path, init, false);
    }
    if (response.status === 401 && typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent(AUTH_UNAUTHORIZED_EVENT));
    }
    throw error;
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

function apiError(status: number, body: Record<string, unknown>): ApiError {
  const detail = body.detail;
  const nested = detail && typeof detail === 'object' && !Array.isArray(detail)
    ? detail as Record<string, unknown>
    : undefined;
  const code = String(body.code ?? nested?.code ?? 'REQUEST_FAILED');
  const rawIssues = body.errors ?? nested?.errors;
  const issues = Array.isArray(rawIssues) ? rawIssues.flatMap((entry) => {
    if (!entry || typeof entry !== 'object') return [];
    const issue = entry as Record<string, unknown>;
    if (typeof issue.message !== 'string') return [];
    return [{
      field: typeof issue.field === 'string' ? issue.field : undefined,
      code: typeof issue.code === 'string' ? issue.code : undefined,
      message: issue.message,
    }];
  }) : [];
  let message = body.message ?? nested?.message;
  if (!message && typeof detail === 'string') message = detail;
  if (!message && Array.isArray(body.errors)) {
    message = body.errors
      .map((entry) => entry && typeof entry === 'object' && 'msg' in entry ? String(entry.msg) : '')
      .filter(Boolean)
      .join('; ');
  }
  return new ApiError(
    status,
    code,
    typeof message === 'string' && message ? message : 'Не удалось выполнить запрос',
    body.trace_id ? String(body.trace_id) : undefined,
    issues,
  );
}

async function withDemo<T>(real: () => Promise<T>, demo: () => T | Promise<T>): Promise<T> {
  if (demoMode === 'always') {
    await demoDelay();
    return demo();
  }
  try {
    return await real();
  } catch (error) {
    if (demoMode !== 'auto' || !import.meta.env.DEV || (error instanceof ApiError && error.status < 500 && error.status !== 404)) throw error;
    await demoDelay();
    return demo();
  }
}

function mapRole(value: unknown): Role {
  const role = String(value).toUpperCase();
  if (role === 'STUDENT' || role === 'TEACHER') return role;
  throw new ApiError(502, 'INVALID_ROLE_PROJECTION', 'Внешняя LMS вернула неподдерживаемую роль');
}

function mapSession(raw: any): Session {
  const memberships = unwrapList<any>(raw.memberships ?? raw.course_memberships ?? []).map((item) => ({
    courseId: String(item.course_id ?? item.course?.id ?? ''),
    courseName: String(item.course_name ?? item.course?.title ?? 'Курс'),
    role: mapRole(item.role), group: item.group_name ?? item.group,
  }));
  const roles = unwrapList<any>(raw.roles ?? []);
  if (!memberships.length && roles.length) {
    roles.forEach((role) => memberships.push({ courseId: '', courseName: 'Все доступные курсы', role: mapRole(role), group: undefined }));
  }
  return {
    id: String(raw.principal?.id ?? raw.id ?? ''),
    displayName: String(raw.principal?.display_name ?? raw.display_name ?? raw.name ?? 'Пользователь'),
    providerName: String(raw.provider?.name ?? raw.provider_name ?? 'Внешняя LMS'),
    memberships, capabilities: raw.capabilities ?? [],
    adminElevationExpiresAt: raw.admin_elevation_expires_at ?? undefined,
  };
}

function mapCourse(raw: any, session?: Session | null): Course {
  const membership = session?.memberships.find((item) => item.courseId === String(raw.id));
  const sourceStatus = String(raw.sync_status ?? 'PENDING').toUpperCase();
  const syncStatus: Course['syncStatus'] = ['CURRENT', 'SYNCED'].includes(sourceStatus)
    ? 'SYNCED'
    : sourceStatus === 'SYNCING' ? 'SYNCING'
      : sourceStatus === 'FAILED' || sourceStatus === 'ERROR' ? 'ERROR' : 'STALE';
  return {
    id: String(raw.id), title: String(raw.title ?? raw.name ?? 'Курс'),
    shortName: String(raw.code ?? raw.short_name ?? raw.title ?? 'Курс'),
    role: mapRole(raw.role ?? membership?.role ?? session?.memberships[0]?.role ?? 'STUDENT'),
    group: raw.group_name ?? membership?.group, term: String(raw.term ?? ''),
    provider: String(raw.provider_name ?? raw.provider ?? 'Внешняя LMS'), externalUrl: raw.external_url ?? raw.url,
    syncStatus,
    syncError: raw.sync_error_code || raw.sync_error_message ? {
      code: String(raw.sync_error_code ?? 'SYNC_FAILED'),
      message: String(raw.sync_error_message ?? ''),
      at: raw.sync_error_at ? String(raw.sync_error_at) : undefined,
      retryable: Boolean(raw.sync_error_retryable),
    } : undefined,
    syncedAt: raw.synced_at, activeCount: raw.active_count === undefined || raw.active_count === null ? undefined : Number(raw.active_count), uncheckedCount: raw.unchecked_count,
  };
}

function mapCourseSyncStatus(raw: any): CourseSyncStatus {
  const sourceStatus = String(raw.status ?? raw.sync_status ?? 'PENDING').toUpperCase();
  const syncStatus: CourseSyncStatus['syncStatus'] = ['CURRENT', 'SYNCED'].includes(sourceStatus)
    ? 'SYNCED'
    : sourceStatus === 'SYNCING' ? 'SYNCING'
      : sourceStatus === 'FAILED' || sourceStatus === 'ERROR' ? 'ERROR' : 'STALE';
  const errorCode = raw.error_code ?? raw.sync_error_code;
  const errorMessage = raw.error_message ?? raw.sync_error_message;
  const errorAt = raw.error_at ?? raw.sync_error_at;
  const retryable = raw.retryable ?? raw.sync_error_retryable;
  return {
    courseId: String(raw.course_id ?? raw.id ?? ''),
    syncStatus,
    updatedAt: raw.updated_at ? String(raw.updated_at) : undefined,
    syncError: errorCode || errorMessage ? {
      code: String(errorCode ?? 'SYNC_FAILED'),
      message: String(errorMessage ?? ''),
      at: errorAt ? String(errorAt) : undefined,
      retryable: Boolean(retryable),
    } : undefined,
  };
}

function mapCourseCatalogEntry(raw: any): CourseCatalogEntry {
  const sourceStatus = String(raw.sync_status ?? 'PENDING').toUpperCase();
  const syncStatus: CourseCatalogEntry['syncStatus'] = ['CURRENT', 'SYNCED'].includes(sourceStatus)
    ? 'SYNCED'
    : sourceStatus === 'SYNCING' ? 'SYNCING'
      : sourceStatus === 'FAILED' || sourceStatus === 'ERROR' ? 'ERROR' : 'STALE';
  return {
    id: String(raw.id),
    connectionId: String(raw.connection_id ?? raw.connection?.id ?? ''),
    connectionName: String(raw.connection_name ?? raw.connection?.name ?? 'Внешняя LMS'),
    externalId: String(raw.external_id ?? ''),
    title: String(raw.title ?? raw.name ?? 'Курс'),
    shortName: String(raw.short_name ?? ''),
    externalUrl: String(raw.external_url ?? ''),
    syncStatus,
    syncError: raw.sync_error_code || raw.sync_error_message ? {
      code: String(raw.sync_error_code ?? 'SYNC_FAILED'),
      message: String(raw.sync_error_message ?? ''),
      at: raw.sync_error_at ? String(raw.sync_error_at) : undefined,
      retryable: Boolean(raw.sync_error_retryable),
    } : undefined,
    addedAt: raw.added_at ? String(raw.added_at) : undefined,
  };
}

function mapTeacherAccessToken(raw: any): TeacherAccessToken {
  return {
    id: String(raw.id),
    label: String(raw.label ?? 'Преподаватель'),
    publicId: String(raw.public_id ?? ''),
    hashFingerprint: String(raw.hash_fingerprint ?? ''),
    canReveal: Boolean(raw.can_reveal),
    boundPrincipalId: raw.bound_principal_id ? String(raw.bound_principal_id) : undefined,
    boundDisplayName: raw.bound_display_name ? String(raw.bound_display_name) : undefined,
    useCount: Number(raw.use_count ?? 0),
    lastUsedAt: raw.last_used_at ? String(raw.last_used_at) : undefined,
    createdAt: String(raw.created_at ?? ''),
  };
}

function mapMoodleHistoryImportEvent(raw: any): MoodleHistoryImportEvent {
  const state = String(raw.state ?? 'PENDING').toUpperCase();
  const payload = raw.payload && typeof raw.payload === 'object' && !Array.isArray(raw.payload)
    ? raw.payload as Record<string, unknown>
    : {};
  return {
    id: String(raw.id ?? ''),
    courseId: raw.course_id ? String(raw.course_id) : undefined,
    aggregateId: String(raw.aggregate_id ?? ''),
    actorKey: payload.actor_external_subject ? String(payload.actor_external_subject) : undefined,
    state: ['PENDING', 'PROCESSING', 'RETRY', 'DELIVERED', 'FAILED', 'BLOCKED'].includes(state)
      ? state as MoodleHistoryImportEvent['state']
      : 'FAILED',
    createdAt: String(raw.created_at ?? ''),
    updatedAt: String(raw.updated_at ?? raw.created_at ?? ''),
    receipt: raw.receipt && typeof raw.receipt === 'object' && !Array.isArray(raw.receipt)
      ? raw.receipt as Record<string, unknown>
      : {},
  };
}

function mapAssessment(raw: any, courseId: string): Assessment {
  const statusMap: Record<string, Assessment['status']> = {
    DRAFT: 'UPCOMING', PUBLISHED: 'AVAILABLE', OPEN: 'AVAILABLE', ACTIVE: 'IN_PROGRESS',
    IN_PROGRESS: 'IN_PROGRESS', SUBMITTED: 'SUBMITTED', GRADED: 'GRADED', CLOSED: 'CLOSED', UPCOMING: 'UPCOMING',
  };
  const kind = String(raw.type ?? raw.kind ?? 'LAB').toUpperCase() as Assessment['kind'];
  const sourceStatus = String(raw.status ?? '').toUpperCase();
  const task = raw.task ?? raw.items?.[0]?.task;
  const mappedStatus = statusMap[sourceStatus] ?? 'AVAILABLE';
  const status = mappedStatus === 'AVAILABLE' && raw.attempt_id && raw.progress !== null && raw.progress !== undefined
    ? 'IN_PROGRESS'
    : mappedStatus;
  return {
    id: String(raw.id), courseId: String(raw.course_id ?? raw.course?.id ?? raw.course ?? courseId), title: String(raw.title ?? 'Работа'), summary: String(raw.summary ?? raw.description ?? raw.instructions ?? ''),
    kind: ['LAB', 'INDEPENDENT', 'CONTROL', 'EXAM'].includes(kind) ? kind : 'LAB',
    status,
    startsAt: raw.opens_at ?? raw.starts_at, deadlineAt: raw.closes_at ?? raw.deadline_at,
    durationMinutes: raw.duration_minutes === undefined || raw.duration_minutes === null
      ? (raw.duration_seconds ? Math.ceil(Number(raw.duration_seconds) / 60) : undefined)
      : Number(raw.duration_minutes),
    attemptLimit: raw.attempt_limit === undefined || raw.attempt_limit === null ? undefined : Number(raw.attempt_limit),
    attemptId: raw.attempt_id, progress: raw.progress === undefined || raw.progress === null ? undefined : Number(raw.progress),
    score: raw.score === undefined || raw.score === null ? undefined : Number(raw.score),
    maxScore: Number(raw.max_score ?? 10), fileMode: raw.file_mode ?? (raw.multi_file || task?.multi_file ? 'MULTI' : 'SINGLE'),
    language: String(raw.language ?? task?.language ?? 'CPP').toUpperCase() === 'C' ? 'C' : 'CPP',
    standard: String(raw.standard ?? raw.language_standard ?? task?.language_standard ?? 'C/C++'),
    pastePolicy: ['ALLOW', 'UNRESTRICTED'].includes(String(raw.paste_policy).toUpperCase()) ? 'ALLOW' : 'STRICT',
    aiEnabled: Boolean(raw.ai_enabled ?? raw.student_ai_enabled), submissionsCount: raw.submissions_count, uncheckedCount: raw.unchecked_count,
    reviewRequired: raw.review_required === undefined ? undefined : Boolean(raw.review_required),
    decisionSupportEnabled: raw.decision_support_enabled === undefined ? undefined : Boolean(raw.decision_support_enabled),
    availabilityRules: raw.availability_rules === undefined ? undefined : unwrapList<any>(raw.availability_rules).map(mapAvailabilityRule),
    publicationStatus: ['DRAFT', 'PUBLISHED', 'CLOSED'].includes(String(raw.status).toUpperCase()) ? String(raw.status).toUpperCase() as Assessment['publicationStatus'] : undefined,
    taskVersionIds: unwrapList<any>(raw.items ?? []).map((item) => item.task_version ?? item.task?.id).filter((value) => value !== undefined && value !== null).map(String),
    policy: raw.policy && typeof raw.policy === 'object' ? raw.policy as Record<string, unknown> : undefined,
    requiresLiveLmsPreparation: Boolean(raw.requires_live_lms_preparation),
  };
}

function mapAvailabilityRule(raw: any): AvailabilityRule {
  const target = String(raw.target_type ?? 'COURSE').toUpperCase();
  return {
    id: String(raw.id), targetType: ['COURSE', 'GROUP', 'PRINCIPAL'].includes(target) ? target as AvailabilityRule['targetType'] : 'COURSE',
    targetExternalId: String(raw.target_external_id ?? ''), allowed: raw.allowed !== false,
    opensAt: raw.opens_at || undefined, closesAt: raw.closes_at || undefined,
    durationSeconds: raw.duration_seconds === undefined || raw.duration_seconds === null ? undefined : Number(raw.duration_seconds),
    attemptLimit: raw.attempt_limit === undefined || raw.attempt_limit === null ? undefined : Number(raw.attempt_limit),
  };
}

function mapAssessmentPublicationTargets(raw: any): AssessmentPublicationTargets {
  return {
    groups: unwrapList<any>(raw.groups).map((group) => ({
      id: String(group.id),
      externalId: String(group.external_id ?? ''),
      name: String(group.name ?? ''),
    })),
    principals: unwrapList<any>(raw.principals).map((principal) => ({
      principalId: String(principal.principal_id),
      externalSubject: String(principal.external_subject ?? ''),
      displayName: String(principal.display_name ?? ''),
      groups: unwrapList<unknown>(principal.groups).map(String),
      opensAt: principal.opens_at || undefined,
      closesAt: principal.closes_at || undefined,
      durationSeconds: principal.duration_seconds === undefined || principal.duration_seconds === null
        ? undefined : Number(principal.duration_seconds),
      attemptLimit: principal.attempt_limit === undefined || principal.attempt_limit === null
        ? undefined : Number(principal.attempt_limit),
      attemptsUnlimited: Boolean(principal.attempts_unlimited),
    })),
    overridesConfirmed: Boolean(raw.overrides_confirmed),
  };
}

function mapCourseImport(raw: any): CourseImportJob {
  return { id: String(raw.id), state: String(raw.state), externalCourseId: String(raw.external_course_id ?? ''), preview: raw.preview ?? {}, capabilityReport: raw.capability_report ?? {}, confirmedCourse: raw.confirmed_course ? String(raw.confirmed_course) : undefined, error: raw.error || undefined };
}

function mapTaskItem(raw: any): TaskBankItem {
  const version = raw.latest_version;
  return {
    id: String(raw.id), course: raw.course ? String(raw.course) : undefined, scope: raw.scope ?? 'COURSE', slug: String(raw.slug ?? ''), category: String(raw.category ?? ''), tags: raw.tags ?? [], createdAt: raw.created_at,
    latestVersion: version ? mapTaskVersion(version, String(raw.id)) : undefined,
  };
}

function mapTaskVersion(raw: any, itemId?: string): TaskVersion {
  const manifest = raw.hidden_test_manifest;
  return {
    id: String(raw.id), itemId: String(raw.item_id ?? raw.item ?? itemId ?? ''), number: Number(raw.number ?? 0),
    title: String(raw.title ?? ''), statement: String(raw.statement ?? ''), language: String(raw.language ?? 'CPP'),
    languageStandard: String(raw.language_standard ?? ''), multiFile: Boolean(raw.multi_file), maxScore: Number(raw.max_score ?? 10), status: String(raw.status ?? 'DRAFT'),
    hiddenTestManifest: manifest?.schema_version === 1 && Array.isArray(manifest.cases) ? manifest as HiddenTestManifestV1 : undefined,
    aiPolicy: raw.ai_policy && typeof raw.ai_policy === 'object' ? raw.ai_policy as Record<string, unknown> : undefined,
  };
}

function mapEvidenceReport(raw: any): EvidenceReport {
  return {
    id: String(raw.id), submissionId: String(raw.submission_id ?? ''), snapshotId: String(raw.snapshot_id ?? ''),
    taskVersionId: String(raw.task_version_id ?? ''), requestedById: String(raw.requested_by_id ?? ''),
    hiddenTestManifestHash: String(raw.hidden_test_manifest_hash ?? ''), taskContentHash: String(raw.task_content_hash ?? ''),
    snapshotManifestHash: String(raw.snapshot_manifest_hash ?? ''), status: String(raw.status ?? 'FAILED').toUpperCase() as EvidenceReport['status'],
    passedCases: Number(raw.passed_cases ?? 0), totalCases: Number(raw.total_cases ?? 0),
    outcomes: unwrapList<any>(raw.outcomes ?? []).map((outcome) => ({
      caseIndex: Number(outcome.case_index ?? 0), name: String(outcome.name ?? 'Кейс'), runId: String(outcome.run_id ?? ''),
      status: String(outcome.status ?? 'INFRASTRUCTURE_ERROR').toUpperCase() as EvidenceReport['outcomes'][number]['status'],
      comparison: String(outcome.comparison ?? 'EXACT').toUpperCase() as EvidenceReport['outcomes'][number]['comparison'],
      exitCode: outcome.exit_code === null || outcome.exit_code === undefined ? undefined : Number(outcome.exit_code),
      actualStdoutSha256: String(outcome.actual_stdout_sha256 ?? ''), expectedStdoutSha256: String(outcome.expected_stdout_sha256 ?? ''),
      actualStdoutPreview: String(outcome.actual_stdout_preview ?? ''), stderrPreview: String(outcome.stderr_preview ?? ''),
      filesystemIsolated: outcome.filesystem_isolated === null || outcome.filesystem_isolated === undefined ? undefined : Boolean(outcome.filesystem_isolated),
      networkEnabled: outcome.network_enabled === null || outcome.network_enabled === undefined ? undefined : Boolean(outcome.network_enabled),
    })),
    findings: unwrapList<any>(raw.findings ?? []).map((finding) => ({
      code: String(finding.code ?? ''), message: String(finding.message ?? ''),
      caseIndex: finding.case_index === null || finding.case_index === undefined ? undefined : Number(finding.case_index),
      runId: finding.run_id ? String(finding.run_id) : undefined,
    })),
    failureCode: raw.failure_code || undefined, failureMessage: raw.failure_message || undefined,
    completedAt: raw.completed_at || undefined, createdAt: String(raw.created_at ?? new Date().toISOString()),
    updatedAt: String(raw.updated_at ?? raw.created_at ?? new Date().toISOString()),
  };
}

function mapFile(raw: any): WorkspaceFile {
  const path = String(raw.path ?? 'main.cpp');
  return { id: String(raw.id ?? path), path, content: String(raw.content ?? ''), readOnly: Boolean(raw.read_only), language: languageForPath(path) };
}

function mapAttempt(raw: any, workspace?: any): Attempt {
  const state = String(raw.state ?? raw.status ?? 'ACTIVE').toUpperCase();
  const submittedStates = new Set(['SUBMITTED', 'AUTO_SUBMITTED', 'FINALIZED', 'GRADED']);
  const files = unwrapList<any>(workspace?.files ?? raw.files ?? []).map(mapFile);
  const rawPastePolicy = String(raw.paste_policy ?? 'INTERNAL_ONLY').toUpperCase();
  return {
    id: String(raw.id), assessmentId: String(raw.assessment_id ?? raw.assessment?.id ?? raw.assessment ?? ''), title: String(raw.title ?? raw.assessment_title ?? raw.assessment?.title ?? 'Работа'),
    statement: String(raw.statement ?? raw.task_statement ?? ''), status: submittedStates.has(state) ? 'SUBMITTED' : state === 'ACTIVE' ? 'ACTIVE' : 'LOCKED',
    revision: Number(workspace?.current_revision ?? workspace?.revision ?? raw.current_revision ?? raw.workspace_revision ?? raw.revision ?? 0),
    acknowledgedRevision: Number(raw.acknowledged_revision ?? workspace?.current_revision ?? workspace?.revision ?? raw.current_revision ?? raw.workspace_revision ?? 0),
    startedAt: raw.started_at ?? new Date().toISOString(), expectedEndAt: raw.expected_end_at ?? undefined, deadlineAt: raw.deadline_at ?? undefined,
    closureReason: raw.closure_reason ?? undefined, closedAt: raw.closed_at ?? raw.submitted_at ?? undefined,
    lastCheckpointAt: raw.last_checkpoint_at, checkpointStatus: raw.checkpoint_status ?? 'SYNCED',
    pastePolicy: ['ALLOW', 'UNRESTRICTED'].includes(rawPastePolicy) ? 'ALLOW' : 'STRICT', aiEnabled: Boolean(raw.ai_enabled),
    fileMode: (workspace?.multi_file ?? raw.workspace?.multi_file ?? raw.multi_file) ? 'MULTI' : 'SINGLE', files,
    requiresLiveLmsPreparation: Boolean(raw.requires_live_lms_preparation),
  };
}

function mapAttemptStatus(raw: any): AttemptStatus {
  const mapped = mapAttempt(raw);
  return {
    id: mapped.id,
    status: mapped.status,
    closureReason: mapped.closureReason,
    closedAt: mapped.closedAt,
    lastCheckpointAt: mapped.lastCheckpointAt,
    checkpointStatus: mapped.checkpointStatus,
  };
}

function mapDiagnostics(items: any[]): Diagnostic[] {
  return items.map((item, index) => {
    const rawSeverity = String(item.severity ?? 'error').toLowerCase();
    const severity: Diagnostic['severity'] = ['error', 'warning', 'info'].includes(rawSeverity) ? rawSeverity as Diagnostic['severity'] : 'error';
    return {
    id: String(item.id ?? `diagnostic-${index}`), fileId: item.file_id,
    path: typeof item.file === 'string' ? item.file : item.file?.path ?? item.path,
    line: item.range?.start_line ?? item.line, column: item.range?.start_column ?? item.column,
    endLine: item.range?.end_line ?? item.end_line, endColumn: item.range?.end_column ?? item.end_column,
    severity, code: item.code, message: String(item.message ?? ''),
    notes: item.notes ?? item.related?.map((note: any) => String(note.message ?? note)),
    };
  });
}

function mapExperiment(raw: any, submissionId?: string): TeacherExperiment {
  return {
    id: String(raw.id), submissionId: String(raw.submission_id ?? raw.submission ?? submissionId ?? ''),
    revision: Number(raw.revision ?? 0), files: unwrapList<any>(raw.files ?? []).map(mapFile),
    changed: Boolean(raw.changed ?? Number(raw.revision ?? 0) > 0), createdAt: raw.created_at ?? new Date().toISOString(),
  };
}

function mapRun(raw: any): RunResult {
  const status = String(raw.status ?? raw.state ?? 'QUEUED').toUpperCase();
  const result = raw.result ?? raw;
  return {
    id: String(raw.id), state: (status === 'SUCCEEDED' ? 'COMPLETED' : status) as RunResult['state'],
    revision: Number(raw.revision ?? 0), exitCode: result.exit_code, stdout: String(result.stdout ?? ''), stderr: String(result.stderr ?? ''),
    durationMs: result.metrics?.wall_time_ms ?? result.duration_ms, memoryKb: result.metrics?.peak_memory_kb ?? result.memory_kb,
    diagnostics: mapDiagnostics(result.diagnostics ?? raw.diagnostics ?? []),
    createdAt: raw.created_at ?? new Date().toISOString(),
  };
}

function mapInteractiveRun(raw: any): InteractiveRun {
  return {
    sessionId: String(raw.session_id ?? raw.sessionId ?? ''),
    status: String(raw.status ?? 'INFRA_ERROR').toUpperCase() as InteractiveRun['status'],
    terminal: Boolean(raw.terminal),
    exitCode: raw.exit_code === null || raw.exit_code === undefined ? undefined : Number(raw.exit_code),
    durationMs: Number(raw.duration_ms ?? 0),
    stdout: String(raw.stdout ?? ''),
    stderr: String(raw.stderr ?? ''),
    outputTruncated: Boolean(raw.output_truncated),
    inputClosed: Boolean(raw.input_closed ?? raw.inputClosed),
    diagnostics: mapDiagnostics(raw.diagnostics ?? []),
  };
}

function mapSubmissionStatus(value: unknown): Submission['status'] {
  const status = String(value ?? 'UNGRADED').toUpperCase();
  return ['UNGRADED', 'CLAIMED', 'GRADED', 'CONFLICT'].includes(status)
    ? status as Submission['status']
    : 'UNGRADED';
}

function mapSubmissionReviewGroup(raw: any) {
  const group = raw.review_group ?? raw.reviewGroup ?? raw.response_group ?? raw.responseGroup ?? raw.quiz_group ?? raw.quizGroup;
  const related = raw.related_submissions ?? raw.relatedSubmissions ?? raw.sibling_submissions ?? raw.siblingSubmissions;
  const sourceItems = group
    ? unwrapList<any>(group.items ?? group.submissions ?? group.responses ?? group.questions ?? [])
    : unwrapList<any>(related ?? []);
  if (!sourceItems.length) return undefined;

  const seen = new Set<string>();
  const items = sourceItems.flatMap((item, index) => {
    const submissionId = item.submission_id ?? item.submissionId ?? item.id;
    if (submissionId === undefined || submissionId === null || seen.has(String(submissionId))) return [];
    seen.add(String(submissionId));
    const rawPosition = Number(item.position ?? item.question_number ?? item.questionNumber ?? item.slot ?? index + 1);
    const score = item.score === undefined || item.score === null ? undefined : Number(item.score);
    const maxScore = item.max_score === undefined && item.maxScore === undefined
      ? undefined
      : Number(item.max_score ?? item.maxScore);
    const statusValue = item.status === undefined || item.status === null
      ? undefined
      : mapSubmissionStatus(item.status);
    return [{
      submissionId: String(submissionId),
      position: Number.isFinite(rawPosition) && rawPosition > 0 ? rawPosition : index + 1,
      title: String(item.title ?? item.question_title ?? item.questionTitle ?? item.assessment_title ?? item.assessmentTitle ?? `Задание ${index + 1}`),
      score: score !== undefined && Number.isFinite(score) ? score : undefined,
      maxScore: maxScore !== undefined && Number.isFinite(maxScore) ? maxScore : undefined,
      status: statusValue,
    }];
  }).sort((left, right) => left.position - right.position);
  if (!items.length) return undefined;
  return {
    id: String(group?.id ?? group?.group_id ?? group?.groupId ?? raw.review_group_id ?? raw.response_group_id ?? `submission-group:${raw.id}`),
    title: group?.title ?? group?.name ?? raw.review_group_title ?? raw.response_group_title,
    items,
  };
}

function mapSubmission(raw: any): Submission {
  const decisionHistory = unwrapList<any>(raw.decision_history ?? []).map(mapReviewDecision);
  const latestDecision = raw.latest_decision
    ? mapReviewDecision(raw.latest_decision)
    : decisionHistory[0];
  const rawOrigin = raw.origin_verification ?? raw.originVerification;
  return {
    id: String(raw.id), source: raw.source ? String(raw.source).toUpperCase() : undefined, assessmentId: String(raw.assessment_id ?? ''), taskVersionId: raw.assigned_task_version_id ? String(raw.assigned_task_version_id) : undefined, courseId: raw.course_id ? String(raw.course_id) : undefined, courseTitle: raw.course_title ? String(raw.course_title) : undefined, assessmentTitle: String(raw.assessment_title ?? raw.assessment?.title ?? 'Работа'),
    studentName: String(raw.student?.display_name ?? raw.student_name ?? 'Студент'), studentGroup: String(raw.group_name ?? raw.student_group ?? '—'),
    submittedAt: raw.submitted_at ?? raw.created_at ?? new Date().toISOString(), status: mapSubmissionStatus(raw.status),
    score: raw.score === undefined || raw.score === null ? undefined : Number(raw.score),
    maxScore: Number(raw.max_score ?? 10), risk: raw.risk ?? 'UNKNOWN', testsPassed: Number(raw.tests_passed ?? 0), testsTotal: Number(raw.tests_total ?? 0),
    reviewRequired: raw.review_required === undefined ? undefined : Boolean(raw.review_required), decisionSupportEnabled: raw.decision_support_enabled === undefined ? undefined : Boolean(raw.decision_support_enabled), canReview: raw.can_review !== false,
    claim: raw.claim ? { id: String(raw.claim.id), ownerId: String(raw.claim.owner_id), ownerName: String(raw.claim.owner_name), expiresAt: raw.claim.lease_expires_at ?? raw.claim.expires_at, mine: Boolean(raw.claim.mine) } : undefined,
    files: unwrapList<any>(raw.files ?? []).map(mapFile), history: mapHistory(raw.history ?? []),
    latestDecision, decisionHistory, reviewGroup: mapSubmissionReviewGroup(raw),
    originVerification: rawOrigin && typeof rawOrigin === 'object' ? {
      state: String(rawOrigin.state ?? 'UNAVAILABLE').toUpperCase() as NonNullable<Submission['originVerification']>['state'],
      transport: rawOrigin.transport ? String(rawOrigin.transport) : undefined,
      checkedAt: rawOrigin.checked_at ?? rawOrigin.checkedAt ?? undefined,
      message: String(rawOrigin.message ?? 'Сведения о происхождении ответа недоступны.'),
    } : undefined,
  };
}

function mapReviewDecision(raw: any) {
  return {
    id: raw.id === undefined || raw.id === null ? undefined : String(raw.id),
    reviewerName: String(raw.reviewer_name ?? raw.reviewer?.display_name ?? 'Преподаватель'),
    grade: Number(raw.grade ?? 0), comment: String(raw.comment ?? ''), revision: Number(raw.revision ?? 1),
    reviewedAt: String(raw.reviewed_at ?? raw.created_at ?? raw.updated_at ?? ''),
    lmsExportState: String(raw.lms_export_state ?? 'PENDING'),
  };
}

function mapHistory(items: any[]): HistoryEvent[] {
  return items.map((item, index) => {
    const eventType = String(item.event_type ?? item.type ?? 'TEXT_EDIT').toUpperCase();
    const explicitType = String(item.type ?? '').toLowerCase();
    const type: HistoryEvent['type'] = ['edit', 'run', 'snapshot', 'paste_blocked', 'internal_paste', 'submit'].includes(explicitType)
      ? explicitType as HistoryEvent['type']
      : eventType.includes('RUN') ? 'run' : eventType.includes('SNAPSHOT') ? 'snapshot' : eventType.includes('SUBMIT') ? 'submit' : item.source === 'INTERNAL_PASTE' ? 'internal_paste' : 'edit';
    const rawClient = item.client ?? item.client_context;
    const client = rawClient && typeof rawClient === 'object' ? {
      ipAddress: rawClient.ip_address ? String(rawClient.ip_address) : undefined,
      browser: rawClient.browser ? String(rawClient.browser) : undefined,
      browserVersion: rawClient.browser_version ? String(rawClient.browser_version) : undefined,
      operatingSystem: rawClient.operating_system ? String(rawClient.operating_system) : undefined,
      deviceType: rawClient.device_type ? String(rawClient.device_type) : undefined,
    } : undefined;
    return {
      id: String(item.id ?? `history-${index}`), type,
      label: item.label ?? (type === 'internal_paste' ? 'Внутренняя вставка' : eventType.startsWith('FILE_') ? 'Изменена структура файлов' : 'Изменён код'),
      detail: item.detail ?? `${item.source ?? 'TYPING'} · последовательность ${item.sequence ?? item.revision ?? 0}`,
      at: item.at ?? item.received_at ?? item.created_at ?? new Date().toISOString(),
      revision: Number(item.revision ?? item.sequence ?? 0),
      client,
    };
  });
}

function mapSettings(raw: any): SystemSettings {
  return {
    revision: raw.revision === undefined ? undefined : Number(raw.revision),
    aiEnabled: raw.ai_enabled === true,
    studentAiEnabled: raw.student_ai_enabled === true,
    runnerEnabled: raw.runner_enabled === true,
    runnerCpuSeconds: Number(raw.runner_cpu_seconds ?? 4),
    runnerMemoryMb: Number(raw.runner_memory_mb ?? 256),
    retentionDays: Number(raw.retention_days ?? 365),
    allowedLmsOrigins: raw.allowed_lms_origins ?? [],
    incidentBanner: String(raw.incident_banner ?? ''),
    services: raw.services ?? [],
  };
}

function mapAuthorshipAnalysis(raw: any): AuthorshipAnalysis {
  const result = raw.result && typeof raw.result === 'object' ? raw.result : undefined;
  return {
    id: String(raw.id), submissionId: String(raw.submission_id ?? raw.submission ?? ''),
    manifestHash: raw.manifest_hash || undefined, payloadHash: raw.payload_hash || undefined,
    exportSchemaVersion: raw.export_schema_version ? String(raw.export_schema_version) : undefined,
    state: String(raw.state ?? 'UNKNOWN').toUpperCase(), outcome: raw.outcome ? String(raw.outcome) : undefined,
    probability: raw.probability === null || raw.probability === undefined ? undefined : Number(raw.probability),
    startedAt: raw.started_at || undefined, completedAt: raw.completed_at || undefined,
    errorCode: raw.error_code || undefined, error: raw.error || undefined,
    result: result ? {
      manifestHash: result.manifest_hash || undefined,
      probability: result.probability === null || result.probability === undefined ? undefined : Number(result.probability),
      confidence: result.confidence === null || result.confidence === undefined ? undefined : Number(result.confidence),
      uncertainty: result.uncertainty === null || result.uncertainty === undefined ? undefined : Number(result.uncertainty),
      analyzer: result.analyzer ? String(result.analyzer) : undefined, model: result.model ? String(result.model) : undefined,
      calibration: result.calibration && typeof result.calibration === 'object' ? result.calibration : {},
      features: result.features && typeof result.features === 'object' ? result.features : {},
      warnings: unwrapList<any>(result.warnings ?? []).map(String), responseHash: result.response_hash || undefined,
      createdAt: result.created_at || undefined,
    } : undefined,
  };
}

function mapSimilarityMatch(raw: any): SimilarityMatch {
  return {
    id: String(raw.id),
    submissionA: String(raw.submission_a_id ?? raw.submission_a ?? ''),
    submissionB: String(raw.submission_b_id ?? raw.submission_b ?? ''),
    score: Number(raw.score ?? 0),
    fingerprintCountA: Number(raw.fingerprint_count_a ?? 0),
    fingerprintCountB: Number(raw.fingerprint_count_b ?? 0),
    sharedFingerprintCount: Number(raw.shared_fingerprint_count ?? 0),
    evidence: unwrapList<any>(raw.evidence ?? []).map((item) => ({
      fileA: String(item.file_a ?? ''), startLineA: Number(item.start_line_a ?? 1), endLineA: Number(item.end_line_a ?? item.start_line_a ?? 1),
      fileB: String(item.file_b ?? ''), startLineB: Number(item.start_line_b ?? 1), endLineB: Number(item.end_line_b ?? item.start_line_b ?? 1),
      tokenCount: Number(item.token_count ?? 1), excerptA: String(item.excerpt_a ?? ''), excerptB: String(item.excerpt_b ?? ''),
    })),
    case: raw.case && typeof raw.case === 'object' ? raw.case : undefined,
  };
}

function mapSimilarityAnalysis(raw: any): SimilarityAnalysis {
  return {
    id: String(raw.id), assessmentId: String(raw.assessment_id ?? raw.assessment ?? ''),
    taskVersionId: raw.task_version_id || raw.task_version ? String(raw.task_version_id ?? raw.task_version) : undefined,
    algorithmVersion: raw.algorithm_version ? String(raw.algorithm_version) : undefined,
    config: raw.config && typeof raw.config === 'object' ? raw.config : {}, state: String(raw.state ?? 'UNKNOWN').toUpperCase(),
    submissionCount: Number(raw.submission_count ?? 0), comparisonCount: Number(raw.comparison_count ?? 0), matchCount: Number(raw.match_count ?? 0),
    startedAt: raw.started_at || undefined, completedAt: raw.completed_at || undefined, error: raw.error || undefined,
    matches: unwrapList<any>(raw.matches ?? []).map(mapSimilarityMatch),
  };
}

function mapSimilarityComparison(raw: any): SimilarityComparison {
  const mapSide = (side: any) => ({
    submissionId: String(side?.submission_id ?? ''), studentName: String(side?.student_name ?? 'Студент'),
    studentGroup: String(side?.student_group ?? ''), submittedAt: side?.submitted_at ?? new Date().toISOString(),
    files: unwrapList<any>(side?.files ?? []).map(mapFile),
  });
  return {
    match: mapSimilarityMatch(raw.match ?? {}),
    assessmentId: String(raw.assessment_id ?? ''), assessmentTitle: String(raw.assessment_title ?? 'Работа'),
    left: mapSide(raw.left), right: mapSide(raw.right),
  };
}

export const api = {
  getConnections: () => withDemo(
    async () => unwrapList<any>(await request('/auth/connections')).map((item) => ({ id: String(item.id), name: String(item.name), kind: String(item.provider ?? item.kind ?? 'LMS'), enabled: item.enabled !== false, loginMode: String(item.login_mode ?? 'REDIRECT').toUpperCase() === 'CREDENTIALS' ? 'CREDENTIALS' as const : 'REDIRECT' as const })),
    () => cloneDemo(demoConnections),
  ),
  getSession: () => withDemo(async () => mapSession(await request('/auth/session')), () => cloneDemo(demoSession)),
  startLogin: async (connectionId: string, adminToken?: string, courseId?: string, teacherToken?: string) => request<{ redirect_url: string }>(`/auth/lms/${connectionId}/start`, { method: 'POST', body: JSON.stringify({ admin_token: adminToken || undefined, teacher_token: teacherToken || undefined, course_id: courseId || undefined }) }),
  moodleCredentialLogin: async (connectionId: string, username: string, password: string, adminToken?: string, teacherToken?: string) => mapSession(await request(`/auth/lms/${connectionId}/credentials`, { method: 'POST', body: JSON.stringify({ username, password, admin_token: adminToken || undefined, teacher_token: teacherToken || undefined }) })),
  devLogin: (role: Role, adminToken?: string) => withDemo(
    async () => mapSession(await request('/auth/dev-login', { method: 'POST', body: JSON.stringify({ role, admin_token: adminToken || undefined }) })),
    () => {
      demoSession = cloneDemo(role === 'TEACHER' ? teacherSession : studentSession);
      if (adminToken) {
        demoSession.capabilities.push('SYSTEM_SETTINGS');
        demoSession.adminElevationExpiresAt = new Date(Date.now() + 30 * 60_000).toISOString();
      }
      return cloneDemo(demoSession);
    },
  ),
  logout: () => withDemo(async () => { await request<void>('/auth/logout', { method: 'POST' }); csrfToken = undefined; }, () => { demoSession = null; csrfToken = undefined; }),
  elevate: (token: string) => withDemo(
    async () => mapSession(await request('/auth/admin-elevation', { method: 'POST', body: JSON.stringify({ admin_token: token }) })),
    () => {
      if (!demoSession) throw new ApiError(401, 'NOT_AUTHENTICATED', 'Сначала войдите через LMS');
      demoSession.capabilities = [...new Set([...demoSession.capabilities, 'SYSTEM_SETTINGS'])];
      demoSession.adminElevationExpiresAt = new Date(Date.now() + 30 * 60_000).toISOString();
      return cloneDemo(demoSession);
    },
  ),
  dropElevation: () => withDemo(() => request<void>('/auth/admin-elevation', { method: 'DELETE' }), () => {
    if (demoSession) { demoSession.capabilities = demoSession.capabilities.filter((value) => value !== 'SYSTEM_SETTINGS'); delete demoSession.adminElevationExpiresAt; }
  }),
  getCourses: (session?: Session | null) => withDemo(
    async () => unwrapList<any>(await request('/courses')).map((item) => mapCourse(item, session)),
    () => session?.memberships.some((item) => item.role === 'TEACHER') ? [cloneDemo(teacherCourse), cloneDemo(demoCourses[1])] : [cloneDemo(studentCourse)],
  ),
  getCourseSyncStatus: (courseId: string) => withDemo(
    async () => mapCourseSyncStatus(await request(`/courses/${courseId}/sync-status`)),
    () => ({ courseId, syncStatus: 'SYNCED' as const }),
  ),
  getCourseCatalog: () => withDemo(
    async () => unwrapList<any>(await request('/system/course-catalog')).map(mapCourseCatalogEntry),
    () => cloneDemo(courseCatalogState),
  ),
  deleteCourseCatalogEntry: (courseId: string) => withDemo(
    () => request<void>(`/system/course-catalog/${courseId}`, { method: 'DELETE' }),
    () => { courseCatalogState = courseCatalogState.filter((item) => item.id !== courseId); },
  ),
  getTeacherTokens: () => withDemo(
    async () => unwrapList<any>(await request('/system/teacher-tokens')).map(mapTeacherAccessToken),
    () => cloneDemo(teacherTokenState),
  ),
  createTeacherToken: (label: string) => withDemo(
    async () => {
      const raw = await request<any>('/system/teacher-tokens', { method: 'POST', body: JSON.stringify({ label }) });
      return { ...mapTeacherAccessToken(raw), token: String(raw.token) } as TeacherAccessTokenIssued;
    },
    () => {
      const issued: TeacherAccessTokenIssued = {
        id: createUuid(), label, publicId: createUuid().slice(0, 12),
        hashFingerprint: createUuid().replaceAll('-', '').slice(0, 16), canReveal: true, useCount: 0,
        createdAt: new Date().toISOString(), token: createUuid().replaceAll('-', '').slice(0, 8),
      };
      teacherTokenState = [issued, ...teacherTokenState];
      return cloneDemo(issued);
    },
  ),
  revealTeacherToken: (tokenId: string) => withDemo(
    async () => {
      const raw = await request<any>(`/system/teacher-tokens/${tokenId}/secret`);
      return { token: String(raw.token ?? '') };
    },
    () => {
      const stored = teacherTokenState.find((item) => item.id === tokenId);
      if (!stored?.canReveal || !('token' in stored) || typeof stored.token !== 'string') {
        throw new ApiError(409, 'TEACHER_TOKEN_SECRET_UNAVAILABLE', 'Открытое значение этого токена недоступно');
      }
      return { token: stored.token };
    },
  ),
  updateTeacherToken: (tokenId: string, token: string) => withDemo(
    async () => mapTeacherAccessToken(await request<any>(`/system/teacher-tokens/${tokenId}`, {
      method: 'PATCH', body: JSON.stringify({ token }),
    })),
    () => {
      const index = teacherTokenState.findIndex((item) => item.id === tokenId);
      if (index === -1) throw new ApiError(404, 'TEACHER_TOKEN_NOT_FOUND', 'Токен преподавателя не найден');
      const updated: TeacherAccessTokenIssued = {
        ...teacherTokenState[index], token, canReveal: true,
        hashFingerprint: token.slice(0, 8).padEnd(16, '0'),
      };
      teacherTokenState = teacherTokenState.map((item, itemIndex) => itemIndex === index ? updated : item);
      return cloneDemo(updated);
    },
  ),
  deleteTeacherToken: (tokenId: string) => withDemo(
    () => request<void>(`/system/teacher-tokens/${tokenId}`, { method: 'DELETE' }),
    () => { teacherTokenState = teacherTokenState.filter((item) => item.id !== tokenId); },
  ),
  getAssessments: (courseId: string) => withDemo(
    async () => unwrapList<any>(await request(`/courses/${courseId}/assessments`)).map((item) => mapAssessment(item, courseId)),
    () => cloneDemo(assessmentState.map((item) => ({ ...item, courseId }))),
  ),
  getLmsActivities: (courseId: string) => withDemo(
    async () => unwrapList<LmsActivity>(await request(`/courses/${courseId}/lms-activities`)),
    () => [],
  ),
  getCourseGroups: (courseId: string) => withDemo(
    async () => unwrapList<any>(await request(`/courses/${courseId}/groups`)).map((raw): CourseGroup => ({
      id: String(raw.id), courseId: String(raw.course_id ?? courseId), externalId: String(raw.external_id ?? ''),
      name: String(raw.name ?? 'Группа'), kind: String(raw.kind ?? 'LMS'), active: raw.active !== false,
    })),
    () => [],
  ),
  getAssessment: (assessmentId: string) => withDemo(
    async () => { const raw = await request<any>(`/assessments/${assessmentId}`); return mapAssessment(raw, String(raw.course ?? '')); },
    () => cloneDemo(assessmentState.find((item) => item.id === assessmentId) ?? assessmentState[0]),
  ),
  createAssessment: (courseId: string, payload: {
    type: Assessment['kind']; title: string; instructions: string; opens_at?: string; closes_at?: string;
    duration_seconds?: number; max_score: number; paste_policy: 'INTERNAL_ONLY' | 'ALLOW'; student_ai_enabled: boolean; multi_file: boolean;
    review_required: boolean; decision_support_enabled: boolean;
    policy?: Record<string, unknown>;
  }) => withDemo(
    async () => mapAssessment(await request(`/courses/${courseId}/assessments`, { method: 'POST', body: JSON.stringify(payload) }), courseId),
    () => {
      const item = mapAssessment({ id: createUuid(), course: courseId, ...payload, status: 'DRAFT' }, courseId);
      assessmentState.push(item); return cloneDemo(item);
    },
  ),
  updateAssessment: (assessmentId: string, payload: {
    title: string; instructions: string; opens_at: string | null; closes_at: string | null; max_score: number;
    student_ai_enabled: boolean; review_required: boolean; decision_support_enabled: boolean;
    policy?: Record<string, unknown>;
  }) => withDemo(
    async () => mapAssessment(await request(`/assessments/${assessmentId}`, { method: 'PATCH', body: JSON.stringify(payload) }), ''),
    () => {
      assessmentState = assessmentState.map((item) => item.id === assessmentId ? {
        ...item, title: payload.title, summary: payload.instructions, startsAt: payload.opens_at ?? undefined,
        deadlineAt: payload.closes_at ?? undefined, maxScore: payload.max_score, aiEnabled: payload.student_ai_enabled,
        reviewRequired: payload.review_required, decisionSupportEnabled: payload.decision_support_enabled, policy: payload.policy ?? item.policy,
      } : item);
      return cloneDemo(assessmentState.find((item) => item.id === assessmentId)!);
    },
  ),
  deleteAssessment: (assessmentId: string) => withDemo(
    () => request<void>(`/assessments/${assessmentId}`, { method: 'DELETE' }),
    () => { assessmentState = assessmentState.filter((item) => item.id !== assessmentId); },
  ),
  addAssessmentItem: (assessmentId: string, taskVersionId: string, points: number) => withDemo(
    () => request(`/assessments/${assessmentId}/items`, { method: 'POST', body: JSON.stringify({ task_version: taskVersionId, position: 0, points }) }),
    () => ({ id: createUuid(), task_version: taskVersionId, position: 0, points }),
  ),
  addAvailabilityRule: (assessmentId: string, payload: { target_type: 'COURSE' | 'GROUP'; target_external_id: string }) => withDemo(
    async () => mapAvailabilityRule(await request(`/assessments/${assessmentId}/availability-rules`, { method: 'POST', body: JSON.stringify({ ...payload, allowed: true }) })),
    () => {
      const rule = mapAvailabilityRule({ id: createUuid(), assessment_id: assessmentId, ...payload, allowed: true });
      assessmentState = assessmentState.map((item) => item.id === assessmentId ? { ...item, availabilityRules: [...(item.availabilityRules ?? []), rule] } : item);
      return rule;
    },
  ),
  deleteAvailabilityRule: (assessmentId: string, ruleId: string) => withDemo(
    () => request<void>(`/assessments/${assessmentId}/availability-rules/${ruleId}`, { method: 'DELETE' }),
    () => { assessmentState = assessmentState.map((item) => item.id === assessmentId ? { ...item, availabilityRules: item.availabilityRules?.filter((rule) => rule.id !== ruleId) } : item); },
  ),
  getAssessmentPublicationTargets: (assessmentId: string) => withDemo(
    async () => mapAssessmentPublicationTargets(await request(`/assessments/${assessmentId}/publication-targets`)),
    () => ({ groups: [], principals: [], overridesConfirmed: true }),
  ),
  publishAssessment: (assessmentId: string, groupIds?: string[]) => withDemo(
    async () => mapAssessment(await request(`/assessments/${assessmentId}/publish`, {
      method: 'POST',
      body: JSON.stringify(groupIds === undefined ? {} : { group_ids: groupIds }),
    }), ''),
    () => {
      assessmentState = assessmentState.map((item) => item.id === assessmentId ? { ...item, publicationStatus: 'PUBLISHED', status: 'AVAILABLE' } : item);
      return cloneDemo(assessmentState.find((item) => item.id === assessmentId)!);
    },
  ),
  validateAssessment: (assessmentId: string) => withDemo(
    () => request<{ valid: boolean; errors: Array<{ field?: string; code?: string; message: string }> }>(`/assessments/${assessmentId}/validate`, { method: 'POST', body: '{}' }),
    () => ({ valid: true, errors: [] }),
  ),
  resolveCourseId: (assessmentId: string) => withDemo(async () => {
    const assessment = await request<any>(`/assessments/${assessmentId}`);
    const courseId = assessment.course_id ?? assessment.course?.id ?? assessment.course;
    if (courseId) return String(courseId);
    throw new ApiError(502, 'COURSE_PROJECTION_MISSING', 'Сервер не вернул курс работы');
  }, () => 'cpp-2026'),
  createCourseImport: (url: string) => withDemo(
    async () => mapCourseImport(await request('/course-imports', { method: 'POST', body: JSON.stringify({ url }) })),
    () => {
      const id = createUuid();
      const externalCourseId = new URL(url).searchParams.get('id') ?? 'demo';
      const job: CourseImportJob = {
        id, state: 'DISCOVERED', externalCourseId,
        preview: { title: `Курс Moodle ${externalCourseId}`, sections: [], groups: [] },
        capabilityReport: { mode: 'demo' },
      };
      courseImportState.set(id, { job, url });
      return cloneDemo(job);
    },
  ),
  confirmCourseImport: (jobId: string) => withDemo(
    async () => mapCourseImport(await request(`/course-imports/${jobId}/confirm`, { method: 'POST', body: '{}' })),
    () => {
      const pending = courseImportState.get(jobId);
      const externalCourseId = pending?.job.externalCourseId ?? 'demo';
      const existing = courseCatalogState.find((item) => item.externalId === externalCourseId);
      const courseId = existing?.id ?? createUuid();
      if (!existing) {
        courseCatalogState.push({
          id: courseId,
          connectionId: demoConnections[0].id,
          connectionName: demoConnections[0].name,
          externalId: externalCourseId,
          title: String(pending?.job.preview.title ?? `Курс Moodle ${externalCourseId}`),
          shortName: '',
          externalUrl: pending?.url ?? `https://edu.mmcs.sfedu.ru/course/view.php?id=${encodeURIComponent(externalCourseId)}`,
          syncStatus: 'STALE',
          addedAt: new Date().toISOString(),
        });
      }
      const confirmed: CourseImportJob = {
        ...(pending?.job ?? { id: jobId, externalCourseId, preview: {}, capabilityReport: {} }),
        state: 'CONFIRMED', confirmedCourse: courseId,
      };
      courseImportState.delete(jobId);
      return cloneDemo(confirmed);
    },
  ),
  getTaskBank: () => withDemo(
    async () => unwrapList<any>(await request('/task-bank/items')).map(mapTaskItem),
    () => cloneDemo(taskBankState),
  ),
  getTaskVersions: () => withDemo(
    async () => unwrapList<any>(await request('/task-versions')).map((item) => mapTaskVersion(item)),
    () => taskBankState.flatMap((item) => item.latestVersion ? [cloneDemo(item.latestVersion)] : []),
  ),
  createTaskItem: (payload: { course: string; slug: string; category: string; tags: string[] }) => withDemo(
    async () => mapTaskItem(await request('/task-bank/items', { method: 'POST', body: JSON.stringify({ scope: 'COURSE', ...payload }) })),
    () => { const item: TaskBankItem = { id: createUuid(), scope: 'COURSE', ...payload }; taskBankState.push(item); return cloneDemo(item); },
  ),
  createTaskVersion: (itemId: string, payload: { title: string; statement: string; language: 'C' | 'CPP'; multiFile: boolean; maxScore: number; hiddenTestManifest?: HiddenTestManifestV1 }) => withDemo(async () => mapTaskVersion(await request<any>(`/task-bank/items/${itemId}/versions`, {
    method: 'POST', body: JSON.stringify({ title: payload.title, statement: payload.statement, language: payload.language, language_standard: payload.language === 'C' ? 'C17' : 'C++20', multi_file: payload.multiFile, starter_files: [{ path: payload.language === 'C' ? 'main.c' : 'main.cpp', content: '' }], build_profile: payload.language === 'C' ? `c-gcc-c17-${payload.multiFile ? 'multi' : 'single'}` : `cpp-gcc-c++20-${payload.multiFile ? 'multi' : 'single'}`, public_examples: [], hidden_test_manifest: payload.hiddenTestManifest ?? {}, max_score: payload.maxScore, difficulty: '1', ai_policy: {} }),
  }), itemId), () => {
    const item = taskBankState.find((candidate) => candidate.id === itemId);
    const version = { id: createUuid(), number: (item?.latestVersion?.number ?? 0) + 1, title: payload.title, statement: payload.statement, language: payload.language, languageStandard: payload.language === 'C' ? 'C17' : 'C++20', multiFile: payload.multiFile, maxScore: payload.maxScore, status: 'DRAFT', hiddenTestManifest: payload.hiddenTestManifest };
    if (item) item.latestVersion = { ...version, itemId }; return { ...version, itemId };
  }),
  updateTaskVersion: (versionId: string, itemId: string, payload: { title: string; statement: string; language: 'C' | 'CPP'; multiFile: boolean; maxScore: number; hiddenTestManifest?: HiddenTestManifestV1 }) => withDemo(async () => mapTaskVersion(await request<any>(`/task-versions/${versionId}`, {
    method: 'PUT', body: JSON.stringify({ title: payload.title, statement: payload.statement, language: payload.language, language_standard: payload.language === 'C' ? 'C17' : 'C++20', multi_file: payload.multiFile, starter_files: [{ path: payload.language === 'C' ? 'main.c' : 'main.cpp', content: '' }], build_profile: payload.language === 'C' ? `c-gcc-c17-${payload.multiFile ? 'multi' : 'single'}` : `cpp-gcc-c++20-${payload.multiFile ? 'multi' : 'single'}`, public_examples: [], hidden_test_manifest: payload.hiddenTestManifest ?? {}, max_score: payload.maxScore, difficulty: '1', ai_policy: {} }),
  }), itemId), () => {
    const item = taskBankState.find((candidate) => candidate.id === itemId);
    const current = item?.latestVersion;
    if (!current || current.id !== versionId || current.status !== 'DRAFT') throw new ApiError(409, 'TASK_VERSION_IMMUTABLE', 'Изменять можно только черновик версии');
    const version = { ...current, title: payload.title, statement: payload.statement, language: payload.language, languageStandard: payload.language === 'C' ? 'C17' : 'C++20', multiFile: payload.multiFile, maxScore: payload.maxScore, hiddenTestManifest: payload.hiddenTestManifest, aiPolicy: {} };
    item.latestVersion = version;
    return cloneDemo(version);
  }),
  validateTaskVersion: (versionId: string) => withDemo(
    () => request<{ valid: boolean; errors: Array<{ field?: string; code?: string; message: string }> }>(`/task-versions/${versionId}/validate`, { method: 'POST', body: '{}' }),
    () => ({ valid: true, errors: [] }),
  ),
  publishTaskVersion: (versionId: string) => withDemo(
    async () => mapTaskVersion(await request(`/task-versions/${versionId}/publish`, { method: 'POST', body: '{}' })),
    () => {
      const version = taskBankState.map((item) => item.latestVersion).find((item) => item?.id === versionId);
      if (!version) throw new ApiError(404, 'TASK_VERSION_NOT_FOUND', 'Версия задания не найдена');
      version.status = 'PUBLISHED';
      return cloneDemo(version);
    },
  ),
  syncCourse: (courseId: string) => withDemo(
    async () => mapCourse(await request(`/courses/${courseId}/sync`, { method: 'POST', body: '{}' })),
    () => cloneDemo(teacherCourse),
  ),
  getAttempt: (attemptId: string) => withDemo(async () => {
    const raw = await request<any>(`/attempts/${attemptId}`);
    const workspace = await request<any>(`/attempts/${attemptId}/workspace`);
    return mapAttempt(raw, workspace);
  }, () => cloneDemo(attemptState)),
  getAttemptStatus: (attemptId: string) => withDemo(
    async () => mapAttemptStatus(await request(`/attempts/${attemptId}`)),
    () => mapAttemptStatus(attemptState),
  ),
  startAttempt: (assessmentId: string) => withDemo(
    async () => { const raw = await request<any>(`/assessments/${assessmentId}/attempts`, { method: 'POST', headers: { 'Idempotency-Key': createUuid() }, body: '{}' }); return mapAttempt(raw, raw.workspace); },
    () => cloneDemo(attemptState),
  ),
  getHistory: (attemptId: string) => withDemo(
    async () => mapHistory(unwrapList<any>(await request(`/attempts/${attemptId}/history`))).sort((left, right) => new Date(right.at).getTime() - new Date(left.at).getTime()),
    () => cloneDemo(demoHistory),
  ),
  saveFile: (attemptId: string, file: WorkspaceFile, revision: number, source: 'typing' | 'internal_paste' = 'typing', receiptId?: string) => withDemo(async () => {
    const raw = await request<any>(`/attempts/${attemptId}/workspace/files/${file.id}`, { method: 'PATCH', headers: { 'If-Match': String(revision) }, body: JSON.stringify({ content: file.content, source: source.toUpperCase(), receipt_id: receiptId }) });
    return { revision: Number(raw.workspace_revision ?? raw.revision ?? revision + 1) };
  }, () => {
    attemptState.files = attemptState.files.map((item) => item.id === file.id ? cloneDemo(file) : item);
    attemptState.revision += 1; attemptState.acknowledgedRevision = attemptState.revision;
    return { revision: attemptState.revision };
  }),
  createFile: (attemptId: string, path: string, revision: number) => withDemo(async () => {
    const raw = await request<any>(`/attempts/${attemptId}/workspace/files/new`, { method: 'POST', headers: { 'If-Match': String(revision) }, body: JSON.stringify({ path }) });
    return { file: mapFile(raw.file ?? raw), revision: Number(raw.revision ?? raw.created_revision ?? raw.file?.created_revision ?? revision + 1) };
  }, () => {
    const file = { id: createUuid(), path, content: '', language: languageForPath(path) };
    attemptState.files.push(file); attemptState.revision += 1; attemptState.acknowledgedRevision = attemptState.revision;
    return { file: cloneDemo(file), revision: attemptState.revision };
  }),
  deleteFile: (attemptId: string, fileId: string, revision: number) => withDemo(async () => {
    const raw = await request<any>(`/attempts/${attemptId}/workspace/files/${fileId}`, { method: 'DELETE', headers: { 'If-Match': String(revision), 'Idempotency-Key': createUuid() }, body: '{}' });
    return { fileId: String(raw.file_id ?? fileId), revision: Number(raw.revision ?? revision + 1) };
  }, () => {
    if (attemptState.files.length <= 1) throw new ApiError(409, 'LAST_FILE_REQUIRED', 'Нельзя удалить последний файл рабочей области');
    attemptState.files = attemptState.files.filter((item) => item.id !== fileId);
    attemptState.revision += 1; attemptState.acknowledgedRevision = attemptState.revision;
    return { fileId, revision: attemptState.revision };
  }),
  createClipboardReceipt: (attemptId: string, fileId: string, text: string, revision: number) => withDemo(
    () => request<{ id: string; expires_at: string }>(`/attempts/${attemptId}/clipboard-receipts`, { method: 'POST', body: JSON.stringify({ file_id: fileId, text, revision }) }),
    () => ({ id: createUuid(), expires_at: new Date(Date.now() + 5 * 60_000).toISOString() }),
  ),
  createRun: (attemptId: string, revision: number, stdin = '') => withDemo(
    async () => mapRun(await request(`/attempts/${attemptId}/runs`, { method: 'POST', body: JSON.stringify({ revision, stdin }) })),
    () => ({ ...cloneDemo(failedRun), id: createUuid(), revision, createdAt: new Date().toISOString() }),
  ),
  getRun: (runId: string) => withDemo(async () => mapRun(await request(`/runs/${runId}`)), () => cloneDemo(failedRun)),
  startInteractiveAttempt: (attemptId: string, revision: number) => withDemo(
    async () => mapInteractiveRun(await request(`/attempts/${attemptId}/interactive-sessions`, { method: 'POST', body: JSON.stringify({ revision }) })),
    (): InteractiveRun => ({ sessionId: createUuid().replaceAll('-', ''), status: 'RUNNING', terminal: false, durationMs: 1, stdout: '', stderr: '', outputTruncated: false, diagnostics: [] }),
  ),
  getInteractiveAttempt: (attemptId: string, sessionId: string) => withDemo(
    async () => mapInteractiveRun(await request(`/attempts/${attemptId}/interactive-sessions/${sessionId}/state`, { method: 'POST', body: '{}' })),
    (): InteractiveRun => ({ sessionId, status: 'RUNNING', terminal: false, durationMs: 1, stdout: '', stderr: '', outputTruncated: false, diagnostics: [] }),
  ),
  sendInteractiveAttemptInput: (attemptId: string, sessionId: string, text: string) => withDemo(
    async () => mapInteractiveRun(await request(`/attempts/${attemptId}/interactive-sessions/${sessionId}/input`, { method: 'POST', body: JSON.stringify({ text }) })),
    (): InteractiveRun => ({ sessionId, status: 'RUNNING', terminal: false, durationMs: 1, stdout: '', stderr: '', outputTruncated: false, diagnostics: [] }),
  ),
  eofInteractiveAttempt: (attemptId: string, sessionId: string) => withDemo(
    async () => mapInteractiveRun(await request(`/attempts/${attemptId}/interactive-sessions/${sessionId}/eof`, { method: 'POST', body: '{}' })),
    (): InteractiveRun => ({ sessionId, status: 'SUCCESS', terminal: true, exitCode: 0, durationMs: 1, stdout: '', stderr: '', outputTruncated: false, diagnostics: [] }),
  ),
  stopInteractiveAttempt: (attemptId: string, sessionId: string) => withDemo(
    async () => mapInteractiveRun(await request(`/attempts/${attemptId}/interactive-sessions/${sessionId}/stop`, { method: 'POST', body: '{}' })),
    (): InteractiveRun => ({ sessionId, status: 'STOPPED', terminal: true, durationMs: 1, stdout: '', stderr: '', outputTruncated: false, diagnostics: [] }),
  ),
  submitAttempt: (attemptId: string, revision: number) => withDemo(
    () => request(`/attempts/${attemptId}/submit`, { method: 'POST', headers: { 'Idempotency-Key': createUuid() }, body: JSON.stringify({ revision }) }),
    () => { attemptState.status = 'SUBMITTED'; return { receipt_id: 'DEMO-8F37', submitted_at: new Date().toISOString(), revision }; },
  ),
  retryAttemptSubmission: (attemptId: string) => withDemo(
    () => request(`/attempts/${attemptId}/submit/retry`, { method: 'POST', headers: { 'Idempotency-Key': createUuid() }, body: '{}' }),
    () => ({ receipt_id: 'DEMO-8F37', submitted_at: new Date().toISOString(), revision: attemptState.revision }),
  ),
  getSubmissions: (assessmentId?: string) => withDemo(
    async () => {
      const pageSize = 500;
      const rows = new Map<string, Submission>();
      let offset = 0;
      while (true) {
        const page = unwrapList<any>(await request(`/assessments/${encodeURIComponent(assessmentId ?? 'all')}/submissions?limit=${pageSize}&offset=${offset}`));
        page.map(mapSubmission).forEach((submission) => rows.set(submission.id, submission));
        if (page.length < pageSize) break;
        offset += page.length;
      }
      return [...rows.values()];
    },
    () => cloneDemo(assessmentId && assessmentId !== 'all' ? submissionsState.filter((item) => item.assessmentId === assessmentId) : submissionsState),
  ),
  getMoodleHistoryImportEvents: () => withDemo(
    async () => unwrapList<any>(await request('/integrations/lms/outbox?limit=500'))
      .filter((item) => String(item.event_type ?? '') === 'moodle.history.import')
      .map(mapMoodleHistoryImportEvent),
    () => [] as MoodleHistoryImportEvent[],
  ),
  retryMoodleHistoryImport: (eventId: string) => withDemo(
    async () => mapMoodleHistoryImportEvent(await request(
      `/integrations/lms/outbox/${encodeURIComponent(eventId)}/retry`,
      {
        method: 'POST',
        body: JSON.stringify({ reason: 'Повторный запуск со страницы работ студентов' }),
      },
    )),
    () => ({
      id: eventId,
      aggregateId: eventId,
      state: 'RETRY' as const,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      receipt: {},
    }),
  ),
  getSubmission: (id: string) => withDemo(async () => mapSubmission(await request(`/submissions/${id}`)), () => cloneDemo(submissionsState.find((item) => item.id === id) ?? submissionsState[0])),
  getEvidenceRuns: (submissionId: string) => withDemo(
    async () => unwrapList<any>(await request(`/submissions/${submissionId}/evidence-runs?limit=50`)).map(mapEvidenceReport),
    () => cloneDemo(evidenceState.filter((report) => report.submissionId === submissionId)),
  ),
  getEvidenceRun: (reportId: string) => withDemo(
    async () => mapEvidenceReport(await request(`/evidence-runs/${reportId}`)),
    () => cloneDemo(evidenceState.find((report) => report.id === reportId) ?? evidenceState[0]),
  ),
  createEvidenceRun: (submissionId: string) => withDemo(
    async () => mapEvidenceReport(await request(`/submissions/${submissionId}/evidence-runs`, {
      method: 'POST', headers: { 'Idempotency-Key': createUuid() }, body: '{}',
    })),
    () => {
      const now = new Date().toISOString();
      const report: EvidenceReport = { id: createUuid(), submissionId, snapshotId: 'demo-snapshot', taskVersionId: 'demo-task', requestedById: 'teacher-demo', hiddenTestManifestHash: 'demo', taskContentHash: 'demo', snapshotManifestHash: 'demo', status: 'COMPLETED', passedCases: 1, totalCases: 1, outcomes: [{ caseIndex: 0, name: 'Демонстрационный кейс', runId: createUuid(), status: 'PASSED', comparison: 'EXACT', exitCode: 0, actualStdoutSha256: 'demo', expectedStdoutSha256: 'demo', actualStdoutPreview: 'OK\n', stderrPreview: '', filesystemIsolated: true, networkEnabled: false }], findings: [], completedAt: now, createdAt: now, updatedAt: now };
      evidenceState = [report, ...evidenceState]; return cloneDemo(report);
    },
  ),
  getAuthorshipAnalyses: (submissionId: string) => withDemo(
    async () => unwrapList<any>(await request(`/submissions/${submissionId}/authorship-analyses`)).map(mapAuthorshipAnalysis),
    () => [],
  ),
  createAuthorshipAnalysis: (submissionId: string) => withDemo(
    async () => mapAuthorshipAnalysis(await request(`/submissions/${submissionId}/authorship-analyses`, { method: 'POST', body: '{}' })),
    () => mapAuthorshipAnalysis({ id: createUuid(), submission: submissionId, state: 'FAILED', error_code: 'DEMO_ANALYZER_UNAVAILABLE', error: 'В demo-режиме внешний анализатор не настроен.' }),
  ),
  getSimilarityAnalyses: (assessmentId: string) => withDemo(
    async () => unwrapList<any>(await request(`/assessments/${assessmentId}/similarity-analyses`)).map(mapSimilarityAnalysis),
    () => [],
  ),
  getSimilarityComparison: (matchId: string) => withDemo(
    async () => mapSimilarityComparison(await request(`/similarity-matches/${matchId}/comparison`)),
    () => {
      const left = submissionsState[0]; const right = submissionsState[1] ?? submissionsState[0];
      return mapSimilarityComparison({
        match: { id: matchId, submission_a_id: left.id, submission_b_id: right.id, score: 0, evidence: [] },
        assessment_id: left.assessmentId, assessment_title: left.assessmentTitle,
        left: { submission_id: left.id, student_name: left.studentName, student_group: left.studentGroup, submitted_at: left.submittedAt, files: left.files },
        right: { submission_id: right.id, student_name: right.studentName, student_group: right.studentGroup, submitted_at: right.submittedAt, files: right.files },
      });
    },
  ),
  createSimilarityAnalysis: (assessmentId: string, taskVersionId?: string) => withDemo(
    async () => mapSimilarityAnalysis(await request(`/assessments/${assessmentId}/similarity-analyses`, { method: 'POST', body: JSON.stringify({ task_version_id: taskVersionId ?? null }) })),
    () => mapSimilarityAnalysis({ id: createUuid(), assessment: assessmentId, state: 'FAILED', error: 'В demo-режиме анализ сходства не настроен.', matches: [] }),
  ),
  claimSubmission: (id: string) => withDemo(async () => {
    const raw = await request<any>(`/submissions/${id}/claims`, { method: 'POST', body: '{}' });
    return { id: String(raw.id), ownerId: String(raw.owner?.id ?? raw.owner_id), ownerName: String(raw.owner?.display_name ?? raw.owner_name), expiresAt: raw.lease_expires_at ?? raw.expires_at, mine: true };
  }, () => {
    const claim = { id: createUuid(), ownerId: 'teacher-demo', ownerName: teacherSession.displayName, expiresAt: inFuture(120), mine: true };
    submissionsState = submissionsState.map((item) => item.id === id ? { ...item, status: 'CLAIMED', claim } : item);
    return claim;
  }),
  releaseClaim: (claimId: string) => withDemo(() => request<void>(`/review-claims/${claimId}`, { method: 'DELETE' }), () => {
    submissionsState = submissionsState.map((item) => item.claim?.id === claimId ? { ...item, status: item.latestDecision ? 'GRADED' : 'UNGRADED', claim: undefined } : item);
  }),
  heartbeatClaim: (claimId: string) => withDemo(async () => {
    const raw = await request<any>(`/review-claims/${claimId}/heartbeat`, { method: 'POST', body: '{}' });
    return { id: String(raw.id), ownerId: String(raw.owner?.id ?? raw.owner_id), ownerName: String(raw.owner?.display_name ?? raw.owner_name), expiresAt: raw.lease_expires_at ?? raw.expires_at, mine: true };
  }, () => ({ id: claimId, ownerId: 'teacher-demo', ownerName: teacherSession.displayName, expiresAt: inFuture(300), mine: true })),
  saveReview: (submissionId: string, grade: number, comment: string) => withDemo(
    () => request(`/submissions/${submissionId}/review-draft`, { method: 'PUT', body: JSON.stringify({ grade, comment }) }), () => ({ grade, comment, revision: 2 }),
  ),
  getReviewDraft: (submissionId: string) => withDemo(async () => {
    const raw = await request<any | null>(`/submissions/${submissionId}/review-draft`);
    return raw ? { grade: Number(raw.grade), comment: String(raw.comment ?? '') } : null;
  }, () => null),
  finalizeReview: (submissionId: string, grade: number, comment: string) => withDemo(
    async () => { const raw = await request<any>(`/submissions/${submissionId}/review-decisions`, { method: 'POST', headers: { 'Idempotency-Key': createUuid() }, body: JSON.stringify({ grade, comment }) }); return { id: String(raw.id), grade: Number(raw.grade ?? grade), comment: String(raw.comment ?? comment), lmsExportState: raw.lms_export_state ? String(raw.lms_export_state) : undefined }; },
    () => {
      const decision = { id: createUuid(), reviewerName: teacherSession.displayName, grade, comment, revision: 1, reviewedAt: new Date().toISOString(), lmsExportState: 'PENDING' };
      submissionsState = submissionsState.map((item) => item.id === submissionId ? { ...item, status: 'GRADED', score: grade, claim: undefined, latestDecision: decision, decisionHistory: [decision, ...item.decisionHistory] } : item);
      return { id: decision.id, grade, comment, lmsExportState: decision.lmsExportState };
    },
  ),
  createExperiment: (submissionId: string) => withDemo(async () => {
    const raw = await request<any>(`/submissions/${submissionId}/teacher-experiments`, { method: 'POST', body: '{}' });
    return mapExperiment(raw, submissionId);
  }, () => { experimentState = { ...cloneDemo(demoExperiment), submissionId, id: createUuid() }; return cloneDemo(experimentState); }),
  saveExperimentFile: (experimentId: string, file: WorkspaceFile, revision: number) => withDemo(async () => {
    const raw = await request<any>(`/teacher-experiments/${experimentId}/files/${file.id}`, { method: 'PATCH', headers: { 'If-Match': String(revision) }, body: JSON.stringify({ content: file.content }) });
    return Number(raw.revision ?? revision + 1);
  }, () => {
    experimentState.files = experimentState.files.map((item) => item.id === file.id ? cloneDemo(file) : item);
    experimentState.changed = true; experimentState.revision += 1; return experimentState.revision;
  }),
  runExperiment: (experimentId: string, revision: number, stdin = '') => withDemo(
    async () => mapRun(await request(`/teacher-experiments/${experimentId}/runs`, { method: 'POST', body: JSON.stringify({ revision, stdin }) })),
    () => ({ ...cloneDemo(failedRun), id: createUuid(), revision, createdAt: new Date().toISOString() }),
  ),
  startInteractiveExperiment: (experimentId: string, revision: number) => withDemo(
    async () => mapInteractiveRun(await request(`/teacher-experiments/${experimentId}/interactive-sessions`, { method: 'POST', body: JSON.stringify({ revision }) })),
    (): InteractiveRun => ({ sessionId: createUuid().replaceAll('-', ''), status: 'SUCCESS', terminal: true, exitCode: 0, durationMs: 1, stdout: 'Демонстрационный интерактивный запуск завершён.\n', stderr: '', outputTruncated: false, diagnostics: [] }),
  ),
  getInteractiveExperiment: (experimentId: string, sessionId: string) => withDemo(
    async () => mapInteractiveRun(await request(`/teacher-experiments/${experimentId}/interactive-sessions/${sessionId}/state`, { method: 'POST', body: '{}' })),
    (): InteractiveRun => ({ sessionId, status: 'SUCCESS', terminal: true, exitCode: 0, durationMs: 1, stdout: 'Демонстрационный интерактивный запуск завершён.\n', stderr: '', outputTruncated: false, diagnostics: [] }),
  ),
  sendInteractiveInput: (experimentId: string, sessionId: string, text: string) => withDemo(
    async () => mapInteractiveRun(await request(`/teacher-experiments/${experimentId}/interactive-sessions/${sessionId}/input`, { method: 'POST', body: JSON.stringify({ text }) })),
    (): InteractiveRun => ({ sessionId, status: 'SUCCESS', terminal: true, exitCode: 0, durationMs: 1, stdout: `${text}\n`, stderr: '', outputTruncated: false, diagnostics: [] }),
  ),
  eofInteractiveExperiment: (experimentId: string, sessionId: string) => withDemo(
    async () => mapInteractiveRun(await request(`/teacher-experiments/${experimentId}/interactive-sessions/${sessionId}/eof`, { method: 'POST', body: '{}' })),
    (): InteractiveRun => ({ sessionId, status: 'SUCCESS', terminal: true, exitCode: 0, durationMs: 1, stdout: '', stderr: '', outputTruncated: false, diagnostics: [] }),
  ),
  stopInteractiveExperiment: (experimentId: string, sessionId: string) => withDemo(
    async () => mapInteractiveRun(await request(`/teacher-experiments/${experimentId}/interactive-sessions/${sessionId}/stop`, { method: 'POST', body: '{}' })),
    (): InteractiveRun => ({ sessionId, status: 'STOPPED', terminal: true, durationMs: 1, stdout: '', stderr: '', outputTruncated: false, diagnostics: [] }),
  ),
  resetExperiment: (experimentId: string, revision: number) => withDemo(
    async () => mapExperiment(await request(`/teacher-experiments/${experimentId}/reset`, { method: 'POST', headers: { 'If-Match': String(revision) }, body: '{}' })),
    () => { experimentState = { ...cloneDemo(demoExperiment), id: experimentId, submissionId: experimentState.submissionId, revision: revision + 1, changed: false }; return cloneDemo(experimentState); },
  ),
  deleteExperiment: (experimentId: string) => withDemo(() => request<void>(`/teacher-experiments/${experimentId}`, { method: 'DELETE' }), () => undefined),
  createStudentAiThread: (attemptId: string, courseId: string, revision: number) => withDemo(
    () => request<{ id: string }>('/ai/student-threads', { method: 'POST', body: JSON.stringify({ attempt: attemptId, course: courseId, revision }) }),
    () => ({ id: `student-ai-${attemptId}` }),
  ),
  createTeacherAiThread: (submissionId: string, courseId: string) => withDemo(
    () => request<{ id: string }>('/ai/teacher-threads', { method: 'POST', body: JSON.stringify({ submission: submissionId, course: courseId }) }),
    () => ({ id: `teacher-ai-${submissionId}` }),
  ),
  getTeacherAiThreads: (courseId: string) => withDemo(
    async () => unwrapList<any>(await request(`/ai/threads?course_id=${encodeURIComponent(courseId)}&mode=TEACHER&limit=100`)).map((raw) => ({
      id: String(raw.id), submissionId: raw.submission_id ? String(raw.submission_id) : undefined,
      status: String(raw.status ?? 'OPEN'), updatedAt: raw.updated_at ? String(raw.updated_at) : undefined,
    })),
    () => [],
  ),
  getAiMessages: (threadId: string) => withDemo(
    async () => unwrapList<any>(await request(`/ai/threads/${threadId}/messages`)).filter((raw) => raw.role !== 'SYSTEM').map((raw) => ({
      id: String(raw.id), from: String(raw.role).toUpperCase() === 'USER' ? 'user' as const : 'ai' as const,
      content: String(raw.content ?? ''), citations: unwrapList<any>(raw.citations ?? []).map((citation) => ({ title: String(citation.title), url: String(citation.url) })),
    })),
    () => [],
  ),
  sendAiMessage: (threadId: string, content: string) => withDemo(async () => {
    const raw = await request<any>(`/ai/threads/${threadId}/messages`, { method: 'POST', body: JSON.stringify({ content }) });
    return { id: String(raw.id ?? createUuid()), content: String(raw.content ?? raw.message?.content ?? raw.response ?? ''), citations: raw.citations ?? [] };
  }, () => ({
    id: createUuid(),
    content: threadId.startsWith('student-ai-')
      ? 'Посмотрите на выражение перед закрывающей фигурной скобкой: каждый оператор return в C++ завершается точкой с запятой. Сопоставьте это правило с позицией из диагностики.'
      : 'Это демонстрационный ответ без результатов серверной проверки. В рабочем режиме помощник анализирует точный снимок сдачи и возвращает вывод со ссылками на доступные проверки.',
    citations: [{ title: 'cppreference · Statements', url: 'https://en.cppreference.com/w/cpp/language/statements' }],
  })),
  getSettings: () => withDemo(async () => mapSettings(await request('/system/settings')), () => cloneDemo(settingsState)),
  getHealth: (): Promise<SystemHealth> => withDemo(async () => {
    const raw = await request<any>('/system/health');
    return { status: raw.status === 'ok' ? 'ok' : raw.status === 'degraded' ? 'degraded' : 'down', database: String(raw.database ?? 'error'), build: String(raw.build ?? 'unknown') } as SystemHealth;
  }, () => ({ status: 'ok' as const, database: 'ok', build: 'demo' })),
  updateSettings: (settings: SystemSettings) => withDemo(
    async () => mapSettings(await request('/system/settings', { method: 'PATCH', body: JSON.stringify(toSnakeSettings(settings)) })),
    () => { settingsState = cloneDemo(settings); return cloneDemo(settingsState); },
  ),
};

export const apiNormalizers = { mapSession, mapCourse, mapCourseSyncStatus, mapCourseCatalogEntry, mapAssessment, mapAssessmentPublicationTargets, mapAttempt, mapRun, mapDiagnostics, mapSettings, mapHistory, mapSubmission, mapExperiment, mapAuthorshipAnalysis, mapSimilarityAnalysis, mapTaskVersion, mapEvidenceReport };

function inFuture(seconds: number): string { return new Date(Date.now() + seconds * 1000).toISOString(); }
function toSnakeSettings(value: SystemSettings) {
  return {
    revision: value.revision,
    ai_enabled: value.aiEnabled, student_ai_enabled: value.studentAiEnabled, runner_enabled: value.runnerEnabled,
    runner_cpu_seconds: value.runnerCpuSeconds, runner_memory_mb: value.runnerMemoryMb,
    retention_days: value.retentionDays, allowed_lms_origins: value.allowedLmsOrigins, incident_banner: value.incidentBanner,
  };
}
