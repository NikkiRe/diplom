# sim-mcp — эмулятор помещения (MCP)

MCP-сервер на официальном Python SDK (`mcp` 1.30, низкоуровневый `Server`, streamable HTTP, ревизия
протокола 2025-11-25). Инвентарь и начальное состояние берёт из заголовка дня daygen, проигрывает события дня
по часам агента/реплеера, рассылает `notifications/resources/updated`, исполняет приводы (с проверкой схемы и
диапазона), принимает структурные ответы `answer`/`refuse` и оценивает реакцию по истинному состоянию.
Контракт — IMPLEMENTATION_SPEC §4 и §5.1.

**Совместимость с hw-mcp — требование, а не пожелание:** агент edgectx в T0 работает с sim-mcp так же, как на
плате с hw-mcp, поэтому ресурсы, результаты инструментов, тексты и схемы generic-каталога повторяют
`hwmcp/internal/{server,hub,inventory}`. Расхождение формата ресурсов уже однажды обнулило T0: агент отвергал
каждое обновление состояния и был слеп к датчикам (ревью, DECISIONS [simday]). Это ловит сквозной тест
`bench/tests/test_simmcp_edgectx.py` с настоящим edgectx.

## Как запускать

Из корня проекта (`/Users/chosenundead/Projects/diploma-edge-ai`), порты sim-mcp — 18090–18099.

```bash
PY=.venv/bin/python
$PY -m bench.daygen gen --seed 1 --profile M+ --split tune --out day.jsonl
# сервер (останавливать по PID: kill <pid> — при SIGTERM пишутся отчёт и run_end)
$PY -m bench.simmcp --day day.jsonl --port 18090 --catalog generic \
    --journal results/sim.jsonl --report results/sim_report.json --run-id R1 &
curl -s localhost:18090/healthz
# размеры каталога и ресурсов, доля answer/refuse в токенах каталога (без запуска сервера)
$PY -m bench.simmcp --day day.jsonl --catalog generic --print-costs
# самопроверка дня «идеальным агентом» (без HTTP)
$PY -m bench.simmcp.selfcheck day.jsonl [--catalog explicit]
```

T0 целиком (edgectx + sim-mcp + mock-llm + реплеер), конфигурация агента — `bench/simmcp/edgectx_simday.yaml`
(сигналы гейта под имена устройств daygen):

```bash
$PY -m bench.mockllm --port 18100 &
$PY -m bench.simmcp --day day.jsonl --port 18090 --journal run/sim.jsonl &
agent/bin/edgectx-darwin-arm64 -config bench/simmcp/edgectx_simday.yaml -listen 127.0.0.1:18080 -trace run/trace.jsonl &
$PY -m bench.replay --day day.jsonl --edgectx http://127.0.0.1:18080 --sim http://127.0.0.1:18090 \
    --mock http://127.0.0.1:18100 --out experiments/results --collect edgectx_trace=run/trace.jsonl \
    --collect simmcp_journal=run/sim.jsonl
```

| флаг | по умолчанию | смысл |
|---|---|---|
| `--day` | — | файл дня daygen |
| `--host`, `--port` | 127.0.0.1, 18090 | порт из диапазона sim-mcp 18090–18099 |
| `--catalog` | generic | `generic` или `explicit` (как hw-mcp) |
| `--scheme` | hw | схема URI ресурсов (`hw://…`, как у hw-mcp) |
| `--journal` | нет | журнал JSONL (дописывается) |
| `--report` | нет | отчёт оценки (JSON), пишется при `/control/finish` и при остановке |
| `--run-id` | время + случайный суффикс | попадает в каждую запись журнала |
| `--notify-all` | выкл. | слать `resources/updated` всем сессиям, а не только подписанным |
| `--structured-content` | выкл. | дублировать результат в `structuredContent` (у hw-mcp только текст; удваивает трафик) |
| `--rich-labels` | выкл. | подписи `list_devices` с диапазоном и допустимыми значениями (`%,1..100,rw`, `amber\|blue\|…,rw`); у hw-mcp их нет, +≈ 33 % токенов результата |
| `--extra-resources` | выкл. | ещё `hw://state` (всё) и `hw://events` (последние изменения); у hw-mcp их нет |
| `--no-eval-tools` | выкл. | без `answer`/`refuse` — прогоны затрат как на плате; вопросы и ловушки тогда не оцениваются |
| `--inline-state` | выкл. | новые значения в `_meta.state` уведомления: агенту не нужно читать ресурс (у hw-mcp нет — кандидат в оптимизацию) |
| `--notify-wait` | 1,0 с | сколько `/control/clock` ждёт доставки уведомлений успевающим сессиям |
| `--session-idle-timeout` | 120 с | SDK закрывает сессию без запросов **и** без GET-потока дольше N с (брошенную); 0 — не закрывать |
| `--start-t` | начало окна | начальные часы |
| `--print-costs` | — | напечатать размеры каталога/ресурсов и выйти |

## Точки входа (один порт)

| путь | что |
|---|---|
| `/mcp` | MCP streamable HTTP (stateful-сессии; уведомления идут в GET-поток сессии) |
| `POST /control/clock {"t": 30000}` (или `{"to_t": …}`) | продвинуть часы: события дня с `t ≤` применяются по порядку, срабатывают таймеры устройств, подписчикам ставятся уведомления (см. ниже). Ответ: `{"t", "applied", "notified", "pending", "inputs_opened", "finished"}`; назад — 409 |
| `GET /control/clock` | текущие часы |
| `POST /control/input {"id": "u3"}` | явно сделать вход текущим (привязка вызовов к реплике; реплеер делает это перед `POST edgectx /v1/user`) |
| `GET /control/state` | истинное состояние всех свойств |
| `GET /control/report` | оценка на текущий момент |
| `POST /control/finish` | закрыть окна оценки, записать отчёт |
| `GET /healthz` | живость, часы, сессии (`sessions`, `subscribed`, `streaming`, `sdk_sessions`, `pending`) и счётчики (`tool_calls`, `resource_reads`, `resource_read_bytes`, `notify_sent`, `notify_coalesced`, `notify_stalls`, `notify_failed`, `sessions_closed`) |

Порядок для реплеера (§5.1): на каждый вход дня — `mock-llm /control/expect` → `edgectx /v1/clock/advance` →
`sim-mcp /control/clock {t}` → (`/control/input {id}`) → `edgectx /v1/user`; в конце — `/control/clock` на конец
окна и `/control/finish`.

## Ресурсы (как у hw-mcp)

* `hw://inventory` — описание для агента (AgentView hw-mcp):
  `{"devices": [{"id", "kind", "location", "source": "sim", "properties": {p: {"type", "writable", "unit"?,
  "min"?, "max"?, "enum"?, "resolution"?}}, "actions": {a: {"description", "input": <JSON Schema>}}}]}`.
  edgectx строит из него L0 («light_kitchen (kitchen): brightness [%, rw, 1..100], color [rw], on [bool, rw]»)
  и точность округления (`resolution`). Значений в инвентаре нет.
* `hw://state/<устройство>` — DeviceState hw-mcp: `{"device", "now", "properties": {p: {"value", "unit"?, "ts",
  "age_s"}}}`.
* Только с `--extra-resources`: `hw://state` (`{"now", "state": {dev: {p: v}}}`) и `hw://events` (`{"now",
  "events": [...]}` — до 100 изменений с последнего продвижения часов, включая записи агента).

Подписка — `resources/subscribe` на любой из них (сервер объявляет `resources.subscribe: true`). Изменение
свойства (от трассы, таймера или записи агента) → `notifications/resources/updated` для `hw://state/<device>`
с `_meta: {"hw/changed": [свойства]}` (как у hw-mcp); с `--inline-state` ещё `_meta.device` и `_meta.state`
(новые значения изменённых свойств).

**Доставка.** Подписки хранятся по `mcp-session-id` отдельно от живости сессии. У каждой сессии — очередь URI
с объединением повторов (при отставании клиента несколько изменений устройства уходят одним уведомлением с
объединённым `hw/changed`; очередь не длиннее числа ресурсов), её разбирает отдельная задача. `/control/clock`
ждёт доставки не дольше `--notify-wait` и только тех сессий, которые не отставали до этого шага; замороженный
клиент (cgroup freezer, CPU 10 %) задерживает часы один раз, подписок не теряет и получает объединённые
уведомления, когда снова читает поток. Сессиям без открытого GET-потока ничего не отправляется (SDK всё равно
отбросил бы сообщение); брошенные сессии (агент убит без DELETE) SDK закрывает по `--session-idle-timeout`.

## Инструменты

**generic:** `list_devices(all?)`, `read_property(device, property)`, `write_property(device, property, value)`,
`invoke_action(device, action, args?)`, `board_health()` — описания и схемы дословно как в hw-mcp
(`addGenericTools`): каталог из пяти инструментов совпадает с hw-mcp по токенам (206 по счёту mock-llm).
**explicit:** `read_<dev>_<prop>()` на каждое свойство, `set_<dev>_<prop>(value)` на каждое записываемое
(схема `value` с `enum`/`minimum`/`maximum` из daygen), `<action>_<dev>(<параметры>)` на каждое действие
(`brew_coffee_kitchen`, `start_vacuum {room?}`, `dock_vacuum`); описания — по шаблонам hw-mcp
(«Read temperature (Cel) of thermo_living_room», «Set heating (boolean) of boiler @entrance»), `board_health()`.
Порядок детерминированный (по id устройства: read, set, действия).
Инструменты оценки `answer(value, unit)` и `refuse(reason)` (коды `no_such_device`, `no_such_room`, `unsafe`,
`unsupported`, `ambiguous`, `other`) есть **только в T0** — на плате их нет. Их вклад в каталог печатает
`--print-costs` (`tools.tokens_eval`: 108 токенов по счёту mock-llm, раньше 135) и пишет журнал (`run_start.
catalog_bytes`); при переносе токенов T0 на плату его вычитают или гоняют прогоны затрат с `--no-eval-tools`.

**Результаты** — только текст (канонический JSON в `content[0].text`); `structuredContent` — по флагу
`--structured-content`. Форматы — как у hw-mcp:

| что | ответ |
|---|---|
| `list_devices` | CompactView: `{"devices": [{"id", "loc", "kind"?, "props": {p: "Cel" \| "bool,rw" \| "%,rw" \| ",rw" \| ""}, "actions"?: {a: "room:string"}}]}` |
| чтение | `{"device", "property", "value", "unit"?, "age_s"}` (возраст с точностью 0,1 с) |
| ошибка чтения | `isError`, `{"error": "unknown_device" \| "unknown_property" \| "invalid_args", "detail"}` — **не** `denied` |
| запись | `{"ok": true, "verdict": "allow", "executed": true, "device", "property", "value", "cmd_id"}` |
| действие | `{"ok": true, "verdict": "allow", "executed": true, "device", "action", "property"+"value" \| "applied", "done_in_s"?, "cmd_id"}` |
| отказ привода | `isError`, `{"denied": true, "verdict": "reject", "reason", "detail", "device", "cmd_id"}`; коды шлюза hw-mcp: `unknown_device`, `unknown_tool` (нет свойства/действия, свойство только для чтения), `invalid_args` (схема, тип, enum, диапазон — точная причина в `detail`), `interlock` (устройство занято) |
| `answer` / `refuse` | `{"ok": true, "recorded": true}` |
| неизвестный инструмент | `isError`, `{"error": "unknown_tool", "detail"}` |

`cmd_id` берётся из `_meta["edgectx/cmd_id"]` запроса (как у hw-mcp), иначе `sim-<n>`. `board_health` —
синтетика с теми же полями, что у hw-mcp (`now`, `cpu_temp_c`, `load`, `memory`, `throttled`, `uptime_s`,
`sensor_faults`, `sensor_errors`), детерминированная функция часов; сбоев датчиков в симуляции дня нет
(`sensor_faults` пуст, `sensor_errors` = 0).

## Оценка

Входы дня: `user_cmd`, `question`, `situation`. Текущий вход пользователя — последний `user_cmd`/`question`,
наступивший по часам (или заданный `/control/input`); его окно — до следующего `user_cmd`/`question`.
Ситуации окно пользователя не закрывают.

* команда (`expect.type = action`): `ok`, если после успешной записи/действия агента в окне выполнена
  `expect.goal` (`satisfied_at`); изменения от трассы (ручные переключения жителя) не засчитываются;
  `pre_satisfied` — цель уже была выполнена в момент реплики; `final_ok` — выполнена при закрытии окна;
* невалидная команда: вызван `refuse` и нет успешных записей/действий, кроме ожидаемых действий открытых ситуаций;
* вопрос: последний `answer` в окне; время принимается числом секунд от полуночи или «ЧЧ:ММ[:СС]» (±60 с),
  числа — с допуском `tol`, строки — по списку `accept` (без регистра, ё = е), логические — да/нет/true/открыта…;
  единица — по синонимам (`Cel` = °C…); для вопросов о текущем состоянии эталон — истинное значение в момент ответа;
* ситуация с `expect.actions`: совпадающая успешная запись в `[t; t + deadline_s]` (`latency_s`).
  Уведомления пользователю sim-mcp не видит — полноту по уведомлениям считают метрики по трассе edgectx.

Отчёт: `{"summary": {"commands", "questions", "situations_with_actions", "by_class", …}, "inputs": [...]}`.

## Журнал (JSONL)

Каждая запись: `ts_wall`, `ts_clock`, `type`, `run_id` (как трасса edgectx §6) и поля типа:
`run_start{catalog, scheme, notify_all, structured, rich_labels, extra_resources, inline_state, notify_wait_s,
eval_tools, day, n_tools, session_idle_timeout, catalog_bytes}`, `clock{t_from, applied, notified, pending,
sessions, inputs}`, `sensor{device, property, value, source}`, `input{input_id, kind, t_event}`, `input_set`,
`subscribe{uri, session}`, `tool_call{call_id, name, args}`, `tool_result{call_id, name, op, ok, denied, reason,
input_id, ms}`, `answer{…}`, `refuse{…}`, `report{summary, inputs}`, `run_end{<счётчики /healthz>}`.

## Самопроверка дня

`bench.simmcp.selfcheck`: «идеальный агент» исполняет эталонные вызовы дня прямо в sim-mcp; для согласованного
дня — 100 % по командам, вопросам и ситуациям с действиями, ни одного отказа и ни одной заранее выполненной
цели. После исправлений проверено на 62 днях (оба split, M+ и S200, 2 круга по домам; синтетика H) в обоих
каталогах — 62/62 OK.

## Производительность (ориентиры, не результаты: замеры при работе других агентов)

| что | было | стало |
|---|---|---|
| вызов инструмента в процессе (generic read / explicit read / write) | 0,386 / 0,181 / 0,467 мс | 0,011 / 0,006 / 0,026 мс (валидаторы схем строятся один раз) |
| heavy-день hh121 S200, шаг к каждому событию (13 742 шага), клиент читает ресурс на уведомление | 17,2 с, CPU sim 8,9 с, p50 1,18 мс, RSS +5,5 МиБ | 14,8 с, CPU 7,3 с, p50 1,08 мс, RSS +1,0 МиБ |
| 200 брошенных сессий (3 подписки на самые частые устройства), 60 шагов по 60 с | p50 0,82 → 19,9 мс, CPU 0,012 → 0,53 с | p50 0,62 → 0,85 мс, CPU 0,009 → 0,019 с; через idle-timeout все 200 закрыты |
| клиент не читает GET-поток, 8000 шагов heavy-дня | один шаг 5,0 с, затем подписки потеряны (0 уведомлений на следующие 300 событий) | один шаг 1,0 с; подписки целы, 4342 уведомления объединены, после возобновления — 302 на 300 событий |
| RSS при 4 циклах по 200 брошенных сессий | — | 80 → 91,4–91,9 МиБ (плато, роста нет) |
| `--inline-state`, edgectx на hh101 07–09 | — | чтений ресурсов 406 → 21, байт 56 → 8 КБ при тех же 23 вызовах LLM и токенах |

## Ограничения

* Трасса датчиков не реагирует на действия агента (котёл не остывает, протечка не прекращается от крана);
  реагируют только приводы и их таймеры (кофемашина, пылесос).
* Шлюза приводов (TTL решения, свежесть, аренда, подтверждение) здесь нет — только схема/диапазон/«занято»
  (коды отказа — подмножество кодов шлюза hw-mcp); шлюз — в hw-mcp.
* RSS sim-mcp ≈ 80 МиБ (Python + SDK): sim-mcp нельзя ставить в cgroup агента (лимиты 32–512 МиБ), а его
  MCP-трафик — под netem «плохой сети»: на плате hw-mcp локальный. Память, освобождённая после закрытых сессий,
  остаётся у процесса (аллокатор Python), но переиспользуется.
* Без аутентификации; слушать только localhost.
* Уведомления уходят в GET-поток сессии; клиент без GET-потока их не получит (так устроен streamable HTTP),
  пока не откроет поток — тогда получит объединённые.
* Проверка открытого GET-потока и закрытых сессий использует внутренние поля SDK mcp 1.30
  (`_server_instances`, `_request_streams`); без них sim-mcp считает поток открытым, а сессию живой.

## Тесты

```bash
.venv/bin/python -m pytest -q bench/tests/test_simmcp_world.py bench/tests/test_simmcp_server.py \
    bench/tests/test_simmcp_notify.py bench/tests/test_simmcp_edgectx.py
```

Мир (проигрывание, валидация, таймеры, отмена таймера новым действием; форматы ресурсов, чтения, отказов — как
у hw-mcp), каталоги (generic дословно как hw-mcp, explicit, доля answer/refuse, `--no-eval-tools`), сравнение
ответов, самопроверка идеальным агентом (synthetic и CASAS, generic и explicit), «бездействующий агент» = 0 %,
неверный ответ и побочное действие; через транспорт — отдельный процесс CLI и клиент mcp SDK: инициализация и
подписки, инструменты обоих каталогов, отказы, `cmd_id` из `_meta`, ресурсы, уведомления с `hw/changed` после
`/control/clock` (только подписанным; всем при `--notify-all`; от таймера), оценка, журнал и отчёт; рассылка:
замороженная сессия не держит часы и не теряет подписок, объединение повторов, `--inline-state`, валидаторы не
перестраиваются; брошенные сессии без GET-потока не получают уведомлений и закрываются по простою. **Сквозной**
`test_simmcp_edgectx.py` собирает edgectx из `agent/` (или `EDGECTX_BIN`) и гоняет реплеер с настоящими
edgectx, sim-mcp и mock-llm: `errors.mcp_state = 0`, журнал L2 агента заполнен, в промпте L0 без подписей,
гейт просыпается на перегрев/протечку, команды и ситуации с действиями — 100 %; вариант с `--inline-state` —
чтений ресурсов < 20 % уведомлений. На исходном sim-mcp этот тест падает (`mcp_state` 193 из 193).
