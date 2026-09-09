# Мехмат.Практикум — платформа практического программирования

«Мехмат.Практикум» — LMS-независимая оболочка для лабораторных, самостоятельных,
контрольных и экзаменационных работ по C/C++. Первая интеграция выполнена для
Moodle: внешняя LMS остаётся источником identity пользователей, их фактического
enrollment, групп и структуры курса. Эффективную роль внутри платформы определяет
сам «Мехмат.Практикум»: enrollment без индивидуальной привязки даёт
`STUDENT`, а `TEACHER` выдаётся только через администраторский пул
преподавательских токенов. Платформа предоставляет браузерную IDE, историю написания, запуск
кода, проверку преподавателем и доступную для конкретного Moodle обратную
синхронизацию. Основной режим не требует установки плагина в Moodle: backend
передаёт логин и пароль отдельному внутреннему Playwright worker, который входит
через штатную HTML-форму Moodle. Пароль не сохраняется; между операциями backend
хранит только AES-GCM encrypted browser `storage_state`.

Это реализованный технический baseline v1, а не подтверждённая
production/exam-ready система. Unit/integration-тесты репозитория не заменяют
проверку на staging-копии Moodle, container runner smoke, нагрузочные тесты,
аудит безопасности и учебную приёмку. Текущий статус и границы перечислены в
[индексе документации](docs/README.md).

Старая версия в `_old_cpp_course_automatization` сохранена только как
исторический прототип. Новый код её не импортирует, не изменяет и не включает в
Docker-сборки.

## Что реализовано или подготовлено в baseline v1

- FastAPI/Pydantic v2 backend, async SQLAlchemy 2, PostgreSQL и Alembic;
- React/TypeScript/Vite SPA с Monaco для desktop и планшетов от 768 px;
- только две эффективные роли — `STUDENT` и `TEACHER`; локальной регистрации и
  локальных паролей нет. Moodle подтверждает identity/enrollment, но не может
  самостоятельно повысить пользователя до `TEACHER`;
- административное повышение уже вошедшей LMS-сессии через поле «Токен
  администратора»; это capability `SYSTEM_SETTINGS`, а не третья роль;
  повышение привязано к server session и сетевому префиксу клиента (`/24` для
  IPv4, `/64` для IPv6); смена префикса снимает только повышение, не LMS-сессию;
- пул до 256 индивидуальных преподавательских токенов в системных настройках.
  Новые токены состоят ровно из восьми ASCII-букв и цифр (`A–Z`, `a–z`, `0–9`).
  Для проверки хранится Argon2id hash, а для явного просмотра администратором —
  AES-GCM ciphertext. Обычный список никогда не содержит secret; раскрытие и
  замена доступны только с `SYSTEM_SETTINGS`, не кэшируются и аудируются.
  Ранее выпущенные восьмизначные URL-safe и структурированные токены продолжают
  работать; старые hash-only записи нельзя раскрыть, пока администратор не заменит
  их значение. Первая успешная Moodle-аутентификация с токеном создаёт строгую
  one-to-one привязку token ↔ principal; следующие входы сохраняют `TEACHER` без
  повторного ввода. Удаление записи немедленно заменяет выданные ею активные
  teacher memberships на `STUDENT`;
- pluginless-вход через отдельный `moodle-browser`: один Chromium, новый
  incognito context на операцию, exact Moodle origin, HMAC-подписанный внутренний
  API и ограниченная очередь. Пароль используется только в login request, не
  сохраняется и не журналируется; очищенный `storage_state` хранится на сервере
  в AES-GCM encrypted виде и обновляется под DB lease/revision. Production API
  отклоняет credential-вход, если
  публичный запрос пришёл не через HTTPS. Флаг
  `MOODLE_CREDENTIAL_LOGIN_ALLOW_INSECURE_HTTP=true` существует только для
  временного закрытого пилота: на участке пользователь → приложение пароль при
  HTTP можно перехватить, поэтому после настройки TLS флаг обязательно выключается;
- глобальный каталог курсов, которым управляет только пользователь с временной
  capability `SYSTEM_SETTINGS`: администратор добавляет Moodle-курс по
  разрешённой ссылке в системных настройках, а последующие входы проецируют
  memberships только из пересечения глобального каталога и Moodle dashboard
  пользователя. Курсы с Moodle dashboard сами по себе больше не создаются и не
  становятся доступными в приложении. Playwright discovery подтверждает доступ
  к целевому курсу и проецирует секции, участников, группы и activities с
  numeric `cmid`; глобальную роль преподавателя задаёт только token binding;
- внутреннее версионирование импортированного содержимого сохраняет снимок для
  уже начатых попыток, но отдельный локальный банк заданий и создание Moodle
  activities скрыты из текущего UI. Название, условие, открытие/закрытие,
  продолжительность, максимальный балл и число попыток всегда повторно
  проецируются из Moodle. Локальная публикация атомарно включает работу только
  для выбранных групп и не зависит от заранее доказанного answer transport.
  Неизвестный transport или drift формы блокирует fail-closed только внешнюю
  запись при checkpoint/final delivery; локальная сдача при этом сохраняется.
  Отсутствие ограничения принимается только как явно прочитанная настройка
  Moodle, а не как fallback parser-а;
- идемпотентная материализация обнаруженных Moodle `mod_assign`/`mod_quiz` по
  course id, module type и numeric `cmid`: однозначно распознанные лабораторные,
  самостоятельные, контрольные и экзаменационные activities появляются как
  выключенные локальные проекции. Преподаватель включает проекцию только для
  выбранных назначенных ему Moodle-групп; обладатель системного доступа может
  выбрать любую группу курса. Повторная синхронизация обновляет Moodle-owned
  поля, не изменяя локальную IDE/review policy. Login, course discovery, Quiz
  Essay answer sync (`ESSAY_ATTACHMENT` и `ESSAY_ONLINE_TEXT`),
  Assignment answer sync (`ASSIGN_FILE` и `ASSIGN_ONLINE_TEXT`) и запись решения преподавателя через штатные формы
  Moodle реализованы в коде и тестах, но ещё не прошли полную live production
  validation на целевом NUC. Запись балла и комментария поддержана для
  однозначно связанного Assignment и отдельного вопроса Quiz Essay, используя
  сохранённые внешние идентификаторы и durable outbox;
- read-only исторический импорт для связанных Moodle activities: после LMS sync
  `sync-worker` постранично получает через Playwright финальные ответы Quiz с
  Essay и стандартного Assignment, материализует код/вложения, оценки и
  комментарии как локальные сдачи и решения. Вопросы Essay одного Quiz
  сохраняются раздельно для независимой оценки, но очередь показывает попытку
  студента одной работой, а экран проверки даёт переключатель между заданиями.
  Вложения одного Essay остаются файлами соответствующего задания. Повтор
  идемпотентен. История
  набора, clipboard-свидетельства и промежуточные состояния Moodle отсутствуют,
  поэтому такие сдачи явно помечаются как импортированные. Реальная разметка
  Assignment в MMCS Moodle и на живом deployment всё ещё требует
  эксплуатационной проверки. В коде pluginless transport для нового
  Assignment ответа работает fail-closed по фактической форме:
  `ASSIGN_FILE`, если доказан file manager, или `ASSIGN_ONLINE_TEXT`,
  если доказан однозначный text control. Если форма даёт оба
  способа, коннектор выбирает `ASSIGN_FILE`. Для Quiz с ровно одним
  Essay по-прежнему используются `ESSAY_ATTACHMENT` и
  `ESSAY_ONLINE_TEXT`; изменившаяся или неоднозначная форма никогда
  не подменяется эвристикой. Перезапись прежнего Assignment-файла допускается
  только после скачивания единственного текущего same-origin attachment и
  совпадения SHA-256 с сохранённой квитанцией;
- исторические source snapshots имеют материализацию v5: ближайший успешный
  sync один раз перечитывает legacy mapping даже без смены Moodle revision,
  после чего снова работает идемпотентно. Вложения скачиваются через
  аутентифицированный HTTP-контекст Playwright, а браузерный `innerText` сохраняет
  форматирование Quiz Essay и Assignment online-text. Для Assignment
  online text и вложения материализуются одновременно, не
  вытесняя друг друга. Безопасные ZIP- и 7z-вложения распаковываются в памяти с
  сохранением относительных путей; `.c/.cc/.cpp/.cxx`, заголовочные файлы и
  `.txt` попадают в рабочую область. Traversal, absolute paths, special/link
  entries и превышение лимитов отклоняются;
- рабочая область до 64 файлов и 512 KiB исходного UTF-8 текста, optimistic
  revision, идемпотентные изменения, серверная цепочка хешей и snapshots;
- запрет внешней вставки при политике `INTERNAL_ONLY` и серверные receipts для
  вставки текста, скопированного в той же попытке;
- ручная сдача, серверная автосдача последней принятой ревизии и локальные
  snapshots/checkpoint-события: `D/10`, в последней пятой `D/20`, с интервалом
  30–900 секунд, плюс начало, завершение, дедлайн и последняя минута;
- полный канонический manifest, SHA-256, `event_chain_head`, epoch и workspace
  revision всегда сохраняются локально и остаются authoritative при сбое
  Moodle. Для явно mapped Quiz с ровно одним Essay или стандартного
  Assignment sync-worker асинхронно отправляет ответ в доказанный
  online-text или file control. В file-режиме workspace из ровно одного source
  передаётся как `main.c` или `main.cpp`; любой набор из нескольких
  файлов передаётся как детерминированный `submission.zip` с
  `.c/.cc/.cpp/.cxx`, `.h/.hh/.hpp/.hxx`, `.inc` и `.txt`, сохраняя
  безопасные относительные пути. Поэтому `main.cpp` вместе с любым
  `.txt` уже отправляется как ZIP:
  `PERIODIC`/`FINAL_MINUTE` дают `DRAFT_SAVED`, а
  `SUBMISSION`/`DEADLINE` — `FINALIZED`. Доставка имеет durable outbox lease,
  стабильный idempotency key и bounded retry; decoded artifact ограничен 4 MiB,
  подписанный внутренний JSON request — 6 MiB. Compiler output, IDE и история
  изменений остаются только локально;
- после каждой подтверждённой доставки контрольной точки коннектор сохраняет
  локальную неизменяемую квитанцию
  точного содержимого: требуемый MD5 и авторитетный SHA-256. Для online text
  дополнительно хранится digest одной канонической формы с сохранением
  внутренних пробелов, табуляции и пустых строк; для файла/ZIP сравниваются
  исходные байты до распаковки. Исторический read-back из Moodle даёт во
  вкладке «Плагиат» зелёный статус при совпадении, жёлтый при отсутствии
  локальной доставки и красный при однозначно связанной, но изменённой сдаче.
  Неполный ответ или неоднозначная корреляция показываются нейтрально и не
  трактуются как нарушение;
- отдельный HMAC-защищённый C/C++ runner с GCC/Clang, фиксированными профилями,
  resource/output timeout и структурированными диагностиками. Временно код
  запускается обычным subprocess внутри runner-контейнера без per-job sandbox;
- immutable submission, список «Все сданные»/«Ожидают проверки»/«Проверенные»,
  серверное закрепление работы за проверяющим, private teacher experiment,
  редактирование/сброс/запуск,
  построчные ошибки, черновик, immutable история решений и перепроверка.
  Преподаватель видит сдачи студентов из общего Moodle `CourseGroup`, а
  `SYSTEM_SETTINGS` даёт глобальный read-only аудит без права менять оценку;
- анализ процесса написания и поиск сходства решений запускаются преподавателем
  вручную во вкладке «Плагиат». Эти проверки не выставляют балл и не создают
  санкции автоматически;
- СППР: преподавательский запуск опубликованных скрытых stdin/stdout-тестов по
  неизменяемому snapshot с сохраняемым отчётом, локальный winnowing-анализ
  сходства с side-by-side сравнением пары и подсветкой совпавших диапазонов,
  внешний API анализа авторства и AI-чаты с разными политиками для
  студента и преподавателя. Запуск официальных hidden-test evidence требует
  активного закрепления работы, включённой СППР и реального runner; mock не
  считается свидетельством. AI budget проверяется сервером до provider call:
  по умолчанию 10 student- и 30 teacher-сообщений на пользователя и режим за
  60 секунд, суммарно по всем его threads. Ни один сигнал не выставляет оценку
  автоматически.

## Что baseline v1 не обещает

- LTI 1.3, NRPS/AGS/Deep Linking и нативный двусторонний редактор Moodle Quiz /
  question bank ещё не реализованы. Pluginless-режим не создаёт и не изменяет
  Moodle activities: он хранит только локальную проекцию поддерживаемой activity
  и её групповой переключатель доступности. Connector-owned task mirror остаётся
  возможностью только опционального plugin 0.3.
  Quiz Essay/Assignment sync переносит материал ответа, но не local
  manifest/history/compiler artifacts;
- Playwright login, course discovery и Quiz Essay/Assignment draft/final sync требуют
  staging-проверки на фактической Moodle и целевом NUC; наличие unit/integration
  tests не является такой проверкой. Idempotency cache browser-worker
  process-local, поэтому ambiguous final response после его restart не имеет
  доказанной exactly-once гарантии до отдельной staging-проверки;
- запись grade/comment через Playwright реализована для однозначно связанного
  стандартного Assignment и отдельного Quiz Essay. До staging write/read-back
  проверки на целевой Moodle это остаётся fail-closed интеграцией: неоднозначная
  форма, изменившиеся идентификаторы или неподдерживаемая шкала блокируют
  доставку и сохраняют локальное решение для повторной отправки;
- frontend сохраняет изменения REST-запросами и держит неподтверждённую очередь
  в памяти вкладки. WebSocket presence и восстановление unacked-операций из
  IndexedDB остаются будущей работой;
- deterministic evidence v1 ограничен teacher-authored stdin/stdout cases с
  `EXACT`/`TRIM_TRAILING_WHITESPACE`; sanitizer, static analysis, versioned
  rubric и автоматически вычисляемая рекомендация остаются следующими этапами;
- workspace и runner допускают `.txt` как runtime data. Перед запуском
  text files копируются в тот же временный cwd, где лежит executable,
  поэтому программа может их читать и перезаписывать, а также создавать
  новые text/binary files. Все runtime-изменения и сгенерированные файлы
  transient: они не возвращаются в workspace и удаляются вместе с job cwd;
- поле `retention_days` сохраняет утверждённое значение политики, но в baseline
  нет автоматического удаления данных. До внедрения отдельного проверенного job
  данные удаляются только по утверждённой операторской процедуре;
- выбранный `UNRESTRICTED_CONTAINER` вообще не является hostile-code sandbox;
  репозиторий не подтверждает защиту от намеренного чтения файлов/сети runner
  либо kernel/container escape;
- реальный Playwright login/discovery/Quiz Essay draft и finalize после
  Docker-сборки на NUC, устойчивость parser к текущей Moodle theme,
  опциональная установка plugin 0.3, будущая hardened sandbox, общая очередь Moodle у
  дедлайна, 100 одновременных попыток и recovery из backup должны быть проверены
  на staging до пилота.

## Состав репозитория

- `frontend/` — React, TypeScript, Monaco и Nginx;
- `backend/` — FastAPI, доменная модель, API, workers и адаптеры;
- `moodle_browser/` — внутренний Playwright service для pluginless login,
  course discovery, Quiz Essay и Assignment file/online-text sync, read-only исторического
  импорта Quiz Essay/Assignment и записи преподавательских баллов/комментариев
  через штатные формы Moodle; один Chromium, наружу не публикуется;
- `runner/` — отдельный сервис компиляции и запуска C/C++;
- `moodle/local/programming_bridge/` — опциональный Moodle local plugin 0.3 для
  расширенных checkpoint/task-mirror возможностей;
- `docs/` — требования, фактический статус, архитектура и критерии приёмки;
- `compose.yml` — Linux baseline с PostgreSQL, local-subprocess runner и
  `moodle-browser`;
- `compose.local.yml` — локальный smoke-профиль, который не исполняет C/C++.

## Быстрый локальный запуск

Нужны Docker Engine/Desktop и Docker Compose v2.

```bash
cp .env.example .env
docker compose -f compose.yml -f compose.local.yml up --build -d
docker compose -f compose.yml -f compose.local.yml exec backend python -m app.cli seed-demo
```

Интерфейс будет доступен на `http://localhost:8080`. Dev-кнопки открывают
сценарий студента или преподавателя. Локальный токен повышения — `ADMIN_TOKEN`
из `.env`, а при пустом значении — `local-admin-token`. В local-профиле
`RUNNER_MOCK_ENABLED=true`: C/C++ не компилируется и не запускается. Этот
override также задаёт `AUTO_MIGRATE=true`; production оставляет
`BACKEND_AUTO_MIGRATE=false` и применяет Alembic отдельной контролируемой
командой.

Полный Linux rollout, pluginless Moodle, опциональный bridge, секреты, backup и
staging checklist описаны в
[DEPLOYMENT.md](DEPLOYMENT.md).

Для Linux-сервера с Docker Compose предусмотрен `run_eduprog.sh`: он собирает
образы, поднимает PostgreSQL, применяет Alembic migrations и проверяет готовность
сервисов. Готовый unit находится в `deploy/systemd/eduprog.service`; скрипт также
понимает legacy-переменные `DBLOGIN`, `DBPASSWORD`, `OPENROUTER_API_KEY` и
`OPENROUTER_MODEL` из прежнего `cpp_markup.env`. При ручном запуске launcher сам
читает разрешённые deployment-переменные из `~/cpp_markup.env` без `source` или
`eval`; другой путь задаётся через `EDUPROG_HOST_ENV_FILE`.

На целевом NUC файл `/home/alexey/cpp_markup.env` должен задавать
`PUBLIC_BIND_ADDRESS=127.0.0.1` и `PUBLIC_PORT=5173`. Это локальный endpoint NUC;
внешний VPS отдельно публикует порт `8080` и направляет его на
`127.0.0.1:5173`. После копирования версии штатный
`sudo systemctl restart eduprog.service` пересобирает образы, применяет
committed Alembic migrations и выполняет readiness-проверки. Точные команды
переноса, health-check и просмотра journal приведены в
[DEPLOYMENT.md](DEPLOYMENT.md#пересборка-после-копирования-изменений-на-nuc).

Frontend собирается внутри Docker: `npm ci` использует committed
`frontend/package-lock.json`, а host `node_modules` исключён через
`frontend/.dockerignore`. Устанавливать npm-пакеты вручную на NUC не нужно.

Проверить, что OpenRouter-настройки дошли до launcher и уже запущенного backend,
не раскрывая API key, можно командой `./run_eduprog.sh diagnose-ai`.

Недоступный runner не должен уводить весь сайт в systemd restart loop: launcher
оставляет frontend/backend доступными и пишет диагностическое предупреждение.

## Проверки репозитория

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
```

PHP CLI/Moodle runtime нужен для проверки plugin отдельно; XML-синтаксис можно
проверить `xmllint --noout moodle/local/programming_bridge/db/install.xml`.

Backend без Compose запускается командами:

```bash
cd backend
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
python -m app.workers.scheduler
python -m app.workers.sync
```

Workers запускаются отдельными процессами; две последние команды приведены как
отдельные process entrypoints и не должны выполняться последовательно в одном
терминале.

## Граница безопасности runner

Runner использует обычный subprocess внутри отдельного read-only Docker-
контейнера с private tmpfs, dropped capabilities и CPU/RAM/PID limits. Per-job
filesystem/network sandbox сейчас отсутствует; backend сохраняет фактические
`filesystem_isolated=false` и `network=host`. Код может обращаться к доступным
контейнеру файлам и сети. Для пилота runner следует вынести на отдельный
минимальный Linux-хост без Moodle, DB, AI, application и Docker-socket секретов.
