# O2. Рантайм на слабой плате: Go-агент, Python-узлы ROS 2, Jetson Nano

Срез: 20.10.2026. Это только исследование: код и конфиги не менялись. Сверка шла с `agent/README.md` и кодом
`agent/internal/{userctx,agent,llm}`, `bench/board/README.md`, `neurobot/sim/budget/README.md`, юнитами
`neurobot/deploy/units/*`, `neurobot/deploy/neurobot.env.example`, кодом `neurobot/{safety,executor,perception,ui}`,
`neurobot/lab/collect_diag.sh`, `sysmon/README.md`, `harness/stats.py`, `research/new_topic/landscape/L4_jetson_nano.md`
и `docs/neurobot/research/R5_external.md`.

Пометки: **[есть]** / **[частично]** / **[нет]** — состояние у нас. **[ист.]** — внешний источник (раздел 7),
**[код]** — по нашему исходнику, **[замер]** — наш замер (двойник или ВМ, то есть ориентир), **[оценка]** — расчёт,
**[гипотеза]** — не проверено.

## 0. Главное

1. Рантайм-решения ядра в целом адекватны и опираются на первоисточники: GOMEMLIMIT при GOGC по умолчанию,
   WAL + `synchronous=NORMAL`, буферизованная трасса, статический бинарник под ARMv8.0, headless-рекомендация,
   бюджет 4 ГБ с CUDA ≈1 ГиБ. Трасса дня — ≈1,5 МБ за 16 ч [замер: `review_runs_20261007/mplus_sysmon`], для износа SD
   это несущественно.
2. Главный риск — **перенос со стенда на плату**. Стенд: ядро 6.8, cgroup v2, PSI, systemd 255, Jazzy, Python 3.12.
   Nano: ядро 4.9, cgroup v1, нет PSI и `memory.peak`, systemd 245, Foxy, Python 3.8. Не переносятся критерии
   «деградации» бюджета (PSI), поведение Go при THP и семантика `MemoryMax`/`CPUWeight` юнитов (§1).
3. Самые дешёвые выигрыши по CPU — на Python-стороне ROS:
   - главный цикл `rclpy.spin_once(node)` в Foxy на каждой итерации добавляет и убирает узел и дважды взводит
     guard [ист.: исходник rclpy foxy];
   - частоты потоков камеры в deploy не заданы, хотя README safety советует глубину 424×240 при 15 к/с.
4. Политика перезапусков не защищает от петли OOM: perception падает каждые ≈2,2 с, slice тратит 2,56 ядра A57
   против 1,27 в норме [замер, budget §6.2]. Пауза есть только при отказе precheck. `OOMScoreAdjust` не задан ни у
   одного юнита.
5. Для энергии и троттлинга нет телеметрии частот и мощности: в sysmon есть только температуры. На Nano DVFS по
   умолчанию при постоянном инференсе 83–89 % времени держит максимальные частоты, а ручная настройка CPU и GPU
   экономит до 19 % энергии [ист.: Dutt et al., IGSC 2023, Nano]. Поэтому `jetson_clocks` — предмет A/B на HIL, а
   не очевидный выигрыш.
6. Часть популярных советов **на Nano не работает**:
   - CUDA lazy loading: нужны CUDA ≥ 11.7 и драйвер ≥ 515;
   - rclpy EventsExecutor: появился в Kilted;
   - systemd `RestartSteps` (с v254) и systemd-oomd (cgroup v2 + PSI);
   - `MemoryHigh` на cgroup v1;
   - INT8 на Maxwell.

   Подробнее — в §4.

## 1. Плата ≠ стенд: что не переносится

| Аспект | Стенд (ВМ sbc/sim) | Jetson Nano (JetPack 4.6, Ubuntu 20.04 образа) | Следствие |
|---|---|---|---|
| Ядро | 6.8 | 4.9-tegra [ист.: L4] | нет PSI (с 4.20), нет `memory.peak` (с 5.19); у cgroup v2 нет контроллера cpu (с 4.15) — на Nano остаётся v1 |
| cgroup | v2 | v1/hybrid [`sysmon/README.md:155`] | критерий бюджета «PSI some > 5 %» на плате не посчитать; замены: `pgmajfault`, `pswpin/pswpout` из `/proc/vmstat`, tegrastats |
| systemd | 255 | 245 [deploy README] | `MemoryMax=`, `CPUWeight=` на v1 — «best-effort translation» [ист.: man v245]; `MemoryHigh/MemorySwapMax/MemoryLow` — «только unified»; надо проверить на роботе (§3, H5) |
| THP и Go | Go ≥ 1.21 на Linux ≥ 6.2 не трогает состояние huge pages [ист.: Go GC guide] | на 4.9 трогает; при THP `always` малые кучи — «до +50 %» памяти [ист.] | память Go-процессов на лестнице в ВМ может быть ниже, чем на плате |
| ROS / Python | Jazzy, 3.12, Cyclone | Foxy, 3.8, Fast DDS 2.1 | EventsExecutor (Kilted, есть экспериментально в Jazzy) на Foxy недоступен; поправка Fast DDS — только оценка (budget §6.1) |
| GPU | нет (балласт 1024 МиБ) | CUDA 10.2, Maxwell sm_53, память общая | память CUDA не видна в PSS [гипотеза в budget README]; пиннинг страниц и некогерентность (§3, M12) |
| Своп | выключен | zram ½ RAM, lzo [ист.: budget §1.1] | ступень «деградации до OOM» появится только на плате |

## 2. Сверка: что уже есть и насколько это адекватно

| Решение (где) | Вердикт | Почему |
|---|---|---|
| `GOMEMLIMIT` = 45 % `MemoryMax` на два Go-процесса, GOGC по умолчанию (`bench/board`) | адекватно | Go GC guide: при общей памяти с другими процессами GOGC=off не ставить, лимит — как «крышка», запас 5–10 %. Крайние ступени лестницы (16–24 МиБ) дадут «thrashing» с замедлением до 2×: CPU сборщика ограничен ≈50 % [ист.]. Это ожидаемо, но в трассе нужен признак срабатывания (M7) |
| `GOMAXPROCS` = ядра платы при квоте (`bench/board`) | адекватно | Go ≥ 1.25 урезал бы `GOMAXPROCS` по квоте cgroup; на плате квоты нет. Решение снимает артефакт эмуляции |
| SQLite: WAL, `synchronous=NORMAL`, `cache_size=-256` КиБ, одно соединение, `busy_timeout` 5 с (`internal/userctx/store.go:130`) | адекватно | документация SQLite. По Jeong et al. (ATC'13) и Kim et al. (FAST'12) именно режим журнала и синхронные записи определяют цену SQLite на флеше; WAL снижает число fsync. Нагрузка агента — единицы записей в минуту; дополнительные PRAGMA (`journal_size_limit`, `temp_store`) выигрыша не дадут |
| Трасса: буфер 64 КиБ, сброс ≤ 2 с, без fsync, таймер только при непустом буфере | адекватно | 1,5 МБ за 16-часовой день, 92 % байтов — записи `input`/`suppress` [замер]. Нужны для инварианта «один исход на вход», урезать не надо |
| Статический бинарник, `CGO_ENABLED=0`, `-trimpath -s -w`, GOARM64 не задан (по умолчанию v8.0) | адекватно | у A57 нет LSE (ARMv8.0); бинарник под v8.1+ упал бы (`bench/board` «Чего нет») |
| Модель бюджета 4 ГБ: MemTotal ≈3956, headless ≈420, GNOME ≈1–1,7 ГБ, CUDA ≈1 ГиБ, zram без кредита | адекватно | слагаемые с источниками; Jetson AI Lab для Orin Nano даёт тот же порядок: GUI ≈800 МБ. Консервативно — без zram |
| Квота CPU по 7-Zip MIPS (budget §2) | спорно (признано) | одиночный поток в slice в ≈3,8 раза быстрее A57; вывод о квоте не воспроизводится (ROADMAP §2 п. 1). Остаётся одно: `cpubench` на плате |
| Критерии «деградации» бюджета (PSI, `memory.events`) | спорно для HIL | на ядре 4.9 и cgroup v1 этих файлов нет. Для платы нужны заменители (§1) |
| perception: `Restart=always`, `RestartSec=1`, `StartLimitIntervalSec=0` | спорно | петля OOM: 96 убийств за ≈3,5 мин, CPU slice 2,56 ядра A57, троттлинг 48 % [замер]. `NEUROBOT_PRECHECK_BACKOFF_S` срабатывает только при отказе precheck (`neurobot_deploy/__main__.py:122`), не после смерти процесса |
| `OOMScoreAdjust` в юнитах | нет — пробел | при OOM ядро выбирает жертву по размеру. Приоритет «цепочка безопасности последней» не задан (H2) |
| Пульт: `MemoryMax=128M`, `MALLOC_ARENA_MAX=2`, `MALLOC_TRIM_THRESHOLD_`, `CPUWeight=20`, `Nice=10` | адекватно, но не проверено на v1 | арены дали рост 48 → 81 МиБ, и предел его снял [замер]. На роботе (v1) `MemoryMax` → `memory.limit_in_bytes` только через best-effort перевод — проверить |
| safety_gate подписан на глубину на полной частоте камеры, обрабатывает ≤ `depth_hz`=10 | спорно | DDS-копирование и десериализация оплачиваются за каждый кадр, ранний `return` их не экономит. README safety советует 424×240 при 15 к/с, но команда камеры в `neurobot.env.example:45` профиль не задаёт |
| Главные циклы `rclpy.spin_once(node, timeout_sec=0.1)` (safety, executor, perception) | спорно | в Foxy `spin_once(node)` на каждый вызов делает `add_node` + `remove_node`, оба взводят guard, набор ожидания пересобирается при каждом ожидании [ист.: исходник]. Пульт уже держит постоянный `SingleThreadedExecutor` (`ui/node.py:254`) |
| Наклон памяти: Тейл–Сен, батч-средние, блочный бутстреп, TOST; Манн–Кендалл — только разведочно (`harness/stats.py`) | адекватно | Machida et al. (2013) показали много ложных срабатываний Манна–Кендалла на «старении». Matias et al. (2014) — дифференциальный подход точнее тренда. Наклоны за 1 ч (до 2,8 МиБ/ч, PLAN §13.5) могут быть прогревом, не утечкой |
| journald persistent 200M, `ROS_LOG_DIR` в tmpfs | адекватно | журнал переживает перезагрузку, мусор rcl не пишется на SD |
| YOLO PyTorch FP16 (`.half()`), вход 320; TensorRT FP16 — опция (PLAN, R5) | адекватно как этап | выигрыш TensorRT по задержке подтверждён форумом и Seeed (R5). По памяти не гарантирован: cuBLAS + cuDNN + TensorRT ≈800 МБ на Jetson [ист.: сотрудник NVIDIA, 2019] |
| Мощность: INA3221 + внешний ваттметр, опрос ≤ 10 Гц (L4 §4) | адекватно | L4T: запас точности 5 % выше 200 мВт, до 15 % ниже. Dutt et al.: опрос раз в 100 мс стоит < 0,5 % энергии, раз в 10 и 1 мс — > 2 % |
| cam-mcp: YUYV без выделений, адаптивная частота 2/8 к/с | адекватно | меньше кадров — меньше CPU; выделений на кадр нет |
| sysmon: `smaps` вместо `smaps_rollup` на ядре < 4.14, режим `--pid` на cgroup v1 | адекватно | учтены особенности ядра 4.9 |

## 3. Рекомендации

Эффект — на ключевую метрику: RAM, CPU, задержки, энергия; вызовы, токены и ₽ эти пункты не меняют. «Nano/Foxy/3.8» —
применимость.

### Высокий приоритет

| # | Что | Сейчас | Эффект | Трудоёмкость | Nano / Foxy / 3.8 |
|---|---|---|---|---|---|
| H1 | **Пауза после смерти процесса**, прежде всего perception. В `ExecStartPre` (precheck) смотреть `NRestarts` и время прошлого старта юнита; при частых рестартах ждать по нарастающей: 5 → 15 → 30 с. Код и сигнал выхода — `ExecMainStatus`, `ExecMainCode`. Альтернатива — `RestartSec=3…5` только у perception | [нет]: пауза только при отказе precheck | снимает петлю OOM, отнимающую CPU у safety_gate: 2,56 против 1,27 ядра A57 [замер] | 0,5 д | да: `NRestarts` в systemd с v235. `RestartSteps` нет до v254. На v1 `Result=oom-kill`, вероятно, не будет: systemd узнаёт об OOM из `memory.events` cgroup v2 [оценка]. Опираться на SIGKILL и частоту |
| H2 | **`OOMScoreAdjust` по ролям**: safety и executor −900 (не −1000, чтобы их утечка не стала неубиваемой), драйверы −500, perception 0, rosbridge, mcp, picoclaw, ui +300…+500 | [нет] | при нехватке памяти первым умирает не цепочка движения. Особенно для ветки GNOME: запас 247–603 МиБ, при пессимистичной ОС дефицит 0,17 ГиБ (budget §6.1) | 1 строка на юнит + тест | да (ядро 4.9, systemd 245 — директива есть в `verify/systemd245-directives.txt`) |
| H3 | **Частоты камеры — в конфиг deploy**: глубина 424×240@15, или 848×480@15 + `decimation_filter`; цвет — @15 или по замеренному FPS perception; IMU — явные `gyro_fps`/`accel_fps` из допустимых для D435i. Имена параметров зависят от версии realsense-ros: 3.x — `depth_width/height/fps`, 4.x — `depth_module.profile`, `rgb_camera.profile` | [частично]: совет есть в README safety, в `neurobot.env.example:45` профиля нет | глубина 24,4 → 3,1 МБ/с (−87 %), цвет 27,6 → 13,8 МБ/с при 15 к/с [оценка]. Меньше CPU на DDS и rclpy у safety_gate (56 % ядра A57 [оценка через 7-Zip]) и perception | конфиг + прогон набора безопасности на двойнике (там уже 424×240@15) + перекалибровка `shock_mps2` по `/camera/imu` | да; меняет вход безопасности — только после прогона набора |
| H4 | **Постоянный исполнитель** вместо `rclpy.spin_once(node)` в safety, executor и perception: `ex = SingleThreadedExecutor(); ex.add_node(node); while …: ex.spin_once(timeout_sec=0.1)` (как в `ui/node.py`) | [нет] (пульт — [есть]) | меньше пересборок набора ожидания на каждый колбэк. Величина неизвестна — мерить `cpu.stat` юнита на двойнике до и после: в Jazzy тот же шаблон `spin_once` | ≈10 строк × 3 узла + тесты | да; EventsExecutor (до 10× по неформальным замерам) — только с Kilted |
| H5 | **Проверить реальность cgroup v1 на роботе** и дополнить `collect_diag.sh`, только чтение: `/proc/cgroups`, `mount \| grep cgroup`, `systemctl get-default`, `/proc/meminfo`, `/proc/vmstat`, `zramctl`, `/sys/block/zram*/{comp_algorithm,mm_stat}`, `vm.swappiness`, `vm.page-cluster`, `/sys/kernel/mm/transparent_hugepage/{enabled,defrag}`, cpufreq governor, `systemctl show -p MemoryLimit,CPUShares neurobot-ui` | [частично]: вопросы в budget §8, в скрипте их нет | без этого неизвестно, работают ли `MemoryMax`/`CPUWeight` юнитов на роботе и чем заменить PSI | 0,5 д | да |

### Средний приоритет

| # | Что | Сейчас | Эффект | Трудоёмкость | Nano / Foxy / 3.8 |
|---|---|---|---|---|---|
| M1 | **Частоты и мощность в sysmon**: `scaling_cur_freq` по ядрам, GPU `devfreq/cur_freq`, INA3221 `in_power0..2` (VDD_IN/GPU/CPU), 1–10 Гц | [частично]: температуры — да, частот и мощности нет; tegrastats — 60 с в diag | Вт·ч за день и видимый троттлинг в 8-часовом прогоне; дрейф K1 можно связать с частотой | 1 д | да; на Nano 2 ГБ INA3221 нет |
| M2 | **A/B `jetson_clocks` на HIL**. Гипотеза: во время манёвра детекция на паузе (L6), DVFS снижает частоты, первые кадры после паузы медленнее, отсюда хвост K1 p95. Мерить K1 p95, мощность и температуры. Запускать после nvpmodel (иначе нет узлов devfreq); с R32.4 вентилятор на максимум — только `--fan` | [нет] | при постоянной нагрузке выигрыш мал: DVFS и так на максимуме 83–89 % времени (Dutt 2023); цена — рост мощности в простое и нагрев | 0,5 д HIL | да; нужен вентилятор |
| M3 | **`MALLOC_ARENA_MAX=2`** (+ фиксированный `MALLOC_TRIM_THRESHOLD_`) для долгоживущих Python-юнитов: watchdog, mcp, executor, safety; perception — после A/B | [частично]: только пульт | наклон памяти K8: за 1 ч до 2,8 МиБ/ч у сторожа, 1,7 — rosbridge, 1,6 — perception [замер]. По умолчанию glibc держит до 8 × ядер арен на 64-бит | строки env + 2 прогона по 1 ч | да; в многопоточном perception возможна конкуренция за замок — мерить |
| M4 | **Пулы потоков**: в perception `torch.set_num_threads(2)` и `cv2.setNumThreads(1–2)`; в safety и executor `OMP_NUM_THREADS=1`, `OPENBLAS_NUM_THREADS=1` | [нет] | меньше переключений контекста на 4 × A57 рядом с safety_gate. Chakraborty et al. (2025): на Jetson узким местом часто бывают планирование потоков и переключения контекста на CPU | строки + замер `ctxsw` (есть в sysmon) и K1 | да |
| M5 | **Приоритет CPU цепочки безопасности**: `Nice=-5…-10` у safety и executor. SCHED_FIFO — только после замера джиттера тика: Python с RT-приоритетом может забрать ядро | [нет] (пульт понижен — [есть]) | меньше джиттер тика 20 Гц при перегрузке: YOLO, петля OOM. PiCAS (RTAS'21, Xavier NX) и обзор Casini et al. (2025/26) — приоритеты и привязка дают предсказуемую задержку цепочек (для rclcpp) | строки + гистограмма интервалов `/neurobot/safety_state` | да; `Nice` действует при любой cgroup, `CPUWeight` на v1 — через перевод |
| M6 | **Паузы GC Python**: `gc.callbacks` пишет длительность сборок gen2 в perception и safety. Если паузы ≥ 10 мс — `gc.freeze()` после прогрева или повышение порогов | [нет] | хвост задержек K1 и тика. Эффект не доказан: документация Python описывает `freeze` для fork, в обсуждении сомневаются в пользе для долгих процессов — сначала замер | 0,5 д | да: `freeze` с 3.7, `callbacks` с 3.3 |
| M7 | **Метрики рантайма Go в трассе** (`run_end` и периодически): `/cpu/classes/gc/total`, `/cpu/classes/total`, `/gc/cycles/total`, `/gc/limiter/last-enabled`, `/sched/pauses/total/gc`, `/memory/classes/total − heap/released` | [частично]: `num_gc` и куча через `ReadMemStats` в `/v1/metrics`; в `run_start` только параметры | на лестнице 16–64 МиБ видно, что замедление — «thrashing» GC (лимитер включился), а не логика агента. Это прямо объясняет метрику «во времени» | 0,5 д | не зависит от платы |
| M8 | **THP для Go-процессов**: если на плате `enabled=always`, то `GODEBUG=disablethp=1` для edgectx, hw-mcp, cam-mcp и PicoClaw. Ещё вариант — `khugepaged/max_ptes_none=0`, но это на всю систему. THP фиксировать в `env` прогонов лестницы | [нет] | RSS Go-процессов (агент 8–33 МиБ [замер]): при THP малые кучи «до +50 %» [ист.] | строка env | да; на ВМ 6.8 эффекта не будет (§1) |
| M9 | **Свой `MemoryMax`** у некритичных юнитов: picoclaw, mcp, rosbridge; у PicoClaw ещё `GOMEMLIMIT` | [частично]: только пульт | утечка упирается в свой предел, а не будит OOM для perception и safety | строки + H5 | на v1 — только после H5 |
| M10 | **zram**: оставить, swap на SD не переносить. Проверить алгоритм (lz4, если ядро 4.9 его даёт), `vm.page-cluster=0`; мерить `mm_stat` и `pswpin` под нагрузкой | [частично]: кредит в модели, на плате не мерили | запас памяти на ветке GNOME. Цена — CPU и задержка первого обращения. Ariadne (HPCA'25): zram не различает горячие и холодные данные и тратит CPU; это урок для горячих страниц perception | 0,5 д замер | да; советы сообщества по алгоритмам — [P], мерить самим |
| M11 | **Отключить лишние службы** на роботе: `nvargus-daemon` при USB-камере без CSI и прочее по `systemctl list-units`; плюс headless | [частично]: headless рекомендован | десятки МиБ и меньше пробуждений [оценка]; GUI на Orin Nano ≈800 МБ [ист.] | действие лаборатории | да |
| M12 | **Гигиена общей памяти в perception**: в `perception_health` — `torch.cuda.memory_allocated/reserved`. Не использовать `pin_memory`: на Nano (CC 5.3 < 7.2) pinned-память на CPU не кэшируется. Знать, что `CUDA_MODULE_LOADING=LAZY` недоступен | [нет] | видна GPU-часть RAM, которой нет в PSS; нет ложных «оптимизаций» | 0,5 д | да (torch 1.13: `memory_reserved`, `PYTORCH_CUDA_ALLOC_CONF` с `max_split_size_mb`, `garbage_collection_threshold`) |
| M13 | **TensorRT FP16 для YOLO**: `yolov5 export --include engine`, загрузка тем же `torch.hub`. Отдельный вариант с замером памяти: YOLO на чистом TensorRT, SNN на CPU, torch без CUDA | [частично]: опция в PLAN, R5 | задержка YOLO ≈120 → 47–70 мс по форуму и Seeed (R5). Память — [гипотеза]: −сотни МиБ возможны только без torch-CUDA; TensorRT сам тянет cuBLAS и cuDNN (≈800 МБ, NVIDIA 2019). `--tacticSources=-cudnn,-cublas` по форуму помогает не всегда | 2–3 д + тест эквивалентности (PLAN: IoU ≥ 0,95) | TensorRT 8.0/8.2 на JetPack 4.6; колесо cp38 есть в образе Qengineering — проверить образ лаборатории |

### Низкий приоритет

- **L1.** Родители `ros2 launch` занимают ≈25 МиБ каждый, всего ≈100 МиБ [замер, budget §6.1]. Одноузловые драйверы
  (камера, лидар) можно запускать сразу исполняемым файлом с параметрами. Выигрыш ≈50 МиБ [оценка], но обёртка
  супервизии рассчитана на launch. [нет]
- **L2.** Режим 5 Вт для робота не годится: 2 ядра × 918 МГц — это ≈31 % CPU режима MAXN, а стеку нужно ≈1,27 ядра A57
  на 1479 МГц, без предобработки YOLO [оценка]. Для сценария «только агент и датчики» это хороший эксперимент
  ВКР: MAXN против 5 Вт, Вт·ч за день против задержки ответа. [нет]
- **L3.** `/v1/metrics`: вместо `runtime.ReadMemStats` (он останавливает мир) брать `runtime/metrics`. При редких
  запросах эффект ничтожен. [нет]
- **L4.** Опции монтирования ext4 (`noatime`, `commit=`) **не менять**: `relatime` уже стоит по умолчанию. Больший
  `commit` увеличит потери при обрыве питания, а гипотеза об отсечке батареи (LAB_HANDOUT п. 6) делает это реальным
  риском. [решение: не делать]
- **L5.** Green Tea GC — по умолчанию с Go 1.26: −10–40 % накладных GC на программах с активной сборкой; +10 % —
  только на новых amd64 с векторными инструкциями [ист.: Go 1.26 release notes]. У нас Go 1.27 и малая куча: делать
  нечего, достаточно упомянуть в тексте ВКР. Отключатель по release notes 1.26 планировали убрать в 1.27. [есть
  автоматически]
- **L6.** Аллокации Go-агента не оптимизировать вслепую. Сначала профиль выделений за T0-день (`pprof`,
  `-benchmem` на пути «вход → гейт → трасса»); если доля CPU на GC из M7 < 2–3 %, трогать не нужно. [нет]

## 4. Не применимо на Nano / Foxy / Python 3.8

| Совет | Почему нет |
|---|---|
| `CUDA_MODULE_LOADING=LAZY` (экономит память CUDA-контекста) | нужны CUDA ≥ 11.7 и драйвер ≥ 515; на Nano CUDA 10.2 [ист.: CUDA Programming Guide] |
| rclpy `EventsExecutor` | экспериментально с Kilted (PR ros2/rclpy#1391); в Foxy нет |
| systemd `RestartSteps`/`RestartMaxDelaySec` | с v254; на Ubuntu 20.04 — 245 (deploy README) |
| systemd-oomd | появился в v247, требует cgroup v2 и PSI (ядро ≥ 4.20) |
| `MemoryHigh=`, `MemorySwapMax=`, `MemoryLow=` | по man v245 — «only if the unified control group hierarchy is used» |
| Своп-файл на SD вместо zram (Jetson AI Lab для Orin) | там своп на NVMe. На SD — износ и задержки (JetsonHacks, форум NVIDIA). Для непрерывной работы робота — нет |
| INT8 TensorRT | Maxwell sm_53 без нативного INT8 (R5) |
| Когерентная pinned-память и `cudaHostRegister` | I/O-когерентность — с CC 7.2 (Xavier); на Nano pinned-память не кэшируется на CPU [ист.: CUDA for Tegra] |
| Готовые образы jetson-containers и NanoLLM | только r35/r36 (L4 §3.4) |

## 5. Что мерить на HIL (рантайм)

Дополнение к PLAN §6 и budget §8. Всё — на одном расписании жестов, 1–2 ч, затем 8 ч.

1. До старта стека: H5 (cgroup, zram, THP, governor, `get-default`), `nvpmodel -q`, `jetson_clocks --show`.
2. Каждую секунду: tegrastats или sysmon с M1 — частоты, мощность, температуры; `/proc/vmstat` (`pgmajfault`,
   `pswpin`, `pswpout`); PSS по процессам (`smaps`; на ядре 4.9 `smaps_rollup` нет).
3. Пары A/B, по одному фактору: `spin_once` против постоянного исполнителя (H4); частоты камеры (H3); `jetson_clocks`
   (M2); `MALLOC_ARENA_MAX` (M3); потоки (M4). Отклик: K1 p50/p95, CPU юнитов, джиттер тика safety, мощность.
4. Наклон памяти — `harness/stats.py`: Тейл–Сен по батч-средним после прогрева, ДИ блочным бутстрепом и TOST.
   Манн–Кендалл — только разведочно (Machida 2013). Сравнивать A/B «до и после правки», а не один тренд
   (Matias 2014).
5. Провокация OOM на роботе — только при снятых колёсах: проверить H1 и H2. Должно умирать некритичное, а пауза
   рестарта не должна отнимать ядро у safety.

## 6. Не найдено или не проверено

- Рецензируемого сравнения rclpy и rclcpp по CPU и задержке не нашёл. Есть только форумы, бенчмарк rclnodejs и
  неформальные замеры EventsExecutor. Цена `add_node`/`remove_node` в `spin_once` нигде не измерена — мерить самим.
- Отключает ли `jetson_clocks` на Nano railgating GPU и cpuidle, в документации L4T для Nano не описано.
- Есть ли lz4 у zram в ядре L4T 4.9, какой THP по умолчанию в L4T и включён ли memcg — проверять на плате (H5).
- Сколько памяти даёт YOLO на TensorRT вместо PyTorch на Nano — чисел не нашёл, только оценки форума.
- Рецензируемых работ 2024–2026 именно про DVFS и инференс на **оригинальном** Nano, кроме Dutt et al. 2023, не нашёл.
  Работы 2025 (Prashanthi et al. — обучение; arXiv 2509.17970 — TX1 и Orin Nano) к нашему случаю переносятся
  только качественно.
- Профили Fast DDS и Cyclone на Foxy для изображений не сравнивал: это направление трафика и DDS, не O2.

## 7. Источники

Документация и исходники:
1. NVIDIA. Jetson Linux R32.7.5 Developer Guide — Power Management for Jetson Nano and Jetson TX1 Devices (nvpmodel,
   jetson_clocks, термозоны: SW-троттлинг CPU 97 °C, HW 100,5 °C, вентилятор с 51 °C; OC/UV-троттлинг; INA3221).
   https://docs.nvidia.com/jetson/archives/l4t-archived/l4t-3275/Tegra%20Linux%20Driver%20Package%20Development%20Guide/power_management_nano.html
2. NVIDIA. CUDA for Tegra Application Note (CUDA 10.2). https://docs.nvidia.com/cuda/archive/10.2/cuda-for-tegra-appnote/index.html
3. NVIDIA. CUDA Programming Guide — Lazy Loading. https://docs.nvidia.com/cuda/cuda-programming-guide/04-special-topics/lazy-loading.html
4. NVIDIA Developer Forums: «Very large CPU RAM usage in TensorRT» (2019). https://forums.developer.nvidia.com/t/very-large-cpu-ram-usage-in-tensorrt/79158 ;
   «cuBLAS, cuDNN and TensorRT memory release on Jetson nano» (JetPack 4.6). https://forums.developer.nvidia.com/t/cublas-cudnn-and-tensorrt-memory-release-on-jetson-nano/193023 ;
   «Jetson_clocks only works when started > 60 seconds after boot». https://forums.developer.nvidia.com/t/jetson-clocks-only-works-when-started-60-seconds-after-boot-why/148207
5. NVIDIA Jetson AI Lab. RAM Optimization (Orin Nano). https://www.jetson-ai-lab.com/tutorials/ram-optimization/
6. Qengineering. Jetson-Nano-Ubuntu-20-image (PyTorch 1.13, TensorRT 8.0.1.6, headless ≈420 MB). https://github.com/Qengineering/Jetson-Nano-Ubuntu-20-image
7. Go team. A Guide to the Go Garbage Collector (GOMEMLIMIT, «Suggested uses», THP). https://go.dev/doc/gc-guide
8. Go. Package runtime/metrics. https://pkg.go.dev/runtime/metrics ; Go 1.26 Release Notes (Green Tea GC). https://go.dev/doc/go1.26
9. systemd v245. systemd.resource-control(5). https://github.com/systemd/systemd/blob/v245/man/systemd.resource-control.xml
10. ros2/rclpy, ветка foxy: `rclpy/__init__.py` (`spin_once`), `rclpy/executors.py` (`add_node`/`remove_node`, набор
    ожидания). https://github.com/ros2/rclpy/tree/foxy/rclpy/rclpy
11. ROS 2 Kilted Kaiju release notes (rclpy EventsExecutor, PR #1391). https://docs.ros.org/en/kilted/Releases/Release-Kilted-Kaiju.html
12. Python 3.8 — gc. https://docs.python.org/3.8/library/gc.html ; обсуждение «It seems to me that gc.freeze() is
    pointless…». https://discuss.python.org/t/it-seems-to-me-that-gc-freeze-is-pointless-and-the-documentation-misleading/71775
13. glibc manual — Memory Allocation Tunables (`glibc.malloc.arena_max`, `trim_threshold`). https://sourceware.org/glibc/manual/latest/html_node/Memory-Allocation-Tunables.html ;
    mallopt(3) (`M_ARENA_TEST`: 8 на 64-бит).
14. PyTorch 1.13 — CUDA semantics, Memory management. https://docs.pytorch.org/docs/1.13/notes/cuda.html
15. ROS 2 design. Node to Participant mapping. https://design.ros2.org/articles/Node_to_Participant_mapping.html

Статьи:
16. Dutt A., Rachuri S. P., Lobo A., Shaik N., Gandhi A., Liu Z. Evaluating the energy impact of device parameters for
    DNN inference on edge. IGSC 2023, pp. 52–55. DOI 10.1145/3634769.3634809. https://par.nsf.gov/biblio/10526477
    (Nano, JetPack 4.6.3: DVFS schedutil и nvhost_podgov на максимуме 83–89 % времени; до −19 % энергии ручной
    настройкой; частота GPU влияет сильнее частоты CPU.)
17. Liang Y., Shen A., Xue C. J., Pan R., Mao H., Mansouri Ghiasi N., Jiang Q., Nadig R., Li L., Ausavarungnirun R.,
    Sadrosadati M., Mutlu O. Ariadne: A Hotness-Aware and Size-Adaptive Compressed Swap Technique for Fast Application
    Relaunch and Reduced CPU Usage on Mobile Devices. HPCA 2025 (расширенная версия — arXiv:2502.12826).
18. Chakraborty A., Tavernier W., Kourtis A., Pickavet M., Oikonomakis A., Colle D. Profiling Concurrent Vision
    Inference Workloads on NVIDIA Jetson — Extended. arXiv:2508.08430, 2025.
19. Prashanthi S. K., Kesanapalli S. A., Simmhan Y. Characterizing the Performance of Accelerated Jetson Edge Devices
    for Training Deep Learning Models. SIGMETRICS 2023; arXiv:2509.20160 (обучение; режимы питания и общая память).
20. Choi H., Xiang Y., Kim H. PiCAS: New Design of Priority-Driven Chain-Aware Scheduling for ROS2. RTAS 2021
    (Xavier NX, SCHED_FIFO по исполнителям). https://intra.ece.ucr.edu/~hyoseung/pdf/rtas21_picas.pdf
21. Casini D., Chen J.-J., Li J., Reghenzani F., Teper H. A Survey of Real-Time Support, Analysis, and Advancements in
    ROS 2. arXiv:2601.10722 (v1 22.12.2025, v2 03.04.2026).
22. Kronauer T., Pohlmann J., Matthé M., Smejkal T., Fettweis G. Latency Analysis of ROS2 Multi-Node Systems. MFI 2021;
    arXiv:2101.02074 (задержка сильно зависит от DDS; до ≈50 % накладных ROS 2 над DDS).
23. Jeong S., Lee K., Lee S., Son S., Won Y. I/O Stack Optimization for Smartphones. USENIX ATC 2013, pp. 309–320.
24. Kim H., Agrawal N., Ungureanu C. Revisiting Storage for Smartphones. USENIX FAST 2012.
25. Machida F., Andrzejak A., Matias R., Vicente E. On the Effectiveness of Mann-Kendall Test for Detection of Software
    Aging. ISSREW 2013, pp. 269–274. DOI 10.1109/ISSREW.2013.6688905.
26. Matias R., Andrzejak A., Machida F., Elias D., Trivedi K. A Systematic Differential Analysis for Fast and Robust
    Detection of Software Aging. SRDS 2014, pp. 311–320. DOI 10.1109/SRDS.2014.38.

Вторичные [P]: Bit-Bots, «Speeding up Python nodes with the Bit-Bots TF Buffer» (ROS Discourse) — о медленном rclpy;
обсуждения zram-алгоритмов (Fedora, Armbian) — только как направление для своего замера.
