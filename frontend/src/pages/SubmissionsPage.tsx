import {
  AlertTriangle, ArrowRight, CheckCircle2, Inbox, LockKeyhole, RotateCcw,
  Search, ShieldQuestion, UserCheck,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { Badge, Button, Card, EmptyState, InlineError, PageLoader, useToast } from '../components/ui';
import { api } from '../lib/api';
import { formatDate } from '../lib/utils';
import type { MoodleHistoryImportEvent, Submission } from '../types';

type SubmissionView = 'all' | 'pending' | 'reviewed';

const views: Array<{ id: SubmissionView; label: string }> = [
  { id: 'all', label: 'Все сданные' },
  { id: 'pending', label: 'Ожидают проверки' },
  { id: 'reviewed', label: 'Проверенные' },
];

function historyFailureMessage(event: MoodleHistoryImportEvent): string {
  if (/^INVALID_RESPONSE: (ASSIGN|QUIZ)_TABLE_NOT_FOUND:/.test(event.lastError ?? '')) {
    return 'Не распознана таблица сдач Moodle. Требуется проверка совместимости коннектора; это не означает отсутствие работ.';
  }
  const code = (event.lastError ?? '').split(':')[0];
  if (['LMS_REAUTH_REQUIRED', 'CREDENTIAL_EXPIRED', 'MISSING_CREDENTIAL'].includes(code)) {
    return 'Истекла сессия преподавателя в Moodle. Войдите повторно и повторите загрузку.';
  }
  if (code === 'TIMEOUT') return 'Moodle не ответил вовремя. Повторите загрузку.';
  if (code === 'UNAVAILABLE') return 'Соединение с Moodle недоступно. Повторите загрузку позже.';
  if (code === 'BROWSER_BUSY') return 'Браузерная сессия занята. Повторите загрузку через несколько секунд.';
  if (['INVALID_RESPONSE', 'RESPONSE_TOO_LARGE'].includes(code)) {
    return 'Не удалось прочитать ответ Moodle для этой работы. Повторите загрузку; если ошибка повторится, сообщите администратору.';
  }
  return 'Импорт этой работы завершился с ошибкой. Повторите загрузку; если ошибка повторится, сообщите администратору.';
}

export function SubmissionsPage() {
  const [items, setItems] = useState<Submission[]>([]);
  const [historyImports, setHistoryImports] = useState<MoodleHistoryImportEvent[]>([]);
  const [historyStatusError, setHistoryStatusError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [claiming, setClaiming] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const toast = useToast();
  const autoRefreshAttempts = useRef(0);
  const requestedView = searchParams.get('view');
  const view: SubmissionView = requestedView === 'pending' || requestedView === 'reviewed' ? requestedView : 'all';
  const query = searchParams.get('q') ?? '';

  const load = useCallback(async (silent = false) => {
    if (silent) setRefreshing(true); else setLoading(true);
    try {
      const [submissions, imports] = await Promise.allSettled([
        api.getSubmissions(),
        api.getMoodleHistoryImportEvents(),
      ]);
      if (submissions.status === 'rejected') throw submissions.reason;
      setItems(submissions.value);
      if (imports.status === 'fulfilled') setHistoryImports(imports.value);
      setHistoryStatusError(imports.status === 'rejected');
      setError(null);
    }
    catch (caught) { setError(caught instanceof Error ? caught.message : 'Не удалось получить работы'); }
    finally {
      if (silent) setRefreshing(false); else setLoading(false);
    }
  }, []);
  const latestHistoryImports = useMemo(() => {
    const result = new Map<string, MoodleHistoryImportEvent>();
    [...historyImports]
      .sort((left, right) => (
        Date.parse(right.createdAt || right.updatedAt) - Date.parse(left.createdAt || left.updatedAt)
      ))
      .forEach((item) => {
      const key = item.aggregateId ? `${item.aggregateId}:${item.actorKey ?? 'legacy'}` : item.id;
      if (!result.has(key)) result.set(key, item);
    });
    return [...result.values()];
  }, [historyImports]);
  const historyImportActive = latestHistoryImports.some((item) => ['PENDING', 'PROCESSING', 'RETRY'].includes(item.state));
  const failedHistoryImports = latestHistoryImports.filter((item) => ['FAILED', 'BLOCKED'].includes(item.state));
  const historyImportFailed = failedHistoryImports.length > 0;
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (loading || refreshing || error || (!historyImportActive && items.length)
      || (!historyImportActive && autoRefreshAttempts.current >= 20)) return undefined;
    const timer = window.setTimeout(() => {
      autoRefreshAttempts.current += 1;
      void load(true);
    }, historyImportActive ? 10_000 : 15_000);
    return () => window.clearTimeout(timer);
  }, [error, historyImportActive, items.length, load, loading, refreshing]);

  const counts = useMemo(() => ({
    all: items.length,
    pending: items.filter((item) => item.reviewRequired !== false && (item.status === 'UNGRADED' || item.status === 'CLAIMED')).length,
    reviewed: items.filter((item) => item.status === 'GRADED').length,
  }), [items]);
  const viewItems = useMemo(() => items.filter((item) => (
    view === 'all'
      || (view === 'pending' && item.reviewRequired !== false && (item.status === 'UNGRADED' || item.status === 'CLAIMED'))
      || (view === 'reviewed' && item.status === 'GRADED')
  )), [items, view]);
  const filtered = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    if (!normalizedQuery) return viewItems;
    return viewItems.filter((item) => `${item.studentName} ${item.studentGroup} ${item.courseTitle ?? ''} ${item.assessmentTitle}`.toLowerCase().includes(normalizedQuery));
  }, [viewItems, query]);

  function setParam(name: string, value: string) {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(name, value); else next.delete(name);
    setSearchParams(next, { replace: true });
  }

  function reviewUrl(item: Submission) {
    return `/review/${item.id}${location.search}`;
  }

  async function claimAndOpen(item: Submission, recheck = false) {
    setClaiming(item.id);
    try {
      await api.claimSubmission(item.id);
      navigate(reviewUrl(item));
    } catch (caught) {
      toast.push('error', recheck ? 'Не удалось начать перепроверку' : 'Работа уже занята', caught instanceof Error ? caught.message : 'Обновите список.');
      void load();
    } finally { setClaiming(null); }
  }

  async function retryFailedHistoryImports() {
    if (refreshing || !failedHistoryImports.length) return;
    setRefreshing(true);
    const results = await Promise.allSettled(
      failedHistoryImports.map((item) => api.retryMoodleHistoryImport(item.id)),
    );
    const retried = results
      .filter((result): result is PromiseFulfilledResult<MoodleHistoryImportEvent> => result.status === 'fulfilled')
      .map((result) => result.value);
    if (retried.length) {
      const retriedIds = new Set(retried.map((item) => item.id));
      setHistoryImports((current) => [...retried, ...current.filter((item) => !retriedIds.has(item.id))]);
      autoRefreshAttempts.current = 0;
      toast.push('info', 'Повторная загрузка запущена', 'Статус и список работ обновятся автоматически.');
    }
    if (retried.length !== results.length) {
      toast.push('error', 'Не все задачи удалось перезапустить', 'Проверьте LMS-сессию и повторите попытку.');
    }
    setRefreshing(false);
  }

  if (loading) return <PageLoader label="Собираем сданные работы…" />;
  if (error) return <InlineError message={error} retry={() => void load()} />;
  const nextSubmission = items.find((item) => item.status === 'UNGRADED' && item.canReview !== false);
  const historyUnavailable = historyStatusError || historyImportFailed;
  const emptyState = !items.length && historyUnavailable
    ? { title: 'Список сдач пока не получен', text: 'Не удалось подтвердить загрузку сдач из Moodle. Пустой список не означает, что студенты ничего не сдали.' }
    : !items.length && historyImportActive
      ? { title: 'Сдачи ещё загружаются', text: 'Дождитесь завершения импорта из Moodle. Список обновится автоматически.' }
      : { title: emptyTitle(view), text: emptyText(view, items.length) };
  return <div className="content-width submissions-page">
    <div className="page-heading"><div><span className="eyebrow">Проверка</span><h1>Работы студентов</h1><p>Все сданные работы, ожидающие решения преподавателя, и уже утверждённые результаты.</p></div><Button disabled={!nextSubmission} onClick={() => { if (nextSubmission) void claimAndOpen(nextSubmission); }}><UserCheck size={17} /> Открыть следующую</Button></div>

    <div className="submission-view-tabs" role="tablist" aria-label="Состояние проверки">
      {views.map((item) => <button key={item.id} type="button" role="tab" aria-selected={view === item.id} className={view === item.id ? 'is-active' : ''} onClick={() => setParam('view', item.id)}><span>{item.label}</span><strong>{counts[item.id]}</strong></button>)}
    </div>

    {historyStatusError && <div className="history-import-status history-import-status--error" role="alert"><AlertTriangle size={17} /><span><strong>Не удалось проверить синхронизацию Moodle</strong><small>Статус импорта недоступен. Уже загруженные работы можно просматривать.</small></span><Button size="sm" variant="secondary" loading={refreshing} onClick={() => void load(true)}>Проверить статус</Button></div>}
    {!historyStatusError && historyImportActive && <div className="history-import-status history-import-status--active" role="status"><RotateCcw className="spin" size={17} /><span><strong>Загружаем прошлые сдачи из Moodle</strong><small>Код, файлы, оценки и комментарии появляются постранично. Список обновится автоматически.</small></span></div>}
    {!historyStatusError && historyImportFailed && <div className="history-import-status history-import-status--error" role="alert"><AlertTriangle size={17} /><span><strong>Часть прошлых сдач не загрузилась</strong>{failedHistoryImports.slice(0, 5).map((item) => <small key={item.id}>{item.assessmentTitle && <b>{item.assessmentTitle}: </b>}{historyFailureMessage(item)}</small>)}{failedHistoryImports.length > 5 && <small>Ещё работ с ошибками: {failedHistoryImports.length - 5}.</small>}{historyImportActive && <small>Другие работы продолжают загружаться. Уже загруженные сдачи доступны для проверки.</small>}</span><Button size="sm" variant="secondary" loading={refreshing} onClick={() => void retryFailedHistoryImports()}>Повторить загрузку</Button></div>}

    <div className="filter-bar"><div className="search-input"><Search size={17} /><input value={query} onChange={(event) => setParam('q', event.target.value)} placeholder="Студент, группа, курс или работа" aria-label="Поиск по работам" /></div></div>
    {filtered.length ? <Card className="submission-table"><div className="submission-table__head"><span>Студент</span><span>Работа</span><span>Проверки</span><span>Статус</span><span>Действия</span></div>{filtered.map((item) => <div className="submission-row" key={item.id}>
      <span className="student-cell"><span className="student-avatar">{item.studentName.split(' ').map((part) => part[0]).join('').slice(0, 2)}</span><span><strong>{item.studentName}</strong><small>{item.studentGroup !== '—' ? `Группа ${item.studentGroup} · ` : ''}{formatDate(item.submittedAt)}</small></span></span>
      <span><strong>{item.assessmentTitle}</strong><small>{item.reviewGroup ? `${taskCountLabel(item.reviewGroup.items.length)} · ` : ''}{item.courseTitle ? `${item.courseTitle} · ` : ''}{item.testsTotal > 0 ? `${item.testsPassed}/${item.testsTotal} тестов по данным API` : 'Результаты тестов не предоставлены'}</small></span>
      <span className="evidence-cell"><Badge tone={item.risk === 'HIGH' ? 'danger' : item.risk === 'MEDIUM' ? 'warning' : item.risk === 'LOW' ? 'success' : 'neutral'}>{item.risk === 'HIGH' ? <AlertTriangle size={12} /> : <ShieldQuestion size={12} />} {riskLabel(item.risk)}</Badge></span>
      <span>{item.reviewRequired === false ? <Badge>Проверка не требуется</Badge> : item.status === 'CLAIMED' && item.claim ? <span className="claim-owner"><LockKeyhole size={14} /><span><strong>{item.claim.mine ? 'Вы проверяете' : item.claim.ownerName}</strong><small>до {formatDate(item.claim.expiresAt, { hour: '2-digit', minute: '2-digit' })}</small></span></span> : item.status === 'GRADED' ? <Badge tone="success"><CheckCircle2 size={12} /> {item.score}/{item.maxScore}</Badge> : item.status === 'CONFLICT' ? <Badge tone="danger">Конфликт LMS</Badge> : <Badge>Не проверено</Badge>}</span>
      <span className="submission-actions">{item.status === 'UNGRADED' && item.canReview !== false ? <Button size="sm" loading={claiming === item.id} onClick={() => void claimAndOpen(item)}><UserCheck size={14} /> Открыть</Button> : item.status === 'CLAIMED' ? <Button size="sm" variant={item.claim?.mine ? 'primary' : 'secondary'} onClick={() => navigate(reviewUrl(item))}><ArrowRight size={14} /> {item.claim?.mine ? 'Продолжить' : 'Открыть'}</Button> : item.status === 'GRADED' ? <><Button size="sm" variant="secondary" onClick={() => navigate(reviewUrl(item))}><ArrowRight size={14} /> Открыть результат</Button>{item.canReview !== false && <Button size="sm" variant="ghost" loading={claiming === item.id} onClick={() => void claimAndOpen(item, true)}><RotateCcw size={14} /> Перепроверить</Button>}</> : <Button size="sm" variant="secondary" onClick={() => navigate(reviewUrl(item))}><ArrowRight size={14} /> Открыть</Button>}</span>
    </div>)}</Card> : <Card className="submission-empty"><EmptyState icon={<Inbox />} title={viewItems.length ? 'По запросу ничего не найдено' : emptyState.title} text={viewItems.length ? 'Измените поисковый запрос.' : emptyState.text} action={!viewItems.length && !items.length ? <Button variant="secondary" loading={refreshing} onClick={() => { autoRefreshAttempts.current = 0; void load(true); }}><RotateCcw size={15} /> Обновить список</Button> : undefined} /></Card>}
    <div className="table-footer"><span>Показано {filtered.length} из {viewItems.length}</span></div>
  </div>;
}

function riskLabel(value: Submission['risk']) { return value === 'HIGH' ? 'Высокий риск' : value === 'MEDIUM' ? 'Требует внимания' : value === 'LOW' ? 'Низкий риск' : 'Нет данных'; }
function taskCountLabel(count: number) {
  const mod100 = count % 100;
  const mod10 = count % 10;
  const suffix = mod100 >= 11 && mod100 <= 14 ? 'заданий' : mod10 === 1 ? 'задание' : mod10 >= 2 && mod10 <= 4 ? 'задания' : 'заданий';
  return `${count} ${suffix}`;
}
function emptyTitle(view: SubmissionView) { return view === 'pending' ? 'Нет работ, ожидающих проверки' : view === 'reviewed' ? 'Проверенных работ пока нет' : 'Сданных работ пока нет'; }
function emptyText(view: SubmissionView, total: number) {
  if (view !== 'all' && total) return 'Переключитесь на другую вкладку, чтобы увидеть остальные работы.';
  return 'Новые сдачи появляются здесь сразу. Исторические сдачи и оценки Moodle импортируются в фоне после синхронизации LMS; для них будет явно указано, что история редактирования недоступна.';
}
