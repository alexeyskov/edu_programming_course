import { describe, expect, it } from 'vitest';
import type { InteractiveRun } from '../types';
import { mergeInteractiveRunSnapshot } from './interactiveRun';

const run: InteractiveRun = {
  sessionId: 'run-1', status: 'RUNNING', terminal: false, durationMs: 200,
  stdout: 'Число: Следующее число: ', stderr: 'warning\n', inputClosed: true,
  outputTruncated: true, diagnostics: [],
};

describe('monotonic interactive program snapshots', () => {
  it('retains stdout, stderr and elapsed time when an old GET follows a newer stdin POST', () => {
    const merged = mergeInteractiveRunSnapshot(run, { ...run, stdout: 'Число: ', stderr: '', durationMs: 100, inputClosed: false, outputTruncated: false });
    expect(merged).toEqual(run);
  });

  it('accepts new output and terminal results even if their last output snapshot is older', () => {
    const merged = mergeInteractiveRunSnapshot(run, { ...run, status: 'SUCCESS', terminal: true, stdout: 'Число: ', stderr: 'warning\nmessage\n', durationMs: 300 });
    expect(merged).toMatchObject({ status: 'SUCCESS', terminal: true, stdout: run.stdout, stderr: 'warning\nmessage\n', durationMs: 300 });
  });

  it('never resurrects a finished process or accepts another session', () => {
    const terminal = { ...run, status: 'STOPPED' as const, terminal: true };
    expect(mergeInteractiveRunSnapshot(terminal, run)).toBe(terminal);
    expect(mergeInteractiveRunSnapshot(run, { ...run, sessionId: 'other', stdout: 'Other code' })).toBe(run);
  });
});
