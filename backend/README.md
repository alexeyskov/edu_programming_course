# Мехмат.Практикум — backend

Backend — нативное async-приложение FastAPI/Starlette с Pydantic v2,
SQLAlchemy 2 и Alembic. Django не используется и legacy Django settings/env
aliases не поддерживаются. HTTP namespace — `/api/v1`; в debug OpenAPI доступен
по `/api/v1/docs`.

## Реализованные области

- Playwright-first pluginless Moodle login, optional delegated bridge,
  server-side sessions, CSRF и dev login;
- только course-scoped роли `STUDENT`/`TEACHER`: LMS подтверждает identity и
  enrollment, а глобальный teacher-token binding задаёт эффективную роль;
- short-lived `SYSTEM_SETTINGS` elevation по Argon2id admin-token verifier;
- course import/sync, roster/groups/sections/activity projection;
- versioned task bank, assessments и explicit `mod_assign` mapping;
- attempts/workspaces/history/internal clipboard/submission;
- synchronous runner dispatch с persisted immutable result;
- claims, review drafts/decisions, private teacher experiments и persisted
  deterministic hidden-test evidence reports;
- AI threads с отдельными student/teacher policies и server-side message budget;
- winnowing similarity и внешний pseudonymous authorship adapter;
- deadline scheduler и transactional LMS outbox worker.

Начальная Alembic revision создаёт 42 таблицы; последующие committed revisions
добавляют runner policy, ограничение внешних ролей, assessment review policy и
deterministic evidence reports. Это схема для чистого развёртывания.
Перенос legacy DB требует отдельной migration на копии данных — нельзя просто
`stamp` неизвестную схему.

Revision `20260825_0009` вводит глобальный каталог курсов, а
`20260825_0010` — пул преподавательских токенов и one-to-one grants. При
применении `0010` прежние активные `TEACHER` memberships деактивируются:
администратор после обновления выпускает индивидуальные токены, и каждый
преподаватель один раз подтверждает новую привязку успешным Moodle-входом.
Revision `20260904_0018` добавляет nullable AES-GCM ciphertext для явного
просмотра токена администратором. Существующие строки намеренно остаются
hash-only и не требуют перевыпуска: они продолжают проходить Argon2id-проверку,
а ciphertext появляется после явной замены значения.

## Локальный запуск

Нужен Python 3.12+ (container использует Python 3.13).

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
export APP_DEBUG=true
export APP_SECRET_KEY=local-development-secret-with-at-least-32-characters
export DATABASE_URL=sqlite+aiosqlite:///./db.sqlite3
alembic upgrade head
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Durable background work запускается в отдельных процессах:

```bash
python -m app.workers.scheduler
python -m app.workers.sync
```

В production нужен PostgreSQL. SQLite пригоден только для локальных
unit/integration-тестов: он не воспроизводит PostgreSQL advisory lock,
`FOR UPDATE SKIP LOCKED`, конкурентные leases и production isolation.

Health endpoints:

- `GET /api/v1/health`;
- `GET /api/v1/system/health`;
- `GET /api/v1/system/readiness`.

`/system/health` показывает состояние API/DB/build, а не сетевой probe каждого
внешнего провайдера. Runner readiness проверяется отдельно.

## Проверки

```bash
pytest
ruff check app tests
alembic check
```

Чтобы увидеть реальные test names/количество, используйте `pytest -q`; README не
фиксирует счётчик, который меняется при разработке. Integration tests Moodle и
runner используют mocks/fakes и не доказывают совместимость с реальной Moodle
или Bubblewrap-хостом.

## Аутентификация, административный и преподавательский токены

Локальной регистрации и паролей нет. `core_principalsession` хранит SHA-256
высокоэнтропийного cookie bearer. Unsafe cookie-authenticated запросы требуют
signed double-submit token от `GET /api/v1/auth/csrf`.

Default connection имеет `auth_mode=PLUGINLESS` и
`pluginless_transport=PLAYWRIGHT`. Credentials endpoint принимает Moodle
username/password в теле одного HTTPS request. Временный HTTP-only стенд можно
явно включить через `MOODLE_CREDENTIAL_LOGIN_ALLOW_INSECURE_HTTP=true`; по
умолчанию и после появления TLS этот флаг должен быть выключен. Backend по
HMAC-аутентифицированному внутреннему каналу передаёт credentials отдельному
`moodle-browser`. Worker открывает только штатную HTML login form exact
provisioned Moodle origin, проверяет identity и возвращает очищенный Playwright
`storage_state`. Password после request не используется, не сохраняется и не
журналируется.

`storage_state` хранится server-side как AES-GCM ciphertext credential kind
`BROWSER_STATE_V1` под отдельным `MOODLE_CREDENTIAL_ENCRYPTION_KEY`; frontend
его не получает. Credential имеет revision, lease owner/expiry и last-used
timestamp: concurrent operation не может затереть более новый state. Legacy
kind `MOBILE_TOKEN` сохраняется только для совместимости данных и не является
default/fallback. Login передаёт browser-worker только numeric ID курсов из
глобального администраторского каталога. Worker возвращает только пересечение
этого allow-list с Moodle dashboard вошедшего пользователя: Moodle тем самым
подтверждает identity и enrollment, но не платформенную роль. Без действующей
привязки преподавательского токена каждый такой membership проецируется как
`STUDENT`; при действующей привязке — как `TEACHER`. HTML role hints, roster и
поле frontend не могут повысить роль, а курс вне каталога или dashboard не может
создать membership.

Bridge login — опциональный enhanced mode для Moodle с установленным
`local_programming_bridge`. В нём login transaction имеет независимые
hashes `state` и browser verifier. Callback проверяет подпись,
issuer/audience, expiry, state, verifier cookie и одноразовый nonce. Оба
режима создают одинаковую локальную principal/session projection. Только
после внешней аутентификации может быть выдана administrative capability;
admin token сам по себе не создаёт session и не даёт доступа к чужому
курсу.

`SYSTEM_SETTINGS` также управляет пулом индивидуальных преподавательских
токенов через `/api/v1/system/teacher-tokens`. `POST` генерирует ровно восемь
ASCII-букв и цифр (`A–Z`, `a–z`, `0–9`) криптографическим генератором с
равномерным выбором без modulo bias. Пространство `62^8` соответствует примерно
47,6 битам энтропии. Селектор для поиска вычисляется из
SHA-256 токена. В `core_teacheraccesstoken` сохраняются Argon2id verifier для
входа и AES-GCM ciphertext для явного административного раскрытия; открытый
secret не журналируется.

`GET /system/teacher-tokens` возвращает только metadata, fingerprint, сведения
о привязке и `can_reveal`, но никогда не secret. `GET
/system/teacher-tokens/{token_id}/secret` возвращает `{ "token": "..." }` только
при активной `SYSTEM_SETTINGS`, с `no-store` и обязательной audit-записью.
`PATCH /system/teacher-tokens/{token_id}` с body `{ "token": "Ab12Cd34" }`
атомарно заменяет verifier и ciphertext; новое значение обязано состоять ровно
из восьми ASCII-букв и цифр. Grant к principal сохраняется, а прежний secret
сразу перестаёт действовать. `DELETE` по-прежнему отзывает саму запись и grant.

Существующие восьмизначные URL-safe токены с `-`/`_` и legacy-токены вида
`edut_<public_id>_<secret>` продолжают проверяться. Записи, созданные до миграции
и содержащие только Argon2id hash, имеют `can_reveal=false`: их secret нельзя
восстановить, но известное значение остаётся валидным до замены или удаления.
После замены такая запись становится раскрываемой. Токен следует передавать
конкретному преподавателю по защищённому каналу.

Первая успешная Moodle-аутентификация с `teacher_token` атомарно создаёт строгую
one-to-one запись `core_teachertokengrant`: один token не может быть использован
другим principal, и у principal не может быть второй активной привязки.
Неуспешная Moodle-аутентификация ничего не привязывает. При следующих входах
backend находит сохранённую grant, поэтому повторно вводить token не требуется.
DELETE token удаляет grant и в той же транзакции деактивирует связанные
`TEACHER` memberships, создавая/активируя эквивалентные `STUDENT` memberships;
следующий запрос уже не обладает teacher-доступом. Административный token и
преподавательский token независимы: первый даёт только `SYSTEM_SETTINGS`, второй
— только роль в курсах из `catalog ∩ Moodle dashboard`.

Прямой Moodle launch без platform-generated `state`/browser verifier после HMAC
проверки дополнительно требует browser `Origin` либо `Referer` с exact origin
настроенного `LMSConnection`; иначе возвращается
`403 INVALID_LAUNCH_ORIGIN`. Stateful login, начатый платформой и связанный
state+verifier cookie, этой проверки не требует.

Повышение привязано к текущей server session и префиксу адреса клиента (`/24`
для IPv4, `/64` для IPv6). Если префикс меняется, middleware отзывает только
`SYSTEM_SETTINGS`; обычная LMS-сессия пользователя продолжает действовать.

Создать verifier:

```bash
python -m app.cli hash-admin-token
```

Для защищённого stdin в CI:

```bash
python -m app.cli hash-admin-token --stdin
```

В production хранится только `ADMIN_TOKEN_HASH`. Plaintext `ADMIN_TOKEN`
разрешён лишь при `APP_DEBUG=true`; конфигурация с plaintext в production
отклоняется при startup.

## Workspace и сроки

Серверный hard limit текущего baseline — 64 активных файла и 512 KiB суммарного
UTF-8 source text. Single-file assessment требует ровно один C/C++ translation
unit: его удалить нельзя, но можно создавать и удалять сопутствующие `.txt`
(кроме Moodle online-text mapping, который физически не переносит второй файл).
Остальные source/header create/delete доступны только в multi-file режиме.
Путь — нормализованный relative POSIX source path.

Workspace и штатные runner profiles принимают C/C++ translation units, headers,
`.inc` и `.txt`. Перед запуском `.txt` копируются с безопасными относительными
путями в тот же временный writable cwd, где лежит executable; программа может
читать и перезаписывать их. Runtime-изменения и созданные text/binary files не
возвращаются в workspace, историю или Moodle и удаляются вместе с job cwd.

Изменение использует `If-Match`/expected revision и idempotency key. Backend
получает полный новый content, вычисляет contiguous semantic delta и добавляет
metadata-bound event в hash chain. После отзыва текущего Moodle membership
student write/manual submit запрещаются; scheduler всё равно может зафиксировать
deadline submission уже принятой ревизии.

Для длительности `D` scheduler ставит checkpoint примерно каждые `D/10`, а в
последней пятой — `D/20`; фактический интервал clamp-ится к 30–900 секундам.
Отдельны точки `ATTEMPT_STARTED`, `FINAL_MINUTE`, `SUBMISSION` и `DEADLINE`.
Автосдача всегда использует последнюю подтверждённую сервером snapshot/revision.

## Moodle sync: pluginless default и optional bridge 0.3

`sync-worker` обрабатывает четыре типа outbox:

- `course.sync` — course/roster/groups/activity projection;
- `attempt.checkpoint` — canonical manifest + SHA-256 + event-chain anchor;
- `task.version` — connector-owned immutable task mirror;
- `review.decision` — grade/comment в подтверждённый `mod_assign` или Quiz
  Essay через штатную форму Playwright; optional bridge остаётся отдельным
  transport-вариантом.

На pluginless Playwright connection `course.sync` читает bounded HTML snapshots
точного Moodle origin. `attempt.checkpoint` поддерживает только явно
сопоставленные активности с доказанным форматом ответа: один или несколько Essay questions в
`mod_quiz` либо стандартный ответ `mod_assign`. Для Assignment по фактической
студенческой форме выбирается `ASSIGN_FILE` или `ASSIGN_ONLINE_TEXT`; при наличии
обоих предпочтителен файловый ответ. Неоднозначность формы, командная сдача и
неподтверждённый transport закрываются fail-closed **при доставке**, до изменения
ответа Moodle. Локальная публикация импортированной работы выбирает только
группы и не зависит от заранее спроецированного transport.

Для Quiz с несколькими вопросами миграция `20260910_0019` добавляет связи
`MoodleQuizQuestion`: общий root attempt координирует независимые рабочие области
вопросов. DTO `quiz_session` возвращает их идентификаторы для переключателя IDE.
Сохранение и запуск относятся к выбранному вопросу; завершение любого вопроса
атомарно фиксирует все решения и создаёт один terminal outbox. Новый внутренний
endpoint `/internal/v1/moodle/quiz/answers/sync` сохраняет ответы по точным slots
и завершает Moodle attempt только после проверки полного набора. Старый
single-answer endpoint не используется для много-вопросной попытки.

Backend, sync-worker, moodle-browser и frontend необходимо обновлять вместе,
предварительно применив миграцию. Существующие одно-вопросные попытки и их
fingerprints сохраняются; старые записи не преобразуются в новые сессии.

Для online-text ровно одна единица трансляции передаётся полными UTF-8 bytes под
стабильным именем `main.c` или `main.cpp`; вспомогательные `.txt` остаются в IDE
и не включаются в текстовый ответ. Для file transport единственный исходник
отправляется напрямую, а любой второй файл, включая `.txt` или заголовок,
переводит ответ в детерминированный `submission.zip`. Архив сохраняет безопасные
относительные пути и допускает `.c/.cc/.cpp/.cxx`, `.h/.hh/.hpp/.hxx`, `.inc`
и `.txt`. Decoded artifact ограничен 4 MiB, внутренний base64/JSON request —
6 MiB. Для online-text transport рабочая область ограничивается одной единицей
трансляции, но может содержать вспомогательные `.txt` для запуска программы.

Reasons `PERIODIC`/`FINAL_MINUTE` требуют `DRAFT_SAVED`, а
`SUBMISSION`/`DEADLINE` — `FINALIZED`. Outbox использует idempotency, lease,
superseding устаревшей revision и bounded retry. Локальный snapshot остаётся
authoritative при недоступности Moodle; events, compiler output и edit history
не выгружаются. `task.version` для pluginless connection не enqueue-ится:
connector-owned task mirror доступен только optional bridge 0.3.

Backend idempotency key durable в outbox, но cache browser-worker находится в
памяти одного процесса. Поэтому ambiguous final response после restart не
является доказанным exactly-once; до pilot нужен staging-тест repeat/finalized
attempt policy конкретной Moodle activity.

`review.decision` через Playwright записывается только после повторной проверки
course/activity/student context и успешного read-back формы. Локальная запись
решения остаётся authoritative до подтверждённого receipt; один лишь mapping
или факт открытия grading page не считается доставкой.

Claim фиксируется короткой транзакцией, Moodle HTTP вызывается без row lock,
receipt/state записывается новой транзакцией. Retry bounded настройками
`LMS_SYNC_*`; несколько workers используют lease и `SKIP LOCKED`. Scheduler
сериализуется PostgreSQL advisory lock.

Assignment или Quiz Essay выбирается из `lms_activities`; mapping остаётся
явным. Course sync проецирует `allowsubmissionsfromdate` и
`cutoffdate`/`duedate` только на mapped assessment. `cutoffdate` приоритетнее;
исчезнувший activity помечается `MISSING_IN_MOODLE`, предыдущие сроки не
стираются. Правила positive numeric `grade_max == Assessment.max_score`
сохраняются для будущей/bridge grade delivery, но Playwright grade/comment
остаётся `501`/manual; неявного пересчёта баллов нет.

Canonical checkpoint/task JSON сериализует Python backend. В optional
bridge mode Moodle plugin
проверяет валидность JSON и SHA-256 точных отправленных UTF-8 байтов, не
пересериализуя их в PHP. После checkpoint recovery backend повторно сверяет
manifest/file hashes, attempt/course/user, epoch, workspace revision и
`event_chain_head`.

## Runner contract

Backend не запускает compiler/binary. Он отправляет HMAC-signed manifest только
с approved profile, source files, stdin и optional `{cpu_seconds, memory_mb}`.
Runtime limits из system settings могут только уменьшить фиксированный профиль
runner, но не увеличить его. Ответ принимается лишь при
`filesystem_isolated=true` и `network=denied`.

Внутренний mock не исполняет C/C++; используйте его только для UI/API smoke.

## Детерминированные evidence runs

Опубликованная `TaskVersion` может хранить hidden-test manifest v1: от 1 до 20
уникально названных stdin/stdout cases; каждое поле потока ограничено 256 KiB
UTF-8, а весь manifest — 1 MiB. Сравнение — только
`EXACT` либо `TRIM_TRAILING_WHITESPACE`; manifest не принимает shell command,
пути, environment, веса или произвольные runner flags. Пустой `{}` означает,
что скрытые тесты отключены.

Преподаватель с active owned review claim и включённой для assessment СППР
может запустить immutable submission snapshot:

- `POST /api/v1/submissions/{id}/evidence-runs` — синхронный bounded запуск;
  необязательный `Idempotency-Key` повторно возвращает тот же отчёт;
- `GET /api/v1/submissions/{id}/evidence-runs` — список отчётов;
- `GET /api/v1/evidence-runs/{report_id}` — один отчёт.

Каждый case отправляется отдельным `TEST` request в реальный runner, а partial
outcomes фиксируются после cases. Defaults: wall 8 секунд и CPU 2 секунды на
case, wall 50 секунд на весь HTTP request, один одновременно выполняющийся
report на преподавателя, 5 reports за 300 секунд и stale recovery через 120
секунд. `EVIDENCE_RUNNING_STALE_SECONDS` обязан превышать total timeout минимум
на 15 секунд. Для одного submission/snapshot допускается только один `RUNNING`
report; interrupted report при следующей попытке становится durable `FAILED`,
а не исчезает.

Официальный report создаётся только при `filesystem_isolated=true` и
`network_enabled=false`; `RUNNER_MOCK_ENABLED=true` возвращает
`EVIDENCE_REQUIRES_REAL_RUNNER` до создания отчёта. Report хранит snapshot/task/
manifest hashes, outcome `PASSED|FAILED|INFRASTRUCTURE_ERROR`, bounded 4096-byte
stdout/stderr previews и findings. Он не содержит grade, recommended score и не
создаёт review decision. Если преподаватель ссылается на hidden-test evidence в
`evidence_ids` решения, текущий контракт принимает ID конкретных official case
`RunRequest`, а не ID report; сервер повторно проверяет submission/snapshot/task
hashes, completed report и isolation flags.

## System settings и ограничения

`SYSTEM_SETTINGS` позволяет включать/выключать AI, student AI и runner, менять
runtime CPU/memory bounds, список LMS origins, incident banner и сохранять
`retention_days`. В baseline `retention_days` — только policy value: cleanup job
и автоматическое удаление данных не реализованы.

`allowed_lms_origins` применяется только к импорту URL: непустой список —
дополнительное пересечение с origins включённых `LMSConnection`; пустой список
оставляет сами connector records авторитетным allow-list. Настройка никогда не
создаёт connection и не разрешает произвольный origin сама по себе.

`POST /course-imports` и подтверждение импорта требуют `SYSTEM_SETTINGS`.
Локальной membership первого ещё не импортированного курса не существует,
поэтому Moodle adapter независимо сверяет actor с актуальными
roster/administration options целевого курса и требует там `TEACHER`. Confirm
включает курс в глобальный каталог. Административное повышение само по себе не
даёт читать или администрировать его содержимое — обычные course endpoints
по-прежнему требуют соответствующий Moodle enrollment и действующую
преподавательскую token binding для роли `TEACHER`.

AI/runner/integrity результаты являются evidence для преподавателя. Финальная
оценка создаётся только review decision человека. Ошибка внешнего сервиса не
должна блокировать локальное редактирование или сдачу.

ИИ-провайдер настраивается через `LLM_API_ADDRESS`, `LLM_API_KEY`, `LLM_MODEL`.
Стандартный профиль: адрес с `/v1`, `AI_API_STYLE=chat_completions` и пустой
`LLM_THINKING=`. Один протокол используется для Ollama, vLLM и SGLang;
`LLM_THINKING=0/1` включайте только при поддержке сервером и моделью.
Прежние `AI_*` значения остаются совместимыми, также поддерживаются
Responses, OpenRouter и родной Ollama `/api/chat`. Локальная
Ollama работает без ключа и без включения debug. Для прямого запуска включите
`AI_ENABLED=true`; глобальные/курсовые ограничения помощника сохраняются.
Примеры приведены в `../DEPLOYMENT.md`, раздел 7.

До вызова AI provider backend считает сообщения владельца по всем его threads
того же режима за скользящее окно. Defaults: student `10`, teacher `30` за `60`
секунд (`AI_*_RATE_LIMIT_*`); превышение возвращает `429 AI_RATE_LIMIT` и не
вызывает provider. Лимит process-independent, потому что опирается на БД и
сериализуется блокировкой principal row.

## Docker и PostgreSQL

Корневой `.env.example` — Compose source of truth. URL `postgresql://` и
`postgres://` нормализуются к `postgresql+asyncpg://`. Container entrypoint
применяет `alembic upgrade head` только при `AUTO_MIGRATE=true`, затем запускает
Uvicorn на `0.0.0.0:8000`. Production Compose передаёт
`BACKEND_AUTO_MIGRATE=false`: migration выполняется отдельной командой до web и
workers, как описано в `../DEPLOYMENT.md`. `compose.local.yml` включает
auto-migrate только для одноузлового developer smoke; workers никогда не
запускают migration автоматически.
