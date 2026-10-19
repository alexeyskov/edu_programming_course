import { Bot } from 'lucide-react';
import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react';
import type { Token } from 'monaco-editor';
import { languageForPath } from '../lib/utils';
import type { AiCodeFinding, WorkspaceFile } from '../types';
import { Badge, Modal } from './ui';
import './mobileCodePreview.css';

export interface MobileCodePreviewHandle {
  revealLine(line: number): void;
}

const LINE_HEIGHT = 26;
const OVERSCAN = 12;
const EMPTY_FINDINGS: AiCodeFinding[] = [];

function tokenClass(type: string): string {
  for (const kind of ['comment', 'keyword', 'string', 'number', 'type']) {
    if (type === kind || type.startsWith(`${kind}.`)) return `mobile-code-token--${kind}`;
  }
  return '';
}

/** A source viewer, not an input: there is no textarea/contenteditable for a
 * mobile browser to focus. Only AI-annotated lines have an interactive target.
 * Windowing keeps large student files from creating thousands of DOM rows.
 */
export const MobileCodePreview = forwardRef<MobileCodePreviewHandle, {
  file: WorkspaceFile;
  aiFindings?: AiCodeFinding[];
}>(function MobileCodePreview({ file, aiFindings = EMPTY_FINDINGS }, ref) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [viewport, setViewport] = useState({ top: 0, height: 600 });
  const [selectedLine, setSelectedLine] = useState<number | null>(null);
  const [highlighted, setHighlighted] = useState<{ content: string; language: string; tokens: Token[][] } | null>(null);
  const language = file.language ?? languageForPath(file.path);
  const lines = useMemo(() => file.content.split(/\r\n|\r|\n/), [file.content]);
  const columns = useMemo(() => lines.reduce((maximum, line) =>
    Math.max(maximum, line.length + (line.match(/\t/g)?.length ?? 0) * 3), 0), [lines]);
  const findings = useMemo(() => aiFindings.filter((item) => item.path === file.path
    && Number.isInteger(item.line) && item.line >= 1 && item.line <= lines.length
    && (item.endLine === undefined || (Number.isInteger(item.endLine)
      && item.endLine >= item.line && item.endLine <= lines.length))
    && ['error', 'warning', 'info'].includes(item.severity)
    && typeof item.message === 'string' && item.message.trim().length > 0),
  [aiFindings, file.path, lines.length]);

  useEffect(() => {
    let cancelled = false;
    // Very large files stay windowed plain text rather than allocating an
    // unbounded token tree on a phone. All lines and AI remarks remain usable.
    if (file.content.length > 256_000 || lines.length > 10_000) return;
    // Reuse the bundled C/C++ tokenizer, not an editor or language-server
    // session. Render escaped plain text immediately if loading fails.
    void import('../lib/monacoSetup').then(async ({ monaco }) => {
      await monaco.editor.colorize('', language, {});
      if (!cancelled) setHighlighted({ content: file.content, language, tokens: monaco.editor.tokenize(file.content, language) });
    }).catch(() => { /* Syntax colour is optional; the source remains visible. */ });
    return () => { cancelled = true; };
  }, [file.content, language, lines.length]);

  useEffect(() => { setSelectedLine(null); }, [file.id, file.content, findings]);
  useEffect(() => {
    const element = scrollRef.current;
    if (!element || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => {
      setViewport({ top: element.scrollTop, height: element.clientHeight || 600 });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useImperativeHandle(ref, () => ({
    revealLine(line) {
      const element = scrollRef.current;
      if (!element) return;
      const first = Math.min(lines.length, Math.max(1, Math.trunc(line) || 1));
      const height = element.clientHeight || 600;
      element.scrollTop = Math.max(0, (first - 1) * LINE_HEIGHT - height / 3);
      setViewport({ top: element.scrollTop, height });
    },
  }), [lines.length]);

  const start = Math.max(0, Math.floor(viewport.top / LINE_HEIGHT) - OVERSCAN);
  const end = Math.min(lines.length, Math.ceil((viewport.top + viewport.height) / LINE_HEIGHT) + OVERSCAN);
  const selected = selectedLine === null ? [] : findings.filter((item) =>
    item.line <= selectedLine && (item.endLine ?? item.line) >= selectedLine);

  return <>
    <div ref={scrollRef} className="mobile-code-preview" role="region"
      aria-label={`Код ${file.path}, только просмотр`} tabIndex={0}
      onScroll={(event) => setViewport({ top: event.currentTarget.scrollTop, height: event.currentTarget.clientHeight || 600 })}>
      <div className="mobile-code-preview__lines" style={{ height: lines.length * LINE_HEIGHT, minWidth: `calc(${columns}ch + 76px)` }}>
        {lines.slice(start, end).map((text, index) => {
          const number = start + index + 1;
          const annotated = findings.some((item) => item.line <= number && (item.endLine ?? item.line) >= number);
          const tokens = highlighted?.content === file.content && highlighted.language === language
            ? highlighted.tokens[number - 1] : undefined;
          const source = tokens?.length ? tokens.map((token, index) =>
            <span key={token.offset} className={tokenClass(token.type)}>{text.slice(token.offset, tokens[index + 1]?.offset)}</span>) : text || ' ';
          const content = <><span className="mobile-code-preview__number" aria-hidden="true">{number}{annotated && <Bot size={13} />}</span><code>{source}</code></>;
          const style = { top: (number - 1) * LINE_HEIGHT, height: LINE_HEIGHT };
          return annotated
            ? <button key={number} type="button" className="mobile-code-preview__line mobile-code-preview__line--ai" data-line={number}
              style={style} aria-label={`Замечания ИИ, строка ${number}`} onClick={() => setSelectedLine(number)}>{content}</button>
            : <div key={number} className="mobile-code-preview__line" data-line={number} style={style}>{content}</div>;
        })}
      </div>
    </div>
    <Modal open={selected.length > 0} title={`Ответ ИИ: строка ${selectedLine}`} onClose={() => setSelectedLine(null)}>
      <div className="mobile-code-findings">
        {selected.map((finding, index) => <article key={`${finding.line}:${index}`}>
          <header><Badge tone={finding.severity === 'error' ? 'danger' : finding.severity === 'warning' ? 'warning' : 'neutral'}>{finding.severity === 'error' ? 'Ошибка' : finding.severity === 'warning' ? 'Замечание' : 'Совет'}</Badge><span>{file.path} · {finding.line}{finding.endLine && finding.endLine !== finding.line ? `–${finding.endLine}` : ''}</span></header>
          <p>{finding.message}</p>
        </article>)}
      </div>
    </Modal>
  </>;
});
