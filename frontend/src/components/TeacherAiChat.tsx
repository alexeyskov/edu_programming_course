import { Bot, Maximize2, Minimize2, Send, Trash2, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api } from '../lib/api';
import { cn } from '../lib/utils';
import { AI_MESSAGE_MAX_LENGTH, composeTeacherAiMessage, parseTeacherAiMessage, type AiCodeAttachment } from '../lib/teacherAiMessage';
import { Button, Modal } from './ui';
import './teacherAiChat.css';

export interface AiAttachmentDraft { id: number; attachment: AiCodeAttachment }
interface Message {
  from: 'user' | 'ai'; text: string;
  citations?: Array<{ title: string; url: string }>;
  incomplete?: boolean;
}

export function safeAiLink(url: string): string {
  try {
    const parsed = new URL(url);
    return ['https:', 'http:'].includes(parsed.protocol) && !parsed.username && !parsed.password ? url : '';
  } catch { return ''; }
}

export function AiMarkdown({ content }: { content: string }) {
  return <div className="ai-markdown"><Markdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={safeAiLink}
    components={{
      a: ({ href, children }) => href ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
      // Model-controlled images must not silently contact external tracking URLs.
      img: ({ alt }) => <span>{alt ?? ''}</span>,
    }}>{content}</Markdown></div>;
}

export function ThinkingIndicator({ summary }: { summary?: string }) {
  const [dots, setDots] = useState(1);
  useEffect(() => {
    const timer = window.setInterval(() => setDots((value) => value % 3 + 1), 450);
    return () => window.clearInterval(timer);
  }, []);
  return <div className="teacher-chat-thinking" role="status" aria-label="Thinking"><span aria-hidden="true">Thinking<span>{'.'.repeat(dots)}</span></span>
    {summary && <small title="Приблизительная тема анализа, определённая по потоку модели">Тема анализа: {summary}</small>}
  </div>;
}

export function CodeAttachment({ attachment, onRemove }: { attachment: AiCodeAttachment; onRemove?(): void }) {
  const lines = attachment.startLine === attachment.endLine ? `строка ${attachment.startLine}` : `строки ${attachment.startLine}–${attachment.endLine}`;
  return <aside className="teacher-chat-attachment" aria-label={onRemove ? 'Прикреплённый код' : 'Фрагмент кода в сообщении'}>
    <div className="teacher-chat-attachment-heading"><span title={`${attachment.path} · ${lines}`}>{attachment.path} · {lines}</span>
      {onRemove && <button type="button" className="teacher-chat-attachment-remove" aria-label="Убрать фрагмент кода" title="Убрать фрагмент кода" onClick={onRemove}><X size={14} /></button>}
    </div>
    <code>{attachment.text.length > 800 ? `${attachment.text.slice(0, 800)}…` : attachment.text}</code>
    {attachment.finding && <small title={attachment.finding}>Замечание ИИ: {attachment.finding}</small>}
  </aside>;
}

export function UserMessage({ content }: { content: string }) {
  const { question, attachment } = parseTeacherAiMessage(content);
  return <>{attachment && <CodeAttachment attachment={attachment} />}<div className="teacher-chat-user-text">{question}</div></>;
}

export function TeacherAiChat({ submissionId, courseId, canSend, teacherComment, draft, expanded, onExpanded }: {
  submissionId: string; courseId: string; canSend: boolean; teacherComment: string;
  draft?: AiAttachmentDraft; expanded: boolean; onExpanded(value: boolean): void;
}) {
  const [message, setMessage] = useState('');
  const [attachment, setAttachment] = useState<AiCodeAttachment>();
  const [sending, setSending] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [thinkingSummary, setThinkingSummary] = useState('');
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [messages, setMessages] = useState<Message[]>([]);
  const [error, setError] = useState('');
  const [clearOpen, setClearOpen] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [clearError, setClearError] = useState('');
  const threadRef = useRef<string>();
  const sendingRef = useRef(false);
  const clearingRef = useRef(false);
  const focusAfterClearRef = useRef(false);
  const controllerRef = useRef<AbortController>();
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const followOutputRef = useRef(true);
  const mountedRef = useRef(false);
  const draftVersionRef = useRef(0);

  useEffect(() => {
    mountedRef.current = true;
    let cancelled = false;
    void (async () => {
      try {
        const threads = await api.getTeacherAiThreads(courseId);
        if (cancelled) return;
        const related = threads.filter((thread) => thread.submissionId === submissionId);
        const existing = related.find((thread) => thread.status === 'OPEN') ?? related[0];
        if (existing) {
          // Closed history may be read, but sending needs a new thread.
          if (existing.status === 'OPEN') threadRef.current = existing.id;
          const stored = await api.getAiMessages(existing.id);
          if (!cancelled) setMessages(stored.map((item) => ({ from: item.from, text: item.content, citations: item.citations })));
        }
      } catch {
        if (!cancelled) setError('Историю чата загрузить не удалось.');
      } finally { if (!cancelled) setLoadingHistory(false); }
    })();
    return () => { cancelled = true; mountedRef.current = false; controllerRef.current?.abort(); };
  }, [courseId, submissionId]);

  useEffect(() => {
    if (!draft) return;
    draftVersionRef.current += 1;
    setAttachment(draft.attachment);
    inputRef.current?.focus();
  }, [draft]);

  useEffect(() => {
    const element = transcriptRef.current;
    if (element && followOutputRef.current) element.scrollTop = element.scrollHeight;
  }, [messages, thinking, thinkingSummary, expanded]);

  useEffect(() => {
    if (!clearing && !clearOpen && focusAfterClearRef.current) {
      focusAfterClearRef.current = false;
      inputRef.current?.focus();
    }
  }, [clearing, clearOpen]);

  async function send() {
    const question = message.trim();
    if (!canSend || !question || sendingRef.current || clearingRef.current || clearOpen || loadingHistory) return;
    const content = composeTeacherAiMessage(question, attachment);
    if (content.length > AI_MESSAGE_MAX_LENGTH) {
      setError('Вопрос вместе с фрагментом слишком длинный. Сократите вопрос или выберите меньший фрагмент кода.');
      return;
    }
    const sentDraftVersion = draftVersionRef.current;
    const controller = new AbortController();
    controllerRef.current = controller;
    sendingRef.current = true;
    setMessage(''); setAttachment(undefined); setError(''); setSending(true); setThinking(true); setThinkingSummary('');
    followOutputRef.current = true;
    setMessages((items) => [...items, { from: 'user', text: content }, { from: 'ai', text: '' }]);
    const updateAnswer = (update: (item: Message) => Message) => setMessages((items) => items.map((item, index) => index === items.length - 1 ? update(item) : item));
    try {
      if (!threadRef.current) threadRef.current = (await api.createTeacherAiThread(submissionId, courseId)).id;
      if (!mountedRef.current || controller.signal.aborted) return;
      const response = await api.sendAiMessageStream(threadRef.current, content, { teacherComment }, {
        signal: controller.signal,
        onEvent: (event) => {
          if (!mountedRef.current || controller.signal.aborted) return;
          if (event.type === 'thinking') {
            setThinking(true);
            if (event.summary) setThinkingSummary(event.summary);
          }
          else {
            setThinking(false);
            updateAnswer((item) => ({ ...item, text: item.text + event.text }));
          }
        },
      });
      if (!mountedRef.current || controller.signal.aborted) return;
      updateAnswer(() => ({ from: 'ai', text: response.content, citations: response.citations }));
    } catch (caught) {
      if (!mountedRef.current || controller.signal.aborted) return;
      updateAnswer((item) => ({ ...item, incomplete: true }));
      setError(caught instanceof Error ? caught.message : 'Помощник недоступен.');
      if (draftVersionRef.current === sentDraftVersion) {
        setMessage(question);
      }
    } finally {
      sendingRef.current = false;
      if (mountedRef.current) { setSending(false); setThinking(false); }
    }
  }

  async function clearChat() {
    if (!canSend || sendingRef.current || clearingRef.current || loadingHistory) return;
    clearingRef.current = true;
    setClearing(true); setClearError('');
    try {
      // Closing old threads and opening the new empty one is one server transaction.
      // No messages are deleted, and clearing never calls the model.
      const thread = await api.resetTeacherAiThread(submissionId, courseId);
      if (!mountedRef.current) return;
      threadRef.current = thread.id;
      draftVersionRef.current += 1;
      setMessages([]); setMessage(''); setAttachment(undefined);
      setThinking(false); setThinkingSummary(''); setError(''); setClearOpen(false);
      followOutputRef.current = true;
      focusAfterClearRef.current = true;
    } catch (caught) {
      if (mountedRef.current) setClearError(caught instanceof Error ? caught.message : 'Не удалось очистить чат. Повторите попытку.');
    } finally {
      clearingRef.current = false;
      if (mountedRef.current) setClearing(false);
    }
  }

  const clearDisabled = !canSend || loadingHistory || sending || clearing || (!messages.length && !message && !attachment && !error);
  return <><section className="teacher-ai-chat teacher-chat" aria-label="Помощник проверки">
    <button type="button" className={cn('teacher-chat-expand', expanded && 'is-active')} aria-pressed={expanded}
      aria-label={expanded ? 'Свернуть чат' : 'Развернуть чат'} title={expanded ? 'Вернуть обычный размер чата' : 'Развернуть на всю рабочую область'} onClick={() => onExpanded(!expanded)}>
      {expanded ? <Minimize2 size={18} /> : <Maximize2 size={18} />}
    </button>
    <div className="teacher-chat-transcript" ref={transcriptRef} aria-label="Переписка с ИИ" aria-busy={sending}
      onScroll={(event) => { const el = event.currentTarget; followOutputRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60; }}>
      <header className="teacher-chat-header">
        <Bot size={18} /><span><strong>Помощник проверки</strong><small>{canSend ? 'Условие и код уже в контексте' : 'История · только чтение'}</small></span>
        <button type="button" className="teacher-chat-clear" aria-label="Очистить чат" title={sending ? 'Дождитесь завершения ответа, чтобы очистить чат' : 'Очистить чат'} disabled={clearDisabled}
          onClick={() => { setClearError(''); setClearOpen(true); }}><Trash2 size={17} /></button>
      </header>
      {loadingHistory && <div className="teacher-chat-note">Загружаю историю…</div>}
      {messages.map((item, index) => item.text && <article className={cn('teacher-chat-message', item.from === 'user' && 'is-user')} key={index}>
        {item.from === 'user' ? <UserMessage content={item.text} /> : <AiMarkdown content={item.text} />}
        {item.incomplete && <small className="teacher-chat-incomplete">Неполный ответ · не сохранён</small>}
        {item.citations?.filter((citation) => safeAiLink(citation.url)).map((citation) => <a className="teacher-chat-citation" key={citation.url} href={citation.url} target="_blank" rel="noopener noreferrer">{citation.title}</a>)}
      </article>)}
      {sending && thinking && <ThinkingIndicator summary={thinkingSummary} />}
      {error && <div className="teacher-chat-error" role="alert">{error}</div>}
    </div>
    <form onSubmit={(event) => { event.preventDefault(); void send(); }}>
      {attachment && <CodeAttachment attachment={attachment} onRemove={() => { draftVersionRef.current += 1; setAttachment(undefined); inputRef.current?.focus(); }} />}
      <div className="teacher-chat-input-row"><textarea ref={inputRef} rows={3} aria-label="Вопрос ИИ" value={message} disabled={!canSend || clearing} maxLength={AI_MESSAGE_MAX_LENGTH}
        onChange={(event) => { draftVersionRef.current += 1; setMessage(event.target.value); }} placeholder={canSend ? 'Вопрос о текущем коде…' : 'Новые сообщения недоступны'}
        onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); } }} />
      <button type="submit" disabled={!canSend || !message.trim() || sending || clearing || clearOpen || loadingHistory} aria-label="Отправить"><Send size={18} /></button></div>
    </form>
  </section>
    <Modal open={clearOpen} title="Очистить чат?" onClose={() => { if (!clearingRef.current) setClearOpen(false); }}
      footer={<><Button variant="ghost" disabled={clearing} onClick={() => setClearOpen(false)}>Отмена</Button><Button variant="danger" loading={clearing} onClick={() => void clearChat()}><Trash2 size={16} /> Очистить</Button></>}>
      <p>Начнётся новый диалог по этой работе. Сообщения, набранный вопрос и прикреплённый фрагмент исчезнут из окна чата.</p>
      <p>Старая переписка сохранится в истории системы, но не будет передаваться ИИ в новом диалоге. Код, оценка и замечания ИИ не изменятся.</p>
      {clearError && <div className="teacher-chat-error" role="alert">{clearError}</div>}
    </Modal>
  </>;
}
