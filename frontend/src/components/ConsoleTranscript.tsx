import { Fragment } from 'react';

export interface ConsoleInput {
  sessionId: string;
  stdoutOffset: number;
  text: string;
}

/** Keep acknowledged input beside the output that was visible when it was sent. */
export function ConsoleTranscript({ sessionId, stdout, inputs }: {
  sessionId: string;
  stdout: string;
  inputs: ConsoleInput[];
}) {
  const sessionInputs = inputs.filter((input) => input.sessionId === sessionId);
  if (!stdout && !sessionInputs.length) return null;
  // Each input/output block already starts on a new visual line. Boundary
  // newlines would add empty rows, unlike newlines inside a block.
  const compactBoundary = (text: string) => text.replace(/^[\r\n]+|[\r\n]+$/g, '');
  let offset = 0;
  const entries = sessionInputs.map((input, index) => {
    const end = Math.min(stdout.length, Math.max(offset, input.stdoutOffset));
    const output = compactBoundary(stdout.slice(offset, end));
    offset = end;
    return <Fragment key={index}>
      {output && <pre className="is-stdout">{output}</pre>}
      <pre className="is-input" aria-label="Введённые данные" title="Ввод в программу">› {input.text || '↵'}</pre>
    </Fragment>;
  });
  const remaining = sessionInputs.length ? compactBoundary(stdout.slice(offset)) : stdout;
  return <div className="console-transcript">{entries}{remaining && <pre className="is-stdout">{remaining}</pre>}</div>;
}
