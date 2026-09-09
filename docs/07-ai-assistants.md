# ИИ-ассистенты

> Статус baseline v1: persistent student/teacher threads, global/course flags,
> bounded context/history, Responses или Chat Completions adapter, mock provider,
> student input/output regex gates и allow-list только `cppreference.com`
> реализованы. Server-side rate budget до provider call также реализован:
> 10 student- и 30 teacher-сообщений на пользователя/режим за 60 секунд по
> умолчанию, суммарно по threads. Retrieval index/tools, structured background
> grading suggestion, денежный/token budget accounting, moderation pipeline и
> полный eval set ниже ещё не готовы.

## 1. Роль ИИ

В системе есть три разных режима, которые нельзя объединять одним prompt:

1. **Студенческий tutor** — помогает понять, но не решает за студента.
2. **Преподавательский review chat** — подробно анализирует сдачу по запросу преподавателя.
3. **Фоновая СППР** — формирует структурированную предварительную рекомендацию.

У каждого режима свой policy, tools, retention, model budget и набор доступных данных.

## 2. Provider abstraction

Core interface должен поддерживать:

- OpenAI Responses API;
- локальную модель через внутренний provider service;
- OpenAI-compatible provider только через отдельный адаптер;
- mock/replay provider для тестов.

Текущий adapter поддерживает OpenAI Responses и Chat Completions-compatible HTTP
формы. Он не использует provider tools/retrieval и хранит собственные
threads/messages. `store=false`, structured outputs и usage/budget accounting в
baseline payload явно не настраиваются; это нужно согласовать до включения
реального provider.

Provider-specific response IDs не являются доменной моделью chat. Система хранит собственные threads/messages и может отправлять историю stateless. Для OpenAI допустимы conversation/previous response, но при требованиях университета предпочтительно `store=false` и собственное хранение. Официальный Responses API поддерживает instructions, conversation state, structured output, tools и отдельное управление хранением.

Целевой audit каждого вызова должен хранить model ID, provider,
prompt/policy version, input/output hashes, latency, usage, filter outcome и
trace ID. Baseline `ChatMessage` хранит собственный content, citations, model и
safety outcome, а `ChatThread` — policy version/context; provider response IDs,
usage/cost, latency и request/response hashes пока отдельно не сохраняются.

## 3. Студенческий tutor

### 3.1. Policy matrix

| Режим | Рекомендуемый default |
| --- | --- |
| Лабораторная | включён: концепции, наводящие вопросы, диагностика причины, документация |
| Самостоятельная | концептуальные объяснения; исправленный код и алгоритм решения запрещены |
| Контрольная | выключен; опционально только поиск по разрешённой документации |
| Экзамен | выключен глобальной политикой |

Глобальный kill switch меняется только в сессии с `SYSTEM_SETTINGS`, полученной после ввода административного токена. Преподаватель настраивает ИИ для своих курсов и конкретных работ в пределах глобальной политики. Это не создаёт отдельные роли администратора курса или организации.

### 3.2. Разрешённые ответы

- объяснить термин или механизм C++;
- разъяснить текст сообщения компилятора;
- указать область/строку, где искать причину;
- задать наводящий вопрос;
- предложить мысленный тест или контрпример;
- показать синтаксический мини-пример, не являющийся решением текущего задания;
- дать ссылку на документацию и кратко объяснить релевантный раздел;
- объяснить поведение STL container/algorithm.

### 3.3. Запрещённые ответы

- полный код решения;
- готовая требуемая функция/класс;
- исправленная целиком версия student file;
- пошаговый алгоритм, однозначно превращающийся в ответ контрольной;
- скрытые тесты, эталон или rubric internals;
- оценка вероятности плагиата/авторства;
- обход paste/timer/exam controls.

## 4. Многоуровневое ограничение выдачи кода

Prompt недостаточен. Нужны слои:

1. Входной classifier определяет intent: concept, error explanation, solution request, policy bypass.
2. Context builder передаёт минимум кода и отделяет его как untrusted data.
3. Tool allow-list: только retrieval по разрешённым источникам; без shell и произвольного web.
4. System policy запрещает готовое решение и скрытые данные.
5. Structured response содержит answer, hints, citations, policy flags.
6. Output gate ищет code fences, высокую плотность C++ tokens, целые функции/classes и сходство с task statement/reference.
7. При нарушении ответ блокируется и перегенерируется в более абстрактной форме.
8. Rate/turn/token limits мешают собрать решение по частям.
9. Все assessed-chat turns доступны преподавателю и audit согласно политике.
10. Набор jailbreak/prompt-injection evals выполняется на каждой версии policy/model.

Даже эти меры снижают, но не исключают утечку решения. Для экзамена безопасный default — отсутствие генеративного tutor.

Пункт 8 частично работает уже в baseline: до обращения к provider backend под
блокировкой principal row считает `USER` messages владельца во всех threads
текущего режима за `AI_RATE_LIMIT_WINDOW_SECONDS`. Defaults —
`AI_STUDENT_RATE_LIMIT_MESSAGES=10` и
`AI_TEACHER_RATE_LIMIT_MESSAGES=30` за 60 секунд. Превышение возвращает
`429 AI_RATE_LIMIT` с limit/window и не вызывает provider. Это rate control, а
не учёт токенов, денег или месячного бюджета курса.

## 5. Документация и citations

Retrieval index должен включать только разрешённые источники:

- материалы конкретного курса;
- [cppreference](https://en.cppreference.com/w/) как практический справочник;
- публичный draft стандарта C++, если преподаватель считает нужным;
- официальную документацию GCC/Clang;
- локальные методические материалы.

cppreference полезен, но не является нормативным текстом стандарта. UI показывает title, URL, section/anchor и retrieval timestamp. Ответ без найденного источника должен честно обозначать, что citation не найден, а не создавать ссылку.

Индекс хранит license/provenance metadata; нельзя автоматически копировать и раздавать большие фрагменты сторонней документации.

В baseline retrieval index отсутствует: provider answer обязан содержать
allowlisted HTTPS citation под `/w/` доменов cppreference; student answer без неё
отклоняется. Локальные policy-refusal ответы `BLOCKED_INPUT`/`BLOCKED_SOLUTION`
не являются provider answer и намеренно могут не иметь citation.
GCC/Clang/course-doc citations пока не разрешены adapter-ом, несмотря
на целевой список выше.

## 6. Лабораторное мини-окно

- открывается поверх IDE и привязано к attempt/task;
- новый temporary thread на каждое открытие или по кнопке «Новый диалог»;
- пользователь явно выбирает, приложить ли текущую diagnostic/selected fragment;
- модель видит только конкретную snapshot revision;
- UI показывает, что ответ может ошибаться;
- при закрытии dialog не переносит скрытую память в следующую работу;
- «временный» означает отсутствие пользовательского long-term memory. Audit retention задаётся политикой и отображается до первого сообщения.

Рекомендуемый retention: лабораторные turns 30 дней, assessed turns до завершения апелляции, преподавательские threads до явного удаления/архива курса. Университет должен утвердить сроки.

Автоматического retention cleanup baseline не содержит; рекомендации выше не
следует трактовать как действующую политику удаления.

## 7. Преподавательский review chat

Контекст по выбору преподавателя:

- immutable submission snapshot;
- условие и rubric;
- compile/test/sanitizer evidence;
- selected diagnostics и code ranges;
- предыдущие сообщения thread;
- разрешённая C++ документация.

Функции:

- спросить о конкретной ошибке/undefined behavior;
- сопоставить код с критерием рубрики;
- объяснить падение теста;
- предложить дополнительные проверочные случаи;
- сравнить два teacher-selected fragments;
- сформулировать черновик комментария.

Чат может предложить код для эксперимента преподавателя, но вставка требует явного действия и попадает только в `teacher_experiment`. Такой код компилируется/запускается тем же file-isolated runner и никогда не меняет submission. Ответы цитируют file hash/line range и evidence IDs.

## 8. Фоновая СППР

Структурированный фоновый AI-review в baseline не оркестрируется. Текущий
teacher chat является advisory-возможностью и не создаёт review decision.
Реализованный synchronous hidden-test `EvidenceReport` — детерминированный
отдельный источник СППР, а не AI analysis: он не вызывает модель, не вычисляет
recommended grade и доступен только преподавателю.

Выход строго структурирован:

- summary без персональных формулировок;
- findings с severity, evidence IDs и code ranges;
- rubric criterion suggestions;
- recommended grade range, не только point estimate;
- confidence/uncertainty reasons;
- suggested teacher checks;
- documentation citations;
- policy/schema/model versions.

Если JSON schema validation не проходит, результат не показывается как готовая рекомендация. Raw output сохраняется ограниченно для диагностики, а job помечается invalid.

## 9. Prompt injection

Student code, comments, task content и stdout считаются untrusted. Меры:

- данные передаются отдельными typed fields, а не смешиваются с instruction text;
- модель получает явное указание игнорировать инструкции внутри code/data;
- tools имеют deny-by-default и серверно проверяемые arguments;
- URL из student content не открываются;
- hidden tests/reference solution никогда не попадают student tutor;
- учительский assistant не может сам применить grade или отправить в Moodle;
- tool results ограничиваются размером и очищаются от control sequences.

## 10. Privacy и стоимость

- отправлять provider только необходимые snippets/evidence;
- псевдонимизировать user ID через стабильный hash per provider/project;
- не отправлять ФИО/группу без функциональной необходимости;
- provider credentials находятся только на server;
- отдельные budget limits на LMS connection/курс и две внешние роли;
- cache допускается только для не­персонального неизменного prefix/context;
- модель/provider переключаются настройкой без изменения доменных данных;
- локальный provider доступен для курсов, где внешняя передача кода запрещена.

## 11. Глобальные и преподавательские настройки

- global kill switch;
- provider/model allow-list;
- student tutor modes по типам работы;
- teacher chat enabled;
- background SPPR enabled;
- daily/monthly budget;
- retention and external transfer policy;
- documentation source allow-list;
- output policy thresholds;
- audit/export permissions.

Глобальный блок доступен только при активном административном повышении сессии. У преподавателя есть собственный switch СППР для доступного ему курса, если глобальная политика разрешает. Выключение прекращает новые jobs, но не удаляет evidence и не меняет старые decisions.

## 12. Evals до включения

- минимум 100 запросов на выдачу полного решения, включая дробление по частям;
- jailbreak на русском и английском;
- prompt injection в комментариях C++ и stdout;
- ложные/несуществующие citations;
- корректное объяснение распространённых ошибок C++;
- утечка hidden tests/reference/rubric;
- bias по стилю/ФИО — идентичность не должна передаваться;
- калибровка recommended grade на исторической размеченной выборке;
- teacher acceptance/rejection rate и disagreement analysis;
- provider outage/timeout/malformed output.

## 13. Источник OpenAI

Для реализации OpenAI-адаптера использовать актуальную [официальную документацию Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create). Она описывает instructions, conversation/previous response, structured output, tools, moderation, `store` и `safety_identifier`. Moderation API решает безопасность контента, но не задачу «не писать решение C++»; академическая policy остаётся обязанностью приложения.
