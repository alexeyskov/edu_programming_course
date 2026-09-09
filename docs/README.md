# Индекс проектной документации

Статус на 26 августа 2026 года: реализован технический baseline v1; production
и экзаменационная готовность не подтверждены. Документы 01–13 одновременно
содержат требования, целевую архитектуру и критерии следующих фаз. Поэтому
формулировки «должна», «целевой» и acceptance item нельзя читать как отчёт о
готовности.

Если описание расходится с кодом, текущий REST-контракт задают FastAPI OpenAPI и
Pydantic schemas, DB — committed Alembic migrations, runner — его models/profiles,
Moodle pluginless — typed contracts `moodle_browser`, а optional plugin —
`version.php`/`db/services.php`. Этот индекс фиксирует человеко-читаемую границу
текущего среза.

## Фактическая готовность baseline v1

| Область | Реализовано | Не заявляется готовым |
| --- | --- | --- |
| Backend | FastAPI, Pydantic v2, async SQLAlchemy 2, Alembic, PostgreSQL, REST `/api/v1` | Django отсутствует; WebSocket/realtime и внешний broker отсутствуют |
| Identity | Playwright pluginless Moodle login, encrypted leased browser state, server session, CSRF; только `STUDENT`/`TEACHER` | LTI 1.3/MFA/локальная регистрация отсутствуют |
| Admin | optional login field и временная `SYSTEM_SETTINGS` capability | это не роль и не способ войти без LMS |
| Course | глобальный admin allow-list, импорт URL разрешённого Moodle origin, teacher check, sections/groups/roster/activities, scheduled sync | второй LMS connector и webhook/change feed не реализованы |
| Работы | Moodle-owned проекция названия, условия, сроков, максимального балла и попыток; локальное включение для выбранных Moodle-групп | локальный банк заданий и создание Moodle activities скрыты/отложены |
| Moodle pluginless | Playwright course/activity discovery; mapped Quiz Essay `ESSAY_ATTACHMENT`/`ESSAY_ONLINE_TEXT` и Assignment `ASSIGN_FILE`/`ASSIGN_ONLINE_TEXT`, draft/final outbox; постраничный идемпотентный read-only импорт финальных Quiz Essay/Assignment сдач, online text, файлов, оценок и комментариев; запись grade/comment через штатные формы Assignment и Quiz Essay | live production validation не выполнена; фактические MMCS Assignment save/finalize и grading markup требуют эксплуатационной проверки; история набора не импортируется; task mirror отсутствует |
| Optional Moodle 0.3 | task-version mirror, `mod_assign` mapping, grade/comment, checkpoint/recovery | plugin alpha не проверен на реальной staging Moodle; activities/questions не создаёт |
| Workspace | REST save, revision/idempotency, 64 files/512 KiB, semantic events, SHA-256 chain, snapshots | IndexedDB recovery, WebSocket ack/presence и event streaming отсутствуют |
| Paste | frontend blocks native external paste for strict policy; server verifies internal receipt | обход вторым устройством/перепечатывание не предотвращается |
| Deadline | server deadline/manual submit/autosubmit; `D/10` → `D/20`, 30–900 с, final minute | client clock offset API и high-load exam proof отсутствуют |
| Runner | HMAC, fixed GCC/Clang C17/C++20 profiles, ordinary subprocess в отдельном контейнере, limits, diagnostics; `.txt` runtime data в cwd executable, временные generated text/binary files | `UNRESTRICTED_CONTAINER`: per-job filesystem/network sandbox нет; runtime outputs transient и не возвращаются в workspace; hardened target отложен |
| Review | список всех доступных сдач с вкладками «Все сданные»/«Ожидают проверки»/«Проверенные», lease/heartbeat, immutable submission, private experiment, hidden-test report, immutable history решений и перепроверка | realtime presence, sanitizer/static и rubric orchestration не завершены |
| СППР | persisted teacher-only deterministic hidden-test reports, winnowing similarity, side-by-side comparison совпавшей пары с подсветкой, external authorship API, student/teacher AI threads | evidence не является вердиктом; automatic score, AST/multi-signal plagiarism и eval-complete AI policy отсутствуют |
| Retention | значение `retention_days` сохраняется в system settings | cleanup/auto-delete job не реализован |
| UI | desktop/large tablets от 768 px | телефоны, accessibility certification и полная crash/offline recovery не заявлены |

## Moodle: точная граница

- Course sync хранит bounded activity projection. Сроки меняются только у
  assessment с явным mapping на обнаруженный `mod_assign` либо `mod_quiz`;
  cutoff приоритетнее due. Исчезнувший mapping помечается, но прежние сроки не
  обнуляются.
- Pluginless Playwright sync для mapped Quiz fail-closed требует ровно один
  Essay question и доказанный response control. `ESSAY_ONLINE_TEXT` записывает
  полный однофайловый UTF-8 source в text control; `ESSAY_ATTACHMENT` отправляет
  один source либо deterministic ZIP нескольких файлов. Artifact ограничен 4 MiB, внутренний request
  6 MiB. `PERIODIC`/`FINAL_MINUTE` дают `DRAFT_SAVED`, а
  `SUBMISSION`/`DEADLINE` — `FINALIZED`. Локальный snapshot authoritative, а
  outbox выполняет lease/retry со стабильным idempotency key. Browser cache
  process-local, поэтому exactly-once после ambiguous restart не заявляется без
  staging-теста.
- Стандартный Assignment поддержан для нового студенческого ответа,
  read-only исторического импорта и преподавательской записи
  балла/комментария. Browser-worker по фактической student form
  доказывает `ASSIGN_FILE` и/или `ASSIGN_ONLINE_TEXT`; при наличии
  обоих выбирает file transport, а неоднозначная или изменившаяся
  форма блокирует mutation. Этот transport покрыт fixtures/tests,
  но до пилота нужен live staging save/finalize/read-back на MMCS Moodle.
- Опубликованная/архивированная task version синхронизируется в отдельную
  connector-owned таблицу только optional plugin. Это task mirror с immutable
  conflict checks и read-back snapshot, а не native Moodle question-bank
  upsert; pluginless task bank не зеркалирует.
- Grade/comment через Playwright записываются через canonical Assignment grader
  либо manual-grading form конкретного Quiz Essay. Доставка использует durable
  outbox и точные внешние identifiers; перед mutation проверяются course/cmid,
  пользователь либо attempt/slot и структура формы. Независимый read-back и
  conflict engine ещё отсутствуют, поэтому изменившаяся/неоднозначная форма
  блокируется fail-closed, а live staging validation остаётся обязательной.
- В file transport ровно один source отправляется как `main.c` или
  `main.cpp`; наличие любого второго файла даёт детерминированный
  `submission.zip`. ZIP содержит `.c/.cc/.cpp/.cxx`,
  `.h/.hh/.hpp/.hxx`, `.inc` и `.txt`,
  сохраняя безопасные relative POSIX paths; compiler output, IDE history и
  runtime-generated files в ответ не входят.
- После LMS sync отдельные bounded outbox jobs постранично читают через
  Playwright уже существующие финальные ответы связанных Quiz с Essay и
  стандартных Assignment. Финальный код/файлы, доступные оценки и комментарии
  материализуются идемпотентно как локальные read-only imports. Истории набора,
  clipboard provenance и промежуточных snapshots в Moodle нет; интерфейс
  показывает это явно. Assignment online text и attachments не
  взаимоисключаются: оба вида ответа сохраняются. ZIP-вложения
  распаковываются только после проверок путей/типов/лимитов и сохраняют
  relative paths. Парсер реальной MMCS Assignment grading/review разметки
  должен пройти эксплуатационную проверку.
- В optional bridge checkpoint содержит полный canonical manifest, SHA-256
  точных UTF-8 байтов, `event_chain_head`, epoch и workspace revision. Это
  внешний recovery anchor, а не доказательство самостоятельного авторства.
  Каноническую сериализацию выполняет Python backend; PHP bridge валидирует JSON
  и exact-byte hash. Pluginless Quiz/Assignment получает только answer
  artifact/online text, но не local manifest и не историю редактирования.
- LTI 1.3, NRPS, AGS, Deep Linking и native two-way bank editor — будущий этап.

## Рекомендуемый порядок чтения

1. [Видение, границы и требования](01-product-requirements.md)
2. [Аудит старого прототипа и реализуемость](02-legacy-audit-and-feasibility.md)
3. [Целевая архитектура и стек](03-architecture-and-stack.md)
4. [Доменная модель и хранение данных](04-domain-and-data-model.md)
5. [IDE, история редактирования и контроль вставки](05-ide-history-and-authorship.md)
6. [Runner, преподавательская песочница и СППР](06-runner-and-decision-support.md)
7. [ИИ-ассистенты](07-ai-assistants.md)
8. [Интеграция с Moodle и аудит курса 549](08-moodle-integration.md)
9. [Внешние роли, административное повышение и UX](09-roles-and-ux.md)
10. [API и интеграционные контракты](10-api-blueprint.md)
11. [Безопасность, надёжность и эксплуатация](11-security-and-operations.md)
12. [Roadmap, тестирование и приёмка](12-roadmap-and-acceptance.md)
13. [Реестр решений и открытых вопросов](13-decisions-and-open-questions.md)

Исторические наблюдения в документе 02 относятся к старому прототипу и не
переписываются под текущее состояние. Аудит страниц курса 549 в документе 08
тоже остаётся зафиксированным наблюдением; ниже него добавлена отдельная граница
реализованного адаптера.

## Термины

- **Работа** — локально включённая для Moodle-групп проекция лабораторной,
  самостоятельной, контрольной или экзаменационной Moodle activity.
- **Задание** — вопрос/slot внутри Moodle activity. Локальная versioned task
  model является отложенным техническим заделом и не показана в текущем UI.
- **Попытка** — ограниченный серверным временем сеанс студента.
- **Workspace** — один или несколько файлов попытки.
- **Snapshot/checkpoint** — соответственно локальное полное authoritative
  состояние и intent асинхронной доставки: Quiz Essay attachment/online text в Playwright
  mode либо recovery envelope в optional Moodle bridge.
- **СППР** — evidence и рекомендации для решения преподавателя; не автоматическое
  выставление оценки.
- **Закрепление проверки (в коде `ReviewClaim`/lease)** — временная серверная
  аренда submission, не допускающая одновременного изменения оценки двумя
  преподавателями. В пользовательском интерфейсе слово `claim` не выводится.
- **LMS connector** — сменный port; текущий default adapter — Moodle Playwright,
  bridge 0.3 остаётся optional enhanced mode.
- **Task mirror** — connector-owned копия immutable programming-task definition;
  не native Moodle question.
- **Административное повышение** — временная capability LMS-пользователя после
  проверки отдельного токена; не третья роль.
