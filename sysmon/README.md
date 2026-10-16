# sysmon — сэмплер ресурсов дерева процессов и cgroup v2

Go-утилита (статический бинарник, без зависимостей кроме стандартной библиотеки). Раз в период (1–30 с)
снимает память, CPU, ввод-вывод, переключения контекста, сеть и температуру выбранных процессов и пишет
JSONL. Нужна для метрик «работы во времени» (THESIS_CONCEPT §12.2–12.3): PSS(t) и наклон Сена, CPU-с/ч,
переключения контекста, запись на флеш, трафик, а также для прогонов с `memory.max` 256/512 МиБ.
Точного счётчика пробуждений в используемых источниках нет: `wakeups: null` означает «неизвестно».

Числа, полученные при разработке (в том числе в примерах ниже), — **не результаты**.

## Сборка и тесты

```bash
cd sysmon
make build-linux-arm64   # bin/sysmon-linux-arm64: CGO_ENABLED=0, статический ELF для платы
make build-mac           # bin/sysmon-darwin-arm64: для разработки (источник ps)
make test                # go test ./... — разбор /proc на фикстурах, работает на macOS
make race vet fmt-check
make vm-test             # те же тесты + живой тест /proc и cgroup в Lima VM "sbc" (Ubuntu 24.04 arm64)
make vm-smoke            # бинарник в VM на нагрузке в scope с memory.max = 256 МиБ
```

## Запуск

```bash
# дерево процессов с корнем 1234 (с потомками), раз в 30 с
sysmon --pid 1234 --out run/sysmon.jsonl
# группа cgroup v2 (все процессы группы и вложенных) + состояние системы
sysmon --cgroup /sys/fs/cgroup/edgectx.slice --system --interval 5s --out run/sysmon.jsonl
# корень и его группа (путь берётся из /proc/<pid>/cgroup)
sysmon --pid 1234 --cgroup auto --out run/sysmon.jsonl
# запустить команду и мерить её дерево; код выхода sysmon = код выхода команды
sysmon --interval 5s --out run/sysmon.jsonl -- ./edgectx --config cfg.yaml
```

| Флаг | По умолчанию | Смысл |
|---|---|---|
| `--pid N[,M]` | — | корни деревьев (повторяемый); потомки включаются, если нет `--no-children` |
| `--cgroup DIR\|auto` | — | каталог cgroup v2; `auto` — группа первого корня |
| `--interval` | `30s` | период, допустимо 1s…30s (шаг 30 с — методика §12.2) |
| `--count`, `--duration` | 0 | остановка после N сэмплов / по времени (0 — без ограничения) |
| `--out` | `-` | файл JSONL (каталоги создаются); `-` — stdout, тогда stdout команды уходит в stderr |
| `--per-proc` | `true` | писать процессы поимённо (`procs`) |
| `--iface` | все, кроме `lo` | интерфейсы для `net` |
| `--net-self` | `false` | брать сеть из netns сэмплера; по умолчанию — из netns корня (`/proc/<pid>/net/dev`) |
| `--system` | `false` | `/proc/stat`, `meminfo`, `loadavg` — для прогона «плата без агента» |
| `--backend` | `auto` | `procfs` (Linux) или `ps` (macOS, упрощённо) |
| `--proc-root`, `--sys-root`, `--cgroup-root` | `/proc`, `/sys`, `/sys/fs/cgroup` | корни для фикстур |
| `--clk-tck` | из `/proc/self/auxv` | тиков в секунду для `stat` (без cgo `sysconf` недоступен) |
| `--thread-ctxsw` | `true` | ctxsw — сумма по `/proc/<pid>/task/*/status` |

Остановка: `--count`, `--duration`, SIGINT/SIGTERM (в режиме команды сигнал пересылается ей, sysmon
дописывает итог после её выхода), завершение всех корней (`--pid` без `--cgroup`), удаление группы.
Снимок, обнаруживший завершение корней или удаление группы, не записывается как нулевой `sample`:
причина завершения остаётся в `end`. Если цель уже завершилась, допустимы только `header` и `end`.
Коды выхода: 0 — норма, 1 — ошибка, 2 — неверные аргументы; в режиме команды — её код.

`--cgroup auto` отказывается от группы, включающей сам sysmon (в том числе через вложенную группу):
исключение PID из `tree` не вычитает его из иерархических счётчиков cgroup. Для групповых метрик нужно
запустить цель в отдельной cgroup; обычный `--pid` продолжает измерять только дерево цели.

## Формат JSONL (`schema: "sysmon.v1"`)

Первая строка — `header`, затем `sample` раз в период, последняя — `end`. Каждая строка сбрасывается на
диск сразу, поэтому при аварии прогона записанное остаётся. Память — КиБ, время CPU — секунды
(накопленные с запуска процесса), `ts_wall` — Unix-время в секундах (для сопоставления с `ts_wall`
трассы edgectx), `t_s` — монотонное время от старта сэмплера.

```jsonc
{"type":"header","schema":"sysmon.v1","version":"0.1.0","backend":"procfs",
 "avail":["rss","vsz","pss","swap","cpu","faults","io","net","ctxsw","cgroup","system","thermal"],
 "host":"…","os":"linux","arch":"arm64","kernel":"6.8.0-106-generic","num_cpu":4,
 "page_size":4096,"clk_tck":100,"interval_s":1,"started_wall":1791313110.56,"sysmon_pid":7015,
 "target":{"pids":[6981],"children":true,"cgroup":"/sys/fs/cgroup/…/sysmon-smoke-28801.scope"}}
{"type":"sample","seq":3,"ts_wall":…,"t_s":3.0,"dt_s":1.0,
 "tree":{"procs":3,"threads":3,"rss_kib":22426,"vsz_kib":…,"pss_kib":15052,"pss_anon_kib":…,"pss_file_kib":…,
         "pss_shmem_kib":0,"swap_kib":0,"swap_pss_kib":0,"cpu_user_s":0.01,"cpu_sys_s":0.0,"cpu_s":0.01,
         "cpu_children_s":0.0,"cpu_pct":0.0,"minflt":…,"majflt":0,
         "io":{"rchar":…,"wchar":…,"syscr":…,"syscw":…,"read_bytes":0,"write_bytes":0,"cancelled_write_bytes":0},
         "ctxsw":{"vol":14,"invol":0}},
 "root":{… то же только для корневых процессов («оркестратор») …},
 "procs":[{"pid":6981,"ppid":6979,"comm":"bash","start_ticks":…,"threads":1,"rss_kib":…,"hwm_kib":…,"pss_kib":…, …}],
 "cgroup":{"procs":3,"memory_current_kib":12800,"memory_peak_kib":…,"memory_max_kib":262144,
           "cpu_usage_s":…,"cpu_user_s":…,"cpu_sys_s":…,"nr_throttled":0,"throttled_s":0,
           "memory_events":{"low":0,"high":0,"max":0,"oom":0,"oom_kill":0,"oom_group_kill":0},
           "memory_stat":{"anon":…,"file":…,"kernel":…,"sock":…,"pgfault":…,"pgmajfault":…},"io":{}},
 "net":{"eth0":{"rx_bytes":…,"tx_bytes":…,"rx_packets":…,"tx_packets":…,"rx_errs":0,"tx_errs":0,"rx_drop":0,"tx_drop":0}},
 "thermal_c":{"cpu-thermal":48.3},
 "system":{"cpu_s":{"user":…,"idle":…},"mem_total_kib":…,"mem_available_kib":…,"load1":0.08, …},
 "self":{"cpu_s":0.031,"max_rss_kib":7552,"rss_kib":…,"pss_kib":7828,"collect_ms":6.2},
 "errors":{"smaps:perm":2}}
{"type":"end","ts_wall":…,"t_s":7.01,"samples":8,"reason":"count",
 "overhead":{"cpu_s":0.056,"cpu_frac":0.008,"collect_ms_mean":5.5,"collect_ms_p50":6.2,"collect_ms_p95":7.0,
             "collect_ms_max":7.2,"max_rss_kib":7552,"pss_kib_last":7828}}
```

- **Поле отсутствует ≠ ноль.** Поля, которых нет у источника (у `ps` — PSS, IO, ctxsw, cgroup), в записи
  отсутствуют; какие группы есть — в `header.avail`. Если поле недоступно лишь у части процессов (EACCES на
  `smaps_rollup` чужого пользователя), сумма в `tree` неполна, и это видно в `tree.missing` (`{"pss": 1}`) и
  в `errors` (`"источник:perm|missing|error"` → число).
- **Группы `tree` и `root`.** `tree` — все выбранные процессы (дерево корней и/или процессы группы, кроме
  самого sysmon); `root` — только корневые процессы без потомков. Методика (§12.3) отчитывает
  «оркестратор» (`root`) и «оркестратор + серверы» (`tree` или `cgroup`).
- **CPU.** `cpu_s` — user+sys живых процессов; `cpu_children_s` — `cutime+cstime` (дождавшиеся потомки),
  так время коротких дочерних процессов не теряется. `cpu_pct` — за интервал, в процентах одного ядра по
  `cpu_s+cpu_children_s`; отрицательные приращения (выход процесса без wait нашим деревом) обрезаются до 0.
  Для границы «система целиком» надёжнее `cgroup.cpu_usage_s` (учитывает всё, что исполнялось в группе).
- **Счётчики** (CPU, `minflt/majflt`, `io`, `ctxsw`, `net`, `cgroup.cpu_*`, `memory_events`) накопленные;
  приращения считает анализ (`bench/metrics`).
- **Накладные расходы.** `self` в каждом сэмпле и `end.overhead`: время CPU процесса sysmon, пик RSS,
  текущие RSS/PSS (Linux), длительность опроса и `ctxsw.vol/invol` из `getrusage` (все потоки, включая
  завершившиеся). Это переключения контекста, не точные пробуждения; `wakeups` остаётся `null`.
  При недоступном `getrusage` пишутся `self.status: "unknown"` / `overhead.self_status: "unknown"`,
  а поля CPU, пика RSS и ctxsw отсутствуют. По методике sysmon работает в своём cgroup, а его расходы
  меряются в прогоне «плата без агента» (`--system`) и вычитаются.
- **OOM.** `end.oom` наблюдает приращение `memory.events.oom_kill` от начала до конца прогона,
  в том числе в обычном режиме `--pid`: пути cgroup корней сохраняются до их завершения. `status`:
  `observed` — хотя бы один счётчик вырос, `not_observed` — доступные счётчики не выросли и пропусков нет,
  `unknown` — нет v2, доступа, исходного/конечного счётчика либо счётчик уменьшился. Для каждой группы
  записаны путь и доступные `oom_kill_before/after/delta`. `scope: "cgroup"` означает наблюдение группы:
  в общей группе это не доказывает OOM конкретного PID; один SIGKILL также не доказывает OOM.

## Источники данных

| Что | Linux (`procfs`) | macOS (`ps`) |
|---|---|---|
| процессы и дерево | обход `/proc/*/stat` (ppid), `(pid, starttime)` — защита от повторного PID; зомби не считаются живыми | `ps -A -o pid,ppid,rss,vsz,utime,time,comm` |
| память | `/proc/<pid>/smaps_rollup` (Rss, Pss, Pss_Anon/File/Shmem, Swap, SwapPss); на ядрах < 4.14 (Jetson Nano, 4.9) — сумма по `smaps`; `VmHWM` из `status` | RSS, VSZ |
| CPU | `stat`: utime, stime, cutime, cstime ÷ CLK_TCK (из `auxv`) | `utime`, `time − utime` (0,01 с) |
| IO | `/proc/<pid>/io` (rchar, wchar, read_bytes, write_bytes …) | — |
| ctxsw | сумма `voluntary/nonvoluntary_ctxt_switches` по `/proc/<pid>/task/*/status` | — |
| cgroup v2 | `memory.current/peak/max`, `memory.stat` (подмножество), `memory.events`, `cpu.stat`, `io.stat`, `cgroup.procs` рекурсивно | — |
| сеть | `/proc/<root>/net/dev` — netns корня | `netstat -ibn`, только с `--iface` |
| температура | `/sys/class/thermal/thermal_zone*/temp` | — |

## Что гарантируется и чего нет

Гарантируется (проверено тестами):
- разбор всех файлов сверен с реальным снимком Ubuntu 24.04 / ядро 6.8 / arm64 (`testdata/real_ubuntu2404_arm64`,
  снят `scripts/capture_fixture.sh`): значения совпадают с файлами; сумма построчного `smaps` совпадает со
  `smaps_rollup` по Rss и с точностью до округления по Pss (откат для старых ядер);
- `comm` с пробелами и скобками, PID, выданный повторно, зомби-корень, исчезновение процесса между чтениями,
  EACCES на чужих процессах, удаление группы, отсутствие `memory.peak` (ядро < 5.19) и `memory.max`;
- живой тест на настоящем `/proc` и cgroup v2 в VM; дымовой прогон в scope с `memory.max` 256 МиБ.

Не гарантируется / ограничения:
- **ctxsw** — только живые потоки: счётчики завершившихся потоков ядро в `/proc` не хранит (у Go потоки
  долгоживущие, у Python — тоже, но короткие потоки недоучитываются). Для группы это видно по `cgroup.cpu_*`;
- **осиротевшие потомки** (перешедшие к init/субжнецу вне дерева) выпадают из `tree`; для полной границы
  используйте `--cgroup`;
- процессы, родившиеся и умершие между сэмплами, в `procs` не видны (их CPU попадает в `cpu_children_s`
  родителя или в `cgroup.cpu_usage_s`, память — нет; пик группы — `memory_peak_kib`);
- сеть — счётчики интерфейсов netns целиком, без разбиения по классам трафика (LLM / TLS / MCP); классы
  по методике дают счётчики nftables — вне sysmon;
- cgroup v1 (L4T на Jetson Nano по умолчанию) не поддерживается: на Nano — режим `--pid`;
- троттлинг Raspberry Pi (`vcgencmd get_throttled`) не читается; температура — только термозоны sysfs;
- macOS-источник — только для разработки: нет PSS/IO/ctxsw/cgroup, сеть хоста к агенту не относится;
- точность периода — `time.Ticker`: при опросе дольше периода тики пропускаются (видно по `dt_s`).

## Устройство

```
cmd/sysmon/          CLI, режим команды, сигналы
internal/procfs/     разбор /proc (stat, status, io, smaps[_rollup], net/dev, meminfo, stat, loadavg, auxv)
internal/cgroup/     cgroup v2
internal/thermal/    термозоны sysfs
internal/pstool/     разбор ps/netstat (macOS)
internal/collect/    выбор процессов (дерево/группа), сбор Snapshot, суммирование Totals
internal/sampler/    цикл, формат JSONL, накладные расходы
internal/selfstat/   getrusage + /proc/self
internal/testfs/     построитель синтетических /proc и cgroup для тестов
testdata/            реальный снимок /proc и cgroup (Lima VM)
scripts/             capture_fixture.sh, vm_test.sh, vm_smoke.sh
```
