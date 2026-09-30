import { DiffEditor } from '@monaco-editor/react';
import {
  AlertTriangle, Bot, Check, CheckCircle2, ChevronLeft, ChevronRight,
  FlaskConical, GitCompareArrows, History, Info, MessageSquareText, Play, RefreshCcw,
  PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen, RotateCcw, Save,
  Send, ShieldCheck, Square, UserCheck, X,
} from 'lucide-react';
import { useCallback, useEffect, useRef, useState, type MutableRefObject } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { CodeWorkspace, type CodeWorkspaceHandle } from '../components/CodeWorkspace';
import { ConsoleTranscript, type ConsoleInput } from '../components/ConsoleTranscript';
import { Badge, Button, Field, InlineError, Modal, PageLoader, useToast } from '../components/ui';
import { useTheme } from '../context/ThemeContext';
import { api } from '../lib/api';
import { cn, formatClientContext, formatDate } from '../lib/utils';
import type { Assessment, AuthorshipAnalysis, Diagnostic, InteractiveRun, SimilarityAnalysis, Submission, SubmissionReviewGroupItem, TeacherExperiment, WorkspaceFile } from '../types';

type EvidenceTab = 'task' | 'integrity' | 'history' | 'ai';
type PrefetchedReview = {
  submission: Submission;
  assessment: Assessment | null;
  draft: { grade: number | null; comment: string } | null;
};

function lmsExportStateLabel(state: string): string {
  const labels: Record<string, string> = {
    IMPORTED: 'Импортировано из Moodle',
    DELIVERED: 'Оценка передана в Moodle',
    PENDING: 'Ожидает отправки в Moodle',
    PROCESSING: 'Передаём оценку в Moodle',
    RETRY: 'Повторяем передачу оценки в Moodle',
    BLOCKED: 'Передача оценки в Moodle заблокирована.',
    FAILED: 'Не удалось подтвердить передачу оценки в Moodle.',
    SUPERSEDED: 'Заменено новой проверкой',
  };
  return labels[state.toUpperCase()] ?? 'Статус обмена с Moodle не определён';
}

export function ReviewPage() {
  const { submissionId = 'sub-1' } = useParams();
  const prefetchedGroupedReview = useRef<PrefetchedReview | null>(null);
  // Async sandbox operations and their refs belong to exactly one submission.
  // A route change must not reuse a previous question's experiment or run.
  return <SubmissionReview key={submissionId} submissionId={submissionId} prefetchedGroupedReview={prefetchedGroupedReview} />;
}

function SubmissionReview({ submissionId, prefetchedGroupedReview }: {
  submissionId: string;
  prefetchedGroupedReview: MutableRefObject<PrefetchedReview | null>;
}) {
  const [submission, setSubmission] = useState<Submission | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeFileId, setActiveFileId] = useState('');
  const [evidenceTab, setEvidenceTab] = useState<EvidenceTab>('task');
  const [experiment, setExperiment] = useState<TeacherExperiment | null>(null);
  const [experimentFiles, setExperimentFiles] = useState<WorkspaceFile[]>([]);
  const [interactiveRun, setInteractiveRun] = useState<InteractiveRun | null>(null);
  const [experimentMode, setExperimentMode] = useState(false);
  const [consoleOpen, setConsoleOpen] = useState(false);
  const [interactiveInput, setInteractiveInput] = useState('');
  const [consoleInputs, setConsoleInputs] = useState<ConsoleInput[]>([]);
  const [inputSending, setInputSending] = useState(false);
  const [diffOpen, setDiffOpen] = useState(false);
  const [running, setRunning] = useState(false);
  const [grade, setGrade] = useState('');
  const [comment, setComment] = useState('');
  const [reviewDirty, setReviewDirty] = useState(false);
  const [decisionOpen, setDecisionOpen] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const [rechecking, setRechecking] = useState(false);
  const [courseId, setCourseId] = useState('');
  const [reviewRequired, setReviewRequired] = useState(true);
  const [decisionSupportEnabled, setDecisionSupportEnabled] = useState(true);
  const [claimLost, setClaimLost] = useState(false);
  const [filesPanelOpen, setFilesPanelOpen] = useState(true);
  const [reviewPanelOpen, setReviewPanelOpen] = useState(true);
  const [closingExperiment, setClosingExperiment] = useState(false);
  const [switchingSubmissionId, setSwitchingSubmissionId] = useState<string | null>(null);
  const editorRef = useRef<CodeWorkspaceHandle>(null);
  const loadPromiseRef = useRef<Promise<void> | null>(null);
  const stoppedClaimsRef = useRef(new Set<string>());
  const saveTimer = useRef<number>();
  const experimentRef = useRef<TeacherExperiment | null>(null);
  const pendingExperimentEdits = useRef<WorkspaceFile[]>([]);
  const experimentFlush = useRef<Promise<number> | null>(null);
  const experimentCreate = useRef<Promise<TeacherExperiment | null> | null>(null);
  const experimentRun = useRef<Promise<void> | null>(null);
  const experimentStop = useRef<Promise<InteractiveRun | null> | null>(null);
  const consoleClose = useRef<Promise<void> | null>(null);
  const experimentClose = useRef<Promise<void> | null>(null);
  const interactiveRunRef = useRef<InteractiveRun | null>(null);
  const inputSendingRef = useRef(false);
  const toast = useToast();
  const toastRef = useRef(toast);
  const { theme } = useTheme();
  const navigate = useNavigate();
  const location = useLocation();
  experimentRef.current = experiment;
  interactiveRunRef.current = interactiveRun;
  toastRef.current = toast;

  useEffect(() => {
    if (!experiment?.id || !interactiveRun) return;
    if (interactiveRun.terminal) {
      clearInteractiveExperimentSession(submissionId, experiment.id);
      return;
    }
    rememberInteractiveExperimentSession(submissionId, experiment.id, interactiveRun.sessionId, experimentMode);
  }, [experiment?.id, experimentMode, interactiveRun, submissionId]);

  useEffect(() => {
    if (!experiment?.id || !interactiveRun || interactiveRun.terminal) return;
    let cancelled = false;
    let timer = 0;
    const poll = async () => {
      try {
        const current = await api.getInteractiveExperiment(experiment.id, interactiveRun.sessionId);
        if (!cancelled && !(interactiveRunRef.current?.sessionId === current.sessionId && interactiveRunRef.current.terminal && !current.terminal)) {
          interactiveRunRef.current = current;
          setInteractiveRun(current);
        }
        if (!cancelled && !current.terminal) timer = window.setTimeout(() => { void poll(); }, 350);
      } catch (caught) {
        if (!cancelled) setInteractiveRun((current) => {
          const failed: InteractiveRun | null = current ? {
            ...current,
            status: 'INFRA_ERROR' as const,
            terminal: true,
            stderr: caught instanceof Error ? caught.message : 'Не удалось получить вывод программы',
          } : current;
          interactiveRunRef.current = failed;
          return failed;
        });
      }
    };
    timer = window.setTimeout(() => { void poll(); }, 200);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [experiment?.id, interactiveRun?.sessionId, interactiveRun?.terminal]);

  const load = useCallback(async () => {
    if (loadPromiseRef.current) return loadPromiseRef.current;
    const operation = (async () => {
      setLoading(true);
      setError(null);
      setClaimLost(false);
      setEvidenceTab('task');
      experimentRef.current = null;
      interactiveRunRef.current = null;
      pendingExperimentEdits.current = [];
      setExperiment(null);
      setExperimentFiles([]);
      setExperimentMode(false);
      setInteractiveRun(null);
      setInteractiveInput('');
      setConsoleInputs([]);
      setConsoleOpen(false);
      setDiffOpen(false);
      try {
        const prefetched = prefetchedGroupedReview.current?.submission.id === submissionId
          ? prefetchedGroupedReview.current
          : null;
        if (prefetched) prefetchedGroupedReview.current = null;
        const item = prefetched?.submission ?? await api.getSubmission(submissionId);
        const [assessment, draft] = prefetched
          ? [prefetched.assessment, prefetched.draft]
          : await Promise.all([
            item.courseId ? Promise.resolve(null) : api.getAssessment(item.assessmentId),
            item.canReview !== false ? api.getReviewDraft(item.id) : Promise.resolve(null),
          ]);
        // The backend deliberately returns canReview=true for a system
        // administrator even when the original assessment did not require a
        // teacher decision.  Treat that explicit authorization as the
        // effective review policy without coupling this page to auth context.
        const requiresReview = item.canReview === true
          || (item.reviewRequired ?? assessment?.reviewRequired ?? true);
        setSubmission(item); setCourseId(item.courseId ?? assessment?.courseId ?? ''); setReviewRequired(requiresReview);
        setDecisionSupportEnabled(item.decisionSupportEnabled ?? assessment?.decisionSupportEnabled ?? true); setActiveFileId(item.files[0]?.id ?? '');
        if (item.source === 'MOODLE_IMPORT' && markMoodleImportNoticeShown(item.id)) {
          toastRef.current.push('info', 'Импортировано из Moodle', 'Доступен итоговый код и решение; истории набора в Moodle нет.');
        }
        if (item.canReview !== false && requiresReview && !item.claim && item.status === 'UNGRADED') { const claim = await api.claimSubmission(item.id); setSubmission({ ...item, status: 'CLAIMED', claim }); }
        const finalDecision = item.latestDecision ?? item.decisionHistory[0];
        const finalDecisionIsReadOnly = Boolean(finalDecision && !item.claim?.mine);
        setGrade(item.status === 'GRADED' || finalDecisionIsReadOnly
          ? finalDecision?.grade.toString() ?? item.score?.toString() ?? ''
          : draft ? draft.grade?.toString() ?? '' : finalDecision?.grade.toString() ?? item.score?.toString() ?? '');
        setComment(item.status === 'GRADED' || finalDecisionIsReadOnly ? finalDecision?.comment ?? '' : draft?.comment ?? finalDecision?.comment ?? ''); setReviewDirty(false); setError(null);
        const storedInteractive = readInteractiveExperimentSession(item.id);
        if (storedInteractive) {
          try {
            const restoredExperiment = await api.createExperiment(item.id);
            if (restoredExperiment.submissionId !== item.id || restoredExperiment.id !== storedInteractive.experimentId) {
              clearInteractiveExperimentSession(item.id);
            } else {
              const restoredRun = await api.getInteractiveExperiment(restoredExperiment.id, storedInteractive.sessionId);
              experimentRef.current = restoredExperiment; setExperiment(restoredExperiment); setExperimentFiles(restoredExperiment.files);
              setActiveFileId((storedInteractive.sandboxMode ?? true) ? restoredExperiment.files[0]?.id ?? '' : item.files[0]?.id ?? '');
              setExperimentMode(storedInteractive.sandboxMode ?? true); setConsoleOpen(true); setInteractiveRun(restoredRun);
              if (restoredRun.terminal) clearInteractiveExperimentSession(item.id, restoredExperiment.id);
            }
          } catch {
            clearInteractiveExperimentSession(item.id);
          }
        }
      } catch (caught) { setError(caught instanceof Error ? caught.message : 'Не удалось открыть сдачу'); }
      finally { setLoading(false); }
    })();
    loadPromiseRef.current = operation.finally(() => { loadPromiseRef.current = null; });
    return loadPromiseRef.current;
  }, [submissionId, prefetchedGroupedReview]);
  useEffect(() => { void load(); return () => { if (saveTimer.current) window.clearTimeout(saveTimer.current); }; }, [load]);
  useEffect(() => {
    const claim = submission?.claim;
    if (!claim?.mine || claimLost || finalizing || stoppedClaimsRef.current.has(claim.id)) return;
    let cancelled = false;
    let inFlight = false;
    const heartbeat = async () => {
      if (cancelled || inFlight || stoppedClaimsRef.current.has(claim.id)) return;
      inFlight = true;
      try {
        const renewed = await api.heartbeatClaim(claim.id);
        if (!cancelled && !stoppedClaimsRef.current.has(claim.id)) {
          setSubmission((value) => value?.claim?.id === claim.id ? { ...value, claim: renewed } : value);
        }
      } catch (caught) {
        if (!cancelled && !stoppedClaimsRef.current.has(claim.id)) {
          setClaimLost(true);
          toastRef.current.push('error', 'Право на изменение проверки потеряно', caught instanceof Error ? caught.message : 'Работа открыта только для чтения.');
        }
      } finally { inFlight = false; }
    };
    void heartbeat();
    const timer = window.setInterval(() => { void heartbeat(); }, 120_000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [submission?.id, submission?.claim?.id, submission?.claim?.mine, claimLost, finalizing]);

  const exportedDecision = submission?.latestDecision ?? submission?.decisionHistory[0];
  useEffect(() => {
    if (!submission || submission.claim?.mine || !exportedDecision
      || !['PENDING', 'PROCESSING', 'RETRY', 'FAILED', 'BLOCKED'].includes(exportedDecision.lmsExportState)) return;
    const id = submission.id;
    const decisionId = exportedDecision.id;
    let cancelled = false;
    let inFlight = false;
    let timer: number;
    const refreshExport = async () => {
      if (cancelled || inFlight) return;
      inFlight = true;
      try {
        const fresh = await api.getSubmission(id);
        const decision = fresh.latestDecision ?? fresh.decisionHistory[0];
        if (!cancelled && decision?.id === decisionId) {
          // Refresh delivery metadata only; never replace unsaved review edits
          // or let a response from the previous question overwrite this one.
          setSubmission((current) => current?.id === id && !current.claim?.mine
            && (current.latestDecision ?? current.decisionHistory[0])?.id === decisionId ? {
              ...current,
              latestDecision: current.latestDecision ? { ...current.latestDecision, lmsExportState: decision.lmsExportState } : undefined,
              decisionHistory: current.decisionHistory.map((entry) => entry.id === decisionId ? { ...entry, lmsExportState: decision.lmsExportState } : entry),
            } : current);
        }
      } catch { /* Keep the last confirmed status on a transient read failure. */ }
      finally {
        inFlight = false;
        if (!cancelled) timer = window.setTimeout(() => { void refreshExport(); }, 5000);
      }
    };
    timer = window.setTimeout(() => { void refreshExport(); }, 3000);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [submission?.id, submission?.claim?.mine, exportedDecision?.id, exportedDecision?.lmsExportState]);

  async function enableExperiment(enterMode = true): Promise<TeacherExperiment | null> {
    if (!submission || !canUseSandbox) return null;
    let current = experimentRef.current?.submissionId === submission.id ? experimentRef.current : null;
    if (!current) {
      if (!experimentCreate.current) {
        const operation = api.createExperiment(submission.id).then((created) => {
          if (created.submissionId !== submission.id) throw new Error('Песочница относится к другому заданию. Откройте задание повторно.');
          experimentRef.current = created;
          setExperiment(created);
          setExperimentFiles(created.files);
          return created;
        });
        experimentCreate.current = operation.finally(() => { experimentCreate.current = null; });
      }
      try {
        current = await experimentCreate.current;
      } catch (caught) {
        toast.push(
          'error',
          enterMode ? 'Песочница не открыта' : 'Запуск не выполнен',
          caught instanceof Error ? caught.message : undefined,
        );
        return null;
      }
    }
    if (current && enterMode) {
      setActiveFileId(experimentRef.current?.files[0]?.id ?? current.files[0]?.id ?? '');
      setExperimentMode(true);
    }
    return current;
  }

  function changeExperimentFile(fileId: string, content: string) {
    if (!experiment || !canUseSandbox || experimentClose.current) return;
    const updated = experimentFiles.map((file) => file.id === fileId ? { ...file, content } : file);
    setExperimentFiles(updated); setExperiment({ ...experiment, changed: true });
    const file = updated.find((item) => item.id === fileId);
    if (!file) return;
    const last = pendingExperimentEdits.current.at(-1);
    if (last?.id === fileId) pendingExperimentEdits.current[pendingExperimentEdits.current.length - 1] = file;
    else pendingExperimentEdits.current.push(file);
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => { void flushExperimentEdits().catch(() => undefined); }, 650);
  }

  function flushExperimentEdits(): Promise<number> {
    if (experimentFlush.current) return experimentFlush.current;
    const operation = (async () => {
      let current = experimentRef.current;
      if (!current) return 0;
      while (pendingExperimentEdits.current.length) {
        const file = pendingExperimentEdits.current[0];
        const revision = await api.saveExperimentFile(current.id, file, current.revision);
        if (pendingExperimentEdits.current[0] === file) pendingExperimentEdits.current.shift();
        current = { ...current, revision, changed: true };
        experimentRef.current = current; setExperiment(current);
      }
      return current.revision;
    })();
    experimentFlush.current = operation.catch((caught) => {
      toast.push('error', 'Изменения рабочей копии не сохранены', caught instanceof Error ? caught.message : undefined);
      throw caught;
    }).finally(() => { experimentFlush.current = null; });
    return experimentFlush.current;
  }

  function runExperiment(): Promise<void> {
    if (experimentRun.current) return experimentRun.current;
    if (!canUseSandbox || experimentClose.current) return Promise.resolve();
    const active = interactiveRunRef.current;
    if (active && !active.terminal) {
      setConsoleOpen(true);
      return Promise.resolve();
    }
    setRunning(true);
    const operation = (async () => {
      try {
        let current = await enableExperiment(false);
        if (!current || experimentClose.current) return;
        // The ordinary Run action always represents the immutable student
        // submission.  A durable teacher experiment may still contain edits
        // from an earlier sandbox session, so restore its base snapshot before
        // compiling while the sandbox UI is closed.
        if (!experimentMode && current.changed) {
          if (saveTimer.current) window.clearTimeout(saveTimer.current);
          saveTimer.current = undefined;
          if (experimentFlush.current || pendingExperimentEdits.current.length) await flushExperimentEdits();
          current = experimentRef.current ?? current;
          const restored = await api.resetExperiment(current.id, current.revision);
          pendingExperimentEdits.current = [];
          experimentRef.current = restored;
          setExperiment(restored);
          setExperimentFiles(restored.files);
          current = restored;
        }
        setConsoleOpen(true);
        if (saveTimer.current) window.clearTimeout(saveTimer.current);
        const revision = pendingExperimentEdits.current.length ? await flushExperimentEdits() : current.revision;
        const result = await api.startInteractiveExperiment(current.id, revision);
        interactiveRunRef.current = result;
        setConsoleInputs([]);
        setInteractiveInput(''); setInteractiveRun(result);
        if (result.diagnostics[0]) editorRef.current?.openDiagnostic(result.diagnostics[0]);
      }
      catch (caught) { toast.push('error', 'Запуск не выполнен', caught instanceof Error ? caught.message : undefined); }
    })();
    experimentRun.current = operation.finally(() => {
      experimentRun.current = null;
      setRunning(false);
    });
    return experimentRun.current;
  }

  function stopActiveExperimentRun(): Promise<InteractiveRun | null> {
    if (experimentStop.current) return experimentStop.current;
    const current = experimentRef.current;
    const active = interactiveRunRef.current;
    if (!current || !active || active.terminal) return Promise.resolve(active);
    const operation = api.stopInteractiveExperiment(current.id, active.sessionId).then((stopped) => {
      interactiveRunRef.current = stopped;
      setInteractiveRun(stopped);
      clearInteractiveExperimentSession(submissionId, current.id);
      return stopped;
    });
    experimentStop.current = operation.finally(() => { experimentStop.current = null; });
    return experimentStop.current;
  }

  async function stopExperiment() {
    if (!interactiveRunRef.current || interactiveRunRef.current.terminal) return;
    setRunning(true);
    try { await stopActiveExperimentRun(); }
    catch (caught) { toast.push('error', 'Программа не остановлена', caught instanceof Error ? caught.message : undefined); }
    finally { setRunning(false); }
  }

  function closeConsole(): Promise<void> {
    if (consoleClose.current) return consoleClose.current;
    const operation = (async () => {
      try {
        if (experimentRun.current) await experimentRun.current;
        await stopActiveExperimentRun();
        setConsoleOpen(false);
      } catch (caught) {
        toast.push('error', 'Консоль не закрыта', caught instanceof Error ? caught.message : 'Не удалось остановить программу.');
      }
    })();
    consoleClose.current = operation.finally(() => { consoleClose.current = null; });
    return consoleClose.current;
  }

  async function sendExperimentInput() {
    const current = experimentRef.current;
    const active = interactiveRunRef.current;
    if (!current || !active || active.terminal || active.inputClosed || inputSendingRef.current) return;
    const text = interactiveInput;
    inputSendingRef.current = true;
    setInputSending(true);
    try {
      const updated = await api.sendInteractiveInput(current.id, active.sessionId, text);
      if (interactiveRunRef.current?.sessionId !== active.sessionId) return;
      setConsoleInputs((inputs) => [...inputs, { sessionId: active.sessionId, stdoutOffset: active.stdout.length, text }]);
      // Do not erase a new line typed while the preceding request was pending.
      setInteractiveInput((value) => value === text ? '' : value);
      if (!interactiveRunRef.current.terminal || updated.terminal) {
        interactiveRunRef.current = updated;
        setInteractiveRun(updated);
      }
    }
    catch (caught) { toast.push('error', 'Ввод не передан программе', caught instanceof Error ? caught.message : undefined); }
    finally { inputSendingRef.current = false; setInputSending(false); }
  }

  function closeExperimentMode(): Promise<void> {
    if (experimentClose.current) return experimentClose.current;
    if (!experimentRef.current) {
      setExperimentMode(false);
      return Promise.resolve();
    }
    setClosingExperiment(true);
    const operation = (async () => {
      try {
        if (saveTimer.current) window.clearTimeout(saveTimer.current);
        saveTimer.current = undefined;
        if (experimentRun.current) await experimentRun.current;
        if (experimentFlush.current || pendingExperimentEdits.current.length) await flushExperimentEdits();
        const current = experimentRef.current;
        if (!current) return;
        await stopActiveExperimentRun();
        clearInteractiveExperimentSession(submissionId, current.id);
        interactiveRunRef.current = null; setExperimentMode(false); setConsoleOpen(false); setInteractiveRun(null); setInteractiveInput(''); setDiffOpen(false);
        if (submission) setActiveFileId(submission.files[0]?.id ?? '');
      } catch (caught) {
        toast.push('error', 'Песочница не закрыта', caught instanceof Error ? caught.message : undefined);
      }
    })();
    experimentClose.current = operation.finally(() => {
      experimentClose.current = null;
      setClosingExperiment(false);
    });
    return experimentClose.current;
  }

  async function persistReviewDraft(notifySuccess: boolean): Promise<boolean> {
    if (!reviewDirty) return true;
    if (!submission?.claim?.mine || claimLost) {
      toast.push('error', 'Изменения не сохранены', 'Закрепление работы потеряно. Останьтесь на текущем задании и обновите страницу.');
      return false;
    }
    const parsedGrade = grade === '' ? null : Number(grade);
    if (parsedGrade !== null && (!Number.isFinite(parsedGrade) || parsedGrade < 0 || parsedGrade > submission.maxScore)) {
      toast.push('error', 'Изменения не сохранены', `Укажите балл от 0 до ${submission.maxScore}, прежде чем переходить к другому заданию.`);
      return false;
    }
    try {
      await api.saveReview(submission.id, parsedGrade, comment);
      setReviewDirty(false);
      if (notifySuccess) toast.push('success', 'Черновик сохранён');
      return true;
    } catch (caught) {
      toast.push('error', 'Черновик не сохранён', caught instanceof Error ? caught.message : undefined);
      return false;
    }
  }

  async function saveDraft() {
    await persistReviewDraft(true);
  }

  async function startRecheck() {
    if (!submission || submission.canReview === false || submission.status !== 'GRADED' || rechecking) return;
    setRechecking(true);
    try {
      const claim = await api.claimSubmission(submission.id);
      setClaimLost(false);
      setSubmission({ ...submission, status: 'CLAIMED', claim });
      toast.push('success', 'Перепроверка начата', 'Предыдущее решение сохранено в истории. Новое решение потребует отдельного утверждения.');
    } catch (caught) {
      toast.push('error', 'Не удалось начать перепроверку', caught instanceof Error ? caught.message : 'Обновите страницу и попробуйте снова.');
    } finally { setRechecking(false); }
  }

  async function openGroupedSubmission(targetSubmissionId: string) {
    if (!submission || targetSubmissionId === submission.id || switchingSubmissionId) return;
    setSwitchingSubmissionId(targetSubmissionId);
    try {
      // A question switch is also a review-context switch. Persist edits first,
      // and retain the current reservation until the destination has been fully
      // loaded and (when necessary) reserved for this teacher.
      if (!await persistReviewDraft(false)) return;
      // Creation/start may still be pending before the console is visible.
      // Finish saving/stopping this question before releasing its claim.
      if (experimentCreate.current) await experimentCreate.current;
      if (experimentRun.current) await experimentRun.current;
      if (experimentClose.current) await experimentClose.current;
      if (consoleClose.current) await consoleClose.current;
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
      saveTimer.current = undefined;
      if (experimentFlush.current || pendingExperimentEdits.current.length) await flushExperimentEdits();
      await stopActiveExperimentRun();

      const target = await api.getSubmission(targetSubmissionId);
      const [assessment, draft] = await Promise.all([
        target.courseId ? Promise.resolve(null) : api.getAssessment(target.assessmentId),
        target.canReview !== false ? api.getReviewDraft(target.id) : Promise.resolve(null),
      ]);
      const targetRequiresReview = target.canReview === true
        || (target.reviewRequired ?? assessment?.reviewRequired ?? true);
      let openableTarget = target;
      if (target.canReview !== false && targetRequiresReview && !target.claim && target.status === 'UNGRADED') {
        const targetClaim = await api.claimSubmission(target.id);
        openableTarget = { ...target, status: 'CLAIMED', claim: targetClaim };
      }
      prefetchedGroupedReview.current = { submission: openableTarget, assessment, draft };

      if (submission.claim?.mine) {
        stoppedClaimsRef.current.add(submission.claim.id);
        await api.releaseClaim(submission.claim.id).catch(() => undefined);
      }
      navigate(`/review/${encodeURIComponent(targetSubmissionId)}${location.search}`);
    } catch (caught) {
      toast.push('error', 'Задание не открыто', caught instanceof Error ? caught.message : 'Не удалось получить следующее задание. Текущее закрепление сохранено.');
    } finally {
      setSwitchingSubmissionId(null);
    }
  }

  async function finalize() {
    if (!submission?.claim?.mine || claimLost || finalizing || grade === '' || !Number.isFinite(Number(grade)) || Number(grade) < 0 || Number(grade) > submission.maxScore) return;
    const claimId = submission.claim.id;
    if (stoppedClaimsRef.current.has(claimId)) return;
    // Finalization releases the claim on the server. In-flight heartbeats
    // must neither restore it nor report that intentional release as an error.
    stoppedClaimsRef.current.add(claimId);
    setFinalizing(true);
    try {
      const decision = await api.finalizeReview(submission.id, Number(grade), comment);
      setSubmission((value) => value?.id === submission.id ? { ...value, claim: undefined } : value);
      setDecisionOpen(false);
      toast.push(decision.lmsExportState === 'BLOCKED' || decision.lmsExportState === 'FAILED' ? 'error' : 'success', 'Оценка утверждена', decision.lmsExportState === 'PENDING' ? 'Выгрузка в Moodle поставлена в очередь.' : decision.lmsExportState ? lmsExportStateLabel(decision.lmsExportState) : 'Статус выгрузки не предоставлен сервером.');
      navigate(`/submissions${location.search}`);
    }
    catch (caught) {
      stoppedClaimsRef.current.delete(claimId);
      toast.push('error', 'Решение не сохранено', caught instanceof Error ? caught.message : undefined);
    }
    finally { setFinalizing(false); }
  }

  if (loading) return <PageLoader label="Получаем снимок сдачи и закрепление работы…" />;
  if (error || !submission) return <InlineError message={error ?? 'Сдача не найдена'} retry={() => void load()} />;
  const canReview = Boolean(submission.canReview !== false && reviewRequired && submission.claim?.mine && !claimLost && !finalizing);
  const canUseDecisionSupport = submission.canReview !== false && decisionSupportEnabled;
  const canAskTeacherAi = submission.canReview !== false && decisionSupportEnabled;
  const validGrade = grade !== '' && Number.isFinite(Number(grade)) && Number(grade) >= 0 && Number(grade) <= submission.maxScore;
  const validDraft = grade === '' || validGrade;
  const files = experimentMode ? experimentFiles : submission.files;
  const diagnostics = interactiveRun?.diagnostics ?? [];
  const finalDecision = submission.latestDecision ?? submission.decisionHistory[0];
  const foreignClaim = Boolean(submission.claim && !submission.claim.mine);
  const reviewedReadOnly = Boolean(finalDecision && !submission.claim?.mine);
  const canUseSandbox = Boolean(!switchingSubmissionId && submission.canReview !== false && (canReview || finalDecision || submission.status === 'GRADED'));
  const interactiveActive = Boolean(interactiveRun && !interactiveRun.terminal);
  const interactiveInputOpen = interactiveActive && !interactiveRun?.inputClosed;
  const editable = Boolean(experimentMode && experiment && canUseSandbox && !closingExperiment);
  const diffFiles = selectDiffFiles(submission.files, experimentFiles, activeFileId);
  const reviewGroupItems = groupedReviewItems(submission);
  const activeGroupIndex = reviewGroupItems.findIndex((item) => item.submissionId === submission.id);
  return <div className={cn('review-page', experimentMode && 'review-page--experiment', consoleOpen && 'review-page--console')}>
    <header className="review-toolbar">
      <div><button className="review-back" aria-label="Вернуться к работам" onClick={async () => { if (submission.claim?.mine) { stoppedClaimsRef.current.add(submission.claim.id); await api.releaseClaim(submission.claim.id).catch(() => undefined); } navigate(`/submissions${location.search}`); }}><ChevronLeft /></button><span><small>{submission.assessmentTitle}</small><strong>{submission.studentName}{submission.studentGroup !== '—' ? ` · ${submission.studentGroup}` : ''}</strong></span></div>
      <div className={cn('claim-banner', !canReview && 'claim-banner--other', reviewedReadOnly && !foreignClaim && 'claim-banner--reviewed')}>
        {reviewedReadOnly && !foreignClaim ? <CheckCircle2 size={15} /> : <UserCheck size={15} />}
        <span><strong>{!reviewRequired ? 'Проверка преподавателя не требуется' : foreignClaim ? `Проверяет ${submission.claim?.ownerName}` : reviewedReadOnly ? 'Работа проверена' : canReview ? 'Вы проверяете работу' : 'Работа не закреплена'}</strong><small>{!reviewRequired ? 'страница доступна только для чтения' : foreignClaim ? 'официальная перепроверка заблокирована; личная песочница доступна' : reviewedReadOnly ? `${finalDecision?.reviewerName ?? 'Преподаватель'}${finalDecision?.reviewedAt ? ` · ${formatDate(finalDecision.reviewedAt)}` : ''}` : canReview ? 'работа закреплена за вами; резервирование продлевается автоматически' : submission.canReview === false ? 'у вас нет доступа к проверке этой работы' : 'режим только для чтения'}{foreignClaim && submission.claim?.expiresAt ? ` · до ${formatDate(submission.claim.expiresAt, { hour: '2-digit', minute: '2-digit' })}` : ''}</small></span>
      </div>
      <div>{!experimentMode ? <><span className="sandbox-tooltip" data-tooltip="Изменения не затрагивают сдачу студента, историю авторства и проверку на плагиат"><Button aria-label="Открыть преподавательскую песочницу" title="Изменения не затрагивают сдачу студента, историю авторства и проверку на плагиат" variant="ghost" disabled={!canUseSandbox || running || closingExperiment} onClick={() => void enableExperiment(true)}><FlaskConical size={15} /> Открыть песочницу</Button></span>{interactiveActive ? <Button aria-label="Остановить программу" variant="secondary" loading={running} onClick={() => void stopExperiment()}><Square size={14} fill="currentColor" /> Остановить</Button> : <Button aria-label="Компилировать и запустить код" variant="secondary" loading={running} disabled={!canUseSandbox || closingExperiment} onClick={() => void runExperiment()}><Play size={15} /> Запустить</Button>}</> : <><Button aria-label="Закрыть преподавательскую песочницу" variant="ghost" loading={closingExperiment} disabled={running} onClick={() => void closeExperimentMode()}><FlaskConical size={15} /> Закрыть песочницу</Button><Button variant="ghost" className="review-compare-toggle" aria-pressed={diffOpen} title={diffOpen ? 'Сравнение включено. Нажмите ещё раз, чтобы вернуться к редактору.' : 'Сравнить с исходным кодом студента'} onClick={() => setDiffOpen((value) => !value)}><GitCompareArrows size={16} /> Сравнить</Button>{interactiveActive ? <Button aria-label="Остановить программу" variant="secondary" loading={running} onClick={() => void stopExperiment()}><Square size={14} fill="currentColor" /> Остановить</Button> : <Button aria-label="Компилировать и запустить код" variant="secondary" loading={running} disabled={!canUseSandbox || closingExperiment} onClick={() => void runExperiment()}><Play size={15} /> Запустить</Button>}</>}{reviewedReadOnly ? submission.canReview !== false ? <Button loading={rechecking} disabled={foreignClaim} onClick={() => void startRecheck()}><RotateCcw size={16} /> Перепроверить</Button> : <Badge>Только просмотр</Badge> : <Button onClick={() => setDecisionOpen(true)} disabled={!canReview || grade === '' || Number(grade) < 0 || Number(grade) > submission.maxScore}><Check size={16} /> Утвердить</Button>}</div>
    </header>
    {reviewGroupItems.length > 1 && <nav className="review-question-switcher" aria-label="Задания в ответе студента">
      <div className="review-question-switcher__heading"><span>Ответ студента</span><strong>{submission.reviewGroup?.title ?? 'Задания Moodle'}</strong><small>Задание {activeGroupIndex + 1} из {reviewGroupItems.length}</small></div>
      <button type="button" className="review-question-switcher__step" aria-label="Предыдущее задание" disabled={activeGroupIndex <= 0 || Boolean(switchingSubmissionId)} onClick={() => void openGroupedSubmission(reviewGroupItems[activeGroupIndex - 1]?.submissionId)}><ChevronLeft size={17} /></button>
      <div className="review-question-switcher__items" role="tablist" aria-label="Переключение между заданиями">
        {reviewGroupItems.map((item) => {
          const active = item.submissionId === submission.id;
          const status = reviewGroupStatusPresentation(item.status);
          return <button key={item.submissionId} type="button" role="tab" aria-selected={active} disabled={Boolean(switchingSubmissionId)} className={cn('review-question-switcher__item', active && 'is-active')} title={item.title} onClick={() => void openGroupedSubmission(item.submissionId)}>
            <span className="review-question-switcher__number">№{item.position}</span>
            <span className="review-question-switcher__copy"><strong>{item.title}</strong><small>{reviewGroupScoreLabel(item)}</small></span>
            <span className={cn('review-question-switcher__status', `is-${status.tone}`)}>{status.label}</span>
          </button>;
        })}
      </div>
      <button type="button" className="review-question-switcher__step" aria-label="Следующее задание" disabled={activeGroupIndex < 0 || activeGroupIndex >= reviewGroupItems.length - 1 || Boolean(switchingSubmissionId)} onClick={() => void openGroupedSubmission(reviewGroupItems[activeGroupIndex + 1]?.submissionId)}><ChevronRight size={17} /></button>
    </nav>}
    <div className={cn('review-layout', !reviewPanelOpen && 'review-layout--review-collapsed')}><section className="review-code"><div className="submission-meta"><span><strong>Сдано {formatDate(submission.submittedAt)}</strong><small>Неизменяемый снимок · {submission.files.length} файл</small></span><div className="submission-meta__actions">{!experimentMode && <Badge tone="success"><ShieldCheck size={13} /> Оригинал</Badge>}{experiment?.changed && experimentMode && <Badge tone="warning">Есть изменения</Badge>}</div></div>
      <div className={cn('review-editor-shell', !filesPanelOpen && 'review-editor-shell--files-collapsed')}>
        <aside className="review-files-rail workspace-files-rail" aria-label="Панель файлов">
          <button type="button" aria-label={filesPanelOpen ? 'Скрыть файлы' : 'Показать файлы'} title={diffOpen && experimentMode ? 'Файлы доступны в режиме редактора' : filesPanelOpen ? 'Скрыть панель файлов' : 'Показать панель файлов'} aria-expanded={filesPanelOpen && !(diffOpen && experimentMode)} aria-controls={diffOpen && experimentMode ? undefined : 'review-file-explorer'} disabled={diffOpen && experimentMode} onClick={() => setFilesPanelOpen((open) => !open)}>{filesPanelOpen ? <PanelLeftClose size={17} /> : <PanelLeftOpen size={17} />}</button>
          <span aria-hidden="true">Файлы</span>
        </aside>
        <div className="review-editor">{diffOpen && experimentMode ? <DiffWorkspace original={diffFiles.original?.content ?? ''} modified={diffFiles.modified?.content ?? ''} path={diffFiles.path} theme={theme} /> : <CodeWorkspace ref={editorRef} files={files} activeFileId={activeFileId} onActiveFile={setActiveFileId} onChange={(id, content) => changeExperimentFile(id, content)} readOnly={!editable} strictPaste={false} scopeId={experiment?.id ?? submission.id} diagnostics={diagnostics} experiment={experimentMode} explorerId="review-file-explorer" explorerVisible={filesPanelOpen} />}</div>
      </div>
      {consoleOpen && <div className="experiment-console"><header><strong>Консоль программы</strong><span>{interactiveRun ? interactiveStatusLabel(interactiveRun) : 'Готова к запуску'}</span>{diagnostics.length > 0 && <Badge tone="danger">{diagnostics.length} ошибка</Badge>}<Button className="experiment-console__close" size="sm" onClick={() => void closeConsole()}><X size={13} /> Закрыть</Button></header><div className="experiment-console__output">{diagnostics.map((item) => <button key={item.id} onClick={() => editorRef.current?.openDiagnostic(item)}><AlertTriangle size={14} /><span><strong>{item.message}</strong><small>{item.path}:{item.line}:{item.column}</small></span><ChevronRight size={15} /></button>)}{interactiveRun && <ConsoleTranscript sessionId={interactiveRun.sessionId} stdout={interactiveRun.stdout} inputs={consoleInputs} />}{interactiveRun?.stderr && <pre className="is-stderr">{interactiveRun.stderr}</pre>}{interactiveRun?.outputTruncated && <p>Вывод остановлен: достигнут установленный лимит.</p>}{!interactiveRun && <p>Нажмите «Запустить». Если программа запросит данные, введите одну строку ниже и нажмите Enter.</p>}{interactiveRun?.terminal && !interactiveRun.stdout && !interactiveRun.stderr && consoleInputs.length === 0 && diagnostics.length === 0 && <p>Программа завершена без вывода.</p>}</div><form className="experiment-console__input" onSubmit={(event) => { event.preventDefault(); void sendExperimentInput(); }}><input aria-label="Ввод программы" value={interactiveInput} onChange={(event) => setInteractiveInput(event.target.value)} disabled={!interactiveInputOpen} maxLength={65_536} autoComplete="off" spellCheck={false} placeholder={interactiveRun?.terminal ? 'Программа завершена' : interactiveInputOpen ? 'Введите строку и нажмите Enter' : 'Сначала запустите программу'} /><button type="submit" aria-label="Передать строку программе" disabled={!interactiveInputOpen || inputSending}><Send size={14} /> Отправить</button></form></div>}
    </section>{reviewPanelOpen ? <aside className="review-side"><div className="review-side__top"><button type="button" className="review-side__collapse" aria-label="Скрыть панель проверки" title="Скрыть панель проверки" onClick={() => setReviewPanelOpen(false)}><PanelRightClose size={16} /></button><div className="evidence-tabs" role="tablist" aria-label="Материалы проверки">{(['task', 'integrity', 'history', 'ai'] as EvidenceTab[]).map((id) => <button key={id} type="button" role="tab" aria-selected={evidenceTab === id} className={evidenceTab === id ? 'is-active' : ''} onClick={() => setEvidenceTab(id)}>{id === 'task' ? 'Задание' : id === 'integrity' ? 'Плагиат' : id === 'history' ? 'История' : 'ИИ'}</button>)}</div></div><div className="evidence-body">{Boolean(submission.sourceWarnings?.length) && <div className="review-source-warning" role="alert"><strong><AlertTriangle size={16} /> Сдача загружена не полностью</strong>{submission.sourceWarnings!.map((warning) => <div key={`${warning.code}:${warning.message}`}><p>{warning.message} Код: {warning.code}.</p>{warning.moodleUrl && <a href={warning.moodleUrl} target="_blank" rel="noopener noreferrer">Открыть ответ в Moodle</a>}</div>)}</div>}<EvidencePanel tab={evidenceTab} submission={submission} courseId={courseId} teacherComment={comment} canUseDecisionSupport={canUseDecisionSupport} canAskTeacherAi={canAskTeacherAi} reviewRequired={reviewRequired} decisionSupportEnabled={decisionSupportEnabled} /></div>
      <div className="grading-panel"><header><div><span className="eyebrow">Решение преподавателя</span><h2>Оценка и комментарий</h2></div><span>{reviewedReadOnly ? 'Утверждено' : canReview ? 'Черновик' : 'Только чтение'}</span></header>
        {reviewedReadOnly && <div className="review-decision-meta"><CheckCircle2 size={16} /><span><strong>{finalDecision?.reviewerName ?? 'Преподаватель'}</strong><small>{finalDecision?.reviewedAt ? formatDate(finalDecision.reviewedAt) : 'Дата решения не предоставлена'}{finalDecision?.revision ? ` · версия ${finalDecision.revision}` : ''}{submission.decisionHistory.length > 1 ? ` · решений в истории: ${submission.decisionHistory.length}` : ''}</small></span>{finalDecision?.lmsExportState && <Badge tone={finalDecision.lmsExportState === 'DELIVERED' ? 'success' : finalDecision.lmsExportState === 'BLOCKED' || finalDecision.lmsExportState === 'FAILED' ? 'danger' : 'neutral'}>{lmsExportStateLabel(finalDecision.lmsExportState)}</Badge>}</div>}
        <div className="grade-input"><Field label="Итоговый балл"><div><input type="number" min="0" max={submission.maxScore} disabled={!canReview} value={grade} onChange={(event) => { setGrade(event.target.value); setReviewDirty(true); }} /><span>/ {submission.maxScore}</span></div></Field></div><p className="no-recommendation">Оценка определяется преподавателем по доступным проверкам в приложении.</p><Field label={reviewedReadOnly ? 'Финальный комментарий студенту' : 'Комментарий студенту'}><textarea rows={8} disabled={!canReview} value={comment} onChange={(event) => { setComment(event.target.value); setReviewDirty(true); }} placeholder={reviewedReadOnly ? 'Комментарий не добавлен' : 'Объясните сильные стороны и что стоит исправить…'} /></Field>{canReview && <footer><Button variant="secondary" disabled={!validDraft || !reviewDirty || Boolean(switchingSubmissionId)} onClick={() => void saveDraft()}><Save size={15} /> Сохранить</Button><Button onClick={() => setDecisionOpen(true)} disabled={!validGrade}>Утвердить <ChevronRight size={15} /></Button></footer>}</div>
    </aside> : <aside className="review-side-rail" aria-label="Панель проверки скрыта"><button type="button" aria-label="Показать панель проверки" title="Показать панель проверки" onClick={() => setReviewPanelOpen(true)}><PanelRightOpen size={17} /></button><span aria-hidden="true">Проверка</span></aside>}</div>
    <Modal open={decisionOpen} title="Утвердить решение" onClose={() => setDecisionOpen(false)} footer={<><Button variant="ghost" onClick={() => setDecisionOpen(false)}>Отмена</Button><Button loading={finalizing} onClick={() => void finalize()}><CheckCircle2 size={16} /> Утвердить и отправить</Button></>}><div className="decision-summary"><span className="decision-grade">{grade}<small>/{submission.maxScore}</small></span><div><strong>{submission.studentName}</strong><p>{comment || 'Комментарий не добавлен'}</p></div></div><p className="modal-copy"><Info size={16} /> Финальное решение снимет закрепление работы. Сервер вернёт фактический статус LMS-выгрузки; без сопоставления с заданием Moodle она может быть заблокирована.</p></Modal>
  </div>;
}

function groupedReviewItems(submission: Submission): SubmissionReviewGroupItem[] {
  const items = submission.reviewGroup?.items ?? [];
  if (items.length < 2 || !items.some((item) => item.submissionId === submission.id)) return [];
  return items.map((item) => item.submissionId === submission.id ? {
    ...item,
    title: item.title || submission.assessmentTitle,
    score: item.score ?? submission.score,
    maxScore: item.maxScore ?? submission.maxScore,
    status: item.status ?? submission.status,
  } : item);
}

function reviewGroupScoreLabel(item: SubmissionReviewGroupItem): string {
  const score = item.score === undefined ? '—' : item.score.toLocaleString('ru-RU');
  if (item.maxScore !== undefined) return `Балл: ${score} / ${item.maxScore.toLocaleString('ru-RU')}`;
  if (item.score !== undefined) return `Балл: ${score}`;
  return 'Балл не указан';
}

function reviewGroupStatusPresentation(status?: SubmissionReviewGroupItem['status']) {
  if (status === 'GRADED') return { label: 'Проверено', tone: 'success' };
  if (status === 'CLAIMED') return { label: 'На проверке', tone: 'info' };
  if (status === 'CONFLICT') return { label: 'Конфликт', tone: 'danger' };
  if (status === 'UNGRADED') return { label: 'Ожидает проверки', tone: 'warning' };
  return { label: 'Статус не указан', tone: 'neutral' };
}

interface StoredInteractiveExperimentSession {
  experimentId: string;
  sessionId: string;
  sandboxMode?: boolean;
}

function interactiveExperimentStorageKey(submissionId: string): string {
  return `eduprog:interactive-review:${submissionId}`;
}

function readInteractiveExperimentSession(submissionId: string): StoredInteractiveExperimentSession | null {
  const raw = window.sessionStorage.getItem(interactiveExperimentStorageKey(submissionId));
  if (!raw) return null;
  try {
    const stored = JSON.parse(raw) as Partial<StoredInteractiveExperimentSession>;
    if (typeof stored.experimentId === 'string' && stored.experimentId && typeof stored.sessionId === 'string' && stored.sessionId) {
      return {
        experimentId: stored.experimentId,
        sessionId: stored.sessionId,
        sandboxMode: typeof stored.sandboxMode === 'boolean' ? stored.sandboxMode : undefined,
      };
    }
  } catch {
    // A malformed or legacy value cannot identify a safe session to reconnect.
  }
  window.sessionStorage.removeItem(interactiveExperimentStorageKey(submissionId));
  return null;
}

function rememberInteractiveExperimentSession(submissionId: string, experimentId: string, sessionId: string, sandboxMode: boolean): void {
  try {
    window.sessionStorage.setItem(
      interactiveExperimentStorageKey(submissionId),
      JSON.stringify({ experimentId, sessionId, sandboxMode } satisfies StoredInteractiveExperimentSession),
    );
  } catch { /* Storage may be unavailable in privacy mode; the live run still works. */ }
}

function markMoodleImportNoticeShown(submissionId: string): boolean {
  try {
    const key = `eduprog:moodle-import-notice:${submissionId}`;
    if (window.sessionStorage.getItem(key) === '1') return false;
    window.sessionStorage.setItem(key, '1');
    return true;
  } catch {
    return true;
  }
}

function clearInteractiveExperimentSession(submissionId: string, experimentId?: string): void {
  if (!experimentId) {
    window.sessionStorage.removeItem(interactiveExperimentStorageKey(submissionId));
    return;
  }
  const stored = readInteractiveExperimentSession(submissionId);
  if (!stored || stored.experimentId === experimentId) {
    window.sessionStorage.removeItem(interactiveExperimentStorageKey(submissionId));
  }
}

function EvidencePanel({ tab, submission, courseId, teacherComment, canUseDecisionSupport, canAskTeacherAi, decisionSupportEnabled }: {
  tab: EvidenceTab; submission: Submission; courseId: string; canUseDecisionSupport: boolean; canAskTeacherAi: boolean;
  reviewRequired: boolean; decisionSupportEnabled: boolean; teacherComment: string;
}) {
  if (tab === 'task') return <section className="review-task" aria-label="Задание студента">
    <h3>{submission.assessmentTitle}</h3>
    <p className="review-task__statement">{submission.taskStatement?.trim() || 'Текст задания не был получен из Moodle. Синхронизируйте работу или посмотрите условие в Moodle.'}</p>
  </section>;
  if (tab === 'integrity') return <IntegrityPanel submission={submission} canReview={canUseDecisionSupport} decisionSupportEnabled={decisionSupportEnabled} />;
  if (tab === 'history') return <ReviewHistoryPanel submission={submission} />;
  return <TeacherAiPanel key={submission.id} submissionId={submission.id} courseId={courseId} canSend={canAskTeacherAi} teacherComment={teacherComment} />;
}

function ReviewHistoryPanel({ submission }: { submission: Submission }) {
  return <div className="review-history-panel">
    <section className="review-decision-history">
      <header><CheckCircle2 size={15} /><span><strong>История решений</strong><small>Предыдущие проверки сохраняются после перепроверки</small></span></header>
      {submission.decisionHistory.length ? submission.decisionHistory.map((decision, index) => <article key={decision.id ?? `${decision.revision}-${decision.reviewedAt}-${index}`}>
        <div><span><strong>{decision.grade}/{submission.maxScore}</strong><small>версия {decision.revision}</small></span><Badge tone={index === 0 ? 'success' : 'neutral'}>{index === 0 ? 'Текущее' : 'Предыдущее'}</Badge></div>
        <p>{decision.comment || 'Комментарий не добавлен'}</p>
        <footer><span>{decision.reviewerName}</span><span>{decision.reviewedAt ? formatDate(decision.reviewedAt) : 'Дата не предоставлена'}</span><span>{lmsExportStateLabel(decision.lmsExportState)}</span></footer>
      </article>) : <p className="review-history-empty">Решений преподавателя пока нет.</p>}
    </section>
    <section className="review-edit-history">
      <header><History size={15} /><span><strong>История написания</strong><small>События до неизменяемого снимка сдачи</small></span></header>
      <div className="review-history">{submission.history.length ? submission.history.map((event) => <div key={event.id}><span><History /></span><p><strong>{event.label}</strong><small>{[event.detail, formatClientContext(event.client), formatDate(event.at)].filter(Boolean).join(' · ')}</small></p></div>) : <p className="review-history-empty">События редактирования не предоставлены.</p>}</div>
    </section>
  </div>;
}


function IntegrityPanel({ submission, canReview, decisionSupportEnabled }: { submission: Submission; canReview: boolean; decisionSupportEnabled: boolean }) {
  const [authorship, setAuthorship] = useState<AuthorshipAnalysis[]>([]);
  const [similarity, setSimilarity] = useState<SimilarityAnalysis[]>([]);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState<'authorship' | 'similarity' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const location = useLocation();

  const load = useCallback(async () => {
    setLoading(true);
    const [authorshipResult, similarityResult] = await Promise.allSettled([
      api.getAuthorshipAnalyses(submission.id), api.getSimilarityAnalyses(submission.assessmentId),
    ]);
    if (authorshipResult.status === 'fulfilled') setAuthorship(authorshipResult.value);
    if (similarityResult.status === 'fulfilled') setSimilarity(similarityResult.value);
    const failed = [authorshipResult, similarityResult].find((result) => result.status === 'rejected');
    setError(failed?.status === 'rejected' ? (failed.reason instanceof Error ? failed.reason.message : 'Часть результатов анализа недоступна') : null);
    setLoading(false);
  }, [submission.id, submission.assessmentId]);

  useEffect(() => { void load(); }, [load]);

  async function launch(kind: 'authorship' | 'similarity') {
    if (!canReview || running) return;
    setRunning(kind); setError(null);
    try {
      if (kind === 'authorship') {
        const created = await api.createAuthorshipAnalysis(submission.id);
        setAuthorship((items) => [created, ...items.filter((item) => item.id !== created.id)]);
      } else {
        const created = await api.createSimilarityAnalysis(submission.assessmentId, submission.taskVersionId);
        setSimilarity((items) => [created, ...items.filter((item) => item.id !== created.id)]);
      }
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Анализ не запущен'); }
    finally { setRunning(null); }
  }

  const author = authorship[0];
  const similarityJob = similarity.find((item) => item.state === 'COMPLETED') ?? similarity[0];
  const authorResult = author?.result;
  const calibrated = Boolean(authorResult?.analyzer && authorResult.model && Object.keys(authorResult.calibration).length);
  const probability = authorResult?.probability ?? author?.probability;
  const relatedMatches = similarityJob?.matches.filter((match) => match.submissionA === submission.id || match.submissionB === submission.id) ?? [];
  const rankedMatches = [...relatedMatches].sort((left, right) => right.score - left.score);

  if (loading) return <div className="evidence-loading"><RefreshCcw size={17} /> Получаем результаты серверных проверок…</div>;
  const origin = submission.originVerification;
  const originState = origin?.state ?? 'UNAVAILABLE';
  const originTitle = originState === 'VERIFIED'
    ? 'Ответ подтверждён'
    : originState === 'EXTERNAL_ORIGIN'
      ? 'Работа сдана напрямую через LMS'
      : originState === 'MISMATCH'
        ? 'Ответ в LMS был изменён'
        : originState === 'PENDING'
          ? 'Ожидается подтверждение LMS'
          : 'Сравнение пока недоступно';
  return <div className="integrity-panel">
    {!decisionSupportEnabled && <p className="evidence-policy-note"><Info size={14} /> СППР отключена в настройках работы. Сохранённые результаты доступны только для чтения.</p>}
    <section className={cn('origin-verification', `origin-verification--${originState.toLowerCase()}`)}>
      {originState === 'VERIFIED' ? <CheckCircle2 /> : originState === 'MISMATCH' ? <AlertTriangle /> : <Info />}
      <p><strong>{originTitle}</strong><span>{origin?.message ?? 'Сервер ещё не получил достаточно данных для сравнения ответа.'}</span></p>
    </section>
    <EvidenceRow title="История написания" value={submission.history.length ? `${submission.history.length} событий; автоматический вывод не сформирован` : 'События не предоставлены'} tone="neutral" />
    <section className="analysis-card">
      <header><div><strong>Анализ процесса написания</strong><small>{author ? `Состояние: ${author.state}${author.completedAt ? ` · ${formatDate(author.completedAt)}` : ''}` : 'Ещё не запускался'}</small></div><Button variant="secondary" loading={running === 'authorship'} disabled={!canReview || running !== null} onClick={() => void launch('authorship')}>{author ? 'Запустить снова' : 'Запустить'}</Button></header>
      {author?.state === 'COMPLETED' && calibrated && probability !== undefined ? <div className="analysis-result"><strong>{formatPercent(probability)}</strong><span>вероятность согласованности процесса с самостоятельным написанием</span><dl><div><dt>Неопределённость</dt><dd>{authorResult?.uncertainty === undefined ? 'не передана' : formatPercent(authorResult.uncertainty)}</dd></div><div><dt>Анализатор</dt><dd>{authorResult?.analyzer} · {authorResult?.model}</dd></div><div><dt>Калибровка</dt><dd>{calibrationLabel(authorResult?.calibration ?? {})}</dd></div></dl></div> : author ? <p className="analysis-empty">{author.error || (author.state === 'COMPLETED' ? 'Числовой результат скрыт: сервер не передал анализатор, модель и калибровку.' : 'Результат ещё не готов или анализ завершился без числового вывода.')}</p> : <p className="analysis-empty">Серверный анализ не запускался. UI не подставляет вероятность по умолчанию.</p>}
      {authorResult?.warnings.map((warning) => <p className="analysis-warning" key={warning}><AlertTriangle size={14} /> {warning}</p>)}
    </section>
    <section className="analysis-card">
      <header><div><strong>Сходство решений</strong><small>{similarityJob ? `Состояние: ${similarityJob.state}${similarityJob.algorithmVersion ? ` · ${similarityJob.algorithmVersion}` : ''}` : 'Ещё не запускалось'}</small></div><Button variant="secondary" loading={running === 'similarity'} disabled={!canReview || running !== null} onClick={() => void launch('similarity')}>{similarityJob ? 'Запустить снова' : 'Запустить'}</Button></header>
      {similarityJob?.state === 'COMPLETED' ? rankedMatches.length ? <div className="similarity-list">{rankedMatches.slice(0, 5).map((match) => <Link key={match.id} to={`/review/${submission.id}/similarity/${match.id}`} state={{ from: `${location.pathname}${location.search}` }}><strong>{formatPercent(match.score)}</strong><span>Связана сдача {otherSubmission(match, submission.id)}<small>{match.sharedFingerprintCount} общих отпечатков · сравнить код</small></span><ChevronRight size={15} /></Link>)}</div> : <p className="analysis-empty">Для этой сдачи сервер не вернул совпадений. Всего сравнений: {similarityJob.comparisonCount}.</p> : <p className="analysis-empty">{similarityJob?.error || 'Результаты появятся после серверного анализа; значение сходства не вычисляется в браузере.'}</p>}
    </section>
    {error && <InlineError message={error} retry={() => void load()} />}
    <div className="integrity-warning"><AlertTriangle /><p><strong>Это предварительная проверка, а не конечное решение</strong>Вероятность и сходство требуют ручной интерпретации. Результаты не меняют оценку и не создают санкции автоматически.</p></div>
  </div>;
}

function formatPercent(value: number): string {
  const normalized = value > 1 ? value : value * 100;
  return `${normalized.toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%`;
}
function calibrationLabel(value: Record<string, unknown>): string {
  const label = value.version ?? value.name ?? value.id;
  return label ? String(label) : 'метаданные переданы';
}
function otherSubmission(match: SimilarityAnalysis['matches'][number], currentId: string): string {
  const id = match.submissionA === currentId ? match.submissionB : match.submissionA;
  return id ? `…${id.slice(-8)}` : 'без идентификатора';
}

function EvidenceRow({ title, value, tone }: { title: string; value: string; tone: 'success' | 'warning' | 'neutral' }) { return <div className="evidence-row"><span className={tone === 'neutral' ? 'status-dot' : `status-dot status-dot--${tone === 'success' ? 'ok' : 'warning'}`} /><span><strong>{title}</strong><small>{value}</small></span><ChevronRight /></div>; }
function EvidenceEmpty({ title }: { title: string }) { return <div className="evidence-empty"><Info /><strong>{title}</strong><p>Когда сервер вернёт результаты проверки с версией инструмента, они появятся здесь.</p></div>; }
function selectDiffFiles(originalFiles: WorkspaceFile[], modifiedFiles: WorkspaceFile[], activeFileId: string): {
  original?: WorkspaceFile; modified?: WorkspaceFile; path: string;
} {
  const active = modifiedFiles.find((file) => file.id === activeFileId)
    ?? originalFiles.find((file) => file.id === activeFileId)
    ?? modifiedFiles[0]
    ?? originalFiles[0];
  if (!active) return { path: 'файл не выбран' };
  const original = originalFiles.find((file) => file.path === active.path)
    ?? originalFiles.find((file) => file.id === activeFileId);
  const modified = modifiedFiles.find((file) => file.path === active.path)
    ?? modifiedFiles.find((file) => file.id === activeFileId);
  return { original, modified, path: active.path };
}

function DiffWorkspace({ original, modified, path, theme }: { original: string; modified: string; path: string; theme: 'light' | 'dark' }) { return <div className="diff-workspace"><header><span>Оригинал студента · {path}</span><span>Преподавательская копия · {path}</span></header><DiffEditor height="100%" original={original} modified={modified} language="cpp" theme={theme === 'dark' ? 'eduprog-dark' : 'eduprog-light'} options={{ readOnly: true, renderSideBySide: true, automaticLayout: true, minimap: { enabled: false }, fontSize: 13, lineHeight: 22 }} /></div>; }

function TeacherAiPanel({ submissionId, courseId, canSend, teacherComment }: { submissionId: string; courseId: string; canSend: boolean; teacherComment: string }) {
  const [opened, setOpened] = useState(false);
  const [message, setMessage] = useState('');
  const [sending, setSending] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [messages, setMessages] = useState<Array<{ from: 'user' | 'ai'; text: string; citations?: Array<{ title: string; url: string }> }>>([]);
  const threadRef = useRef<string>();
  const mountedRef = useRef(false);
  useEffect(() => { mountedRef.current = true; return () => { mountedRef.current = false; }; }, []);
  async function openChat() {
    setOpened(true); setLoadingHistory(true);
    try {
      const threads = await api.getTeacherAiThreads(courseId);
      if (!mountedRef.current) return;
      const related = threads.filter((thread) => thread.submissionId === submissionId);
      const existing = related.find((thread) => thread.status === 'OPEN') ?? related[0];
      if (existing) {
        threadRef.current = existing.id;
        const stored = await api.getAiMessages(existing.id);
        if (!mountedRef.current) return;
        setMessages(stored.map((item) => ({ from: item.from, text: item.content, citations: item.citations })));
      }
    } catch (caught) {
      if (!mountedRef.current) return;
      setMessages([{ from: 'ai', text: caught instanceof Error ? `Историю чата загрузить не удалось: ${caught.message}` : 'Историю чата загрузить не удалось.' }]);
    } finally { if (mountedRef.current) setLoadingHistory(false); }
  }
  async function send() {
    const content = message.trim();
    if (!canSend || !content || sending) return;
    setMessage(''); setMessages((items) => [...items, { from: 'user', text: content }]); setSending(true);
    try {
      if (!threadRef.current) threadRef.current = (await api.createTeacherAiThread(submissionId, courseId)).id;
      if (!mountedRef.current) return;
      const response = await api.sendAiMessage(threadRef.current, content, { teacherComment });
      if (!mountedRef.current) return;
      setMessages((items) => [...items, { from: 'ai', text: response.content, citations: response.citations }]);
    } catch (caught) {
      if (!mountedRef.current) return;
      setMessages((items) => [...items, { from: 'ai', text: caught instanceof Error ? `Ошибка: ${caught.message}` : 'Помощник недоступен' }]);
    } finally { if (mountedRef.current) setSending(false); }
  }
  if (!opened) return <div className="teacher-ai"><span><Bot /></span><h3>{canSend ? 'Спросить о работе' : 'История чата по работе'}</h3><p>{canSend ? 'Вопрос получит контекст открытой задачи: условие, все файлы сдачи студента и ваш комментарий, если он есть. Чат не сохраняет комментарий и не меняет оценку.' : 'Сохранённый диалог доступен для аудита. Новые сообщения доступны преподавателю в его области проверки при включённой СППР.'}</p><button onClick={() => void openChat()}><MessageSquareText size={16} /> Открыть чат</button><small>{canSend ? 'Для вопросов закреплять работу за собой не нужно. Диалог не изменяет утверждённую оценку.' : 'Режим только для чтения.'}</small></div>;
  return <div className="teacher-ai-chat"><header><Bot size={16} /><span><strong>Помощник проверки</strong><small>{canSend ? 'Условие и код выбранной задачи' : 'История · только чтение'}</small></span></header><div>{loadingHistory ? <p>Загружаю историю…</p> : messages.length ? messages.map((item, index) => <div className={cn('teacher-ai-message', item.from === 'user' && 'is-user')} key={index}><p>{item.text}</p>{item.citations?.map((citation) => <a key={citation.url} href={citation.url} target="_blank" rel="noreferrer">{citation.title}</a>)}</div>) : !canSend && <p>Сохранённых сообщений по этой работе нет.</p>}{sending && <p>Анализирую…</p>}</div><form onSubmit={(event) => { event.preventDefault(); void send(); }}><input value={message} disabled={!canSend || sending || loadingHistory} onChange={(event) => setMessage(event.target.value)} placeholder={canSend ? 'Вопрос о текущем коде…' : 'Новые сообщения недоступны'} /><button disabled={!canSend || !message.trim() || sending || loadingHistory} aria-label="Отправить"><Send size={14} /></button></form></div>;
}

function interactiveStatusLabel(run: InteractiveRun): string {
  const labels: Record<InteractiveRun['status'], string> = {
    RUNNING: 'Выполняется · ожидает ввод при необходимости',
    SUCCESS: `Завершена${run.exitCode === undefined ? '' : ` · код ${run.exitCode}`}`,
    COMPILE_ERROR: 'Ошибка компиляции',
    RUNTIME_ERROR: `Ошибка выполнения${run.exitCode === undefined ? '' : ` · код ${run.exitCode}`}`,
    TIME_LIMIT: 'Остановлена по лимиту времени',
    MEMORY_LIMIT: 'Остановлена по лимиту памяти',
    OUTPUT_LIMIT: 'Остановлена по лимиту вывода',
    WORKSPACE_LIMIT: 'Остановлена по лимиту файлов',
    STOPPED: 'Остановлена преподавателем',
    INFRA_ERROR: 'Сервис запуска недоступен',
  };
  return labels[run.status];
}
