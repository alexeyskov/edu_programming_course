import type * as Monaco from 'monaco-editor';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { WorkspaceFile } from '../types';
import { installCodeIntelligence, lspRange, type CodeIntelligenceQuery, type CodeIntelligenceResponse, type CodeIntelligenceScope } from './codeIntelligence';

vi.mock('./api', () => ({ api: { queryCodeIntelligence: vi.fn() } }));

const range = { start: { line: 0, character: 0 }, end: { line: 0, character: 3 } };
const response = (result: unknown, diagnostics: CodeIntelligenceResponse['diagnostics'] = []): CodeIntelligenceResponse => ({ result, diagnostics });
const token = { isCancellationRequested: false, onCancellationRequested: () => ({ dispose() {} }) } as Monaco.CancellationToken;
const position = { lineNumber: 1, column: 4 };
const context = { triggerKind: 1 } as Monaco.languages.CompletionContext;

class Model {
  private version = 1;
  private disposed = false;
  constructor(public content: string, public uri: { toString(): string }) {}
  getValue() { return this.content; }
  setValue(content: string) { this.content = content; this.version++; }
  getVersionId() { return this.version; }
  isDisposed() { return this.disposed; }
  dispose() { this.disposed = true; }
  getLineCount() { return this.content.split('\n').length; }
  getLineContent(line: number) { return this.content.split('\n')[line - 1] ?? ''; }
  getLineMaxColumn(line: number) { return this.getLineContent(line).length + 1; }
  validateRange(value: Monaco.IRange) { return value; }
  getWordUntilPosition(position: Monaco.IPosition) {
    const word = this.getLineContent(position.lineNumber).slice(0, position.column - 1).match(/[\w]*$/)?.[0] ?? '';
    return { word, startColumn: position.column - word.length, endColumn: position.column };
  }
}

function harness(scope: CodeIntelligenceScope | undefined = { kind: 'attempt', id: 'attempt-1' }) {
  const uris = (value: string) => ({ toString: () => value.replace('inmemory:///', 'inmemory:/') });
  const models = new Map<string, Model>();
  const fileList: WorkspaceFile[] = [
    { id: 'main', path: 'main.cpp', content: 'int main() { return 0; }', language: 'cpp' },
    { id: 'header', path: 'lib/thing.h', content: 'int helper();', language: 'cpp' },
    { id: 'data', path: 'data.txt', content: 'private data not needed by clangd' },
  ];
  const config = { scopeId: 'workspace-1', scope: scope as CodeIntelligenceScope | undefined, files: fileList, readOnly: scope?.kind === 'submission', onOpenFile: vi.fn(), onStatus: vi.fn() };
  const model = new Model(fileList[0].content, uris('inmemory:///workspace-1/main.cpp'));
  models.set(model.uri.toString(), model);
  let activeModel = model;
  const contentListeners = new Set<() => void>();
  const modelListeners = new Set<() => void>();
  const providers: Record<string, Record<string, any>> = {};
  const disposables: { dispose: ReturnType<typeof vi.fn> }[] = [];
  const register = (name: string) => (language: string, provider: unknown) => {
    (providers[name] ??= {})[language] = provider;
    const disposable = { dispose: vi.fn() }; disposables.push(disposable); return disposable;
  };
  let opener: Monaco.editor.ICodeEditorOpener | undefined;
  const editor = {
    getId: () => 'test-editor', getModel: () => activeModel,
    getAction: vi.fn(() => ({ run: vi.fn() })),
    setSelection: vi.fn(), revealRangeInCenter: vi.fn(), focus: vi.fn(),
    onDidChangeModelContent: (fn: () => void) => { contentListeners.add(fn); return { dispose: () => contentListeners.delete(fn) }; },
    onDidChangeModel: (fn: () => void) => { modelListeners.add(fn); return { dispose: () => modelListeners.delete(fn) }; },
  };
  const monaco = {
    Uri: { parse: uris },
    languages: {
      CompletionItemKind: Object.fromEntries(['Text', 'Method', 'Function', 'Constructor', 'Field', 'Variable', 'Class', 'Interface', 'Module', 'Property', 'Unit', 'Value', 'Enum', 'Keyword', 'Snippet', 'Color', 'File', 'Reference', 'Folder', 'EnumMember', 'Constant', 'Struct', 'Event', 'Operator', 'TypeParameter'].map((name, index) => [name, index])),
      CompletionItemInsertTextRule: { InsertAsSnippet: 4 },
      registerCompletionItemProvider: register('completion'), registerHoverProvider: register('hover'), registerSignatureHelpProvider: register('signature'),
      registerDefinitionProvider: register('definition'), registerReferenceProvider: register('references'), registerDocumentSymbolProvider: register('symbols'), registerDocumentFormattingEditProvider: register('formatting'),
    },
    editor: {
      getModel: (uri: { toString(): string }) => models.get(uri.toString()),
      createModel: (content: string, _language: string, uri: { toString(): string }) => { const model = new Model(content, uri); models.set(uri.toString(), model); return model; },
      setModelMarkers: vi.fn(),
      registerEditorOpener: (value: Monaco.editor.ICodeEditorOpener) => { opener = value; return { dispose: vi.fn() }; },
    },
  };
  const query = vi.fn(async (_body: CodeIntelligenceQuery, _signal: AbortSignal) => response(null));
  const bridge = installCodeIntelligence(editor as unknown as Monaco.editor.IStandaloneCodeEditor, monaco as unknown as typeof Monaco, () => config, query);
  return { config, model, models, providers, editor, monaco, query, bridge, disposables, contentListeners, modelListeners,
    complete: (target = model, at = position, cancel = token) => providers.completion.cpp.provideCompletionItems(target, at, context, cancel),
    change: (content: string) => { model.setValue(content); contentListeners.forEach((fn) => fn()); },
    switchModel: (target: Model) => { activeModel = target; modelListeners.forEach((fn) => fn()); },
    open: (resource: { toString(): string }, selection = { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 4 }) => opener!.openCodeEditor(editor as unknown as Monaco.editor.ICodeEditor, resource as Monaco.Uri, selection),
  };
}
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { resolve, promise }; }
beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); });

describe('C/C++ intelligence bridge', () => {
  it('uses canonical Monaco URIs and sends current unsaved source, not auxiliary data files', async () => {
    const h = harness(); h.model.setValue('std::vector<int> values; values.');
    h.query.mockResolvedValue(response({ items: [{ label: 'push_back', kind: 2, detail: 'void push_back(int)', textEdit: { range, newText: 'push_back' }, command: { command: 'evil' } }], isIncomplete: true }));
    const result = await h.complete();
    expect(h.query).toHaveBeenCalledWith({ scope: { kind: 'attempt', id: 'attempt-1' }, method: 'completion', path: 'main.cpp', position: { line: 0, character: 3 }, context: { triggerKind: 1 }, files: [
      { path: 'main.cpp', content: h.model.content }, { path: 'lib/thing.h', content: 'int helper();' },
    ] }, expect.any(AbortSignal));
    expect(result).toMatchObject({ incomplete: true, suggestions: [{ label: 'push_back', insertText: 'push_back', kind: h.monaco.languages.CompletionItemKind.Method, detail: 'void push_back(int)' }] });
    expect(result.suggestions[0]).not.toHaveProperty('command');
    expect(h.config.onStatus).toHaveBeenCalledWith('ready');
    h.bridge.dispose();
  });

  it('isolates providers from other attempts, prefix lookalikes and non-source files', async () => {
    const h = harness();
    for (const path of ['inmemory:///workspace-2/main.cpp', 'inmemory:///workspace-10/main.cpp', 'inmemory:///workspace-1/not-known.cpp', 'inmemory:///workspace-1/data.txt']) {
      expect(await h.complete(new Model('int x;', h.monaco.Uri.parse(path)))).toEqual({ suggestions: [] });
    }
    expect(h.query).not.toHaveBeenCalled(); h.bridge.dispose();
  });

  it('falls back to local identifiers and include paths after service failure without polling repeatedly', async () => {
    const h = harness(); h.query.mockRejectedValue(new Error('service unavailable'));
    const first = await h.complete(); expect(first.suggestions.map((item: { label: string }) => item.label)).toContain('helper');
    expect(h.config.onStatus).toHaveBeenCalledWith('fallback');
    h.change('#include "lib/');
    const headers = await h.complete(h.model, { lineNumber: 1, column: h.model.content.length + 1 });
    expect(headers.suggestions).toEqual([expect.objectContaining({ label: 'lib/thing.h', range: expect.objectContaining({ startColumn: 11 }) })]);
    await vi.advanceTimersByTimeAsync(25_000); expect(h.query).toHaveBeenCalledTimes(1);
    h.query.mockResolvedValue(response([])); await vi.advanceTimersByTimeAsync(5_000);
    await h.complete(); expect(h.query).toHaveBeenCalledTimes(2); h.bridge.dispose();
  });

  it('keeps basic completions operational when semantic service is not configured', async () => {
    const h = harness(undefined); h.config.scope = undefined; // default argument provides a scope; explicitly clear it
    const result = await h.complete(); expect(result.suggestions.length).toBeGreaterThan(10); expect(h.query).not.toHaveBeenCalled(); h.bridge.dispose();
  });

  it('uses native word, keyword and file kinds without fabricated fallback descriptions', async () => {
    const h = harness(); h.query.mockRejectedValue(new Error('service unavailable'));
    const words = (await h.complete()).suggestions;
    expect(words).toContainEqual(expect.objectContaining({ label: 'helper', kind: h.monaco.languages.CompletionItemKind.Text }));
    expect(words).toContainEqual(expect.objectContaining({ label: 'continue', kind: h.monaco.languages.CompletionItemKind.Keyword }));
    for (const item of words) {
      expect(item).not.toHaveProperty('detail');
      expect(item).not.toHaveProperty('documentation');
    }
    h.change('#include "lib/');
    const headers = (await h.complete(h.model, { lineNumber: 1, column: h.model.content.length + 1 })).suggestions;
    expect(headers).toEqual([expect.objectContaining({ label: 'lib/thing.h', kind: h.monaco.languages.CompletionItemKind.File })]);
    expect(headers[0]).not.toHaveProperty('detail');
    expect(headers[0]).not.toHaveProperty('documentation');
    h.bridge.dispose();
  });

  it('sends no client files for immutable submissions and never offers mutating providers', async () => {
    const h = harness({ kind: 'submission', id: 'submission-1' });
    h.query.mockResolvedValue(response({ contents: [{ language: 'cpp', value: 'int main()' }, { kind: 'markdown', value: '[execute](command:evil) ![track](https://example.test/pixel)' }], range }));
    const hover = await h.providers.hover.cpp.provideHover(h.model, position, token);
    expect(h.query.mock.calls[0][0]).toEqual({ scope: { kind: 'submission', id: 'submission-1' }, method: 'hover', path: 'main.cpp', position: { line: 0, character: 3 } });
    expect(hover.contents[0]).toMatchObject({ isTrusted: false, supportHtml: false });
    expect(hover.contents[1].value).toBe('execute track');
    expect(await h.complete()).toEqual({ suggestions: [] });
    expect(await h.providers.formatting.cpp.provideDocumentFormattingEdits(h.model, { tabSize: 4, insertSpaces: true }, token)).toEqual([]);
    await h.bridge.format(); expect(h.editor.getAction).not.toHaveBeenCalled(); expect(h.query).toHaveBeenCalledTimes(1); h.bridge.dispose();
  });

  it('sends latest sandbox content but disables formatting on a read-only file', async () => {
    const h = harness({ kind: 'experiment', id: 'sandbox-1' });
    h.query.mockResolvedValue(response([{ range, newText: 'long' }]));
    const edits = await h.providers.formatting.cpp.provideDocumentFormattingEdits(h.model, { tabSize: 4, insertSpaces: true }, token);
    expect(edits).toEqual([{ range: lspRange(range), text: 'long' }]);
    expect(h.query.mock.calls[0][0]).toMatchObject({ scope: { kind: 'experiment', id: 'sandbox-1' }, context: { tabSize: 4, insertSpaces: true }, files: expect.any(Array) });
    h.config.files[0].readOnly = true;
    expect(await h.providers.formatting.cpp.provideDocumentFormattingEdits(h.model, { tabSize: 4, insertSpaces: true }, token)).toEqual([]);
    expect(h.query).toHaveBeenCalledTimes(1); h.bridge.dispose();
  });

  it('rejects responses after any workspace file changed, not only the current model', async () => {
    const h = harness(); const pending = deferred<CodeIntelligenceResponse>(); h.query.mockReturnValue(pending.promise);
    const answer = h.providers.hover.cpp.provideHover(h.model, position, token);
    await Promise.resolve();
    h.models.set('inmemory:/workspace-1/lib/thing.h', new Model('int helper();', h.monaco.Uri.parse('inmemory:///workspace-1/lib/thing.h')));
    h.config.files = h.config.files.map((file) => file.id === 'header' ? { ...file, content: 'double helper();' } : file);
    pending.resolve(response({ contents: 'old signature' }, [{ path: 'main.cpp', diagnostics: [{ range, message: 'old error', severity: 1 }] }]));
    expect(await answer).toBeNull(); expect(h.monaco.editor.setModelMarkers).not.toHaveBeenCalled(); h.bridge.dispose();
  });

  it('serializes different methods and drops a stale queued request before sending it', async () => {
    const h = harness(); const pending = deferred<CodeIntelligenceResponse>(); h.query.mockReturnValueOnce(pending.promise);
    const hover = h.providers.hover.cpp.provideHover(h.model, position, token); await Promise.resolve();
    const completion = h.complete(); expect(h.query).toHaveBeenCalledTimes(1);
    h.change('int updated;'); pending.resolve(response({ contents: 'old' }));
    expect(await hover).toBeNull(); expect(await completion).toEqual({ suggestions: [] }); expect(h.query).toHaveBeenCalledTimes(1);
    h.bridge.dispose();
  });

  it('rejects stale completions after typing and cancels them on scope switch', async () => {
    const h = harness(); const pending = deferred<CodeIntelligenceResponse>(); h.query.mockReturnValue(pending.promise);
    const answer = h.complete(); await Promise.resolve(); const signal = h.query.mock.calls[0][1]; h.change('int newer;');
    h.config.scope = { kind: 'attempt', id: 'attempt-2' }; h.config.scopeId = 'workspace-2'; h.bridge.refresh();
    expect(signal.aborted).toBe(true); pending.resolve(response([{ label: 'obsolete' }]));
    expect(await answer).toEqual({ suggestions: [] }); expect(h.config.onStatus).not.toHaveBeenCalledWith('ready'); h.bridge.dispose();
  });

  it('cancels superseded same-method requests and respects cancellation tokens', async () => {
    const h = harness(); const pending = deferred<CodeIntelligenceResponse>(); h.query.mockReturnValue(pending.promise);
    let cancel!: () => void;
    const first = h.complete(); await Promise.resolve(); const firstSignal = h.query.mock.calls[0][1];
    const second = h.complete(h.model, position, { isCancellationRequested: false, onCancellationRequested: (fn: (value: unknown) => void) => { cancel = () => fn(undefined); return { dispose() {} }; } });
    expect(firstSignal.aborted).toBe(true); cancel(); expect(h.query).toHaveBeenCalledTimes(1);
    pending.resolve(response([{ label: 'late' }])); await Promise.all([first, second]); expect(h.config.onStatus).not.toHaveBeenCalledWith('ready'); h.bridge.dispose();
  });

  it('limits concurrency without delaying editing or spawning an unbounded queue', async () => {
    const h = harness(); const pending = deferred<CodeIntelligenceResponse>(); h.query.mockReturnValue(pending.promise);
    const requests = [h.complete(), h.providers.hover.cpp.provideHover(h.model, position, token), h.providers.definition.cpp.provideDefinition(h.model, position, token), h.providers.references.cpp.provideReferences(h.model, position, { includeDeclaration: true }, token)];
    expect(await h.providers.symbols.cpp.provideDocumentSymbols(h.model, token)).toEqual([]); expect(h.query).toHaveBeenCalledTimes(1);
    h.bridge.dispose(); expect(h.query.mock.calls.every((call) => call[1].aborted)).toBe(true);
    pending.resolve(response(null)); await Promise.all(requests);
  });

  it('debounces diagnostics, maintains a separate marker owner, and clears stale hints on edits', async () => {
    const h = harness(); h.query.mockResolvedValue(response(null, [
      { path: 'main.cpp', diagnostics: [{ range, message: 'undeclared variable', severity: 1, code: 'undeclared' }] },
      { path: '../../other/main.cpp', diagnostics: [{ range, message: 'outside', severity: 1 }] },
    ]));
    await vi.advanceTimersByTimeAsync(600); h.change('int value;');
    await vi.advanceTimersByTimeAsync(1199); expect(h.query).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1); expect(h.query).toHaveBeenCalledTimes(1);
    expect(h.monaco.editor.setModelMarkers).toHaveBeenCalledWith(h.model, expect.stringMatching(/^clangd:/), [expect.objectContaining({ message: 'undeclared variable', severity: 8, source: 'clangd' })]);
    expect(h.monaco.editor.setModelMarkers).not.toHaveBeenCalledWith(expect.anything(), 'compiler', expect.anything());
    h.change('int valid;'); expect(h.monaco.editor.setModelMarkers).toHaveBeenLastCalledWith(h.model, expect.stringMatching(/^clangd:/), []);
    h.bridge.dispose();
  });

  it('preserves the original diagnostic message and source returned by clangd', async () => {
    const h = harness();
    h.query.mockResolvedValue(response(null, [{ path: 'main.cpp', diagnostics: [
      { range, message: "Use of undeclared identifier 'cout'", severity: 1, source: 'clang', code: 'undeclared_var_use' },
    ] }]));
    await vi.advanceTimersByTimeAsync(1200);
    expect(h.monaco.editor.setModelMarkers).toHaveBeenCalledWith(h.model, expect.stringMatching(/^clangd:/), [expect.objectContaining({
      message: "Use of undeclared identifier 'cout'", source: 'clang', code: 'undeclared_var_use',
    })]);
    h.bridge.dispose();
  });

  it('maps project definitions and opens target tabs but rejects system and foreign paths', async () => {
    const h = harness(); h.query.mockResolvedValue(response([
      { uri: 'lib/thing.h', range }, { uri: '/usr/include/c++/vector', range }, { uri: 'file:///secret', range }, { uri: '../other/header.h', range },
    ]));
    const result = await h.providers.definition.cpp.provideDefinition(h.model, position, token);
    expect(result).toHaveLength(1); expect(result[0].uri.toString()).toBe('inmemory:/workspace-1/lib/thing.h');
    expect(h.open(result[0].uri)).toBe(true); expect(h.config.onOpenFile).toHaveBeenCalledWith('header');
    h.switchModel(h.models.get(result[0].uri.toString())!); expect(h.editor.setSelection).toHaveBeenCalled(); expect(h.editor.focus).toHaveBeenCalled();
    expect(h.open(h.monaco.Uri.parse('inmemory:///workspace-2/lib/thing.h'))).toBe(false); h.bridge.dispose();
  });

  it('converts signature help, document symbols and reference context', async () => {
    const h = harness(); h.query.mockResolvedValueOnce(response({ signatures: [{ label: 'void f(int number)', parameters: [{ label: [7, 17] }] }], activeParameter: 0, activeSignature: 0 }));
    const signature = await h.providers.signature.cpp.provideSignatureHelp(h.model, position, token, { triggerKind: 2, triggerCharacter: '(', isRetrigger: false });
    expect(signature.value.signatures[0].parameters[0].label).toEqual([7, 17]);
    expect(h.query.mock.calls[0][0].context).toEqual({ triggerKind: 2, triggerCharacter: '(', isRetrigger: false });
    h.query.mockResolvedValueOnce(response([{ name: 'main', kind: 12, range, selectionRange: range, children: [] }]));
    expect(await h.providers.symbols.cpp.provideDocumentSymbols(h.model, token)).toEqual([expect.objectContaining({ name: 'main', kind: 11, range: lspRange(range) })]);
    h.query.mockResolvedValueOnce(response([])); await h.providers.references.cpp.provideReferences(h.model, position, { includeDeclaration: true }, token);
    expect(h.query.mock.calls[2][0].context).toEqual({ includeDeclaration: true }); h.bridge.dispose();
  });

  it('supports numeric argument placeholders without registering commands', async () => {
    const h = harness();
    h.query.mockResolvedValue(response([{ label: 'f', insertText: 'f(${1:value})$0', insertTextFormat: 2, command: { command: 'run' }, additionalTextEdits: [
      { range, newText: 'int' },
    ] }]));
    const result = await h.complete(); expect(result.suggestions[0].insertText).toBe('f(${1:value})$0');
    expect(result.suggestions[0].insertTextRules).toBe(4);
    expect(result.suggestions[0].additionalTextEdits).toEqual([{ range: lspRange(range), text: 'int' }]);
    expect(result.suggestions[0].command).toBeUndefined(); h.bridge.dispose();
  });

  it('rejects an entire formatting batch and completion when any edit is invalid', async () => {
    const h = harness(); const edits = [{ range, newText: '' }, { range: { start: { line: 200, character: 0 }, end: { line: 200, character: 0 } }, newText: 'replacement' }];
    h.query.mockResolvedValueOnce(response(edits));
    expect(await h.providers.formatting.cpp.provideDocumentFormattingEdits(h.model, { tabSize: 4, insertSpaces: true }, token)).toEqual([]);
    h.query.mockResolvedValueOnce(response([{ label: 'f', additionalTextEdits: edits }]));
    expect((await h.complete()).suggestions).toEqual([]);
    h.query.mockResolvedValueOnce(response([{ label: 'f', textEdit: { range: { start: { line: 0, character: 0 }, end: { line: 0, character: 5000 } }, newText: 'f' } }]));
    expect((await h.complete()).suggestions).toEqual([]); h.bridge.dispose();
  });

  it.each(['f($CLIPBOARD)', 'f(${CLIPBOARD})', 'f(${1:${CLIPBOARD}})', 'f(${TM_SELECTED_TEXT})', 'f(${1/foo/bar/})'])('never expands untrusted snippet variables or transforms: %s', async (insertText) => {
    const h = harness(); h.query.mockResolvedValue(response([{ label: 'f', insertText, insertTextFormat: 2 }]));
    const result = await h.complete(); expect(result.suggestions[0].insertTextRules).toBeUndefined(); h.bridge.dispose();
  });

  it('disposes providers, listeners, outstanding requests and advisory markers', async () => {
    const h = harness(); const pending = deferred<CodeIntelligenceResponse>(); h.query.mockReturnValue(pending.promise);
    const answer = h.complete(); await Promise.resolve(); h.bridge.dispose();
    expect(h.disposables.every((item) => item.dispose.mock.calls.length === 1)).toBe(true);
    expect(h.contentListeners.size).toBe(0); expect(h.modelListeners.size).toBe(0); expect(h.query.mock.calls[0][1].aborted).toBe(true);
    pending.resolve(response([{ label: 'too late' }])); expect(await answer).toEqual({ suggestions: [] });
    await vi.advanceTimersByTimeAsync(60_000); expect(h.query).toHaveBeenCalledTimes(1);
  });
});

describe('LSP range validation', () => {
  it('converts UTF-16 positions without changing their units', () => {
    expect(lspRange({ start: { line: 2, character: 4 }, end: { line: 3, character: 8 } })).toEqual({ startLineNumber: 3, startColumn: 5, endLineNumber: 4, endColumn: 9 });
  });
  it.each([null, {}, { start: { line: 0.5, character: 0 }, end: { line: 1, character: 0 } }, { start: { line: 2, character: 0 }, end: { line: 1, character: 0 } }, { start: { line: 0, character: 4 }, end: { line: 0, character: 2 } }])('rejects malformed ranges %s', (value) => { expect(lspRange(value)).toBeUndefined(); });
});
