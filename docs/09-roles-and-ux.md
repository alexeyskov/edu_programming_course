# LMS-identity, глобальные роли, административное повышение и UX

> Статус: identity/role model, bridge login, admin elevation, course/assessment,
> IDE, закрепление работы за проверяющим, hidden-test evidence и system-settings screens реализованы. Wireframes и полные
> capability lists ниже остаются целевой UX; baseline не имеет LTI/WebSocket,
> native Moodle bank UI, automatic retention и части advanced settings.

## 1. Зафиксированная модель доступа

В прикладном интерфейсе существуют только две роли:

- `STUDENT` — глобальная роль по умолчанию;
- `TEACHER` — глобальная роль principal с активной one-to-one привязкой teacher token.

LMS-коннектор возвращает identity, enrolment и groups. Он не назначает прикладную роль: Moodle role/control markup не повышает principal до `TEACHER`. Привилегии преподавателя глобальны, но каждое course action дополнительно требует, чтобы курс был в глобальном каталоге, а principal — в актуальном LMS-enrolment/group scope.

Локальная регистрация отсутствует. Проекция пользователя создаётся после успешной внешней аутентификации и не содержит пароля. Экранов общего ручного управления учётными записями/ролями нет; пул teacher tokens — единственный узкий механизм повышения до глобального `TEACHER`.

### 1.1. Преподавательский токен — отзываемая глобальная привязка

- Токен создаёт principal с активной `SYSTEM_SETTINGS` elevation. Новое значение
  имеет ровно восемь ASCII-букв/цифр; в БД остаются public ID, label, Argon2id
  hash и AES-GCM ciphertext. Список показывает только metadata и `can_reveal`.
- Администратор может явно раскрыть сохранённый secret либо заменить его другим
  восьмизначным ASCII alphanumeric значением. Раскрытие не кэшируется, оба
  действия аудируются без secret material. У legacy hash-only записей просмотр
  недоступен, но известный старый токен работает до замены/удаления.
- Первый успешный LMS-вход с токеном атомарно привязывает его к внешнему principal. После этого повторно вводить токен не нужно.
- Токен и principal связаны one-to-one. Попытка привязать занятый токен к другому principal или второй токен к тому же principal отклоняется.
- Удаление токена администратором немедленно отзывает grant и teacher capabilities, но не удаляет identity и учебную историю. До новой валидной привязки действует `STUDENT`.

### 1.2. Административное повышение — не роль

Необязательное поле «Токен администратора» на входе включает capability `SYSTEM_SETTINGS` только после успешной аутентификации через LMS. Повышение:

- принадлежит конкретной server-side session и внешнему actor;
- ограничено по времени и отзывается при logout либо явном снятии elevation;
  смена client network prefix (`/24` IPv4, `/64` IPv6) снимает только elevation,
  не LMS-сессию; global revoke/rotation API ещё нет;
- открывает реализованные глобальные feature/policy settings и health; connector
  CRUD, token rotation и полный operations dashboard ещё target;
- не превращает студента в преподавателя;
- даёт глобальный read-only доступ к локальным сдачам, уже сохранённым решениям
  и связанным результатам integrity/evidence для аудита, но не даёт права
  закреплять работу за собой, запускать новые проверки, менять оценку или
  редактировать объекты курса;
- логируется по key version и не сохраняется backend/БД. После успешной
  проверки браузер по явной продуктовой политике хранит raw-токен origin-scoped
  в `localStorage`, чтобы скрыто подставлять его при следующем входе и step-up;
  на общем устройстве пользователь обязан применить «Забыть сохранённые токены».

Машинные service credentials не отображаются как роли и работают только через узкие integration scopes.

## 2. Серверные capabilities

Роли просты, но проверки остаются объектными:

- `course.view`, `course.import`, `course.sync`, `course.configure_wrapper`;
- `membership.view_external`, `availability.configure`;
- `task.view_student`, `task.manage`, `task.publish`;
- `assessment.view`, `assessment.manage`, `attempt.start`, `attempt.write`, `attempt.submit`, `attempt.reopen`;
- `submission.view_own`, `submission.view_course`, `submission.view_history`;
- `review.claim`, `review.decide`, `review.override_claim`;
- `evidence.run`, `evidence.view_hidden`;
- `ai.student_use`, `ai.teacher_use`, `ai.configure_course`;
- `plagiarism.review`, `authorship.review`;
- `system.settings`, `integration.configure`, `audit.view_system`.

Правила вывода capabilities:

- `STUDENT` получает только собственные назначенные работы и попытки;
- `TEACHER` получает управление и проверку только при активном teacher-token grant и в course/group scope, подтверждённом пересечением каталога и LMS-enrolment/groups;
- `SYSTEM_SETTINGS` получается из административного повышения и не подменяет
  глобальную роль или course scope для мутаций; отдельный реализованный
  review-audit policy разрешает с ним глобальное чтение сдач и сохранённых
  результатов;
- local availability assignment может сузить видимость студенту, но не расширить внешний enrolment;
- критические проверки выполняются backend, а скрытие кнопки во frontend — только UX.

## 3. Матрица действий

| Действие | `STUDENT` | `TEACHER` в допущенном course scope | Сессия с `SYSTEM_SETTINGS` |
| --- | --- | --- | --- |
| Писать active attempt | только свою | только student preview, без сдачи | не добавляет право |
| Смотреть submission и историю решений | только свою в разрешённом режиме | студентов назначенного course/group scope | все локальные сдачи read-only |
| Видеть сохранённые review evidence | нет | да, в scope видимой сдачи | да, read-only; новый запуск запрещён без обычного teacher scope |
| Видеть authoring hidden tests/reference | нет | да, в review/task scope | только если actor также `TEACHER` нужного курса |
| Запустить official hidden-test report | нет | да, когда работа закреплена за преподавателем и включена СППР | capability сама права не добавляет |
| Закрепить/освободить работу для проверки | нет | да | не добавляет право |
| Принять финальное решение | нет | да | не добавляет право |
| Редактировать task/assessment | нет | да в своём курсе | не добавляет право |
| Добавить курс в глобальный каталог по ссылке | нет | нет | да; connection уже должен быть provisioned, connector валидирует exact URL и доступность данных |
| Создать/просмотреть/заменить/удалить teacher token | нет | нет | да; list не содержит secret, явное раскрытие/замена аудируются, delete немедленно отзывает grant |
| Настроить видимость студенту/группе | нет | да в своём курсе | не добавляет право |
| Изменить глобальные AI/runner/retention bounds | нет | нет | да |
| Убрать курс из глобального каталога | нет | нет | да; история сохраняется, memberships выключаются |
| Создать/изменить LMS connection | нет | нет | baseline только deployment CLI; UI/API target |
| Смотреть system audit/health/outbox | нет | course-limited outbox API | health/outbox API; audit export target |
| Создать пользователя/роль/локальный курс | отсутствует | отсутствует | отсутствует |

Глобальное чтение через `SYSTEM_SETTINGS` ограничено review-аудитом. Оно не
создаёт `can_review`, не позволяет закрепить работу или перепроверить её и не
открывает общее редактирование курса/банка заданий.

## 4. Вход

### 4.1. Экран входа

```text
┌─────────────────────────────────────────────┐
│ Система курса программирования              │
│ Внешняя система: Moodle MMCS                │
│                                             │
│ Токен преподавателя (для первой       │
│ привязки, необязательно)          [••••] │
│ Токен администратора (необязательно) [••••] │
│ [ Войти через Moodle ]                      │
│                                             │
│ Регистрация и восстановление пароля —       │
│ на стороне Moodle                           │
└─────────────────────────────────────────────┘
```

Оба поля токенов имеют `type=password` и `autocomplete=off`. После успешной
проверки их значения сохраняются только origin-scoped в browser `localStorage`
и при следующем входе скрыто подставляются обратно. Экран входа предоставляет
действие «Забыть сохранённые токены». На общем компьютере его нужно использовать
после завершения работы. Moodle-пароль по-прежнему очищается после POST и
никогда не сохраняется приложением. Преподавательский grant остаётся
server-side, однако сохранённый токен можно повторно передавать той же учётной
записи. Ошибки валидации не раскрывают secret hash. После возврата из Moodle
пользователь видит внешнее имя, provider, глобальную роль и course contexts из
catalog∩enrolment.

Если срок `SYSTEM_SETTINGS` истёк во время открытой страницы, 403
`CAPABILITY_REQUIRED` переводит интерфейс в локальный step-up экран. Пользователь
повторно вводит административный токен (сохранённое значение уже находится в
password-поле), получает новое elevation и продолжает работу без повторного
Moodle login.

### 4.2. Вход из Moodle

В baseline standalone login идёт через
`local/programming_bridge/launch.php`: Moodle `require_login`, явное `sesskey`
confirmation и короткое HMAC assertion возвращаются в FastAPI callback. LTI
External Tool launch — future. Повторный bridge launch использует ту же external
identity. После отзыва membership student workspace writes и manual submit
запрещаются, а уже сохранённые audit/submission records не удаляются.

### 4.3. Выход

Logout закрывает local session и administrative elevation. Baseline не имеет
WebSocket/IndexedDB session state и не выполняет Moodle single logout; внешняя
Moodle-сессия остаётся активной.

## 5. Добавление внешнего курса

Вход в bootstrap-flow доступен только principal с активной `SYSTEM_SETTINGS`. Connector валидирует exact origin/course ID и читает только доступные авторизованной LMS-сессии данные; ни Moodle role, ни elevation не подменяют обычные course permissions.

1. Нажать «Добавить курс» и вставить URL, например `https://edu.mmcs.sfedu.ru/course/view.php?id=549`.
2. Увидеть распознанные provider/course ID и результат проверки exact URL/доступности данных.
3. Baseline выполняет bounded discovery HTTP в request/import flow; отдельного
   progress job UI пока нет.
4. Просмотреть секции, группы, участников, activities и сроки. Native Moodle
   question bank не импортируется. Поддерживаемые Assignment/Quiz из разделов
   лабораторных, самостоятельных, контрольных и экзамена создают выключенные
   локальные проекции работ.
5. Увидеть для каждого свойства статус `SUPPORTED`, `PARTIAL`, `UNSUPPORTED` или `CONFLICT`.
6. Открыть read-only Moodle-название, условие, сроки, шкалу и число попыток.
   Исправления этих полей выполняются в Moodle и приходят при следующем sync.
7. Выбрать одну или несколько назначенных преподавателю Moodle-групп и включить
   работу. Обладатель `SYSTEM_SETTINGS` может выбрать любые группы курса.
8. После включения работа видна только студентам выбранных групп; повторное
   подтверждение того же экрана атомарно заменяет набор групп.

URL — locator. UI не обещает «проанализировать любую страницу»: неизвестный
origin, отсутствие авторизованного доступа к требуемым данным или неподдерживаемый
connector дают конкретную ошибку. Повторное добавление того же stable course ID
предлагает открыть существующую проекцию, а не создаёт дубль.

Очередь проверки содержит локальные сдачи и read-only исторические импорты
связанных Moodle Quiz Essay/Assignment. Исторический импорт запускается в фоне
после LMS sync, идёт постранично и идемпотентно, переносит финальный код/файлы,
доступную оценку и комментарий. Такие attempts не выдаются за полноценно
записанные в «Мехмат.Практикум»: у Moodle нет нашей истории набора,
clipboard-свидетельств и промежуточных snapshots, поэтому происхождение и
отсутствие истории явно показаны в UI. Реальная MMCS Assignment разметка требует
эксплуатационной проверки.

Одна Moodle Quiz attempt занимает одну строку очереди даже при нескольких Essay.
Строка показывает число заданий, а экран проверки — горизонтальный нумерованный
переключатель с отдельными баллами и статусами вопросов. Переход сначала
сохраняет изменённый черновик текущего вопроса и закрепляет следующий; при ошибке
текущая страница и её закрепление сохраняются.

### 5.1. Доступность работ

Экран «Доступ» строится по внешним memberships/groups и позволяет:

- включить работу для всего курса;
- включить или исключить внешние группы;
- задать индивидуальное включение/исключение и time accommodation;
- предварительно посмотреть итоговый effective access конкретного студента;
- увидеть stale badge, если roster давно не синхронизировался;
- отправить представимое правило обратно в Moodle или явно оставить его local-only.

Нельзя создать локального студента или вручную добавить его в группу. Исправление состава выполняется во внешней LMS, после чего запускается sync.

## 6. Student information architecture

### 6.1. Главная

- доступные внешние курсы;
- ближайшие локально разрешённые работы и deadlines;
- состояние попыток и LMS export;
- объявления/инциденты и состояние сервисов.

### 6.2. Страница работы до старта

- тип, доступность, duration и attempts;
- правила вставки, ИИ и сети;
- browser/network self-check;
- C/C++ standard и file mode;
- подтверждение правил и кнопка «Начать»;
- accommodation/override, если он назначен преподавателем.

### 6.3. IDE

```text
┌──────────────┬──────────────────────────────────┬──────────────┐
│ Условие /    │ Файлы и вкладки + Monaco         │ Таймер       │
│ навигация    │                                  │ состояние    │
│ по заданиям  │                                  │ сохранения   │
├──────────────┴──────────────────────────────────┴──────────────┤
│ Build / Run / Tests / stdin / stdout / diagnostics / AI help │
└───────────────────────────────────────────────────────────────┘
```

Baseline всегда показывает save state, acknowledged revision, checkpoint и
remaining time. Текущий момент берётся с устройства и отдельный server-time
offset API отсутствует; authoritative write/deadline всё равно проверяет backend.
Online/offline и snapshot details ограничены, WebSocket presence отсутствует.
Цвет не является единственным каналом. После срока editor read-only.

### 6.4. Завершение

Dialog показывает final acknowledged revision, save/run/checkpoint state и
подтверждает необратимость. Baseline server возвращает submission/receipt ID,
server time и revision; отдельный receipt hash и актуальный LMS export state в
этом ответе отсутствуют.

## 7. Teacher information architecture

### 7.1. Dashboard курса

- sections/assessments и connector sync status;
- количество активных, ожидающих проверки, закреплённых и конфликтных работ;
- warnings runner/AI/LMS;
- readiness ближайшей контрольной/экзамена;
- включённые Moodle-работы, правила доступа групп и настройки оболочки.

Преподаватель имеет глобальный teacher-token grant, но видит только курсы из
catalog∩LMS-enrolment scope и сдачи студентов, с которыми у его активного
membership есть общий активный `CourseGroup`. Для исторических MMCS-данных
поддержан узкий compatibility fallback для точной метки вида
`подгруппа Фамилия И.О.`; частичное совпадение фамилии доступ не выдаёт. Это не
локальное ручное назначение студентов: состав остаётся проекцией Moodle.

### 7.2. Проверка работ

Страница «Проверка» загружает все локальные сдачи, доступные actor по серверному
scope, и имеет три вкладки: «Все сданные», «Ожидают проверки» и «Проверенные».
Поиск фильтрует по студенту, внешней группе и работе. «Открыть следующую» и
«Открыть» атомарно закрепляют за преподавателем только непроверенную доступную
сдачу. Сдачи работ с `review_required=false` остаются в «Все сданные» для
аудита, но не считаются ожидающими проверки и открываются только для чтения;
чужая активная аренда также остаётся видимой, но не редактируемой.

Проверенную работу можно открыть вместе с финальной оценкой, комментарием,
автором и временем решения. Действие «Перепроверить» создаёт новое
закрепление;
следующее утверждение добавляет immutable decision revision с `supersedes_id`,
не стирая предыдущие решения. Сессия только с `SYSTEM_SETTINGS` видит все три
вкладки глобально, однако получает `can_review=false`: ей доступны открытие
результата и история, но не закрепление, перепроверка и изменение оценки.

### 7.3. Review workspace

```text
┌──────────────┬──────────────────────────────┬──────────────────┐
│ Студент/     │ Immutable code + diff/history│ Evidence tabs    │
│ задания      │ teacher sandbox toggle       │ tests/AI/plag.   │
├──────────────┴──────────────────────────────┼──────────────────┤
│ Rubric + draft comment + final grade        │ Teacher AI chat  │
└─────────────────────────────────────────────┴──────────────────┘
```

Проверяющий видит lease owner/expiry, последнее решение и полную историю его
immutable revisions. Teacher sandbox имеет отдельную заметную рамку и никогда
не изменяет сдачу.

В baseline вкладка «Тесты» загружает сохранённые reports и, когда работа
закреплена за текущим преподавателем, `review_required` и включённой СППР,
запускает новый bounded hidden-test
report по immutable submission. Она показывает final/partial status, 1–20
cases, findings, hashes, bounded stdout/stderr previews и подтверждённые
filesystem/network flags. Сохранённые reports остаются read-only видимыми
преподавателю после отключения СППР. UI явно сообщает, что report не заполняет
и не утверждает оценку.

В режиме песочницы преподаватель получает private editable copy exact submission snapshot. Доступны:

- редактирование всех разрешённых файлов и многофайловая сборка;
- Build/Run/stdin/stdout тем же отдельным runner-контейнером;
- Problems + inline markers; клик по compiler diagnostic открывает точный файл/строку/столбец;
- связанные notes, raw build log и optional teacher-only AI explanation;
- diff первого файла «эксперимент ↔ оригинал» в baseline; полноценный
  multi-file diff — следующий этап;
- reset к исходной сдаче и явный soft delete эксперимента;
- предупреждение о stale diagnostics после следующего edit.

Experiment history не смешивается со student history/authorship. Его run не становится официальным evidence и не отправляется в Moodle автоматически.

## 8. Системные настройки по административному токену

После elevation появляется отдельный раздел «Настройки системы»:

В baseline экран/API реально управляют AI/student-AI/runner switches,
runtime CPU/memory, LMS origin list, incident banner и policy value
`retention_days`, глобальным каталогом курсов и пулом teacher tokens; экран показывает ограниченный backend/DB health. Для teacher token видны label, public ID/hash fingerprint, binding, use count, timestamps и доступность раскрытия. Secret отсутствует в списке и появляется только после отдельного действия администратора; его можно заменить ровно восьмизначным ASCII alphanumeric значением. Старые hash-only записи помечаются как нераскрываемые, но остаются действительными. Раскрытие и замена аудируются; удаление требует явного подтверждения и немедленно отзывает привязку. Outbox имеет
отдельный API, audit export endpoint отсутствует. Следующий
расширенный список — target, а не текущее обещание UI:

Непустой `allowed_lms_origins` дополнительно сужает enabled
`LMSConnection` origins для course URL import; пустой список оставляет connector
records авторитетными. Это поле не создаёт подключение.

- LMS connections, origin allow-list и connector capabilities;
- current/next administrative token key version, rotation и revoke без показа секрета;
- global runner profiles и resource ceilings;
- AI provider/model allow-list, kill switch, budgets и data-transfer policy;
- retention, audit export, backup/restore status;
- service health, queues, outbox/inbox и incident banner;
- global feature flags и policy bounds.

Изменение `retention_days` не запускает автоматическое удаление. Token rotation
UI, backup status, provider allow-list/budget и полный queue dashboard требуют
дальнейшей реализации/операторских процедур.

Раздел не содержит локального управления пользователями или ролями. Истечение elevation скрывает раздел и требует повторного ввода токена; активная teacher/student session при этом сохраняется.

## 9. Закрепление работы за проверяющим

В API и модели этот механизм называется `ReviewClaim`/lease, но интерфейс
показывает только понятные пользователю формулировки «Вы проверяете работу»,
«Работу проверяет …» и «Закрепление продлевается автоматически».

1. Открытие для изменения решения атомарно закрепляет работу.
2. Если target свободен, baseline выдаёт lease на 300 секунд; frontend делает
   heartbeat сразу и затем каждые 120 секунд.
3. Другие преподаватели видят внешнее имя owner и могут открыть read-only.
4. Сохранение черновика требует актуальной revision закрепления.
5. При disconnect lease истекает; private draft остаётся.
6. Автоматического takeover/уведомления baseline нет; новый teacher получает
   закрепление после release/expiry. Audited override — future.
7. Финальное решение освобождает работу.

Для экзамена default granularity — submission; для нескольких независимых items преподаватель может выбрать item-level.

## 10. Включение Moodle-работы для групп

Локальный task-bank UI отложен и скрыт. Преподаватель не создаёт копию задания
и не настраивает заново его название, условие, сроки, максимальный балл или
число попыток. Рабочий flow:

1. Синхронизировать курс и выбрать обнаруженную Moodle activity.
2. Просмотреть read-only название, условие, сроки, шкалу и число попыток.
3. Выбрать одну или несколько назначенных преподавателю Moodle-групп. Actor с
   `SYSTEM_SETTINGS` может выбрать любую группу курса.
4. Нажать «Опубликовать»: это включает IDE только для выбранных групп и не
   изменяет Moodle activity.

Transport ответа не является параметром этой формы и не блокирует локальное
включение. При старте попытки LMS-adapter проецирует его в generic workspace
profile; перед checkpoint/final delivery adapter повторно проверяет фактическую
student form. Ошибка или drift в этот момент блокирует внешнюю запись
fail-closed, но локальная сдача остаётся сохранённой. Если profile не доказан
уже при старте, IDE не создаёт попытку с угаданным файловым режимом и предлагает
синхронизировать курс.

Следующий sync всегда перезаписывает локальную проекцию Moodle-owned полей.
Локально остаются только настройки оболочки, которые не подменяют Moodle:
paste/AI policy, СППР и визуальное включение для групп. Внутренние task-version
API и hidden-test manifest сохраняются как технический задел и не являются
пользовательским банком заданий текущего релиза.

## 11. AI, плагиат и авторство

Student AI виден только по policy, не вставляет текст в IDE и показывает
allowlisted citations. Teacher thread привязан к immutable submission snapshot;
AI не применяет оценку. Server-side message budget суммируется по threads одного
владельца/режима и до provider call возвращает `429 AI_RATE_LIMIT` при
превышении (defaults 10 student, 30 teacher за 60 секунд).

Baseline-плагиат вычисляет один versioned lexical-winnowing score, исключает
совпадающие со starter file последовательности и сохраняет matched fragments /
case disposition. Review UI ранжирует до пяти кандидатов и открывает отдельное
side-by-side сравнение immutable файлов: список фрагментов переводит оба Monaco
редактора к соответствующим диапазонам строк, а совпадения подсвечиваются с
обеих сторон. Несколько component scores, AST/control-flow и исключение общего
boilerplate вне starter остаются следующими улучшениями.

Первым блоком вкладки «Плагиат» показывается автоматическая проверка
происхождения ответа после обратной синхронизации Moodle. Зелёный статус
означает, что полный ответ Moodle совпал хотя бы с одной успешно доставленной
контрольной точкой Мехмат.Практикума; жёлтый — для этой Moodle-сдачи нет локальной
квитанции и она могла быть выполнена напрямую; красный — однозначно связанный
ответ отличается от всех доставленных контрольных точек. Неполный read-back
показывается нейтрально. Сам статус не меняет оценку и не является вердиктом о
нарушении.

Поиск сходства решений и анализ процесса написания запускаются преподавателем
вручную кнопками «Запустить»/«Запустить снова». Проверка происхождения ответа,
напротив, не требует отдельной кнопки: она пересчитывается при чтении сдачи по
результатам фоновой синхронизации.

Обычный преподаватель получает такую пару, если хотя бы одна её сдача входит в
его обычный `CourseGroup` scope. Вторая сдача раскрывается только в контексте
этого similarity match и не добавляется в общую очередь преподавателя. Actor с
`SYSTEM_SETTINGS` может читать все пары. Сервер фильтрует списки matches до
сериализации; comparison endpoint возвращает `404` для постороннего actor.
Совпадение является свидетельством для ручного решения, а не вердиктом.
Авторство показывает probability только вместе с analyzer/model/calibration,
uncertainty и предупреждением «не является доказательством». Ни один сигнал не
меняет балл автоматически.

## 12. Ошибки, деградация и accessibility

- LMS недоступна после уже созданной попытки: IDE и local submission работают, sync pending; новый login/import может быть недоступен.
- Runner недоступен: writing/submission продолжаются, запуск queued/unavailable.
- AI недоступен: panels disabled, ручная проверка работает.
- Realtime reconnect показывает unacked count и не утверждает «сохранено» без ack.
- Deadline offline сообщает, что гарантирована только последняя server-ack revision.
- Screen-reader labels, keyboard-only paths, high contrast, reduced motion и font scaling обязательны.
- IME/assistive input не классифицируется как внешняя вставка автоматически.
- Accessibility accommodation задаётся преподавателем для external principal; авторский анализ получает этот context и не применяет обычную calibration без оговорки.
