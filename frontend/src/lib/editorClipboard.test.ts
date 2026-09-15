import type * as Monaco from 'monaco-editor';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createEditorClipboardSession, installEditorClipboardGuard, INTERNAL_CLIPBOARD_TYPE, type EditorClipboardSession } from './editorClipboard';

const cleanups: Array<() => void> = [];
afterEach(() => { cleanups.splice(0).forEach((dispose) => dispose()); document.body.innerHTML = ''; vi.useRealTimers(); });

function clipboard(text = '') {
  const data = new Map([['text/plain', text]]);
  return {
    getData: (type: string) => data.get(type) ?? '',
    setData: (type: string, value: string) => { data.set(type, value); },
    clearData: () => data.clear(),
  };
}

function harness(sharedSession?: EditorClipboardSession, scopeId = 'attempt-a', clipboardScopeId?: string) {
  const root = document.createElement('div');
  const container = document.createElement('div');
  const input = document.createElement('textarea');
  container.append(input); root.append(container); document.body.append(root);
  const files = [{ id: 'a', path: 'main.cpp', content: 'int value;' }, { id: 'b', path: 'other.cpp', content: 'target ' }];
  const config = {
    strictPaste: true, readOnly: false, scopeId, clipboardScopeId, active: files[0], files,
    onPasteBlocked: vi.fn(), onInternalCopy: vi.fn<(...args: [string, string, string?]) => Promise<string | null>>()
      .mockResolvedValue('receipt-1'),
  };
  let version = 1;
  let hasFocus = true;
  let selection = select(0, 3);
  const model = {
    getValue: () => config.active.content,
    getEOL: () => '\n',
    getValueInRange: (range: Monaco.IRange) => config.active.content.slice(range.startColumn - 1, range.endColumn - 1),
    getVersionId: () => version,
    getOffsetAt: (position: Monaco.IPosition) => position.column - 1,
    getPositionAt: (offset: number) => ({ lineNumber: 1, column: offset + 1 }),
    getLineCount: () => 1,
    getLineMaxColumn: () => config.active.content.length + 1,
  };
  const originalTrigger = vi.fn();
  const editor = {
    getContainerDomNode: () => container,
    getSelections: () => [selection],
    getModel: () => model,
    hasTextFocus: () => hasFocus,
    pushUndoStop: vi.fn(),
    setPosition: (position: Monaco.IPosition) => { selection = select(position.column - 1); },
    executeEdits: vi.fn((_source: string, changes: Array<{ range: Monaco.IRange; text: string }>) => {
      for (const { range, text } of changes) {
        config.active.content = config.active.content.slice(0, range.startColumn - 1) + text
          + config.active.content.slice(range.endColumn - 1);
      }
      version += 1;
    }),
    trigger: originalTrigger,
  };
  const apply = vi.fn((_receipt: string, _range: unknown, operation: () => void) => operation());
  // Model the early handler installed by Monaco on its own container.
  const monacoPaste = vi.fn();
  container.addEventListener('paste', monacoPaste, true);
  const dispose = installEditorClipboardGuard(root, editor as unknown as Monaco.editor.IStandaloneCodeEditor, () => config, apply, sharedSession);
  cleanups.push(dispose);
  const dispatch = (type: string, data = clipboard(), target: HTMLElement = input) => {
    const event = new Event(type, { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'clipboardData', { value: data });
    target.dispatchEvent(event);
    return event;
  };
  return { config, editor, apply, model, monacoPaste, originalTrigger, dispatch, input, root, dispose,
    select: (start: number, end = start) => { selection = select(start, end); },
    focus: (value: boolean) => { hasFocus = value; },
    edit: () => { version += 1; },
  };
}

function select(start: number, end = start) {
  return {
    startLineNumber: 1, endLineNumber: 1, startColumn: start + 1, endColumn: end + 1,
    isEmpty: () => start === end,
    getStartPosition: () => ({ lineNumber: 1, column: start + 1 }),
  };
}
async function settle() { for (let i = 0; i < 8; i++) await Promise.resolve(); }

describe('strict editor clipboard', () => {
  it.each(['AI generated solution', 'int'])('blocks external text, even if it already exists: %s', async (text) => {
    const h = harness();
    expect(h.dispatch('paste', clipboard(text)).defaultPrevented).toBe(true);
    await settle();
    expect(h.monacoPaste).not.toHaveBeenCalled();
    expect(h.config.onInternalCopy).not.toHaveBeenCalled();
    expect(h.editor.executeEdits).not.toHaveBeenCalled();
    expect(h.config.onPasteBlocked).toHaveBeenCalledOnce();
  });

  it('allows internal copy/paste in the same file, waiting for the receipt', async () => {
    const h = harness();
    let resolve!: (id: string) => void;
    h.config.onInternalCopy.mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    const data = clipboard();
    expect(h.dispatch('copy', data).defaultPrevented).toBe(true);
    expect(data.getData('text/plain')).toBe('int');
    expect(data.getData(INTERNAL_CLIPBOARD_TYPE)).not.toBe('');
    h.select(10);
    h.dispatch('paste', data);
    expect(h.editor.executeEdits).not.toHaveBeenCalled();
    resolve('receipt-1'); await settle();
    expect(h.config.active.content).toBe('int value;int');
    expect(h.apply).toHaveBeenCalledWith('receipt-1', { offset: 10, deleteCount: 0 }, expect.any(Function));
    expect(h.monacoPaste).not.toHaveBeenCalled();
  });

  it('allows cross-file and repeated paste with fresh one-use receipts', async () => {
    const h = harness(); const data = clipboard();
    h.dispatch('copy', data); await settle();
    h.config.active = h.config.files[1]; h.select(7);
    h.dispatch('paste', data); await settle();
    h.config.onInternalCopy.mockResolvedValueOnce('receipt-2');
    h.dispatch('paste', data); await settle();
    expect(h.config.active.content).toBe('target intint');
    expect(h.apply.mock.calls.map((call) => call[0])).toEqual(['receipt-1', 'receipt-2']);
  });

  it.each(['copy', 'cut'])('keeps pending %s proof across a question remount, without editing the disposed model', async (action) => {
    const session = createEditorClipboardSession();
    const first = harness(session, 'question-1', 'quiz-attempt');
    let resolve!: (id: string) => void;
    first.config.onInternalCopy.mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    const data = clipboard(); first.dispatch(action, data); first.dispose();
    const second = harness(session, 'question-2', 'quiz-attempt');
    second.config.active.content = 'target '; second.select(7); second.dispatch('paste', data);
    resolve('question-1-receipt'); await settle();
    expect(first.editor.executeEdits).not.toHaveBeenCalled();
    expect(second.config.active.content).toBe('target int');
    expect(second.apply).toHaveBeenCalledWith('question-1-receipt', { offset: 7, deleteCount: 0 }, expect.any(Function));
    expect(second.config.onInternalCopy).not.toHaveBeenCalled();
  });

  it('renews repeated cross-question pastes from the last destination after cutting the original', async () => {
    const session = createEditorClipboardSession();
    const first = harness(session, 'question-1', 'quiz-attempt'); const data = clipboard();
    first.dispatch('cut', data); await settle(); first.dispose();
    const second = harness(session, 'question-2', 'quiz-attempt');
    second.config.active.content = 'destination '; second.select(12); second.dispatch('paste', data); await settle();
    expect(second.config.active.content).toBe('destination int'); second.dispose();
    const returned = harness(session, 'question-1', 'quiz-attempt');
    returned.config.active.content = ' value;'; returned.select(7);
    returned.config.onInternalCopy.mockResolvedValueOnce('renewed-question-2');
    returned.dispatch('paste', data); await settle();
    expect(returned.config.onInternalCopy).toHaveBeenCalledWith('a', 'int', 'question-2');
    expect(returned.config.active.content).toBe(' value;int');
    expect(returned.apply.mock.calls[0][0]).toBe('renewed-question-2');
  });

  it('does not carry a marker into a different work or quiz attempt', async () => {
    const session = createEditorClipboardSession();
    const first = harness(session, 'question-1', 'quiz-attempt'); const data = clipboard();
    first.dispatch('copy', data); await settle(); first.dispose();
    const other = harness(session, 'other-question', 'other-quiz-attempt');
    other.select(10); other.dispatch('paste', data); await settle();
    expect(other.editor.executeEdits).not.toHaveBeenCalled();
    expect(other.config.onInternalCopy).not.toHaveBeenCalled();
    expect(session.copy).toBeNull();
  });

  it('still blocks external clipboard text after switching questions', async () => {
    const session = createEditorClipboardSession();
    const first = harness(session, 'question-1', 'quiz-attempt');
    first.dispatch('copy', clipboard()); await settle(); first.dispose();
    const second = harness(session, 'question-2', 'quiz-attempt');
    second.select(10); second.dispatch('paste', clipboard('int')); await settle();
    expect(second.editor.executeEdits).not.toHaveBeenCalled();
    expect(second.config.onInternalCopy).not.toHaveBeenCalled();
    expect(second.config.onPasteBlocked).toHaveBeenCalledOnce();
  });

  it('accepts clipboard CRLF normalization, but not any other text change', async () => {
    const h = harness(); h.config.active.content = 'int\nvalue'; h.select(0, 4);
    const data = clipboard(); h.dispatch('copy', data); await settle();
    data.setData('text/plain', 'int\r\n');
    h.config.active = h.config.files[1]; h.select(7); h.dispatch('paste', data); await settle();
    expect(h.config.active.content).toBe('target int\n');
    expect(h.config.onPasteBlocked).not.toHaveBeenCalled();
  });

  it('allows cut then paste after the source text has disappeared', async () => {
    const h = harness(); const data = clipboard();
    h.dispatch('cut', data);
    expect(h.config.active.content).toBe('int value;');
    await settle(); expect(h.config.active.content).toBe(' value;');
    h.select(7); h.dispatch('paste', data); await settle();
    expect(h.config.active.content).toBe(' value;int');
  });

  it.each(['missing', 'changed', 'wrong-token', 'expired', 'scope'])('rejects stale/foreign clipboard metadata: %s', async (reason) => {
    const h = harness(); const data = clipboard();
    h.dispatch('copy', data); await settle(); h.select(10);
    if (reason === 'missing') data.setData(INTERNAL_CLIPBOARD_TYPE, '');
    if (reason === 'wrong-token') data.setData(INTERNAL_CLIPBOARD_TYPE, 'forged');
    if (reason === 'changed') data.setData('text/plain', 'new text');
    if (reason === 'scope') h.config.scopeId = 'another-attempt';
    if (reason === 'expired') { vi.useFakeTimers(); vi.setSystemTime(Date.now() + 300_001); }
    h.dispatch('paste', data); await settle();
    expect(h.editor.executeEdits).not.toHaveBeenCalled();
    expect(h.config.onPasteBlocked).toHaveBeenCalled();
  });

  it('rejects a text-only Monaco command, without affecting typing commands', () => {
    const h = harness();
    h.editor.trigger('keyboard', 'paste', { text: 'outside' });
    expect(h.originalTrigger).not.toHaveBeenCalled();
    h.editor.trigger('keyboard', 'type', { text: 'x' });
    expect(h.originalTrigger).toHaveBeenCalledWith('keyboard', 'type', { text: 'x' });
  });

  it.each(['selection', 'content', 'readOnly', 'copy', 'dispose'])('does not apply a delayed paste after %s changed', async (reason) => {
    const h = harness(); let resolve!: (id: string) => void;
    h.config.onInternalCopy.mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    const data = clipboard(); h.dispatch('copy', data); h.select(10); h.dispatch('paste', data);
    if (reason === 'selection') h.select(5);
    if (reason === 'content') h.edit();
    if (reason === 'readOnly') h.config.readOnly = true;
    if (reason === 'copy') { h.select(4, 9); h.dispatch('copy', clipboard()); }
    if (reason === 'dispose') h.dispose();
    resolve('receipt'); await settle();
    expect(h.editor.executeEdits).not.toHaveBeenCalled();
  });

  it('does not cut or paste if server verification fails', async () => {
    const h = harness(); h.config.onInternalCopy.mockRejectedValue(new Error('offline'));
    const data = clipboard(); h.dispatch('cut', data); await settle();
    h.select(10); h.dispatch('paste', data); await settle();
    expect(h.editor.executeEdits).not.toHaveBeenCalled();
  });

  it('does not mint receipts when copying from find/replace or outside the editor', () => {
    const h = harness(); h.focus(false); h.dispatch('copy', clipboard());
    h.dispatch('copy', clipboard(), h.root);
    expect(h.config.onInternalCopy).not.toHaveBeenCalled();
  });

  it.each(['insertFromPaste', 'insertFromPasteAsQuotation', 'insertFromDrop', 'insertFromYank'])('blocks alternate input: %s', (inputType) => {
    const h = harness();
    const event = new InputEvent('beforeinput', { inputType, bubbles: true, cancelable: true });
    h.input.dispatchEvent(event); expect(event.defaultPrevented).toBe(true);
  });

  it('blocks drop/middle-click but preserves IME and normal typing', () => {
    const h = harness(); expect(h.dispatch('drop').defaultPrevented).toBe(true);
    const middle = new MouseEvent('mousedown', { button: 1, bubbles: true, cancelable: true });
    h.input.dispatchEvent(middle); expect(middle.defaultPrevented).toBe(true);
    for (const inputType of ['insertText', 'insertCompositionText', 'historyUndo', 'historyRedo']) {
      const event = new InputEvent('beforeinput', { inputType, bubbles: true, cancelable: true });
      h.input.dispatchEvent(event); expect(event.defaultPrevented).toBe(false);
    }
  });

  it('converts UTF-16 selection positions to Unicode code points', async () => {
    const h = harness(); const data = clipboard(); h.dispatch('copy', data); await settle();
    h.config.active = h.config.files[1]; h.config.active.content = '😀 x'; h.select(3, 4);
    h.dispatch('paste', data); await settle();
    expect(h.apply).toHaveBeenCalledWith('receipt-1', { offset: 2, deleteCount: 1 }, expect.any(Function));
    expect(h.config.active.content).toBe('😀 int');
  });

  it('leaves unrestricted workspaces and disposed editors unchanged', () => {
    const h = harness(); h.config.strictPaste = false;
    expect(h.dispatch('paste', clipboard('external')).defaultPrevented).toBe(false);
    expect(h.monacoPaste).toHaveBeenCalledOnce();
    h.editor.trigger('keyboard', 'paste', { text: 'external' });
    expect(h.originalTrigger).toHaveBeenCalledOnce();
    h.dispose(); expect(h.editor.trigger).toBe(h.originalTrigger);
  });
});
