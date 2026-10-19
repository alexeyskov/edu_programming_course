import { Database, RefreshCcw, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../lib/api';
import type { StorageCategory, StorageCleanupTarget, StorageUsage } from '../types';
import { Button, Card, InlineError, Modal, useToast } from './ui';
import './storageCleanupSettings.css';

const categoryDetails: Record<StorageCategory, { title: string; action: string; description: string }> = {
  edit_history: {
    title: 'История написания кода', action: 'Очистить историю написания',
    description: 'Подробные изменения файлов завершённых попыток. Исходный код сдач сохраняется.',
  },
  teacher_ai_chats: {
    title: 'Чаты преподавателей с ИИ', action: 'Очистить чаты преподавателей',
    description: 'Переписки с помощником проверки. Результаты ИИ-проверок и чаты студентов сохраняются.',
  },
  synced_submissions: {
    title: 'Архив ответов и комментариев', action: 'Очистить архив ответов',
    description: 'Только завершённые работы с подтверждённой синхронизацией утверждённой оценки и комментария в Moodle.',
  },
};

const categoryOrder: StorageCategory[] = ['edit_history', 'teacher_ai_chats', 'synced_submissions'];

export function formatStorageBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes.toLocaleString('ru-RU')} Б`;
  const units = ['КиБ', 'МиБ', 'ГиБ', 'ТиБ'];
  const power = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length);
  return `${(bytes / 1024 ** power).toLocaleString('ru-RU', { maximumFractionDigits: 2 })} ${units[power - 1]}`;
}

function canClean(preview: StorageUsage, target: StorageCleanupTarget): boolean {
  if (target === 'quota') return preview.usedBytes > preview.storageLimitGb * 1024 ** 3
    && preview.categories.some((item) => item.eligibleRecords > 0);
  return preview.categories.some((item) => item.category === target && item.eligibleRecords > 0);
}

export function StorageCleanupSettings({ onAccessError, refreshKey = 0 }: {
  onAccessError(error: unknown): boolean;
  refreshKey?: number;
}) {
  const [preview, setPreview] = useState<StorageUsage | null>(null);
  const [confirmation, setConfirmation] = useState<{ preview: StorageUsage; target: StorageCleanupTarget } | null>(null);
  const [loading, setLoading] = useState(true);
  const [purging, setPurging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resultWarnings, setResultWarnings] = useState<string[]>([]);
  const requestId = useRef(0);
  const purgingRef = useRef(false);
  const mounted = useRef(false);
  const { push } = useToast();

  const load = useCallback(async (target?: StorageCleanupTarget) => {
    const current = ++requestId.current;
    setLoading(true);
    setError(null);
    setConfirmation(null);
    try {
      const next = await api.getStorageUsage();
      if (!mounted.current || current !== requestId.current) return;
      setPreview(next);
      if (target && canClean(next, target)) setConfirmation({ preview: next, target });
      else if (target) push('info', 'Очистка не требуется', 'После обновления нет данных, доступных для выбранной очистки.');
    } catch (caught) {
      if (!mounted.current || current !== requestId.current) return;
      setPreview(null);
      if (!onAccessError(caught)) setError(caught instanceof Error ? caught.message : 'Не удалось получить объём хранимых данных.');
    } finally {
      if (mounted.current && current === requestId.current) setLoading(false);
    }
  }, [onAccessError, push]);

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => { mounted.current = false; requestId.current += 1; };
  }, [load, refreshKey]);

  const purge = async () => {
    if (!confirmation || purgingRef.current) return;
    purgingRef.current = true;
    setPurging(true);
    setError(null);
    setResultWarnings([]);
    try {
      const result = await api.cleanUpStorage(confirmation.target, confirmation.preview.eligibleBefore);
      if (!mounted.current) return;
      setConfirmation(null);
      setResultWarnings(result.warnings);
      push(result.overLimit ? 'info' : 'success', 'Очистка хранилища завершена',
        `Удалено записей: ${result.removedRecords}. Освобождено: ${formatStorageBytes(result.reclaimedBytes)}.${result.overLimit ? ' Лимит пока превышен: оставшиеся данные защищены от удаления.' : ''}`);
      await load();
    } catch (caught) {
      if (!mounted.current) return;
      // A lost response is not permission to repeat destructive work. Discard
      // the snapshot and require another preview and explicit confirmation.
      setConfirmation(null);
      setPreview(null);
      if (!onAccessError(caught)) setError(caught instanceof Error ? caught.message : 'Не удалось завершить очистку. Обновите данные.');
    } finally {
      purgingRef.current = false;
      if (mounted.current) setPurging(false);
    }
  };

  const overLimit = preview !== null && preview.usedBytes > preview.storageLimitGb * 1024 ** 3;
  const warnings = [...new Set([...(preview?.warnings ?? []), ...resultWarnings])];
  const confirmationItems = confirmation?.preview.categories.filter((item) =>
    confirmation.target === 'quota' || item.category === confirmation.target) ?? [];

  return <>
    <Card className="settings-card storage-cleanup">
      <header><span><Database /></span><div><h2>Управление хранилищем</h2><p>Объём данных приложения и безопасная очистка старых записей.</p></div></header>
      <p className="muted">Лимит относится к хранимым данным, не к оперативной памяти. При его превышении автоматически удаляются самые старые доступные записи: сначала история написания, затем чаты преподавателей с ИИ, в последнюю очередь — синхронизированные ответы и комментарии.</p>
      {error && <InlineError title="Хранилище" message={error} />}
      {loading && <p className="muted" role="status">Подсчитываем объём данных…</p>}
      {preview && !loading && <>
        <dl className="storage-cleanup__summary" aria-label="Объём хранимых данных">
          <div><dt>Хранимые данные</dt><dd>{formatStorageBytes(preview.usedBytes)}</dd></div>
          <div><dt>Сохранённый лимит</dt><dd>{preview.storageLimitGb.toLocaleString('ru-RU')} ГиБ</dd></div>
          {preview.databaseBytes !== null && <div><dt>Размер базы на диске</dt><dd>{formatStorageBytes(preview.databaseBytes)}</dd></div>}
        </dl>
        {overLimit && <p className="storage-cleanup__warning" role="status">Лимит превышен. Если безопасно удаляемых данных недостаточно, несинхронизированные работы всё равно сохраняются.</p>}
        <div className="storage-cleanup__categories">
          {categoryOrder.map((category) => {
            const data = preview.categories.find((item) => item.category === category);
            const details = categoryDetails[category];
            return <section key={category} className="storage-cleanup__category" aria-label={details.title}>
              <h3>{details.title}</h3><p>{details.description}</p>
              <p className="storage-cleanup__counts">Всего: {data?.records ?? 0} записей · {formatStorageBytes(data?.usedBytes ?? 0)}<br />Доступно для очистки: {data?.eligibleRecords ?? 0} · {formatStorageBytes(data?.eligibleBytes ?? 0)}</p>
              <Button variant="danger" disabled={purging || !data?.eligibleRecords} onClick={() => void load(category)}><Trash2 size={16} />{details.action}</Button>
            </section>;
          })}
        </div>
        {preview.lastCleanupAt && <p className="muted">Последняя очистка: {new Date(preview.lastCleanupAt).toLocaleString('ru-RU')}.</p>}
      </>}
      {warnings.length > 0 && <div className="storage-cleanup__warning" role="status">{warnings.map((warning) => <p key={warning}>{warning}</p>)}</div>}
      <p className="muted storage-cleanup__scope">Активные попытки, неподтверждённые оценки и несинхронизированные ответы защищены. Удаление истории написания и чатов необратимо. Ответы и комментарии можно заново импортировать из Moodle, пока они доступны в LMS. Освободившееся место в PostgreSQL используется повторно: размер файлов базы на диске может не уменьшиться. Файлы Docker и журналы PostgreSQL эта очистка не удаляет.</p>
      <div className="storage-cleanup__actions">
        <Button variant="secondary" disabled={loading || purging} onClick={() => void load()}><RefreshCcw size={16} />Обновить объём данных</Button>
        <Button variant="danger" disabled={loading || purging || !preview || !canClean(preview, 'quota')} onClick={() => void load('quota')}><Trash2 size={16} />Очистить до лимита</Button>
      </div>
    </Card>
    <Modal open={confirmation !== null} title="Очистить данные хранилища?"
      onClose={() => { if (!purgingRef.current) setConfirmation(null); }}
      footer={<><Button variant="secondary" disabled={purging} onClick={() => setConfirmation(null)}>Отмена</Button><Button variant="danger" loading={purging} onClick={() => void purge()}>Удалить безвозвратно</Button></>}>
      {confirmation && <>
        <p>{confirmation.target === 'quota'
          ? `Будут удаляться старые записи до сохранённого лимита ${confirmation.preview.storageLimitGb} ГиБ, в указанном ниже порядке. Очистка остановится после достижения лимита или исчерпания доступных данных.`
          : 'Будут удалены доступные старые записи выбранной категории. Новые записи в эту очистку не попадут.'}</p>
        <ol className="storage-cleanup__confirmation">{categoryOrder.filter((category) => confirmationItems.some((item) => item.category === category)).map((category) => {
          const item = confirmationItems.find((entry) => entry.category === category)!;
          return <li key={category}>{categoryDetails[category].title}: <strong>{item.eligibleRecords}</strong> записей, {formatStorageBytes(item.eligibleBytes)}.</li>;
        })}</ol>
        <p>Активные работы, неподтверждённые оценки и ответы без подтверждённой синхронизации с Moodle не удаляются. Для архива ответов потребуется повторный импорт из Moodle.</p>
        <p><strong>Историю написания и чаты восстановить нельзя. Отменить удаление невозможно.</strong></p>
      </>}
    </Modal>
  </>;
}
