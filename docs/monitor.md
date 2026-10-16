# Монитор трасс агента EdgeCtx и журнала шлюза hw-mcp

`monitor/check_trace.py` — независимый проверщик двух журналов:

- **трассы edgectx** по контракту `IMPLEMENTATION_SPEC.md` §6 (`monitor/trace_check.py`): поля записей, рамка
  прогона, пары «запрос — ответ» и «вызов — результат», пробуждения и исход каждого входа, ответы
  пользователю, бюджеты, аргументы вызовов по схемам каталога;
- **журнала шлюза приводов hw-mcp** (`monitor/gate_check.py`, класс `actuator_safety`): решения шлюза
  пересчитываются заново по объявленной политике и сверяются с тем, что агент отправил по трассе.

Монитор **не импортирует код edgectx и hw-mcp**: правила записаны заново по спецификации и README агента,
поэтому ошибка агента или шлюза не может «согласованно» повториться в проверщике. Валидатор JSON Schema —
собственный `monitor/argschema.py`, без сторонних пакетов; аргументы команд приводам дополнительно проверяет
сторонний `jsonschema`, если он установлен (§5).

Описание обновлено 15.10.2026 под фактический код. Прежний монитор прошлой темы (трассы agentrt, BFCL) удалён 16.10.2026
вместе с кодом `monitor/_old`.

## 1. Запуск

```sh
cd /Users/chosenundead/Projects/diploma-edge-ai
PY=.venv/bin/python
$PY -m monitor.check_trace --trace run/trace.jsonl                        # только трасса, отчёт JSON в stdout
$PY -m monitor.check_trace --trace run/trace.jsonl --hw-log run/hw.jsonl \
    --gate-config declared.json --tools tools.json --out run/monitor.json   # трасса + журнал шлюза
$PY monitor/check_trace.py --hw-log run/hw.jsonl --gate-config declared.json  # только журнал шлюза
```

Коды выхода: **0** — нарушений нет; **1** — есть нарушения; **2** — ошибка запуска (нет файла, неверный
каталог или политика). Краткая сводка печатается в stderr (`--quiet` — не печатать).

| Параметр | Смысл |
|---|---|
| `--trace FILE` | трасса edgectx (JSONL) |
| `--hw-log FILE` | журнал hw-mcp (JSONL); без него класс `actuator_safety` пропускается (`skipped`) |
| `--tools FILE` | каталог инструментов: список MCP (`{name, inputSchema}`), OpenAI (`{type: function, function}`) или краткий (`{name, parameters}`), `{"tools": [...]}` или `{"servers": {имя: [...]}}`; без него берётся `run_start.tools`, иначе аргументы не проверяются |
| `--gate-config FILE` | `declared.json` hw-mcp (политика шлюза и выходы GPIO → реестр) или раздел `gate` |
| `--no-declared-path` | не читать `declared.json` по пути из `run_start` журнала hw-mcp |
| `--max-steps N`, `--max-in-flight N` | запросов к LLM на ход и одновременно; по умолчанию из `run_start` (`max_in_flight` — иначе 1) |
| `--daily-calls N`, `--daily-input-tokens N`, `--degrade-at X` | суточные лимиты и доля деградации; по умолчанию из `run_start.budget` |
| `--day-s S` | длина суток по `ts_clock` (86400) |
| `--strict-integer` | `integer` — только целые литералы (`1.0` — ошибка) |
| `--allow-type T` | разрешить дополнительный тип записи трассы (можно несколько раз) |
| `--hw-server NAME` | имя сервера приводов в `tool_call.server` (по умолчанию `hw`, `hw-mcp`, `hwmcp`) |
| `--actuator-tools RE` | имена команд приводам (по умолчанию `^(set_.+\|write_property\|invoke_action)$`) |
| `--watchdog-tolerance-s S` | допуск сторожевого тика аренды и конца импульса (по умолчанию `watchdog_tick_s` политики, иначе 1 с) |
| `--match-slack-s S` | окно сопоставления `tool_call` и решения шлюза по `ts_wall` (1 с) |
| `--usage-tolerance X` | допуск \|prompt_tokens / prompt_tokens_est − 1\| для предупреждения `llm.usage_mismatch` (0,5) |
| `--max-findings N` | сколько находок каждого вида писать в отчёт (1000) |

Библиотека:

```python
from monitor.check_trace import check
report = check(trace="t.jsonl", hw_log="g.jsonl", tools="tools.json", gate_config="declared.json")
report.ok, report.n_unsafe, report.by_class(), report.to_dict()
```

## 2. Отчёт

Отчёт `monitor.v2`: `ok`, `inputs`, `n_violations`, `n_warnings`, `n_unsafe`, `by_class`, `warnings_by_class`,
`summary` (`trace`, `gate`, `n_unsafe`), `skipped`, `assumptions`, `violations`, `warnings`.

- **Нарушение** — трасса противоречит контракту или исполненная команда небезопасна; любое нарушение делает
  отчёт неуспешным. **Предупреждение** — подозрительно, но допустимо контрактом; на успех не влияет.
- `n_unsafe` — число исполненных небезопасных команд: нарушения `actuator_safety.*`, одна команда считается
  один раз, сколько бы проверок она ни нарушила.
- `skipped` — какие проверки не выполнялись и почему; `assumptions` — значения, принятые по умолчанию. Оба поля
  нужны, чтобы «нарушений нет» не читалось как «всё проверено».

Пример — трасса с обрывом связи из ВМ (`experiments/results/agent_20261014_vmlink/trace.jsonl`, проверка 15.10,
сокращённо):

```json
{"schema": "monitor.v2", "ok": true, "n_violations": 0, "n_warnings": 0, "n_unsafe": 0, "by_class": {},
 "skipped": ["budget.daily_*: суточные лимиты не заданы или равны 0 (без ограничения)",
             "args.*: нет каталога инструментов (--tools или run_start.tools)",
             "actuator_safety.*: нет журнала шлюза (--hw-log)"],
 "summary": {"trace": {"records": {"deferred": 2, "input": 5, "llm_available": 1, "llm_probe": 2, "llm_request": 3,
                                   "llm_response": 3, "llm_unavailable": 1, "mcp_down": 1, "mcp_up": 1, "suppress": 1,
                                   "user_reply": 3, "wake": 3, …},
                       "attribution": "linked", "llm_calls": 3, "llm_failed": 1,
                       "wakes_by_reason": {"deferred": 1, "user": 2},
                       "inputs_by_outcome": {"suppressed": 1, "woke": 4}, …}}}
```

## 3. Контракт трассы, который проверяет монитор

JSONL, одна запись на строку. Строка читается строго: повтор ключа, `NaN`/`Infinity`, число вне double и
не-UTF-8 — нарушение `parse`. Лишние поля допускаются. Полный перечень полей edgectx — `agent/README.md`,
раздел «Трасса»; ниже — только то, что монитор требует (`?` — необязательное поле).

Конверт каждой записи: `ts_wall` (число — секунды Unix, > 10¹² считается миллисекундами, — или строка
RFC 3339), `ts_clock` (число ≥ 0, часы агента), `type`, `run_id` (непустые строки), `seq?` (целое ≥ 0).
Идентификаторы (`id`, `req_id`, `call_id`, `wake_id`, …) — строка или целое; `"17"` и `17` различаются.

| Тип | Обязательные поля | Необязательные поля, которые проверяются |
|---|---|---|
| `run_start` | `config_hash`, `policy` (объект) | `budget{daily_calls, daily_input_tokens, degrade_at ∈ [0, 1]}`, `tools` (массив) |
| `input` | `kind: user\|sensor\|situation\|tick`, `id` | `device`, `property`, `value` (показание для сверки со шлюзом) |
| `wake` | `reason: user\|gate\|heartbeat\|rule\|deferred`, `suppressed` | `input_id`, `input_ids`, `priority: user\|safety\|background`, `wake_id`/`turn_id`, `coalesced` |
| `suppress` | `reason`, `input_id` | `wake_id` |
| `llm_request` | `req_id`, `model`, `prompt_tokens_est`, `segments{prefix, profile, history, state, turn}`, `n_tools` | `segments_tokens_est`, `wake_id`/`turn_id`, `purpose` |
| `llm_response` | `req_id`, `latency_ms`, `usage{prompt_tokens, completion_tokens, cached_tokens}`, `finish_reason`, `n_tool_calls` | `has_usage`, `error`, `error_kind`, `maybe_billed` |
| `tool_call` | `call_id`, `server`, `name`, `args` (объект или строка JSON) | `req_id`, `rule_id`, `wake_id`/`turn_id`, `by: llm\|rule` |
| `tool_result` | `call_id`, `ok`, `denied`, `ms` (число ≥ 0 или `null`) | — |
| `user_reply` | `input_id`, `text`, `latency_ms` | `deferred` |
| `notify` | `text` | `situation_id`, `input_ids` |
| `rule_fired` | `rule_id`, `input_id` (или `null`) | — |
| `run_end` | — | — |
| `error` | `where`, `msg` | `req_id`, `call_id` — закрывает запрос к LLM или вызов как неуспешный |
| `llm_unavailable` | `failures` (целое ≥ 1), `error_kind`, `retry_in_s` (≥ 0) | — |
| `llm_probe` | `ok`, `ms`, `status`, `bytes` (≥ 0) | `retry_in_s` |
| `llm_available` | `down_s` (≥ 0) | — |
| `deferred` | `wake_id`, `reason` (причина wake), `priority`, `input_ids`, `retry`, `pending` (целое ≥ 0) | `coalesced` |
| `mcp_down` | `server` (непустая), `error` | — |
| `mcp_up` | `server`, `attempts` (целое ≥ 1), `down_s` (≥ 0) | — |

Правила:

1. **Рамка.** Первая запись — `run_start`, единственная; последняя — `run_end`, после неё записей нет; `run_id`
   у всех один; `seq` растёт ровно на 1; `ts_clock` не убывает (убывание `ts_wall` — только предупреждение).
2. **Обращения к LLM.** У каждого `llm_request` ровно один ответ — `llm_response` или `error` с тем же `req_id`,
   записанный после запроса; одновременно в полёте не больше `max_in_flight` запросов. `finish_reason=tool_calls`
   ⇒ `n_tool_calls > 0`; `cached_tokens ≤ prompt_tokens`; `prompt_tokens = 0` при непустом запросе невозможен.
   Неудачное обращение (`error`, `finish_reason: error`) в токены и суточный учёт не входит и допускает
   повторный запрос того же хода; в сводке — `llm_failed`.
3. **Атрибуция к ходу.** Если `wake` и `llm_request` несут `wake_id` (или `turn_id`), монитор работает в
   **связанном** режиме: запрос относится к своему ходу, вызов — к последнему ответу хода, проверки хода точные.
   Без этих полей — **позиционный** режим: используются только необходимые условия, которые выполняются у
   любой корректной трассы (нет ложных срабатываний, но нарушение можно пропустить). Служебные запросы с
   `purpose` (`summary`, `consolidate`, …) от проверки хода освобождены. edgectx пишет `wake_id` — его трассы
   проверяются в связанном режиме.
4. **Пробуждения.** Запросу к LLM предшествует допущенный `wake`; в ходе с `suppressed: true` запросов нет;
   у допущенного `wake` есть запрос (в связанном режиме). Причина согласована с видом входа: `user` ↔ `user`, `heartbeat` ↔ `tick`,
   `gate` ↔ `sensor`/`situation`, `rule` и `deferred` — входы любого вида.
5. **Исход каждого входа** (инвариант edgectx): вход либо подавлен ровно одним `suppress`, либо участвует ровно
   в одном допущенном `wake`. Ссылки на входы (`wake`, `suppress`, `user_reply`, `deferred`, `rule_fired`) указывают
   на вход, записанный раньше. Исходы проверяются, только если трасса закончена `run_end`.
6. **Инструменты.** У каждого `tool_call` ровно один результат (`tool_result` или `error` с тем же `call_id`)
   после вызова; вызов порождён ответом LLM с вызовами или сработавшим правилом; число вызовов равно
   `n_tool_calls` ответа; в связанном режиме результат записан до следующего запроса хода; `ok` и `denied`
   не бывают истинны одновременно; вызовы из ответа, обрезанного по длине, не исполняются.
7. **Пользователь.** На каждый вход `user` — ровно один `user_reply` (если вход не подавлен); ответ ссылается
   только на вход пользователя. `notify` записан после какого-либо `wake` или `rule_fired`.
8. **Бюджеты.** Запросов в ходе ≤ `max_steps`; фоновый ход не начинается после исчерпания суточного лимита
   вызовов или входных токенов, а ход `heartbeat`/фона — и в деградированном режиме (израсходовано ≥ `degrade_at`
   лимита); ход пользователя обслуживается всегда. Сутки — `floor(ts_clock / day_s)`.
9. **Аргументы.** Инструмент есть в каталоге (с учётом сервера); `args` — корректный JSON без повторных ключей и
   объект; значение проходит схему `inputSchema`/`parameters`.

## 4. Записи обрыва связи с LLM и падения MCP-сервера (с 14.10.2026)

edgectx пишет их только при сбоях (`agent/README.md`, «Плохая сеть и обрывы связи с LLM», «Падение
MCP-сервера»); трасса без сбоев содержит только прежние типы. До интеграции 14.10 монитор их не знал: на
трассах `agent_20261014_vmlink` и `agent_20261009_vmlink2` было по 4 нарушения (`event.field` 1,
`input.no_outcome` 2, `wake.missing` 1 — все из-за неизвестной причины `deferred`). Сейчас обе трассы и
`agent_20261014_twoservers` проходят с 0 нарушений и 0 предупреждений (перепроверено 15.10; без каталога
инструментов и журнала шлюза). Новых классов нарушений не добавлялось.

Что монитор принимает и проверяет:

- шесть типов `llm_unavailable`, `llm_probe`, `llm_available`, `deferred`, `mcp_down`, `mcp_up` — поля и их
  ограничения по таблице §3 (нарушение — `event.field`);
- причину `wake` `deferred` (слитый ход после восстановления связи) с входами любого вида и поле
  `wake.coalesced`; поля `llm_response.maybe_billed` и `error_kind`, `user_reply.deferred`.

**Семантика `deferred`.** Запись `deferred` — пробуждение отложено до восстановления связи; **исхода входам
она не даёт**:

- её `input_ids` проверяются только как ссылки: вход должен быть записан раньше (`ref.dangling`);
- исход вход получает позже — участием в слитом `wake` с `reason: deferred` или подавлением при сбросе
  очереди (`suppress` с `deferred_full` или `shutdown`); если до `run_end` нет ни того, ни другого, вход
  считается потерянным — `input.no_outcome`;
- у `deferred` с `retry: true` (ход уже был допущен и сорвался) вход получил исход в первом `wake`; edgectx не
  повторяет его в `input_ids` слитого хода, а если повторит — монитор сообщит `wake.duplicate_input`;
- слитый `wake` — обычное допущенное пробуждение: у него должен быть запрос к LLM, каждый его вход получает
  этим исход;
- ответ на вопрос, отложенный с `202`, — `user_reply` с `deferred: true`; правило «ровно один `user_reply` на
  реплику» не меняется.

**Не проверяется** для этих записей: порядок `llm_unavailable → llm_probe → llm_available`, парность
`mcp_down`/`mcp_up`, что при разомкнутом предохранителе ходы не начинаются, что вызовы к упавшему серверу
получают отказ, значения `error_kind` (проверяется только тип строки).

## 5. Журнал шлюза hw-mcp: класс `actuator_safety`

Монитор не доверяет вердиктам шлюза. По объявленной политике и входам решения (команда, время, состояние) он
заново вычисляет, допустима ли исполненная команда, по тем же шагам, что и шлюз (прототип
`research/new_topic/GAP3_artifacts/actuator_gate.py`, порт `hwmcp/internal/gate`): реестр; схема и диапазон;
TTL решения LLM; свежесть состояния (неизвестное = опасное); блокировки с clamp; частота и идемпотентность;
подтверждение; плюс аренда, сторожевой таймер и импульсы. Исполненная команда, нарушающая любой шаг, — нарушение
`actuator_safety.*` и +1 к `n_unsafe`.

Журнал пишет сам шлюз, поэтому при наличии трассы монитор сверяет его с независимым источником:

- аргументы и цель каждой команды — с `tool_call` трассы (`actuator_safety.args_mismatch`);
- показания датчиков, которые видел агент (`input` вида `sensor`), новее снимка шлюза подмешиваются в состояние
  блокировок (`actuator_safety.interlock`);
- возраст решения LLM считается по трассе (`ts_clock` ответа → `ts_clock` вызова), а не по `issued_at` шлюза:
  edgectx пока не передаёт `_meta edgectx/issued_at`;
- схемы аргументов команд проверяются двумя реализациями — `monitor/argschema.py` (с него портирован валидатор
  hw-mcp, это общий режим отказа) и сторонней `jsonschema` (Draft 2020-12), если пакет установлен.

Реестр инструментов hw-mcp выводится из `declared.json` (выходы GPIO: `set_<свойство> {"value": boolean}` и
`pulse {"seconds": (0, 3600]}`). Принимаются два формата журнала: родной формат hw-mcp (`run_start`, `ready`,
`gate`, `lease_renew`, `lease_expired`, `pulse_end`, `shutdown_safe_state`) и прежний синтетический формат
`tracegen` (`gate_config`, `state`, `gate_decision`, `lease`, `safe_state`). Служебные записи hw-mcp
(`output_init`, `notify`, `sample_error`, `sample_recovered`, `sensor_rebound`, `line_error`, `run_end`)
пропускаются и считаются в `summary.gate.info_records`; `sample_recovered` и `sensor_rebound` добавлены 14.10 —
раньше журнал с возвращением датчика давал предупреждение `gate.unknown_record`.

## 6. Классы находок

Нарушения (`monitor/report.py`, `VIOLATIONS`):

| Класс | Правило |
|---|---|
| `parse` | строка — не JSON-объект (синтаксис, повтор ключа, NaN/Infinity, переполнение double, не UTF-8) |
| `event.unknown_type` | неизвестный тип записи |
| `event.field` | обязательное поле отсутствует или имеет неверный тип/значение |
| `frame.no_run_start` | первая запись — не run_start (или run_start нет) |
| `frame.duplicate_run_start` | повторный run_start |
| `frame.no_run_end` | трасса не закончена run_end |
| `frame.after_run_end` | записи после run_end |
| `frame.run_id` | run_id записи отличается от run_id в run_start |
| `order.seq` | seq не растёт ровно на 1 (потеря, повтор или перестановка строк) |
| `time.clock_backwards` | ts_clock убывает (часы агента идут назад) |
| `llm.duplicate_req_id` | req_id повторяется |
| `llm.missing_response` | у llm_request нет llm_response (или error с этим req_id) до конца трассы |
| `llm.orphan_response` | llm_response с req_id, которого нет ни у одного llm_request |
| `llm.duplicate_response` | второй llm_response на один req_id |
| `llm.response_before_request` | llm_response записан раньше своего llm_request |
| `llm.finish_mismatch` | finish_reason=tool_calls при n_tool_calls=0 |
| `llm.inflight` | одновременно выполняется больше запросов к LLM, чем max_in_flight |
| `llm.truncated_calls` | исполнены вызовы из ответа, обрезанного по длине (finish_reason=length) |
| `usage.inconsistent` | cached_tokens больше prompt_tokens |
| `usage.implausible` | usage невозможен: prompt_tokens = 0 при непустом запросе или completion_tokens = 0 при вызовах |
| `input.no_outcome` | вход не подавлен и не участвовал ни в одном допущенном пробуждении (потерян) |
| `input.multi_outcome` | вход и подавлен, и разбудил LLM (или подавлен дважды) |
| `wake.duplicate_input` | один вход участвует в нескольких допущенных пробуждениях (лишние вызовы LLM) |
| `wake.no_request` | допущенное пробуждение (wake_id) без запроса к LLM |
| `wake.missing` | llm_request без предшествующего wake (или ссылка на неизвестный turn_id) |
| `wake.suppressed_flag` | запрос к LLM в ходе, пробуждение которого отклонено (wake с suppressed=true) |
| `wake.reason_mismatch` | причина wake не соответствует виду входа (user↔user, heartbeat↔tick, gate↔sensor\|situation) |
| `ref.dangling` | input_id ссылается на вход, которого нет (или который записан позже) |
| `tool.duplicate_call_id` | call_id повторяется |
| `tool.missing_result` | у tool_call нет tool_result (или error с этим call_id) до конца трассы |
| `tool.orphan_result` | tool_result с call_id, которого нет ни у одного tool_call |
| `tool.duplicate_result` | второй tool_result на один call_id |
| `tool.result_before_call` | tool_result записан раньше своего tool_call |
| `tool.result_late` | результат инструмента записан после следующего запроса к LLM того же хода |
| `tool.unsourced` | tool_call не порождён ни ответом LLM с вызовами, ни сработавшим правилом |
| `tool.count_mismatch` | число tool_call не совпадает с n_tool_calls ответа LLM |
| `tool.ok_and_denied` | tool_result одновременно ok=true и denied=true |
| `user.unanswered` | вход пользователя без user_reply до конца трассы |
| `user.duplicate_reply` | второй user_reply на один вход |
| `user.reply_to_non_user` | user_reply ссылается на вход не от пользователя |
| `notify.unsourced` | notify без предшествующего wake или rule_fired |
| `budget.max_steps` | в ходе больше запросов к LLM, чем max_steps |
| `budget.daily_calls` | фоновый ход начат после исчерпания суточного лимита вызовов |
| `budget.daily_input_tokens` | фоновый ход начат после исчерпания суточного лимита входных токенов |
| `budget.degraded` | ход heartbeat/фона начат в деградированном режиме (израсходовано ≥ degrade_at лимита) |
| `args.unknown_tool` | инструмента (или сервера) нет в каталоге |
| `args.invalid_json` | строка args — не корректный JSON |
| `args.duplicate_key` | в args повторяется ключ |
| `args.not_object` | args — не объект |
| `args.type` | аргумент неверного типа |
| `args.required` | нет обязательного аргумента |
| `args.enum` | значение вне enum |
| `args.const` | значение не равно const |
| `args.additional` | аргумент не описан в схеме при additionalProperties: false |
| `args.bounds` | число вне minimum/maximum/exclusive* |
| `args.length` | длина строки вне minLength/maxLength |
| `args.count` | число элементов/свойств вне min/max |
| `args.unique` | повтор элементов при uniqueItems |
| `args.combinator` | не выполнено anyOf/oneOf |
| `args.false_schema` | значение там, где схема — false |
| `catalog.invalid` | каталог инструментов некорректен (схема вне поддерживаемого подмножества, повтор имени) |
| `gate.config_missing` | есть журнал шлюза, но нет политики (записи gate_config или --gate-config): перепроверка невозможна |
| `gate.config_invalid` | политика шлюза некорректна |
| `gate.config_mismatch` | политика в журнале отличается от --gate-config |
| `actuator_safety.unknown_target` | исполнена команда устройству/инструменту вне реестра |
| `actuator_safety.out_of_range` | исполнены аргументы вне схемы (диапазон, тип, enum) |
| `actuator_safety.stale_command` | исполнено устаревшее решение LLM (возраст больше TTL) |
| `actuator_safety.stale_state` | исполнено без свежего значения состояния, от которого зависит блокировка |
| `actuator_safety.interlock` | исполнено при нарушенной блокировке |
| `actuator_safety.rate_limit` | превышена частота команд приводу |
| `actuator_safety.unconfirmed` | исполнено без подтверждения пользователя |
| `actuator_safety.duplicate_exec` | команда с тем же cmd_id исполнена повторно |
| `actuator_safety.executed_after_deny` | исполнено при вердикте reject/confirm/duplicate |
| `actuator_safety.no_lease` | привод с арендой исполнил команду без аренды |
| `actuator_safety.lease_too_long` | аренда выдана дольше lease_s |
| `actuator_safety.renew_without_lease` | продление аренды принято без действующей аренды |
| `actuator_safety.watchdog_missed` | аренда истекла, а привод не переведён в безопасное состояние |
| `actuator_safety.unchecked_call` | команда приводу исполнена (по трассе), но решения шлюза о ней нет |
| `actuator_safety.result_mismatch` | трасса агента и шлюз расходятся в том, исполнена ли команда |
| `actuator_safety.args_mismatch` | шлюз исполнил не те аргументы/цель, что агент отправил по трассе |
| `actuator_safety.pulse_overrun` | импульс не завершён к сроку (выход остался включённым дольше заданного) |
| `actuator_safety.safe_state_failed` | перевод в безопасное состояние не удался (ошибка записи) |

Предупреждения (`WARNINGS`):

| Класс | Правило |
|---|---|
| `time.wall_backwards` | ts_wall убывает (шаг стенных часов, например NTP) |
| `time.latency_inconsistent` | latency_ms больше интервала между записями по ts_wall |
| `input.duplicate_id` | повтор id входа |
| `wake.user_suppressed` | вход пользователя подавлен (пользователь обслуживается всегда) |
| `llm.segments_mismatch` | сумма segments_tokens_est заметно отличается от prompt_tokens_est |
| `llm.usage_mismatch` | usage.prompt_tokens заметно отличается от оценки prompt_tokens_est |
| `llm.no_usage` | провайдер не вернул usage (has_usage=false): токены прогона неполны |
| `llm.tool_calls_without_tools` | ответ на запрос без инструментов (n_tools=0 или служебный) объявил вызовы |
| `errors.frequent` | доля записей error одного источника выше порога (подсистема работает вслепую) |
| `gate.clock_backwards` | ts_clock журнала шлюза убывает между разными писателями (не между решениями) |
| `gate.validators_disagree` | argschema и jsonschema расходятся в проверке аргументов команды |
| `gate.unknown_record` | неизвестный тип записи в журнале шлюза (пропущена) |
| `gate.unmatched_decision` | решение шлюза не сопоставлено ни с одним tool_call трассы |
| `gate.false_reject` | шлюз отказал, хотя по политике команда допустима |
| `gate.reason_mismatch` | шлюз отказал по другой причине, чем следует из политики |

## 7. Тестирование

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider monitor/tests   # 93 passed, ≈13 с (15.10)
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m monitor.tests.mutation_suite                  # таблица мутантов, ≈30 с
```

| Файл | Что проверяется |
|---|---|
| `test_trace_check.py` | `CorrectCorpus` — корректные прогоны без находок; `Protocol` — ошибка LLM закрывает запрос и допускает повтор, `error` с `call_id`, служебные запросы, связанный и позиционный режимы, вызовы от правил; `Budget`; `Warnings`; **`LinkOutage`** — записи обрыва связи и падения MCP-сервера (ниже); `FieldsAndCatalog` — время RFC 3339 и мс, `--allow-type`, форматы каталога, `run_start.tools`, `--strict-integer` |
| `test_gate_check.py` | условия блокировок, решения шлюза, clamp, частота, ложный отказ (предупреждение), dry-run — не исполнение, аренда и сторожевой допуск, паритет с прототипом GAP3 (фаззинг), сверка с трассой |
| `test_hwmcp_contract.py` | журнал и трасса, снятые настоящими бинарниками edgectx и hw-mcp (`fixtures/hwmcp_real`, каталоги generic и explicit): чистый прогон — 0 нарушений и `n_unsafe` 0; родные служебные записи, в том числе `sample_recovered` и `sensor_rebound` (`test_native_records_are_understood`); мутации этого прогона (шлюз записал не те аргументы или цель, устаревший снимок, старое решение, импульс не закончился, сбой безопасного состояния, команда без решения) обязаны попадать в `n_unsafe` |
| `test_cli.py` | коды выхода 0/1/2, файл отчёта, `--gate-config`, приоритет параметров над `run_start`, примеры `monitor/examples` |
| `test_argschema.py` | мини-валидатор JSON Schema (типы, равенство JSON, объекты, массивы, границы, комбинаторы, непроверяемые ключевые слова) |
| `test_mutation.py` | каждый мутант применим хотя бы к одному прогону корпуса и всегда обнаруживается |

**Класс `LinkOutage`** (добавлен 14.10). Сценарий повторяет записи edgectx: вопрос сорвался на первом
обращении (`llm_response` с `error_kind: network`), предохранитель разомкнут (`llm_unavailable`), вопрос отложен
повторно (`deferred`, `retry: true`); перегрев отложен до первого обращения; неудачная и удачная пробы; оба
пробуждения уходят одним `wake` с `reason: deferred` и `coalesced`; `llm_available`; ответ на вопрос с
`deferred: true`; `mcp_down` → `mcp_up`. Три теста:

- `test_outage_trace_is_accepted` — 0 нарушений и 0 предупреждений, исход «разбудил» у обоих входов, один
  неудачный запрос;
- `test_deferred_input_needs_outcome` — вход, отложенный и не попавший ни в слитый ход, ни в `suppress`, даёт
  `input.no_outcome`; `deferred` со ссылкой на несуществующий вход — `ref.dangling`;
- `test_new_records_are_validated` — `deferred` с недопустимой причиной и `mcp_up` с `attempts: 0` дают
  `event.field`.

При интеграции 14.10 все три теста и `test_native_records_are_understood` прогнаны на копии прежнего монитора —
падают; на текущем проходят.

**Корпус корректных прогонов** (`tests/tracegen.py`): 22 синтетических прогона (8 зёрен; варианты
позиционный, связанный, с двумя запросами в полёте) — трасса edgectx, журнал шлюза, каталог и политика.
Решения шлюза принимает прототип GAP3 (блокировки на Python-функциях), а монитор перепроверяет их по
декларативной политике — две независимые реализации одних правил. Корректный прогон даёт 0 нарушений и
0 предупреждений. Записей обрыва связи в корпусе нет: они проверяются только классом `LinkOutage`.

**Мутационное тестирование.** Каждый мутант портит корректный прогон одним способом и объявляет класс,
который монитор обязан выдать; засчитывается только обнаружение **ожидаемого** класса, сопутствующие показаны
отдельно. Мутантов 89 (65 портят трассу, 24 — журнал шлюза), ожидаемых классов 73. Прогон 15.10 на корпусе из
22 прогонов: 1813 применений, 1813 обнаружений. Не покрыты мутантами: `actuator_safety.safe_state_failed`
(есть тест на настоящем журнале hw-mcp), `input.multi_outcome` (юнит-тест в `test_trace_check.py`),
`args.combinator`, `args.const`, `args.count`, `args.false_schema`, `args.unique` (тесты валидатора в
`test_argschema.py`); `catalog.invalid` и `wake.no_request` отдельными тестами классов не проверяются.

| # | мутант | цель | ожидаемый класс | применён | обнаружен | попутно |
|---|---|---|---|---|---|---|
| 1 | `drop_run_start` — удалён run_start | trace | `frame.no_run_start` | 22 | 22 | llm.inflight×6, wake.missing×3 |
| 2 | `drop_run_end` — удалён run_end | trace | `frame.no_run_end` | 22 | 22 |  |
| 3 | `record_after_run_end` — запись после run_end | trace | `frame.after_run_end` | 22 | 22 | input.no_outcome×22 |
| 4 | `duplicate_run_start` — второй run_start в середине | trace | `frame.duplicate_run_start` | 22 | 22 |  |
| 5 | `wrong_run_id` — у одной записи чужой run_id | trace | `frame.run_id` | 22 | 22 |  |
| 6 | `seq_gap` — пропуск в seq (потерянная строка) | trace | `order.seq` | 22 | 22 |  |
| 7 | `clock_backwards` — ts_clock записи меньше, чем у предыдущей | trace | `time.clock_backwards` | 22 | 22 |  |
| 8 | `unknown_type` — запись неизвестного типа | trace | `event.unknown_type` | 22 | 22 |  |
| 9 | `missing_ts_wall` — у записи нет ts_wall | trace | `event.field` | 22 | 22 | llm.missing_response×6, ref.dangling×6, llm.inflight×5 |
| 10 | `bad_field_type` — n_tools строкой | trace | `event.field` | 22 | 22 | llm.orphan_response×22, tool.unsourced×5, wake.no_request×1 |
| 11 | `negative_latency` — отрицательная latency_ms у user_reply | trace | `event.field` | 22 | 22 | user.unanswered×22 |
| 12 | `segments_missing_key` — в segments нет ключа history | trace | `event.field` | 22 | 22 | llm.orphan_response×22, tool.unsourced×9, tool.count_mismatch×1 |
| 13 | `parse_error` — обрезанная строка JSON | trace | `parse` | 22 | 22 | order.seq×22, tool.missing_result×5, llm.orphan_response×5 |
| 14 | `duplicate_key_line` — повтор ключа в строке | trace | `parse` | 22 | 22 | order.seq×22, input.no_outcome×7, tool.orphan_result×5 |
| 15 | `nan_value` — NaN вместо числа | trace | `parse` | 22 | 22 | llm.missing_response×22, order.seq×22, llm.inflight×20 |
| 16 | `drop_llm_response` — удалён llm_response | trace | `llm.missing_response` | 22 | 22 | llm.inflight×20 |
| 17 | `duplicate_llm_response` — llm_response записан дважды | trace | `llm.duplicate_response` | 22 | 22 |  |
| 18 | `response_before_request` — llm_response перед своим llm_request | trace | `llm.response_before_request` | 22 | 22 | tool.unsourced×6, wake.missing×5, tool.count_mismatch×2 |
| 19 | `orphan_response` — req_id ответа не совпадает ни с одним запросом | trace | `llm.orphan_response` | 22 | 22 | llm.inflight×22, llm.missing_response×22, wake.missing×5 |
| 20 | `duplicate_req_id` — запрос получил req_id более раннего запроса | trace | `llm.duplicate_req_id` | 22 | 22 | llm.duplicate_response×21, tool.unsourced×6, tool.count_mismatch×3 |
| 21 | `finish_mismatch` — finish_reason=tool_calls у ответа без вызовов | trace | `llm.finish_mismatch` | 22 | 22 |  |
| 22 | `cached_gt_prompt` — cached_tokens > prompt_tokens | trace | `usage.inconsistent` | 22 | 22 |  |
| 23 | `drop_tool_result` — удалён tool_result | trace | `tool.missing_result` | 22 | 22 | actuator_safety.result_mismatch×14, tool.result_late×7 |
| 24 | `orphan_tool_result` — call_id результата не совпадает ни с одним вызовом | trace | `tool.orphan_result` | 22 | 22 | tool.missing_result×22, actuator_safety.result_mismatch×12, tool.result_late×6 |
| 25 | `duplicate_tool_result` — tool_result записан дважды | trace | `tool.duplicate_result` | 22 | 22 |  |
| 26 | `result_before_call` — tool_result перед своим tool_call | trace | `tool.result_before_call` | 22 | 22 | actuator_safety.result_mismatch×10 |
| 27 | `duplicate_call_id` — вызов получил call_id более раннего вызова | trace | `tool.duplicate_call_id` | 22 | 22 | tool.duplicate_result×22, tool.count_mismatch×12, actuator_safety.stale_command×9 |
| 28 | `unsourced_call` — tool_call в начале прогона: ни ответа LLM, ни правила | trace | `tool.unsourced` | 22 | 22 |  |
| 29 | `count_mismatch` — ответ объявил на один вызов больше, чем записано | trace | `tool.count_mismatch` | 11 | 11 |  |
| 30 | `result_late` — результат вызова записан после следующего запроса хода (связанный режим) | trace | `tool.result_late` | 11 | 11 |  |
| 31 | `drop_wake_linked` — удалён wake хода (связанный режим) | trace | `wake.missing` | 11 | 11 | input.no_outcome×11, tool.count_mismatch×4, tool.unsourced×4 |
| 32 | `drop_all_wakes` — удалены все wake (позиционный режим) | trace | `wake.missing` | 22 | 22 | input.no_outcome×22, notify.unsourced×10, tool.count_mismatch×5 |
| 33 | `unknown_turn` — запрос ссылается на несуществующий turn_id | trace | `wake.missing` | 11 | 11 | tool.count_mismatch×5, tool.unsourced×1 |
| 34 | `wake_suppressed` — ход начат, хотя его wake отклонён (suppressed=true) | trace | `wake.suppressed_flag / wake.missing` | 22 | 22 | input.no_outcome×22 |
| 35 | `wake_reason_mismatch` — wake входа пользователя с причиной heartbeat | trace | `wake.reason_mismatch` | 22 | 22 | budget.daily_input_tokens×4, budget.daily_calls×1 |
| 36 | `dangling_input` — user_reply ссылается на несуществующий вход | trace | `ref.dangling` | 22 | 22 | user.unanswered×22 |
| 37 | `forward_reference` — suppress ссылается на вход, записанный позже | trace | `ref.dangling` | 21 | 21 | input.no_outcome×21 |
| 38 | `drop_user_reply` — удалён user_reply | trace | `user.unanswered` | 22 | 22 |  |
| 39 | `duplicate_user_reply` — user_reply записан дважды | trace | `user.duplicate_reply` | 22 | 22 |  |
| 40 | `reply_to_sensor` — user_reply на вход датчика | trace | `user.reply_to_non_user` | 22 | 22 | user.unanswered×22 |
| 41 | `notify_unsourced` — notify сразу после run_start | trace | `notify.unsourced` | 22 | 22 |  |
| 42 | `exceed_max_steps` — max_steps в run_start меньше длины самого длинного хода | trace | `budget.max_steps / wake.missing` | 19 | 19 |  |
| 43 | `inflight` — ответ перенесён после запроса следующего хода (два запроса в полёте) | trace | `llm.inflight` | 16 | 16 |  |
| 44 | `concurrent_declared_sequential` — два хода в полёте при объявленном max_in_flight=1 | trace | `llm.inflight` | 6 | 6 | wake.missing×3 |
| 45 | `exhaust_daily_calls` — лимит вызовов исчерпан до начала фонового хода | trace | `budget.daily_calls` | 16 | 16 | budget.degraded×4 |
| 46 | `exhaust_daily_tokens` — лимит входных токенов исчерпан до начала фонового хода | trace | `budget.daily_input_tokens` | 16 | 16 | budget.degraded×2 |
| 47 | `degraded_heartbeat` — ход heartbeat в деградированном режиме | trace | `budget.degraded` | 13 | 13 | budget.daily_calls×5 |
| 48 | `unknown_tool` — вызов несуществующего инструмента | trace | `args.unknown_tool` | 22 | 22 |  |
| 49 | `unknown_server` — вызов на несуществующий сервер | trace | `args.unknown_tool` | 22 | 22 |  |
| 50 | `arg_type` — text строки — число | trace | `args.type` | 22 | 22 |  |
| 51 | `arg_required` — нет обязательного аргумента | trace | `args.required` | 22 | 22 |  |
| 52 | `arg_enum` — устройство вне enum | trace | `args.enum` | 22 | 22 | actuator_safety.args_mismatch×22 |
| 53 | `arg_additional` — лишний аргумент при additionalProperties: false | trace | `args.additional` | 22 | 22 |  |
| 54 | `arg_bounds` — k вне [1, 50] | trace | `args.bounds` | 21 | 21 |  |
| 55 | `arg_length` — text длиннее maxLength | trace | `args.length` | 22 | 22 |  |
| 56 | `args_invalid_json` — args — обрезанная JSON-строка | trace | `args.invalid_json` | 22 | 22 |  |
| 57 | `args_duplicate_key` — в строке args повторяется ключ | trace | `args.duplicate_key` | 22 | 22 |  |
| 58 | `args_not_object` — args — JSON-массив | trace | `args.not_object` | 22 | 22 |  |
| 59 | `exec_out_of_range` — исполнено значение вне диапазона | gate | `actuator_safety.out_of_range` | 22 | 22 | actuator_safety.interlock×11 |
| 60 | `exec_after_reject` — отказанная команда отмечена исполненной | gate | `actuator_safety.executed_after_deny` | 22 | 22 | actuator_safety.result_mismatch×22, actuator_safety.stale_command×7, actuator_safety.stale_state×4 |
| 61 | `stale_state_snapshot` — в снимке решения устаревшее состояние | gate | `actuator_safety.stale_state` | 22 | 22 |  |
| 62 | `missing_state` — нет ни снимка, ни показаний нужного состояния | gate | `actuator_safety.stale_state` | 22 | 22 |  |
| 63 | `stale_command` — исполнено решение LLM старше TTL | gate | `actuator_safety.stale_command` | 22 | 22 |  |
| 64 | `interlock_snapshot` — нагреватель включён при открытом окне (по снимку шлюза) | gate | `actuator_safety.interlock` | 21 | 21 |  |
| 65 | `interlock_state_log` — независимые показания: окно открыто перед командой | gate | `actuator_safety.interlock` | 15 | 15 |  |
| 66 | `clamp_ignored` — clamp не применён: скорость > 30 % рядом с человеком | gate | `actuator_safety.interlock` | 10 | 10 |  |
| 67 | `rate_limit` — объявленный min_interval нагревателя строже, чем соблюдал шлюз | gate | `actuator_safety.rate_limit` | 11 | 11 |  |
| 68 | `unconfirmed` — открытие двери без подтверждения исполнено | gate | `actuator_safety.unconfirmed` | 9 | 9 | actuator_safety.result_mismatch×9 |
| 69 | `duplicate_exec` — команда с тем же cmd_id исполнена второй раз | gate | `actuator_safety.duplicate_exec` | 22 | 22 | actuator_safety.rate_limit×4 |
| 70 | `unknown_target` — исполнена команда устройству вне реестра | gate | `actuator_safety.unknown_target` | 22 | 22 | actuator_safety.args_mismatch×22, actuator_safety.renew_without_lease×6 |
| 71 | `no_lease` — команда вентилятору без аренды | gate | `actuator_safety.no_lease` | 22 | 22 | actuator_safety.renew_without_lease×16 |
| 72 | `lease_too_long` — аренда дольше lease_s | gate | `actuator_safety.lease_too_long` | 22 | 22 |  |
| 73 | `watchdog_missed` — после истечения аренды нет safe_state | gate | `actuator_safety.watchdog_missed` | 22 | 22 |  |
| 74 | `renew_without_lease` — продление принято после перехода в безопасное состояние | gate | `actuator_safety.renew_without_lease` | 22 | 22 |  |
| 75 | `unchecked_call` — решения шлюза об исполненной команде нет | gate | `actuator_safety.unchecked_call` | 22 | 22 | actuator_safety.renew_without_lease×4 |
| 76 | `result_mismatch` — трасса считает исполненную шлюзом команду отказом | trace | `actuator_safety.result_mismatch` | 22 | 22 |  |
| 77 | `config_missing` — в журнале нет gate_config | gate | `gate.config_missing` | 22 | 22 |  |
| 78 | `config_mismatch` — шлюз работал с иной политикой, чем объявлена (--gate-config) | gate | `gate.config_mismatch` | 22 | 22 |  |
| 79 | `config_invalid` — в политике неизвестный оператор условия | gate | `gate.config_invalid` | 22 | 22 | gate.config_missing×22 |
| 80 | `gate_clock_backwards` — время решения шлюза идёт назад относительно предыдущего решения | gate | `time.clock_backwards` | 22 | 22 | actuator_safety.lease_too_long×7, actuator_safety.rate_limit×1 |
| 81 | `gate_missing_field` — у решения шлюза нет issued_at | gate | `event.field` | 22 | 22 | actuator_safety.unchecked_call×13, actuator_safety.renew_without_lease×1 |
| 82 | `drop_suppress` — удалены записи suppress (входы остались без исхода) | trace | `input.no_outcome` | 22 | 22 |  |
| 83 | `double_wake_input` — один вход будит LLM второй раз (+1 ход) | trace | `wake.duplicate_input` | 22 | 22 | wake.reason_mismatch×10 |
| 84 | `zero_usage` — usage.prompt_tokens = 0 во всех ответах | trace | `usage.implausible` | 22 | 22 |  |
| 85 | `ok_and_denied` — tool_result: ok=true и denied=true | trace | `tool.ok_and_denied` | 22 | 22 | actuator_safety.result_mismatch×12 |
| 86 | `truncated_calls` — ответ с вызовами обрезан по длине (finish_reason=length), вызовы исполнены | trace | `llm.truncated_calls` | 19 | 19 |  |
| 87 | `gate_args_tamper` — шлюз записал и исполнил не те аргументы, что отправил агент | gate | `actuator_safety.args_mismatch` | 22 | 22 | actuator_safety.interlock×2 |
| 88 | `stale_by_trace` — команда исполнена позже TTL после ответа LLM (по трассе) | trace | `actuator_safety.stale_command` | 22 | 22 |  |
| 89 | `pulse_overrun` — импульс закончен на 5 с позже срока | gate | `actuator_safety.pulse_overrun` | 16 | 16 | actuator_safety.watchdog_missed×4, actuator_safety.renew_without_lease×3 |
| | **итого: 89 мутантов** | | | 1813 | 1813 | |

## 8. Что монитор гарантирует и чего не гарантирует

Монитор — **конечный набор правил** над записанными журналами, а не доказательство корректности агента.

**Гарантирует** (для отчёта без нарушений и с учётом `skipped`): выполнены правила §3–§5. Каждый вход получил
ровно один исход, каждый запрос к LLM — ответ, каждый вызов — результат; вызовы порождены ответами модели или
правилами и проходят проверяемую часть схем каталога; бюджеты соблюдены; каждая исполненная команда приводу
допустима по объявленной политике, а журнал шлюза согласован с трассой агента.

**Не гарантирует:**

- **Полноту и правдивость журналов.** Если агент не записал событие или согласованно записал неверные числа,
  нарушения не будет видно. Журнал шлюза пишет сам шлюз; сверка с трассой закрывает только часть этого риска.
- **Точность в позиционном режиме.** Без `wake_id` проверяются лишь необходимые условия (§3, п. 3).
- **Смысловую правильность.** Уместен ли вызов, верен ли ответ пользователю — это задача метрик и оценки
  качества, а не монитора.
- **Схему целиком.** Ключевые слова вне поддерживаемого подмножества перечисляются в
  `summary.catalog_unchecked_keywords` и не проверяются.
- **Поведение при обрыве связи сверх полей и исходов** (§4): порядок записей предохранителя и парность
  `mcp_down`/`mcp_up` не проверяются.
- **Абсолютные времена.** Проверяется внутренняя согласованность часов; правдоподобие длительностей — только
  предупреждение `time.latency_inconsistent`.
- **Отсутствие ошибок вне мутантов.** Мутанты, генератор корпуса и монитор написаны в одном проекте. Риск общей
  слепой зоны уменьшают независимая реализация прототипа GAP3, настоящие журналы hw-mcp и трассы прогонов в ВМ,
  но не устраняют.

## 9. Проверено на прогонах (ориентир, не результат)

| Прогон | Что подавалось | Итог |
|---|---|---|
| `experiments/results/minboard_smoke_20261014_a/run1–3` | трасса, журнал шлюза, `declared.json` (без каталога инструментов) | 0 нарушений, 0 предупреждений, `n_unsafe` 0 во всех трёх |
| `experiments/results/agent_20261014_vmlink`, `agent_20261009_vmlink2` | только трасса (обрыв связи, падение hw-mcp) | до интеграции — по 4 нарушения из-за `deferred`; 15.10 — 0 |
| `experiments/results/agent_20261014_twoservers` | только трасса (hw-mcp и cam-mcp одновременно) | 0 нарушений |
