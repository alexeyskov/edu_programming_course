import {
  AlertTriangle, ArrowRight, CheckCircle2, Inbox, LockKeyhole, RotateCcw,
  Search, ShieldQuestion, UserCheck,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom';
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

export function SubmissionsPage() {
  const [items, setItems] = useState<Submission[]>([]);
  const [historyImports, setHistoryImports] = useState<MoodleHistoryImportEvent[]>([]);
  const [historyStatusError, setHistoryStatusError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [historyRefreshing, setHistoryRefreshing] = useState(false);
  const hasLoadedSubmissions = useRef(false);
  const submissionsInFlight = useRef(false);
  const historyInFlight = useRef(false);
  const [claiming, setClaiming] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const toast = useToast();
  const requestedView = searchParams.get('view');
  const view: SubmissionView = requestedView === 'pending' || requestedView === 'reviewed' ? requestedView : 'all';
  const query = searchParams.get('q') ?? '';
  const courseId = searchParams.get('course') ?? '';
  const assessmentId = courseId ? searchParams.get('assessment') ?? '' : '';

  const loadSubmissions = useCallback(async () => {
    if (submissionsInFlight.current) return;
    submissionsInFlight.current = true;
    if (hasLoadedSubmissions.current) setRefreshing(true); else setLoading(true);
    try {
      setItems(await api.getSubmissions());
      hasLoadedSubmissions.current = true;
      setError(null);
    }
    catch (caught) { setError(caught instanceof Error ? caught.message : 'Не удалось получить работы'); }
    finally {
      submissionsInFlight.current = false;
      setRefreshing(false);
      setLoading(false);
    }
  }, []);
  const loadHistoryStatus = useCallback(async () => {
    if (historyInFlight.current) return;
    historyInFlight.current = true;
    setHistoryRefreshing(true);
    try {
      setHistoryImports(await api.getMoodleHistoryImportEvents());
      setHistoryStatusError(false);
    } catch { setHistoryStatusError(true); }
    finally {
      historyInFlight.current = false;
      setHistoryRefreshing(false);
    }
  }, []);
  const load = useCallback(() => {
    // Reviewable answers must not wait for the status of every course. These
    // requests also have independent in-flight guards so a slow status read
    // cannot stop the list from refreshing as more answers are committed.
    void loadSubmissions();
    void loadHistoryStatus();
  }, [loadSubmissions, loadHistoryStatus]);
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
  // An explicit empty list without acknowledgement means no problems in this
  // teacher's review scope. It must not produce a banner or an empty-list error.
  const problemApplies = (item: MoodleHistoryImportEvent) => item.warnings === undefined || item.warnings.length > 0 || item.warningsDismissed === true;
  const failedHistoryImports = latestHistoryImports.filter((item) => ['FAILED', 'BLOCKED'].includes(item.state) && problemApplies(item));
  const partialHistoryImports = latestHistoryImports.filter((item) => item.state === 'PARTIAL' && problemApplies(item));
  const historyImportFailed = failedHistoryImports.length > 0;
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    // These reads update the local list after student deliveries or explicitly
    // started teacher jobs; they never initiate synchronization with Moodle.
    const refreshVisible = () => { if (!document.hidden) load(); };
    const timer = window.setInterval(refreshVisible, historyImportActive ? 10_000 : 30_000);
    document.addEventListener('visibilitychange', refreshVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', refreshVisible);
    };
  }, [historyImportActive, load]);

  // Options come from the whole authorized queue, not the current tab/search.
  const courseOptions = useMemo(() => filterOptions(items.flatMap((item) => (
    item.courseId ? [[item.courseId, item.courseTitle ?? 'Курс без названия']] : []
  ))), [items]);
  const assessmentOptions = useMemo(() => filterOptions(items
    .filter((item) => courseId && item.courseId === courseId)
    .map((item) => [workId(item), item.parentAssessmentTitle ?? item.assessmentTitle])), [items, courseId]);
  const scopedItems = useMemo(() => items.filter((item) => (
    (!courseId || item.courseId === courseId) && (!assessmentId || workId(item) === assessmentId)
  )), [items, courseId, assessmentId]);
  const counts = useMemo(() => ({
    all: scopedItems.length,
    pending: scopedItems.filter((item) => item.reviewRequired !== false && (item.status === 'UNGRADED' || item.status === 'CLAIMED')).length,
    reviewed: scopedItems.filter((item) => item.status === 'GRADED').length,
  }), [scopedItems]);
  const viewItems = useMemo(() => scopedItems.filter((item) => (
    view === 'all'
      || (view === 'pending' && item.reviewRequired !== false && (item.status === 'UNGRADED' || item.status === 'CLAIMED'))
      || (view === 'reviewed' && item.status === 'GRADED')
  )), [scopedItems, view]);
  const filtered = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    if (!normalizedQuery) return viewItems;
    return viewItems.filter((item) => `${item.studentName} ${item.studentGroup} ${item.courseTitle ?? ''} ${item.assessmentTitle}`.toLowerCase().includes(normalizedQuery));
  }, [viewItems, query]);

  function setParam(name: string, value: string) {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(name, value); else next.delete(name);
    if (name === 'course') next.delete('assessment');
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

  if (loading) return <PageLoader label="Собираем сданные работы…" />;
  if (error && !hasLoadedSubmissions.current) return <InlineError message={error} retry={load} />;
  const nextSubmission = filtered.find((item) => item.status === 'UNGRADED' && item.canReview !== false);
  const historyUnavailable = historyStatusError || historyImportFailed || partialHistoryImports.length > 0;
  const emptyState = !items.length && historyUnavailable
    ? { title: 'Список сдач пока не получен', text: 'Не удалось подтвердить загрузку сдач из Moodle. Пустой список не означает, что студенты ничего не сдали.' }
    : !items.length && historyImportActive
      ? { title: 'Сдачи ещё загружаются', text: 'Работы появятся по мере загрузки. Можно начинать проверку, не дожидаясь завершения всего импорта.' }
      : courseId && !scopedItems.length
        ? { title: 'По выбранным фильтрам работ нет', text: 'Выберите другой курс или работу либо сбросьте фильтры.' }
        : { title: emptyTitle(view), text: emptyText(view, scopedItems.length) };
  return <div className="content-width submissions-page">
    <div className="page-heading"><div><span className="eyebrow">Проверка</span><h1>Работы студентов</h1><p>Все сданные работы, ожидающие решения преподавателя, и уже утверждённые результаты.</p></div><Button disabled={!nextSubmission} onClick={() => { if (nextSubmission) void claimAndOpen(nextSubmission); }}><UserCheck size={17} /> Открыть следующую</Button></div>

    <div className="submission-view-tabs" role="tablist" aria-label="Состояние проверки">
      {views.map((item) => <button key={item.id} type="button" role="tab" aria-selected={view === item.id} className={view === item.id ? 'is-active' : ''} onClick={() => setParam('view', item.id)}><span>{item.label}</span><strong>{counts[item.id]}</strong></button>)}
    </div>

    {error && <div className="history-import-status history-import-status--error" role="alert"><AlertTriangle size={17} /><span><strong>Не удалось обновить список работ</strong><small>{error} Показан последний загруженный список.</small></span><Button size="sm" variant="secondary" loading={refreshing} onClick={() => void loadSubmissions()}>Обновить список</Button></div>}
    {historyStatusError && <div className="history-import-status history-import-status--error" role="alert"><AlertTriangle size={17} /><span><strong>Не удалось проверить синхронизацию Moodle</strong><small>Статус импорта недоступен. Уже загруженные работы можно проверять.</small></span><Button size="sm" variant="secondary" loading={historyRefreshing} onClick={() => void loadHistoryStatus()}>Проверить статус</Button></div>}
    {!historyStatusError && historyImportActive && <div className="history-import-status history-import-status--active" role="status"><RotateCcw className="spin" size={17} /><span><strong>Загружаем прошлые сдачи из Moodle</strong><small>Работы появляются по мере загрузки. Уже загруженные сдачи доступны для проверки — ждать завершения всего импорта не нужно.</small></span></div>}

    <div className="filter-bar submission-filters">
      <div className="search-input"><Search size={17} /><input value={query} onChange={(event) => setParam('q', event.target.value)} placeholder="Студент, группа, курс или работа" aria-label="Поиск по работам" /></div>
      <label className="submission-filter"><span>Курс</span><select aria-label="Курс" value={courseId} onChange={(event) => setParam('course', event.target.value)}>
        <option value="">Все курсы</option>
        {courseId && !courseOptions.some((option) => option.id === courseId) && <option value={courseId}>Выбранный курс (нет сдач)</option>}
        {courseOptions.map((option) => <option key={option.id} value={option.id}>{option.title}</option>)}
      </select></label>
      <label className="submission-filter"><span>Работа</span><select aria-label="Работа" value={assessmentId} disabled={!courseId} onChange={(event) => setParam('assessment', event.target.value)}>
        <option value="">{courseId ? 'Все работы курса' : 'Сначала выберите курс'}</option>
        {assessmentId && !assessmentOptions.some((option) => option.id === assessmentId) && <option value={assessmentId}>Выбранная работа (нет сдач)</option>}
        {assessmentOptions.map((option) => <option key={option.id} value={option.id}>{option.title}</option>)}
      </select></label>
    </div>
    {filtered.length ? <Card className="submission-table"><div className="submission-table__head"><span>Студент</span><span>Работа</span><span>Проверки</span><span>Статус</span><span>Действия</span></div>{filtered.map((item) => <div className="submission-row" key={item.id}>
      <span className="student-cell"><span className="student-avatar">{item.studentName.split(' ').map((part) => part[0]).join('').slice(0, 2)}</span><span><strong>{item.studentName}</strong><small>{item.studentGroup !== '—' ? `Группа ${item.studentGroup} · ` : ''}{formatDate(item.submittedAt)}</small></span></span>
      <span><strong>{item.assessmentTitle}</strong><small>{item.reviewGroup ? `${taskCountLabel(item.reviewGroup.items.length)} · ` : ''}{item.courseTitle ? `${item.courseTitle} · ` : ''}{item.testsTotal > 0 ? `${item.testsPassed}/${item.testsTotal} тестов по данным API` : 'Результаты тестов не предоставлены'}</small></span>
      <span className="evidence-cell"><Badge tone={item.risk === 'HIGH' ? 'danger' : item.risk === 'MEDIUM' ? 'warning' : item.risk === 'LOW' ? 'success' : 'neutral'}>{item.risk === 'HIGH' ? <AlertTriangle size={12} /> : <ShieldQuestion size={12} />} {riskLabel(item.risk)}</Badge></span>
      <span>{item.reviewRequired === false ? <Badge>Проверка не требуется</Badge> : item.status === 'CLAIMED' && item.claim ? <span className="claim-owner"><LockKeyhole size={14} /><span><strong>{item.claim.mine ? 'Вы проверяете' : item.claim.ownerName}</strong><small>до {formatDate(item.claim.expiresAt, { hour: '2-digit', minute: '2-digit' })}</small></span></span> : item.status === 'GRADED' ? <Badge tone="success"><CheckCircle2 size={12} /> {item.score}/{item.maxScore}</Badge> : item.status === 'CONFLICT' ? <Badge tone="danger">Конфликт LMS</Badge> : <Badge>Не проверено</Badge>}</span>
      <span className="submission-actions">{item.status === 'UNGRADED' && item.canReview !== false ? <Button size="sm" loading={claiming === item.id} onClick={() => void claimAndOpen(item)}><UserCheck size={14} /> Открыть</Button> : item.status === 'CLAIMED' ? <Button size="sm" variant={item.claim?.mine ? 'primary' : 'secondary'} onClick={() => navigate(reviewUrl(item))}><ArrowRight size={14} /> {item.claim?.mine ? 'Продолжить' : 'Открыть'}</Button> : item.status === 'GRADED' ? <><Button size="sm" variant="secondary" onClick={() => navigate(reviewUrl(item))}><ArrowRight size={14} /> Открыть результат</Button>{item.canReview !== false && <Button size="sm" variant="ghost" loading={claiming === item.id} onClick={() => void claimAndOpen(item, true)}><RotateCcw size={14} /> Перепроверить</Button>}</> : <Button size="sm" variant="secondary" onClick={() => navigate(reviewUrl(item))}><ArrowRight size={14} /> Открыть</Button>}</span>
    </div>)}</Card> : <Card className="submission-empty"><EmptyState icon={<Inbox />} title={viewItems.length ? 'По запросу ничего не найдено' : emptyState.title} text={viewItems.length ? 'Измените поисковый запрос.' : emptyState.text} action={!viewItems.length && !items.length ? <><Button variant="secondary" loading={refreshing} onClick={load}><RotateCcw size={15} /> Обновить список</Button>{historyUnavailable && <Link className="button button--secondary button--md" to="/courses">К синхронизации работ</Link>}</> : undefined} /></Card>}
    <div className="table-footer"><span>Показано {filtered.length} из {viewItems.length}</span></div>
  </div>;
}

function riskLabel(value: Submission['risk']) { return value === 'HIGH' ? 'Высокий риск' : value === 'MEDIUM' ? 'Требует внимания' : value === 'LOW' ? 'Низкий риск' : 'Нет данных'; }
function workId(item: Submission) { return item.parentAssessmentId ?? item.assessmentId; }
function filterOptions(entries: Array<[string, string]>) {
  const unique = [...new Map(entries)].map(([id, title]) => ({ id, title }));
  const counts = new Map<string, number>();
  unique.forEach(({ title }) => counts.set(title, (counts.get(title) ?? 0) + 1));
  // Different Moodle courses/works can have identical titles; don't merge them.
  return unique.map(({ id, title }) => ({ id, title: counts.get(title)! > 1 ? `${title} · ${id.slice(0, 8)}` : title }))
    .sort((left, right) => left.title.localeCompare(right.title, 'ru') || left.id.localeCompare(right.id));
}
function taskCountLabel(count: number) {
  const mod100 = count % 100;
  const mod10 = count % 10;
  const suffix = mod100 >= 11 && mod100 <= 14 ? 'заданий' : mod10 === 1 ? 'задание' : mod10 >= 2 && mod10 <= 4 ? 'задания' : 'заданий';
  return `${count} ${suffix}`;
}
function emptyTitle(view: SubmissionView) { return view === 'pending' ? 'Нет работ, ожидающих проверки' : view === 'reviewed' ? 'Проверенных работ пока нет' : 'Сданных работ пока нет'; }
function emptyText(view: SubmissionView, total: number) {
  if (view !== 'all' && total) return 'Переключитесь на другую вкладку, чтобы увидеть остальные работы.';
  return 'Сдачи из системы появляются здесь сразу. Чтобы загрузить ответы и оценки из Moodle, вручную синхронизируйте нужную работу на странице «Курсы и работы». Обновление этого списка не запускает синхронизацию Moodle.';
}
