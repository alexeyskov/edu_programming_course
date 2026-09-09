import { afterEach, describe, expect, it, vi } from 'vitest';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.resetModules();
  vi.unstubAllGlobals();
});

describe('submission origin and plagiarism analysis contract', () => {
  it.each([
    ['VERIFIED', 'Ответ Moodle совпадает с отправленным через систему.'],
    ['EXTERNAL_ORIGIN', 'Работа была сдана напрямую через Moodle.'],
    ['MISMATCH', 'Ответ Moodle отличается от отправленного через систему.'],
  ] as const)('maps the %s origin verification state without exposing digests', async (state, message) => {
    const { apiNormalizers } = await import('./api');

    const submission = apiNormalizers.mapSubmission({
      id: 'submission-1',
      assessment_id: 'assessment-1',
      assigned_task_version_id: 'task-version-7',
      assessment_title: 'Самостоятельная работа',
      student_name: 'Студент',
      submitted_at: '2026-08-30T10:00:00Z',
      status: 'UNGRADED',
      max_score: 5,
      origin_verification: {
        state,
        transport: 'ONLINE_TEXT',
        checked_at: '2026-08-30T10:01:00Z',
        message,
        artifact_md5: 'must-not-cross-the-api-boundary',
        artifact_sha256: 'must-not-cross-the-api-boundary',
      },
    });

    expect(submission).toMatchObject({
      taskVersionId: 'task-version-7',
      originVerification: {
        state,
        transport: 'ONLINE_TEXT',
        checkedAt: '2026-08-30T10:01:00Z',
        message,
      },
    });
    expect(submission.originVerification).not.toHaveProperty('artifactMd5');
    expect(submission.originVerification).not.toHaveProperty('artifactSha256');
  });

  it('sends the submission task version when plagiarism similarity is started manually', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'a'.repeat(32) }))
      .mockResolvedValueOnce(jsonResponse({
        id: 'analysis-1',
        assessment: 'assessment-1',
        task_version: 'task-version-7',
        state: 'PENDING',
        comparison_count: 0,
        match_count: 0,
        matches: [],
      }, 201));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await import('./api');

    await expect(api.createSimilarityAnalysis('assessment-1', 'task-version-7')).resolves.toMatchObject({
      id: 'analysis-1',
      assessmentId: 'assessment-1',
      state: 'PENDING',
    });

    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/assessments/assessment-1/similarity-analyses');
    const init = fetchMock.mock.calls[1][1] as RequestInit;
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ task_version_id: 'task-version-7' });
  });
});
