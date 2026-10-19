import { AlertTriangle, Bot } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api';
import { cn } from '../lib/utils';
import type { AiCodeFinding, AiCodeReview } from '../types';
import { Button } from './ui';
import './aiCodeReview.css';

/** Analysis is optional ancillary data: never block code, grading, or navigation. */
export function useAiCodeReview(submissionId: string | undefined) {
  const [report, setReport] = useState<AiCodeReview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const startRef = useRef<() => Promise<string | null>>();

  useEffect(() => {
    setReport(null);
    setError(null);
    setStarting(false);
    if (!submissionId) return;
    let cancelled = false;
    let timer = 0;
    let generation = 0;
    let launching = false;
    let current: AiCodeReview | null = null;
    const update = (value: AiCodeReview | null) => { current = value; setReport(value); };
    function schedule(delay = current && ['QUEUED', 'RUNNING'].includes(current.state) ? 2000 : 30_000) {
      window.clearTimeout(timer);
      if (!cancelled) timer = window.setTimeout(() => { void refresh(); }, delay);
    }
    async function refresh() {
      if (cancelled || launching) return;
      const version = ++generation;
      try {
        const loaded = await api.getAiCodeReview(submissionId!);
        if (cancelled || version !== generation) return;
        setError(null);
        update(loaded);
      } catch (caught) {
        if (cancelled || version !== generation) return;
        setError(caught instanceof Error ? caught.message : 'Не удалось получить ИИ-анализ.');
        // A lost request must not leave stale annotations visible after access revocation.
        update(null);
      } finally {
        if (!cancelled && version === generation) schedule();
      }
    }
    startRef.current = async () => {
      if (cancelled || launching || !current?.enabled || !['NOT_STARTED', 'FAILED'].includes(current.state)) return null;
      launching = true;
      ++generation; // An older poll must not overwrite the queue response.
      window.clearTimeout(timer);
      setStarting(true); setError(null);
      let failed = false;
      try {
        const queued = await api.startAiCodeReview(submissionId, current.state === 'FAILED');
        if (cancelled) return null;
        update(queued);
        return queued.state === 'UNAVAILABLE' ? queued.error || 'ИИ-анализ недоступен.' : null;
      } catch (caught) {
        if (cancelled) return null;
        failed = true;
        const message = caught instanceof Error ? caught.message : 'Не удалось запустить ИИ-анализ.';
        setError(message);
        return message;
      } finally {
        launching = false;
        if (!cancelled) {
          setStarting(false);
          // The POST may have succeeded even if its response was lost. Only
          // read the status next; never retry a paid analysis automatically.
          schedule(failed ? 2000 : undefined);
        }
      }
    };
    void refresh();
    return () => { cancelled = true; ++generation; startRef.current = undefined; window.clearTimeout(timer); };
  }, [submissionId]);

  const start = () => startRef.current?.() ?? Promise.resolve(null);
  return { report, error, starting, start, retry: () => { void start(); } };
}

export function AiReviewIndicator({ report, onOpen }: { report: AiCodeReview | null; onOpen(): void }) {
  if (!report?.enabled) return null;
  const label = report.state === 'COMPLETED' ? `ИИ: замечаний ${report.findings.length}`
    : report.state === 'NOT_STARTED' ? 'ИИ: не запускался'
      : ['QUEUED', 'RUNNING'].includes(report.state) ? 'ИИ: анализ…'
      : 'ИИ: анализ недоступен';
  return <button className="ai-review-indicator" type="button" onClick={onOpen} title="Открыть рекомендации ИИ"><Bot size={14} />{label}</button>;
}

export function AiReviewFeedback({ report, error, starting, canRetry, onRetry }: {
  report: AiCodeReview | null; error: string | null; starting: boolean;
  canRetry: boolean; onRetry(): void;
}) {
  const attempts = report?.attemptCount ?? 0;
  const maximum = report?.maxAttempts ?? 3;
  const busy = starting || report?.state === 'QUEUED' || report?.state === 'RUNNING';
  if (busy) return <section className="ai-review-feedback" role="status" aria-live="polite">
    <strong><Bot size={16} /> ИИ-проверка выполняется</strong>
    <p>{starting ? 'Отправляем работу на проверку.' : report?.state === 'RUNNING'
      ? `Попытка ${Math.max(1, attempts)} из ${maximum}. Ожидаем ответ ИИ.`
      : attempts > 0 ? `Готовится повторная попытка ${Math.min(attempts + 1, maximum)} из ${maximum}.`
        : 'Работа в очереди на проверку.'}</p>
  </section>;
  if (!error && report?.state !== 'FAILED') return null;
  return <section className="ai-review-feedback ai-review-feedback--error" role="alert">
    <strong><AlertTriangle size={16} /> {error ? 'Не удалось получить состояние ИИ-проверки' : 'ИИ-проверка не выполнена'}</strong>
    {!error && attempts > 0 && <p>{attempts >= maximum ? 'Все попытки исчерпаны.' : 'Проверка остановлена.'} Выполнено: {attempts} из {maximum}.</p>}
    <p>{error || report?.error || 'Не удалось получить результат анализа.'}</p>
    <small>Оценка и комментарий преподавателя не изменены.</small>
    {!error && report?.enabled && report.state === 'FAILED' && <Button size="sm" variant="secondary" disabled={!canRetry} onClick={onRetry}>Повторить ИИ-проверку</Button>}
  </section>;
}

export function AiReviewRecommendation({ report, maxScore, canApprove, onApprove, applying = false }: {
  report: AiCodeReview | null; maxScore: number; canApprove: boolean; onApprove(): void; applying?: boolean;
}) {
  if (!report?.enabled || report.state !== 'COMPLETED' || report.recommendationStatus === 'DISCARDED') return null;
  const grade = report.recommendedGrade;
  const validGrade = grade != null && Number.isFinite(grade) && grade >= 0 && grade <= maxScore;
  return <section className="ai-review-recommendation" aria-label="Ответ ИИ">
    <header><strong><Bot size={15} /> Ответ ИИ:</strong><span>{validGrade ? `${grade}/${maxScore}` : 'Без балла'}</span></header>
    <p>{report.recommendedComment || report.summary}</p>
    <small>{report.recommendationStatus === 'APPROVED'
      ? 'На основе рекомендации утверждено решение преподавателя. Статус отправки указан у основного решения.'
      : `Кнопка подставит балл и комментарий в поля формы. Отправка в Moodle — только через «Утвердить». ${report.automatic
        ? 'При выходе отменяется только неутверждённый ИИ-черновик в форме. Ответ автоматической проверки и подсветка замечаний сохраняются.'
        : 'При выходе без утверждения ИИ-черновик и рекомендация ручной проверки отменяются.'}`}</small>
    {report.id && report.recommendationStatus === 'PENDING' && validGrade && report.recommendedComment?.trim() && <Button size="sm" variant="secondary" disabled={!canApprove} loading={applying} onClick={onApprove}>Подставить оценку и комментарий</Button>}
  </section>;
}

export function AiHighlightsToggle({ report, checked, onChange, onStart, starting = false, error, originalVisible }: {
  report: AiCodeReview | null; checked: boolean; onChange(value: boolean): void; originalVisible: boolean;
  onStart(): void; starting?: boolean; error?: string | null;
}) {
  const completed = Boolean(report?.enabled && report.state === 'COMPLETED');
  const busy = starting || report?.state === 'RUNNING' || report?.state === 'QUEUED';
  const canStart = Boolean(originalVisible && !busy && report?.enabled && ['NOT_STARTED', 'FAILED'].includes(report.state));
  const maximum = report?.maxAttempts ?? 3;
  const attempt = report?.state === 'QUEUED' ? (report.attemptCount ?? 0) + 1 : Math.max(1, report?.attemptCount ?? 0);
  const title = !originalVisible ? 'Подсветка доступна в оригинале сдачи'
    : starting ? 'Отправляем работу на ИИ-проверку. Повторный запуск недоступен.'
      : report?.state === 'QUEUED' ? 'Работа в очереди на ИИ-проверку. Результат появится автоматически.'
        : report?.state === 'RUNNING' ? 'ИИ проверяет работу. Результат появится автоматически.'
          : completed ? `Показать или скрыть все замечания ИИ (${report?.findings.length ?? 0})`
            : canStart ? (error || report?.state === 'FAILED'
              ? `${error || report?.error || 'ИИ-проверка не завершена.'} Нажмите, чтобы повторить проверку через ИИ.`
              : 'Работа ещё не проверялась ИИ. Нажмите для проверки через ИИ.')
              : error || report?.error || (!report ? 'Получаем состояние ИИ-проверки…' : 'ИИ-проверка сейчас недоступна.');
  if (!completed) return <span title={title} className="ai-review-launch-tooltip">
    <button type="button" className={cn('ai-review-launch', canStart && 'ai-review-launch--ready')} disabled={!canStart}
      aria-label={busy ? 'ИИ-проверка выполняется' : 'Проверить работу через ИИ'} aria-busy={busy} onClick={onStart}>
      <Bot size={14} />
      <span>{starting ? 'Запуск ИИ…' : report?.state === 'QUEUED' ? (report.attemptCount ? `Повтор ИИ ${Math.min(attempt, maximum)}/${maximum}…` : 'В очереди ИИ') : report?.state === 'RUNNING' ? `Проверка ИИ ${attempt}/${maximum}…` : report?.state === 'FAILED' ? 'Повторить ИИ' : 'Проверить ИИ'}</span>
    </button>
  </span>;
  return <label className="ai-highlights-toggle" title={title}>
    <input type="checkbox" role="switch" aria-label="Подсветка замечаний ИИ" checked={originalVisible && checked} disabled={!originalVisible} onChange={(event) => onChange(event.target.checked)} />
    <span className="ai-highlights-toggle__track" aria-hidden="true" /><Bot size={14} /><span>Замечания ИИ</span>
  </label>;
}

export function AiCodeReviewPanel({ report, error, highlights, onHighlights, onFinding, onRetry, originalVisible }: {
  report: AiCodeReview | null;
  error: string | null;
  highlights: boolean;
  onHighlights(value: boolean): void;
  onFinding(finding: AiCodeFinding): void;
  onRetry(): void;
  originalVisible: boolean;
}) {
  return <section className="ai-code-review" aria-label="Автоматическая ИИ-проверка">
    <h3><Bot size={18} /> Автоматическая ИИ-проверка</h3>
    {error ? <div role="alert"><p>{error}</p><Button size="sm" variant="secondary" onClick={onRetry}>Обновить ИИ-анализ</Button></div>
      : !report ? <p role="status">Получаем состояние анализа…</p>
        : !report.enabled || report.state === 'DISABLED' ? <p>{report.error || 'Выключена для этой работы. Администратор может включить её в настройках задания.'}</p>
          : report.state === 'NOT_STARTED' ? <p>Для этой сдачи анализ не запускался. Уже оценённые работы не отправляются на автоматическую проверку задним числом.</p>
            : report.state === 'QUEUED' ? <p role="status">ИИ-анализ поставлен в очередь. Можно продолжать проверку.</p>
            : report.state === 'RUNNING' ? <p role="status">ИИ анализирует условие и сданный код. Можно продолжать проверку.</p>
            : report.state === 'UNAVAILABLE' ? <p>{report.error || 'ИИ недоступен. Проверьте глобальные настройки и подключение провайдера.'}</p>
              : report.state === 'FAILED' ? <div role="alert"><p>{report.error || 'ИИ-анализ не выполнен. Проверку работы можно продолжать.'}</p><Button size="sm" variant="secondary" onClick={onRetry}>Повторить ИИ-анализ</Button></div>
                : <>
                  <p className="ai-code-review__summary">{report.summary}</p>
                  {report.findings.length > 0 ? <>
                    <label className="ai-code-review__toggle"><input type="checkbox" checked={highlights} onChange={(event) => onHighlights(event.target.checked)} /> Подсвечивать замечания ИИ в коде</label>
                    {!originalVisible && <p>Подсветка доступна в оригинале сдачи, а не в песочнице.</p>}
                    <ul>{report.findings.map((finding, index) => <li key={`${finding.path}:${finding.line}:${index}`}>
                      <button type="button" onClick={() => onFinding(finding)}><strong>{finding.path}:{finding.line}{finding.endLine && finding.endLine !== finding.line ? `–${finding.endLine}` : ''}</strong><span>{finding.message}</span></button>
                    </li>)}</ul>
                  </> : <p>ИИ не указал замечаний к строкам кода. Это не гарантирует правильность решения.</p>}
                  {report.model && <small>Модель: {report.model}</small>}
                </>}
    <p className="ai-code-review__notice">Рекомендации могут содержать ошибки. Окончательную оценку выставляет преподаватель.</p>
  </section>;
}
