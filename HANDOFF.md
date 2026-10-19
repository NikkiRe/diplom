# HANDOFF: ВКР «Разработка ИИ-агента для приложений граничного ИИ» — полный контекст

> **19.10.2026.** Репозиторий: https://github.com/NikkiRe/diplom. Новичкам сначала `docs/OVERVIEW.md` и `docs/ROADMAP.md`; работа на Windows — `docs/WINDOWS.md`. Этот файл писался на Mac: пути `/Users/...` и команды `limactl` — маковские.


Срез: 2026-10-15. Обновлено по итогам работ 07–15.10.2026: сенсорное ядро, этап «минимальная плата», интеграция,
начало работы по роботу NeuroChip. Срез 07.10 — `~/.Trash/diploma-cleanup-20261015/docs/HANDOFF.md.before-20261015`.
**19.10** по итогам шага W3 робота NeuroChip (прогоны на двойнике, приёмки 17.10 и 18.10, доработки) обновлены §0,
строка NeuroChip в §4, §5 (состояние ВМ), §8, §9, §10, §13 и §14; остальное — срез 15.10. Копия до правки —
`~/Documents/ChatGPT/диплом/backups/twin-20261018/docs-w3-before/`.
Корень проекта: `/Users/chosenundead/Projects/diploma-edge-ai` («корень»). Точка входа для следующего агента:
прочитай целиком, затем файлы из §13.

**Как читать цифры.** Всё снято на Mac (M4 Pro) или в ВМ Lima на нём, с `bench/mockllm` вместо модели, обычно при
параллельной работе других агентов, по одному прогону: **ориентир, не результат**. На настоящей плате, датчиках,
камере, роботе и с реальной LLM не запускалось ничего — всё проверено только виртуально.

---

## 0. Самое главное (на один экран)

- **Студент:** Евстигнеев Никита Артёмович, ИТМО, ПИиКТ, P3431, 09.03.01 «Информатика и вычислительная техника»,
  4 курс. Интерес — системное ПО / low-level; пишет по-русски, неформально, коротко; уклон в инфобез отверг.
  Задачу с роботом принёс из лаборатории NeuroChip (ИТМО), где работает (§14).
- **Руководитель:** Сергей Вячеславович. **Тема (согласована 06.10):** «Разработка ИИ-агента для приложений граничного
  искусственного интеллекта».
- **Постановка руководителя:** агент на **одноплатнике**; **внешняя LLM** (RouterAI, «GPT mini разных версий»); агент
  держит **контекст пользователя и оборудования**, об оборудовании узнаёт через **MCP**; инструменты — датчики,
  приводы, камеры; аналоги — **NanoClaw, PicoClaw** (NanoClaw не замеряем — Claude Agent SDK, Docker,
  Node ≥ 22; вместо него nanobot, у руководителя **не согласовано**); можно **ROS и Gazebo**.
- **Ключевая метрика (подчёркнута дважды):** работа и экономность **во времени** — вызовы LLM, токены, ₽, трафик,
  задержка, память и её рост, CPU, энергия за рабочий день, работа при жёстких лимитах. Надёжность — НЕ цель;
  у робота безопасность и непрерывность — ограничения и условия допуска.
- **Железо.** MacBook M4 Pro, 12 ядер, 24 ГБ. На кафедре Jetson Nano: 15.10 студент — «вроде 4 гига оперативы»
  (06.10 говорил «вроде 1 ГБ», таких нет); модель и JetPack не подтверждены. JetPack 4.6.x — Ubuntu 18.04, ядро 4.9,
  Python 3.6. У робота NeuroChip свой Jetson Nano (README лаборатории: Ubuntu 20.04, ROS 2 Foxy, Python 3.8; не
  проверено). Плату эмулирует ВМ `sbc`; запасной вариант — RPi 5 4 ГБ. INA3221 занижает мощность до 50 %.
  `powermetrics` требует sudo — запускает только пользователь.
- **Ключ RouterAI:** со слов студента (14.10) выдан, но **деньги не тратить** — только `bench/mockllm`. Значения
  ключей нигде не записывать.
- **Что есть (15.10):** ≈83 тыс. строк кода и тестов (без `neurobot/` и vendor). Компоненты: агент
  EdgeCtx, hw-mcp, **cam-mcp** (новый), robot-mcp, sysmon. Стенды: «день» (T0); **«минимальная плата»**
  (`bench/board` + `scripts/board`, новый); **«живое sysfs»** (`bench/sysfsim` + модуль ядра gpio-sim, новый); робот
  (ВМ `sim`, ROS 2 Jazzy + Gazebo Harmonic). Тесты 15.10: **Go — 59 пакетов ok; pytest — 475 passed, 16 skipped;
  baselines — 52 passed, 2 skipped.** `neurobot/` (19.10, Mac, Python 3.12, весь каталог) — **850 passed,
  95 skipped**; код для робота — ещё и под Python 3.8 (`experiments/results/neurobot_20261018_acceptfix2/tests/`).
- **Главное за 09–15.10.** Агент переживает обрыв связи с LLM и падение MCP-сервера. hw-mcp получил GPIO uAPI v1 (нужен
  ядру 4.9) и «живые» датчики. Написан cam-mcp: кадры в LLM не уходят. Виртуальная плата проверена «задано vs
  измерено». Живая цепочка «датчики → hw-mcp → агент → LLM → шлюз → реле» прошла в ВМ под 64 МиБ / 25 % CPU /
  сеть 3g. Найден и исправлен дефект дебаунса в гейте агента.
- **Сейчас (19.10):**
  1. Робот NeuroChip, запрос студента 15.10 (всё в Gazebo/ROS с проверкой датчиков, Jetson 4 ГБ, «чтобы физически
     ничего не пострадало», PLAN §13): шаг W3 на цифровом двойнике сделан — прогоны A0/A1, приёмка 17.10, доработки,
     повторная приёмка 18.10, доводка 19.10 (§14). Итог — ориентир стенда, на роботе не проверялось. Доводку 19.10
     независимо не перепринимали.
  2. Ждут студента два решения (PLAN §13.6): как читать K6 «stop через MCP» (штатный стоп плавный, ноль через ≈0,5 с)
     и оставить ли `nodata_unseen=slow` — отступление от буквы §13.3.
  3. Правки `neurobot/` и документов 16–19.10 (после коммита 28e85dd, он старше части работы 16.10) на 19.10 лежат в
     рабочем дереве незакоммиченными; коммит и push — по просьбе студента. Дальше — поставка пакета 1 лаборатории и
     HIL (§10).

  ВМ на 19.10: `sim` запущена (`neurobot.target` остановлен), `sbc` остановлена.

---

## 1. Хронология (что было и почему)

1. **До 06.10.** Наследство Codex («EdgeComputeLab», игрушка), переезд из `~/Projects/инфобез`, обзор литературы
   (`research/literature/`). Первая постановка — среда локальной LLM на llama.cpp — руководителем не выбрана.
   Всё это ушло в Корзину (§11).
2. **06.10, встреча с руководителем:** новая постановка (§0). Исследование → `research/new_topic/THESIS_CONCEPT.md`,
   `SOURCES.md`, `EXPLAINER_FOR_STUDENT.md`, `landscape/AGENT_LANDSCAPE.md`; план ВКР → `docs/plan/vkr_plan.html`.
3. **06–07.10:** реализация по `IMPLEMENTATION_SPEC.md` v2: агент, hw-mcp, стенд «дня», монитор, sysmon, стенд
   робота (ВМ `sim`), robot-mcp, обвязка PicoClaw/nanobot. Пользователь одобрил **лестницу ограничений** и
   **сценарий с роботом**. Днём 07.10 всё остановлено, собран пакет `docs/supervisor_pack/`.
4. **07.10, вечер — сенсорное ядро** (робот и камера отложены). Сделано в отдельной копии (резерв
   `~/Documents/ChatGPT/диплом/backups/sensor-core-20261007-01.*`): `sample_periods` в `declared.json`, очередь MCP
   агента ≤ 1024, ограниченная история, честные ошибки реплеера, исправления sysmon, `python -m bench.sensors`.
   Статус — `docs/SENSOR_CORE.md`.
5. **09.10 — этап «минимальная плата»: плата может быть очень слабой, сеть плохой.** Резерв всего проекта —
   `backups/minboard-20261009-1445.before.tar.gz`, ≈407 МБ. Сделано:
   - агент: таймауты, повторы, предохранитель, очередь отложенного, 202, переподключение MCP;
   - hw-mcp: uAPI v1, `-gpio-driver`, метки чипов, живые датчики, `-session-timeout`;
   - написаны **cam-mcp** и **bench/sysfsim**;
   - robot-mcp: отмена целей Nav2.

   По каждому компоненту — ревью. В ВМ `sbc` — **Oops ядра 6.8.0-106** при снятии занятого чипа gpio-sim (§12).
6. **14.10.** ВМ `sbc` перезагружена в ядро 6.8.0-146. Исправления по ревью доведены и независимо перепроверены
   (agent, hw-mcp, sysfsim). Написан **bench/board + scripts/board**: прогоны verify a–f. Новая задача — **робот
   NeuroChip** (анализ, план, разбор материалов, §14).
7. **14–15.10 — интеграция.**
   - Полный прогон тестов.
   - Монитор понимает новые записи агента.
   - Сквозной тест камеры перенесён в `bench/tests`.
   - **Живой смоук минимальной платы** в ВМ, 3 прогона. В нём найден и исправлен дефект дебаунса гейта.
   - Бинарники agent и hw-mcp пересобраны 15.10 00:16.
   - `vm_check.sh` sysfsim целиком — код 0.
8. **15.10:** запрос пользователя (§0) → PLAN §13 и обновление документов.

---

## 2. Постановка ВКР (рабочая)

- **Объект:** функционирование ИИ-агента на одноплатнике с внешней LLM и MCP-доступом к оборудованию.
  **Предмет:** ведение контекстов пользователя и оборудования и выбор момента вызова модели; их влияние на затраты
  и ресурсы при многочасовой работе.
- **Цель (эффект в цифрах):** сократить суточные затраты на внешнюю LLM (вызовы, входные токены, ₽/сутки) против
  штатной периодической проверки (PicoClaw, nanobot). Не быть дороже PicoClaw в самой экономной настройке. Условия:
  полнота реакции на значимые события не хуже (допуск 5 п.п.), 0 небезопасных команд приводам.
- **Гипотезы (`vkr_plan.html` §5):**
  - H1 — событийный фильтр против heartbeat;
  - H2 — компактный каталог + состояние по требованию;
  - H3 — маскирование + окно истории;
  - H4 — рост памяти за 8 ч ≤ 1 МиБ/ч;
  - H5 — сводка детектора камеры вместо кадра;
  - H6 — сводки робота вместо сырых тем. В плане старый ориентир прототипа, у Go-адаптера — 3,7–10× токенов и
    ≈110–220× трафика;
  - H7 — карта выживаемости при лимитах;
  - **H8 предложена** (PLAN §12, не согласована) — место замыкания контура «жест → движение».
- **Сценарии:** «помещение» (датчики, реле, камера; день по CASAS + Bentley 2018) и «мобильный робот». Для робота
  было TB3 + Nav2 в Gazebo. **Предложено** (PLAN §12): робот NeuroChip на стенде и HIL, Nav2 — второстепенно. В
  DECISIONS — только после согласия студента и руководителя.
- **Метод:** одинаковый «рабочий день»; ускоренный T0 (виртуальные часы агента, сжатие k у аналогов); 8-ч прогоны T2
  на плате; лестница ограничений; парный бутстреп, «не хуже» с допуском. **Предрегистрации нет**
  (`experiments/PROTOCOL.md`). Дома CASAS (seed 20261006): tune hh103 hh105 hh106 hh107 hh114 hh118 hh122; holdout
  hh101 hh102 hh110 hh121 hh126 hh129 hh130 — до заморозки не смотреть.
- **Сроки ГИА** (`research/consultation-gia-1/ГИА_главное_и_план_действий.txt`): **15–30.10 закрепление за
  руководителем — идёт сейчас**; до 30.11 тема в ИСУ; 15.01 глава 1; март — проверка 1; апрель — проверка 2;
  15.05 текст; конец мая предзащита; защита ~3–20.06. ПЗ 40–60 стр., 3 главы.

---

## 3. Архитектура (что построено) и связи

```
[Пользователь] ⇄ HTTP /v1/user ⇄ [Одноплатник: агент EdgeCtx (Go, статический бинарник)]
  ├─ гейт (дебаунс + предел цепочки 2·debounce_s, пороги), локальные правила, heartbeat; часы real/virtual
  ├─ планировщик и бюджет; контекст пользователя (SQLite WAL); контекст оборудования («нет связи» по устройству)
  ├─ LLM-клиент: таймауты, повторы, предохранитель с пробой /models, очередь отложенного, 202 deferred
  └─ MCP-хост с переподключением ⇄ hw-mcp    (Go: sysfs IIO/hwmon/w1, GPIO uAPI v1/v2, шлюз приводов)
                                 ⇄ cam-mcp   (Go: камера + детектор движения на плате; кадры в LLM не идут)
                                 ⇄ sim-mcp   (Python: эмулятор помещения по CASAS, T0)
                                 ⇄ robot-mcp (Go: адаптер → rosbridge → ROS 2; к EdgeCtx НЕ подключён)
                 ⇄ HTTPS ⇄ внешняя LLM (RouterAI; в разработке — только bench/mockllm)
Стенд «плата»: ВМ sbc, board.sh run — агент и MCP-серверы в одном юните (cgroup v2 + netns/netem);
  bench/sysfsim — «живое» sysfs и чип gpio-sim вместо датчиков; sysmon — сэмплер.
Стенд «робот»: ВМ sim — ROS 2 Jazzy, Gazebo Harmonic, TB3/Nav2, rosbridge 2.7.1; профиль neurobot (§14).
```

**Связи на 15.10 и чем проверены:**
- агент ⇄ sim-mcp: `bench/tests/test_simmcp_edgectx.py`, `bench.sensors`;
- агент ⇄ hw-mcp:
  - `test_hwmcp_edgectx.py` (копия sysfs-фикстуры);
  - блок в `agent/configs/example.yaml` рабочий (`TestExampleHWBlock`);
  - ВМ, HTTP, обрыв сети и перезапуск hw-mcp — `experiments/results/agent_20261014_vmlink/`;
- агент ⇄ cam-mcp: `bench/tests/test_cammcp_edgectx.py`; hw-mcp и cam-mcp вместе —
  `agent_20261014_twoservers/` (Mac);
- hw-mcp ⇄ sysfsim: `test_sysfsim_hwmcp.py` (Mac, files); `vm_check.sh` (ВМ, gpio-sim) —
  `sysfsim_vmcheck_20261015_b/`;
- **агент + hw-mcp (stdio, cdev, чип по метке) + sysfsim (gpio-sim) под `board.sh run`** —
  `minboard_smoke_20261014_a/`;
- агент ⇄ robot-mcp: **не подключался**, поддержки робота в `agent/` нет. robot-mcp проверен на `fakebridge` и в
  ВМ `sbc`; с Gazebo/Nav2 — только сборка 07.10, до исправлений 09.10.

Подробнее: `IMPLEMENTATION_SPEC.md`, README компонентов.

---

## 4. Компоненты, состояние, как запускать

**Тесты 15.10.** Go — 59 пакетов ok: agent 18, hwmcp 12, sysmon 8, robotmcp 10, cammcp 10, cpubench 1 (сверено
`go list`). pytest `bench/tests monitor/tests harness/tests` — 475 passed, 16 skipped: 11 ВМ-тестов board без
`BOARD_VM=1`, 4 gpio-sim, shellcheck. `bench/baselines/tests` — 52 passed, 2 skipped. Сверка
`pytest --collect-only` даёт 491 и 54 теста. `neurobot/tests` — отдельно.

**Окружение.** `.venv/bin/python` 3.12 (uv; mcp, numpy, scipy, pandas, psutil, httpx, tiktoken, pytest, websockets,
cbor2). Go 1.27.1, MCP Go SDK v1.8.0, limactl 2.1.1. В корне `.venv-nanobot` (nanobot 0.3.5) и `.venv-rosmcp`
(ros-mcp-server 3.1.2). PicoClaw 0.3.1 — `bench/baselines/vendor/picoclaw/v0.3.1/`.

Объёмы — `wc -l` 15.10: Go без `*_test.go` / `*_test.go`; Python — код / тесты.

| Компонент | Путь | Что | Код / тесты | Запуск; бинарники (sha256) |
|---|---|---|---|---|
| Агент EdgeCtx | `agent/` | гейт, правила, контексты, LLM-клиент с предохранителем и очередью отложенного, MCP-хост | 9 332 / 5 450 | `go test -race ./...`; `make build-mac build-linux-arm64`. `bin/edgectx-linux-arm64` 13,6 МБ, статический, `a778aff8…`; darwin `301a6ea4…`; 15.10 00:16, **актуальны** |
| hw-mcp | `hwmcp/` | sysfs IIO/hwmon/w1/thermal, GPIO uAPI v1/v2, чип по метке, живые датчики, шлюз (аренда, `safe_state`), `-session-timeout` | 8 322 / 5 441 | `make test`, `make build build-linux-arm64`; ВМ: `make vm-test vm-gpiosim vm-hotplug`, `tools/vm_smoke.sh`. `.build/hw-mcp-linux-arm64` 8,8 МБ `08825328…`, `hw-mcp` `a01dc69a…`; 15.10, актуальны |
| cam-mcp | `cammcp/` | источники synthetic/dir/v4l2, детектор на плате, эпизоды, `camera_lost`/`camera_back`, `snapshot` по запросу | 3 299 / 2 352 | `make test`, `make build-linux-arm64`, `make vm-test vm-smoke vm-v4l2`. `.build/cam-mcp-linux-arm64` 8,8 МБ `794f352d…` (09.10; позже менялся только комментарий, сборка та же) |
| robot-mcp | `robotmcp/` | status, go_to, patrol, look, stop; события; rosbridge с отменой целей Nav2 | 5 839 / 2 150 | `make`. `.build/robot-mcp-linux-arm64` 9,5 МБ `8c30ba2c…`, `fake-rosbridge` `d296f5be…`, `robot-probe` `17042ce6…` (09.10, актуальны; копии в ВМ `~/robotmcp` совпадают) |
| sysmon | `sysmon/` | сэмплер PSS/RSS/CPU/IO/net и cgroup v2 → JSONL | 3 010 / 1 476 | `make test`. **`bin/*` от 06.10 старше правок 07.10 — пересобрать** |
| Плата | `bench/board/`, `scripts/board/` | слабая плата в ВМ `sbc`: память, CPU, диск, сеть; профили с источниками, пресеты плат; `board.sh` (netns, netem в обе стороны, расписание обрывов, `run` под лимитами с итогом); `verify`; `cpubench`, `calibrate` | Python 1 599, sh/jq 517, Go 162 / тесты 1 126 + 77 Go | `board.sh` — пример ниже; `python -m bench.board verify --out DIR` (≈4 мин), `… calibrate`; ВМ-тесты — `BOARD_VM=1` |
| sysfsim | `bench/sysfsim/` | «живое sysfs» по фикстуре hw-mcp (BME280, DHT11, ADS1115, DS18B20, thermal, PIR, геркон, реле); физика, отказы, журнал истины; GPIO files или gpio-sim; часы real/virtual | Python 1 777, sh 202 / 1 093 | `python -m bench.sysfsim list-scenarios / check / init / run` (dense 15 мин, day 8 ч); ВМ: `sudo bash bench/sysfsim/vm_check.sh <hw-mcp>`. Только stdlib |
| Стенд «дня» | `bench/` | `daygen`, `simmcp`, `mockllm`, `replay`, `metrics`, `costmodel`, `sensors`, `baselines` (+ llmtap) | ≈13,4 тыс. / ≈4,4 тыс. + 0,8 тыс. | `pytest -q bench/tests monitor/tests harness/tests`; `pytest -q bench/baselines/tests` |
| Монитор | `monitor/` | протокол трассы и решения шлюза, мутационные тесты; с 15.10 — записи обрыва связи | 3 020 / 3 030 | `python -m monitor.check_trace --trace …`; `docs/monitor.md` |
| Статистика | `harness/` | бутстреп, McNemar, TOST, Холм, Сен, блочный бутстреп | 912 / 547 | `harness/tests` |
| Стенд робота | `scripts/sim/` | ВМ `sim`: ROS 2, Gazebo, TB3, Nav2, rosbridge; с 15.10 профиль `profiles/neurobot.sh` | ≈3,3 тыс. | `start_sim.sh --nav2 --camera lazy`, `stop_sim.sh`; `docs/robot_sim.md` |
| NeuroChip | `neurobot/` | пакет 1 (perception v2, исполнитель, `safety_gate`, развёртывание), двойник, прогоны и наборы, материалы для лаборатории | — | W3 на двойнике сделан, на роботе не проверялся (§14); README каталогов |

Проверка sha: `shasum -a 256 agent/bin/* hwmcp/.build/hw-mcp* cammcp/.build/cam-mcp* robotmcp/.build/*-linux-arm64`.

```bash
.venv/bin/python -m bench.daygen gen --seed 1 --profile M+ --split tune --out day.jsonl
.venv/bin/python -m bench.simmcp --day day.jsonl --port 18090 --catalog generic --journal results/sim.jsonl
.venv/bin/python -m bench.sensors --out <новый каталог>          # gate/heartbeat/hybrid на mockllm
bash experiments/results/minboard_smoke_20261014_a/run.sh <новое имя>   # ≈1,5 мин, ВМ sbc
B() { limactl shell --workdir / sbc -- sudo bash "$PWD/scripts/board/board.sh" "$@"; }
B net-up p1 4g; B run a --mem 64 --cpu 25 --cpus 1 --net p1 --timeout 600 -- <абс. путь> …; B cleanup
```
Порты: edgectx 18080–18089, sim-mcp 18090–18099, mock-llm 18100–18109, robot-mcp 18110–18119, baselines
18120–18139, rosbridge 9090.

---

## 5. Стенд (виртуальные машины Lima) — на 19.10 `sim` запущена, `sbc` остановлена

- **`sbc` — «виртуальная плата».** Ubuntu 24.04.4 arm64, 4 vCPU, 4 ГиБ, 20 ГиБ.
  - **Ядро 6.8.0-146-generic** (сменилось при перезагрузке; до неё — 6.8.0-106 после Oops 09.10).
  - Поставлены `linux-modules-extra-6.8.0-146-generic` (≈116 МБ, из него модули gpio-sim и iio_dummy; в ядре
    `CONFIG_GPIO_CDEV_V1=y`) и `shellcheck` 0.9.0. Есть `gpiod` 1.6.3: gpiomon и др. работают через uAPI v1.
  - На 15.10 00:48: загружены `ifb`, `gpio_sim`, `iio_dummy`; `tainted` 0; устройств gpio-sim нет; `ip_forward` 0.
  - Внутри: `~/.venv-rosmcp`, `~/.venv-nanobot`, `~/baselines`, `~/robotmcp`. В `~/work` — каталоги других агентов
    (`board`, `sysfsim`, `sysfsim-rev-corr`, `sysfsim-review`).
  - Проект смонтирован по тому же пути; из ВМ — только чтение (`PYTHONDONTWRITEBYTECODE=1`).
- **`sim` — симулятор робота.** Ubuntu 24.04, 6 vCPU, 8 ГиБ, 40 ГиБ, ядро 6.8.0-106.
  - ROS 2 Jazzy, Gazebo Harmonic 8.15, TB3 waffle_pi, Nav2, rosbridge 2.7.1 (9090 → хост 127.0.0.1:9090; из `sbc`
    — `ws://host.lima.internal:9090`), `diff_drive_controller` 4.42.1. Для двойника NeuroChip через apt поставлены
    `gz_ros2_control` 1.2.20, `twist_mux` 4.5.0, `imu_filter_madgwick` 2.1.6 (версии — `vm_versions.txt` в
    `experiments/results/neurobot_20261018_*`).
  - Headless, llvmpipe. Стенд NeuroChip в ВМ: пакет — `/opt/neurobot`, прогоны и наборы — `/var/tmp/nb-*`. Не
    останавливать без согласования.
- `limactl start|stop|list`; шаблон `scripts/sim/sim.lima.yaml`. ВМ общие — не удалять и не перезагружать без спроса.

## 6. Данные
- `data/casas/` — CASAS (Zenodo 10.5281/zenodo.15708568, CC BY 4.0, 81 дом) + `subset/` (14 домов), PROVENANCE,
  SHA256SUMS. `data/massive/` — шаблоны команд iot. Цены RouterAI — `research/new_topic/F_artifacts/price_table.py`.
- `hwmcp/testdata/fixture_root` — синтетическая фикстура sysfs, на ней строится `bench/sysfsim`.
- `third_party/neurochip/{demo, ros_mcp_server, robotic_platform, robot_scanner_pptx}` — материалы лаборатории:
  сторонние, только для чтения. Секреты вычищены, веса — непроверенный pickle (§12).

## 7. Предварительные цифры (все — ориентиры)

**Смоук минимальной платы 14–15.10** (`minboard_smoke_20261014_a/`). Состав: sysfsim (gpio-sim) + hw-mcp (stdio,
cdev, чип по метке) + edgectx в одном юните `board.sh run`: 64 МиБ, CPU 25 %, 1 ядро, netns 3g. mockllm — на Mac.
Длительность ≈80 с, по одному прогону на версию.
- Вопрос: `ok`, 2 обращения к LLM, 1,41 с. Всего 4 обращения, 5 060 входных токенов (3 584 из кэша).
- Перегрев в 35,0 с будит модель гейтом. Вентилятор включается через шлюз: run1 — 65,0 с (дефект дебаунса),
  run2 — 50,0 с, run3 — 48,8 с. Остаток — до 4 с цепочки дебаунса, 10 с окна коалесценции и опрос.
- Юнит: `memory.peak` 33,1–33,5 из 64 МиБ, OOM 0; CPU 0,13–0,17 с за 80 с.
- Трафик: ≈32 КБ вверх, ≈7,3 КБ вниз, потерь 0.
- Монитор: 0 нарушений, `n_unsafe` 0; `tainted` 0/0.

**Агент в ВМ при обрыве** (`agent_20261014_vmlink`; MemoryMax 48M, CPUQuota 50 %, GOMEMLIMIT 24 МиБ).
- Потеря 100 % → `llm_unavailable`, вопрос получил 202.
- Восстановление через пробу (`down_s` 8,2) → один слитый ход.
- Перезапуск hw-mcp → `mcp_down`/`mcp_up`, значение перечитано.
- `memory.peak` 8,1 МБ, CPU 41,5 мс за ≈30 с.
- Отказ только chat при живом `/models`: 18 пробных ходов за час, паузы до 150–300 с (тест в копии).

**Виртуальная плата, verify** (`board_verify_20261014_f/`; Mac от батареи): 132 строки — 101 совпадает,
2 расходятся, 29 справочно.
- Память 64/16 МиБ: OOM ровно на пределе.
- CPU 200 % / 50 % / 5 % → 200 / 50,0 / 4,98 %.
- sd-good: 30/50 МБ/с → 30,7/51,3; sd-awful: 4/12,5 → 4,02/12,5.
- RTT, задано → измерено, мс: wifi 6 → 7,5; **wifi-weak 30 → 40,2**; 4g 70 → 74,5; 3g 300 → 306; gprs 1000 → 1096.
- TCP без потерь — 76–95 % заданного у большинства профилей; **4g вниз — 9 %**.
- Потери UDP: lossy 2,04 / 2,25 % при заданных 2,26 %. flaky: окно обрыва точно до 0,05 с.
- calibrate, 100/50/25/10/5 % → 2421/1185/646/245/114 ops/s.

**Ограничения стенда** (`bench/board/README.md`, «Найденные ограничения», п. 1–3; ранние выводы об этом были неверны):
- (а) таймеры простаивающей ВМ запаздывают: RTT точен (70,16 ± 0,02 мс при 70) только при занятых всех 4 vCPU, в
  простое — 77 мс; завышение 4–13 мс у профилей с RTT до ≈100 мс;
- (б) скорость CPU при малой квоте нестабильна между прогонами: от сети 5 % дают 21–24 ops/s, от батареи —
  пропорционально (22–26 ops/s на 1 %); калибровать в тех же условиях;
- (в) загрузка в ВМ через usernet Lima переполняет очередь netem: 4g — 3,7 МБ то за 4,6 с, то за 27 с; ответам LLM
  в десятки КБ не мешает.

**Компоненты.**
- hw-mcp: опрос 8 датчиков — 12,2 мкс на шаг с открытием файлов и 2,7 мкс с удержанием, 0 выделений (ВМ).
- cam-mcp (ВМ, CPUQuota 25 %, синтетика 38 с): 182 кадра, 0 пропущенных, 2 эпизода, уведомление ≈450 Б; детектор
  ≈0,62 мс, CPU ≈3,5 мс на кадр. Память писать так: anon ≈4 МиБ, VmHWM ≈11 МиБ, `memory.peak` 13–14 МБ (≈8,4 МБ из них —
  кэш страниц бинарника). Каталог — 112 токенов (cookbook) / 185 (JSON); с `set_detector` — 197 / 358.
- sysfsim: CPU 0,112 с за 21,2 с, RSS 22,8 МиБ.
- robot-mcp (Mac): выход по SIGTERM ≈11 мс, HTTP с подписчиком ≈2 мс, код 0.
- `bench.sensors`, вызовов LLM: gate 9, heartbeat 36, hybrid 35.

**Из среза 07.10, на исправленном стенде не повторялось:**
- Аналоги (2 ч, k = 60, оракул): агент 77 вызовов / ≈0,20 млн входных токенов; самый экономный PicoClaw 102 /
  ≈0,39 млн → **≈1,9×**; nanobot 96 / ≈0,78 млн при k = 15 (`bench/baselines/smoke/20261007/table.md`,
  `experiments/results/review_runs_20261007/`). Агент ≈13 МиБ PSS в простое, ≈26–29 МиБ RSS на дне.
- robot-mcp против ros-mcp-server: каталог 274 / 2 221 токен, на задачу в 3,7–10× меньше токенов, трафик поездки
  ≈4–8 / ≈926 КБ. «stop 0,5 мс» — сборка 07.10, stop тогда не ждал ответа Nav2 на отмену; **перемерить**.

**Не измерено:** T0-день «таймер против фильтра», лестница, рабочий день робота, реальная LLM и плата, разброс.

## 8. Открытые проблемы (статус на 15.10; робот NeuroChip — на 19.10)

**Закрыто с 07.10.**
- Ревью агента. Проведено 09.10 (22 находки), исправлено и независимо проверено 14.10.
- Дефекты агента: `delta_tail` при ошибке LLM, порядок записей относительно `run_start`, нет переподключения MCP,
  ресинхронизация после `mcp_up`.
- Монитор не принимал трассы с обрывом: исправлено 15.10, трассы vmlink и twoservers дают 0 нарушений.
- hw-mcp:
  - uAPI v1 сделан; проверен на gpio-sim ядра 6.8, на ядре 4.9 — нет;
  - сделаны SessionTimeout и `sample_periods`;
  - дефект stdio linux-сборки 09.10 устранён: в смоуке hw-mcp работал по stdio.
- robot-mcp: отмена целей Nav2 подключена во всех местах — stop, таймаут, завершение, обрыв, подключение.
  Неподтверждённая отмена рвёт соединение. Внешняя отмена видна в `done` как `canceled`. Гонка stop ↔ цель закрыта.
- sysmon: OOM, пустой сэмпл, `--cgroup auto` — исправлено 07.10 в исходниках.
- cam-mcp написан. `board_health` sim-mcp совпадает по полям с hw-mcp.
- Сквозная проверка «агент + hw-mcp + sysfsim» сделана.
- monitor actuator_safety: на журналах шлюза настоящего hw-mcp в смоуках — 0 нарушений. Исходный случай
  ложного срабатывания отдельно не перепроверен.

**Открыто.**
- **Стенд «дня».**
  - `bench/metrics` не разбирает `deferred`, `user_reply.deferred`, `llm_probe.bytes`.
  - Оракул mockllm не умеет отвечать на слитый ход с несколькими вопросами. Решить к лестнице с обрывами.
  - Повторного ревью стенда после 07.10 не было.
- **Паритет аналогов.** Умолчания исправлены 07.10 (PicoClaw-best legacy, k = 15), сравнение не повторено. Правила:
  - в сжатом времени верны только счётные метрики (`timecomp.METRIC_VALIDITY`);
  - PicoClaw, nanobot и ros-mcp-server не патчить; mock-llm под них не править — только `llmtap`.
- **Агент.**
  - Сбой подписки или чтения при старте не повторяется.
  - Поздние ответы (после 202/504) идут только в трассу.
  - Нет `_meta edgectx/cmd_id`.
  - Хвост слитого хода — до 1024 строк.
  - Множитель 2 цепочки дебаунса — допущение. Окно коалесценции 10 с — фактор W эксперимента.
  - `ImageContent` заменяется текстом.
  - Нет `_meta edgectx/by`, периодических правил, `configs/robot.yaml`.
- **hw-mcp.**
  - Настоящие датчики и ядро 4.9 не проверены.
  - DS18B20 на 4.9 не найдётся: атрибут `temperature` появился в 5.8, разбора `w1_slave` нет.
  - Новые устройства после старта не добавляются (uevent нет); пропавший чип GPIO не переоткрывается.
  - Вернувшееся устройство находится через ≤ 5 мин (допущение).
  - SessionTimeout не проверен с PicoClaw/nanobot.
  - `-path` без «/» роняет ServeMux; в cam-mcp проверка есть — перенести.
- **cam-mcp.**
  - Нет проверки на UVC-камере и Jetson.
  - V4L2 не переоткрывается после отключения USB.
  - В покое камера стримит на `-active-fps`.
  - Снимок до LLM не доходит.
  - Нет SessionTimeout.
  - Блока cam-mcp нет в `agent/configs/example.yaml` (образец — README cam-mcp).
- **robot-mcp.**
  - Исправления 09.10 не проверены на Gazebo/Nav2: ответ `cancel_goal`, торможение, задержка stop.
  - «Молчащая» связь обнаруживается за ≈2,75 мин, пинга нет — решение студента.
  - Если Nav2 не ответил на отмену, stop не шлёт ноль.
  - Флаги `robot-probe` и `fake-rosbridge` в README не описаны.
  - Робот к агенту не подключён. План для TB3 — `robot_scenario.md` §12–13 (≈14–17 дней), для NeuroChip — PLAN Э3.
- **board.**
  - Окно гонки: `trap` ставится после фонового `systemd-run`.
  - `wall_s` и расписание считаются по часам ВМ и прыгают после сна Mac (§12).
  - Успешный путь `verify`/`calibrate` без юнит-тестов.
  - Не решено, как учитывать ограничения (а)–(в) в лестнице.
  - Не проверены: память 1024–24 МиБ, часть квот, sd-slow, несколько `run` в одном netns, прогоны длиннее минуты,
    `cpubench` на Jetson.
  - SHA в `env` прогона `_f` не совпадают с текущими `probe.py`, `calibrate.py`, `verify.py`: правились комментарии,
    это оговорено в README.
- **Робот NeuroChip** (19.10; подробно — §14 и `experiments/results/neurobot_20261018_acceptfix2/`, `notes.md` §7).
  - На роботе не проверено ничего (HIL); 8-часового прогона нет — K8 (наклон памяти за 8 ч) не измерен.
  - Ждут решения студента (PLAN §13.6): K6 «stop через MCP» и `nodata_unseen=slow`.
  - На двойнике видимые перезапуски — только от смерти базы: kill -9 узла базы перезапускает Gazebo, мир начинается
    заново, удар IMU, защёлка `shock`, её снимает человек (2 за 34 мин, 3 за 1 ч). На роботе так быть не должно —
    [допущение до HIL].
  - Сторож не лечит зависание всех датчиков сразу и SIGSTOP `twist_mux` (README deploy, «Ограничения»).
  - Геймпад `/cmd_vel_joy` идёт в `twist_mux` мимо `safety_gate`: только предупреждение (приёмка 17.10, P4 — 0,20 м
    мимо L2).
  - Не защищено по построению: низкий предмет вплотную вне поля камеры, задний ход (сзади датчиков нет), не
    отражающий лазер предмет у корпуса, нависания (README safety, «Что НЕ защищено»).
  - Верная реакция на жест — 38 из 80 (A0 — 28 из 80), в основном из-за неверного класса SNN на синтетических формах;
    K1 p95 ≤ 0,5 с на стенде не выполнено (0,95 с).
  - Нужны ответы лаборатории: что urg_node2 отдаёт на «нет отражения» (NaN или +inf), запись `/camera/imu` на
    остановках (порог `shock`), `cmd_vel_timeout` базы, поведение STM32 при тишине `/cmd_vel` (`neurobot/lab/
    LAB_HANDOUT.md`, ANALYSIS §5).
- **sysmon:** бинарники `bin/*` устарели. `go 1.25` в sysmon и cpubench при 1.27.1 у остальных.
- **Предрегистрация** не написана.
- **Пакет для руководителя (07.10) устарел:** «cam-mcp нет», «ревью агента прервано», stop 0,5 мс, ориентир H6.

## 9. Документы
- **Для руководителя (07.10):** `docs/supervisor_pack/01_korotko.html` … `05_plan.html`, `out/` — PDF/DOCX и общий
  PDF (26 стр.). Конвертер — `tools/html2docx.py`. Нужен Python с python-docx:
  `/Users/chosenundead/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3`. PDF — Chrome
  headless. План ВКР — `docs/plan/vkr_plan.html` + PDF/DOCX; `05_plan.html` — его копия, править оба.
- **Сенсорное ядро:** `docs/SENSOR_CORE.md`. **Спецификация:** `IMPLEMENTATION_SPEC.md` (обновляется 15.10).
- **Журнал решений:** `docs/DECISIONS.md`. Записи этапа 09–15.10 ([edgectx], [hw-mcp], [cam-mcp], [board], [sysfsim],
  [robot-mcp], [monitor], [стенд]) вносятся при обновлении 15.10. Первоисточник — README компонентов.
- **README компонентов** — главный источник фактов о коде: `agent/`, `hwmcp/`, `cammcp/`, `robotmcp/`, `sysmon/`,
  `bench/*/` (в т. ч. `board/`, `sysfsim/`, `sensors/`), `scripts/board/`.
- **Артефакты этапа** (`experiments/results/`):
  - `minboard_smoke_20261014_a/README.md`;
  - `board_verify_20261014_f/` — итог; a и b — первый код, c — оборван, d и e — до переноса netem на ifb;
  - `hwmcp_vmchecks_20261014_a/`, `sysfsim_vmcheck_20261015_b/`;
  - `agent_20261014_{vmlink,twoservers}/`, ранние `agent_20261009_*`;
  - `cammcp_vm_smoke_20261009_*_r2/`.
- **Робот NeuroChip:** `docs/neurobot/ANALYSIS.md`, `PLAN.md` (§13 — 15.10; статус W3 и решения студента —
  §13.5–13.6, 19.10), `research/` — R1 демо, R2 ros_mcp_server, R3 PicoClaw, R4 переиспользование, R5 внешние
  источники, R6 симуляция, R7 платформа и презентация. Ведёт параллельный процесс — только читать.
- **Прочее:** `docs/robot_sim.md`, `docs/methodology_refs.md`, `docs/monitor.md`; ГИА — `research/consultation-gia-1/`.
- **Концепция** — `research/new_topic/`. Устарели частично: `THESIS_CONCEPT.md` ред. 2 и `EXPLAINER` (плата только
  RPi 5, робот вне ядра); `robot_scenario.md` §12.3, §13 п. 6, §10.2, §6.1 (stop теперь ждёт ответа на отмену).
- **Приоритет при расхождении:** поздние записи DECISIONS > README компонентов > supervisor_pack/vkr_plan >
  robot_scenario > THESIS_CONCEPT.

## 10. Что дальше (начинать только после указания пользователя)
0. **Спросить пользователя:**
   - итоги встречи с руководителем по пакету 07.10 (в проекте не записаны);
   - оформлено ли закрепление (ГИА 15–30.10);
   - согласована ли замена NanoClaw на nanobot;
   - станет ли NeuroChip сценарием «мобильный робот»;
   - модель Jetson и доступ к нему; периферия;
   - можно ли тратить ключ RouterAI.
1. **Робот NeuroChip.** Шаг W3 (Э2 + Э2.5 на двойнике) сделан (§14). Дальше по порядку:
   - решения студента по PLAN §13.6 (K6 «stop через MCP», `nodata_unseen`), затем коммит правок `neurobot/` и
     документов 16–19.10 (после коммита 28e85dd);
   - поставка пакета 1 лаборатории (ROADMAP §3) и ответы на `LAB_HANDOUT.md`;
   - HIL по чек-листу PLAN §6; 8-часовой прогон (K8) — по протоколу Э4;
   - Э3 — после HIL; эксперименты Э4 — только после согласования с руководителем.
2. **Предрегистрация** `experiments/PROTOCOL.md` — до первых полных дней. В неё: пороги гейта и hw-mcp,
   `debounceChainMax`, окно коалесценции, `-stuck-s`, пороги robot-mcp 0,1 м / 0,1 рад.
3. **Лестница ограничений** поверх `board.sh run` и `Vm.run(..., timeout)`:
   - решить, как учитывать ограничения (а)–(в);
   - калибровать CPU в тех же условиях;
   - пересобрать sysmon; cgroup юнита — `system.slice/board-run-<имя>.service`;
   - метрики и оракул для `deferred`.
4. **Полные T0-дни:** heartbeat / gate / hybrid; политики истории; аналоги при честном паритете.
5. **Робот ⇄ агент:** robot-mcp в EdgeCtx (NeuroChip — профиль base, TB3 — robot_scenario §12). Проверить robot-mcp на
   Gazebo/Nav2, перемерить stop.
6. **С разрешением тратить RouterAI:**
   - проба провайдера: `cached_tokens`, размер `/models`, Retry-After;
   - выбор модели и проверка на живой модели;
   - бюджет ≈1,3–1,5 тыс. ₽ на gpt-4o-mini, с запасом ×1,5.
7. **С доступом к Jetson:**
   - дымовой тест: `cat /proc/device-tree/model; free -h; uname -r; ldd --version`, доступ к routerai.ru;
   - uAPI v1 на 4.9; DS18B20;
   - `cpubench`/`calibrate` → CPU пресетов `jetson-nano-*` в `bench/board/profiles.json`;
   - 8-ч прогоны.
8. **ПЗ:** глава 1 к 15.01.2027; обновить supervisor_pack/vkr_plan к следующей встрече; объяснять студенту просто.

## 11. Что удалено и где копии
- `~/.Trash/diploma-cleanup-2026-10-06/` (legacy, agentrt, llama.cpp, BFCL…) и `~/.Trash/diploma-cleanup-20261009/`
  (по отчётам 09.10 — устаревшие смоуки cam-mcp). **15.10 по этим путям их нет** (`stat`: No such file or
  directory) — вероятно, Корзину очищали; восстановимость не гарантирована.
- `~/.Trash/diploma-cleanup-20261014/` (есть):
  - `board/before-fix`;
  - `integration/`: `before/` — копии до правок 15.10; `run2-bin/` — agent `624eca9d…`, hw-mcp `80f9e388…`;
    `superseded/sysfsim_vmcheck_20261015_a`; `pycache/`;
  - `robotic_platform_secrets/` (§12);
  - каталоги процесса NeuroChip: `deploy`, `lab`, `onboard`, `gestures`, `gestures-review`, `stand`.
- `~/.Trash/diploma-cleanup-20261015/docs/` — копии документов до обновления 15.10.
- **Резервы вне Корзины:** `~/Documents/ChatGPT/диплом/backups/`:
  - `sensor-core-20261007-01.*`;
  - `minboard-20261009-1445.before.tar.gz` — весь проект, 407 МБ;
  - `scripts-sim-2026101{4,5}`;
  - `neurobot-sim-stand-20261015`, `gestures-20261015`, `lab-20261015`.

  Рабочая копия 07.10 — `~/Documents/ChatGPT/диплом/sensor-core-20261007-01/`.
- Qwen3-8B — `~/Projects/rudojo/models/` (чужой проект, не трогать). `board_verify_20261014_c` (оборванный) оставлен.

## 12. Правила работы и ловушки
- Русский, коротко; пользователь неформален. Загрузки > 1 ГБ — спросить. Удалять только в
  `~/.Trash/diploma-cleanup-<дата>/`, перед правкой класть туда копию.
- **Честность:**
  - всё с Mac, ВМ или заглушки — «ориентир, не результат»;
  - «в проверенных источниках не найдено» вместо «никто не делал»;
  - готовое (ROS, Gazebo, gpio-sim, netem, cgroup, systemd, MCP SDK, PicoClaw, YOLOv5, snnTorch) не выдавать за своё;
  - разделять «реализовано / проверено (чем) / не проверено на железе»;
  - загрузки фиксировать: версия, размер, SHA-256, лицензия.
- `research/` — только чтение. Python — с `PYTHONDONTWRITEBYTECODE=1`. rosbridge дальше 127.0.0.1 не публиковать.
  Замеры для ВКР — только по протоколу: от сети, без фона, повторы. «ultracode» — крупное через Workflow с ревью.

**Ловушки:**
- **С 16.10 проект под git** (https://github.com/NikkiRe/diplom, публичный). Коммиты — от имени студента, без строк соавторства ИИ (просьба студента). Материалы лаборатории, артефакты прогонов, данные CASAS и сборки в репозиторий не входят (`.gitignore`).
- **`experiments/results` не перезаписывать.** `robotmcp/tools/live_smoke.sh`, `scripts/sim/rosmcp_smoke.sh` и
  `smoke.sh` по умолчанию пишут в `*_<сегодня>`, первые два — с `rm -rf`: всегда задавать новый OUT. Перезаписывают
  также `--force` у `bench.replay`/`drive_*` и `make golden`. `bench.simmcp --journal` дописывает.
- **Бинарники:** `agent/bin` и `hwmcp/.build` актуальны на 15.10; **`sysmon/bin` устарели**. После правок
  пересобирать и обновлять копии в ВМ. `drive_edgectx` берёт `agent/bin`.
- **gpio-sim на ядре 6.8: use-after-free в `lineevent_release`.** Если снять чип, пока линию держит потребитель
  (`gpiomon`, hw-mcp, `gpioset`, тест): «REMOVING GPIOCHIP WITH GPIOS STILL REQUESTED» → WARNING `gpiod_free` →
  Oops → процессы в состоянии D. Лечится только перезагрузкой ВМ (09.10: `tainted` 640). Порядок:
  1. завершить потребителей и дождаться выхода;
  2. по `/sys/kernel/debug/gpio` убедиться, что линии свободны;
  3. `live=0`, `rmdir`;
  4. `tainted` проверить до и после.

  Образец — `hwmcp/tools/vm_gpiosim_check.sh`. На ядре с `tainted` ≠ 0 проверки не запускаются. Параллельным
  прогонам — разные метки чипов.
- **Сон Mac и часы ВМ.** После сна хоста часы ВМ переводятся скачком. `board.sh` считает `wall_s` и расписание по
  `EPOCHREALTIME`. Видели `wall_s` 598 с у прогона ≈1 с; ВМ-тест упал при 12,5 с монотонных и 25:53 настенных;
  расписание догоняет пропущенные шаги пачкой. Длинное — под `caffeinate -i`.
- **`limactl shell` без tty не передаёт сигналы в ВМ.** Срок ставит сама ВМ: `board.sh run --timeout`, а
  `Vm.run` при своём таймауте останавливает юнит.
- **Общие ВМ.** Имена с `<pid>`, чужое не трогать. `BOARD_VM=1` не запускать параллельно с verify или лестницей.
  CPU и задержки при чужой нагрузке не сравнимы.
- **Секреты лаборатории.** В архиве `third_party/neurochip/robotic_platform` были токен VK и ключи API. Рабочая
  копия вычищена, оригиналы — `~/.Trash/diploma-cleanup-20261014/robotic_platform_secrets/`. В журналах с робота —
  URL long poll VK с `key=`, в документах они `<REDACTED>`. Секреты никогда не выводить и не цитировать.
  `neurobot/lab/redact.py` ловит ключ VK только как JWT (R7 N18) — проверять вывод `collect_diag`.
- **Сторонний pickle.** Веса `yolo_hand.pt`, `snn_emnist_small.pth` — непроверенный pickle.
  - На Mac `torch.load`/`pickle.load` не делать — только статический разбор (zipfile + pickletools, R1).
  - Исполнять — только `weights_only=True` в изолированной одноразовой ВМ и с разрешения пользователя.
  - `yolo_hand.pt` вне офлайн-ВМ не исполнять (PLAN §8).
- **`neurobot/` и `docs/neurobot/`** ведёт параллельный процесс — не править.
- **Корзина:** `ls ~/.Trash` → «Operation not permitted» (защита macOS); подкаталоги открывать по полному пути.
- **Ядро `sbc`** может смениться при перезагрузке — тогда поставить `linux-modules-extra-$(uname -r)`.
- **Память Claude:** `~/.claude/projects/-Users-chosenundead-Projects--------/memory/` — привязана к
  `~/Projects/инфобез`, из `diploma-edge-ai` читать вручную. При расхождении верить HANDOFF и README.

## 13. Порядок чтения для нового агента
1. Этот файл.
2. `docs/SENSOR_CORE.md`; `experiments/results/minboard_smoke_20261014_a/README.md`.
3. README: `agent/`, `hwmcp/`, `cammcp/`, `bench/board/` и `scripts/board/`, `bench/sysfsim/`, `robotmcp/`, `sysmon/`,
   `bench/*/`.
4. `docs/DECISIONS.md` от «2026-10-06 (после встречи с руководителем)» до конца; выше — отменённая старая тема.
5. `IMPLEMENTATION_SPEC.md`.
6. `docs/supervisor_pack/01_korotko.html`, `04_sdelano.html`, `docs/plan/vkr_plan.html` — с поправкой на §8.
7. Робот: §14 → `docs/neurobot/ANALYSIS.md` (§0, §5.1, §5.2) → `PLAN.md` (§0, §7, §13) →
   `experiments/results/neurobot_20261018_acceptfix2/compare.md` → README `neurobot/safety`, `neurobot/deploy`,
   `neurobot/sim/twin`, `neurobot/sim/runs` → R1–R7 по необходимости.
8. По необходимости: `THESIS_CONCEPT.md`, `AGENT_LANDSCAPE.md`, `robot_scenario.md`, `docs/robot_sim.md`, ГИА,
   `research/new_topic/landscape/L4_jetson_nano.md`.

## 14. Робот лаборатории NeuroChip (с 14.10)

**Что это.** 14.10 студент принёс задачу из лаборатории NeuroChip (ИТМО).
- Железо: демо-робот на Jetson Nano (README: Ubuntu 20.04, ROS 2 Foxy, Python 3.8), RealSense D435i, лидар URG-04LX,
  STM32F103 + L298N по UART.
- Демо `predict_live.py` — один процесс: YOLOv5s «hand» → траектория → холст 28×28 → SNN 784-128-4 (snnTorch,
  EMNIST) → класс 0..3 → манёвр с `time.sleep` 3–11 с → `/cmd_vel`. Сверху — PicoClaw и MCP лаборатории.
- Жалобы: «после пары использований надо перезагружать», задержки, проблемы с запуском.
- Цель лаборатории: помахал — поехал; часами без ручных перезапусков; безопасная остановка; агент понимает команды,
  но не стоит в контуре жеста.
- Для ВКР — кандидат на сценарий «мобильный робот» (быстрый рефлекс + событийный агент сверху, H8). Не решено.
- В анализе код лаборатории не запускали и не меняли, веса разобраны только статически.

**Что нашли (ANALYSIS §0)** — пометки источника: [код], [опыт], [замер] (ориентир), [оценка], [гипотеза].
1. Рефлекс «жест → движение» уже работает без LLM: демо само публикует `/cmd_vel` [код]. Задача — быстрый и
   надёжный локальный контур и правильное подключение агента сверху.
2. «Надо перезагружать» — несколько независимых механизмов [код]:
   - поток камеры молча умирает от первого исключения, процесс жив и «глух»;
   - PicoClaw не поднимает упавший stdio-сервер, строка в stdout рвёт связь навсегда;
   - `/reload` убивает stdio-серверы; у вызова инструмента нет таймаута;
   - инструменты камеры и лидара в MCP лаборатории не работают никогда.
3. Задержку задаёт конструкция: жест закончен через 1,5 с без руки → ≈1,6–1,8 с до команды [оценка]. SNN
   ≈0,6–0,8 мс [замер]. После команды робот 3–11 с «глух».
4. Предобработка: холст в 3–25 раз бледнее MNIST. «Немая» SNN → класс 0 → разворот на 7 с. Таких ответов 7–88 %
   [замер на синтетике].
5. Безопасность держится на неизвестной прошивке STM32: при препятствии и выходе демо не шлёт ноль, а замолкает.
6. Через PicoClaw реального времени нет [оценка]: событие = 2 вызова LLM по ≈3,1–3,6 тыс. входных токенов, ≈3–6 с.
   Толчки MCP ≤ 0,044 м: «0,5 м» = 13–17 вызовов [оракул].

**Ответы и новые материалы (ANALYSIS §5.1, §5.2).**
- **Первые ответы.** Перезагружают вручную, примерно после 3–4 жестов. Поведение робота при тишине `/cmd_vel`
  неизвестно — проектируем от худшего. Жесты — «нарисованные цифры и помахать». Гипотезы причины:
  1. камера отваливается под нагрузкой моторов (питание/USB);
  2. ≈10 новых WS-соединений к rosbridge 1.3.1 на команду;
  3. stdio и `/reload` PicoClaw.
- **Цепочка движения:** `/cmd_vel` → `twist_mux` → `diff_drive_controller` (ros2_control) → STM32. По умолчанию в Foxy
  `cmd_vel_timeout` 0,5 с: робот, **вероятно**, тормозит через ≈0,5 с тишины. Издатели `/cmd_vel_tracker` и
  `/cmd_vel_joy` перекрывают наш ноль по приоритету.
- **Лидар.**
  - Вероятно, не видит прямо по курсу: блок NaN из 186 лучей от −60,8° до +4,2° (корпус D435i перед URG), ≈53 %
    сектора остановки. Демо отбрасывает NaN молча.
  - На высоте ≈0,67 м не видит низких препятствий.
  - Порог 0,25 м отсчитывается от оси при крае базы 0,21 м: запас ≈4 см против выбега 0,16–0,23 м.
- **Агентский стек на роботе.**
  - PicoClaw 0.2.8, gpt-4o-mini через cloud.ru, каналы VK и pico.
  - Единственный MCP — прокси к воркеру :8766; кода воркера нет, 29.09 — «Connection refused».
  - Дубли инструментов: FastMCP берёт первую регистрацию, миллисекунды уходят как `duration_s` (2000 или 300) — опасно.
- **Журналы:** 52 остановки — все ручные; медиана 2 вызова между перезапусками, в 85 % жизней ≤ 4.
- **Жесты по слайдам:** круг → полный оборот, дуга → вперёд, горизонталь → направо/налево. Геометрия [оценка по
  фото]: палубы Ø0,39 м, колёса Ø0,072 м, колея ≈0,38 м, ≈4 кг.

**План (PLAN §0, §7).**
- Три опоры:
  1. рефлекс замыкается на роботе, без LLM, MCP и rosbridge;
  2. у `/cmd_vel` один писатель с явным нулём;
  3. каждый компонент — отдельный процесс под systemd, живость по прогрессу.

  Агент (PicoClaw или EdgeCtx) работает сверху по MCP/HTTP: 0 вызовов LLM на жест.
- Этапы, итого ≈29 рабочих дней (пакет 1 — через ≈8,5 д, пакет 2 — через ≈19,5 д):
  - Э0 — факты с робота: LAB_HANDOUT, `collect_diag.sh`;
  - Э1 — стенд и базовая линия A0;
  - Э2 — пакет 1: perception v2, страж `/cmd_vel`, юниты, правка ros_mcp_server ≤ 20 строк, сторож, RUNBOOK;
  - Э3 — пакет 2: executor v2, robot-mcp base/compat, EdgeCtx `neurobot.yaml`;
  - Э4 — эксперименты, только после согласования с руководителем;
  - Э5 — текст.
- Не делаем: LLM в контуре жеста, патчи PicoClaw, `twist_mux` как стоп, Nav2, переобучение сетей, распознавание в
  движении, траты RouterAI, задержки стенда как результат.
- **PLAN §13 (15.10), этап Э2.5** — ответ на запрос пользователя.
  - **Цифровой двойник** вместо TB3. Модель по фото; корпус камеры сам даёт слепой сектор лидара. Датчики: URG
    683 луча, RGB-D, IMU, одометрия. Граф как на роботе: gz_ros2_control, `diff_drive_controller`, `twist_mux`.
    Миры: низкий ящик 0,15 м, ступенька, пандус.
  - **Бюджет Jetson 4 ГБ.** Всё бортовое — в cgroup «jetson» в ВМ `sim`. MemoryMax = 4 ГБ минус ОС [допущение];
    память драйверов и CUDA — «балластом». Квота CPU — по соотношению Cortex-A57 и хоста [допущение]. Лестница
    4 → 3 → 2 ГБ.
  - **Безопасность v2, слои L0–L3:**
    - L0 — аварийная кнопка;
    - L1 — пределы контроллера;
    - L2 — `safety_gate`, единственный писатель `/cmd_vel`: тормозной путь от края корпуса; NaN в секторе = стоп;
      глубина для низких препятствий и обрыва; IMU; пробуксовка; геозабор; блокировка старта; защёлка estop
      снимает только человек;
    - L3 — инструменты агента ≤ 1 м и ≤ 0,15 м/с.
  - **Проверка на двойнике:** датчики (темы, частоты, TF, слепой сектор, ящик, пандус) и набор безопасности
    (стена, ящик, человек, ступенька, наклон, упор, убийство стража, rosbridge, MCP, агента, «300 секунд»,
    лишний геймпад). Это стенд, то есть ориентир; итог — только HIL на роботе.

**Статус на 19.10 — шаг W3 на двойнике сделан; на роботе не проверялось ничего.**
- **Построено** (`neurobot/`, README в каждом каталоге): perception v2 (патч к `predict_live.py`), исполнитель v1,
  `safety_gate` (L2), развёртывание (`neurobot.target`, юниты, обёртка `ros2 launch`, сторож, `neurobot doctor`,
  защёлка, патчи MCP лаборатории, конфиг PicoClaw), двойник `sim/twin`, наборы `sim/suites`, прогоны `sim/runs`,
  бюджет `sim/budget`, генератор жестов `sim/gestures`, материалы для лаборатории `lab/`. Готовое (ROS 2, Gazebo,
  gz_ros2_control, twist_mux, YOLO, PyTorch, PicoClaw, MCP SDK) своим не считается.
- **Ход W3** (артефакты — `experiments/results/neurobot_2026101{6,8}_*`, у каждого `manifest.json`):
  1. 16.10 — прогоны A0 (стек лаборатории как на роботе) и A1 (пакет 1 + безопасность v2) по одному расписанию жестов
     и отказов, 1 ч, бюджет 4 ГБ, start_test (`…_20261016_{a0,a1,long,budget_a1,start}`).
  2. 17.10 — независимая приёмка: принято частично, к HIL не готово. Не принято: «0 касаний» (в `main_a1` — касание
     стула); сравнение с A0 без пробега (A1 в зале стоял в `blocked` 73–80 % времени, пробег 6,3 м против 42,4 м у
     A0 — пересчёт 18.10 без переносов робота); K1 с ранними жестами; агент отвечал «готово» без движения; rosbridge
     и драйверы на `ros2 launch` не поднимались после смерти узла; K6 по MCP и по скану не измерен; защёлку снимал
     любой узел ROS с `by: human`.
  3. 18.10 — доработки: `safety_gate` (`…_20261018_safety_tune`), развёртывание и агентский путь
     (`…_20261018_deploy_fix`), метрики и повтор A1 (`…_20261018_a1v2`). Незакоммиченные правки прерванного
     исполнителя 17.10 исполнители компонентов проверили: годное приняли, негодное переделали на месте; `git checkout`
     не делали — коммит 28e85dd старше части работы 16.10, откат унёс бы и её. Повторная приёмка 18.10 подтвердила
     закрытие прошлых замечаний и дала пять новых: тупики защиты (робот 221 с стоял у ножки стула), слепой лидар под
     именем драйвера (S3b: 1,49 м за 10 с на одной глубине), решения студента по K6 и `nodata_unseen`, доказательства
     на итоговом коде, учёт правок 17.10.
  4. 19.10 — доводка (`…_20261018_acceptfix2`): исполнитель сам отъезжает назад из тупика по своему следу, полный
     отказ лидара — `lidar_fault`; 1 ч, бюджет и start_test повторены на итоговом коде; проект решений для PLAN
     §13.6. Независимо не перепринималась.
- **Итог на двойнике** — `experiments/results/neurobot_20261018_acceptfix2/compare.md` [замер на двойнике, ориентир
  стенда, не результат робота]. Итоговый столбец снят в комнате `lab` 5 × 5 м, остальные — в зале `hall` 9 × 9 м:
  сравнение по строкам — ориентир, а не мера улучшения кода.

  | Метрика | A0, зал | A1 16.10, зал | A1 18.10, зал | A1 итог, `lab` |
  |---|---|---|---|---|
  | K1 t0 → `/cmd_vel`, p50 / p95, без ранних жестов, с | 1,81 / 2,11 | 0,66 / 0,95 | 0,68 / 0,97 | 0,67 / 0,95 |
  | Верная реакция на жест | 28 из 80 | 19 из 80 | 37 из 80 | 38 из 80 |
  | Пробег; доля `blocked` | 42,4 м; — | 6,3 м; 79 % | 22,6 м; 13 % | 22,1 м; 2 % |
  | Касания (истина Gazebo), все прогоны столбца | 23 | 2 в 4 прогонах | 0 в 3 | 0 в 3 (`lab_v5`, `long_v5`, `budget_v5b`) |
  | Вмешательства человека / видимые перезапуски | 8 / 7 | 1 / 1 | 2 / 2 | 2 / 2 — обе смерти базы двойника |
  | Тупики защиты (стоит дольше 60 с) | — | 0 | 1 (221 с) | 0 |
  | «Вперёд» агенту: поехал или сообщил о запрете | 0 из 3 | 0 из 3 | 3 из 3 | 3 из 3 |

  - Вызовов LLM на жест — 0 у обеих систем; K6 «едет без свежей команды > 0,5 с» — у A0 5 эпизодов, у A1 0 (не
    считая перерывов мира двойника, когда робот по истине стоит).
  - 1 ч на итоговом коде (`runs/long_v5`, зал): касаний 0, глухих жестов 0; 31 отказ по расписанию — компоненты
    поднялись сами во всех; видимых перезапусков 3 — смерти базы двойника, после которых защёлку `shock` снимал
    человек; 1 тупик — отъезд за 3,5 с; K1 0,67 / 0,91 с (первая треть 0,61 / 0,78, последняя 0,60 / 0,87); сумма
    PSS компонентов пакета без драйверов двойника 620,9 → 630,8 МиБ (7,1 МиБ/ч), по процессам наклон до 2,8 МиБ/ч
    (сторож) — за 1 ч это не K8 (≤ 1 МиБ/ч на процесс за 8 ч).
  - Бюджет 4 ГБ (`runs/budget_v5b`: весь A1 в `jetson.slice`, MemoryMax 2406 МиБ — ветка GNOME): OOM 0, уникальная
    память 1866,6 МиБ, запас — headless 1469, GNOME 539 МиБ (пессимистично 183). Модель бюджета — [допущение] до
    замера на Nano; числа по CPU ненадёжны.
  - start_test (K9; холодный старт — рестарт `neurobot.target`, не перезагрузка ОС): 101 из 101, сломанные старты
    5 из 5 — doctor назвал верно; «ОК» doctor — максимум 20,55 с при T = 26 с (правило ⌈1,2 × максимум прошлых
    циклов⌉ зафиксировано до замера, `k9_T.json`).
  - Смерть и зависание драйвера внутри `ros2 launch`: kill -9 узла лидара или камеры — снова работает через
    1,4–1,7 с, SIGSTOP — 3,8–5,5 с, kill -9 rosbridge — 2,8–3,1 с; 16.10 — 97,6 с и человек
    (`…_20261018_a1v2/notes.md` §4, `…_20261018_deploy_fix/manifest.json`).
  - K6 (серия 18.10, `…_20261018_a1v2/k6/k6_final_n30.json`, n = 30; на итоговом коде не перемерялась — путь стопа
    не менялся): stop через MCP — торможение начинается за 33 / 62 мс (p50 / p95), ноль в `/cmd_vel` — через
    514 / 545 мс: штатный стоп плавный, по букве PLAN «ноль ≤ 100 мс» не выполнено; препятствие — ноль после скана
    за 0,4 / 0,7 мс, 30 из 30. Агент: 0 неправдивых ответов из 20 (та же серия, `k6/agent_truth_n5.json`).
  - Наборы безопасности (`neurobot/sim/suites`): полный набор (23 сценария) и датчики 10 из 10 — на коде 18.10
    (`…_20261018_safety_tune/safety.md` §4): касаний 0 везде, где остановка возможна; касания там, где не видит ни
    один датчик или L2 не действует по построению: низкий предмет вплотную вне поля камеры, задний ход в стену,
    геймпад мимо `safety_gate`. На итоговом коде 19.10 (свой отъезд исполнителя, `lidar_fault`) — регрессии `final3`:
    11 сценариев, касаний 0, и 5 проверок датчиков (`…_20261018_acceptfix2/notes.md` §2, PLAN §13.5).
- **Не выполнено и открыто** — §8 («Робот NeuroChip»): решения студента по PLAN §13.6, HIL, 8 ч, видимые
  перезапуски от смерти базы двойника, верная реакция 38 из 80, K1 p95 ≤ 0,5 с.
- Артефакты 14–15.10 (`neurobot_2026101{4,5}_*`) — промежуточные; сводка 16.10 (`…_20261016_a1/compare.md`) приёмкой
  17.10 частично отвергнута — цитировать итоговую сводку.
- Ответы лаборатории частичные. Вопросы №1–3 ANALYSIS §5 блокируют непрерывный режим: STM32 и мост, вид
  «перезагружать», версии. Пока таймаут STM32 неизвестен — робот только под присмотром.
