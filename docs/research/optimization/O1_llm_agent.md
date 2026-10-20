# O1. LLM-агент и затраты: сверка с литературой и что улучшить

Срез: 20.10.2026. Только исследование. Код и конфиги не менялись, деньги не тратились.
Сверено с кодом: `agent/internal/{prompt,llm,userctx,agent,gate,hwctx}`, `agent/configs/example.yaml`,
`bench/mockllm`, `bench/costmodel`, `bench/baselines` (PicoClaw v0.3.1 — исходники тега `2cf030d`),
`docs/neurobot/PLAN.md` §3–4, смоук `bench/baselines/smoke/20261007/*/summary.md`.
Прежние заметки не повторяю: D (контекст и кэш, 06.10), GAP1 (TTL), R6 (каскады), G3 (сжатие).
Здесь — что изменилось с 06.10 и что видно при сверке с кодом.

Метки:
- [код] — проверено по исходникам;
- [док] — документация провайдера, снята 20.10.2026;
- [ст] — статья: метаданные сверены по arXiv API или Crossref, числа — из аннотации или HTML;
- [расчёт] — мой расчёт; [ориентир] — оракул mock-llm, не результат.

Статус по нашему коду: **[есть] / [частично] / [нет]**.

---

## 0. Коротко

1. **Самый крупный неиспользованный рычаг — скрытые reasoning-токены.**
   - `reasoning_effort` агент не передаёт [код]. Для gpt-5-mini это значит умолчание `medium`. Умолчание подтверждают сторонние источники; на странице модели OpenAI оно не указано.
   - Reasoning-токены оплачиваются как выход: 215,41 ₽/1M против 26,93 ₽/1M за вход у RouterAI.
   - При допущении концепции (+400 reasoning-токенов на вызов) выход даёт ≈74 % стоимости хода [расчёт, §4].
   - Задать уровень можно без кода, через `llm.extra`: RouterAI заявляет `reasoning_effort` и `verbosity` для gpt-5-mini [док].
2. **Окно истории сдвигается на один ход за раз, и это ломает кэш префикса после каждого хода.** Это касается `window` и `mask` с `window_turns` [код].
   - TokenPilot (EMNLP 2026 Findings) показывает: вытеснение на каждом ходе (B = 1) повышает промахи кэша, лучше вытеснять пачкой (B = 3).
   - У Anthropic для той же цели есть `clear_at_least` [ст, док].
   - Исправление — гистерезис окна, ≈20 строк Go.
3. **Пустые фоновые ходы (`NO_ACTION`) пишутся в историю и занимают места в окне из 8 ходов** [код]. После тихих часов в окне остаются одни «плановые проверки», а диалог с пользователем из него вытеснен.
4. **Второй вызов «скажи, что сделал» — заметная доля вызовов.**
   - В смоуке у оракула это 24 из 77 вызовов [ориентир].
   - LLMCompiler и ReWOO показывают, что план без повторного чтения наблюдений экономит вызовы и токены [ст].
   - У нас его можно убрать шаблонным ответом по результату инструмента. Для робота: `task_end` без LLM, если цель достигнута.
5. **Изменились модели и провайдер:**
   - у GPT-5.6+ кэш живёт ≥ 30 мин, а запись в кэш стоит 1,25× [док];
   - у GPT-6 Luna вызов функций в Chat Completions работает только при `reasoning_effort: none` [док];
   - у RouterAI эндпоинт OpenAI для gpt-6-luna и gpt-5.6-luna поддерживает **только Responses API** [док, `/endpoints`].

   Наш клиент работает только с Chat Completions [код]. Значит, самую дешёвую новую модель с кэшем 30 мин без доработки клиента взять нельзя.
6. **Модель стоимости (`bench/costmodel`, `price_table.call_cost_rub`) не знает надбавки за запись в кэш** [код]. А mock-llm по умолчанию считает 0 reasoning-токенов (`--reasoning-tokens 0`) [код]. Прогноз ₽ для gpt-5-mini с `medium` занижен до ≈3 раз [расчёт, §4].
7. **Главная архитектура адекватна свежей литературе:**
   - гейт по событиям вместо опроса;
   - LLM вне контура реального времени;
   - стабильный префикс и канонический JSON;
   - маскирование наблюдений, дельта состояния, правила без LLM.

   Подробно — §1. Новые подтверждения: ECML PKDD 2026 о пороговых триггерах вызова LLM, E³-Agent, TokenPilot, Manus.
8. **Не стоит делать:** LLMLingua, MemGPT/Mem0, ACON, обучаемые маршрутизаторы на Jetson, исполнение кода через MCP, динамический отбор инструментов (§3).

---

## 1. Что уже сделано — вердикт по литературе

| Решение | Где | Вердикт | Почему |
|---|---|---|---|
| Гейт: дебаунс, порог с гистерезисом, `delta` (send-on-delta), cooldown, коалесценция, конец эпизода; LLM будится по событию, а не по таймеру | `internal/gate` [код] | адекватно | Классика event-triggered: Tabuada 2007, Heemels 2012, Miskowicz 2006. Свежее: в ECML PKDD 2026 (Wang, arXiv:2607.13048) доказана оптимальность пороговых политик вызова LLM и нижняя граница интервала между срабатываниями (против дребезга), то есть теория под порог + cooldown. Тот же шаблон в E³-Agent (arXiv:2605.27428): LLM — обработчик исключений по событию, повторы гасит cooldown по сигнатуре. В arXiv:2605.30152 решение «когда действовать» отдано малой модели, а не LLM |
| LLM не в контуре жеста; 0 вызовов на жест | `neurobot`, PLAN §3–4 | адекватно | DoReMi (IROS 2024): LLM задаёт ограничения, нарушение ловит дешёвый детектор, LLM зовут только при рассогласовании. When2Ask: обучаемое «когда спрашивать LLM» резко сокращает обращения |
| Правила, созданные LLM, исполняются гейтом без LLM | `create_rule` [код] | адекватно, самый сильный рычаг | LLM вызывается один раз, дальше исполнение локально. Индустриальный аналог — автоматизации и AI Tasks Home Assistant (блог HA, 11.09.2025) |
| Стабильный префикс, изменчивое в хвосте, время в хвосте, канонический JSON, порядок инструментов | `internal/prompt`, `canon`, `texts.go` [код] | адекватно | Документация OpenAI: кэш требует совпадения всего отрендеренного префикса. Manus (2025): метка времени в префиксе ломает кэш, нужна детерминированная сериализация. TokenPilot: стабилизация префикса подняла долю попаданий 38,7 → 79,2 % (PinchBench) на GPT-5.4-mini и OpenClaw |
| Инструменты упавшего MCP-сервера остаются в каталоге | `mcphost` [код] | адекватно | Manus: «mask, don't remove» — смена набора инструментов посреди сессии ломает кэш и путает модель |
| Маскирование старых результатов инструментов (`mask`) | `userctx.View` [код] | адекватно; см. R2 | Complexity Trap: маскирование ≈ вдвое дешевле и не хуже LLM-сводки. Индустриальная форма — `clear_tool_uses` у Anthropic |
| Дельта состояния с последнего ответа (`delta_tail`), «нет связи» по устройствам | `hwctx` [код] | адекватно | AgentDiet (FSE 2026): устаревшая информация — главный мусор траекторий, вход −39,9…59,7 % |
| `summary_system` (LLM-сводка) как вариант для сравнения | `turn.go: maybeSummarize` [код] | адекватно как плечо эксперимента, не как умолчание | Complexity Trap: сводка удлиняет траектории на 13–15 % и ломает кэш. ACE: перезапись может схлопнуть контекст |
| Обрезка результата инструмента `max_tool_result_chars` 4000 | `turn.go` [код] | адекватно; мелкое улучшение — R11 | ACON сжимает наблюдения > 1024 токенов. TokenPilot оставляет начало и конец (600 + 400 символов) |
| Без потока, одно keep-alive, HTTP/2, возобновление TLS | `internal/llm` [код] | адекватно | Ответ ≈1,3 КиБ против ≈42,8 КиБ с SSE (концепция, решение 9) |
| `prompt_cache_key` не передаётся | [код] | адекватно до пробы | RouterAI не заявляет параметр в `supported_parameters` [док 20.10]. PicoClaw шлёт ключ только на «родные» OpenAI/Azure: другие OpenAI-совместимые провайдеры отвечают 422 на неизвестное поле (`openai_compat/provider.go`). У GPT-5.6+ ключ для оптимизации не нужен [док] |
| Endpoint закреплён (`provider.only=[openai]`) | `example.yaml` [код] | адекватно | У gpt-5-mini у RouterAI три endpoint'а с разной ценой (openai, openai/flex, azure: у Azure дороже чтение кэша) [док]. Без закрепления цена и кэш «плавают» |
| Модель по умолчанию `openai/gpt-5-mini` без `reasoning_effort` | `example.yaml`, README [код] | **устарело** | См. R1. В концепции основная модель до E0-Q — gpt-4o-mini, в конфиге — gpt-5-mini. Расхождение стоит свести |
| Оценка задержки «реплика → движение» 1,2–2,5 с для робота | PLAN §3 | **спорно** | В оценку заложен TTFT 0,68–0,82 с, а это время для моделей без рассуждения. У reasoning-модели с `medium` первый токен идёт после скрытых рассуждений. Оценка верна при `none`/`minimal` или для gpt-4o-mini (у лаборатории gpt-4o-mini через cloud.ru, ANALYSIS) |
| Keep-warm heartbeat (концепция, решение 8) | концепция | **спорно** | Окупаемость зависит от цены выхода. Пинг с рассуждением тратит выходные токены по цене 8× входа. У GPT-5.6+ TTL ≥ 30 мин, и heartbeat 30 мин стоит ровно на границе; холодный вызов там стоит 1,25× на вход. Пересчитать после R1 и R7 |
| `max_tokens` 1024 в конфиге PicoClaw лаборатории | PLAN §4, ANALYSIS PC8 | адекватно для gpt-4o-mini; **ошибочно** при переходе на reasoning-модель | У reasoning-моделей лимит включает рассуждение. При исчерпании ответ `incomplete` без видимого текста, но вход и рассуждение оплачены [док] |

---

## 2. Рекомендации (по приоритету)

### R1. Задать `reasoning_effort` явно — **высокий** — [частично]
- **Сейчас.** Механизм есть: `llm.extra` пробрасывается в тело запроса как есть [код]. Значение не задано, поэтому у gpt-5-mini работает умолчание провайдера. `usage.reasoning_tokens` пишется в трассу [есть].
- **Что сделать.**
  1. В конфиге прогонов задать `llm.extra.reasoning_effort`:
     - `minimal` или `low` для gpt-5-mini и gpt-5-nano (по сторонним таблицам gpt-5-mini принимает `minimal`; на странице модели OpenAI это не указано — проверить в E0-P);
     - `none` для GPT-6 Luna (иначе нет инструментов в Chat Completions [док]).

     `verbosity: low` — по желанию.
  2. Уровень держать постоянным в течение прогона: он входит в ключ кэша [док].
  3. Для паритета задать тот же уровень PicoClaw через `extra_body`. Сам `thinking_level` PicoClaw для моделей OpenAI `reasoning_effort` не выставляет [код].
- **Эффект.** На ход из 2 вызовов gpt-5-mini: 0,249 ₽ при R = 400 против 0,077 ₽ при R = 0, то есть −69 % [расчёт §4]. Плюс меньше задержка: скрытые токены декодируются до первого видимого.

  В литературе:
  - низкий уровень на рутинных агентных задачах: −43 % выходных токенов и −20 % времени, правила соблюдены (arXiv:2608.03169, препринт, 840 прогонов);
  - у GPT-5 и o-серии нет прироста точности low → medium → high на базовой арифметике (ACL 2026 Findings);
  - «overthinking» в агентных задачах ухудшает результат: −43 % затрат при +≈30 % качества при отборе (Cuadron 2025);
  - при хорошем кэше агентная нагрузка по вычислениям упирается в декодирование, а не в разбор входа (IISWC 2026; замер на своём сервере Gemma/Qwen, не на API). Для нас это довод, что выходные токены важнее входных.
- **Риск.** Качество выбора инструмента на сложных репликах. Проверка — E0-Q на живой модели: уровни minimal / low / medium на одних репликах, McNemar и TOST по протоколу.
- **Трудоёмкость.** Конфиг — минуты. Сравнение уровней в E0-Q — часть уже запланированных трат.
- **Jetson / Foxy / Py3.8.** Не зависит: настройка запроса.

### R2. Окно и маска с гистерезисом (пакетное вытеснение) — **высокий/средний** — [нет]
- **Сейчас.** `lastN(turns, W)` сдвигает начало истории на каждом новом ходе после заполнения окна. `mask` с `mask_keep_turns` маскирует на каждом ходе ровно один следующий ход [код, `userctx.View`].

  В обоих случаях меняется середина или начало истории. Весь хвост после места изменения оплачивается без скидки кэша (0,1× у gpt-5-mini).
- **Что сделать.**
  - Вытеснять пачкой: когда ходов стало W + B, срезать до W. Тогда префикс неизменен B ходов подряд.
  - Маскировать тоже пачкой: сразу B ходов.
  - Параметр `window_hysteresis_turns` (B ≈ 3–4).
  - Ввести **до предрегистрации**, так как он меняет плечи H4.
- **Основание.**
  - TokenPilot: B = 1 «вызывает преждевременное усечение, ломает постоянство раскладки и повышает промахи кэша», оптимум — B = 3. Общий эффект системы — −56…87 % стоимости, но это вклад всей системы, а не одного B.
  - Anthropic `clear_at_least`: очищать не меньше заданного объёма, иначе инвалидация кэша не окупается.
  - «Don't Break the Cache» (2026): динамику — в конец, не трогать середину.
- **Эффект.** Только для ходов в пределах TTL кэша: у gpt-5-mini 5–10 мин бездействия, у GPT-5.6+ ≥ 30 мин [док].
  - В смоуке реплики идут в среднем раз в 5,5 мин дня [ориентир], так что попадания между ходами реальны.
  - После заполнения окна каждый ход переоплачивает всю историю (до 8 ходов). С B = 3–4 — один раз за B ходов.
  - Точное число — прогоном T0 с эмуляцией кэша mock-llm. Плечи: `mask` W = 8 против W = 8, B = 4; TTL 300 и 1800 с.
- **Трудоёмкость.** ≈20–40 строк в `userctx` (`lastN`, `flatten`, `pruneTurnsLocked`) плюс тест на неизменность префикса.
- **Jetson.** Да. RAM +B ходов — килобайты.

### R3. Не хранить пустые фоновые ходы в истории — **средний** — [нет]
- **Сейчас.** `finishTurn` сохраняет фоновый ход при `history_background: true` независимо от исхода [код]. Ход `[плановая проверка 07:30] → NO_ACTION` занимает место в окне из 8 ходов.
- **Что сделать.** Сохранять фоновый ход, только если было действие, уведомление или вызов инструмента. Пустые не сохранять или сжимать в одну строку счётчика. Окно считать по «содержательным» ходам.
- **Основание.** AgentDiet: «бесполезная» и «избыточная» информация траекторий. Complexity Trap: простая очистка не хуже сводки.
- **Эффект.** По токенам мало (≈20 токенов на пустой ход [оценка]). Главное — качество: при `hybrid` за 4 тихих часа набегает 8 тиков, и окно из 8 ходов уже не содержит диалога с пользователем. Плюс меньше сдвигов окна, а значит, меньше поломок кэша (R2).
- **Трудоёмкость.** ≈10 строк плюс тест.
- **Jetson.** Да.

### R4. Убрать второй вызов «отчитайся» — **средний** — [нет]
- **Сейчас.** После вызова привода ход всегда идёт на следующий шаг LLM за текстом ответа [код, `runTurn`]. В смоуке у оракула 24 вызова `final` из 77 [ориентир: оракул добирает число вызовов до k по VisionClaw].
- **Что сделать** (два варианта, оба детерминированные).
  - (а) Встроенный инструмент `finish(reply)`. Модель вызывает его **параллельно** с командами в одном ответе: `parallel_tool_calls` по умолчанию true [док RouterAI]. Если все команды `ok`, агент отдаёт `reply` пользователю и закрывает ход. При `denied` или ошибке ход идёт к модели как сейчас.
  - (б) Шаблонный ответ из полей результата: робот — `task_end{moved_m, stopped_by}`, hw-mcp — `ok`/`denied`. Для робота: пробуждение по `task_end` заменить уведомлением-шаблоном, если `stopped_by` = цель достигнута. LLM будится только на `blocked`/`estop`/`unconfirmed`.

  Честность ответа так не падает, а растёт: текст берётся из поля, а не из пересказа модели.
- **Основание.** LLMCompiler (ICML 2024): параллельные вызовы по плану, до 3,7× по задержке и до 6,7× по стоимости против ReAct. ReWOO: план без чтения наблюдений, 5× по токенам на HotpotQA. Это доводы за «спланировать и исполнить без лишнего круга», а не прямые числа для нас.
- **Эффект.** Минус один вызов на команду; у робота «0,5 м» — с 2 (и 3 с `task_end` у EdgeCtx) до 1 [оценка]. Задержка ответа пользователю тоже минус один круг до LLM. Число — прогоном T0, у оракула нужен режим «finish».
- **Риск.** Модель может не выдавать `finish` вместе с командой. Это проверяется в E0-Q, при отказе остаётся обычный путь.
- **Трудоёмкость.** (б) ≈50 строк плюс правило гейта. (а) ≈80–120 строк плюс оракул mock-llm.
- **Jetson.** Да.

### R5. Локальный быстрый путь для частых команд — **средний** (эффект большой, риск качества) — [нет]
- **Сейчас.** Каждая реплика будит LLM [код]. В смоуке 65 из 77 вызовов — пользовательские [ориентир].
- **Что сделать.** До LLM — детерминированный разбор узкого набора интентов:
  - «включи/выключи <устройство>»;
  - «какая температура в <комнате>» — ответ из состояния L1.

  Устройства берутся из инвентаря L0. Команда идёт через тот же шлюз приводов. Неуверенный разбор отправляется в LLM. Опционально — кэш планов: после успешного хода LLM запомнить «нормализованная реплика → вызовы» и повторять только при совпадении устройства и аргументов.
- **Основание.**
  - Home Assistant «prefer handling commands locally» (релиз 2024.12, блог 13.02.2025 и 11.09.2025): простые команды раньше впустую уходили в LLM — «замедляет и тратит токены».
  - Agentic Plan Caching (NeurIPS 2025): −50,31 % стоимости и −27,28 % задержки в среднем.
  - vCache (ICLR 2026) — если когда-либо брать семантический кэш: только с гарантией ошибки, не со статическим порогом.
- **Эффект.** Доля простых команд в дне × (все вызовы и токены хода). Задержка — миллисекунды вместо секунд. Долю считать по разметке daygen.
- **Риски.** Русская морфология (GAP4 §5.3), неоднозначность, честное сравнение с PicoClaw. Это отличие системы, его и меряют, как гейт. Безопасность не страдает: шлюз тот же.
- **Трудоёмкость.** Средняя: ≈300–500 строк Go плюс тесты на русских репликах.
- **Jetson.** Да. ML не нужен.

### R6. Маршрутизация по типу пробуждения: фон во flex / дешёвую модель — **средний/низкий** — [нет]
- **Сейчас.** Один `model` и один `extra` на все ходы [код]. Фоновых пробуждений с исходом `NO_ACTION` много: в смоуке оракул ответил «noop» на 8 из 12 событий [ориентир].
- **Что сделать.** Переопределять `service_tier`/endpoint и, по желанию, модель по приоритету хода:
  - `background` → `service_tier: flex` (−50 %, медленнее, без гарантии мощности);
  - `user`/`safety` → `default`.

  RouterAI прямо рекомендует такую схему [док, service-tiers]. Обучаемый маршрутизатор не нужен: класс известен из гейта.
- **Важно.** Flex без мощности возвращает ошибку (средства не списываются) [док]. Такая ошибка не должна размыкать общий предохранитель `llm.breaker`. Нужен откат фонового хода на `default` или отдельный счётчик сбоев flex. Иначе пользовательские ходы попадут в «отложено».
- **Основание.**
  - FrugalGPT, RouteLLM, Unified routing/cascading (обзор R6): выигрыш зависит от смеси запросов.
  - Обзор маршрутизации arXiv:2502.00409.
  - Обучаемые маршрутизаторы — BERT у RouteLLM, Arch-Router 1,5B — на Jetson в бюджет робота не помещаются (§3).
- **Эффект.** Доля фоновых вызовов × 50 %. В смоуке это 16 % вызовов, то есть ≈8 % ₽ [ориентир]. Зависит от heartbeat и гейта.
- **Трудоёмкость.** ≈60–100 строк (переопределение по приоритету, учёт в предохранителе) плюс тест.
- **Jetson.** Да.

### R7. Честный учёт в стенде: reasoning-токены и запись в кэш — **средний** (дёшево) — [частично]
- **Сейчас.**
  - `call_cost_rub` = (вход − кэш)·вход + кэш·чтение + выход·выход, без `input_cache_write` [код].
  - mock-llm `--reasoning-tokens 0` по умолчанию, `--cache-ttl 300` [код].
  - У gpt-6-luna и gpt-5.6-luna RouterAI отдаёт `input_cache_write` (13,46 ₽/1M против 10,77) [док].
- **Что сделать.**
  1. Добавить надбавку за запись в `call_cost_rub` для моделей с полем `input_cache_write`. Записанными считать некэшированные токены промпта ≥ 1024.
  2. Отчитывать ₽ полосой по R ∈ {0; R_minimal; R_medium}, где R_* — медианы `reasoning_tokens` из E0-P.
  3. Ввести профили mock-llm «до 5.6» (TTL 300–600 с, без надбавки) и «5.6+» (TTL 1800 с, запись 1,25×).
- **Эффект.** Не экономия, а правильные прогнозы. Без этого ₽ для gpt-5-mini с `medium` занижен до ≈3× [расчёт §4], а у luna не видна цена холодных вызовов.
- **Трудоёмкость.** ≈30–60 строк Python плюс тесты паритета (`bench/tests/test_costmodel.py` сверяет порт с исходником — нужна версия формулы).

### R8. Responses API в LLM-клиенте — **средний, условно** — [нет]
- **Сейчас.** Клиент работает только с `chat/completions` [код].

  У RouterAI:
  - gpt-5-mini и gpt-5-nano поддерживают `chat` и `responses`;
  - у gpt-6-luna и gpt-5.6-luna эндпоинты `openai` (включая flex и priority) — только `responses`; у azure и bedrock список пуст [док, `/models/{id}/endpoints`, 20.10].

  OpenAI: GPT-6 Luna и Sol поддерживают инструменты в Chat Completions только при `reasoning_effort: none` [док].
- **Когда делать.** Если в E0-Q лучшей по ₽ при приемлемом качестве окажется luna: 10,77 / 1,08 / 53,85 ₽ против 26,93 / 2,69 / 215,41 у gpt-5-mini, кэш ≥ 30 мин.
- **Что ещё даст.** `previous_response_id` мог бы резко сократить исходящий трафик: сейчас каждое обращение шлёт весь промпт, в смоуке ≈14,6 КБ вверх на вызов против ≈0,8 КБ вниз [ориентир]. Поддерживает ли RouterAI хранение состояния — в документации (`llms-full.txt`) **не найдено**. Хранение у провайдера — отдельный вопрос приватности.
- **Трудоёмкость.** Высокая: ≈300–500 строк Go (формат items: `function_call`/`function_call_output`, usage `input_tokens_details`/`output_tokens_details`) плюс эндпоинт `/v1/responses` в mock-llm.
- **Jetson.** Да.

### R9. Расширить пробу E0-P — **высокий** (дёшево, снимает неизвестные) — [частично: проба `cached_tokens` уже в плане]
Добавить в пробу:
1. `reasoning_tokens` и задержку на наших промптах при minimal, low, medium (gpt-5-mini) и none (luna) — по 10–20 запросов.
2. Принимает ли RouterAI `prompt_cache_key` (200 или 4xx) и меняет ли он долю попаданий.
3. Кривую TTL. Если `cached_tokens` не придёт — по TTFT, методом статистического аудита из Gu et al. (ICML 2025): попадание в кэш даёт измеримо меньшую задержку.
4. Доступность и задержку `service_tier: flex` для gpt-5-mini.
5. Ответ `/v1/responses` для luna.

Трудоёмкость — скрипт ≈100 строк. Деньги — единицы–десятки ₽.

### R10. Профиль как дописывание, а не перезапись — **низкий** — [частично]
Сегмент `profile` стоит перед историей. Любая консолидация (раз в `profile_consolidate_s` = 600 с после изменения) ломает кэш истории [код]. Вариант:
- новые факты — строками в хвост (ACE: append-only контекст дружит с кэшем, 91,8 % входа из кэша);
- перестройка профиля — редко, в простое.

Эффект мал: факты меняются редко. Трудоёмкость ≈30 строк.

### R11. Обрезка результата инструмента: начало + конец — **низкий** — [частично]
Сейчас `truncateRunes` оставляет только начало [код]. TokenPilot оставляет 600 символов начала и 400 конца — у JSON-ответов и журналов важное часто в конце. ≈10 строк.

---

## 3. Что не делать и почему

| Метод | Почему не для нас |
|---|---|
| LLMLingua / LLMLingua-2 (Findings ACL 2024) | Промпты короткие и в JSON (G3: метод проверен на прозе). Компрессору нужна своя модель-энкодер. На Jetson RAM занята стеком робота (1866,6 МиБ уникальной памяти, запас с GNOME 539 МиБ — PLAN §13.5). Сжатие под запрос ломает кэш на каждом вызове (Cache-Aware Prompt Compression, arXiv:2607.15516) |
| MemGPT / Letta, Mem0, A-MEM | Лишние вызовы LLM на запись в память. Объём памяти у нас малый: SQLite, факты, сводка. Разбор в D §2 |
| ACON (ICML 2026) | Нужны оптимизатор (o3) и дистилляция компрессора. Компромисс «дешевле, но медленнее»: медиана 73 → 88–102 с на задачу |
| Обучаемые маршрутизаторы (RouteLLM-BERT/MF, Arch-Router 1,5B) | RAM на Jetson. Маршрутизатору на эмбеддингах нужен ещё один сетевой вызов. Класс хода и так известен — R6 |
| Context-folding, MEM1 (RL-обучение модели) | Требуют своей обученной модели. У нас внешняя API-модель |
| Исполнение кода через MCP (Anthropic, 04.11.2025: 150 000 → 2 000 токенов) | Нужна песочница для кода модели рядом с приводами — против принципа «LLM вне функции безопасности». Каталоги маленькие (12–27 инструментов) |
| Динамический отбор инструментов (RAG-MCP, tool search) | Каталог мал. Смена `tools` ломает кэш [док]. PicoClaw-Discovery в T0 выключен по той же причине |
| TSCG — компиляция схем в текст (arXiv:2605.04107) | Выигрыш показан в основном на малых моделях 4–14B. Для GPT-5.2 авторы называют результат «чувствительным к операторам». Без родного вызова функций теряется структура `tool_calls`. Наши описания уже короткие [есть] |
| «Богаче описания инструментов» | По arXiv:2602.14878: дополненные описания дают +5,85 п.п. успеха, но +67,46 % шагов и регресс в 16,67 % случаев. Держать описания короткими, но с ясной целью — аудит описаний robot-mcp/hw-mcp по рубрике статьи достаточен |
| Семантический кэш ответов на вопросы о состоянии | Ответ зависит от текущего состояния: кэш выдаст устаревшее. Для вопросов о состоянии лучше быстрый путь из L1 (R5) |
| Jev / `decisions` RouterAI как фильтр фона | Фирменная модель без открытой оценки качества, ещё один сетевой вызов и привязка к провайдеру. Можно упомянуть в обзоре, но не в ядре |

---

## 4. Расчёт: сколько стоит рассуждение (прайс RouterAI, 20.10.2026)

Ход из 2 вызовов:
- вызов 1 — холодный: 2000 токенов входа, 40 выхода;
- вызов 2 — 2100 входа, из них 1920 из кэша, 20 выхода;
- R — скрытые reasoning-токены на вызов.

Надбавка за запись 1,25× — только у gpt-6-luna. Цены ₽/1M (вход / кэш / выход): из `GET /api/v1/models`.

Формула вызова: (вход − кэш) · (цена записи, если она есть и вход ≥ 1024, иначе цена входа) + кэш · цена чтения + (выход + R) · цена выхода. Это `call_cost_rub` плюс надбавка за запись. Скрипт в репозиторий не клали — по формуле расчёт повторяется за минуту.

| Модель | R = 0 | R = 100 | R = 400 | Доля выхода при R = 400 |
|---|---:|---:|---:|---:|
| gpt-5-mini | 0,077 | 0,120 | 0,249 | 74 % |
| gpt-5-mini, flex | 0,038 | 0,060 | 0,125 | 74 % |
| gpt-5-nano | 0,015 | 0,024 | 0,050 | 74 % |
| gpt-4.1-nano (без рассуждения) | 0,031 | — | — | — |
| gpt-4o-mini (без рассуждения) | 0,055 | — | — | — |
| gpt-6-luna (`none`; только Responses у OpenAI-endpoint RouterAI) | 0,035 | — | — | — |

R = 400 — допущение концепции §14, не замер. Реальное R на наших промптах даст E0-P (R9).

Цены RouterAI с 06.10 сдвинулись: gpt-5-mini 27,60 → 26,93 ₽/1M входа (−2,4 %). Замороженный прайс на дату старта (решение 10 концепции) оправдан.

---

## 5. Что изменилось у провайдеров с заметки D (06.10) — [док, 20.10.2026]

1. **OpenAI, модели до GPT-5.6:**
   - `prompt_cache_retention`: `in_memory` — 5–10 мин бездействия, максимум час; `24h` — обычно ≈30 мин, до суток;
   - `24h` есть у gpt-5.5, 5.4, 5.2, 5.1*, 5, 4.1 — mini и nano в списке нет;
   - у организаций без ZDR умолчание — `24h` там, где он поддерживается;
   - `prompt_cache_key` — ≈15 запросов в минуту на ключ, ключ «влияет на маршрутизацию, не гарантирует попадание»;
   - минимальная длина зависит от инструментов, `reasoning_effort` и `verbosity`.
2. **OpenAI, GPT-5.6+:**
   - кэш ≥ 30 мин после последнего использования (`prompt_cache_options.ttl`, только `30m`);
   - запись 1,25×, чтение 0,1× (у GPT-6.1 Sol — 0,05×);
   - минимум — 1024 *видимых* токена;
   - `prompt_cache_key` для оптимизации не нужен.
3. **Ключ кэша ломают:** `model`, `tools` (имена, описания, схемы, порядок), `parallel_tool_calls`, формат ответа, `reasoning.effort`, `verbosity`, компактизация.
4. **OpenAI, reasoning:**
   - у GPT-6 Sol и Luna вызов функций в Chat Completions — только с `reasoning_effort: none`; у GPT-6 Astra и 6.1 Sol инструменты — только в Responses;
   - при исчерпании `max_output_tokens` ответ может быть пустым, но оплаченным.
5. **RouterAI:**
   - `service_tier` flex (≈−50 %; без мощности — ошибка без списания) и priority (без мощности — тихий откат на default) работают в Chat Completions и Responses;
   - фактический режим возвращается в поле ответа `service_tier` — агент его уже пишет в трассу [код];
   - `prompt_cache_key`, хранение состояния Responses и правила кэша в документации RouterAI не описаны (поиск по `llms-full.txt`) — **не найдено**.

---

## 6. Применимость к Jetson Nano 4 ГБ, Foxy, Python 3.8

- **R1–R11 применимы.** Это конфиг или код статического Go-бинарника EdgeCtx (linux/arm64, ядро 4.9) и стенда на ПК. От ROS 2 Foxy и Python 3.8 не зависят.
- **R1 и паритет для PicoClaw лаборатории** — только конфиг (`extra_body`). Если лаборатория перейдёт на reasoning-модель, поднять `max_tokens` выше 1024 или задать `reasoning_effort`.
- **Всё с локальной нейросетью** — LLMLingua, маршрутизаторы, TGL-триггер ≈220 МиБ из arXiv:2605.30152 — не применимо при текущем бюджете памяти робота.

---

## 7. Источники

**Документация (снято 20.10.2026):**
1. OpenAI. Prompt caching. https://developers.openai.com/api/docs/guides/prompt-caching
2. OpenAI. Reasoning models. https://developers.openai.com/api/docs/guides/reasoning
3. OpenAI. Using the latest model (миграция на GPT-6). https://developers.openai.com/api/docs/guides/latest-model
4. OpenAI. Pricing. https://developers.openai.com/api/docs/pricing
5. OpenAI. GPT-6 Luna (страница модели). https://developers.openai.com/api/docs/models/gpt-6-luna; GPT-5 mini: https://developers.openai.com/api/docs/models/gpt-5-mini
6. RouterAI.
   - Каталог: `GET https://routerai.ru/api/v1/models`;
   - endpoint'ы моделей: `GET https://routerai.ru/api/v1/models/{id}/endpoints` (openai/gpt-5-mini, gpt-5-nano, gpt-6-luna, gpt-5.6-luna);
   - режимы обработки: https://routerai.ru/docs/guides/overview/service-tiers;
   - параметры запроса: https://routerai.ru/docs/guides/overview/parameters;
   - полная документация: https://routerai.ru/llms-full.txt.
7. Anthropic. Context editing (`clear_tool_uses_20250919`, `clear_at_least`). https://platform.claude.com/docs/en/build-with-claude/context-editing
8. Jones A., Kelly C. Code execution with MCP: Building more efficient agents. Anthropic Engineering, 04.11.2025. https://www.anthropic.com/engineering/code-execution-with-mcp
9. Ji Y. ('Peak'). Context Engineering for AI Agents: Lessons from Building Manus. Manus blog, 18.07.2025. https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
10. Home Assistant.
    - Speech-to-Phrase brings voice home — Voice chapter 9, 13.02.2025: https://www.home-assistant.io/blog/2025/02/13/voice-chapter-9-speech-to-phrase/;
    - Building the AI-powered local smart home, 11.09.2025: https://www.home-assistant.io/blog/2025/09/11/ai-in-home-assistant
11. PicoClaw v0.3.1 (тег `2cf030d`): `pkg/providers/openai_compat/provider.go` (`prompt_cache_key` только для OpenAI и Azure; `applyThinkingControl`), `pkg/config/config.go` (`extra_body`).

**Кэш и контекст:**

12. Lumer E., Nizar F., Jangiti A., Frank K. et al. Don't Break the Cache: An Evaluation of Prompt Caching for Long-Horizon Agentic Tasks. arXiv:2601.06007, 2026.
13. Xu B., Xue Z., Chen D., Fu C. et al. TokenPilot: Cache-Efficient Context Management for LLM Agents. arXiv:2606.17016, 2026 (EMNLP 2026 Findings — по комментарию arXiv).
14. Song Y. Cache-Aware Prompt Compression: A Two-Tier Cost Model for LLM API Caching. arXiv:2607.15516, 2026 (препринт).
15. Lindenbauer T., Slinko I., Felder L., Bogomolov E., Zharov Y. The Complexity Trap: Simple Observation Masking Is as Efficient as LLM Summarization for Agent Context Management. arXiv:2508.21433; DL4C @ NeurIPS 2025.
16. Kang M., Chen W.-N., Han D., Inan H. A. et al. ACON: Optimizing Context Compression for Long-horizon LLM Agents. arXiv:2510.00615; ICML 2026.
17. Zhang Q., Hu C., Upasani S., Ma B. et al. Agentic Context Engineering. arXiv:2510.04618; ICLR 2026.
18. Xiao Y.-A., Gao P., Peng C., Xiong Y. Reducing Cost of LLM Agents with Trajectory Reduction // Proc. ACM Softw. Eng. 3 (FSE), 2026. P. 1241–1263. DOI 10.1145/3797084.
19. Packer C., Wooders S., Lin K., Fang V. et al. MemGPT: Towards LLMs as Operating Systems. arXiv:2310.08560, 2023.
20. Pan Z., Wu Q., Jiang H., Xia M. et al. LLMLingua-2. arXiv:2403.12968; Findings of ACL 2024.
21. Gu C., Li X. L., Kuditipudi R., Liang P. et al. Auditing Prompt Caching in Language Model APIs. arXiv:2502.07776; ICML 2025.
22. Yuan Y., Nayak A., Kundu S., Talati N. Agentic AI Workload Characteristics. arXiv:2605.26297; IISWC 2026.

**Число вызовов и планирование:**

23. Kim S., Moon S., Tabrizi R., Lee N. et al. An LLM Compiler for Parallel Function Calling. arXiv:2312.04511; ICML 2024.
24. Xu B., Peng Z., Lei B., Mukherjee S. et al. ReWOO: Decoupling Reasoning from Observations for Efficient Augmented Language Models. arXiv:2305.18323, 2023.
25. Gim I., Lee S., Zhong L. Asynchronous LLM Function Calling (AsyncLM). arXiv:2412.07017, 2024.
26. Zhang Q., Wornow M., Wan G., Olukotun K. Agentic Plan Caching: Test-Time Memory for Fast and Cost-Efficient LLM Agents. arXiv:2506.14852; NeurIPS 2025.
27. Schroeder L. G., Desai A., Cuadron A., Chu K. et al. vCache: Verified Semantic Prompt Caching. arXiv:2502.03771; ICLR 2026.

**Инструменты:**

28. Yuan S., Song K., Chen J., Tan X. et al. EASYTOOL: Enhancing LLM-based Agents with Concise Tool Instruction. arXiv:2401.06201, 2024.
29. Hasan M. M., Li H., Rajbahadur G. K., Adams B. et al. Model Context Protocol (MCP) Tool Descriptions Are Smelly! arXiv:2602.14878, 2026.
30. Sakizli F. TSCG: Deterministic Tool-Schema Compilation for Agentic LLM Deployments. arXiv:2605.04107, 2026.

**Маршрутизация и каскады** (подробно — R6):

31. Chen L., Zaharia M., Zou J. FrugalGPT. arXiv:2305.05176.
32. Ong I., Almahairi A., Wu V., Chiang W.-L. et al. RouteLLM. arXiv:2406.18665.
33. Dekoninck J., Baader M., Vechev M. A Unified Approach to Routing and Cascading for LLMs. arXiv:2410.10347.
34. Varangot-Reille C., Bouvard C., Gourru A., Ciancone M. et al. Doing More with Less: A Survey on Routing Strategies for Resource Optimisation in LLM-Based Systems. arXiv:2502.00409, 2025.
35. Tran C., Paracha S., Hafeez A., Chen S. Arch-Router: Aligning LLM Routing with Human Preferences. arXiv:2506.16655, 2025.

**Событийный вызов:**

36. Tabuada P. Event-Triggered Real-Time Scheduling of Stabilizing Control Tasks // IEEE TAC. 2007. Vol. 52, № 9. P. 1680–1685. DOI 10.1109/TAC.2007.904277.
37. Heemels W. P. M. H., Johansson K. H., Tabuada P. An introduction to event-triggered and self-triggered control // IEEE CDC 2012. P. 3270–3285. DOI 10.1109/CDC.2012.6425820.
38. Miskowicz M. Send-On-Delta Concept: An Event-Based Data Reporting Strategy // Sensors. 2006. Vol. 6, № 1. P. 49–63. DOI 10.3390/s6010049.
39. Wang Z. Uncertainty-Aware Sequential Decision Rules for Event-Triggered LLM Invocation in Streaming Systems. arXiv:2607.13048; ECML PKDD 2026.
40. Bao R., Sun Y., Chen Z., Yang F. et al. E³-Agent: An Executable and Evolving Agent for Resource Management of Edge Generative Inference. arXiv:2605.27428, 2026.
41. Liu X., Zhang R., Abdi A. H., Galley M. et al. Do Proactive Agents Need an LLM to Decide When to Act? arXiv:2605.30152, 2026.
42. Hu B., Zhao C., Zhang P., Zhou Z. et al. Enabling Intelligent Interactions between an Agent and an LLM: A Reinforcement Learning Approach (When2Ask). arXiv:2306.03604.
43. Guo Y., Wang Y.-J., Zha L., Chen J. DoReMi: Grounding Language Model by Detecting and Recovering from Plan-Execution Misalignment. arXiv:2307.00329; IROS 2024.
44. Paglieri D., Cupiał B., Cook J., Piterbarg U. et al. Learning When to Plan: Efficiently Allocating Test-Time Compute for LLM Agents. arXiv:2509.03581, 2025.

**Рассуждение:**

45. Cuadron A., Li D., Ma W., Wang X. et al. The Danger of Overthinking: Examining the Reasoning-Action Dilemma in Agentic Tasks. arXiv:2502.08235, 2025.
46. Chen X., Xu J., Liang T., He Z. et al. Do NOT Think That Much for 2+3=? On the Overthinking of o1-Like LLMs. arXiv:2412.21187, 2024.
47. Sui Y., Chuang Y.-N., Wang G., Zhang J. et al. Stop Overthinking: A Survey on Efficient Reasoning for LLMs. arXiv:2503.16419; TMLR 2025.
48. Srivastava G., Hussain A., Srinivasan S., Wang X. Do LLMs Overthink Basic Math Reasoning? arXiv:2507.04023; Findings of ACL 2026.
49. Xu X., Wu W. Low Reasoning Effort Is Enough for Routine Office Work by Language-Model Agents. arXiv:2608.03169, 2026 (препринт).
50. Wani S. G., Dholakia A., Ellison D. The Reasoning Tax: Token Economics of LLM Reasoning. arXiv:2608.26235; TPCTC 2026.

**Не найдено / не проверено:**
- официальное умолчание и допустимые значения `reasoning_effort` для gpt-5-mini на странице модели OpenAI (есть только у сторонних источников);
- правила кэша, `prompt_cache_key` и `previous_response_id` у RouterAI;
- venue When2Ask и EASYTOOL (в комментарии arXiv не указано).
