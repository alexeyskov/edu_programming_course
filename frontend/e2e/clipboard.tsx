// Development-only fixture; not an entry point of the production Vite build.
import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { loader } from '@monaco-editor/react';
import { CodeWorkspace } from '../src/components/CodeWorkspace';
import { ThemeProvider } from '../src/context/ThemeContext';
import { createEditorClipboardSession } from '../src/lib/editorClipboard';
import { normalizeClipboardText } from '../src/lib/utils';
import type { WorkspaceFile } from '../src/types';
import '../src/styles.css';

if (!new URLSearchParams(location.search).has('cdn')) {
  loader.config({ paths: { vs: '/node_modules/monaco-editor/min/vs' } });
}

function Fixture() {
  const [questions, setQuestions] = useState<Record<string, WorkspaceFile[]>>({
    'question-1': [
      { id: 'main', path: 'main.cpp', content: 'int value = 42;\n', language: 'cpp' },
      { id: 'other', path: 'other.cpp', content: '// other\n', language: 'cpp' },
    ],
    'question-2': [{ id: 'second', path: 'main.cpp', content: '// task two\n', language: 'cpp' }],
    'another-work': [{ id: 'foreign', path: 'main.cpp', content: '// unrelated\n', language: 'cpp' }],
  });
  const [question, setQuestion] = useState('question-1');
  const [activeFiles, setActiveFiles] = useState<Record<string, string>>({ 'question-1': 'main', 'question-2': 'second', 'another-work': 'foreign' });
  const [clipboardSession] = useState(createEditorClipboardSession);
  const files = questions[question];
  const [blocked, setBlocked] = useState(0);
  const [receipts, setReceipts] = useState(0);
  const [edits, setEdits] = useState<unknown[]>([]);
  return <>
    <button onClick={() => setQuestion('question-1')}>Задача 1</button>
    <button onClick={() => setQuestion('question-2')}>Задача 2</button>
    <button onClick={() => setQuestion('another-work')}>Другая работа</button>
    <textarea aria-label="External text" defaultValue="AI_EXTERNAL_SOLUTION" />
    <output data-testid="blocked">{blocked}</output>
    <output data-testid="receipts">{receipts}</output>
    <div style={{ height: 500 }}><CodeWorkspace key={question}
      files={files} activeFileId={activeFiles[question]} onActiveFile={(fileId) => setActiveFiles((items) => ({ ...items, [question]: fileId }))} scopeId={question}
      clipboardSession={clipboardSession} clipboardScopeId={question === 'another-work' ? question : 'clipboard-fixture'}
      strictPaste onPasteBlocked={() => setBlocked((value) => value + 1)}
      onInternalCopy={async (fileId, text, sourceAttemptId = question) => {
        const content = questions[sourceAttemptId]?.find((file) => file.id === fileId)?.content;
        if (content === undefined || !normalizeClipboardText(content).includes(normalizeClipboardText(text))) return null;
        setReceipts((value) => value + 1);
        return crypto.randomUUID();
      }}
      onChange={(fileId, content, source, receiptId, range) => {
        setQuestions((items) => ({ ...items, [question]: items[question].map((file) => file.id === fileId ? { ...file, content } : file) }));
        setEdits((items) => [...items, { fileId, source, receiptId, range }]);
      }}
    /></div>
    <pre data-testid="files">{JSON.stringify(files)}</pre>
    <pre data-testid="edits">{JSON.stringify(edits)}</pre>
  </>;
}
createRoot(document.getElementById('root')!).render(<ThemeProvider><Fixture /></ThemeProvider>);
