import { afterEach, describe, expect, it, vi } from 'vitest';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

async function freshApi() {
  vi.resetModules();
  return import('./api');
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('API transport security contract', () => {
  it('waits for a busy student session with the same start idempotency key', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'a'.repeat(32) }))
      .mockResolvedValueOnce(jsonResponse({ code: 'MOODLE_SESSION_BUSY' }, 409))
      .mockResolvedValueOnce(jsonResponse({ code: 'MOODLE_SESSION_BUSY' }, 409))
      .mockResolvedValueOnce(jsonResponse({ id: 'new-attempt', state: 'ACTIVE', workspace: { files: [] } }));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await freshApi();
    const result = api.startAttempt('assessment-1');
    await vi.advanceTimersByTimeAsync(4_100);
    await expect(result).resolves.toMatchObject({ id: 'new-attempt' });
    expect(fetchMock).toHaveBeenCalledTimes(4);
    const keys = fetchMock.mock.calls.slice(1).map(([, init]) => new Headers(init.headers).get('Idempotency-Key'));
    expect(keys[0]).toBeTruthy();
    expect(new Set(keys).size).toBe(1);
  });

  it('cancels waiting for a student session when leaving the page', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'a'.repeat(32) }))
      .mockResolvedValueOnce(jsonResponse({ code: 'MOODLE_SESSION_BUSY' }, 409));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await freshApi();
    const controller = new AbortController();
    const result = api.startAttempt('assessment-1', controller.signal);
    const rejected = expect(result).rejects.toMatchObject({ name: 'AbortError' });
    await vi.advanceTimersByTimeAsync(1_000);
    controller.abort();
    await rejected;
    await vi.advanceTimersByTimeAsync(100_000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it.each(['MOODLE_ATTEMPT_BINDING_CONFLICT', 'MOODLE_UNAVAILABLE', 'FORBIDDEN'])(
    'does not replay a start after %s', async (code) => {
      const fetchMock = vi.fn()
        .mockResolvedValueOnce(jsonResponse({ csrf_token: 'a'.repeat(32) }))
        .mockResolvedValueOnce(jsonResponse({ code }, 409));
      vi.stubGlobal('fetch', fetchMock);
      const { api } = await freshApi();
      await expect(api.startAttempt('assessment-1')).rejects.toMatchObject({ code });
      expect(fetchMock).toHaveBeenCalledTimes(2);
    },
  );

  it('sends the verified paste range and receipt with the workspace revision', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'a'.repeat(32) }))
      .mockResolvedValueOnce(jsonResponse({ revision: 8 }));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await freshApi();
    await api.saveFile('attempt-1', { id: 'file-1', path: 'main.cpp', content: 'int int' }, 7, 'internal_paste', 'receipt-1', { offset: 4, deleteCount: 0 });
    const request = fetchMock.mock.calls[1][1];
    expect(new Headers(request.headers).get('If-Match')).toBe('7');
    expect(JSON.parse(request.body)).toEqual({
      content: 'int int', source: 'INTERNAL_PASTE', receipt_id: 'receipt-1',
      paste_range: { offset: 4, delete_count: 0 },
    });
  });

  it('reads the persisted course synchronization state without starting another job', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      course_id: 'course-1', status: 'SYNCING', updated_at: '2026-09-02T10:00:00Z',
      error_code: '', error_message: '', retryable: false,
    }));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await freshApi();

    await expect(api.getCourseSyncStatus('course-1')).resolves.toMatchObject({
      courseId: 'course-1', syncStatus: 'SYNCING',
    });
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toContain('/courses/course-1/sync-status');
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: 'include' });
  });

  it('retries a failed Moodle history import through the outbox mutation', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'a'.repeat(32) }))
      .mockResolvedValueOnce(jsonResponse({
        id: 'event-1', event_type: 'moodle.history.import', aggregate_id: 'assessment-1',
        state: 'RETRY', created_at: '2026-09-03T01:00:00Z', updated_at: '2026-09-03T02:00:00Z',
        payload: { actor_external_subject: 'teacher-1' }, receipt: {},
      }));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await freshApi();

    await expect(api.retryMoodleHistoryImport('event-1')).resolves.toMatchObject({
      id: 'event-1', aggregateId: 'assessment-1', actorKey: 'teacher-1', state: 'RETRY',
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toContain('/integrations/lms/outbox/event-1/retry');
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: 'POST', credentials: 'include' });
  });

  it('retries a mutation only when the backend identifies a CSRF failure', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'a'.repeat(32) }))
      .mockResolvedValueOnce(jsonResponse({ code: 'CSRF_FAILED', message: 'expired' }, 403))
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'b'.repeat(32) }))
      .mockResolvedValueOnce(jsonResponse({
        principal: { id: 'p1', display_name: 'Teacher' },
        provider: { id: 'lms1', name: 'Moodle', provider: 'MOODLE' },
        roles: ['TEACHER'], memberships: [], capabilities: ['SYSTEM_SETTINGS'],
      }));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await freshApi();

    await expect(api.elevate('valid-token')).resolves.toMatchObject({ displayName: 'Teacher' });
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(new Headers(fetchMock.mock.calls[1][1]?.headers).get('X-CSRFToken')).toBe('a'.repeat(32));
    expect(new Headers(fetchMock.mock.calls[3][1]?.headers).get('X-CSRFToken')).toBe('b'.repeat(32));
  });

  it('does not repeat an authorization failure disguised as an ordinary 403', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'a'.repeat(32) }))
      .mockResolvedValueOnce(jsonResponse({ code: 'INVALID_ADMIN_TOKEN', message: 'invalid token' }, 403));
    vi.stubGlobal('fetch', fetchMock);
    const { api, ApiError } = await freshApi();

    await expect(api.elevate('invalid-token')).rejects.toMatchObject({
      status: 403, code: 'INVALID_ADMIN_TOKEN', message: 'invalid token',
    } satisfies Partial<InstanceType<typeof ApiError>>);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('unwraps structured FastAPI detail errors for a useful UI message', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'a'.repeat(32) }))
      .mockResolvedValueOnce(jsonResponse({ detail: { code: 'POLICY_REJECTED', message: 'policy rejected' } }, 422));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await freshApi();

    await expect(api.elevate('token')).rejects.toMatchObject({ code: 'POLICY_REJECTED', message: 'policy rejected' });
  });

  it.each([
    ['ACTIVE_REVIEW_CLAIM_REQUIRED', 'Сначала закрепите работу за собой.'],
    ['CLAIM_NOT_ACTIVE', 'Резервирование работы больше не действует. Откройте работу повторно.'],
    ['CLAIM_NOT_OWNED', 'Работа закреплена за другим преподавателем.'],
    ['CLAIM_EXPIRED', 'Срок резервирования работы истёк. Откройте работу повторно.'],
    ['CLAIM_NOT_FOUND', 'Резервирование работы не найдено. Откройте работу повторно.'],
    ['MOODLE_ATTEMPT_SUPERSEDED', 'Эта попытка уже не последняя в Moodle. Проверять и оценивать нужно последнюю попытку после её завершения.'],
  ])('localizes review reservation error %s before it reaches the UI', async (code, expectedMessage) => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({ code, message: 'raw claim error' }, 409));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await freshApi();

    await expect(api.getCourses()).rejects.toMatchObject({ code, message: expectedMessage });
  });

  it.each([
    ['INTEGRATION_ERROR', 'Не удалось выполнить обмен с внешним сервисом.'],
    ['NOT_CONFIGURED', 'Подключение к внешнему сервису не настроено.'],
    ['TIMEOUT', 'Внешний сервис не ответил вовремя. Повторите попытку.'],
    ['BROWSER_BUSY', 'Браузерный коннектор Moodle занят. Повторите синхронизацию через несколько секунд.'],
    ['UNAVAILABLE', 'Внешний сервис временно недоступен. Повторите попытку позже.'],
    ['INVALID_RESPONSE', 'Внешний сервис вернул неожиданный ответ.'],
    ['RESPONSE_TOO_LARGE', 'Ответ внешнего сервиса слишком большой.'],
    ['LMS_IMPORT_REQUIRES_CONFIGURATION', 'Импортированная работа ещё не готова. Синхронизируйте курс с Moodle.'],
    ['MOODLE_SOURCE_UNCONFIRMED', 'Moodle не подтвердил все параметры работы. Повторите синхронизацию курса.'],
    ['LMS_DELIVERY_PROFILE_UNRESOLVED', 'Moodle ещё не подтвердил формат ответа. Синхронизируйте курс и повторите запуск попытки.'],
    ['MOODLE_RUNTIME_PREPARATION_REQUIRED', 'Перед открытием редактора нужно проверить попытку в Moodle. Вернитесь к работе и откройте её ещё раз.'],
    ['MOODLE_RUNTIME_PREPARATION_FAILED', 'Moodle не подтвердил запуск попытки. Повторите открытие работы; если ошибка сохранится, сообщите преподавателю.'],
    ['MOODLE_ATTEMPT_BINDING_CONFLICT', 'Открытая попытка не совпадает с попыткой Moodle. Работа не изменена; обновите страницу и повторите вход.'],
  ])('localizes integration error %s before it reaches the UI', async (code, expectedMessage) => {
    // Use a non-5xx response here: development `auto` mode deliberately falls
    // back to demo data for transport/server failures.  This test exercises
    // only the user-facing error localization contract.
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({ code, message: 'LMS synchronization failed' }, 409));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await freshApi();

    await expect(api.getCourses()).rejects.toMatchObject({ code, message: expectedMessage });
  });

  it('localizes a known legacy integration message even when the error code is generic', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({ code: 'LEGACY_GATEWAY_ERROR', message: 'LMS synchronization failed' }, 409));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await freshApi();

    await expect(api.getCourses()).rejects.toMatchObject({
      code: 'LEGACY_GATEWAY_ERROR', message: 'Не удалось синхронизировать курс с Moodle.',
    });
  });

  it('notifies the auth provider when an authenticated API call returns 401', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({ code: 'NOT_AUTHENTICATED', message: 'expired' }, 401));
    vi.stubGlobal('fetch', fetchMock);
    const { api, AUTH_UNAUTHORIZED_EVENT } = await freshApi();
    const listener = vi.fn();
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, listener);

    await expect(api.getCourses()).rejects.toMatchObject({ status: 401, code: 'NOT_AUTHENTICATED' });
    expect(listener).toHaveBeenCalledOnce();
    window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, listener);
  });

  it('deletes a workspace file with optimistic revision and an idempotency key', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'a'.repeat(32) }))
      .mockResolvedValueOnce(jsonResponse({ file_id: 'f1', revision: 8 }));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await freshApi();

    await expect(api.deleteFile('attempt-1', 'f1', 7)).resolves.toEqual({ fileId: 'f1', revision: 8 });
    const init = fetchMock.mock.calls[1][1] as RequestInit;
    const headers = new Headers(init.headers);
    expect(init.method).toBe('DELETE');
    expect(init.body).toBe('{}');
    expect(headers.get('If-Match')).toBe('7');
    expect(headers.get('Idempotency-Key')).toBeTruthy();
    expect(headers.get('X-CSRFToken')).toBe('a'.repeat(32));
  });

  it('starts deterministic evidence for the submission with an empty mutation and idempotency key', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'a'.repeat(32) }))
      .mockResolvedValueOnce(jsonResponse({
        id: 'e1', submission_id: 's1', snapshot_id: 'snap1', task_version_id: 'v1', requested_by_id: 'p1',
        hidden_test_manifest_hash: 'm', task_content_hash: 't', snapshot_manifest_hash: 's', status: 'COMPLETED',
        passed_cases: 1, total_cases: 1, outcomes: [], findings: [], created_at: '2026-08-24T12:00:00Z', updated_at: '2026-08-24T12:00:01Z',
      }, 201));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await freshApi();

    await expect(api.createEvidenceRun('s1')).resolves.toMatchObject({ id: 'e1', submissionId: 's1', status: 'COMPLETED' });
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/submissions/s1/evidence-runs');
    const init = fetchMock.mock.calls[1][1] as RequestInit;
    expect(init.method).toBe('POST');
    expect(init.body).toBe('{}');
    expect(new Headers(init.headers).get('Idempotency-Key')).toBeTruthy();
  });

  it('maps the authorised plagiarism comparison and its exact line evidence', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      match: {
        id: 'match-1', submission_a_id: 'submission-a', submission_b_id: 'submission-b', score: '0.73',
        fingerprint_count_a: 17, fingerprint_count_b: 19, shared_fingerprint_count: 11,
        evidence: [{
          file_a: 'src/a.cpp', start_line_a: 3, end_line_a: 6,
          file_b: 'main.cpp', start_line_b: 12, end_line_b: 15,
          token_count: 24, excerpt_a: 'for (...)', excerpt_b: 'for (...)',
        }],
      },
      assessment_id: 'assessment-1', assessment_title: 'Контрольная работа',
      left: {
        submission_id: 'submission-a', student_name: 'Анна', student_group: '1.1',
        submitted_at: '2026-08-25T10:00:00Z', files: [{ path: 'src/a.cpp', language: 'cpp', content: 'int a;' }],
      },
      right: {
        submission_id: 'submission-b', student_name: 'Борис', student_group: '1.2',
        submitted_at: '2026-08-25T10:01:00Z', files: [{ path: 'main.cpp', language: 'cpp', content: 'int b;' }],
      },
    }));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await freshApi();

    await expect(api.getSimilarityComparison('match-1')).resolves.toMatchObject({
      assessmentId: 'assessment-1', assessmentTitle: 'Контрольная работа',
      match: {
        id: 'match-1', submissionA: 'submission-a', submissionB: 'submission-b', score: 0.73,
        sharedFingerprintCount: 11,
        evidence: [{ fileA: 'src/a.cpp', startLineA: 3, endLineA: 6, fileB: 'main.cpp', startLineB: 12, endLineB: 15, tokenCount: 24 }],
      },
      left: { submissionId: 'submission-a', studentName: 'Анна', files: [{ id: 'src/a.cpp', path: 'src/a.cpp', content: 'int a;' }] },
      right: { submissionId: 'submission-b', studentName: 'Борис' },
    });
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/similarity-matches/match-1/comparison', expect.any(Object));
  });

  it('loads every page of globally visible submissions', async () => {
    const firstPage = Array.from({ length: 500 }, (_, index) => ({
      id: `submission-${index}`, assessment_id: 'assessment-1', course_id: 'course-1',
      assessment_title: 'Контрольная', student_name: `Студент ${index}`, submitted_at: '2026-08-25T10:00:00Z',
      status: 'UNGRADED', max_score: 10, review_required: true, decision_support_enabled: true,
    }));
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(firstPage))
      .mockResolvedValueOnce(jsonResponse([{ ...firstPage[0], id: 'submission-500', student_name: 'Студент 500' }]));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await freshApi();

    const submissions = await api.getSubmissions();

    expect(submissions).toHaveLength(501);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/assessments/all/submissions?limit=500&offset=0');
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/assessments/all/submissions?limit=500&offset=500');
  });

  it('sends the strict hidden_test_manifest v1 object when a task version is created', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'a'.repeat(32) }))
      .mockResolvedValueOnce(jsonResponse({ id: 'v1', item_id: 'task-1', number: 1, title: 'Task', language: 'CPP', language_standard: 'C++20', multi_file: false, status: 'DRAFT' }, 201));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await freshApi();
    const manifest = { schema_version: 1 as const, cases: [{ name: 'sample', stdin: '1\n', expected_stdout: '2\n', comparison: 'EXACT' as const }] };

    await api.createTaskVersion('task-1', { title: 'Task', statement: 'Statement', language: 'CPP', multiFile: false, maxScore: 10, hiddenTestManifest: manifest });
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/task-bank/items/task-1/versions');
    const body = JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body));
    expect(body.hidden_test_manifest).toEqual(manifest);
    expect(body).not.toHaveProperty('hiddenTestManifest');
  });

  it('reads and removes entries through the administrator course catalog contract', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse([{
        id: 'c1', connection_id: 'lms1', connection_name: 'MMCS Moodle', external_id: '549',
        title: 'C++', short_name: 'CPP', external_url: 'https://edu.mmcs.sfedu.ru/course/view.php?id=549',
        sync_status: 'CURRENT', added_at: '2026-08-25T10:00:00Z',
      }]))
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'a'.repeat(32) }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await freshApi();

    await expect(api.getCourseCatalog()).resolves.toEqual([
      expect.objectContaining({ id: 'c1', connectionName: 'MMCS Moodle', externalId: '549', syncStatus: 'SYNCED' }),
    ]);
    await expect(api.deleteCourseCatalogEntry('c1')).resolves.toBeUndefined();

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/system/course-catalog');
    expect(fetchMock.mock.calls[2][0]).toBe('/api/v1/system/course-catalog/c1');
    const init = fetchMock.mock.calls[2][1] as RequestInit;
    expect(init.method).toBe('DELETE');
    expect(new Headers(init.headers).get('X-CSRFToken')).toBe('a'.repeat(32));
  });

  it('reveals and replaces a teacher token through the administrator-only contract', async () => {
    const metadata = {
      id: 'token-1', label: 'Преподаватель', public_id: 'public-1', hash_fingerprint: 'hash-1',
      can_reveal: true, use_count: 2, created_at: '2026-09-04T10:00:00Z',
      bound_principal_id: 'principal-1', bound_display_name: 'Алексей',
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse([metadata]))
      .mockResolvedValueOnce(jsonResponse({ token: 'Ab12Cd34' }))
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'a'.repeat(32) }))
      .mockResolvedValueOnce(jsonResponse({ ...metadata, hash_fingerprint: 'hash-2' }));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await freshApi();

    await expect(api.getTeacherTokens()).resolves.toEqual([
      expect.objectContaining({ id: 'token-1', canReveal: true, boundDisplayName: 'Алексей' }),
    ]);
    await expect(api.revealTeacherToken('token-1')).resolves.toEqual({ token: 'Ab12Cd34' });
    await expect(api.updateTeacherToken('token-1', 'Zx90Yw12')).resolves.toMatchObject({
      id: 'token-1', canReveal: true, hashFingerprint: 'hash-2', boundPrincipalId: 'principal-1',
    });

    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/system/teacher-tokens/token-1/secret');
    expect(fetchMock.mock.calls[3][0]).toBe('/api/v1/system/teacher-tokens/token-1');
    const updateInit = fetchMock.mock.calls[3][1] as RequestInit;
    expect(updateInit.method).toBe('PATCH');
    expect(updateInit.body).toBe(JSON.stringify({ token: 'Zx90Yw12' }));
    expect(new Headers(updateInit.headers).get('X-CSRFToken')).toBe('a'.repeat(32));
  });
});
