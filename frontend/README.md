# Мехмат.Практикум — frontend

React/TypeScript SPA для студенческих работ и преподавательской проверки. Целевая ширина — от 768 px: desktop с разными соотношениями сторон и большие планшеты. Телефонная компоновка намеренно не поддерживается в первой версии.

Текущий frontend подключён к FastAPI `/api/v1` и реализует реальные сценарии
входа, курса/банка/работы, IDE, истории, запуска/сдачи, очереди проверки,
teacher experiment, integrity/AI и системных настроек. Demo adapter остаётся
только средством разработки и не должен попадать в production build.

Рабочая область ограничена backend до 64 файлов и 512 KiB исходного UTF-8
текста. UI не должен считать клиентскую валидацию авторитетной: file mode,
revision, paste receipt, membership и deadline повторно проверяются сервером.

## Локальный запуск

Требования: Node.js 22 и npm 10.

```bash
cp .env.example .env.local
npm ci
npm run dev
```

Vite слушает `http://localhost:5173` и проксирует `/api` на `http://localhost:8000`. В `.env.local` доступны режимы:

- `VITE_DEMO_MODE=always` — только встроенные сценарии, backend не нужен;
- `VITE_DEMO_MODE=auto` — API с fallback на demo при сетевой/404 ошибке только в dev;
- `VITE_DEMO_MODE=never` — только реальный API; production default;
- `VITE_DEV_LOGIN=true` — показать вход студентом/преподавателем через `/auth/dev-login`.

Для demo административное повышение включается любым непустым токеном. Это поведение существует только внутри client demo adapter и не относится к реальному API.

## Проверки

```bash
npm test
npm run build
```

Тесты покрывают безопасные пути файлов, TTL внутреннего clipboard receipt,
пагинацию и нормализацию реальных snake_case DTO attempt/run/diagnostics/
evidence, а также client validation hidden-test manifest. Server validation всё
равно остаётся авторитетной.

## Docker

```bash
docker build -t eduprog-frontend .
docker run --rm -p 8080:8080 --add-host backend:host-gateway eduprog-frontend
```

Production-образ собирает SPA в Node stage и отдаёт его Nginx на порту `8080`. Контракт для общей Compose-сети:

- backend DNS: `backend:8000`;
- `/api/` проксируется без изменения URI;
- `/ws/` проксируется с WebSocket upgrade;
- `GET /healthz` отвечает `200 ok` без обращения к backend;
- неизвестные browser routes возвращают `index.html`.

Nginx принимает из входящего `X-Forwarded-Proto` только точное значение `http`
или `https`; malformed/chained значение заменяется собственным `$scheme`. Это
нужно для TLS termination перед контейнером. Поэтому frontend-порт должен быть
доступен только доверенному reverse proxy/туннелю и не публиковаться в обход
этой точки входа.

Сборочные аргументы Docker: `VITE_API_BASE`, `VITE_DEMO_MODE`, `VITE_DEV_LOGIN`, `VITE_APP_NAME`. Для production не включайте demo/dev login.

## Фактический REST API baseline

SPA работает с FastAPI `/api/v1` и secure session cookie. Перед первой unsafe
mutation клиент получает `GET /auth/csrf` с `{ "csrf_token": "..." }`, затем
передаёт `X-CSRFToken`. После `403` токен обновляется и запрос повторяется ровно
один раз.

Для default `PLAYWRIGHT` connection форма отправляет Moodle login/password один
раз в credentials endpoint и очищает password/admin-token после завершения.
Playwright `storage_state` остаётся только в зашифрованном server-side
credential под lease/revision и никогда не передаётся SPA.

Коллекции могут возвращаться plain array или как `{ "results": [...] }`, `{ "items": [...] }`, `{ "data": [...] }`. Основные вызовы frontend:

| Область | Endpoint |
| --- | --- |
| Auth | `GET /auth/csrf`, `GET /auth/connections`, `GET /auth/session`, `POST /auth/lms/{connection_id}/credentials`, `POST /auth/lms/{connection_id}/start`, `POST /auth/dev-login`, `POST /auth/logout` |
| Повышение | `POST /auth/admin-elevation`, `DELETE /auth/admin-elevation` |
| Курсы | `GET /courses`, `POST /courses/{id}/sync`, `POST /course-imports`, `POST /course-imports/{id}/confirm` |
| Банк | `GET/POST /task-bank/items`, `POST /task-bank/items/{id}/versions`, `GET /task-versions`, `POST /task-versions/{id}/validate`, `POST /task-versions/{id}/publish` |
| Работы | `GET/POST /courses/{course_id}/assessments`, `GET /assessments/{id}`, `POST /assessments/{id}/items`, `POST /assessments/{id}/validate`, `POST /assessments/{id}/publish` |
| Попытка | `POST /assessments/{id}/attempts`, `GET /attempts/{id}`, `GET /attempts/{id}/workspace`, `GET /attempts/{id}/history`, `PATCH /attempts/{id}/workspace/files/{file_id}`, `POST /attempts/{id}/workspace/files/new`, `POST /attempts/{id}/clipboard-receipts`, `POST /attempts/{id}/submit` |
| Запуск | `POST /attempts/{id}/runs`, `GET /runs/{id}` |
| Проверка | `GET /assessments/{id}/submissions` (`id=all` для общей очереди), `GET /submissions/{id}`, `POST /submissions/{id}/claims`, `POST /review-claims/{id}/heartbeat`, `DELETE /review-claims/{id}`, `GET/POST /submissions/{id}/evidence-runs`, `GET /evidence-runs/{id}`, `PUT /submissions/{id}/review-draft`, `POST /submissions/{id}/review-decisions` |
| Песочница | `POST /submissions/{id}/teacher-experiments`, `PATCH /teacher-experiments/{id}/files/{file_id}`, `POST /teacher-experiments/{id}/runs`, `POST /teacher-experiments/{id}/reset`, `DELETE /teacher-experiments/{id}` |
| Integrity | `GET/POST /submissions/{id}/authorship-analyses`, `GET/POST /assessments/{id}/similarity-analyses` |
| ИИ | `POST /ai/student-threads`, `POST /ai/teacher-threads`, `POST /ai/threads/{id}/messages` |
| Система | `GET/PATCH /system/settings`, `GET /system/health` |

При создании работы frontend показывает обнаруженные course-sync активности
`mod_assign` и `mod_quiz`. Выбранный module+numeric `cmid` превращается в явный
mapping, а Moodle open/due/cutoff проецируются в сроки локальной работы. Для Quiz
текущий контракт допускает только попытку с ровно одним Essay file question:
один source отправляется полностью, несколько файлов — детерминированным ZIP;
decoded artifact ограничен 4 MiB, внутренний JSON request — 6 MiB.
`PERIODIC`/`FINAL_MINUTE` должны завершаться `DRAFT_SAVED`, а
`SUBMISSION`/`DEADLINE` — `FINALIZED`; локальный snapshot остаётся authoritative
при ошибке внешней доставки.

Grade/comment через Playwright пока не выгружаются: browser route возвращает
`501`, поэтому официальную оценку преподаватель переносит в Moodle вручную.
Положительный numeric `grade_max` Assignment и совпадение с `max_score` остаются
валидацией mapping для optional bridge/будущей grade delivery. Frontend не
создаёт и не изменяет Moodle activity.

Unsafe запросы workspace используют `If-Match` с последней подтверждённой ревизией; start/submit/finalize используют `Idempotency-Key`. DTO передаются в `snake_case`. Минимальный session-контракт:

```json
{
  "principal": {"id": "uuid", "display_name": "Имя"},
  "roles": ["STUDENT", "TEACHER"],
  "memberships": [{"course_id": "uuid", "course_name": "C++", "role": "TEACHER"}],
  "capabilities": ["SYSTEM_SETTINGS"],
  "admin_elevation_expires_at": null
}
```

Attempt может иметь только редактируемое состояние `ACTIVE`. `FINISHING`, `LOCKED`, `VOID` и неизвестные будущие состояния fail closed в read-only; `SUBMITTED` и `AUTO_SUBMITTED` отображаются как сданные. Workspace возвращает `current_revision` и `files`. Runner DTO поддерживается в виде:

```json
{
  "status": "FAILED",
  "revision": 8,
  "result": {
    "stdout": "",
    "stderr": "compile failed",
    "diagnostics": [{
      "file": "src/main.cpp",
      "range": {"start_line": 4, "start_column": 8, "end_line": 4, "end_column": 9},
      "severity": "error",
      "code": "expected_semi",
      "message": "expected ;"
    }]
  }
}
```

Diagnostic также может быть flat, но основной контракт — `file` плюс вложенный `range`. Authorship UI показывает число только для `COMPLETED` result, если сервер передал `analyzer`, `model` и непустую `calibration`; иначе числовой вывод намеренно скрывается. Similarity UI показывает только фактические `matches` конкретной сдачи и `algorithm_version`. Ни один integrity result не меняет оценку автоматически.

Вкладка review «Тесты» читает persisted hidden-test reports и запускает новый
report только при `review_required`, включённой СППР и owned active claim. Она
показывает immutable manifest/task/snapshot hashes, final/partial status,
1–20 outcomes, findings, bounded stdout/stderr previews и isolation flags.
Сохранённые reports остаются доступны read-only при отключённой СППР. Report не
заполняет grade и не создаёт решение; запрос синхронный, а server default total
timeout — 50 секунд. Mock runner official evidence не создаёт.

Изменённые файлы IDE сохраняются последовательно с `If-Match`; перед сборкой и сдачей очередь полностью flush-ится. `409` не затирает данные: UI останавливает сохранение, позволяет скачать локальную копию и только явно загрузить серверную версию.

Неподтверждённая очередь текущей реализации хранится в памяти открытой вкладки. При попытке закрыть её браузер показывает предупреждение, но полноценное зашифрованное offline-восстановление через IndexedDB пока не заявляется как готовое.

## Намеренные ограничения первой версии

- Компоновка рассчитана на ширину от 768 px; телефонный интерфейс не готов.
- Monaco completion предлагает только C/C++ keywords и идентификаторы, уже найденные в текущей workspace. Генеративного inline completion нет.
- Workspace допускает `.txt`, но текущие реальные runner profiles принимают
  только translation units и headers. До реализации task-data mapping текстовый
  файл приведёт к отказу run manifest; UI пока не предотвращает этот случай.
  Runner также умеет `.c++`/`.inc`, которых core workspace не разрешает, поэтому
  их нельзя считать end-to-end форматом baseline.
- Редактор версии задания создаёт согласованный starter file/build profile,
  новую immutable version и bounded hidden-test manifest v1: disabled `{}` либо
  1–20 уникально названных stdin/stdout cases с `EXACT` или
  `TRIM_TRAILING_WHITESPACE`. Name ограничено 100 символами, каждое text field —
  262144 байтами UTF-8, aggregate JSON — 1 MiB; backend повторно валидирует manifest
  при публикации. Rubric, public-suite и reference-solution editors требуют
  отдельного этапа.
- Очередь несохранённых edits не восстанавливается после закрытия/краша вкладки; конфликт ревизий можно экспортировать в JSON до явной перезагрузки.
- Таймер использует deadline сервера, но текущий момент берёт с устройства: offset server time API пока не применяется.
- Claim heartbeat реализован polling каждые 120 секунд. WebSocket-индикация чужих claim и live-событий пока не используется.
- `/system/health` показывает только явно возвращённые API/database/build поля; UI не предполагает готовность runner, LMS или AI.
- Moodle plugin 0.3 task bank — connector-owned read-only mirror с точки зрения
  Moodle UI; native Quiz/question-bank two-way editor и LTI Deep Linking не
  реализованы.
- Playwright login/course discovery/Quiz Essay UI и transport покрыты
  репозиторными тестами, но это не live production validation на Moodle/NUC;
  pilot требует отдельного staging прогона.
- `retention_days` можно сохранить в системных настройках, но автоматического
  удаления в baseline нет; UI не должен обещать, что изменение поля уже удаляет
  данные.
