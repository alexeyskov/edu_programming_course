import { describe, expect, it } from 'vitest';
import { findWorkspacePasteSource, formatGreetingName, formatRemaining, formatSessionElapsed, languageForPath, unwrapList, validateWorkspacePath, workspaceIdentifiers } from './utils';

describe('workspace utilities', () => {
  it('accepts only safe C/C++ workspace paths', () => {
    expect(validateWorkspacePath('src/answer.cpp')).toBeNull();
    expect(validateWorkspacePath('include/answer.hh')).toBeNull();
    expect(validateWorkspacePath('include/body.inc')).toBeNull();
    expect(languageForPath('include/body.inc')).toBe('cpp');
    expect(languageForPath('include/answer.hh')).toBe('cpp');
    expect(validateWorkspacePath('fixtures/input.txt')).toBeNull();
    expect(languageForPath('fixtures/input.txt')).toBe('plaintext');
    expect(validateWorkspacePath('../secret.cpp')).toMatch(/относительные пути/);
    expect(validateWorkspacePath('/etc/passwd')).toMatch(/относительные пути/);
    expect(validateWorkspacePath('src\\answer.cpp')).toMatch(/прямой слеш/);
    expect(validateWorkspacePath('src//answer.cpp')).toMatch(/относительные пути/);
    expect(validateWorkspacePath('run.sh')).toMatch(/C\/C\+\+ и текстовые/);
  });

  it('locates a source file when renewing an already verified internal copy', () => {
    const files = [
      { id: 'main', path: 'main.cpp', content: 'int main() { return helper(); }' },
      { id: 'header', path: 'helper.hpp', content: 'int helper();\n' },
    ];
    expect(findWorkspacePasteSource(files, 'int helper();\n')?.id).toBe('header');
    expect(findWorkspacePasteSource(files, 'return helper()')?.id).toBe('main');
    expect(findWorkspacePasteSource(files, 'int copied_from_outside();')).toBeUndefined();
    expect(findWorkspacePasteSource(files, '')).toBeUndefined();
  });

  it('unwraps plain and paginated collections', () => {
    expect(unwrapList([1, 2])).toEqual([1, 2]);
    expect(unwrapList({ results: [3, 4] })).toEqual([3, 4]);
    expect(unwrapList({ items: [5] })).toEqual([5]);
  });

  it('formats a server deadline countdown', () => {
    expect(formatRemaining(new Date(61_000).toISOString(), 0)).toBe('01:01');
    expect(formatRemaining(new Date(3_661_000).toISOString(), 0)).toBe('01:01:01');
    expect(formatRemaining(undefined, 0)).toBe('Без ограничения');
  });

  it('formats approximate elapsed attempt time when Moodle owns the deadline', () => {
    expect(formatSessionElapsed(new Date(1_000).toISOString(), 3_662_000)).toBe('01:01:01');
    expect(formatSessionElapsed('invalid', 3_662_000)).toBe('00:00:00');
  });

  it('formats a Moodle display name for the dashboard greeting', () => {
    expect(formatGreetingName('Коваленко Алексей')).toBe('Коваленко А.');
    expect(formatGreetingName('  Коваленко   алексей Сергеевич ')).toBe('Коваленко А.');
    expect(formatGreetingName('Коваленко')).toBe('Коваленко');
    expect(formatGreetingName('   ')).toBe('Студент');
  });

  it('collects local cross-file identifiers without inventing completions', () => {
    const identifiers = workspaceIdentifiers([
      { id: 'a', path: 'main.cpp', content: 'int calculate_total(int input_value);' },
      { id: 'b', path: 'sum.cpp', content: 'double calculate_total(double second_value) { return second_value; }' },
    ]);
    expect(identifiers).toContain('calculate_total');
    expect(identifiers).toContain('input_value');
    expect(identifiers.filter((value) => value === 'calculate_total')).toHaveLength(1);
    expect(identifiers).not.toContain('return');
  });
});
