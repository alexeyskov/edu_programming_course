import { describe, expect, it, vi } from 'vitest';
import { readAiStream } from './aiStream';

function response(text: string, chunkSize = 7) {
  const bytes = new TextEncoder().encode(text);
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (let i = 0; i < bytes.length; i += chunkSize) controller.enqueue(bytes.slice(i, i + chunkSize));
      controller.close();
    },
  });
  return new Response(body, { headers: { 'content-type': 'text/event-stream' } });
}
const done = 'event: done\r\ndata: {"id":"reply-1","content":"Привет!","citations":[]}\r\n\r\n';
const makeError = (body: Record<string, unknown>) => new Error(String(body.message));

describe('AI event stream', () => {
  it('accepts long answers with many tiny events past the old 2 MiB wire limit', async () => {
    const answer = 'я'.repeat(70000);
    const text = 'event: delta\ndata: {"text":"я"}\n\n'.repeat(70000)
      + `event: done\ndata: ${JSON.stringify({ id: 'reply-long', content: answer })}\n\n`;
    expect(new TextEncoder().encode(text).byteLength).toBeGreaterThan(2_097_152);
    let received = 0;
    const result = await readAiStream(response(text, 8192), (event) => { if (event.type === 'delta') received += event.text.length; }, makeError);
    expect(received).toBe(answer.length);
    expect(result.content).toBe(answer);
  });
  it('bounds incomplete events separately without allowing unbounded buffering', async () => {
    await expect(readAiStream(response('data: ' + 'x'.repeat(2 * 1024 * 1024), 65536), vi.fn(), makeError)).rejects.toThrow('Событие потока');
  });
  it.each([1, 7, 64])('handles UTF-8, CRLF, comments and fragmented events (%i bytes)', async (size) => {
    const update = vi.fn();
    const result = await readAiStream(response(': heartbeat\r\n\r\nevent: thinking\r\ndata: {}\r\n\r\nevent: delta\r\ndata: {"text":"При"}\r\n\r\nevent: delta\r\ndata: {"text":"вет!"}\r\n\r\n' + done, size), update, makeError);
    expect(update.mock.calls.map(([event]) => event)).toEqual([{ type: 'thinking' }, { type: 'delta', text: 'При' }, { type: 'delta', text: 'вет!' }]);
    expect(result.content).toBe('Привет!');
  });
  it('rejects an incomplete connection without treating partial text as success', async () => {
    const update = vi.fn();
    await expect(readAiStream(response('event: delta\ndata: {"text":"часть"}\n\n'), update, makeError)).rejects.toThrow('Соединение прервано');
    expect(update).toHaveBeenCalledWith({ type: 'delta', text: 'часть' });
  });
  it('recognizes errors even after HTTP 200 without retrying', async () => {
    await expect(readAiStream(response('event: error\ndata: {"code":"TIMEOUT","message":"Не дождались ответа"}\n\n'), vi.fn(), makeError)).rejects.toThrow('Не дождались ответа');
  });
  it('forwards bounded topic summaries but ignores unexpected reasoning fields', async () => {
    const update = vi.fn();
    await readAiStream(response('event: thinking\ndata: {"summary":"указатели и память","reasoning":"PRIVATE"}\n\nevent: thinking\ndata: {"summary":{}}\n\n' + done), update, makeError);
    expect(update.mock.calls.map(([event]) => event)).toEqual([{ type: 'thinking', summary: 'указатели и память' }, { type: 'thinking' }]);
    const long = 'x'.repeat(500);
    await readAiStream(response(`event: thinking\ndata: ${JSON.stringify({ summary: long })}\n\n` + done), update, makeError);
    expect(update).toHaveBeenLastCalledWith({ type: 'thinking', summary: 'x'.repeat(200) });
  });
  it('rejects unexpected JSON instead of pretending it was streamed', async () => {
    await expect(readAiStream(new Response('{}', { headers: { 'content-type': 'application/json' } }), vi.fn(), makeError)).rejects.toThrow('Сервер не вернул поток');
  });
});
