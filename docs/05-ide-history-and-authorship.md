# IDE, история редактирования и анализ авторства

> Статус baseline v1: Monaco отправляет последовательные REST replacement с
> expected revision; сервер вычисляет contiguous semantic delta, записывает
> metadata-bound SHA-256 chain и snapshots. Hard limit — 64 файла и 512 KiB
> исходного UTF-8 текста. Unacked queue живёт в памяти вкладки; WebSocket,
> IndexedDB recovery и расширенные focus/completion events ниже остаются target.

## 1. Реалистичная модель защиты

Браузер не является доверенной средой. Перехват `paste` не мешает:

- открыть решение на втором устройстве и перепечатать;
- использовать DevTools или изменить JavaScript;
- эмулировать клавиатуру внешней программой;
- вводить текст через accessibility/IME-интерфейсы;
- заранее выучить или распечатать решение.

Поэтому система решает две задачи: повышает стоимость прямой копипасты и сохраняет качественную историю для последующего анализа. Интерфейс и регламент не должны заявлять, что самостоятельность «доказана» технически.

## 2. Режимы рабочей области

### Single-file

- ровно один translation unit (`.c/.cc/.cpp/.cxx`) — системный `main.cpp`
  либо файл из опубликованной версии задания;
- дополнительно можно создавать и удалять текстовые `.txt`-файлы с входными
  данными; для Moodle online-text transport это действие заранее блокируется,
  потому что такой ответ не способен перенести второй файл;
- скрытые системные файлы не показываются;
- максимально простой MVP.

### Multi-file

- дерево файлов и вкладки;
- workspace-разрешения `.c`, `.cc`, `.cpp`, `.cxx`, `.h`, `.hh`, `.hpp`,
  `.hxx`, `.inc`, `.txt`; runner компилирует translation units, оставляет
  headers для локальных include и копирует `.txt` в writable cwd рядом с
  executable;
- лимиты числа файлов, глубины, имени и суммарного размера;
- template files могут быть read-only или editable;
- build profile генерирует система; произвольные scripts, CMakeLists и symlinks запрещены в строгих работах;
- entry point и список targets задаёт преподаватель.

Модель данных всегда многофайловая. Single-file — политика, а не отдельный формат хранения.

Baseline допускает не более 64 активных файлов и 512 KiB суммарного исходного
UTF-8 текста. Single-file требует ровно один translation unit, но допускает
сопутствующие `.txt`; исходный файл удалить нельзя. Произвольное переименование
отдельным endpoint пока отсутствует.

### Продолжение работы позже

Несданная попытка и подтверждённые сервером файлы хранятся в БД. Повторное
открытие доступной работы возвращает ту же активную попытку, версию задания,
код и ревизию, в том числе после нескольких дней и нового входа в систему.
В Moodle при этом повторно проверяются права и доступность; уже завершённая
попытка не открывается для редактирования заново.

Кнопка «Сохранить и выйти» дожидается сохранения всех изменений и возвращает в
карточку работы без сдачи. «Завершить» означает сдачу, а не паузу. IDE также
отправляет накопленные изменения при переходе на другую страницу приложения
и скрытии вкладки. До закрытия браузера нужно дождаться «Сохранено»: при
отсутствии связи неподтверждённые изменения остаются только в памяти вкладки.

Для работы с подтверждённым отсутствием лимита IDE показывает «Без таймера»;
само время, проведённое вне редактора, такую попытку не завершает. Установленный
лимит Moodle сохраняется. Отсутствие прочитанного таймера Quiz не считается
доказательством неограниченного времени: оно зависит от живой сессии и
переопределений Moodle.

Для Quiz с прочитанным таймером время решения сокращается на
`MOODLE_SYNC_TIMEOUT` секунд (по умолчанию 300): этот запас остаётся для
автоматической выгрузки и завершения попытки. Обратный отсчёт IDE показывает
уже сокращённое время. После его окончания все задачи становятся недоступны
для редактирования, и сервер сдаёт последние подтверждённые ревизии независимо
от того, открыта ли вкладка. Например, 15 минут Moodle дают 10 минут решения
при резерве 5 минут. «Без таймера» этот механизм не затрагивает.

## 3. Контроль внешней вставки

### 3.1. Клиентские барьеры

В строгом режиме обрабатываются и блокируются:

- `paste` и `beforeinput` с `insertFromPaste`, `insertFromDrop`, `insertFromYank`;
- drop файлов/текста и Monaco drop/paste actions;
- middle-click paste в поддерживающих его ОС;
- вставка из browser context menu;
- drag из другого окна;
- генеративные inline completions.

Capture-обработчик находится на внешнем контейнере React, **выше** обработчиков
Monaco: установка на `editor.getDomNode()` была слишком поздней для некоторых
Monaco paste providers. Текстовая команда `editor.trigger(..., 'paste', ...)`
без clipboard metadata отклоняется отдельно. В строгом режиме встроенное меню
Monaco заменено стандартным меню браузера, отключены `pasteAs`/`dropIntoEditor`;
обычные Ctrl/Cmd+C/X/V используют проверяемые browser clipboard events.

IME, обычный ввод, undo/redo и автодополнение не блокируются по числу вставленных
символов. Произвольная подмена модели/API через DevTools не предотвращается
этим UI-барьером: браузер не является доверенной средой. Bulk edits по-прежнему
выделяются сервером в истории, а не объявляются доказательством нарушения.

### 3.2. Внутренний clipboard receipt

Реализованный flow использует локальную метку происхождения и серверный receipt
на полный скопированный текст. Задачи одной попытки Moodle Quiz могут обмениваться
кодом, но разные работы, попытки и студенты остаются изолированы:

1. Только `copy`/`cut` из выделенного кода редактора создаёт случайную метку
   `application/x-eduprog-workspace-copy` в clipboard и запись в памяти текущей
   открытой IDE. Без выделения копируется текущая строка. Копирование из
   условия, ИИ-чата, find/replace или другой страницы метку не создаёт.
2. Клиент flush-ит edits и отправляет source file ID, подтверждённую workspace
   revision и скопированный текст (до 256 KiB).
3. Сервер проверяет, что текст является substring текущего файла той же попытки,
   и выдаёт однократный receipt на пять минут.
4. Frontend блокирует native edit и сверяет метку, полный текст, общую попытку
   и TTL. Совпадение внешнего текста с кодом **не** разрешает вставку. Быстрая
   вставка ждёт подтверждения копирования; смена документа/выделения/режима
   чтения во время ожидания отменяет отложенное изменение. При сверке текста
   нормализуются только переводы строк CRLF/LF; остальные символы должны совпасть.
5. Вместе с `INTERNAL_PASTE` отправляется точный `paste_range` (offset и
   delete_count в Unicode code points). Сервер проверяет, что вне диапазона
   файл не изменился и вставленный текст совпал с hash/length receipt. Receipt
   остаётся однократным и привязан к principal/исходной задаче/revision/TTL. Для
   вставки в другую задачу сервер проверяет общий `MoodleQuizQuestion.root_attempt_id`
   и владельца исходной попытки. Revision сравнивается с исходной рабочей областью,
   а не с независимой ревизией назначения; в событии сохраняется `clipboard_source`.
   Полям клиента и одному совпадению assessment/course система не доверяет. Старые
   клиенты без `paste_range` поддерживаются через прежний contiguous delta.
6. Cut удаляет исходный фрагмент только после подтверждения копирования.
   Его последующая вставка разрешена по receipt даже после исчезновения текста
   из исходного файла. Повторная вставка по той же действительной локальной
   метке запрашивает новый receipt из уже сохранённого текста рабочей области.
   Если предыдущая вставка была в другой задаче этой попытки, IDE читает её
   сохранённый код и ревизию для нового подтверждения.

Работает вставка одного непрерывного фрагмента в тот же или другой файл, в том
числе после переключения задачи внутри одной попытки Quiz. Метка и подтверждение
живут в памяти родительской IDE, отдельно от моделей Monaco и очередей сохранения
каждой задачи. При переключении код сохраняется до размонтирования редактора;
ожидающая вставка или вырезание в старом редакторе отменяется, подтверждение
копирования сохраняется. Метка живёт пять минут и не переносится через
перезагрузку страницы, закрытие IDE, другую вкладку или другую попытку: тогда
нужно скопировать код заново. Если браузер/clipboard
manager удалил метку, вставка отклоняется, а не разрешается по одному тексту.
Offline receipt не выдаётся. Метаданные clipboard не являются криптографическим
доказательством действия пользователя: изменённый клиент может обходить UI.

### 3.3. Что считать внутренним

Внутренним считается только копирование из кода текущей работы/попытки,
подтверждённое сервером. Между задачами одной попытки Quiz перенос разрешён.
Нельзя переносить фрагменты между лабораторной и контрольной или разными
попытками Quiz через старую метку. Read-only starter
files той же попытки считаются частью рабочей области (копирование разрешено,
изменение read-only файла — нет). Политика `ALLOW` сохраняет свободную вставку.

Регрессионные проверки: `frontend/src/lib/editorClipboard.test.ts`,
`backend/tests/test_clipboard_paste.py`. Для настоящего Chromium запустите Vite
на `127.0.0.1:5187`, затем `python scripts/check_editor_clipboard.py`;
вариант `--cdn` проверяет версию Monaco из штатного loader. Fixture использует
искусственные файлы и не обращается к Moodle/production API.

## 4. События истории

История записывает семантические изменения документа, а не raw keylogger. Это снижает объём и риск сбора лишних данных, сохраняя возможность воспроизведения.

### 4.1. Целевые типы

Baseline сохраняет replacement deltas, file create/delete, snapshots,
clipboard source, run и submission/checkpoint metadata. Focus/visibility,
completion/formatter/code-action provenance, rename и полный lifecycle event
catalog ниже ещё не собираются.

Для начала попытки, каждой принятой правки или операции с файлом, запуска и
ручной сдачи также сохраняется минимальный сетевой контекст: точный IP-адрес,
определённые из `User-Agent` название и версия браузера, ОС и тип устройства.
Сырой `User-Agent`, canvas/WebGL fingerprint, геолокация и характеристики
оборудования не собираются. Браузерные поля являются самодекларируемыми и сами
по себе не доказывают личность студента. IP считается достоверным только при
закрытом прямом доступе к backend и корректной цепочке доверенных reverse proxy.

- attempt started/reconnected/ended;
- file created/renamed/deleted;
- text edit with one or more non-overlapping changes;
- undo/redo;
- internal copy/cut/paste;
- completion accepted, включая provider kind и inserted length;
- formatter/code action applied;
- template restored;
- focus gained/lost и document visibility changed;
- run/compile requested с snapshot revision;
- AI help opened/question asked;
- snapshot created and Moodle checkpoint queued;
- server rejected edit/deadline reached.

Selection/cursor movement по умолчанию не сохраняется: это большой шум. Можно хранить агрегаты продолжительности фокуса, но не глобальные клавиши и не содержимое других приложений.

### 4.2. Поля text edit

- server sequence;
- client instance and local sequence;
- file ID;
- base document version;
- ordered changes: range offsets, deleted length/hash, inserted text or encrypted content ref;
- source: typing, IME, internal paste, undo, redo, completion, formatter, code action, recovery;
- client monotonic time and server received time;
- previous/current document hash;
- previous/current event-chain hash.

Для простого анализа inserted text можно хранить в событии. Для строгой минимизации данных — отдельный encrypted blob с ограниченным доступом. В любом случае финальный код уже является образовательной записью, поэтому это решение принимает политика университета.

## 5. Доставка и восстановление

### 5.1. Протокол

Ниже — целевой realtime protocol. Baseline v1 использует REST, полный новый
content, `If-Match`/revision и idempotency key; frontend flush-ит очередь перед
run/submit и при `409` предлагает выгрузить локальную копию.

- при подключении сервер возвращает current sequence/hash и snapshot manifest;
- клиент отправляет пакет до 20 операций или не реже одного раза в 250–500 мс;
- server ack содержит последний принятый sequence, новый workspace hash и возможный authoritative patch;
- повтор пакета с тем же idempotency key безопасен;
- пропуск sequence приводит к resync, а не silent merge;
- при конфликте клиент загружает server snapshot и предлагает восстановить неподтверждённый локальный patch.

### 5.2. IndexedDB

Не реализовано в baseline v1. Сейчас очередь хранится только в памяти открытой
вкладки, а `beforeunload` предупреждает пользователя.

Локальная очередь содержит только текущую попытку, шифруется ключом сессии и очищается после подтверждённой сдачи/истечения retention. Ключ не должен быть постоянным общим секретом. При закрытии вкладки `sendBeacon` полезен, но не считается гарантией; основной канал — WebSocket ack.

### 5.3. Snapshots

Рекомендуемые причины:

- каждые 100 событий или 30 секунд при активном вводе;
- перед каждой сборкой/проверкой;
- перед Moodle checkpoint;
- при уходе в background;
- при ручной и автоматической сдаче.

Snapshot хранит manifest и content-addressed файлы. Он не заменяет event stream, а ускоряет восстановление и анализ.

## 6. Цепочка целостности

Для каждого события сервер вычисляет hash от canonical representation + previous event hash. Финальный submission manifest содержит:

- attempt/epoch;
- final sequence;
- head event hash;
- hashes всех файлов;
- task/policy/toolchain versions;
- server submit time;
- signature системы.

Это обнаруживает последующее изменение базы/экспорта, но не делает браузер доверенным: студент всё ещё может посылать допустимые API-команды через DevTools. Данное ограничение явно передаётся внешнему анализатору.

## 7. Таймер и дедлайн

- `started_at` и `deadline_at` определяет сервер.
- Клиент показывает расчёт по последнему server time sync и не продлевает время локально.
- Baseline UI считает remaining time от server-provided deadline, но использует
  локальные часы устройства; отдельный server-clock offset/heartbeat API пока
  отсутствует. Целевой WebSocket heartbeat должен содержать remaining time и
  last ack.
- За 5, 2 и 1 минуту показываются предупреждения; при менее 60 секунд создаётся forced snapshot/checkpoint.
- В submission входит только операция, принятая сервером не позже дедлайна.
- Network grace не принимается по client timestamp, иначе время можно подделать. Индивидуальное продление оформляется override до/после инцидента с аудитом.
- После deadline endpoints workspace write возвращают состояние `LOCKED`; UI переключается read-only.

## 8. Moodle checkpoints

Moodle checkpoint — экспорт полной текущей snapshot revision, а не диффа. Алгоритм:

1. При старте создаётся initial checkpoint.
2. До последней пятой длительности используется период `D/10`.
3. В последней пятой — `D/20`.
4. Baseline применяет clamp 30 секунд — 15 минут (900 секунд).
5. При остатке менее минуты, finish и deadline создаются обязательные checkpoints.
6. Outbox key имеет вид attempt + snapshot hash + target, поэтому повтор безопасен.
7. Неудача Moodle не блокирует IDE; status виден преподавателю и оператору.

Bridge 0.3 не изменяет legacy Quiz Essay и LTI activity. Python backend создаёт
canonical UTF-8 bytes полного recovery manifest; PHP проверяет валидный JSON и
SHA-256 exact bytes и хранит их с `event_chain_head`, epoch и workspace
revision. Native Quiz/LTI draft update остаётся будущим connector.

## 9. Внешний анализатор авторства

### 9.1. Асинхронная модель

Ниже — целевая очередь/webhook модель. Baseline создаёт DB job, выполняет один
bounded server-to-server HTTP request в API flow и сохраняет terminal result;
webhook, object storage и signed download URL отсутствуют.

- система создаёт analysis job;
- формирует pseudonymous export;
- загружает его в analyzer по short-lived signed URL или streaming request;
- анализатор отвечает webhook с подписью либо результат опрашивается;
- результат связывается с exact manifest hash;
- timeout/error остаётся отдельным состоянием, проверка преподавателя продолжается.

### 9.2. Экспортный пакет baseline v1

Текущий adapter отправляет один bounded JSON object (до
`AUTHORSHIP_MAX_EXPORT_BYTES`, default 16 MiB):

- `schema_version: "1.0"`;
- keyed pseudonyms для course, assessment, student и submission;
- final submission manifest hash/revision/time window, полные финальные source
  files и starter files с hashes;
- ordered edit events до финальной revision: sequence, epoch, source/type,
  безопасно нормализованные changes, previous/current event hashes и server
  received time;
- финальный `event_chain_head`.

Это обычный JSON, не NDJSON/archive и не signed-download flow. Periodic
snapshots, run requests, focus intervals и AI activity пока не входят в payload.
ФИО, e-mail, Moodle ID, group и grade не передаются; pseudonyms стабильны для
типа/entity внутри deployment и вычисляются keyed HMAC, а не являются случайным
ID каждого job. Полный исходный код и edit changes передаются, поэтому внешний
provider/правовое основание/retention должны быть утверждены до включения.

### 9.3. Ответ анализатора

Baseline требует exact `manifest_hash`, непустые `analyzer`, `model` и
`calibration.version`, значения `probability`, `confidence`, `uncertainty` в
диапазоне `[0,1]`, object `features` и bounded string-array `warnings`. Backend
сохраняет canonical response hash; mismatch/невалидная схема не показываются как
готовый результат. Числовой UI намеренно скрыт без analyzer/model/non-empty
calibration. Ни один threshold не создаёт санкцию: UI не должен окрашивать
`0.49` как виновен, а `0.51` как невиновен.

## 10. Возможные признаки для будущего анализатора

- распределение размеров и частоты вставок;
- длительность пауз и возвратов к предыдущим строкам;
- частота compile/fix cycles;
- доля undo/redo и локальных исправлений;
- появление крупных самодостаточных блоков;
- соответствие порядка появления идентификаторов структуре решения;
- смена стиля/лексики по времени;
- количество focus gaps;
- использование internal paste/completion;
- согласованность с предыдущими подтверждёнными работами.

Accessibility, дислексия, моторные особенности, IDE-привычки и плохая сеть могут существенно влиять на признаки. Они требуют альтернативного процесса и запрета автоматических санкций.

## 11. Приёмочные сценарии IDE

1. Внешний paste, drop и context-menu paste не меняют документ и создают security event.
2. Internal copy/paste в той же попытке воспроизводится и связан receipt.
3. Copy из другой попытки отклоняется.
4. IME-ввод не классифицируется как внешняя вставка.
5. Undo/redo и completion помечены своим source.
6. Перезагрузка восстанавливает server state и безопасно повторяет unacked batch.
7. Два браузерных экземпляра одной попытки не создают divergent history; второй получает policy error или read-only.
8. После дедлайна никакой write endpoint не принимает изменение.
9. Final snapshot воспроизводится из events и совпадает по hash.
10. Export v1 проходит schema validation и не содержит прямых идентификаторов.
