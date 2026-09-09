# Компиляция, запуск, преподавательская песочница и СППР

> Статус baseline v1: отдельный synchronous HMAC runner реализует фиксированные
> GCC/Clang C17/C++20 single/multi profiles, ordinary subprocess внутри
> отдельного runner-контейнера, resource limits и structured diagnostics. Per-job
> filesystem/network sandbox временно отсутствует. Backend хранит run results; teacher
> experiment умеет edit/run/reset/delete и UI diff первого файла. Teacher-only
> hidden-test manifest/report v1 реализован для immutable submission и не
> выставляет балл. Public-suite orchestration, sanitizer/static-analysis,
> versioned rubric, приоритетная очередь и multi-file diff остаются следующими
> фазами.

## 1. Принятая политика доверия

Код студентов и преподавателей считается непроверенным, но для первой версии не
считается заведомо враждебным. Проект временно принимает выполнение ordinary
subprocess без per-job filesystem/network sandbox. Фактическая граница —
отдельный непривилегированный runner-контейнер без application/DB/Moodle/AI
mounts и Docker socket, с read-only rootfs, private tmpfs и resource limits.
Код при этом может читать доступные контейнеру файлы и пользоваться его сетью.

Основной backend:

- не вызывает `g++`;
- не запускает бинарники;
- не распаковывает недоверенные архивы;
- не монтирует student workspace;
- не имеет Docker socket.

Он создаёт immutable run request и общается только с runner service/gateway. В пилоте runner может жить на том же physical host, но под отдельной непривилегированной identity и вне процесса приложения.

## 2. Runner architecture

### 2.1. Компоненты

Список ниже — целевая архитектура. Текущий runner имеет synchronous endpoint и
локальный concurrency semaphore; отдельной priority queue и artifact uploader
нет.

- scheduler/queue с приоритетами exam > interactive > background SPPR;
- runner gateway, проверяющий подпись request и limits profile;
- file-isolated job launcher на Linux;
- immutable toolchain images;
- artifact uploader с write-only temporary credentials;
- result normalizer.

### 2.2. Временный реализованный профиль `UNRESTRICTED_CONTAINER`

По решению владельца продукта текущая реализация запускает compiler/binary
обычным subprocess внутри отдельного runner Docker-контейнера. Bubblewrap и
per-job filesystem/network namespace отключены. Результат обязан сообщать
`filesystem_isolated=false`, `network=host`; backend сохраняет эти признаки.
Сохраняются fixed command profiles, отдельный working directory, cleanup,
process-tree kill и CPU/memory/PID/time/output/workspace limits.

Ниже сохранён целевой профиль для последующего возврата sandbox:

### 2.3. Целевой профиль `FILESYSTEM_ONLY`

- один непривилегированный OS process tree/rootless container на run request;
- per-job UID и private mount/filesystem namespace либо эквивалентный allow-list executor;
- source snapshot монтируется read-only на этапе compile;
- отдельные writable `build` и `tmp`; размер ограничен;
- toolchain/sysroot/headers/libraries доступны только read/execute;
- application directories, DB, object storage, secrets, Unix sockets, homes и соседние job paths отсутствуют в namespace;
- environment создаётся по allow-list и не наследует secrets runner/core;
- runtime view содержит executable, нужные runtime libraries и только объявленные task data files; compiler/source/object files по умолчанию не видны;
- файлы ввода задания read-only, разрешённая output directory writable; произвольный host path не может быть target;
- symlink/hardlink/path traversal проверяются до запуска и не позволяют выйти из private tree;
- cleanup и kill process tree выполняет внешний watchdog.

Возможные реализации: rootless OCI container, `bubblewrap`/mount namespace или эквивалентная политика systemd/OS. Конкретный механизм выбирается ADR и подтверждается файловыми contract tests. Простого `cwd` и Unix permissions одного общего пользователя недостаточно.

### 2.4. Остаточный риск, принимаемый владельцем

Профиль `FILESYSTEM_ONLY` не обещает:

- отдельного kernel и защиты от kernel/container escape;
- строгой фильтрации всех syscalls;
- защиты от side channels и уязвимости compiler/runtime;
- безопасного network access в гипотетическом будущем profile;
- сохранности host при неизвестной privilege-escalation уязвимости.

Текущий interim baseline сеть не выключает (`network=host`). Целевой
`FILESYSTEM_ONLY` должен вернуть `network=denied`; усиленный будущий профиль
`HARDENED` может добавить gVisor/microVM и syscall filtering без изменения API.

### 2.5. Обязательные лимиты доступности

| Фаза | CPU | Wall | Memory | PIDs | Output | Workspace |
| --- | --- | --- | --- | --- | --- | --- |
| Compile single | 10 s | 20 s | 1 GiB | 64 | 1 MiB | 20 MiB |
| Compile multi | 20 s | 40 s | 2 GiB | 96 | 2 MiB | 50 MiB |
| One test | 2 s | 3 s | 256 MiB | 32 | 256 KiB | ephemeral |
| Interactive lab | 10 s | 30 s | 256 MiB | 32 | 1 MiB | ephemeral |
| Static analysis | 20 s | 40 s | 2 GiB | 64 | 2 MiB | 50 MiB |

Первые две compile-строки и `One test` (runtime phase текущего runner profile)
реализованы. Evidence wrapper поверх одного case имеет отдельный default wall
budget 8 секунд, включающий compile/HTTP overhead, и общий report budget 50
секунд. `Interactive lab` и `Static analysis` — целевые профили, не доступные по
текущему API. Optional runtime CPU/memory из system settings могут
только снизить 2 CPU seconds/256 MiB, но не увеличить profile; wall/PID/output и
compile limits через request не меняются.

Эти лимиты не означают недоверие к студенту: они защищают общую систему от обычного бесконечного цикла, рекурсии, утечки памяти, fork loop и случайно огромного вывода. Лимиты являются profile versions. Глобальные границы меняются только в сессии с административным повышением, а преподаватель выбирает разрешённый профиль курса. Экзаменационный профиль фиксируется до начала.

## 3. Build profiles

### Single-file

Система генерирует команду по template: compiler digest, language standard, flags, source path и target. Студент не передаёт произвольные compiler flags.

### Multi-file

Система генерирует build manifest из списка файлов и преподавательского profile. Варианты:

- compile all `.cpp/.cc/.cxx` в один executable;
- compile specified source list;
- library + teacher harness;
- несколько targets, но только в лабораторных поздней версии.

Локальные include сохраняются как отдельные файлы; проект не объединяется в один `.cpp`. Это даёт корректную семантику translation units, ODR и линковки.

Runner принимает `.c` для C либо `.cpp/.cc/.cxx` для C++ и headers
`.h/.hh/.hpp/.hxx/.inc`. Core workspace использует тот же набор и дополнительно
допускает `.txt`. Перед запуском `.txt` копируются в private writable cwd рядом
с executable: программа может читать, перезаписывать их и создавать собственные
текстовые или бинарные файлы обычными средствами C/C++. Этот каталог временный,
удаляется после run/session и не переносит runtime-generated файлы обратно в
workspace или историю редактирования.

### Toolchain reproducibility

Baseline response/storage уже связывает run с requested revision/profile,
compiler family/version, manifest/executable SHA-256 и executor/filesystem policy
через result metrics. Image digest, task/test-suite hash и UI replay selector из
списка ниже ещё target.

Целевой расширенный result должен хранить:

- image digest;
- compiler version;
- standard and flags;
- generated build manifest hash;
- task/test suite hashes;
- executor, filesystem-policy and optional hardening versions.

Выбор при повторной проверке «тот же toolchain»/«актуальный» и UI сравнения
версий пока не реализованы.

## 4. Выполнение тестов

### 4.1. Реализованный hidden-test manifest v1

`TaskVersion.hidden_test_manifest` принимает либо ровно `{}` (проверки
отключены), либо объект `schema_version: 1` с 1–20 cases. У case есть уникальное
без учёта регистра имя до 100 символов, `stdin`, `expected_stdout` до 262144
байт UTF-8 каждый и comparison `EXACT|TRIM_TRAILING_WHITESPACE`. Канонически
сериализованный manifest ограничен 1 MiB. Команды shell, paths, environment,
веса и runner flags в этот контракт не входят.

Преподаватель запускает cases только для immutable submission snapshot. Каждый
case создаёт отдельный `RunRequest` origin `IMMUTABLE_SUBMISSION`, mode `TEST` и
отдельный ordinary subprocess/unique working directory; per-job sandbox сейчас
отсутствует. Student API не выдаёт manifest или report. Teacher report
содержит status `RUNNING|COMPLETED|FAILED`, hashes manifest/task/snapshot,
`PASSED|FAILED|INFRASTRUCTURE_ERROR` для каждого выполненного case, exit code,
hashes ожидаемого/фактического stdout, bounded stdout/stderr preview и фактические
isolation flags. Неверный ответ или ошибка student program не останавливают
следующий case; недоверенный/недоступный runner завершает report как durable
`FAILED` с уже сохранёнными partial outcomes.

Это не общий test-suite engine. Публичные примеры пока являются содержимым
версии задания, а не отдельной оркестрацией; student-run/teacher-visible suites,
sanitizers, static analysis, teacher harness и data-file mapping остаются target.
Иерархия status вроде `WRONG_ANSWER`, `TIME_LIMIT`, `MEMORY_LIMIT` как единый
per-test API тоже target: текущий official outcome намеренно свёрнут к трём
значениям выше, а детали остаются в linked `RunResult`/finding.

## 5. Целевой интерактивный запуск

Baseline принимает bounded `stdin` целиком и синхронно возвращает bounded
stdout/stderr; живой WebSocket terminal/stream отсутствует.

- WebSocket соединение браузера идёт к core, а не напрямую к execution job;
- stdin ограничивается по скорости и объёму;
- stdout/stderr нормализуются и ограничиваются;
- binary завершается по idle/wall timeout;
- клиент может закрыть сессию; server watchdog закрывает orphan;
- никакой интерактивный процесс не переживает попытку или deployment.

## 6. Детерминированная часть СППР

Baseline flow:

1. Проверить внешний `TEACHER` membership, `review_required`,
   `decision_support_enabled` и active owned review claim.
2. Проверить, что runner включён и не mock, task version опубликована, hidden
   manifest валиден, snapshot/каждый file hash совпадают.
3. Создать persisted `RUNNING` report и фиксировать каждый child run/result и
   partial outcome отдельными транзакциями, не удерживая DB lock во время HTTP.
4. Для каждого case выполнить snapshot через `UNRESTRICTED_CONTAINER` с CPU
   limit не выше 2 секунд по default.
5. Сохранить фактические `filesystem_isolated=false`/`network_enabled=true`;
   infrastructure failure не трактовать как ошибку решения студента.
6. Завершить durable report; никакой grade/review decision из него не создавать.

Endpoint синхронный и жёстко ограничен: default wall 8 секунд на case, 50 секунд
на весь report (конфигурационный максимум 60, ниже Nginx timeout 75), CPU 2
секунды на case. На преподавателя разрешён один concurrent report и 5 reports за
300 секунд. Через 120 секунд незавершённый report считается stale; retry
переводит его в `FAILED` и освобождает partial-unique slot. Для пары
submission/snapshot может существовать только один `RUNNING` report. Optional
`Idempotency-Key` возвращает прежний report того же teacher/submission вместо
повторного запуска.

Ссылка на report не является готовой ссылкой решения: текущий
`review_decision.evidence_ids` принимает individual official case run IDs и
повторно проверяет completed report, exact submission/snapshot/task hashes,
exact origin/snapshot/case linkage. Private teacher experiment не проходит эту
проверку.

Следующая фаза добавит public/visible suites, sanitizers, static checks,
versioned rubric и, при необходимости, отдельную bounded AI recommendation.
ИИ не должен генерировать test harness, который подменяет алгоритм студента, как
основной источник оценки. LLM-generated tests допустимы лишь как явно
неофициальный эксперимент; teacher-authored deterministic cases остаются
проверяемым источником.

## 7. Диагностики

Единый формат диагностики:

- producer/source and version;
- workspace origin: student attempt, immutable submission или teacher experiment;
- exact source snapshot/revision and build profile;
- file ID/path;
- start/end line and columns;
- severity info/warning/error;
- stable code;
- message;
- child/related notes and template-instantiation chain;
- compiler fix-it range/text, если producer действительно его сообщил;
- optional teacher-only AI explanation/remediation;
- visibility policy;
- related run/evidence.

Baseline структурированно парсит compiler diagnostics: GCC JSON и Clang SARIF,
с version-tolerant text fallback; raw stderr сохраняется bounded рядом с
normalized records. Clang SARIF официально нестабилен, поэтому adapter должен
оставаться привязанным к toolchain version. clang-tidy/sanitizer producers и их
structured pipeline пока target.

Paths маппятся на file IDs без раскрытия executor path. Диагностика generated harness/system header показывается с корректным origin; UI не должен приписывать ей случайную строку student file. Linker error без source location остаётся в Problems/build log и не получает выдуманную строку.

### 7.1. Отображение в IDE

- Monaco marker подчёркивает точный range и показывает message при наведении;
- gutter отмечает строки с error/warning;
- панель Problems группирует сообщения по файлу и severity;
- клик открывает файл и переводит cursor на line/column;
- связанные `note` раскрываются деревом под основной ошибкой;
- build log остаётся доступен как первичный output;
- stale diagnostics скрываются или помечаются, когда editor revision изменилась;
- fix-it никогда не применяется автоматически: студенту доступен preview в разрешённом режиме, преподаватель может применить его только к своей experiment copy;
- student не видит hidden harness/test paths и teacher-only explanation.

Официальные форматы: [GCC diagnostic formatting](https://gcc.gnu.org/onlinedocs/gcc/Diagnostic-Message-Formatting-Options.html) и [Clang diagnostic formats](https://clang.llvm.org/docs/UsersManual.html#cmdoption-clang-fdiagnostics-format).

## 8. Преподавательская песочница

Термин «песочница» здесь означает безопасную для оригинала рабочую копию, а не обещание усиленной kernel isolation.

### 8.1. Жизненный цикл

1. Преподаватель открывает immutable submission и нажимает «Открыть в песочнице».
2. Core создаёт private `teacher_experiment` из exact submission snapshot; оригинал остаётся read-only.
3. Monaco открывает редактируемые multi-file models с заметной рамкой «Эксперимент преподавателя».
4. Изменения получают отдельную revision и сохраняются для reload, но не
   смешиваются со student authorship history; полноценного event stream
   эксперимента baseline не экспортирует.
5. Compile/run создаёт request с origin `TEACHER_EXPERIMENT` и использует тот же `UNRESTRICTED_CONTAINER`/build profile.
6. Diagnostics привязываются к exact experiment revision и отображаются по строкам.
7. «Сравнить» в baseline показывает diff первого файла; multi-file diff —
   следующий UI этап. «Сбросить» возвращает base snapshot, а «Удалить» ставит
   `deleted_at` у private copy по явному действию преподавателя.

### 8.2. Инварианты

- edit/reset/run не меняют submission, student history, deadline или Moodle checkpoint;
- experiment result не становится официальным evidence автоматически;
- преподаватель может сослаться на experiment в своём комментарии, но final decision хранит явный origin;
- автоматическая вставка AI-generated teacher code в experiment в baseline
  отсутствует; если будет добавлена, она должна требовать отдельного подтверждения;
- одновременно можно иметь ограниченное число private experiments; default — одна активная на преподавателя/submission;
- другой преподаватель не видит private experiment без отдельной функции sharing, не входящей в MVP;
- `expires_at` проверяется при доступе к experiment, но отдельного cleanup job
  нет; `retention_days` не запускает автоудаление. Original submission и review
  decision не меняются ни expiry, ни soft delete experiment.

## 9. Преподавательская СППР

Список ниже — целевой полный экран. Baseline показывает submission/run,
diagnostics, claim, comment/grade, integrity/AI и отдельную вкладку persisted
hidden-test reports: преподаватель с active claim запускает report, выбирает
предыдущий, видит partial/final status, cases, hashes, bounded output previews и
isolation flags. Report не подставляет grade. Sanitizer/static findings,
вычисляемый rubric score и фоновая структурированная AI-рекомендация ещё не
оркестрируются.

Экран показывает блоки независимо:

1. Финальная сдача и toolchain.
2. Компиляция и линковка.
3. Тесты с группировкой по критериям рубрики.
4. Runtime/sanitizer/static findings.
5. ИИ-рекомендация с confidence и citations.
6. Плагиат с pairwise fragments.
7. История/авторство.
8. Рубрика и teacher draft.

Recommended score представляется диапазоном, например `6–8`, и раскладывается по критериям. Если evidence конфликтует, UI показывает конфликт, а не усредняет его скрыто.

## 10. Финальное решение

- Только пользователь с `review.decide` может создать decision.
- Decision требует active write claim или override permission.
- Baseline валидирует grade по `Assessment.max_score`; `criterion_scores`
  сохраняются как JSON, но отдельной versioned rubric schema/weight validation
  ещё нет.
- Apply создаёт immutable decision revision и Moodle outbox event.
- AI toggle не влияет на уже выставленное решение.
- Повторная оценка создаёт новую revision и `supersedes_id`; отдельное
  обязательное поле причины изменения пока отсутствует.
- Для экзамена подпись проверяющего сохраняется структурированно; добавлять её в свободный Moodle comment можно только интеграционным formatter.

## 11. Плагиат

### Candidate pipeline

Baseline выполняет bounded lexical normalized-token winnowing, удаляет
совпадающие со starter file последовательности, сохраняет один score и matched
fragments для unordered pair, затем позволяет преподавателю вести case.
Исключение общего boilerplate вне starter, MinHash/LSH, AST/control-flow,
несколько component scores ниже — target pipeline, не текущий результат.
Текущий UI уже показывает immutable пару side-by-side, подсвечивает matched
line ranges и позволяет переходить по списку фрагментов; это средство ручного
разбора evidence, а не автоматический вывод о плагиате.

1. Сравниваются решения одной task version/family и релевантных прошлых потоков.
2. Boilerplate/starter fingerprints исключаются.
3. MinHash/LSH или embeddings выбирают кандидатов при больших архивах.
4. Кандидаты получают несколько scores:
   - normalized token winnowing/containment;
   - AST subtree similarity;
   - optional control-flow/semantic features;
   - raw token/display-fragment match.
5. UI показывает причины и фрагменты.

Старый winnowing с маскированием identifiers полезен как baseline, но не должен быть единственным алгоритмом: большое количество типового учебного boilerplate даёт false positives. Embedding используется для retrieval, не для окончательного вывода.

### Case workflow

`SUSPECTED -> REVIEWING -> CONFIRMED | DISMISSED | INCONCLUSIVE`. Эти состояния
и teacher comment поддерживаются; никакой threshold автоматически не меняет
grade. Требование двух reviewers — будущая policy/модель, baseline его не
применяет.

## 12. Очереди и деградация

- экзаменационные compile/run имеют отдельную квоту и приоритет;
- AI и plagiarism jobs не конкурируют за runner slots с интерактивной работой;
- circuit breaker отключает provider, но не IDE;
- перегруженный runner возвращает ETA/queue state, не бесконечный spinner;
- перед экзаменом выполняется prewarm и synthetic load check;
- результаты infrastructure error автоматически доступны для rerun без изменения submission.

## 13. Проверки executor и принятого риска

Текущий обязательный limit/container suite:

- readiness/result честно сообщает `UNRESTRICTED_CONTAINER`,
  `filesystem_isolated=false`, `network=host`;
- runner запускается отдельным непривилегированным read-only контейнером без
  application/DB/Moodle/AI mounts и Docker socket;
- cleanup удаляет workspace после завершения/аварии;
- fork/thread loop, infinite loop/sleep, memory/disk exhaustion и giant output останавливаются лимитами;
- compiler template bomb ограничивается compile profile.

Целевой suite после возврата `FILESYSTEM_ONLY`:

- student code читает свои declared input files и пишет только в разрешённый output/tmp;
- чтение/запись application source, DB paths, secrets, home, sibling job и arbitrary absolute path получает отказ;
- toolchain/headers/runtime читаются, но не изменяются;
- runtime job не видит source/object files, если task profile их не объявляет;
- environment не содержит Moodle, DB, object storage, AI и runner credentials;
- symlink/hardlink/path traversal не выходит из private tree;
- два параллельных jobs не видят друг друга;
- crash/core dump не оставляет host artifact вне private tree;
- network test подтверждает deny-all для целевого profile.

Kernel escape/ptrace/raw-syscall adversarial guarantees не являются критерием `FILESYSTEM_ONLY`; отсутствие такой защиты указывается в risk register и UI system settings. Если внедряется `HARDENED`, для него добавляется отдельный sandbox-escape/seccomp/network suite.
