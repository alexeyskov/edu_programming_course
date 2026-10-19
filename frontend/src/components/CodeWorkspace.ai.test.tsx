import { act, render } from '@testing-library/react';
import { useEffect, useLayoutEffect } from 'react';
import type * as Monaco from 'monaco-editor';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ThemeProvider } from '../context/ThemeContext';
import type { AiCodeFinding, WorkspaceFile } from '../types';
import { CodeWorkspace } from './CodeWorkspace';

const mock = vi.hoisted(() => {
  interface Model {
    uri: { toString(): string };
    getLineCount(): number;
    getLineMaxColumn(line: number): number;
    isDisposed(): boolean;
  }
  const models = new Map<string, Model>();
  const modelListeners = new Set<() => void>();
  const disposeListeners = new Set<() => void>();
  const collections: { items: unknown[]; clear: ReturnType<typeof vi.fn>; set(items: unknown[]): void }[] = [];
  const mount = { deferred: false, pending: undefined as (() => void) | undefined };
  let current: Model | null = null;
  const editor = {
    addAction: vi.fn((_action: Monaco.editor.IActionDescriptor) => ({ dispose: vi.fn() })),
    getAction: (id: string) => ({ id: `editor-test:${id}` }),
    getSelection: () => null,
    onDidChangeCursorSelection: () => ({ dispose: vi.fn() }),
    getModel: () => current,
    createDecorationsCollection: (items: unknown[]) => {
      const collection = { items, clear: vi.fn(() => { collection.items = []; }), set: (next: unknown[]) => { collection.items = next; } };
      collections.push(collection);
      return collection;
    },
    onDidChangeModel: (listener: () => void) => {
      modelListeners.add(listener);
      return { dispose: () => modelListeners.delete(listener) };
    },
    onDidDispose: (listener: () => void) => {
      disposeListeners.add(listener);
      return { dispose: () => disposeListeners.delete(listener) };
    },
  };
  const monaco = {
    Uri: { parse: (value: string) => ({ toString: () => value }) },
    editor: {
      getModel: (uri: { toString(): string }) => models.get(uri.toString()),
      setModelMarkers: vi.fn((_model: Model, _owner: string, _markers: unknown[]) => undefined),
    },
    languages: { registerCompletionItemProvider: () => ({ dispose: vi.fn() }) },
  };
  function switchModel(path: string, content: string) {
    if (!models.has(path)) models.set(path, {
      uri: { toString: () => path },
      getLineCount: () => content.split('\n').length,
      getLineMaxColumn: (line) => (content.split('\n')[line - 1]?.length ?? 0) + 1,
      isDisposed: () => false,
    });
    current = models.get(path)!;
    modelListeners.forEach((listener) => listener());
  }
  return { models, collections, monaco, editor, switchModel, disposeListeners, modelListeners, mount };
});

vi.mock('./MonacoEditors', () => ({
  MonacoEditor: ({ path, value, onMount }: { path: string; value: string; onMount: (editor: typeof mock.editor, monaco: typeof mock.monaco) => void }) => {
    useLayoutEffect(() => { mock.switchModel(path, value); }, [path, value]);
    useEffect(() => {
      // @monaco-editor/react keeps the initial onMount callback while loading.
      const mount = () => onMount(mock.editor, mock.monaco);
      if (mock.mount.deferred) mock.mount.pending = mount;
      else mount();
      return () => { mock.mount.pending = undefined; mock.disposeListeners.forEach((listener) => listener()); };
    }, []);
    return <div data-testid="editor" />;
  },
}));
vi.mock('../lib/editorClipboard', () => ({ installEditorClipboardGuard: () => vi.fn() }));

const files: WorkspaceFile[] = [
  { id: 'main', path: 'main.cpp', content: 'int main() {\n  return 0;\n}', language: 'cpp' },
  { id: 'header', path: 'array.h', content: '#pragma once\nvoid check();', language: 'cpp' },
];
const finding: AiCodeFinding = { path: 'main.cpp', line: 2, severity: 'warning', message: 'Проверьте границы массива.' };
function workspace(findings: AiCodeFinding[], activeFileId = 'main', scopeId = 'submission-1', onAskAi?: (item: AiCodeFinding) => void, mode?: 'sandbox' | 'editable') {
  return <ThemeProvider><CodeWorkspace files={files} activeFileId={activeFileId} scopeId={scopeId}
    onActiveFile={() => undefined} onChange={() => undefined} aiFindings={findings} onAskAi={onAskAi}
    diagnostics={[{ id: 'compile-error', path: 'main.cpp', line: 1, severity: 'error', message: 'Compiler error' }]} readOnly={mode !== 'editable'} experiment={mode === 'sandbox'} />
  </ThemeProvider>;
}
function aiMarkers(path = 'inmemory:///submission-1/main.cpp') {
  return mock.monaco.editor.setModelMarkers.mock.calls.filter(([model, owner]) => model.uri.toString() === path && owner.startsWith('ai-review-')).at(-1)?.[2];
}

beforeEach(() => {
  mock.models.clear();
  mock.collections.length = 0;
  mock.modelListeners.clear();
  mock.disposeListeners.clear();
  mock.monaco.editor.setModelMarkers.mockClear();
  mock.editor.addAction.mockClear();
  mock.mount.deferred = false;
  mock.mount.pending = undefined;
});

describe('CodeWorkspace AI recommendations', () => {
  it('applies a saved report that arrives before the editor finishes loading', () => {
    mock.mount.deferred = true;
    const onAskAi = vi.fn();
    const view = render(workspace([], 'main', 'submission-1', onAskAi));
    view.rerender(workspace([finding], 'main', 'submission-1', onAskAi));
    expect(aiMarkers()).toBeUndefined();

    expect(mock.mount.pending).toBeTypeOf('function');
    act(() => mock.mount.pending!());
    expect(aiMarkers()).toEqual([expect.objectContaining({ message: finding.message })]);
    expect(mock.collections.at(-1)?.items).toHaveLength(1);
    expect(onAskAi).not.toHaveBeenCalled();
  });

  it('does not restore highlights switched off before the editor finishes loading', () => {
    mock.mount.deferred = true;
    const view = render(workspace([finding]));
    view.rerender(workspace([]));
    act(() => mock.mount.pending!());
    expect(aiMarkers() ?? []).toEqual([]);
  });

  it('uses the latest file and submission if they change before editor readiness', () => {
    mock.mount.deferred = true;
    const view = render(workspace([finding]));
    const latest = { ...finding, path: 'array.h', line: 1, message: 'Замечание к другому файлу новой сдачи.' };
    view.rerender(workspace([latest], 'header', 'submission-2'));
    act(() => mock.mount.pending!());
    expect(aiMarkers('inmemory:///submission-2/array.h')).toEqual([expect.objectContaining({ message: latest.message })]);
    expect(aiMarkers()).toBeUndefined();
  });

  it('applies newly completed findings immediately to an already mounted editor', () => {
    const view = render(workspace([]));
    view.rerender(workspace([finding]));
    expect(aiMarkers()).toEqual([expect.objectContaining({ message: finding.message })]);
  });

  it('shows advisory text only in the plain-text marker, without a duplicate decoration hover', () => {
    const message = '[открыть](command:run) <script>alert(1)</script> **текст**';
    render(workspace([{ ...finding, message }]));
    expect(aiMarkers()).toEqual([expect.objectContaining({ startLineNumber: 2, source: 'Ответ ИИ', message })]);
    expect(mock.monaco.editor.setModelMarkers).toHaveBeenCalledWith(expect.anything(), 'compiler', [expect.objectContaining({ message: 'Compiler error' })]);
    expect(mock.collections.at(-1)?.items).toEqual([expect.objectContaining({ options: expect.objectContaining({
      className: 'ai-review-line',
      hoverMessage: undefined,
    }) })]);
  });

  it('ignores unknown paths and invalid line ranges rather than attaching them elsewhere', () => {
    render(workspace([
      finding,
      { ...finding, path: '../main.cpp' },
      { ...finding, line: 0 },
      { ...finding, line: 4 },
      { ...finding, line: 1.5 },
      { ...finding, endLine: 1 },
      { ...finding, endLine: 4 },
      { ...finding, message: '  ' },
    ]));
    expect(aiMarkers()).toHaveLength(1);
  });

  it('only trusts the draft action and revalidates its scope, file and finding on click', () => {
    const onAskAi = vi.fn();
    const view = render(workspace([finding], 'main', 'submission-1', onAskAi));
    const action = mock.editor.addAction.mock.calls[0][0];
    const decoration = mock.collections.at(-1)?.items[0] as { options: { hoverMessage: Monaco.IMarkdownString } };
    const hover = decoration.options.hoverMessage;
    const registeredCommand = `editor-test:${action.id}`;
    expect(hover.isTrusted).toEqual({ enabledCommands: [registeredCommand] });
    expect(hover.supportHtml).toBe(false);
    expect(hover.value).toMatch(/^\[Спросить у ИИ\]\(command:/);
    expect(hover.value).not.toContain(finding.message);
    expect(aiMarkers()).toEqual([expect.objectContaining({ message: finding.message, source: 'Ответ ИИ' })]);
    const link = hover.value.match(/\[Спросить у ИИ\]\(command:([^?]+)\?(.+)\)$/)!;
    expect(link[1]).toBe(registeredCommand);
    const args = JSON.parse(decodeURIComponent(link[2])) as unknown[];
    expect(onAskAi).not.toHaveBeenCalled();
    const editor = mock.editor as unknown as Monaco.editor.IStandaloneCodeEditor;
    act(() => { void action.run(editor, ...args); });
    expect(onAskAi).toHaveBeenCalledExactlyOnceWith(finding);
    // A stale hover or forged parameters must not target another student's code.
    act(() => { void action.run(editor, 'other-submission', ...args.slice(1)); });
    act(() => { void action.run(editor, args[0], 'array.h', ...args.slice(2)); });
    act(() => { void action.run(editor, args[0], args[1], 999, args[3]); });
    view.rerender(workspace([{ ...finding, message: 'Обновлённое замечание' }], 'main', 'submission-1', onAskAi));
    act(() => { void action.run(editor, ...args); });
    view.rerender(workspace([finding], 'header', 'submission-1', onAskAi));
    act(() => { void action.run(editor, ...args); });
    expect(onAskAi).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(mock.editor.addAction.mock.results[0].value.dispose).toHaveBeenCalledTimes(1);
  });

  it('does not register AI actions for student callers, editing or sandbox mode', () => {
    const onAskAi = vi.fn();
    const view = render(workspace([finding]));
    expect(mock.editor.addAction).not.toHaveBeenCalled();
    view.rerender(workspace([finding], 'main', 'submission-1', onAskAi, 'editable'));
    view.rerender(workspace([finding], 'main', 'submission-1', onAskAi, 'sandbox'));
    expect(mock.editor.addAction).not.toHaveBeenCalled();
    view.rerender(workspace([finding], 'main', 'submission-1', onAskAi));
    expect(mock.editor.addAction).toHaveBeenCalledTimes(1);
    view.rerender(workspace([finding], 'main', 'submission-1', onAskAi, 'sandbox'));
    expect(mock.editor.addAction.mock.results[0].value.dispose).toHaveBeenCalledTimes(1);
  });

  it('clears annotations when disabled without touching compiler diagnostics', () => {
    const view = render(workspace([finding]));
    const decorations = mock.collections.at(-1)!;
    mock.monaco.editor.setModelMarkers.mockClear();
    view.rerender(workspace([]));
    expect(aiMarkers()).toEqual([]);
    expect(decorations.clear).toHaveBeenCalled();
    expect(mock.monaco.editor.setModelMarkers.mock.calls.filter(([, owner]) => owner === 'compiler')).toEqual([
      [expect.anything(), 'compiler', [expect.objectContaining({ message: 'Compiler error' })]],
    ]);
  });

  it('cleans the previous file and scope and restores annotations on model changes', () => {
    const findings = [finding, { ...finding, path: 'array.h', line: 1 }];
    const view = render(workspace(findings));
    view.rerender(workspace(findings, 'header'));
    expect(aiMarkers()).toEqual([]);
    expect(aiMarkers('inmemory:///submission-1/array.h')).toHaveLength(1);
    view.rerender(workspace(findings, 'header', 'submission-2'));
    expect(aiMarkers('inmemory:///submission-1/array.h')).toEqual([]);
    expect(aiMarkers('inmemory:///submission-2/array.h')).toHaveLength(1);
    act(() => mock.switchModel('inmemory:///other-submission/main.cpp', files[0].content));
    expect(aiMarkers('inmemory:///submission-2/array.h')).toEqual([]);
    expect(aiMarkers('inmemory:///other-submission/main.cpp')).toBeUndefined();
    act(() => mock.switchModel('inmemory:///submission-2/array.h', files[1].content));
    expect(aiMarkers('inmemory:///submission-2/array.h')).toHaveLength(1);
    const decorations = mock.collections.at(-1)!;
    view.unmount();
    expect(aiMarkers('inmemory:///submission-2/array.h')).toEqual([]);
    expect(decorations.clear).toHaveBeenCalled();
    expect(mock.modelListeners.size).toBe(0);
  });
});
