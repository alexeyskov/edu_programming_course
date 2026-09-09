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

## 3. Контроль внешней вставки

### 3.1. Клиентские барьеры

В строгом режиме обрабатываются и блокируются:

- `paste` и `beforeinput` с `insertFromPaste`;
- drop файлов/текста и Monaco drop/paste actions;
- middle-click paste в поддерживающих его ОС;
- вставка из browser context menu;
- массовая замена model value вне разрешённого command path;
- drag из другого окна;
- генеративные inline completions.

Нельзя просто отключить все много­символьные изменения: автозакрытие скобок, rename, undo/redo, formatter, IME и обычные completion тоже создают несколько символов. Каждая разрешённая команда должна иметь отдельный `source`.

### 3.2. Внутренний clipboard receipt

Реализованный baseline flow использует receipt на полный скопированный текст
той же попытки:

1. При `paste` IDE проверяет, что точный вставляемый фрагмент уже присутствует
   хотя бы в одном файле текущей рабочей области. Копирование внутри Monaco
   заранее выполняет ту же подготовку и сокращает задержку вставки.
2. Клиент flush-ит edits и отправляет file ID, подтверждённую workspace revision
   и найденный текст (до 256 KiB).
3. Сервер проверяет, что текст является substring текущего файла той же попытки,
   и выдаёт однократный receipt на пять минут.
4. Frontend блокирует native edit и принимает только в точности тот же текст по
   receipt той же попытки. Поэтому фрагмент можно копировать любым способом, но
   вставить его удастся лишь тогда, когда он уже есть в текущей рабочей области.
5. Server replacement вычисляет semantic delta и принимает
   `INTERNAL_PASTE`, только если вставленный delta совпал с hash/length receipt;
   затем receipt помечается использованным.

Baseline receipt не привязан к точному source range и не работает offline. Для
будущего более строгого protocol можно добавить source/target range, sequence и
восстанавливаемый IndexedDB receipt, но это требует отдельной схемы и тестов;
WebSocket сейчас отсутствует.

### 3.3. Что считать внутренним

Рекомендуемый default: только текст, уже присутствующий в текущей попытке. Не
разрешать перенос нового фрагмента между лабораторной и контрольной, даже если
пользователь тот же. Read-only starter files этой же попытки считаются частью
рабочей области.

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
