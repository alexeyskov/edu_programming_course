# Moodle Programming Bridge 0.3

`local_programming_bridge` — первый Moodle-адаптер «Мехмат.Практикум». Он не получает
пароли Moodle, не экспортирует пользовательские cookies и не создаёт локальных
пользователей платформы. Текущая release в `version.php` — `0.3.0`, maturity —
`MATURITY_ALPHA`, compatibility floor — Moodle 4.2.2 (`2023042402`).

Репозиторий не подтверждает production-совместимость: plugin нужно установить и
прогнать на staging-копии фактической университетской Moodle и целевой
поддерживаемой ветки. В среде разработки проекта не было полноценного Moodle/PHP
runtime. Исторический аудит видел косвенные признаки Moodle 4.2.2, но текущая
версия сайта не подтверждена; `requires=2023042402` — compatibility floor
plugin, а не факт о deployment. Если фактическая LMS всё ещё на 4.2, её ветка
больше не получает security fixes и rollout должен включать upgrade либо
формальное принятие риска.

## Реализованный контракт 0.3

| Web-service function | Назначение |
| --- | --- |
| `local_programming_bridge_get_course_snapshot` | course, sections, activities; для `mod_assign` также open/due/cutoff/max grade |
| `local_programming_bridge_get_membership_snapshot` | active enrolments, groups и проекция только `STUDENT`/`TEACHER` |
| `local_programming_bridge_push_grade` | идемпотентная оценка и plain-text feedback в явно сопоставленный `mod_assign` |
| `local_programming_bridge_store_checkpoint` | идемпотентное хранение полного recovery manifest до 4 MiB |
| `local_programming_bridge_get_latest_checkpoint` | последняя проверенная recovery-точка попытки |
| `local_programming_bridge_upsert_task_definition` | идемпотентный connector-owned mirror неизменяемой версии задания |
| `local_programming_bridge_get_task_bank_snapshot` | снимок connector-owned task mirror для сверки/восстановления |

Standalone delegated login выполняет
`/local/programming_bridge/launch.php`. После обычного `require_login()` Moodle
показывает явное подтверждение с `sesskey`, формирует короткоживущее
HMAC-утверждение и отправляет его на exact platform callback. Backend проверяет
issuer, audience, state, browser verifier cookie, expiry и одноразовый nonce.
Для прямого launch без platform state/verifier backend дополнительно сверяет
browser `Origin` либо `Referer` с exact origin этого Moodle connection; proxy не
должен удалять/переписывать header, иначе ответ будет
`403 INVALID_LAUNCH_ORIGIN`. Stateful login, начатый платформой, использует
state+verifier и этой дополнительной проверки не требует.
Это узкий bridge protocol, а не LTI 1.3 и не экспорт Moodle-сессии.

## Семантика синхронизации

### Course и сроки

Course snapshot возвращает все видимые Moodle activities. Backend хранит
ограниченную проекцию и обновляет сроки только у локальной работы с уже
существующим явным mapping `Assessment -> mod_assign`. В UI преподаватель
выбирает Assignment из обнаруженного списка, а не вводит произвольный URL.

- `cutoffdate` имеет приоритет над `duedate`;
- `allowsubmissionsfromdate` проецируется как открытие;
- если mapping исчез из нового Moodle snapshot, он помечается
  `MISSING_IN_MOODLE`, а последние локальные сроки не стираются автоматически;
- plugin не создаёт и не редактирует Moodle activity.

### Оценка и комментарий

`push_grade` принимает только положительный `cmid`, который реально разрешается
в `mod_assign` указанного курса. Проверяются enrolment студента и диапазон
Assignment grade. Повтор с тем же idempotency key и payload возвращает прежний
receipt; повтор с другим payload отклоняется. Native Quiz/per-question grading и
обратное чтение официальной оценки не входят в 0.3.

Backend разрешает mapping и отправку решения только если `grade_max` Moodle —
конечное положительное число и в точности совпадает с
`Assessment.max_score`. Периодическая сверка помечает нарушенный mapping как
`UNSUPPORTED_MOODLE_GRADING` либо `GRADE_RANGE_MISMATCH`; grade outbox в таком
состоянии блокируется, а не масштабирует балл неявно.

### Checkpoint и внешний anchor истории

`store_checkpoint` принимает UTF-8 JSON полного ordered file manifest, его
SHA-256, opaque attempt/snapshot references, причину, epoch, workspace revision
и `eventchainhead`. Размер manifest ограничен 4 MiB. Канонические байты создаёт
Python backend: PHP не сериализует JSON повторно, потому что допустимые spelling
floating-point значений могут различаться между runtime. Plugin проверяет, что
JSON валиден, и считает SHA-256 именно от принятых байтов до записи;
`get_latest_checkpoint` повторяет проверку перед выдачей.

Backend также сверяет recovery envelope, file hashes, course/user/attempt,
snapshot hash, epoch, revision и 64-символьный `event_chain_head`. Поэтому Moodle
хранит внешний anchor состояния истории, но он не является криптографической
подписью университета и сам по себе не доказывает авторство.

### Task bank mirror

После публикации/архивации course-scoped версии backend отправляет созданные им
канонические UTF-8 байты определения через transactional outbox. Plugin
валидирует JSON и SHA-256 точных байтов, не выполняя межъязыковую
re-serialization. Ключ `(courseid, taskref, versionnum)` неизменяем: другой
content/definition hash отклоняется. После receipt backend сохраняет
`ExternalMapping` на запись plugin.

Это отдельная таблица `local_prgbridge_task`, а не native Moodle question bank.
0.3 не создаёт категории/questions/versions Quiz, random pools, Assignment или
LTI resources. Полноценный native two-way editor, conflict resolution и LTI
1.3/Deep Linking остаются последующей фазой.

## Установка

1. Скопируйте каталог в `<moodle-root>/local/programming_bridge`.
2. Создайте backup и выполните штатный Moodle upgrade. Для обновления с ранней
   версией `db/upgrade.php` добавляет checkpoint anchors и task mirror table.
3. В site administration задайте exact HTTPS `platformurl` без query/fragment и
   общий secret не короче 32 байт. Значение должно совпасть с
   `MOODLE_LAUNCH_SHARED_SECRET` backend.
4. Включите predefined external service `programming_bridge`.
5. Создайте отдельного restricted web-service user и token, ограничьте его
   нужными курсами и IP/network policy, если это поддерживает эксплуатация.
6. Выдайте минимальные course capabilities и очистите Moodle caches после их
   изменения.

Backend использует token только server-to-server через
`/webservice/rest/server.php`; frontend и runner его не получают.

## Capabilities

- `local/programming_bridge:use` — launch и course snapshot; доступен стандартным
  student/teacher/editingteacher/manager archetypes;
- `moodle/course:viewparticipants` — membership snapshot;
- `local/programming_bridge:pushgrade` — экспорт оценки; по умолчанию
  editingteacher/manager, для service user назначается явно;
- `local/programming_bridge:storecheckpoint` — запись и чтение checkpoint;
- `local/programming_bridge:synctaskbank` — запись/чтение task mirror.

Последние две capabilities намеренно не выдаются student/teacher archetypes;
назначайте их только restricted service account. Task definition может содержать
hidden-test metadata, а checkpoint — исходный код студента.

## Таблицы, backup и privacy

- `local_prgbridge_receipt` — idempotency receipts;
- `local_prgbridge_checkpoint` — recovery manifests и history anchors;
- `local_prgbridge_task` — connector-owned immutable task mirrors.

Полный site/database backup Moodle захватывает эти таблицы. Однако plugin 0.3 не
содержит `backup2` steps для course backup/restore, поэтому нельзя считать, что
обычный backup/restore отдельного курса переносит записи или корректно
перепривязывает IDs. Реализованный privacy provider экспортирует и удаляет
пользовательские строки `local_prgbridge_checkpoint`; receipt/task rows не имеют
прямого `userid` и этим provider не экспортируются. Оба поведения нужно отдельно
проверить на staging и включить в утверждённую политику хранения.

В plugin 0.3 нет cron-задачи автоматического удаления; оператор не должен
считать поле `retention_days` платформы реализованной политикой удаления.

## Обязательная staging-проверка

- clean install и upgrade с предыдущих revisions на реальной Moodle;
- delegated login для student/teacher, state/verifier/nonce/replay negatives и
  direct-launch exact `Origin`/`Referer` checks;
- course/roster/groups и suspend/unenrol reconciliation;
- projection Assignment open/due/cutoff и исчезновение mapped activity;
- positive numeric `grade_max`, exact `max_score` match, blocked mismatch,
  grade/comment bounds, idempotent retry и service-user least privilege;
- checkpoint 4 MiB boundary, invalid JSON, exact-byte SHA/anchor corruption и
  recovery;
- task publish/archive mirror, immutable-conflict и read-back snapshot;
- site backup, course backup/restore limitation и фактический privacy behavior;
- проверка Moodle cron/logging, PHP warnings и нагрузки на тестовом курсе.

До прохождения этого набора plugin остаётся alpha и не должен обслуживать
контрольные или экзамены.

Минимальные локальные syntax-проверки, если установлены `xmllint` и PHP CLI:

```bash
xmllint --noout db/install.xml
find . -name '*.php' -exec php -l {} \;
```

Они не загружают Moodle API и поэтому не подтверждают корректность external
parameters/returns, capabilities, XMLDB upgrade или `grade_update`.
