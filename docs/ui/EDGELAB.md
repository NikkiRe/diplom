# EdgeLab — лаборатория решений EdgeCtx

## Backend и протокол

`neurobot.ui.lab` — стандартная библиотека Python 3.8. `LabStore` хранит импортированные трассы,
нормализованные зависимости, задания и результаты локальных экспериментов. Сетевые маршруты
подключаются сервером UI; backend лаборатории не принимает серверные пути, адреса провайдеров,
произвольные команды или адреса оборудования из HTTP-запроса.

```python
from neurobot.ui.lab import LabStore, mutate

store = LabStore(
    allow_experiments=True,
    python="/absolute/path/to/project/.venv/bin/python",  # trusted startup configuration
    binary="/absolute/path/to/edgectx",                  # optional, trusted startup configuration
)
document = store.snapshot()
run = store.run("run-id")              # dict or None
original_jsonl = store.export("run-id")  # bytes or None
code, result = mutate(store, "/api/lab/import", {"name": "День 1", "content": jsonl_text})
store.close()  # cancels/reaps the owned runner process group and releases its temporary workspace
```

Без `python` используется `EDGELAB_PYTHON`, затем текущий интерпретатор. Импорт работает на Python 3.8;
для исполнения имеющегося `bench.simmcp` нужен Python ≥3.10 с зависимостями проекта. Недостающие
зависимости приводят к явному `failed`, автоматической установки нет. Если бинарник не задан,
runner собирает **текущий исходный Go-агент** с `GOPROXY=off`, `GOSUMDB=off`, `GOTOOLCHAIN=local`
в собственный временный каталог с доступным для записи Go cache. Сборка кэшируется по хешу исходников.

Для шлюза на слабой плате сервер создаёт `LabStore(allow_experiments=False)`: импорт и анализ доступны,
новые эксперименты возвращают HTTP 403. Конструктор `demo=True` добавляет только небольшой пример,
явно обозначенный `SYNTHETIC DEMONSTRATION`; это не экспериментальный результат. Рабочий стартовый
набор получают исполнением настоящей пары экспериментов через `store.start()`.

`GET /api/lab` возвращает:

```json
{"runs": ["summary objects"], "jobs": ["job objects"], "scenarios": ["scenario descriptions"], "limits": {}}
```

Summary содержит `id`, `label`, `kind` (`trace`/`experiment`/`live`), `origin`, `clock`, `t_start`, `t_end`,
`metrics`, `status`. Полный `store.run(id)` дополнительно содержит `events`, `config`, `observations`,
`contexts`, `provenance`, а эксперимент — `scenario`, `scenario_truth`, `evaluation`, `resources`.
`store.run()` и `snapshot()` возвращают копии, внешнее изменение ответа не меняет хранилище.

`POST /api/lab/import` принимает ровно один вариант:

```json
{"name": "Трасса EdgeCtx", "content": "{\"type\":\"run_start\",...}\n..."}
```

```json
{"name": "Трасса EdgeCtx", "events": [{"type": "run_start", "clock": "virtual", "ts_clock": 0}]}
```

Успех: HTTP 201 `{"run": summary}`. JSONL сохраняется для экспорта побайтно; импорт массива
сериализуется в JSONL с сохранением всех исходных полей и идентификаторов. Несколько разных `run_id`
в одном импорте запрещены. Неизвестные типы событий и vendor-поля сохраняются. Повреждённая запись,
нечисловые значения часов, NaN/Infinity, переполнение глубины/размера не превращаются в выдуманные
показания; нечисловое время остаётся `null`, некорректный JSON отклоняется целиком.

Нормализованное событие получает стабильный в пределах прогона `id: e000001`, `ordinal` и `t`.
Оригинальный `id` сохранён как `record_id`; у входа — также `input_id`. Оригиналы доступны через
`store.export(id)`. Для virtual `t` берётся из `ts_clock`, иначе явного `t`; для real/wall — из
Unix `ts_wall`, иначе явного `t`. Домены не смешиваются: real `ts_clock` может быть секундами от эпохи
дня и не является Unix timestamp. При неизвестном clock выбирается явное `t`, иначе `ts_wall`,
`clock_known: false`. При отсутствии подходящего времени — `null`. Порядок записей не меняется.

`event.links` содержит только ссылки на предшествующие записи по явным `input_id/input_ids`,
`wake_id`, `req_id`, `call_id`. Повторные/неоднозначные идентификаторы не образуют зависимостей,
пишется предупреждение provenance. Близость временных отметок сама по себе связь не создаёт.
Это граф записанных зависимостей, а не доказательство физической причинности.

`observations` содержит принятые агентом sensor inputs:

```json
{"event_id":"e000002","t":1,"device":"thermo_lab","property":"temperature","value":23,"unit":null,"origin":"agent_input"}
```

Начальное оборудование могло быть прочитано до sensor inputs, поэтому такой ряд не является полным
сырым состоянием аппаратуры. Пропущенные значения/единицы не восстанавливаются предположениями.
В экспериментах независимый заданный мир находится в `scenario_truth.header.initial_state` и
`scenario_truth.events`; единицы/схемы — в `scenario_truth.header.devices[].properties`.
Фактическое конечное состояние sim-mcp прочитано отдельно в `scenario_truth.final_state` (`t`, `state`),
включая изменения виртуальных приводов. Заданные входы не являются полной историей изменений приводов.

`contexts` содержит указатели, а не квадратично повторённые копии состояния:

```json
{"event_id":"e000004","req_id":"r-1","t":3,"observed_through":1,
 "prompt":{"available":false,"snapshot_event_id":null,"reason":"Полный контекст этого запроса не записан"}}
```

`observed_through: N` означает первые N записей `observations` в порядке трассы. Точный контекст
доступен только при записанном на соответствующем `llm_request` поле `context_snapshot` с
`complete: true` и `messages`. При превышении лимита capture или обычной старой трассе UI показывает
недоступность, а не реконструирует промпт из текущей базы. Скрытые рассуждения LLM не доступны.

## Исполняемые локальные эксперименты

```json
{"scenario":"noise-burst","policy":"gate","threshold":30,"debounce_s":2,
 "heartbeat_s":30,"duration_s":180,"seed":7,"history":"mask","window_turns":8,"label":"Гейт · seed 7"}
```

`POST /api/lab/experiments` возвращает HTTP 202 `{"job": {...}}`. Доступны сценарии `noise-burst`,
`overheat`, `leak-recovery`; политики `gate`, `heartbeat`, `hybrid`. Пределы: длительность 60–600
виртуальных секунд, heartbeat 5–300 с, debounce 0–10 с, порог 20–100 °C, seed 0–2147483647.
История: `full`, `window`, `mask` (по умолчанию), `summary_system`; `window_turns` — строго целое 1–32
(по умолчанию 8), размеры batch и mask_keep не превышают выбранное окно.
Порог относится к комнатному датчику; фиксированный аварийный порог котла — 85 °C. Входы и seed
сценария не зависят от политики, поэтому пары имеют одинаковый `day_sha256`.

Задание проходит `queued → running → completed/failed/cancelled`; `phase` уточняет сборку, запуск
симулятора/агента, исполнение и сбор доказательств. `run_id` появляется только после успешного
завершения replay с `run_start/run_end`. Ошибка запуска, таймаут, отмена и неполные артефакты не
создают успешный прогон. `POST /api/lab/cancel {"id":"job-id"}` переводит активное задание в
`cancelling`, затем `cancelled`. Отмена и `close()` завершают только собственную группу процессов.

Runner исполняет настоящий Go EdgeCtx + `bench.simmcp` + `bench.mockllm` в oracle mode + `bench.replay`.
Все адреса сгенерированы сервером и относятся к `127.0.0.1`; MCP-приводы меняют только виртуальный мир.
LLM upstream отсутствует, словарь токенизатора не загружается: явно выбран `heuristic`. В конфигурации
агента включён ограниченный `trace_context`, поэтому prompt capture относится к реальным запросам этого
локального прогона. Оценку ситуаций/вопросов выполняет существующий `bench.metrics`, а не UI-эвристика.

Provenance содержит хеши дня, бинарника, исходников и исходной трассы, manifest replay, seed,
`execution: actual_go_agent_local_oracle`, `dev_run: true`, `external_llm: false`,
`physical_equipment: false`, `real_money_spent: 0`. Эти результаты не являются проверкой качества
реальной модели, физического устройства или конечными результатами диплома. Повтор запускается с
начала сценария; исполняемого checkpoint/fork в произвольный момент пока нет.

Метрики — число **записанных** входов/подавлений/пробуждений/запросов/инструментов, usage, группы
по источнику пробуждения и задержки HTTP. Неизвестные tokens/cost/energy — `null`.
`usage_complete` отличает полную сумму usage от известной части; `maybe_billed_calls` сохраняет
неопределённость оплаты. Для эксперимента `tokens_basis: mock_usage_estimate`; `cost_rub` остаётся
`null` (тариф не рассчитывался), реальных расходов на модель нет. Cached tokens являются подмножеством
prompt tokens и повторно к ним не прибавляются.

`resources` — выборочные фактические счётчики **PID агента на текущем хосте**: Linux `/proc` или
`ps`, интервал 0,2 с, `samples[{wall_s,ts_wall,cpu_s,rss_bytes}]`, `rss_sampled_max_bytes`,
`cpu_observed_s`, `observed_wall_s`, `source`, `host`, `diagnostic_only: true`. Если счётчик недоступен,
значение остаётся `null`. Выборочный максимум RSS не является полным пиком памяти; CPU измерен между
реально полученными отсчётами. Эти числа нельзя масштабировать на виртуальные сутки или объявлять
измерением Jetson. `evaluation.metric_validity` также маркирует ограничения виртуального T0.

## Ограничения хранения и проверка

Импорт — до 8 MiB / 12 000 событий / глубины 24 / 250 000 JSON-узлов. Хранилище — до 16 прогонов и
32 MiB суммарно, включая оригиналы экспорта. Одновременно одно задание, до 32 заданий за сессию;
занятость возвращает 409. Задание ограничено 180 с реального времени, артефакты — 24 MiB, результат —
8 MiB. Превышение явно завершает задание/импорт ошибкой, автоматического усечения нет.
Хранилище временное; экспорт нужен для сохранения пользовательских трасс.

```bash
python3 -m unittest neurobot.ui.tests.test_ui_lab -v

EDGELAB_TEST_BIN=/absolute/path/to/edgectx \
EDGELAB_PYTHON=/absolute/path/to/project/.venv/bin/python \
/absolute/path/to/project/.venv/bin/python -m unittest neurobot.ui.tests.test_ui_lab -v
```

Вторая команда дополнительно выполняет фактическую пару gate/heartbeat на одном seeded дне и
проверяет успешную запись виртуального клапана. Проверки покрывают исходные ID и lossless export,
неоднозначные связи, недоступный контекст, неизвестный usage, лимиты, ошибку запуска, запрет
экспериментов на шлюзе, отмену и завершение собственных дочерних процессов.

## Явный read-only адаптер камеры

`neurobot.ui.cam_snapshot.snapshot(registry, source_id)` получает JPEG только по явному запросу
пользователя от уже подключённого loopback-источника. HTTP: `POST /api/catalog/frame {"source_id":"…"}`.
Адаптер открывает отдельную MCP-сессию, проверяет `serverInfo.name: cam-mcp`, наличие и содержимое
`cam://inventory`, модель камеры проекта и точную ограниченную схему read-only `snapshot`.
Одно совпадение имени инструмента или `readOnlyHint` не разрешает произвольный вызов. Разрешён ровно
`snapshot` с `max_width ≤ 320`, `quality ≤ 60`; общая allowlist каталогового клиента не расширяется.
`robot.Look`, `set_detector` и инструменты приводов не вызываются. Сессия закрывается собственным DELETE.

Ответ: `{mimeType: "image/jpeg", data: base64, metadata, observed_at, origin, source}`.
Метаданные содержат исходный результат snapshot, сведения камеры/устройства, `actual_width`,
`actual_height`, `decoded_bytes`, `source_id`. `source` равен фактическому типу источника cam-mcp:
`synthetic`, `dir` или `v4l2`; синтетический кадр должен сохранять эту маркировку в UI.
JPEG ограничен 512 KiB и 320 × 1280 px, MCP-ответ — 768 KiB, метаданные — 32 KiB; запрос имеет
таймаут 3 с. Одновременно один запрос на источник, не чаще 1 Гц (409/429). Перенаправления запрещены.

```bash
EDGELAB_TEST_CAM_BIN=/absolute/path/to/cam-mcp \
python3 -m unittest neurobot.ui.tests.test_ui_cam_snapshot -v
```

Проверяются JSON/SSE, частный сеанс, ограничения размера, отказ неизвестному серверу/схеме,
запрет произвольного URL и фактический JPEG от нативного cam-mcp в synthetic-режиме.

## Наблюдение действующей трассы

Trusted startup hook `store.watch_trace(path, label=None)` наблюдает локальный JSONL-файл раз в секунду.
CLI сервера принимает повторяемый `--trace /path/to/edgectx_trace.jsonl`. HTTP API по-прежнему не
принимает пути. Разрешены только обычные файлы; неготовый/ещё не созданный файл даёт `waiting` без
выдуманных данных. Одновременно до четырёх watcher; прежние 8 MiB / 12 000 событий / 32 MiB хранения
применяются и к live-трассам. Подготовка файла и его обновление выполняются самим действующим агентом.

`snapshot().watchers` содержит `{id,label,status,error,run_id,revision,last_success}`. Статусы:
`waiting`, `watching`, `completed`, `error`. Полные пути в API не раскрываются: provenance содержит
только basename. Прогон имеет `kind: live`, `status: live` до завершённого `run_end`; `id` остаётся
одинаковым при добавлении записей, `revision` увеличивается только при изменении полного JSONL-префикса.
Summary также включает `revision`; UI может обновлять один прогон, сохраняя выбранный момент просмотра.

Незавершённая последняя строка без `\n` ожидается до окончания записи. Любая повреждённая **полная**
строка вызывает явную ошибку: watcher сохраняет последнюю принятую версию, маркирует её `stale` и
публикует `error`. Ни запись, ни превышение лимита не пропускаются молча. Новая версия заменяет старую
атомарно с учётом общей квоты; оригинал JSONL для экспорта обновляется одновременно с нормализацией.

Замена inode, усечение файла или изменение уже прочитанного префикса создают новый прогон; предыдущие
данные сохраняются отдельно, активный старый прогон становится `partial`. Несколько `run_start` в одном
неразделённом файле дают ошибку вместо слияния прогонов. После исчезновения файла watcher ждёт новый
источник. `close()` останавливает и присоединяет все watcher-потоки; пользовательские файлы не удаляются.

```bash
python3 -m unittest neurobot.ui.tests.test_ui_lab_watch -v
```

Девять проверок покрывают создание файла позже старта, частичную строку, стабильные ID/revision,
реальный фоновый poll и close, повреждённые строки, ротацию/усечение, квоты и запрет HTTP-путей.
