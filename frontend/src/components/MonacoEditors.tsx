import { lazy, Suspense } from 'react';
import type { DiffEditorProps, EditorProps } from '@monaco-editor/react';

// Load the local editor/worker only on editor screens, not on login/course lists.
const Editor = lazy(async () => {
  await import('../lib/monacoSetup');
  const module = await import('@monaco-editor/react');
  return { default: module.default };
});
const DiffEditor = lazy(async () => {
  await import('../lib/monacoSetup');
  const module = await import('@monaco-editor/react');
  return { default: module.DiffEditor };
});
const fallback = <div className="page-loader" role="status">Загрузка редактора…</div>;
export function MonacoEditor(props: EditorProps) { return <Suspense fallback={fallback}><Editor {...props} /></Suspense>; }
export function MonacoDiffEditor(props: DiffEditorProps) { return <Suspense fallback={fallback}><DiffEditor {...props} /></Suspense>; }
