import {
  AlertTriangle, Bot, CheckCircle2, ChevronDown, CircleStop, Clock3, Cloud,
  FileClock, FilePlus2, History, Info, MessageCircleQuestion, PanelBottomClose, PanelBottomOpen,
  PanelLeftClose, PanelLeftOpen, Play, Send, Square, TerminalSquare, Trash2, X,
} from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { CodeWorkspace, type CodeWorkspaceHandle } from '../components/CodeWorkspace';
import { Badge, Button, Field, InlineError, Modal, PageLoader, useToast } from '../components/ui';
import { api, ApiError } from '../lib/api';
import { cn, formatClientContext, formatDate, formatRemaining, formatSessionElapsed, isTextDataPath, isTranslationUnitPath, severityOrder, validateWorkspacePath } from '../lib/utils';
import { createUuid } from '../lib/uuid';
import type { Attempt, Diagnostic, HistoryEvent, InteractiveRun, WorkspaceFile } from '../types';

type BottomTab = 'output' | 'problems' | 'history';
type SubmissionPhase = 'confirm' | 'preparing' | 'waiting' | 'error';
const SUBMISSION_SLOW_AFTER_MS = 12_000;

function canDeleteWorkspaceFile(
  file: WorkspaceFile,
  files: WorkspaceFile[],
  fileMode: Attempt['fileMode'],
): boolean {
  if (isTextDataPath(file.path)) return true;
  if (fileMode === 'SINGLE') return false;
  const remaining = files.filter((item) => item.id !== file.id);
  return remaining.some((item) => isTranslationUnitPath(item.path));
}

export function IdePage() {
  const { attemptId = '' } = useParams();
  const navigate = useNavigate();
  const toast = useToast();
  const editorRef = useRef<CodeWorkspaceHandle>(null);
  const saveTimerRef = useRef<number>();
  const attemptRef = useRef<Attempt | null>(null);
  const dirtyFilesRef = useRef<Array<{ file: WorkspaceFile; source: 'typing' | 'internal_paste'; receiptId?: string }>>([]);
  const flushPromiseRef = useRef<Promise<number> | null>(null);
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [files, setFiles] = useState<WorkspaceFile[]>([]);
  const [activeFileId, setActiveFileId] = useState('');
  const [history, setHistory] = useState<HistoryEvent[]>([]);
  const [interactiveRun, setInteractiveRun] = useState<InteractiveRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<'saved' | 'saving' | 'offline' | 'error' | 'closed'>('saved');
  const [bottomTab, setBottomTab] = useState<BottomTab>('problems');
  const [running, setRunning] = useState(false);
  const [interactiveInput, setInteractiveInput] = useState('');
  const [remaining, setRemaining] = useState('');
  const [now, setNow] = useState(Date.now());
  const [statementOpen, setStatementOpen] = useState(true);
  const [bottomPanelOpen, setBottomPanelOpen] = useState(true);
  const [aiOpen, setAiOpen] = useState(false);
  const [submitOpen, setSubmitOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [newPath, setNewPath] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<WorkspaceFile | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submissionPhase, setSubmissionPhase] = useState<SubmissionPhase>('confirm');
  const [submissionError, setSubmissionError] = useState('');
  const [submissionSlow, setSubmissionSlow] = useState(false);
  const [courseId, setCourseId] = useState('');
  const [conflictOpen, setConflictOpen] = useState(false);
  const [lmsFinalizedOpen, setLmsFinalizedOpen] = useState(false);
  const latestFiles = useRef(files);
  const interactiveRunRef = useRef<InteractiveRun | null>(null);
  const lmsFinalizedRef = useRef(false);
  latestFiles.current = files;
  attemptRef.current = attempt;
  interactiveRunRef.current = interactiveRun;

  const closeForLmsFinalization = useCallback((reportedAttempt?: Attempt) => {
    if (lmsFinalizedRef.current) {
      setLmsFinalizedOpen(true);
      return;
    }
    lmsFinalizedRef.current = true;
    if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
    saveTimerRef.current = undefined;
    dirtyFilesRef.current = [];
    setSaveState('closed');
    setSubmitOpen(false); setCreateOpen(false); setDeleteTarget(null); setConflictOpen(false); setAiOpen(false);
    setSubmissionPhase('confirm'); setSubmissionError(''); setSubmissionSlow(false);
    setSubmitting(false); setRunning(false);
    const currentAttempt = reportedAttempt ?? attemptRef.current;
    if (currentAttempt) {
      const lockedAttempt = {
        ...currentAttempt,
        status: 'LOCKED' as const,
        closureReason: 'LMS_ATTEMPT_FINALIZED',
      };
      attemptRef.current = lockedAttempt;
      setAttempt(lockedAttempt);
      window.sessionStorage.removeItem(interactiveStorageKey(currentAttempt.id));
      const active = interactiveRunRef.current;
      if (active && !active.terminal) {
        void api.stopInteractiveAttempt(currentAttempt.id, active.sessionId).catch(() => undefined);
        const stopped = { ...active, status: 'STOPPED' as const, terminal: true };
        interactiveRunRef.current = stopped;
        setInteractiveRun(stopped);
      }
    }
    setLmsFinalizedOpen(true);
  }, []);

  const handleLmsFinalizedError = useCallback((caught: unknown): boolean => {
    if (caught instanceof ApiError && caught.code === 'LMS_ATTEMPT_FINALIZED') {
      closeForLmsFinalization();
      return true;
    }
    return false;
  }, [closeForLmsFinalization]);

  const load = useCallback(async () => {
    if (attemptRef.current?.id !== attemptId) {
      lmsFinalizedRef.current = false;
      setLmsFinalizedOpen(false);
    }
    setLoading(true); setError(null);
    try {
      let loaded = await api.getAttempt(attemptId);
      if (loaded.requiresLiveLmsPreparation) {
        const prepared = await api.startAttempt(loaded.assessmentId);
        if (prepared.id !== attemptId) {
          navigate(`/ide/${prepared.id}`, { replace: true });
          return;
        }
        loaded = await api.getAttempt(attemptId);
        if (loaded.requiresLiveLmsPreparation) {
          throw new ApiError(
            409,
            'MOODLE_RUNTIME_PREPARATION_REQUIRED',
            'Moodle preparation did not bind the active attempt',
          );
        }
      }
      const loadedHistory = await api.getHistory(attemptId);
      const resolvedCourseId = await api.resolveCourseId(loaded.assessmentId);
      dirtyFilesRef.current = []; setConflictOpen(false);
      attemptRef.current = loaded; setAttempt(loaded); setCourseId(resolvedCourseId); setFiles(loaded.files); setActiveFileId(loaded.files[0]?.id ?? ''); setHistory(loadedHistory); setSaveState('saved');
      setInteractiveRun(null); setInteractiveInput('');
      if (loaded.closureReason === 'LMS_ATTEMPT_FINALIZED') closeForLmsFinalization(loaded);
      else if (loaded.status === 'SUBMITTED' && loaded.checkpointStatus !== 'SYNCED') {
        setSubmissionPhase(loaded.checkpointStatus === 'ERROR' ? 'error' : 'waiting');
        setSubmissionError(loaded.checkpointStatus === 'ERROR' ? 'Moodle не подтвердил получение ответа.' : '');
        setSubmissionSlow(false);
        setSubmitOpen(true);
      } else {
        setSubmissionPhase('confirm'); setSubmissionError(''); setSubmissionSlow(false);
      }
      const storedSession = window.sessionStorage.getItem(interactiveStorageKey(loaded.id));
      if (storedSession && loaded.closureReason !== 'LMS_ATTEMPT_FINALIZED') {
        try {
          const recovered = await api.getInteractiveAttempt(loaded.id, storedSession);
          setInteractiveRun(recovered); setBottomTab('output'); setBottomPanelOpen(true);
        } catch {
          window.sessionStorage.removeItem(interactiveStorageKey(loaded.id));
        }
      }
    } catch (caught) {
      if (!handleLmsFinalizedError(caught)) setError(caught instanceof Error ? caught.message : 'Не удалось открыть попытку');
    }
    finally { setLoading(false); }
  }, [attemptId, closeForLmsFinalization, handleLmsFinalizedError, navigate]);
  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!attempt?.id || attempt.status !== 'ACTIVE' || lmsFinalizedRef.current) return;
    let cancelled = false;
    let polling = false;
    const poll = async () => {
      if (polling || cancelled || lmsFinalizedRef.current) return;
      polling = true;
      try {
        const current = await api.getAttemptStatus(attempt.id);
        if (cancelled) return;
        if (current.closureReason === 'LMS_ATTEMPT_FINALIZED') {
          closeForLmsFinalization();
          return;
        }
        setAttempt((value) => value?.id === current.id ? {
          ...value,
          status: current.status,
          closureReason: current.closureReason,
          closedAt: current.closedAt,
          lastCheckpointAt: current.lastCheckpointAt,
          checkpointStatus: current.checkpointStatus,
        } : value);
      } catch (caught) {
        if (!cancelled) handleLmsFinalizedError(caught);
      } finally {
        polling = false;
      }
    };
    void poll();
    const timer = window.setInterval(() => { void poll(); }, 5_000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [attempt?.id, attempt?.status, closeForLmsFinalization, handleLmsFinalizedError]);

  useEffect(() => {
    if (!attempt?.id || attempt.status !== 'SUBMITTED' || submissionPhase !== 'waiting') return;
    let cancelled = false;
    let polling = false;
    const poll = async () => {
      if (cancelled || polling) return;
      polling = true;
      try {
        const current = await api.getAttemptStatus(attempt.id);
        if (cancelled) return;
        if (current.closureReason === 'LMS_ATTEMPT_FINALIZED') {
          closeForLmsFinalization();
          return;
        }
        setAttempt((value) => value?.id === current.id ? {
          ...value,
          status: current.status,
          closureReason: current.closureReason,
          closedAt: current.closedAt,
          lastCheckpointAt: current.lastCheckpointAt,
          checkpointStatus: current.checkpointStatus,
        } : value);
        if (current.checkpointStatus === 'SYNCED') {
          setSubmissionSlow(false);
          setSubmissionPhase('confirm');
          setSubmitOpen(false);
          toast.push('success', 'Работа сдана', 'Moodle подтвердил получение ответа и завершение попытки.');
          navigate('/');
        } else if (current.checkpointStatus === 'ERROR') {
          setSubmissionSlow(false);
          setSubmissionError('Moodle не подтвердил получение ответа. Локальная финальная версия сохранена — её можно отправить повторно.');
          setSubmissionPhase('error');
        } else {
          setSubmissionError('');
        }
      } catch (caught) {
        if (!cancelled && !handleLmsFinalizedError(caught)) {
          setSubmissionError(caught instanceof Error ? caught.message : 'Не удалось проверить состояние отправки.');
        }
      } finally {
        polling = false;
      }
    };
    void poll();
    const timer = window.setInterval(() => { void poll(); }, 1_500);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [attempt?.id, attempt?.status, closeForLmsFinalization, handleLmsFinalizedError, navigate, submissionPhase, toast.push]);

  useEffect(() => {
    if (!submitOpen || submissionPhase !== 'waiting') return;
    const timer = window.setTimeout(() => setSubmissionSlow(true), SUBMISSION_SLOW_AFTER_MS);
    return () => window.clearTimeout(timer);
  }, [submissionPhase, submitOpen]);

  useEffect(() => {
    if (!attempt) return;
    const update = () => {
      const value = Date.now();
      setNow(value);
      const visibleEnd = attempt.deadlineAt ?? attempt.expectedEndAt;
      setRemaining(visibleEnd
        ? formatRemaining(visibleEnd, value)
        : formatSessionElapsed(attempt.startedAt, value));
    };
    update(); const timer = window.setInterval(update, 1000); return () => window.clearInterval(timer);
  }, [attempt]);

  useEffect(() => () => { if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current); }, []);
  useEffect(() => () => {
    const currentAttempt = attemptRef.current;
    const active = interactiveRunRef.current;
    if (currentAttempt && active && !active.terminal) {
      void api.stopInteractiveAttempt(currentAttempt.id, active.sessionId).catch(() => undefined);
    }
  }, []);
  useEffect(() => {
    if (!attempt?.id || !interactiveRun || interactiveRun.terminal) return;
    let cancelled = false;
    let timer = 0;
    const poll = async () => {
      try {
        const current = await api.getInteractiveAttempt(attempt.id, interactiveRun.sessionId);
        if (!cancelled) setInteractiveRun(current);
        if (!cancelled && !current.terminal) timer = window.setTimeout(() => { void poll(); }, 350);
      } catch (caught) {
        if (!cancelled) setInteractiveRun((current) => current ? {
          ...current,
          status: 'INFRA_ERROR',
          terminal: true,
          stderr: caught instanceof Error ? caught.message : 'Не удалось получить вывод программы',
        } : current);
      }
    };
    timer = window.setTimeout(() => { void poll(); }, 200);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [attempt?.id, interactiveRun?.sessionId, interactiveRun?.terminal]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (dirtyFilesRef.current.length) event.preventDefault(); };
    window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);
  }, []);
  const locked = !attempt || attempt.status !== 'ACTIVE' || Boolean(attempt.deadlineAt && new Date(attempt.deadlineAt).getTime() <= now);
  const diagnostics = interactiveRun?.diagnostics ?? [];
  const interactiveActive = Boolean(interactiveRun && !interactiveRun.terminal);

  function changeFile(fileId: string, content: string, source: 'typing' | 'internal_paste', receiptId?: string) {
    if (locked || !attempt || lmsFinalizedRef.current) return;
    const updated = files.map((file) => file.id === fileId ? { ...file, content } : file);
    const changedFile = updated.find((file) => file.id === fileId);
    if (!changedFile) return;
    latestFiles.current = updated; setFiles(updated); setSaveState('saving');
    const last = dirtyFilesRef.current.at(-1);
    const entry = { file: changedFile, source, receiptId };
    if (source === 'typing' && last?.source === 'typing' && last.file.id === fileId) dirtyFilesRef.current[dirtyFilesRef.current.length - 1] = entry;
    else dirtyFilesRef.current.push(entry);
    setHistory((items) => [{ id: createUuid(), type: source === 'internal_paste' ? 'internal_paste' : 'edit', label: source === 'internal_paste' ? 'Внутренняя вставка' : `Изменён ${files.find((file) => file.id === fileId)?.path}`, at: new Date().toISOString(), revision: attempt.revision + 1 }, ...items]);
    if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
    saveTimerRef.current = window.setTimeout(() => { void flushDirtyFiles().catch(() => undefined); }, 700);
  }

  function flushDirtyFiles(): Promise<number> {
    if (flushPromiseRef.current) return flushPromiseRef.current;
    const operation = (async () => {
      let currentAttempt = attemptRef.current;
      if (!currentAttempt) return 0;
      if (lmsFinalizedRef.current) return currentAttempt.acknowledgedRevision;
      while (dirtyFilesRef.current.length > 0 && !lmsFinalizedRef.current) {
        const pending = dirtyFilesRef.current[0];
        setSaveState('saving');
        try {
          const result = await api.saveFile(currentAttempt.id, pending.file, currentAttempt.acknowledgedRevision, pending.source, pending.receiptId);
          if (lmsFinalizedRef.current) return currentAttempt.acknowledgedRevision;
          if (dirtyFilesRef.current[0] === pending) dirtyFilesRef.current.shift();
          currentAttempt = { ...currentAttempt, revision: result.revision, acknowledgedRevision: result.revision };
          attemptRef.current = currentAttempt;
          setAttempt(currentAttempt);
        } catch (caught) {
          if (handleLmsFinalizedError(caught)) throw caught;
          setSaveState(navigator.onLine ? 'error' : 'offline');
          if (caught instanceof ApiError && caught.code === 'REVISION_CONFLICT') setConflictOpen(true);
          throw caught;
        }
      }
      if (!lmsFinalizedRef.current) setSaveState('saved');
      return currentAttempt.acknowledgedRevision;
    })();
    flushPromiseRef.current = operation.finally(() => { flushPromiseRef.current = null; });
    return flushPromiseRef.current;
  }

  async function ensureSaved() {
    if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
    if (!attemptRef.current) return 0;
    if (!dirtyFilesRef.current.length && !flushPromiseRef.current) return attemptRef.current.acknowledgedRevision;
    return flushDirtyFiles();
  }

  async function execute() {
    if (!attemptRef.current || lmsFinalizedRef.current || (interactiveRunRef.current && !interactiveRunRef.current.terminal)) return;
    setRunning(true); setBottomTab('output'); setBottomPanelOpen(true);
    try {
      const revision = await ensureSaved();
      const currentAttempt = attemptRef.current;
      if (!currentAttempt || lmsFinalizedRef.current) return;
      const result = await api.startInteractiveAttempt(currentAttempt.id, revision);
      window.sessionStorage.setItem(interactiveStorageKey(currentAttempt.id), result.sessionId);
      setInteractiveInput(''); setInteractiveRun(result);
      setBottomTab(result.diagnostics.length ? 'problems' : 'output');
      setHistory((items) => [{ id: createUuid(), type: 'run', label: 'Запуск программы', detail: result.status === 'COMPILE_ERROR' ? 'Ошибка компиляции' : result.terminal ? 'Выполнено' : 'Программа запущена', at: new Date().toISOString(), revision }, ...items]);
    } catch (caught) {
      if (!handleLmsFinalizedError(caught)) toast.push('error', 'Запуск не выполнен', caught instanceof Error ? caught.message : undefined);
    }
    finally { setRunning(false); }
  }

  async function sendInteractiveInput() {
    const currentAttempt = attemptRef.current;
    const active = interactiveRunRef.current;
    if (!currentAttempt || !active || active.terminal || active.inputClosed || locked || lmsFinalizedRef.current) return;
    const text = interactiveInput;
    try {
      const updated = await api.sendInteractiveAttemptInput(currentAttempt.id, active.sessionId, text);
      setInteractiveInput(''); setInteractiveRun(updated);
    } catch (caught) {
      if (!handleLmsFinalizedError(caught)) toast.push('error', 'Ввод не передан программе', caught instanceof Error ? caught.message : undefined);
    }
  }

  async function stopInteractive() {
    const currentAttempt = attemptRef.current;
    const active = interactiveRunRef.current;
    if (!currentAttempt || !active || active.terminal) return;
    setRunning(true);
    try { setInteractiveRun(await api.stopInteractiveAttempt(currentAttempt.id, active.sessionId)); }
    catch (caught) {
      if (!handleLmsFinalizedError(caught)) toast.push('error', 'Программа не остановлена', caught instanceof Error ? caught.message : undefined);
    }
    finally { setRunning(false); }
  }

  async function createFile() {
    if (!attemptRef.current || lmsFinalizedRef.current) return;
    const normalizedPath = newPath.trim();
    const pathError = validateWorkspacePath(normalizedPath);
    if (pathError) { toast.push('error', pathError); return; }
    if (attemptRef.current.fileMode === 'SINGLE' && !isTextDataPath(normalizedPath)) {
      toast.push('error', 'В однофайловом режиме можно добавлять только текстовые файлы .txt');
      return;
    }
    try {
      const revision = await ensureSaved();
      const created = await api.createFile(attemptRef.current.id, normalizedPath, revision);
      const current = { ...attemptRef.current, revision: created.revision, acknowledgedRevision: created.revision, files: [...attemptRef.current.files, created.file] };
      attemptRef.current = current; setAttempt(current); setFiles((items) => [...items, created.file]); setActiveFileId(created.file.id); setCreateOpen(false); setNewPath('');
    }
    catch (caught) {
      if (!handleLmsFinalizedError(caught)) toast.push('error', 'Файл не создан', caught instanceof Error ? caught.message : undefined);
    }
  }

  async function deleteFile() {
    const target = deleteTarget;
    const currentAttempt = attemptRef.current;
    if (!target || !currentAttempt || lmsFinalizedRef.current || !canDeleteWorkspaceFile(target, latestFiles.current, currentAttempt.fileMode)) return;
    setDeleting(true);
    try {
      const revision = await ensureSaved();
      const result = await api.deleteFile(currentAttempt.id, target.id, revision);
      const remaining = latestFiles.current.filter((file) => file.id !== result.fileId);
      latestFiles.current = remaining;
      const updatedAttempt = { ...attemptRef.current!, revision: result.revision, acknowledgedRevision: result.revision, files: remaining };
      attemptRef.current = updatedAttempt; setAttempt(updatedAttempt); setFiles(remaining);
      if (activeFileId === result.fileId) setActiveFileId(remaining[0]?.id ?? '');
      setDeleteTarget(null);
      toast.push('success', 'Файл удалён', target.path);
    } catch (caught) {
      if (!handleLmsFinalizedError(caught)) toast.push('error', 'Файл не удалён', caught instanceof Error ? caught.message : undefined);
    }
    finally { setDeleting(false); }
  }

  async function createInternalReceipt(fileId: string, text: string): Promise<string | null> {
    if (lmsFinalizedRef.current) return null;
    try {
      const revision = await ensureSaved();
      const current = attemptRef.current;
      if (!current) return null;
      return (await api.createClipboardReceipt(current.id, fileId, text, revision)).id;
    } catch (caught) {
      if (!handleLmsFinalizedError(caught)) toast.push('error', 'Фрагмент не подтверждён', caught instanceof Error ? caught.message : 'Сначала дождитесь сохранения файла.');
      return null;
    }
  }

  async function submit() {
    if (!attemptRef.current || lmsFinalizedRef.current) return;
    setSubmitting(true); setSubmissionPhase('preparing'); setSubmissionError(''); setSubmissionSlow(false);
    try {
      const active = interactiveRunRef.current;
      if (active && !active.terminal) {
        const stopped = await api.stopInteractiveAttempt(attemptRef.current.id, active.sessionId);
        setInteractiveRun(stopped);
      }
      const revision = await ensureSaved(); const current = attemptRef.current;
      if (lmsFinalizedRef.current) return;
      await api.submitAttempt(current.id, revision);
      const submitted = { ...current, status: 'SUBMITTED' as const, revision, acknowledgedRevision: revision, checkpointStatus: 'PENDING' as const };
      attemptRef.current = submitted; setAttempt(submitted); setSubmissionPhase('waiting'); setSubmissionSlow(false);
      window.sessionStorage.removeItem(interactiveStorageKey(current.id));
    } catch (caught) {
      if (!handleLmsFinalizedError(caught)) {
        const message = caught instanceof Error ? caught.message : 'Не удалось завершить работу';
        setSubmissionError(message); setSubmissionPhase('confirm');
        toast.push('error', 'Не удалось завершить работу', message);
      }
    }
    finally { setSubmitting(false); }
  }

  async function retrySubmission() {
    const current = attemptRef.current;
    if (!current || lmsFinalizedRef.current) return;
    setSubmitting(true); setSubmissionError(''); setSubmissionSlow(false);
    try {
      await api.retryAttemptSubmission(current.id);
      const queued = { ...current, checkpointStatus: 'PENDING' as const };
      attemptRef.current = queued; setAttempt(queued); setSubmissionPhase('waiting'); setSubmissionSlow(false);
    } catch (caught) {
      if (!handleLmsFinalizedError(caught)) {
        const message = caught instanceof Error ? caught.message : 'Не удалось повторить отправку';
        setSubmissionError(message); setSubmissionPhase('error');
        toast.push('error', 'Повторная отправка не запущена', message);
      }
    } finally { setSubmitting(false); }
  }

  const lmsFinalizedModal = <Modal open={lmsFinalizedOpen} title="Сеанс работы завершён через Moodle" onClose={() => navigate('/')} footer={<Button onClick={() => navigate('/')}>Вернуться к работам</Button>}><div className="conflict-copy"><AlertTriangle /><div><strong>Работа закрыта</strong><p>Ответ уже был завершён непосредственно в Moodle. Редактирование остановлено, и система не пыталась перезаписать ответ.</p></div></div></Modal>;

  if (loading) return <PageLoader label="Открываем рабочую область…" />;
  if (!attempt && lmsFinalizedOpen) return lmsFinalizedModal;
  if (error || !attempt) return <InlineError message={error ?? 'Попытка не найдена'} retry={() => void load()} />;
  return <div className="ide-page">
    <div className="ide-toolbar"><div className="ide-title">{!statementOpen && <button type="button" className="condition-toggle" aria-controls="attempt-condition" aria-expanded={false} onClick={() => setStatementOpen(true)}><PanelLeftOpen size={16} /><span>Показать условие</span></button>}<span><small>{locked ? 'Только чтение' : 'Активная попытка'}</small><strong>{attempt.title}</strong></span></div><div className="ide-status"><span className={cn('save-state', `save-state--${saveState}`)}><Cloud size={15} />{saveState === 'saved' ? `Сохранено · r${attempt.acknowledgedRevision}` : saveState === 'saving' ? 'Сохраняем…' : saveState === 'offline' ? 'Нет связи · очередь хранится в этой вкладке' : saveState === 'closed' ? 'Сеанс завершён в Moodle' : 'Ошибка сохранения'}</span><span title={attempt.deadlineAt || attempt.expectedEndAt ? 'Примерное оставшееся время по текущей сессии Moodle' : 'Примерное время с начала сессии'}><Clock3 size={16} /><strong>{attempt.deadlineAt || attempt.expectedEndAt ? remaining : `В сессии · ${remaining}`}</strong></span><span className="server-time">На устройстве: {new Intl.DateTimeFormat('ru', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(now)}</span></div><div className="ide-actions">{attempt.aiEnabled && <Button variant="ghost" onClick={() => setAiOpen(true)}><Bot size={17} /> Помощь ИИ</Button>}{interactiveActive ? <Button variant="secondary" loading={running} onClick={() => void stopInteractive()}><Square size={15} fill="currentColor" /> Остановить</Button> : <Button variant="secondary" loading={running} disabled={locked} onClick={() => void execute()}><Play size={16} fill="currentColor" /> Запустить</Button>}<Button onClick={() => setSubmitOpen(true)} disabled={locked}><CircleStop size={16} /> Завершить</Button></div></div>
    <div className={cn('ide-layout', !statementOpen && 'ide-layout--condition-hidden')}>
      {statementOpen && <aside id="attempt-condition" className="condition-panel"><header><div><span className="eyebrow">Условие</span><h2>{attempt.title}</h2></div><button type="button" className="condition-panel__toggle" aria-controls="attempt-condition" aria-expanded={true} onClick={() => setStatementOpen(false)}><PanelLeftClose size={14} /><span>Скрыть условие</span></button></header><div className="condition-body"><p>{attempt.statement || 'Текст условия пока не получен. Обновите страницу или сообщите преподавателю.'}</p><h3>Параметры рабочей области</h3><dl className="condition-facts"><div><dt>Файлы</dt><dd>{attempt.fileMode === 'MULTI' ? 'Многофайловый режим' : 'Один исходный файл'}</dd></div><div><dt>Срок</dt><dd>{attempt.deadlineAt || attempt.expectedEndAt ? `Около ${remaining}` : 'Контролируется Moodle'}</dd></div><div><dt>Помощник</dt><dd>{attempt.aiEnabled ? 'Доступен' : 'Отключён'}</dd></div></dl><div className="rules-card"><Info size={16} /><div><strong>Политика вставки</strong><p>{attempt.pastePolicy === 'STRICT' ? 'Можно вставлять только фрагменты, которые уже присутствуют в текущей рабочей области.' : 'Вставка разрешена политикой этой работы.'}</p></div></div></div></aside>}
      <section className={cn('ide-center', !bottomPanelOpen && 'ide-center--bottom-collapsed')}><div className="ide-editor"><CodeWorkspace ref={editorRef} files={files} activeFileId={activeFileId} onActiveFile={setActiveFileId} onChange={changeFile} onCreateFile={() => setCreateOpen(true)} onDeleteFile={setDeleteTarget} canDeleteFile={(file) => canDeleteWorkspaceFile(file, files, attempt.fileMode)} readOnly={locked} strictPaste={attempt.pastePolicy === 'STRICT'} scopeId={attempt.id} diagnostics={diagnostics} onPasteBlocked={() => { setHistory((items) => [{ id: createUuid(), type: 'paste_blocked', label: 'Внешняя вставка заблокирована', at: new Date().toISOString(), revision: attempt.revision }, ...items]); toast.push('info', 'Вставка запрещена', 'Разрешено вставлять только фрагменты, которые уже есть в текущей рабочей области.'); }} onInternalCopy={createInternalReceipt} /></div>
        <BottomPanel open={bottomPanelOpen} onOpenChange={setBottomPanelOpen} active={bottomTab} setActive={setBottomTab} run={interactiveRun} diagnostics={diagnostics} history={history} input={interactiveInput} setInput={setInteractiveInput} starting={running && !interactiveActive} canInput={interactiveActive && !locked && !interactiveRun?.inputClosed} onSend={() => void sendInteractiveInput()} onDiagnostic={(diagnostic) => editorRef.current?.openDiagnostic(diagnostic)} /></section>
    </div>
    <AiTutor open={aiOpen} onClose={() => setAiOpen(false)} attemptId={attempt.id} courseId={courseId} revision={attempt.acknowledgedRevision} />
    <Modal open={createOpen} title="Новый файл" onClose={() => setCreateOpen(false)} footer={<><Button variant="ghost" onClick={() => setCreateOpen(false)}>Отмена</Button><Button onClick={() => void createFile()} disabled={!newPath}><FilePlus2 size={16} /> Создать</Button></>}><Field label="Относительный путь" hint={attempt.fileMode === 'SINGLE' ? 'Можно добавить текстовый файл .txt с данными для программы' : 'Разрешены исходники, заголовки C/C++ и .txt'}><input autoFocus value={newPath} onChange={(event) => setNewPath(event.target.value)} placeholder={attempt.fileMode === 'SINGLE' ? 'input.txt' : 'src/solution.cpp'} /></Field></Modal>
    <Modal open={Boolean(deleteTarget)} title="Удалить файл?" onClose={() => !deleting && setDeleteTarget(null)} footer={<><Button variant="ghost" disabled={deleting} onClick={() => setDeleteTarget(null)}>Отмена</Button><Button variant="danger" loading={deleting} onClick={() => void deleteFile()}><Trash2 size={16} /> Удалить</Button></>}><p className="modal-copy">Файл <strong>{deleteTarget?.path}</strong> будет удалён из рабочей области отдельной серверной ревизией. Единственный исходный файл удалить нельзя.</p></Modal>
    <Modal open={submitOpen} title={submissionPhase === 'confirm' ? 'Завершить работу?' : submissionPhase === 'error' ? 'Moodle не подтвердил сдачу' : submissionSlow ? 'Сдача продолжается в фоне' : 'Передаём работу в Moodle…'} onClose={() => { if (submitting) return; if (submissionPhase === 'confirm') setSubmitOpen(false); else navigate('/'); }} footer={submissionPhase === 'confirm' ? <><Button variant="ghost" disabled={submitting} onClick={() => setSubmitOpen(false)}>Вернуться к коду</Button><Button loading={submitting} onClick={() => void submit()}>Сдать ревизию {attempt.acknowledgedRevision}</Button></> : submissionPhase === 'error' ? <><Button variant="ghost" disabled={submitting} onClick={() => navigate('/')}>Вернуться к работам</Button><Button loading={submitting} onClick={() => void retrySubmission()}>Повторить отправку</Button></> : <Button variant={submissionSlow ? 'secondary' : 'ghost'} disabled={submitting} onClick={() => navigate('/')}>{submissionSlow ? 'Вернуться к работам' : 'Продолжить в фоне'}</Button>}>
      <div className="submit-summary"><span>{submissionPhase === 'error' ? <AlertTriangle /> : submissionPhase === 'confirm' ? <CheckCircle2 /> : <Cloud />}</span><div><strong>{submissionPhase === 'confirm' ? `Финальная ревизия: ${attempt.acknowledgedRevision}` : submissionPhase === 'error' || submissionSlow ? 'Финальная версия сохранена в системе' : 'Ожидаем подтверждение Moodle'}</strong><p>{submissionPhase === 'confirm' ? (saveState === 'saved' ? 'Все изменения подтверждены сервером.' : 'Перед сдачей система дождётся сохранения изменений.') : submissionPhase === 'error' ? submissionError : submissionError || (submissionSlow ? 'Moodle отвечает дольше обычного. Можно вернуться к списку — отправка продолжится автоматически.' : 'Обычно это занимает несколько секунд. Успех будет показан только после загрузки ответа и завершения попытки в Moodle.')}</p></div></div>
      <dl className="submit-details"><div><dt>{attempt.deadlineAt || attempt.expectedEndAt ? 'Осталось времени' : 'Время в сессии'}</dt><dd>{remaining}</dd></div><div><dt>Последний запуск</dt><dd>{interactiveRun ? (interactiveRun.status === 'SUCCESS' ? 'успешный' : interactiveRun.terminal ? 'с ошибкой' : 'выполняется') : 'не запускалось'}</dd></div><div><dt>{submissionPhase === 'confirm' ? 'Последнее сохранение в Moodle' : 'Состояние Moodle'}</dt><dd>{submissionPhase === 'error' ? 'Ошибка отправки' : submissionPhase === 'confirm' ? formatDate(attempt.lastCheckpointAt) : submissionSlow ? 'Продолжается в фоне' : 'Отправляется'}</dd></div></dl>
      {submissionPhase === 'confirm' && <p className="warning-copy"><AlertTriangle size={16} /> После подтверждения редактирование будет недоступно.</p>}
    </Modal>
    <Modal open={conflictOpen} title="Конфликт ревизии" onClose={() => setConflictOpen(false)} footer={<><Button variant="secondary" onClick={() => downloadLocalCopy(files, attempt.id)}>Скачать локальную копию</Button><Button variant="danger" onClick={() => void load()}>Загрузить серверную версию</Button></>}><div className="conflict-copy"><AlertTriangle /><div><strong>Серверная рабочая область изменилась</strong><p>Автосохранение остановлено: локальные изменения остаются в этой вкладке. Скачайте их перед загрузкой серверной версии или закройте окно и скопируйте нужные фрагменты вручную.</p></div></div></Modal>
    {lmsFinalizedModal}
  </div>;
}

function BottomPanel({ open, onOpenChange, active, setActive, run, diagnostics, history, input, setInput, starting, canInput, onSend, onDiagnostic }: { open: boolean; onOpenChange(value: boolean): void; active: BottomTab; setActive(value: BottomTab): void; run: InteractiveRun | null; diagnostics: Diagnostic[]; history: HistoryEvent[]; input: string; setInput(value: string): void; starting: boolean; canInput: boolean; onSend(): void; onDiagnostic(value: Diagnostic): void }) {
  const tabs: Array<{ id: BottomTab; label: string; icon: typeof TerminalSquare; count?: number }> = [
    { id: 'output', label: 'Консоль', icon: TerminalSquare }, { id: 'problems', label: 'Проблемы', icon: AlertTriangle, count: diagnostics.length },
    { id: 'history', label: 'История', icon: History, count: history.length },
  ];
  return <section className={cn('bottom-panel', !open && 'bottom-panel--collapsed')}><header>{open ? tabs.map(({ id, label, icon: Icon, count }) => <button key={id} type="button" className={cn(active === id && 'is-active')} role="tab" aria-selected={active === id} onClick={() => setActive(id)}><Icon size={14} />{label}{count !== undefined && <em>{count}</em>}</button>) : <strong className="bottom-panel__collapsed-label"><TerminalSquare size={14} /> Консоль скрыта</strong>}<span /><button type="button" className="bottom-panel__toggle" aria-controls="student-bottom-panel" aria-expanded={open} onClick={() => onOpenChange(!open)}>{open ? <PanelBottomClose size={13} /> : <PanelBottomOpen size={13} />}{open ? 'Закрыть' : 'Открыть консоль'}</button></header>{open && <div id="student-bottom-panel" className="bottom-panel__body">
    {active === 'output' && <div className="student-console"><div className="student-console__output">{starting && <p>$ Компиляция и запуск…</p>}{run?.stdout && <pre>{run.stdout}</pre>}{run?.stderr && <pre className="terminal-error">{run.stderr}</pre>}{run?.outputTruncated && <p className="terminal-error">Вывод остановлен: достигнут установленный лимит.</p>}{!starting && !run && <p>$ Нажмите «Запустить». Если программа запросит данные, введите строку ниже и нажмите Enter.</p>}{run?.terminal && <p className={run.status === 'SUCCESS' ? 'terminal-success' : run.status === 'STOPPED' ? '' : 'terminal-error'}>{interactiveStatusLabel(run)} · {run.durationMs} мс</p>}</div><form className="student-console__form" onSubmit={(event) => { event.preventDefault(); onSend(); }}><input aria-label="Ввод программы" value={input} onChange={(event) => setInput(event.target.value)} disabled={!canInput} maxLength={65_536} autoComplete="off" spellCheck={false} placeholder={run?.terminal ? 'Программа завершена' : canInput ? 'Введите строку и нажмите Enter' : 'Сначала запустите программу'} /><button type="submit" aria-label="Передать строку программе" disabled={!canInput}><Send size={14} /> Отправить</button></form></div>}
    {active === 'problems' && <div className="problems-list">{diagnostics.length ? [...diagnostics].sort((a, b) => severityOrder(a.severity) - severityOrder(b.severity)).map((item) => <button key={item.id} onClick={() => onDiagnostic(item)}><span className={cn('problem-icon', `problem-icon--${item.severity}`)}>{item.severity === 'error' ? '×' : '!'}</span><span><strong>{item.message}</strong><small>{item.path ?? 'Сборка'}{item.line ? `:${item.line}:${item.column ?? 1}` : ''}{item.code ? ` · ${item.code}` : ''}</small>{item.notes?.map((note) => <em key={note}>{note}</em>)}</span><ChevronDown size={15} /></button>) : <div className="panel-empty"><CheckCircle2 size={20} /><span><strong>Проблем не найдено</strong><small>Запустите сборку для обновления диагностики.</small></span></div>}</div>}
    {active === 'history' && <div className="history-list">{history.slice(0, 15).map((item) => <div key={item.id}><span><FileClock size={14} /></span><p><strong>{item.label}</strong><small>{[item.detail ?? `Ревизия ${item.revision}`, formatClientContext(item.client), formatDate(item.at)].filter(Boolean).join(' · ')}</small></p></div>)}</div>}
  </div>}</section>;
}

function AiTutor({ open, onClose, attemptId, courseId, revision }: { open: boolean; onClose(): void; attemptId: string; courseId: string; revision: number }) {
  const [message, setMessage] = useState('');
  const [sending, setSending] = useState(false);
  const threadIdRef = useRef<string>();
  const [messages, setMessages] = useState<Array<{ from: 'ai' | 'user'; text: string; citations?: Array<{ title: string; url: string }> }>>([{ from: 'ai', text: 'Я помогу разобраться с концепцией или ошибкой, но не напишу готовое решение. Что сейчас вызывает затруднение?' }]);
  async function sendMessage() {
    const content = message.trim();
    if (!content || sending) return;
    setMessage(''); setMessages((items) => [...items, { from: 'user', text: content }]); setSending(true);
    try {
      if (!threadIdRef.current) threadIdRef.current = (await api.createStudentAiThread(attemptId, courseId, revision)).id;
      const response = await api.sendAiMessage(threadIdRef.current, content);
      setMessages((items) => [...items, { from: 'ai', text: response.content, citations: response.citations }]);
    } catch (caught) {
      setMessages((items) => [...items, { from: 'ai', text: caught instanceof Error ? `Помощник сейчас недоступен: ${caught.message}` : 'Помощник сейчас недоступен.' }]);
    } finally { setSending(false); }
  }
  if (!open) return null;
  return <aside className="ai-drawer"><header><span><Bot /><span><strong>Учебный помощник</strong><small>Привязан к ревизии {revision}</small></span></span><button onClick={onClose}><X /></button></header><div className="ai-policy"><ShieldAlertIcon /><p>Помощник объясняет подход и ссылается на документацию. Готовый код не выдаётся.</p></div><div className="ai-messages">{messages.map((item, index) => <div className={cn('ai-message', item.from === 'user' && 'ai-message--user')} key={index}>{item.text}{item.citations?.map((citation) => <a key={citation.url} href={citation.url} target="_blank" rel="noreferrer">{citation.title}</a>)}</div>)}{sending && <div className="ai-message ai-message--thinking">Помощник анализирует вопрос…</div>}</div><form onSubmit={(event) => { event.preventDefault(); void sendMessage(); }}><textarea value={message} disabled={sending} onChange={(event) => setMessage(event.target.value)} placeholder="Спросить о концепции или ошибке…" /><Button size="icon" loading={sending} disabled={!message.trim()} aria-label="Отправить"><Send size={17} /></Button></form><small className="ai-disclaimer">Ответы ИИ могут содержать ошибки — проверяйте документацию.</small></aside>;
}

function ShieldAlertIcon() { return <MessageCircleQuestion size={17} />; }

function downloadLocalCopy(files: WorkspaceFile[], attemptId: string) {
  const payload = JSON.stringify({ attempt_id: attemptId, exported_at: new Date().toISOString(), files: files.map(({ path, content }) => ({ path, content })) }, null, 2);
  const url = URL.createObjectURL(new Blob([payload], { type: 'application/json' }));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = `eduprog-${attemptId}-local-copy.json`; anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function interactiveStorageKey(attemptId: string): string {
  return `eduprog:interactive-attempt:${attemptId}`;
}

function interactiveStatusLabel(run: InteractiveRun): string {
  const labels: Record<InteractiveRun['status'], string> = {
    RUNNING: 'Программа выполняется',
    SUCCESS: `Процесс завершён с кодом ${run.exitCode ?? 0}`,
    COMPILE_ERROR: 'Ошибка компиляции',
    RUNTIME_ERROR: 'Ошибка выполнения',
    TIME_LIMIT: 'Превышено время выполнения',
    MEMORY_LIMIT: 'Превышен лимит памяти',
    OUTPUT_LIMIT: 'Превышен лимит вывода',
    WORKSPACE_LIMIT: 'Превышен лимит рабочей области',
    STOPPED: 'Программа остановлена',
    INFRA_ERROR: 'Сервис компиляции недоступен',
  };
  return labels[run.status];
}
