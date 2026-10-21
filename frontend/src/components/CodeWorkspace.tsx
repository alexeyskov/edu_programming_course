import type { BeforeMount, OnMount } from '@monaco-editor/react';
import { MonacoEditor as Editor } from './MonacoEditors';
import { MobileCodePreview, type MobileCodePreviewHandle } from './MobileCodePreview';
import type * as Monaco from 'monaco-editor';
import { ChevronRight, FileCode2, FilePlus2, Folder, LockKeyhole, PanelLeftClose, Trash2, WrapText, X } from 'lucide-react';
import { forwardRef, useEffect, useId, useImperativeHandle, useMemo, useRef, useState } from 'react';
import { useTheme } from '../context/ThemeContext';
import { cn, languageForPath } from '../lib/utils';
import { installEditorClipboardGuard, type EditorClipboardSession } from '../lib/editorClipboard';
import { installAiSelectionAction, type AiCodeSelection } from '../lib/editorAiSelection';
import { installCodeIntelligence, type CodeIntelligenceScope, type CodeIntelligenceStatus } from '../lib/codeIntelligence';
import { useMediaQuery } from '../lib/workspaceLayout';
import type { AiCodeFinding, Diagnostic, InternalPasteRange, WorkspaceFile } from '../types';
import { Badge, Button } from './ui';
import './aiAnnotations.css';
import './codeIntelligence.css';

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
  onChange(fileId: string, content: string, source: 'typing' | 'internal_paste', receiptId?: string, pasteRange?: InternalPasteRange): void;
  onCreateFile?(): void;
  onDeleteFile?(file: WorkspaceFile): void;
  canDeleteFile?(file: WorkspaceFile): boolean;
  readOnly?: boolean;
  strictPaste?: boolean;
  scopeId: string;
  intelligenceScope?: CodeIntelligenceScope;
  clipboardScopeId?: string;
  clipboardSession?: EditorClipboardSession;
  diagnostics?: Diagnostic[];
  aiFindings?: AiCodeFinding[];
  onAskAi?(finding: AiCodeFinding): void;
  onAskAiSelection?(selection: AiCodeSelection): void;
  allowEditableAiSelection?: boolean;
  highlights?: CodeHighlight[];
  theme?: 'light' | 'dark';
  experiment?: boolean;
  explorerVisible?: boolean;
  explorerId?: string;
  onExplorerCollapse?(): void;
  onPasteBlocked?(): void;
  onInternalCopy?(fileId: string, text: string, sourceAttemptId?: string): Promise<string | null>;
}

const severityMap: Record<Diagnostic['severity'], Monaco.MarkerSeverity> = {
  error: 8 as Monaco.MarkerSeverity, warning: 4 as Monaco.MarkerSeverity, info: 2 as Monaco.MarkerSeverity,
};

export const CodeWorkspace = forwardRef<CodeWorkspaceHandle, Props>(function CodeWorkspace({
  files, activeFileId, onActiveFile, onChange, onCreateFile, onDeleteFile, canDeleteFile,
  readOnly: requestedReadOnly = false, strictPaste = false,
  scopeId, diagnostics = [], aiFindings = [], onAskAi, onAskAiSelection, allowEditableAiSelection = false, highlights = [], theme: requestedTheme, experiment = false, explorerVisible = true, explorerId,
  onExplorerCollapse, onPasteBlocked, onInternalCopy, clipboardScopeId, clipboardSession, intelligenceScope,
}, ref) {
  const mobile = useMediaQuery('(max-width: 767px)');
  const readOnly = requestedReadOnly || mobile;
  const { theme: applicationTheme } = useTheme();
  const theme = requestedTheme ?? applicationTheme;
  const editorRef = useRef<Monaco.editor.IStandaloneCodeEditor | null>(null);
  const mobilePreviewRef = useRef<MobileCodePreviewHandle>(null);
  const monacoRef = useRef<typeof Monaco | null>(null);
  const highlightDecorationsRef = useRef<Monaco.editor.IEditorDecorationsCollection | null>(null);
  const aiMarkerOwner = `ai-review-${useId()}`;
  const aiAskCommand = `eduprog.ask-ai.${useId().replace(/:/g, '')}`;
  const aiActionRef = useRef<Monaco.IDisposable>();
  const aiConfigRef = useRef({ aiFindings, onAskAi, scopeId, readOnly, experiment });
  aiConfigRef.current = { aiFindings, onAskAi, scopeId, readOnly, experiment };
  const aiSelectionRef = useRef<ReturnType<typeof installAiSelectionAction>>();
  const selectionConfigRef = useRef({ scopeId, readOnly, experiment, allowEditable: allowEditableAiSelection, onAsk: onAskAiSelection });
  selectionConfigRef.current = { scopeId, readOnly, experiment, allowEditable: allowEditableAiSelection, onAsk: onAskAiSelection };
  const aiModelRef = useRef<Monaco.editor.ITextModel | null>(null);
  const aiDecorationsRef = useRef<Monaco.editor.IEditorDecorationsCollection | null>(null);
  const clipboardRootRef = useRef<HTMLDivElement | null>(null);
  const internalPasteRef = useRef(false);
  const internalPasteReceiptRef = useRef<string>();
  const internalPasteRangeRef = useRef<InternalPasteRange>();
  const [openFiles, setOpenFiles] = useState<string[]>([activeFileId]);
  const active = files.find((file) => file.id === activeFileId) ?? files[0];
  const activeRef = useRef(active);
  const filesRef = useRef(files);
  const intelligenceRef = useRef<ReturnType<typeof installCodeIntelligence>>();
  const [intelligenceStatus, setIntelligenceStatus] = useState<CodeIntelligenceStatus>('connecting');
  const intelligenceConfigRef = useRef({ scopeId, scope: intelligenceScope, files, readOnly, onOpenFile: onActiveFile, onStatus: setIntelligenceStatus });
  intelligenceConfigRef.current = { scopeId, scope: intelligenceScope, files, readOnly, onOpenFile: onActiveFile, onStatus: setIntelligenceStatus };
  const pasteConfigRef = useRef({ strictPaste, readOnly, scopeId, clipboardScopeId, onPasteBlocked, onInternalCopy });
  activeRef.current = active;
  filesRef.current = files;
  pasteConfigRef.current = { strictPaste, readOnly, scopeId, clipboardScopeId, onPasteBlocked, onInternalCopy };

  useEffect(() => { intelligenceRef.current?.refresh(); }, [scopeId, intelligenceScope?.id, intelligenceScope?.kind, files, activeFileId, readOnly]);

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

  const refreshAiAction = useMemo(() => () => {
    const editor = editorRef.current;
    const monaco = monacoRef.current;
    const config = aiConfigRef.current;
    if (!editor || !monaco) return;
    if (!config.onAskAi || !config.readOnly || config.experiment) {
      aiActionRef.current?.dispose();
      aiActionRef.current = undefined;
      return;
    }
    if (aiActionRef.current) return;
    aiActionRef.current = editor.addAction({
      id: aiAskCommand, label: 'Спросить у ИИ',
      run: (_editor, requestedScope: unknown, path: unknown, index: unknown, fingerprint: unknown) => {
        const current = aiConfigRef.current;
        if (!current.readOnly || current.experiment || !current.onAskAi) return;
        if (requestedScope !== current.scopeId || typeof path !== 'string' || typeof index !== 'number' || !Number.isInteger(index)) return;
        const finding = current.aiFindings[index];
        if (!finding || finding.path !== path || fingerprint !== `${finding.line}:${finding.endLine}:${finding.message}`) return;
        if (editor.getModel()?.uri.toString() !== monaco.Uri.parse(`inmemory:///${current.scopeId}/${path}`).toString()) return;
        current.onAskAi(finding);
      },
    });
  }, [aiAskCommand]);
  useEffect(() => { refreshAiAction(); }, [refreshAiAction, onAskAi, readOnly, experiment]);

  const clearAiAnnotations = useMemo(() => () => {
    const previous = aiModelRef.current;
    if (previous && !previous.isDisposed()) monacoRef.current?.editor.setModelMarkers(previous, aiMarkerOwner, []);
    aiModelRef.current = null;
    aiDecorationsRef.current?.clear();
    aiDecorationsRef.current = null;
  }, [aiMarkerOwner]);

  const setAiAnnotations = useMemo(() => () => {
    clearAiAnnotations();
    const editor = editorRef.current;
    const monaco = monacoRef.current;
    const model = editor?.getModel();
    if (!editor || !monaco || !model || model.isDisposed() || !active) return;
    // A model change may precede the React effect. Never decorate the previous file/scope.
    if (model.uri.toString() !== monaco.Uri.parse(`inmemory:///${scopeId}/${active.path}`).toString()) return;
    const findings = aiFindings.filter((item) => item.path === active.path
      && Number.isInteger(item.line) && item.line >= 1 && item.line <= model.getLineCount()
      && (item.endLine === undefined || (Number.isInteger(item.endLine) && item.endLine >= item.line && item.endLine <= model.getLineCount()))
      && ['warning', 'info', 'error'].includes(item.severity)
      && typeof item.message === 'string' && item.message.trim().length > 0);
    if (!findings.length) return;
    // addAction registers an editor-prefixed command, not the descriptor ID.
    const hoverCommand = onAskAi && readOnly && !experiment ? editor.getAction(aiAskCommand)?.id : undefined;
    const annotations = findings.map((item) => ({
      range: {
        startLineNumber: item.line, startColumn: 1,
        endLineNumber: item.endLine ?? item.line,
        endColumn: model.getLineMaxColumn(item.endLine ?? item.line),
      },
      message: item.message.trim().slice(0, 4000),
      findingIndex: aiFindings.indexOf(item),
      fingerprint: `${item.line}:${item.endLine}:${item.message}`,
      severity: severityMap[item.severity],
    }));
    aiModelRef.current = model;
    monaco.editor.setModelMarkers(model, aiMarkerOwner, annotations.map((item) => ({
      ...item.range, severity: item.severity, message: item.message, source: 'Ответ ИИ',
    })));
    aiDecorationsRef.current = editor.createDecorationsCollection(annotations.map((item) => ({
      range: item.range,
      options: {
        isWholeLine: true,
        className: 'ai-review-line',
        linesDecorationsClassName: 'ai-review-gutter',
        // Monaco already renders the marker's message and source in the hover.
        // The decoration adds only our action, never a duplicate message or model-provided Markdown.
        hoverMessage: hoverCommand ? {
          value: `[Спросить у ИИ](command:${hoverCommand}?${encodeURIComponent(JSON.stringify([scopeId, active.path, item.findingIndex, item.fingerprint]))})`,
          isTrusted: { enabledCommands: [hoverCommand] }, supportHtml: false,
        } : undefined,
      },
    })));
  }, [active, aiFindings, aiMarkerOwner, aiAskCommand, onAskAi, readOnly, experiment, clearAiAnnotations, scopeId]);
  const refreshAiAnnotationsRef = useRef(setAiAnnotations);
  refreshAiAnnotationsRef.current = setAiAnnotations;
  useEffect(() => { setAiAnnotations(); return clearAiAnnotations; }, [setAiAnnotations, clearAiAnnotations]);
  useEffect(() => { aiSelectionRef.current?.refresh(); }, [scopeId, readOnly, experiment, allowEditableAiSelection, onAskAiSelection, active?.path]);

  useImperativeHandle(ref, () => ({
    openDiagnostic(diagnostic) {
      const file = files.find((item) => item.id === diagnostic.fileId || item.path === diagnostic.path);
      if (file) onActiveFile(file.id);
      window.setTimeout(() => {
        if (mobile) { mobilePreviewRef.current?.revealLine(diagnostic.line ?? 1); return; }
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
        if (mobile) { mobilePreviewRef.current?.revealLine(startLine); return; }
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
    focus() { if (!mobile) editorRef.current?.focus(); },
  }), [files, onActiveFile, mobile]);

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
    editorRef.current = editor; monacoRef.current = monaco;
    aiSelectionRef.current = installAiSelectionAction(editor, monaco, () => ({ ...selectionConfigRef.current, path: activeRef.current?.path }));
    refreshAiAction(); setMarkers(); setHighlights();
    // Monaco keeps the initial onMount callback during asynchronous startup.
    // Apply the current report, file and toggle state, not the captured initial ones.
    refreshAiAnnotationsRef.current();
    const aiModelListener = editor.onDidChangeModel(() => refreshAiAnnotationsRef.current());
    intelligenceRef.current = installCodeIntelligence(editor, monaco, () => intelligenceConfigRef.current);
    const dom = clipboardRootRef.current;
    if (!dom) { intelligenceRef.current.dispose(); return; }
    const disposeClipboard = installEditorClipboardGuard(dom, editor, () => ({
      ...pasteConfigRef.current, active: activeRef.current, files: filesRef.current,
    }), (receiptId, range, apply) => {
      internalPasteReceiptRef.current = receiptId;
      internalPasteRangeRef.current = range;
      internalPasteRef.current = true;
      try { apply(); }
      finally {
        internalPasteRef.current = false;
        internalPasteReceiptRef.current = undefined;
        internalPasteRangeRef.current = undefined;
      }
    }, clipboardSession);
    editor.onDidDispose(() => {
      aiSelectionRef.current?.dispose();
      aiSelectionRef.current = undefined;
      aiActionRef.current?.dispose();
      aiActionRef.current = undefined;
      clearAiAnnotations();
      aiModelListener.dispose();
      highlightDecorationsRef.current?.clear();
      highlightDecorationsRef.current = null;
      intelligenceRef.current?.dispose();
      intelligenceRef.current = undefined;
      disposeClipboard();
      if (editorRef.current === editor) editorRef.current = null;
    });
  };

  function closeTab(id: string) {
    const remaining = openFiles.filter((item) => item !== id);
    setOpenFiles(remaining);
    if (id === activeFileId) onActiveFile(remaining.at(-1) ?? files[0]?.id);
  }

  if (!active) return <div className="editor-empty"><FileCode2 /><p>В рабочей области пока нет файлов</p>{onCreateFile && !readOnly && <Button variant="secondary" onClick={onCreateFile}><FilePlus2 size={16} /> Создать файл</Button>}</div>;

  return <div ref={clipboardRootRef} className={cn('code-workspace', experiment && 'code-workspace--experiment', !explorerVisible && 'code-workspace--explorer-hidden')}>
    {mobile && <label className="workspace-file-picker"><FileCode2 size={17} /><select aria-label="Файл для просмотра" value={active.id} onChange={(event) => onActiveFile(event.target.value)}>{files.map((file) => <option key={file.id} value={file.id}>{file.path}</option>)}</select><LockKeyhole size={14} aria-label="Только чтение" /></label>}
    <aside id={explorerId} className="file-explorer" hidden={!explorerVisible}><header><span>Файлы</span><span className="file-explorer__actions">{onCreateFile && !readOnly && <button onClick={onCreateFile} title="Создать файл" aria-label="Создать файл"><FilePlus2 size={15} /></button>}{onExplorerCollapse && <button onClick={onExplorerCollapse} title="Скрыть панель файлов" aria-label="Скрыть файлы"><PanelLeftClose size={15} /></button>}</span></header><div className="folder-label"><ChevronRight size={14} /><Folder size={15} /><span>workspace</span></div><div className="file-list">{files.map((file) => <div key={file.id} className={cn('file-entry', file.id === activeFileId && 'is-active')}><button className="file-entry__open" onClick={() => onActiveFile(file.id)}><FileCode2 size={15} /><span>{file.path}</span>{file.readOnly && <LockKeyhole size={12} />}</button>{onDeleteFile && !readOnly && !file.readOnly && (canDeleteFile?.(file) ?? files.length > 1) && <button className="file-entry__delete" onClick={() => onDeleteFile(file)} title={`Удалить ${file.path}`} aria-label={`Удалить ${file.path}`}><Trash2 size={13} /></button>}</div>)}</div><footer><span>Файлов: {files.length}</span></footer></aside>
    <section className="editor-stage"><div className="editor-tabs">{openFiles.map((id) => { const file = files.find((item) => item.id === id); return file ? <button key={id} className={cn(id === activeFileId && 'is-active')} onClick={() => onActiveFile(id)}><FileCode2 size={14} /><span>{file.path.split('/').at(-1)}</span>{openFiles.length > 1 && <X size={13} onClick={(event) => { event.stopPropagation(); closeTab(id); }} />}</button> : null; })}<span className="editor-tabs__fill" />{intelligenceScope && <div className="code-intelligence-tools"><span className={`code-intelligence-status code-intelligence-status--${intelligenceStatus}`} title={intelligenceStatus === 'ready' ? 'Подсказки C/C++, параметры функций, переход к определению (F12), ссылки (Shift+F12), символы файла (Ctrl/Cmd+Shift+O). Диагностика редактора не заменяет запуск компилятора.' : intelligenceStatus === 'fallback' ? 'clangd временно недоступен. Работают локальные подсказки; сохранение, сдача и запуск кода доступны.' : 'Подключение подсказок C/C++. Сохранение и запуск кода доступны.'}>{intelligenceStatus === 'ready' ? 'C/C++' : intelligenceStatus === 'fallback' ? 'Базовые подсказки' : 'C/C++…'}</span>{!readOnly && !active.readOnly && <button type="button" className="code-intelligence-format" aria-label="Форматировать код" title="Форматировать код (Shift+Alt+F)" onClick={() => { void intelligenceRef.current?.format(); }}><WrapText size={15} /></button>}</div>}</div>
      {mobile ? <MobileCodePreview key={`${scopeId}:${active.id}`} ref={mobilePreviewRef} file={active} aiFindings={aiFindings} /> : <Editor path={`inmemory:///${scopeId}/${active.path}`} value={active.content} language={active.language ?? languageForPath(active.path)} beforeMount={beforeMount} onMount={handleMount}
        onChange={(value) => { if (!readOnly && !active.readOnly) onChange(active.id, value ?? '', internalPasteRef.current ? 'internal_paste' : 'typing', internalPasteRef.current ? internalPasteReceiptRef.current : undefined, internalPasteRef.current ? internalPasteRangeRef.current : undefined); }}
        theme={theme === 'dark' ? 'eduprog-dark' : 'eduprog-light'} options={{
          // Keep clickable teacher AI hovers outside the editor row's overflow clip.
          fixedOverflowWidgets: Boolean(onAskAi || onAskAiSelection),
          // No documentHighlight provider is registered. Avoid Monaco 0.52's
          // idle highlighter cancellation on file switches; selection/search
          // highlighting and explicit reference navigation remain available.
          occurrencesHighlight: 'off',
          quickSuggestionsDelay: 200,
          readOnly: readOnly || active.readOnly, readOnlyMessage: { value: mobile ? 'На телефоне доступен просмотр. Продолжите редактирование на компьютере.' : 'Редактирование недоступно в режиме чтения.' }, minimap: { enabled: !mobile, maxColumn: 80 }, fontSize: 14, lineHeight: 23, lineNumbersMinChars: mobile ? 3 : 4, fontFamily: "'JetBrains Mono', 'SFMono-Regular', Consolas, monospace", fontLigatures: true, padding: { top: 14, bottom: 18 }, smoothScrolling: true, cursorSmoothCaretAnimation: 'on', automaticLayout: true, scrollBeyondLastLine: false, bracketPairColorization: { enabled: true }, guides: { bracketPairs: true, indentation: true }, suggest: { showSnippets: false }, quickSuggestions: !readOnly, inlineSuggest: { enabled: false }, contextmenu: !strictPaste, pasteAs: { enabled: !strictPaste }, dropIntoEditor: { enabled: !strictPaste }, dragAndDrop: !strictPaste, wordWrap: mobile ? 'on' : 'off', renderValidationDecorations: 'on',
        }} />}
    </section>
  </div>;
});
