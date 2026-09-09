import type {
  Assessment, Attempt, AuthConnection, Course, CourseCatalogEntry, HistoryEvent, RunResult, Session,
  Submission, SystemSettings, TeacherExperiment, WorkspaceFile,
} from '../types';

const now = Date.now();
const inMinutes = (minutes: number) => new Date(now + minutes * 60_000).toISOString();
const agoMinutes = (minutes: number) => new Date(now - minutes * 60_000).toISOString();

export const demoConnections: AuthConnection[] = [
  { id: 'moodle-mmcs', name: 'Moodle мехмата ЮФУ', kind: 'MOODLE', enabled: true, loginMode: 'CREDENTIALS' },
];

export const studentSession: Session = {
  id: 'student-demo',
  displayName: 'Анна Смирнова',
  providerName: 'Moodle мехмата ЮФУ',
  memberships: [
    { courseId: 'cpp-2026', courseName: 'Основы программирования C/C++', role: 'STUDENT', group: '1.2' },
  ],
  capabilities: ['course.view', 'assessment.view', 'attempt.start', 'attempt.write', 'attempt.submit', 'ai.student_use'],
};

export const teacherSession: Session = {
  id: 'teacher-demo',
  displayName: 'Алексей Петров',
  providerName: 'Moodle мехмата ЮФУ',
  memberships: [
    { courseId: 'cpp-2026', courseName: 'Основы программирования C/C++', role: 'TEACHER' },
    { courseId: 'alg-2026', courseName: 'Алгоритмы и структуры данных', role: 'TEACHER' },
  ],
  capabilities: ['course.view', 'course.import', 'course.sync', 'assessment.manage', 'task.manage', 'submission.view_course', 'review.claim', 'review.decide', 'ai.teacher_use'],
};

export const demoCourses: Course[] = [
  {
    id: 'cpp-2026', title: 'Основы программирования C/C++', shortName: 'C/C++ · 1 курс',
    role: 'STUDENT', group: '1.2', term: 'Весна 2026', provider: 'Moodle', syncStatus: 'SYNCED',
    syncedAt: agoMinutes(8), activeCount: 3,
  },
  {
    id: 'alg-2026', title: 'Алгоритмы и структуры данных', shortName: 'Алгоритмы',
    role: 'TEACHER', term: 'Весна 2026', provider: 'Moodle', syncStatus: 'STALE',
    syncedAt: agoMinutes(93), activeCount: 4, uncheckedCount: 7,
  },
];

export const studentCourse: Course = { ...demoCourses[0], role: 'STUDENT' };
export const teacherCourse: Course = {
  ...demoCourses[0], role: 'TEACHER', group: undefined, uncheckedCount: 12, activeCount: 4,
};

export const demoCourseCatalog: CourseCatalogEntry[] = [
  {
    id: 'cpp-2026', connectionId: 'moodle-mmcs', connectionName: 'Moodle мехмата ЮФУ',
    externalId: '549', title: 'Основы программирования C/C++', shortName: 'C/C++ · 1 курс',
    externalUrl: 'https://edu.mmcs.sfedu.ru/course/view.php?id=549', syncStatus: 'SYNCED',
    addedAt: agoMinutes(8),
  },
];

export const demoAssessments: Assessment[] = [
  {
    id: 'lab-vectors', courseId: 'cpp-2026', title: 'Лабораторная №4 · Векторы и итераторы',
    summary: 'Практика работы с std::vector, алгоритмами STL и пользовательскими функциями.',
    kind: 'LAB', status: 'IN_PROGRESS', deadlineAt: inMinutes(46), durationMinutes: 120,
    attemptId: 'attempt-demo', progress: 62, maxScore: 10, fileMode: 'MULTI', language: 'CPP',
    standard: 'C++20', pastePolicy: 'STRICT', aiEnabled: true, submissionsCount: 27, uncheckedCount: 4,
  },
  {
    id: 'control-pointers', courseId: 'cpp-2026', title: 'Контрольная №2 · Указатели и память',
    summary: 'Индивидуальный вариант. Динамические структуры и корректное управление памятью.',
    kind: 'CONTROL', status: 'UPCOMING', startsAt: inMinutes(24 * 60), deadlineAt: inMinutes(25 * 60 + 30),
    durationMinutes: 90, maxScore: 20, fileMode: 'SINGLE', language: 'CPP', standard: 'C++20',
    pastePolicy: 'STRICT', aiEnabled: false, submissionsCount: 0, uncheckedCount: 0,
  },
  {
    id: 'independent-strings', courseId: 'cpp-2026', title: 'Самостоятельная · Строки',
    summary: 'Обработка текста без регулярных выражений.', kind: 'INDEPENDENT', status: 'GRADED',
    deadlineAt: agoMinutes(2880), progress: 100, score: 8, maxScore: 10, fileMode: 'SINGLE',
    language: 'CPP', standard: 'C++17', pastePolicy: 'STRICT', aiEnabled: false,
    submissionsCount: 31, uncheckedCount: 0,
  },
  {
    id: 'exam-final', courseId: 'cpp-2026', title: 'Экзаменационная работа',
    summary: 'Итоговая работа по материалам семестра.', kind: 'EXAM', status: 'UPCOMING',
    startsAt: inMinutes(21 * 24 * 60), durationMinutes: 180, maxScore: 40, fileMode: 'MULTI',
    language: 'CPP', standard: 'C++20', pastePolicy: 'STRICT', aiEnabled: false,
    submissionsCount: 0, uncheckedCount: 0,
  },
];

const attemptFiles: WorkspaceFile[] = [
  {
    id: 'file-main', path: 'src/main.cpp', language: 'cpp', content: `#include <iostream>\n#include <vector>\n#include "statistics.hpp"\n\nint main() {\n    std::vector<int> values{4, 8, 15, 16, 23, 42};\n    std::cout << mean(values) << '\\n';\n    return 0;\n}\n`,
  },
  {
    id: 'file-header', path: 'include/statistics.hpp', language: 'cpp', content: `#pragma once\n\n#include <vector>\n\ndouble mean(const std::vector<int>& values);\n`,
  },
  {
    id: 'file-source', path: 'src/statistics.cpp', language: 'cpp', content: `#include "statistics.hpp"\n\ndouble mean(const std::vector<int>& values) {\n    int sum = 0;\n    for (int value : values) {\n        sum += value;\n    }\n    return static_cast<double>(sum) / values.size()\n}\n`,
  },
];

export const demoAttempt: Attempt = {
  id: 'attempt-demo', assessmentId: 'lab-vectors', title: 'Лабораторная №4 · Векторы и итераторы',
  statement: 'Реализуйте функцию `mean`, вычисляющую среднее арифметическое элементов вектора. Разделите объявление и реализацию по файлам. Пустой вектор обрабатывать не требуется.',
  status: 'ACTIVE', revision: 18, acknowledgedRevision: 18, startedAt: agoMinutes(74),
  deadlineAt: inMinutes(46), lastCheckpointAt: agoMinutes(7), checkpointStatus: 'SYNCED',
  pastePolicy: 'STRICT', aiEnabled: true, fileMode: 'MULTI', files: attemptFiles,
};

export const demoHistory: HistoryEvent[] = [
  { id: 'h5', type: 'snapshot', label: 'Контрольная точка Moodle', detail: 'Ревизия 18 синхронизирована', at: agoMinutes(7), revision: 18 },
  { id: 'h4', type: 'edit', label: 'Изменён statistics.cpp', detail: '3 операции · набор с клавиатуры', at: agoMinutes(9), revision: 18 },
  { id: 'h3', type: 'run', label: 'Запуск программы', detail: 'Ошибка компиляции · 428 мс', at: agoMinutes(11), revision: 15 },
  { id: 'h2', type: 'internal_paste', label: 'Внутренняя вставка', detail: '5 символов из main.cpp', at: agoMinutes(19), revision: 12 },
  { id: 'h1', type: 'edit', label: 'Созданы исходные файлы', detail: 'Шаблон задания', at: agoMinutes(74), revision: 1 },
];

export const failedRun: RunResult = {
  id: 'run-demo', state: 'FAILED', revision: 18, exitCode: 1, stdout: '',
  stderr: `src/statistics.cpp:9:54: error: expected ';' after return statement`, durationMs: 428,
  diagnostics: [{
    id: 'diag-1', fileId: 'file-source', path: 'src/statistics.cpp', line: 9, column: 54,
    endLine: 9, endColumn: 55, severity: 'error', code: 'expected_semi_after_return',
    message: "Ожидался символ ';' после выражения return", notes: ['Добавьте точку с запятой перед закрывающей фигурной скобкой.'],
  }], createdAt: agoMinutes(11),
};

const submissionFiles: WorkspaceFile[] = [
  { id: 's-main', path: 'main.cpp', language: 'cpp', content: `#include <iostream>\n#include <vector>\n\nint main() {\n    int n;\n    std::cin >> n;\n    std::vector<int> a(n);\n    for (int& value : a) std::cin >> value;\n\n    int best = 0;\n    for (int value : a) {\n        if (value > best) best = value;\n    }\n    std::cout << best << '\\n';\n}\n` },
];

export const demoSubmissions: Submission[] = [
  { id: 'sub-1', assessmentId: 'control-pointers', assessmentTitle: 'Контрольная №2', studentName: 'Мария Воронова', studentGroup: '1.1', submittedAt: agoMinutes(12), status: 'UNGRADED', maxScore: 20, risk: 'LOW', testsPassed: 8, testsTotal: 8, files: submissionFiles, history: demoHistory, decisionHistory: [] },
  { id: 'sub-2', assessmentId: 'control-pointers', assessmentTitle: 'Контрольная №2', studentName: 'Илья Морозов', studentGroup: '1.2', submittedAt: agoMinutes(18), status: 'CLAIMED', maxScore: 20, risk: 'MEDIUM', testsPassed: 6, testsTotal: 8, files: submissionFiles, history: demoHistory, decisionHistory: [], claim: { id: 'claim-other', ownerId: 'other', ownerName: 'Елена Сергеева', expiresAt: inMinutes(1), mine: false } },
  { id: 'sub-3', assessmentId: 'lab-vectors', assessmentTitle: 'Лабораторная №4', studentName: 'Никита Орлов', studentGroup: '1.2', submittedAt: agoMinutes(36), status: 'UNGRADED', maxScore: 10, risk: 'HIGH', testsPassed: 4, testsTotal: 7, files: submissionFiles, history: demoHistory, decisionHistory: [] },
  { id: 'sub-4', assessmentId: 'independent-strings', assessmentTitle: 'Самостоятельная · Строки', studentName: 'Софья Лебедева', studentGroup: '1.1', submittedAt: agoMinutes(85), status: 'GRADED', score: 9, maxScore: 10, risk: 'LOW', testsPassed: 9, testsTotal: 9, files: submissionFiles, history: demoHistory, latestDecision: { id: 'decision-demo', reviewerName: 'Алексей Коваленко', grade: 9, comment: 'Работа проверена.', revision: 1, reviewedAt: agoMinutes(70), lmsExportState: 'DELIVERED' }, decisionHistory: [{ id: 'decision-demo', reviewerName: 'Алексей Коваленко', grade: 9, comment: 'Работа проверена.', revision: 1, reviewedAt: agoMinutes(70), lmsExportState: 'DELIVERED' }] },
  { id: 'sub-5', assessmentId: 'control-pointers', assessmentTitle: 'Контрольная №2', studentName: 'Даниил Козлов', studentGroup: '1.3', submittedAt: agoMinutes(7), status: 'CONFLICT', maxScore: 20, risk: 'UNKNOWN', testsPassed: 0, testsTotal: 8, files: submissionFiles, history: demoHistory, decisionHistory: [] },
];

export const demoExperiment: TeacherExperiment = {
  id: 'experiment-demo', submissionId: 'sub-1', revision: 1,
  files: submissionFiles.map((file) => ({ ...file, id: `exp-${file.id}` })), changed: false, createdAt: new Date().toISOString(),
};

export const demoSettings: SystemSettings = {
  aiEnabled: true, studentAiEnabled: true, runnerEnabled: true, runnerCpuSeconds: 4,
  runnerMemoryMb: 256, retentionDays: 365, allowedLmsOrigins: ['https://edu.mmcs.sfedu.ru'],
  incidentBanner: '', services: [
    { name: 'Основной API', status: 'ok', detail: '42 мс' },
    { name: 'Runner', status: 'ok', detail: '2 исполнителя' },
    { name: 'Moodle Bridge', status: 'ok', detail: 'синхронизация 8 мин назад' },
    { name: 'ИИ-провайдер', status: 'degraded', detail: 'повышенная задержка' },
  ],
};

export function cloneDemo<T>(value: T): T {
  return structuredClone(value);
}
