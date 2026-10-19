# Спецификация реализации (v2): ИИ-агент одноплатного компьютера EdgeCtx

Версия 2, 2026-10-06; обновлено 15.10.2026 по итогам этапа «минимальная плата» (§1, §3, §4, §6, §8–§10); строка
`neurobot/` в §1 — 19.10.2026.
Основание: `docs/plan/vkr_plan.html` (план ВКР) и `research/new_topic/THESIS_CONCEPT.md` (§7–§13 — подробности).
Этот файл — контракт между компонентами. Отступления записывать в `docs/DECISIONS.md`.

## 0. Принципы
1. Агент на плате — **Go, статический бинарник** (`CGO_ENABLED=0`, GOOS=linux GOARCH=arm64 для платы; на Mac — нативно для
   разработки). Должен собираться и под linux/arm64 (Jetson Nano: ядро 4.9, glibc 2.27 — статика это покрывает).
2. **Всё время — из `Clock`** (реальные или виртуальные часы). Без этого нет ускоренного режима T0.
3. **LLM внешняя**, OpenAI-совместимый Chat Completions с tools. В разработке — только `mock-llm` (0 ₽).
   Реальный RouterAI — позже, по ключу.
4. **Измерения отделены от разработки.** Числа при параллельной работе агентов — не результаты.
5. Каждое изменчивое значение в промпте — в хвост; стабильный префикс; каноническая сериализация JSON
   (ключи отсортированы, без отступов, UTF-8 без `\u`, округление до точности датчика).
6. Безопасность приводов — детерминированный шлюз в `hw-mcp`, а не правила в промпте.

## 1. Каталоги
```
agent/            Go-модуль edgectx (github.com/local/edgectx): cmd/edgectx, internal/{agent,breaker,backoff,clock,gate,
                  sched,userctx,hwctx,prompt,llm,mcphost,trace,channel,config,…}; tests; Makefile (build-mac,
                  build-linux-arm64); bin/edgectx-{darwin,linux}-arm64
hwmcp/            Go-модуль hw-mcp: inventory из sysfs (IIO/hwmon/w1/gpiochip), --root <fixture>, линии GPIO через
                  /dev/gpiochipN (uAPI v1/v2, чип по номеру или метке) или файлы, «живые» датчики, tools/resources,
                  шлюз приводов с арендой; порт research/new_topic/GAP2_artifacts/sysfs_probe.py и GAP3_artifacts/actuator_gate.py
cammcp/           Go-модуль cam-mcp: камера с детектором движения на плате (источники synthetic | dir | v4l2); кадры в
                  LLM не уходят — агенту приходят уведомления о начале и конце эпизода и об отказе камеры; контракт
                  ресурсов как у hw-mcp
robotmcp/         Go-модуль robot-mcp (адаптер робота TurtleBot3 поверх rosbridge), robot-probe, fake-rosbridge
sysmon/           Go: сэмплер ресурсов дерева процессов (Linux: /proc/<pid>/smaps_rollup, stat, io, cgroup v2 cpu.stat/memory.current;
                  macOS: упрощённо через ps) → JSONL
bench/            Python 3.12 (.venv): daygen/, simmcp/, mockllm/, replay/, costmodel/, metrics/, sensors/, baselines/,
                  board/, sysfsim/; tests/ (в том числе сквозные тесты нескольких компонентов)
bench/board/      «убогая» плата в ВМ sbc: profiles.json (оси память, CPU, диск, сеть; пресеты плат), verify («задано vs
                  измерено»), calibrate (квота CPU → ops/s, cpubench на Go), vm.py (Vm.run)
bench/sysfsim/    симулятор «живого» sysfs: IIO/hwmon/1-Wire/thermal и линии GPIO (файлы или модуль ядра gpio-sim),
                  физика помещений, отказы датчиков, журнал истины; только stdlib (запускается системным Python ВМ)
scripts/board/    ВМ-сторона board: board.sh (netns и netem в обе стороны, расписание обрывов, run под лимитами
                  cgroup v2 через systemd-run, уборка), netem.jq, record.jq
scripts/sim/      стенд робота в ВМ sim (ROS 2 Jazzy, Gazebo Harmonic, TurtleBot3, rosbridge) — docs/robot_sim.md
harness/stats.py  статистика (+ наклон Сена с ДИ и TOST на наклоне, блочный бутстреп)
monitor/          проверка трасс edgectx (контракт §6) и журнала шлюза hw-mcp (класс actuator_safety) — docs/monitor.md;
experiments/      results/<run_id>/ (сырые JSONL, сценарии, README прогона); configs/ и PROTOCOL.md — позже
data/casas/       подмножество трасс CASAS (Zenodo 10.5281/zenodo.15708568, CC BY 4.0) + PROVENANCE
data/massive/     ru/en шаблоны команд iot (из research/new_topic/GAP4_artifacts/data)
docs/neurobot/    робот лаборатории NeuroChip: анализ (ANALYSIS.md), план (PLAN.md), разбор материалов (research/R1–R7) — готовы
neurobot/         робот NeuroChip: пакет 1 плана docs/neurobot/PLAN.md (perception v2, исполнитель, safety_gate,
                  развёртывание), двойник, наборы и прогоны — README в каталогах; архитектура и темы — PLAN §2. На 19.10
                  шаг W3 сделан только на двойнике: приёмка 17.10 — принято частично, 18.10 — пять новых замечаний,
                  исправлены 19.10, доводка независимо не перепринята; на роботе не проверялся; статус — PLAN §13.5
```
Python: `/Users/chosenundead/Projects/diploma-edge-ai/.venv/bin/python` (3.12; mcp 1.30, numpy, scipy, pandas, matplotlib,
psutil, httpx, pyyaml). Go 1.27.1 (brew). Тесты Python — unittest/pytest допустимо поставить в .venv через uv.

## 2. Формат «дня» (daygen → JSONL, одна строка — одно событие)
```json
{"t": 1234.5, "kind": "user_cmd", "text": "включи свет в лаборатории", "lang": "ru", "id": "u17",
 "expect": {"type": "action", "calls": [{"name": "write_property", "args": {"device": "relay1", "property": "on", "value": true}}]}}
{"t": 1301.0, "kind": "sensor", "device": "pir1", "property": "motion", "value": 1, "source": "casas:hh101"}
{"t": 1302.0, "kind": "situation", "id": "s4", "name": "door_open_at_night", "significant": true, "deadline_s": 300,
 "expect": {"type": "notify_or_action"}}
{"t": 2000.0, "kind": "question", "id": "q3", "text": "когда последний раз открывали дверь?", "answer": {"value": 1290.0, "unit": "t"}}
```
- `t` — секунды от начала дня (виртуальные часы). Заголовок файла (первая строка) — `{"kind":"header", "seed":…, "profile":"L|M|M+|H",
  "home":"hh101", "split":"tune|holdout", "window":[07:00,23:00], "devices":[…]}`.
- Профили команд пользователя (Bentley 2018): L=2.61, M=5.55, M+=10.1, H=30.44 команды/сутки; события — ритм выбранного дома CASAS.
- Значимые ситуации — предикаты над трассой (дверь открыта ночью, нет движения днём N ч, перегрев > порога) — задаются в daygen
  до прогонов. Дома CASAS делятся на tune/holdout детерминированно (seed).

## 3. edgectx — агент
- **Конфиг** (YAML): `clock: real|virtual`, `llm: {base_url, model, api_key_env, timeout_s}`, `mcp: [{name, transport: stdio|http,
  cmd|url}]`, `policy: {wake: heartbeat|gate|hybrid, heartbeat_min: 30, gate: {debounce_s, cooldown_s, coalesce_s, rules_path},
  history: full|window|mask|summary_system, window_turns: N, hwctx: snapshot_head|delta_tail|pull, catalog: explicit|generic}`,
  `budget: {daily_calls, daily_input_tokens}`, `trace: path`.
- **Каналы пользователя**: локальный HTTP `POST /v1/user {text}` → ответ после хода (или сразу `202` с `deferred: true`,
  если модель недоступна и вопрос отложен); `POST /v1/event` (для реплеера: события датчиков, если не через MCP);
  `POST /v1/clock/advance {to_t}` (только virtual); `GET /v1/metrics`, `GET /v1/health`, `POST /v1/shutdown`.
  Простой веб-чат — опционально. Подробно — `agent/README.md`, «HTTP-канал».
- **Цикл**: события (MCP `notifications/resources/updated`, пользовательские реплики, тики Clock) → гейт/heartbeat решает,
  будить ли LLM → сборщик промпта → LLM (tools = инструменты MCP-серверов согласно `catalog`) → вызовы инструментов
  через MCP (до `max_steps`) → ответ пользователю / уведомление → трасса.
- **Контекст пользователя**: SQLite (pure Go, напр. modernc.org/sqlite) — профиль-факты, история, правила-договорённости;
  политики истории — см. конфиг. Правила, созданные LLM (инструмент `create_rule`), исполняются гейтом без LLM.
- **Контекст оборудования**: L0 инвентарь (текстовая проекция в стабильном префиксе), L1 состояние (snapshot в начале /
  delta в хвосте / pull через инструменты), L2 события (только разбудившее + top-k по запросу), L3 `board_health` по запросу.
- **Порядок сегментов промпта**: [system + схемы инструментов + инвентарь L0] → [профиль] → [история/сводка] → [дельта
  состояния + событие] → [ход].
- **LLM-клиент**: одно keep-alive соединение, без стриминга; журналирует `usage` целиком.
- **MCP host**: официальный Go SDK (github.com/modelcontextprotocol/go-sdk), ревизия протокола 2025-11-25; stdio и streamable HTTP.
  Несколько серверов одновременно (инвентари объединяются). Упавший сервер переподключается сам с растущей паузой
  (`mcp_down` → `mcp_up`), его показания до свежего чтения помечаются «нет связи».
- **Обрыв связи с LLM** (`agent/README.md`, «Плохая сеть и обрывы связи с LLM»): повторы 429/5xx/сетевых ошибок с паузой;
  предохранитель с пробой `GET {base_url}/models` и полуоткрытым состоянием; пока модели нет, пробуждения откладываются
  (не больше `sched.deferred_max`) и после восстановления уходят одним слитым ходом. Таймаут попытки и оборванный ответ не
  повторяются (`maybe_billed`). Локальные правила, гейт и шлюз приводов работают без LLM.

## 4. MCP-серверы
- **hw-mcp (Go)**: `list_devices`, `read_property(device, property)`, `write_property(device, property, value)`,
  `invoke_action(device, action, args)`, `board_health()`; режим каталога `explicit` (инструмент на свойство:
  `read_<dev>_<prop>`, `set_<dev>_<prop>`, `<action>_<dev>`) или `generic` (4 общих); `board_health` — в обоих.
  Ресурсы: `hw://inventory`, `hw://inventory/text` (проекция L0), `hw://td` (WoT TD 1.1), `hw://probe` (сырой инвентарь),
  `hw://diag` (счётчики и ресурсы процесса — для оператора, не для LLM), `hw://state/<device>` (с подпиской; новое
  состояние — в `_meta` уведомления). Источник: sysfs (`--root` на фикстуре `research/new_topic/GAP2_artifacts/fixture_root`
  или каталоге `bench/sysfsim`) + `declared.json`; линии GPIO — `/dev/gpiochipN` (`-gpio-driver cdev`, uAPI v2 или v1 —
  `-gpio-uapi auto|v1|v2`; чип по номеру или метке) или файлы (`files`). «Живые» датчики: пропавшее устройство, нечитаемое
  или «застрявшее» значение отдаётся как `value: null` с причиной (`absent`, `unreadable`, `not_a_number`, `stuck`…),
  пропавшие IIO/hwmon ищутся заново (`-rescan-period`, период удваивается до 300 с).
  `board_health` = `{now, load, memory, uptime_s, cpu_temp_c, throttled, sensor_faults, sensor_errors}`: `sensor_faults` —
  какие свойства сейчас без значения и почему, `sensor_errors` — неудачных чтений с начала работы; поля платы читаются
  относительно `--root` (на фикстуре без `/proc` — `null`). Шлюз приводов: идемпотентность → реестр → схема и диапазон →
  TTL решения → свежесть состояния (неизвестное = опасное) → блокировки/clamp → частота → подтверждение для необратимого;
  пробная проверка без исполнения (`_meta edgectx/dry_run`); аренда со сторожевым тиком. Отказ — структурный
  (`{"denied": true, "verdict": …, "reason": …, "detail": …}`). Решения шлюза пишутся в журнал hw-mcp. Подробно —
  `hwmcp/README.md`.
- **cam-mcp (Go)**: камера с детектором движения на плате; контракт ресурсов как у hw-mcp, агент подключает её без
  изменений кода. Инструменты: `camera_status`, `motion_state`, `snapshot` (маленький JPEG; агент передаёт модели вместо
  картинки текст `[изображение …]`), по флагу `-set-detector` — `set_detector`. Ресурсы: `cam://inventory`,
  `cam://state/<камера>` (свойства `motion`, `area_pct`, `zone`). `notifications/resources/updated` — только на начало и
  конец эпизода движения и на отказ и возврат камеры; в `_meta`: `state`, `cam/event: motion_start | motion_end |
  camera_lost | camera_back`, `cam/changed`, `cam/episode` (только если есть эпизод). После 3 ошибок источника подряд
  `motion` = `null` с `error`. Журнал JSONL в формате hw-mcp; в нём, среди прочего, `episode_start`/`episode_end` и
  `source_lost`/`source_recovered`. `-bg-tau` ≤ 1h. Транспорт stdio или streamable HTTP. Подробно — `cammcp/README.md`.
- **robot-mcp (Go)**: инструменты уровня задачи для робота поверх rosbridge, состояние — событиями. Контракт —
  `robotmcp/README.md`, `docs/robot_sim.md`.
- **sim-mcp (Python, mcp SDK)**: эмулятор помещения: инвентарь (те же схемы, что hw-mcp generic/explicit), состояние,
  проигрывание событий из файла дня по `Clock` агента (сервер получает время от агента или реплеера), уведомления
  `resources/updated`, инструменты ответа `answer(value, unit)` / `refuse(reason)`, проверка цели по истинному состоянию.
  `board_health` — с тем же набором полей, что у hw-mcp (`sensor_faults: {}`, `sensor_errors: 0`), ради паритета токенов.
  Транспорт streamable HTTP (localhost).

## 5. mock-llm (Python stdlib http.server)
OpenAI-совместимый `POST /v1/chat/completions` (tools, tool_calls, usage), режимы:
- `record` — проксирует на реальный endpoint и пишет пары (хеш нормализованного запроса → ответ);
- `replay` — отвечает из записи (детерминированно, 0 ₽);
- `oracle` — для T0: по метаданным (через заголовок `X-Bench-Expect` или сопоставление с текущим событием дня из реплеера)
  возвращает правильные tool_calls/ответ; `usage` считается оценкой токенов (tiktoken o200k_base, если установлен; иначе
  эвристика) + калибровка позже. Задержка — параметр (фикс./распределение).
Пишет журнал запросов: размер промпта по сегментам, токены, время.

### 5.1. Реплеер и управление временем в T0 (bench/replay)
- Реплеер читает файл дня и по порядку событий: (1) `POST mock-llm /control/expect {input_id, expect, situation?}` —
  что считается правильной реакцией на ближайший вход (для режима oracle); (2) продвигает часы:
  `POST edgectx /v1/clock/advance {to_t}` и `POST sim-mcp /control/clock {t}` (sim-mcp на продвижение публикует
  события датчиков с `t ≤ to_t` как изменения ресурсов → `notifications/resources/updated`); (3) для `user_cmd`/`question`
  — `POST edgectx /v1/user {id, text}` и ждёт ответа; (4) для `situation` — ничего не подаёт (агент должен сам заметить по
  датчикам), фиксирует дедлайн для метрики полноты.
- В конце — `POST edgectx /v1/clock/advance {to_t: конец окна}`, сбор трасс edgectx, hw/sim-mcp, mock-llm, sysmon в
  `experiments/results/<run_id>/`, `manifest.json` (конфиги, seed, sha бинарников, версии).
- Oracle в mock-llm отвечает «как идеальная модель» на основании `expect`: tool_calls для команд, `answer` для вопросов,
  уведомление/действие для значимых ситуаций, «ничего не делать» для фоновых heartbeat без ситуаций. Так T0 меряет
  затраты политик (вызовы, токены, трафик), а не качество модели; качество меряется позже на живой модели.

## 6. Трасса edgectx (JSONL, по событию на строку)
Обязательные поля каждой записи: `ts_wall`, `ts_clock`, `type`, `run_id`; edgectx пишет также `seq` (сквозной номер).
Здесь — минимальный контракт; все поля — `agent/README.md`, раздел «Трасса», константы типов — `agent/internal/trace/trace.go`.
Основные типы:
`run_start{config_hash, policy, …, breaker, runtime}`, `input{kind:user|sensor|situation|tick, id}`,
`wake{wake_id, reason: user|gate|heartbeat|rule|deferred, suppressed, priority, input_ids, coalesced?}`,
`suppress{reason, input_id}`, `llm_request{req_id, wake_id, model, prompt_tokens_est, segments:{prefix,profile,history,state,
turn}, n_tools}`, `llm_response{req_id, latency_ms, usage:{prompt_tokens, completion_tokens, cached_tokens}, finish_reason,
n_tool_calls, attempts}`, `tool_call{call_id, server, name, args}`, `tool_result{call_id, ok, denied, ms}`,
`user_reply{input_id, text, latency_ms, ok, deferred?}`, `notify{situation_id?, text}`, `rule_fired{rule_id, input_id}`, `run_end{}`.
У неудачного обращения к LLM `llm_response` несёт `error`, `error_kind: network|timeout|http_429|http_5xx|http_4xx|response|
canceled`, `finish_reason: error`, нулевой `usage` и `maybe_billed: true`, если запрос мог быть уже обработан и оплачен
(таймаут попытки, оборванный ответ) — такой ход не повторяется. Отдельного типа `llm_error` нет.

Дополнительные типы edgectx — только при сбоях (в трассе без обрывов их нет): `error{where, msg}` — сбой подсистемы без
остановки прогона; обрыв связи с LLM — `llm_unavailable{failures, error_kind, error, retry_in_s}` (предохранитель
разомкнут), `llm_probe{ok, ms, status, bytes, error?, error_kind?, retry_in_s?}` (проба связи), `llm_available{down_s}`
(модель снова ответила), `deferred{wake_id, reason, priority, input_ids, retry, pending, coalesced?}` (пробуждение
отложено); падение MCP-сервера — `mcp_down{server, error}`, `mcp_up{server, attempts, down_s}`. Причины `suppress`
дополнены `deferred_full` (очередь отложенного полна) и `heartbeat_pending`.

Инварианты: `run_start` — первая запись, `run_end` — последняя; у каждого `llm_request` один `llm_response`, у каждого
`tool_call` один `tool_result`; **каждый вход получает ровно один исход** — `suppress` или участие в одном допущенном
`wake`; у реплики пользователя ровно один `user_reply`. Запись `deferred` исхода входу **не даёт**: исход — участие в
слитом ходе (`wake` с `reason: deferred`, слитые пробуждения — в `coalesced`, в `input_ids` — только входы без исхода,
любого вида) или сброс очереди (`suppress` с `deferred_full`/`shutdown`). У `deferred` с `retry: true` вход уже получил
исход в первом `wake`. Ответ на вопрос, отложенный с `202`, — `user_reply` с `deferred: true`.
`monitor/check_trace.py` проверяет протокол, ссылки и исходы входов, схемы аргументов, бюджеты и перепроверяет решения
шлюза по журналу hw-mcp (класс actuator_safety); см. `docs/monitor.md`. Не сделано: `bench/metrics` не разбирает эпизоды
`deferred`, `user_reply.deferred` и байты проб `llm_probe.bytes`; оракул mock-llm не умеет отвечать на слитый ход с
несколькими вопросами — нужно к прогонам с обрывами сети, таких прогонов пока нет.

## 7. Метрики (bench/metrics)
По трассе + sysmon + day-файлу: вызовы LLM/сутки (окно), входные/выходные/кэш-токены, ₽ (замороженный прайс
`research/new_topic/F_artifacts/price_table.py`), трафик, p50/p95 «событие → действие», полнота значимых ситуаций (по
`situation` с дедлайном), точность ответов на `question`, N_unsafe (из шлюза и монитора), RSS/PSS(t) и наклон Сена, CPU-с.

## 8. Режимы прогона
- **T0** (Mac): виртуальные часы; реплеер подаёт день в edgectx и sim-mcp; mock-llm `oracle`; день проигрывается за минуты.
- **T-VM**: Lima VM `sbc` (Ubuntu 24.04 arm64, 4 vCPU, 4 ГиБ, ядро 6.8.0-146-generic) как «убогая» плата. Агент и его
  MCP-серверы запускаются в одном юните `scripts/board/board.sh run <имя> --mem M --cpu P [--cpus N] [--disk D] [--net ns]
  [--gomemlimit %|off] [--timeout S] -- cmd` (systemd-run, cgroup v2; GOMEMLIMIT по умолчанию 45 % MemoryMax; итог —
  строка `board-run: {JSON}` с `reason: exit|signal|oom-kill|timeout|not-started`). Сеть — netns `board-<ns>` с netem в обе
  стороны (`net-up`, `net-set`), обрывы — `net-schedule`. Уровни и пресеты — `bench/board/profiles.json`: память
  1024…16 МиБ, квота CPU 400…5 %, диски `nvme`…`sd-awful`, сети `lan`…`gprs`, `lossy`, `satellite`, `offline`, `flaky`;
  пресеты `jetson-nano-4g` (стеку 1024 МиБ из 4 ГБ, общих с GPU), `jetson-nano-2g`, `pi-zero-2w`, `potato`, `abyss` —
  допущения до замеров на плате. Что ограничения действуют, проверяет `python -m bench.board verify` (отчёт «задано vs
  измерено», `experiments/results/board_verify_20261014_f/`). Из Python — `bench.board.vm.Vm(...).run(..., timeout)`: при
  таймауте или Ctrl+C юнит останавливается, сбой клиента он не переживёт. Датчики — `bench/sysfsim` (бэкенд gpio-sim) вне
  юнита платы: симулятор играет роль железа и ядра. Образец связки — `experiments/results/minboard_smoke_20261014_a/`
  (`run.sh` на Mac, `vm.sh` в ВМ). edgectx, hw-mcp, cam-mcp — статические бинарники linux/arm64. Числа T-VM — ориентир,
  не результат: ВМ на ноутбуке, у стенда есть известные искажения (`bench/board/README.md`, «Найденные ограничения стенда»).
- **T2** (позже): плата, реальные часы, RouterAI.

## 9. Тесты (обязательные)
- Go: юнит-тесты каждого пакета (`go test ./...`), гонки (`-race` на Mac), сборка `GOOS=linux GOARCH=arm64 CGO_ENABLED=0`.
- hw-mcp: тесты на фикстуре sysfs (паритет с `GAP2_artifacts/test_gap2.py`), шлюз (паритет с `actuator_gate.py`).
- Python: daygen детерминирован по seed; sim-mcp; mock-llm все режимы; метрики на синтетической трассе с известным ответом.
- Интеграция T0: короткий день (2 ч виртуальных) end-to-end, трасса принимается монитором.
- Сквозные тесты нескольких компонентов — в `bench/tests` (`test_hwmcp_edgectx`, `test_simmcp_edgectx`,
  `test_sysfsim_hwmcp`, `test_cammcp_edgectx`); тесты, которым нужны ВМ, root или gpio-sim, без них пропускаются
  (`BOARD_VM=1` включает ВМ-тесты board).

## 10. Ресурсы при разработке
Порты: edgectx 18080–18089, sim-mcp 18090–18099, mock-llm 18100–18109, robot-mcp 18110–18119 (по умолчанию 18110;
fake-rosbridge — 18119). hw-mcp и cam-mcp по HTTP по умолчанию слушают `127.0.0.1:0` — свободный порт, адрес печатается в
stderr и в `-ready-file`. rosbridge стенда робота — 9090 (готовый компонент ROS). Никаких реальных API-ключей. Свои
процессы останавливать по PID. Lima VM `sbc` — общая; не удалять её. ВМ-тесты board (`BOARD_VM=1`) не запускать
параллельно с `verify` и прогонами под `board.sh`. На ядре 6.8 чип gpio-sim снимать только после выхода всех
потребителей его линий и проверки по `/sys/kernel/debug/gpio`, что линии свободны: иначе возможен Oops ядра
(use-after-free в `lineevent_release`, наблюдался в ВМ 09.10); образец уборки — `hwmcp/tools/vm_gpiosim_check.sh`.
