import {
  AlertTriangle, Bot, CheckCircle2, ChevronDown, CircleStop, Clock3, Cloud,
  FileClock, FilePlus2, History, Info, MessageCircleQuestion, PanelBottomClose, PanelBottomOpen,
  PanelLeftClose, PanelLeftOpen, Play, Send, Square, TerminalSquare, Trash2, X,
} from 'lucide-react';
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { CodeWorkspace, type CodeWorkspaceHandle } from '../components/CodeWorkspace';
import { Badge, Button, Field, InlineError, Modal, PageLoader, useToast } from '../components/ui';
import { api, ApiError } from '../lib/api';
import { createServerClock } from '../lib/serverClock';
import { createEditorClipboardSession, type EditorClipboardSession } from '../lib/editorClipboard';
import { cn, findWorkspacePasteSource, formatClientContext, formatDate, formatRemaining, formatSessionElapsed, isTextDataPath, isTranslationUnitPath, severityOrder, validateWorkspacePath } from '../lib/utils';
import { createUuid } from '../lib/uuid';
import type { Attempt, Diagnostic, HistoryEvent, InternalPasteRange, InteractiveRun, WorkspaceFile } from '../types';

type BottomTab = 'output' | 'problems' | 'history';
type SubmissionPhase = 'confirm' | 'preparing' | 'waiting' | 'error';
type LmsClosureReason = 'LMS_ATTEMPT_FINALIZED' | 'LMS_ATTEMPT_DELETED';
const SUBMISSION_SLOW_AFTER_MS = 12_000;

function isLmsClosureReason(reason?: string): reason is LmsClosureReason {
  return reason === 'LMS_ATTEMPT_FINALIZED' || reason === 'LMS_ATTEMPT_DELETED';
}

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
  const [clipboardSession] = useState(createEditorClipboardSession);
  // Each Moodle question owns an independent workspace and async lifecycle.
  // A late response from the previous question must never replace the new one.
  return <AttemptWorkspace key={attemptId} attemptId={attemptId} clipboardSession={clipboardSession} />;
}

function AttemptWorkspace({ attemptId, clipboardSession }: { attemptId: string; clipboardSession: EditorClipboardSession }) {
  const navigate = useNavigate();
  const toast = useToast();
  const editorRef = useRef<CodeWorkspaceHandle>(null);
  const saveTimerRef = useRef<number>();
  const attemptRef = useRef<Attempt | null>(null);
  const dirtyFilesRef = useRef<Array<{ file: WorkspaceFile; source: 'typing' | 'internal_paste'; receiptId?: string; pasteRange?: InternalPasteRange }>>([]);
  const flushPromiseRef = useRef<Promise<number> | null>(null);
  const mountedRef = useRef(true);
  const loadGenerationRef = useRef(0);
  const switchingQuestionRef = useRef(false);
  const submittingRef = useRef(false);
  const [switchingQuestionId, setSwitchingQuestionId] = useState<string | null>(null);
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [files, setFiles] = useState<WorkspaceFile[]>([]);
  const [activeFileId, setActiveFileId] = useState('');
  const [history, setHistory] = useState<HistoryEvent[]>([]);
  const [interactiveRun, setInteractiveRun] = useState<InteractiveRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [metadataError, setMetadataError] = useState(false);
  const [metadataLoading, setMetadataLoading] = useState(false);
  const [saveState, setSaveState] = useState<'saved' | 'saving' | 'offline' | 'error' | 'closed'>('saved');
  const [bottomTab, setBottomTab] = useState<BottomTab>('problems');
  const [running, setRunning] = useState(false);
  const [interactiveInput, setInteractiveInput] = useState('');
  const [remaining, setRemaining] = useState('');
  const serverClock = useRef(createServerClock());
  const [now, setNow] = useState<number | null>(null);
  const [statementOpen, setStatementOpen] = useState(true);
  const [filesPanelOpen, setFilesPanelOpen] = useState(true);
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
  const [lmsClosureReason, setLmsClosureReason] = useState<LmsClosureReason>('LMS_ATTEMPT_FINALIZED');
  const latestFiles = useRef(files);
  const interactiveRunRef = useRef<InteractiveRun | null>(null);
  const lmsFinalizedRef = useRef(false);
  latestFiles.current = files;
  attemptRef.current = attempt;
  interactiveRunRef.current = interactiveRun;

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const closeForLmsFinalization = useCallback((reportedAttempt?: Attempt, reason: LmsClosureReason = 'LMS_ATTEMPT_FINALIZED') => {
    if (lmsFinalizedRef.current && reason !== 'LMS_ATTEMPT_DELETED') {
      setLmsFinalizedOpen(true);
      return;
    }
    lmsFinalizedRef.current = true;
    setLmsClosureReason(reason);
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
        closureReason: reason,
        aiEnabled: false,
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
    if (caught instanceof ApiError && isLmsClosureReason(caught.code)) {
      closeForLmsFinalization(undefined, caught.code);
      return true;
    }
    return false;
  }, [closeForLmsFinalization]);

  const loadMetadata = useCallback(async (loaded: Attempt, generation: number) => {
    setMetadataLoading(true);
    const [loadedHistory, resolvedCourse] = await Promise.allSettled([
      api.getHistory(loaded.id), api.resolveCourseId(loaded.assessmentId),
    ]);
    if (!mountedRef.current || generation !== loadGenerationRef.current) return;
    if (loadedHistory.status === 'fulfilled') {
      // Editing may have begun while history was loading. Keep those local
      // events instead of replacing them with the older server response.
      setHistory((current) => {
        const ids = new Set(current.map((item) => item.id));
        return [...current, ...loadedHistory.value.filter((item) => !ids.has(item.id))];
      });
    }
    if (resolvedCourse.status === 'fulfilled') setCourseId(resolvedCourse.value);
    setMetadataError(loadedHistory.status === 'rejected' || resolvedCourse.status === 'rejected');
    setMetadataLoading(false);
  }, []);

  const load = useCallback(async () => {
    const generation = ++loadGenerationRef.current;
    const isCurrent = () => mountedRef.current && loadGenerationRef.current === generation;
    if (attemptRef.current?.id !== attemptId) {
      lmsFinalizedRef.current = false;
      setLmsFinalizedOpen(false);
      setLmsClosureReason('LMS_ATTEMPT_FINALIZED');
    }
    setLoading(true); setError(null);
    try {
      let loaded = await api.getAttempt(attemptId);
      if (!isCurrent() || lmsFinalizedRef.current) return;
      if (loaded.requiresLiveLmsPreparation) {
        if (loaded.quizSession) {
          throw new ApiError(409, 'MOODLE_RUNTIME_PREPARATION_REQUIRED', 'Не удалось открыть подготовленную задачу Moodle. Обновите страницу.');
        }
        const prepared = await api.startAttempt(loaded.assessmentId);
        if (!isCurrent() || lmsFinalizedRef.current) return;
        if (prepared.id !== attemptId) {
          navigate(`/ide/${prepared.id}`, { replace: true });
          return;
        }
        loaded = await api.getAttempt(attemptId);
        if (!isCurrent() || lmsFinalizedRef.current) return;
        if (loaded.requiresLiveLmsPreparation) {
          throw new ApiError(
            409,
            'MOODLE_RUNTIME_PREPARATION_REQUIRED',
            'Moodle preparation did not bind the active attempt',
          );
        }
      }
      dirtyFilesRef.current = []; setConflictOpen(false);
      serverClock.current.sync(loaded.serverNow, loaded.serverTimeReceivedAt);
      setNow(serverClock.current.now());
      attemptRef.current = loaded; setAttempt(loaded); setCourseId(''); setFiles(loaded.files); setActiveFileId(restoredActiveFile(loaded)); setHistory([]); setSaveState('saved');
      setMetadataError(false);
      // A slow or failed history/course lookup must not block the student's
      // code, autosave or final submission. These are ancillary to the workspace.
      void loadMetadata(loaded, generation);
      setInteractiveRun(null); setInteractiveInput('');
      if (isLmsClosureReason(loaded.closureReason)) closeForLmsFinalization(loaded, loaded.closureReason);
      else if (loaded.status === 'SUBMITTED' && loaded.checkpointStatus !== 'SYNCED') {
        setSubmissionPhase(loaded.checkpointStatus === 'ERROR' ? 'error' : 'waiting');
        setSubmissionError(loaded.checkpointStatus === 'ERROR' ? 'Moodle не подтвердил получение ответа.' : '');
        setSubmissionSlow(false);
        setSubmitOpen(true);
      } else {
        setSubmissionPhase('confirm'); setSubmissionError(''); setSubmissionSlow(false);
      }
      const storedSession = window.sessionStorage.getItem(interactiveStorageKey(loaded.id));
      if (storedSession && !isLmsClosureReason(loaded.closureReason)) {
        try {
          const recovered = await api.getInteractiveAttempt(loaded.id, storedSession);
          if (!isCurrent() || lmsFinalizedRef.current) return;
          setInteractiveRun(recovered); setBottomTab('output'); setBottomPanelOpen(true);
        } catch {
          window.sessionStorage.removeItem(interactiveStorageKey(loaded.id));
        }
      }
    } catch (caught) {
      if (!isCurrent()) return;
      if (!handleLmsFinalizedError(caught)) setError(caught instanceof Error ? caught.message : 'Не удалось открыть попытку');
    }
    finally { if (isCurrent()) setLoading(false); }
  }, [attemptId, closeForLmsFinalization, handleLmsFinalizedError, loadMetadata, navigate]);
  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!attempt || !files.some((file) => file.id === activeFileId)) return;
    try { window.sessionStorage.setItem(activeFileStorageKey(attempt.id), activeFileId); }
    catch { /* Remembering a selection must not block editing or saving. */ }
  }, [attempt?.id, activeFileId, files]);

  useEffect(() => {
    if (!attempt?.id || attempt.status !== 'ACTIVE' || lmsFinalizedRef.current) return;
    let cancelled = false;
    let polling = false;
    const poll = async () => {
      if (polling || cancelled || lmsFinalizedRef.current) return;
      polling = true;
      try {
        const current = await api.getAttemptStatus(attempt.id);
        if (cancelled || lmsFinalizedRef.current) return;
        serverClock.current.sync(current.serverNow, current.serverTimeReceivedAt);
        setNow(serverClock.current.now());
        if (isLmsClosureReason(current.closureReason)) {
          closeForLmsFinalization(undefined, current.closureReason);
          return;
        }
        const value = attemptRef.current;
        // A request begun before submission can return ACTIVE afterwards.
        // State transitions are monotonic; that stale read must not reopen code.
        if (!value || value.id !== current.id || (value.status !== 'ACTIVE' && current.status === 'ACTIVE')) return;
        const updated = {
          ...value,
          status: current.status,
          closureReason: current.closureReason,
          closedAt: current.closedAt,
          lastCheckpointAt: current.lastCheckpointAt,
          checkpointStatus: current.checkpointStatus,
          aiEnabled: current.aiEnabled ?? value.aiEnabled,
          deadlineAt: current.deadlineAt ?? value.deadlineAt,
          expectedEndAt: current.expectedEndAt ?? value.expectedEndAt,
          moodleSyncTimeoutSeconds: current.moodleSyncTimeoutSeconds ?? value.moodleSyncTimeoutSeconds,
        };
        attemptRef.current = updated;
        setAttempt(updated);
        if (current.aiEnabled === false) setAiOpen(false);
        if (current.status === 'SUBMITTED') {
          // The deadline worker submits every solution even without an open
          // tab. Enter the same receipt flow as a manual submission, and never
          // treat an earlier periodic checkpoint as proof of final delivery.
          setAiOpen(false);
          setSubmissionPhase(current.checkpointStatus === 'ERROR' ? 'error' : 'waiting');
          setSubmissionError(current.checkpointStatus === 'ERROR' ? 'Moodle не подтвердил получение ответа.' : '');
          setSubmissionSlow(false);
          setSubmitOpen(true);
        }
      } catch (caught) {
        if (!cancelled) handleLmsFinalizedError(caught);
      } finally {
        polling = false;
      }
    };
    void poll();
    const timer = window.setInterval(() => { void poll(); }, 5_000);
    const refreshTime = () => { if (!document.hidden) void poll(); };
    document.addEventListener('visibilitychange', refreshTime);
    window.addEventListener('pageshow', refreshTime);
    return () => {
      cancelled = true; window.clearInterval(timer);
      document.removeEventListener('visibilitychange', refreshTime);
      window.removeEventListener('pageshow', refreshTime);
    };
  }, [attempt?.id, attempt?.status, closeForLmsFinalization, handleLmsFinalizedError]);

  useEffect(() => {
    if (!attempt?.id || attempt.status !== 'SUBMITTED' || !['waiting', 'error'].includes(submissionPhase)) return;
    let cancelled = false;
    let polling = false;
    const poll = async () => {
      if (cancelled || polling) return;
      polling = true;
      try {
        const current = await api.getAttemptStatus(attempt.id);
        if (cancelled || lmsFinalizedRef.current) return;
        if (isLmsClosureReason(current.closureReason)) {
          closeForLmsFinalization(undefined, current.closureReason);
          return;
        }
        const value = attemptRef.current;
        if (!value || value.id !== current.id || current.status === 'ACTIVE') return;
        const updated = {
          ...value,
          status: current.status,
          closureReason: current.closureReason,
          closedAt: current.closedAt,
          lastCheckpointAt: current.lastCheckpointAt,
          checkpointStatus: current.checkpointStatus,
        };
        attemptRef.current = updated;
        setAttempt(updated);
        if (current.status === 'SUBMITTED' && current.checkpointStatus === 'SYNCED') {
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
      const value = serverClock.current.now();
      setNow(value);
      if (value === null) { setRemaining('Уточняем время…'); return; }
      const visibleEnd = attempt.deadlineAt ?? attempt.expectedEndAt;
      setRemaining(visibleEnd
        ? formatRemaining(visibleEnd, value)
        : attempt.hasTimeLimit === false ? '' : formatSessionElapsed(attempt.startedAt, value));
      const timeLeft = attempt.deadlineAt ? new Date(attempt.deadlineAt).getTime() - value : Infinity;
      if (attempt.status === 'ACTIVE' && timeLeft > 0 && timeLeft <= 3_000 && dirtyFilesRef.current.length) {
        if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
        void flushDirtyFiles().catch(() => undefined);
      }
    };
    update(); const timer = window.setInterval(update, 1000); return () => window.clearInterval(timer);
  }, [attempt]);

  useEffect(() => {
    const save = () => { if (dirtyFilesRef.current.length) void flushDirtyFiles().catch(() => undefined); };
    const onHidden = () => { if (document.visibilityState === 'hidden') save(); };
    document.addEventListener('visibilitychange', onHidden);
    return () => {
      document.removeEventListener('visibilitychange', onHidden);
      if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
      // In-app navigation must not discard edits waiting for the debounce.
      save();
    };
  }, []);
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
        if (!cancelled && !interactiveRunRef.current?.terminal) setInteractiveRun(current);
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
    const warn = (event: BeforeUnloadEvent) => { if (dirtyFilesRef.current.length) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);
  }, []);
  const locked = !attempt || attempt.status !== 'ACTIVE' || Boolean(now !== null && attempt.deadlineAt && new Date(attempt.deadlineAt).getTime() <= now);
  const editorReadOnly = locked || Boolean(switchingQuestionId) || submitting;
  const untimed = attempt?.hasTimeLimit === false && !attempt.deadlineAt && !attempt.expectedEndAt;
  const quizQuestions = attempt?.quizSession?.questions ?? [];
  const multiQuestion = quizQuestions.length > 1;
  const diagnostics = interactiveRun?.diagnostics ?? [];
  const interactiveActive = Boolean(interactiveRun && !interactiveRun.terminal);

  function changeFile(fileId: string, content: string, source: 'typing' | 'internal_paste', receiptId?: string, pasteRange?: InternalPasteRange) {
    if (!mountedRef.current || locked || !attempt || lmsFinalizedRef.current || switchingQuestionRef.current || submittingRef.current) return;
    const updated = latestFiles.current.map((file) => file.id === fileId ? { ...file, content } : file);
    const changedFile = updated.find((file) => file.id === fileId);
    if (!changedFile) return;
    latestFiles.current = updated; setFiles(updated); setSaveState('saving');
    const last = dirtyFilesRef.current.at(-1);
    const entry = { file: changedFile, source, receiptId, pasteRange };
    if (source === 'typing' && last?.source === 'typing' && last.file.id === fileId) dirtyFilesRef.current[dirtyFilesRef.current.length - 1] = entry;
    else dirtyFilesRef.current.push(entry);
    setHistory((items) => [{ id: createUuid(), type: source === 'internal_paste' ? 'internal_paste' : 'edit', label: source === 'internal_paste' ? 'Внутренняя вставка' : `Изменён ${files.find((file) => file.id === fileId)?.path}`, at: new Date().toISOString(), revision: attempt.revision + 1 }, ...items]);
    if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
    const serverNow = serverClock.current.now();
    const nearDeadline = Boolean(serverNow !== null && attempt.deadlineAt && new Date(attempt.deadlineAt).getTime() - serverNow <= 3_000);
    saveTimerRef.current = window.setTimeout(() => { void flushDirtyFiles().catch(() => undefined); }, nearDeadline ? 0 : 700);
  }

  function flushDirtyFiles(): Promise<number> {
    if (flushPromiseRef.current) return flushPromiseRef.current;
    const operation = (async () => {
      let currentAttempt = attemptRef.current;
      if (!currentAttempt) return 0;
      if (lmsFinalizedRef.current) return currentAttempt.acknowledgedRevision;
      while (dirtyFilesRef.current.length > 0 && !lmsFinalizedRef.current) {
        if (currentAttempt.status !== 'ACTIVE') {
          setSaveState('error');
          throw new ApiError(409, 'ATTEMPT_READ_ONLY', 'Эта попытка уже завершена. Последние несохранённые правки остаются в редакторе.');
        }
        const pending = dirtyFilesRef.current[0];
        setSaveState('saving');
        try {
          const result = await api.saveFile(currentAttempt.id, pending.file, currentAttempt.acknowledgedRevision, pending.source, pending.receiptId, pending.pasteRange);
          if (lmsFinalizedRef.current) return currentAttempt.acknowledgedRevision;
          if (dirtyFilesRef.current[0] === pending) dirtyFilesRef.current.shift();
          // Status polling may have observed the deadline/final submission
          // while this save was in flight. Preserve its newer state.
          currentAttempt = { ...(attemptRef.current ?? currentAttempt), revision: result.revision, acknowledgedRevision: result.revision };
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

  async function prepareAiContext(expectedAttemptId: string) {
    const checkContext = () => {
      if (!mountedRef.current || attemptRef.current?.id !== expectedAttemptId || switchingQuestionRef.current || lmsFinalizedRef.current) {
        throw new Error('Открытая задача изменилась. Задайте вопрос в чате нужной задачи.');
      }
      if (!courseId) throw new Error('Данные курса для чата ещё не загружены. Дождитесь загрузки или повторите её над редактором.');
    };
    checkContext();
    const revision = await ensureSaved();
    checkContext();
    return revision;
  }

  async function switchQuestion(targetAttemptId: string) {
    const current = attemptRef.current;
    if (!current || targetAttemptId === current.id || switchingQuestionRef.current || running || submitting || lmsFinalizedRef.current) return;
    if (!current.quizSession?.questions.some((question) => question.attemptId === targetAttemptId)) return;
    switchingQuestionRef.current = true;
    setSwitchingQuestionId(targetAttemptId);
    try {
      // CodeWorkspace emits edits synchronously. Drain the whole autosave queue
      // before disposing its model, including an already in-flight save.
      await ensureSaved();
      if (!mountedRef.current || lmsFinalizedRef.current) return;
      const active = interactiveRunRef.current;
      if (active && !active.terminal) {
        const stopped = await api.stopInteractiveAttempt(current.id, active.sessionId);
        if (!mountedRef.current || lmsFinalizedRef.current) return;
        if (!stopped.terminal) throw new Error('Программа ещё останавливается. Повторите переключение через несколько секунд.');
        interactiveRunRef.current = stopped;
        setInteractiveRun(stopped);
      }
      // Navigation never starts another Moodle attempt: siblings already exist.
      navigate(`/ide/${targetAttemptId}`, { replace: true });
    } catch (caught) {
      if (mountedRef.current && !handleLmsFinalizedError(caught)) {
        toast.push('error', 'Не удалось переключить задачу', 'Текущий код остаётся в редакторе. ' + (caught instanceof Error ? caught.message : 'Дождитесь сохранения и повторите переключение.'));
      }
    } finally {
      if (mountedRef.current) {
        switchingQuestionRef.current = false;
        setSwitchingQuestionId(null);
      }
    }
  }

  async function execute() {
    if (!attemptRef.current || switchingQuestionRef.current || lmsFinalizedRef.current || (interactiveRunRef.current && !interactiveRunRef.current.terminal)) return;
    setRunning(true); setBottomTab('output'); setBottomPanelOpen(true);
    try {
      const revision = await ensureSaved();
      const currentAttempt = attemptRef.current;
      if (!currentAttempt || !mountedRef.current || lmsFinalizedRef.current) return;
      const result = await api.startInteractiveAttempt(currentAttempt.id, revision);
      if (!mountedRef.current || lmsFinalizedRef.current) {
        if (!result.terminal) void api.stopInteractiveAttempt(currentAttempt.id, result.sessionId).catch(() => undefined);
        return;
      }
      window.sessionStorage.setItem(interactiveStorageKey(currentAttempt.id), result.sessionId);
      setInteractiveInput(''); setInteractiveRun(result);
      setBottomTab(result.diagnostics.length ? 'problems' : 'output');
      setHistory((items) => [{ id: createUuid(), type: 'run', label: 'Запуск программы', detail: result.status === 'COMPILE_ERROR' ? 'Ошибка компиляции' : result.terminal ? 'Выполнено' : 'Программа запущена', at: new Date().toISOString(), revision }, ...items]);
    } catch (caught) {
      if (mountedRef.current && !handleLmsFinalizedError(caught)) toast.push('error', 'Запуск не выполнен', caught instanceof Error ? caught.message : undefined);
    }
    finally { if (mountedRef.current) setRunning(false); }
  }

  async function sendInteractiveInput() {
    const currentAttempt = attemptRef.current;
    const active = interactiveRunRef.current;
    if (!currentAttempt || !active || active.terminal || active.inputClosed || locked || lmsFinalizedRef.current) return;
    const text = interactiveInput;
    try {
      const updated = await api.sendInteractiveAttemptInput(currentAttempt.id, active.sessionId, text);
      if (!mountedRef.current || lmsFinalizedRef.current) return;
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

  async function createInternalReceipt(fileId: string, text: string, sourceAttemptId?: string): Promise<string | null> {
    if (lmsFinalizedRef.current) return null;
    try {
      const revision = await ensureSaved();
      const current = attemptRef.current;
      if (!current) return null;
      if (sourceAttemptId && sourceAttemptId !== current.id) {
        // A previous paste may be in another question. Switching flushed its
        // edits; read that source's own revision, never the destination revision.
        const quiz = current.quizSession;
        if (!quiz?.questions.some((question) => question.attemptId === sourceAttemptId)) return null;
        const source = await api.getAttempt(sourceAttemptId);
        if (source.quizSession?.rootAttemptId !== quiz.rootAttemptId) return null;
        const sourceFile = findWorkspacePasteSource(source.files, text);
        if (!sourceFile) return null;
        return (await api.createClipboardReceipt(source.id, sourceFile.id, text, source.acknowledgedRevision)).id;
      }
      return (await api.createClipboardReceipt(current.id, fileId, text, revision)).id;
    } catch (caught) {
      if (!handleLmsFinalizedError(caught)) toast.push('error', 'Фрагмент не подтверждён', caught instanceof Error ? caught.message : 'Сначала дождитесь сохранения файла.');
      return null;
    }
  }

  async function submit() {
    if (!attemptRef.current || submittingRef.current || switchingQuestionRef.current || lmsFinalizedRef.current) return;
    submittingRef.current = true;
    setSubmitting(true); setSubmissionPhase('preparing'); setSubmissionError(''); setSubmissionSlow(false);
    try {
      const active = interactiveRunRef.current;
      if (active && !active.terminal) {
        const stopped = await api.stopInteractiveAttempt(attemptRef.current.id, active.sessionId);
        setInteractiveRun(stopped);
      }
      const revision = await ensureSaved(); const current = attemptRef.current;
      if (!mountedRef.current || lmsFinalizedRef.current) return;
      await api.submitAttempt(current.id, revision);
      if (!mountedRef.current || lmsFinalizedRef.current) return;
      const submitted = { ...current, status: 'SUBMITTED' as const, revision, acknowledgedRevision: revision, checkpointStatus: 'PENDING' as const };
      attemptRef.current = submitted; setAttempt(submitted); setSubmissionPhase('waiting'); setSubmissionSlow(false);
      window.sessionStorage.removeItem(interactiveStorageKey(current.id));
    } catch (caught) {
      if (!mountedRef.current) return;
      if (!handleLmsFinalizedError(caught)) {
        // A lost POST response is not proof of failure: the independent status
        // poll can already have confirmed that the server accepted the answer.
        if (attemptRef.current?.status === 'SUBMITTED') {
          setSubmissionPhase('waiting');
          return;
        }
        const message = caught instanceof Error ? caught.message : 'Не удалось завершить работу';
        setSubmissionError(message); setSubmissionPhase('confirm');
        toast.push('error', 'Не удалось завершить работу', message);
      }
    }
    finally { submittingRef.current = false; if (mountedRef.current) setSubmitting(false); }
  }

  async function retrySubmission() {
    const current = attemptRef.current;
    if (!current || submittingRef.current || lmsFinalizedRef.current) return;
    submittingRef.current = true;
    setSubmitting(true); setSubmissionError(''); setSubmissionSlow(false);
    try {
      await api.retryAttemptSubmission(current.id);
      if (!mountedRef.current || lmsFinalizedRef.current) return;
      const queued = { ...current, checkpointStatus: 'PENDING' as const };
      attemptRef.current = queued; setAttempt(queued); setSubmissionPhase('waiting'); setSubmissionSlow(false);
    } catch (caught) {
      if (!handleLmsFinalizedError(caught)) {
        const message = caught instanceof Error ? caught.message : 'Не удалось повторить отправку';
        setSubmissionError(message); setSubmissionPhase('error');
        toast.push('error', 'Повторная отправка не запущена', message);
      }
    } finally { submittingRef.current = false; if (mountedRef.current) setSubmitting(false); }
  }

  const lmsDeleted = lmsClosureReason === 'LMS_ATTEMPT_DELETED';
  const lmsFinalizedModal = <Modal open={lmsFinalizedOpen} title={lmsDeleted ? 'Попытка удалена в Moodle' : 'Сеанс работы завершён через Moodle'} onClose={() => navigate('/')} footer={<Button onClick={() => navigate('/')}>Вернуться к работам</Button>}><div className="conflict-copy"><AlertTriangle /><div><strong>{lmsDeleted ? 'Эта попытка больше недоступна' : 'Работа закрыта'}</strong><p>{lmsDeleted ? 'Moodle подтвердил удаление попытки. Редактирование и повторная отправка остановлены. Сохранённый код остался в системе, но эта попытка больше не показывается среди сданных работ. Вернитесь к списку работ, чтобы открыть доступную попытку.' : 'Ответ уже был завершён непосредственно в Moodle. Редактирование остановлено, и система не пыталась перезаписать ответ.'}</p></div></div></Modal>;

  if (loading) return <PageLoader label="Открываем рабочую область…" />;
  if (!attempt && lmsFinalizedOpen) return lmsFinalizedModal;
  if (error || !attempt) return <InlineError message={error ?? 'Попытка не найдена'} retry={() => void load()} />;
  return <div className="ide-page">
    {metadataError && <div role="alert">Не удалось загрузить историю или данные курса. Код доступен для редактирования и сдачи. <Button variant="ghost" loading={metadataLoading} onClick={() => void loadMetadata(attempt, loadGenerationRef.current)}>Повторить загрузку дополнительных данных</Button></div>}
    <div className="ide-toolbar"><div className="ide-title"><span><small>{locked ? 'Только чтение' : 'Активная попытка'}</small><strong>{attempt.title}</strong></span></div><div className="ide-status"><span className={cn('save-state', `save-state--${saveState}`)}><Cloud size={15} />{saveState === 'saved' ? `Сохранено · r${attempt.acknowledgedRevision}` : saveState === 'saving' ? 'Сохраняем…' : saveState === 'offline' ? 'Нет связи · очередь хранится в этой вкладке' : saveState === 'closed' ? 'Сеанс завершён в Moodle' : 'Ошибка сохранения'}</span>{untimed ? <span>Без таймера</span> : <span title={attempt.deadlineAt || attempt.expectedEndAt ? 'Примерное оставшееся время по текущей сессии Moodle' : 'Примерное время с начала сессии'}><Clock3 size={16} /><strong>{attempt.deadlineAt || attempt.expectedEndAt ? remaining : `В сессии · ${remaining}`}</strong></span>}{now !== null && <span className="server-time">Время сервера: {new Intl.DateTimeFormat('ru', { timeZone: 'UTC', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(now)} UTC</span>}</div><div className="ide-actions">{attempt.aiEnabled && <Button variant="ghost" disabled={Boolean(switchingQuestionId)} onClick={() => setAiOpen(true)}><Bot size={17} /> Помощь ИИ</Button>}<Button onClick={() => setSubmitOpen(true)} disabled={editorReadOnly || running}><CircleStop size={16} /> Завершить работу</Button></div></div>
    {multiQuestion && <nav className="ide-question-switcher" aria-label="Задачи работы" aria-busy={Boolean(switchingQuestionId)}>
      <span className="ide-question-switcher__label">{switchingQuestionId ? 'Сохраняем и переключаем…' : 'Задачи работы'}</span>
      <div className="ide-question-switcher__tabs" role="tablist" aria-label="Выбор задачи">
        {quizQuestions.map((question) => <button key={question.attemptId} type="button" role="tab" id={`quiz-question-${question.attemptId}`} aria-selected={question.attemptId === attempt.id} aria-controls="quiz-task-panel" className={cn('ide-question-switcher__tab', question.attemptId === attempt.id && 'is-active')} disabled={Boolean(switchingQuestionId) || running || submitting || submitOpen || lmsFinalizedOpen} onClick={() => void switchQuestion(question.attemptId)} title={question.title}>
          <strong>Задача {question.position}</strong>{question.title.trim() && !/^(?:Задание|Задача)\s*№?\s*\d+$/iu.test(question.title.trim()) && <span>{question.title}</span>}
        </button>)}
      </div>
    </nav>}
    <div id={multiQuestion ? 'quiz-task-panel' : undefined} role={multiQuestion ? 'tabpanel' : undefined} aria-labelledby={multiQuestion ? `quiz-question-${attempt.id}` : undefined} className={cn('ide-layout', !statementOpen && 'ide-layout--condition-hidden')}>
      <aside className={cn('condition-panel', !statementOpen && 'condition-panel--collapsed')}>
        <header>
          <button type="button" className="condition-panel__toggle" title={statementOpen ? 'Скрыть условие' : 'Показать условие'} aria-label={statementOpen ? 'Скрыть условие' : 'Показать условие'} aria-controls="attempt-condition" aria-expanded={statementOpen} onClick={() => setStatementOpen((value) => !value)}>{statementOpen ? <PanelLeftClose size={18} /> : <PanelLeftOpen size={18} />}</button>
          {statementOpen && <div><span className="eyebrow">Условие</span><h2>{attempt.title}</h2></div>}
        </header>
        {!statementOpen && <span className="condition-panel__rail-label" aria-hidden="true">Задание</span>}
        <div id="attempt-condition" className="condition-body" hidden={!statementOpen}><p className="condition-statement">{attempt.statement || 'Текст условия пока не получен. Обновите страницу или сообщите преподавателю.'}</p><h3>Параметры рабочей области</h3><dl className="condition-facts"><div><dt>Файлы</dt><dd>{attempt.fileMode === 'MULTI' ? 'Многофайловый режим' : 'Один исходный файл'}</dd></div><div><dt>Срок</dt><dd>{untimed ? 'Без таймера' : attempt.deadlineAt || attempt.expectedEndAt ? `Около ${remaining}` : 'Контролируется Moodle'}</dd></div><div><dt>Помощник</dt><dd>{attempt.aiEnabled ? 'Доступен' : 'Отключён'}</dd></div></dl>{untimed && <p>Код сохраняется автоматически. Нажмите «Завершить работу», чтобы сдать работу.</p>}<div className="rules-card"><Info size={16} /><div><strong>Политика вставки</strong><p>{attempt.pastePolicy === 'STRICT' ? 'Копируйте код прямо из редактора. Его можно вставлять в другие файлы и задачи этой работы в рамках текущей попытки. Внешняя вставка запрещена.' : 'Вставка разрешена политикой этой работы.'}</p></div></div></div>
      </aside>
      <section className={cn('ide-center', !bottomPanelOpen && 'ide-center--bottom-collapsed')}><div className="ide-editor">
        <aside className="workspace-files-rail" aria-label="Управление панелью файлов"><button type="button" title={filesPanelOpen ? 'Скрыть файлы' : 'Показать файлы'} aria-label={filesPanelOpen ? 'Скрыть файлы' : 'Показать файлы'} aria-controls="student-file-explorer" aria-expanded={filesPanelOpen} onClick={() => setFilesPanelOpen((value) => !value)}>{filesPanelOpen ? <PanelLeftClose size={18} /> : <PanelLeftOpen size={18} />}</button><span aria-hidden="true">Файлы</span></aside>
        <CodeWorkspace ref={editorRef} explorerId="student-file-explorer" explorerVisible={filesPanelOpen} files={files} activeFileId={activeFileId} onActiveFile={setActiveFileId} onChange={changeFile} onCreateFile={() => setCreateOpen(true)} onDeleteFile={setDeleteTarget} canDeleteFile={(file) => canDeleteWorkspaceFile(file, files, attempt.fileMode)} readOnly={editorReadOnly} strictPaste={attempt.pastePolicy === 'STRICT'} scopeId={attempt.id} clipboardScopeId={attempt.quizSession?.rootAttemptId ?? attempt.id} clipboardSession={clipboardSession} diagnostics={diagnostics} onPasteBlocked={() => { setHistory((items) => [{ id: createUuid(), type: 'paste_blocked', label: 'Неподтверждённая вставка заблокирована', at: new Date().toISOString(), revision: attempt.revision }, ...items]); toast.push('info', 'Вставка запрещена', 'Скопируйте фрагмент заново из редактора любой задачи этой работы (Ctrl/Cmd+C), затем вставьте (Ctrl/Cmd+V). Текст из других страниц и приложений не принимается.'); }} onInternalCopy={createInternalReceipt} /></div>
        <BottomPanel runControl={interactiveActive ? <Button className="program-run-button" variant="secondary" loading={running} disabled={Boolean(switchingQuestionId)} onClick={() => void stopInteractive()}><Square size={15} fill="currentColor" /> Остановить</Button> : <Button className="program-run-button" variant="secondary" loading={running} disabled={editorReadOnly} onClick={() => void execute()}><Play size={16} fill="currentColor" /> Запустить</Button>} open={bottomPanelOpen} onOpenChange={setBottomPanelOpen} active={bottomTab} setActive={setBottomTab} run={interactiveRun} diagnostics={diagnostics} history={history} input={interactiveInput} setInput={setInteractiveInput} starting={running && !interactiveActive} canInput={interactiveActive && !editorReadOnly && !interactiveRun?.inputClosed} onSend={() => void sendInteractiveInput()} onDiagnostic={(diagnostic) => editorRef.current?.openDiagnostic(diagnostic)} /></section>
    </div>
    <AiTutor key={attempt.id} open={aiOpen} onClose={() => setAiOpen(false)} attemptId={attempt.id} courseId={courseId} prepareContext={() => prepareAiContext(attempt.id)} />
    <Modal open={createOpen} title="Новый файл" onClose={() => setCreateOpen(false)} footer={<><Button variant="ghost" onClick={() => setCreateOpen(false)}>Отмена</Button><Button onClick={() => void createFile()} disabled={!newPath}><FilePlus2 size={16} /> Создать</Button></>}><Field label="Напишите имя файла" hint={attempt.fileMode === 'SINGLE' ? 'Можно добавить текстовый файл .txt с данными для программы' : 'Разрешены исходники, заголовки C/C++ и .txt'}><input autoFocus value={newPath} onChange={(event) => setNewPath(event.target.value)} placeholder={attempt.fileMode === 'SINGLE' ? 'input.txt' : 'solution.cpp'} /></Field></Modal>
    <Modal open={Boolean(deleteTarget)} title="Удалить файл?" onClose={() => !deleting && setDeleteTarget(null)} footer={<><Button variant="ghost" disabled={deleting} onClick={() => setDeleteTarget(null)}>Отмена</Button><Button variant="danger" loading={deleting} onClick={() => void deleteFile()}><Trash2 size={16} /> Удалить</Button></>}><p className="modal-copy">Файл <strong>{deleteTarget?.path}</strong> будет удалён из рабочей области отдельной серверной ревизией. Единственный исходный файл удалить нельзя.</p></Modal>
    <Modal open={submitOpen} title={submissionPhase === 'confirm' ? 'Завершить работу?' : submissionPhase === 'error' ? 'Moodle не подтвердил сдачу' : submissionSlow ? 'Сдача продолжается в фоне' : 'Передаём работу в Moodle…'} onClose={() => { if (submitting) return; if (submissionPhase === 'confirm') setSubmitOpen(false); else navigate('/'); }} footer={submissionPhase === 'confirm' ? <><Button variant="ghost" disabled={submitting} onClick={() => setSubmitOpen(false)}>Вернуться к коду</Button><Button loading={submitting} onClick={() => void submit()}>{multiQuestion ? 'Сдать все задачи' : `Сдать ревизию ${attempt.acknowledgedRevision}`}</Button></> : submissionPhase === 'error' ? <><Button variant="ghost" disabled={submitting} onClick={() => navigate('/')}>Вернуться к работам</Button><Button loading={submitting} onClick={() => void retrySubmission()}>Повторить отправку</Button></> : <Button variant={submissionSlow ? 'secondary' : 'ghost'} disabled={submitting} onClick={() => navigate('/')}>{submissionSlow ? 'Вернуться к работам' : 'Продолжить в фоне'}</Button>}>
      <div className="submit-summary"><span>{submissionPhase === 'error' ? <AlertTriangle /> : submissionPhase === 'confirm' ? <CheckCircle2 /> : <Cloud />}</span><div><strong>{submissionPhase === 'confirm' ? (multiQuestion ? `Сдача всей работы · задач: ${quizQuestions.length}` : `Финальная ревизия: ${attempt.acknowledgedRevision}`) : submissionPhase === 'error' || submissionSlow ? 'Финальная версия сохранена в системе' : 'Ожидаем подтверждение Moodle'}</strong><p>{submissionPhase === 'confirm' ? (saveState === 'saved' ? 'Все изменения подтверждены сервером.' : 'Перед сдачей система дождётся сохранения изменений.') : submissionPhase === 'error' ? submissionError : submissionError || (submissionSlow ? 'Moodle отвечает дольше обычного. Можно вернуться к списку — отправка продолжится автоматически.' : 'Обычно это занимает несколько секунд. Успех будет показан только после загрузки ответа и завершения попытки в Moodle.')}</p></div></div>
      <dl className="submit-details">{!untimed && <div><dt>{attempt.deadlineAt || attempt.expectedEndAt ? 'Осталось времени' : 'Время в сессии'}</dt><dd>{remaining}</dd></div>}<div><dt>Последний запуск</dt><dd>{interactiveRun ? (interactiveRun.status === 'SUCCESS' ? 'успешный' : interactiveRun.terminal ? 'с ошибкой' : 'выполняется') : 'не запускалось'}</dd></div><div><dt>{submissionPhase === 'confirm' ? 'Последнее сохранение в Moodle' : 'Состояние Moodle'}</dt><dd>{submissionPhase === 'error' ? 'Ошибка отправки' : submissionPhase === 'confirm' ? formatDate(attempt.lastCheckpointAt) : submissionSlow ? 'Продолжается в фоне' : 'Отправляется'}</dd></div></dl>
      {submissionPhase === 'confirm' && <p className="warning-copy"><AlertTriangle size={16} /> {multiQuestion ? 'Будут сданы сохранённые решения всех задач и завершена вся попытка Moodle. После подтверждения редактирование всех задач будет недоступно.' : 'После подтверждения редактирование будет недоступно.'}</p>}
    </Modal>
    <Modal open={conflictOpen} title="Конфликт ревизии" onClose={() => setConflictOpen(false)} footer={<><Button variant="secondary" onClick={() => downloadLocalCopy(files, attempt.id)}>Скачать локальную копию</Button><Button variant="danger" onClick={() => void load()}>Загрузить серверную версию</Button></>}><div className="conflict-copy"><AlertTriangle /><div><strong>Серверная рабочая область изменилась</strong><p>Автосохранение остановлено: локальные изменения остаются в этой вкладке. Скачайте их перед загрузкой серверной версии или закройте окно и скопируйте нужные фрагменты вручную.</p></div></div></Modal>
    {lmsFinalizedModal}
  </div>;
}

function BottomPanel({ runControl, open, onOpenChange, active, setActive, run, diagnostics, history, input, setInput, starting, canInput, onSend, onDiagnostic }: { runControl: ReactNode; open: boolean; onOpenChange(value: boolean): void; active: BottomTab; setActive(value: BottomTab): void; run: InteractiveRun | null; diagnostics: Diagnostic[]; history: HistoryEvent[]; input: string; setInput(value: string): void; starting: boolean; canInput: boolean; onSend(): void; onDiagnostic(value: Diagnostic): void }) {
  const tabs: Array<{ id: BottomTab; label: string; icon: typeof TerminalSquare; count?: number }> = [
    { id: 'output', label: 'Консоль', icon: TerminalSquare }, { id: 'problems', label: 'Проблемы', icon: AlertTriangle, count: diagnostics.length },
    { id: 'history', label: 'История', icon: History, count: history.length },
  ];
  return <section className={cn('bottom-panel', !open && 'bottom-panel--collapsed')}><header>{open ? <div className="bottom-panel__tabs" role="tablist" aria-label="Вывод программы и история">{tabs.map(({ id, label, icon: Icon, count }) => <button key={id} type="button" className={cn(active === id && 'is-active')} role="tab" aria-selected={active === id} onClick={() => setActive(id)}><Icon size={14} />{label}{count !== undefined && <em>{count}</em>}</button>)}</div> : <strong className="bottom-panel__collapsed-label"><TerminalSquare size={14} /> Консоль скрыта</strong>}<div className="bottom-panel__actions">{runControl}<button type="button" className="bottom-panel__toggle" aria-controls="student-bottom-panel" aria-expanded={open} onClick={() => onOpenChange(!open)}>{open ? <PanelBottomClose size={13} /> : <PanelBottomOpen size={13} />}{open ? 'Закрыть' : 'Открыть консоль'}</button></div></header>{open && <div id="student-bottom-panel" className="bottom-panel__body">
    {active === 'output' && <div className="student-console"><div className="student-console__output">{starting && <p>$ Компиляция и запуск…</p>}{run?.stdout && <pre>{run.stdout}</pre>}{run?.stderr && <pre className="terminal-error">{run.stderr}</pre>}{run?.outputTruncated && <p className="terminal-error">Вывод остановлен: достигнут установленный лимит.</p>}{!starting && !run && <p>$ Нажмите «Запустить». Если программа запросит данные, введите строку ниже и нажмите Enter.</p>}{run?.terminal && <p className={run.status === 'SUCCESS' ? 'terminal-success' : run.status === 'STOPPED' ? '' : 'terminal-error'}>{interactiveStatusLabel(run)} · {run.durationMs} мс</p>}</div><form className="student-console__form" onSubmit={(event) => { event.preventDefault(); onSend(); }}><input aria-label="Ввод программы" value={input} onChange={(event) => setInput(event.target.value)} disabled={!canInput} maxLength={65_536} autoComplete="off" spellCheck={false} placeholder={run?.terminal ? 'Программа завершена' : canInput ? 'Введите строку и нажмите Enter' : 'Сначала запустите программу'} /><button type="submit" aria-label="Передать строку программе" disabled={!canInput}><Send size={14} /> Отправить</button></form></div>}
    {active === 'problems' && <div className="problems-list">{diagnostics.length ? [...diagnostics].sort((a, b) => severityOrder(a.severity) - severityOrder(b.severity)).map((item) => <button key={item.id} onClick={() => onDiagnostic(item)}><span className={cn('problem-icon', `problem-icon--${item.severity}`)}>{item.severity === 'error' ? '×' : '!'}</span><span><strong>{item.message}</strong><small>{item.path ?? 'Сборка'}{item.line ? `:${item.line}:${item.column ?? 1}` : ''}{item.code ? ` · ${item.code}` : ''}</small>{item.notes?.map((note) => <em key={note}>{note}</em>)}</span><ChevronDown size={15} /></button>) : <div className="panel-empty"><CheckCircle2 size={20} /><span><strong>Проблем не найдено</strong><small>Запустите сборку для обновления диагностики.</small></span></div>}</div>}
    {active === 'history' && <div className="history-list">{history.slice(0, 15).map((item) => <div key={item.id}><span><FileClock size={14} /></span><p><strong>{item.label}</strong><small>{[item.detail ?? `Ревизия ${item.revision}`, formatClientContext(item.client), formatDate(item.at)].filter(Boolean).join(' · ')}</small></p></div>)}</div>}
  </div>}</section>;
}

function AiTutor({ open, onClose, attemptId, courseId, prepareContext }: { open: boolean; onClose(): void; attemptId: string; courseId: string; prepareContext(): Promise<number> }) {
  const [message, setMessage] = useState('');
  const [sending, setSending] = useState(false);
  const threadIdRef = useRef<string>();
  const mountedRef = useRef(false);
  useEffect(() => { mountedRef.current = true; return () => { mountedRef.current = false; }; }, []);
  const [messages, setMessages] = useState<Array<{ from: 'ai' | 'user'; text: string; citations?: Array<{ title: string; url: string }> }>>([]);
  async function sendMessage() {
    const content = message.trim();
    if (!content || sending) return;
    setMessage(''); setMessages((items) => [...items, { from: 'user', text: content }]); setSending(true);
    try {
      const revision = await prepareContext();
      if (!mountedRef.current) return;
      if (!threadIdRef.current) threadIdRef.current = (await api.createStudentAiThread(attemptId, courseId, revision)).id;
      if (!mountedRef.current) return;
      const response = await api.sendAiMessage(threadIdRef.current, content, { revision });
      if (!mountedRef.current) return;
      setMessages((items) => [...items, { from: 'ai', text: response.content, citations: response.citations }]);
    } catch (caught) {
      if (!mountedRef.current) return;
      setMessages((items) => [...items, { from: 'ai', text: caught instanceof Error ? `Помощник сейчас недоступен: ${caught.message}` : 'Помощник сейчас недоступен.' }]);
    } finally { if (mountedRef.current) setSending(false); }
  }
  if (!open) return null;
  return <aside className="ai-drawer"><header><span><Bot /><span><strong>Учебный помощник</strong><small>Условие и все файлы открытой задачи</small></span></span><button onClick={onClose}><X /></button></header><div className="ai-policy"><ShieldAlertIcon /><p>Только объяснения и ссылки на документацию по языку, без написания кода.</p></div><div className="ai-messages">{messages.map((item, index) => <div className={cn('ai-message', item.from === 'user' && 'ai-message--user')} key={index}>{item.text}{item.citations?.map((citation) => <a key={citation.url} href={citation.url} target="_blank" rel="noreferrer">{citation.title}</a>)}</div>)}{sending && <div className="ai-message ai-message--thinking">Сохраняем код и готовим ответ…</div>}</div><form onSubmit={(event) => { event.preventDefault(); void sendMessage(); }}><textarea value={message} disabled={sending} onChange={(event) => setMessage(event.target.value)} placeholder="Ваш вопрос…" /><Button size="icon" loading={sending} disabled={!message.trim()} aria-label="Отправить"><Send size={17} /></Button></form><small className="ai-disclaimer">Ответы ИИ могут содержать ошибки — проверяйте документацию.</small></aside>;
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

function activeFileStorageKey(attemptId: string): string {
  return `eduprog:active-file:${attemptId}`;
}

function restoredActiveFile(attempt: Attempt): string {
  try {
    const stored = window.sessionStorage.getItem(activeFileStorageKey(attempt.id));
    if (stored && attempt.files.some((file) => file.id === stored)) return stored;
  } catch { /* Storage can be unavailable in a restricted browser. */ }
  return attempt.files[0]?.id ?? '';
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
