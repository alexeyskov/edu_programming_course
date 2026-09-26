import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createElement } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Assessment, HiddenTestCase } from '../types';
import { AssessmentIntroPage, buildHiddenTestManifest } from './PlaceholderPages';

const mocks = vi.hoisted(() => ({ getAssessment: vi.fn(), startAttempt: vi.fn() }));

vi.mock('../lib/api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../lib/api')>(), api: mocks,
}));
vi.mock('../context/AuthContext', () => ({ useAuth: () => ({ primaryRole: 'STUDENT' }) }));

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
    mocks.getAssessment.mockReset();
    mocks.startAttempt.mockReset().mockResolvedValue({ id: 'attempt-1' });
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
