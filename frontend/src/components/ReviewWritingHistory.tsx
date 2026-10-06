import { ChevronDown, History } from 'lucide-react';
import { useId, useMemo, useState } from 'react';
import { Button } from './ui';
import { formatClientContext, formatDate } from '../lib/utils';
import type { HistoryEditDetail, HistoryEvent, WorkspaceFile } from '../types';
import './reviewWritingHistory.css';

const PAGE_SIZE = 100;
const ACTIONS: Record<string, string> = {
  REPLACE_CONTENT: 'Изменение файла',
  FILE_CREATE: 'Создание файла',
  FILE_DELETE: 'Удаление файла',
};
const DETAILS: Record<string, string> = {
  ATTEMPT_STARTED: 'Открыта попытка',
  SUBMISSION: 'Работа отправлена студентом',
  DEADLINE: 'Работа отправлена по истечении времени',
  PERIODIC: 'Автоматическое сохранение',
  FINAL_MINUTE: 'Сохранение перед завершением',
  QUEUED: 'В очереди',
  RUNNING: 'Выполняется',
  SUCCEEDED: 'Успешно',
  COMPLETED: 'Завершено',
  FAILED: 'Завершено с ошибкой',
  CANCELLED: 'Отменено',
};

const EDIT_ACTIONS: Record<HistoryEditDetail['operation'], string> = {
  insert: 'Вставка текста', delete: 'Удаление текста', replace: 'Замена текста',
  create: 'Создание файла', remove: 'Удаление файла', unchanged: 'Без изменения текста',
};

function EditDetails({ change }: { change: HistoryEditDetail }) {
  const [open, setOpen] = useState(false);
  const position = change.startLine && change.startColumn
    ? `Строка ${change.startLine}, позиция ${change.startColumn}`
    : change.offset != null ? `Смещение от начала файла: ${change.offset} символов` : 'Позиция не сохранена';
  const hasRange = change.endLine && change.endColumn && (change.endLine !== change.startLine || change.endColumn !== change.startColumn);
  const removed = ['delete', 'replace', 'remove'].includes(change.operation);
  const inserted = ['insert', 'replace', 'create'].includes(change.operation);
  return <details className="writing-history__change" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{EDIT_ACTIONS[change.operation]} · {position}</summary>
    {open && <div className="writing-history__change-body">
      {hasRange && <small>До строки {change.endLine}, позиции {change.endColumn} (не включая), до изменения.</small>}
      {removed && <section className="writing-history__removed" aria-label="Удалённый текст"><strong>Удалено{change.deleteCount != null ? ` символов: ${change.deleteCount}` : ''}</strong>{change.deletedText == null ? <p>Удалённый текст не сохранён и не восстановлен по снимкам.</p> : <pre><code>{change.deletedText || '(пустой файл)'}</code></pre>}</section>}
      {inserted && <section className="writing-history__inserted" aria-label="Вставленный текст"><strong>Вставлено{change.insertText != null ? ` символов: ${Array.from(change.insertText).length}` : ''}</strong>{change.insertText == null ? <p>Исходное содержимое файла не сохранено.</p> : <pre><code>{change.insertText || '(пустой файл)'}</code></pre>}</section>}
      {change.operation === 'unchanged' && <small>Содержимое не изменилось.</small>}
    </div>}
  </details>;
}

export function ReviewWritingHistory({ events, files }: { events: HistoryEvent[]; files: WorkspaceFile[] }) {
  const [expanded, setExpanded] = useState(true);
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);
  const listId = useId();
  const ordered = useMemo(() => [...events].sort((left, right) =>
    new Date(left.at).getTime() - new Date(right.at).getTime() || left.revision - right.revision,
  ), [events]);
  const paths = useMemo(() => new Map(files.map((file) => [file.id, file.path])), [files]);

  return <section className="writing-history">
    <button type="button" className="writing-history__toggle" aria-expanded={expanded} aria-controls={listId} onClick={() => setExpanded((value) => !value)}>
      <History size={17} aria-hidden="true" />
      <span><strong>История написания</strong><small>{events.length ? `Событий: ${events.length} · Время, файлы и действия` : 'Сохранённых событий нет'}</small></span>
      <ChevronDown size={16} className={expanded ? 'is-expanded' : ''} aria-hidden="true" />
    </button>
    <div id={listId} hidden={!expanded}>
      {ordered.length ? <>
        <p className="writing-history__note">Сохранённые события до сдачи выбранного ответа. Нажмите на правку, чтобы увидеть вставленный и удалённый текст. Время указано в часовом поясе вашего браузера.</p>
        <ol className="writing-history__list">
          {ordered.slice(0, visibleCount).map((event) => {
            const path = event.filePath || (event.fileId ? paths.get(event.fileId) : undefined);
            const action = event.type === 'internal_paste' ? event.label : ACTIONS[event.detail ?? ''] ?? event.label;
            const detail = ACTIONS[event.detail ?? ''] ? '' : DETAILS[event.detail ?? ''] ?? event.detail;
            const client = formatClientContext(event.client);
            return <li key={event.id}>
              <div className="writing-history__event-heading"><strong>{action}</strong><time dateTime={event.at}>{formatDate(event.at, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' })}</time></div>
              {path && <code className="writing-history__path">{path}</code>}
              {detail && <small>{detail}</small>}
              {event.editDetails?.map((change, index) => <EditDetails key={index} change={change} />)}
              {['REPLACE_CONTENT', 'FILE_CREATE', 'FILE_DELETE'].includes(event.detail ?? '') && !event.editDetails?.length && <small>Подробности этой правки не сохранены.</small>}
              {client && <small>{client}</small>}
            </li>;
          })}
        </ol>
        {visibleCount < ordered.length && <Button variant="secondary" onClick={() => setVisibleCount((count) => count + PAGE_SIZE)}>Показать ещё ({ordered.length - visibleCount})</Button>}
      </> : <p className="review-history-empty">События написания для этой сдачи не сохранены.</p>}
    </div>
  </section>;
}
