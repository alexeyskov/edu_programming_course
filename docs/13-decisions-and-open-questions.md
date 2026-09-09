# Реестр решений и открытых вопросов

> Статус: решения сопоставлены с baseline v1. `ACCEPTED` означает принятое
> направление, а не завершённую production-приёмку. Внешние вопросы теперь
> блокируют pilot/production rollout, хотя разработка с mocks уже выполнена.

## 1. Статусы

- `ACCEPTED` — прямо следует из требований пользователя и меняется только новым решением владельца продукта.
- `RECOMMENDED` — архитектурная рекомендация, которую нужно оформить ADR до реализации.
- `NEEDS_EXTERNAL_CONFIRMATION` — зависит от администратора Moodle, инфраструктуры, права или эксплуатации.
- `DEFERRED` — сознательно после v1.

ADR должен содержать контекст, варианты, решение, последствия, migration/rollback и дату пересмотра. Этот документ — реестр, а не замена ADR.

## 2. Зафиксированные продуктовые решения

| ID | Статус | Решение | Последствие |
| --- | --- | --- | --- |
| D-001 | ACCEPTED | Продукт — специализированная оболочка над существующей LMS | Не строим локальный SIS/LMS и каталог accounts |
| D-002 | ACCEPTED | Первая LMS — Moodle `edu.mmcs.sfedu.ru`; integration является сменным adapter layer | Core не содержит Moodle HTML/IDs/roles |
| D-003 | ACCEPTED | Глобальных прикладных ролей ровно две: `STUDENT`, `TEACHER` | Reviewer/course/system admin roles отсутствуют; course access является отдельным object scope |
| D-004 | ACCEPTED | Identity, enrolments и groups приходят из внешней LMS; глобальная роль из LMS не берётся | Локальная identity — проекция external principal; Moodle role/control metadata не повышает до `TEACHER` |
| D-005 | ACCEPTED | Локальной регистрации, приглашения и локальных паролей нет | Отсутствуют соответствующие UI/API/schema transitions |
| D-006 | ACCEPTED | Поле «Токен администратора» открывает глобальные настройки | Реализуется как session capability, не третья роль |
| D-007 | ACCEPTED | Курс добавляется ссылкой внешнего курса только при `SYSTEM_SETTINGS` | URL — exact-origin locator; adapter проверяет course ID и доступность данных, но не назначает роль |
| D-008 | ACCEPTED | Доступность программных работ студентам/groups настраивается в оболочке | Targets ссылаются только на внешние memberships/groups |
| D-009 | ACCEPTED | Moodle остаётся официальным внешним контуром состава/курса/оценок | Используются mappings, field ownership, outbox и conflicts |
| D-010 | ACCEPTED | Финальное решение принимает преподаватель | СППР/AI/plagiarism/authorship не применяют оценку автоматически |
| D-011 | ACCEPTED | Внешняя вставка ограничена, внутренняя разрешается с provenance | Запрет — deterrence/evidence, не доказательство честности |
| D-012 | ACCEPTED | Хранится полная воспроизводимая история изменения | Semantic events + snapshots + server sequence/hash chain |
| D-013 | ACCEPTED | Внешний анализатор авторства подключается по API | Версионированный pseudonymous export, human interpretation |
| D-014 | ACCEPTED | Многофайловый режим настраивается для задания | Domain поддерживает multi-file с v1; build не flatten |
| D-015 | ACCEPTED | После срока student code не редактируется | Server-authoritative deadline и immutable final snapshot |
| D-016 | ACCEPTED | Local snapshots/checkpoints: D/10, в конце D/20, finish и <60 с | Они не зависят от Moodle; внешний checkpoint доступен только connector capability |
| D-017 | ACCEPTED | При проверке показывается текущий owner | DB-backed claim/lease не допускает одновременное изменение решения; viewers read-only |
| D-018 | ACCEPTED | Student AI помогает, но не пишет решение; teacher AI шире | Раздельные policies/tools/evals/retention |
| D-019 | ACCEPTED | Преподаватель может отключить СППР, global settings — AI целиком | Course switch ограничен global policy |
| D-020 | ACCEPTED | Старый prototype не является кодовой основой | Разрешено переносить требования/UX-идеи, не исходники/архитектуру |
| D-021 | SUPERSEDED BY D-034 | Первоначально требовалась файловая isolation | Временно заменено unrestricted container mode |
| D-022 | ACCEPTED | При проверке сохраняется private editable teacher sandbox и построчные compiler diagnostics | Оригинал неизменяем; experiment имеет отдельные revision/diff/reset/delete |
| D-023 | DEFERRED | Локальный task bank и генерация вариантов через ИИ исключены из текущего UI | Pluginless не умеет корректно создавать native Moodle question-bank items; импортированные activity не используют локальные task versions |
| D-024 | SUPERSEDED BY D-034 | Первоначально сеть runner отключалась | Временно заменено unrestricted container mode |
| D-025 | ACCEPTED | `retention_days` до отдельного проекта cleanup является только policy value | UI/API не обещают автоматическое удаление |
| D-026 | ACCEPTED | Grade mapping не выполняет неявный scaling | Playwright записывает фактический локальный балл только в однозначно адресованную штатную форму Assignment/Quiz Essay; mismatch/unsupported scale блокируется, staging read-back обязателен |
| D-027 | ACCEPTED | Canonical local checkpoint/task JSON сериализует Python backend | Optional PHP bridge валидирует JSON и SHA-256 exact bytes без re-encoding |
| D-028 | ACCEPTED | Deterministic hidden-test evidence запускает только преподаватель по immutable submission | Active claim/policy + real runner; actual isolation flags сохраняются; report не создаёт grade |
| D-034 | ACCEPTED TEMPORARY | Runner выполняет ordinary subprocess внутри отдельного контейнера без per-job filesystem/network sandbox | Совместимость с NUC и старой версией; resource/output/wall limits и truthful metadata сохраняются |
| D-029 | ACCEPTED | Moodle default — pluginless через отдельный bounded Playwright browser-worker | Установка plugin не нужна для login/course discovery/Quiz Essay/Assignment answer sync; один Chromium и exact-origin typed operations |
| D-030 | ACCEPTED | Moodle password используется transient и не сохраняется; sanitized `storage_state` AES-GCM encrypted at rest | Credential kind `BROWSER_STATE_V1` имеет revision/lease; потеря encryption key или expiry требует re-login |
| D-031 | ACCEPTED | `TEACHER` выводится только из индивидуального отзываемого teacher token из системного пула; без grant действует `STUDENT` | Первый успешный LMS-вход создаёт one-to-one token↔principal grant; замена secret сохраняет grant, delete токена отзывает grant и teacher memberships |
| D-032 | ACCEPTED | Глобальный каталог курсов ограничивает login, sync и authorization scope | Даже `TEACHER` видит/изменяет только курсы из catalog∩LMS-enrolment/group scope; LMS dashboard не автоимпортируется |
| D-033 | ACCEPTED | Исторические сдачи связанных Moodle Quiz Essay/Assignment импортируются read-only после LMS sync | Bounded постраничный Playwright crawl и stable external revision обеспечивают идемпотентность; Assignment online text и attachments сохраняются вместе, safe ZIP распаковывается с relative paths; история набора не переносится, MMCS markup требует live-проверки |
| D-035 | ACCEPTED, LIVE GATE OPEN | Pluginless Assignment write выбирает transport по фактической student form | `ASSIGN_FILE` предпочтителен при file+online text; selector drift/unsupported team policy fail closed; до пилота нужен staging save/finalize/read-back |

## 3. Рекомендуемые архитектурные решения

| ID | Статус | Рекомендация | Причина/компромисс |
| --- | --- | --- | --- |
| A-001 | ACCEPTED | Modular monolith FastAPI + Pydantic v2 + async SQLAlchemy 2 + Alembic и отдельный runner service | Явные HTTP/domain/data boundaries; web process не запускает binaries |
| A-002 | RECOMMENDED | React/TypeScript/Vite + Monaco | Зрелая browser-editor model, diff/diagnostic/completion APIs |
| A-003 | ACCEPTED | PostgreSQL как source of truth и durable job/outbox; S3/MinIO и Redis вводятся по мере реализации, внешний broker — только по нагрузочным метрикам | Начальный scheduler/sync используют row claim, lease и retry без отдельного broker |
| A-004 | RECOMMENDED | Generic versioned LMS connector port + conformance kit | Позволяет добавить другую LMS без переписывания core |
| A-005 | RECOMMENDED | После pluginless baseline добавить LTI 1.3; narrow bridge остаётся optional enhanced mode | LTI требует администратора, но даёт стандартные launch/context/grade flows |
| A-006 | ACCEPTED | Standalone login через transient Playwright HTML-form operation | Password не сохраняется; worker возвращает validated identity и sanitized encrypted browser state |
| A-007 | ACCEPTED | Один browser-worker/Chromium и bounded queue; новый context на операцию | Persistent context на пользователя отсутствует, state хранится encrypted с DB lease/revision |
| A-008 | RECOMMENDED | Admin token проверяется в login request, session elevation выдаётся только после успешной Moodle identity verification | Токен не создаёт анонимную session и не проходит в Moodle |
| A-009 | RECOMMENDED | `FILESYSTEM_ONLY`: rootless/mount namespace, per-job UID, allow-listed read-only toolchain и private writable workspace | Выполняет требование файлов; не выдаёт ложную гарантию kernel safety |
| A-009a | ACCEPTED TEMPORARY | `UNRESTRICTED_CONTAINER`: обычный subprocess внутри отдельного runner-контейнера, без Bubblewrap; фактические false/host сохраняются | Совместимость с NUC/старой реализацией ценой явно принятого риска; resource/output/wall limits остаются |
| A-010 | ACCEPTED | Transactional outbox и idempotency; inbox/explicit external conflicts — target | Pluginless доставляет Quiz Essay/Assignment answer и grade/comment; task mirror, checkpoint/grade read-back и conflict inbox отсутствуют |
| A-011 | RECOMMENDED | Internal save ≈1 с и local checkpoints authoritative; внешний checkpoint асинхронный и optional | Moodle outage не должен терять работу |
| A-012 | ACCEPTED | Deterministic evidence до LLM | Bounded hidden-test report реализован; sanitizer/static/rubric остаются расширением |
| A-013 | RECOMMENDED | Winnowing + AST/multi-signal plagiarism, без auto-sanction | Сохраняет полезный baseline старой версии и уменьшает ложные срабатывания |
| A-014 | RECOMMENDED | Syntax/word completion в MVP; clangd staged pool позже | 100 clangd processes требуют подтверждённого memory budget |
| A-015 | DEFERRED | Отдельная canonical programming task version возможна только в будущем контуре банка | В текущем релизе Moodle activity целиком authoritative, а локальная публикация означает только включение оболочки для групп |
| A-016 | ACCEPTED | Один SQLAlchemy `AsyncSession` на request/worker iteration; state transitions под revision/`FOR UPDATE`, outbox claim через `SKIP LOCKED` | AsyncSession не разделяется между tasks; внешняя сеть никогда не ожидается под row lock |

## 4. Решения, требующие подтверждения до pilot/production

### Q-001. Кто владелец продукта и данных?

- **Нужно:** named product owner, data controller/owner, технический owner и принимающий security risk.
- **Default:** кафедра/институт определяет product owner; университетская ИТ-служба — infrastructure/security partner.
- **Почему блокирует:** retention, доступ к коду, AI transfer и incident decisions нельзя делегировать коду.

### Q-002. Какова фактическая версия Moodle и будет ли она обновлена?

- **Нужно:** точная `version.php`, target version/date, staging clone и rollback;
  исторический признак 4.2.2 и guest crawler cache не подтверждают current build.
- **Default:** 4.5 LTS или текущая поддерживаемая ветка, compatibility CI минимум для target и следующей ветки.
- **Почему блокирует production:** 4.2 security support завершена; bridge на устаревшей ветке увеличивает общий риск.

### Q-003. Можно ли установить `local_programming_bridge` и позже LTI tool?

- **Нужно:** site-level registration, plugin installation, web services, cron, service account и firewall.
- **Baseline:** plugin не обязателен. `PLUGINLESS` использует Playwright для
  login, course contents, доступного roster/groups, mapped Quiz Essay
  attachment/online text, Assignment file/online text, исторического импорта и
  grade/comment. Bridge 0.3 нужен только
  для enhanced signed launch, external recovery checkpoint и task mirror.
- **Риск:** разметка/theme Moodle может измениться; browser operation поэтому
  узко типизирована, bounded и fail-closed, но требует staging regression.

### Q-004. Как именно работает standalone «Войти через Moodle»?

- **Baseline:** browser отправляет credentials в HTTPS body core; backend
  передаёт их HMAC-защищённому `moodle-browser`, который входит через штатную
  HTML-form exact configured HTTPS origin и сразу забывает password. Server-side
  остаётся только sanitized AES-GCM encrypted `storage_state` под lease/revision.
- **Optional:** bridge endpoint или будущий LTI resource link не требуют
  credential-form login.
- **Запрещено:** password persistence/logging, cookies/state во frontend,
  foreign origin и произвольные URL/selectors/JavaScript browser operation.

### Q-005. Как маппить Moodle roles? — решено

- **Принято:** не маппить Moodle roles в глобальную роль приложения. LMS даёт identity, enrolment и groups; role/capability/control markup может быть connector diagnostic, но не privilege evidence.
- **Глобальная роль:** active one-to-one teacher-token grant → `TEACHER`; иначе → `STUDENT`.
- **Course scope:** независимо от роли нужны и active global-catalog entry, и актуальный LMS-enrolment/group scope.

### Q-006. Кто вправе добавлять курс по URL? — решено

- **Baseline:** preview и confirm требуют активной `SYSTEM_SETTINGS`; duplicate course ID использует существующий mapping. External `TEACHER` role не является bootstrap-gate.
- **Инвариант:** connection provisioned deployment CLI; adapter проверяет exact origin/course ID и доступность данных, но не выводит из них глобальную роль. Elevation позволяет bootstrap import, но после confirm не даёт читать course data без teacher grant и LMS-enrolment.

### Q-007. Как трактовать группы преподавателей и студентов?

- **Default:** Moodle enrolments/groups authoritative внутри глобального course catalog; principal с teacher-token grant видит students только в catalog∩enrolment scope, дополнительно суженном separate-groups policy и mappings.
- **Нужно:** подтвердить пересечения 13 group records и комиссионный доступ.
- **Запрет:** matching по названию группы/ФИО.

### Q-008. Кто владеет параметрами и видимостью? — решено

- **Принято:** Moodle владеет названием, условием, open/close/timelimit,
  максимальным баллом и числом попыток. Оболочка хранит только локальное
  enablement для выбранных Moodle-групп; оно не меняет activity в Moodle.
- **Следствие:** отсутствуют локальные fallback-сроки, ручная шкала и
  last-write-wins для этих полей.

### Q-009. Где canonical task bank? — отложено

- **Принято для текущего релиза:** банк скрыт. Canonical задания — Moodle
  activities/questions. Native two-way bank или генерация вариантов через ИИ
  возвращаются в scope только после отдельного Moodle write contract.
- **Baseline:** local immutable version реализована; pluginless не публикует
  mirror. Connector-owned mirror доступен только optional bridge 0.3;
  import/native question bank и conflict workflow отсутствуют.
- **Альтернатива:** полноценное двухстороннее редактирование требует custom question type и существенно увеличивает v1.

### Q-010. Какой migration scope курса 549?

- **Baseline принят:** structure + поддерживаемые activities/mappings и
  read-only исторический импорт финальных submissions/grades/comments/files для
  связанных Quiz Essay и стандартных Assignment. Импорт запускается после LMS
  sync, выполняется постранично и идемпотентно.
- **Не входит:** история набора, clipboard provenance, промежуточные snapshots,
  полный native question bank, versions/random pools и неподдерживаемые
  activities. Реальная MMCS Assignment разметка остаётся эксплуатационным gate.

### Q-011. Как код хранится в Moodle checkpoint?

- **Варианты:** archive attachment; text response; signed link + hash/metadata; custom plugin record.
- **Pluginless baseline:** explicit mapped Quiz с ровно одним Essay использует
  `ESSAY_ONLINE_TEXT`/`ESSAY_ATTACHMENT`; Assignment определяет по фактической
  форме `ASSIGN_ONLINE_TEXT`/`ASSIGN_FILE`, причём file предпочтителен,
  если доступны оба. Online text принимает ровно один source. File
  transport отправляет workspace из ровно одного source как
  `main.c`/`main.cpp`, а любой набор из двух и более файлов — как
  deterministic `submission.zip` с `.c/.cc/.cpp/.cxx`,
  `.h/.hh/.hpp/.hxx`, `.inc` и `.txt` и safe relative paths. Decoded artifact
  ≤4 MiB, signed base64/JSON request ≤6 MiB;
  `PERIODIC`/`FINAL_MINUTE` → `DRAFT_SAVED`, `SUBMISSION`/`DEADLINE` →
  `FINALIZED`. Local canonical snapshot/history остаётся authoritative и
  повторяется outbox при outage.
- **Optional bridge:** plugin record хранит тот же canonical envelope и
  поддерживает recovery; нужны staging privacy/backup tests.

### Q-012. Как синхронизировать grade/comment?

- **Baseline:** Playwright grade/comment route записывает решение через
  canonical grader однозначно связанного Assignment либо manual-grading form
  отдельного Quiz Essay. Backend использует durable outbox и точные внешние
  identifiers; подпись проверяющего добавляется один раз.
- **Ограничение:** независимый read-back/conflict engine отсутствует, поэтому
  изменившаяся или неоднозначная форма блокируется fail-closed. Нужны staging
  write/read-back и подтверждение поведения нестандартной шкалы/ручной правки.

### Q-013. Как создаётся и передаётся первый admin token?

- **Default:** offline generation deployment operator, secret manager injection, одноразовая защищённая передача, немедленная rotation после handover.
- **Bootstrap:** canonical pluginless Moodle origin provisioned deployment CLI;
  bridge/LTI metadata нужны только выбранному optional mode. Admin token без
  успешной Moodle identity verification не создаёт session.
- **Запрещено:** commit/env sample, чат, e-mail в открытом виде, UI «показать текущий токен».
- **Нужно:** named custodian и аварийный revoke process.

### Q-014. Каков TTL административного повышения?

- **Default:** 30 минут inactivity, 2 часа absolute, re-entry для rotation/export/danger operations.
- **Нужно:** согласовать допустимое удобство. Постоянный `is_admin` в profile противоречит требованию token-at-login и увеличивает риск.

### Q-015. Может ли student с правильным token открыть settings?

- **Буквальное требование:** да, token добавляет capability после любой внешней аутентификации.
- **Рекомендация:** сохранить эту семантику, но network/rate/audit controls; admin elevation всё равно не даёт teacher/course-data access.
- **Граница:** требовать teacher-token grant для elevation не нужно: это смешало бы независимые оси. `SYSTEM_SETTINGS` не меняет `STUDENT|TEACHER` и не расширяет catalog/enrolment scope.

### Q-016. Нужен ли один общий token или несколько versioned tokens?

- **Baseline:** один `ADMIN_TOKEN_HASH` из deployment config; current+previous,
  rotation API и global revoke выданных elevations не реализованы. Rotation —
  контролируемая смена config/restart.
- **Цель после v1:** индивидуальные administrators + MFA/WebAuthn, общий recovery secret offline.

## 5. Решения до pilot

### Q-017. C/C++ стандарты и библиотеки

- **Baseline:** фиксированные C17 и C++20 profiles для GCC/Clang; C++17/23 ещё
  не зарегистрированы.
- **Нужно:** точные compiler flags, Windows-specific compatibility, encoding и allowed libraries текущего курса.

### Q-018. Multi-file scope MVP

- **Baseline:** `.c/.cc/.cpp/.cxx/.h/.hh/.hpp/.hxx/.inc` для реального
  runner, ограничение count/size/path и direct generated compile/link; без
  arbitrary CMake. Workspace и runner также принимают `.txt`: runner
  копирует его в writable cwd executable как runtime data.
- **Нужно:** разрешено ли создавать свои headers и сколько translation units.

### Q-019. Interactive programs

- **Default:** bounded stdin stream/turns/time; no terminal emulation; runtime видит executable
  и workspace `.txt` в одном writable cwd. Программа может перезаписать
  `.txt` или создать новые text/binary files, но все эти outputs transient:
  job cwd удаляется, а изменения не возвращаются в workspace.
- **Network baseline:** всегда off, result обязан сообщать `denied`, backend
  отклоняет другое значение. Любой будущий network-enabled профиль потребует
  отдельного решения о scan/HTTP/DNS/local-network risk.
- **Нужно:** есть ли задачи, требующие файлового ввода/вывода, бинарных данных или сети.

### Q-020. Grace period и network loss

- **Default:** server deadline жёсткий; только received-before-deadline events входят в submission; instructor override создаёт новую audited epoch.
- **Нужно:** университетское правило на краткий технический grace и массовый incident extension.

### Q-021. LMS membership freshness при outage

- **Default:** уже начатая attempt продолжается; новый login/import зависит от LMS; teacher critical write требует reasonably fresh snapshot.
- **Нужно:** TTL и действие при полученном suspend во время active exam.

### Q-022. Retention и privacy notice

- **Baseline:** поле policy сохраняется, но автоматического cleanup/auto-delete
  нет. До решения применяется только утверждённая операторская процедура.

- **Нужно:** сроки history/code/chats/AI/audit/backups, legal basis, appeals и external provider geography.
- **Default технический:** указан в документе безопасности, но не включается production без утверждения.

### Q-023. Accessibility exceptions

- **Default:** audited per-principal integrity policy/accommodation; authorship model получает flag и не сравнивает с обычной calibration автоматически.
- **Нужно:** процесс назначения и допустимые assistive inputs.

### Q-024. Нагрузочный профиль

- **Default:** 150 sessions, 100 exam attempts, 500 events/s + 2× burst, 50 run queue.
- **Нужно:** реальные максимумы всех параллельных курсов и hardware budget.

### Q-025. SLO/RPO/RTO

- **Default:** 99,9% exam window; acknowledged-edit loss 0; disaster RPO ≤5 минут/RTO ≤60 минут.
- **Нужно:** формальное принятие владельцем и бюджет для достижения.
- **Runner risk:** до pilot named owner подписывает `RISK-RUNNER-001` для
  текущего `UNRESTRICTED_CONTAINER` либо выбирает отдельный host и возвращает
  `FILESYSTEM_ONLY`/`HARDENED` profile.

## 6. Решения до включения AI/СППР

### Q-026. Provider и передача данных

- **Нужно:** cloud/local provider, договор, region, retention, `store=false`, allowed source scope и budget.
- **Default:** adapter abstraction; pseudonymized minimum context; AI feature flag off до approval.

### Q-027. Student AI policy

- **Default:** lab conceptual on; independent strict; control/exam off.
- **Нужно:** допустимы ли mini syntax examples и compiler error excerpts по каждому preset.

### Q-028. SPPR scoring

- **Default:** отдельные evidence cards + recommended range; teacher manually applies.
- **Нужно:** rubric weights, diagnostics visibility студенту и причины rejection/override.

### Q-029. Plagiarism cohort

- **Default:** same task family/current assessment + historical opt-in; exclude starter/common fingerprints; human case.
- **Нужно:** можно ли сравнивать между годами/курсами и сроки хранения.

### Q-030. Authorship analyzer contract

- **Нужно:** provider, schema, calibration population, SLA, probability/CI/explanations, appeal and deletion.
- **Default:** asynchronous versioned NDJSON manifest; no threshold creates sanction.

## 7. Отложенные решения

| ID | Статус | Решение |
| --- | --- | --- |
| F-001 | DEFERRED | Второй LMS adapter и cross-provider identity linking |
| F-002 | DEFERRED | Individual administrator identities/MFA replacing shared token |
| F-003 | DEFERRED | Полный clangd per workspace/pool |
| F-004 | DEFERRED | Custom Moodle question type с полноценным bidirectional editor |
| F-005 | DEFERRED | Другие programming languages/toolchains |
| F-006 | DEFERRED | Collaborative pair programming |
| F-007 | DEFERRED | Video/biometric proctoring |
| F-008 | DEFERRED | `HARDENED` runner с gVisor/microVM, syscall policy и сильной network isolation |

`DEFERRED` не означает «невозможно». Эти пункты не должны скрыто попадать в MVP и ломать сроки.

## 8. Decision gates

### До первого commit реализации

- Q-001–Q-016 решены либо имеют formal risk acceptance;
- Moodle staging и auth feasibility доказаны минимальным spike;
- connector boundary и two-role model отражены в ADR/API/schema;
- admin-token custodian/rotation/revoke определены.

### До pilot с реальными студентами

- Q-017–Q-025 утверждены;
- privacy notice и informed processing доступны;
- restore, Moodle outage и authorization tests пройдены;
- pilot rollback process согласован.

### До включения AI/authorship

- Q-026–Q-030 утверждены;
- eval thresholds и human-review policy подписаны;
- external transfer/retention разрешены.

### До экзамена

- все G0–G9 и go/no-go criteria из roadmap выполнены;
- открытые high risks приняты named owner;
- on-call и fallback отрепетированы.
