# Pluginless-интеграция с Moodle через Playwright

Этот документ фиксирует принятый вариант интеграции без установки плагина в
Moodle, результаты авторизованного просмотра курса 549 и границы реализованного
baseline.
Moodle остаётся внешним владельцем identity, enrolments, групп, структуры курса,
условий заданий, временных окон, максимальных баллов и официальных попыток.
Глобальную роль `STUDENT|TEACHER` определяет локальная teacher-token policy, а
не Moodle role. «Мехмат.Практикум» предоставляет IDE, компилятор, историю
написания, локальные snapshots и интерфейс проверки.

> Зафиксированное продуктовое правило: импортированная работа не редактируется
> и не создаётся заново в Практикуме. Преподаватель выбирает уже существующую
> activity Moodle и включает её в Практикуме для одной или нескольких своих
> Moodle-групп. Название, условие, открытие/закрытие, шкала и число попыток
> всегда перечитываются из Moodle. Локальная «публикация» — это только
> управляемая видимость оболочки, а не изменение Moodle activity.

Локальная публикация атомарно создаёт только availability выбранных групп и не
требует заранее доказанного answer transport либо ручной настройки стартового
файла. Connector проецирует известный transport в generic workspace profile
при старте попытки, а перед checkpoint/final delivery повторно проверяет
фактическую student form. Неизвестный transport или изменившийся control
не блокирует локальную публикацию, но до следующей успешной синхронизации
останавливает создание попытки вместо угадывания workspace mode. Drift после
старта блокирует внешнюю mutation fail-closed, но не уже сохранённую сдачу.
Moodle-owned название, расписание, шкала и число попыток остаются read-only
проекцией.

> Default `PLUGINLESS` transport — `PLAYWRIGHT`: отдельный внутренний
> browser-worker. Legacy `MOBILE_TOKEN` не является fallback принятого flow: на
> обследованной установке этот путь не дал пригодного ответа, а установка
> университетского плагина сейчас не предполагается. Пароль нужен только для
> создания браузерной сессии и никогда не сохраняется.

## 1. Статус возможностей

Нужно различать реализованный и протестированный в репозитории transport,
ручной live inspection конкретной попытки и ещё не выполненную production
validation на staging Moodle/NUC.

| Возможность | Статус |
| --- | --- |
| Вход через штатную HTML-форму Moodle | реализованный Playwright transport |
| Хранение сессии без пароля | реализовано как зашифрованный `storage_state` |
| Чтение курса, sections, activities и ограниченного roster | реализованный browser transport |
| Проверка exact Moodle origin и блокировка внешних origin | реализовано |
| Загрузка файла в текущий Quiz Essay и переход к summary | подтверждено ручным live inspection, без final submit |
| Периодическая доставка draft/checkpoint в Moodle | реализована через outbox, статус `DRAFT_SAVED` |
| Final submit по Finish/deadline | реализован через outbox, статус `FINALIZED` |
| Новый ответ Quiz с ровно одним Essay | реализованы оба доказанных режима: `ESSAY_ATTACHMENT` и `ESSAY_ONLINE_TEXT` |
| Read-only импорт существующих Quiz Essay/Assignment сдач | реализован фоновыми постраничными идемпотентными jobs после LMS sync; реальный MMCS Assignment markup требует эксплуатационной проверки |
| Запись оценки и комментария преподавателя | реализована через штатные формы Assignment и отдельного Quiz Essay; требуется live staging validation/read-back |
| Новый студенческий ответ в Assignment | реализованы `ASSIGN_FILE` и `ASSIGN_ONLINE_TEXT`; transport доказывается по фактической student form, file предпочтителен при наличии обоих; нужен live staging save/finalize/read-back |
| Компиляция, запуск, IDE и история изменений | только локально, вне Moodle |

Внутренний grade route browser-worker является узкой типизированной операцией:
он записывает балл и комментарий через canonical grader стандартного Assignment
либо manual-grading form конкретного вопроса Quiz Essay. Наличие unit-тестов не
заменяет staging write/read-back на целевой Moodle, поэтому неоднозначная или
изменившаяся форма отклоняется fail-closed.

## 2. Архитектура browser transport

Путь запроса:

```text
браузер пользователя
  -> FastAPI core
  -> HMAC-подписанный внутренний запрос
  -> один moodle-browser worker / один Chromium
  -> https://edu.mmcs.sfedu.ru
```

Browser-worker работает только во внутренней Docker-сети и не публикует порт
наружу. Один процесс держит один Chromium. Для каждой операции создаётся новый
incognito browser context, в него загружается конкретный `storage_state`, а после
операции context закрывается. Постоянного браузера на каждого пользователя нет.

Один Chromium обслуживает bounded набор изолированных contexts. Фоновые обходы
курса используют отдельную полосу, а подготовка, сохранение и завершение
студенческой попытки — foreground-полосу; входу оставлен отдельный резерв.
Если соответствующая полоса занята дольше лимита, запрос получает
контролируемый `busy/unavailable`, а не создаёт новый Chromium. Это принципиально
для N150 и 16 ГиБ RAM. Увеличивать concurrency можно только после нагрузочного
теста; несколько Uvicorn workers для browser-service запускать нельзя, иначе
появятся несколько Chromium и независимые nonce-cache.

Core outbox обрабатывается двумя claim-lanes по умолчанию и приоритетно выбирает
`attempt.checkpoint` перед `course.sync`. После локального нажатия «Завершить» UI
показывает успех только когда терминальный checkpoint получил `DELIVERED`; при
`FAILED/BLOCKED` сохранённую финальную ревизию можно поставить в очередь снова,
не создавая новую локальную или Moodle-попытку.

Core и browser-worker общаются только через узкие операции:

- login;
- course discovery;
- Quiz Essay и Assignment draft answer/finalization;
- read-only discovery существующих Quiz Essay/Assignment submissions;
- запись grade/comment для однозначно адресованной исторической сдачи.

API не принимает произвольный URL, CSS selector или JavaScript от клиента.
Внутренние запросы подписываются HMAC-SHA256 по timestamp, одноразовому nonce и
SHA-256 тела. Размер тела, ответа, `storage_state`, число страниц roster и время
навигации ограничены. Decoded answer artifact имеет hard limit 4 MiB, а
подписанный JSON request с base64 — 6 MiB.

## 3. Вход и жизненный цикл сессии

### 3.1. Первый вход

1. Пользователь выбирает provisioned Moodle connection и вводит Moodle login и
   password. Необязательные токены приложения не заменяют Moodle-аутентификацию: teacher token нужен для первой one-to-one привязки глобальной роли, admin token — для временной `SYSTEM_SETTINGS` elevation.
2. FastAPI применяет rate limit и передаёт credentials во внутренний
   HMAC-подписанный login request.
3. Browser-worker открывает только
   `https://edu.mmcs.sfedu.ru/login/index.php`, использует штатную форму с её
   hidden `logintoken`, заполняет `username`/`password` и ждёт авторизованную
   страницу.
4. Worker проверяет признаки авторизованной сессии и identity, затем возвращает
   очищенный Playwright `storage_state`. Backend передаёт ему только numeric ID курсов из глобального каталога; dashboard Moodle не является источником автоимпорта. Для каждого разрешённого ID worker проверяет авторизованную доступность точного `/course/view.php?id=...`; это доказательство enrolment/scope, но не глобальной роли. Любые role/control hints из HTML игнорируются при выводе `TEACHER`. Число запросов и общий бюджет ограничены; тайм-аут или ошибка отдельной страницы не отменяет identity login, но не даёт course scope.
5. После завершения request/context пароль больше не используется. Он не
   записывается в БД, cookie приложения, browser local storage, audit, access
   log или analytics.
6. Core создаёт собственную server-side session пользователя. Если при входе передан валидный свободный teacher token, он атомарно привязывается к подтверждённому principal. Существующая активная привязка даёт глобальную роль `TEACHER`; при её отсутствии всегда выбирается `STUDENT`.

В production участок от браузера пользователя до приложения также должен быть
HTTPS. Временный HTTP escape hatch допустим только для закрытого пилота: при нём
пароль защищён хуже, хотя Moodle origin остаётся HTTPS.

### 3.2. Что хранится

В PostgreSQL хранится credential kind `BROWSER_STATE_V1`, а не пароль. JSON
`storage_state` предварительно проверяется и ограничивается, затем шифруется
AES-GCM. Associated data связывает ciphertext с `connection_id`,
`principal_id` и kind. Подмена записи, перенос к другому principal или неверный
encryption key приводят к fail-closed и повторному входу.

Строка сессии имеет revision, lease owner/expiry и last-used timestamp. Lease
не позволяет двум backend/worker операциям одновременно менять одну Moodle
сессию; revision защищает от сохранения устаревшего `storage_state`. Новый state
после успешной операции атомарно заменяет предыдущий. Истёкшая или отозванная
Moodle session переводит principal в `reauthentication required`. После
успешного повторного входа checkpoint-и этого пользователя, остановленные
именно из-за истёкшей сессии, возвращаются в outbox; прочие protocol/mapping
ошибки автоматически не перезапускаются.

`MOODLE_CREDENTIAL_ENCRYPTION_KEY` должен быть постоянным отдельным секретом не
короче 32 символов и резервироваться вместе с БД. Его нельзя регенерировать при
обычном restart. Legacy kind `MOBILE_TOKEN` может оставаться в БД для
совместимости миграции, но не является password fallback и не нужен принятому
browser flow.

## 4. Exact-origin политика

Для первого connector настроен единственный origin:

```text
https://edu.mmcs.sfedu.ru
```

Это именно origin, без path, query, fragment, userinfo и произвольного порта.
Ссылка курса принимается только если имеет тот же origin, ожидаемый путь
`/course/view.php` и один положительный numeric `id`, например:

```text
https://edu.mmcs.sfedu.ru/course/view.php?id=549
```

Browser-worker сам строит известные Moodle routes. После каждой навигации он
повторно проверяет origin; redirect наружу отклоняется. Chromium route policy
блокирует запросы к другим origin, service workers отключены, downloads по
умолчанию запрещены. Переданный `storage_state` очищается от cookies и origins,
не относящихся к настроенному Moodle.

Эта защита обязательна: Playwright не должен превращаться в общий HTTP proxy,
SSRF-инструмент или исполнитель селекторов, присланных пользователем.

## 5. Авторизованный аудит курса 549

Аудит выполнен 24 августа 2026 года после ручного входа владельца учётной
записи. В документацию не перенесены пароль, cookies, session key и персональные
данные студентов.

Подтверждено:

- canonical URL — `https://edu.mmcs.sfedu.ru/course/view.php?id=549`;
- название — «Программирование на С++»;
- Moodle course id — `549`, context id — `52955`;
- формат курса — topics;
- section и activity определяются по numeric IDs, а не по тексту названия;
- скрытые sections видны только с соответствующими полномочиями.

### 5.1. Sections и activity cmid

`id` в URL вида `/mod/quiz/view.php?id=30354` или
`/mod/assign/view.php?id=23457` — course-module id (`cmid`). Именно `cmid` вместе
с course id и module type является устойчивым locator для mapping.

| Section | Название | Зафиксированные activities |
| ---: | --- | --- |
| 0 | Общее | без activities в просмотренном snapshot |
| 1 | Информация для студентов | 7 resources/URLs |
| 2 | Материалы для преподавателей | 4 URL/labels, section hidden |
| 3 | Материалы к лекциям | 15 resources |
| 4 | Лабораторные работы | `mod_assign`, в том числе cmid `23457`, `26437`, `23458`, `23461`, `23462`, `23467`, `23466`, `23465`, `23468`, `33724`, `23469`, `33725`, `23470`; также resources/URLs и добор `31414`, `31659` |
| 5 | Проверочные (самостоятельные работы) | `mod_quiz`: cmid `30354`, `30355`, `30360`, `30362`, `30364`, `30423` |
| 6 | Контрольные работы | `mod_quiz`: cmid `23471`, `23472`, `42084`; также files/label |
| 7 | Индивидуальные задания | `mod_assign`: cmid `23474`, `23475`, `33733`, `23476`, `33734`, `31670` |
| 8 | Тесты | `mod_quiz`: cmid `23736`, `38150`, `23742`, `23743`, `37478` |
| 9 | Экзамен | `mod_quiz`: cmid `43700`, `31417`, `43701`, `31424`, `43702`, `31421` |
| 10 | АРХИВ | 35 mixed Quiz/Assignment/resource activities |
| 11 | Для добора баллов | `mod_quiz`, cmid `43705` |

Это snapshot конкретного курса, а не hard-coded справочник. При sync connector
заново читает section pages и хранит external revision/hash. Переименование не
ломает mapping, но исчезновение или смена module type требует явного
reconciliation.

Baseline проецирует однозначно распознанные `mod_assign`/`mod_quiz` из разделов
лабораторных, проверочных/самостоятельных, контрольных и экзамена. Для курса 549
это разделы 4, 5, 6 и 9. Разделы «Индивидуальные задания», «Тесты», «АРХИВ»,
«Для добора баллов», а также resource/url/label не становятся программными
работами без явного включения поддерживаемого типа.

Каждая поддерживаемая activity создаёт локальную проекцию `Assessment`, но её
Moodle-поля остаются read-only. Для Quiz Essay каждый вопрос/slot является
отдельной работой; два Essay-вопроса нельзя показывать как два файла одной
работы. Повторный sync идемпотентен по `(connection, module, cmid, slot)` и
обновляет Moodle-проекцию. Локального fallback-срока, вручную введённой шкалы
или заменяющего Moodle текста условия нет: неполная проекция помечается ошибкой
синхронизации и повторно читается из Moodle.

Импортированная activity изначально выключена в Практикуме. Преподаватель может
только выбрать свои Moodle-группы и включить её; обладатель системного доступа
может выбрать любые группы курса. Студент видит работу, только если она включена
и его Moodle membership входит в выбранную группу. Отдельный локальный банк
заданий и создание новых Moodle activities отложены и скрыты из текущего UI.

### 5.2. Проверенная самостоятельная работа

На activity «Самостоятельная работа №1» подтверждены:

- course `549`, `mod_quiz`, cmid `30354`, context `124916`;
- вопрос типа Moodle `essay`, состояние `manualgraded`;
- в просмотренной попытке есть file manager/attachment control;
- нет textarea, contenteditable online editor и CodeRunner control;
- следовательно, конкретная текущая попытка — **Essay с file attachment only**;
- тестовая загрузка файла `.cpp` успешно появилась в file manager;
- после сохранения Moodle открыл summary с отдельной кнопкой
  «Отправить всё и завершить тест».

Live inspection дошёл до summary, но намеренно не выполнял final submit. Он
подтверждает технический маршрут draft attachment, а не разрешает connector
безусловно завершать реальные студенческие попытки.

Настройки Essay могут различаться между activities и даже выбранными random
questions. Поэтому capability определяется по фактической попытке:

- `ESSAY_ATTACHMENT` — загружаем полный source либо детерминированный ZIP,
  online-text editor не заполняем;
- `ESSAY_ONLINE_TEXT` — записываем единственный C/C++ translation unit в
  однозначный textarea/editor; вспомогательные `.txt` остаются только в IDE;
- отсутствие обоих способов не блокирует локальное включение для групп, но
  блокирует внешнюю mutation до появления поддерживаемой формы.

Текущий transport дополнительно требует ровно один вопрос Essay и однозначный
control выбранного режима. Import сохраняет конкретный `answer_transport`, а
при каждом draft/final worker проверяет, что фактическая попытка всё ещё его
поддерживает. Несколько Essay-вопросов, teacher preview либо неоднозначная
разметка отклоняются fail-closed.

### 5.3. Стандартный Assignment

Для `mod_assign` browser-worker не выводит тип ответа из названия,
описания activity или исторической grading page. Перед каждой записью
он открывает фактическую student submission form и доказывает штатные
controls, hidden identifiers и save/final-submit actions:

- `ASSIGN_FILE` — найден однозначный file manager и его ограничения;
- `ASSIGN_ONLINE_TEXT` — найден однозначный online-text editor;
- если доказаны оба transport, выбирается `ASSIGN_FILE`;
- если не доказан ни один или форма неоднозначна, mutation не
  выполняется.

Draft save заменяет connector-owned answer в текущей попытке, а
Finish/deadline идёт через штатную final-submit/confirmation форму.
Реализация и fixtures не заменяют live приёмку: до пилота на целевой
MMCS Moodle нужно проверить save, replacement, finalize, retry/read-back,
лимиты/типы файлов и team-submission policy.

## 6. Глобальный каталог курсов и роли

1. Администратор приложения provisioned connection для exact origin.
2. Пользователь входит через Moodle с административным токеном и получает
   временную capability `SYSTEM_SETTINGS`; административный токен не заменяет
   Moodle-аутентификацию и не является третьей ролью.
3. В системных настройках администратор вставляет canonical URL курса. Только
   `SYSTEM_SETTINGS` может начать и подтвердить импорт. Подтверждённый курс
   включается в один глобальный allow-list для всех пользователей системы.
4. Browser-worker открывает course page и section-specific pages, извлекает
   course id, section id, module type, cmid, название, visibility и доступные
   временные параметры.
5. Системный пул teacher tokens управляется только через `SYSTEM_SETTINGS`. Новые токены имеют ровно восемь ASCII-букв/цифр; каждая новая или заменённая запись содержит Argon2id hash и AES-GCM ciphertext. Список возвращает metadata и `can_reveal`, а явные раскрытие/замена аудируются; secret не попадает в list и telemetry. Legacy hash-only запись нельзя раскрыть, но её старое URL-safe либо структурированное значение остаётся валидным до замены/удаления. Первый успешный LMS-вход с токеном создаёт one-to-one grant; token и principal нельзя перепривязать, пока токен не удалён, а замена только secret сохраняет существующий grant.
6. Глобальная роль выводится только из teacher-token grant: grant есть — `TEACHER`, нет — `STUDENT`. Moodle role, controls и roster metadata не повышают роль. Удаление токена немедленно удаляет grant, деактивирует связанные `TEACHER` memberships и возвращает student default.
7. Roster и groups читаются постранично и с жёсткими пределами. Неполный roster не
   используется как доказательство удаления отсутствующих студентов.
8. При каждом следующем входе backend передаёт browser-worker только ID курсов из глобального каталога. Moodle подтверждает enrolment/groups отдельно для каждой такой страницы; остальные курсы Moodle dashboard не запрашиваются, не создаются и не показываются. Для каждого допущенного membership роль материализуется из одной глобальной teacher-token binding.
9. Удаление курса из каталога — логическое: все активные memberships курса
   выключаются, но работы, сдачи, история и аудит не удаляются. Повторное
   подтверждение ссылки может вернуть курс в каталог.
10. Поддерживаемые однозначно классифицированные activities автоматически
    создаются как выключенные локальные проекции. Преподаватель выбирает свои
    Moodle-группы и включает проекцию; редактирования Moodle-полей и отдельной
    публикации версии задания нет. Неизвестный answer transport не мешает
    локальному включению, но его диагностика сохраняется и внешняя доставка
    остаётся fail-closed до успешной проверки фактической student form.

Moodle остаётся owner для identity, enrollment, groups, sections, названия и
условия activity, open/due/close, максимального балла и Quiz attempts. Локальная
система владеет только фактом включения оболочки для выбранных Moodle-групп,
состоянием IDE, paste/AI policy, историей, запуском и проверкой. Эти локальные
свойства не подменяют и не переписывают поля Moodle activity.

## 7. Контракт синхронизации решения

### 7.1. Локальный canonical state

Во время работы canonical рабочее состояние находится в
«Мехмат.Практикум»:

- текущие файлы;
- immutable edit events и hash chain;
- snapshots/checkpoints;
- результаты компиляции и запуска;
- compiler diagnostics;
- данные для последующего анализа авторства.

Компилятор, runner, IDE и полная история изменений **не переносятся в Moodle**.
Moodle получает только материал ответа, необходимый для штатной попытки.

### 7.2. Форматы Moodle response

Принятый формат доставки:

- `ESSAY_ONLINE_TEXT` и `ASSIGN_ONLINE_TEXT` — UTF-8 текст единственного C/C++
  translation unit записывается в доказанный text control; workspace может
  содержать вспомогательные `.txt`, но transport их не передаёт;
- `ESSAY_ATTACHMENT` и `ASSIGN_FILE`, ровно один source-файл — его
  точные UTF-8 bytes передаются как `main.c` или `main.cpp`;
- `ESSAY_ATTACHMENT` и `ASSIGN_FILE`, два и более файлов — один
  детерминированный `submission.zip`. В архив входят C/C++ source
  (`.c/.cc/.cpp/.cxx`), headers (`.h/.hh/.hpp/.hxx`), `.inc` и `.txt`;
  безопасные relative POSIX paths сохраняются, entries сортируются,
  timestamps и права фиксированы, traversal/absolute paths/symlinks
  запрещены. История, manifest, compiler artifacts и созданные при
  запуске runtime-файлы в архив не входят;
- при `attachment-only`, как у проверенной самостоятельной cmid `30354`,
  connector не пытается найти или создать отсутствующий textarea.

Результат helper содержит filename, raw bytes, SHA-256 и размер. Raw artifact
ограничен 4 MiB; base64 делает transport, а весь внутренний request ограничен
6 MiB. Нарушение размера, структуры или hash даёт protocol error до mutation.

Перед загрузкой повторно проверяются course id, cmid, module, attempt/slot,
response type и capabilities. Dynamic `sesskey`, `sequencecheck`, draft item id
и имена controls читаются из текущей страницы, а не сохраняются как
постоянные селекторы. Для Assignment импорт настроек подтверждает `maxfiles`,
accepted file types и team-submission policy; текущая student form заново
доказывает transport и конкретный effective `maxbytes` (включая наследуемый
лимит). Перед overwrite browser-worker скачивает единственный same-origin
attachment из текущего file manager и сверяет SHA-256 с durable receipt. Live
изменение `maxfiles`/accepted types между course sync и отправкой дополнительно
проверит сам Moodle uploader; этот TOCTOU-сценарий входит в staging acceptance.

### 7.3. Draft/checkpoint и final submit

Промежуточная синхронизация обновляет только draft/current answer:

1. Core создаёт локальный snapshot по cadence задания.
2. Для одной principal session остаётся только самая новая ожидающая версия;
   устаревшие checkpoint jobs coalesce, чтобы не переполнять один worker.
3. Browser-worker открывает существующую попытку, заменяет connector-owned
   file/attachment либо online text согласно заново доказанному
   transport и сохраняет ответ через штатный Quiz или Assignment flow. Для
   существующего Assignment attachment совпадения имени недостаточно: до
   mutation проверяется exact-byte SHA-256 удалённого файла.
4. Worker возвращает безопасный receipt/hash и обновлённый `storage_state`.
5. Операция **не выполняет** final-submit/confirmation текущего module.

Для outbox reasons `PERIODIC` и `FINAL_MINUTE` ожидаемый receipt status —
`DRAFT_SAVED`. Любой другой статус считается несогласованным ответом и
отклоняется.

Finalization допускается только в двух случаях:

- студент нажал Finish в «Мехмат.Практикуме»;
- deadline worker зафиксировал окончание разрешённого времени.

Перед finalization выполняется принудительный checkpoint последней revision.
Затем connector выполняет доказанную штатную последовательность final
submit/confirmation для Quiz или Assignment. Для reasons `SUBMISSION` и
`DEADLINE` ожидаемый статус — `FINALIZED`.
Повторы используют стабильный idempotency key, но cache browser-worker
process-local. После его restart ambiguous final response не имеет доказанной
exactly-once гарантии: activity policy и повтор должны пройти staging-проверку,
а создание новой попытки при retry считается ошибкой. После локального deadline
редактор блокируется независимо от доступности Moodle.

Для задания длительностью `D` локальный scheduler сохраняет по `D/10`, в
финальной части — по `D/20`, при Finish и менее чем за минуту до конца. Из-за
одного browser-worker Moodle delivery должна coalesce повторения и отдавать
приоритет final-minute/finalize jobs. При outage локальный snapshot не теряется,
а UI показывает pending/failed sync; нагрузочная приёмка общего deadline для
реального числа студентов обязательна до экзамена.

Этот flow реализован backend outbox и browser-worker. Локальный snapshot остаётся
authoritative до и после внешней доставки: ошибка Moodle не откатывает принятую
revision, а bounded retry повторяет тот же artifact/idempotency intent. Это не
означает live production validation: upload/finalize, session expiry и общая
очередь должны пройти отдельные staging и нагрузочные тесты.

### 7.4. Read-only импорт исторических сдач

После LMS sync core ставит отдельный `moodle.history.import` job для каждой
связанной `mod_quiz`/`mod_assign` activity. Browser-worker проверяет текущую
teacher identity и course scope, постранично читает Quiz overview/review либо
стандартные Assignment grading/grader pages и возвращает bounded набор финальных
ответов. Sync worker материализует финальный код или доступные вложения, оценку
и комментарий как локальные submission/snapshot/review records с явным
источником Moodle.

Одна Quiz attempt может содержать несколько независимых вопросов Essay. В этом
случае каждый `response_id`/slot материализуется как отдельная локальная работа,
версия условия и сдача со своей оценкой и комментарием. Несколько вложений или
файлов ZIP/7z внутри одного Essay остаются многофайловым решением одного задания.
Stable mapping связывает дочернюю работу одновременно с parent attempt и
`response_id`; повторный sync обновляет только изменившийся ответ. Импорты,
созданные старой aggregate-схемой, детерминированно переносятся в первый Essay,
поэтому после обновления рядом не остаётся третьей объединённой сдачи.

Во внешней модели это одна попытка студента. Поэтому очередь проверки группирует
дочерние Essay по точному набору `(course, parent assessment, parent attempt,
student)` и считает его одной работой. Внутри проверки отображаются нумерованные
задания, между которыми преподаватель переключается без возврата в очередь;
балл, комментарий, закрепление и повторная проверка остаются независимыми для
каждого вопроса. Повторные попытки и ответы разных студентов не смешиваются.

Review Moodle может быть разбит на `page=N`. Коннектор предпочитает проверенную
same-origin ссылку `showall=1`, иначе обходит все доступные страницы (не более
64), проверяя course/cmid/attempt/student на каждой из них. Только полный обход
помечается `responses_complete=true`; при частичной загрузке ранее найденные
задания не скрываются.

Повтор безопасен: stable external submission ID и external revision обновляют
существующий импорт, а не создают дубль. Импорт read-only и не меняет Moodle;
локальная последующая перепроверка не затирается повторно полученной
исторической оценкой. Moodle не содержит событий редактора
«Мехмат.Практикума», clipboard receipts и промежуточной hash chain, поэтому эти
данные не реконструируются. В истории сдачи показывается «Импортировано из
Moodle» и пояснение об отсутствии истории редактирования.

Материализация исходников имеет версию `5`. При первом успешном sync после
обновления mapping старой версии принудительно перечитывается даже при прежнем
`external_revision`, создаёт один новый immutable import snapshot и получает
маркер v5; дальнейшие sync снова идемпотентны. Вложения скачиваются через
HTTP-контекст Playwright с cookies текущей Moodle-сессии, как в прежнем
Selenium-коннекторе; page-level `fetch` остаётся только совместимым fallback.
Quiz Essay и Assignment
online-text читаются через браузерный `innerText`, чтобы сохранить табуляцию,
пробелы и пустые строки. Assignment online text и его attachments не
взаимоисключаются и материализуются одновременно. Подходящие ZIP и 7z
распаковываются в bounded memory с сохранением safe relative paths; исходники,
заголовочные и `.txt`-файлы сохраняются. Entries с absolute/path traversal,
special/link metadata, недопустимым типом или превышенным лимитом не
извлекаются. Ошибка 7z отображается отдельным диагностическим файлом вместо
неинформативного `moodle-import.txt`.

Парсеры защищены exact-origin, ограничениями страниц/элементов/размера и
sanitized fixtures. Это не заменяет эксплуатационную проверку фактической MMCS
Assignment grading/review разметки: до неё нельзя обещать полноту импорта всех
лабораторных и вложений на реальном сервере.

## 8. Проверка и оценки: реализованный transport, staging gate открыт

Live inspection подтвердил преподавательские страницы:

- Assignment grading для лабораторных;
- Quiz overview report для самостоятельной;
- review links к отдельным attempts/questions.

Реализованный flow использует identifiers, полученные историческим импортом:

- Assignment: `course_id`, `cmid`, `user_id` и при наличии номер попытки;
- Quiz Essay: `course_id`, `cmid`, `user_id`, `attempt_id` и `question_slot`.

Browser-worker повторно проверяет exact origin, course context, URL и скрытые
идентификаторы формы, находит ровно один grade control, comment editor и save
control, затем выполняет штатное сохранение. Backend доставляет mutation через
durable outbox с устойчивым idempotency key. К комментарию один раз добавляется
подпись проверяющего вида `Фамилия И.`; исторический parser отделяет такую
подпись от редактируемого текста, поэтому повторный import/export её не
дублирует.

Открытыми остаются эксплуатационные gates:

1. отдельный staging-тест записи для Assignment и Quiz Essay на специально
   созданных попытках;
2. независимый read-back сохранённых mark/comment и обнаружение параллельной
   ручной правки;
3. проверка нестандартных шкал/форм feedback;
4. аудит без текста студенческого решения и cookies;
5. нагрузочный тест ограниченной очереди.

До успешного receipt локальное решение остаётся authoritative и доступно для
повторной доставки. Неоднозначная разметка не превращается в ручное
предположение: operation завершается ошибкой, не меняя другой attempt/slot.

Опциональный `local_programming_bridge` остаётся возможным будущим enhanced
mode, если администраторы Moodle разрешат установку. Он не является скрытым
fallback текущего pluginless connection.

## 9. Источники истины

| Данные | Owner | Направление |
| --- | --- | --- |
| Identity, enrollment и groups | Moodle | read-only projection только в scope глобального каталога |
| Глобальная роль `STUDENT|TEACHER` | локальная система | `STUDENT` по умолчанию; `TEACHER` только по активному one-to-one teacher-token grant |
| Глобальный каталог курсов | локальная система | `SYSTEM_SETTINGS` allow-list; ограничивает login/sync/authorization scope |
| Sections, activities, cmid, название, условие, сроки, шкала и число попыток | Moodle | read-only projection; повторный sync обновляет локальную копию |
| Включение работы в Практикуме и выбранные Moodle-группы | локальная система | локальная visibility policy; Moodle activity не изменяется |
| Пароль Moodle | нигде | только transient login input |
| Browser session | Moodle, encrypted copy в core | обновляется после browser operation |
| Настройки IDE/paste/AI/hidden tests | локальная система | в Moodle не выгружаются |
| Исходники текущего ответа | локальная система | async answer mirror через outbox: Quiz Essay `ESSAY_ATTACHMENT`/`ESSAY_ONLINE_TEXT` и Assignment `ASSIGN_FILE`/`ASSIGN_ONLINE_TEXT` |
| Edit history, snapshots, compiler output | локальная система | в Moodle не выгружаются |
| Исторический финальный код/файлы, оценки и комментарии | Moodle | read-only постраничный идемпотентный импорт Quiz Essay/Assignment после LMS sync; без истории набора |
| Final attempt state | Moodle после finalize | только Finish/deadline |
| Решение преподавателя | локальная система | сохраняется локально и доставляется в штатную форму конкретного Moodle attempt/slot |
| Official gradebook | Moodle | read-back/conflict engine ещё не готов |

«Синхронизация» здесь не означает last-write-wins. При сомнении mutation
останавливается, локальные данные сохраняются, пользователю показывается явный
статус и требуется повторная аутентификация либо действие преподавателя.

## 10. Эксплуатационные требования

- browser-service: один Uvicorn worker и один Chromium;
- default concurrency: одна browser operation;
- сервис доступен только backend по внутренней сети;
- readiness проверяет запущенный и подключённый Chromium;
- browser busy, navigation timeout и Moodle 5xx не должны перезапускать весь
  сайт;
- cookies, `storage_state`, password, `sesskey`, request bodies и страницы с
  ответами студентов не логируются;
- safe diagnostics содержат только категорию ошибки или HTTP status;
- encryption key и БД резервируются совместно;
- после изменения Moodle theme/version parsers прогоняются на sanitized
  fixtures и staging account;
- grade mutation и Quiz/Assignment answer submit реализованы, но не должны считаться
  production-ready до отдельного staging opt-in, write/read-back и приёмки;
- exam rollout требует измерить throughput общей очереди на целевом N150.

## 11. Приёмка Playwright pluginless

1. Верный Moodle login создаёт app session; неверный возвращает generic 401 без
   раскрытия upstream HTML.
2. Пароль отсутствует в БД, logs, audit, traces и browser storage приложения.
3. `storage_state` зашифрован, ограничен exact origin и переживает restart;
   tamper/wrong key fail closed.
4. URL другого origin/path/userinfo/fragment и redirect наружу отклоняются.
5. Одновременно существует не более настроенного числа browser operations;
   переполненная очередь завершается bounded ошибкой.
6. Course 549 импортирует sections 0–11 и сохраняет module type/cmid, включая
   Quiz cmid `30354`.
7. Для cmid `30354` capability определяется как Essay attachment-only; connector
   не требует textarea.
8. Однофайловый source и многофайловый ZIP проходят staging upload и hash
   read-back в `ESSAY_ATTACHMENT`; однофайловый UTF-8 source проходит
   save/read-back в `ESSAY_ONLINE_TEXT`.
9. В тестовом Assignment фактическая student form доказывает
   `ASSIGN_FILE` и/или `ASSIGN_ONLINE_TEXT`; при обоих выбирается file.
   Ровно один source читается обратно как `main.c`/`main.cpp`; source +
   любой `.txt` или header, а также любой другой многофайловый набор —
   как `submission.zip` с точными relative paths.
10. Промежуточный checkpoint не выполняет final submit.
11. Finish и deadline сначала доставляют последнюю revision и только затем
    завершают попытку; повтор в живом worker использует idempotency cache, а
    ambiguous retry после restart отдельно подтверждён без создания новой
    попытки до production.
12. Outage Moodle не уничтожает локальные files/history/snapshot.
13. Assignment и Quiz Essay grade/comment проходят staging write/read-back;
    повтор outbox не создаёт второй комментарий/оценку, а несовпадение
    target/form завершается fail-closed.
14. Пиковая очередь общего дедлайна проверена на целевом N150 до допуска к
    контрольной или экзамену.
15. Любая Moodle role/control markup без teacher-token grant оставляет principal в роли `STUDENT`; свободный токен атомарно привязывается ровно к одному external principal.
16. Удаление teacher token немедленно отзывает grant и teacher capabilities, но не удаляет identity, submissions, history и audit.
17. После LMS sync исторические Quiz Essay submissions постранично появляются в
    очереди без дублей и с явной пометкой отсутствия истории набора.
18. Импорт стандартного Assignment проверен на sanitized fixtures; фактическая
    MMCS grading/review разметка проходит отдельную эксплуатационную приёмку.
19. Assignment student-answer save/finalize проходит отдельную live
    приёмку на целевой MMCS Moodle до допуска к пилоту; fixture-тесты
    доказывают fail-closed parser contract, но не production compatibility темы.

## 12. Официальные справочные материалы

- [Playwright authentication/storage state](https://playwright.dev/python/docs/auth)
- [Playwright browser contexts](https://playwright.dev/python/docs/browser-contexts)
- [Moodle Quiz activity](https://docs.moodle.org/402/en/Quiz_activity)
- [Moodle Essay question type](https://docs.moodle.org/402/en/Essay_question_type)
- [Moodle External Services](https://moodledev.io/docs/5.0/apis/subsystems/external)
- [1EdTech LTI Advantage guide](https://standards.1edtech.org/lti/guides/implementation_guide/implementation-guide)
