export interface AiChatMessage {
  id: string;
  content: string;
  citations: Array<{ title: string; url: string }>;
}
export type AiStreamEvent = { type: 'thinking'; summary?: string } | { type: 'delta'; text: string };
const MAX_STREAM_BYTES = 32 * 1024 * 1024;
const MAX_EVENT_CHARS = 2 * 1024 * 1024;
const MAX_ANSWER_CHARS = 1024 * 1024;

/** SSE can split anywhere, including within Cyrillic characters and event delimiters. */
export async function readAiStream(
  response: Response,
  onEvent: (event: AiStreamEvent) => void,
  streamError: (body: Record<string, unknown>) => Error,
): Promise<AiChatMessage> {
  if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) {
    throw new Error('Сервер не вернул поток ответа ИИ. Обновите сервер и страницу.');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let pending = '';
  let event = '';
  let data: string[] = [];
  let received = 0;
  let eventChars = 0;
  let answerChars = 0;
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      received += chunk.value.byteLength;
      if (received > MAX_STREAM_BYTES) throw new Error('Поток ИИ превышает допустимый размер.');
      pending += decoder.decode(chunk.value, { stream: true });
      while (pending.includes('\n')) {
        const end = pending.indexOf('\n');
        const line = pending.slice(0, end).replace(/\r$/, '');
        pending = pending.slice(end + 1);
        eventChars += line.length + 1;
        if (eventChars > MAX_EVENT_CHARS) throw new Error('Событие потока ИИ превышает допустимый размер.');
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
        else if (!line) {
          if (data.length) {
            const payload = JSON.parse(data.join('\n'));
            if (!payload || typeof payload !== 'object') throw new Error('Некорректный ответ ИИ.');
            if (event === 'error') throw streamError(payload);
            if (event === 'thinking') onEvent({ type: 'thinking', ...(typeof payload.summary === 'string' && payload.summary.trim() ? { summary: payload.summary.slice(0, 200) } : {}) });
            if (event === 'delta' && typeof payload.text === 'string') {
              answerChars += payload.text.length;
              if (answerChars > MAX_ANSWER_CHARS) throw new Error('Ответ ИИ превышает допустимый размер.');
              onEvent({ type: 'delta', text: payload.text });
            }
            if (event === 'done') {
              if (typeof payload.id !== 'string' || typeof payload.content !== 'string') throw new Error('Некорректный ответ ИИ.');
              if (payload.content.length > MAX_ANSWER_CHARS) throw new Error('Ответ ИИ превышает допустимый размер.');
              return { id: payload.id, content: payload.content, citations: Array.isArray(payload.citations) ? payload.citations : [] };
            }
          }
          event = ''; data = []; eventChars = 0;
        }
      }
      if (eventChars + pending.length > MAX_EVENT_CHARS) throw new Error('Событие потока ИИ превышает допустимый размер.');
    }
    throw new Error('Соединение прервано до завершения ответа ИИ. Частичный ответ не сохранён.');
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
