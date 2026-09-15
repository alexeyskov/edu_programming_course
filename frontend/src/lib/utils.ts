import type { AssessmentKind, AssessmentStatus, ClientContext, Diagnostic, WorkspaceFile } from '../types';

export const cppKeywords = [
  'alignas', 'alignof', 'auto', 'bool', 'break', 'case', 'catch', 'char', 'class', 'const',
  'constexpr', 'continue', 'default', 'delete', 'do', 'double', 'else', 'enum', 'explicit',
  'extern', 'false', 'float', 'for', 'friend', 'if', 'inline', 'int', 'long', 'namespace',
  'new', 'nullptr', 'operator', 'private', 'protected', 'public', 'return', 'short', 'signed',
  'sizeof', 'static', 'struct', 'switch', 'template', 'this', 'throw', 'true', 'try', 'typedef',
  'typename', 'union', 'unsigned', 'using', 'virtual', 'void', 'volatile', 'while',
] as const;

export function workspaceIdentifiers(files: WorkspaceFile[]): string[] {
  const keywords = new Set<string>(cppKeywords);
  const identifiers = new Set<string>();
  for (const file of files) {
    for (const match of file.content.matchAll(/\b[A-Za-z_][A-Za-z0-9_]*\b/g)) {
      if (!keywords.has(match[0]) && match[0].length > 1) identifiers.add(match[0]);
    }
  }
  return [...identifiers].sort((left, right) => left.localeCompare(right));
}

export function findWorkspacePasteSource(
  files: WorkspaceFile[],
  text: string,
): WorkspaceFile | undefined {
  if (!text) return undefined;
  const normalized = normalizeClipboardText(text);
  return files.find((file) => normalizeClipboardText(file.content).includes(normalized));
}

// Clipboard/Monaco may translate Windows line endings, but no other characters.
export function normalizeClipboardText(text: string): string {
  return text.replace(/\r\n/g, '\n');
}

export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ');
}

export const kindLabel: Record<AssessmentKind, string> = {
  LAB: 'Лабораторная',
  INDEPENDENT: 'Самостоятельная',
  CONTROL: 'Контрольная',
  EXAM: 'Экзамен',
};

export const statusLabel: Record<AssessmentStatus, string> = {
  UPCOMING: 'Скоро',
  AVAILABLE: 'Доступно',
  IN_PROGRESS: 'В работе',
  SUBMITTED: 'Сдано',
  GRADED: 'Проверено',
  CLOSED: 'Закрыто',
};

export function formatDate(value?: string, options?: Intl.DateTimeFormatOptions): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat('ru-RU', options ?? {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  }).format(date);
}

export function formatClientContext(context?: ClientContext): string {
  if (!context) return '';
  const deviceLabels: Record<string, string> = {
    DESKTOP: 'компьютер',
    MOBILE: 'мобильное устройство',
    TABLET: 'планшет',
    BOT: 'автоматический клиент',
  };
  const browser = [context.browser, context.browserVersion].filter(Boolean).join(' ');
  return [
    context.ipAddress ? `IP: ${context.ipAddress}` : '',
    browser,
    context.operatingSystem,
    context.deviceType ? deviceLabels[context.deviceType] ?? context.deviceType : '',
  ].filter(Boolean).join(' · ');
}

export function formatRemaining(deadline?: string, now = Date.now()): string {
  if (!deadline) return 'Без ограничения';
  const seconds = Math.max(0, Math.floor((new Date(deadline).getTime() - now) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return `${hours ? `${String(hours).padStart(2, '0')}:` : ''}${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
}

export function formatSessionElapsed(startedAt: string, now = Date.now()): string {
  const started = new Date(startedAt).getTime();
  const seconds = Number.isFinite(started) ? Math.max(0, Math.floor((now - started) / 1000)) : 0;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
}

export function severityOrder(value: Diagnostic['severity']): number {
  return value === 'error' ? 0 : value === 'warning' ? 1 : 2;
}

export function unwrapList<T>(data: T[] | { results?: T[]; items?: T[]; data?: T[] }): T[] {
  if (Array.isArray(data)) return data;
  return data.results ?? data.items ?? data.data ?? [];
}

export function initials(name: string): string {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((word) => word[0]?.toUpperCase()).join('');
}

export function formatGreetingName(displayName: string): string {
  const nameParts = displayName.trim().split(/\s+/).filter(Boolean);
  if (!nameParts.length) return 'Студент';
  if (nameParts.length === 1) return nameParts[0];

  const [surname, givenName] = nameParts;
  const initial = [...givenName][0]?.toLocaleUpperCase('ru-RU');
  return initial ? `${surname} ${initial}.` : surname;
}

export function languageForPath(path: string): string {
  if (/\.(c)$/i.test(path)) return 'c';
  if (/\.(h|hh|hpp|hxx|inc|cc|cpp|cxx)$/i.test(path)) return 'cpp';
  return 'plaintext';
}

export function isTranslationUnitPath(path: string): boolean {
  return /\.(c|cc|cpp|cxx)$/i.test(path);
}

export function isTextDataPath(path: string): boolean {
  return /\.txt$/i.test(path);
}

export function validateWorkspacePath(path: string): string | null {
  const normalized = path.trim();
  if (!normalized) return 'Введите имя файла';
  if (normalized.includes('\\')) return 'Используйте прямой слеш / в относительном пути';
  if (normalized.startsWith('/') || normalized.split('/').some((part) => part === '' || part === '.' || part === '..')) {
    return 'Разрешены только относительные пути внутри рабочей области';
  }
  if (!/^[\p{L}\p{N}_./-]+$/u.test(normalized)) return 'В имени есть недопустимые символы';
  if (!/\.(c|cc|cpp|cxx|h|hh|hpp|hxx|inc|txt)$/i.test(normalized)) return 'Допустимы только C/C++ и текстовые файлы';
  return null;
}
