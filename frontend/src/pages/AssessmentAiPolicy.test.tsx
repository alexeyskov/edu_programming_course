import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '../components/ui';
import type { Assessment } from '../types';
import { AssessmentIntroPage } from './PlaceholderPages';

const mocks = vi.hoisted(() => ({
  getAssessment: vi.fn(), getAssessmentPublicationTargets: vi.fn(), publishAssessment: vi.fn(),
  setAssessmentAiReviewPolicy: vi.fn(),
}));
const auth = vi.hoisted(() => ({ primaryRole: 'TEACHER', capabilities: [] as string[] }));

vi.mock('../lib/api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../lib/api')>(), api: mocks,
}));
vi.mock('../context/AuthContext', () => ({ useAuth: () => ({ primaryRole: auth.primaryRole, session: { capabilities: auth.capabilities } }) }));

const assessment: Assessment = {
  id: 'assessment-1', courseId: 'course-1', title: 'Лабораторная №1', summary: '',
  kind: 'LAB', status: 'AVAILABLE', publicationStatus: 'PUBLISHED', maxScore: 5,
  fileMode: 'SINGLE', language: 'CPP', standard: 'C++17', pastePolicy: 'ALLOW',
  aiEnabled: false, policy: { moodle_metadata_read_only: true },
  availabilityRules: [{ id: 'rule-1', targetType: 'GROUP', targetExternalId: 'group-1', allowed: true }],
};

beforeEach(() => {
  vi.clearAllMocks();
  auth.primaryRole = 'TEACHER';
  auth.capabilities = [];
  mocks.getAssessment.mockResolvedValue(assessment);
  mocks.getAssessmentPublicationTargets.mockResolvedValue({
    groups: [{ id: 'local-group', externalId: 'group-1', name: 'Тестовая группа' }],
    principals: [], overridesConfirmed: true,
  });
  mocks.publishAssessment.mockImplementation(async (_id, _groups, aiEnabled) => ({ ...assessment, aiEnabled }));
  mocks.setAssessmentAiReviewPolicy.mockImplementation(async (_id, aiReviewEnabled) => ({ ...assessment, aiReviewEnabled }));
});
afterEach(cleanup);

function renderAssessment() {
  render(<MemoryRouter initialEntries={['/assessments/assessment-1']}><ToastProvider><Routes>
    <Route path="/assessments/:assessmentId" element={<AssessmentIntroPage />} />
  </Routes></ToastProvider></MemoryRouter>);
}

describe('Moodle group access', () => {
  it('saves an explicit empty group list when the last group is unchecked', async () => {
    renderAssessment();
    fireEvent.click(await screen.findByRole('button', { name: 'Изменить доступ' }));
    const group = await screen.findByRole('checkbox', { name: /Тестовая группа/ });
    expect(group).toBeChecked();
    fireEvent.click(group);
    const save = screen.getByRole('button', { name: 'Сохранить доступ' });
    expect(save).toBeEnabled();
    fireEvent.click(save);
    await waitFor(() => expect(mocks.publishAssessment).toHaveBeenCalledWith('assessment-1', [], false));
  });

  it('does not mistake a failed group request for an empty selection', async () => {
    mocks.getAssessmentPublicationTargets.mockRejectedValue(new Error('Группы не загружены'));
    renderAssessment();
    fireEvent.click(await screen.findByRole('button', { name: 'Изменить доступ' }));
    await screen.findByText('Группы не загружены');
    const save = screen.getByRole('button', { name: 'Сохранить доступ' });
    expect(save).toBeDisabled();
    fireEvent.click(save);
    expect(mocks.publishAssessment).not.toHaveBeenCalled();
  });

  it('still requires a selected group when initially opening a draft', async () => {
    mocks.getAssessment.mockResolvedValue({ ...assessment, publicationStatus: 'DRAFT', availabilityRules: [] });
    renderAssessment();
    fireEvent.click(await screen.findByRole('button', { name: 'Настроить доступ' }));
    const group = await screen.findByRole('checkbox', { name: /Тестовая группа/ });
    expect(screen.getByRole('button', { name: 'Открыть работу' })).toBeDisabled();
    fireEvent.click(group);
    expect(screen.getByRole('button', { name: 'Открыть работу' })).toBeEnabled();
  });
});

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

describe('per-assessment automatic AI review', () => {
  it('is disabled by default for an administrator and explains the separate recommendation policy', async () => {
    auth.capabilities = ['SYSTEM_SETTINGS'];
    renderAssessment();
    expect(await screen.findByRole('checkbox', { name: 'Автоматическая ИИ-проверка' })).not.toBeChecked();
    expect(screen.getByText(/но не выставляет оценку/)).toBeInTheDocument();
    expect(screen.getByText(/сразу после сдачи студентом, а для ответов из Moodle — после синхронизации/)).toBeInTheDocument();
    expect(screen.getByText(/уже сданные работы без итоговой оценки/)).toBeInTheDocument();
    expect(screen.getByText(/Для анализа ИИ должен быть включён и настроен/)).toBeInTheDocument();
    expect(mocks.setAssessmentAiReviewPolicy).not.toHaveBeenCalled();
  });

  it.each([false, true])('lets an administrator save a change from %s independently of student access', async (initial) => {
    auth.capabilities = ['SYSTEM_SETTINGS'];
    mocks.getAssessment.mockResolvedValue({ ...assessment, aiReviewEnabled: initial });
    renderAssessment();
    const checkbox = await screen.findByRole('checkbox', { name: 'Автоматическая ИИ-проверка' });
    expect(checkbox).toHaveProperty('checked', initial);
    fireEvent.click(checkbox);
    await waitFor(() => expect(mocks.setAssessmentAiReviewPolicy).toHaveBeenCalledWith('assessment-1', !initial));
    expect(await screen.findByText('Настройка сохранена')).toBeInTheDocument();
    expect(checkbox).toHaveProperty('checked', !initial);
    expect(mocks.publishAssessment).not.toHaveBeenCalled();
    expect(mocks.getAssessmentPublicationTargets).not.toHaveBeenCalled();

    mocks.getAssessment.mockResolvedValue({ ...assessment, aiReviewEnabled: !initial });
    cleanup();
    renderAssessment();
    expect(await screen.findByRole('checkbox', { name: 'Автоматическая ИИ-проверка' })).toHaveProperty('checked', !initial);
  });

  it('prevents duplicate writes while the setting is being saved', async () => {
    auth.capabilities = ['SYSTEM_SETTINGS'];
    let resolveSave!: (value: Assessment) => void;
    mocks.setAssessmentAiReviewPolicy.mockReturnValue(new Promise<Assessment>((resolve) => { resolveSave = resolve; }));
    renderAssessment();
    const checkbox = await screen.findByRole('checkbox', { name: 'Автоматическая ИИ-проверка' });
    fireEvent.click(checkbox);
    expect(checkbox).toBeDisabled();
    expect(screen.getByText('Сохраняем настройку…')).toBeInTheDocument();
    fireEvent.click(checkbox);
    expect(mocks.setAssessmentAiReviewPolicy).toHaveBeenCalledTimes(1);
    resolveSave({ ...assessment, aiReviewEnabled: true });
    await waitFor(() => expect(checkbox).toBeEnabled());
    expect(checkbox).toBeChecked();
  });

  it.each([false, true])('restores the saved %s policy and shows an error if saving fails', async (initial) => {
    auth.capabilities = ['SYSTEM_SETTINGS'];
    mocks.getAssessment.mockResolvedValue({ ...assessment, aiReviewEnabled: initial });
    mocks.setAssessmentAiReviewPolicy.mockRejectedValue(new Error('Настройка недоступна'));
    renderAssessment();
    const checkbox = await screen.findByRole('checkbox', { name: 'Автоматическая ИИ-проверка' });
    fireEvent.click(checkbox);
    expect(await screen.findByText('Настройка недоступна')).toBeInTheDocument();
    expect(checkbox).toHaveProperty('checked', initial);
    expect(checkbox).toBeEnabled();
    expect(screen.queryByText('Настройка сохранена')).not.toBeInTheDocument();
  });

  it.each(['TEACHER', 'STUDENT'])('does not expose the policy to an ordinary %s', async (role) => {
    auth.primaryRole = role;
    mocks.getAssessment.mockResolvedValue({ ...assessment, aiReviewEnabled: true });
    renderAssessment();
    await screen.findByRole('heading', { name: assessment.title });
    expect(screen.queryByRole('checkbox', { name: 'Автоматическая ИИ-проверка' })).not.toBeInTheDocument();
    expect(mocks.setAssessmentAiReviewPolicy).not.toHaveBeenCalled();
  });

  it('allows an administrator to enable review for a closed assignment without reopening it', async () => {
    auth.capabilities = ['SYSTEM_SETTINGS'];
    const closedAssessment = { ...assessment, status: 'CLOSED', publicationStatus: 'CLOSED' } as Assessment;
    mocks.getAssessment.mockResolvedValue(closedAssessment);
    mocks.setAssessmentAiReviewPolicy.mockResolvedValue({ ...closedAssessment, aiReviewEnabled: true });
    renderAssessment();
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Автоматическая ИИ-проверка' }));
    await screen.findByText('Настройка сохранена');
    expect(screen.getByRole('checkbox', { name: 'Автоматическая ИИ-проверка' })).toBeChecked();
    expect(screen.queryByRole('button', { name: 'Изменить доступ' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Начать попытку' })).not.toBeInTheDocument();
    expect(mocks.publishAssessment).not.toHaveBeenCalled();
  });
});
