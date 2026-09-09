# Moodle browser service

Внутренний сервис для pluginless-интеграции с Moodle. Процесс держит один
Chromium, но создаёт отдельный incognito-контекст на каждую операцию. Число
параллельных операций ограничено семафором (по умолчанию одна).

Сервис намеренно имеет только шесть прикладных маршрутов:

- `POST /internal/v1/moodle/login`;
- `POST /internal/v1/moodle/course/discover`;
- `POST /internal/v1/moodle/activity/submissions/discover`;
- `POST /internal/v1/moodle/assignment/grade`;
- `POST /internal/v1/moodle/assignment/submission/sync`;
- `POST /internal/v1/moodle/quiz/essay/sync`.

Произвольные URL и выполнение JavaScript через API не поддерживаются. Сервис
принимает только свой настроенный HTTPS origin, сам строит известные пути
Moodle и блокирует сетевые запросы Chromium на другие origin.

`login` получает от core ограниченный `allowed_course_ids` — глобальный список
курсов, предварительно подтверждённых администратором приложения. Dashboard
Moodle используется только как часть проверки сессии и никогда не расширяет
этот список. Через `fetch` в той же авторизованной вкладке worker получает
канонические `/course/view.php?id=...` страницы разрешённых ID без полной
навигации и рендеринга. Роль `TEACHER` подтверждается только
same-origin элементом управления с тем же course id; отсутствие такого элемента
на успешно полученной авторизованной странице означает `STUDENT`. Непроверенные
из-за лимита, тайм-аута или редиректа курсы остаются `UNKNOWN` и не повышают
права, но дополнительная проверка роли не отменяет успешный вход. Полный roster
по-прежнему подтверждается отдельным `course/discover`. Лимит страниц входа
задаётся `MOODLE_BROWSER_MAX_LOGIN_COURSE_ROLE_PAGES` (по умолчанию 64), общий
временной бюджет — `MOODLE_BROWSER_LOGIN_COURSE_ROLE_BUDGET_SECONDS` (15 секунд).

`course/discover` ограниченно обходит ссылки секций Moodle top-format. URL
каждой секции перестраивается сервисом из проверенных числовых `course id` и
`section`, а результаты объединяются с дедупликацией по `cmid`.

`activity/submissions/discover` читает исторические сдачи одного заранее
известного `quiz` или `assign`. Запрос пагинируется непрозрачным для core
курсором и возвращает не более 25 попыток. Стабильный `external_id` строится из
`module`, `cmid` и идентификатора попытки/пользователя, а `external_revision` —
из канонического содержимого ответа и оценки; это позволяет core выполнять
идемпотентный upsert. Для quiz используется преподавательский overview report
и отдельные review-страницы, для assign — grading table и grader page.
Вложения скачиваются только с same-origin `/pluginfile.php/`. Общий wire-бюджет
исходного текста и base64-вложений — 2 МиБ на ответ API (дополнительно действует
индивидуальный верхний предел артефакта до 4 МиБ). Превысивший бюджет файл или
ответ возвращается как недоступный, а не как обрезанный. Операция доступна
только если страница курса подтверждает роль преподавателя текущей LMS-сессии.

`quiz/essay/sync` принимает один итоговый артефакт не более 4 МиБ и работает
только с настоящей попыткой quiz, в которой ровно один вопрос essay и один
file manager. Имя файла стабильно и безопасно; существующий файл заменяется
только после явного Moodle-confirmation. `finalize=false` сохраняет черновик и
возвращается к попытке, `finalize=true` отдельно подтверждает окончательную
отправку. Teacher preview, неоднозначная разметка, чужой курс/`cmid`, истёкшая
сессия и редирект на другой origin отклоняются. `question_slot` определяется
по странице Moodle и только возвращается в receipt — клиент его не задаёт.

`assignment/submission/sync` работает только с заранее сопоставленным numeric
`cmid` и подтверждённой штатной формой Assignment. По фактическим включённым
submission plugins он принимает либо полный UTF-8 online text, либо один
файловый артефакт (`main.c`, `main.cpp` или `submission.zip`), проверяет лимиты и
accepted file types до изменения ответа, сохраняет draft и при `finalize=true`
проходит отдельное штатное подтверждение отправки. Замена другого имени файла
не выполняется по одному basename: если доказанный предыдущий managed-артефакт
ещё присутствует, операция блокируется до его безопасного ручного удаления;
чужие и вложенные файлы connector не удаляет. Замена под тем же именем требует
ровно один same-origin `draftfile.php`/`pluginfile.php` URL в текущем file
manager: browser-worker скачивает прежний файл до открытия picker и сверяет его
SHA-256 с durable receipt. Отсутствие ссылки, неоднозначность либо несовпадение
блокируют mutation. Значение Moodle `maxsize=0` считается наследуемым, а не
безлимитным: действует внутренний предел 4 МиБ, а текущий file manager обязан
раскрыть конкретный effective maxbytes. Остаётся обязательной staging-проверка
server-side валидации upload для конкретной инсталляции Moodle.

`assignment/grade` открывает точную штатную grading form связанной сдачи,
проверяет внешний идентификатор и шкалу и записывает преподавательский балл и
комментарий. Неоднозначная либо изменившаяся разметка отклоняется fail-closed;
до production этот путь всё равно требует live staging write/read-back.

Кэш idempotency ограничен и находится в памяти одного процесса. Поэтому
backend должен сохранять свой durable outbox; после перезапуска сервиса защита
от повторной окончательной отправки не восстанавливается автоматически.

Состояние Playwright ограничено явной JSON-схемой и размером, очищается от
cookie и localStorage других origin и должно храниться backend только в
зашифрованном виде. Пароль Moodle сервис не возвращает и не сохраняет.

## Аутентификация внутренних запросов

Все прикладные запросы подписываются HMAC-SHA256. Каноническая строка:

```text
<unix timestamp>\n<nonce>\n<sha256(raw request body)>
```

Заголовки: `X-Moodle-Timestamp`, `X-Moodle-Nonce`,
`X-Moodle-Signature: v1=<hex digest>`. Nonce одноразовый в пределах TTL.
Health endpoints публичны и не содержат секретов.

JSON-запрос ограничен 6 МиБ (base64 для артефакта до 4 МиБ плюс bounded
`storage_state`). Лимиты задаются `MOODLE_BROWSER_ARTIFACT_MAX_BYTES` и
`MOODLE_BROWSER_REQUEST_BODY_MAX_BYTES`, верхние пределы — 4 и 8 МиБ.

## Локальный запуск

```bash
cd moodle_browser
python -m pip install -e '.[test]'
export MOODLE_BROWSER_SHARED_SECRET='at-least-32-random-bytes-change-this'
uvicorn moodle_browser.app:create_app --factory --host 127.0.0.1 --port 8083
```

Для production используется один Uvicorn worker: несколько worker-процессов
создали бы несколько Chromium и независимые nonce-кэши.

## Docker

Образ основан на официальном Playwright Python image и фиксирует одинаковую
версию Python-пакета и браузеров (`1.62.0`):

```bash
docker build -t edu-moodle-browser:1.62.0 moodle_browser
```

Сервис должен находиться только во внутренней Docker-сети и не публиковаться
на внешнем интерфейсе.
