# Реплеер дня (T0)

Проигрывает файл дня (`bench/daygen`, спецификация §2) в агенте `edgectx`, эмуляторе `sim-mcp` и
`mock-llm` по виртуальным часам, останавливает агент и собирает артефакты прогона в `<out>/<run_id>/` с
`manifest.json`. Спецификация — `IMPLEMENTATION_SPEC.md` §5.1. Только стандартная библиотека Python.

Числа, полученные при разработке, — **не результаты** (в `manifest.json` стоит `"dev_run": true`).

## Как запускать

Из корня репозитория. Порты стенда: edgectx 18080–18089, sim-mcp 18090–18099, mock-llm 18100–18109; занятость
проверять `lsof -nP -iTCP:18080-18109 -sTCP:LISTEN`, свои процессы останавливать по PID.

```bash
# 1. сервисы (каждый — свой процесс; PID запомнить)
.venv/bin/python -m bench.mockllm --mode oracle --port 18100 --journal run/mockllm_journal.jsonl &
.venv/bin/python -m bench.simmcp --day data/days/mplus_s3.jsonl --port 18090 --journal run/sim_journal.jsonl &
agent/bin/edgectx-darwin-arm64 -config run/t0.yaml -run-id mplus-s3 &      # llm.trace_headers: true, clock: virtual
sysmon/bin/sysmon-darwin-arm64 --pid <PID edgectx> --interval 1s --out run/sysmon.jsonl &   # необязательно

# 2. проигрывание дня: в конце реплеер сам вызывает POST edgectx /v1/shutdown и ждёт run_end в трассе
.venv/bin/python -m bench.replay --day data/days/mplus_s3.jsonl \
    --edgectx http://127.0.0.1:18080 --sim http://127.0.0.1:18090 --mock http://127.0.0.1:18100 \
    --out experiments/results --run-id mplus-s3 \
    --collect edgectx_trace=run/edgectx.trace.jsonl --collect sysmon=run/sysmon.jsonl \
    --config edgectx=run/t0.yaml --binary edgectx=agent/bin/edgectx-darwin-arm64

# 3. метрики и проверка трассы монитором
.venv/bin/python -m bench.metrics experiments/results/mplus-s3
.venv/bin/python -m monitor.check_trace --trace experiments/results/mplus-s3/edgectx_trace.jsonl

# 4. остановить свои mock-llm, sim-mcp и sysmon по PID (edgectx уже остановлен реплеером)
kill -TERM <pid mock-llm> <pid sim-mcp> <pid sysmon>
```

| Флаг | По умолчанию | Смысл |
|---|---|---|
| `--day`, `--out` | — | файл дня; корень результатов (прогон — `<out>/<run_id>/`) |
| `--edgectx`, `--sim`, `--mock` | — | URL сервисов; без `--sim`/`--mock` соответствующие вызовы пропускаются |
| `--run-id` | `<имя дня>-<дата-время>` | id прогона; непустой каталог — ошибка, если нет `--force` |
| `--max-step` | 60 | максимальный шаг виртуальных часов, с |
| `--sync` | auto | синхронизация с уведомлениями sim-mcp (см. ниже): `auto` \| `inputs` \| `updates` \| `none` |
| `--sync-timeout` | 2 | предел ожидания синхронизации, с (`timeout_ms` в `/v1/clock/advance`) |
| `--settle-ms`, `--settle-timeout` | 0, 30 | ждать N мс «тишины» mock-llm после шагов с таймерами/уведомлениями (для агентов без заголовков часов) |
| `--no-shutdown`, `--shutdown-timeout` | выкл., 30 | не останавливать edgectx в конце / сколько ждать `run_end` в трассе, с |
| `--user-timeout`, `--http-timeout` | 120, 30 | таймауты ответа на реплику и прочих вызовов, с |
| `--sensor-mode` | sim | `sim` — события датчиков публикует sim-mcp по часам; `post` — ещё и `POST edgectx /v1/event`; `none` |
| `--no-expire` | выкл. | не снимать ожидание ситуации в mock-llm по дедлайну |
| `--no-reset-mock` | выкл. | не сбрасывать mock-llm в начале (журнал всё равно забирается с текущего `journal_seq`) |
| `--window-end` | конец окна | остановить проигрывание раньше (с от полуночи) |
| `--collect L=PATH` | — | скопировать файл в каталог прогона как `L.<расширение>` (повторяемый); метка `edgectx*trace*` — трасса, в которой ждётся `run_end` |
| `--config L=PATH`, `--binary L=PATH` | — | конфиг целиком / SHA-256 бинарника в manifest |
| `--dry-run` | выкл. | без сети: только план шагов и manifest |

Коды выхода: 0 — прогон завершён (сбои отдельных реплик — в `manifest.errors`), 1 — прерван (сервис
недоступен, HTTP-ошибка обязательного вызова), 2 — неверные аргументы.

## Протокол

События дня обрабатываются по времени; при равном `t` ситуации идут раньше датчиков (см. ниже).

| Событие | Действия реплеера |
|---|---|
| `user_cmd`, `question` | продвинуть часы до `t` → синхронизация с уведомлениями последнего шага → `POST mock /control/expect {input_id, kind, text, t, expect, k?}` (у вопроса `expect.type = answer`, `answer` из события) → `POST sim /control/input {id}` → `POST edgectx /v1/user {id, text}` и ждать ответа. Ожидание регистрируется **сразу перед репликой**: фоновые пробуждения окна продвижения не могут его перехватить |
| `situation` | продвинуть часы до `t − ε` (ε = 1 мс) → зарегистрировать ожидание всех ситуаций момента `t` (`situation: {id, name, significant, deadline_s, t}`) → продвинуть до `t`; агенту ничего не подаётся. В момент `t + deadline_s` — `POST mock /control/expire {input_id}` |
| `sensor` | продвинуть часы до `t` (sim-mcp сам публикует события с `t ≤ to_t` как `notifications/resources/updated`) |

Шаг часов (не длиннее `--max-step`, обязательно с точками дедлайнов) — три вызова в таком порядке:
`POST mock /control/clock {t}` → `POST edgectx /v1/clock/advance {to_t[, min_inputs, timeout_ms]}` →
`POST sim /control/clock {t}`. Сначала агент доходит до `t` (таймеры, heartbeat; edgectx сам дожидается
окончания своих ходов внутри `/v1/clock/advance`), потом sim-mcp публикует датчики момента `t`. Время
запроса к LLM mock-llm берёт из заголовка `X-Edgectx-Clock-T`; `/control/clock` — запасной вариант.

**Синхронизация с уведомлениями.** Уведомления sim-mcp идут агенту асинхронно (SSE); ответ
`/control/clock` содержит `notified` — сколько уведомлений разослано. Следующий шаг не начинается, пока агент
их не принял:
- `inputs` (контракт `agent/README`): следующий `/v1/clock/advance` несёт `min_inputs: {"sensor": S + notified}`,
  где `S` — `inputs.sensor` из ответа предыдущего advance, и `timeout_ms`; промах — `missing_inputs` в ответе;
- `updates`: до следующего advance реплеер опрашивает `GET edgectx /v1/metrics` и ждёт, пока `mcp.updates`
  (принятые уведомления, точный счётчик) не догонит сумму `notified`; цена — HTTP-запрос к агенту на каждый
  шаг с уведомлениями (на дне M+ CPU edgectx ≈ +0,3–0,5 с из ≈ 1,6–2 с);
- `auto` (по умолчанию): `inputs`; после 3 промахов (вход не засчитывается, если значение не изменилось,
  ресурс не разобран и т. п.) — `updates` до конца прогона; `none` — без синхронизации.
Перед репликой то же самое делается отдельно (в `inputs` — advance на текущее время с `min_inputs`).

В `updates` накопительный target `mcp.updates` отсчитывается от начала прогона: агент должен явно отдавать
счётчик, включая начальный ноль. Таймаут или исчезновение счётчика прерывает прогон; target не сбрасывается.
В `auto` каждый промах `min_inputs` также подтверждается по `updates`, после трёх промахов этот режим
становится постоянным. Перед истечением ситуации и перед `finish` дополнительный advance на текущее t
ждёт окончания обработки: принятие уведомления само по себе ещё не означает `idle`.

Фиксированного ожидания «тишины» нет (`--settle-ms 0`): ходы агента ждёт сам edgectx. `--settle-ms N > 0`
нужен только для агента без `X-Edgectx-Clock-T`: тогда реплеер ждёт тишины mock-llm после шагов, где
сработали таймеры агента или sim-mcp разослал уведомления, и после реплик.

Почему ситуация регистрируется в `t − ε`: если ситуация возникает в момент датчика (протечка, перегрев),
пробуждение гейта на этом датчике должно её «видеть»; а пробуждения раньше `t` не должны засчитываться как
реакция. В конце — продвижение до конца окна (или последнего события, если оно позже), синхронизация,
`POST sim /control/finish` (оценка sim-mcp по истинному состоянию), `GET edgectx /v1/metrics`,
`POST edgectx /v1/shutdown` и ожидание `run_end` в трассе (без него монитор трассу не принимает), сбор.

## Каталог прогона

| Файл | Откуда |
|---|---|
| `day.jsonl` | копия файла дня |
| `replay.jsonl` | журнал реплеера: `start`, `advance` (`to_t`, `notified`, `timers_fired`), `expect`, `user` (`rtt_ms`, ответ), `expire`, `sync_missing`/`sync_timeout`/`sync_fallback`, `error`, `end`; по `advance` (ts_wall → t) метрики переводят стенное время sysmon в виртуальное |
| `mockllm_journal.jsonl`, `mockllm_state.json` | `GET mock /control/journal?since=<journal_seq после reset>` (только этот прогон), `/control/state` |
| `simmcp_report.json` | ответ `POST sim /control/finish` |
| `edgectx_metrics.json` | `GET edgectx /v1/metrics` до остановки агента |
| `<метка>.<ext>` | файлы `--collect` (трасса edgectx — с `run_end`, трассы hw-/sim-mcp, sysmon, отчёт монитора) |
| `manifest.json` | `status` (`completed`, `completed_with_errors`, `aborted`), `completed`, запрошенное и проигранное окна (`requested_window_s`, `played_window_s`), id, время, параметры реплеера (`sync`, `settle_ms`, `shutdown`…), статистика (`sync`: ожидания, промахи, таймауты, опросы метрик), `edgectx_shutdown` (`status`, `run_end`, `waited_s`), заголовок дня (seed, профиль, дом, split), SHA-256 дня, конфиги целиком, SHA-256 бинарников и всех файлов, версии, git-коммит (если есть), `errors`, `warnings` |

При обязательной HTTP-ошибке или незавершённом advance (`idle: false`) сохраняются доступные артефакты
и manifest со статусом `aborted`. Ошибка HTTP реплики записывается как `user.ok: false` и `user_errors`,
а не успешный ответ. Наличие каталога прогона ещё не доказывает его завершение.

## Двойники для проверки без Go-агента

`bench/replay/fakes.py`: `FakeAgent` (HTTP-API edgectx с `/v1/shutdown`, heartbeat + гейт по устройствам,
generic-каталог, трасса формата edgectx с `wake_id`, `purpose`, `segments` в символах и
`segments_tokens_est`, заголовки `X-Edgectx-*` как в `turn.go`) и `FakeSim` (публикует датчики дня в агент
через `/v1/event`). Это тестовые двойники, а не модель edgectx:

```bash
.venv/bin/python -m bench.mockllm --port 18100 &
.venv/bin/python -m bench.replay.fakes agent --llm http://127.0.0.1:18100 --trace /tmp/trace.jsonl --port 18080 &
.venv/bin/python -m bench.replay.fakes sim --day day.jsonl --agent http://127.0.0.1:18080 --port 18090 &
.venv/bin/python -m bench.replay --day day.jsonl --edgectx http://127.0.0.1:18080 --sim http://127.0.0.1:18090 \
    --mock http://127.0.0.1:18100 --out /tmp/results --collect edgectx_trace=/tmp/trace.jsonl
```

## Тесты

```bash
.venv/bin/python -m pytest bench/tests/test_replay.py bench/tests/test_replay_e2e.py bench/tests/test_replay_simmcp.py -q
```
`test_replay.py` — разбор дня, порядок вызовов и размер шагов на записывающих двойниках, ожидание реплики
после продвижения часов и перед `/v1/user`, ситуации в `t − ε` и дедлайны, `min_inputs` по `notified`,
переход `auto → updates` после промахов, manifest и SHA-256, режим `post`, недоступный сервис, CLI;
`test_replay_e2e.py` — сквозной прогон mock-llm + двойники → метрики с заранее известным ответом, остановка
агента и `run_end` в трассе, второй прогон на том же mock-llm даёт тот же журнал (A/A);
`test_replay_simmcp.py` — контракт с настоящим sim-mcp на дне daygen (пропускается, если компонента нет).

## Ограничения

- `inputs` требует роста входов `sensor`; промах в явном режиме прерывает прогон. Для уведомления об
  неизменившемся свойстве используйте `updates` или `auto` с поддерживаемым счётчиком `mcp.updates`.
- В T0 день проигрывается за секунды; чтобы sysmon успел снять ряд памяти по виртуальному времени,
  нужен шаг sysmon 1 с и/или замедление (`--settle-ms 25` даёт ≈ 2 мин на день M+).
