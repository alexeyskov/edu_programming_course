import type * as Monaco from 'monaco-editor';
import type { InternalPasteRange, WorkspaceFile } from '../types';
import { findWorkspacePasteSource, normalizeClipboardText } from './utils';
import { createUuid } from './uuid';

export const INTERNAL_CLIPBOARD_TYPE = 'application/x-eduprog-workspace-copy';
const COPY_TTL_MS = 5 * 60_000;

interface ClipboardConfig {
  strictPaste: boolean;
  readOnly: boolean;
  /** Model/workspace identity, independent from the shared quiz clipboard scope. */
  scopeId: string;
  clipboardScopeId?: string;
  active?: WorkspaceFile;
  files: WorkspaceFile[];
  onPasteBlocked?(): void;
  onInternalCopy?(fileId: string, text: string, sourceAttemptId?: string): Promise<string | null>;
}

interface CopyRecord {
  token: string;
  scopeId: string;
  text: string;
  createdAt: number;
  ready: Promise<string | null>;
  used: boolean;
  sourceAttemptId: string;
  sourceFileId: string;
}

/** Kept by the IDE page, never in localStorage or shared with another browser tab. */
export interface EditorClipboardSession {
  copy: CopyRecord | null;
}

export function createEditorClipboardSession(): EditorClipboardSession {
  return { copy: null };
}

/** UI barrier, not a trusted-client boundary. Never infer provenance from text alone. */
export function installEditorClipboardGuard(
  root: HTMLElement,
  editor: Monaco.editor.IStandaloneCodeEditor,
  getConfig: () => ClipboardConfig,
  applyInternalPaste: (receiptId: string, range: InternalPasteRange, apply: () => void) => void,
  sharedSession?: EditorClipboardSession,
): () => void {
  const session = sharedSession ?? createEditorClipboardSession();
  const clipboardScope = () => getConfig().clipboardScopeId ?? getConfig().scopeId;
  // Changing work/attempt never carries a confirmation into the next work.
  if (session.copy?.scopeId !== clipboardScope()) session.copy = null;
  let disposed = false;
  let pasting = false;
  const container = editor.getContainerDomNode();
  const inEditor = (event: Event) => event.target instanceof Node && container.contains(event.target);
  const block = () => getConfig().onPasteBlocked?.();
  const cancel = (event: Event) => { event.preventDefault(); event.stopImmediatePropagation(); };
  const currentSelection = () => {
    const selections = editor.getSelections();
    return selections?.length === 1 ? selections[0] : null;
  };
  const sameSelection = (range: Monaco.IRange) => {
    const selection = currentSelection();
    return selection && selection.startLineNumber === range.startLineNumber
      && selection.startColumn === range.startColumn && selection.endLineNumber === range.endLineNumber
      && selection.endColumn === range.endColumn;
  };

  const copyHandler = (event: ClipboardEvent) => {
    const config = getConfig();
    if (!config.strictPaste || !inEditor(event)) return;
    // Find/replace widgets and other page text are not code-copy sources.
    if (!editor.hasTextFocus()) { session.copy = null; return; }
    if (config.readOnly) { session.copy = null; return; }
    cancel(event);
    session.copy = null;
    const model = editor.getModel();
    const selection = currentSelection();
    if (!model || !selection || !config.active || !config.onInternalCopy || !event.clipboardData
      || config.readOnly) { block(); return; }
    let range: Monaco.IRange = selection;
    if (selection.isEmpty()) {
      // Match the usual editor action: Ctrl/Cmd+C without selection copies a line.
      const line = selection.startLineNumber;
      range = line < model.getLineCount()
        ? { startLineNumber: line, startColumn: 1, endLineNumber: line + 1, endColumn: 1 }
        : { startLineNumber: line, startColumn: 1, endLineNumber: line, endColumn: model.getLineMaxColumn(line) };
    }
    const text = model.getValueInRange(range);
    if (!text || new TextEncoder().encode(text).length > 262_144) { block(); return; }
    const record: CopyRecord = {
      token: createUuid(), scopeId: clipboardScope(), text, createdAt: Date.now(),
      ready: Promise.resolve(null), used: false,
      sourceAttemptId: config.scopeId, sourceFileId: config.active.id,
    };
    try {
      event.clipboardData.clearData();
      event.clipboardData.setData('text/plain', text);
      event.clipboardData.setData(INTERNAL_CLIPBOARD_TYPE, record.token);
    } catch { block(); return; }
    session.copy = record;
    const version = model.getVersionId();
    // A cut must not remove the source before the server has verified it.
    record.ready = config.onInternalCopy(config.active.id, text).then((receiptId) => {
      const latest = getConfig();
      // Keep the proof when a sibling question remounts the editor. Only the
      // original, still-mounted editor may perform the deferred cut itself.
      if (!receiptId || session.copy !== record || clipboardScope() !== record.scopeId) return null;
      if (!disposed && event.type === 'cut' && !latest.readOnly && !latest.active?.readOnly
        && editor.getModel() === model && model.getVersionId() === version && sameSelection(selection)) {
        editor.pushUndoStop();
        editor.executeEdits('internal-cut', [{ range, text: '', forceMoveMarkers: true }]);
        editor.setPosition({ lineNumber: range.startLineNumber, column: range.startColumn });
        editor.pushUndoStop();
      }
      return receiptId;
    }).catch(() => null);
  };

  const pasteHandler = (event: ClipboardEvent) => {
    const config = getConfig();
    if (!config.strictPaste || !inEditor(event)) return;
    cancel(event);
    const record = session.copy;
    const text = normalizeClipboardText(event.clipboardData?.getData('text/plain') ?? '');
    const token = event.clipboardData?.getData(INTERNAL_CLIPBOARD_TYPE) ?? '';
    const validCopy = () => !disposed && session.copy === record && record !== null
      && record.scopeId === clipboardScope() && record.token === token && normalizeClipboardText(record.text) === text
      && Date.now() - record.createdAt >= 0 && Date.now() - record.createdAt < COPY_TTL_MS;
    if (!validCopy() || config.readOnly || !config.active || config.active.readOnly || !editor.hasTextFocus()
      || pasting) { block(); return; }
    const selection = currentSelection();
    const model = editor.getModel();
    if (!record || !selection || !model) { block(); return; }
    if (normalizeClipboardText(model.getValueInRange(selection)) === text) return;
    const version = model.getVersionId();
    const fileId = config.active.id;
    pasting = true;
    void (async () => {
      let receiptId: string | null;
      if (!record.used) receiptId = await record.ready;
      else {
        // Renew a one-use receipt only for a still-marked internal copy, never for
        // arbitrary clipboard text that happens to match a file.
        const source = findWorkspacePasteSource(getConfig().files, text);
        receiptId = config.onInternalCopy
          ? source ? await config.onInternalCopy(source.id, text)
            : await config.onInternalCopy(record.sourceFileId, text, record.sourceAttemptId)
          : null;
      }
      const latest = getConfig();
      if (!receiptId || !validCopy() || !latest.strictPaste || latest.readOnly || latest.active?.readOnly
        || latest.scopeId !== config.scopeId || latest.active?.id !== fileId || editor.getModel() !== model
        || model.getVersionId() !== version || !sameSelection(selection)) {
        if (!disposed) block();
        return;
      }
      const offset = model.getOffsetAt(selection.getStartPosition());
      const range: InternalPasteRange = {
        // Monaco offsets are UTF-16; the backend/history use Unicode code points.
        offset: Array.from(model.getValue().slice(0, offset)).length,
        deleteCount: Array.from(model.getValueInRange(selection)).length,
      };
      record.used = true;
      applyInternalPaste(receiptId, range, () => {
        editor.pushUndoStop();
        editor.executeEdits('internal-paste', [{ range: selection, text, forceMoveMarkers: true }]);
        editor.setPosition(model.getPositionAt(offset + text.replace(/\n/g, model.getEOL()).length));
        editor.pushUndoStop();
      });
      // The pasted destination is a source for a later one-use confirmation,
      // including after a cut left the original question without this fragment.
      record.sourceAttemptId = config.scopeId;
      record.sourceFileId = fileId;
    })().catch(() => { if (!disposed) block(); }).finally(() => { pasting = false; });
  };

  const dropHandler = (event: DragEvent) => {
    if (getConfig().strictPaste && inEditor(event)) { cancel(event); block(); }
  };
  const beforeInputHandler = (event: InputEvent) => {
    if (getConfig().strictPaste && inEditor(event)
      && /^(insertFromPaste|insertFromDrop|insertFromYank)/.test(event.inputType)) {
      cancel(event); block();
    }
  };
  const middleClickHandler = (event: MouseEvent) => {
    if (getConfig().strictPaste && inEditor(event) && event.button === 1) { cancel(event); block(); }
  };
  // Monaco's menu can readText() then trigger('paste') without a ClipboardEvent.
  // Such a text-only command has no provenance. Native paste is handled above.
  const originalTrigger = editor.trigger;
  const guardedTrigger: typeof editor.trigger = function (source, handlerId, payload) {
    if (getConfig().strictPaste && handlerId === 'paste') { block(); return; }
    return originalTrigger.call(editor, source, handlerId, payload);
  };
  editor.trigger = guardedTrigger;

  // This ancestor must capture BEFORE Monaco's own container-level handlers.
  root.addEventListener('copy', copyHandler, true);
  root.addEventListener('cut', copyHandler, true);
  root.addEventListener('paste', pasteHandler, true);
  root.addEventListener('drop', dropHandler, true);
  root.addEventListener('beforeinput', beforeInputHandler, true);
  root.addEventListener('mousedown', middleClickHandler, true);
  return () => {
    disposed = true;
    if (!sharedSession) session.copy = null;
    root.removeEventListener('copy', copyHandler, true);
    root.removeEventListener('cut', copyHandler, true);
    root.removeEventListener('paste', pasteHandler, true);
    root.removeEventListener('drop', dropHandler, true);
    root.removeEventListener('beforeinput', beforeInputHandler, true);
    root.removeEventListener('mousedown', middleClickHandler, true);
    if (editor.trigger === guardedTrigger) editor.trigger = originalTrigger;
  };
}
