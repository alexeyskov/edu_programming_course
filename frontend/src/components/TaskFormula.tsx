import { useMemo } from 'react';
import katex from 'katex';
import 'katex/dist/katex.min.css';

/** Only KaTeX's generated, untrusted-input-safe markup enters this element. */
export default function TaskFormula({ tex, display }: { tex: string; display: boolean }) {
  const html = useMemo(() => {
    if (!tex.trim() || tex.length > 4096) return null;
    try {
      return katex.renderToString(tex, {
        displayMode: display,
        output: 'htmlAndMathml',
        throwOnError: true,
        trust: false,
        strict: 'ignore',
        maxExpand: 256,
        maxSize: 10,
        macros: {}, // Never share macros across tasks or users.
      });
    } catch {
      return null;
    }
  }, [tex, display]);
  if (html === null) return <code className="task-statement__math-source" title="Не удалось отобразить формулу">{tex}</code>;
  return <span className={`task-statement__math${display ? ' task-statement__math--display' : ''}`} data-tex={tex} dangerouslySetInnerHTML={{ __html: html }} />;
}
