# O6. Непрерывная работа и наблюдаемость: что проверено и что улучшить

Срез: 20.10.2026. Только исследование: код и конфиги не менялись. Обозначения: [есть] / [частично] / [нет];
[код] — прочитано в коде, [замер] — артефакты проекта, [пересчёт] — посчитано мной по сырым артефактам,
[ист.] — внешний источник, [оценка] — без замера. Всё про стенд — ориентир, не результат робота.

Что прочитано: `neurobot/deploy` (README, RUNBOOK, `units/*`, `neurobot_deploy/watchdog.py`, `checks.py`,
`neurobot.env.example`), `neurobot/sim/runs` (`metrics.py`, `runner.py`, `schedule.py`), `docs/neurobot/PLAN.md`
§1, §5, §6, §13.5, `R5_external.md` §3, `R6_sim.md` §6–7, `sysmon/README.md`, итоговые прогоны
`experiments/results/neurobot_20261018_acceptfix2/runs/{lab_v5,long_v5}`.
Пересечения с соседними файлами: пауза рестарта, `OOMScoreAdjust`, `MALLOC_ARENA_MAX`, частоты и мощность в sysmon
уже разобраны в `O2_edge_runtime.md` (H1, H2, M3, M1). Здесь — только ссылки на них и то, что добавляет O6.

## 0. Главное

1. **Архитектура супервизии в целом соответствует литературе.** Это crash-only и микроперезагрузка компонентов
   (Candea и Fox 2003, Candea et al. 2004): systemd, готовность по READY, сторож по прогрессу, обёртка launch,
   защёлка на диске, манёвр после рестарта не возобновляется. Переделывать её не нужно.
2. **Счёт «видимых перезапусков» (K3) занижен.** Отказ в расписании ставится через 1 с после конца жеста, то есть во
   время манёвра. Код относит к отказу только жесты, закончившиеся после его начала, поэтому оборванный манёвр
   предыдущего жеста не засчитывается. А по PLAN §1 это видимый перезапуск («движение оборвалось на середине
   команды»). [пересчёт] В `long_v5` так оборваны манёвры при 10 из 31 отказа, в `lab_v5` — при 8 из 24. Сейчас
   там видимыми записаны 3 и 2 отказа.
3. **TTR в K5 смешивает длительность внесённого отказа и восстановление.** Для отказов камеры 4–7 с из
   7,8–11,2 с — само удержание отказа. TTD не считается, хотя PLAN §6 его требует. Спонтанные рестарты без
   внесённого отказа не классифицируются вовсе — а на HIL других и не будет.
4. **Дешёвые дыры в надзоре:** у самого сторожа нет `WatchdogSec`. Зависший `twist_mux` можно ловить по
   `/diagnostics`: он публикует их раз в 1 с даже в Foxy. Свежесть камеры сторож проверяет по сырым кадрам —
   это лишняя нагрузка (probe effect, ros2probe 2026).
5. **eBPF и ros2_tracing на Nano/Foxy практически неприменимы.** В стоковом ядре L4T нет `bpf()`, а в Foxy нет
   трассировочных точек для узлов rclpy. Дешёвая наблюдаемость на ядре 4.9 — это procfs (`schedstat`, delay
   accounting), `/diagnostics` драйверов и sysmon.

## 1. Адекватность текущих решений

| Решение | Вердикт | Почему | Источник |
|---|---|---|---|
| systemd: `Restart=always`, `WatchdogSec` по прогрессу (safety 3 с, executor 3 с, perception 5 с), SIGABRT + `PYTHONFAULTHANDLER` | адекватно | Микроперезагрузка компонента вместо перезагрузки всего. faulthandler по SIGABRT пишет стек Python в журнал — диагностика зависаний уже есть | Candea et al. OSDI 2004; Candea, Fox HotOS 2003 |
| Готовность по READY и `ExecStartPost`-пробам, `After=` без `Requires=` | адекватно | «Порядок по готовности» вместо `sleep`. Без `Requires=` упавшая зависимость не блокирует рестарт навсегда. 101 из 101 холодного старта [замер] | systemd.service(5) |
| Обёртка `supervised.launch.py`: смерть узла → гасим весь launch → рестарт юнита | адекватно | Рекурсивная перезапускаемость на уровне launch. Перезапуск одного узла через respawn ломал бы порядок spawner. Цена — рестарт всего launch базы: 7–9 с на двойнике [замер] | Candea, Fox HotOS 2001 |
| Защёлка в `StateDirectory`, манёвр после рестарта не возобновляется | адекватно | RORY (ICRA 2021) показал, что «просто рестарт» с потерей или частичным состоянием даёт опасное поведение. У нас безопасное состояние после рестарта — «стоять», а опасное состояние (защёлка) хранится на диске | Zou, Bai ICRA 2021 |
| Внешний сторож по свежести тем и ping rosbridge | адекватно по идее, спорно в деталях | Ловит «жив, но не работает», чего не видят systemd и launch. Но: (а) подписка на сырые кадры камеры — лишняя нагрузка; (б) самого сторожа никто не сторожит; (в) `twist_mux` без темы свежести не виден (§3) | ros2probe arXiv:2606.10746 |
| «Молчат все темы — никого не трогаем» | адекватно | Общая причина (DDS, сам сторож): рестарты не умножаются | — |
| Проба MCP `initialize` + `tools/list` вместо вызова инструмента | адекватно | Не дёргает сервисы rosbridge 1.3.1, которые текут в цикле | `R7`, M11 |
| Сторож — сервис, а не таймер | адекватно | На systemd 245 таймер раз в 10 с даёт ≈17 тыс. строк в сутки | [код] |
| `StartLimitIntervalSec=0` и пауза только при отказе precheck | спорно | Петля рестартов perception (CUDA ≈1 ГиБ на попытку) ничем не тормозится. Подробно — O2 H1. Kubernetes делает 10 → 20 → 40 с … ≤ 300 с со сбросом после 10 мин работы | O2; kubernetes.io Pod Lifecycle |
| Определение «видимого перезапуска» (PLAN §1) | адекватно | Это пользовательский SLI: доля «хороших событий» среди валидных, по наблюдаемому выходу системы, а не по живости процессов | SRE Workbook «Implementing SLOs»; Basiri et al. 2016 |
| Реализация K3 в `runs/metrics.py` | ошибочно (занижает) | §2.1 | [код], [пересчёт] |
| TTR = от начала отказа до «роль снова работает» | спорно | Включает удержание отказа; для агентского пути это живость процесса, а не обслуживание; TTD нет (§2.2) | Avizienis et al. 2004; Chen, Toueg, Aguilera 2002 |
| Итог «0 видимых» как критерий | спорно | 0 из n без границы ничего не говорит о вероятности. При n = 28 верхняя 95-процентная граница ≈ 3/28 ≈ 11 % | Hanley, Lippman-Hand 1983 |
| Наклон памяти: МНК по всему окну, рестарты внутри ряда | спорно | Рестарт процесса — ступень в ряду: МНК по ряду со ступенями смещён. В `long_v5` perception перезапускался 3 раза, а его наклон 1,6 МиБ/ч посчитан по всему ряду | Grottke et al. 2006; O2 (Machida 2013) |
| Пороги сторожа (2 с, две пробы по 1 с) | [допущение] — пока адекватно | 15 из 15 SIGSTOP подняты за 4,5–4,9 с, ложных рестартов 0 за ≈1 ч [замер]. Для робота нужны TTD и частота ложных срабатываний за 8 ч | Chen, Toueg, Aguilera 2002 |

## 2. Правильно ли мы считаем «видимый перезапуск» и TTR

### 2.1. Видимый перезапуск: три расхождения кода с PLAN §1

[код] `fault_rows` (`neurobot/sim/runs/metrics.py:206–279`):
```python
hit = [s for s in segs if s["kind"] == "gesture" and s["t_leave"] >= start and s["t_start"] <= stop]
lost = [... reaction in LOST_REACTIONS ...]          # LOST_REACTIONS = ("none", "interrupted")
```

1. **Оборванный манёвр предыдущего жеста не засчитывается.** `schedule.py`: «отказ ставится через 1 с после
   последнего бокса сегмента». Значит, в момент отказа робот обычно выполняет манёвр (до 11 с). У такого жеста
   `t_leave < start`, и в `hit` он не попадает.
   [пересчёт по `metrics.json`, `gestures.jsonl`, `truth_schedule.jsonl`] Жест, закончившийся ≤ 3 с до отказа, имеет
   реакцию `interrupted`:

   | Прогон | Отказов | Таких случаев | Без участия safety_gate | Остановлен safety_gate по `stale_*` датчика, который выключил сам отказ | Сейчас видимых |
   |---|---|---|---|---|---|
   | `long_v5` | 31 | 10 | 3 (kill_perception ×2, kill_executor) | 7 (камера, лидар, safety, база) | 3 |
   | `lab_v5` | 24 | 8 | 3 (kill_perception, kill_executor, kill_agent — вероятно, не связан) | 5 | 2 |

   Защёлки от смерти базы уже учтены: в `long_v5` одна из 10 — kill_base. Значит, по букве PLAN видимых в `long_v5`
   может быть до 12 из 31, а не 3. Остановка защитой по пропавшему датчику — штатное безопасное поведение. Но
   движение при этом оборвано, а значит, отказ виден.
   Что сделать (0,5–1 д, только стенд/анализ):
   - окно отказа должно захватывать жесты, у которых `[t0, t0 + REACTION_WINDOW_S]` пересекает
     `[start, stop]`;
   - причины видимости разделить: «оборван манёвр», «потерян жест», «нужен человек», «оборван защитой по
     датчику отказа»;
   - доля отказов, пришедшихся на манёвр, у расписания искусственно высокая: её надо писать в отчёт.
     Расписание не менять — это честный стресс.
2. **«Поздно» не засчитывается.** PLAN требует верной реакции за 3 с. Наблюдатель считает `late`, а `fault_rows`
   его не использует. В итоговых прогонах `late = 0`, так что на числа это не повлияло. На роботе перезапуск
   perception длится 5–40 с (CUDA), и расхождение станет заметным.
3. **Классифицируются только внесённые отказы.** Рестарты systemd и сторожа вне расписания (`NRestarts` больше
   числа отказов, ложный рестарт сторожа) не проверяются на видимость. На HIL и 8-часовом прогоне без инъекций K3
   будет «0» по построению. Нужно строить события рестартов по журналу: `Main process exited`, `Watchdog timeout`,
   строки сторожа `sensor_stale` / `rosbridge_down`. Затем применять к ним те же правила.

Кроме того: `visible_failures` наблюдателя (42 в `long_v5`) и K3 (3) — разные метрики, и в тексте ВКР их легко
смешать. Предложение: SLI «верная реакция ≤ 3 с» считать отдельно в окнах отказов и вне окон. Разница даёт потери,
вызванные отказами, — и учитывает «неверную реакцию» после заморозки камеры статистически, без ручной атрибуции
(SRE Workbook: SLI = хорошие / валидные события).

### 2.2. TTR и TTD

[код] `runner.watch_recovery`: TTR отсчитывается от начала отказа, для perception — до
`progress_since(t0 + hold_s)`, то есть не раньше конца удержания отказа.

| Отказ | Удержание | TTR в сводке (A1 итог) | После снятия причины [пересчёт] |
|---|---|---|---|
| camera_delay | 7 с | 8,4–8,6 с | ≈1,4–1,6 с |
| camera_freeze | 6 с | 7,8 с | ≈1,8 с |
| camera_black | 4 с | 11,2 с | ≈7,2 с — медленно, стоит разобрать |
| stop_perception | 8 с | 9,8 с | systemd убивает на 5 с; конец удержания маскирует реальное время подъёма |
| SIGSTOP драйверов | 30 с | 4,4–5,2 с | не завышено: поднимает сторож |

Предложение (0,5 д, стенд/анализ): для каждого отказа писать четыре величины:
- **TTD** — от отказа до действия супервизора, по меткам журнала (`Watchdog timeout`, `Main process exited`,
  строка сторожа);
- **TTR от снятия причины**;
- **длительность недоступности** роли — это и есть то, что видит пользователь;
- **время до первого обслуженного запроса**: жест с верной реакцией или успешная реплика. Для агентского пути
  нынешний «процесс жив, порт отвечает» — это живость, а не обслуживание (Avizienis et al. 2004:
  восстановление — возврат к правильному обслуживанию).

Распределения — p50/p95/max по типу отказа, без средних: Davidovič (Google, 2021) показал, что MTTR при скошенных
распределениях и малых выборках не годится для решений. Для «0 видимых» нужно писать n и верхнюю 95-процентную
границу ≈ 3/n. Невосстановившиеся отказы — цензурированные наблюдения, а не «TTR = длина прогона».

Для времени: сумма недоступности пути жеста за час (площадь «треугольника устойчивости», Bruneau et al. 2003) —
одна понятная величина «во времени», совместимая с ключевой метрикой ВКР.

## 3. Рекомендации

| # | Что | Статус | Эффект на метрику | Трудоёмкость | Nano / Foxy / Py 3.8 | Приоритет | Источники |
|---|---|---|---|---|---|---|---|
| C1 | Исправить K3: окно по реакции, `late`, разбиение причин; рестарты вне расписания — из журнала (§2.1) | [частично] | честный K3; ожидаемо видимых больше (до 12 из 31 в `long_v5`) | 0,5–1 д + пересчёт `compare.md` из сырых данных | анализ на Mac | высокий | PLAN §1; SRE Workbook |
| C2 | TTR → TTD + TTR после снятия причины + недоступность + первый обслуженный запрос; p50/p95/max, 3/n для нулей (§2.2) | [частично] | K5 перестаёт приписывать восстановлению длительность отказа камеры | 0,5 д | анализ | высокий | Davidovič 2021; Hanley 1983; Avizienis 2004 |
| C3 | `WatchdogSec=30` у `neurobot-watchdog`, `WATCHDOG=1` из главного цикла (Notifier уже есть: `neurobot/executor/sd_notify.py`); `Type=notify` с READY после первой пробы | [нет] | зависший сторож (дедлок rclpy/DDS) сейчас молча оставляет драйверы без надзора | 1–2 ч + тест | да (sd_notify без libsystemd, v245) | высокий | systemd.service(5) |
| C4 | Свежесть `twist_mux` по `/diagnostics`: в Foxy (twist_mux 4.0.1) он публикует «Twist mux status» таймером раз в 1 с в том же исполнителе, поэтому SIGSTOP или дедлок останавливают и диагностику | [нет] (README: «сторож не видит SIGSTOP twist_mux», проба отложена на пакет 2) | закрывает известную дыру без пакета 2; TTD ≈ 3 с | 2–4 ч: в FRESH — тема с фильтром по `status.name` | да | высокий | twist_mux foxy-devel: `DIAGNOSTICS_PERIOD = 1s`, `twist_mux_diagnostics.cpp` |
| C5 | Свежесть камеры по `/camera/color/camera_info:2` вместо `image_raw` (уже предложено в README, не сделано). Альтернатива — `/diagnostics` realsense (`diagnostics_period` > 0, частоты потоков) | [частично] | −≈14 МБ/с копирования кадров в Python-сторож [оценка проекта]; возможно, и наклон памяти сторожа 2,8 МиБ/ч [гипотеза] | одна строка env + повтор `recovery.py` на двойнике | да; `diagnostics_period` есть в realsense-ros 4.51.1 (Foxy) | высокий | ros2probe (probe effect: на RPi 4 `ros2 topic hz` — 26–42 % CPU под нагрузкой); realsense-ros README |
| C6 | Наклон памяти по сегментам жизни PID: рестарт = разрыв ряда; Тейл–Сен по батч-средним, ДИ блочным бутстрепом, TOST — всё уже есть в `harness/stats.py`; к индикаторам старения добавить число fd (`/proc/<pid>/fd`) | [частично]: `metrics.memory()` — МНК по всему окну; fd не собираются | K8 перестаёт смешивать рестарты и утечку; fd ловят утечки сокетов rosbridge/MCP | 0,5 д | да | высокий | Grottke et al. 2006 (Манн–Кендалл, Сен, сезонность); Cotroneo, Cukic 2026 (автокорреляция, множественные сравнения); O2 |
| C7 | Пауза для петли рестартов: первый рестарт сразу (TTR), дальше по нарастающей, сброс после ~10 мин работы | [нет] | без влияния на одиночный отказ; убирает петлю CUDA-инициализаций | 0,5 д | да (`NRestarts` с v235) | средний | **O2 H1**; kubernetes.io (10 → 300 с, сброс через 10 мин) |
| C8 | `OOMScoreAdjust` по ролям | [нет] | при нехватке памяти первой умирает не цепочка движения | минуты | да | средний | **O2 H2** |
| C9 | Качество сторожа как детектора отказов: TTD по классам и частота ложных рестартов в час на 8 ч без инъекций — предрегистрировать предел | [частично]: «за ≈1 ч 0 лишних» | обоснование порогов 2 с / 2 пробы на роботе | входит в 8-часовой прогон | да | средний | Chen, Toueg, Aguilera 2002 (T_D, T_MR) |
| C10 | Ресурсный журнал на роботе для 8 ч/HIL: юнит sysmon `--pid` (cgroup v1) раз в 30 с + частоты и мощность (O2 M1); проверить `smaps_rollup` | [частично]: юнита sysmon в deploy нет; tegrastats — 60 с в `collect_diag.sh` | без этого K8 и дрейф K1 на роботе не измерить | 0,5 д | да. `smaps_rollup` есть в ядре L4T r32.7.4+ и отсутствует в ≤ r32.7.3 (по исходникам). Без него — построчный smaps; у процесса с CUDA это дорого: на Android — сотни мс на большой процесс | средний | LKML smaps_rollup 2017; OE4T linux-tegra-4.9 |
| C11 | Дешёвые счётчики ядра 4.9 в sysmon: `/proc/<pid>/schedstat` (время в очереди на CPU), delay accounting (ожидание IO и свопа zram) | [нет] | объясняет хвост K1 и тика под нагрузкой без трассировки; на 4.9 нет PSI — это его аналог на уровне задачи | 0,5 д | да: в стоковом `tegra_defconfig` `SCHEDSTATS=y`, `TASK_DELAY_ACCT=y`, `TASKSTATS=y`; накладные — чтение procfs [оценка] | средний | L4T `tegra_defconfig` r32.7.5 |
| C12 | Омоложение по условию, а не по таймеру (`RuntimeMaxSec`): плановый рестарт агентского пути (rosbridge, MCP, PicoClaw, сторож) в простое (робот стоит, нет реплики и жеста N с), если PSS выше базовой линии на X МиБ. Perception — только при подтверждённом наклоне | [нет] | K8 на 8 ч и выше; для жестов невидимо: рефлекс идёт мимо rosbridge | 0,5–1 д | да | средний — **только если** 8-часовой прогон покажет наклон > предела | Huang et al. 1995; Vaidyanathan, Trivedi 2005; Candea et al. 2004 |
| C13 | «Старение» агента: тренд входных токенов на вызов и размера сессий PicoClaw за 8 ч (Сен), служебные вызовы сжатия истории в час; политика ротации сессий | [частично]: служебные вызовы считаются (`service_calls`) | прямо ключевая метрика: токены и ₽ во времени | 2–4 ч анализа | да | средний | Zhu et al. 2026 (AgingBench) |
| C14 | Согласовать лестницу восстановления: собственное переподключение драйвера должно успевать раньше рестарта сторожем. Сейчас realsense `reconnect_timeout` 6 с (по умолчанию), а сторож рестартует через 2 с + 2 пробы; у urg_node2 `error_limit` 4 / `error_reset_period` 5 с | [нет] | меньше лишних рестартов USB-камеры при коротком сбое; проверять только на HIL с выдёргиванием USB | 2 ч + HIL | да (параметры есть в Foxy-версиях) | низкий | Candea, Fox 2001 (рекурсивная перезапускаемость: сначала минимальный компонент); realsense-ros 4.51.1; urg_node2 README |
| C15 | `MALLOC_ARENA_MAX=2` и фиксированный порог trim для Python-сервисов | [частично] (только пульт) | наклон памяти | минуты + A/B 1 ч | да | низкий | **O2 M3**; mallopt(3): явный порог выключает динамический mmap-порог |
| C16 | Аппаратный сторож платы (`RuntimeWatchdogSec` в system.conf + tegra-wdt) — только решением лаборатории | [нет] | от «вечного висяка» платы; цена — видимая перезагрузка | 1 ч + проверка на плате | частично: в defconfig `TEGRA21X_WATCHDOG=y`, `WATCHDOG_NOWAYOUT=y`; по форуму — путаница с таймаутом (expiry-count) и может понадобиться узел DT. Ядро уже паникует и перезагружается через 5 с при soft/hard lockup (`*LOCKUP_PANIC=y`, `PANIC_TIMEOUT=5`) | низкий | NVIDIA Forums 2021; L4T defconfig |
| C17 | `py-spy dump --pid` для «жив, но не работает» на HIL (без изменения кода) | [нет] (faulthandler по SIGABRT есть) | быстрее причина зависаний perception и rosbridge | 0 | Py 3.8 — да; нужен root (ptrace) | низкий | py-spy README |

## 4. Что не применимо к Nano / Foxy / Python 3.8 (и почему)

| Идея | Почему нет | Проверка |
|---|---|---|
| eBPF: bcc, bpftrace, ros2probe, трассировщики на eBPF-uprobes | В стоковом `tegra_defconfig` L4T r32.7.5 `CONFIG_BPF_SYSCALL` не задан, по умолчанию в Kconfig 4.9 — `n`. В конфиге 4.9.140 нет и `KPROBES`/`UPROBES`. Нужна пересборка ядра — не для лаборатории. ros2probe мерили на ядре 5.15 и Humble | OE4T `l4t-r32.7.5-base`: `tegra_defconfig`, `init/Kconfig`; gist 4.9.140; NVIDIA Forums 2022 |
| ros2_tracing для наших узлов | В Foxy из deb не работает «из коробки»: нужна сборка rcl/rclcpp с LTTng. Трассировочные точки Foxy — только инициализация и `callback_start`/`callback_end` rclcpp. rclpy не зависит от `tracetools` ни в одной ветке. Для Python-узлов на Foxy трасса почти пуста. Это закрывает открытый вопрос O7 | README ros2_tracing (foxy, rolling); `tracetools.h` foxy; `rclpy/package.xml` |
| Lifecycle-узлы на rclpy | Модуль `rclpy.lifecycle` появился в Humble (PR #865, слит 23.12.2021); в ветке foxy его нет. Lifecycle — только у C++-узлов лаборатории (urg_node2 уже lifecycle, auto_start) | GitHub rclpy |
| greenwave_monitor, ros2_pulse | Только Humble+ | index.ros.org; README ros2_pulse |
| DDS QoS deadline/liveliness для слежения за драйверами | Издатель с Default и подписка с конечным deadline несовместимы: связь не установится. Нужно менять QoS драйверов лаборатории | ROS 2 Foxy docs, QoS compatibilities |
| PSI, systemd-oomd, `MemoryHigh` | PSI — с ядра 4.20; oomd требует PSI и cgroup v2; `MemoryHigh` работает только на unified. `MemoryMax` на v1 systemd 245 переводит «best effort» в `memory.limit_in_bytes`. Замена PSI на 4.9 — delay accounting (C11) или `memory.pressure_level` memcg v1 | systemd v245 `systemd.resource-control.xml` |
| ROSA, SUAVE, MAPE-K-фреймворки с базой знаний | TypeDB, BT, PDDL — тяжело для Nano и избыточно: нам нужен рестарт, а не перепланирование архитектуры | Silva et al. 2025; Sartaj et al. 2025 |
| Checkpoint-replay (RORY) | Для робота безопаснее «стоять после рестарта», чем воспроизводить команды | Zou, Bai 2021 |
| ML-детекторы старения (ADWIN и т. п.) | Для 8-часового прогона с фиксированным расписанием достаточно Тейла–Сена с ДИ; ML — для смены нагрузки в эксплуатации | Silva et al. SSCAD 2025 |

## 5. Порядок работ

1. C1 + C2: пересчитать K3 и K5 из уже снятых сырых данных, без новых прогонов. После этого поправить
   `compare.md` и PLAN §13.5 (сейчас там «видимые — только смерть базы»).
2. C3, C4, C5: правки deploy по 1–4 ч, проверка — `recovery.py` (SIGSTOP `twist_mux`, kill сторожа) на двойнике.
3. C6, C10, C11, C13 — до 8-часового прогона: иначе K8 и «старение» нечем мерить.
4. C7, C8 (O2), C9 — в протокол 8-часового прогона и HIL.
5. C12, C14, C16 — по итогам 8 ч и HIL.

## 6. Чего не нашёл

- Публикаций именно про «невидимые для пользователя перезапуски» робота — как и в R5 [НН]. Определение PLAN §1 —
  своя предрегистрированная метрика. Его обоснование — SLI и chaos engineering, а не готовый стандарт.
- Эмпирических исследований старения ПО (утечки, наклоны) именно на ROS 2 в рецензируемых работах 2023–2026 не
  нашёл. Есть только сообщение на Discourse о росте памяти fleet adapter (Humble). Ближайшие работы — Cotroneo,
  Cukic 2026 (LLM-сервисы) и Costa et al. 2026 (Python).
- Рецензируемой работы 2023–2026 про внесение отказов в ROS 2 с метриками TTR не нашёл. Есть только
  GitHub-стенды и MAVFI (2021, БПЛА).
- Kaâniche et al. «user-perceived availability» (DSN 2003) подтвердить не удалось — не цитировать.
- Таймаут и поведение tegra-wdt на стоковой Nano — только форумы; проверять на плате.

## 7. Источники

Научные:
1. Candea G., Kawamoto S., Fujiki Y., Friedman G., Fox A. Microreboot — A Technique for Cheap Recovery. OSDI 2004,
   pp. 31–44. https://www.usenix.org/conference/osdi-04/microreboot—-technique-cheap-recovery ; arXiv:cs/0406005
2. Candea G., Fox A. Crash-Only Software. HotOS IX, 2003. https://www.usenix.org/conference/hotos-ix/crash-only-software
3. Candea G., Fox A. Recursive Restartability: Turning the Reboot Sledgehammer into a Scalpel. HotOS-VIII, 2001,
   pp. 125–130. https://infoscience.epfl.ch/record/98456
4. Huang Y., Kintala C., Kolettis N., Fulton N. D. Software Rejuvenation: Analysis, Module and Applications. FTCS-25,
   1995, pp. 381–390. DOI 10.1109/FTCS.1995.466961
5. Vaidyanathan K., Trivedi K. S. A Comprehensive Model for Software Rejuvenation. IEEE TDSC 2(2):124–137, 2005.
   DOI 10.1109/TDSC.2005.15
6. Grottke M., Li L., Vaidyanathan K., Trivedi K. S. Analysis of Software Aging in a Web Server. IEEE Trans.
   Reliability 55(3):411–420, 2006. DOI 10.1109/TR.2006.879609 (Манн–Кендалл, Сен, сезонный вариант — проверено по
   тексту)
7. Cotroneo D., Cukic B. Characterizing Software Aging in GPU-Based LLM Serving Systems. arXiv:2606.11916, 2026
8. Costa G., Santos C., Natella R., Andrade E. A Case Study on Software Aging in LLM-Generated Python Applications.
   WTF/SBC 2026. DOI 10.5753/wtf.2026.23233
9. Zhu J., Ro Y., Robertson J., Wang K., Li J., Vikalo H., Akella A., Wang Z. Your Agents Are Aging Too: Agent
   Lifespan Engineering for Deployed Systems. arXiv:2605.26302, 2026
10. Silva R. J. M., Nascimento M. G., Machida F., Andrade E. Adaptive Detection of Software Aging under Workload
    Shift. SSCAD 2025. arXiv:2511.03103
11. Avizienis A., Laprie J.-C., Randell B., Landwehr C. Basic Concepts and Taxonomy of Dependable and Secure
    Computing. IEEE TDSC 1(1):11–33, 2004. DOI 10.1109/TDSC.2004.2
12. Chen W., Toueg S., Aguilera M. K. On the Quality of Service of Failure Detectors. IEEE Trans. Computers
    51(5):561–580, 2002
13. Davidovič Š. Incident Metrics in SRE. Google / O'Reilly, 2021.
    https://sre.google/resources/practices-and-processes/incident-metrics-in-sre/
14. Thurgood S., Ferguson D., Hidalgo A., Beyer B. Implementing SLOs // The Site Reliability Workbook.
    https://sre.google/workbook/implementing-slos/
15. Basiri A., Behnam N., de Rooij R., Hochstein L., Kosewski L., Reynolds J., Rosenthal C. Chaos Engineering. IEEE
    Software 33(3):35–41, 2016. DOI 10.1109/MS.2016.60; arXiv:1702.05843
16. Bruneau M. et al. A Framework to Quantitatively Assess and Enhance the Seismic Resilience of Communities.
    Earthquake Spectra 19(4):733–752, 2003. DOI 10.1193/1.1623497
17. Hanley J. A., Lippman-Hand A. If Nothing Goes Wrong, Is Everything All Right? Interpreting Zero Numerators.
    JAMA 249(13):1743–1745, 1983. DOI 10.1001/jama.1983.03330370053031
18. Bédard C., Lütkebohle I., Dagenais M. ros2_tracing: Multipurpose Low-Overhead Framework for Real-Time Tracing of
    ROS 2. IEEE RA-L 7(3):6511–6518, 2022. DOI 10.1109/LRA.2022.3174346
19. Yu J., Lee S., Choi Y., Park K.-J. ros2probe: Non-intrusive, Kernel-selective Observability for Robot Operating
    System 2 Middleware. arXiv:2606.10746, 2026 (eBPF на AF_PACKET, Aya; ядро 5.15, Humble)
20. Abaza H., Roy D., Fan S., Saidi S., Motakis A. Trace-enabled Timing Model Synthesis for ROS2-based Autonomous
    Applications. arXiv:2311.13333, 2023
21. Caldas R., Piñera García J. A., Schiopu M., Pelliccione P., Rodrigues G., Berger T. Runtime Verification and
    Field-based Testing for ROS-based Robotic Systems. IEEE TSE, 2024. DOI 10.1109/TSE.2024.3444697; arXiv:2404.11498
22. Sartaj H. et al. Software Engineering for Self-Adaptive Robotics: A Research Agenda. arXiv:2505.19629, 2025
23. Silva G. R. et al. ROSA: A Knowledge-based Solution for Robot Self-Adaptation. Frontiers in Robotics and AI, 2025.
    DOI 10.3389/frobt.2025.1531743; arXiv:2505.00733
24. Zou Y.-H., Bai J.-J. Effective Crash Recovery of Robot Software Programs in ROS. ICRA 2021, pp. 9498–9504.
    DOI 10.1109/ICRA48506.2021.9560876

Документация, исходники, issues:

25. systemd v245, `man/systemd.resource-control.xml` (перевод `MemoryMax` на v1, `MemoryHigh` только unified).
    https://github.com/systemd/systemd/blob/v245/man/systemd.resource-control.xml
26. ROS 2 Foxy, About QoS settings — таблица совместимости deadline.
    https://github.com/ros2/ros2_documentation/blob/foxy/source/Concepts/About-Quality-of-Service-Settings.rst
27. rclpy PR #865 «Implement managed nodes» (слит 23.12.2021); `rclpy/rclpy/lifecycle` нет в ветке foxy и есть в
    humble. https://github.com/ros2/rclpy/pull/865
28. ros2_tracing README (foxy и rolling); `tracetools.h` ветки foxy.
    https://github.com/ros2/ros2_tracing/blob/foxy/README.md
29. twist_mux foxy-devel 4.0.1: `include/twist_mux/twist_mux.hpp` (`DIAGNOSTICS_PERIOD = 1s`),
    `src/twist_mux_diagnostics.cpp`. https://github.com/ros-teleop/twist_mux/tree/foxy-devel
30. realsense-ros 4.51.1: README (`diagnostics_period`, `reconnect_timeout`, `wait_for_device_timeout`),
    `base_realsense_node.cpp`. https://github.com/IntelRealSense/realsense-ros/tree/4.51.1
31. urg_node2 README (lifecycle, `error_limit`, `error_reset_period`, `/diagnostics`; Foxy).
    https://github.com/Hokuyo-aut/urg_node2
32. ROS 2 Foxy rosdistro: diagnostics 3.1.2, ros2_tracing 1.0.5, bond_core 2.1.0, system_modes 0.9.0,
    rosbridge_suite 1.3.1, launch 0.10.10. https://github.com/ros/rosdistro/blob/master/foxy/distribution.yaml
33. Ядро L4T 4.9 (зеркало OE4T): `arch/arm64/configs/tegra_defconfig` (r32.7.5), `init/Kconfig` (`BPF_SYSCALL default n`),
    `fs/proc/base.c` (`smaps_rollup` есть в r32.7.4 и отсутствует в r32.7.3).
    https://github.com/OE4T/linux-tegra-4.9
34. Colascione D. [PATCH] Add /proc/pid/smaps_rollup. LKML, 08.2017.
    https://lkml.iu.edu/hypermail/linux/kernel/1708.1/00604.html
35. NVIDIA Forums: Enable and setup watchdog for Jetson Nano (2021),
    https://forums.developer.nvidia.com/t/enable-and-setup-watchdog-for-jetson-nano/167539 ; Kernel support for eBPF
    (2022), https://forums.developer.nvidia.com/t/kernel-support-for-ebpf/201100 ; kernel config 4.9.140-tegra (gist),
    https://gist.github.com/matfra/a5f2da3559ec3fcf1bfad9b6ada53517
36. Kubernetes — Pod Lifecycle, Container restarts (10 → 300 с, сброс через 10 мин).
    https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/
37. greenwave_monitor (Humble+). https://index.ros.org/p/greenwave_monitor/ ; ros2_pulse (Humble/Jazzy/Kilted).
    https://github.com/TanayK07/ros2_pulse
38. mallopt(3). https://man7.org/linux/man-pages/man3/mallopt.3.html
39. py-spy. https://github.com/benfred/py-spy
