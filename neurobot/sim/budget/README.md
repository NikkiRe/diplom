# Бюджет Jetson Nano 4 ГБ на стенде (этап Э2.5, PLAN §13.2)

Срез: 16.10.2026. Основание — `docs/neurobot/PLAN.md` §13.2 (а также §2, §5), `docs/neurobot/ANALYSIS.md` §5.2,
`docs/neurobot/research/R7_robotic_platform.md` (R7a: §1.1, §1.5, §5, N7) и `R7_robot_slides.md` (R7b: слайды 2–5).

Компонент отвечает на вопрос студента «у Jetson 4 ГБ — учитывай»:
1. **модель бюджета** (`model.json`, `model.py`): сколько из 4 ГБ общей памяти CPU+GPU остаётся стеку робота при
   JetPack 4.6 и Ubuntu 20.04 — без рабочего стола и с ним; каждое слагаемое со ссылкой или пометкой;
2. **запуск** (`stack.py`, `budget.sh`): всё, что на роботе работает на Jetson, кроме драйверов, которые заменяет
   Gazebo, идёт в ВМ sim в cgroup `jetson.slice` с MemoryMax по модели и квотой CPU, приближающей 4 ядра Cortex-A57;
   недостающие части — заместители и балласт с честной пометкой;
3. **отчёт** (`collect.py`, `health.py`, `report.py`): PSS и RSS по компонентам, сумма против бюджета и запас,
   лестница MemoryMax 4 → 3 → 2,4 → 2 → 1,75 → 1,63 → 1,5 ГиБ (что ломается первым), CPU по компонентам при
   жестах 10 к/с.

Все числа стенда — **ориентир, не результат**: ВМ на Mac M4 Pro с Jazzy и torch CPU — не Jetson Nano с Foxy и CUDA.
Результат дают только замеры на плате (§8, «Как проверить на плате»).

Пометки: **[ист.]** — внешний источник (ссылки в `model.json` и в конце файла); **[замер]** — замер на стенде;
**[оценка]** — расчёт без замера; **[допущение]** — принято без данных; **[код]** — по исходнику.

Готовое, на котором стоит компонент (не наше): systemd и cgroup v2 (`MemoryMax`, `CPUQuota`, `AllowedCPUs`, scope
по `StartTransientUnit`), ROS 2 Jazzy, цифровой двойник (`neurobot/sim/twin`, другой компонент) на Gazebo Harmonic,
`realsense2_camera` 4.58, `urg_node` 1.1.2, `ros2_control` (`mock_components`, `diff_drive_controller`,
`joint_state_broadcaster`), `twist_mux`, `robot_state_publisher`, `imu_filter_madgwick`, `rosbridge_server` 2.7.1,
PicoClaw 0.2.8 (Sipeed), MCP SDK 1.27.2, PyTorch, p7zip 16.02 (бенчмарк 7-Zip). Своё: модель и её источники,
обвязка запуска в slice, перенос узлов двойника, заместители драйверов (launch), балласт, сэмплер, наблюдатель
здоровья, калибровка квоты, разбор и отчёт. `sysmon` (`sysmon/`) — наш инструмент из другого каталога, здесь —
сверка.

## 1. Модель памяти

### 1.1. Слагаемые

| Слагаемое | Значение (мин.–макс.), МиБ | Пометка | Основание |
|---|---|---|---|
| Физическая память | 4096, общая для CPU и GPU | [ист.] | tegrastats NVIDIA |
| Видно приложениям (MemTotal) | **3956** (3950–3964) | [ист.] | tegrastats T210 «RAM 220/3955MB»; форум NVIDIA «…/3964MB»; 7-Zip на Nano «RAM size 3950 MB». Остальные ≈ 130–140 МиБ — прошивка, резервы ядра и карвауты |
| ОС без рабочего стола (headless) | **420** (400–500) | [ист.] | форум NVIDIA: «approx. 400MB» и «about 400 MB» по ssh без Unity; Qengineering (Ubuntu 20.04 для Nano): «headless OS consuming ~420 MB»; верх 500 — [допущение]: службы робота сверх чистой системы |
| ОС с LXDE | **700** (700–1000) | [оценка] | JetsonHacks: GNOME ≈ 1,7 ГБ, LXDE экономит ≈ 1 ГБ (страница закрыта проверкой от ботов — числа по выдаче поиска); NVIDIA сама ставит LXDE на Nano 2 ГБ |
| ОС с рабочим столом GNOME | **1350** (1000–1700; через часы — до 2500) | [ист.] / [оценка] середины | форум NVIDIA: «idles at ~1GB»; «close to 1.7 gigabytes» после загрузки и «до 2,5 ГБ через несколько часов» (наблюдения пользователей) |
| Резерв (не отдаём стеку) | 200 | [допущение] | ≈ 5 %: slab ядра, кэш журналов; без запаса ядро сначала вытесняет код наших процессов (Pss_File), и робот деградирует раньше OOM |
| Контекст CUDA, cuDNN, cuBLAS PyTorch | **1024** (до 1334) | [ист.] | модератор NVIDIA на Nano, JetPack 4.5.1, PyTorch 1.8: «The memory increases around 1GiB»; в его же tegrastats 1052 → 2386 МБ (+1334, вместе с импортом torch) |
| Потоки камеры (`realsense2_camera`) | 30 МиБ, 20 % ядра A57 | [оценка] | очереди librealsense (`frames_queue_size` 16) × (цвет 640×480×3 Б + глубина 848×480×2 Б) ≈ 28 МиБ; CPU приёма и публикации 30 к/с — [допущение] |
| zram | 4 устройства, всего ½ RAM ≈ 1978 МиБ, lzo | [ист.] | сотрудник NVIDIA: своп 2 ГБ — это zram, 1:2 к RAM, `/etc/systemd/nvzramconfig.sh`; JetsonHacks: 4 × 494,6 МБ, `mem=$((("${totalmem}" / 2 / "${NRDEVICES}") * 1024))`; ядро 4.9: `default_compressor = "lzo"` |

На роботе есть ЖК-экран (слайд 3), а демо лаборатории рисует окно OpenCV (кадр «Collected: 29», слайд 8): вероятно,
рабочий стол сейчас работает. Набор слайда 4 (Ubuntu 20.04 + JetPack 4.6 + Python 3.8 + PyTorch 1.13 + ROS 2 Foxy)
совпадает с образом Qengineering, а в нём рабочий стол — GNOME [гипотеза: образ не подтверждён лабораторией].
Поэтому считаются обе ветки, и ветка GNOME — та, что сейчас вероятнее у лаборатории; headless — то, что мы
предлагаем (perception в пакете 1 работает без окна).

**zram.** Своп JetPack — только сжатая RAM: страница, вытесненная в zram, занимает 1/k своего размера (k = 2–3 для
lzo — [допущение]). zram освобождает (1 − 1/k) от **холодных** анонимных страниц ценой CPU на сжатие и задержки на
первом обращении; горячим страницам (кадры, тензоры, буферы DDS) он не помогает, а страница perception, вытесненная
посреди цикла, превращается в задержку кадра. На стенде своп выключен (`MemorySwapMax=0`): бюджет — **без кредита
zram**, консервативно. `model.py` даёт кредит отдельно (`zram_credit`: при 500 МиБ холодных страниц и k = 2–3 —
250–333 МиБ); на плате его надо мерить (`/sys/block/zram*/mm_stat`).

### 1.2. Бюджет стека (MemoryMax `jetson.slice`)

MemoryMax = видимая память − ОС ветки − резерв (`python3 -m neurobot.sim.budget.model`):

| Ветка ОС | value | pessimistic | optimistic |
|---|---|---|---|
| headless | **3336** | 3250 | 3364 |
| lxde | **3056** | 2750 | 3064 |
| gnome | **2406** | 2050 | 2764 |

pessimistic — меньшая видимая память и бо́льшая ОС; optimistic — наоборот. GNOME «через несколько часов»
(2,5 ГБ) оставил бы стеку 1256 МиБ — меньше, чем нужно perception с CUDA (§6).

## 2. Модель CPU

Квота — по опубликованному соотношению одноядерной производительности:
- Jetson Nano (Cortex-A57, 1479 МГц, MAXN): `7za b -mmt1` p7zip 16.02 — **1578 MIPS**, четыре потока — 5260
  [ист.: sbc-bench, `results/4rLX.txt`];
- ВМ sim, тот же двоичный p7zip 16.02 из Debian bullseye (SHA-256 сверен с индексом пакетов Debian), одно ядро
  (`taskset`): медиана трёх прогонов [замер, `cpucal_host.json`];
- **CPUQuota = 4 × 100 % × (1578 / MIPS ВМ)** на `AllowedCPUs=0-3`, период 20 мс (как `bench/board`).

Квота держит **суммарное время CPU** четырёх A57, а не скорость одного ядра: одиночный поток в slice работает с
полной скоростью ядра M4 Pro (≈ 3,8–3,9 раза быстрее A57), а многопоточный 7-Zip под квотой даёт ≈ 1,6 от Nano
(`cpucal_check.json`) — по многопоточному 7-Zip квоту пришлось бы снизить до ≈ 66 %. Поэтому основной шаг идёт с
квотой по одному ядру (как просит PLAN), а шаг `cpu66` — с квотой, равной Nano по многопоточному 7-Zip. Режим 5 Вт
(nvpmodel 1: 2 ядра, ниже частота) не моделировался. Соотношение A57/ВМ для Python, numpy и DDS может отличаться от
7-Zip — **[допущение] до калибровки `cpubench` (`bench/board/cpubench`) на плате**. Калибровка зависит от нагрузки
ВМ соседними агентами: 16.10 одно ядро давало 5883–6246 MIPS в разных прогонах (квота 101–107 %); прогон `b1` —
медиана 5956, квота 106 %.

«% ядра A57» в отчёте — это % ядра ВМ, делённый на отношение (≈ 0,26): сколько занял бы компонент на Nano, если
Python и DDS замедляются так же, как 7-Zip.

## 3. Состав cgroup `jetson`

Состав — по графу ROS робота 02.06 (R7a §1.5), слайду 5 и пакету 1 (`neurobot/deploy`, вариант А агентского пути).

| Компонент на Jetson | На стенде | Как учтён |
|---|---|---|
| perception (predict_live v2, `--camera ros`; YOLO и SNN на torch 1.13 + CUDA) | тот же код (`neurobot/perception/predict_live.py`), `--yolo fake` (FakeYolo по таймлайну gesturegen), SNN лаборатории `snn_emnist_small.pth` на torch 2.4.1 CPU (`weights_only`), кадры камеры двойника 10 к/с | [замер] + **балласт cuda 1024 МиБ в том же процессе** (`ballast.py --run`): OOM на плате выбрал бы perception, а не отдельный процесс-балласт. CPU предобработки и NMS YOLO не учтён |
| safety_gate | тот же код, Python 3.12 | [замер] |
| motion_executor | тот же код | [замер] |
| rosbridge (`ros2 launch` + `rosbridge_websocket` + `rosapi`) | 2.7.1 с аргументами deploy (`127.0.0.1`, глобы) | [замер] |
| MCP: ros_mcp_server лаборатории + патч deploy (HTTP) | копия `third_party/…/ros_mcp_server.py` + `neurobot/deploy/mcp/ros_mcp_server.patch`, mcp 1.27.2 | [замер], без вызовов инструментов |
| PicoClaw 0.2.8 gateway | тот же бинарник linux-arm64, конфиг `neurobot/deploy/fakes/picoclaw.json` с портами шага (поля 0.2.8: без `max_llm_retries`, `load_image`; MCP `type: http`; `log_level: info`) | [замер], без каналов и ходов LLM |
| twist_mux, robot_state_publisher, imu_filter_madgwick | **процессы двойника, перенесённые в slice** (scope `jetson-<узел>.scope`) | [замер]; страницы, выделенные до переноса, остаются на счету прежней группы — cgroup v2 не переносит заряд, MemoryMax их не ограничивает, но PSS учтён полностью |
| база: `ros2 launch` + controller_manager + diff_cont + joint_broad; железо — мост UART к STM32 | заместитель `launch/base_proxy.launch.py`: `ros2_control_node` с `mock_components/GenericSystem`, те же `controllers.yaml` (50 Гц) и URDF двойника, свой домен DDS; `describe.py` отдаёт URDF один раз | [замер заместителя]; колёсами двойника управляет controller_manager внутри Gazebo |
| камера: `ros2 launch realsense2_camera` | тот же launch (4.58) без устройства, свой домен | [замер заместителя] + балласт потоков 30 МиБ и 20 % ядра A57 [оценка] |
| лидар: `ros2 launch urg_node2` | `launch/lidar_proxy.launch.py`: `urg_node` (urg_node2 в Jazzy нет) без устройства, свой домен; узел дважды в секунду пробует открыть порт | [замер заместителя] |

Не учтены (нет данных или не наш стек): второй `urg_node2` из графа робота (вероятно, ошибка запуска — ещё ≈ один
лидар), `image_compressor` и `topic_watchdog` лаборатории (назначение неизвестно), сторож `neurobot watchdog`
(завязан на юниты `neurobot-*`), воркеры 8765/8766 лаборатории (вариант Б deploy; YOLO-воркер на CUDA — второй
контекст ≈ 1 ГиБ, R7a N7, с perception в 4 ГБ не помещается), sysmon. Список — в `model.json` (`not_counted`).

Узлы двойника, которые подменяют драйверы (мост Gazebo, `sensor_model.py`), `cmd_relay.py` (есть только на двойнике)
и `truth.py`, — вне slice, как и сам Gazebo. Наблюдатели бюджета — в `budget-obs.slice`, вне лимитов.

**Супервизия** юнитов — как у `neurobot/deploy/units` (`Type=notify` + `WatchdogSec` 5 / 3 / 3 с у perception,
executor, safety; `Restart=always`, `RestartSec`, `KillSignal`, `TimeoutStartSec`). Порядок подъёма — PLAN §5.4:
заместители драйверов → safety_gate → исполнитель → perception → rosbridge → MCP → PicoClaw; готовность — по READY,
порту или строке «MCP server initialization complete» в журнале PicoClaw.

**Домены и порты** (общая ВМ): двойник и стек — `ROS_DOMAIN_ID` 86, заместители — 87; rosbridge 19186, MCP 18186,
PicoClaw 18796 — всё только на 127.0.0.1.

## 4. Запуск

С Mac из корня проекта; ВМ «sim» запущена; каталог проекта смонтирован в ВМ только для чтения по тому же пути.
Рабочий каталог в ВМ — `~/budget_20261016` (`BUDGET_BASE`).

```sh
neurobot/sim/budget/budget.sh prepare            # один раз: пакеты, загрузки с проверкой SHA-256, venv mcp, патч MCP
neurobot/sim/budget/budget.sh cpucal b1          # квота: 7-zip ×3 на одном ядре ВМ + проверка в slice (≈ 3 мин)
neurobot/sim/budget/budget.sh run b1 --cpu-variant 66   # main 600 с, лестница 5 × 180 с, шаг cpu66; затем report
neurobot/sim/budget/budget.sh run b1 --ladder 1792,1664  # 16.10: повтор main (с uniq.json) и уточнение порога
neurobot/sim/budget/budget.sh fetch b1 experiments/results/neurobot_20261016_budget
neurobot/sim/budget/budget.sh down               # аварийно: юниты бюджета (BUDGET_RUN=b1 — и двойник прогона)
```

**Подготовка** (`prepare`, повторный вызов ничего не перекачивает): apt `ros-jazzy-realsense2-camera`,
`ros-jazzy-urg-node` (≈ 15 МБ); `p7zip-full_16.02+dfsg-8_arm64.deb` с `deb.debian.org` (SHA-256 из индекса Debian
bullseye); `picoclaw_Linux_arm64.tar.gz` v0.2.8 из релиза sipeed/picoclaw (SHA-256 из `picoclaw_0.2.8_checksums.txt`);
venv `mcp==1.27.2`, `websocket-client==1.9.2`; копия `ros_mcp_server.py` лаборатории с патчем deploy (`third_party/` не
меняется). Python с torch для perception — `~/nb/venv` (готовит компонент perception).

### 4.1. `budget.sh`

| Команда и флаги | По умолчанию | Смысл |
|---|---|---|
| `run RUN --main-mem M` | бюджет headless из `model.json` (3336) | MemoryMax шага `main` |
| `--ladder L` | `4096,3072,2406,2048,1536` | лестница MemoryMax (2406 — бюджет GNOME); пусто — без лестницы |
| `--cpu-variant Q` | нет | шаг `cpu<Q>`: память main, квота Q % |
| `--seconds`, `--ladder-seconds` | 600, 180 | замер шага после прогрева 30 с |

Квота и отношение A57/ВМ берутся из `RUN/cpucal_host.json`. Двойник поднимается один раз (`world:=lab color_hz:=10
rosbridge:=false`, свой корень прогонов `RUN/twin_runs`) и гасится в конце.

### 4.2. `stack.py` (ВМ, root)

`sudo python3 -m neurobot.sim.budget.stack run|down|status` (из корня проекта):

| Флаг `run` | По умолчанию | Смысл |
|---|---|---|
| `--out DIR` | обязателен | каталог прогона; шаги — `DIR/<метка>/` |
| `--mem M[,M…]`, `--labels` | обязателен; `mem<M>` | MemoryMax шагов, МиБ (≥ 256), их метки |
| `--quota`, `--ratio` | обязательны | CPUQuota, %; отношение A57/ВМ (для балласта CPU и отчёта) |
| `--cpus` | 4 | `AllowedCPUs=0-(N-1)`, `GOMAXPROCS` PicoClaw |
| `--seconds`, `--warmup`, `--interval` | 600, 30, 5 | замер, прогрев после готовности, период collect и sysmon, с |
| `--domain`, `--proxy-domain` | 86, 87 | `ROS_DOMAIN_ID` двойника и стека; заместителей драйверов |
| `--world`, `--color-hz` | `lab`, 10 | мир двойника; кадры/с камеры цвета = темп детекции perception |
| `--seed` | 20261016 | зерно gesturegen (40 жестов, 12 отрицательных сегментов, 10 к/с; общий таймлайн, свой якорь на шаг) |
| `--base`, `--ml-python` | `~/budget_20261016`, `~/nb/venv/bin/python` | инструменты; Python с torch |
| `--keep-twin` | выкл. | не гасить двойник: следующий `run` в том же `--out` его подхватит |

`down [--twin-root DIR]` — остановить `jetson-*` и `budget-*`, погасить двойник корня, снять настройки slice.
Код 2 — ошибка аргументов (в том числе запуск не от root или без `SUDO_USER`).

Шаг пишет: `step.json` (конфиг slice, времена, готовность компонентов, состояние юнитов в конце, строки OOM из журнала
ядра), `units.json`, `events.jsonl` (подъём, перенос), `collect.jsonl`, `health.jsonl`, `sysmon.jsonl`, `uniq.json`
(уникальные страницы в середине окна), журналы
`logs/<компонент>.log`, конфиги шага (`mcp.json`, `picoclaw/config.json`, `robot_mock.urdf`, `controllers.yaml`).

### 4.3. Остальные модули

| Модуль | Флаги (по умолчанию) | Что делает |
|---|---|---|
| `model.py` | `--model`, `--host-mips N`, `--json` | таблица бюджетов веток, балласт, кредит zram, квота по MIPS ВМ; проверка структуры `model.json` (пометки, источники, порядок границ), код 2 — ошибка |
| `ballast.py` | `--mib N` (обяз.), `--net`, `--cpu-pct P` (0), `--period-ms` (100), `--run SCRIPT [-- арг.]` | резидентная анонимная память (по байту в каждую страницу), `--net` — за вычетом своего PSS; CPU циклами «работа — сон»; `--run` — выполнить скрипт как `__main__` в этом же процессе |
| `collect.py` | `--units`, `--out` (обяз.), `--interval` (5), `--duration` (0 — до сигнала) | память и CPU slice, юнитов и их процессов (smaps_rollup, /proc/<pid>/stat), PSI slice, loadavg ВМ → JSONL; `unique_snapshot()` — уникальные страницы по pagemap (root, зовёт stack.py) |
| `health.py` | `--out` (обяз.), `--interval` (1) | частоты и состояния `/neurobot/*`, `/cmd_vel`, `/diff_cont/cmd_vel_unstamped`, RTF двойника → JSONL |
| `cpucal.py` | `host --bin 7za [--cpu 5] [--repeats 3] [--cores 4] [--out]`; `check --bin --quota [--cpus 4] [--period-ms 20] [--out]` (root) | 7-Zip одного ядра ВМ → отношение и квота; проверка квоты 7-Zip в `budget-cal.slice` |
| `report.py` | `--run` (обяз.), `--main` (`main`), `--cpucal`, `--check`, `--model`, `--out` | `report.json`, `report.md` (§5) |
| `manifest.py` | `--run` (обяз.), `--base` | `manifest.json`: версии, SHA-256 кода и бинарников, конфиги шагов, отклонения |
| `describe.py` | `<robot.urdf>` | однократный издатель `/robot_description` для заместителя базы |
| `run_ros.sh` | команда | окружение ROS стенда (`scripts/sim/vm_env.sh`) и `exec` без смены PID |

Логи всех модулей — в stderr (у юнитов — в `logs/<компонент>.log` шага); stdout — только данные (JSON, таблица).

## 5. Что меряется и как читать отчёт

- **PSS** (`smaps_rollup`) — по компонентам: общие библиотеки делятся между процессами; сумма PSS slice сходится с
  `sysmon` (сверка в отчёте). Но PSS делит библиотеки и с процессами **вне** slice (Gazebo, узлы двойника, соседние
  агенты), поэтому сумма PSS занижает то, что стек займёт на плате.
- **Уникальные страницы** (`uniq.json`, снимок в середине окна от root по `/proc/<pid>/pagemap`): каждая физическая
  страница — один раз на группу, как на плате, где библиотеки делят только эти процессы. С ними сравнивается бюджет;
  сумма RSS — верхняя граница (общее считается в каждом процессе).
- **`memory.current`** slice меньше обоих: перенесённые узлы двойника и страницы файлов, прочитанные раньше другими
  группами, на счету slice не числятся — поэтому порог OOM на стенде ниже, чем на плате (§6.2).
- **Окно замера** — от прогрева 30 с после готовности до конца шага; медиана по сэмплам раз в 5 с. **Пик** — за весь
  шаг, вместе с подъёмом; у slice — максимум `memory.current` по сэмплам (`memory.peak` slice копится через шаги).
- **CPU** — приращение `cpu.stat usage_usec` группы юнита за окно, в % одного ядра ВМ и «% ядра A57» (§2); у slice —
  ещё доля периодов CFS с троттлингом.
- **Исход шага** (`report.py`): **OOM** — выросло `memory.events oom_kill` slice (жертвы — по журналу ядра и PID из
  collect); **деградация** — без OOM, но компонент не готов, были перезапуски, кадры/с perception < 80 % опорных,
  perception молчит (нет `/neurobot/perception_health`) больше 20 % секундных окон, `/neurobot/safety_state` < 4 Гц
  (норма 5), давление памяти PSI some > 5 % времени, мажорные отказы страниц > 50/с или двойник погас; иначе **норма**. Первое, что ломается, — исход верхнего не нормального шага лестницы.
- Балласт в таблицах помечен ролью `ballast` (процесс потоков камеры) и строкой «код + cuda» у perception.

## 6. Результаты 16.10.2026 (ориентир, не результат)

Артефакты — `experiments/results/neurobot_20261016_budget/`: `report.md` и `report.json` (всё ниже), шаги с сырыми
замерами, `cpucal_*.json`, `rmw_delta.txt`, `manifest.json`. Условия: ВМ sim общая с другими агентами (load1 ≈ 6 на
6 vCPU, занятость ВМ ≈ 43 %), двойник `lab` с RTF ≈ 0,96, жесты gesturegen 10 к/с (30 жестов за 600 с), квота 106 %
(отношение A57/ВМ 0,265 при load1 ≈ 4–6). Шаг `main_a` — первый прогон `main` до того, как в шаг добавлен снимок
уникальных страниц (`uniq.json`); перед повтором `main` каталог и метка в `step.json` переименованы вручную, шаг
оставлен как повтор: сумма PSS 1734,9 против 1733,3 МиБ, CPU 1,31 против 1,27 ядра
A57. Код других компонентов правился параллельно: `neurobot/safety/depth.py` изменён в 07:48, во время шага `main`
(safety_gate стартовал в 07:38 с прежней версией); хеши в `manifest.json` сняты после прогона, в 08:03.

### 6.1. Память стека по компонентам (шаг `main`, MemoryMax 3336 МиБ, 600 с) [замер]

| Компонент | Роль | PSS, МиБ | Уникальные, МиБ | Из чего |
|---|---|---|---|---|
| perception | замер + балласт | 1305 | 1375 | код 281 (torch CPU, SNN, rclpy, OpenCV) + cuda 1024 |
| rosbridge | замер | 111 | 137 | `ros2 launch` 29 + rosbridge_websocket 43 + rosapi 41 |
| camera | заместитель | 61 | 100 | `ros2 launch` 25 + realsense2_camera_node без потоков 39 |
| mcp | замер | 42 | 52 | ros_mcp_server, mcp 1.27.2 |
| safety_gate | замер | 38 | 63 | |
| base | заместитель | 36 | 63 | `ros2 launch` 25 + ros2_control_node 13 |
| executor | замер | 33 | 57 | |
| camera_streams | балласт | 30 | 36 | очереди кадров [оценка] |
| lidar | заместитель | 30 | 51 | `ros2 launch` 25 + urg_node 6 |
| PicoClaw 0.2.8 | замер | 28 | 28 | 22 — сам бинарник |
| robot_state_publisher, imu_filter_madgwick, twist_mux | перенесены | 7 + 7 + 6 | 21 + 20 + 18 | |
| **slice** | | **1733** | **1803** | RSS 2081; memory.current 1575 (anon 1557, kernel 15); sysmon — те же 1733 |

Уникальные страницы устойчивы: 1802–1810 МиБ во всех шагах без OOM и в контрольном прогоне, а сумма PSS гуляет
1733–1770 МиБ — она зависит от того, сколько процессов вне slice делят те же библиотеки.
Без балласта процессы стенда занимают ≈ 745 МиБ уникальных страниц, из них четыре родителя `ros2 launch` — по
≈ 25 МиБ (≈ 100 МиБ на всех) и каждый узел на Python — 31–43 МиБ анонимной памяти.

**Бюджет против уникальных страниц (1803 МиБ):**

| Ветка ОС | Бюджет value | Запас | Бюджет pessimistic | Запас |
|---|---|---|---|---|
| headless | 3336 | **+1533** | 3250 | +1447 |
| LXDE | 3056 | +1253 | 2750 | +947 |
| GNOME | 2406 | **+603** | 2050 | +247 |

Поправки на плату, которых стенд не видит [оценка]: верх CUDA (+310 МиБ: 1334 вместо 1024), Fast DDS вместо Cyclone
(+4–5 МиБ анонимной памяти на участника DDS и ≈ 7 МиБ библиотеки — `rmw_delta.txt`; ≈ 16 участников → +80–95 МиБ, а
в Foxy участник создаётся на каждый узел, так что больше), память ядра slice (+15 МиБ). Пессимистичный стек —
≈ 2,2 ГиБ: без рабочего стола запас ≈ 1,1 ГиБ; с GNOME запас ≈ +0,2 ГиБ при средней ОС и **дефицит ≈ 0,17 ГиБ** при
пессимистичной (1,7 ГБ); GNOME «через часы» (2,5 ГБ) оставляет стеку 1256 МиБ — perception с CUDA не помещается.
YOLO-воркер лаборатории на CUDA рядом с perception (вариант Б deploy, второй контекст + torch ≈ 1,3 ГиБ) не помещается
и без рабочего стола (≈ 3,5 ГиБ против 3336). Значит, для 4 ГБ: **без рабочего стола (или LXDE) и один процесс
с CUDA**.

### 6.2. Лестница MemoryMax (180 с на шаг) [замер]

| MemoryMax, МиБ | Исход | Уникальные / max memory.current, МиБ | Кадры/с, safety_state |
|---|---|---|---|
| 4096, 3072, 2406, 2048, 1792, 1664 | норма: PSI 0, мажорных отказов 0, перезапусков 0 | 1803–1808 / 1579–1597 | 10 к/с, 4,8–4,9 Гц |
| **1536** | **OOM-петля perception**: 96 убийств ядром и 97 перезапусков за шаг (≈ 3,5 мин), perception молчит 100 % окон, жестов 0 | 486 / 1536 | — ; 4,8 Гц |

- **Что ломается первым — perception** (самый большой процесс, он же держит CUDA): ядро убивает его каждые ≈ 2,2 с
  (READY → OOM → `RestartSec=1`). Остальные живы: safety_gate держит 4,8 Гц, исполнитель и двойник работают, но робот
  глух к жестам. **Ступени «деградации» перед OOM нет**: память почти вся анонимная (файловых страниц 3 МиБ), свопа
  нет — вытеснять нечего, PSI ≈ 0 до самого OOM. На плате с zram ступень появится: холодные страницы уйдут в сжатый
  своп, ценой CPU и задержек кадров.
- **Петля перезапусков съедает CPU**: slice — 2,56 ядра A57 против 1,27 в норме, троттлинг в 48 % периодов CFS;
  ≈ 1,3 ядра уходят на повторные импорты torch (по группам юнитов их не видно: группа perception пересоздаётся при
  каждом перезапуске, виден только slice). На плате это отняло бы CPU у safety_gate — довод за откат перезапусков при
  OOM (deploy: `RestartSec` больше 1 с или ограничение частоты стартов perception).
- Порог на стенде — между 1536 и 1664 МиБ, то есть по заряженной памяти slice (≈ 1,58–1,60 ГБ). Узлы двойника и
  файловые страницы, прочитанные раньше другими группами, slice не заряжены, поэтому на плате порог выше: уникальные
  1,8 ГиБ + ядро, а с поправками §6.1 — ≈ 2,2 ГиБ.

### 6.3. CPU при жестах 10 к/с (шаг `main`) [замер; «ядро A57» — через 7-Zip, допущение]

| Компонент | % ядра ВМ | ≈ % ядра A57 | Замечание |
|---|---|---|---|
| safety_gate | 14,8 | **56** | самый дорогой: Python на каждом сообщении IMU 2 × 200 Гц, глубины 15 Гц, скана 10 Гц |
| camera_streams | 4,9 | 18 | балласт, [допущение] |
| perception | 3,3 | 13 | без YOLO: предобработка и NMS на CPU и ожидание GPU не учтены |
| imu_filter_madgwick | 3,3 | 13 | завышено: на двойнике разбирает ещё `/clock` 1 кГц |
| base (заместитель) | 2,9 | 11 | controller_manager 50 Гц |
| robot_state_publisher | 2,5 | 10 | завышено `/clock` 1 кГц |
| executor | 1,5 | 6 | |
| rosbridge, mcp, PicoClaw, camera, lidar, twist_mux | ≤ 0,1 каждый | ≤ 0,5 | агентский путь в простое |
| **slice** | **33,7** | **≈ 1,27 ядра из 4** | троттлинга нет; при квоте 66 % (`cpu66`, по многопоточному 7-Zip) — 1,8 % периодов, исход «норма» |

На плате добавятся YOLO (предобработка и NMS на CPU), разбор кадров 30 к/с вместо 10 и ходы LLM PicoClaw; одиночный
поток на A57 в ≈ 3,8 раза медленнее, чем в slice (§2), поэтому задержки стенда не переносятся на плату. Для safety_gate
стоит проверить, нужны ли ему обе IMU по 200 Гц (рекомендация владельцу компонента).

## 7. Тесты

```sh
# Mac, Python 3.12 (.venv) и 3.8 (uv): логика модели, балласта, разбора замеров, отчёта, команд systemd
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider neurobot/sim/budget/tests
# ВМ sim (Jazzy): то же + живой разбор собственной группы cgroup и /proc
source scripts/sim/vm_env.sh; PYTHONDONTWRITEBYTECODE=1 ~/nb/venv/bin/python -m pytest -q -p no:cacheprovider \
    neurobot/sim/budget/tests
```

| Файл | Что проверяет |
|---|---|
| `test_budget_model.py` | `model.json` проходит проверку, каждый внешний источник процитирован; бюджет = видимая − ОС − резерв; порядок случаев и веток; квота по 7-Zip (и для 2 ядер); кредит zram с потолком; проверка ловит неверные пометки, источники, границы, балласт, нет раздела; CLI |
| `test_budget_ballast.py` | `--net` вычитает свой PSS; все страницы записаны; разбор `smaps_rollup`; цикл CPU и его пределы; `--run` выполняет скрипт как `__main__` в том же PID с кодом выхода скрипта; неверные аргументы — код 2; отдельный процесс выходит по SIGTERM |
| `test_budget_collect.py` | разбор `cpu.stat`/`memory.stat`, `smaps_rollup`, `/proc/<pid>/stat` с пробелами и скобками в имени, PSI, `/proc/stat`; процессы вложенных групп; вышедший процесс; остановленный юнит; уникальные страницы: общая — один раз, без root (PFN 0) — нет данных, резервы адресного пространства пропускаются; CLI пишет header и сэмплы; на Linux — своя группа и /proc |
| `test_budget_health.py` | окно: счёт сообщений, состояния safety_gate, ненулевые `/cmd_vel`; замолчавший издатель не оставляет старых значений; битый JSON |
| `test_budget_cpucal.py` | разбор вывода 7-Zip ВМ и Nano (sbc-bench), нет строки Tot, отношение и квота, `check` только от root |
| `test_budget_stack.py` | неверные аргументы `run` — код 2 до любых действий; порядок и роли компонентов; супервизия как у deploy; slice, пользователь, журналы, домены; балласт CUDA в perception; CPU балласта камеры в долях ядра ВМ; rosbridge только 127.0.0.1 и глобы deploy; свойства slice; scope для переноса; замена железа в URDF; конфиг MCP; **конфиг PicoClaw проходит схему 0.2.8 deploy**; PID узлов двойника только из его группы; запуск без root — код 2 |
| `test_budget_report.py` | приращение счётчика с перезапуском; окно, PSS, CPU, балласт cuda, мажорные отказы, PSI, троттлинг, пик slice по сэмплам, OOM и жертвы (по PID и по имени, сгруппированы); молчание perception; исход норма / деградация / OOM; бюджет по уникальным страницам, без них — по PSS; отчёт и «первое, что ломается» |
| `test_budget_manifest.py` | шаги, SHA-256 кода, отклонения; нет каталога — код 2 |

## 8. Отклонения, ограничения и не сделано

Отклонения стенда от платы (они же — в `manifest.json`, `deviations`):
- Jazzy, Python 3.12 и Cyclone DDS вместо Foxy, Python 3.8 и Fast DDS (умолчание Foxy) — память процессов ROS и
  Python на плате будет другой;
- torch 2.4.1 CPU вместо 1.13 + CUDA, FakeYolo вместо YOLO; CUDA — балласт [ист.];
- драйверы — пакеты Jazzy без устройств в отдельном домене; потоки камеры — балласт [оценка];
- камера цвета двойника 10 к/с, у realsense-ros на роботе, вероятно, 30: разбор лишних кадров в rclpy не учтён;
- квота CPU держит суммарное время, а не скорость ядра (§2); своп выключен (§1.1);
- ВМ общая с другими агентами: CPU-секунды и RTF зависят от их нагрузки (load1 и занятость ВМ — в отчёте).

**Как проверить на плате** (вопросы к лаборатории, дополнение к Э0):
- `free -m` и `tegrastats` сразу после загрузки — ОС ветки лаборатории; `systemctl get-default` и
  `loginctl list-sessions` — есть ли рабочий стол на ЖК-экране;
- `cat /proc/meminfo`, `zramctl`, `/sys/block/zram0/comp_algorithm` и `mm_stat` под нагрузкой — кредит zram;
- до и после старта perception: `tegrastats` RAM и сумма PSS (`smem` или `/proc/*/smaps_rollup`) — память CUDA вне
  PSS процесса [гипотеза: аллокации nvmap в RSS процесса не видны — сверить по `/sys/kernel/debug/nvmap/iovmm/clients`];
- `cpubench` (`bench/board/cpubench`) на плате → `python -m bench.board calibrate --target <ops/s>` — квота;
- `sudo nvpmodel -q` — режим MAXN или 5 Вт.

**Не сделано:**
- наклон памяти за 1–2 ч (PLAN §13.2) — шаги по 3–10 мин; дрейф не оценён;
- ходы LLM PicoClaw и вызовы MCP под нагрузкой (агентский путь в простое);
- поправка на Fast DDS — только оценка по одиночным узлам Jazzy (`rmw_delta.txt`), на Python 3.8 и torch 1.13 — нет
  (на стенде нет Foxy);
- режим 5 Вт (2 ядра); троттлинг по температуре;
- zram на стенде (своп в общей ВМ не включался — он изменил бы условия соседних агентов);
- CPU предобработки и NMS YOLO, разбор кадров 30 к/с.

## Источники

- NVIDIA, tegrastats Utility (L4T): https://docs.nvidia.com/jetson/l4t/Tegra%20Linux%20Driver%20Package%20Development%20Guide/AppendixTegraStats.html
- Форум NVIDIA, CUDA и RAM на Nano: https://forums.developer.nvidia.com/t/calling-cuda-consumes-all-the-ram-memory/183240
- Форум NVIDIA, RAM рабочего стола и headless: https://forums.developer.nvidia.com/t/reduce-ram-usage/84276 ,
  https://forums.developer.nvidia.com/t/headed-mode-to-headless-mode/120388 ,
  https://forums.developer.nvidia.com/t/high-memory-usage-the-os-of-jetson-nano/143129
- Qengineering, Ubuntu 20.04 для Jetson Nano: https://github.com/Qengineering/Jetson-Nano-Ubuntu-20-image
- JetsonHacks, LXDE: https://jetsonhacks.com/2020/11/07/save-1gb-of-memory-use-lxde-on-your-jetson/
- Форум NVIDIA, zram JetPack: https://forums.developer.nvidia.com/t/jetpack-4-2-3-has-a-built-in-2gb-swap-file-we-are-not-able-to-increase-this-to-4-gb/108324
- JetsonHacks, размер zram: https://github.com/JetsonHacksNano/resizeSwapMemory
- Ядро Linux 4.9, zram: https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/plain/drivers/block/zram/zram_drv.c?h=v4.9
- sbc-bench, Jetson Nano: https://github.com/ThomasKaiser/sbc-bench/blob/master/results/4rLX.txt
- Форум NVIDIA, режимы nvpmodel Nano: https://forums.developer.nvidia.com/t/cpu-running-count-and-enabling/145700
- Внутренние: `docs/neurobot/research/R7_robotic_platform.md`, `R7_robot_slides.md`,
  `research/new_topic/landscape/L4_jetson_nano.md`, `bench/board/README.md`.
