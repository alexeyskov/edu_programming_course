// Development-only fixture: excluded from the production build.
import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { CodeWorkspace } from '../src/components/CodeWorkspace';
import { ThemeProvider } from '../src/context/ThemeContext';
import { monaco } from '../src/lib/monacoSetup';
import type { WorkspaceFile } from '../src/types';
import '../src/styles.css';

declare global { interface Window { smokeMonaco: typeof monaco } }
window.smokeMonaco = monaco;
function Fixture() {
  const [scope, setScope] = useState<'attempt' | 'submission' | 'experiment'>('attempt');
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
    <div style={{ height: 600 }}><CodeWorkspace files={files} activeFileId={active} onActiveFile={setActive}
      scopeId={`smoke-${scope}`} intelligenceScope={{ kind: scope, id: '11111111-1111-4111-8111-111111111111' }}
      strictPaste={scope === 'attempt'} readOnly={scope === 'submission'} experiment={scope === 'experiment'}
      onChange={(id, content, source) => { setFiles((files) => files.map((file) => file.id === id ? { ...file, content } : file)); setChanges((value) => value + 1); setSource(source); }}
    /></div>
  </>;
}
createRoot(document.getElementById('root')!).render(<ThemeProvider><Fixture /></ThemeProvider>);
