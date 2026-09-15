import { describe, expect, it } from 'vitest';
import { ApiError, apiNormalizers } from './api';

describe('backend DTO normalization', () => {
  it('localizes the terminal Moodle-attempt contract shown in the IDE', () => {
    const error = new ApiError(
      409,
      'LMS_ATTEMPT_FINALIZED',
      'The Moodle attempt was already finalized outside the application',
    );

    expect(error.message).toBe('Сеанс работы завершён через Moodle.');
  });

  it.each([
    {
      code: 'MOODLE_QUIZ_GRADING_METHOD_UNCONFIRMED',
      serverMessage: 'The Moodle Quiz grading method has not been confirmed',
      message: 'Не удалось подтвердить метод оценивания теста Moodle. Повторите синхронизацию курса.',
    },
    {
      code: 'MOODLE_LAST_ATTEMPT_GRADING_REQUIRED',
      serverMessage: "Moodle Quiz must use the 'Last attempt' grading method",
      message: 'Сервер системы использует устаревшую проверку метода оценивания. Обновите систему: настройки Moodle менять не требуется.',
    },
  ])('localizes $code without requesting Moodle settings changes', ({ code, serverMessage, message }) => {
    const error = new ApiError(409, code, serverMessage);

    expect(error.message).toBe(message);
    expect(error.message).not.toContain(serverMessage);
  });

  it('maps the session contract and SYSTEM_SETTINGS elevation', () => {
    const session = apiNormalizers.mapSession({
      principal: { id: 'p1', display_name: 'Иван Иванов' },
      roles: ['TEACHER'], capabilities: ['SYSTEM_SETTINGS'],
      admin_elevation_expires_at: '2026-08-24T12:00:00Z',
    });
    expect(session.displayName).toBe('Иван Иванов');
    expect(session.memberships[0].role).toBe('TEACHER');
    expect(session.capabilities).toContain('SYSTEM_SETTINGS');
  });

  it('rejects an unsupported LMS role instead of silently granting a local role', () => {
    expect(() => apiNormalizers.mapSession({
      principal: { id: 'p1', display_name: 'Иван Иванов' },
      memberships: [{ course_id: 'c1', course_name: 'C++', role: 'ADMIN' }],
      capabilities: [],
    })).toThrow(/неподдерживаемую роль/);
  });

  it('maps attempt and workspace current revision', () => {
    const attempt = apiNormalizers.mapAttempt({
      id: 'a1', assessment: 'as1', assessment_title: 'Контрольная', state: 'ACTIVE',
      deadline_at: '2026-08-24T12:00:00Z', workspace_revision: 7,
    }, { current_revision: 8, files: [
      { id: 'f1', path: 'main.cpp', content: 'int main(){}', language: 'CPP' },
      { id: 'f2', path: 'fixtures/input.txt', content: '42\n', language: 'TEXT' },
    ] });
    expect(attempt.assessmentId).toBe('as1');
    expect(attempt.title).toBe('Контрольная');
    expect(attempt.acknowledgedRevision).toBe(8);
    expect(attempt.files[0].path).toBe('main.cpp');
    expect(attempt.files[0].language).toBe('cpp');
    expect(attempt.files[1].language).toBe('plaintext');
  });

  it('treats INTERNAL_ONLY as strict and keeps an unlimited deadline nullable', () => {
    const strict = apiNormalizers.mapAttempt({ id: 'a1', assessment: 'as1', state: 'ACTIVE', paste_policy: 'INTERNAL_ONLY' }, { current_revision: 1, multi_file: true, files: [] });
    const open = apiNormalizers.mapAttempt({ id: 'a2', assessment: 'as1', state: 'ACTIVE', paste_policy: 'UNRESTRICTED' }, { current_revision: 1, files: [] });
    expect(strict.pastePolicy).toBe('STRICT');
    expect(strict.fileMode).toBe('MULTI');
    expect(strict.deadlineAt).toBeUndefined();
    expect(open.pastePolicy).toBe('ALLOW');
  });

  it('preserves the local editing deadline and separate Moodle upload reserve', () => {
    const result = apiNormalizers.mapAttempt({
      id: 'timed', state: 'ACTIVE',
      deadline_at: '2026-09-10T10:10:00Z', expected_end_at: '2026-09-10T10:15:00Z',
      moodle_sync_timeout_seconds: 300,
    });
    expect(result.deadlineAt).toBe('2026-09-10T10:10:00Z');
    expect(result.expectedEndAt).toBe('2026-09-10T10:15:00Z');
    expect(result.moodleSyncTimeoutSeconds).toBe(300);
    expect(apiNormalizers.mapAttempt({ id: 'untimed' }).moodleSyncTimeoutSeconds).toBeUndefined();
  });

  it('preserves the safe live-Moodle preparation hint on an attempt', () => {
    expect(apiNormalizers.mapAttempt({
      id: 'a1', assessment_id: 'as1', state: 'ACTIVE',
      requires_live_lms_preparation: true,
    }, { current_revision: 0, files: [] }).requiresLiveLmsPreparation).toBe(true);
  });

  it.each([true, false, null, undefined])('preserves the tri-state time limit hint %s', (hasTimeLimit) => {
    const attempt = apiNormalizers.mapAttempt({ id: 'a1', state: 'ACTIVE', has_time_limit: hasTimeLimit });
    expect(attempt.hasTimeLimit).toBe(typeof hasTimeLimit === 'boolean' ? hasTimeLimit : undefined);
  });

  it('maps the pre-created quiz question workspaces in Moodle order', () => {
    expect(apiNormalizers.mapAttempt({
      id: 'attempt-2', state: 'ACTIVE',
      quiz_session: {
        id: 'session-1', root_attempt_id: 'attempt-1',
        questions: [
          { attempt_id: 'attempt-2', slot: '7', title: 'Массивы', position: 2 },
          { attempt_id: 'attempt-1', slot: 3, title: 'Строки', position: 1 },
        ],
      },
    }).quizSession).toEqual({
      id: 'session-1', rootAttemptId: 'attempt-1',
      questions: [
        { attemptId: 'attempt-1', slot: '3', title: 'Строки', position: 1 },
        { attemptId: 'attempt-2', slot: '7', title: 'Массивы', position: 2 },
      ],
    });
    expect(apiNormalizers.mapAttempt({ id: 'single-attempt', state: 'ACTIVE' }).quizSession).toBeUndefined();
  });

  it.each(['FINISHING', 'LOCKED', 'VOID', 'EXPIRED', 'CLOSED', 'TIMED_OUT', 'CANCELLED', 'ENDED', 'TERMINATED', 'FUTURE_UNKNOWN_STATE'])('keeps terminal or unknown attempt state %s read-only', (state) => {
    expect(apiNormalizers.mapAttempt({ id: 'a1', state }, { current_revision: 1, files: [] }).status).toBe('LOCKED');
  });

  it.each(['SUBMITTED', 'AUTO_SUBMITTED', 'FINALIZED', 'GRADED'])('maps submitted terminal state %s', (state) => {
    expect(apiNormalizers.mapAttempt({ id: 'a1', state }, { current_revision: 1, files: [] }).status).toBe('SUBMITTED');
  });

  it('maps nested runner result and diagnostic range', () => {
    const run = apiNormalizers.mapRun({
      id: 'r1', status: 'FAILED', revision: 8,
      result: {
        exit_code: 1, stderr: 'compile failed', stdout: '',
        metrics: { wall_time_ms: 321, peak_memory_kb: 4096 },
        diagnostics: [{
          file: 'src/main.cpp', range: { start_line: 4, start_column: 8, end_line: 4, end_column: 9 },
          severity: 'error', code: 'expected_semi', message: 'expected ;', related: [{ message: 'after statement' }],
        }],
      },
    });
    expect(run.exitCode).toBe(1);
    expect(run.durationMs).toBe(321);
    expect(run.diagnostics[0]).toMatchObject({ path: 'src/main.cpp', line: 4, column: 8, endLine: 4, endColumn: 9 });
    expect(run.diagnostics[0].notes).toEqual(['after statement']);
  });

  it('maps edit events into displayable history entries', () => {
    const [event] = apiNormalizers.mapHistory([{ id: 'e1', event_type: 'TEXT_EDIT', source: 'INTERNAL_PASTE', received_at: '2026-08-24T10:00:00Z', sequence: 9 }]);
    expect(event).toMatchObject({ id: 'e1', type: 'internal_paste', at: '2026-08-24T10:00:00Z', revision: 9 });
    expect(event.label).toBeTruthy();
  });

  it('honours the explicit AttemptHistoryEventRead type returned by FastAPI', () => {
    const [event] = apiNormalizers.mapHistory([{
      id: 'e1', type: 'internal_paste', label: 'Внутренняя вставка', at: '2026-08-24T10:00:00Z', revision: 9,
      client: { ip_address: '203.0.113.42', browser: 'Firefox', browser_version: '142.0', operating_system: 'Linux', device_type: 'DESKTOP' },
    }]);
    expect(event.type).toBe('internal_paste');
    expect(event.client).toEqual({
      ipAddress: '203.0.113.42', browser: 'Firefox', browserVersion: '142.0', operatingSystem: 'Linux', deviceType: 'DESKTOP',
    });
  });

  it('maps the latest review decision and its immutable history from snake_case', () => {
    const submission = apiNormalizers.mapSubmission({
      id: 'submission-1', source: 'moodle_import', assessment_id: 'assessment-1', course_id: 'course-1', course_title: 'Программирование на C++', status: 'GRADED', score: '8.5', max_score: '10',
      latest_decision: {
        id: 'decision-2', reviewer_name: 'Коваленко А.', grade: '8.5', comment: 'Исправлены замечания',
        revision: 2, reviewed_at: '2026-08-25T12:00:00Z', lms_export_state: 'PENDING',
      },
      decision_history: [
        { id: 'decision-2', reviewer_name: 'Коваленко А.', grade: '8.5', comment: 'Исправлены замечания', revision: 2, reviewed_at: '2026-08-25T12:00:00Z', lms_export_state: 'PENDING' },
        { id: 'decision-1', reviewer_name: 'Сергеева Е.', grade: '7', comment: 'Первая проверка', revision: 1, reviewed_at: '2026-08-25T11:00:00Z', lms_export_state: 'SUPERSEDED' },
      ],
    });

    expect(submission).toMatchObject({ source: 'MOODLE_IMPORT', courseId: 'course-1', courseTitle: 'Программирование на C++' });
    expect(submission.latestDecision).toMatchObject({ reviewerName: 'Коваленко А.', grade: 8.5, revision: 2, lmsExportState: 'PENDING' });
    expect(submission.decisionHistory).toHaveLength(2);
    expect(submission.decisionHistory[1]).toMatchObject({ reviewerName: 'Сергеева Е.', comment: 'Первая проверка', revision: 1 });
  });

  it('maps independently reviewable Moodle Quiz questions into one response group', () => {
    const submission = apiNormalizers.mapSubmission({
      id: 'question-2', assessment_id: 'assessment-2', assessment_title: 'Задание с массивами',
      status: 'CLAIMED', max_score: 4,
      review_group: {
        id: 'quiz-response-81', title: 'Самостоятельная работа №3',
        items: [
          { submission_id: 'question-1', position: 1, title: 'Задание со строками', score: 3, max_score: 3, status: 'GRADED' },
          { submission_id: 'question-2', position: 2, title: 'Задание с массивами', max_score: 4, status: 'CLAIMED' },
        ],
      },
    });

    expect(submission.reviewGroup).toEqual({
      id: 'quiz-response-81', title: 'Самостоятельная работа №3',
      items: [
        { submissionId: 'question-1', position: 1, title: 'Задание со строками', score: 3, maxScore: 3, status: 'GRADED' },
        { submissionId: 'question-2', position: 2, title: 'Задание с массивами', score: undefined, maxScore: 4, status: 'CLAIMED' },
      ],
    });
  });

  it('normalizes backend course sync states', () => {
    expect(apiNormalizers.mapCourse({ id: 'c1', title: 'C++', sync_status: 'CURRENT', role: 'TEACHER' }).syncStatus).toBe('SYNCED');
    expect(apiNormalizers.mapCourse({ id: 'c1', title: 'C++', sync_status: 'PENDING', role: 'TEACHER' }).syncStatus).toBe('STALE');
    expect(apiNormalizers.mapCourse({ id: 'c1', title: 'C++', sync_status: 'FAILED', role: 'TEACHER' }).syncStatus).toBe('ERROR');
    expect(apiNormalizers.mapCourse({
      id: 'c1', title: 'C++', sync_status: 'FAILED', role: 'TEACHER',
      sync_error_code: 'UNAVAILABLE', sync_error_message: 'browser busy',
      sync_error_at: '2026-08-27T18:00:00Z', sync_error_retryable: true,
    }).syncError).toEqual({
      code: 'UNAVAILABLE', message: 'browser busy',
      at: '2026-08-27T18:00:00Z', retryable: true,
    });
  });

  it('normalizes the durable course synchronization status endpoint', () => {
    expect(apiNormalizers.mapCourseSyncStatus({
      course_id: 'c1', status: 'FAILED', updated_at: '2026-09-02T10:00:00Z',
      error_code: 'UNAVAILABLE', error_message: 'browser session is busy',
      error_at: '2026-09-02T09:59:59Z', retryable: true,
    })).toEqual({
      courseId: 'c1', syncStatus: 'ERROR', updatedAt: '2026-09-02T10:00:00Z',
      syncError: {
        code: 'UNAVAILABLE', message: 'browser session is busy',
        at: '2026-09-02T09:59:59Z', retryable: true,
      },
    });
  });

  it.each([
    ['CURRENT', 'SYNCED'],
    ['SYNCING', 'SYNCING'],
    ['FAILED', 'ERROR'],
    ['PENDING', 'STALE'],
  ] as const)('normalizes catalog sync state %s to %s', (source, expected) => {
    expect(apiNormalizers.mapCourseCatalogEntry({
      id: 'c1', connection_id: 'lms1', connection_name: 'MMCS Moodle', external_id: '00549',
      title: 'C++', short_name: 'CPP', external_url: 'https://edu.mmcs.sfedu.ru/course/view.php?id=549',
      sync_status: source, added_at: '2026-08-25T10:00:00Z',
    })).toMatchObject({
      connectionId: 'lms1', connectionName: 'MMCS Moodle', externalId: '00549', syncStatus: expected,
    });
  });

  it('uses the student projection to mark an active attempt and reads nested task metadata', () => {
    const assessment = apiNormalizers.mapAssessment({
      id: 'as1', course_id: 'c1', type: 'LAB', status: 'PUBLISHED', attempt_id: 'a1', progress: 0,
      max_score: '12.50', score: '7.25', duration_seconds: 3_601, multi_file: false,
      requires_live_lms_preparation: true,
      task: { language: 'C', language_standard: 'C17', multi_file: false },
    }, 'c1');
    expect(assessment).toMatchObject({
      status: 'IN_PROGRESS', language: 'C', standard: 'C17', durationMinutes: 61,
      maxScore: 12.5, score: 7.25, progress: 0,
      requiresLiveLmsPreparation: true,
    });
  });

  it('does not invent a concrete language standard when the assessment projection omits its task', () => {
    expect(apiNormalizers.mapAssessment({ id: 'as1', course_id: 'c1', type: 'LAB', status: 'PUBLISHED', max_score: 10 }, 'c1').standard).toBe('C/C++');
  });

  it('keeps teacher review flags and availability rules from the FastAPI projection', () => {
    const assessment = apiNormalizers.mapAssessment({
      id: 'as1', course_id: 'c1', type: 'CONTROL', status: 'DRAFT', max_score: '20.00',
      review_required: true, decision_support_enabled: false,
      availability_rules: [{ id: 'r1', target_type: 'GROUP', target_external_id: 'moodle-g7', allowed: true }],
    }, 'c1');
    expect(assessment).toMatchObject({ reviewRequired: true, decisionSupportEnabled: false });
    expect(assessment.availabilityRules).toEqual([expect.objectContaining({ id: 'r1', targetType: 'GROUP', targetExternalId: 'moodle-g7', allowed: true })]);
  });

  it('maps Moodle groups and individual override targets without losing limits', () => {
    expect(apiNormalizers.mapAssessmentPublicationTargets({
      groups: [{ id: 'g1', external_id: 'moodle-g1', name: '2.4' }],
      principals: [{
        principal_id: 'p1', external_subject: '104684', display_name: 'Test User',
        groups: ['2.4'], opens_at: '2026-04-01T09:00:00Z', closes_at: '2026-10-02T00:00:00Z',
        duration_seconds: 1800, attempt_limit: 10, attempts_unlimited: false,
      }],
      overrides_confirmed: true,
    })).toEqual({
      groups: [{ id: 'g1', externalId: 'moodle-g1', name: '2.4' }],
      principals: [{
        principalId: 'p1', externalSubject: '104684', displayName: 'Test User', groups: ['2.4'],
        opensAt: '2026-04-01T09:00:00Z', closesAt: '2026-10-02T00:00:00Z',
        durationSeconds: 1800, attemptLimit: 10, attemptsUnlimited: false,
      }],
      overridesConfirmed: true,
    });
  });

  it('does not infer enabled services from an empty settings payload', () => {
    const settings = apiNormalizers.mapSettings({});
    expect(settings.aiEnabled).toBe(false);
    expect(settings.studentAiEnabled).toBe(false);
    expect(settings.runnerEnabled).toBe(false);
    expect(settings.services).toEqual([]);
  });

  it('normalizes uppercase diagnostic severity', () => {
    const [diagnostic] = apiNormalizers.mapDiagnostics([{ file: 'main.cpp', range: { start_line: 2, start_column: 1 }, severity: 'WARNING', message: 'warning' }]);
    expect(diagnostic).toMatchObject({ path: 'main.cpp', line: 2, severity: 'warning' });
  });

  it('maps authorship evidence without inventing missing values', () => {
    const analysis = apiNormalizers.mapAuthorshipAnalysis({
      id: 'aa1', submission: 's1', state: 'COMPLETED', outcome: 'ANALYZER_RESULT', probability: '0.82',
      result: { probability: 0.82, uncertainty: 0.08, analyzer: 'history-api', model: 'v3', calibration: { version: '2026-08' }, warnings: ['small sample'] },
    });
    expect(analysis).toMatchObject({ id: 'aa1', submissionId: 's1', state: 'COMPLETED', probability: 0.82 });
    expect(analysis.result).toMatchObject({ analyzer: 'history-api', model: 'v3', uncertainty: 0.08, warnings: ['small sample'] });
    expect(apiNormalizers.mapAuthorshipAnalysis({ id: 'aa2', submission: 's1', state: 'PENDING' }).probability).toBeUndefined();
  });

  it('maps similarity jobs and server-provided matches', () => {
    const analysis = apiNormalizers.mapSimilarityAnalysis({
      id: 'sa1', assessment: 'as1', algorithm_version: 'tokens-v2', state: 'COMPLETED', comparison_count: 12, match_count: 1,
      matches: [{ id: 'm1', submission_a: 's1', submission_b: 's2', score: '0.34', evidence: { fragments: 2 } }],
    });
    expect(analysis).toMatchObject({ assessmentId: 'as1', algorithmVersion: 'tokens-v2', comparisonCount: 12, matchCount: 1 });
    expect(analysis.matches[0]).toMatchObject({ submissionA: 's1', submissionB: 's2', score: 0.34 });
  });

  it('keeps task versions addressable after a newer draft is created', () => {
    const version = apiNormalizers.mapTaskVersion({ id: 'v1', item: 'task-1', number: 2, title: 'Версия 2', language: 'CPP', language_standard: 'C++20', multi_file: true, status: 'DRAFT', ai_policy: { lms_import_requires_configuration: true } });
    expect(version).toMatchObject({ id: 'v1', itemId: 'task-1', number: 2, multiFile: true, status: 'DRAFT', aiPolicy: { lms_import_requires_configuration: true } });
  });

  it('maps deterministic evidence reports and per-case outcomes without changing their semantics', () => {
    const report = apiNormalizers.mapEvidenceReport({
      id: 'er1', submission_id: 's1', snapshot_id: 'snap1', task_version_id: 'v1', requested_by_id: 'teacher1',
      hidden_test_manifest_hash: 'manifest-sha', task_content_hash: 'task-sha', snapshot_manifest_hash: 'snapshot-sha',
      status: 'COMPLETED', passed_cases: 1, total_cases: 2,
      outcomes: [{
        case_index: 1, name: 'negative input', run_id: 'run2', status: 'FAILED', comparison: 'TRIM_TRAILING_WHITESPACE',
        exit_code: 0, actual_stdout_sha256: 'actual-sha', expected_stdout_sha256: 'expected-sha',
        actual_stdout_preview: '-2\n', stderr_preview: '', filesystem_isolated: true, network_enabled: false,
      }],
      findings: [{ code: 'OUTPUT_MISMATCH', message: 'Output differs', case_index: 1, run_id: 'run2' }],
      failure_code: null, failure_message: null, completed_at: '2026-08-24T12:01:00Z',
      created_at: '2026-08-24T12:00:00Z', updated_at: '2026-08-24T12:01:00Z',
    });
    expect(report).toMatchObject({ id: 'er1', submissionId: 's1', status: 'COMPLETED', passedCases: 1, totalCases: 2 });
    expect(report.outcomes[0]).toMatchObject({ caseIndex: 1, status: 'FAILED', comparison: 'TRIM_TRAILING_WHITESPACE', filesystemIsolated: true, networkEnabled: false });
    expect(report.findings[0]).toMatchObject({ code: 'OUTPUT_MISMATCH', caseIndex: 1, runId: 'run2' });
  });
});
