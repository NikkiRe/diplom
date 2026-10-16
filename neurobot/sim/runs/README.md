# Прогоны A0/A1 на цифровом двойнике (этап Э2.5, PLAN §13.4)

Срез: 16.10.2026. Основание — `docs/neurobot/PLAN.md` §1 (K1–K9, «видимый перезапуск»), §5, §6, §13;
`ANALYSIS.md` §5.2; `research/R7_robot_slides.md` (§2.3, §3, §4.5) и `R7_robotic_platform.md` (§1, §5, §6.5, N1, N8).

Каталог сравнивает на одном двойнике две системы робота NeuroChip по одному расписанию жестов и отказов:
- **A0 — их система как на роботе:** демо `predict_live.py` лаборатории без правок пишет `/cmd_vel` прямо в
  `twist_mux`; rosbridge; PicoClaw **0.2.8** с их конфигом и их stdio-прокси `robot_vision_mcp_proxy.py`
  (с дублями инструментов, N1); вместо воркера 8766, кода которого у лаборатории нет, — честная заглушка. Процессы
  без супервизии: упавшее поднимает человек;
- **A1 — пакет 1 + безопасность v2:** `neurobot.target` из `neurobot/deploy` (safety_gate — единственный писатель
  `/cmd_vel`, исполнитель, perception v2, rosbridge с глобами, ros_mcp_server с патчем, PicoClaw 0.2.8, сторож) с
  профилем двойника `neurobot/sim/suites/twin.env`.

И ещё три прогона A1: `start_test` (100 холодных стартов и «сломанные» старты), бюджет 4 ГБ (A1 целиком в
`jetson.slice` по `neurobot/sim/budget`) и длительный прогон. Метрики — PLAN §1 (K1–K9).

Наше: файлы этого каталога. Готовое (не наше): ROS 2 Jazzy, Gazebo Harmonic, gz_ros2_control, ros2_controllers,
twist_mux, rosbridge_suite, systemd, Xvfb, PicoClaw (Sipeed), MCP SDK, PyTorch, snnTorch. Код лаборатории —
`predict_live.py`, `robot_vision_mcp_proxy.py`, `ros_mcp_server.py`, их конфиг PicoClaw и веса SNN; компоненты
пакета — двойник, deploy, safety_gate, исполнитель, perception v2, gesturegen, наблюдатель жестов, бюджет — здесь
только их обвязка.

**Честность чисел.** Стенд — Gazebo с программным рендерингом в общей ВМ на Mac, рядом идут стенды других
компонентов: задержки, TTR, CPU и память — **ориентир, не результат**; результат прогона — счётчики (касания по
истине Gazebo, глухие жесты, вмешательства человека, вызовы LLM). Геометрия робота — [оценка по фото] (R7b §2.3).
Формы жестов — гипотеза (R7b §4.5), классы снимает настоящая сеть лаборатории; точность распознавания на синтетике
ничего не доказывает. Итог — только HIL на роботе.

## 1. Схема

```
Mac: bench/mockllm (оракул, 127.0.0.1:18150, 0 ₽) ◄── HTTP host.lima.internal ──┐
ВМ sim ──────────────────────────────────────────────────────────────────────────┼────────────────────────────
 A0 (домен 61): twin.launch.py (мир hall) ── /scan /odom … ── predict_live.py как есть ─► /cmd_vel ─► twist_mux │
                 подставной pyrealsense2 + FakeYolo (таймлайн, faults.json)                    ─► diff_cont   │
                 rosbridge :9161 · PicoClaw 0.2.8 :18790 (их конфиг) ─stdio→ их прокси ─HTTP→ worker8766 ──────┘
 A1 (домен 23): neurobot.target: driver@twin (мир hall) · safety_gate · исполнитель · perception v2 ◄ relay.py ◄
                 /camera/color/image_raw (faults.json) · rosbridge :9123 · ros_mcp_server :18123 · PicoClaw :18793
 runner.py (root): якорь таймлайна → отказы (kill -9, SIGSTOP), реплики в канал pico, оператор-человек;
                 наблюдатель жестов, сэмплер памяти, истина Gazebo, safety_state → каталог прогона
```

## 2. Что одинаково у A0 и A1

### 2.1. Расписание (`schedule.py`)

`python -m neurobot.sim.runs.schedule make --profile P --out DIR [--seed 20261016]`, `… info DIR`.

| Профиль | Жестов / отрицательных | Паузы, с | Длительность | Отказы | Реплики агенту |
|---|---|---|---|---|---|
| `main` | 80 / 20 | 12–18 (≈3 жеста в минуту) | ≈33,6 мин | каждый 8-й сегмент с 4-го: 12 | каждый 8-й с 7-го: 12, из них 3 «вперёд» |
| `long` | 75 / 20 | 10–55 (1–4 в минуту) | ≈58,7 мин | каждый 6-й с 3-го: 15 | каждый 6-й с 5-го: 15 |
| `long30` | 37 / 10 | 10–55 | ≈30,7 мин | каждый 4-й с 3-го: 11 | каждый 4-й с 5-го: 11 |
| `smoke` | 10 / 2 | 12–18 | ≈4,2 мин | каждый сегмент с 1-го | каждый 3-й со 2-го |
| `start` | 24 «крючка» / 0 | 5–7 | ≈3,7 мин | — | — |

Формы — все формы gesturegen после R7 (`neurobot/sim/gestures/shapes.py`: слайд 12, GIF, R1), темп детекции
10 к/с; классы на этих жестах снимает сеть лаборатории (`run.sh shapeclass`, §6). Отказ ставится через 1 с после
последнего бокса сегмента, реплика агенту — через 2 с (тесты проверяют, что они не делят сегмент): успеет ли
компонент подняться до следующего жеста — это и есть видимость перезапуска. Цикл отказов (`FAULT_CYCLE`):
камера — нет кадров 7 с (у демо — RuntimeError через 5 с, R1), kill -9 perception, застывший кадр 6 с, kill -9
узла rosbridge, чёрные кадры 4 с, kill -9 MCP-сервера, SIGSTOP perception 8 с, kill -9 агента (PicoClaw), kill -9
исполнителя, kill -9 safety_gate. Роль сопоставляется процессу системы: у A0 perception — демо, MCP — их прокси;
исполнителя и safety_gate у A0 нет — такие отказы пропускаются с пометкой.

Выход: `timeline.jsonl`, `truth.jsonl` (gesturegen), `faults.json` (формат подставного pyrealsense2), `plan.json`.

### 2.2. Отказы камеры

У A0 их вносит подставной pyrealsense2 из `faults.json` (README gestures §5). У A1 камерой владеет
`realsense2_camera` (на двойнике — камера Gazebo), perception читает тему (`--camera ros`), поэтому отказы вносит
`relay.py` на уровне темы — так выглядит сбой узла камеры на роботе: `delay`/`stall`/`gone` — кадров нет,
`freeze` — тот же кадр с тем же `header.stamp`, `black` — нулевые пиксели, `slow` — не чаще `fps`. Расписание —
от якоря таймлайна, до якоря кадры идут без отказов.

`python3 relay.py --faults F --anchor A --out-topic T --log L [--in-topic /camera/color/image_raw]`.

### 2.3. Агентский путь и LLM

Реплика пользователя уходит в канал **pico** шлюза PicoClaw (WebSocket `/pico/ws`, Bearer — тестовый токен
прогона; канал pico включён и в конфиге робота). Перед репликой `runner.py` ставит ожидание оракулу mockllm
(`/control/expect`): «Какое сейчас состояние робота?» → инструмент состояния, «Проедь немного вперёд.» → инструмент
движения; затем «Готово.». Инструменты — как вызвала бы модель: у A0 `robot_demo_get_status` и
`robot_nudge_forward {"duration_ms": 300, "speed": 0.8}` (так модель вызвала его на роботе 29.09 — N1), у A1
`robot_get_status` и `robot_nudge {forward, 1000 мс, 0,15 м/с}`. Успех реплики — финальный ответ хода и все
инструменты без ошибки по сессии PicoClaw на диске. Вызовы LLM считаются по журналу mockllm (по `input_id`
ожидания); вызовы без реплики — K4 «вызовы LLM на жест». `pico.py` — клиент WebSocket и управление mockllm.

`worker8766.py --log FILE [--port 8766]` — заглушка воркера лаборатории для A0: API прокси (`/state`,
`/vision/*`, `/lidar/status`, `/motion/*`), журнал каждого запроса, робот не двигается (что делает настоящий
воркер с `/motion/nudge`, неизвестно).

### 2.4. Оператор (`runner.py`)

Модель человека у робота [допущение], одна для обеих систем: через `--operator-delay` (30 с) после того, как
заметно, он чинит, и каждое его действие — вмешательство человека (видимый перезапуск, PLAN §1):
- A0: демо умерло или детектор не вызывается 10 с (глухой процесс, R1) — перезапуск демо; агент не ответил или
  PicoClaw умер — перезапуск шлюза (rosbridge в A0 никто не использует — его смерть никто не замечает);
- A1: защёлка safety_gate стоит дольше задержки — `neurobot estop release` в терминале; юнит в `failed` —
  запуск;
- обе: касание без перерывов дольше 20 с (робот застрял) — перенос робота на старт (у A1 — при выключенном
  safety_gate, как `harness.relocate` наборов).

## 3. Мир `hall`

`worlds/hall.sdf` — зал 9 × 9 м: препятствия мира `lab` двойника (низкий ящик 0,15 м, стол с нависанием, стул,
стоящий человек в слепом секторе лидара, дверной проём) в тех же местах относительно робота. Комната `lab`
5 × 5 м мала для получаса манёвров демо (они уводят робота на 1–1,5 м). Следствие, которое надо помнить при
чтении A1: urg_node2 (и `sensor_model.py` двойника) отдаёт NaN и на «нет отражения» — стена дальше 5,6 м; правило
(А) safety_gate запрещает вперёд, если такой участок лежит в коридоре (лучи ближе ≈60° к курсу), поэтому в зале
A1 часто стоит перед дальними стенами (`stopped_by_safety`, причина `lidar_nodata`).

## 4. Запуск

С Mac из корня проекта; ВМ sim запущена. Всё в ВМ — в `/var/tmp/nb-runs`, код — снимок `src/` (sha256 —
`snapshot.sha256`), прогоны идут юнитами `runs-<метка>` и переживают обрыв `limactl`.

```sh
neurobot/sim/runs/run.sh snapshot                    # снимок кода в ВМ, venv mcp для прокси A0
neurobot/sim/runs/run.sh mockllm start DIR           # mockllm на Mac (127.0.0.1:18150), журнал DIR/mockllm_all.jsonl
neurobot/sim/runs/run.sh sched main                  # и long30, start
neurobot/sim/runs/run.sh shapeclass main             # «форма → класс» сетью лаборатории
neurobot/sim/runs/run.sh a1-install                  # пакет на двойнике: setup_twin.sh + правки прогона
neurobot/sim/runs/run.sh run a0 main_a0 main         # A0 и A1 — параллельно (разные домены, порты, двойники)
neurobot/sim/runs/run.sh run a1 main_a1 main
neurobot/sim/runs/run.sh wait main_a1
neurobot/sim/runs/run.sh start-test 100              # K9
neurobot/sim/runs/run.sh budget budget 2406          # A1 в jetson.slice, MemoryMax 2406 МиБ (ветка GNOME)
neurobot/sim/runs/run.sh fetch main_a1 experiments/results/neurobot_20261016_a1/vm
python -m neurobot.sim.runs.metrics DIR --out DIR/metrics.json
python -m neurobot.sim.runs.compare --a0 … --a1 … [--start …] [--budget …] [--long …] [--shapeclass …] --out compare.md
python -m neurobot.sim.runs.manifest --dir DIR --kind a0|a1|start|budget|long --versions … --snapshot …
```

| Команда / модуль | Флаги (по умолчанию) | Что делает |
|---|---|---|
| `run.sh` | `RUNS_MOCK_PORT` (18150), `RUNS_VENV_MCP` (venv mcp 1.27.2 бюджета), `RUNS_ML_PYTHON` (`~/nb/venv`) | команды выше; `versions DEST` — версии ВМ; `fetch` не копирует тестовые токены pico |
| `runner.py run` | `--system a0\|a1`, `--sched`, `--out`, `--mockllm` (обяз.); `--label` (main), `--operator-delay` (30), `--tail-s` (40), `--collect-interval` (10), `--ml-python`, `--picoclaw` (`/var/tmp/nb-pc028/picoclaw`), `--mcp-venv` (`/var/tmp/nb-runs/mcpvenv`), `--keep` | прогон (§2); SIGTERM — прерывание с записью того, что успели, система останавливается |
| `runner.py a1-install`, `a1-teardown` | `--mockllm` | пакет на двойнике и его снятие |
| `budget_a1.py` | `--out`, `--mem`, `--sched`, `--mockllm` (обяз.); `--quota` (106), `--ratio` (0,265), `--cpus` (4), `--label` (budget) | §7 |
| `start_gesture.py prepare\|check` | `--sched`; у `check` — `--log`, `--window` (8) | §6 |
| `metrics.py` | `RUN_DIR`, `--truth`, `--out` | §5 |
| `compare.py`, `manifest.py` | см. `--help` | сводка и манифест каталога результатов |

## 5. Метрики (`metrics.py`)

| Метрика | Откуда | Как |
|---|---|---|
| K1, K2 | наблюдатель жестов (`summary.json`) | t0 (истина gesturegen) → первая ненулевая `/cmd_vel` и → движение; жесты при RTF < 0,95 не входят |
| реакции | наблюдатель | верная / неверная (класс сети — распознавание) / нет / оборвана / не наблюдалась; у A1 — сколько из «нет» и «оборвана» пришлось на `blocked`/`estop` safety_gate (`stopped_by_safety`, причины) |
| K3: глухие жесты | `probe.jsonl` (вызовы детектора FakeYolo у обеих систем) | жест, во время которого детектор отработал меньше половины кадров: процесс умер, завис, поток камеры умер, кадров нет |
| K3: перезапуски | `run.json` | видимый (PLAN §1), если понадобился человек, компонент не поднялся сам, у пути жеста — потерян жест (глухой или без реакции) или TTR дольше паузы до следующего жеста, у агентского пути — реплика без ответа; иначе невидимый |
| K4 | журнал mockllm | вызовы LLM без реплики (на жест) — 0; на реплику, токенов на вызов, инструментов в каталоге |
| K5 | `run.json` | TTR — от отказа до «роль снова работает»: perception — детектор снова вызывается после конца отказа; остальное — новый процесс жив (порт, MCP, WebSocket rosbridge отвечают) |
| K6 | наблюдатель | «едет без свежей команды > 0,5 с» |
| K7 | наблюдатель | ложные действия на отрицательных сегментах, движение по «не распознано», самопроизвольные |
| касания | `truth.jsonl` (истина Gazebo) | эпизоды (перерыв < 1 с — один эпизод), модели, наименьший зазор |
| K8 | `collect.jsonl` (`neurobot/sim/budget/collect.py`) | PSS по компонентам в начале и в конце, наклон МНК (без первых 2 мин); дрейф K1 — первая и последняя треть |

## 6. start_test (K9)

`run.sh start-test N`: `start_gesture.py prepare` (таймлайн `start`, узел кадров камеры без отказов), затем
`neurobot start-test --cycles N` пакета deploy (холодный старт → doctor «ОК» → жест → стоп → явный ноль) и
сценарии «сломанного старта». Жест цикла — `start_gesture.py check`: ближайший «крючок» слайда 12, начало которого
впереди (сеть лаборатории на полилинии отвечает на него классом 0 — «полный оборот» на месте, `shapeclass` start),
успех — заявка исполнителя и движение по `/odom` или законный стоп safety_gate; якорь удаляется, и следующий
холодный старт начинает таймлайн заново.

## 7. Бюджет 4 ГБ (`budget_a1.py`)

A1 целиком в `jetson.slice`: drop-in `Slice=jetson.slice` для юнитов пакета (в `/run`), свойства slice
`budget/stack.slice_props` (MemoryMax, MemorySwapMax=0, CPUQuota, AllowedCPUs), балласт CUDA 1024 МиБ в процессе
perception (`budget/ballast.py --run`), заместители драйверов (база ros2_control с mock_components,
realsense2_camera и urg_node без устройств, балласт потоков камеры) и перенос узлов двойника — `budget/stack.py`.
Обычный прогон `runner.py` по расписанию; в середине — уникальные страницы slice, в конце — `budget` в `run.json`:
память по компонентам, запас против веток модели бюджета, `oom_kill` slice. Всё своё снимается в конце.

## 8. Тесты

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider neurobot/sim/runs/tests   # Mac 3.12
uv run --python 3.8 --no-project --with 'numpy<1.25' --with pytest python -m pytest -q -p no:cacheprovider \
    neurobot/sim/runs/tests                                                                            # Python 3.8
```

| Файл | Что проверяет |
|---|---|
| `test_runs_schedule.py` | места событий (после последнего бокса, не последний сегмент), цикл отказов, «вперёд» на номерах, отказы и реплики не делят сегмент ни в одном профиле, `faults.json` принимает подставной pyrealsense2, детерминизм, отказ перезаписи и неизвестного профиля |
| `test_runs_pico.py` | кадры WebSocket всех длин и маска, неполный кадр, финальный ответ; клиент против подставного шлюза: Bearer, ping → pong, ход из двух сообщений, отказ по токену; управление mockllm |
| `test_runs_parts.py` | заглушка 8766 (API, журнал, робот не двигается); узел отказов: до якоря без отказов, delay, freeze, black, slow, журнал; журнал probe (прогресс после момента, новый процесс, недописанная строка); исходы инструментов по сессии PicoClaw; конфиг PicoClaw A0 (меняются только LLM, VK и пути) и A1; `.security.yml`; правки env-файла A1 повторяемы и трогают только свои ключи; обёртка балласта; демо A0 (compile скрыт, weights_only, FakeYolo); выбор жеста start_test |
| `test_runs_metrics.py` | глухие жесты по probe и рестарт процесса; касания с дребезгом; видимость перезапуска путей жеста и агента; агент и LLM; память и дрейф; остановки защитой; сборка метрик и сводки на синтетическом каталоге |
