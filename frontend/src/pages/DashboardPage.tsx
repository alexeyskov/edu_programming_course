import { ArrowRight, BookOpen, CheckCircle2, Clock3, Cloud, FileCheck2, RefreshCw, ShieldAlert, Users } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Badge, Button, Card, EmptyState, InlineError, PageLoader } from '../components/ui';
import { useAuth } from '../context/AuthContext';
import { api } from '../lib/api';
import { formatDate, formatGreetingName, kindLabel, statusLabel } from '../lib/utils';
import type { Assessment, Course } from '../types';

export function DashboardPage() {
  const { session, primaryRole } = useAuth();
  const [courses, setCourses] = useState<Course[]>([]);
  const [assessments, setAssessments] = useState<Assessment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const loadedCourses = await api.getCourses(session);
      setCourses(loadedCourses);
      const all = await Promise.all(loadedCourses.map((course) => api.getAssessments(course.id)));
      setAssessments(all.flat());
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Ошибка загрузки'); }
    finally { setLoading(false); }
  }, [session]);
  useEffect(() => { void load(); }, [load]);
  if (loading) return <PageLoader />;
  if (error) return <InlineError message={error} retry={() => void load()} />;
  return primaryRole === 'TEACHER' ? <TeacherDashboard courses={courses} assessments={assessments} onReload={load} /> : <StudentDashboard courses={courses} assessments={assessments} displayName={session?.displayName ?? 'Студент'} />;
}

function StudentDashboard({ courses, assessments, displayName }: { courses: Course[]; assessments: Assessment[]; displayName: string }) {
  const active = assessments.find((item) => item.status === 'IN_PROGRESS');
  const upcoming = assessments.filter((item) => item.status !== 'IN_PROGRESS').slice(0, 3);
  return <div className="content-width dashboard">
    <div className="page-heading"><div><span className="eyebrow">{new Intl.DateTimeFormat('ru-RU', { weekday: 'long', day: 'numeric', month: 'long' }).format(new Date())}</span><h1>Добрый день, {formatGreetingName(displayName)}</h1><p>{active ? 'Есть начатая работа. Подтверждённая ревизия и состояние сохранения видны внутри IDE.' : 'Начатых работ сейчас нет.'}</p></div><Badge tone="neutral"><Cloud size={14} /> Данные курсов загружены</Badge></div>
    {active && <Card className="continue-card"><div className="continue-card__meta"><Badge tone="info">{kindLabel[active.kind]}</Badge><span><Clock3 size={15} /> {active.deadlineAt ? `до ${formatDate(active.deadlineAt)}` : 'без ограничения времени'}</span></div><h2>{active.title}</h2><p>{active.summary}</p>{active.progress !== undefined && <div className="progress-line"><span style={{ width: `${active.progress}%` }} /></div>}<div className="continue-card__footer"><span><strong>Активная попытка</strong><small>Откройте IDE, чтобы продолжить с серверной ревизии.</small></span><Link to={assessmentOpenPath(active)} className="button button--primary button--lg">Продолжить работу <ArrowRight size={17} /></Link></div></Card>}
    <div className="section-heading"><div><h2>Ближайшие работы</h2><p>Сроки определяются сервером и синхронизируются с LMS.</p></div><Link to="/courses">Все работы <ArrowRight size={16} /></Link></div>
    {upcoming.length
      ? <div className="assessment-grid">{upcoming.map((item) => <AssessmentCard key={item.id} item={item} />)}</div>
      : <Card className="dashboard-empty-card"><EmptyState icon={<Clock3 />} title="Ближайших работ пока нет" text="Новые лабораторные, самостоятельные и контрольные появятся здесь после публикации преподавателем." /></Card>}
    <div className="section-heading"><div><h2>Мои курсы</h2><p>Состав и группы приходят из внешней системы.</p></div></div>
    {courses.length
      ? <div className="course-row">{courses.map((course) => <Link className="course-mini" to={`/courses/${course.id}`} key={course.id}><span className="course-mini__icon"><BookOpen /></span><span><strong>{course.title}</strong><small>{course.shortName}{course.group ? ` · группа ${course.group}` : ''}</small></span><ArrowRight /></Link>)}</div>
      : <Card className="dashboard-empty-card"><EmptyState icon={<BookOpen />} title="Доступных курсов пока нет" text="После добавления курса администратором здесь появятся курсы, доступные вашей учётной записи LMS." /></Card>}
  </div>;
}

function TeacherDashboard({ courses, assessments, onReload }: { courses: Course[]; assessments: Assessment[]; onReload(): Promise<void> }) {
  const knownUnchecked = courses.filter((item) => item.uncheckedCount !== undefined);
  const unchecked = knownUnchecked.reduce((sum, item) => sum + (item.uncheckedCount ?? 0), 0);
  const workCount = assessments.filter((item) => item.publicationStatus !== 'CLOSED').length;
  const draftCount = assessments.filter((item) => item.publicationStatus === 'DRAFT').length;
  const knownSubmissionCounts = assessments.filter((item) => item.submissionsCount !== undefined);
  const submissions = knownSubmissionCounts.reduce((sum, item) => sum + (item.submissionsCount ?? 0), 0);
  const syncIssues = courses.filter((item) => item.syncStatus === 'STALE' || item.syncStatus === 'ERROR').length;
  const [syncing, setSyncing] = useState(false);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const syncingCourseIds = courses
    .filter((course) => course.syncStatus === 'SYNCING')
    .map((course) => course.id)
    .sort();
  const syncingCourseKey = syncingCourseIds.join(',');
  const remoteSyncing = syncingCourseIds.length > 0;
  const persistedSyncError = courses.find((course) => course.syncStatus === 'ERROR')?.syncError;

  useEffect(() => {
    if (!syncingCourseKey) {
      setStatusError(null);
      return;
    }
    const courseIds = syncingCourseKey.split(',');
    let cancelled = false;
    let timer: number | undefined;
    let consecutiveFailures = 0;
    let settled = false;

    const poll = async () => {
      try {
        const statuses = await Promise.all(courseIds.map((courseId) => api.getCourseSyncStatus(courseId)));
        if (cancelled) return;
        consecutiveFailures = 0;
        setStatusError(null);
        if (statuses.some((status) => status.syncStatus === 'SYNCING')) {
          timer = window.setTimeout(() => { void poll(); }, 1_500);
          return;
        }
        settled = true;
        const failed = statuses.find((status) => status.syncStatus === 'ERROR');
        if (failed) {
          setSyncError(failed.syncError?.message || 'Синхронизация Moodle завершилась с ошибкой.');
        }
        await onReload();
      } catch (caught) {
        if (cancelled || settled) return;
        consecutiveFailures += 1;
        if (consecutiveFailures >= 3) {
          setStatusError(caught instanceof Error
            ? caught.message
            : 'Не удалось проверить состояние синхронизации. Повторяем автоматически.');
        }
        timer = window.setTimeout(() => { void poll(); }, 1_500);
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [onReload, syncingCourseKey]);

  async function synchronize() {
    setSyncing(true); setSyncError(null);
    try { for (const course of courses) await api.syncCourse(course.id); await onReload(); }
    catch (caught) {
      setSyncError(caught instanceof Error ? caught.message : 'Синхронизация не выполнена');
      await onReload().catch(() => undefined);
    }
    finally { setSyncing(false); }
  }
  return <div className="content-width dashboard">
    <div className="page-heading"><div><span className="eyebrow">Режим преподавателя</span><h1>Рабочий обзор</h1><p>{remoteSyncing ? 'Синхронизация продолжается на сервере. Страница обновится автоматически.' : 'Показатели рассчитаны по синхронизированным данным добавленных курсов.'}</p></div><Button variant="secondary" loading={syncing || remoteSyncing} disabled={!courses.length || syncing || remoteSyncing} onClick={() => void synchronize()}>{!syncing && !remoteSyncing && <RefreshCw size={16} />} {remoteSyncing ? 'Синхронизируется Moodle' : syncing ? 'Запускаем синхронизацию' : 'Синхронизировать Moodle'}</Button></div>
    {statusError && <InlineError title="Не удалось проверить состояние синхронизации" message={statusError} />}
    {(syncError || persistedSyncError) && <InlineError message={syncError ?? persistedSyncError?.message ?? 'Синхронизация Moodle завершилась с ошибкой.'} retry={remoteSyncing ? undefined : () => void synchronize()} />}
    <div className="metric-grid"><Card className="metric"><span className="metric__icon metric__icon--amber"><FileCheck2 /></span><div><small>Ждут проверки</small><strong>{knownUnchecked.length ? unchecked : '—'}</strong><em>{knownUnchecked.length ? 'по данным добавленных курсов' : 'данные пока не получены'}</em></div></Card><Card className="metric"><span className="metric__icon metric__icon--green"><Users /></span><div><small>Работы в системе</small><strong>{workCount}</strong><em>{draftCount ? `${draftCount} ещё не включены для групп` : 'все работы распределены'}</em></div></Card><Card className="metric"><span className="metric__icon metric__icon--blue"><CheckCircle2 /></span><div><small>Сдачи</small><strong>{knownSubmissionCounts.length ? submissions : '—'}</strong><em>{knownSubmissionCounts.length ? 'по данным работ' : 'данные пока не получены'}</em></div></Card><Card className="metric"><span className="metric__icon metric__icon--purple"><ShieldAlert /></span><div><small>{remoteSyncing && !syncIssues ? 'Курсы синхронизируются' : 'Курсы требуют внимания'}</small><strong>{remoteSyncing && !syncIssues ? syncingCourseIds.length : syncIssues}</strong><em>{remoteSyncing && !syncIssues ? 'обновляются на сервере' : syncIssues ? 'нужно повторить синхронизацию' : 'все курсы актуальны'}</em></div></Card></div>
    <div className="dashboard-columns"><section><div className="section-heading"><div><h2>Курсы</h2><p>Данные доступных курсов</p></div><Link to="/courses">Все курсы</Link></div><Card className="list-card">{courses.length ? courses.map((course) => <Link to={`/courses/${course.id}`} className="course-list-item" key={course.id}><span className="course-avatar">{course.shortName.slice(0, 2)}</span><span><strong>{course.title}</strong><small>{course.term || 'Период не указан'}{course.activeCount !== undefined ? ` · ${course.activeCount} работ` : ''}</small></span><Badge tone={course.syncStatus === 'SYNCED' ? 'success' : course.syncStatus === 'SYNCING' ? 'info' : course.syncStatus === 'ERROR' ? 'danger' : 'warning'}>{course.syncStatus === 'SYNCED' ? 'Синхронизирован' : course.syncStatus === 'SYNCING' ? 'Синхронизируется' : course.syncStatus === 'ERROR' ? 'Ошибка синхронизации' : 'Нужна синхронизация'}</Badge><ArrowRight /></Link>) : <EmptyState icon={<BookOpen />} title="Курсов пока нет" text="Добавьте курс в настройках системы и выполните синхронизацию LMS." />}</Card></section>
      <section><div className="section-heading"><div><h2>Ближайшие события</h2><p>Контрольные и экзамены</p></div></div><Card className="timeline-card">{assessments.some((item) => ['CONTROL', 'EXAM'].includes(item.kind)) ? assessments.filter((item) => ['CONTROL', 'EXAM'].includes(item.kind)).slice(0, 3).map((item) => <div className="timeline-event" key={item.id}><div><strong>{item.startsAt ? new Date(item.startsAt).getDate() : '—'}</strong><small>{item.startsAt ? new Intl.DateTimeFormat('ru', { month: 'short' }).format(new Date(item.startsAt)) : ''}</small></div><span><strong>{item.title}</strong><small>{item.durationMinutes} мин · {item.standard}</small></span><Badge tone="neutral">{statusLabel[item.status]}</Badge></div>) : <EmptyState icon={<Clock3 />} title="Событий пока нет" text="Опубликованные контрольные и экзамены появятся здесь." />}</Card></section></div>
    <Card className="readiness"><div><span className="status-dot" /><span><strong>Состояние серверных компонентов проверяется отдельно</strong><small>Подробная диагностика приложения, базы, компилятора и Moodle доступна обладателю системного доступа в настройках.</small></span></div></Card>
  </div>;
}

function AssessmentCard({ item }: { item: Assessment }) {
  return <Card className="assessment-card"><div><Badge tone={item.kind === 'EXAM' ? 'danger' : item.kind === 'CONTROL' ? 'warning' : 'neutral'}>{kindLabel[item.kind]}</Badge><Badge tone={item.status === 'GRADED' ? 'success' : 'neutral'}>{statusLabel[item.status]}</Badge></div><h3>{item.title}</h3><p>{item.summary}</p><dl><div><dt>Срок</dt><dd>{formatDate(item.deadlineAt)}</dd></div><div><dt>Среда</dt><dd>{item.standard} · {item.fileMode === 'MULTI' ? 'несколько файлов' : 'один файл'}</dd></div></dl><Link to={assessmentOpenPath(item)}>Открыть <ArrowRight size={15} /></Link></Card>;
}

function assessmentOpenPath(item: Assessment): string {
  if (item.requiresLiveLmsPreparation) return `/assessments/${item.id}`;
  return item.attemptId ? `/ide/${item.attemptId}` : `/assessments/${item.id}`;
}
