# R3. PicoClaw на роботе: почему «надо перезагружать» и годится ли он для реального времени

Срез: 14.10.2026. Автор: Claude (субагент R3). Объект: PicoClaw **v0.3.1** (тег `2cf030d2`, go-sdk MCP v1.6.1).
Исходники: `bench/baselines/vendor/src/picoclaw` (только чтение), бинарник
`bench/baselines/vendor/picoclaw/v0.3.1/darwin-arm64/picoclaw` (SHA-256 `21eea141…5c42c1` совпал с `versions.json`).

Пометки: **[код]** — прочитано в исходниках тега (файл:строка от корня исходников PicoClaw);
**[эксп.]** — воспроизведено на Mac настоящим бинарником v0.3.1 (3 повтора, §9); **[веб]** — внешний источник со ссылкой;
**[оценка]** — мой расчёт или вывод, не измерение. Ключ RouterAI не использовался: LLM — локальная заглушка, 0 ₽.
Все числа задержек и памяти сняты на Mac (M4 Pro), при параллельной работе других агентов — **ориентиры, не результаты**.

---

## 0. Коротко

1. **Упавший stdio MCP-сервер PicoClaw не поднимает никогда.** Переподключение есть только для HTTP-сессии, которую
   сервер «забыл» (HTTP 404 → `ErrSessionMissing`) [код `pkg/mcp/manager.go:539-561, 598-606`]. Для stdio после смерти
   процесса каждый вызов отвечает `failed to call tool: connection closed … EOF` до перезапуска всего PicoClaw [эксп. 3/3].
2. **Одна строка `print()` в stdout stdio-сервера убивает соединение навсегда** (`invalid trailing data at the end of
   stream`), процесс сервера завершается [эксп. 3/3]. При этом сам вызов на сервере уже выполнился — робот поехал бы,
   а агент считает это ошибкой. `predict_live.py` печатает в stdout (`print` в строках 70, 282, 464, 611, 634, 639) —
   если его или YOLO обернуть в stdio-MCP, это первый кандидат на «после пары использований».
3. **У вызова MCP-инструмента нет таймаута** [код: `MCPServerConfig` без поля timeout, `pkg/config/config.go:1222-1247`;
   `ToolRegistry.ExecuteWithContext` без дедлайна, `pkg/tools/registry.go:251-322`; ход — `context.WithCancel` без
   дедлайна, `pkg/agent/turn_coord.go:18`]. Зависший инструмент держит ход бесконечно (90 с до ручного `/stop` в опыте).
4. **Один зависший ход блокирует ВСЕХ:** по умолчанию `max_parallel_turns` = 1 (`pkg/agent/agent_init.go:60-64`), реплики
   той же сессии уходят в очередь «steering» (не больше 10, переполнение молча отбрасывается — только запись в лог,
   `pkg/agent/agent.go:199-212`, `pkg/agent/steering.go:27,78-80`), реплики других сессий/каналов не обрабатываются вовсе
   [эксп.: 0 ответов, 0 вызовов LLM]. `/stop` снимает ход, но **очищает очередь** — отправленные жесты теряются.
5. **`/reload` (команда в чате, `POST /reload`, `gateway.hot_reload`) убивает stdio MCP-серверы.** Перезагрузка
   конфига заново запускает их с контекстом `context.WithTimeout(…, 30 s)` + `defer reloadCancel()`
   (`pkg/gateway/gateway.go:619-622`), а процесс stdio создаётся через `exec.CommandContext(ctx, …)`
   (`pkg/mcp/manager.go:404`) → через доли секунды после «Config reload triggered!» новый процесс сервера убит, все
   вызовы — `connection closed` [эксп. 4/4: на упавшем и на здоровом сервере; для HTTP-транспорта перезагрузка
   безвредна, 1/1]. То есть «перезагрузить конфиг» не лечит, а ломает робота.
6. **Если MCP-сервер не поднялся при старте, агент превращается в «зомби»:** `AgentLoop.Run` возвращает ошибку
   (`pkg/agent/agent.go:151-153`), шлюз запускает его как `go agentLoop.Run(ctx)` и ошибку игнорирует
   (`pkg/gateway/gateway.go:253`), а `/health` и `/ready` уже отвечают 200 (`gateway.go:222`). Ни одна реплика не
   обрабатывается, LLM не вызывается [эксп. 3/3]. Исправление апстрима (PR #2725 «make MCP initialization failure
   non-fatal») закрыто **без слияния** [веб].
7. **Для HTTP-транспорта есть самовосстановление:** перезапуск сервера → следующая реплика проходит (404 → reconnect);
   вызов при лежащем сервере — одна ошибка, потом сам переподключается; зависший HTTP-сервер, убитый внешним
   сторожем, сразу возвращает ошибку зависшему вызову, следующие проходят [эксп.: «лежит» 3/3, «завис+сторож» 3/3,
   «перезапуск» 1/1]. Отсюда главный практический
   рецепт: **MCP по streamable HTTP + внешний супервизор (systemd `Restart=always` + сторож) + сервер поднимать до PicoClaw**.
8. **Событий нет.** MCP-клиент создаётся без обработчиков (`mcp.NewClient(…, nil)`, `pkg/mcp/manager.go:337-340`),
   ресурсы не читаются, подписок нет; для `type: http` standalone SSE выключен (`:351-363`). Жест может дойти до
   агента только как **входящее сообщение канала** (MQTT, TCP-канал MaixCAM, Telegram и т. п.) — и каждое такое
   сообщение = полный ход LLM, без фильтра, дебаунса и локальных правил (хуки не умеют ответить вместо LLM,
   `pkg/agent/hooks.go:28-33`, `pipeline_llm.go:83-119`).
9. **Реальное время через PicoClaw недостижимо по построению**: жест → 2 вызова LLM (вызов инструмента + финальный ответ)
   ≈ 2×(1,3–2,3 с) по замерам ROSClaw (другой стек, облачная LLM) [веб, arXiv:2603.26997: «Mean per-turn LLM latency
   was 1.3–2.3 s (P95 ≤3.8 s)»] плюс движение 0,5–1,5 с (R2) → **≈3–6 с от жеста до движения** [оценка]. При сбоях
   провайдера — до ≈6 мин до сообщения об ошибке (таймаут 120 с × 3 попытки + паузы, §3.2) [код, оценка].
10. **Стоимость:** один запрос PicoClaw даже в минимальной конфигурации ≈3,1–3,6 тыс. входных токенов o200k (схемы
   15 инструментов — 2 580, системный промпт — 531) [эксп.]; жест ≈6,4–7 тыс. входных токенов; на потоке жестов
   +26 % вызовов LLM уходит на сводки истории (40 жестов → 101 вызов, из них 21 — сводки) [эксп.].
11. **Память PicoClaw не проблема:** шлюз 35–41 МБ RSS на Mac [эксп.], ≈31 МБ пик на RPi 4 (Zenodo 2026, по
    `AGENT_LANDSCAPE.md`), PSS 47,6 МиБ в ВМ sbc (HANDOFF §7). Python-сервер MCP (FastMCP) сам по себе ≈56–58 МБ
    RSS [эксп., Mac] — больше агента. На Jetson Nano 4 ГБ основной потребитель — YOLO+SNN/CUDA, не агент.
12. **Апстрим почти стоит:** последний релиз — v0.3.1 (03.07.2026); в `main` после него 50 коммитов, последний
    19.08.2026, `pkg/mcp` не менялся; PR с health-check/переподключением MCP (#1666), неблокирующей инициализацией MCP
    (#2725) и обновлением go-sdk до 1.8.0 (#3388) закрыты без слияния [веб, GitHub API]. Ждать исправлений не стоит.
13. **Ещё один «глухой» сценарий — Telegram через прокси:** у HTTP-клиента канала нет таймаутов, long-poll на полуоткрытом
    соединении висит вечно, бот «глохнет» до перезапуска, `/health` = ok [веб, issue #3422 открыт 11.10.2026; код
    `pkg/channels/telegram/telegram.go:92-109`]. В РФ Telegram часто через прокси/VPN — релевантно.

**Вывод для лаборатории.** PicoClaw можно сделать «непрерывно работающим» конфигом и внешним супервизором (§7.1),
но «помахал рукой — поехал» без задержек через него не получится: рефлекс «жест → движение» должен замыкаться
локально, а LLM — подключаться для смысловых запросов. Это ровно то, что уже есть в EdgeCtx (событийный гейт,
локальные правила, переподключение MCP, таймауты) — §7.3.

---

## 1. Версия PicoClaw и что изменилось после v0.3.1

| Факт | Источник |
|---|---|
| Последний релиз — **v0.3.1**, опубликован 2026-07-03T07:37Z; более новых релизов нет (есть только `nightly` от 2026-07-02) | [веб] `gh api repos/sipeed/picoclaw/releases` (14.10.2026) |
| `main` = v0.3.1 + 50 коммитов, последний `bbf6893c` 2026-08-19; `pushed_at` 2026-10-09 (пуш не в main) | [веб] `gh api repos/sipeed/picoclaw/compare/v0.3.1...main`, `/commits/main` |
| Изменения после тега, касающиеся контекста/сессий: только `legacyContextManager.Clear` ищет владельца сессии вместо агента по умолчанию (`/clear` для маршрутизированных агентов, PR #3224) | [веб] compare v0.3.1...main, патч `pkg/agent/context_legacy.go` |
| `pkg/mcp` после тега не менялся; bump go-sdk 1.6.1→1.8.0 (#3388) закрыт без слияния 2026-10-09 | [веб] compare; `gh api repos/sipeed/picoclaw/pulls/3388` (`merged=false`) |
| PR #2725 «make MCP initialization failure non-fatal»: в описании — «the agent loop exits immediately, leaving the application in a zombie state»; закрыт без слияния 2026-05-20 | [веб] `gh api …/pulls/2725` |
| PR #1666 «Refactor MCP Manager: Process Lifecycle, Resilience…» (health-check через ListTools, Online/Offline, переподключение с экспоненциальной паузой 1–16 с) — закрыт без слияния 2026-05-04 | [веб] `gh api …/pulls/1666` |
| PR #2664 «retry tool calls on lost HTTP sessions» — **слит** 2026-04-25 (это и есть переподключение по 404, которое мы видим) | [веб] `gh api …/pulls/2664` (`merged=true`) |
| Issue #3404 (28.09.2026): сборка исправлений надёжности, PR закрывает stale-бот; в планах — «async summarization drops messages appended during the LLM call» | [веб] `gh api …/issues/3404` |
| Issue #3422 (открыт 11.10.2026): Telegram-канал «stops receiving messages forever», `/health` ok, аптайм 363 ч; версия v0.3.1 | [веб] `gh api …/issues/3422` |

Ссылки: https://github.com/sipeed/picoclaw/releases , https://github.com/sipeed/picoclaw/pull/2725 ,
https://github.com/sipeed/picoclaw/pull/1666 , https://github.com/sipeed/picoclaw/pull/2664 ,
https://github.com/sipeed/picoclaw/issues/3404 , https://github.com/sipeed/picoclaw/issues/3422 ,
https://github.com/sipeed/picoclaw/pull/3388 .

**Какая версия стоит в лаборатории — неизвестно** (вопрос §8). Если старше v0.2.7 (до 25.04.2026), нет даже
переподключения HTTP-сессии.

---

## 2. MCP в PicoClaw: подключение, отказы, таймауты

### 2.1. Как подключается [код]

* Конфиг `tools.mcp.servers.<имя>`: `command`/`args`/`env`/`env_file` (stdio) или `url`/`headers` (HTTP), `type`
  (`stdio` | `http`/`streamable-http` | `sse`; по умолчанию stdio при `command`, sse при `url`), `deferred`.
  **Поля таймаута нет** (`pkg/config/config.go:1222-1247`).
* Инициализация — один раз (`sync.Once`) при старте `AgentLoop.Run` или первом `ProcessDirect`/`ProcessHeartbeat`
  (`pkg/agent/agent_mcp.go:78-131`). Серверы подключаются параллельно (`pkg/mcp/manager.go:205-251`); упавшие на старте
  **повторно не пробуются**; если упали все — ошибка (`:259-269`), менеджер закрывается, `initErr` кэшируется
  (`agent_mcp.go:118-131`) до горячей перезагрузки конфига (`agent.go:453-475`).
* stdio: `exec.CommandContext(ctx, …)` + свой транспорт `isolatedCommandTransport` (`manager.go:394-445`,
  `isolated_command_transport.go`): построчный JSON из stdout ребёнка; любой не-JSON в stdout → ошибка чтения
  → соединение закрыто (`isolated_command_transport.go:114-138`). stderr ребёнка не подключён (уходит в /dev/null) —
  трассы падения сервера в логах PicoClaw не видно.
* HTTP: `StreamableClientTransport`, для `type: http` standalone SSE выключен (`manager.go:351-375`); свой
  `http.Client` с заголовками без `Timeout` (`:378-391`).
* Каталог инструментов снимается **один раз** при подключении (`listServerTools`, `:566-596`) и регистрируется в реестре
  агента (`agent_mcp.go:140-205`); уведомления `tools/list_changed` не обрабатываются (`NewClient(…, nil)`, `:337-340`).
* Вызов: `MCPTool.Execute` → `Manager.CallTool` (`pkg/tools/integration/mcp_tool.go:255-281`, `manager.go:507-564`).
  Повтор с переподключением — **только** если ошибка — `mcp.ErrSessionMissing` или содержит «session not found»
  (`manager.go:541-558, 598-606`). Для stdio такой ошибки не бывает → нет восстановления.
* Результат: текст >16 КиБ символов уходит в файл `workspace/.artifacts/mcp/…` (без очистки: «TODO: Add lifecycle
  cleanup/retention», `mcp_tool.go:452`); картинки — в медиа-хранилище (`:377-389`).

### 2.2. Что происходит при отказах — эксперименты [эксп.]

Стенд: шлюз PicoClaw v0.3.1 + MQTT-канал (mosquitto на 127.0.0.1) + заглушка LLM (вызывает `robot_nudge`, потом
отвечает текстом) + Python FastMCP-сервер (mcp 1.30) с управляемым отказом. Конфиг — результат `picoclaw onboard`
+ модель/канал/MCP, веб-инструменты выключены, heartbeat выключен. Каждый сценарий — 3 повтора, результат одинаков.

| Сценарий | Транспорт | Отказ | Что видит пользователь (реплики по порядку) | Сервер перезапущен? | `/health`, `/ready` |
|---|---|---|---|---|---|
| ok | stdio | нет | ok ok ok; 0,11–0,13 с на жест (2 вызова LLM + 1 MCP, LLM локальная) | — | 200 / 200 |
| die | stdio | процесс сервера завершился на 3-м вызове | ok ok ERR ERR ERR (`calling "tools/call": EOF`, затем `connection closed … client is closing: EOF`) | **нет** | 200 / 200 |
| noise | stdio | на 2-м вызове 1 строка в stdout | ok ERR ERR ERR (`invalid trailing data at the end of stream`); процесс сервера после этого завершён; **вызов на сервере выполнен** | **нет** | 200 / 200 |
| hang | stdio | инструмент завис на 2-м вызове | ok, — , — , — (ожидание по 30 с); реплики 3–4 до LLM не дошли; `/stop` → «Task stopped. "жест 2…" was canceled.»; следующая реплика снова висит (stdio-сервер занят синхронным инструментом, FastMCP вызывает sync-функцию прямо в цикле событий) | нет | 200 / 200 |
| hang + другая сессия | stdio | то же, реплики 3–4 от другого MQTT-клиента | ok, —, —, — — **другая сессия тоже без ответа** (1 рабочий слот) | нет | 200 / 200 |
| startfail | stdio | сервер падает при запуске | —, —, — ; 0 вызовов LLM; в логе «Failed to load MCP servers, MCP tools will not be available» — но не работает весь агент | нет | 200 / 200 |
| http restart | HTTP | сервер перезапущен между репликами 2 и 3 | ok ok ok ok ok; в логе «MCP server session was lost during tool call, reconnecting» | сам переподключился | 200 / 200 |
| http down | HTTP | реплика 3 при выключенном сервере, потом запуск | ok ok ERR(`connection refused`) ok ok | сам переподключился | 200 / 200 |
| http hang + сторож | HTTP | завис на 2-м вызове, внешний сторож убил и перезапустил процесс | ok, (зависание → сразу после kill: ERR `request terminated without response`), ok ok | сам переподключился | 200 / 200 |
| die + `/reload` | stdio | сервер умер на 2-м вызове, перед 4-й репликой — `/reload` в чат | ok ERR ERR, «Config reload triggered!», ERR ERR: новый процесс сервера запускается (в журнале сервера есть `start`), подключается («Connected to MCP server») и **через <1 с убит** | запущен и сразу убит | 200 / 200 |
| ok + `/reload` | stdio | здоровый сервер, перед 3-й репликой `/reload` | ok ok, «Config reload triggered!», ERR ERR (`connection closed`) — перезагрузка конфига ломает здоровый stdio-сервер | убит | 200 / 200 |
| http + `/reload` | HTTP | здоровый сервер, перед 3-й репликой `/reload` | ok ok, «Config reload triggered!», ok ok | не нужен | 200 / 200 |

Следствия:
* `/health` и `/ready` PicoClaw **ничего не говорят** о работоспособности MCP и агентного цикла — супервизору нужна
  своя проверка (пробная реплика или проверка процесса MCP-сервера).
* Для живой модели строка «MCP tool execution failed: … connection closed» уходит в LLM как результат инструмента
  (`mcp_tool.go:259-263`); дальше модель пишет пользователю что-то вроде «сервис робота недоступен, перезапустите» —
  это **правдоподобное объяснение** фразы «говорит, надо перезагружать» [оценка; текст ответа зависит от модели].
* Опасный побочный эффект: при `noise` (и в общем случае при обрыве после отправки запроса) действие на сервере могло
  выполниться, а агент получил ошибку и может повторить команду — двойное движение [эксп. + оценка].

### 2.3. Таймауты [код]

| Где | Значение | Файл |
|---|---|---|
| Вызов MCP-инструмента | **нет** | `config.go:1222-1247`, `registry.go:251-322`, `turn_coord.go:18` |
| HTTP-клиент MCP | нет `Timeout` (только DisableStandaloneSSE) | `manager.go:372-391` |
| Остановка stdio-процесса | 5 с → SIGTERM → SIGKILL | `isolated_command_transport.go:19, 63-94` |
| HTTP-запрос к LLM | 120 с по умолчанию (`request_timeout` в `model_list` можно уменьшить) | `pkg/providers/common/common.go:39`, `config.go:779` |
| Повторы LLM | 2 повтора, паузы 2 и 4 с (`max_llm_retries`, `llm_retry_backoff_secs`) | `defaults.go:42-43`, `pipeline_llm.go:260-325` |
| Сводка истории (legacy) | 120 с на сводку | `context_legacy.go:173` |
| Ручная отмена | `/stop` в том же чате → `HardAbort` → отмена контекста хода | `agent_stop.go:12-80` |

---

## 3. Контекст и сессии

### 3.1. Что хранится и как растёт [код + эксп.]

* Сессия = чат канала (у MQTT — `client_id` в теме). История — append-only JSONL в `workspace/sessions/<ключ>.jsonl` +
  `.meta.json` (сводка, логическое смещение `skip`) (`pkg/memory/jsonl.go:52-66`). **Переживает перезапуск**
  PicoClaw — перезагрузка не «чистит» сессию; чистит `/clear` (команды `/new` в v0.3.1 нет, `pkg/commands/`).
* Каждое сообщение: open+write+**fsync** JSONL + атомарная запись meta (temp+fsync+rename+fsync каталога)
  (`jsonl.go:593-651`, `pkg/fileutil/file.go:88,112`) → ≈3 fsync на сообщение, ход с одним инструментом = 4 сообщения ≈
  12 fsync. На microSD Jetson Nano это заметная добавка к задержке и износ карты [оценка: не измерено].
* `GetHistory` каждый раз читает файл целиком сканером (`jsonl.go:654-674`); после усечения `Save` переписывает файл
  (`Compact`, `:796-835`) — на диске рост ограничен.
* Умолчания (`pkg/config/defaults.go:31-43`, `pkg/agent/instance.go:157-197`): `max_tokens` 32 768,
  `context_window` = 4×max_tokens = 131 072 (если не задан), сводка при >20 сообщений или >75 % окна,
  `max_tool_iterations` 50, `max_llm_retries` 2. ContextManager по умолчанию — legacy (`turn_coord.go:336-340`).
* Legacy-сводка асинхронная: при >10 сообщений — 2 частичные сводки + слияние = до 3 вызовов LLM, оставляет последние
  ≈4 сообщения (`context_legacy.go:77-105, 172-260`). В сводку идут только user/assistant, результаты инструментов
  отбрасываются (`:194-197`).
* Опыт «40 жестов подряд» [эксп.]: 40/40 ответов; **101 вызов LLM, из них 21 — сводки** (+26 %); история в запросе
  колебалась 6–22 сообщений, тело запроса 12–15 КБ; RSS шлюза 37→42 МБ. Т. е. контекст не «взрывается», но поток
  однотипных команд, которым история не нужна, оплачивается лишними вызовами.
* Гонка: сводка считает `keepCount` по снимку истории, а после вызова LLM (до 120 с) делает
  `TruncateHistory(keepCount)` (`context_legacy.go:183-188, 247`; семантика «оставить последние N», `jsonl.go:711-747`)
  → сообщения, пришедшие во время сводки, вытесняют несводённые старые [код; подтверждено как известная ошибка в
  issue #3404, «async summarization drops messages appended during the LLM call»].

### 3.2. Переполнение окна и ошибки LLM [код]

* Ошибка провайдера, в тексте которой есть `context_length_exceeded`, `context window`, `token limit`, `max_tokens`,
  `invalidparameter`, `request too large` и т. п., считается переполнением (`pipeline_llm.go:291-301`): пользователю
  уходит **«Context window exceeded. Compressing history and retrying...»** (`:353-357`), legacy-менеджер выбрасывает
  старейшие ~50 % ходов с пометкой «[Emergency compression dropped N oldest messages…]» (`context_legacy.go:115-170`),
  повтор. Если не помогло — «context window still exceeded … refusing to drop active turn messages» (`:444`).
* Риск конфигурации: `max_tokens` 32 768 уходит в запрос; для моделей, имя которых не содержит `gpt-5`/`o1`/`glm`,
  — полем `max_tokens` (`pkg/providers/openai_compat/provider.go:178-189`). У gpt-4o-mini потолок вывода 16 384
  (снимок каталога RouterAI 06.10.2026: `max_completion_tokens: 16384` у обоих эндпоинтов,
  `research/new_topic/F_artifacts/routerai_endpoints_openai_gpt-4o-mini_2026-10-06.json`; как RouterAI обрабатывает
  превышение — не проверено) → вероятна ошибка 400 со словом `max_tokens` → её примут за
  переполнение окна и будут «сжимать историю» [оценка; проверить на живой модели или по логам лаборатории].
* Прочие тексты, которые видит пользователь: «Error processing message: LLM call failed after retries: …»
  (`error_format.go:9-23`, `pipeline_llm.go:470`), «The model returned an empty response. This may indicate a provider
  error or token limit.» и «I've reached `max_tool_iterations` … Increase `max_tool_iterations` in config.json…»
  (`agent.go:121-122`). Все три звучат как «что-то сломалось, надо чинить/перезапускать».
* Худший случай ожидания при недоступном провайдере: 3 попытки × 120 с + 2 + 4 с ≈ **366 с** до ошибки [оценка по коду].
  За это время все остальные чаты стоят (1 рабочий слот).

### 3.3. Очереди и параллельность [код + эксп.]

* Все ходы последовательны (`max_parallel_turns` по умолчанию 1, `agent_init.go:60-64`).
* Реплика в сессию с активным ходом → очередь steering (≤10, `steering.go:27`); переполнение — только `WARN` в логе
  (`agent.go:199-212`). Очередное сообщение, пришедшее во время хода, **отменяет оставшиеся вызовы инструментов
  этого хода** («Skipped due to queued user message.», `pipeline_execute.go:342-343, 730-731`) — серия жестов будет
  обрывать начатые действия.
* Входная шина: буфер 64, при заполнении публикация блокируется (`pkg/bus/bus.go:28, 169-172`).

---

## 4. События, каналы и как жест может дойти до агента

### 4.1. MCP-события [код]

Нет: ни `notifications/resources/updated`, ни `tools/list_changed`, ни `resources/read`/`subscribe`
(`grep ReadResource|Subscribe|ResourceUpdated|ToolListChanged` по `pkg/` — пусто; `NewClient(…, nil)`). Агент узнаёт о
мире только (а) когда сам вызывает инструмент в ходе, (б) по входящему сообщению канала, (в) по heartbeat/cron.

### 4.2. Источники «пробуждения» [код]

| Источник | Как работает | Годится для жеста? |
|---|---|---|
| Heartbeat | раз в N минут, **минимум 5** (`pkg/heartbeat/service.go:27, 51-58`), отдельная сессия | нет — опрос раз в ≥5 мин |
| Cron | задачи `every` (мс) или `command` через exec без LLM (`pkg/tools/cron.go`) | опрос; `command` — без LLM, но и без агента |
| USB hotplug | `pkg/devices` (выключено по умолчанию), только USB | нет |
| **MQTT-канал** | `/<prefix>/<agent_id>/<client_id>/request` → ход в сессии client_id; авто-переподключение и переподписка (`pkg/channels/mqtt/mqtt.go:80-93, 161`) | **да** — самый чистый путь: детектор публикует `{"text":"жест: вперёд"}` |
| **Канал MaixCAM** | TCP-сервер; устройство шлёт JSON `{"type":"person_detected","data":{class_name,score,x,y,w,h}}` → «📷 Person detected! Class: …» во входящие (`pkg/channels/maixcam/maixcam.go:146-211`) | да, без доработок: распознаватель может представиться MaixCAM и слать `class_name: gesture_forward` (хак) |
| Telegram/VK/Discord/… | реплики человека | только для человека; Telegram через прокси — риск «оглохнуть» (#3422) |
| `pico` (WebSocket веб-UI) | нужен токен, поток ответа | для человека |
| CLI `picoclaw agent -m` | новый процесс на реплику: MCP-серверы стартуют заново каждый раз; 0,28–0,41 с на Mac с Python-сервером [эксп.] | работает «без перезагрузок» ценой холодного старта; на Nano дольше [оценка] |

Любой путь — это **ход LLM на каждое событие**: нет дебаунса, слияния, cooldown, локальных правил «событие →
действие без LLM» (хуки `BeforeLLM` могут только изменить/прервать запрос, ответить вместо модели не могут —
`pkg/agent/hooks.go:28-33`, `pipeline_llm.go:83-119`; `respond` есть только для инструментов).

### 4.3. Как сейчас устроена связка в лаборатории (по коду из архивов)

`predict_live.py` публикует `/cmd_vel` сам (`src/camera/predict_live.py:351-370, 486`), PicoClaw в этот контур
не входит; PicoClaw управляет роботом отдельно через `ros_mcp_server.py` (stdio, `readme.md` §7.3). Значит,
«жест → реакция» через агента сейчас нет вообще; чтобы он был, детектор должен отправлять событие в канал PicoClaw.

---

## 5. Ресурсы на слабой плате

| Что | Значение | Источник |
|---|---|---|
| PicoClaw gateway, RSS | 35–41 МБ в опытах (минимальный конфиг, Mac, страницы 16 КиБ) | [эксп.] |
| PicoClaw best, PSS в ВМ sbc | 47,6 МиБ (с hw-mcp в дереве — 47,6 / пик 58,2 RSS) | HANDOFF §7, `bench/baselines/README.md` §8 |
| PicoClaw на RPi 4, пиковый RSS | ≈31 МБ | Zenodo 10.5281/zenodo.20593332 (по `AGENT_LANDSCAPE.md` §3.1) |
| Python FastMCP-сервер (минимальный), RSS | 56–58 МБ (stdio и HTTP) | [эксп., Mac] |
| Бинарник linux-arm64 | 34,5 МБ, статический, `GOARM64=v8.0` — подходит Cortex-A57 Jetson Nano | `AGENT_LANDSCAPE.md` §3.1, §7 |
| Задержка самого PicoClaw на ход | ≈0,11–0,13 с на жест с локальной заглушкой LLM (2 вызова LLM + MCP + MQTT) | [эксп., Mac; на Nano больше — не измерено] |
| fsync на сообщение | ≈3 (≈12 на ход с инструментом) | [код, §3.1] |
| Входные токены на вызов | 3,1–3,6 тыс. (15 инструментов) | [эксп., tiktoken o200k] |

Замечание: ros_mcp_server на роботе (Ubuntu 20.04, Python 3.8 по README демо) в текущем виде не запустится —
пакет `mcp` требует Python ≥3.10 (метаданные mcp 1.30: `Requires-Python: >=3.10`), а в коде `str | None` и
`dict[str, Any]` в аннотациях модуля [код `ros_mcp_server.py:22, 67, 125`]. Вероятно, сервер у них работает на другом
Python или на другой машине — уточнить (подробно — R2).

---

## 6. Почему «после пары использований надо перезагружать» — гипотезы по убыванию правдоподобия

| # | Гипотеза | Как проявляется | Проверка у лаборатории |
|---|---|---|---|
| H1 | stdio MCP-сервер умер (OOM рядом с PyTorch/CUDA на 4 ГБ, исключение интерпретатора, Ctrl+C) | все команды робота — «connection closed», LLM просит перезапустить | `ps` после сбоя: есть ли процесс `ros_mcp_server.py`; лог PicoClaw: `connection closed`/`EOF`; `dmesg | grep -i oom` |
| H2 | что-то печатает в stdout stdio-сервера (`print`, логгер в stdout, прогресс-бары; особенно если YOLO/SNN обёрнуты в MCP) | первый же вывод — `invalid trailing data…`, дальше то же, что H1 | лог PicoClaw: `invalid trailing data` / `invalid character`; поиск `print(` в коде их MCP-серверов |
| H2a | кто-то делал `/reload` или включён `gateway.hot_reload` (правили конфиг на ходу) | сразу после перезагрузки конфига все команды робота — «connection closed»; лечит только перезапуск процесса | лог: `Manual reload triggered` / `Config file changed, reloading...`, затем `connection closed` |
| H3 | зависший вызов (нет таймаута) | агент «молчит», новые сообщения не обрабатываются ни в одном чате; `/stop` помогает на один раз | лог: последний `Tool execution started` без `completed`; отвечает ли `/stop` |
| H4 | MCP не поднялся на старте → «зомби» | с самого старта ни одного ответа, `/health` ok | лог при запуске: `All MCP servers failed to connect` |
| H5 | Telegram через прокси «оглох» | сообщения в Telegram не доходят, процесс жив | `/health` ok + нет записей `Processing message` |
| H6 | конфиг модели (`max_tokens` 32 768 > лимита модели) | «Context window exceeded. Compressing history…», потом ошибка | лог: текст ошибки провайдера |
| H7 | очередь steering переполнена / `/stop` очистил очередь | часть жестов «пропадает» | лог: `Failed to enqueue steering message` |

Менее вероятно: рост контекста (сводки работают, §3.1), утечки памяти PicoClaw (в 40-жестовом опыте RSS +4 МБ — мало
данных, многочасовой прогон не делался).

---

## 7. Рекомендации

### 7.1. Если оставлять PicoClaw (быстрый путь, без патчей)

1. **MCP-серверы — по streamable HTTP, не stdio**, каждый — отдельный systemd-сервис `Restart=always`,
   `RestartSec=1`; PicoClaw-шлюз — сервис с `After=`/`Requires=` на MCP-сервисы и `Restart=always`. Доказано опытами
   §2.2: HTTP-сервер можно перезапускать под работающим PicoClaw — он переподключится сам.
2. **Сторож зависаний** (внешний, ≈30 строк): раз в 5–10 с вызывать у MCP-сервера дешёвый инструмент
   (`robot_get_status`) с таймаутом 3–5 с; нет ответа → `systemctl restart` сервера. Зависший вызов PicoClaw тогда
   сразу получит ошибку, следующий пройдёт (§2.2, «http hang + сторож»).
3. **Проверка живости самого агента**: `/health` не годится; сторож шлёт в MQTT пробную реплику в отдельную сессию
   (или смотрит, что на последнее событие был ответ) и при молчании >N с перезапускает шлюз. Сессии на диске
   переживут перезапуск — пользователь этого не заметит, кроме паузы в несколько секунд.
4. **Никакого stdout в MCP-серверах**: логирование только в stderr/файл (`logging` с `StreamHandler(sys.stderr)`),
   `print(..., file=sys.stderr)`; проверить, что библиотеки детектора (в демо — YOLOv5 через `torch.hub`
   `ultralytics/yolov5`, `predict_live.py:523`; в requirements `ultralytics==8.3.190`) ничего не пишут в stdout —
   самый надёжный приём: в начале сервера сохранить настоящий stdout для MCP, а `sys.stdout` перенаправить в stderr.
5. **Инструменты MCP-сервера — `async def` или в пуле потоков**, со своими таймаутами и `try/finally` автостопа
   (иначе один медленный вызов блокирует сервер целиком — опыт «hang», R2 D5/D6).
6. **Конфиг PicoClaw** (`~/.picoclaw/config.json`):
   * `agents.defaults.max_tokens`: 1024–2048 (ответы короткие; заодно уходит риск H6);
   * `model_list[].request_timeout`: 15–20 с; `agents.defaults.max_llm_retries`: 1;
   * `agents.defaults.max_tool_iterations`: 5–8;
   * `agents.defaults.max_parallel_turns`: 2–3 (чтобы зависший ход в одной сессии не глушил остальные; при stdio-
     сервере это не спасёт — он однопоточный);
   * выключить лишние встроенные инструменты (`tools.<имя>.enabled: false` для `append_file`, `edit_file`, `list_dir`,
     `load_image`, `read_file`, `send_file`, `spawn`, `subagent`, `write_file`, `exec`, `cron`, `web*`) — каталог
     15→2–4 инструмента, вход ≈3,1 тыс. → ≈1 тыс. токенов на вызов [оценка по §5: схемы 2 580 из 3 131];
   * для потока жестов — отдельная сессия (свой MQTT `client_id`) и периодический `/clear` в ней, либо
     `summarize_message_threshold` побольше: история команд не нужна, а сводки — +26 % вызовов;
   * **не пользоваться `/reload`, `POST /reload` и `gateway.hot_reload` при stdio-серверах** — они убивают MCP-процессы
     (§2.2); после правки конфига перезапускать весь сервис PicoClaw (`systemctl restart`). С HTTP-серверами
     `/reload` безопасен.
7. **Подача жестов** — MQTT (уже штатно в PicoClaw, с авто-переподключением): детектор публикует
   `{"text": "жест: вперёд (уверенность 0.93)"}` в `/picoclaw/<agent_id>/gesture/request`, **после** локального
   дебаунса (одна команда на жест, а не на кадр) и с отбрасыванием жестов, пока предыдущий выполняется.
8. Telegram через прокси — до исправления #3422 считать ненадёжным, проверять сторожем (п. 3).

### 7.2. Где PicoClaw принципиально не подходит

* **Рефлекс «жест → движение»** требует десятков–сотен мс; ход PicoClaw с облачной LLM — секунды (≥2 вызова LLM на
  жест), без верхней границы при сбоях сети (§0 п. 9, §3.2). ROSClaw прямо пишет, что LLM-агенты годятся для
  «deliberative planning but not reactive control», а реактивные контуры «require sub-100 ms loops» [веб,
  https://arxiv.org/html/2603.26997].
* **Каждое событие = оплачиваемый ход LLM** (≈6,4–7 тыс. входных токенов на жест в минимальном конфиге [эксп.]; при
  RouterAI-цене gpt-4o-mini 16,56 ₽ за 1 млн входных токенов (снимок 06.10.2026, `research/new_topic/F_artifacts/
  price_table_output_2026-10-06.txt`) — ≈0,11 ₽ на жест только за вход, gpt-5-mini — ≈0,18 ₽; при непрерывной работе
  с жестом раз в 3 с (1 200 жестов/ч) это ≈130–230 ₽/ч без учёта кэша префикса; с кэшем — дешевле,
  особенно у gpt-5-mini (кэш-вход 2,76 ₽ за 1 млн) [оценка]). Локальный фильтр/правила в PicoClaw добавить без патча нельзя.
* **Нет событийной модели MCP** и нет управления жизненным циклом MCP-серверов — устойчивость только снаружи.
* **Последовательная обработка** и отсутствие таймаутов: один медленный инструмент останавливает всего агента.
* Апстрим не принимает исправления надёжности (PR закрываются stale-ботом) — патчить придётся самим (форк), а по
  правилам ВКР PicoClaw как аналог не патчим (`bench/baselines/README.md` §9, HANDOFF §8).

### 7.3. Где лучше наш EdgeCtx (по `agent/README.md`; что реализовано, а что нет)

| Проблема PicoClaw | EdgeCtx (реализовано, по README и тестам) |
|---|---|
| упавший MCP не поднимается | host сам перезапускает stdio/переподключает HTTP с паузой `reconnect_s·2^(n−1)` до `reconnect_max_s`, восстанавливает подписки и состояние; пока сервер лежит — вызовы сразу «сервер недоступен (переподключение)» (`agent/README.md`, раздел «Падение MCP-сервера…») |
| нет таймаута вызова | `mcp[].timeout_s` (`agent/README.md`, таблица конфигурации) |
| нет событий | подписка на ресурсы MCP, уведомления одной ограниченной очередью (`internal/mcphost`) |
| ход LLM на каждое событие | событийный гейт: дебаунс, сигналы change/threshold/delta/episode, cooldown, коалесценция; **локальные правила `notify/tool/wake` без LLM** (`internal/gate`) — «жест → robot_nudge» можно исполнять правилом за миллисекунды, а LLM будить только для непонятного/сложного |
| при отказе LLM всё стоит | предохранитель, пробы, очередь отложенного (≤32), восстановление одним ходом |
| рост истории/сводки | политики `full/window/mask/summary_system`, ограничения памяти во времени |

Чего в EdgeCtx **нет** (HANDOFF §8): робот к агенту не подключён (robot-mcp проверен только `robot-probe`), нет
картинок в сообщениях LLM, нет периодических правил; cam-mcp — детектор движения, не жестов; ревью агента прервано.
То есть «EdgeCtx лучше» — по архитектуре и коду, на роботе не проверено.

**Предлагаемая архитектура для лаборатории** [оценка]: распознаватель жестов (YOLO+SNN) → событие (MQTT или ресурс
MCP «gesture») → локальное правило/автомат «жест → короткое движение с автостопом» (рефлекс, <100 мс до `/cmd_vel`,
без LLM) → журнал событий; агент (EdgeCtx или PicoClaw) получает сводку («за минуту: 3 жеста вперёд, 1 стоп») и
отвечает на вопросы/сложные команды. В Gazebo это проверяется без робота: синтетический поток жестов + TurtleBot3
(R6).

---

## 8. Что спросить у лаборатории

1. Версия PicoClaw (`picoclaw version`) и полный `config.json` (без ключей): `max_tokens`, модель, каналы, MCP-серверы
   (stdio/HTTP), `max_parallel_turns`, `hot_reload`. Пользовались ли `/reload` или веб-лаунчером для правки
   конфига на работающем агенте?
2. Как именно звучит «надо перезагружать»: дословный ответ агента, в каком канале (Telegram? через прокси?), через
   сколько реплик/минут; что перезапускают — PicoClaw, `ros_mcp_server.py`, `predict_live.py`, весь Jetson?
3. Лог PicoClaw вокруг сбоя (`~/.picoclaw/logs/gateway.log` или stdout сервиса) — искать `connection closed`, `EOF`,
   `invalid trailing data`, `All MCP servers failed`, `Failed to enqueue steering`, `Context window exceeded`.
4. Жив ли процесс MCP-сервера после сбоя (`ps aux | grep mcp`), были ли OOM (`dmesg`), сколько свободной памяти при
   работающем `predict_live.py` (`tegrastats`/`free -h`).
5. Где запущены PicoClaw и `ros_mcp_server.py` — на Jetson (Python 3.8?) или на ПК; каким интерпретатором.
6. Обёрнуты ли YOLO/SNN в MCP-сервер (и печатает ли он в stdout), или `predict_live.py` работает отдельно и сам
   публикует `/cmd_vel`? Должен ли жест идти через агента или напрямую?
7. Какая реакция на жест нужна и с какой задержкой («поехал через 0,3 с» или «через 3 с допустимо»)? Сколько жестов в
   минуту в непрерывном режиме? Нужен ли ответ агента текстом на каждый жест?
8. Какой ключ/модель дали (RouterAI? gpt-4o-mini/gpt-5-mini?), есть ли лимит ₽ — чтобы оценить цену потока жестов.

---

## 9. Как воспроизвести опыты

Каталог скриптов — временный: `/private/tmp/claude-501/…/scratchpad/r3pc/` (`fake_llm.py`, `flaky_mcp.py`, `run_exp.py`,
результаты `runs/<сценарий>/result.json`, `gateway.log`, `llm.jsonl`, `mcp.jsonl`, `responses.tsv`). Если он удалён —
всё восстанавливается по описанию:

* **LLM-заглушка** (stdlib `http.server`, порт 18139, `/v1/chat/completions`): если последнее сообщение `role=tool` или
  нет инструмента с `robot_nudge` в имени — финальный текст `FINAL: <начало результата>`; иначе `tool_calls` на
  `robot_nudge({"direction":"forward"})`. Пишет JSONL (число сообщений, инструментов, байт) и первые 8 тел запросов.
* **MCP-сервер** (FastMCP из `.venv`, mcp 1.30; `robot_nudge`, `robot_stop`): режимы `ok`, `die:K` (`os._exit(1)` на
  K-м вызове), `noise:K` (`print("0: 480x640 1 hand, 12.3ms")` в stdout), `hang:K`/`http_hang:K` (`time.sleep(1e6)`),
  `startfail` (выход до импорта mcp), `http_ok` (streamable HTTP на порту 18136).
* **Оркестратор**: `PICOCLAW_HOME`/`HOME` во временном каталоге → `picoclaw onboard` → поверх конфига: `model_list`
  (provider openai, model gpt-5-mini, api_base заглушки, ключ-заглушка), `channel_list.mqtt` (mosquitto -p 18138,
  `agent_id: edge`, `topic_prefix: /picoclaw`), `tools.mcp.servers.robot` (stdio: `.venv/bin/python -I flaky_mcp.py
  <режим> <журнал>` или `type: http`), веб-инструменты и heartbeat выключены, `gateway.port` 18137 → `picoclaw gateway`
  → ожидание `/ready` → реплики `mosquitto_pub -t /picoclaw/edge/user/request -m '{"text":"жест i: вперёд"}'`, ответы —
  `mosquitto_sub -t '/picoclaw/edge/+/response'`; опционально `/stop`, реплики от клиента `other`, убийство/перезапуск
  HTTP-сервера, `/reload` в чат. Все процессы — в своих группах, гасятся SIGTERM/SIGKILL.
* Токены: `tiktoken` o200k_base из локального кэша `research/new_topic/GAP2_artifacts/.cache/tk` по сохранённым телам
  запросов (системный промпт, сообщения, `tools` по отдельности).

Сценарии и повторы: ok, die ×3, noise ×3, hang ×3, startfail ×3, http_restart ×1, http_down ×3, http_hang ×3,
hang + другая сессия ×1, die + `/reload` ×2, ok + `/reload` ×2, http + `/reload` ×1, 40 жестов ×1, токены ×1;
CLI `agent -m` ×3. Итог по повторам совпал полностью. `/reload` подаётся командой в чат (MQTT): `POST /reload` на
порту шлюза требует токен из pid-файла (ответ 401 без него).
