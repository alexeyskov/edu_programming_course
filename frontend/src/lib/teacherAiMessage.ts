import type { AiCodeSelection } from './editorAiSelection';

export interface AiCodeAttachment extends AiCodeSelection { finding?: string }
export const AI_MESSAGE_MAX_LENGTH = 20000;
const attachmentPrefix = 'Выделенный фрагмент кода: ';

/** Store the attachment with the question so it also survives reloading chat history. */
export function composeTeacherAiMessage(question: string, attachment?: AiCodeAttachment): string {
  if (!attachment) return question;
  const { text, ...location } = attachment;
  let fenceLength = 3;
  for (const match of text.matchAll(/`+/g)) fenceLength = Math.max(fenceLength, match[0].length + 1);
  const fence = '`'.repeat(fenceLength);
  return `${attachmentPrefix}${JSON.stringify(location)}\n${fence}cpp\n${text}\n${fence}\n\n${question}`;
}

/** Old/plain messages stay unchanged; only our complete attachment envelope is unpacked. */
export function parseTeacherAiMessage(content: string): { question: string; attachment?: AiCodeAttachment } {
  const plain = { question: content };
  if (!content.startsWith(attachmentPrefix)) return plain;
  const headerEnd = content.indexOf('\n');
  if (headerEnd < 0) return plain;
  try {
    const location = JSON.parse(content.slice(attachmentPrefix.length, headerEnd));
    if (!location || typeof location.path !== 'string' || !location.path
      || !Number.isSafeInteger(location.startLine) || location.startLine < 1
      || !Number.isSafeInteger(location.endLine) || location.endLine < location.startLine
      || (location.finding !== undefined && typeof location.finding !== 'string')) return plain;
    const opening = /^(`{3,})cpp\n/.exec(content.slice(headerEnd + 1));
    if (!opening) return plain;
    const codeStart = headerEnd + 1 + opening[0].length;
    const closing = `\n${opening[1]}\n\n`;
    const codeEnd = content.indexOf(closing, codeStart);
    if (codeEnd < 0) return plain;
    const question = content.slice(codeEnd + closing.length);
    if (!question.trim()) return plain;
    return {
      question,
      attachment: {
        path: location.path, startLine: location.startLine, endLine: location.endLine,
        text: content.slice(codeStart, codeEnd),
        ...(location.finding === undefined ? {} : { finding: location.finding }),
      },
    };
  } catch { return plain; }
}
