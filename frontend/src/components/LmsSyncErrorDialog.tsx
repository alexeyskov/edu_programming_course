import { Clock3, RefreshCcw, TriangleAlert } from 'lucide-react';
import type { LmsSyncDiagnostic } from '../types';
import { formatDate } from '../lib/utils';
import { Button, Modal } from './ui';

const explanations: Record<string, { title: string; text: string }> = {
  BROWSER_BUSY: {
    title: 'Коннектор Moodle был занят',
    text: 'Все доступные процессы коннектора заняты другими операциями. Подождите несколько секунд и повторите синхронизацию; сохранённые данные курса не потеряны.',
  },
  UNAVAILABLE: {
    title: 'Moodle или браузерный коннектор не ответил',
    text: 'Связь прервалась либо Moodle отвечал дольше допустимого. Сохранённые данные курса не потеряны.',
  },
  TIMEOUT: {
    title: 'Moodle отвечал слишком долго',
    text: 'Операция остановлена по тайм-ауту. Обычно достаточно повторить синхронизацию.',
  },
  LMS_REAUTH_REQUIRED: {
    title: 'Сессия Moodle истекла',
    text: 'Выйдите из Мехмат.Практикума и войдите снова, чтобы обновить защищённую Moodle-сессию.',
  },
  SESSION_EXPIRED: {
    title: 'Сессия Moodle истекла',
    text: 'Выйдите из Мехмат.Практикума и войдите снова, затем повторите синхронизацию.',
  },
  TEACHER_CONTEXT_REQUIRED: {
    title: 'Нет сессии преподавателя для этого курса',
    text: 'Синхронизацию должен запустить преподаватель, которому курс доступен в Moodle.',
  },
  CONNECTION_DISABLED: {
    title: 'Подключение Moodle отключено',
    text: 'Включите подключение в настройках системы и повторите синхронизацию.',
  },
  INVALID_RESPONSE: {
    title: 'Moodle вернул страницу неизвестного формата',
    text: 'Интерфейс Moodle мог измениться. Техническая причина ниже поможет уточнить проблемную страницу.',
  },
  COURSE_PROJECTION_FAILED: {
    title: 'Полученные данные курса не удалось сохранить',
    text: 'Снимок Moodle не прошёл внутреннюю проверку целостности. Повторите запрос; если ошибка сохранится, используйте код ниже для диагностики.',
  },
};

function diagnosticExplanation(diagnostic?: LmsSyncDiagnostic) {
  if (!diagnostic) return {
    title: 'Последняя синхронизация завершилась с ошибкой',
    text: 'Сервер предыдущей версии не сохранил подробности. Повторите синхронизацию — новый результат будет записан с точной причиной.',
  };
  return explanations[diagnostic.code.toUpperCase()] ?? {
    title: 'Синхронизация Moodle не завершена',
    text: diagnostic.retryable
      ? 'Ошибка допускает повторную попытку. Данные предыдущей успешной синхронизации сохранены.'
      : 'Проверьте техническую причину ниже. Возможно, потребуется повторный вход или настройка подключения.',
  };
}

export function LmsSyncErrorDialog({
  open,
  courseTitle,
  diagnostic,
  retrying,
  onClose,
  onRetry,
}: {
  open: boolean;
  courseTitle: string;
  diagnostic?: LmsSyncDiagnostic;
  retrying: boolean;
  onClose(): void;
  onRetry(): void;
}) {
  const explanation = diagnosticExplanation(diagnostic);
  return <Modal
    open={open}
    title="Ошибка синхронизации Moodle"
    width="620px"
    onClose={onClose}
    footer={<>
      <Button variant="ghost" onClick={onClose}>Закрыть</Button>
      <Button loading={retrying} onClick={onRetry}><RefreshCcw size={16} /> Повторить синхронизацию</Button>
    </>}
  >
    <div className="lms-sync-error">
      <div className="lms-sync-error__summary"><TriangleAlert size={22} /><div><strong>{explanation.title}</strong><p>{explanation.text}</p></div></div>
      <dl>
        <div><dt>Курс</dt><dd>{courseTitle}</dd></div>
        {diagnostic?.at && <div><dt><Clock3 size={13} /> Последняя попытка</dt><dd>{formatDate(diagnostic.at)}</dd></div>}
        <div><dt>Код</dt><dd><code>{diagnostic?.code || 'DETAILS_NOT_RECORDED'}</code></dd></div>
        {diagnostic?.message && <div><dt>Техническая причина</dt><dd>{diagnostic.message}</dd></div>}
      </dl>
    </div>
  </Modal>;
}
