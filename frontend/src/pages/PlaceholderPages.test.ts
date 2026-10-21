import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createElement } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Assessment, HiddenTestCase } from '../types';
import { AssessmentIntroPage, buildHiddenTestManifest } from './PlaceholderPages';
import { ToastProvider } from '../components/ui';

const mocks = vi.hoisted(() => ({ getAssessment: vi.fn(), startAttempt: vi.fn(), getAssessmentPublicationTargets: vi.fn(), publishAssessment: vi.fn(), setAssessmentAiReviewPolicy: vi.fn() }));
const authRole = vi.hoisted(() => ({ primaryRole: 'STUDENT' as 'STUDENT' | 'TEACHER', capabilities: [] as string[] }));

vi.mock('../lib/api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../lib/api')>(), api: mocks,
}));
vi.mock('../context/AuthContext', () => ({ useAuth: () => ({ primaryRole: authRole.primaryRole, session: { capabilities: authRole.capabilities } }) }));

afterEach(cleanup);

describe('student assessment start', () => {
  const assessment: Assessment = {
    id: 'assessment-1', courseId: 'course-1', title: 'Лабораторная №1', summary: '',
    kind: 'LAB', status: 'AVAILABLE', maxScore: 5, fileMode: 'SINGLE',
    language: 'CPP', standard: 'C++17', pastePolicy: 'ALLOW', aiEnabled: false,
  };

  function renderAssessment(patch: Partial<Assessment> = {}) {
    mocks.getAssessment.mockResolvedValue({ ...assessment, ...patch });
    render(createElement(MemoryRouter, { initialEntries: ['/assessments/assessment-1'] },
      createElement(Routes, null,
        createElement(Route, { path: '/assessments/:assessmentId', element: createElement(AssessmentIntroPage) }),
        createElement(Route, { path: '/ide/:attemptId', element: createElement('p', null, 'Попытка открыта') }),
      ),
    ));
  }

  beforeEach(() => {
    authRole.primaryRole = 'STUDENT';
    authRole.capabilities = [];
    mocks.getAssessment.mockReset();
    mocks.startAttempt.mockReset().mockResolvedValue({ id: 'attempt-1' });
    mocks.setAssessmentAiReviewPolicy.mockReset();
  });

  it('shows automatic review as enabled by default to an administrator', async () => {
    authRole.primaryRole = 'TEACHER';
    authRole.capabilities = ['SYSTEM_SETTINGS'];
    renderAssessment();
    expect(await screen.findByRole('checkbox', { name: 'Автоматическая ИИ-проверка' })).toBeChecked();
    expect(screen.getByText(/Для новых работ включено по умолчанию/)).toBeInTheDocument();
    expect(screen.queryByText(/По умолчанию выключено/)).not.toBeInTheDocument();
    expect(mocks.setAssessmentAiReviewPolicy).not.toHaveBeenCalled();
  });

  it('keeps an administrator opt-out disabled instead of resetting it on display', async () => {
    authRole.primaryRole = 'TEACHER';
    authRole.capabilities = ['SYSTEM_SETTINGS'];
    renderAssessment({ aiReviewEnabled: false });
    expect(await screen.findByRole('checkbox', { name: 'Автоматическая ИИ-проверка' })).not.toBeChecked();
    expect(mocks.setAssessmentAiReviewPolicy).not.toHaveBeenCalled();
  });

  it('saves an explicit opt-out once without changing student AI help', async () => {
    authRole.primaryRole = 'TEACHER';
    authRole.capabilities = ['SYSTEM_SETTINGS'];
    mocks.setAssessmentAiReviewPolicy.mockResolvedValue({ ...assessment, aiReviewEnabled: false });
    renderAssessment({ aiReviewEnabled: true });
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Автоматическая ИИ-проверка' }));
    await waitFor(() => expect(screen.getByRole('checkbox', { name: 'Автоматическая ИИ-проверка' })).not.toBeChecked());
    expect(await screen.findByText('Настройка сохранена')).toBeInTheDocument();
    expect(mocks.setAssessmentAiReviewPolicy).toHaveBeenCalledExactlyOnceWith(assessment.id, false);
  });

  it('restores default-on display after a policy save failure instead of pretending it was saved', async () => {
    authRole.primaryRole = 'TEACHER';
    authRole.capabilities = ['SYSTEM_SETTINGS'];
    mocks.setAssessmentAiReviewPolicy.mockRejectedValue(new Error('Сервис временно недоступен'));
    renderAssessment();
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Автоматическая ИИ-проверка' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Сервис временно недоступен');
    expect(screen.getByRole('checkbox', { name: 'Автоматическая ИИ-проверка' })).toBeChecked();
    expect(screen.queryByText('Настройка сохранена')).not.toBeInTheDocument();
  });

  it.each([
    { rules: [] },
    { rules: [{ id: 'g1', targetType: 'GROUP' as const, targetExternalId: 'group-1', allowed: false }] },
  ])('does not call a Moodle work published when no group has access: %j', async ({ rules }) => {
    authRole.primaryRole = 'TEACHER';
    renderAssessment({ publicationStatus: 'PUBLISHED', policy: { moodle_metadata_read_only: true }, availabilityRules: rules });
    expect(await screen.findByText('Не включена')).toBeInTheDocument();
    expect(screen.queryByText('Опубликована')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Настроить доступ' })).toBeInTheDocument();
  });

  it('still shows publication for an explicitly allowed group', async () => {
    authRole.primaryRole = 'TEACHER';
    renderAssessment({ publicationStatus: 'PUBLISHED', policy: { moodle_metadata_read_only: true }, availabilityRules: [
      { id: 'g1', targetType: 'GROUP', targetExternalId: 'group-1', allowed: true },
    ] });
    expect(await screen.findByText('Опубликована')).toBeInTheDocument();
    expect(screen.queryByText('Не включена')).not.toBeInTheDocument();
  });

  it('updates the publication badge immediately after the last selected group is revoked', async () => {
    authRole.primaryRole = 'TEACHER';
    const open = { ...assessment, publicationStatus: 'PUBLISHED' as const,
      policy: { moodle_metadata_read_only: true }, availabilityRules: [
        { id: 'g1', targetType: 'GROUP' as const, targetExternalId: 'group-1', allowed: true },
      ] };
    mocks.getAssessment.mockResolvedValue(open);
    mocks.getAssessmentPublicationTargets.mockResolvedValue({ groups: [{ id: 'group-id', externalId: 'group-1', name: '2 — Тестовая группа' }], principals: [], overridesConfirmed: true });
    mocks.publishAssessment.mockResolvedValue({ ...open, availabilityRules: [] });
    render(createElement(MemoryRouter, { initialEntries: ['/assessments/assessment-1'] },
      createElement(ToastProvider, null, createElement(Routes, null,
        createElement(Route, { path: '/assessments/:assessmentId', element: createElement(AssessmentIntroPage) }),
      )),
    ));
    fireEvent.click(await screen.findByRole('button', { name: 'Изменить доступ' }));
    const group = await screen.findByRole('checkbox', { name: /2 — Тестовая группа/ });
    expect(group).toBeChecked(); fireEvent.click(group);
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить доступ' }));
    expect(await screen.findByText('Не включена')).toBeInTheDocument();
    expect(mocks.publishAssessment).toHaveBeenCalledWith(assessment.id, [], false);
    expect(screen.queryByText('Опубликована')).not.toBeInTheDocument();
  });

  it.each([
    { maxScore: 0, expected: 'Без балльного оценивания · попыток: 1' },
    { maxScore: 5, expected: 'до 5 баллов · попыток: 1' },
  ])('describes Moodle grading without treating a zero maximum as points ($maxScore)', async ({ maxScore, expected }) => {
    authRole.primaryRole = 'TEACHER';
    mocks.publishAssessment.mockReset();
    mocks.getAssessment.mockResolvedValue({ ...assessment, maxScore, attemptLimit: 1,
      publicationStatus: 'PUBLISHED', policy: { moodle_metadata_read_only: true }, availabilityRules: [] });
    mocks.getAssessmentPublicationTargets.mockResolvedValue({ groups: [], principals: [], overridesConfirmed: true });
    render(createElement(MemoryRouter, { initialEntries: ['/assessments/assessment-1'] },
      createElement(ToastProvider, null, createElement(Routes, null,
        createElement(Route, { path: '/assessments/:assessmentId', element: createElement(AssessmentIntroPage) }),
      )),
    ));
    fireEvent.click(await screen.findByRole('button', { name: 'Настроить доступ' }));
    expect(await screen.findByText(expected)).toBeVisible();
    expect(screen.queryByText('до 0 баллов · попыток: 1')).not.toBeInTheDocument();
    expect(mocks.publishAssessment).not.toHaveBeenCalled();
  });

  it('starts an available attempt directly without an acknowledgement checkbox', async () => {
    renderAssessment();
    const start = await screen.findByRole('button', { name: 'Начать попытку' });
    expect(start).toBeEnabled();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    fireEvent.click(start);
    await screen.findByText('Попытка открыта');
    expect(mocks.startAttempt).toHaveBeenCalledWith('assessment-1', expect.any(AbortSignal));
  });

  it('shows imported task images before starting an assessment', async () => {
    const url = 'https://edu.mmcs.sfedu.ru/filter/tex/pix.php/formula.png';
    renderAssessment({ summary: `Вычислите ![Формула суммы](<${url}>).` });
    expect(await screen.findByRole('img', { name: 'Формула суммы' })).toHaveAttribute('src', `/api/v1/statement-images/assessment/${assessment.id}?url=${encodeURIComponent(url)}`);
    expect(mocks.startAttempt).not.toHaveBeenCalled();
  });

  it.each([
    { startsAt: '2999-01-01T00:00:00Z' },
    { deadlineAt: '2000-01-01T00:00:00Z' },
    { status: 'CLOSED' as const },
  ])('keeps local availability restrictions for %j', async (patch) => {
    renderAssessment(patch);
    const start = await screen.findByRole('button', { name: /Откроется|Работа закрыта/ });
    expect(start).toBeDisabled();
    fireEvent.click(start);
    expect(mocks.startAttempt).not.toHaveBeenCalled();
  });

  it('lets Moodle verify managed availability at attempt start', async () => {
    renderAssessment({ requiresLiveLmsPreparation: true, status: 'CLOSED', deadlineAt: '2000-01-01T00:00:00Z' });
    const start = await screen.findByRole('button', { name: 'Начать попытку' });
    expect(start).toBeEnabled();
    fireEvent.click(start);
    await screen.findByText('Попытка открыта');
    expect(mocks.startAttempt).toHaveBeenCalledTimes(1);
  });

  it('shows a server rejection without navigating into the attempt', async () => {
    mocks.startAttempt.mockRejectedValue(new Error('Число попыток исчерпано'));
    renderAssessment();
    fireEvent.click(await screen.findByRole('button', { name: 'Начать попытку' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Число попыток исчерпано');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Начать попытку' })).toBeEnabled());
    expect(screen.queryByText('Попытка открыта')).not.toBeInTheDocument();
  });
});

const testCase = (name: string, patch: Partial<HiddenTestCase> = {}): HiddenTestCase => ({
  name, stdin: '', expected_stdout: 'OK\n', comparison: 'EXACT', ...patch,
});

describe('hidden-test manifest v1 editor contract', () => {
  it('serializes only the strict manifest-v1 fields and trims case names', () => {
    const result = buildHiddenTestManifest(true, [testCase('  sample  ', { comparison: 'TRIM_TRAILING_WHITESPACE' })]);
    expect(result.error).toBeUndefined();
    expect(result.manifest).toEqual({
      schema_version: 1,
      cases: [{ name: 'sample', stdin: '', expected_stdout: 'OK\n', comparison: 'TRIM_TRAILING_WHITESPACE' }],
    });
  });

  it('uses an empty object at the API layer when hidden tests are disabled', () => {
    expect(buildHiddenTestManifest(false, [testCase('ignored')])).toEqual({});
  });

  it('rejects empty and duplicate case names without regard to case', () => {
    expect(buildHiddenTestManifest(true, [testCase('  ')])).toMatchObject({ error: expect.stringContaining('не заполнено') });
    expect(buildHiddenTestManifest(true, [testCase('Boundary'), testCase('boundary')])).toMatchObject({ error: expect.stringContaining('уникальными') });
  });

  it('enforces per-field and aggregate backend limits before submission', () => {
    expect(buildHiddenTestManifest(true, [testCase('large', { stdin: 'x'.repeat(262_145) })])).toMatchObject({ error: expect.stringContaining('262 144') });
    expect(buildHiddenTestManifest(true, [testCase('multibyte', { stdin: 'я'.repeat(131_073) })])).toMatchObject({ error: expect.stringContaining('байта UTF-8') });
    const largeCases = Array.from({ length: 3 }, (_, index) => testCase(`large-${index}`, {
      stdin: 'x'.repeat(262_144), expected_stdout: 'y'.repeat(262_144),
    }));
    expect(buildHiddenTestManifest(true, largeCases)).toMatchObject({ error: expect.stringContaining('1 МиБ') });
  });

  it('accepts no more than twenty cases', () => {
    expect(buildHiddenTestManifest(true, Array.from({ length: 21 }, (_, index) => testCase(`case-${index}`))))
      .toMatchObject({ error: expect.stringContaining('1 до 20') });
  });
});
