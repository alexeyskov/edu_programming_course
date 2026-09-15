import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '../components/ui';
import type { Assessment } from '../types';
import { AssessmentIntroPage } from './PlaceholderPages';

const mocks = vi.hoisted(() => ({
  getAssessment: vi.fn(), getAssessmentPublicationTargets: vi.fn(), publishAssessment: vi.fn(),
}));

vi.mock('../lib/api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../lib/api')>(), api: mocks,
}));
vi.mock('../context/AuthContext', () => ({ useAuth: () => ({ primaryRole: 'TEACHER' }) }));

const assessment: Assessment = {
  id: 'assessment-1', courseId: 'course-1', title: 'Лабораторная №1', summary: '',
  kind: 'LAB', status: 'AVAILABLE', publicationStatus: 'PUBLISHED', maxScore: 5,
  fileMode: 'SINGLE', language: 'CPP', standard: 'C++17', pastePolicy: 'ALLOW',
  aiEnabled: false, policy: { moodle_metadata_read_only: true },
  availabilityRules: [{ id: 'rule-1', targetType: 'GROUP', targetExternalId: 'group-1', allowed: true }],
};

beforeEach(() => {
  vi.clearAllMocks();
  mocks.getAssessment.mockResolvedValue(assessment);
  mocks.getAssessmentPublicationTargets.mockResolvedValue({
    groups: [{ id: 'local-group', externalId: 'group-1', name: 'Тестовая группа' }],
    principals: [], overridesConfirmed: true,
  });
  mocks.publishAssessment.mockImplementation(async (_id, _groups, aiEnabled) => ({ ...assessment, aiEnabled }));
});
afterEach(cleanup);

describe('per-assessment student AI access', () => {
  it.each([false, true])('lets the teacher change the current %s policy without editing Moodle metadata', async (initial) => {
    mocks.getAssessment.mockResolvedValue({ ...assessment, aiEnabled: initial });
    render(<MemoryRouter initialEntries={['/assessments/assessment-1']}><ToastProvider><Routes>
      <Route path="/assessments/:assessmentId" element={<AssessmentIntroPage />} />
    </Routes></ToastProvider></MemoryRouter>);

    fireEvent.click(await screen.findByRole('button', { name: 'Изменить доступ' }));
    await screen.findByText('Тестовая группа');
    const checkbox = screen.getByRole('checkbox', { name: /Разрешить учебного ИИ-помощника/ });
    expect(checkbox).toHaveProperty('checked', initial);
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить доступ' }));
    await waitFor(() => expect(mocks.publishAssessment).toHaveBeenCalledWith('assessment-1', ['local-group'], !initial));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });
});
