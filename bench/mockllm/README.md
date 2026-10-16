# mock-llm — OpenAI-совместимая заглушка LLM для стенда

Сервер `POST /v1/chat/completions` (Chat Completions с `tools`, `tool_calls`, `usage`) на `http.server`
из стандартной библиотеки. Нужен, чтобы гонять агента `edgectx` без денег и без ключей: в режиме T0
(виртуальные часы) он отвечает «как идеальная модель» по разметке дня и считает, сколько токенов и
кэш-попаданий дал бы провайдер. Спецификация — `IMPLEMENTATION_SPEC.md` §5, §5.1.

Числа, полученные при разработке, — **не результаты**.

## Как запускать

Все команды — из корня репозитория (`/Users/chosenundead/Projects/diploma-edge-ai`).

```bash
# оракул T0 (порт стенда 18100–18109; 0 — случайный свободный); журнал JSONL — по флагу
.venv/bin/python -m bench.mockllm --mode oracle --port 18100 --journal run/mockllm_journal.jsonl

# то же с задержкой ответа и другой длиной сводки истории
.venv/bin/python -m bench.mockllm --mode oracle --port 18100 --latency lognormal:800:0.5 --seed 1 \
    --summary-tokens 400

# запись: прокси на настоящий API (в разработке — на второй экземпляр mock-llm)
.venv/bin/python -m bench.mockllm --mode record --port 18101 \
    --upstream http://127.0.0.1:18100/v1 --recording run/rec.jsonl [--upstream-key-env ROUTERAI_API_KEY]

# воспроизведение записи (детерминированно, 0 ₽); промах — 404 или --replay-miss oracle
.venv/bin/python -m bench.mockllm --mode replay --recording run/rec.jsonl --port 18100

# остановка — по PID (SIGTERM)
kill -TERM <pid>
```

При старте в stdout печатается одна строка JSON `{"event":"listening","host","port","mode","pid","tokenizer"}`
(`--port-file` дополнительно пишет порт в файл). Код выхода 2 — неверные аргументы.

Для edgectx в конфигурации агента нужно `llm.trace_headers: true` (заголовки `X-Edgectx-*`, см. ниже), иначе
оракул сопоставляет ходы по тексту, а время для TTL кэша берёт из `/control/clock` (конец шага реплеера).

Основные флаги:

| Флаг | По умолчанию | Смысл |
|---|---|---|
| `--cache-ttl` | 300 | TTL эмуляции префикс-кэша провайдера, с (0 — кэша нет) |
| `--cache-min-tokens`, `--cache-granularity` | 1024, 128 | правила OpenAI: кэшируется промпт ≥ 1024 токенов, `cached_tokens` кратно 128 |
| `--clock` | auto | время для TTL: заголовок часов (`X-Edgectx-Clock-T`, `X-Bench-Clock`) → `/control/clock` → стенные часы |
| `--latency` | 0 | задержка ответа, мс: `fixed:250`, `uniform:100:300`, `lognormal:800:0.5` (медиана, σ), `normal:500:100`; `--seed` |
| `--noop-text` | `""` | ответ «ничего не делать» на фоновое пробуждение без ситуаций |
| `--summary-tokens` | 400 | длина сводки оракула на запрос `purpose=summary`, токенов (`summ_len` модели `D_artifacts/cost_model.py`) |
| `--pad-profile` | targeted | добор до `k`: `targeted` — чтения целей хода, `list_devices` не больше одного раза; `legacy` — как до ревью |
| `--sequential-calls` | выкл. | вызовы команды по одному на шаг (по умолчанию все сразу, параллельно) |
| `--ignore-k` | выкл. | не добирать число вызовов LLM на вход до `k` из разметки дня |
| `--tool-tokens` | cookbook | счёт токенов описаний инструментов: `cookbook` или `json` |
| `--calib a,b` | `0,1` | калибровка `prompt_tokens = a + b·оценка` (решение 6 концепции, по данным E0-P) |
| `--reasoning-tokens` | 0 | скрытые reasoning-токены на вызов (gpt-5-*: ≈400 по допущению §14 концепции) |
| `--tls-cert/--tls-key` | — | HTTPS |

## Эндпойнты

| Метод и путь | Тело / ответ |
|---|---|
| `POST /v1/chat/completions` (и `/chat/completions`) | запрос OpenAI; `stream: true` → 400 (агент работает без потока) |
| `GET /v1/models`, `GET /healthz` | список из одной модели; `{"ok": true}` |
| `POST /control/expect` | `{input_id, expect, situation?, text?, kind?, t?, k?}` — ожидаемая реакция на ближайший вход |
| `POST /control/clock` | `{t}` — виртуальное время (с) для TTL кэша, если в запросе нет заголовка часов |
| `POST /control/expire` | `{input_id}` — снять ожидание ситуации (дедлайн прошёл) |
| `POST /control/clear` | `{input_id?}` — удалить ожидание (без id — все) |
| `POST /control/reset` | `{journal?: bool}` — полный сброс между прогонами (см. ниже); ответ `{ok, journal_seq}` |
| `GET /control/state` | счётчики, `inflight`, `idle_ms`, `journal_seq`, `clock_sources`, `clock_header_missing`, ожидания |
| `GET /control/journal[?since=N]` | журнал запросов (NDJSON), записи с `seq > N` |

`/control/reset` сбрасывает всё, от чего зависят ответы и `usage`: кэш, ожидания, счётчики id вызовов оракула
(`call_000001…`), ГСЧ задержки (пересоздаётся с `--seed`), счётчик запросов (id ответов), часы и счётчики
источников часов; `journal: true` очищает журнал в памяти. Номер `seq` журнала монотонен и не сбрасывается:
реплеер забирает только свой прогон через `GET /control/journal?since=<journal_seq из ответа reset>`.
Повторный прогон на том же экземпляре даёт те же тела запросов, id вызовов и задержки (тест A/A).

### Заголовки запроса

Первый найденный из списка; имена edgectx — как в `agent/internal/agent/turn.go` (проверяется тестом).

| Назначение | Заголовки |
|---|---|
| ожидание вместо `/control/expect` | `X-Bench-Expect` (JSON или base64(JSON)) |
| виртуальное время запроса, с (TTL кэша) | `X-Bench-Clock`, **`X-Edgectx-Clock-T`**, `X-Edgectx-Clock` |
| id входа(ов), через запятую | `X-Bench-Input-Id`, **`X-Edgectx-Input-Ids`**, `X-Edgectx-Input`, `X-Edgectx-Input-Id`, `X-Edgectx-Inputs` |
| причина пробуждения | `X-Bench-Wake`, **`X-Edgectx-Wake-Reason`**, `X-Edgectx-Wake` |
| назначение запроса (`turn` / `summary`) | `X-Bench-Purpose`, **`X-Edgectx-Purpose`** |

Все `X-Bench-*` и `X-Edgectx-*` попадают в журнал (по `X-Edgectx-Req-Id` метрики связывают журнал с
`llm_request` трассы, по `X-Edgectx-Run-Id` — отбрасывают записи чужих прогонов). В записи журнала
`clock_source` — `header | control | wall`; запрос с заголовками `X-Edgectx-*`, но без заголовка часов,
получает `warning` и увеличивает `clock_header_missing` в `/control/state` (реплеер выносит это в
`manifest.warnings`).

## Оракул

Реплеер регистрирует ожидание входа (реплика — сразу перед `POST /v1/user`, ситуация — в `t − 1 мс`). Запрос
агента оракул относит к одному из случаев (в этом порядке):

0. **Сводка истории** — `X-Edgectx-Purpose: summary` (без заголовка: запрос без `tools`, в system которого
   «сводк»/«summar»): детерминированная сводка ровно `--summary-tokens` токенов — факты расшифровки
   (`Вход:`, `Вызов:`, `Агент:` и строки прежней сводки, самые свежие в пределах длины), остаток —
   нейтральный текст. **Ожидания не трогаются никогда**, при любом тексте запроса.
1. **Продолжение хода** — в хвосте сообщения `tool` с id вызовов, которые выдал оракул: следующий шаг или
   финальный ответ (`Готово.`; для вопроса — `"<value> <unit>"`). Отказ шлюза в результате инструмента
   (`{"denied": true, "reason": …}`) завершает ход без повторов.
2. **Новый ход пользователя** — подсказка `X-Edgectx-Input-Ids` с id ожидания; если подсказки нет или id
   неизвестен — последнее сообщение `user` содержит текст ожидаемой реплики (без учёта регистра и лишних
   пробелов). Сопоставление по тексту — только для `purpose = turn` и `wake = user`; известный, но уже
   выполненный id в подсказке запрещает сопоставление по тексту (чужое ожидание не перехватывается).
   - команда (`type: action`) → `tool_calls` из `expect.calls`;
   - вопрос (`type: answer`) → `answer(value, unit)` из `expect.calls` или `expect.answer`;
   - невалидная (`type: refuse`) → `refuse(reason)`; имя аргумента берётся из схемы (`reason_code` и т. п.).
3. **Иное пробуждение** (heartbeat, гейт): если есть незакрытые значимые ситуации — уведомление (инструмент
   `notify`/`notify_user`/`send_notification`, если он в каталоге, с `situation_id`; иначе текст
   `Внимание: <name> [sit:<id>]`) плюс действия из `expect.actions`; несколько ситуаций обслуживаются одним
   ответом. Иначе — «ничего не делать»: пустой ответ без вызовов.

**Число вызовов на вход.** daygen размечает каждый вход числом вызовов LLM `k` (VisionClaw: 0/1/2–3/≥4
инструмента → 1/2/3/5 вызовов). Оракул добирает ход до `k` вызовов шагами чтения перед действием. Профиль
`targeted` (по умолчанию): чтения целей хода — `expect.goal` команды (daygen: `device/property`, в том числе
для `invoke_action`), `answer.ref` вопроса о состоянии, свойства из аргументов действий; если целей меньше,
чем шагов добора, — один `list_devices` в начале (обзор, ≈ 700 токенов инвентаря) и `recent_events`
(журнал событий edgectx), затем повтор чтений целей / `recent_events`. Нет нужного инструмента — замена по
цепочке «чтение → recent_events → list_devices (≤ 1 раза)»; читать нечем вовсе — шаг выпадает (k_eff меньше).
Профиль `legacy` — прежний: чтение устройства первого действия, иначе `list_devices` на каждый шаг добора.
Ход с инструментом не короче двух вызовов, поэтому `k_eff = max(k, 2)` и E[k_eff] = 3,10 против 2,89 у
модели (см. `bench/costmodel`, `expected_k_effective`).

**Каталоги.** Имена разрешаются по каталогу запроса: точное имя; с префиксом сервера (`sim__answer`,
`sim.answer`, `sim_answer`); для explicit-каталога — `calls_explicit`/`actions_explicit` из разметки daygen,
иначе по правилу `write_property{device, property, value}` → `set_<device>_<property>{value}`,
`read_property` → `read_<device>_<property>`, `invoke_action` → `<action>_<device>`. Неразрешённые
имена отмечаются в журнале (`oracle.unresolved_tools`).

## usage и кэш

`prompt_tokens` — оценка по OpenAI Cookbook («How to count tokens with tiktoken», константы gpt-4o-mini):
3 токена на сообщение + токены полей, +3 на затравку ответа; инструменты — `num_tokens_for_tools` (вложенные
объекты — рекурсивно). Токенизатор — o200k_base (`tiktoken`; словарь берётся из локального кэша
`research/new_topic/GAP2_artifacts/.cache/tk`, проверенного по SHA-256, без сети); без `tiktoken` —
эвристика, и в журнале будет `tokenizer: "heuristic"`. Провайдер оплачивает свой отрендеренный формат, поэтому
оценку нужно откалибровать (`--calib`) по E0-P: режим record пишет рядом «оплаченные» `usage` и свою оценку.

`cached_tokens` — эмуляция префиксного кэша: последовательность токенов запроса в порядке рендеринга
(system/developer → инструменты в **исходном порядке ключей тела**, без сортировки → остальные сообщения)
сравнивается с промптами недавних запросов; попадание — общий префикс ≥ 1024 токенов, округлённый вниз до
128 и не больше `prompt_tokens`; если `prompt_tokens` < 1024 или усечённое значение < 1024 — кэша нет, запрос
не записывается и жизнь записи не продлевается. Запись живёт `--cache-ttl` секунд без обращений, попадание
продлевает её. Время — из заголовка часов запроса (см. `--clock`).

## Журнал

JSONL, строка на запрос (`type: "llm"`) или управляющее действие (`type: "control"`); `seq` монотонен. Поля
запроса: `req_id`, `ts_wall`, `t_clock`, `clock_source`, `warning?`, `mode`, `status`, `model`, `n_messages`,
`n_tools`, `usage` (как в ответе), `estimate` (оценка до калибровки, сегменты `tools/system/history/turn/priming`,
длина общего префикса, токенизатор), `bytes` (`request_body`, `request_total` с заголовками, `response_body`,
`response_total`), `latency_ms` (`total`, `injected`), `finish_reason`, `n_tool_calls`, `tool_names`,
`oracle.decision` (`summary | user_start | continue | final | situation | noop | denied_final | continue_unknown`),
`headers` (`X-Bench-*`, `X-Edgectx-*`), в record/replay — `record`/`replay` с ключом записи.

## Запись и воспроизведение

Ключ — SHA-256 канонического JSON тела без полей `user, metadata, store, prompt_cache_key,
prompt_cache_retention, service_tier, stream_options` (`--hash-ignore`). Повторы одного запроса выдаются по
порядку записи (после последнего повторяется последний) — так воспроизводятся и A/A-пары.

## Тесты

```bash
.venv/bin/python -m pytest bench/tests/test_mockllm.py bench/tests/test_mockllm_server.py -q
```
`test_mockllm.py` — токены по формулам Cookbook, правила кэша (порог 1024 по промпту провайдера, шаг 128,
TTL, продление только попаданием, порядок ключей инструментов), оракул во всех ветвях (сводка не трогает
ожидания, подсказка id сильнее текста, добор `targeted`/`legacy`, `reset`), задержка, ключи записи;
`test_mockllm_server.py` — HTTP, виртуальный TTL, контракт имён заголовков с `turn.go`, `clock_source` и
предупреждение без заголовка часов, воспроизводимость двух прогонов после `reset`, record через второй
экземпляр mock-llm → replay байт в байт, недоступный upstream, процесс CLI с SIGTERM.

## Ограничения

- Оценка токенов не равна оплаченным токенам провайдера без калибровки; картинки считаются грубо (85 токенов).
- Кэш эмулируется по нашей сериализации запроса, а не по рендерингу провайдера; маршрутизация по машинам
  и вытеснение под нагрузкой не моделируются.
- Сводка оракула — фиксированной длины (модель `summ_len`), её содержание не проверяет качество сжатия.
- Без `X-Edgectx-Input-Ids` новый ход сопоставляется по тексту реплики; ожидание без текста берётся по очереди
  (FIFO) любым следующим сообщением `user`.
- Стриминг (SSE) не поддержан — по спецификации агент работает без потока.
