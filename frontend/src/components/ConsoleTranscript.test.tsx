import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { ConsoleTranscript } from './ConsoleTranscript';

afterEach(cleanup);

describe('console transcript', () => {
  it('interleaves input with output, preserving partial lines and repeated input positions', () => {
    const { container } = render(<ConsoleTranscript sessionId="run" stdout={'size>values>done\n'} inputs={[
      { sessionId: 'run', stdoutOffset: 5, text: '2' },
      { sessionId: 'run', stdoutOffset: 12, text: '1' },
      { sessionId: 'run', stdoutOffset: 12, text: '2' },
    ]} />);
    expect([...container.querySelectorAll('pre')].map((entry) => entry.textContent)).toEqual([
      'size>', '› 2', 'values>', '› 1', '› 2', 'done',
    ]);
    expect(screen.getAllByLabelText('Введённые данные')).toHaveLength(3);
  });

  it('removes empty boundary rows around input while preserving spacing inside output', () => {
    const first = 'Введите размер массива\r\n';
    const second = '\r\n\r\nВведите элементы массива\r\n';
    const last = '\nИзначальный массив: 1 2 3\nИтоговый массив: 1 2 3\n\n  Продолжение\n';
    const { container } = render(<ConsoleTranscript sessionId="run" stdout={first + second + last} inputs={[
      { sessionId: 'run', stdoutOffset: first.length, text: '3' },
      { sessionId: 'run', stdoutOffset: first.length + second.length, text: '1 2 3' },
    ]} />);
    expect([...container.querySelectorAll('pre')].map((entry) => entry.textContent)).toEqual([
      'Введите размер массива', '› 3', 'Введите элементы массива', '› 1 2 3',
      'Изначальный массив: 1 2 3\nИтоговый массив: 1 2 3\n\n  Продолжение',
    ]);
  });

  it('preserves original output whitespace when there is no input to interleave', () => {
    const stdout = '\n  Первая строка\n\nВторая строка\n';
    const { container } = render(<ConsoleTranscript sessionId="run" stdout={stdout} inputs={[]} />);
    expect(container.querySelector('pre')?.textContent).toBe(stdout);
  });

  it('does not display input from another program session', () => {
    render(<ConsoleTranscript sessionId="new" stdout="ready" inputs={[
      { sessionId: 'old', stdoutOffset: 0, text: 'previous input' },
    ]} />);
    expect(screen.getByText('ready')).toBeVisible();
    expect(screen.queryByLabelText('Введённые данные')).not.toBeInTheDocument();
  });

  it('renders blank lines, Unicode and markup safely as input text', () => {
    const { container } = render(<ConsoleTranscript sessionId="run" stdout="Число: " inputs={[
      { sessionId: 'run', stdoutOffset: 7, text: '' },
      { sessionId: 'run', stdoutOffset: 7, text: '<script>привет()</script>' },
    ]} />);
    expect(screen.getByText('› ↵')).toBeVisible();
    expect(screen.getByText('› <script>привет()</script>')).toBeVisible();
    expect(container.querySelector('script')).toBeNull();
  });
});
