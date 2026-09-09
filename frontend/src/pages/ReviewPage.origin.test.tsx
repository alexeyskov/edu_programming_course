import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { forwardRef, useImperativeHandle } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '../components/ui';
import { ThemeProvider } from '../context/ThemeContext';
import type { Submission } from '../types';
import { ReviewPage } from './ReviewPage';

const mocks = vi.hoisted(() => ({
  getSubmission: vi.fn(),
  getReviewDraft: vi.fn(),
  getEvidenceRuns: vi.fn(),
  getAuthorshipAnalyses: vi.fn(),
  getSimilarityAnalyses: vi.fn(),
  createAuthorshipAnalysis: vi.fn(),
  createSimilarityAnalysis: vi.fn(),
}));

vi.mock('../lib/api', () => ({ api: mocks }));
vi.mock('@monaco-editor/react', () => ({ DiffEditor: () => <div>Diff editor</div> }));
vi.mock('../components/CodeWorkspace', () => ({
  CodeWorkspace: forwardRef((_props, ref) => {
    useImperativeHandle(ref, () => ({ openDiagnostic: vi.fn(), focus: vi.fn() }));
    return <div>Код работы</div>;
  }),
}));

const submission: Submission = {
  id: 'submission-1',
  assessmentId: 'assessment-1',
  taskVersionId: 'task-version-7',
  courseId: 'course-1',
  assessmentTitle: 'Самостоятельная работа',
  studentName: 'Студент',
  studentGroup: '1.1',
  submittedAt: '2026-08-30T10:00:00Z',
  status: 'GRADED',
  score: 4,
  maxScore: 5,
  risk: 'UNKNOWN',
  testsPassed: 0,
  testsTotal: 0,
  reviewRequired: true,
  decisionSupportEnabled: true,
  canReview: true,
  files: [{ id: 'main', path: 'main.cpp', content: 'int main() {}', language: 'cpp' }],
  history: [],
  latestDecision: {
    id: 'decision-1', reviewerName: 'Преподаватель П.', grade: 4, comment: '', revision: 1,
    reviewedAt: '2026-08-30T10:05:00Z', lmsExportState: 'DELIVERED',
  },
  decisionHistory: [],
  originVerification: {
    state: 'VERIFIED',
    transport: 'ONLINE_TEXT',
    checkedAt: '2026-08-30T10:01:00Z',
    message: 'Ответ Moodle совпадает с отправленным через систему.',
  },
};

beforeEach(() => {
  Object.values(mocks).forEach((mock) => mock.mockReset());
  mocks.getSubmission.mockResolvedValue(submission);
  mocks.getReviewDraft.mockResolvedValue(null);
  mocks.getEvidenceRuns.mockResolvedValue([]);
  mocks.getAuthorshipAnalyses.mockResolvedValue([]);
  mocks.getSimilarityAnalyses.mockResolvedValue([]);
  mocks.createAuthorshipAnalysis.mockResolvedValue({
    id: 'authorship-1', submissionId: submission.id, state: 'PENDING', createdAt: '2026-08-30T10:02:00Z',
  });
  mocks.createSimilarityAnalysis.mockResolvedValue({
    id: 'similarity-1', assessmentId: submission.assessmentId, state: 'PENDING', comparisonCount: 0, matchCount: 0, matches: [],
  });
});

afterEach(() => cleanup());

describe('plagiarism evidence panel', () => {
  it('shows LMS origin verification and permits manual analyses for an already reviewed authorised submission', async () => {
    render(
      <MemoryRouter initialEntries={['/review/submission-1']}>
        <ThemeProvider><ToastProvider><Routes>
          <Route path="/review/:submissionId" element={<ReviewPage />} />
        </Routes></ToastProvider></ThemeProvider>
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Плагиат' }));

    expect(await screen.findByText('Ответ подтверждён')).toBeInTheDocument();
    expect(screen.getByText('Ответ Moodle совпадает с отправленным через систему.')).toBeInTheDocument();

    const authorshipCard = screen.getByText('Анализ процесса написания').closest('section');
    const similarityCard = screen.getByText('Сходство решений').closest('section');
    expect(authorshipCard).not.toBeNull();
    expect(similarityCard).not.toBeNull();

    fireEvent.click(within(authorshipCard!).getByRole('button', { name: 'Запустить' }));
    await waitFor(() => expect(mocks.createAuthorshipAnalysis).toHaveBeenCalledWith('submission-1'));

    fireEvent.click(within(similarityCard!).getByRole('button', { name: 'Запустить' }));
    await waitFor(() => expect(mocks.createSimilarityAnalysis).toHaveBeenCalledWith('assessment-1', 'task-version-7'));
  });
});
