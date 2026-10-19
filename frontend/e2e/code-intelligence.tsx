// Development-only fixture: excluded from the production build.
import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { loader } from '@monaco-editor/react';
import { CodeWorkspace } from '../src/components/CodeWorkspace';
import { ThemeProvider } from '../src/context/ThemeContext';
import { monaco } from '../src/lib/monacoSetup';
import type { AiCodeFinding, WorkspaceFile } from '../src/types';
import '../src/styles.css';

declare global { interface Window { smokeMonaco: typeof monaco; smokeEditorWaiting?: boolean } }
window.smokeMonaco = monaco;
const params = new URLSearchParams(window.location.search);
const aiHoverFixture = params.has('ai-hover');
const deferredEditorFixture = params.has('defer-editor');
let openEditor: (() => void) | undefined;
if (deferredEditorFixture) {
  const ready = new Promise<void>((resolve) => { openEditor = resolve; });
  const initialize = loader.init.bind(loader);
  loader.init = () => {
    const initialization = initialize();
    window.smokeEditorWaiting = true;
    return Object.assign(ready.then(() => initialization), { cancel: () => initialization.cancel() });
  };
}
const aiFinding: AiCodeFinding = {
  path: 'main.cpp', line: 4, severity: 'warning',
  message: 'Проверьте границы массива. [не выполнять](command:forged) <script>alert(1)</script>',
};
function Fixture() {
  const [scope, setScope] = useState<'attempt' | 'submission' | 'experiment'>(aiHoverFixture ? 'submission' : 'attempt');
  const [asked, setAsked] = useState(0);
  const [findings, setFindings] = useState<AiCodeFinding[]>(deferredEditorFixture ? [] : [aiFinding]);
  const [active, setActive] = useState('main');
  const [changes, setChanges] = useState(0);
  const [source, setSource] = useState('');
  const [files, setFiles] = useState<WorkspaceFile[]>([
    { id: 'main', path: 'main.cpp', content: '#include <vector>\n#include "thing.h"\nint main() {\n  std::vector<int> values;\n  values.\n  return helper();\n}\n', language: 'cpp' },
    { id: 'header', path: 'thing.h', content: 'inline int helper() { return 42; }\n', language: 'cpp' },
  ]);
  return <>
    <button onClick={() => setScope('attempt')}>Студент</button>
    <button onClick={() => setScope('submission')}>Проверка</button>
    <button onClick={() => setScope('experiment')}>Песочница</button>
    <output data-testid="changes">{changes}</output>
    <output data-testid="source">{source}</output>
    {aiHoverFixture && <><output data-testid="ai-finding">{aiFinding.message}</output><output data-testid="ai-asked">{asked}</output><output data-testid="ai-finding-count">{findings.length}</output>
      {deferredEditorFixture && <><button onClick={() => setFindings([aiFinding])}>Получить сохранённый отчёт</button><button onClick={() => openEditor?.()}>Открыть редактор</button></>}
    </>}
    <div style={{ height: 600 }}><CodeWorkspace files={files} activeFileId={active} onActiveFile={setActive}
      scopeId={`smoke-${scope}`} intelligenceScope={aiHoverFixture ? undefined : { kind: scope, id: '11111111-1111-4111-8111-111111111111' }}
      aiFindings={aiHoverFixture ? findings : undefined} onAskAi={aiHoverFixture ? () => setAsked((value) => value + 1) : undefined}
      strictPaste={scope === 'attempt'} readOnly={scope === 'submission'} experiment={scope === 'experiment'}
      onChange={(id, content, source) => { setFiles((files) => files.map((file) => file.id === id ? { ...file, content } : file)); setChanges((value) => value + 1); setSource(source); }}
    /></div>
  </>;
}
createRoot(document.getElementById('root')!).render(<ThemeProvider><Fixture /></ThemeProvider>);
