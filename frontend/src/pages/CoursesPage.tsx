import { ArrowRight, BookOpen, CalendarDays, ChevronDown, ChevronUp, ExternalLink, RefreshCw, Search } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Badge, Button, Card, EmptyState, InlineError, PageLoader, useToast } from '../components/ui';
import { LmsSyncErrorDialog } from '../components/LmsSyncErrorDialog';
import { useAuth } from '../context/AuthContext';
import { api } from '../lib/api';
import { formatDate, kindLabel, statusLabel } from '../lib/utils';
import type { Assessment, AssessmentSyncStatus, Course } from '../types';

const workTitleCollator = new Intl.Collator('ru', { numeric: true, sensitivity: 'base' });

export function CoursesPage() {
  const { session, primaryRole } = useAuth();
  const [courses, setCourses] = useState<Course[]>([]);
  const [assessments, setAssessments] = useState<Assessment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [typeFilter, setTypeFilter] = useState('ALL');
  const [statusFilter, setStatusFilter] = useState('ALL');
  const [titleSortDirection, setTitleSortDirection] = useState<'asc' | 'desc'>('asc');
  const [expandedCourseIds, setExpandedCourseIds] = useState<Set<string>>(() => new Set());
  const [syncErrorCourse, setSyncErrorCourse] = useState<Course | null>(null);
  const [syncRetrying, setSyncRetrying] = useState(false);
  const [syncStatuses, setSyncStatuses] = useState<AssessmentSyncStatus[]>([]);
  const [pendingStarts, setPendingStarts] = useState<Set<string>>(new Set());
  const [statusError, setStatusError] = useState<string | null>(null);
  const startsRef = useRef(new Set<string>());
  const loadInFlight = useRef(false);
  const mutationVersion = useRef(0);
  const courseStatusVersions = useRef(new Map<string, number>());
  const toast = useToast();
  const teacher = primaryRole === 'TEACHER';

  const load = useCallback(async (silent = false) => {
    if (loadInFlight.current) return;
    loadInFlight.current = true;
    const version = mutationVersion.current;
    const statusVersions = new Map(courseStatusVersions.current);
    if (!silent) setLoading(true);
    try {
      const data = await api.getCourses(session);
      const [loadedAssessments, statuses] = await Promise.all([
        Promise.all(data.map((course) => api.getAssessments(course.id))).then((groups) => groups.flat()),
        teacher ? Promise.all(data.filter((course) => course.role === 'TEACHER').map((course) => api.getAssessmentSyncStatuses(course.id))).then((groups) => groups.flat()) : [],
      ]);
      // A read started before a teacher pressed Sync must not unlock that job.
      if (version !== mutationVersion.current) return;
      // Work-list reads can finish after the lightweight status poll. Preserve
      // newer outcomes instead of putting a completed course back into SYNCING.
      setCourses((current) => data.map((course) => {
        const newer = courseStatusVersions.current.get(course.id) !== statusVersions.get(course.id)
          ? current.find((item) => item.id === course.id) : undefined;
        return newer ? { ...course, syncStatus: newer.syncStatus, syncError: newer.syncError } : course;
      }));
      setAssessments(loadedAssessments);
      setSyncStatuses(statuses);
      setStatusError(null);
      setError(null);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : 'Ошибка загрузки';
      if (silent) setStatusError(message); else setError(message);
    } finally {
      loadInFlight.current = false;
      if (!silent) setLoading(false);
    }
  }, [session, teacher]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!teacher) return undefined;
    // Read shared job states only; polling never initiates a Moodle sync.
    const refreshVisible = () => { if (!document.hidden) void load(true); };
    const timer = window.setInterval(refreshVisible, 5_000);
    document.addEventListener('visibilitychange', refreshVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', refreshVisible);
    };
  }, [load, teacher]);

  const syncingCourseKey = teacher ? courses
    .filter((course) => course.role === 'TEACHER' && course.syncStatus === 'SYNCING')
    .map((course) => course.id).sort().join(',') : '';
  useEffect(() => {
    if (!syncingCourseKey) return;
    let cancelled = false;
    // Poll each active course independently: neither a slow work list nor
    // another course's request should delay its completion indicator.
    const watchers = syncingCourseKey.split(',').map((courseId) => {
      let timer: number | undefined;
      let inFlight = false;
      let settled = false;
      const poll = async () => {
        if (cancelled || inFlight || settled || document.hidden) return;
        inFlight = true;
        const version = mutationVersion.current;
        try {
          const status = await api.getCourseSyncStatus(courseId);
          if (cancelled || version !== mutationVersion.current) return;
          courseStatusVersions.current.set(courseId, (courseStatusVersions.current.get(courseId) ?? 0) + 1);
          setCourses((current) => current.map((course) => course.id === courseId
            ? { ...course, syncStatus: status.syncStatus, syncError: status.syncError } : course));
          settled = status.syncStatus !== 'SYNCING';
          if (settled) void load(true);
        } catch {
          // Keep the last known state and retry. The catalog refresh reports
          // persistent connection errors without hiding already loaded works.
        } finally {
          inFlight = false;
          if (!cancelled && !settled) timer = window.setTimeout(() => { void poll(); }, 1_500);
        }
      };
      const refresh = () => {
        if (timer !== undefined) window.clearTimeout(timer);
        void poll();
      };
      void poll();
      return { refresh, stop: () => { if (timer !== undefined) window.clearTimeout(timer); } };
    });
    const refreshVisible = () => { if (!document.hidden) watchers.forEach((watcher) => watcher.refresh()); };
    document.addEventListener('visibilitychange', refreshVisible);
    window.addEventListener('focus', refreshVisible);
    return () => {
      cancelled = true;
      watchers.forEach((watcher) => watcher.stop());
      document.removeEventListener('visibilitychange', refreshVisible);
      window.removeEventListener('focus', refreshVisible);
    };
  }, [load, syncingCourseKey]);

  function courseBusy(course: Course) {
    const current = courses.find((item) => item.id === course.id) ?? course;
    return current.syncStatus === 'SYNCING' || startsRef.current.has(`course:${course.id}`);
  }

  function answersBusy(courseId: string) {
    return assessments.some((item) => item.courseId === courseId && (
      syncStatuses.some((status) => status.assessmentId === item.id && status.status === 'SYNCING')
      || startsRef.current.has(`assessment:${item.id}`)
    ));
  }

  function toggleCourse(courseId: string) {
    setExpandedCourseIds((current) => {
      const next = new Set(current);
      if (next.has(courseId)) next.delete(courseId);
      else next.add(courseId);
      return next;
    });
  }

  async function startSync(course: Course, assessment?: Assessment) {
    const key = assessment ? `assessment:${assessment.id}` : `course:${course.id}`;
    if (startsRef.current.has(key) || courseBusy(course) || statusError) return;
    if (!assessment && answersBusy(course.id)) return;
    if (assessment && syncStatuses.some((status) => status.assessmentId === assessment.id && status.status === 'SYNCING')) return;
    startsRef.current.add(key);
    setPendingStarts(new Set(startsRef.current));
    mutationVersion.current += 1;
    try {
      if (assessment) {
        const updated = await api.syncAssessment(assessment.id);
        setSyncStatuses((current) => [...current.filter((status) => status.assessmentId !== updated.assessmentId), updated]);
      } else {
        const updated = await api.syncCourse(course.id);
        setCourses((current) => current.map((item) => item.id === course.id ? { ...item, ...updated } : item));
      }
      setSyncErrorCourse(null);
    } catch (caught) {
      toast.push('error', 'Синхронизация не запущена', caught instanceof Error ? caught.message : 'Обновите состояние и повторите попытку.');
    } finally {
      mutationVersion.current += 1;
      startsRef.current.delete(key);
      setPendingStarts(new Set(startsRef.current));
    }
  }

  async function retrySync() {
    if (!syncErrorCourse || syncRetrying) return;
    setSyncRetrying(true);
    try { await startSync(syncErrorCourse); } finally { setSyncRetrying(false); }
  }

  if (loading) return <PageLoader />;
  if (error) return <InlineError message={error} retry={() => void load()} />;

  const filtered = assessments.filter((item) => item.title.toLowerCase().includes(query.toLowerCase())
    && (typeFilter === 'ALL' || item.kind === typeFilter)
    && (statusFilter === 'ALL' || item.status === statusFilter || item.publicationStatus === statusFilter));

  return <div className="content-width courses-page">
    <div className="page-heading">
      <div className="page-heading__copy">
        <span className="eyebrow">Курсов: {courses.length}</span>
        <h1>{primaryRole === 'TEACHER' ? 'Курсы и работы' : 'Мои работы'}</h1>
        <p>{primaryRole === 'TEACHER'
          ? 'Синхронизируйте курс вручную, чтобы обновить список работ и участников. Ответы студентов загружаются отдельно кнопкой у каждой работы.'
          : 'Все доступные задания из ваших курсов.'}</p>
      </div>
    </div>

    {statusError && <InlineError title="Не удалось обновить состояние синхронизации" message={statusError} retry={() => void load(true)} />}

    <div className="filter-bar">
      <div className="search-input"><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Найти работу" /></div>
      <select aria-label="Тип работы" value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)}>
        <option value="ALL">Все типы</option><option value="LAB">Лабораторные</option><option value="INDEPENDENT">Самостоятельные</option><option value="CONTROL">Контрольные</option><option value="EXAM">Экзамены</option>
      </select>
      <select aria-label="Статус" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
        <option value="ALL">Все статусы</option>{primaryRole === 'TEACHER' && <option value="DRAFT">Не включена</option>}<option value="AVAILABLE">Доступно</option><option value="IN_PROGRESS">В работе</option><option value="GRADED">Проверено</option><option value="CLOSED">Закрыто</option>
      </select>
    </div>

    <div className="course-detail-list">
      {courses.length ? courses.map((course) => {
        const courseWorks = filtered.filter((item) => item.courseId === course.id)
          .sort((left, right) => workTitleCollator.compare(left.title, right.title) * (titleSortDirection === 'asc' ? 1 : -1));
        const courseHasWorks = assessments.some((item) => item.courseId === course.id);
        const syncingCourse = courseBusy(course);
        const syncingAnswers = answersBusy(course.id);
        const canSync = teacher && course.role === 'TEACHER';
        const collapsed = !expandedCourseIds.has(course.id);
        const worksId = `course-works-${course.id}`;
        const toggleLabel = `${collapsed ? 'Развернуть' : 'Свернуть'} список работ курса ${course.title}`;
        return <Card className={`course-detail${collapsed ? ' course-detail--collapsed' : ''}`} key={course.id}>
          <header>
            <span className="course-detail__icon"><BookOpen /></span>
            <div><h2>{course.title}</h2><p>{course.shortName}{course.group && ` · группа ${course.group}`}</p></div>
            {course.syncStatus === 'ERROR'
              ? <button type="button" className="sync-error-trigger" onClick={() => setSyncErrorCourse(course)} aria-label={`Показать ошибку синхронизации курса ${course.title}`}><Badge tone="danger"><RefreshCw size={12} /> Ошибка LMS</Badge></button>
              : <Badge tone={course.syncStatus === 'SYNCED' ? 'success' : syncingCourse ? 'info' : 'warning'}><RefreshCw size={12} className={syncingCourse ? 'spin' : ''} /> {syncingCourse ? 'Синхронизируется' : course.syncStatus === 'SYNCED' ? 'Список курса загружен' : 'Курс не синхронизирован'}</Badge>}
            {canSync && <Button size="sm" variant="secondary" disabled={syncingCourse || syncingAnswers || Boolean(statusError)}
              aria-label={`Синхронизировать курс ${course.title}`}
              title={syncingAnswers ? 'Дождитесь синхронизации ответов в этом курсе' : 'Обновить список работ, студентов и преподавателей; ответы не загружаются'}
              onClick={() => void startSync(course)}><RefreshCw size={15} className={syncingCourse ? 'spin' : ''} /> Синхронизировать курс</Button>}
            {course.externalUrl && <a href={course.externalUrl} target="_blank" rel="noreferrer" aria-label="Открыть в LMS"><ExternalLink size={17} /></a>}
            <Button type="button" variant="ghost" size="icon" className="course-detail__toggle"
              aria-label={toggleLabel} title={toggleLabel} aria-expanded={!collapsed} aria-controls={worksId}
              onClick={() => toggleCourse(course.id)}>
              {collapsed ? <ChevronDown size={20} aria-hidden="true" /> : <ChevronUp size={20} aria-hidden="true" />}
            </Button>
          </header>
          <div id={worksId} hidden={collapsed}>
          {courseWorks.length ? <div className={`work-table${canSync ? ' work-table--manual-sync' : ''}`}>
            <div className="work-table__head">
              <button
                type="button"
                className="work-table__sort"
                onClick={() => setTitleSortDirection((current) => current === 'asc' ? 'desc' : 'asc')}
                aria-label={`Работа: по ${titleSortDirection === 'asc' ? 'возрастанию' : 'убыванию'} имени. Сортировать по ${titleSortDirection === 'asc' ? 'убыванию' : 'возрастанию'}`}
                title={`Сортировать по ${titleSortDirection === 'asc' ? 'убыванию' : 'возрастанию'} имени работы`}
              >
                Работа {titleSortDirection === 'asc' ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}
              </button>
              <span>Период</span><span>Статус</span><span />
            </div>
            {courseWorks.map((item) => {
              const status = syncStatuses.find((current) => current.assessmentId === item.id);
              const syncing = status?.status === 'SYNCING' || pendingStarts.has(`assessment:${item.id}`);
              const syncTone = syncing ? '' : status?.status === 'COMPLETED' ? ' work-sync-button--success' : status?.status === 'FAILED' || status?.status === 'PARTIAL' ? ' work-sync-button--failed' : '';
              const diagnostic = status?.lastError ? `${status.lastError}${status.errorCode ? ` Код: ${status.errorCode}.` : ''}` : 'Повторите синхронизацию';
              const syncDescription = syncing ? 'Ответы синхронизируются' : status?.status === 'FAILED' ? `Ошибка синхронизации ответов: ${diagnostic}` : status?.status === 'PARTIAL' ? `Синхронизировано с предупреждениями: ${diagnostic} Загруженные работы можно проверять.` : status?.status === 'COMPLETED' ? `Ответы синхронизированы${status.updatedAt ? `: ${formatDate(status.updatedAt)}` : ''}` : 'Ответы ещё не синхронизировались';
              return <div key={item.id} className="work-row-shell"><Link to={item.requiresLiveLmsPreparation || !item.attemptId ? `/assessments/${item.id}` : `/ide/${item.attemptId}`} className="work-row">
              <span><Badge tone="neutral">{kindLabel[item.kind]}</Badge><strong>{item.title}</strong><small>{item.standard} · {item.fileMode === 'MULTI' ? 'многофайловая' : 'один файл'}</small></span>
              <span><CalendarDays size={15} />{item.deadlineAt ? formatDate(item.deadlineAt) : 'Без ограничения'}</span>
              <Badge tone={item.publicationStatus === 'DRAFT' ? 'warning' : item.status === 'GRADED' ? 'success' : item.status === 'IN_PROGRESS' ? 'info' : 'neutral'}>{item.publicationStatus === 'DRAFT' ? 'Не включена' : statusLabel[item.status]}</Badge><ArrowRight size={17} />
            </Link>{canSync && <button type="button" className={`work-sync-button${syncTone}`}
              disabled={syncingCourse || syncing || Boolean(statusError)}
              aria-label={`Синхронизировать ответы: ${item.title}`}
              title={syncingCourse ? 'Сначала дождитесь синхронизации курса' : syncDescription}
              onClick={() => void startSync(course, item)}><RefreshCw size={18} className={syncing ? 'spin' : ''} /><span className="sr-only">{syncDescription}</span></button>}</div>;
            })}
          </div> : <EmptyState icon={<BookOpen />} title={courseHasWorks ? 'По фильтру ничего не найдено' : 'В курсе пока нет работ'} text={courseHasWorks ? 'Измените поисковый запрос, тип или статус работы.' : teacher ? 'Нажмите «Синхронизировать курс», чтобы обновить список работ из Moodle.' : 'Преподаватель должен обновить список работ курса из Moodle.'} />}
          </div>
        </Card>;
      }) : <Card className="courses-empty"><EmptyState icon={<BookOpen />} title="Доступных курсов пока нет" text="Курсы появятся после добавления администратором и сопоставления с вашей учётной записью LMS." /></Card>}
    </div>
    <LmsSyncErrorDialog
      open={Boolean(syncErrorCourse)}
      courseTitle={syncErrorCourse?.title ?? ''}
      diagnostic={syncErrorCourse?.syncError}
      retrying={syncRetrying}
      retryDisabled={!teacher || Boolean(statusError) || Boolean(syncErrorCourse && (courseBusy(syncErrorCourse) || answersBusy(syncErrorCourse.id)))}
      onClose={() => setSyncErrorCourse(null)}
      onRetry={() => void retrySync()}
    />
  </div>;
}
