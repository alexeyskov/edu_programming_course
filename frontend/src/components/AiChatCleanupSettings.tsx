import { RefreshCcw, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../lib/api';
import type { AiChatCleanupPreview } from '../types';
import { Button, Card, InlineError, Modal, useToast } from './ui';
import './aiChatCleanupSettings.css';

export function AiChatCleanupSettings({ onAccessError }: { onAccessError(error: unknown): boolean }) {
  const [preview, setPreview] = useState<AiChatCleanupPreview | null>(null);
  const [confirmation, setConfirmation] = useState<AiChatCleanupPreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [purging, setPurging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestId = useRef(0);
  const purgingRef = useRef(false);
  const mounted = useRef(false);
  const { push } = useToast();

  const load = useCallback(async (confirm = false) => {
    const current = ++requestId.current;
    setLoading(true);
    setError(null);
    try {
      const next = await api.getAiChatCleanup();
      if (!mounted.current || current !== requestId.current) return;
      setPreview(next);
      if (confirm && next.threads > 0) setConfirmation(next);
    } catch (caught) {
      if (!mounted.current || current !== requestId.current) return;
      // A failed refresh invalidates the old counts and their confirmation.
      setPreview(null);
      if (!onAccessError(caught)) {
        setError(caught instanceof Error ? caught.message : 'Не удалось получить список для очистки.');
      }
    } finally {
      if (mounted.current && current === requestId.current) setLoading(false);
    }
  }, [onAccessError]);

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => { mounted.current = false; requestId.current += 1; };
  }, [load]);

  const purge = async () => {
    if (!confirmation || purgingRef.current) return;
    purgingRef.current = true;
    setPurging(true);
    setError(null);
    try {
      const removed = await api.cleanUpAiChats(confirmation.closedBefore);
      if (!mounted.current) return;
      setConfirmation(null);
      push('success', 'Очистка переписок завершена', `Удалено чатов: ${removed.threads}, сообщений: ${removed.messages}.`);
      await load();
    } catch (caught) {
      if (!mounted.current) return;
      // The request may have completed even if its response was lost. Never
      // automatically repeat deletion; require a fresh preview/confirmation.
      setConfirmation(null);
      setPreview(null);
      if (!onAccessError(caught)) {
        setError(caught instanceof Error ? caught.message : 'Не удалось завершить очистку. Обновите список.');
      }
    } finally {
      purgingRef.current = false;
      if (mounted.current) setPurging(false);
    }
  };

  return <>
    <Card className="settings-card ai-chat-cleanup">
      <header><span><Trash2 /></span><div>
        <h2>Очистка переписок с ИИ</h2>
        <p>Удалённые из чата переписки преподавателей и студентов остаются в истории. Здесь их можно удалить из базы окончательно.</p>
      </div></header>
      <p className="muted">Действующие чаты, результаты ИИ-проверок, оценки и история написания кода сохранятся.</p>
      {error && <InlineError title="Очистка переписок" message={error} />}
      {loading && <p className="muted" role="status">Подсчитываем закрытые переписки…</p>}
      {preview && !loading && <>
        <dl className="ai-chat-cleanup__counts" aria-label="Доступно для очистки">
          <div><dt>Чаты преподавателей</dt><dd>{preview.teacherThreads}</dd></div>
          <div><dt>Чаты студентов</dt><dd>{preview.studentThreads}</dd></div>
          <div><dt>Сообщения</dt><dd>{preview.messages}</dd></div>
        </dl>
        {preview.threads === 0 && <p className="muted">Нет закрытых переписок, доступных для очистки.</p>}
        <small className="muted">
          Чаты доступны для очистки не раньше чем через {Math.ceil(preview.graceSeconds / 60)} мин. после закрытия или последнего сообщения — для завершения запросов ИИ и сохранения лимитов.
          {preview.protectedThreads > 0 && ` Пока сохранены недавно закрытые чаты: ${preview.protectedThreads}.`}
        </small>
      </>}
      <div className="ai-chat-cleanup__actions">
        <Button variant="secondary" disabled={loading || purging} onClick={() => void load()}>
          <RefreshCcw size={16} />Обновить счётчики
        </Button>
        <Button variant="danger" disabled={loading || purging || !preview?.threads} onClick={() => void load(true)}>
          <Trash2 size={16} />Очистить закрытые переписки
        </Button>
      </div>
    </Card>
    <Modal
      open={confirmation !== null}
      title="Удалить закрытые переписки с ИИ?"
      onClose={() => { if (!purgingRef.current) setConfirmation(null); }}
      footer={<>
        <Button variant="secondary" disabled={purging} onClick={() => setConfirmation(null)}>Отмена</Button>
        <Button variant="danger" loading={purging} onClick={() => void purge()}>Удалить безвозвратно</Button>
      </>}
    >
      {confirmation && <>
        <p>Будут удалены закрытые чаты преподавателей: <strong>{confirmation.teacherThreads}</strong>, студентов: <strong>{confirmation.studentThreads}</strong>. Всего сообщений: <strong>{confirmation.messages}</strong>.</p>
        <p>Действующие чаты, ИИ-проверки работ, оценки и код останутся без изменений. Новые закрытые переписки в эту очистку не попадут.</p>
        <p><strong>Отменить удаление и восстановить переписки из интерфейса будет нельзя.</strong></p>
      </>}
    </Modal>
  </>;
}
