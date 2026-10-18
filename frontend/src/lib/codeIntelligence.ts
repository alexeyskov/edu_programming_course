import type * as Monaco from 'monaco-editor';
import type { WorkspaceFile } from '../types';
import { api } from './api';
import { cppKeywords, languageForPath, workspaceIdentifiers } from './utils';

export interface CodeIntelligenceScope { kind: 'attempt' | 'submission' | 'experiment'; id: string }
export type CodeIntelligenceMethod = 'completion' | 'hover' | 'signatureHelp' | 'definition' | 'references' | 'documentSymbol' | 'diagnostics' | 'formatting';
export interface CodeIntelligenceQuery {
  scope: CodeIntelligenceScope;
  files?: { path: string; content: string }[];
  method: CodeIntelligenceMethod;
  path: string;
  position?: { line: number; character: number };
  context?: Record<string, string | number | boolean>;
}
export interface CodeIntelligenceResponse {
  result: unknown;
  diagnostics: { path: string; diagnostics: unknown[] }[];
}
export type CodeIntelligenceStatus = 'connecting' | 'ready' | 'fallback';
interface Config {
  scopeId: string;
  scope?: CodeIntelligenceScope;
  files: WorkspaceFile[];
  readOnly: boolean;
  onOpenFile(id: string): void;
  onStatus?(status: CodeIntelligenceStatus): void;
}
type Json = Record<string, unknown>;
type Query = (query: CodeIntelligenceQuery, signal: AbortSignal) => Promise<CodeIntelligenceResponse>;
const codePath = /\.(?:cpp|cc|cxx|c\+\+|c|h|hh|hpp|hxx|h\+\+|ipp|tpp|inc)$/i;
const standardHeaders = ['algorithm', 'array', 'cassert', 'cctype', 'cmath', 'cstdio', 'cstdlib', 'cstring', 'deque', 'exception', 'fstream', 'functional', 'iomanip', 'iostream', 'iterator', 'limits', 'list', 'map', 'memory', 'numeric', 'optional', 'queue', 'set', 'sstream', 'stack', 'stdexcept', 'string', 'string_view', 'tuple', 'unordered_map', 'unordered_set', 'utility', 'vector', 'assert.h', 'ctype.h', 'math.h', 'stdio.h', 'stdlib.h', 'string.h'];
const completionKinds = ['Text', 'Method', 'Function', 'Constructor', 'Field', 'Variable', 'Class', 'Interface', 'Module', 'Property', 'Unit', 'Value', 'Enum', 'Keyword', 'Snippet', 'Color', 'File', 'Reference', 'Folder', 'EnumMember', 'Constant', 'Struct', 'Event', 'Operator', 'TypeParameter'] as const;
const asObject = (value: unknown): Json | undefined => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Json : undefined;
const text = (value: unknown) => typeof value === 'string' ? value : '';
const index = (value: unknown): value is number => Number.isInteger(value) && (value as number) >= 0 && (value as number) < 10_000_000;

export function lspRange(value: unknown): Monaco.IRange | undefined {
  const range = asObject(value); const start = asObject(range?.start); const end = asObject(range?.end);
  if (!start || !end || !index(start.line) || !index(start.character) || !index(end.line) || !index(end.character)
    || end.line < start.line || (end.line === start.line && end.character < start.character)) return;
  return { startLineNumber: start.line + 1, startColumn: start.character + 1, endLineNumber: end.line + 1, endColumn: end.character + 1 };
}

/** No HTML, command links, remote images or trusted commands from a language server. */
function markdown(value: unknown): Monaco.IMarkdownString | undefined {
  const entry = asObject(value);
  let content = typeof value === 'string' ? value : text(entry?.value);
  if (!content) return;
  if (entry?.language) content = `\`\`\`${text(entry.language).replace(/[^\w+]/g, '')}\n${content}\n\`\`\``;
  return { value: content.replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1'), isTrusted: false, supportHtml: false };
}

function validEdit(value: unknown, model: Monaco.editor.ITextModel): Monaco.languages.TextEdit | undefined {
  const edit = asObject(value); const range = lspRange(edit?.range);
  if (!range || typeof edit?.newText !== 'string' || !rangeFits(range, model)) return;
  return { range, text: edit.newText };
}
function rangeFits(range: Monaco.IRange, model: Monaco.editor.ITextModel) {
  return range.endLineNumber <= model.getLineCount() && range.startColumn <= model.getLineMaxColumn(range.startLineNumber)
    && range.endColumn <= model.getLineMaxColumn(range.endLineNumber);
}

/** One bridge per editor. Monaco providers are global, so every call is scoped to known canonical URIs. */
export function installCodeIntelligence(
  editor: Monaco.editor.IStandaloneCodeEditor,
  monaco: typeof Monaco,
  getConfig: () => Config,
  query: Query = (body, signal) => api.queryCodeIntelligence(body, signal),
) {
  const subscriptions: Monaco.IDisposable[] = [];
  const requests = new Map<string, AbortController>();
  const waiting: { priority: number; grant(): void; cancel(): void }[] = [];
  let querying = false;
  const ownedModels = new Set<Monaco.editor.ITextModel>();
  const markedModels = new Set<Monaco.editor.ITextModel>();
  const markerOwner = `clangd:${getConfig().scopeId}:${editor.getId?.() ?? 'editor'}`;
  let disposed = false;
  let diagnosticsTimer: ReturnType<typeof setTimeout> | undefined;
  let cooldownUntil = 0;
  let activeScope = scopeKey();
  let pendingNavigation: { path: string; range: Monaco.IRange } | undefined;
  let semanticInstalled = false;

  const uriFor = (path: string) => monaco.Uri.parse(`inmemory:///${getConfig().scopeId}/${path}`);
  function scopeKey() { const config = getConfig(); return `${config.scopeId}:${config.scope?.kind}:${config.scope?.id}`; }
  function fileFor(model: Monaco.editor.ITextModel) {
    return getConfig().files.find((file) => codePath.test(file.path) && uriFor(file.path).toString() === model.uri.toString());
  }
  function editable(model: Monaco.editor.ITextModel) {
    return !getConfig().readOnly && getConfig().scope?.kind !== 'submission' && !fileFor(model)?.readOnly;
  }
  function snapshot() {
    const active = editor.getModel();
    return getConfig().files.filter((file) => codePath.test(file.path)).map((file) => ({ path: file.path,
      // onChange updates inactive file buffers in React. Cached Monaco models
      // can instead predate an experiment reset or a newly loaded snapshot.
      content: active?.uri.toString() === uriFor(file.path).toString() ? active.getValue() : file.content,
    }));
  }
  function signature() { return `${scopeKey()}\n${JSON.stringify(snapshot())}`; }
  function setStatus(status: CodeIntelligenceStatus) { if (!disposed) getConfig().onStatus?.(status); }
  function clearMarkers() {
    for (const model of markedModels) if (!model.isDisposed()) monaco.editor.setModelMarkers(model, markerOwner, []);
    markedModels.clear();
  }
  // A clangd workspace processes queries in order. Coalesce per method and keep a
  // small queue, with completions ahead of passive diagnostics/symbol discovery.
  function acquire(method: CodeIntelligenceMethod, signal: AbortSignal): Promise<(() => void) | undefined> {
    if (signal.aborted) return Promise.resolve(undefined);
    const release = () => {
      const next = waiting.sort((a, b) => b.priority - a.priority).shift();
      if (next) next.grant(); else querying = false;
    };
    if (!querying) { querying = true; return Promise.resolve(release); }
    return new Promise((resolve) => {
      const item = {
        priority: method === 'completion' ? 3 : method === 'signatureHelp' ? 2 : method === 'diagnostics' || method === 'documentSymbol' ? 0 : 1,
        grant() { cleanup(); resolve(release); },
        cancel() { const index = waiting.indexOf(item); if (index >= 0) waiting.splice(index, 1); cleanup(); resolve(undefined); },
      };
      const timer = setTimeout(item.cancel, 4000);
      const cleanup = () => { clearTimeout(timer); signal.removeEventListener('abort', item.cancel); };
      waiting.push(item); signal.addEventListener('abort', item.cancel, { once: true });
    });
  }
  function applyDiagnostics(groups: CodeIntelligenceResponse['diagnostics']) {
    if (!Array.isArray(groups)) return;
    for (const group of groups) {
      if (!getConfig().files.some((file) => file.path === group.path)) continue;
      const model = monaco.editor.getModel(uriFor(group.path));
      if (!model || model.isDisposed() || !Array.isArray(group.diagnostics)) continue;
      const markers = group.diagnostics.flatMap((value): Monaco.editor.IMarkerData[] => {
        const item = asObject(value); const range = lspRange(item?.range);
        if (!range || typeof item?.message !== 'string' || range.startLineNumber > model.getLineCount()) return [];
        const severity = item.severity === 1 ? 8 : item.severity === 2 ? 4 : item.severity === 4 ? 1 : 2;
        return [{ ...model.validateRange(range), message: item.message, severity, source: text(item.source) || 'clangd', code: typeof item.code === 'string' || typeof item.code === 'number' ? String(item.code) : undefined }];
      });
      monaco.editor.setModelMarkers(model, markerOwner, markers);
      markedModels.add(model);
    }
  }
  async function ask(method: CodeIntelligenceMethod, model: Monaco.editor.ITextModel, token?: Monaco.CancellationToken, position?: Monaco.IPosition, context?: CodeIntelligenceQuery['context']) {
    const config = getConfig(); const file = fileFor(model);
    if (disposed || !config.scope || !file || token?.isCancellationRequested || model.isDisposed() || cooldownUntil > Date.now()) return;
    const key = method;
    requests.get(key)?.abort(); requests.delete(key);
    if (requests.size >= 4) return;
    const controller = new AbortController(); requests.set(key, controller);
    const cancellation = token?.onCancellationRequested(() => controller.abort());
    const before = signature(); const version = model.getVersionId();
    let release: (() => void) | undefined;
    try {
      release = await acquire(method, controller.signal);
      if (!release || disposed || controller.signal.aborted || model.isDisposed() || before !== signature()) return;
      const response = await query({ scope: { ...config.scope }, method, path: file.path,
        ...(config.scope.kind !== 'submission' ? { files: snapshot() } : {}),
        ...(position ? { position: { line: position.lineNumber - 1, character: position.column - 1 } } : {}),
        ...(context ? { context } : {}),
      }, controller.signal);
      if (disposed || controller.signal.aborted || model.isDisposed() || model.getVersionId() !== version || before !== signature()) return;
      setStatus('ready'); applyDiagnostics(response.diagnostics);
      return response.result;
    } catch {
      if (!disposed && !controller.signal.aborted && before === signature()) {
        cooldownUntil = Date.now() + 30_000;
        clearMarkers(); setStatus('fallback');
      }
      return;
    } finally {
      release?.();
      cancellation?.dispose();
      if (requests.get(key) === controller) requests.delete(key);
    }
  }
  function localCompletions(model: Monaco.editor.ITextModel, position: Monaco.IPosition): Monaco.languages.CompletionItem[] {
    const word = model.getWordUntilPosition(position);
    const range = { startLineNumber: position.lineNumber, endLineNumber: position.lineNumber, startColumn: word.startColumn, endColumn: word.endColumn };
    const prefix = model.getLineContent(position.lineNumber).slice(0, position.column - 1);
    const include = /^\s*#\s*include\s*[<"]([^>"]*)$/.exec(prefix);
    if (include) {
      const names = new Set([...getConfig().files.filter((file) => codePath.test(file.path)).map((file) => file.path), ...standardHeaders]);
      return [...names].filter((name) => name.startsWith(include[1])).map((name) => ({ label: name, insertText: name, kind: monaco.languages.CompletionItemKind.File,
        range: { ...range, startColumn: position.column - include[1].length } }));
    }
    return [
      // Match Monaco's word-based suggestions: plain text, not an inferred
      // variable/type or a fabricated description. Semantic details come from clangd.
      ...workspaceIdentifiers(snapshot().map((file) => ({ ...file, id: file.path }))).slice(0, 500).map((identifier) => ({ label: identifier, kind: monaco.languages.CompletionItemKind.Text, insertText: identifier, range, sortText: `1-${identifier}` })),
      ...cppKeywords.map((keyword) => ({ label: keyword, kind: monaco.languages.CompletionItemKind.Keyword, insertText: keyword, range, sortText: `2-${keyword}` })),
    ];
  }
  function completion(value: unknown, model: Monaco.editor.ITextModel, position: Monaco.IPosition): Monaco.languages.CompletionList | undefined {
    const list = asObject(value); const items = Array.isArray(value) ? value : list?.items;
    if (!Array.isArray(items)) return;
    const word = model.getWordUntilPosition(position);
    const defaults = asObject(list?.itemDefaults);
    const suggestions = items.flatMap((raw): Monaco.languages.CompletionItem[] => {
      const item = asObject(raw); if (!item || typeof item.label !== 'string') return [];
      const edit = asObject(item.textEdit);
      const rawRange = edit?.range ?? edit?.insert ?? asObject(defaults?.editRange)?.insert ?? defaults?.editRange;
      const range = rawRange === undefined ? { startLineNumber: position.lineNumber, endLineNumber: position.lineNumber, startColumn: word.startColumn, endColumn: word.endColumn } : lspRange(rawRange);
      if (!range || !rangeFits(range, model)) return [];
      const additionalTextEdits = Array.isArray(item.additionalTextEdits) ? item.additionalTextEdits.map((candidate) => validEdit(candidate, model)) : [];
      if (additionalTextEdits.some((edit) => !edit)) return [];
      const insertText = text(edit?.newText) || text(item.textEditText) || text(item.insertText) || item.label;
      // Function argument tab stops are normal editor edits and use onChange.
      // Never expand snippet variables (notably $CLIPBOARD) or transforms that
      // could bypass the assessment's clipboard restrictions.
      const insertTextFormat = item.insertTextFormat ?? defaults?.insertTextFormat;
      const safeSnippet = insertTextFormat === 2 && !/\$/.test(insertText
        .replace(/\\[$\\{}]/g, '').replace(/\$\d+|\$\{\d+(?::[^${}]*|\|[^${}]*\|)?\}/g, ''));
      const plainText = insertTextFormat === 2 && !safeSnippet ? insertText.replace(/\$\{\d+:([^}]*)\}/g, '$1').replace(/\$\{\d+\}|\$\d+/g, '') : insertText;
      const kindName = completionKinds[Number(item.kind) - 1] ?? 'Text';
      return [{ label: item.label, kind: monaco.languages.CompletionItemKind[kindName], insertText: plainText, range,
        insertTextRules: safeSnippet ? monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet : undefined,
        detail: text(item.detail), documentation: markdown(item.documentation), sortText: text(item.sortText) || item.label,
        filterText: text(item.filterText) || undefined, preselect: item.preselect === true,
        additionalTextEdits: additionalTextEdits as Monaco.languages.TextEdit[],
      }];
    });
    return { suggestions, incomplete: list?.isIncomplete === true };
  }
  function ensureModel(path: string) {
    const file = getConfig().files.find((candidate) => candidate.path === path);
    if (!file || !codePath.test(file.path)) return;
    const uri = uriFor(path); let model = monaco.editor.getModel(uri);
    if (!model) { model = monaco.editor.createModel(file.content, file.language ?? languageForPath(path), uri); ownedModels.add(model); }
    else if (model !== editor.getModel() && model.getValue() !== file.content) model.setValue(file.content);
    return model;
  }
  function locations(value: unknown): Monaco.languages.Location[] {
    return (Array.isArray(value) ? value : value ? [value] : []).flatMap((candidate) => {
      const item = asObject(candidate); const path = text(item?.uri ?? item?.targetUri); const range = lspRange(item?.range ?? item?.targetSelectionRange ?? item?.targetRange);
      if (!range || !getConfig().files.some((file) => file.path === path) || !ensureModel(path)) return [];
      return [{ uri: uriFor(path), range }];
    });
  }
  function navigate() {
    const model = editor.getModel(); const target = pendingNavigation;
    if (!model || !target || model.uri.toString() !== uriFor(target.path).toString()) return;
    pendingNavigation = undefined;
    editor.setSelection(target.range); editor.revealRangeInCenter(target.range); editor.focus();
  }
  function installSemanticProviders() {
    if (semanticInstalled || !getConfig().scope) return;
    semanticInstalled = true;
    for (const language of ['c', 'cpp']) {
      subscriptions.push(monaco.languages.registerHoverProvider(language, { async provideHover(model, position, token) {
        const result = asObject(await ask('hover', model, token, position));
        if (!result) return null;
        const contents = (Array.isArray(result.contents) ? result.contents : [result.contents]).flatMap((item) => { const content = markdown(item); return content ? [content] : []; });
        return contents.length ? { contents, range: lspRange(result.range) } : null;
      } }));
      subscriptions.push(monaco.languages.registerSignatureHelpProvider(language, {
        signatureHelpTriggerCharacters: ['(', ','], signatureHelpRetriggerCharacters: [')'],
        async provideSignatureHelp(model, position, token, context) {
          const result = asObject(await ask('signatureHelp', model, token, position, { triggerKind: context.triggerKind, isRetrigger: context.isRetrigger, ...(context.triggerCharacter ? { triggerCharacter: context.triggerCharacter } : {}) }));
          if (!result || !Array.isArray(result.signatures)) return null;
          const signatures: Monaco.languages.SignatureInformation[] = result.signatures.flatMap((entry) => {
            const item = asObject(entry); if (!item || typeof item.label !== 'string') return [];
            return [{ label: item.label, documentation: markdown(item.documentation), parameters: Array.isArray(item.parameters) ? item.parameters.flatMap((entry) => {
              const parameter = asObject(entry); const label = parameter?.label;
              if (typeof label !== 'string' && !(Array.isArray(label) && label.length === 2 && label.every(index))) return [];
              return [{ label: label as string | [number, number], documentation: markdown(parameter?.documentation) }];
            }) : [], activeParameter: index(item.activeParameter) ? item.activeParameter : undefined }];
          });
          return { value: { signatures, activeSignature: index(result.activeSignature) ? result.activeSignature : 0, activeParameter: index(result.activeParameter) ? result.activeParameter : 0 }, dispose() {} };
        },
      }));
      subscriptions.push(monaco.languages.registerDefinitionProvider(language, { async provideDefinition(model, position, token) { return locations(await ask('definition', model, token, position)); } }));
      subscriptions.push(monaco.languages.registerReferenceProvider(language, { async provideReferences(model, position, context, token) { return locations(await ask('references', model, token, position, { includeDeclaration: context.includeDeclaration })); } }));
      subscriptions.push(monaco.languages.registerDocumentSymbolProvider(language, { displayName: 'clangd', async provideDocumentSymbols(model, token) {
        const result = await ask('documentSymbol', model, token);
        function symbols(items: unknown): Monaco.languages.DocumentSymbol[] {
          if (!Array.isArray(items)) return [];
          return items.flatMap((raw) => {
            const item = asObject(raw); const range = lspRange(item?.range ?? asObject(item?.location)?.range);
            if (!range || typeof item?.name !== 'string' || !index(item.kind) || item.kind < 1 || item.kind > 26) return [];
            return [{ name: item.name, detail: text(item.detail), kind: item.kind - 1, tags: [], range, selectionRange: lspRange(item.selectionRange) ?? range, children: symbols(item.children) }];
          });
        }
        return symbols(result);
      } }));
      subscriptions.push(monaco.languages.registerDocumentFormattingEditProvider(language, { displayName: 'clangd', async provideDocumentFormattingEdits(model, options, token) {
        if (!fileFor(model) || !editable(model)) return [];
        const result = await ask('formatting', model, token, undefined, { tabSize: Math.min(8, Math.max(2, options.tabSize)), insertSpaces: options.insertSpaces });
        if (!Array.isArray(result) || !editable(model)) return [];
        const edits = result.map((item) => validEdit(item, model));
        // Formatting is atomic: never apply the deletion half of an invalid
        // delete/insert batch and silently lose student source text.
        return edits.every((edit): edit is Monaco.languages.TextEdit => Boolean(edit)) ? edits : [];
      } }));
    }
    subscriptions.push(monaco.editor.registerEditorOpener({ openCodeEditor(source, resource, selection) {
      if (source !== editor) return false;
      const file = getConfig().files.find((item) => uriFor(item.path).toString() === resource.toString());
      if (!file) return false;
      const range = selection && 'startLineNumber' in selection ? selection : { startLineNumber: selection?.lineNumber ?? 1, endLineNumber: selection?.lineNumber ?? 1, startColumn: selection?.column ?? 1, endColumn: selection?.column ?? 1 };
      pendingNavigation = { path: file.path, range }; getConfig().onOpenFile(file.id); navigate();
      return true;
    } }));
    subscriptions.push(editor.onDidChangeModelContent(() => { clearMarkers(); scheduleDiagnostics(); }));
    subscriptions.push(editor.onDidChangeModel(() => { navigate(); scheduleDiagnostics(); }));
  }
  for (const language of ['c', 'cpp']) {
    subscriptions.push(monaco.languages.registerCompletionItemProvider(language, {
      triggerCharacters: ['.', '>', ':', '<', '"', '/'],
      async provideCompletionItems(model, position, context, token) {
        if (!fileFor(model) || !editable(model)) return { suggestions: [] };
        const version = model.getVersionId(); const scope = scopeKey();
        const result = await ask('completion', model, token, position, { triggerKind: context.triggerKind, ...(context.triggerCharacter ? { triggerCharacter: context.triggerCharacter } : {}) });
        if (disposed || token.isCancellationRequested || model.isDisposed() || model.getVersionId() !== version || scope !== scopeKey() || !editable(model)) return { suggestions: [] };
        return completion(result, model, position) ?? { suggestions: localCompletions(model, position) };
      },
    }));
  }
  function scheduleDiagnostics() {
    if (diagnosticsTimer) clearTimeout(diagnosticsTimer);
    if (!getConfig().scope || disposed || cooldownUntil > Date.now()) return;
    diagnosticsTimer = setTimeout(() => {
      diagnosticsTimer = undefined; const model = editor.getModel();
      if (model && !model.isDisposed() && fileFor(model)) void ask('diagnostics', model);
    }, 1200);
  }
  function refresh() {
    if (disposed) return;
    if (activeScope !== scopeKey()) {
      for (const request of requests.values()) request.abort(); requests.clear();
      clearMarkers(); cooldownUntil = 0; pendingNavigation = undefined; activeScope = scopeKey();
      for (const model of ownedModels) if (!model.isDisposed() && editor.getModel() !== model) model.dispose();
      ownedModels.clear(); setStatus('connecting');
    }
    installSemanticProviders(); scheduleDiagnostics();
  }
  refresh();
  return {
    refresh,
    async format() {
      const model = editor.getModel(); if (!model || !fileFor(model) || !editable(model)) return;
      // Switching tabs while the action awaits the server can cancel Monaco's
      // action. Cancellation must not become an unhandled UI rejection.
      try { await editor.getAction('editor.action.formatDocument')?.run(); } catch { /* no edit is applied on a cancelled action */ }
    },
    dispose() {
      disposed = true; if (diagnosticsTimer) clearTimeout(diagnosticsTimer);
      for (const request of requests.values()) request.abort(); requests.clear();
      subscriptions.forEach((subscription) => subscription.dispose()); clearMarkers();
      for (const model of ownedModels) if (!model.isDisposed() && editor.getModel() !== model) model.dispose();
      ownedModels.clear();
    },
  };
}
