import { describe, expect, it } from 'vitest';
import { composeTeacherAiMessage, parseTeacherAiMessage, type AiCodeAttachment } from './teacherAiMessage';

const attachment: AiCodeAttachment = { path: 'main.cpp', startLine: 26, endLine: 26, text: 'new char[10]{"abc defg"};' };

describe('teacher chat code attachments', () => {
  it('keeps the teacher question and selected code separate without inventing a question', () => {
    const question = 'Зачем здесь 10?';
    expect(parseTeacherAiMessage(composeTeacherAiMessage(question, attachment))).toEqual({ question, attachment });
    expect(composeTeacherAiMessage(question, attachment)).not.toContain('Что делает этот код');
    expect(composeTeacherAiMessage(question)).toBe(question);
  });

  it('preserves multiline selections, whitespace, filenames and diagnostic context exactly', () => {
    const selected = {
      ...attachment, path: 'dir/заголовок "a".h', endLine: 29,
      text: '\t// ```cpp\r\n  const char* text = "````";\r\n\n', finding: 'Проверьте границы.',
    };
    const question = 'Почему?\n\nА так можно?';
    expect(parseTeacherAiMessage(composeTeacherAiMessage(question, selected))).toEqual({ question, attachment: selected });
  });

  it('does not truncate the attached code to the visible preview', () => {
    const selected = { ...attachment, text: '// строка\n'.repeat(400) };
    expect(parseTeacherAiMessage(composeTeacherAiMessage('Вопрос', selected)).attachment?.text).toBe(selected.text);
  });

  it.each([
    'Обычный вопрос',
    'Объясни выделенный фрагмент main.cpp, строки 26–26.\n\nnew char[10]{"abc defg"};',
    'Выделенный фрагмент кода: не JSON\n```cpp\ncode\n```\n\nВопрос',
    'Выделенный фрагмент кода: null\n```cpp\ncode\n```\n\nВопрос',
    'Выделенный фрагмент кода: {"path":"main.cpp","startLine":0,"endLine":1}\n```cpp\ncode\n```\n\nВопрос',
    'Выделенный фрагмент кода: {"path":"main.cpp","startLine":2,"endLine":1}\n```cpp\ncode\n```\n\nВопрос',
    composeTeacherAiMessage('Вопрос', attachment).replace('\n```\n\n', '\n``\n\n'),
    composeTeacherAiMessage('', attachment),
  ])('leaves legacy or malformed message content untouched: %s', (content) => {
    expect(parseTeacherAiMessage(content)).toEqual({ question: content });
  });
});
