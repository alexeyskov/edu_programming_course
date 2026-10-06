import type * as Monaco from 'monaco-editor';

export interface AiCodeSelection {
  path: string;
  startLine: number;
  endLine: number;
  text: string;
}
interface SelectionConfig {
  scopeId: string; path?: string; readOnly: boolean; experiment: boolean;
  onAsk?(selection: AiCodeSelection): void;
}

/** Opt-in teacher-only action; selecting text never sends a model request. */
export function installAiSelectionAction(
  editor: Monaco.editor.IStandaloneCodeEditor,
  monaco: Pick<typeof Monaco, 'Uri'>,
  getConfig: () => SelectionConfig,
) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'ai-selection-action';
  button.textContent = 'Спросить у ИИ';
  button.title = 'Прикрепить выделенный код к вопросу в чате';
  let position: Monaco.IPosition | null = null;
  let mounted = false;
  const widget: Monaco.editor.IContentWidget = {
    getId: () => 'eduprog.ai-selection',
    getDomNode: () => button,
    getPosition: () => position ? { position, preference: [1, 2] } : null, // ABOVE, BELOW
    allowEditorOverflow: true,
    suppressMouseDown: true,
  };
  function currentSelection(): AiCodeSelection | null {
    const config = getConfig();
    const selection = editor.getSelection();
    const model = editor.getModel();
    if (!config.readOnly || config.experiment || !config.onAsk || !config.path || !selection || selection.isEmpty() || !model) return null;
    // Monaco canonicalizes custom URIs (inmemory:/// -> inmemory:/) and encodes paths.
    const expectedUri = monaco.Uri.parse(`inmemory:///${config.scopeId}/${config.path}`);
    if (model.uri.toString() !== expectedUri.toString()) return null;
    const text = model.getValueInRange(selection);
    if (!text.trim()) return null;
    return { path: config.path, startLine: selection.startLineNumber, endLine: selection.endLineNumber, text };
  }
  function refresh() {
    const selection = currentSelection();
    position = selection ? editor.getSelection()!.getPosition() : null;
    if (position && !mounted) { editor.addContentWidget(widget); mounted = true; }
    else if (!position && mounted) { editor.removeContentWidget(widget); mounted = false; }
    if (mounted) editor.layoutContentWidget(widget);
  }
  const preventSelectionLoss = (event: MouseEvent) => event.preventDefault();
  const ask = () => {
    const selected = currentSelection();
    if (selected) getConfig().onAsk?.(selected);
  };
  button.addEventListener('mousedown', preventSelectionLoss);
  button.addEventListener('click', ask);
  const selectionListener = editor.onDidChangeCursorSelection(refresh);
  const modelListener = editor.onDidChangeModel(refresh);
  refresh();
  return {
    refresh,
    dispose() {
      selectionListener.dispose(); modelListener.dispose();
      if (mounted) editor.removeContentWidget(widget);
      button.removeEventListener('mousedown', preventSelectionLoss);
      button.removeEventListener('click', ask);
    },
  };
}
