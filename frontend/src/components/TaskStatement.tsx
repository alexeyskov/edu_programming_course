import { lazy, Suspense, useMemo, useState } from 'react';
import { apiBase } from '../lib/apiConfig';
import { cn } from '../lib/utils';
import './taskStatement.css';

type StatementScope = { kind: 'attempt' | 'submission' | 'assessment'; id: string };
type StatementPart = { text: string } | { image: string; alt: string } | { tex: string };
const TaskFormula = lazy(() => import('./TaskFormula'));
const texImagePath = /\/filter\/tex\/pix\.php(?:\/|$)/;

// Only image tokens and explicit TeX delimiters are interpreted. In particular,
// C++ angle brackets, Markdown, and untrusted HTML otherwise remain plain text.
function statementParts(content: string): StatementPart[] {
  const parts: StatementPart[] = [];
  const token = /!\[((?:\\[\\\[\]]|[^\]\\\r\n])*)\]\(<([^<>\s]+)>\)|\\\(([\s\S]*?)\\\)|\\\[([\s\S]*?)\\\]|\$\$([\s\S]*?)\$\$/g;
  let offset = 0;
  let images = 0;
  let formulas = 0;
  for (const match of content.matchAll(token)) {
    let part: StatementPart;
    if (match[1] !== undefined) {
      if (images >= 32 || match[1].length > 4096 || match[2].length > 4096
        || /[\x00-\x1f\x7f]/.test(match[1]) || /[\x00-\x1f\x7f#]/.test(match[2])) continue;
      let url: URL;
      try { url = new URL(match[2]); } catch { continue; }
      if (url.protocol !== 'https:' || url.username || url.password) continue;
      part = { image: match[2], alt: match[1].replace(/\\([\\\[\]])/g, '$1') };
      images += 1;
    } else {
      const tex = match[3] ?? match[4] ?? match[5];
      const precedingSlashes = content.slice(0, match.index).match(/\\+$/)?.[0].length ?? 0;
      if (formulas >= 64 || precedingSlashes % 2 || !tex.trim() || tex.length > 4096
        || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(tex)) continue;
      part = { tex };
      formulas += 1;
    }
    if (match.index > offset) parts.push({ text: content.slice(offset, match.index) });
    parts.push(part);
    offset = match.index + match[0].length;
  }
  if (offset < content.length) parts.push({ text: content.slice(offset) });
  return parts;
}

function StatementFormula({ tex }: { tex: string }) {
  // Task equations occupy a separate line even if Moodle marked them inline.
  // This is presentation only: the saved statement and its version stay intact.
  return <Suspense fallback={<span className="task-statement__math-loading" role="status" aria-label="Отображение формулы" />}><TaskFormula tex={tex} display /></Suspense>;
}

function imageFormulaSource(image: string, alt: string): string | null {
  const source = alt.trim();
  if (!source || source.length > 4096) return null;
  const delimited = /^(?:\\\[([\s\S]*?)\\\]|\\\(([\s\S]*?)\\\)|\$\$([\s\S]*?)\$\$)$/.exec(source);
  if (delimited) return delimited[1] ?? delimited[2] ?? delimited[3];
  // Moodle's TeX filter puts the original TeX in the image's alt/title. If
  // its image is unavailable, typeset that source locally, not as code. An
  // ordinary picture's caption must never be guessed to be a formula.
  if (texImagePath.test(new URL(image).pathname)) return source;
  return null;
}

function StatementImage({ image, alt, scope }: { image: string; alt: string; scope: StatementScope }) {
  const [failed, setFailed] = useState(false);
  // The backend verifies both access to the task and the exact source URL in
  // its statement. Moodle cookies and arbitrary external URLs stay off-page.
  const source = `${apiBase}/statement-images/${scope.kind}/${encodeURIComponent(scope.id)}?url=${encodeURIComponent(image)}`;
  const label = alt || 'Изображение из условия задания';
  const formula = imageFormulaSource(image, alt);
  const formulaClass = (formula !== null || texImagePath.test(new URL(image).pathname)) && 'task-statement__image--formula';
  if (failed) {
    if (formula) return <StatementFormula tex={formula} />;
    return <span className={cn('task-statement__image-fallback', formulaClass)} role="img" aria-label={label} title="Не удалось загрузить изображение из Moodle">{alt || 'Изображение из Moodle недоступно'}</span>;
  }
  return <img className={cn('task-statement__image', formulaClass)} src={source} alt={label} loading="lazy" decoding="async" referrerPolicy="no-referrer" onError={() => setFailed(true)} />;
}

export function TaskStatement({ content, scope, className }: { content: string; scope: StatementScope; className?: string }) {
  const parts = useMemo(() => statementParts(content), [content]);
  return <div className={cn('task-statement', className)}>{parts.map((part, index) => 'text' in part
    ? <span key={index}>{part.text}</span>
    : 'image' in part
      ? <StatementImage key={`${scope.kind}:${scope.id}:${index}:${part.image}`} image={part.image} alt={part.alt} scope={scope} />
      : <StatementFormula key={`${index}:${part.tex}`} tex={part.tex} />)}</div>;
}
