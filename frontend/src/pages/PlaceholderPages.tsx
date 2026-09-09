import { AlertTriangle, BookOpenCheck, CheckCircle2, Clock3, FileCode2, Library, Pencil, Plus, Rocket, ShieldCheck, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { Badge, Button, Card, EmptyState, Field, InlineError, Modal, PageLoader, useToast } from '../components/ui';
import { useAuth } from '../context/AuthContext';
import { api, ApiError } from '../lib/api';
import { formatDate, kindLabel } from '../lib/utils';
import type { Assessment, AssessmentPublicationTargets, Course, CourseGroup, HiddenTestCase, HiddenTestManifestV1, TaskBankItem } from '../types';

type TaskBankForm = {
  course: string; slug: string; category: string; title: string; statement: string; language: 'C' | 'CPP'; multiFile: boolean;
  maxScore: string; hiddenTestsEnabled: boolean; hiddenTestCases: HiddenTestCase[];
};

function blankTaskBankForm(course = ''): TaskBankForm {
  return { course, slug: '', category: '', title: '', statement: '', language: 'CPP', multiFile: false, maxScore: '10', hiddenTestsEnabled: false, hiddenTestCases: [] };
}

function blankHiddenTest(index: number): HiddenTestCase {
  return { name: `Кейс ${index + 1}`, stdin: '', expected_stdout: '', comparison: 'EXACT' };
}

type PublicationIssue = { field?: string; code?: string; message: string };

const publicationIssueMessages: Record<string, string> = {
  LMS_IMPORT_REQUIRES_CONFIGURATION: 'Импортированный черновик создан старой версией системы. Повторите синхронизацию курса.',
  MOODLE_ANSWER_TRANSPORT_UNSUPPORTED: 'Способ отправки будет повторно проверен при запуске попытки и доставке ответа в Moodle.',
  MOODLE_SOURCE_UNCONFIRMED: 'Moodle не подтвердил название, условие, сроки, лимит времени, максимальный балл или число попыток. Повторите синхронизацию курса.',
  REQUIRED: 'Заполните обязательные параметры задания и добавьте стартовый файл.',
  INVALID_FILE: 'Проверьте описание стартовых файлов.',
  DUPLICATE_PATH: 'Имена стартовых файлов не должны повторяться.',
  TASK_REQUIRED: 'Добавьте хотя бы одно задание к работе.',
  TASK_VERSION_UNAVAILABLE: 'Выбранная версия задания больше недоступна.',
  TASK_COURSE_MISMATCH: 'Выбранное задание относится к другому курсу.',
  FILE_MODE_MISMATCH: 'Режим файлов в работе и задании должен совпадать.',
  INVALID_POINTS: 'Баллы задания должны быть больше нуля и не превышать максимум работы.',
  INVALID_WINDOW: 'Время закрытия должно быть позже времени открытия.',
  POSITIVE_SCORE_REQUIRED: 'Максимальный балл должен быть больше нуля.',
  SECTION_COURSE_MISMATCH: 'Выбранный раздел не относится к этому курсу.',
  CONTENT_HASH_MISMATCH: 'Сохраните условие задания ещё раз перед публикацией.',
  INVALID_HIDDEN_TEST_MANIFEST: 'Проверьте настройки скрытых тестов.',
  INDIVIDUAL_PUBLICATION_UNSUPPORTED: 'Работа открывается группе. Индивидуальную доступность каждого студента система проверит непосредственно в Moodle при запуске.',
};

export function formatPublicationIssues(issues: PublicationIssue[]): string {
  const messages = issues.map((issue) => publicationIssueMessages[issue.code ?? ''] ?? 'Проверьте параметры работы перед публикацией.')
    .map((message) => message.trim()).filter(Boolean);
  return [...new Set(messages)].join(' ');
}

export function AssessmentIntroPage() {
  const { primaryRole } = useAuth();
  const { assessmentId = '' } = useParams();
  const [assessment, setAssessment] = useState<Assessment | null>(null);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [editing, setEditing] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();
  const load = useCallback(async () => {
    setLoading(true);
    try { setAssessment(await api.getAssessment(assessmentId)); setError(null); }
    catch (caught) { setError(caught instanceof Error ? caught.message : 'Работа не найдена'); }
    finally { setLoading(false); }
  }, [assessmentId]);
  useEffect(() => { void load(); }, [load]);
  if (loading) return <PageLoader label="Получаем параметры работы…" />;
  if (error && !assessment) return <InlineError message={error} retry={() => void load()} />;
  if (!assessment) return <InlineError message="Работа не найдена" />;
  const item = assessment;
  const lmsManaged = item.requiresLiveLmsPreparation || Boolean(item.policy?.moodle_metadata_read_only);
  const notOpened = Boolean(item.startsAt && new Date(item.startsAt).getTime() > Date.now());
  const closed = item.status === 'CLOSED' || Boolean(item.deadlineAt && new Date(item.deadlineAt).getTime() <= Date.now());
  async function start() {
    setStarting(true); setError(null);
    try { const attempt = await api.startAttempt(item.id); navigate(`/ide/${attempt.id}`); }
    catch (caught) { setError(caught instanceof Error ? caught.message : 'Попытка не запущена'); }
    finally { setStarting(false); }
  }
  return <div className="content-width narrow-page">
    <Link to="/courses" className="back-link">← Ко всем работам</Link>
    <Card className="assessment-intro">
      <div className="intro-badges"><Badge tone={item.kind === 'EXAM' ? 'danger' : item.kind === 'CONTROL' ? 'warning' : 'neutral'}>{kindLabel[item.kind]}</Badge>{primaryRole === 'TEACHER' && <Badge tone={item.publicationStatus === 'PUBLISHED' ? 'success' : 'warning'}>{item.publicationStatus === 'PUBLISHED' ? 'Опубликована' : item.publicationStatus === 'CLOSED' ? 'Закрыта' : 'Черновик'}</Badge>}</div>
      <h1>{item.title}</h1><p>{item.summary || 'Дополнительные инструкции преподавателем не указаны.'}</p>{error && <InlineError message={error} />}
      <div className="intro-facts"><div><Clock3 /><span><small>Продолжительность</small><strong>{item.durationMinutes ? `${item.durationMinutes} минут` : 'Без ограничения'}</strong></span></div><div><FileCode2 /><span><small>{lmsManaged ? 'Формат ответа' : 'Рабочая область'}</small><strong>{lmsManaged ? `Уточняется при запуске · ${item.standard}` : `${item.fileMode === 'MULTI' ? 'Несколько файлов' : 'Один файл'} · ${item.standard}`}</strong></span></div><div><ShieldCheck /><span><small>Правило вставки</small><strong>{item.pastePolicy === 'STRICT' ? 'Только внутри попытки' : 'Разрешена'}</strong></span></div></div>
      {item.startsAt && <p className="schedule-copy">Открытие: {formatDate(item.startsAt)}{item.deadlineAt && ` · закрытие: ${formatDate(item.deadlineAt)}`}</p>}
      {primaryRole === 'TEACHER' ? <div className="teacher-intro-actions"><p>Название, условие, сроки, максимальный балл и число попыток загружаются из Moodle. В Мехмат.Практикуме вы выбираете только группы преподавателя. Возможность сдачи для конкретного студента и способ отправки проверяются непосредственно в Moodle при запуске.</p>{item.publicationStatus !== 'CLOSED' && <div><Button size="lg" onClick={() => setEditing(true)}><ShieldCheck size={16} /> {item.publicationStatus === 'DRAFT' ? 'Настроить доступ' : 'Изменить доступ'}</Button></div>}</div> : <><div className="self-check"><CheckCircle2 /><span><strong>Интерфейс готов к началу</strong><small>{lmsManaged ? 'При запуске Moodle проверит доступность работы именно для вашей учётной записи.' : 'Окончательную доступность, срок и число попыток проверит сервер.'}</small></span></div><label className="rules-check"><input type="checkbox" checked={accepted} onChange={(event) => setAccepted(event.target.checked)} /><span>Я ознакомился с параметрами работы и понимаю, что серверное время является авторитетным.</span></label>{item.attemptId ? <Button size="lg" loading={starting} onClick={() => lmsManaged ? void start() : navigate(`/ide/${item.attemptId}`)}>Продолжить попытку</Button> : <Button size="lg" loading={starting} disabled={!accepted || (!lmsManaged && (notOpened || closed))} onClick={() => void start()}>{!lmsManaged && closed ? 'Работа закрыта' : !lmsManaged && notOpened ? `Откроется ${formatDate(item.startsAt)}` : 'Начать попытку'}</Button>}</>}
    </Card>
    {editing && <MoodlePublicationEditor assessment={item} onUpdated={setAssessment} onClose={() => setEditing(false)} />}
  </div>;
}

function MoodlePublicationEditor({ assessment, onUpdated, onClose }: { assessment: Assessment; onUpdated(value: Assessment): void; onClose(): void }) {
  const [targets, setTargets] = useState<AssessmentPublicationTargets | null>(null);
  const [selectedGroups, setSelectedGroups] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();

  useEffect(() => {
    let active = true;
    api.getAssessmentPublicationTargets(assessment.id).then((value) => {
      if (!active) return;
      const selectedGroupExternalIds = new Set((assessment.availabilityRules ?? [])
        .filter((rule) => rule.allowed && rule.targetType === 'GROUP')
        .map((rule) => rule.targetExternalId));
      setTargets(value);
      setSelectedGroups(new Set(value.groups.filter((group) => selectedGroupExternalIds.has(group.externalId)).map((group) => group.id)));
      setError(null);
    }).catch((caught) => {
      if (active) setError(caught instanceof Error ? caught.message : 'Адресаты Moodle не загружены');
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [assessment.availabilityRules, assessment.id]);

  function toggleGroup(id: string) {
    setSelectedGroups((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  async function save() {
    if (!selectedGroups.size) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await api.publishAssessment(assessment.id, [...selectedGroups]);
      onUpdated(updated);
      toast.push('success', assessment.publicationStatus === 'DRAFT' ? 'Работа открыта выбранным группам' : 'Доступ к работе обновлён');
      onClose();
    } catch (caught) {
      const details = caught instanceof ApiError && caught.issues.length
        ? formatPublicationIssues(caught.issues)
        : caught instanceof Error ? caught.message : 'Доступ не изменён';
      setError(details);
    } finally {
      setSaving(false);
    }
  }

  return <Modal open title={assessment.publicationStatus === 'DRAFT' ? 'Открыть работу' : 'Доступ к работе'} width="720px" onClose={onClose} footer={<><Button variant="ghost" onClick={onClose}>Отмена</Button><Button loading={saving} disabled={loading || !selectedGroups.size} onClick={() => void save()}><ShieldCheck size={15} /> {assessment.publicationStatus === 'DRAFT' ? 'Открыть работу' : 'Сохранить доступ'}</Button></>}>
    <div className="moodle-publication-form">
      <section className="moodle-source-summary"><strong>{assessment.title}</strong><p>{assessment.summary || 'Moodle не опубликовал отдельное условие для этой активности.'}</p><dl><div><dt>Период</dt><dd>{assessment.startsAt ? formatDate(assessment.startsAt) : 'без даты открытия'} · {assessment.deadlineAt ? formatDate(assessment.deadlineAt) : 'без даты закрытия'}</dd></div><div><dt>Оценивание</dt><dd>до {assessment.maxScore} баллов · попыток: {assessment.attemptLimit ?? 'без ограничения'}</dd></div></dl><small>Эти данные доступны только для чтения и обновляются при синхронизации курса с Moodle.</small></section>
      {error && <InlineError message={error} />}
      <section className="moodle-group-picker"><header><strong>Группы преподавателя</strong><small>Работа появится у всех студентов выбранных групп. При запуске Moodle отдельно проверит доступность для конкретного студента.</small></header>{loading ? <PageLoader label="Получаем группы…" /> : targets?.groups.length ? targets.groups.map((group) => <label key={group.id} className="option-check"><input type="checkbox" checked={selectedGroups.has(group.id)} onChange={() => toggleGroup(group.id)} /><span><strong>{group.name}</strong><small>Идентификатор группы Moodle: {group.externalId}</small></span></label>) : <p className="modal-copy">Доступных групп нет.</p>}</section>
    </div>
  </Modal>;
}

function DraftAssessmentEditor({ assessment, onUpdated, onClose }: { assessment: Assessment; onUpdated(value: Assessment): void; onClose(): void }) {
  const lmsMapping = assessment.policy?.lms_activity_mapping && typeof assessment.policy.lms_activity_mapping === 'object'
    ? assessment.policy.lms_activity_mapping as Record<string, unknown> : undefined;
  const [form, setForm] = useState({
    title: assessment.title, instructions: assessment.summary, opensAt: toDateTimeLocal(assessment.startsAt), closesAt: toDateTimeLocal(assessment.deadlineAt),
    maxScore: String(assessment.maxScore), studentAiEnabled: assessment.aiEnabled,
    reviewRequired: assessment.reviewRequired ?? assessment.kind !== 'LAB', decisionSupportEnabled: assessment.decisionSupportEnabled ?? true,
    syncDeadlines: Boolean(lmsMapping?.sync_deadlines),
  });
  const [groups, setGroups] = useState<CourseGroup[]>([]);
  const [availabilityTarget, setAvailabilityTarget] = useState('COURSE');
  const [saving, setSaving] = useState(false);
  const [ruleSaving, setRuleSaving] = useState(false);
  const toast = useToast();
  useEffect(() => {
    let active = true;
    api.getCourseGroups(assessment.courseId).then((items) => { if (active) setGroups(items.filter((item) => item.active && item.externalId)); })
      .catch((caught) => { if (active) toast.push('error', 'Группы курса не загружены', caught instanceof Error ? caught.message : undefined); });
    return () => { active = false; };
  }, [assessment.courseId, toast]);
  const rules = assessment.availabilityRules ?? [];
  const chosenType = availabilityTarget === 'COURSE' ? 'COURSE' : 'GROUP';
  const chosenExternalId = availabilityTarget === 'COURSE' ? '' : availabilityTarget.slice('GROUP:'.length);
  const duplicateRule = rules.some((rule) => rule.targetType === chosenType && rule.targetExternalId === chosenExternalId);

  async function save() {
    const maxScore = Number(form.maxScore);
    if (!form.title.trim() || !Number.isFinite(maxScore) || maxScore <= 0) { toast.push('error', 'Проверьте название и максимальный балл'); return; }
    if (form.opensAt && form.closesAt && new Date(form.closesAt) <= new Date(form.opensAt)) { toast.push('error', 'Закрытие должно быть позже открытия'); return; }
    setSaving(true);
    try {
      const updated = await api.updateAssessment(assessment.id, {
        title: form.title.trim(), instructions: form.instructions.trim(), opens_at: form.opensAt ? new Date(form.opensAt).toISOString() : null,
        closes_at: form.closesAt ? new Date(form.closesAt).toISOString() : null, max_score: maxScore,
        student_ai_enabled: form.studentAiEnabled, review_required: form.reviewRequired, decision_support_enabled: form.decisionSupportEnabled,
        policy: lmsMapping ? { ...(assessment.policy ?? {}), lms_activity_mapping: { ...lmsMapping, sync_deadlines: form.syncDeadlines } } : assessment.policy,
      });
      onUpdated(updated); toast.push('success', 'Настройки черновика сохранены'); onClose();
    } catch (caught) { toast.push('error', 'Черновик не сохранён', caught instanceof Error ? caught.message : undefined); }
    finally { setSaving(false); }
  }

  async function addRule() {
    if (duplicateRule || ruleSaving) return;
    setRuleSaving(true);
    try {
      const created = await api.addAvailabilityRule(assessment.id, { target_type: chosenType, target_external_id: chosenExternalId });
      onUpdated({ ...assessment, availabilityRules: [...rules, created] }); toast.push('success', 'Правило доступа добавлено');
    } catch (caught) { toast.push('error', 'Правило не добавлено', caught instanceof Error ? caught.message : undefined); }
    finally { setRuleSaving(false); }
  }

  async function removeRule(ruleId: string) {
    if (!window.confirm('Удалить это правило доступности?')) return;
    setRuleSaving(true);
    try {
      await api.deleteAvailabilityRule(assessment.id, ruleId);
      onUpdated({ ...assessment, availabilityRules: rules.filter((rule) => rule.id !== ruleId) }); toast.push('success', 'Правило доступа удалено');
    } catch (caught) { toast.push('error', 'Правило не удалено', caught instanceof Error ? caught.message : undefined); }
    finally { setRuleSaving(false); }
  }

  const ruleLabel = (type: string, externalId: string) => type === 'COURSE' ? 'Весь курс' : type === 'GROUP'
    ? `Группа: ${groups.find((group) => group.externalId === externalId)?.name ?? externalId}` : `Отдельный студент: ${externalId}`;
  return <Modal open title="Настройки черновика" width="680px" onClose={onClose} footer={<><Button variant="ghost" onClick={onClose}>Отмена</Button><Button loading={saving} onClick={() => void save()}><Pencil size={15} /> Сохранить</Button></>}>
    <div className="draft-assessment-form"><Field label="Название"><input value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} /></Field><Field label="Инструкции"><textarea rows={5} value={form.instructions} onChange={(event) => setForm({ ...form, instructions: event.target.value })} /></Field><div><Field label="Открытие"><input type="datetime-local" value={form.opensAt} onChange={(event) => setForm({ ...form, opensAt: event.target.value })} /></Field><Field label="Закрытие"><input type="datetime-local" value={form.closesAt} onChange={(event) => setForm({ ...form, closesAt: event.target.value })} /></Field><Field label="Максимальный балл"><input type="number" min="0.01" step="0.01" value={form.maxScore} onChange={(event) => setForm({ ...form, maxScore: event.target.value })} /></Field></div>{lmsMapping && <label className="option-check"><input type="checkbox" checked={form.syncDeadlines} onChange={(event) => setForm({ ...form, syncDeadlines: event.target.checked })} /> Обновлять сроки из Moodle при следующих синхронизациях</label>}<label className="option-check"><input type="checkbox" checked={form.studentAiEnabled} onChange={(event) => setForm({ ...form, studentAiEnabled: event.target.checked })} /> Разрешить учебного ИИ-помощника</label><label className="option-check"><input type="checkbox" checked={form.reviewRequired} onChange={(event) => setForm({ ...form, reviewRequired: event.target.checked })} /> Требуется проверка преподавателя</label><label className="option-check"><input type="checkbox" checked={form.decisionSupportEnabled} onChange={(event) => setForm({ ...form, decisionSupportEnabled: event.target.checked })} /> Включить СППР</label>
      <section className="availability-editor"><header><strong>Доступность</strong><small>{rules.length ? 'Явные правила применяются сервером по приоритету.' : 'Без правил работа доступна всему курсу.'}</small></header>{rules.map((rule) => <div key={rule.id}><span><strong>{ruleLabel(rule.targetType, rule.targetExternalId)}</strong><small>{rule.allowed ? 'Доступ разрешён' : 'Доступ запрещён'}</small></span><button type="button" disabled={ruleSaving} onClick={() => void removeRule(rule.id)} aria-label="Удалить правило"><Trash2 size={15} /></button></div>)}<footer><select value={availabilityTarget} onChange={(event) => setAvailabilityTarget(event.target.value)}><option value="COURSE">Весь курс</option>{groups.map((group) => <option key={group.id} value={`GROUP:${group.externalId}`}>Группа: {group.name}</option>)}</select><Button size="sm" variant="secondary" loading={ruleSaving} disabled={duplicateRule} onClick={() => void addRule()}><Plus size={14} /> Добавить правило</Button></footer></section>
    </div>
  </Modal>;
}

function toDateTimeLocal(value?: string): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

export function TaskBankPage() {
  const { session } = useAuth();
  const toast = useToast();
  const [items, setItems] = useState<TaskBankItem[]>([]);
  const [courses, setCourses] = useState<Course[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [publishingId, setPublishingId] = useState<string | null>(null);
  const [versioningItemId, setVersioningItemId] = useState<string | null>(null);
  const [editingVersionId, setEditingVersionId] = useState<string | null>(null);
  const [form, setForm] = useState<TaskBankForm>(() => blankTaskBankForm());
  const [searchParams, setSearchParams] = useSearchParams();
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [loadedItems, loadedCourses] = await Promise.all([api.getTaskBank(), api.getCourses(session)]);
      setItems(loadedItems); setCourses(loadedCourses.filter((course) => course.role === 'TEACHER')); setError(null);
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Не удалось открыть банк заданий'); }
    finally { setLoading(false); }
  }, [session]);
  useEffect(() => { void load(); }, [load]);
  async function create() {
    if (!form.course || (!versioningItemId && !form.slug.trim()) || !form.title.trim() || !form.statement.trim()) return;
    const maxScore = Number(form.maxScore);
    if (!Number.isFinite(maxScore) || maxScore <= 0) { toast.push('error', 'Максимальный балл должен быть положительным числом'); return; }
    const hiddenTests = buildHiddenTestManifest(form.hiddenTestsEnabled, form.hiddenTestCases);
    if (hiddenTests.error) { toast.push('error', 'Проверьте скрытые тесты', hiddenTests.error); return; }
    setSaving(true);
    try {
      const itemId = versioningItemId ?? (await api.createTaskItem({ course: form.course, slug: form.slug.trim(), category: form.category.trim(), tags: [] })).id;
      const payload = { title: form.title.trim(), statement: form.statement.trim(), language: form.language, multiFile: form.multiFile, maxScore, hiddenTestManifest: hiddenTests.manifest };
      if (editingVersionId) await api.updateTaskVersion(editingVersionId, itemId, payload);
      else await api.createTaskVersion(itemId, payload);
      setCreateOpen(false); setVersioningItemId(null); setEditingVersionId(null); setForm(blankTaskBankForm()); await load();
      toast.push('success', editingVersionId ? 'Черновик задания сохранён' : versioningItemId ? 'Новая неизменяемая версия создана' : 'Черновик задания создан');
    } catch (caught) { toast.push('error', 'Задание не создано', caught instanceof Error ? caught.message : undefined); }
    finally { setSaving(false); }
  }
  function openNewItem() {
    setVersioningItemId(null); setEditingVersionId(null); setForm(blankTaskBankForm(courses[0]?.id ?? '')); setCreateOpen(true);
  }
  function openNextVersion(item: TaskBankItem) {
    const latest = item.latestVersion;
    if (!item.course || !latest) return;
    setVersioningItemId(item.id);
    setEditingVersionId(null);
    const cases = latest.hiddenTestManifest?.cases.map((item) => ({ ...item })) ?? [];
    setForm({ course: item.course, slug: item.slug, category: item.category, title: latest.title, statement: latest.statement, language: latest.language.toUpperCase() === 'C' ? 'C' : 'CPP', multiFile: latest.multiFile, maxScore: String(latest.maxScore), hiddenTestsEnabled: cases.length > 0, hiddenTestCases: cases });
    setCreateOpen(true);
  }
  function openDraft(item: TaskBankItem) {
    const latest = item.latestVersion;
    if (!item.course || !latest || latest.status !== 'DRAFT') return;
    setVersioningItemId(item.id);
    setEditingVersionId(latest.id);
    const cases = latest.hiddenTestManifest?.cases.map((entry) => ({ ...entry })) ?? [];
    setForm({ course: item.course, slug: item.slug, category: item.category, title: latest.title, statement: latest.statement, language: latest.language.toUpperCase() === 'C' ? 'C' : 'CPP', multiFile: latest.multiFile, maxScore: String(latest.maxScore), hiddenTestsEnabled: cases.length > 0, hiddenTestCases: cases });
    setCreateOpen(true);
  }
  useEffect(() => {
    const versionId = searchParams.get('editVersion');
    if (loading || !versionId) return;
    const item = items.find((candidate) => candidate.latestVersion?.id === versionId);
    if (item) openDraft(item);
    const next = new URLSearchParams(searchParams);
    next.delete('editVersion');
    setSearchParams(next, { replace: true });
  }, [items, loading, searchParams, setSearchParams]);
  async function publishVersion(versionId: string) {
    setPublishingId(versionId);
    try {
      const result = await api.validateTaskVersion(versionId);
      if (!result.valid) throw new Error(formatPublicationIssues(result.errors) || 'Версия не прошла проверку перед публикацией.');
      await api.publishTaskVersion(versionId); await load(); toast.push('success', 'Версия задания опубликована');
    } catch (caught) { toast.push('error', 'Версия не опубликована', caught instanceof Error ? caught.message : undefined); }
    finally { setPublishingId(null); }
  }
  if (loading) return <PageLoader label="Загружаем банк заданий…" />;
  if (error) return <InlineError message={error} retry={() => void load()} />;
  const hiddenTestValidation = buildHiddenTestManifest(form.hiddenTestsEnabled, form.hiddenTestCases);
  return <div className="content-width task-bank-page"><div className="page-heading"><div><span className="eyebrow">Версионируемый контент</span><h1>Банк заданий</h1><p>Импортированные из Moodle элементы создаются как безопасные черновики и требуют проверки перед публикацией.</p></div><Button onClick={openNewItem}><Plus size={16} /> Создать задание</Button></div>{items.length ? <div className="task-grid">{items.map((item) => {
      const latestVersion = item.latestVersion;
      const statusLabel = !latestVersion
        ? 'Без версии'
        : latestVersion.status === 'PUBLISHED'
          ? 'Опубликовано'
          : 'Черновик';
      return <Card key={item.id} className="task-card">
        <header className="task-card__header">
          <span className="task-card__icon" aria-hidden="true"><BookOpenCheck /></span>
          <div className="task-card__state">
            <Badge tone={latestVersion?.status === 'PUBLISHED' ? 'success' : 'warning'} className="task-card__status">
              <span>{statusLabel}</span>
            </Badge>
            {latestVersion && <span className="task-card__version">Версия {latestVersion.number}</span>}
          </div>
        </header>
        <h3>{latestVersion?.title || item.slug}</h3>
        <p>{latestVersion ? `${latestVersion.languageStandard} · ${latestVersion.multiFile ? 'несколько файлов' : 'один файл'}` : 'Создайте первую версию задания'}</p>
        <footer><small>{item.category || 'Без категории'}</small>{latestVersion?.status === 'DRAFT' ? <div className="task-card__actions"><Button size="sm" variant="secondary" onClick={() => openDraft(item)}><Pencil size={14} /> Изменить</Button><Button size="sm" variant="secondary" loading={publishingId === latestVersion.id} onClick={() => void publishVersion(latestVersion.id)}><Rocket size={14} /> Опубликовать</Button></div> : latestVersion && item.course ? <Button size="sm" variant="secondary" onClick={() => openNextVersion(item)}><Plus size={14} /> Новая версия</Button> : <code>{item.slug}</code>}</footer>
      </Card>;
    })}</div> : <Card className="task-bank-empty"><EmptyState icon={<Library />} title="Банк заданий пуст" text="После синхронизации поддерживаемые лабораторные, самостоятельные, контрольные и экзаменационные активности Moodle появятся здесь как черновики." action={<Button onClick={openNewItem}>Создать первое задание</Button>} /></Card>}
    <Modal open={createOpen} title={editingVersionId ? 'Настройка черновика' : versioningItemId ? 'Новая версия задания' : 'Новое задание'} width="860px" onClose={() => { setCreateOpen(false); setVersioningItemId(null); setEditingVersionId(null); }} footer={<><Button variant="ghost" onClick={() => { setCreateOpen(false); setVersioningItemId(null); setEditingVersionId(null); }}>Отмена</Button><Button loading={saving} disabled={!form.course || (!versioningItemId && !form.slug.trim()) || !form.title.trim() || !form.statement.trim() || !Number.isFinite(Number(form.maxScore)) || Number(form.maxScore) <= 0 || Boolean(hiddenTestValidation.error)} onClick={() => void create()}>{editingVersionId ? 'Сохранить черновик' : 'Создать черновик версии'}</Button></>}><div className="task-form"><Field label="Курс"><select value={form.course} disabled={Boolean(versioningItemId)} onChange={(event) => setForm({ ...form, course: event.target.value })}><option value="">Выберите курс</option>{courses.map((course) => <option key={course.id} value={course.id}>{course.title}</option>)}</select></Field><div><Field label="Slug"><input value={form.slug} disabled={Boolean(versioningItemId)} onChange={(event) => setForm({ ...form, slug: event.target.value })} placeholder="vectors-basic" /></Field><Field label="Категория"><input value={form.category} disabled={Boolean(versioningItemId)} onChange={(event) => setForm({ ...form, category: event.target.value })} placeholder="STL" /></Field></div><div><Field label="Язык"><select value={form.language} onChange={(event) => setForm({ ...form, language: event.target.value as 'C' | 'CPP' })}><option value="CPP">C++20</option><option value="C">C17</option></select></Field><Field label="Максимальный балл"><input type="number" min="0.01" step="0.01" value={form.maxScore} onChange={(event) => setForm({ ...form, maxScore: event.target.value })} /></Field><label className="option-check"><input type="checkbox" checked={form.multiFile} onChange={(event) => setForm({ ...form, multiFile: event.target.checked })} /> Многофайловая сборка</label></div><Field label={versioningItemId ? 'Название версии' : 'Название первой версии'}><input value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} /></Field><Field label="Условие"><textarea rows={7} value={form.statement} onChange={(event) => setForm({ ...form, statement: event.target.value })} /></Field><HiddenTestManifestEditor enabled={form.hiddenTestsEnabled} cases={form.hiddenTestCases} error={hiddenTestValidation.error} onEnabled={(enabled) => setForm({ ...form, hiddenTestsEnabled: enabled, hiddenTestCases: enabled && form.hiddenTestCases.length === 0 ? [blankHiddenTest(0)] : form.hiddenTestCases })} onCases={(hiddenTestCases) => setForm({ ...form, hiddenTestCases })} /><p className="modal-copy">{editingVersionId ? 'До публикации черновик можно изменять. После сохранения импортированная заглушка будет заменена проверенным преподавателем содержимым. ' : versioningItemId ? 'Опубликованная версия останется неизменной; будет создан следующий номер. ' : ''}Профиль runner и стартовый файл будут выбраны согласованно: {form.language === 'C' ? 'C17' : 'C++20'} · {form.multiFile ? 'multi' : 'single'}. Скрытые тесты сохраняются в manifest v1 и окончательно проверяются сервером при публикации.</p></div></Modal>
  </div>;
}

function HiddenTestManifestEditor({ enabled, cases, error, onEnabled, onCases }: {
  enabled: boolean; cases: HiddenTestCase[]; error?: string; onEnabled(value: boolean): void; onCases(value: HiddenTestCase[]): void;
}) {
  function update(index: number, patch: Partial<HiddenTestCase>) {
    onCases(cases.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item));
  }
  return <section className="hidden-tests-editor">
    <header><label className="option-check"><input type="checkbox" checked={enabled} onChange={(event) => onEnabled(event.target.checked)} /> Детерминированные скрытые тесты</label><small>Manifest v1: только stdin, ожидаемый stdout и безопасный режим сравнения.</small></header>
    {enabled && <div className="hidden-test-body"><div className="hidden-test-toolbar"><span>{cases.length} из 20 кейсов</span><Button size="sm" variant="secondary" disabled={cases.length >= 20} onClick={() => onCases([...cases, blankHiddenTest(cases.length)])}><Plus size={14} /> Добавить кейс</Button></div>{cases.map((testCase, index) => <article className="hidden-test-case" key={index}><header><strong>Кейс {index + 1}</strong><button type="button" disabled={cases.length <= 1} aria-label={`Удалить кейс ${index + 1}`} title={cases.length <= 1 ? 'Manifest должен содержать хотя бы один кейс' : 'Удалить кейс'} onClick={() => onCases(cases.filter((_, itemIndex) => itemIndex !== index))}><Trash2 size={15} /></button></header><div className="hidden-test-meta"><Field label="Название"><input maxLength={100} value={testCase.name} onChange={(event) => update(index, { name: event.target.value })} /></Field><Field label="Сравнение"><select value={testCase.comparison} onChange={(event) => update(index, { comparison: event.target.value as HiddenTestCase['comparison'] })}><option value="EXACT">Точное</option><option value="TRIM_TRAILING_WHITESPACE">Игнорировать хвостовые пробелы</option></select></Field></div><div className="hidden-test-streams"><Field label="stdin" hint={`${utf8ByteLength(testCase.stdin).toLocaleString('ru-RU')} / 262 144 байт UTF-8`}><textarea rows={4} maxLength={262_144} spellCheck={false} value={testCase.stdin} onChange={(event) => update(index, { stdin: event.target.value })} /></Field><Field label="Ожидаемый stdout" hint={`${utf8ByteLength(testCase.expected_stdout).toLocaleString('ru-RU')} / 262 144 байт UTF-8`}><textarea rows={4} maxLength={262_144} spellCheck={false} value={testCase.expected_stdout} onChange={(event) => update(index, { expected_stdout: event.target.value })} /></Field></div></article>)}{error && <p className="hidden-test-error"><AlertTriangle size={14} /> {error}</p>}</div>}
  </section>;
}

export function buildHiddenTestManifest(enabled: boolean, cases: HiddenTestCase[]): { manifest?: HiddenTestManifestV1; error?: string } {
  if (!enabled) return {};
  if (cases.length < 1 || cases.length > 20) return { error: 'Нужно добавить от 1 до 20 кейсов.' };
  const normalizedCases = cases.map((item) => ({
    name: item.name.trim(), stdin: item.stdin, expected_stdout: item.expected_stdout, comparison: item.comparison,
  }));
  const emptyName = normalizedCases.findIndex((item) => !item.name);
  if (emptyName >= 0) return { error: `У кейса ${emptyName + 1} не заполнено название.` };
  const longName = normalizedCases.findIndex((item) => item.name.length > 100);
  if (longName >= 0) return { error: `Название кейса ${longName + 1} длиннее 100 символов.` };
  const oversized = normalizedCases.findIndex((item) => utf8ByteLength(item.stdin) > 262_144 || utf8ByteLength(item.expected_stdout) > 262_144);
  if (oversized >= 0) return { error: `stdin или stdout кейса ${oversized + 1} превышает 262 144 байта UTF-8.` };
  const invalidComparison = normalizedCases.findIndex((item) => !['EXACT', 'TRIM_TRAILING_WHITESPACE'].includes(item.comparison));
  if (invalidComparison >= 0) return { error: `У кейса ${invalidComparison + 1} выбран неподдерживаемый режим сравнения.` };
  const names = normalizedCases.map((item) => item.name.normalize('NFKC').toLocaleLowerCase());
  if (new Set(names).size !== names.length) return { error: 'Названия кейсов должны быть уникальными без учёта регистра.' };
  const manifest: HiddenTestManifestV1 = { schema_version: 1, cases: normalizedCases };
  if (new TextEncoder().encode(JSON.stringify(manifest)).length > 1_048_576) return { error: 'Manifest превышает общий лимит 1 МиБ.' };
  return { manifest };
}

function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).length;
}
