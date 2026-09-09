import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { forwardRef, useImperativeHandle } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { SimilarityComparison } from '../types';
import { SimilarityComparisonPage } from './SimilarityComparisonPage';

const mocks = vi.hoisted(() => ({
  getSimilarityComparison: vi.fn(),
  currentOpenRange: vi.fn(),
  peerOpenRange: vi.fn(),
}));

vi.mock('../lib/api', () => ({ api: { getSimilarityComparison: mocks.getSimilarityComparison } }));
vi.mock('../components/CodeWorkspace', () => ({
  CodeWorkspace: forwardRef((props: { scopeId: string; activeFileId: string; highlights: unknown[] }, ref) => {
    const openRange = props.scopeId.includes('-current-') ? mocks.currentOpenRange : mocks.peerOpenRange;
    useImperativeHandle(ref, () => ({ openRange, openDiagnostic: vi.fn(), focus: vi.fn() }));
    return <div data-testid={props.scopeId.includes('-current-') ? 'current-code' : 'peer-code'} data-active-file={props.activeFileId} data-highlights={props.highlights.length} />;
  }),
}));

const comparison: SimilarityComparison = {
  match: {
    id: 'match-1', submissionA: 'left', submissionB: 'right', score: 0.75,
    fingerprintCountA: 20, fingerprintCountB: 18, sharedFingerprintCount: 12,
    evidence: [{
      fileA: 'left.cpp', startLineA: 2, endLineA: 4,
      fileB: 'src/right.cpp', startLineB: 8, endLineB: 10,
      tokenCount: 21, excerptA: 'left', excerptB: 'right',
    }],
  },
  assessmentId: 'assessment-1', assessmentTitle: 'Самостоятельная работа',
  left: {
    submissionId: 'left', studentName: 'Анна Л.', studentGroup: '1.1', submittedAt: '2026-08-25T10:00:00Z',
    files: [{ id: 'left-main', path: 'main.cpp', content: '', language: 'cpp' }, { id: 'left-match', path: 'left.cpp', content: '', language: 'cpp' }],
  },
  right: {
    submissionId: 'right', studentName: 'Борис П.', studentGroup: '1.2', submittedAt: '2026-08-25T10:01:00Z',
    files: [{ id: 'right-main', path: 'main.cpp', content: '', language: 'cpp' }, { id: 'right-match', path: 'src/right.cpp', content: '', language: 'cpp' }],
  },
};

beforeEach(() => {
  mocks.getSimilarityComparison.mockReset().mockResolvedValue(comparison);
  mocks.currentOpenRange.mockReset();
  mocks.peerOpenRange.mockReset();
});
afterEach(cleanup);

describe('plagiarism comparison', () => {
  it('orients evidence around the opened submission and immediately reveals the first matching ranges', async () => {
    render(<MemoryRouter initialEntries={['/review/right/similarity/match-1']}><Routes>
      <Route path="/review/:submissionId/similarity/:matchId" element={<SimilarityComparisonPage />} />
    </Routes></MemoryRouter>);

    expect(await screen.findByText('Борис П.')).toBeInTheDocument();
    expect(screen.getByText('Анна Л.')).toBeInTheDocument();
    expect(screen.getByText('Это предварительная проверка, а не конечное решение.')).toBeInTheDocument();
    expect(screen.queryByText(/свидетельство/i)).not.toBeInTheDocument();
    expect(screen.getByText('src/right.cpp:8–10')).toBeInTheDocument();
    expect(screen.getByText(/left\.cpp:2–4/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('current-code')).toHaveAttribute('data-active-file', 'right-match'));
    expect(screen.getByTestId('peer-code')).toHaveAttribute('data-active-file', 'left-match');

    await waitFor(() => expect(mocks.currentOpenRange).toHaveBeenCalledWith('src/right.cpp', 8, 10));
    expect(mocks.peerOpenRange).toHaveBeenCalledWith('left.cpp', 2, 4);

    mocks.currentOpenRange.mockClear();
    mocks.peerOpenRange.mockClear();
    fireEvent.click(screen.getByRole('listitem'));
    await waitFor(() => expect(mocks.currentOpenRange).toHaveBeenCalledWith('src/right.cpp', 8, 10));
    expect(mocks.peerOpenRange).toHaveBeenCalledWith('left.cpp', 2, 4);
  });
});
