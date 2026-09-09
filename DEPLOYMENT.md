# Развёртывание «Мехмат.Практикум»

Документ описывает воспроизводимый baseline первой версии. Он не является гарантией production-безопасности: перед доступом реальных студентов нужны собственные threat model, нагрузочные испытания, аудит хоста, TLS, мониторинг и проверенный план восстановления.

Baseline считается кандидатом только для development/staging. В частности,
Playwright login/course discovery и Quiz Essay/Assignment draft/final sync реализованы в
репозитории, но ещё не подтверждены после сборки на целевом NUC и реальной
Moodle. Опциональная установка Moodle plugin 0.3, будущая hardened sandbox, exam load,
backup recovery и внешние AI/authorship providers также требуют отдельной
staging-проверки. Grade/comment write через Playwright реализован для точно
связанного Assignment либо отдельного вопроса Quiz Essay через штатные формы,
но до production требует live staging write/read-back.

## 1. Что запускает Compose

| Сервис | Назначение | Доступность |
|---|---|---|
| `frontend` | Nginx + React SPA, proxy `/api/` | `${PUBLIC_BIND_ADDRESS}:${PUBLIC_PORT}` |
| `backend` | FastAPI/Pydantic v2 + Uvicorn/ASGI | только сети Compose |
| `deadline-worker` | закрытие сроков и планирование checkpoint/outbox | только БД |
| `sync-worker` | доставка outbox в Moodle с повторами | БД и исходящий HTTPS |
| `runner` | GCC/Clang, обычный subprocess внутри контейнера | только backend во внутренней сети |
| `moodle-browser` | один Chromium для pluginless login/course discovery, Quiz Essay/Assignment answer sync, read-only исторического Quiz Essay/Assignment импорта и grade/comment write через штатные формы | только backend во внутренних сетях |
| `postgres` | данные, серверные сессии, leases и transactional outbox | только backend/workers |

`python -m app.workers.scheduler` и `python -m app.workers.sync` — штатные long-running процессы. Они используют те же async SQLAlchemy repositories, что и API, но имеют собственный жизненный цикл и не стартуют внутри Uvicorn worker. Scheduler сериализуется PostgreSQL advisory lock; несколько sync workers могут конкурентно забирать outbox-пакеты через короткие транзакции с `SELECT ... FOR UPDATE SKIP LOCKED`, lease и bounded retry. Исходящий Moodle HTTP-запрос выполняется после фиксации claim, а receipt записывается отдельной транзакцией.

## 2. Требования к серверу

- Linux x86-64 или arm64 с актуальным ядром;
- Docker Engine и Docker Compose v2;
- синхронизация времени (NTP), поскольку HMAC-подписи runner и
  `moodle-browser` краткоживущие;
- отдельный TLS reverse proxy перед `frontend`;
- желательно отдельная машина/VM для runner.

Не монтируйте в runner или `moodle-browser` Docker socket, исходники приложения,
домашние каталоги либо `.env`. Их порты не публикуются наружу.

## 3. Подготовка конфигурации

```bash
cp .env.example .env
openssl rand -hex 48
openssl rand -hex 32
openssl rand -hex 32
openssl rand -hex 32
```

Первое значение используйте как `APP_SECRET_KEY`, второе — как
`RUNNER_SHARED_SECRET`, третье — как `MOODLE_CREDENTIAL_ENCRYPTION_KEY`, четвёртое
— как независимый `MOODLE_BROWSER_SHARED_SECRET` внутреннего HMAC-канала.
Pluginless default не требует `MOODLE_LAUNCH_SHARED_SECRET`; для optional bridge
сгенерируйте ещё один независимый secret. Задайте пароль PostgreSQL и синхронно
обновите `POSTGRES_PASSWORD` и `DATABASE_URL`, например
`postgresql://programming_course:password@postgres:5432/programming_course`.
Backend сам преобразует URL в asyncpg-формат.

Для публичного адреса `https://code.example.edu` обязательны как минимум:

```dotenv
PUBLIC_BIND_ADDRESS=127.0.0.1
PUBLIC_PORT=5173
PUBLIC_BASE_URL=https://code.example.edu
FRONTEND_URL=https://code.example.edu
ALLOWED_HOSTS=code.example.edu,localhost,127.0.0.1,backend
CORS_ALLOWED_ORIGINS=https://code.example.edu
SESSION_COOKIE_SECURE=true
SECURE_SSL_REDIRECT=true
SECURE_HSTS_SECONDS=31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS=true
APP_DEBUG=false
DEV_AUTH_ENABLED=false
MOODLE_MOCK_ENABLED=false
RUNNER_MOCK_ENABLED=false
AI_MOCK_ENABLED=false
VITE_DEMO_MODE=never
VITE_DEV_LOGIN=false
MOODLE_BASE_URL=https://edu.mmcs.sfedu.ru
MOODLE_CREDENTIAL_ENCRYPTION_KEY=<отдельный-постоянный-secret-не-короче-32-символов>
MOODLE_BROWSER_SHARED_SECRET=<отдельный-HMAC-secret-не-короче-32-байт>
MOODLE_BROWSER_MAX_CONCURRENT_OPERATIONS=3
MOODLE_BROWSER_QUEUE_WAIT_SECONDS=8
MOODLE_BROWSER_NAVIGATION_TIMEOUT_MS=30000
MOODLE_BROWSER_LOGIN_OPERATION_TIMEOUT_SECONDS=45
```

Значения `PUBLIC_BIND_ADDRESS=127.0.0.1` и `PUBLIC_PORT=5173` относятся к
целевому NUC: Compose публикует frontend только на loopback NUC. Внешний VPS
слушает свой порт `8080` и проксирует либо туннелирует его на
`127.0.0.1:5173` NUC. Не задавайте на NUC `PUBLIC_PORT=8080` только ради
совпадения номеров портов. `PUBLIC_BASE_URL`, `FRONTEND_URL`, allowed hosts и
CORS должны описывать внешний URL, который открывает браузер пользователя
(включая `:8080`, если это часть публичного origin), а не внутренний адрес NUC.

`VITE_*` — build-time настройки. После их изменения требуется `docker compose build frontend`.

### Токен административного повышения

Открытый административный токен не хранится в production `.env`. Получить Argon2id verifier можно без подключения к БД:

```bash
docker compose build backend
docker compose run --rm --no-deps backend python -m app.cli hash-admin-token
```

Запишите вывод как `ADMIN_TOKEN_HASH='...'` в одинарных кавычках: так символы `$` не интерполируются Compose. Оставьте `ADMIN_TOKEN` пустым. Повышение даёт лишь временную capability системных настроек уже вошедшему через LMS пользователю; третьей роли в системе нет.

После истечения capability страница настроек предлагает ввести токен повторно,
не разрывая Moodle-сессию. После успешной проверки frontend сохраняет
административный токен origin-scoped в `localStorage` и при следующем входе
подставляет его в скрытое password-поле; там же может храниться успешно
проверенный преподавательский токен. Moodle-пароль не сохраняется. На общем
компьютере используйте кнопку «Забыть сохранённые токены» на странице входа.
Хранилище привязано к точному origin: смена HTTP на HTTPS, домена или порта
создаёт для браузера другое хранилище и не переносит сохранённые значения.

### Пул преподавательских токенов

Преподавательские токены не задаются в `.env`: ими управляет пользователь с
временной capability `SYSTEM_SETTINGS` в разделе системных настроек. Для каждого
преподавателя выпускайте отдельную подписанную меткой запись. Backend генерирует
ровно восемь символов из ASCII-алфавита `A–Z`, `a–z`, `0–9`: используется
криптографический генератор и равномерный выбор без modulo bias. Пространство
из `62^8` значений соответствует примерно 47,6 битам энтропии.
В PostgreSQL сохраняются Argon2id hash для аутентификации и AES-GCM ciphertext
для явно запрошенного административного просмотра. Список токенов возвращает
только metadata и признак `can_reveal`, но не secret. Просмотр открытого значения
выполняется отдельным действием с активной `SYSTEM_SETTINGS`, имеет заголовки
`Cache-Control: no-store`/`Pragma: no-cache` и записывается в аудит без самого
секрета. Передавайте значение конкретному преподавателю по защищённому каналу.

Администратор может заменить значение существующей записи на новый токен ровно
из восьми ASCII-букв и цифр. Старое значение после успешной замены сразу перестаёт
проходить проверку; существующая one-to-one привязка записи к преподавателю при
этом сохраняется. Восьмизначные URL-safe токены старого формата (включая `-` и
`_`) и legacy-значения вида `edut_<public_id>_<secret>` остаются валидными до
замены или удаления. У созданных до появления шифрованного столбца hash-only
записей открытое значение восстановить нельзя: UI показывает их как недоступные
для раскрытия, но вход с известным исходным токеном продолжает работать. Замена
такой записи создаёт новый Argon2id hash и восстанавливаемый AES-GCM ciphertext.

AES-GCM ключ преподавательских токенов выводится с разделением контекста из
`APP_SECRET_KEY`. Поэтому при восстановлении базы необходимо вернуть то же
значение `APP_SECRET_KEY`; иначе ранее зашифрованные токены нельзя будет раскрыть
(их Argon2id-проверка при этом остаётся независимой).

Moodle остаётся доказательством identity и enrollment, но не выдаёт роль
`TEACHER`. При первом успешном Moodle-входе с индивидуальным токеном backend
создаёт строгую one-to-one привязку token ↔ LMS principal. Ошибка Moodle login
ничего не привязывает, один token нельзя передать второй учётной записи, а одна
учётная запись не может занять второй token. После успешной привязки повторные
входы выполняются без token и сохраняют роль. Эта роль действует только в
пересечении глобального каталога и Moodle dashboard пользователя и не открывает
чужой курс.

Удаление записи token — операция отзыва, а не косметическая очистка: grant
удаляется, связанные активные `TEACHER` memberships немедленно переводятся в
`STUDENT`. Для возврата полномочий выпустите новый token и попросите пользователя
войти с ним через Moodle. Не используйте общий token кафедры или группы: строгая
привязка и аудит рассчитаны на индивидуальную выдачу.

## 4. Сборка и запуск на Linux

Для целевого NUC используйте launcher и systemd-процедуру ниже: она создаёт
external PostgreSQL volume, подставляет сохранённые секреты, выполняет миграции
до старта приложения и проверяет readiness. Следующий raw Compose-вариант нужен
только для отдельной установки с подготовленным `.env`; порт `8080` здесь —
default этой установки, а не порт NUC:

```bash
docker volume inspect eduprog_postgres-data >/dev/null 2>&1 || \
  docker volume create eduprog_postgres-data
docker compose build
docker compose up -d postgres
docker compose run --rm --no-deps backend alembic upgrade head
docker compose up -d
docker compose ps
```

Alembic применяет только committed revisions из `backend/app/db/migrations/`. Перед production-обновлением запускайте `alembic upgrade head` как отдельный контролируемый шаг; не запускайте schema generation по ORM metadata. Backend обслуживается Uvicorn и не создаёт демо-данные либо пользователей автоматически.

Оставляйте `BACKEND_AUTO_MIGRATE=false` в production. Entry point запускает
Alembic только при `AUTO_MIGRATE=true`; основной Compose передаёт ему значение
`BACKEND_AUTO_MIGRATE`, а workers не auto-migrate. Local override намеренно
включает auto-migrate для smoke-профиля с одним backend.

Эквивалентный процессный контракт backend без Compose:

```bash
cd backend
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Флаг `--reload` допустим только при локальной разработке. Scheduler и sync worker запускаются отдельными командами из раздела 1 и не заменяются FastAPI lifespan hooks или in-process background tasks.

Проверить состояние схемы можно без её изменения:

```bash
docker compose exec backend alembic current
docker compose exec backend alembic heads
```

`alembic downgrade` не является универсальным rollback: используйте его только для заранее проверенной обратимой revision. Для необратимых data migrations применяется roll-forward из backup/новой revision.

Проверки после запуска:

```bash
curl --fail --show-error http://127.0.0.1:8080/healthz
curl --fail --show-error -H 'X-Forwarded-Proto: https' \
  http://127.0.0.1:8080/api/v1/system/health
docker compose exec -T runner python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8081/health/ready')))"
docker compose exec -T moodle-browser python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8083/health/ready')))"
docker compose logs --tail=100 backend moodle-browser deadline-worker sync-worker runner
```

Runner readiness должна вернуть `ready: true`, `executor: local` и
`policy: UNRESTRICTED_CONTAINER`. Это выбранный временный режим без per-job
sandbox; предупреждение в readiness является ожидаемым.
`moodle-browser` readiness должна вернуть `"ready": true` и
`"browser": "connected"`.

### Запуск через systemd на ASUS NUC

В корне репозитория находится `run_eduprog.sh`. Скрипт принимает существующие
переменные старого развёртывания:

- `DBLOGIN` → `POSTGRES_USER`;
- `DBPASSWORD` → `POSTGRES_PASSWORD`;
- `OPENROUTER_API_KEY` → `AI_API_KEY`;
- `OPENROUTER_MODEL` → `AI_MODEL`.

При наличии ключа OpenRouter скрипт также задаёт
`AI_BASE_URL=https://openrouter.ai/api/v1` и
`AI_API_STYLE=chat_completions`. Пароль PostgreSQL URL-кодируется перед созданием
`DATABASE_URL`, поэтому его специальные символы не попадают в URI как
разделители.

`EnvironmentFile=/home/alexey/cpp_markup.env` в systemd экспортирует эти
переменные процессу launcher автоматически. При ручном запуске launcher сам
читает разрешённые DB/AI/volume aliases из `~/cpp_markup.env` и передаёт файл
Docker Compose как дополнительный `--env-file` с приоритетом над runtime-файлом,
не исполняя его через `source` или
`eval`. Другой путь можно указать в `EDUPROG_HOST_ENV_FILE`, например:

```bash
EDUPROG_HOST_ENV_FILE=/home/alexey/cpp_markup.env ./run_eduprog.sh diagnose-ai
```

На целевом NUC в `/home/alexey/cpp_markup.env` должны быть заданы именно эти
host-level параметры (systemd unit читает их из `EnvironmentFile=`):

```dotenv
PUBLIC_BIND_ADDRESS=127.0.0.1
PUBLIC_PORT=5173
```

Порт `8080` внешнего VPS в этот файл не переносится. Он настраивается на VPS как
внешний listener, направленный на NUC `127.0.0.1:5173`.

`diagnose-ai` показывает effective base URL, model, API style и только факт
наличия ключа — само значение ключа команда никогда не печатает. Если backend
уже работает, выводятся также фактические значения внутри его контейнера.

PostgreSQL новой установки запускается в Compose и использует volume
`eduprog_postgres-data`. При обновлении ранее запущенного проекта launcher
сначала выбирает уже существующий `eduprog_postgres-data`. Только если нового
volume ещё нет, он останавливает Compose project `contour` и подключает
существующий `contour_postgres-data` к новым контейнерам `eduprog-*`. Это
сохраняет уже созданную базу приложения и не запускает два PostgreSQL над одним
каталогом. Старый volume не переименовывается автоматически; не выполняйте
`docker compose down -v` во время миграции.

При первом старте скрипт создаёт с правами `0600` файл
`~/.config/eduprog/runtime.env` и один раз генерирует в нём секреты приложения,
runner, Moodle browser HMAC и псевдонимизации. Там же находятся консервативные
начальные лимиты для N150/16 GiB: два backend worker, до двух параллельных job,
3 GiB runner memory, один Chromium, одна одновременная Moodle browser operation,
1 GiB browser memory и 512 MiB shared memory. Сюда же записываются отдельные
`MOODLE_CREDENTIAL_ENCRYPTION_KEY` и `MOODLE_BROWSER_SHARED_SECRET`; при
обновлении старого runtime-файла launcher добавляет недостающие значения один
раз. Если существует `~/.config/contour/runtime.env`, а новый файл ещё не
создан, launcher копирует его целиком с mode `0600`: ключ шифрования Moodle и
сессии при переименовании не меняются. Менять секреты при обычном перезапуске
нельзя.

Для ручной первой проверки:

```bash
cd /home/alexey/programming/edu_programming_course
./run_eduprog.sh start
./run_eduprog.sh status
./run_eduprog.sh logs
```

Файл `deploy/systemd/eduprog.service` рассчитан на тот же путь и пользователя.
Не запускайте его одновременно с прежним `cpp_markup.service`: оба unit управляют
одним приложением и одним публичным портом. Для новой установки, где старого
unit нет:

```bash
sudo cp deploy/systemd/eduprog.service /etc/systemd/system/eduprog.service
sudo systemctl daemon-reload
sudo systemctl enable --now eduprog.service
systemctl status eduprog.service
journalctl -u eduprog.service -f
```

Для переименования действующего NUC unit используйте отдельную процедуру из
раздела «Пересборка после копирования изменений»: сначала остановите и отключите
старый unit, затем включите `eduprog.service`.

`serve` оставляет Docker Compose в foreground, поэтому логи контейнеров попадают
в journal. `ExecStop` останавливает контейнеры, но сохраняет PostgreSQL volume.
Пользователь `alexey` должен иметь доступ к Docker daemon, например через группу
`docker`; это проверяется скриптом до сборки.

Unit вызывает launcher явно через `/bin/bash`, поэтому executable-бит файла
не является обязательным для systemd. Ошибка `status=203/EXEC` означает, что
systemd не смог исполнить команду до запуска Docker. Проверьте существование и
читаемость обоих файлов, а также Bash:

```bash
ls -l /bin/bash /home/alexey/programming/edu_programming_course/run_eduprog.sh
/bin/bash -n /home/alexey/programming/edu_programming_course/run_eduprog.sh
sudo -u alexey /bin/bash \
  /home/alexey/programming/edu_programming_course/run_eduprog.sh help
```

После первого запуска можно интерактивно получить verifier административного
токена:

```bash
./run_eduprog.sh admin-token-hash
```

Запишите напечатанное значение как `ADMIN_TOKEN_HASH='...'` в
`/home/alexey/cpp_markup.env` или runtime-файл, оставив `ADMIN_TOKEN` пустым.
Исходный токен команда не сохраняет: его нужно записать в менеджер секретов.

Если имя unit пока нужно сохранить для внешнего мониторинга, можно заменить
содержимое существующего `/etc/systemd/system/cpp_markup.service`. Сначала
остановите старый процесс и сделайте резервную копию:

```bash
sudo systemctl stop cpp_markup.service
sudo cp /etc/systemd/system/cpp_markup.service /etc/systemd/system/cpp_markup.service.old
sudo cp deploy/systemd/eduprog.service /etc/systemd/system/cpp_markup.service
sudo systemctl daemon-reload
sudo systemctl restart cpp_markup.service
```

Перед публичным доступом дополните `cpp_markup.env` либо
`~/.config/eduprog/runtime.env` значениями `PUBLIC_BASE_URL`, `FRONTEND_URL`,
`ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, exact `MOODLE_BASE_URL` и production
cookie/TLS параметрами из раздела 3. В принятом Playwright flow пароль и
персональный Moodle token в env не задаются: backend хранит только encrypted
browser state. Переменные процесса из `EnvironmentFile` имеют приоритет над
runtime-файлом Compose.

## 5. Локальный smoke-профиль

Для быстрой проверки UI/API без реальной компиляции используйте локальный
mock-профиль:

```bash
cp .env.example .env
docker compose -f compose.yml -f compose.local.yml up --build -d
docker compose -f compose.yml -f compose.local.yml exec backend python -m app.cli seed-demo
```

Откройте `http://localhost:8080` и войдите dev-кнопкой. Для проверки настроек локальный административный токен равен `ADMIN_TOKEN` из `.env` либо `local-admin-token`, если переменная пуста. `RUNNER_MOCK_ENABLED=true`, поэтому результат компиляции демонстрационный. Mock также намеренно не создаёт official hidden-test evidence: соответствующий POST вернёт `503 EVIDENCE_REQUIRES_REAL_RUNNER`.

Runner и на локальной машине, и в основном Compose использует
`RUNNER_EXECUTOR=local`: компилятор и программа являются обычными subprocess
внутри отдельного runner-контейнера. Ответ честно помечается
`filesystem_isolated=false`, `network=host`.

## 6. Подключение Moodle

### 6.1. Playwright pluginless default без прав администратора Moodle

Основной режим использует отдельный контейнер `moodle-browser`, а не требует
Moodle Mobile service или установки plugin. Backend передаёт login/password по
HMAC-аутентифицированному внутреннему каналу. Worker открывает штатную форму
`https://edu.mmcs.sfedu.ru/login/index.php`, проверяет авторизованный identity и
возвращает очищенный Playwright `storage_state`. Пароль после login request не
сохраняется и не журналируется.

Browser state хранится в PostgreSQL только как AES-GCM ciphertext kind
`BROWSER_STATE_V1` под постоянным `MOODLE_CREDENTIAL_ENCRYPTION_KEY`. Он
привязан к connection/principal и не возвращается frontend. Revision и короткий
DB lease не позволяют конкурентной операции затереть более новый state. На
следующей операции worker создаёт новый incognito context с этим state,
обновляет state и закрывает context. Постоянного browser process/context на
каждого пользователя нет: один контейнер держит один Chromium для всех
ограниченных операций.

Backend и worker должны одновременно получать одинаковый независимый
`MOODLE_BROWSER_SHARED_SECRET`. `MOODLE_SERVICE_TOKEN`,
`MOODLE_LAUNCH_SHARED_SECRET` и route
`/local/programming_bridge/launch.php` для этого режима не нужны.

После readiness создайте или обновите connection:

```bash
cd /home/alexey/programming/edu_programming_course
./run_eduprog.sh bootstrap-moodle \
  https://edu.mmcs.sfedu.ru \
  'Мехмат Moodle'
```

Команда идемпотентно задаёт `auth_mode=PLUGINLESS` и новый default
`pluginless_transport=PLAYWRIGHT`. Она не проверяет пользовательские credentials;
реальная проверка выполняется на странице входа. Legacy connection без явного
transport marker сохраняет прежнее поведение ради безопасной миграции; повторный
bootstrap явно переводит выбранную connection на Playwright.

Проверьте контейнер и его Chromium без публикации внутреннего порта:

```bash
./run_eduprog.sh status
sudo docker exec eduprog-moodle-browser-1 python -c \
  "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8083/health/ready', timeout=3)))"
sudo docker logs --tail=200 eduprog-moodle-browser-1
sudo docker logs --tail=200 eduprog-backend-1
```

Readiness должна содержать `"ready": true` и `"browser": "connected"`.

`bootstrap-moodle` создаёт только connector, но не импортирует пользовательские
курсы. После входа с полем «Токен администратора» откройте «Настройки» →
«Курсы системы», вставьте URL вида
`https://edu.mmcs.sfedu.ru/course/view.php?id=549` и подтвердите добавление.
Курс становится глобально разрешённым. При следующих входах Playwright
пересекает предварительно добавленные course IDs с dashboard вошедшего
пользователя; полный dashboard Moodle не переносится в систему. Это пересечение
подтверждает enrollment. Эффективная роль вычисляется отдельно: активная
индивидуальная teacher-token grant даёт `TEACHER`, её отсутствие — `STUDENT`,
независимо от HTML role hints Moodle.

Миграция `20260825_0009` включает в новый каталог ранее явно подтверждённые
course-import jobs. Курсы, которые старый login создал неявно из dashboard,
остаются выключенными, а их memberships деактивируются без удаления истории.
Если нужный старый курс исчез после обновления, добавьте его URL через системные
настройки; повторный discovery безопасно переиспользует локальную запись.

Следующая миграция `20260825_0010` создаёт пул токенов и grants и деактивирует
все прежние активные `TEACHER` memberships. Это намеренная fail-closed граница:
после rollout администратор сначала выпускает персональные токены, затем каждый
преподаватель один раз входит с Moodle credentials и своим token. Студенческие
memberships, работы и история не удаляются.

Миграция `20260904_0018` добавляет nullable encrypted secret без изменения
Argon2id verifier и grant. Она не может восстановить прежнее открытое значение,
поэтому существующие записи после upgrade имеют `can_reveal=false`, но остаются
валидными. Для раскрытия такой записи администратор явно задаёт новое значение;
удалять токен или повторно привязывать преподавателя не требуется.

Проверьте наличие secrets, не печатая их значения:

```bash
grep -q '^MOODLE_CREDENTIAL_ENCRYPTION_KEY=..' \
  /home/alexey/.config/eduprog/runtime.env && echo 'credential key configured'
grep -q '^MOODLE_BROWSER_SHARED_SECRET=..' \
  /home/alexey/.config/eduprog/runtime.env && echo 'browser HMAC configured'
stat -c '%a %U:%G %n' /home/alexey/.config/eduprog/runtime.env
```

Ожидается mode `600`. Не выводите secrets, cookies или `storage_state` в journal,
тикет либо чат. Encryption key резервируется отдельно от DB dump и
восстанавливается вместе с БД. При его потере browser states fail closed, и всем
пользователям потребуется повторный вход.

При `APP_DEBUG=false` credential endpoint по умолчанию требует внешнюю схему
HTTPS. Для временного закрытого стенда без TLS существует явный флаг:

```dotenv
MOODLE_CREDENTIAL_LOGIN_ALLOW_INSECURE_HTTP=true
SESSION_COOKIE_SECURE=false
SECURE_SSL_REDIRECT=false
SECURE_HSTS_SECONDS=0
```

Он ослабляет участок browser пользователя → приложение; Moodle origin всё равно
остаётся HTTPS, но Moodle password на внешнем HTTP-участке можно перехватить.
Используйте режим только временно в доверенной сети и выключите его сразу после
настройки TLS.

### 6.2. Текущие возможности и границы

Playwright login и course discovery реализованы: parser читает sections,
activities, numeric `cmid`, ограниченный roster/groups и подтверждает доступ к
курсу по самой Moodle. Глобальный статус преподавателя подтверждает только
индивидуальная token binding. Это ещё нужно проверить после пересборки
контейнера на NUC; успешные unit/integration tests не заменяют live staging
login.

Авторизованный аудит курса 549 отдельно подтвердил, что текущая
«Самостоятельная работа №1» (`mod_quiz`, cmid `30354`) является Essay с file
attachment и без online text editor. Загрузка `.cpp` до страницы summary
технически возможна. Backend и browser-worker реализуют два доказанных режима
асинхронной доставки ответа Quiz Essay: `ESSAY_ATTACHMENT` передаёт полный
однофайловый UTF-8 source либо детерминированный ZIP исходников, а
`ESSAY_ONLINE_TEXT` записывает полный однофайловый UTF-8 source в однозначный
text control. Decoded artifact ограничен 4 MiB; с учётом base64 и JSON
HMAC-защищённый внутренний request ограничен 6 MiB.

Для стандартного Assignment реализованы `ASSIGN_FILE` и
`ASSIGN_ONLINE_TEXT`. Browser-worker выбирает их по фактической student
submission form перед каждой mutation; если форма даёт file и online
text, выбирается `ASSIGN_FILE`. Типизированный contract и fixtures не
заменяют live staging save/finalize/read-back на целевой MMCS Moodle.

Общий file artifact строится по фактическому числу файлов. Ровно один
source передаётся как `main.c`/`main.cpp`. Любой второй файл — в том
числе `main.cpp` + `input.txt` — переводит ответ в детерминированный
`submission.zip`. В него входят `.c/.cc/.cpp/.cxx`,
`.h/.hh/.hpp/.hxx`, `.inc` и `.txt` с точными safe relative paths.
Runtime-generated files в архив не входят.

Операция Quiz fail-closed требует в фактической попытке ровно один вопрос Essay и
ровно один доказанный transport: однозначный file manager для
`ESSAY_ATTACHMENT` либо однозначный text control для `ESSAY_ONLINE_TEXT`.
Произвольный question slot, teacher preview, неоднозначная или изменившаяся
разметка не принимаются. Assignment аналогично fail-closed отклоняет
неоднозначную/изменившуюся форму, неподдерживаемую team policy и нарушение
file count/size/type. Перед overwrite того же managed filename browser-worker
скачивает ровно один текущий same-origin attachment и сравнивает SHA-256 с
durable receipt; отсутствие/неоднозначность ссылки, ошибка чтения или ручная
замена файла блокируют операцию до открытия upload picker. Наследуемый Moodle
`maxbytes=0` также обязан разрешиться в конкретный effective limit. `PERIODIC` и
`FINAL_MINUTE` сохраняют только draft и завершаются статусом `DRAFT_SAVED`.
`SUBMISSION` и `DEADLINE` проходят summary/confirmation и возвращают
`FINALIZED`. Локальный snapshot остаётся authoritative: Moodle outage не
отменяет принятую сервером работу, а outbox хранит idempotency key, lease и
выполняет bounded retry. Compiler output, IDE и edit history в Moodle не
передаются.

Это описание реализованного контракта, а не live production validation. До
пилота нужны staging-проверки загрузки, повторов, refresh browser state,
истечения Moodle session и общей очереди около дедлайна. Idempotency cache
`moodle-browser` ограничен памятью одного процесса; ambiguous final response
после restart не следует считать доказанным exactly-once без отдельного
staging-теста политики попыток Moodle.

Grade/comment write через browser transport реализован для exact mapped
Assignment и отдельного вопроса Quiz Essay: browser-worker открывает конкретную
штатную форму, проверяет внешний идентификатор и шкалу, записывает балл и
комментарий и fail-closed отклоняет отсутствующую, неоднозначную или изменившуюся
разметку. Наличие реализации и mapping ещё не является production-доказательством:
до пилота обязателен live staging write/read-back для обоих типов, включая
повтор, mismatch шкалы, истёкшую сессию и проверку фактического результата в
Moodle.

### 6.3. Optional bridge 0.3

Следующие шаги нужны только если администратор Moodle позднее разрешит
расширенный режим:

1. Скопируйте `moodle/local/programming_bridge` в `<moodle-root>/local/programming_bridge` и до upgrade проверьте фактический `$plugin->requires` в `version.php`; compatibility target — 4.2.2+, рекомендуемая рабочая ветка — 4.5 LTS или новее.
2. Выполните штатное обновление Moodle и настройте exact HTTPS URL платформы и общий `MOODLE_LAUNCH_SHARED_SECRET`.
3. Включите predefined service `programming_bridge`, создайте restricted web-service user/token и выдайте ему доступ только к синхронизируемым курсам.
4. Запишите token в `MOODLE_SERVICE_TOKEN` backend, не в БД.
5. Создайте bridge connection через отдельную launcher-команду:

```bash
./run_eduprog.sh start
./run_eduprog.sh bootstrap-moodle-bridge \
  https://edu.mmcs.sfedu.ru \
  'Мехмат Moodle bridge'
./run_eduprog.sh status
```

CLI-команда идемпотентна. Delegated bridge login заработает только после
установки plugin, настройки exact public HTTPS platform URL и одинакового
`MOODLE_LAUNCH_SHARED_SECRET` с обеих сторон. Не переводите действующий
pluginless connection в `BRIDGE`, пока plugin и service token не проверены на
staging: mode определяет login и sync contract всей записи.

Enabled `LMSConnection` records остаются основным allow-list для course URL.
Если в system settings задан непустой `allowed_lms_origins`, он дополнительно
сужает этот набор; пустой список не разрешает новые origins и не заменяет
bootstrap connection.

Подробности optional capability приведены в
`moodle/local/programming_bridge/README.md`.
Release 0.3 предоставляет course/roster/activity snapshots, `mod_assign`
grade/comment, полный checkpoint с внешним `event_chain_head` anchor и
connector-owned task-version mirror. Для restricted service user нужны
`moodle/course:viewparticipants`, `local/programming_bridge:use`,
`local/programming_bridge:pushgrade`,
`local/programming_bridge:storecheckpoint` и
`local/programming_bridge:synctaskbank` только в синхронизируемых курсах.

После course sync преподаватель выбирает обнаруженный Moodle Assignment в форме
работы. Сохраняется явный `Assessment -> mod_assign` mapping; `cutoffdate` имеет
приоритет над `duedate`, а `allowsubmissionsfromdate` становится временем
открытия. Если activity исчезла, mapping получает `MISSING_IN_MOODLE`, но
последние сроки не стираются. Plugin не создаёт Moodle activity и не изменяет
native Quiz/question bank.

Для grade mapping Moodle должен вернуть положительный numeric `grade_max`, в
точности равный `Assessment.max_score`. При отсутствии/нечисловой шкале sync
выставляет `UNSUPPORTED_MOODLE_GRADING`, при несовпадении —
`GRADE_RANGE_MISMATCH`; grade outbox блокируется до исправления. Автоматического
масштабирования балла нет.

Checkpoint и task definition сериализуются канонически Python backend. Plugin
валидирует JSON и SHA-256 именно принятых UTF-8 байтов и не пересериализует их в
PHP. Это важно для воспроизводимости hash между runtime.

### Optional bridge: ошибка `Not Found` на `launch.php`

Переход браузера на Moodle с корректным `state`, после которого Apache отвечает
`404 Not Found`, означает, что запрос дошёл до Moodle, но bridge не установлен в
этой Moodle. Это не ошибка frontend, backend или проброса порта NUC. Обычный
pluginless login вообще не открывает этот route: если он всё ещё открывается,
проверьте, что connection переведён в `auth_mode=PLUGINLESS`. Команда
`bootstrap-moodle` создаёт только локальную запись подключения и не может
установить plugin на внешний сервер.

Проверить наличие маршрута можно без передачи секретов:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  https://edu.mmcs.sfedu.ru/local/programming_bridge/launch.php
```

До установки ожидается `404`; после установки запрос без активной Moodle-сессии
обычно перенаправляется на страницу входа, то есть возвращает `3xx`, а не `404`.
Администратор Moodle должен скопировать plugin, выполнить Moodle upgrade,
очистить caches и настроить в plugin:

- exact публичный HTTPS origin платформы, включая нестандартный порт, но без
  path/query/fragment;
- secret не короче 32 байт, в точности совпадающий с
  `MOODLE_LAUNCH_SHARED_SECRET` backend.

Playwright является явным transport mode connection, а не молчаливым fallback.
Browser-per-user и persistent contexts не используются: один Chromium создаёт
короткоживущий incognito context на операцию, а между операциями хранится только
зашифрованный state. Сначала проверяйте на тестовом курсе login, roster/group
reconciliation, activity/deadline projection и parser после каждого изменения
Moodle theme. Следите за логами `moodle-browser`, backend и `sync-worker`: Quiz
Essay/Assignment answer mutation и grade/comment write через точные штатные формы
Assignment/Quiz Essay реализованы, но должны пройти live staging write/read-back.
Постоянные ошибки и fail-closed отказы не должны оставаться незамеченными.

Исторический аудит видел косвенный build-признак `2023042402.01` (ветка
4.2.2), но 24 августа 2026 года повторный запрос root URL завершился timeout, а
доступный guest crawler cache курса не раскрывал версию. Не используйте 4.2.2
как current deployment fact. До установки получите фактический Moodle
`version.php` и staging clone у администратора. По [официальному lifecycle
Moodle](https://moodledev.io/general/releases) ветка 4.2 уже снята с security
support; target должна быть поддерживаемой веткой, совместимость с которой
проверена отдельно. `$plugin->requires=2023042402` — только floor самого plugin,
а не доказательство версии университетского сайта.

### Пересборка после копирования изменений на NUC

Rollout всех committed-миграций до `20260904_0018` проводите в коротком окне
обслуживания. В частности, миграция `20260825_0010` выключает прежние `TEACHER`
memberships, а новые grants появятся только после индивидуальных входов. До
копирования изменений сделайте проверенный
backup PostgreSQL и отдельно сохраните `/home/alexey/cpp_markup.env` и
`/home/alexey/.config/eduprog/runtime.env`. Не выполняйте `docker compose down -v`.

Если checkout переносится с рабочей машины без Git remote, находясь в корне
проверенной версии, скопируйте его на NUC так (подставьте адрес NUC):

```bash
rsync -a \
  --exclude '.git/' \
  --exclude '.env' \
  --exclude '.venv/' \
  --exclude 'node_modules/' \
  --exclude '__pycache__/' \
  --exclude '_old_cpp_course_automatization/' \
  ./ alexey@<NUC_HOST>:/home/alexey/programming/edu_programming_course/
```

Production-секреты при этом не копируются из checkout: launcher и unit читают
их из двух сохранённых файлов вне репозитория. После копирования убедитесь, что
на NUC присутствуют новые `compose.yml`, `run_eduprog.sh`, каталоги backend,
frontend, runner и `moodle_browser`.

Если NUC уже работает под `eduprog.service`, скопируйте новый репозиторий и
выполните ровно следующие команды. `serve` пересоберёт изменившиеся Docker layers,
применит committed Alembic migrations и дождётся readiness:

```bash
cd /home/alexey/programming/edu_programming_course
sudo systemctl restart eduprog.service
sudo systemctl status eduprog.service --no-pager
sudo journalctl -eu eduprog.service --since '10 minutes ago' --no-pager
./run_eduprog.sh status
sudo docker exec eduprog-backend-1 python -m alembic current
curl --fail --show-error http://127.0.0.1:5173/healthz
curl --fail --show-error -H 'X-Forwarded-Proto: https' \
  http://127.0.0.1:5173/api/v1/system/health
curl --fail --show-error -H 'X-Forwarded-Proto: https' \
  http://127.0.0.1:5173/api/v1/system/readiness
```

Ручной `npm install` на NUC не требуется. `serve` вызывает команду
`docker compose build`; frontend Dockerfile выполняет `npm ci` по committed
`frontend/package-lock.json`, а `frontend/.dockerignore` исключает случайно
скопированный host `node_modules`. При неизменном lock-файле Docker вправе
использовать dependency layer cache, но изменение frontend sources всё равно
перезапускает `npm run build`.

В выводе `alembic current` ожидается `20260826_0012 (head)`. Затем завершите
прикладную часть rollout в таком порядке:

1. Войдите через Moodle с административным token. После `0010` профиль может
   временно отображаться как `STUDENT`; capability `SYSTEM_SETTINGS` от этого не
   зависит.
2. В «Настройки» → «Курсы системы» убедитесь, что нужные Moodle course URLs
   включены в глобальный каталог. Один teacher token не открывает курс вне этого
   списка или вне dashboard пользователя.
3. В разделе преподавательских токенов создайте отдельную запись с понятной
   меткой для каждого преподавателя. Передайте показанный открытый token по
   защищённому каналу. Для новой записи администратор сможет позднее запросить
   явное раскрытие; обычный список открытые значения не показывает.
4. Преподаватель выходит из старой сессии и один раз входит с Moodle
   login/password и своим token. Только успешная Moodle-аутентификация создаёт
   one-to-one grant.
5. Проверьте список курсов и роль, затем выполните ещё один обычный вход без
   teacher token: сохранённая привязка должна восстановить `TEACHER`.
6. При необходимости замените значение через редактирование записи. Замена
   инвалидирует прежний secret, но сохраняет ID и привязку преподавателя.
7. При отзыве удалите соответствующую запись в настройках. DELETE немедленно
   демотирует memberships этого пользователя до `STUDENT`; для повторной выдачи
   нужен новый token и новый успешный Moodle-вход.

Если unit всё ещё называется `cpp_markup.service`, его также можно один раз
перезапустить: launcher уже использует Compose project и контейнеры с префиксом
`eduprog`, безопасно останавливает прежний project `contour` и повторно использует
его PostgreSQL volume, если новый volume ещё не существует.

```bash
cd /home/alexey/programming/edu_programming_course
sudo systemctl restart cpp_markup.service
sudo systemctl status cpp_markup.service --no-pager
sudo journalctl -eu cpp_markup.service --since '5 minutes ago' --no-pager
```

Чтобы переименовать и сам systemd unit без параллельного запуска двух стеков:

```bash
sudo systemctl stop cpp_markup.service
sudo systemctl disable cpp_markup.service
sudo cp deploy/systemd/eduprog.service /etc/systemd/system/eduprog.service
sudo systemctl daemon-reload
sudo systemctl enable --now eduprog.service
sudo systemctl status eduprog.service --no-pager
```

Новый launcher скопирует runtime-конфигурацию, остановит оставшиеся контейнеры
project `contour` и подключит существующий PostgreSQL volume. Старый unit-файл и
старые Docker images можно удалить только после проверки входа и данных; volume
удалять нельзя.

Если нужно заодно обновить base images, сначала остановите unit,
запустите launcher update, затем верните foreground-владельца systemd:

```bash
cd /home/alexey/programming/edu_programming_course
sudo systemctl stop eduprog.service
./run_eduprog.sh update
sudo systemctl start eduprog.service
sudo journalctl -eu eduprog.service -f
```

Если unit ещё не переименован, замените в этом блоке `eduprog.service` на
`cpp_markup.service`; не запускайте оба unit одновременно.

Миграция `20260825_0008` автоматически материализует
`pluginless_transport=PLAYWRIGHT` у старых pluginless-подключений Moodle без
явно выбранного transport. Поэтому после обычного обновления вход уже не
попытается использовать отключённый Mobile Web Service. Для явной проверки
настройки или после изменения Moodle origin можно идемпотентно выполнить
bootstrap после readiness:

```bash
./run_eduprog.sh bootstrap-moodle https://edu.mmcs.sfedu.ru 'Мехмат Moodle'
```

На NUC `./run_eduprog.sh status` является корректным эквивалентом
`docker compose ps`: launcher подставляет legacy `DBLOGIN`/`DBPASSWORD` и
persistent runtime secrets. После bootstrap проверьте контейнеры, Chromium и
логи:

```bash
./run_eduprog.sh status
sudo docker exec eduprog-moodle-browser-1 python -c \
  "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8083/health/ready', timeout=3)))"
sudo docker logs --tail=200 eduprog-moodle-browser-1
sudo docker logs --tail=200 eduprog-backend-1
```

В обычном deployment с полным `.env` те же проверки выполняются через Compose:

```bash
docker compose ps
docker compose exec -T moodle-browser python -c \
  "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8083/health/ready', timeout=3)))"
docker compose logs --tail=200 moodle-browser backend
```

Ожидаются контейнер `moodle-browser` в состоянии healthy и JSON с
`"ready": true`, `"browser": "connected"`. Если контейнер отсутствует, на NUC
ещё работает старая Compose-сборка; убедитесь, что скопированы `compose.yml` и
директория `moodle_browser/`, затем снова перезапустите systemd unit.

После появления healthy-сервисов проверьте тот локальный порт, который задан в
`PUBLIC_PORT`. В текущем NUC-развёртывании приложение привязано к
`127.0.0.1:5173`, а внешний VPS публикует отдельный порт `8080` и направляет его
в этот локальный endpoint. Это две стороны proxy/tunnel, поэтому номера не
должны совпадать:

```bash
curl --fail --show-error http://127.0.0.1:5173/healthz
curl --fail --show-error -H 'X-Forwarded-Proto: https' \
  http://127.0.0.1:5173/api/v1/system/health
curl --fail --show-error -H 'X-Forwarded-Proto: https' \
  http://127.0.0.1:5173/api/v1/auth/connections
```

Последний ответ должен содержать `"login_mode":"CREDENTIALS"`; после этого
сделайте hard refresh страницы входа. Этот публичный ответ не раскрывает
`storage_state` или выбранный внутренний transport.

На самом VPS отдельно проверьте, что его listener `8080` действительно доходит
до NUC (команда выполняется на VPS):

```bash
curl --fail --show-error http://127.0.0.1:8080/healthz
```

Если listener использует TLS или virtual host, выполните эквивалентный запрос к
его реальному публичному URL, например
`curl --fail --show-error https://code.example.edu:8080/healthz`. Успех проверки
NUC на `5173` не заменяет эту отдельную проверку VPS на `8080`.

Production PostgreSQL volume объявлен external и управляется launcher, поэтому
Compose не должен удалять его даже с `down -v`. Всё равно не используйте `-v` в
рабочей среде: локальный smoke override создаёт managed volume и там этот ключ
удаляет данные. Если браузер держит старые статические файлы, выполните жёсткое
обновление страницы (`Ctrl+Shift+R`).

## 7. AI-провайдер

По умолчанию `AI_ENABLED=false`. Включайте AI только после согласования передачи учебных данных:

```dotenv
AI_ENABLED=true
AI_MOCK_ENABLED=false
AI_BASE_URL=https://api.openai.com/v1
AI_API_KEY=...
AI_MODEL=...
AI_API_STYLE=responses
```

API key находится только в backend. Frontend и runner его не получают.

## 8. TLS и reverse proxy

По умолчанию frontend привязан к `127.0.0.1:${PUBLIC_PORT}`: reverse proxy либо
туннель обращается к этому адресу, а прямой сетевой доступ закрыт. Порт,
публикуемый внешним VPS, может отличаться от `PUBLIC_PORT` на NUC. Не ставьте
`PUBLIC_BIND_ADDRESS=0.0.0.0`, если порт не защищён firewall или отдельной
доверенной сетью. Reverse proxy должен:

- завершать TLS и перенаправлять HTTP на HTTPS;
- передавать `Host`, `X-Forwarded-For` и `X-Forwarded-Proto`;
- для optional bridge не удалять и не переписывать browser
  `Origin`/`Referer` на Moodle callback: прямой stateless launch сверяет их
  с exact origin provisioned connection; pluginless login не имеет browser callback;
- не публиковать backend, PostgreSQL и runner напрямую.

Встроенный Nginx сохраняет только точный `X-Forwarded-Proto: http|https`, а
malformed/chained значение заменяет собственной схемой. Поэтому внешний proxy
должен именно заменять этот заголовок, а опубликованный frontend-порт должен
быть доступен только ему. Это также позволяет backend корректно применять
`SECURE_SSL_REDIRECT` и Secure cookie после TLS termination на VPS.

История попытки сохраняет точный адрес из ASGI `request.client` вместе с
ограниченными сведениями о браузере. Backend сам не читает произвольный
`X-Forwarded-For`: адрес сначала должен быть нормализован Uvicorn только от
proxy из `FORWARDED_ALLOW_IPS`. Не выставляйте backend наружу и не используйте
`FORWARDED_ALLOW_IPS=*`, если к его порту может обратиться кто-либо кроме
встроенного Nginx. IP-адрес является персональными данными и должен попадать под
утверждённые правила доступа, срока хранения и удаления учебных записей.

Встроенный Nginx уже proxy-передаёт `/api/` в backend. CSP содержит Moodle SFedU как допустимый `frame-ancestor`; для другого LMS домена измените и пересоберите frontend-конфигурацию до запуска.

## 9. Runner: временный режим без sandbox

По текущему решению продукта компиляция и программа запускаются как обычные
subprocess внутри отдельного Docker-контейнера runner — так же, как в старой
версии, но вынесены из backend. Bubblewrap и предварительная namespace-проверка
не используются. Это убирает зависимость от host AppArmor/user namespaces, но
не делает недоверенный код безопасным.

Сохраняются только внешняя граница контейнера и эксплуатационные ограничения:
read-only root filesystem, отдельный job tmpfs, непривилегированный uid,
dropped capabilities, `no-new-privileges`, container CPU/RAM/PID limits, а также
per-process wall/CPU/address-space/open-files/output/workspace limits. Код может
читать доступные runner-контейнеру файлы и пользоваться его сетью. Не монтируйте
в runner application data, home, БД, Moodle credentials или Docker socket.

Workspace `.txt` перед запуском копируются в тот же private writable cwd,
где лежит executable. Программа может читать/перезаписывать их и
создавать новые text/binary files. Это не persistent-хранилище: изменённые
и generated runtime files не возвращаются в workspace/Moodle и удаляются
вместе с job cwd после one-shot или interactive run.

Проверить режим и лимиты без вывода секретов можно командой:

```bash
./run_eduprog.sh diagnose-runner
```

Она печатает JSON `/health/ready`, фактические container limits и последние
логи runner.
Тот же JSON отдельно можно получить так:

```bash
sudo docker exec eduprog-runner-1 python -c "import http.client; c=http.client.HTTPConnection('127.0.0.1',8081,timeout=3); c.request('GET','/health/ready'); r=c.getresponse(); print(r.status, r.read().decode())"
```

Нормальный результат: `ready: true`, `executor: local`,
`policy: UNRESTRICTED_CONTAINER`; результат запуска содержит
`filesystem_isolated=false` и `network=host`.

Для systemd режим `serve` после успешного detached-start только следует за
логами и не выполняет второй mutating `docker compose up`. Worker-контейнеры
также используют один уже собранный backend image вместо трёх конкурирующих
сборок одного tag.

Backend принимает и сохраняет фактические признаки отсутствия изоляции. Runtime
CPU/memory из системных настроек могут только снижать фиксированные profile
limits. Hard limit core workspace — 64 файла и 512 KiB исходного UTF-8 текста.

## 10. Операционные лимиты baseline

| Область | Значение по умолчанию / hard bound |
| --- | --- |
| Workspace core | 64 файла, 512 KiB source text |
| Runner request / response | 4 MiB / 8 MiB на backend adapter |
| Runner interactive rate | 2 concurrent на пользователя, 30 запусков / 60 с |
| Hidden-test evidence | 8 с wall и 2 с CPU на case; 50 с на report; 1 concurrent report/teacher; 5 reports / 300 с; stale 120 с |
| AI message rate | student 10, teacher 30 сообщений на пользователя/режим за 60 с; суммарно по threads |
| Runner container | 2 jobs, 3 GiB, 2 CPU, 1 GiB job tmpfs |
| Moodle browser | 1 Chromium process / 3 bounded contexts: background crawl, manual sync and reserved login; queue 8 с, login deadline 45 с, storage state 256 KiB, decoded answer artifact 4 MiB, request 6 MiB, container 1 GiB/1.5 CPU, shm 512 MiB |
| Local checkpoint | canonical snapshot authoritative локально; mapped Quiz Essay/Assignment получает online text либо file artifact через retrying outbox; ровно один source → `main.c`/`main.cpp`, любой второй файл → `submission.zip` |
| Checkpoint cadence | `D/10`, затем `D/20`; clamp 30–900 с |
| Final checkpoints | start, manual submit, `<60 с`, deadline |
| LMS retries | 8 попыток, exponential 15–3600 с, lease 300 с; 2 sync-worker lanes (`LMS_SYNC_WORKER_CONCURRENCY`) и одна резервная lane только для финальной сдачи |
| Course reconciliation | каждые 900 с (`LMS_SYNC_COURSE_INTERVAL_SECONDS`) |
| LMS receipt | не более 64 KiB (`LMS_SYNC_RECEIPT_MAX_BYTES`) |
| Moodle browser / AI / authorship HTTP | 300 / 45 / 30 с по `.env.example`; budget чтения settings всех activity — 240 с |

`DEADLINE_POLL_SECONDS` задаёт частоту polling scheduler, а не checkpoint
interval. `LMS_SYNC_WORKER_POLL_SECONDS` задаёт ожидание пустой outbox, а
`LMS_SYNC_WORKER_CONCURRENCY` — число параллельных claim-lanes (по умолчанию 2,
hard bound 8). Student checkpoints выбираются раньше долгих `course.sync`, поэтому
обход курса не должен задерживать финальную сдачу. Меняйте лимиты только после
нагрузочного теста; container CPU/memory не заменяют per-job limits runner.

`LMS_EMBEDDED_TERMINAL_WORKER_ENABLED=true` оставляет в backend одну резервную
lane, которая забирает только финальные `SUBMISSION`/`DEADLINE` checkpoints.
Она не заменяет `sync-worker` для синхронизации курсов и истории, но остановка
отдельного worker-контейнера больше не оставляет студента в бесконечном окне
«Передаём работу в Moodle».

Один процесс Chromium выбран намеренно для N150. Внутри него три изолированных
контекста имеют разные полосы: фон не занимает контекст входа, а ручная
синхронизация не ждёт исторического импорта. Не увеличивайте значение выше `3`
и не запускайте несколько Uvicorn workers до измерения RAM, CPU и общей очереди.
Quiz Essay checkpoint
jobs coalesce/supersede устаревшие revisions; это поведение всё равно нужно
проверить под фактической экзаменационной нагрузкой.

`MOODLE_BROWSER_ACTIVITY_DETAIL_BUDGET_SECONDS` ограничивает суммарное чтение
детальных settings всех activity одного курса. Значение backend
`MOODLE_BROWSER_HTTP_TIMEOUT_SECONDS` должно быть больше этого budget; поставка
использует соответственно 240 и 300 секунд, чтобы курс примерно с 24 работами
не оставался частично неподтверждённым из-за прежнего 30-секундного лимита.

AI rate budget проверяется в БД до provider call. Его параметры:
`AI_STUDENT_RATE_LIMIT_MESSAGES`, `AI_TEACHER_RATE_LIMIT_MESSAGES` и
`AI_RATE_LIMIT_WINDOW_SECONDS`; превышение возвращает `429 AI_RATE_LIMIT`.

Evidence report выполняется синхронно через backend, поэтому
`EVIDENCE_TOTAL_TIMEOUT_SECONDS` имеет hard maximum 60 секунд и должен оставаться
ниже edge `proxy_read_timeout` (в поставляемом Nginx — 330 секунд; запас также
нужен полной синхронизации Moodle).
`EVIDENCE_RUNNING_STALE_SECONDS` должен быть больше total timeout минимум на 15
секунд. `EVIDENCE_CASE_TIMEOUT_SECONDS`, `EVIDENCE_CPU_SECONDS_PER_CASE`,
`EVIDENCE_MAX_CONCURRENT_REPORTS_PER_TEACHER`,
`EVIDENCE_RATE_LIMIT_REPORTS` и `EVIDENCE_RATE_LIMIT_WINDOW_SECONDS` изменяйте
только вместе с runner capacity/load test. Частичный прогресс сохраняется в БД;
после прерывания следующий retry переводит stale report в `FAILED` и может
создать новый. Один submission/snapshot не может иметь два `RUNNING` reports.

## 11. Резервное копирование и обновление

Создание логического backup:

```bash
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > eduprog.dump
```

Проверяйте восстановление на отдельной БД. Исходники и история MVP находятся в PostgreSQL, поэтому потеря volume означает потерю работ.

Для Moodle нужен также site/database backup. Plugin 0.3 не реализует Moodle
`backup2` steps для backup/restore отдельного курса: перенос checkpoint/task
mirror при course copy нельзя предполагать. Privacy provider покрывает
пользовательские checkpoint rows, но это также нужно проверить на staging.

Перед обновлением:

1. сделайте и проверьте backup;
2. сохраните неизменными секреты, `.env`, включая `APP_SECRET_KEY`, и отдельный
   `MOODLE_CREDENTIAL_ENCRYPTION_KEY`; без него browser states в DB не
   восстановятся. `APP_SECRET_KEY` необходим для раскрытия зашифрованных
   преподавательских токенов. Сохраните также `MOODLE_BROWSER_SHARED_SECRET`
   для внутреннего HMAC-канала;
3. выполните `docker compose build --pull`;
4. запустите PostgreSQL и выполните `docker compose run --rm backend alembic upgrade head`;
5. выполните `docker compose up -d`;
6. проверьте `alembic current`, health, вход через Moodle, `moodle-browser`,
   runner и оба фоновых worker.

После обновления уже добавленный Moodle-курс удалять и добавлять заново не
нужно. Войдите как преподаватель и нажмите «Синхронизировать LMS». Подходящие
`assign`/`quiz` из разделов лабораторных, самостоятельных, контрольных и
экзамена появятся в «Курсах и работах» как выключенные связанные проекции.
Отдельный банк заданий скрыт. Преподаватель выбирает одну или несколько своих
Moodle-групп и включает проекцию; обладатель `SYSTEM_SETTINGS` может выбрать
любую группу курса. Название, условие, сроки, длительность попытки, балл и число
попыток не вводятся локально и не угадываются: публикация и прямой старт
fail-closed, пока Playwright не подтвердил каждую настройку Moodle. Явно
выключенное Moodle ограничение считается подтверждённым отсутствием лимита.

Это правило применяется и к legacy assessment со статусом `PUBLISHED`: после
обновления он не виден и не запускается до успешной полной синхронизации и
назначения группы. Отдельной schema migration для provenance и historical v5
нет — маркеры хранятся в существующем JSON mapping/policy, но штатный
`alembic upgrade head` всё равно обязателен для остальных миграций релиза.

При первом успешном history sync после обновления legacy source mapping без
маркера v5 перечитывается один раз даже при неизменном `external_revision`.
Создаётся новый immutable import snapshot с сохранёнными табами/пробелами;
для Assignment online text и attachments сохраняются одновременно, а не
заменяют друг друга. Safe ZIP и 7z распаковываются с сохранением relative
paths; absolute/path traversal, symlink, недопустимые суффиксы и превышение
лимитов блокируют извлечение.
После записи маркера дальнейшие sync снова идемпотентны. Поэтому после rollout
дождитесь завершения `sync-worker` и выборочно проверьте форматирование
исторического Assignment/Quiz Essay, сосуществование online text и
attachments и точность путей из тестового ZIP.

Повторная синхронизация не создаёт дубликаты. Разделы с неоднозначным назначением
(`АРХИВ`, общие тесты, индивидуальные задания, добор баллов) автоматически не
импортируются. Нативный Moodle question bank не копируется. Для связанных Quiz
с Essay и стандартных Assignment после LMS sync создаются фоновые bounded jobs:
они постранично и идемпотентно импортируют финальный код/файлы, доступные оценки
и комментарии как read-only локальные сдачи. История набора, clipboard
provenance и промежуточные snapshots отсутствуют в Moodle и не
восстанавливаются; UI явно помечает такой импорт. До pilot нужно проверить
парсер фактической MMCS Assignment grading/review разметки.
Quiz с несколькими Essay разбивается по стабильному question slot: каждый Essay
получает собственную сдачу, оценку и комментарий, но одна Moodle attempt
показывается в очереди одной работой с переключателем заданий внутри проверки.
Несколько файлов одного Essay остаются одним многофайловым решением; ZIP и 7z
распаковываются с bounded-проверками путей, типов и размеров.

Публикация локальной работы для просмотра этого архива не требуется:
исторические сдачи доступны преподавателю в разделе «Проверка» сразу после
завершения фонового импорта. Публиковать импортированный черновик следует только
тогда, когда эту работу нужно открыть студентам для новых попыток в
«Мехмат.Практикуме».

Scheduler можно масштабировать только при рабочем PostgreSQL advisory-lock контракте: активную работу выполняет один экземпляр, остальные ожидают. Sync worker допускает несколько экземпляров лишь при атомарном claim через `FOR UPDATE SKIP LOCKED`, lease timeout и идемпотентном внешнем API. Row lock нельзя держать во время Moodle/AI/runner HTTP-вызова. Runner рассчитан на один Uvicorn worker; горизонтальное масштабирование потребует общего replay-store для nonce.

`moodle-browser` также рассчитан строго на один Uvicorn worker и один Chromium.
Горизонтальное масштабирование потребует общего replay-store, распределённого
lease browser state и отдельной проверки Moodle session concurrency.

## 12. Проверки перед pilot

Репозиторные проверки:

```bash
cd backend
python -m pip install -r requirements-dev.txt
pytest
ruff check app tests
alembic check

cd ../frontend
npm ci
npm test
npm run build

cd ../runner
python -m pip install -e '.[test]' ruff
pytest
ruff check src tests

cd ../moodle_browser
python -m pip install -e '.[test]' ruff
pytest
ruff check src tests

cd ..
docker compose --env-file .env.example -f compose.yml config --quiet
docker compose --env-file .env.example -f compose.yml -f compose.local.yml config --quiet
xmllint --noout moodle/local/programming_bridge/db/install.xml
find moodle/local/programming_bridge -name '*.php' -exec php -l {} \;
```

Последняя команда требует PHP CLI подходящей версии; `php -l` проверяет только
syntax и не заменяет Moodle external-function/install/upgrade tests.

Затем на staging отдельно подтвердите:

1. teacher/student Playwright login, encrypted `storage_state`, повторный вход
   после restart и reauthentication после expiry; пароль/cookies/state
   отсутствуют в URL, logs, traces и frontend storage;
2. exact-origin policy, HMAC replay/clock checks, bounded queue и
   course/sections/activities/roster/groups discovery на текущей Moodle theme;
3. roster revoke во время активной попытки и deadline autosubmit;
4. Quiz Essay attachment-only capability для cmid `30354`: full single source и
   deterministic multi-file ZIP; отдельно на staging fixture подтвердите
   `ESSAY_ONLINE_TEXT` с полным однофайловым UTF-8 source. Для обоих режимов
   проверьте 4 MiB artifact/6 MiB request и fail-closed при не одном Essay либо
   неоднозначном response control; на отдельном Assignment проверьте
   автовыбор `ASSIGN_FILE`/`ASSIGN_ONLINE_TEXT`, предпочтение file при обоих,
   draft replacement, final confirmation и read-back. Artifact-матрица:
   один source → `main.c`/`main.cpp`; source + `.txt`/header или любые два
   файла → `submission.zip` с relative paths;
5. `PERIODIC`/`FINAL_MINUTE` дают `DRAFT_SAVED`, а
   `SUBMISSION`/`DEADLINE` — `FINALIZED`; локальный checkpoint сохраняется при
   Moodle outage; отдельно подтвердите, что retry/coalescing, включая restart
   browser-worker после ambiguous final response, не создают новую попытку;
   затем выполните grade/comment write и независимый read-back для exact mapped
   Assignment и отдельного Quiz Essay, включая mismatch шкалы и повтор;
6. runner local-subprocess smoke: compile, run, timeout, output limit и честные
   `filesystem_isolated=false`/`network=host`; отдельно зафиксируйте принятие
   риска отсутствия sandbox; проверьте чтение/перезапись workspace `.txt`,
   создание text/binary output в cwd и их удаление после job;
7. teacher-only hidden-test report на real runner: active claim/policy gates,
   per-case/total timeout, partial failure, stale recovery, idempotency replay,
   concurrency/rate limits и отсутствие automatic grade; mock здесь не подходит;
8. platform DB + credential-encryption-key restore, Moodle site/course backup
   limitation, privacy export, browser-state lease/revision recovery и
   exam-like load одного Chromium.

Если позже выбран optional bridge, добавьте отдельные clean
install/upgrade, HMAC launch, direct-launch origin, checkpoint recovery,
task-mirror, privacy и backup tests. Они не входят в pluginless acceptance.

До этого нельзя обозначать среду как production или exam-ready. LTI 1.3,
native Moodle question-bank two-way editing и automatic retention cleanup в этот
checklist не входят, потому что их ещё нет в baseline.

## 13. Диагностика и остановка

```bash
docker compose ps
docker compose exec -T moodle-browser python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8083/health/ready', timeout=3)))"
docker compose logs -f --tail=200 backend moodle-browser runner deadline-worker sync-worker frontend postgres
docker compose down
```

Если `moodle-browser` аварийно завершается с
`EACCES: permission denied, mkdtemp '/tmp/playwright-artifacts-*'`, проверьте,
что на NUC используется актуальный `compose.yml`: официальный Playwright Noble
запускает `pwuser` как `1001:1001`, и владельцы tmpfs `/tmp`, `.cache` и
`.config` должны совпадать. Текущая конфигурация задаёт `1001:1001`, оставляя
`/tmp` с обычным режимом `1777`. После обновления пересоздайте именно этот
контейнер или перезапустите systemd unit; простого restart старого container без
его recreate недостаточно.

Если форма входа показывает «Moodle вернул некорректный ответ при входе», это не
равнозначно неверному паролю: backend получил `INVALID_RESPONSE` от браузерного
адаптера. Сразу после одной попытки входа снимите обе связанные записи:

```bash
sudo docker logs --since 5m --tail=200 eduprog-moodle-browser-1
sudo docker logs --since 5m --tail=200 eduprog-backend-1
```

Адаптер пишет только тип и ограниченное описание ошибки разметки; пароль,
cookies, HTML страницы и browser `storage_state` в эти сообщения не попадают.

`docker compose down` сохраняет production `postgres-data`, объявленный как
external. Не применяйте `down -v` к среде с нужными данными: local override
использует managed volume, который этим ключом удаляется.
