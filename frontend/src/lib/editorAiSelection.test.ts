import type * as Monaco from 'monaco-editor';
import { describe, expect, it, vi } from 'vitest';
import { installAiSelectionAction } from './editorAiSelection';

function setup(overrides = {}, modelPath = 'main.cpp') {
  let selected = true;
  const config = { scopeId: 'sub', path: 'main.cpp', readOnly: true, experiment: false, onAsk: vi.fn(), ...overrides };
  const change = new Set<() => void>();
  const models = new Set<() => void>();
  const editor = {
    getSelection: () => ({ isEmpty: () => !selected, startLineNumber: 2, endLineNumber: 3, getPosition: () => ({ lineNumber: 3, column: 5 }) }),
    getModel: () => ({ uri: { toString: () => `inmemory:/sub/${modelPath.replaceAll(' ', '%20')}` }, getValueInRange: () => 'int x = 0;\nreturn x;' }),
    addContentWidget: vi.fn(), removeContentWidget: vi.fn(), layoutContentWidget: vi.fn(),
    onDidChangeCursorSelection: (fn: () => void) => { change.add(fn); return { dispose: () => change.delete(fn) }; },
    onDidChangeModel: (fn: () => void) => { models.add(fn); return { dispose: () => models.delete(fn) }; },
  };
  const monaco = { Uri: { parse: (value: string) => ({ toString: () => value.replace('inmemory:///', 'inmemory:/').replaceAll(' ', '%20') }) } };
  const handle = installAiSelectionAction(editor as unknown as Monaco.editor.IStandaloneCodeEditor, monaco as Pick<typeof Monaco, 'Uri'>, () => config);
  return { config, editor, handle, change, models, clear: () => { selected = false; change.forEach((fn) => fn()); } };
}

describe('opt-in code selection action', () => {
  it('allows editable student code only with explicit opt-in and never starts a request on selection', () => {
    const { editor, config, handle } = setup({ readOnly: false, allowEditable: true });
    expect(editor.addContentWidget).toHaveBeenCalledTimes(1);
    expect(config.onAsk).not.toHaveBeenCalled();
    const button = editor.addContentWidget.mock.calls[0][0].getDomNode() as HTMLButtonElement;
    button.click();
    expect(config.onAsk).toHaveBeenCalledWith(expect.objectContaining({ path: 'main.cpp' }));
    config.experiment = true;
    handle.refresh();
    expect(editor.removeContentWidget).toHaveBeenCalledTimes(1);
    button.click();
    expect(config.onAsk).toHaveBeenCalledTimes(1);
    handle.dispose();
  });

  it('only prepares a draft after explicitly clicking the floating action', () => {
    const { editor, config, handle, clear, change, models } = setup();
    expect(editor.addContentWidget).toHaveBeenCalledTimes(1);
    expect(config.onAsk).not.toHaveBeenCalled();
    const button = editor.addContentWidget.mock.calls[0][0].getDomNode() as HTMLButtonElement;
    expect(button.textContent).toBe('Спросить у ИИ');
    button.click();
    expect(config.onAsk).toHaveBeenCalledWith({ path: 'main.cpp', startLine: 2, endLine: 3, text: 'int x = 0;\nreturn x;' });
    clear();
    expect(editor.removeContentWidget).toHaveBeenCalledTimes(1);
    button.click();
    expect(config.onAsk).toHaveBeenCalledTimes(1);
    handle.dispose();
    expect(change.size + models.size).toBe(0);
  });

  it.each([{ readOnly: false }, { experiment: true }, { onAsk: undefined }, { scopeId: 'other-submission' }])('is absent without editable opt-in, in a sandbox, without a callback or for another model: %j', (config) => {
    const { editor, handle } = setup(config);
    expect(editor.addContentWidget).not.toHaveBeenCalled();
    handle.dispose();
  });

  it('removes an already-visible action when entering the sandbox', () => {
    const { editor, config, handle } = setup();
    config.experiment = true;
    handle.refresh();
    expect(editor.removeContentWidget).toHaveBeenCalledTimes(1);
    handle.dispose();
  });

  it('compares normalized Monaco URIs, including paths with spaces', () => {
    const { editor, config, handle } = setup({ path: 'include/my file.h' }, 'include/my file.h');
    expect(editor.addContentWidget).toHaveBeenCalledTimes(1);
    const button = editor.addContentWidget.mock.calls[0][0].getDomNode() as HTMLButtonElement;
    button.click();
    expect(config.onAsk).toHaveBeenCalledWith(expect.objectContaining({ path: 'include/my file.h' }));
    handle.dispose();
  });
});
