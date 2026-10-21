import type { InteractiveRun } from '../types';

/** Poll and stdin responses race; cumulative output and final state cannot go backwards. */
export function mergeInteractiveRunSnapshot(current: InteractiveRun, received: InteractiveRun): InteractiveRun {
  if (current.sessionId !== received.sessionId || (current.terminal && !received.terminal)) return current;
  return {
    ...received,
    stdout: current.stdout.length > received.stdout.length ? current.stdout : received.stdout,
    stderr: current.stderr.length > received.stderr.length ? current.stderr : received.stderr,
    outputTruncated: current.outputTruncated || received.outputTruncated,
    inputClosed: current.inputClosed || received.inputClosed,
    durationMs: Math.max(current.durationMs, received.durationMs),
  };
}
