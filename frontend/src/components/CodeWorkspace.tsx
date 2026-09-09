import Editor, { type BeforeMount, type OnMount } from '@monaco-editor/react';
import type * as Monaco from 'monaco-editor';
import { ChevronRight, FileCode2, FilePlus2, Folder, LockKeyhole, PanelLeftClose, Trash2, X } from 'lucide-react';
import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react';
import { useTheme } from '../context/ThemeContext';
import { cn, cppKeywords, findWorkspacePasteSource, isReceiptUsable, languageForPath, workspaceIdentifiers, type InternalClipboardReceipt } from '../lib/utils';
import type { Diagnostic, WorkspaceFile } from '../types';
import { Badge, Button } from './ui';

export interface CodeWorkspaceHandle {
  openDiagnostic(diagnostic: Diagnostic): void;
  openRange(path: string, startLine: number, endLine?: number): void;
  focus(): void;
}

export interface CodeHighlight {
  path: string;
  startLine: number;
  endLine: number;
  index: number;
  active?: boolean;
}

interface Props {
  files: WorkspaceFile[];
  activeFileId: string;
  onActiveFile(id: string): void;
  onChange(fileId: string, content: string, source: 'typing' | 'internal_paste', receiptId?: string): void;
  onCreateFile?(): void;
  onDeleteFile?(file: WorkspaceFile): void;
  canDeleteFile?(file: WorkspaceFile): boolean;
  readOnly?: boolean;
  strictPaste?: boolean;
  scopeId: string;
  diagnostics?: Diagnostic[];
  highlights?: CodeHighlight[];
  theme?: 'light' | 'dark';
  experiment?: boolean;
  explorerVisible?: boolean;
  onExplorerCollapse?(): void;
  onPasteBlocked?(): void;
  onInternalCopy?(fileId: string, text: string): Promise<string | null>;
}

const severityMap: Record<Diagnostic['severity'], Monaco.MarkerSeverity> = {
  error: 8 as Monaco.MarkerSeverity, warning: 4 as Monaco.MarkerSeverity, info: 2 as Monaco.MarkerSeverity,
};

export const CodeWorkspace = forwardRef<CodeWorkspaceHandle, Props>(function CodeWorkspace({
  files, activeFileId, onActiveFile, onChange, onCreateFile, onDeleteFile, canDeleteFile,
  readOnly = false, strictPaste = false,
  scopeId, diagnostics = [], highlights = [], theme: requestedTheme, experiment = false, explorerVisible = true,
  onExplorerCollapse, onPasteBlocked, onInternalCopy,
}, ref) {
  const { theme: applicationTheme } = useTheme();
  const theme = requestedTheme ?? applicationTheme;
  const editorRef = useRef<Monaco.editor.IStandaloneCodeEditor | null>(null);
  const monacoRef = useRef<typeof Monaco | null>(null);
  const highlightDecorationsRef = useRef<Monaco.editor.IEditorDecorationsCollection | null>(null);
  const receiptRef = useRef<InternalClipboardReceipt | null>(null);
  const internalPasteRef = useRef(false);
  const internalPasteReceiptRef = useRef<string>();
  const [openFiles, setOpenFiles] = useState<string[]>([activeFileId]);
  const active = files.find((file) => file.id === activeFileId) ?? files[0];
  const activeRef = useRef(active);
  const filesRef = useRef(files);
  const pasteConfigRef = useRef({ strictPaste, readOnly, scopeId, onPasteBlocked, onInternalCopy });
  activeRef.current = active;
  filesRef.current = files;
  pasteConfigRef.current = { strictPaste, readOnly, scopeId, onPasteBlocked, onInternalCopy };

  useEffect(() => {
    if (activeFileId && !openFiles.includes(activeFileId)) setOpenFiles((items) => [...items, activeFileId]);
  }, [activeFileId, openFiles]);

  const setMarkers = useMemo(() => () => {
    const monaco = monacoRef.current;
    if (!monaco) return;
    for (const file of files) {
      const uri = monaco.Uri.parse(`inmemory:///${scopeId}/${file.path}`);
      const model = monaco.editor.getModel(uri);
      if (!model) continue;
      const fileDiagnostics = diagnostics.filter((item) => item.fileId === file.id || item.path === file.path);
      monaco.editor.setModelMarkers(model, 'compiler', fileDiagnostics.map((item) => ({
        severity: severityMap[item.severity], message: item.message, code: item.code,
        startLineNumber: Math.max(1, item.line ?? 1), startColumn: Math.max(1, item.column ?? 1),
        endLineNumber: Math.max(1, item.endLine ?? item.line ?? 1), endColumn: Math.max(1, item.endColumn ?? (item.column ?? 1) + 1),
      })));
    }
  }, [files, diagnostics, scopeId]);

  useEffect(() => { setMarkers(); }, [setMarkers, activeFileId]);

  const setHighlights = useMemo(() => () => {
    const editor = editorRef.current;
    const current = activeRef.current;
    if (!editor || !current) return;
    const decorations = highlights
      .filter((item) => item.path === current.path)
      .map((item) => ({
        range: {
          startLineNumber: Math.max(1, item.startLine), startColumn: 1,
          endLineNumber: Math.max(item.startLine, item.endLine), endColumn: 1,
        },
        options: {
          isWholeLine: true,
          className: item.active ? 'similarity-line similarity-line--active' : 'similarity-line',
          linesDecorationsClassName: item.active ? 'similarity-gutter similarity-gutter--active' : 'similarity-gutter',
          hoverMessage: { value: `Совпадающий фрагмент №${item.index}` },
        },
      }));
    if (highlightDecorationsRef.current) highlightDecorationsRef.current.set(decorations);
    else highlightDecorationsRef.current = editor.createDecorationsCollection(decorations);
  }, [highlights]);

  useEffect(() => { setHighlights(); }, [setHighlights, activeFileId]);

  useImperativeHandle(ref, () => ({
    openDiagnostic(diagnostic) {
      const file = files.find((item) => item.id === diagnostic.fileId || item.path === diagnostic.path);
      if (file) onActiveFile(file.id);
      window.setTimeout(() => {
        const editor = editorRef.current;
        if (!editor) return;
        const position = { lineNumber: diagnostic.line ?? 1, column: diagnostic.column ?? 1 };
        editor.setPosition(position); editor.revealPositionInCenter(position); editor.focus();
      }, 30);
    },
    openRange(path, startLine, endLine = startLine) {
      const file = files.find((item) => item.path === path);
      if (file) onActiveFile(file.id);
      window.setTimeout(() => {
        const editor = editorRef.current;
        if (!editor) return;
        const first = Math.max(1, startLine);
        const last = Math.max(first, endLine);
        const endColumn = editor.getModel()?.getLineMaxColumn(last) ?? 1;
        editor.setSelection({ startLineNumber: first, startColumn: 1, endLineNumber: last, endColumn });
        editor.revealLinesInCenter(first, last);
        editor.focus();
      }, 30);
    },
    focus() { editorRef.current?.focus(); },
  }), [files, onActiveFile]);

  const beforeMount: BeforeMount = (monaco) => {
    monaco.editor.defineTheme('eduprog-dark', {
      base: 'vs-dark', inherit: true, rules: [
        { token: 'comment', foreground: '6a9955', fontStyle: 'italic' },
        { token: 'keyword', foreground: 'c586c0' }, { token: 'type', foreground: '4ec9b0' },
        { token: 'string', foreground: 'ce9178' }, { token: 'number', foreground: 'b5cea8' },
      ], colors: {
        'editor.background': '#1e1e1e', 'editor.foreground': '#d4d4d4', 'editorGutter.background': '#1e1e1e',
        'editorLineNumber.foreground': '#858585', 'editorLineNumber.activeForeground': '#c6c6c6',
        'editor.lineHighlightBackground': '#2a2d2e', 'editor.selectionBackground': '#264f78',
        'editorIndentGuide.background1': '#404040', 'editorCursor.foreground': '#aeafad',
        'editorError.foreground': '#f14c4c', 'editorWarning.foreground': '#cca700',
      },
    });
    monaco.editor.defineTheme('eduprog-light', { base: 'vs', inherit: true, rules: [], colors: { 'editor.background': '#ffffff', 'editorGutter.background': '#f3f3f3', 'editor.lineHighlightBackground': '#f5f5f5', 'editor.selectionBackground': '#add6ff', 'editorCursor.foreground': '#000000' } });
  };

  const handleMount: OnMount = (editor, monaco) => {
    editorRef.current = editor; monacoRef.current = monaco; setMarkers(); setHighlights();
    const completionProvider = (language: 'c' | 'cpp') => monaco.languages.registerCompletionItemProvider(language, {
      provideCompletionItems(model, position) {
        const scopePrefix = `inmemory:///${pasteConfigRef.current.scopeId}/`;
        if (!model.uri.toString().startsWith(scopePrefix)) return { suggestions: [] };
        const word = model.getWordUntilPosition(position);
        const range = { startLineNumber: position.lineNumber, endLineNumber: position.lineNumber, startColumn: word.startColumn, endColumn: word.endColumn };
        const local = workspaceIdentifiers(filesRef.current).map((identifier) => ({
          label: identifier, kind: monaco.languages.CompletionItemKind.Variable, insertText: identifier,
          detail: 'Идентификатор из текущей рабочей области', range, sortText: `0-${identifier}`,
        }));
        const keywords = cppKeywords.map((keyword) => ({
          label: keyword, kind: monaco.languages.CompletionItemKind.Keyword, insertText: keyword,
          detail: 'Ключевое слово C/C++', range, sortText: `1-${keyword}`,
        }));
        return { suggestions: [...local, ...keywords] };
      },
    });
    const completionProviders = [completionProvider('c'), completionProvider('cpp')];
    const dom = editor.getDomNode();
    if (!dom) { completionProviders.forEach((provider) => provider.dispose()); return; }
    const copyHandler = (event: ClipboardEvent) => {
      const selection = editor.getSelection(); const model = editor.getModel();
      const currentActive = activeRef.current;
      const config = pasteConfigRef.current;
      if (!config.strictPaste || !selection || !model || selection.isEmpty() || !currentActive || !config.onInternalCopy) return;
      const text = model.getValueInRange(selection);
      receiptRef.current = null;
      const receiptScope = config.scopeId;
      void config.onInternalCopy(currentActive.id, text).then((serverReceiptId) => {
        if (serverReceiptId) {
          receiptRef.current = { attemptId: receiptScope, text, sourceFileId: currentActive.id, createdAt: Date.now(), serverReceiptId };
        }
      });
      event.clipboardData?.setData('text/plain', text);
    };
    const pasteHandler = (event: ClipboardEvent) => {
      const config = pasteConfigRef.current;
      if (!config.strictPaste || config.readOnly) return;
      const text = event.clipboardData?.getData('text/plain') ?? '';
      event.preventDefault(); event.stopImmediatePropagation();
      const sourceFile = findWorkspacePasteSource(filesRef.current, text);
      if (!sourceFile) {
        config.onPasteBlocked?.(); return;
      }
      const selection = editor.getSelection();
      const model = editor.getModel();
      if (!selection || !model) return;
      const applyPaste = (receiptId: string) => {
        internalPasteReceiptRef.current = receiptId;
        internalPasteRef.current = true;
        editor.executeEdits('internal-paste', [{ range: selection, text, forceMoveMarkers: true }]);
        receiptRef.current = null;
        window.setTimeout(() => { internalPasteRef.current = false; internalPasteReceiptRef.current = undefined; }, 0);
      };
      if (isReceiptUsable(receiptRef.current, config.scopeId, text) && receiptRef.current?.serverReceiptId) {
        applyPaste(receiptRef.current.serverReceiptId);
        return;
      }
      if (!config.onInternalCopy) {
        config.onPasteBlocked?.();
        return;
      }
      const modelVersion = model.getVersionId();
      const receiptScope = config.scopeId;
      void config.onInternalCopy(sourceFile.id, text).then((serverReceiptId) => {
        const latest = pasteConfigRef.current;
        if (
          !serverReceiptId
          || latest.readOnly
          || !latest.strictPaste
          || latest.scopeId !== receiptScope
          || editor.getModel() !== model
          || model.getVersionId() !== modelVersion
        ) return;
        receiptRef.current = {
          attemptId: receiptScope,
          text,
          sourceFileId: sourceFile.id,
          createdAt: Date.now(),
          serverReceiptId,
        };
        applyPaste(serverReceiptId);
      });
    };
    const dropHandler = (event: DragEvent) => {
      const config = pasteConfigRef.current;
      if (config.strictPaste && !config.readOnly) { event.preventDefault(); event.stopImmediatePropagation(); config.onPasteBlocked?.(); }
    };
    const beforeInputHandler = (event: InputEvent) => {
      const config = pasteConfigRef.current;
      if (config.strictPaste && event.inputType === 'insertFromPaste' && !internalPasteRef.current) { event.preventDefault(); config.onPasteBlocked?.(); }
    };
    dom.addEventListener('copy', copyHandler, true); dom.addEventListener('cut', copyHandler, true);
    dom.addEventListener('paste', pasteHandler, true); dom.addEventListener('drop', dropHandler, true);
    dom.addEventListener('beforeinput', beforeInputHandler, true);
    editor.onDidDispose(() => {
      highlightDecorationsRef.current?.clear();
      highlightDecorationsRef.current = null;
      completionProviders.forEach((provider) => provider.dispose());
      dom.removeEventListener('copy', copyHandler, true); dom.removeEventListener('cut', copyHandler, true);
      dom.removeEventListener('paste', pasteHandler, true); dom.removeEventListener('drop', dropHandler, true);
      dom.removeEventListener('beforeinput', beforeInputHandler, true);
    });
  };

  function closeTab(id: string) {
    const remaining = openFiles.filter((item) => item !== id);
    setOpenFiles(remaining);
    if (id === activeFileId) onActiveFile(remaining.at(-1) ?? files[0]?.id);
  }

  if (!active) return <div className="editor-empty"><FileCode2 /><p>В рабочей области пока нет файлов</p>{onCreateFile && <Button variant="secondary" onClick={onCreateFile}><FilePlus2 size={16} /> Создать файл</Button>}</div>;

  return <div className={cn('code-workspace', experiment && 'code-workspace--experiment', !explorerVisible && 'code-workspace--explorer-hidden')}>
    {explorerVisible && <aside className="file-explorer"><header><span>Файлы</span><span className="file-explorer__actions">{onCreateFile && !readOnly && <button onClick={onCreateFile} title="Создать файл" aria-label="Создать файл"><FilePlus2 size={15} /></button>}{onExplorerCollapse && <button onClick={onExplorerCollapse} title="Скрыть панель файлов" aria-label="Скрыть файлы"><PanelLeftClose size={15} /></button>}</span></header><div className="folder-label"><ChevronRight size={14} /><Folder size={15} /><span>workspace</span></div><div className="file-list">{files.map((file) => <div key={file.id} className={cn('file-entry', file.id === activeFileId && 'is-active')}><button className="file-entry__open" onClick={() => onActiveFile(file.id)}><FileCode2 size={15} /><span>{file.path}</span>{file.readOnly && <LockKeyhole size={12} />}</button>{onDeleteFile && !readOnly && !file.readOnly && (canDeleteFile?.(file) ?? files.length > 1) && <button className="file-entry__delete" onClick={() => onDeleteFile(file)} title={`Удалить ${file.path}`} aria-label={`Удалить ${file.path}`}><Trash2 size={13} /></button>}</div>)}</div><footer><span>Файлов: {files.length}</span></footer></aside>}
    <section className="editor-stage"><div className="editor-tabs">{openFiles.map((id) => { const file = files.find((item) => item.id === id); return file ? <button key={id} className={cn(id === activeFileId && 'is-active')} onClick={() => onActiveFile(id)}><FileCode2 size={14} /><span>{file.path.split('/').at(-1)}</span>{openFiles.length > 1 && <X size={13} onClick={(event) => { event.stopPropagation(); closeTab(id); }} />}</button> : null; })}<span className="editor-tabs__fill" /></div>
      <Editor path={`inmemory:///${scopeId}/${active.path}`} value={active.content} language={active.language ?? languageForPath(active.path)} beforeMount={beforeMount} onMount={handleMount} onChange={(value) => onChange(active.id, value ?? '', internalPasteRef.current ? 'internal_paste' : 'typing', internalPasteRef.current ? internalPasteReceiptRef.current : undefined)} theme={theme === 'dark' ? 'eduprog-dark' : 'eduprog-light'} options={{ readOnly: readOnly || active.readOnly, readOnlyMessage: { value: 'Редактирование недоступно в режиме чтения.' }, minimap: { enabled: true, maxColumn: 80 }, fontSize: 14, lineHeight: 23, fontFamily: "'JetBrains Mono', 'SFMono-Regular', Consolas, monospace", fontLigatures: true, padding: { top: 14, bottom: 18 }, smoothScrolling: true, cursorSmoothCaretAnimation: 'on', automaticLayout: true, scrollBeyondLastLine: false, bracketPairColorization: { enabled: true }, guides: { bracketPairs: true, indentation: true }, suggest: { showSnippets: false }, quickSuggestions: !readOnly, inlineSuggest: { enabled: false }, contextmenu: true, dragAndDrop: !strictPaste, wordWrap: 'off', renderValidationDecorations: 'on' }} />
    </section>
  </div>;
});
