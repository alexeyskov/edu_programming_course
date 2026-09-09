import { AlertTriangle, ArrowLeft, Braces, GitCompareArrows } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import {
  CodeWorkspace,
  type CodeHighlight,
  type CodeWorkspaceHandle,
} from '../components/CodeWorkspace';
import { Badge, Button, InlineError, PageLoader } from '../components/ui';
import { api } from '../lib/api';
import { formatDate } from '../lib/utils';
import type {
  SimilarityComparison,
  SimilarityEvidenceFragment,
  SimilaritySubmissionSide,
} from '../types';

interface OrientedComparison {
  current: SimilaritySubmissionSide;
  peer: SimilaritySubmissionSide;
  fragments: Array<{
    current: RangeSide;
    peer: RangeSide;
    tokenCount: number;
  }>;
}

interface RangeSide {
  path: string;
  startLine: number;
  endLine: number;
  excerpt: string;
}

export function SimilarityComparisonPage() {
  const { submissionId = '', matchId = '' } = useParams();
  const [comparison, setComparison] = useState<SimilarityComparison | null>(null);
  const [activeFragment, setActiveFragment] = useState(0);
  const [currentFileId, setCurrentFileId] = useState('');
  const [peerFileId, setPeerFileId] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const currentEditor = useRef<CodeWorkspaceHandle>(null);
  const peerEditor = useRef<CodeWorkspaceHandle>(null);
  const navigate = useNavigate();
  const location = useLocation();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const loaded = await api.getSimilarityComparison(matchId);
      setComparison(loaded);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Не удалось открыть сравнение');
    } finally {
      setLoading(false);
    }
  }, [matchId]);

  useEffect(() => { void load(); }, [load]);

  const oriented = useMemo(
    () => comparison ? orientComparison(comparison, submissionId) : null,
    [comparison, submissionId],
  );

  useEffect(() => {
    if (!oriented) return;
    const firstFragment = oriented.fragments[0];
    const currentFile = oriented.current.files.find((file) => file.path === firstFragment?.current.path);
    const peerFile = oriented.peer.files.find((file) => file.path === firstFragment?.peer.path);
    setCurrentFileId(currentFile?.id ?? oriented.current.files[0]?.id ?? '');
    setPeerFileId(peerFile?.id ?? oriented.peer.files[0]?.id ?? '');
    setActiveFragment(0);
    if (!firstFragment) return;
    const timer = window.setTimeout(() => {
      currentEditor.current?.openRange(
        firstFragment.current.path,
        firstFragment.current.startLine,
        firstFragment.current.endLine,
      );
      peerEditor.current?.openRange(
        firstFragment.peer.path,
        firstFragment.peer.startLine,
        firstFragment.peer.endLine,
      );
    }, 40);
    return () => window.clearTimeout(timer);
  }, [oriented]);

  function openFragment(index: number) {
    if (!oriented) return;
    const fragment = oriented.fragments[index];
    if (!fragment) return;
    setActiveFragment(index);
    const currentFile = oriented.current.files.find((file) => file.path === fragment.current.path);
    const peerFile = oriented.peer.files.find((file) => file.path === fragment.peer.path);
    if (currentFile) setCurrentFileId(currentFile.id);
    if (peerFile) setPeerFileId(peerFile.id);
    window.setTimeout(() => {
      currentEditor.current?.openRange(
        fragment.current.path,
        fragment.current.startLine,
        fragment.current.endLine,
      );
      peerEditor.current?.openRange(
        fragment.peer.path,
        fragment.peer.startLine,
        fragment.peer.endLine,
      );
    }, 40);
  }

  if (loading) return <PageLoader label="Получаем неизменяемые снимки пары…" />;
  if (error || !comparison || !oriented) {
    return <InlineError message={error ?? 'Сравнение не найдено'} retry={() => void load()} />;
  }

  const currentHighlights = highlightsFor(oriented.fragments, 'current', activeFragment);
  const peerHighlights = highlightsFor(oriented.fragments, 'peer', activeFragment);
  const returnTo = typeof location.state === 'object' && location.state
    && 'from' in location.state && typeof location.state.from === 'string'
    ? location.state.from
    : `/review/${submissionId}`;

  return <div className="similarity-comparison-page">
    <header className="similarity-comparison-toolbar">
      <Button variant="ghost" size="sm" onClick={() => navigate(returnTo)}>
        <ArrowLeft size={16} /> К работе
      </Button>
      <div>
        <span className="eyebrow">Проверка совпадений</span>
        <strong>{comparison.assessmentTitle}</strong>
      </div>
      <Badge tone="warning">
        <GitCompareArrows size={13} /> Сходство отпечатков {formatPercent(comparison.match.score)}
      </Badge>
    </header>

    <div className="similarity-disclaimer">
      <AlertTriangle size={18} />
      <div><strong>Это предварительная проверка, а не конечное решение.</strong><span>Оцените контекст, условие задания, шаблонный код и историю написания обеих работ.</span></div>
    </div>

    <div className="similarity-comparison-layout">
      <aside className="similarity-fragments">
        <header><Braces size={17} /><div><strong>Совпадающие фрагменты</strong><small>{oriented.fragments.length} диапазонов строк</small></div></header>
        {oriented.fragments.length ? <div role="list" aria-label="Совпадающие фрагменты">
          {oriented.fragments.map((fragment, index) => <button
            type="button"
            role="listitem"
            key={`${fragment.current.path}:${fragment.current.startLine}:${index}`}
            className={activeFragment === index ? 'is-active' : undefined}
            onClick={() => openFragment(index)}
          >
            <span>{index + 1}</span>
            <div><strong>{fragment.current.path}:{lineRange(fragment.current)}</strong><small>{fragment.peer.path}:{lineRange(fragment.peer)} · {fragment.tokenCount} токенов</small></div>
          </button>)}
        </div> : <p>Сервер зафиксировал совпадение отпечатков, но не вернул диапазоны строк.</p>}
      </aside>

      <section className="similarity-code-pair">
        <ComparisonSideHeader side={oriented.current} label="Открытая работа" />
        <ComparisonSideHeader side={oriented.peer} label="Связанная работа" />
        <div className="similarity-code-pane">
          <CodeWorkspace
            ref={currentEditor}
            files={oriented.current.files}
            activeFileId={currentFileId}
            onActiveFile={setCurrentFileId}
            onChange={() => undefined}
            readOnly
            scopeId={`similarity-current-${comparison.match.id}`}
            highlights={currentHighlights}
          />
        </div>
        <div className="similarity-code-pane">
          <CodeWorkspace
            ref={peerEditor}
            files={oriented.peer.files}
            activeFileId={peerFileId}
            onActiveFile={setPeerFileId}
            onChange={() => undefined}
            readOnly
            scopeId={`similarity-peer-${comparison.match.id}`}
            highlights={peerHighlights}
          />
        </div>
      </section>
    </div>
  </div>;
}

function ComparisonSideHeader({ side, label }: { side: SimilaritySubmissionSide; label: string }) {
  return <header className="similarity-side-header"><span>{label}</span><strong>{side.studentName}</strong><small>{side.studentGroup || 'Группа не указана'} · сдано {formatDate(side.submittedAt)}</small></header>;
}

function orientComparison(comparison: SimilarityComparison, currentId: string): OrientedComparison {
  const currentIsLeft = comparison.left.submissionId === currentId;
  const current = currentIsLeft ? comparison.left : comparison.right;
  const peer = currentIsLeft ? comparison.right : comparison.left;
  return {
    current,
    peer,
    fragments: comparison.match.evidence.map((fragment) => currentIsLeft
      ? orientFragment(fragment)
      : orientFragment(fragment, true)),
  };
}

function orientFragment(fragment: SimilarityEvidenceFragment, reverse = false) {
  const a = { path: fragment.fileA, startLine: fragment.startLineA, endLine: fragment.endLineA, excerpt: fragment.excerptA };
  const b = { path: fragment.fileB, startLine: fragment.startLineB, endLine: fragment.endLineB, excerpt: fragment.excerptB };
  return { current: reverse ? b : a, peer: reverse ? a : b, tokenCount: fragment.tokenCount };
}

function highlightsFor(
  fragments: OrientedComparison['fragments'],
  side: 'current' | 'peer',
  active: number,
): CodeHighlight[] {
  return fragments.map((fragment, index) => ({
    path: fragment[side].path,
    startLine: fragment[side].startLine,
    endLine: fragment[side].endLine,
    index: index + 1,
    active: index === active,
  }));
}

function lineRange(side: RangeSide) {
  return side.startLine === side.endLine ? side.startLine : `${side.startLine}–${side.endLine}`;
}

function formatPercent(value: number) { return `${Math.round(value * 100)}%`; }
