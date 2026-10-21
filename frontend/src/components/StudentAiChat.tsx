import { Bot, Maximize2, MessageCircleQuestion, Minimize2, Send, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api';
import { AI_MESSAGE_MAX_LENGTH, composeTeacherAiMessage, type AiCodeAttachment } from '../lib/teacherAiMessage';
import { cn } from '../lib/utils';
import { AiMarkdown, CodeAttachment, safeAiLink, ThinkingIndicator, UserMessage, type AiAttachmentDraft } from './TeacherAiChat';
import './studentAiChat.css';

interface Message {
  from: 'user' | 'ai';
  text: string;
  citations?: Array<{ title: string; url: string }>;
  incomplete?: boolean;
}

/** Uses the student endpoint/policy even though the visual language is shared with review. */
export function StudentAiChat({ open, onClose, attemptId, courseId, canSend, prepareContext, draft }: {
  open: boolean; onClose(): void; attemptId: string; courseId: string; canSend: boolean;
  prepareContext(): Promise<number>; draft?: AiAttachmentDraft;
}) {
  const [message, setMessage] = useState('');
  const [attachment, setAttachment] = useState<AiCodeAttachment>();
  const [messages, setMessages] = useState<Message[]>([]);
  const [sending, setSending] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [summary, setSummary] = useState('');
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState(false);
  const threadRef = useRef<string>();
  const controllerRef = useRef<AbortController>();
  const sendingRef = useRef(false);
  const mountedRef = useRef(false);
  const draftVersionRef = useRef(0);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const followOutputRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; controllerRef.current?.abort(); };
  }, []);

  useEffect(() => {
    if (!canSend) {
      controllerRef.current?.abort();
      setSending(false); setPreparing(false); setThinking(false);
    }
  }, [canSend]);

  useEffect(() => {
    if (!draft) return;
    draftVersionRef.current += 1;
    setAttachment(draft.attachment);
    inputRef.current?.focus();
  }, [draft]);

  useEffect(() => { if (open && canSend && courseId) inputRef.current?.focus(); }, [open, canSend, courseId]);

  useEffect(() => {
    const element = transcriptRef.current;
    if (element && followOutputRef.current) element.scrollTop = element.scrollHeight;
  }, [messages, thinking, summary, expanded, open]);

  async function send() {
    const question = message.trim();
    if (!canSend || !courseId || !question || sendingRef.current) return;
    const content = composeTeacherAiMessage(question, attachment);
    if (content.length > AI_MESSAGE_MAX_LENGTH) {
      setError('Вопрос вместе с фрагментом слишком длинный. Сократите вопрос или выберите меньший фрагмент кода.');
      return;
    }
    const controller = new AbortController();
    controllerRef.current = controller;
    const isCurrent = () => mountedRef.current && !controller.signal.aborted && controllerRef.current === controller;
    const sentDraftVersion = draftVersionRef.current;
    sendingRef.current = true;
    setSending(true); setPreparing(true); setThinking(false); setSummary(''); setError('');
    // An attachment belongs to one sent question, not to the next request/retry.
    setMessage(''); setAttachment(undefined);
    followOutputRef.current = true;
    setMessages((items) => [...items, { from: 'user', text: content }, { from: 'ai', text: '' }]);
    const updateAnswer = (update: (item: Message) => Message) => setMessages((items) => items.map((item, index) => index === items.length - 1 ? update(item) : item));
    try {
      // Save all dirty files before both creating a thread and every subsequent question.
      const revision = await prepareContext();
      if (!isCurrent()) return;
      if (!threadRef.current) threadRef.current = (await api.createStudentAiThread(attemptId, courseId, revision)).id;
      if (!isCurrent()) return;
      setPreparing(false); setThinking(true);
      const response = await api.sendAiMessageStream(threadRef.current, content, { revision }, {
        signal: controller.signal,
        onEvent: (event) => {
          if (!isCurrent()) return;
          if (event.type === 'thinking') {
            setThinking(true);
            if (event.summary) setSummary(event.summary);
          } else {
            setThinking(false);
            updateAnswer((item) => ({ ...item, text: item.text + event.text }));
          }
        },
      });
      if (!isCurrent()) return;
      updateAnswer(() => ({ from: 'ai', text: response.content, citations: response.citations }));
    } catch (caught) {
      if (!isCurrent()) return;
      updateAnswer((item) => ({ ...item, incomplete: true }));
      setError(caught instanceof Error ? `Помощник сейчас недоступен: ${caught.message}` : 'Помощник сейчас недоступен.');
      if (draftVersionRef.current === sentDraftVersion) setMessage(question);
    } finally {
      if (controllerRef.current === controller) {
        sendingRef.current = false;
        if (mountedRef.current) { setSending(false); setPreparing(false); setThinking(false); }
      }
    }
  }

  if (!open) return null;
  return <aside className={cn('student-ai-drawer', expanded && 'student-ai-drawer--expanded')} aria-label="Учебный помощник">
    <section className="teacher-ai-chat teacher-chat student-ai-chat">
      <div className="student-chat-actions">
        <button type="button" aria-label={expanded ? 'Свернуть чат' : 'Развернуть чат'} aria-pressed={expanded} title={expanded ? 'Вернуть обычный размер чата' : 'Развернуть чат'} onClick={() => setExpanded((value) => !value)}>{expanded ? <Minimize2 size={18} /> : <Maximize2 size={18} />}</button>
        <button type="button" aria-label="Закрыть помощник" title="Закрыть помощник" onClick={() => { setExpanded(false); onClose(); }}><X size={18} /></button>
      </div>
      <div className="teacher-chat-transcript" ref={transcriptRef} aria-label="Переписка с ИИ" aria-busy={sending}
        onScroll={(event) => { const el = event.currentTarget; followOutputRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60; }}>
        <header className="teacher-chat-header"><Bot size={18} /><span><strong>Учебный помощник</strong><small>Условие и код уже в контексте</small></span></header>
        <div className="student-chat-policy"><MessageCircleQuestion size={16} /><span>Объяснения и подсказки без готового решения. Выделите код в редакторе и нажмите «Спросить у ИИ».</span></div>
        {canSend && !courseId && <div className="teacher-chat-note" role="status">Загружаем данные курса для помощника…</div>}
        {messages.map((item, index) => item.text && <article className={cn('teacher-chat-message', item.from === 'user' && 'is-user')} key={index}>
          {item.from === 'user' ? <UserMessage content={item.text} /> : <AiMarkdown content={item.text} />}
          {item.incomplete && <small className="teacher-chat-incomplete">Неполный ответ · не сохранён</small>}
          {item.citations?.filter((citation) => safeAiLink(citation.url)).map((citation) => <a className="teacher-chat-citation" key={citation.url} href={citation.url} target="_blank" rel="noopener noreferrer">{citation.title}</a>)}
        </article>)}
        {sending && (preparing ? <div className="teacher-chat-note" role="status">Сохраняем код…</div> : thinking && <ThinkingIndicator summary={summary} />)}
        {error && <div className="teacher-chat-error" role="alert">{error}</div>}
      </div>
      <form onSubmit={(event) => { event.preventDefault(); void send(); }}>
        {attachment && <CodeAttachment attachment={attachment} onRemove={() => { draftVersionRef.current += 1; setAttachment(undefined); inputRef.current?.focus(); }} />}
        <div className="teacher-chat-input-row"><textarea ref={inputRef} rows={3} aria-label="Вопрос ИИ" placeholder="Ваш вопрос…" value={message} disabled={!canSend || !courseId} maxLength={AI_MESSAGE_MAX_LENGTH}
          onChange={(event) => { draftVersionRef.current += 1; setMessage(event.target.value); }}
          onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); } }} />
          <button type="submit" disabled={!canSend || !courseId || !message.trim() || sending} aria-label="Отправить"><Send size={18} /></button></div>
      </form>
      <small className="student-chat-disclaimer">Ответы ИИ могут содержать ошибки — проверяйте документацию.</small>
    </section>
  </aside>;
}
