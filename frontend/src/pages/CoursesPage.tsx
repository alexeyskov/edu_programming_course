import { ArrowRight, BookOpen, CalendarDays, ChevronDown, ChevronUp, ExternalLink, RefreshCw, Search } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Badge, Card, EmptyState, InlineError, PageLoader } from '../components/ui';
import { LmsSyncErrorDialog } from '../components/LmsSyncErrorDialog';
import { useAuth } from '../context/AuthContext';
import { api } from '../lib/api';
import { formatDate, kindLabel, statusLabel } from '../lib/utils';
import type { Assessment, Course } from '../types';

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
  const [syncErrorCourse, setSyncErrorCourse] = useState<Course | null>(null);
  const [syncRetrying, setSyncRetrying] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.getCourses(session);
      const loadedAssessments = await Promise.all(
        data.map((course) => api.getAssessments(course.id)),
      ).then((groups) => groups.flat());
      setCourses(data);
      setAssessments(loadedAssessments);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Ошибка загрузки');
    } finally {
      setLoading(false);
    }
  }, [session]);

  useEffect(() => { void load(); }, [load]);

  async function retrySync() {
    if (!syncErrorCourse || syncRetrying) return;
    setSyncRetrying(true);
    try {
      const updated = await api.syncCourse(syncErrorCourse.id);
      const updatedAssessments = await api.getAssessments(updated.id);
      setCourses((current) => current.map((course) => course.id === updated.id ? updated : course));
      setAssessments((current) => [
        ...current.filter((assessment) => assessment.courseId !== updated.id),
        ...updatedAssessments,
      ]);
      setSyncErrorCourse(null);
    } catch {
      const latest = await api.getCourses(session).catch(() => null);
      if (latest) {
        setCourses(latest);
        setSyncErrorCourse(latest.find((course) => course.id === syncErrorCourse.id) ?? syncErrorCourse);
      }
    } finally {
      setSyncRetrying(false);
    }
  }

  if (loading) return <PageLoader />;
  if (error) return <InlineError message={error} retry={() => void load()} />;

  const filtered = assessments.filter((item) => item.title.toLowerCase().includes(query.toLowerCase())
    && (typeFilter === 'ALL' || item.kind === typeFilter)
    && (statusFilter === 'ALL' || item.status === statusFilter || item.publicationStatus === statusFilter));

  return <div className="content-width courses-page">
    <div className="page-heading">
      <div>
        <span className="eyebrow">Курсов: {courses.length}</span>
        <h1>{primaryRole === 'TEACHER' ? 'Курсы и работы' : 'Мои работы'}</h1>
        <p>{primaryRole === 'TEACHER'
          ? 'Название, условие, сроки, шкала и число попыток синхронизируются из Moodle.'
          : 'Все доступные задания из ваших курсов.'}</p>
      </div>
    </div>

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
        return <Card className="course-detail" key={course.id}>
          <header>
            <span className="course-detail__icon"><BookOpen /></span>
            <div><h2>{course.title}</h2><p>{course.shortName}{course.group && ` · группа ${course.group}`}</p></div>
            {course.syncStatus === 'ERROR'
              ? <button type="button" className="sync-error-trigger" onClick={() => setSyncErrorCourse(course)} aria-label={`Показать ошибку синхронизации курса ${course.title}`}><Badge tone="danger"><RefreshCw size={12} /> Ошибка LMS</Badge></button>
              : <Badge tone={course.syncStatus === 'SYNCED' ? 'success' : course.syncStatus === 'SYNCING' ? 'info' : 'warning'}><RefreshCw size={12} /> {course.syncStatus === 'SYNCED' ? 'LMS актуальна' : course.syncStatus === 'SYNCING' ? 'Синхронизируется' : 'Данные ожидают синхронизации'}</Badge>}
            {course.externalUrl && <a href={course.externalUrl} target="_blank" rel="noreferrer" aria-label="Открыть в LMS"><ExternalLink size={17} /></a>}
          </header>
          {courseWorks.length ? <div className="work-table">
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
            {courseWorks.map((item) => <Link key={item.id} to={item.requiresLiveLmsPreparation || !item.attemptId ? `/assessments/${item.id}` : `/ide/${item.attemptId}`} className="work-row">
              <span><Badge tone="neutral">{kindLabel[item.kind]}</Badge><strong>{item.title}</strong><small>{item.standard} · {item.fileMode === 'MULTI' ? 'многофайловая' : 'один файл'}</small></span>
              <span><CalendarDays size={15} />{item.deadlineAt ? formatDate(item.deadlineAt) : 'Без ограничения'}</span>
              <Badge tone={item.publicationStatus === 'DRAFT' ? 'warning' : item.status === 'GRADED' ? 'success' : item.status === 'IN_PROGRESS' ? 'info' : 'neutral'}>{item.publicationStatus === 'DRAFT' ? 'Не включена' : statusLabel[item.status]}</Badge><ArrowRight size={17} />
            </Link>)}
          </div> : <EmptyState icon={<BookOpen />} title={courseHasWorks ? 'По фильтру ничего не найдено' : 'В курсе пока нет работ'} text={courseHasWorks ? 'Измените поисковый запрос, тип или статус работы.' : 'После следующей синхронизации поддерживаемые активности Moodle появятся здесь автоматически.'} />}
        </Card>;
      }) : <Card className="courses-empty"><EmptyState icon={<BookOpen />} title="Доступных курсов пока нет" text="Курсы появятся после добавления администратором и сопоставления с вашей учётной записью LMS." /></Card>}
    </div>
    <LmsSyncErrorDialog
      open={Boolean(syncErrorCourse)}
      courseTitle={syncErrorCourse?.title ?? ''}
      diagnostic={syncErrorCourse?.syncError}
      retrying={syncRetrying}
      onClose={() => setSyncErrorCourse(null)}
      onRetry={() => void retrySync()}
    />
  </div>;
}
