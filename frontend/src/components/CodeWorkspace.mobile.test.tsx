import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ThemeProvider } from '../context/ThemeContext';
import type { WorkspaceFile } from '../types';
import { CodeWorkspace } from './CodeWorkspace';

const editor = vi.hoisted(() => vi.fn());
vi.mock('./MonacoEditors', () => ({ MonacoEditor: (props: { value: string; options: { readOnly: boolean } }) => {
  editor(props);
  return <textarea aria-label="Desktop editor" readOnly={props.options.readOnly} value={props.value} onChange={() => {}} />;
} }));
vi.mock('../lib/monacoSetup', () => ({ monaco: { editor: {
  colorize: vi.fn().mockResolvedValue(''), tokenize: () => [],
} } }));
const files: WorkspaceFile[] = [{ id: 'main', path: 'main.cpp', content: 'int main() {\n  return 0;\n}', language: 'cpp' }];
beforeEach(() => { editor.mockClear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('never mounts a Monaco input on a phone and tapping a finding only opens its description', async () => {
  vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  const change = vi.fn(); const ask = vi.fn();
  const { container } = render(<ThemeProvider><CodeWorkspace files={files} activeFileId="main" onActiveFile={vi.fn()} onChange={change} scopeId="submission"
    aiFindings={[{ path: 'main.cpp', line: 2, severity: 'warning', message: 'Проверьте результат.' }]} onAskAi={ask} onAskAiSelection={ask} /></ThemeProvider>);
  expect(screen.getByRole('region', { name: 'Код main.cpp, только просмотр' })).toBeInTheDocument();
  expect(editor).not.toHaveBeenCalled();
  expect(container.querySelector('textarea, input, [contenteditable]')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Замечания ИИ, строка 2' }));
  expect(within(screen.getByRole('dialog')).getByText('Проверьте результат.')).toBeInTheDocument();
  expect(change).not.toHaveBeenCalled(); expect(ask).not.toHaveBeenCalled();
  await act(async () => {});
});

it('restores the desktop editor on resize without changing the source', async () => {
  const listeners = new Set<() => void>();
  const media = { matches: true, addEventListener: (_type: string, listener: () => void) => listeners.add(listener), removeEventListener: (_type: string, listener: () => void) => listeners.delete(listener) };
  vi.stubGlobal('matchMedia', () => media);
  const change = vi.fn();
  render(<ThemeProvider><CodeWorkspace files={files} activeFileId="main" onActiveFile={vi.fn()} onChange={change} scopeId="attempt" /></ThemeProvider>);
  await act(async () => {});
  expect(editor).not.toHaveBeenCalled();
  act(() => { media.matches = false; listeners.forEach((listener) => listener()); });
  expect(screen.queryByRole('region', { name: /только просмотр/ })).not.toBeInTheDocument();
  expect(screen.getByLabelText('Desktop editor')).toHaveValue(files[0].content);
  expect(screen.getByLabelText('Desktop editor')).not.toHaveAttribute('readonly');
  expect(change).not.toHaveBeenCalled();
});

it('does not offer file creation for an empty read-only phone workspace', () => {
  vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  const create = vi.fn();
  render(<ThemeProvider><CodeWorkspace files={[]} activeFileId="" onActiveFile={vi.fn()} onChange={vi.fn()} onCreateFile={create} scopeId="empty" /></ThemeProvider>);
  expect(screen.getByText('В рабочей области пока нет файлов')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Создать файл' })).not.toBeInTheDocument();
  expect(create).not.toHaveBeenCalled();
  expect(editor).not.toHaveBeenCalled();
});
