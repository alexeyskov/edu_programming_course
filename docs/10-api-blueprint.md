# API и интеграционные контракты

> Статус: это объединённый baseline + target blueprint. Фактический контракт
> всегда берётся из FastAPI OpenAPI (`/api/v1/openapi.json` в debug) и Pydantic
> schemas. WebSocket, LTI, webhooks и несколько advanced endpoints, перечисленных
> ниже, ещё не реализованы.

## 1. Общие правила

- Public prefix `/api/v1`; breaking change создаёт новую major версию.
- OpenAPI — source of truth текущих REST-контрактов; AsyncAPI/WebSocket schema —
  future.
- UUID в URL, external IDs только в integration namespace.
- Даты RFC 3339 UTC.
- Денежных операций нет; grade хранится decimal, не float.
- Идемпотентные state transitions принимают `Idempotency-Key`; workspace
  mutation дополнительно получает/выводит стабильный client request key.
- Optimistic concurrency использует `If-Match`/entity revision.
- Baseline коллекции чаще возвращают bounded plain arrays; cursor pagination —
  future для больших архивов.
- Domain error содержит stable code/message/details; request ID доступен в
  response header/log context, но не каждое FastAPI validation error имеет тот
  же body. Stack trace клиенту не возвращается вне debug.
- Permissions проверяются на сервере для каждого объекта.
- Каждый FastAPI route объявляет отдельные Pydantic v2 request/response models; mutation models по умолчанию запрещают неизвестные поля.
- SQLAlchemy entities и lazy relationships не возвращаются из route напрямую: application service формирует явный response DTO после object-level authorization.
- `AsyncSession` передаётся request-scoped dependency, но domain policy не зависит от FastAPI `Request`, `Depends` или HTTPException.

### 1.1. Граница текущего API

Baseline routers: `foundation`, `auth`, `courses`, `authoring`, `attempts`,
`evidence`, `reviews`, `integrity`, `ai`, `system`. Реализованы CRUD/переходы, которые
использует `frontend/README.md`, в том числе course imports, task versions,
assessments, workspace create/patch/delete, history, run/submit, review
claim/draft/decision, teacher experiments, AI threads, similarity/authorship,
system settings, пул teacher tokens и LMS outbox retry.

Не реализованы как REST/WS: LTI endpoints/JWKS, WebSocket attempt/run streams,
attempt reopen/overrides, run cancel, общий public/visible/sanitizer/static test
pipeline, review claim
override/decision revise, AI analyses/feedback, signed authorship download,
connector CRUD/webhooks/conflict UI, admin-token rotation API, audit export и
destructive-action plan. Нельзя разрабатывать frontend против этих target
маршрутов без добавления backend schema/tests.

## 2. Authentication

- `GET /auth/connections` — доступные заранее настроенные LMS providers; не раскрывает secrets.
- `POST /auth/lms/{connection_id}/start` — создаёт short-lived bridge login transaction; body может содержать необязательные `admin_token`, `teacher_token` и `course_id`. Secret values проверяются до redirect и не хранятся в transaction; для teacher token сохраняется только internal token ID до callback.
- `POST /auth/lms/{connection_id}/credentials` — pluginless Playwright login; body содержит Moodle `username`/`password` и может содержать `admin_token`/`teacher_token`. Пароль и plaintext tokens не сохраняются в login transaction или логах; для новых teacher tokens отдельный системный пул хранит Argon2id verifier и AES-GCM ciphertext. После успешной проверки frontend может сохранить только прикладные токены origin-scoped в `localStorage` согласно UX-политике.
- `POST /auth/moodle/callback` — текущий скрытый от OpenAPI bridge callback;
  stateful path берёт connection из login transaction, direct path однозначно
  сопоставляет enabled connection по signed issuer; URL parameter не выбирает
  connection;
- `GET /auth/session` — external principal, глобальная роль, course memberships в catalog∩LMS-enrolment scope, effective capabilities и `admin_elevation_expires_at`.
- `POST /auth/admin-elevation` — повторный step-up в уже аутентифицированной сессии.
- `DELETE /auth/admin-elevation` — снять повышение без logout.
- `POST /auth/logout` — закрытие server session.
- `/integrations/moodle/lti/login|launch|jwks` — target LTI 1.3, отсутствует в
  baseline.

Frontend по явной UX-политике может запомнить admin/teacher token origin-scoped
в `localStorage` до действия «Забыть сохранённые токены», но не хранит там
bearer/access token, пароль или Moodle cookie. Основная браузерная авторизация —
secure server session cookie + CSRF protection. Admin token не является API
bearer credential и без успешной внешней аутентификации не создаёт session.
Moodle baseline использует restricted
web-service token и отдельный HMAC launch secret из server config; mTLS/OAuth
являются будущими вариантами connector credentials.

`SYSTEM_SETTINGS` elevation привязан к server session и client network prefix
(`/24` IPv4, `/64` IPv6). Смена префикса автоматически отзывает elevation, но
не завершает обычную LMS-сессию.

Stateless direct Moodle callback (без platform state/verifier transaction)
после HMAC также требует browser `Origin` либо `Referer`, чей exact origin
совпадает с configured connection; failure — `403 INVALID_LAUNCH_ORIGIN`.
Stateful callback полагается на state + verifier cookie и не требует этого
header-check.

Endpoints регистрации, приглашения, локального пароля и произвольного назначения роли отсутствуют. Глобальная роль выводится только из one-to-one teacher-token grant (`STUDENT` по умолчанию). Объектная course authorization дополнительно проверяет catalog-enabled course и connector-managed enrolment/group snapshot с freshness policy. LMS role metadata не повышает principal.

## 3. Courses и состав

Реализованы:

- `GET /courses`, `GET /courses/{id}`;
- `GET /system/course-catalog`, `PUT|DELETE /system/course-catalog/{id}`;
- `POST /course-imports`, `GET /course-imports/{id}`,
  `POST /course-imports/{id}/confirm|cancel`;
- `GET /courses/{id}/sections|memberships|groups|lms-activities`;
- `GET|PATCH /courses/{id}/policies`;
- `POST /courses/{id}/sync`, `GET /courses/{id}/sync-status`.

Course-level availability CRUD и отдельный effective-access preview endpoint не
реализованы. Baseline создаёт assessment-scoped availability rules через
`POST /assessments/{id}/availability-rules`, удаляет через `DELETE
/assessments/{id}/availability-rules/{rule_id}`.

Прямого `POST /courses` нет. URL принимается только для origin уже настроенного connector и не загружается generic HTTP client. Состав, identity, enrolment и groups из LMS не изменяются local PATCH; API возвращает field ownership. Глобальная роль не берётся из LMS. Availability rules лишь сужают функции оболочки внутри существующего enrolment и не расширяют catalog scope.

Создать и подтвердить import preview может только principal с временной
`SYSTEM_SETTINGS`; это отдельный bootstrap-gate до local course projection.
Moodle discovery дополнительно проверяет exact course page и доступность нужных данных, но не повышает actor по LMS role/control hints. Confirm включает курс в глобальный allow-list, а DELETE выключает его и
активные memberships без физического удаления истории. Elevation не заменяет
membership для обычных course read/write endpoints.

## 4. Банк заданий — внутренний/deferred API

Реализованы `GET|POST /task-bank/items`, `GET|PATCH
/task-bank/items/{id}`, `POST /task-bank/items/{id}/versions`, `GET
/task-versions`, `GET /task-versions/{id}` и переходы
`POST /task-versions/{id}/validate|publish|archive`.

Эти маршруты сохранены как технический задел и compatibility surface, но UI
текущего pluginless-релиза их не показывает. Они не создают Moodle activity или
native question-bank item и не участвуют в включении импортированной работы для
студентов. Клиент не должен считать успешный локальный `publish` публикацией в
Moodle.

Clone, отдельный test-suite/rubric CRUD, usage и Moodle-mapping endpoints пока
отсутствуют; в Playwright mode task mirror при publish/archive не enqueue-ится.
Mirror доступен только optional plugin и не является native Moodle bank upsert.

Student representation не содержит hidden tests/reference solution.
Teacher-authoring request/response `TaskVersion` уже принимает bounded
`hidden_test_manifest` v1: `{}` либо 1–20 stdin/stdout cases с comparison
`EXACT|TRIM_TRAILING_WHITESPACE`, до 1 MiB aggregate. Это встроенное поле версии,
не отдельный test-suite CRUD. Rubric editor/API, teacher harness, public-suite
orchestration и reference-solution model пока не реализованы.

## 5. Работы

Реализованы `GET|POST /courses/{id}/assessments`, `GET|PATCH|DELETE
/assessments/{id}`, `POST /assessments/{id}/items`, `DELETE
/assessments/{id}/items/{item_id}`, `POST
/assessments/{id}/availability-rules` и переходы
`validate|publish|close`. Отдельные override/readiness endpoints — target.

Student representation скрывает assignment pools, hidden checks, grader notes и future items.

Для Moodle-managed assessment название, условие, open/close/timelimit,
максимальный балл и число попыток являются read-only projection и обновляются
следующим LMS sync. Текущий UI не вызывает local create/edit для этих полей.
Публикация импортированной работы создаёт только availability rules выбранных
Moodle-групп и не требует заранее доказанного transport, ручной настройки
стартового файла или изменения Moodle-owned параметров. При старте попытки
provider adapter проецирует известный transport в generic workspace profile.
Если external mapping существует, но profile не доказан, start attempt отвечает
`LMS_DELIVERY_PROFILE_UNRESOLVED` и не создаёт workspace с угаданным режимом.
Перед checkpoint/final delivery browser-worker повторно доказывает transport по
фактической student form и предпочитает file, если доказаны оба;
selector/control drift блокирует внешнюю mutation fail-closed. Локальная
публикация, workspace и immutable submission при этом не откатываются.

## 6. Попытки и workspace

Baseline предоставляет `POST /assessments/{id}/attempts`, `GET
/attempts/{id}`, `GET /attempts/{id}/workspace`, `GET
/attempts/{id}/history`, `POST /attempts/{id}/clipboard-receipts`, `POST
/attempts/{id}/submit`, file replacement/delete по
`/attempts/{id}/workspace/files/{file_id}` и create по
`/attempts/{id}/workspace/files` либо compatibility path `.../files/new`.

Отдельные file-list/snapshot-list endpoints, rename и attempt reopen/override
пока отсутствуют. Workspace GET уже возвращает bounded files и revision.
Разрешённый `.txt` имеет `TEXT`/`plaintext` семантику и передаётся runner
как writable runtime data в cwd executable. Изменённые исходные `.txt` и
созданные программой text/binary outputs существуют только внутри job и
не материализуются обратно в workspace API.

Runner profile выбирает `single|multi` по зафиксированному при start attempt
`Workspace.multi_file`, а не по предварительному Moodle-import placeholder;
то же effective значение используется для student run, interactive run,
teacher sandbox и deterministic evidence по этой сдаче.

### Target WebSocket `/ws/v1/attempts/{id}` (не реализован)

Client messages:

- hello/resume with last ack;
- edit batch;
- heartbeat;
- focus state;
- internal clipboard operation;
- request snapshot;
- presence.

Server messages:

- hello ack with authoritative revision/time/deadline;
- edit ack/rejection;
- resync required/authoritative patch;
- timer/deadline/lock;
- snapshot/checkpoint state;
- policy changed where safely applicable;
- service degradation.

Все message types имеют schema version и client request ID. Backpressure state запрещает клиенту бесконечно накапливать память.

## 7. Compile/run

В baseline реализованы synchronous `POST /attempts/{id}/runs`, authorized `GET
/runs/{id}` и `POST /teacher-experiments/{id}/runs`. Diagnostics входят в run
result. Отдельный diagnostics endpoint, cancel и WebSocket streaming пока
отсутствуют; teacher-only deterministic evidence endpoints описаны ниже.

Client не передаёт image name, shell command, mount path или arbitrary flags. Только approved build/execution profile ID. `FILESYSTEM_ONLY` profile возвращает фактическую policy version и признак network mode; API не утверждает наличие усиленной kernel isolation.

### 7.1. Deterministic hidden-test evidence

Реализованы:

- `POST /submissions/{submission_id}/evidence-runs`, body `{}` — синхронно
  создаёт и исполняет report; optional `Idempotency-Key` длиной 1–200 UTF-8
  bytes обеспечивает replay;
- `GET /submissions/{submission_id}/evidence-runs?offset=0&limit=50` — список,
  `limit` 1–100;
- `GET /evidence-runs/{report_id}` — один report.

Все три маршрута требуют активного one-to-one teacher-token grant и актуального catalog∩LMS-enrolment/group scope целевого курса.
Создание дополнительно требует `review_required=true`,
`decision_support_enabled=true`, active owned claim, опубликованную task version,
валидный hidden-test manifest, включённый real runner и exact immutable snapshot
hashes. `RUNNER_MOCK_ENABLED` не выдаёт official evidence. Чтение сохранённого
report не требует claim и остаётся доступным teacher при последующем отключении
СППР.

Response связывает report с submission/snapshot/task/requester и тремя SHA-256,
имеет status `RUNNING|COMPLETED|FAILED`, counts, partial case outcomes
`PASSED|FAILED|INFRASTRUCTURE_ERROR`, bounded previews/findings и timestamps.
Defaults: 8 с wall/2 с CPU на case, 50 с total, один concurrent report на
teacher, 5 reports за 300 с, stale recovery 120 с. Для submission/snapshot
разрешён один `RUNNING` report. Endpoint не возвращает grade/recommendation и не
создаёт decision.

## 8. Сдачи и review

Реализованы:

- `GET /assessments/{assessment_ref}/submissions` и `GET /submissions/{id}`;
- claim create/heartbeat/release через `POST /submissions/{id}/claims`, `POST
  /review-claims/{id}/heartbeat`, `DELETE /review-claims/{id}`;
- `GET|PUT /submissions/{id}/review-draft` и `POST
  /submissions/{id}/review-decisions`;
- experiment create, existing-file `PATCH`, reset, run и soft delete через
  `/submissions/{id}/teacher-experiments` и `/teacher-experiments/{id}/...`.

Обычный преподаватель видит только submission студентов из общего активного
`CourseGroup` (плюс строгий compatibility fallback для исторических MMCS-меток
`подгруппа Фамилия И.О.`). Scope вычисляется сервером до pagination/serialization.
Сессия с `SYSTEM_SETTINGS` может читать все локальные submissions независимо от
группы, но list/detail возвращают `can_review=false`, если actor не проходит
обычный teacher scope. Все claim/draft/experiment/evidence-run/decision
мутации требуют именно обычного teacher scope; административное повышение его
не заменяет.

Frontend представляет тот же list тремя состояниями `view=all|pending|reviewed`
и сохраняет `view`/поиск при переходе в workspace. Это client-side параметры,
не дополнительные REST query текущего baseline. `all` содержит также сдачи
работ с `review_required=false`; они имеют `can_review=false` и не входят в
`pending`. Detail DTO содержит
`latest_decision` и `decision_history`; list DTO — status последнего решения и
`can_review`. Проверенная работа остаётся доступной для чтения, а перепроверка
использует тот же claim endpoint и затем создаёт новую immutable decision
revision с `supersedes_id`.

Experiment GET/diff endpoint и file create/delete отсутствуют; UI строит diff
первого файла из submission/experiment DTO. Claim override, отдельный review
history/revise route и ручной decision-export route также отсутствуют. Новое
решение создаётся тем же decision endpoint после нового claim; Moodle outbox
создаётся автоматически, а retry выполняется generic outbox endpoint.

Decision request содержит grade, comment, opaque `criterion_scores` и
`evidence_ids`; active owned claim проверяется сервером. Отдельных rubric-version
и expected review-revision полей в текущей request schema нет. Повтор создаёт
новую immutable decision revision/supersedes link.

Для deterministic report в `evidence_ids` передаются ID отдельных official case
run, а не `EvidenceReport.id`. Backend проверяет completed report, exact
submission/snapshot/task hashes, origin/revision и подтверждённые
filesystem-isolated/no-network flags; private teacher experiment не принимается.

Teacher experiment endpoints проверяют внешний `TEACHER` course scope. Ни один endpoint не принимает действие «заменить submission experiment-версией»; перенос фрагмента в final comment выполняется отдельно и аудируется.

## 9. ИИ

Baseline реализует `POST /ai/student-threads`, `POST /ai/teacher-threads`, `GET
/ai/threads`, `GET /ai/threads/{id}`, `GET|POST
/ai/threads/{id}/messages` и `POST /ai/threads/{id}/close`. Отдельные AI
analysis/feedback endpoints и streaming отсутствуют.

Baseline не stream-ит provider output: он получает bounded полный ответ,
применяет student output/citation gate и только затем сохраняет/возвращает его.
Будущий streaming допустим лишь при эквивалентной безопасной фильтрации до
показа фрагментов.

## 10. Плагиат и авторство

Фактические baseline routes:

- `GET|POST /assessments/{id}/similarity-analyses`, `GET
  /similarity-analyses`, `GET /similarity-analyses/{id}`;
- `GET /plagiarism-cases`, `GET|PATCH /plagiarism-cases/{id}`;
- `GET /similarity-matches/{match_id}/comparison`;
- `GET|POST /submissions/{id}/authorship-analyses`, `GET
  /authorship-analyses`, `GET /authorship-analyses/{id}`.

Внешний authorship вызов выполняется server-to-server в request flow. Signed
download/export и analyzer webhook сейчас не реализованы.

`GET /submissions/{id}` включает teacher-only поле `origin_verification` со
state `VERIFIED|EXTERNAL_ORIGIN|MISMATCH|PENDING|UNAVAILABLE`, способом ответа,
временем проверки и безопасным сообщением. MD5/SHA-256 и удалённые ключи
корреляции наружу не возвращаются. Статус вычисляется автоматически по
неизменяемым квитанциям успешной доставки и полному историческому read-back из
LMS. Ручными остаются `POST .../similarity-analyses` и
`POST .../authorship-analyses`.

Similarity analysis/match collections фильтруются сервером по видимым
submission. Comparison endpoint возвращает обе стороны immutable pair,
идентичности/группы, файлы и typed line-range fragments. Для обычного
преподавателя достаточно, чтобы хотя бы одна сторона входила в его стандартный
review scope: peer раскрывается только этим endpoint и не расширяет общую
очередь. `SYSTEM_SETTINGS` даёт глобальное read-only чтение. Если ни одна
сторона не разрешена, endpoint маскирует существование пары ответом `404`.

Целевой внешний analyzer callback (не baseline):

- mTLS и/или signed webhook;
- timestamp/nonce/replay protection;
- job ID + manifest hash + analyzer version;
- idempotent completion;
- payload schema validation and max size.

## 11. LMS integration

Baseline public API содержит course import/sync/activity projection и
system/course-scoped outbox read/retry. LMS connection bootstrap выполняет CLI;
connector CRUD, mappings/conflicts endpoints и inbound webhooks — future. Moodle
adapter server-to-server вызывает семь узких external functions plugin 0.3.

Реализованы `GET /integrations/lms/outbox`, `GET
/integrations/lms/outbox/{id}` и `POST /integrations/lms/outbox/{id}/retry` с
system либо course-teacher scope. Connection CRUD/capabilities, public mapping,
conflict/reconcile и webhook endpoints — target; первое connection создаётся
deployment CLI.

Baseline Playwright adapter покрывает recognize URL, delegated auth, discovery,
roster/groups/activities, Quiz Essay и Assignment checkpoint/finalization, исторический
импорт Quiz Essay/Assignment и запись review decision через штатную форму
конкретной сдачи. Assignment answer route принимает только узкий typed
intent; сам worker обнаруживает `ASSIGN_FILE`/`ASSIGN_ONLINE_TEXT`,
заменяет connector-owned draft и возвращает bounded receipt. Task mirror не
поддерживается. Полный generic versioned port с change feed, inbox/conflicts и
вторым adapter — target.
В file transport workspace из ровно одного source превращается в
`main.c`/`main.cpp`; наличие любого второго файла — в детерминированный
`submission.zip`. В ZIP попадают `.c/.cc/.cpp/.cxx`,
`.h/.hh/.hpp/.hxx`, `.inc` и `.txt` с безопасными relative paths. Поэтому
`main.cpp` + `input.txt` — уже ZIP, а не один `.cpp` artifact.
В online-text transport workspace содержит ровно один translation unit и может
содержать вспомогательные `.txt`, но LMS получает только основной source:
вспомогательные файлы отбрасываются на transport boundary.
В history import Assignment online text и attachments представляют два
одновременно сохраняемых канала ответа. ZIP из attachment
материализуется только после проверки relative path, symlink,
суффикса, count и size; материализация текущего контракта имеет версию
`3` и повторяется идемпотентно по stable external revision.
Универсальные «выполнить Moodle function», «fetch arbitrary URL» или передача
raw Moodle cookie запрещены.

Первое connection создаётся deployment provisioning, а не public API: иначе без
external login невозможно получить сессию для `SYSTEM_SETTINGS`. Baseline API не
редактирует и не добавляет connections после bootstrap.

## 12. Settings и audit

Baseline реализует `GET/PATCH /system/settings`, system health/readiness,
глобальный course catalog, teacher-token pool, course/system-filtered outbox и retry. Admin-token rotation/revoke, preferences, audit
export и plan/confirm bulk actions ниже — target. `retention_days` сейчас только
policy value, без cleanup endpoint/job. Непустой `allowed_lms_origins`
дополнительно сужает enabled connections при URL import; пустой оставляет
connection records authoritative и не создаёт новый connector.

- `GET|PATCH /system/settings` — требует активной `SYSTEM_SETTINGS` elevation;

Все операции teacher-token pool также требуют активной `SYSTEM_SETTINGS` elevation:

- `GET /system/teacher-tokens` — список metadata пула: ID/label/public ID/hash fingerprint, `can_reveal`, binding, counters и timestamps; raw secrets не возвращаются;
- `POST /system/teacher-tokens` — body `{ "label": "..." }`, status `201`; генерирует ровно восемь ASCII-букв/цифр, возвращает metadata и полный `token` с `no-store`, сохраняет Argon2id hash и AES-GCM ciphertext;
- `GET /system/teacher-tokens/{token_id}/secret` — возвращает `{ "token": "..." }` только для записи с ciphertext; response имеет `Cache-Control: no-store` и `Pragma: no-cache`, а раскрытие аудируется без secret material. Для legacy hash-only записи возвращает явную ошибку недоступности, не инвалидируя токен;
- `PATCH /system/teacher-tokens/{token_id}` — body `{ "token": "Ab12Cd34" }`; принимает ровно восемь ASCII-букв/цифр, атомарно заменяет Argon2id hash и AES-GCM ciphertext, сохраняет token ID и существующий grant, аудирует замену без secret material;
- `DELETE /system/teacher-tokens/{token_id}` — status `204`; удаляет token/grant, немедленно деактивирует связанные `TEACHER` memberships и аудирует revoke;

Верификация сохраняет обратную совместимость: ранее выпущенные восьмизначные
URL-safe значения (`A–Z`, `a–z`, `0–9`, `-`, `_`) и структурированные
`edut_<public_id>_<secret>` принимаются до явной замены или удаления. Наличие
ciphertext влияет только на возможность административного раскрытия, но не на
Argon2id-аутентификацию.
- `POST /system/admin-token/rotate` — принимает новый secret через защищённое тело, сохраняет только hash/secret reference и создаёт transition window;
- `POST /system/admin-token/revoke-previous`;
- `GET/PATCH /me/preferences`;
- `GET /audit/entries` — course-limited для teacher или system-wide при elevation;
- `POST /audit/exports`;
- `GET /system/health`, `/system/readiness`; отдельного `/system/status` нет;
- destructive bulk actions сначала `POST .../plan`, затем confirmation по plan ID/hash.

API общего управления локальными пользователями/ролями отсутствует; teacher-token pool — узкая privilege boundary. Admin elevation не даёт implicit course data access; object authorization требует и teacher grant, и catalog∩LMS-enrolment/group scope.

## 13. Webhooks и domain events

Внутренние/внешние event names:

- course/membership/group changed;
- task version published;
- assessment published/schedule changed;
- attempt started/locked/submitted/reopened;
- snapshot/checkpoint created;
- run completed;
- teacher experiment created/reset/deleted;
- evidence produced;
- review claimed/decided/revised;
- Moodle export delivered/conflicted;
- AI/similarity/authorship completed;
- policy changed.

Event содержит ID, type/version, occurred/recorded time, LMS connection/course scope, external actor или service context, aggregate ID/revision, trace ID и payload ref. At-least-once delivery предполагается всегда.

## 14. Защита API

Список ниже — production target. Baseline реализует sessions/CSRF,
allowed-origin/host checks, object authorization, bounded Pydantic bodies,
Argon2id admin verifier, Argon2id teacher-token hashes с public selector и
отдельным AES-GCM ciphertext для административного reveal, per-user runner rate/concurrency и AI message rate по
user/mode во всех threads (по умолчанию student 10, teacher 30 за 60 секунд;
`429 AI_RATE_LIMIT` до provider call). Hidden-test reports имеют отдельные
per-teacher concurrency/rate limits (default 1 concurrent, 5 за 300 секунд),
per-case/total timeout и stale recovery. Distributed
admin-token rate limit/progressive lock, signed URLs и unified per-course API
rate limits ещё отсутствуют. Pluginless credential login уже имеет атомарные
DB-backed лимиты отдельно по keyed username и network, включая admin/teacher token attempts в
этой форме; отдельный post-login elevation endpoint временно дополнительно
ограничивает reverse proxy.

- CSRF для cookie mutations;
- strict origin/CORS allow-list;
- per-external-principal/course/IP rate limits с exam overrides; отдельный жёсткий limit для admin-token attempts;
- object-level authorization;
- request/response size limits;
- antivirus/content checks только как дополнительный слой; исходники считаются непроверенными, а принятая v1 policy ограничивает прежде всего filesystem view;
- output encoding and CSP;
- no secrets/PII in URL/query where logs retain it;
- signed URLs короткого срока и single-purpose;
- audit для privilege and data export operations.
- constant-time admin-token verification, selector-bounded Argon2id teacher-token verification, generic error, progressive delay/temporary lock и token value redaction во всех telemetry pipelines.

## 15. Contract tests

Текущие pytest/Vitest suites проверяют DTO, authorization, idempotency,
deadline/outbox, adapters и runner. Сгенерированный TypeScript client, OpenAPI
breaking-diff gate, LTI/webhook conformance и real Moodle version matrix — target.

- generated TypeScript client собирается из OpenAPI в CI;
- schema examples validate;
- backward compatibility check блокирует breaking diff;
- consumer-driven tests core ↔ bridge, core ↔ runner, core ↔ analyzer;
- connector conformance kit, который обязан пройти каждый будущий LMS adapter;
- LTI conformance suite;
- webhook replay/duplicate/out-of-order tests;
- Moodle 4.5/5.2 plugin CI matrix;
- deadline/idempotency/concurrency property tests.
