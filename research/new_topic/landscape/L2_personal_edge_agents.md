# L2. Личные и локальные агенты на собственном железе: проверенная карта (на 2026-10-06)

Область: «Claw»-семейство (OpenClaw и лёгкие клоны), агенты на микроконтроллерах, голосовые edge-ассистенты, общие агенты-хосты MCP. Цель: понять, что реально существует и используется. Отдельно — что из этого запустится на кафедральном Jetson Nano и что можно делать на MacBook прямо сейчас.

Смежные заметки, которые я не дублирую:
- `../C_llm_agents_robotics_iot.md` — научные работы (SayCan, ROSA, IoT-MCP, DCP), PicoClaw с аппаратной стороны (§3.5 [23]).
- `../B_mcp_hardware.md` — MCP для оборудования.
- `../E_time_cost_metrics.md` — метрики «во времени» и стоимость. Там разобран баг OpenClaw #61690 о стоимости heartbeat.
- `../G_edge_ai_onboard.md` — инференс на Pi и Jetson.

Метки достоверности:
- **[заявлено]** — README или документация самого проекта.
- **[незав.]** — независимый источник: статья или отчёт пользователя.
- **[моё]** — посчитано мной: `wc -l` по shallow-клону, размер файла релиза из GitHub API.
- **[не проверено]** — вывод или гипотеза без проверки.

Ничего не устанавливалось и не запускалось. Код читался через `git clone --depth 1` в scratchpad, метаданные — через GitHub API, PyPI JSON, npm registry, pypistats.

---

## 0. Коротко

1. **То, что назвал руководитель, существует и это целое семейство.** Родоначальник — OpenClaw: ★391 493, создан 2025-11-24, история имён Clawdbot → Moltbot → OpenClaw, январь 2026. За октябрь 2025 – октябрь 2026 появились десятки «клонов»: nanobot (Python, ★48.8k), ZeroClaw (Rust, ★32.9k), NanoClaw (TS, ★30.9k), PicoClaw (Go, ★30.0k), IronClaw (Rust, ★12.6k), NullClaw (Zig, ★8.1k), MimiClaw (C/ESP32, ★5.8k), Moltis, zclaw, ESP-Claw от Espressif и др. Архитектурный паттерн у всех один: процесс-шлюз, мессенджер-каналы (Telegram почти везде), внешняя LLM по API, цикл вызова инструментов, файлы памяти (`MEMORY.md`, `USER.md`, `HEARTBEAT.md`), cron/heartbeat, skills и MCP.
2. **На кафедральном Jetson Nano со штатным JetPack 4.6 Node.js-агенты не запустятся.** Это OpenClaw, NanoClaw, NemoClaw, ClawBox, TinyClaw. JetPack 4.6.6 / L4T R32.7.6 — последний релиз для Nano: Ubuntu 18.04, ядро 4.9, glibc 2.27, Python 3.6. А Node 22/24 для arm64 требует glibc ≥ 2.28 и ядро ≥ 4.18. Реальные кандидаты для Nano — статические бинарники: PicoClaw (Go, `CGO_ENABLED=0`), ZeroClaw (сборка `aarch64-musl`), NullClaw (Zig, заявлен статический). Ещё Python-агенты (nanobot) с Python 3.11+ через `uv` (python-build-standalone требует glibc ≥ 2.17). **«Jetson Nano на 1 ГБ» не существует.** Бывают Jetson Nano Developer Kit на 4 ГБ (2019) и Jetson Nano 2GB Developer Kit (2020). По RAM это укладывается в официальный минимум OpenClaw (1 ГБ), но блокирует софт, а не память.
3. **Почти все цифры RAM заявлены самими авторами, и сравнительные таблицы кочуют из README в README.** Пример: «OpenClaw > 1 ГБ, NanoBot > 100 МБ, PicoClaw < 10 МБ, ZeroClaw < 5 МБ, NullClaw ~1 МБ». Независимых измерений я нашёл два:
   - arXiv 2608.27886 (2026-08-28). OpenClaw и NanoBot на gpt-4o-mini: медиана пикового потребления памяти **2926.6 MiB против 136.1 MiB**, медиана времени 34.1 с против 10.4 с, полностью выполнено 31 % и 25 % задач. Оговорки: Docker/WSL2, способ замера памяти не описан.
   - Отчёт пользователя ZeroClaw (#8642, v0.6.8). При 4 MCP-серверах и 34+ инструментах RSS растёт **~100 МБ → 2.5 ГБ за минуты**. Заявленная цель проекта — «< 5 MB RAM», и в собственном документе FND-001 она названа «vision target», а не измерением.
4. **«nano/pico/micro» в названии ничего не говорит о размере кода [моё].** В феврале 2026 nanobot заявлял «~4 000 строк ядра (3 510 реальных)». Сейчас пакет `nanobot/` — 128 221 строка Python. NanoClaw («a handful of files») — 45 014 строк TS без тестов. PicoClaw — 134 248 строк Go без тестов, NullClaw — 289 734 строки Zig. Сопровождающие PicoClaw сами фиксируют рост памяти с 10 до 10–20 МБ (issue #346).
5. **MCP стал стандартом де-факто.** MCP-клиент есть во всех проверенных Linux-агентах этого семейства. На уровне устройства **xiaozhi-esp32 (★30.4k) и ESP-Claw (Espressif) делают сам микроконтроллер MCP-сервером**: `McpServer::AddTool`, `lua.run_script`. Home Assistant с версии 2025.2 одновременно MCP-сервер (streamable HTTP, Assist API) и MCP-клиент (SSE, только tools). У OpenClaw есть «node-hosted MCP»: MCP-серверы запускаются на периферийном устройстве и публикуются шлюзу. Это готовая референсная архитектура для «MCP-сервера информации об оборудовании».
6. **Контекст оборудования first-class есть лишь у немногих.**
   - PicoClaw: инструменты `i2c`/`spi`/`serial`, монитор USB-hotplug, skill с картами регистров датчиков.
   - ZeroClaw: трейт `Peripheral`, реестр плат по USB VID/PID (Nucleo, Arduino, ESP32), даташиты, прошивка Pico с обязательным подтверждением.
   - NullClaw: i2c, spi, peripherals.
   - ESP-Claw: YAML-описание платы `board_peripherals.yaml`.
   - zclaw, MimiClaw: GPIO, DHT, I2C на ESP32.
   
   Ни в одном проверенном проекте я не нашёл MCP-ресурса «состояние железа + ресурсы агента» (CPU/RAM/температура/токены за период), который отдавался бы LLM как контекст. Это возможная ниша ВКР **[не проверено исчерпывающе]**.
7. **Экономию токенов решают архитектурно, и это видно в коде.**
   - NanoClaw: «script gates» — дешёвый скрипт решает, будить ли LLM.
   - WireClaw, ESP-Claw, ZeroClaw: LLM один раз компилирует правило (`rule_create`, Lua-скрипт, SOP), дальше оно исполняется без LLM.
   - PicoClaw: rule-based маршрутизация на дешёвую модель.
   - zclaw: лимиты по умолчанию 100 вызовов/ч и 1000/сутки.
   - Обратный пример — OpenClaw: в системный промпт на каждом прогоне подмешиваются bootstrap-файлы до 60 000 символов (`bootstrapTotalMaxChars`). А пустой `HEARTBEAT.md` стоил «$20+/неделю» (#61690).
8. **Голосовой edge.**
   - Доминирует xiaozhi-esp32: ESP32-S3/C3/P4, офлайн wake word, 171 вариант прошивки, MCP на устройстве.
   - Открытый стандарт — Home Assistant Voice PE (ESP32-S3) + протокол Wyoming (Open Home Foundation).
   - Rhasspy архивирован (2025-04). Willow почти не развивается: последние коммиты — только CI. Устройство 01 от Open Interpreter заброшено с 2024-11.
9. **Метрики.**
   - OpenClaw: встроенный учёт токенов и стоимости (`/usage cost`, `openclaw status --usage`).
   - NullClaw: `cost.zig` с ценами за 1M токенов.
   - ZeroClaw: Prometheus по умолчанию, OTel опционально.
   - Самая продуманная телеметрия энергии — в исследовательском OpenJarvis (Stanford Hazy Research, ★10.6k): Apple, NVIDIA (NVML), AMD, RAPL, метрика «intelligence per joule». **Монитор энергии для Jetson (tegrastats) там помечен «Design Needed»**, то есть не сделан. Это ещё одна конкретная ниша.
10. **Что делать сейчас на Mac.** Все перечисленные Linux-агенты имеют сборки под darwin-arm64. Базовые линии для замеров: PicoClaw (Go) и nanobot (Python), OpenClaw — тяжёлая референсная точка. Совместимость с Jetson-userland можно проверить в `docker run --platform linux/arm64 ubuntu:18.04` с лимитом памяти 2–4 ГБ. Это проверит glibc, но не ядро 4.9 **[не проверено, рекомендация]**.

---

## 1. Методика и ограничения

- Звёзды, форки, даты, язык и лицензия взяты из `api.github.com/repos/...` на 2026-10-06. Активность — число коммитов в ветку по умолчанию за 30 дней (`/commits?since=2026-09-06`) и дата последнего коммита в ней. `pushed_at` бывает обманчив, потому что пуши идут в другие ветки: у PicoClaw `pushed_at` = 2026-09-24, а последний коммит в `main` — 2026-08-19.
- Зависимости и версии рантайма брались из npm registry (`engines.node`), PyPI JSON (`requires_python`, `requires_dist`) и Node.js `BUILDING.md` (минимальные glibc и ядро).
- Размеры кода считал `wc -l` по shallow-клонам: тесты исключены, комментарии и пустые строки включены. Это грубая оценка, а не SLOC.
- Размеры бинарников — поле `size` ассетов последнего релиза в GitHub API. Это архивы, а не распакованные бинарники.
- **RAM я сам не измерял.** Запуск и установка были запрещены условиями задачи. Все цифры RAM — либо заявления авторов, либо внешние измерения, с указанием источника.
- Огромные счётчики коммитов у OpenClaw (17 299 за 30 дней) и Hermes Agent (17 172) почти наверняка включают автоматизированные или агентные коммиты. Как меру «зрелости» их использовать нельзя.

---

## 2. Факты о целевом железе: Jetson Nano (кафедра)

| Факт | Значение | Источник |
|---|---|---|
| Модели Nano | Jetson Nano (модуль/devkit) **4 ГБ** 64-bit LPDDR4, 25.6 ГБ/с; **Jetson Nano 2GB Developer Kit**. Варианта на 1 ГБ нет | nvidia.com, страница Jetson Nano product-development; developer.nvidia.com, get-started-jetson-nano-2gb-devkit |
| CPU/GPU/питание | 4× Cortex-A57 до 1.43 ГГц; 128-ядерный Maxwell, 472 GFLOPS; 5–10 Вт. 2GB devkit — USB-C 5 В⎓3 А без PD; NVIDIA рекомендует swap | те же |
| Последний софт | **JetPack 4.6.6 / Jetson Linux R32.7.6 — финальный релиз R32/JetPack 4** | developer.nvidia.com/embedded/linux-tegra-r3276 |
| ОС | rootfs на Ubuntu 18.04, **ядро 4.9** | там же |
| glibc / Python | Ubuntu 18.04 arm64: libc6 **2.27**-3ubuntu1.x, python3 **3.6** | launchpad.net/ubuntu/bionic/arm64/{libc6,python3} |
| Docker из архива Ubuntu | docker.io до 20.10.21 | launchpad.net/ubuntu/bionic/arm64/docker.io |
| Node.js 22/24 | linux-arm64: **glibc ≥ 2.28, ядро ≥ 4.18** (Tier 1) | nodejs/node `BUILDING.md` (v22.x, v24.x) |
| Python через uv | python-build-standalone: glibc ≥ 2.17 для большинства целей (riscv64 — ≥ 2.28) | astral-sh/python-build-standalone `docs/running.rst` |
| Обход | Неофициальный образ Ubuntu 20.04 для Nano с JetPack 4.6.1 (Qengineering, ★983): glibc 2.31 ≥ 2.28, но ядро остаётся 4.9 | github.com/Qengineering/Jetson-Nano-Ubuntu-20-image |

Что из этого следует для выбора агента:
- Node-агентам нужен обход: образ на 20.04 или контейнер с новым userland. Ядро 4.9 ниже порога 4.18, который заявлен для Tier 1 support. Работает ли Node 24 на ядре 4.9 на практике — **[не проверено]**.
- Статические бинарники Go/Rust-musl/Zig от glibc не зависят.
- Python-агенты работают через `uv python install 3.12`. Но у каждой нативной зависимости должно быть колесо `manylinux_2_17`/`manylinux2014` для aarch64. У nanobot само колесо `manylinux_2_17_aarch64`; колёса зависимостей **[не проверено]**.

---

## 3. Таксономия

| Группа | Что объединяет | Проекты |
|---|---|---|
| A. Тяжёлые шлюзы | Node/Python, сотни зависимостей, ориентир — Mac mini/VPS, на Pi минимум 1–2 ГБ | OpenClaw, Hermes Agent, NemoClaw (+OpenShell), ClawBox (appliance на Jetson Orin) |
| B. Лёгкие клоны для Linux-плат | один процесс или бинарник, ориентир — Pi/RISC-V/роутер | nanobot, NanoClaw, PicoClaw, ZeroClaw, NullClaw, IronClaw, Moltis; второстепенные — MicroClaw, TinyClaw, OpenFang |
| C. Агенты на микроконтроллерах | без Linux, ESP-IDF/FreeRTOS, LLM только в облаке | MimiClaw, zclaw, ESP-Claw, WireClaw (+ femtoclaw, EmbedClaw — мелкие) |
| D. Голосовые edge-ассистенты | wake word и аудио на устройстве, ASR/LLM/TTS на сервере | xiaozhi-esp32 (+server, py-xiaozhi), Home Assistant Assist + Voice PE + Wyoming, Willow, Rhasspy (архив), Open Interpreter 01 (заброшен) |
| E. Общие агенты и MCP-хосты, исследовательские | не edge-специфичны, но задают стандарты (MCP/ACP) и методику | Goose (AAIF), Open Interpreter (теперь форк Codex), OpenJarvis (Stanford) |

---

## 4. Сводная таблица

Звёзды — на 2026-10-06. «Посл.» — последний коммит в ветке по умолчанию. RAM: «з» — заявлено, «н» — независимо. «Nano» — Jetson Nano с JetPack 4.6 (glibc 2.27, ядро 4.9).

| # | Проект | Язык / рантайм | ★ | Создан | Посл. | RAM | MCP | Железо в инструментах | Nano |
|---|---|---|---|---|---|---|---|---|---|
| 1 | OpenClaw | TS, Node ≥ 24.16 | 391 493 | 2025-11-24 | 10-06 | з: мин. 1 ГБ, рек. 2 ГБ+; н: медиана пика 2926.6 MiB | да (+ node-hosted) | через «nodes»: камера, экран, локация | нет нативно |
| 2 | Hermes Agent | Python 3.11–3.14 + Node TUI | 251 587 | 2025-07-22 | 10-06 | не указано | да | нет | с трудом [не проверено] |
| 3 | NemoClaw | TS + OpenShell (Rust) | 22 666 | 2026-03-15 | 10-06 | з: мин. 8 ГБ, 4 vCPU | через агентов | нет | нет |
| 4 | ClawBox | OpenClaw OS (TS) | 30 | 2026-02-07 | 10-05 | Orin Nano 8 ГБ | да (ОС управляется MCP) | локальная LLM на Orin | нет (только Orin, JP 6.2) |
| 5 | nanobot | Python ≥ 3.11 | 48 821 | 2026-02-01 | 10-06 | н: медиана пика 136.1 MiB (v0.1.5) | да | нет | вероятно да через uv |
| 6 | NanoClaw | TS, Node 22+, Docker, Bun | 30 882 | 2026-01-31 | 10-05 | не указано | да | нет | нет нативно |
| 7 | PicoClaw | Go 1.25, static | 30 012 | 2026-02-04 | 08-19 | з: < 10 МБ (сейчас 10–20 МБ) | да | i2c, spi, serial, USB-hotplug | да (высокая уверенность) |
| 8 | ZeroClaw | Rust, musl-сборки | 32 934 | 2026-02-13 | 10-06 | з: цель < 5 МБ; н: 100 МБ → 2.5 ГБ с MCP | да | GPIO/I2C/SPI/USB, STM32, Arduino, ESP32 | вероятно да |
| 9 | NullClaw | Zig | 8 102 | 2026-02-16 | 10-05 | з: ~1 МБ, бинарник 678 КБ (релиз 4.2 MiB) | да | i2c, spi, Serial, RPi GPIO, STM32 | вероятно да |
| 10 | IronClaw | Rust, musl-сборки | 12 639 | 2026-02-03 | 09-10 | не указано; PostgreSQL | да | нет | с трудом |
| 11 | Moltis | Rust 1.91+ | 2 884 | 2026-01-29 | 09-22 | не указано; feature `lightweight` | да (stdio + HTTP/SSE) | нет | не проверено (gnu-бинарник) |
| 12 | MimiClaw | C, ESP-IDF ≥ 5.5, ESP32-S3 | 5 783 | 2026-02-04 | 10-04 | з: бюджет памяти ~0.3 МБ, свободно ~7.7 МБ PSRAM | нет | GPIO | н/п (MCU) |
| 13 | zclaw | C, ESP-IDF | 2 233 | 2026-02-16 | 05-17 | з: прошивка ≤ 888 KiB | нет | GPIO, DHT, I2C | н/п |
| 14 | ESP-Claw | C + Lua, ESP-IDF | 2 200 | 2026-04-17 | 09-01 | не указано | да (сервер и клиент) | YAML платы, Lua-скрипты GPIO | н/п |
| 15 | WireClaw | C++, ESP32 | 193 | 2026-02-09 | 02-23 | не указано | нет | GPIO, сенсоры, реле, правила | н/п |
| 16 | xiaozhi-esp32 | C++, ESP-IDF ≥ 6.0.1 | 30 445 | 2024-08-31 | 10-02 | — | да (на устройстве) | динамик, LED, серво, GPIO, камера | н/п (сервер отдельно) |
| 17 | xiaozhi-esp32-server | Python/Java/Vue | 10 737 | 2025-02-02 | 09-29 | з: 2C/2G (всё через API) … 4C/8G (FunASR) | да | через MCP устройств | вероятно да через Docker [не проверено] |
| 18 | Home Assistant (+Assist, MCP) | Python ≥ 3.14.2 | 91 275 | 2013-09-17 | 10-06 | з: HA OS 2 ГБ, 2 vCPU | сервер + клиент | весь умный дом | через Container (Docker ≥ 23) [не проверено] |
| 19 | Wyoming / Voice PE | Python / ESPHome | 401 / 765 | 2023 / 2024 | 09-29 / 09-17 | — | нет (свой протокол) | ESP32-S3 + micro_wake_word | — |
| 20 | Willow | C, ESP32-S3-BOX | 3 122 | 2023-03-31 | 08-04 | — | нет | до 400 голосовых команд на устройстве | н/п |
| 21 | Rhasspy | — | 2 746 | 2020-01-08 | архив | — | нет | — | — |
| 22 | Open Interpreter (+01) | Rust (форк Codex) / Python | 68 523 / 5 159 | 2023-07-14 | 10-02 / 2024-11 | — | да | 01: ESP32 голос | не проверено |
| 23 | Goose | Rust | 54 998 | 2024-08-23 | 10-06 | — | да (70+ расширений) | нет | вероятно да (musl CLI) |
| 24 | OpenJarvis | Python + Rust | 10 623 | 2026-02-15 | 10-06 | — | не проверял | телеметрия энергии | не проверено |
| 25 | MicroClaw / TinyClaw / OpenFang | Rust / TS-Bun / Rust | 746 / 292 / 18 211 | 02-2026 | 10-06 / 02-25 / 05-12 | OpenFang з: бинарник ~32 МБ | MicroClaw да | нет | см. карточку |

---

## 5. Карточки

### Группа A. Тяжёлые шлюзы

#### 1. OpenClaw (бывш. Clawdbot → Moltbot)
- **URL:** https://github.com/openclaw/openclaw. MIT, TypeScript. Сайт docs.openclaw.ai. Стюард — OpenClaw Foundation, некоммерческая 501(c)(3) [заявлено, README].
- **Метаданные:** ★391 493, форков 82 299, создан 2025-11-24, последний коммит 2026-10-06, 17 299 коммитов за 30 дней. npm-пакет `openclaw` 2026.9.8: 266 версий, первая опубликована 2026-01-29, **15 591 834 загрузки** за 2026-09-05…2026-10-04 (api.npmjs.org).
- **История имён** [заявлено, `docs/start/lore.md`]: Clawd в Clawdbot. В январе 2026 Anthropic попросила сменить имя из-за товарного знака. Дальше Moltbot, затем OpenClaw. Совпадает с описанием `cloudflare/moltworker`: «OpenClaw (formerly Moltbot, formerly Clawdbot)».
- **Что делает.** Персональный ассистент. Один процесс Gateway; каналы — WhatsApp, Telegram, Slack, Discord, Google Chat, Signal, iMessage, Teams и ещё 20+. Есть компаньон-приложения macOS/iOS/Android/Windows/Linux. Модели и харнессы (Claude, Codex, локальные) подключаются как плагины.
- **Архитектура «Gateway + nodes»** [заявлено, `docs/nodes/index.md`, `node-host.md`, `mcp-and-skills.md`]:
  - node — периферийное устройство с `role: "node"`. Оно публикует команды `camera.*`, `device.*`, `notifications.*`, `system.*` через `node.invoke`.
  - **Node-hosted MCP:** MCP-серверы конфигурируются на узле, узел публикует их tools шлюзу (`mcp.tools.call.v1`). OAuth-MCP в v1 не поддерживается.
  - Каналы приходят только на шлюз, узлы — периферия.
  
  Это почти буквально постановка ВКР «MCP-сервер информации об оборудовании», только узел OpenClaw — сам Node.js-процесс.
- **Footprint.**
  - Требования [заявлено, `docs/install/raspberry-pi.md`]: «Minimum: 1 GB RAM, 1 core, 500 MB free disk, 64-bit OS. Recommended: 2 GB+». По платам: Pi 5 4/8 ГБ — Best; Pi 4 4 ГБ — Good; Pi 4 2 ГБ — OK, нужен swap; Pi 4 1 ГБ — Tight; Pi 3B+ — Slow; Pi Zero 2 W (512 МБ) — No.
  - Node `>=24.16.0 <25 || >=26.1.0`. 66 прямых зависимостей, в том числе `@modelcontextprotocol/sdk`, `playwright-core`, `grammy`. Распакованный пакет — **388 955 824 байт (≈371 MiB), 13 320 файлов** [моё, npm registry]. Код — ≈400 MiB TypeScript по GitHub languages API.
  - **Независимое измерение** [незав., arXiv 2608.27886]: OpenClaw 2026.2.26-beta.1 (Node 22.22.2) на gpt-4o-mini. Медиана пикового потребления памяти **2926.6 MiB**, медиана времени 34.063 с. Оговорки: Docker Desktop/WSL2, конфигурация хоста и способ замера памяти не описаны.
- **Токены и стоимость** [заявлено, `docs/reference/token-use.md`, `docs/concepts/usage-tracking.md`]:
  - Системный промпт собирается на каждый прогон: список инструментов, skills, файлы `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`, `MEMORY.md`. Лимиты: `bootstrapMaxChars` = 20 000 на файл, `bootstrapTotalMaxChars` = 60 000 суммарно.
  - Учёт: `/status` (токены и оценка стоимости), `/usage tokens|full|cost`, `openclaw status --usage`, `openclaw gateway usage-cost`.
  - Баг #61690 (закрыт): пустой `HEARTBEAT.md` всё равно стоил $20+/неделю из-за переигрывания всей истории. Подробно — в E-заметке.
- **MCP:** да, клиент MCP плюс node-hosted MCP.
- **Где запускают:** Mac mini как «канонический» хост. Документация описывает Raspberry Pi и VPS, Cloudflare Workers (`cloudflare/moltworker`, ★9 952), Jetson Orin Nano Super (`eliranwong/NvidiaJetsonOpenClaw`, ★13). Коммерческий апплаенс ClawBox — см. ниже. Пакет `packages/agents/openclaw` есть в `dusty-nv/jetson-containers`; совместимость с L4T R32 не проверял.
- **Jetson Nano:** **нет нативно.** Node 24 требует glibc ≥ 2.28 и ядро ≥ 4.18, на Nano 2.27 и 4.9. По RAM 4 ГБ формально достаточно, 2 ГБ — «OK со swap» по таблице для Pi. Но независимый замер (~2.9 ГБ пика) показывает, что на 2 ГБ будет тяжело **[не проверено на Nano]**.

#### 2. Hermes Agent (Nous Research)
- **URL:** https://github.com/NousResearch/hermes-agent. MIT, Python (+TS TUI).
- **Метаданные:** ★251 587, создан 2025-07-22, последний коммит 2026-10-06. PyPI `hermes-agent` 0.19.0 от 2026-07-20, автор «Nous Research», `requires_python` `<3.14,>=3.11`, 30 прямых зависимостей без extras. **162 501 загрузка** за последний месяц (pypistats).
- **Что делает.** Самообучающийся агент: создаёт и улучшает skills по опыту, ищет по прошлым диалогам, строит модель пользователя. Один процесс-шлюз обслуживает Telegram, Discord, Slack, WhatsApp, Signal, Email и **Home Assistant**. Есть cron, субагенты, 7 бэкендов терминала (local, Docker, SSH, Singularity, Modal, Daytona, Vercel Sandbox) [заявлено, README].
- **Footprint** [заявлено, docs/installation]. Явных требований к RAM/CPU нет. Первичные установки работают на Python 3.14. Установщик ставит закреплённые Python, Node.js, npm, ripgrep, FFmpeg, а также `agent-browser` с Chromium. Для glibc-Linux нужен `libatomic.so.1`. Есть APT-репозиторий для Termux/aarch64. Слоган «Run it on a $5 VPS».
- **MCP:** да («MCP Integration»).
- **Где запускают:** VPS, Termux на Android. NVIDIA NemoClaw поддерживает Hermes как второй агент. В `jetson-containers` 2026-04-07 добавлен скрипт установки hermes-agent (по истории коммитов).
- **Jetson Nano:** **с трудом [не проверено].** Python 3.11–3.14 через uv возможен, но установщик тянет Node.js (glibc 2.28) и Chromium.

#### 3. NVIDIA NemoClaw (+ NVIDIA OpenShell)
- **URL:** https://github.com/NVIDIA/NemoClaw (Apache-2.0, ★22 666, создан 2026-03-15). Песочница: https://github.com/NVIDIA/OpenShell (Rust, ★15 065, создан 2026-02-24).
- **Что делает.** Референсный стек для запуска агентов в песочницах OpenShell: управляемый инференс, сетевая политика egress, состояние, жизненный цикл. Поддерживает OpenClaw (по умолчанию), Hermes и LangChain Deep Agents Code. Статус — **alpha** [заявлено, README].
- **Footprint** [заявлено, docs.nvidia.com/nemoclaw/.../prerequisites]: **минимум 4 vCPU, 8 ГБ RAM, 20 ГБ диска**; рекомендовано 16 ГБ и 40 ГБ. Node ≥ 22.19, Docker/Colima/rootless Podman. Протестировано: DGX Spark, Ubuntu 24.04, macOS Apple Silicon (с ограничениями), WSL2. **Jetson в списке нет.** Есть сторонние порты: `jetsonhacks/NemoClaw-Orin` (★26) и `pastoriomarco/NemoClaw-Thor`.
- **MCP:** через агентов.
- **Jetson Nano:** **нет** — 8 ГБ минимум.
- **Польза для ВКР:** образец изоляции агента и egress-политики. Хороший аргумент в разделе о безопасности агента, у которого есть доступ к железу.

#### 4. ClawBox (ID Robots Ltd) — апплаенс OpenClaw на Jetson
- **URL:** https://github.com/ID-Robots/clawbox. ★30, лицензия NOASSERTION, последний коммит 2026-10-05.
- **Что это.** Коммерческое устройство: **NVIDIA Jetson Orin Nano 8GB (Super), NVMe на 512 ГБ, JetPack 6.2 (Ubuntu 22.04 / L4T R36.x)**. JetPack 7 пока не поддержан. Работает «OpenClaw OS». Локально предустановлена Gemma 4 на llama.cpp, локальные модели «до ~8B». Облачные модели — опционально. Агент **управляет устройством через MCP-инструменты**. Установщик ставит Node.js 24 + Bun [заявлено, README].
- **Польза:** реальный пример «OpenClaw на Jetson», но только на Orin. Подтверждает, что граница проходит между JetPack 4 (Nano) и JetPack 6 (Orin).
- **Jetson Nano:** нет.

### Группа B. Лёгкие клоны для Linux-плат

#### 5. nanobot (HKUDS, Гонконгский университет)
- **URL:** https://github.com/HKUDS/nanobot. MIT, Python.
- **Метаданные:** ★48 821, создан 2026-02-01, последний коммит 2026-10-06, 423 коммита за 30 дней. PyPI `nanobot-ai` 0.3.5: `requires_python >=3.11`, 36 прямых зависимостей без extras (61 вместе с extras), включая `mcp>=1.26`, `openai>=2.8`, `anthropic`, `tiktoken`, `pydantic`. Колесо `manylinux_2_17_aarch64` — 48.2 МБ. **47 015 загрузок** за последний месяц.
- **Что делает.** Self-hosted агент: WebUI/TUI/CLI, каналы Telegram, Discord, Slack, WeChat, Feishu, Email, Matrix, Signal, WhatsApp, QQ, DingTalk, Teams, Mattermost, Linear. Инструменты: файлы, shell, веб, MCP, cron, субагенты. Долговременная память «Dream», OpenAI-совместимый API и Python SDK [заявлено, README; список каналов — по каталогу `nanobot/channels/`, моё].
- **Footprint:**
  - Заявленное в феврале [README на коммите `ec4340d`, до 2026-02-10]: «~4,000 lines of code — 99% smaller than Clawdbot's 430k+ lines», «Real-time line count: 3,510 lines».
  - **Сейчас [моё]:** 128 221 строка Python в `nanobot/`. Из них channels — 73 124, agent — 24 372, webui — 22 610, providers — 16 758. Скрипт подсчёта `core_agent_lines.sh` из репозитория удалён.
  - Независимо [незав., arXiv 2608.27886]: NanoBot v0.1.5, Python 3.12.3, gpt-4o-mini — медиана пика памяти **136.1 MiB**, медиана времени 10.446 с. Это старая и маленькая версия.
  - В чужих сравнительных таблицах (PicoClaw, NullClaw) пишут «> 100 MB, > 30 s старт на 0.8 ГГц». Это заявление конкурентов.
- **MCP:** да.
- **Где запускают:** серверы, Linux-платы. PicoClaw вырос из nanobot («inspired by NanoBot»).
- **Jetson Nano:** **вероятно да** через `uv` с Python 3.11+ (glibc ≥ 2.17) и колесо `manylinux_2_17_aarch64`. По памяти порядок 100–300 МБ укладывается в 2–4 ГБ. Доступность aarch64-колёс всех нативных зависимостей **[не проверено]**.
- **Польза для ВКР:** готовая **Python-базовая линия**. Язык тот же, что, вероятно, у агента ВКР, а независимый замер уже есть, с ним можно сравниваться.

#### 6. NanoClaw (бывш. qwibitai/nanoclaw, gavrielc/nanoclaw)
- **URL:** https://github.com/nanocoai/nanoclaw. MIT, TypeScript. Старые имена `qwibitai/nanoclaw` и `gavrielc/nanoclaw` GitHub API перенаправляет на `nanocoai/nanoclaw` [моё].
- **Метаданные:** ★30 882, форков 12 777, создан 2026-01-31, последний коммит 2026-10-05, 200 коммитов за 30 дней.
- **Что делает.** «Лёгкая и безопасная альтернатива OpenClaw»:
  - один хост-процесс на Node маршрутизирует сообщения в **отдельный Linux-контейнер на каждую группу агентов**; внутри Bun + Claude Agent SDK;
  - обмен через два SQLite-файла (`inbound.db`/`outbound.db`) с одним писателем на каждый;
  - credential gateway (OneCLI Agent Vault) подставляет ключи, так что агент их не видит;
  - каналы ставятся skills-командами `/add-telegram`, `/add-whatsapp` и т. д.;
  - провайдер по умолчанию — Claude; альтернативы — `/add-codex`, `/add-opencode` (OpenRouter и др.), `/add-ollama-provider` или `ANTHROPIC_BASE_URL`.
  
  Есть «script gates» для задач по расписанию: скрипт решает, будить ли LLM [заявлено, README].
- **Footprint:** требования — macOS/Linux, Node 22+, pnpm 10+, Docker, Claude Code для `/customize` и `/add-*`. Явных цифр RAM нет. Тезис «один процесс и несколько файлов» расходится с **45 014 строками TS без тестов в `src/` + `container/`** [моё]. Про OpenClaw README пишет «nearly half a million lines of code, 53 config files, 70+ dependencies» [заявлено].
- **MCP:** да (MCP-инструменты в agent-runner, шаблоны агентов).
- **Где запускают:** macOS/Linux/WSL2, образы amd64/arm64 [docs/hardened-image.md]. Специальных инструкций под SBC не нашёл.
- **Jetson Nano:** **нет нативно.** Нужны Node 22+ (glibc 2.28), Docker с контейнером на каждого агента, Claude Code.
- **Польза:** образец изоляции (контейнер на агента), credential gateway и script gates для экономии токенов.

#### 7. PicoClaw (Sipeed)
- **URL:** https://github.com/sipeed/picoclaw. MIT, Go. Сайт picoclaw.io.
- **Метаданные:** ★30 012, создан 2026-02-04. Последний релиз v0.3.1 от 2026-07-03, последний коммит в `main` — 2026-08-19, **0 коммитов за 30 дней**: активность снизилась. `go.mod`: go 1.25.13, `modelcontextprotocol/go-sdk v1.6.1`.
- **Что делает.** Персональный агент на Go, «inspired by NanoBot», написан с нуля. «95 % ядра сгенерировано агентом» [заявлено]. Каналы (каталог `pkg/channels/`, моё): Telegram, Discord, Slack, Matrix, IRC, WhatsApp, Feishu, DingTalk, QQ, WeCom, Weixin, LINE, **VK**, **MQTT**, DeltaChat, OneBot, MaixCAM, webhooks. Есть WebUI-лаунчер, vision, маршрутизация моделей по правилам, MCP (с v0.2.1, 2026-03-09).
- **Footprint:**
  - [заявлено]: «$10 hardware · 10MB RAM», «Boots in <1s even on a 0.6GHz single-core». С оговоркой: «Recent builds may use 10-20MB». Минимум — 10 МБ свободной RAM (рекомендовано 32 МБ+), диск — 20 МБ под бинарник (50 МБ+ с workspace) [`docs/guides/hardware-compatibility.md`].
  - Issue #346 от сопровождающих: «baseline ~10MB», целевые платы с 64 МБ DDR, предложена «красная линия» 20 МБ.
  - [моё]: 134 248 строк Go без тестов. Архив релиза `picoclaw_Linux_arm64.tar.gz` — 21.9 МБ, вероятно вместе с лаунчером и WebUI **[не проверено]**. Сборка с `CGO_ENABLED=0` (`.goreleaser.yaml`, `Makefile`), то есть статическая.
- **Аппаратные инструменты** [моё, код]:
  - `pkg/tools/hardware/i2c*.go` — действия `detect`/`scan`/`read`/`write` через ioctl `/dev/i2c-*`, только Linux;
  - `spi*.go`, `serial*.go`;
  - `pkg/devices/sources/usb_linux.go` — монитор USB-hotplug.
  
  Подробнее с картами регистров и подтверждением записи — C-заметка, §3.5 [23].
- **MCP:** да (go-sdk; CLI `mcp add/list/test` с v0.2.8; управление в WebUI с v0.2.9).
- **Где запускают** [заявлено, список совместимости]:
  - RISC-V: LicheeRV-Nano (SG2002, 256 МБ), Milk-V Duo 64M, CanMV-K230, SpacemiT K1;
  - Raspberry Pi 1–5, Zero;
  - роутеры OpenWrt на MIPS (> 32 МБ свободной RAM);
  - NanoKVM, MaixCAM/MaixCAM2, Android через Termux, LoongArch.
  
  **Jetson не указан.**
- **Jetson Nano:** **да (высокая уверенность, но не проверено).** Статический бинарник linux-arm64, а I2C/SPI через `/dev/i2c-*`/`spidev` на Jetson доступны.
- **Польза:** главная **лёгкая базовая линия** и источник паттернов «hardware skill» и «i2c с подтверждением». Минус для эталона: активность в main упала с августа.

#### 8. ZeroClaw
- **URL:** https://github.com/zeroclaw-labs/zeroclaw. Apache-2.0, Rust.
- **Метаданные:** ★32 934, создан 2026-02-13, последний коммит 2026-10-06, 433 коммита за 30 дней. Релиз v0.8.5 от 2026-09-05; архивы `aarch64-unknown-linux-musl` (29.6 МБ), `aarch64-…-gnu`, `armv7-gnueabihf`, `aarch64-linux-android`.
- **Что делает.** Рантайм агента одним бинарником:
  - ~20 провайдеров, 30+ каналов, инструменты (shell, browser, HTTP, hardware, MCP);
  - SOP-движок — событийные «стандартные процедуры» по MQTT/webhook/cron/событию периферии, с точками одобрения;
  - ACP для IDE; автономия по умолчанию `supervised`;
  - песочницы Landlock/Bubblewrap/Seatbelt/Docker, криптографические «tool receipts» [заявлено, README].
- **Железо** [заявлено, `docs/book/src/hardware/*`; моё, `crates/zeroclaw-hardware/src/registry.rs`]:
  - feature `hardware`: GPIO/I2C/SPI/USB, трейт `Peripheral`;
  - реестр плат по VID/PID: Nucleo-F401RE/F411RE, Arduino Uno/Uno Q/Mega, CP2102, ESP32 (CH340);
  - `pico_flash` требует одобрения оператора при любом уровне автономии;
  - датащиты плат; инструменты железа можно исключить из не-CLI каналов.
  
  Для Pi рекомендуют Podman вместо Docker: нет резидентного `dockerd`, это экономит память.
- **Footprint:** [заявлено, `docs/book/src/foundations/fnd-001-intentional-architecture.md`]: «Vision target: <5 MB RAM at runtime»; измерено: бинарник foundation (`--no-default-features`) — **6.6 МБ (stripped)**, полный монолит — ~8.8 МБ.
  - [незав., issue #8642]: v0.6.8 в Kubernetes с лимитом 1–2 ГиБ, 4 MCP-сервера (34+ инструмента), 2 параллельных цикла. «RSS grows ~100 MB → 2.5 GB+ within minutes», `RssAnon` 2486 МБ, `VmPeak` 3.8 ГБ. Предполагаемая причина — глубокое клонирование JSON-схем MCP-инструментов на каждой итерации.
  - Soak-тест #11421 при ограниченной нагрузке рост не воспроизвёл, issue открыт.
- **Метрики:** `observability-prometheus` в default, OTLP опционально [заявлено].
- **MCP:** да.
- **Jetson Nano:** **вероятно да** (musl-бинарник). Но `peripheral-rpi` — это GPIO Raspberry Pi, у Jetson GPIO другой **[не проверено]**. Сборка из исходников на 4 ГБ требует swap: в гайде для Pi — 4 ГБ swap.
- **Польза:** лучший найденный пример **«контекста оборудования» в коде**: реестр плат, даташиты, политика безопасности для инструментов железа. А issue #8642 — прямой аргумент, почему ВКР должна **измерять RSS во времени при росте числа MCP-инструментов** (связано с G3: размер MCP-схем).

#### 9. NullClaw
- **URL:** https://github.com/nullclaw/nullclaw. MIT, Zig.
- **Метаданные:** ★8 102, создан 2026-02-16, последний коммит 2026-10-05, 41 коммит за 30 дней. Релиз v2026.5.29. Экосистема: nullhub (★1 828), nullboiler; сторонний ClawWatch (★65) — «NullClaw на Galaxy Watch».
- **Что делает.** Полный набор возможностей claw:
  - 50+ провайдеров, 19 каналов (включая MaixCam);
  - 35+ инструментов, включая `hardware_info`, `hardware_memory`, `i2c`, `spi`;
  - периферия: Serial, Arduino, RPi GPIO, STM32/Nucleo;
  - память на SQLite (FTS5 + векторы) и 9 других движков;
  - рантаймы Native/Docker/WASM, песочницы Landlock/Firejail/Bubblewrap/Docker [заявлено, README].
  
  `src/cost.zig` — учёт токенов с ценой за 1M [моё].
- **Footprint:** [заявлено]: «678 KB binary · ~1 MB peak RSS · <2 ms startup (Apple Silicon), <8 ms на 0.8 ГГц». Метод: «/usr/bin/time -l on ReleaseSmall builds» на macOS arm64, февраль 2026. [моё]: ассет `nullclaw-linux-aarch64.bin` — **4 396 464 байт (≈4.2 MiB)**, `riscv64` — 4.9 МБ, то есть в разы больше 678 КБ (другой режим оптимизации или рост кода — не выяснял); код — **289 734 строки Zig** в `src/`.
- **MCP:** да.
- **Jetson Nano:** **вероятно да.** README называет бинарник статическим, но тут же пишет «requires nothing but libc», так что формулировка противоречива **[не проверено]**.

#### 10. IronClaw (NEAR AI)
- **URL:** https://github.com/nearai/ironclaw. Apache-2.0/MIT, Rust.
- **Метаданные:** ★12 639, создан 2026-02-03. Последний коммит в `main` — 2026-09-10, при этом релиз ironclaw-v1.4.1 вышел 2026-09-29, видимо из другой ветки. Архив `aarch64-unknown-linux-musl` — 57.6 МБ.
- **Что делает.** «Agent OS» с упором на безопасность:
  - недоверенные инструменты — в WASM-песочнице с capability-правами;
  - секреты подставляются на границе хоста, есть детектор утечек и защита от prompt injection;
  - allowlist эндпоинтов;
  - каналы: REPL, HTTP-вебхуки, WASM-каналы (Telegram, Slack), веб-шлюз;
  - routines (cron/события), heartbeat, динамическое создание инструментов как WASM;
  - MCP [заявлено, README].
  
  Отличие от OpenClaw: «PostgreSQL vs SQLite».
- **Jetson Nano:** **с трудом.** Бинарник musl запустится, но PostgreSQL на 2–4 ГБ — лишняя нагрузка **[не проверено]**.
- **Польза:** раздел о безопасности: WASM-песочница против контейнеров.

#### 11. Moltis
- **URL:** https://github.com/moltis-org/moltis. MIT, Rust 1.91+.
- **Метаданные:** ★2 884, создан 2026-01-29, последний коммит 2026-09-22. Релиз 20260913.02: `aarch64-unknown-linux-gnu.tar.gz` (66 МБ), `arm64.deb` (43 МБ).
- **Что делает.** Персональный агент-сервер одним бинарником, без Node:
  - голос (8 TTS + 7 STT провайдеров), память с гибридным поиском;
  - каналы Telegram, Signal, Discord, Teams, Slack, WhatsApp, Matrix, Nostr;
  - MCP stdio + HTTP/SSE, песочницы Docker/Podman/Apple Container/WASM.
  
  «Runs on a Mac Mini, a Raspberry Pi». Для слабых устройств — `--no-default-features --features lightweight`. Объём: «~270K Rust LoC в 59 крейтах», цикл агента и интерфейс модели — «~7.5K строк» [заявлено, README].
- **Jetson Nano:** **не проверено.** Бинарник gnu: если он собран на свежем glibc, на 2.27 не запустится. Musl-сборки нет.

#### 12. Второстепенные: MicroClaw, TinyClaw, OpenFang
- **MicroClaw** (https://github.com/microclaw/microclaw): Rust, MIT, ★746, v0.8.0 от 2026-10-05. Server + десктоп-приложение Work + встраиваемый `microclaw-sdk`. MCP — да. «Inspired by nanoclaw».
- **TinyClaw** (https://github.com/warengonzaga/tinyclaw): TS на Bun, **GPL-3.0**, ★292. Статус в README: «Under Active Development… wait for the first official release». Последний коммит в `main` — 2026-02-25.
- **OpenFang** (https://github.com/RightNow-AI/openfang): Rust, Apache-2.0, ★18 211. «Agent Operating System», заявлен «single ~32MB binary». Последний коммит — 2026-05-12, то есть почти полгода тишины. Пакет `packages/agents/openfang` есть в `jetson-containers`.

### Группа C. Агенты на микроконтроллерах (LLM в облаке, агентный цикл на MCU)

#### 13. MimiClaw (memovai)
- **URL:** https://github.com/memovai/mimiclaw. MIT, C (ESP-IDF ≥ 5.5).
- **Метаданные:** ★5 783, создан 2026-02-04, последний коммит 2026-10-04 (1 за 30 дней). Код — 9 466 строк C/H в `main/` [моё].
- **Что делает.** «Personal Agent on a $5 chip. No Linux, no Node.js»:
  - ESP32-S3 с **16 МБ flash и 8 МБ PSRAM**; Telegram (long polling) и WebSocket; Anthropic или OpenAI;
  - инструменты: `web_search` (Tavily/Brave), время, cron, файлы, **GPIO** (`main/tools/tool_gpio.c` с `gpio_policy.c`);
  - `MEMORY.md`, `HEARTBEAT.md` (каждые 30 мин), `cron.json` на SPIFFS 12 МБ; «0.5 W» [заявлено].
- **Footprint** [заявлено, `docs/ARCHITECTURE.md`]:
  - стеки задач FreeRTOS ~40 КБ SRAM, буферы WiFi ~30 КБ;
  - в PSRAM: TLS ×2 ~120 КБ, JSON ~32 КБ, кэш сессии ~32 КБ, системный промпт ~16 КБ, поток ответа ~32 КБ;
  - свободно ~7.7 МБ PSRAM.
- **MCP:** нет (в `main/` не нашёл) [моё].
- **Польза:** доказывает, что **агентный цикл с памятью и cron умещается в ~0.3 МБ RAM**, если LLM вынесена в облако. Это нижняя граница «тонкого агента». ESP-Claw от Espressif прямо ссылается на MimiClaw.

#### 14. zclaw
- **URL:** https://github.com/tnm/zclaw. MIT, C.
- **Метаданные:** ★2 233, создан 2026-02-16, последний коммит 2026-05-17. Код — 10 531 строка C/H [моё].
- **Что делает.** Ассистент на ESP32 (протестированы ESP32, C3, S3, C6; рекомендуют Seeed XIAO ESP32-C3):
  - Telegram или веб-relay; Anthropic, OpenAI, OpenRouter, Ollama;
  - расписания, **GPIO, DHT, I2C** (`gpio_read_all`, `i2c_scan`, `i2c_read/write`, `dht_read`) с ограничителями;
  - диагностика `get_diagnostics` (память, лимиты, время);
  - лимиты по умолчанию **100 вызовов LLM/ч и 1000/сутки** [заявлено].
- **Footprint** [заявлено]: прошивка целиком **≤ 888 KiB** (ESP-IDF, FreeRTOS, WiFi, TLS, сертификаты), из них ~35 КБ — собственный код.
- **MCP:** нет.

#### 15. ESP-Claw (Espressif, официальный)
- **URL:** https://github.com/espressif/esp-claw. Apache-2.0, C + Lua.
- **Метаданные:** ★2 200, создан 2026-04-17, последний коммит в `master` — 2026-09-01. Сайт esp-claw.com.
- **Что делает.** «Chat Coding» агент для IoT:
  - поведение устройства задаётся в чате, LLM пишет **Lua-скрипты**, которые загружаются динамически;
  - событийный цикл с реакцией «за миллисекунды»;
  - структурированная память на устройстве;
  - **MCP одновременно как Server и Client**; IM: Telegram, QQ, Feishu, WeChat.
  
  Чипы: ESP32-S3, P4, C5, S31. LLM в стиле OpenAI или Anthropic. Для «самопрограммирования» рекомендуют сильные модели (gpt-5.4, qwen3.6-plus, claude-sonnet-4.6, deepseek-v4-pro). Вдохновлён OpenClaw, реализация цикла и IM опирается на MimiClaw [заявлено, README].
- **Важно для ВКР** [моё, дерево репозитория]:
  - приложение `application/mcp_server_point` — урезанная прошивка, где ESP32 работает **MCP-сервером** с инструментами `lua.run_script`, `lua.run_script_async`, `lua.list_jobs`, `lua.get_job`, `lua.stop_job`;
  - описание платы — **декларативные YAML** (`board_peripherals.yaml`: i2c_master с пинами sda/scl, i2s и т. д.; `board_devices.yaml`). Это готовый формат «контекста оборудования».
- **Польза:** официальный вендор чипов признал паттерн claw. Схема «MCU = MCP-сервер, Linux-плата/облако = агент» — ровно та топология, что нужна ВКР.

#### 16. WireClaw
- **URL:** https://github.com/M64GitHub/WireClaw. C++, лицензия не указана, ★193, последний коммит 2026-02-23 (заброшен?).
- **Что делает.** Агент на ESP32-C6/S3/C3 с 4 МБ flash; Telegram, serial, NATS. Главная идея — **офлайн-движок правил.** LLM вызывает `rule_create(sensor, condition, threshold, on_action, off_action…)`, после чего правило крутится в основном цикле «No cloud, no server, no LLM calls». Память сохраняется во flash [заявлено, README].
- **Польза:** чистый пример паттерна **«LLM как компилятор локальных правил»**. Снижает токены во времени и убирает зависимость от сети.

Ещё мелкие проекты, только для полноты: `samiul000/femtoclaw` (C, ★37, порт PicoClaw на ESP32/Pico W), `wireless-tag-com/EmbedClaw` (C, ★44), `practice-together/pico-claw-micropython` (★7). Отдельно: `device-context-protocol/dcp` (★60) — компактный протокол «LLM → устройство», заявлено 27.6 КБ flash и 0.6 КБ RAM; научная часть разобрана в C-заметке [20].

### Группа D. Голосовые edge-ассистенты

#### 17. xiaozhi-esp32 (+ xiaozhi-esp32-server, py-xiaozhi)
- **URL:** https://github.com/78/xiaozhi-esp32. MIT, C++.
- **Метаданные:** ★30 445, форков 7 134, создан 2024-08-31, последний коммит 2026-10-02, 50 коммитов за 30 дней.
- **Что делает.** «An MCP-based chatbot»:
  - офлайн wake word (ESP-SR), транспорт WebSocket или MQTT+UDP, Opus;
  - конвейер ASR + LLM + TTS или end-to-end realtime-модели; распознавание говорящего; дисплеи, камера, 4G;
  - **MCP на стороне устройства** (динамик, LED, серво, GPIO) и **MCP в облаке** (умный дом, ПК, поиск);
  - чипы ESP32, C3, C5, C6, S3, P4; 138 каталогов плат и 171 вариант прошивки; ESP-IDF ≥ 6.0.1;
  - по умолчанию подключается к серверу xiaozhi.me (бесплатная realtime-модель Qwen) [заявлено, README].
- **MCP-API устройства** [заявлено, `docs/mcp-usage.md`]:
  - бэкенд после подключения шлёт `initialize`, затем `tools/list` и `tools/call` (JSON-RPC 2.0);
  - инструменты регистрируются через `McpServer::AddTool(name, description, properties, callback)`;
  - **`AddUserOnlyTool`** — скрытые от модели привилегированные инструменты: перезагрузка, прошивка. Это хороший приём безопасности.
- **Сервер** (https://github.com/xinnan-tech/xiaozhi-esp32-server; ★10 737, MIT, последний коммит 2026-09-29) [заявлено, README]:
  - минимальная установка: «2 ядра / 2 ГБ, если всё через API; 2 ядра / 4 ГБ с локальным FunASR»;
  - полная: 2C/4G и 4C/8G соответственно;
  - MCP-точки доступа, отправка MCP-команд на устройство по MQTT.
- **py-xiaozhi** (https://github.com/huangjunsen0406/py-xiaozhi; ★3 491) — Python-клиент для ПК и Raspberry Pi: MCP-сервер инструментов (камера, музыка, громкость), GPIO. Заявлено «минимум 4 ГБ RAM (рек. 8 ГБ+)».
- **Jetson Nano:** устройство — ESP32. Сервер на Nano в режиме «всё через API» по заявленным 2C/2G формально проходит, через Docker 20.10 **[не проверено]**.
- **Польза:** самая массовая реальная система **«устройство = MCP-сервер, LLM в облаке»**. Её протокол можно цитировать как индустриальный аналог.

#### 18. Home Assistant: Assist + LLM + MCP (+ Voice PE, Wyoming)
- **URL:** https://github.com/home-assistant/core. Apache-2.0, ★91 275, релиз 2026.9.4 (2026-09-27), 1 954 коммита за 30 дней. `requires-python >=3.14.2` (pyproject).
- **MCP** [заявлено, home-assistant.io/integrations/mcp_server и /mcp; моё, `manifest.json`]:
  - **MCP Server** (с 2025.2): streamable HTTP; публикует tools и prompts из LLM API (обычно Assist). При наличии `GetLiveContext` отдаёт ресурс `homeassistant://assist/context-snapshot`. Sampling и notifications не поддерживаются. Зависимость `mcp==1.28.1`.
  - **MCP Client** (с 2025.2): SSE, stdio — через mcp-proxy. Поддерживаются только tools: prompts, resources и sampling — нет. Инструменты доступны conversation-агентам (OpenAI, Anthropic, Google, Ollama и др.). Доступ к устройствам ограничивается страницей «exposed entities».
- **Footprint** [заявлено, installation/linux]: HA OS — 2 ГБ RAM, 2 vCPU. HA Container требует **Docker Engine ≥ 23.0.0**, а Docker Desktop не подходит.
- **Голос:**
  - **Home Assistant Voice PE** (https://github.com/esphome/home-assistant-voice-pe; ★765): ESPHome на ESP32-S3 с PSRAM, `micro_wake_word` на устройстве [моё, yaml].
  - **Wyoming** (https://github.com/OHF-Voice/wyoming; ★401): «JSONL + PCM» поверх TCP, стандарт Open Home Foundation. **Без аутентификации и шифрования by design**, только для доверенной сети [заявлено].
  - `rhasspy/wyoming-satellite` архивирован. Преемник — `OHF-Voice/linux-voice-assistant` (★643, протокол ESPHome).
  - Локальные LLM для HA — `acon96/home-llm` (★1 446).
- **Jetson Nano:** только HA Container. Docker из архива Ubuntu 18.04 — 20.10.21, а нужен ≥ 23: придётся ставить Docker CE из репозитория Docker, поддержка bionic **[не проверено]**.
- **Польза:** HA — естественный «MCP-сервер умного дома». ВКР может подключать его как внешний источник контекста оборудования, не реализуя протоколы устройств.

#### 19. Willow
- **URL:** https://github.com/HeyWillow/willow. Apache-2.0, C, ★3 122.
- **Что делает.** Голосовой ассистент на **ESP32-S3-BOX (-3)** (~$50). Распознаёт на устройстве до 400 команд, wake word «Hi ESP»/«Alexa». Для общей транскрипции — Willow Inference Server. Интеграции: HA, openHAB, REST. LLM не упоминается [заявлено, heywillow.io].
- **Активность:** релиз 0.4.3 от 2026-07-21. Последние коммиты (2026-07-25…08-01) — только CI и зависимости, 0 за 30 дней. **Проект фактически на поддержке.**

#### 20. Rhasspy
- https://github.com/rhasspy/rhasspy — **архивирован**, последний пуш 2025-04-22, ★2 746. `rhasspy3` (★382) не обновлялся с 2023-12. Наследие ушло в Wyoming/OHF (Piper, openWakeWord и др.). **Для ВКР — только историческая ссылка.**

### Группа E. Общие агенты и MCP-хосты, исследовательские

#### 21. Open Interpreter (+ 01)
- **URL:** https://github.com/openinterpreter/openinterpreter (старое имя `open-interpreter` перенаправляется). Apache-2.0, ★68 523.
- **Что сейчас.** **Проект сменил природу.** Теперь это Rust-форк OpenAI Codex с «эмуляцией харнессов» (claude-code, kimi-code, qwen-code и др.), совместимостью с MCP, ACP и Codex exec. Релизы rust-v0.0.55 (2026-09-30) [заявлено, README]. Классический Python-пакет `open-interpreter` застрял на 0.4.3 от 2024-10-26, хотя всё ещё даёт 28 176 загрузок в месяц.
- **01** (https://github.com/openinterpreter/01; AGPL-3.0, ★5 159): «голосовой интерфейс для desktop, mobile и ESP32». Последний пуш 2024-11-01 — **заброшен**.
- **Польза:** только как пример эволюции «агент, выполняющий код на машине пользователя» и стандарта ACP. Для edge-железа не годится.

#### 22. Goose (Agentic AI Foundation)
- **URL:** https://github.com/aaif-goose/goose (бывш. `block/goose`, перенаправляется). Apache-2.0, Rust, ★54 998, v1.53.0 от 2026-10-02.
- **Что делает.** Агент общего назначения: десктоп, CLI, API. 15+ провайдеров, **70+ расширений через MCP**. Проект в AAIF при Linux Foundation [заявлено]. Есть CLI `goose-aarch64-unknown-linux-musl` (43.9 МБ bz2).
- **Jetson Nano:** **вероятно да** для musl-CLI **[не проверено]**. Но для edge это не специализированный агент.
- **Польза:** эталонный «MCP-хост». На нём удобно проверять собственный MCP-сервер оборудования ВКР наряду с Claude Desktop и HA.

#### 23. OpenJarvis (Stanford Hazy Research)
- **URL:** https://github.com/open-jarvis/OpenJarvis. Apache-2.0, Python + Rust-расширение.
- **Метаданные:** ★10 623, создан 2026-02-15, последний коммит 2026-10-06. PyPI `openjarvis` 1.0.4 от 2026-09-18, Python `>=3.10,<3.14`. Статья arXiv 2605.17172 «OpenJarvis: Personal AI, On Personal Devices».
- **Что делает.** Local-first стек персонального ИИ: примитивы Intelligence/Engine/Agents/Tools & Memory/Learning; оценки, где энергия, FLOPs, задержка и стоимость в $ — «first-class constraints»; поиск конфигурации с помощью облачной LLM. Заявлено: снижение маржинальной стоимости API ~800× и задержки 4×; on-device в среднем в пределах 3.2 п.п. от лучшего облачного baseline. В статье есть сравнение с OpenClaw и Hermes Agent [заявлено, arXiv abs].
- **Телеметрия** [моё, `src/openjarvis/telemetry/`]:
  - модули `energy_apple.py`, `energy_nvidia.py`, `energy_amd.py`, `energy_rapl.py`, `flops.py`, `itl.py`, `phase_metrics.py`, `steady_state.py`;
  - метрика IPJ (intelligence per joule), энергия prefill и decode отдельно;
  - в `GPU_SPECS` есть Jetson Orin NX/AGX Orin, но в `docs/development/roadmap.md` пункт «NVIDIA Jetson Orin — Design Needed… energy monitor (tegrastats)».
- **Польза:** готовая **методика метрик** (энергия/токены/стоимость/задержка) и признанный пробел — нет монитора Jetson. Связано с E-заметкой (E8, tegrastats).

---

## 6. Сквозные наблюдения

### 6.1. Откуда берутся цифры RAM
Сравнительная таблица «OpenClaw > 1 GB / NanoBot > 100 MB / PicoClaw < 10 MB / ZeroClaw < 5 MB / NullClaw ~1 MB» встречается в README PicoClaw и NullClaw; у OpenFang есть похожая столбчатая диаграмма. Судя по поисковой выдаче, её пересказывают и обзорные блоги: Medium, KDnuggets, DEV и др. Содержимое блогов я не проверял. Протокол замера нигде не приведён, кроме собственного `time -l` у NullClaw. Независимые данные:

| Источник | Что измерено | Результат | Оговорки |
|---|---|---|---|
| arXiv 2608.27886 (Salman, Halgamuge, Susnjak, 2026-08-28) | OpenClaw 2026.2.26-beta.1 и NanoBot v0.1.5, gpt-4o-mini, 23 промпта в детальном слое | медиана пика памяти 2926.6 MiB и 136.1 MiB; медиана времени 34.063 с и 10.446 с; среднегеометрическое отношение 19.44 (память) и 2.98 (время); полностью выполнено 31 % и 25 % задач | Docker Desktop/WSL2; CPU/RAM хоста и способ замера памяти не задокументированы; разная температура (0 и 0.1) |
| ZeroClaw issue #8642 (v0.6.8) | рабочая нагрузка в Kubernetes, 4 MCP-сервера / 34+ инструмента | RSS ~100 МБ → 2.5 ГБ+ за минуты, `VmPeak` 3.8 ГБ | отчёт пользователя; воспроизведение (#11421) при ограниченной нагрузке рост не показало |
| PicoClaw issue #346 (сопровождающие) | собственная оценка | «baseline ~10MB», тенденция роста, предложен потолок 20 МБ | самооценка без протокола |

**Вывод:** независимого, воспроизводимого и длительного (часы) профиля ресурсов claw-агента на одноплатнике **нет**. Это прямой и защищаемый вклад ВКР: методика из E-заметки (Mann–Kendall, Sen, батч-средние) плюс эти агенты как базовые линии.

### 6.2. Рост кода против «минималистичных» названий [моё]

| Проект | Заявлено | Сейчас (`wc -l`, без тестов) |
|---|---|---|
| nanobot | «~4,000 строк» (февраль 2026) | 128 221 (Python, пакет целиком; agent/ — 24 372) |
| NanoClaw | «handful of files» | 45 014 (TS, `src/` + `container/`) |
| PicoClaw | «ultra-lightweight» | 134 248 (Go) |
| NullClaw | «~230 source files» | 289 734 (Zig) |
| Moltis | «~270K Rust LoC» (сами) | — |
| MimiClaw / zclaw | — | 9 466 / 10 531 (C) |

За 8 месяцев «клоны» выросли до размеров, сопоставимых с тем, что они критиковали. Для ВКР это аргумент держать агент **узким и измеримым**: сравнивать по ресурсам во времени, а не по числу каналов.

### 6.3. Контекст пользователя — файлы
Де-факто конвенция OpenClaw: `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`, `MEMORY.md`, `HEARTBEAT.md`. Её переняли MimiClaw (`MEMORY.md`, `HEARTBEAT.md`), PicoClaw и NullClaw (`IdentityConfig: OpenClaw (markdown)`, Heartbeat `HEARTBEAT.md`). «Контекст пользователя» из постановки в индустрии = markdown-файлы, которые на каждом ходу подмешиваются в промпт с лимитом. У OpenClaw это 20 000 символов на файл и 60 000 суммарно. Цена такого подхода в токенах на сутки и есть то, что ВКР должна мерить (см. D- и E-заметки).

### 6.4. Контекст оборудования — где он есть

| Проект | Механизм |
|---|---|
| PicoClaw | инструменты `i2c`/`spi`/`serial`, USB-hotplug, hardware skill с картами регистров датчиков |
| ZeroClaw | `Peripheral` trait, реестр VID/PID, даташиты, `pico_flash` с обязательным одобрением, исключение инструментов железа из не-CLI каналов |
| NullClaw | `hardware_info`, `hardware_memory`, `i2c`, `spi`, peripherals |
| ESP-Claw | YAML-описание платы (`board_peripherals.yaml`), MCU — MCP-сервер Lua-инструментов |
| xiaozhi-esp32 | MCP на устройстве: `AddTool` и скрытые `AddUserOnlyTool` |
| OpenClaw | nodes с `camera.*`, `device.*`, `location.get`; node-hosted MCP |
| Home Assistant | MCP-сервер над всем умным домом, ресурс `context-snapshot` |

Чего я не нашёл в проверенных проектах: **единого MCP-ресурса «здоровье устройства и агента»**. Такой ресурс отдавал бы LLM CPU/RAM/температуру/питание платы вместе с расходом токенов и стоимостью агента за окно, чтобы агент сам учитывал ограничения: деградировал, реже будил LLM, переключался на дешёвую модель. Ближе всех OpenJarvis (телеметрия есть, но для оценки, а не как контекст агента) и NullClaw `hardware_memory` (только инструмент). **[не проверено исчерпывающе]**

### 6.5. Паттерны экономии токенов
1. Будить LLM только по скрипту-гейту (NanoClaw).
2. LLM компилирует правило или скрипт, дальше исполнение без LLM: WireClaw `rule_create`, ESP-Claw Lua, ZeroClaw SOP.
3. Маршрутизация по сложности (PicoClaw «Smart routing», TinyClaw «tiering»).
4. Жёсткие лимиты вызовов (zclaw: 100/ч, 1000/сутки).
5. Анти-паттерн — heartbeat с переигрыванием всей истории (OpenClaw #61690).

### 6.6. Бенчмарки для «claw»-агентов
- **PinchBench** (https://github.com/pinchbench/skill, ★1 359): 147 задач для OpenClaw-агента; метрики — success rate, скорость, стоимость, «value» (оценка автоматическими проверками и LLM-судьёй) [заявлено, pinchbench.com].
- ClawProBench (★824), WildClawBench (InternLM, ★529), ClawEnvKit (arXiv 2604.18543) — генерация сред для «claw-like agents».

Для ВКР нужен не «ещё один» бенчмарк качества, а **профиль ресурсов и стоимости во времени** на фиксированном наборе задач. Задачи можно взять из PinchBench или подобных **[не проверено, применимость к железу]**.

---

## 7. Что запускать где

### 7.1. Jetson Nano, JetPack 4.6 (Ubuntu 18.04, glibc 2.27, ядро 4.9, 2 или 4 ГБ)

| Вердикт | Проекты | Почему |
|---|---|---|
| Да (высокая уверенность, не проверено) | PicoClaw | Go, `CGO_ENABLED=0`, linux-arm64; I2C/SPI через `/dev` |
| Вероятно да | ZeroClaw (musl), NullClaw, Goose CLI (musl), nanobot (uv + Python 3.11+) | glibc-независимые бинарники или Python из python-build-standalone (glibc ≥ 2.17) |
| С трудом | Hermes Agent (Node, Chromium), IronClaw (PostgreSQL), HA Container (Docker ≥ 23), xiaozhi-server (Docker) | тяжёлые зависимости или новый Docker |
| Нет нативно | OpenClaw, NanoClaw, NemoClaw, ClawBox, TinyClaw, Moltis (gnu-бинарник, не проверено) | Node 22/24 → glibc ≥ 2.28 и ядро ≥ 4.18; NemoClaw — минимум 8 ГБ; ClawBox — только Orin/JP 6.2 |
| Обход | неофициальный образ Ubuntu 20.04 (Qengineering), контейнеры с новым userland | glibc 2.31, но ядро 4.9 остаётся [не проверено] |

### 7.2. Raspberry Pi 5 (вероятная целевая плата)
Raspberry Pi OS Bookworm/Trixie, 64-бит. Поддерживается всё семейство: OpenClaw официально, «Pi 5 — Best». У PicoClaw и ZeroClaw есть гайды под Pi, у ZeroClaw — нативный GPIO `peripheral-rpi`. Для ВКР Pi 5 снимает софт-ограничения Jetson Nano; выбор платы — в G-заметке.

### 7.3. MacBook M4 Pro (сейчас)
- У всех Linux-агентов есть сборки darwin-arm64: OpenClaw (npm), nanobot (колесо macosx_13_0_arm64), PicoClaw `Darwin_arm64`, ZeroClaw `aarch64-apple-darwin`, NullClaw `macos-aarch64`, IronClaw, Moltis, Goose, MicroClaw.
- Провайдер — любой OpenAI-совместимый эндпоинт: `api_base` у PicoClaw, `custom:` у NullClaw, «custom OpenAI-compatible endpoint» у ZeroClaw и zclaw, OpenAI-compatible у nanobot. Через это подключается RouterAI (цены и эндпоинты — в `../F_artifacts/`).
- **Эмуляция ограничений Jetson** [рекомендация, не проверено]:
  - `docker run --platform linux/arm64 --memory=2g --cpus=4 ubuntu:18.04`. На M4 это нативный arm64 без эмуляции QEMU. Проверяет glibc-совместимость бинарника и колёс, а лимит памяти имитирует 2/4 ГБ.
  - Ядро будет от Linux-VM Docker Desktop (6.x), поэтому проблемы ядра 4.9 так не воспроизвести.
  - Для Pi 5 — `debian:bookworm` arm64.

---

## 8. Выводы для ВКР

1. **Позиционирование.** Агент ВКР принадлежит классу «тонкий claw-агент на одноплатнике + внешняя LLM + MCP к железу». Класс массово существует: 10+ проектов с ★ > 5k, официальные решения Espressif (ESP-Claw) и NVIDIA (NemoClaw). Новизна не в самом классе, а в (а) **измеримости ресурсов и стоимости во времени** и (б) **формализованном контексте оборудования как MCP-ресурсе**. Независимых измерений почти нет (§6.1), а «hardware health как контекст» в проверенных проектах не реализован (§6.4).
2. **Базовые линии для экспериментов:**
   - PicoClaw (Go, лёгкий, i2c/spi) и nanobot (Python, тот же язык, есть независимый замер 136 MiB);
   - OpenClaw — как тяжёлая референсная точка (~2.9 GiB пика по arXiv);
   - ZeroClaw — если нужен Rust-эталон с богатым железом, плюс задокументированный рост RSS с MCP.
3. **Выбор рантайма агента ВКР с учётом Jetson Nano:** Python 3.11+ через `uv` (glibc ≥ 2.17) или статический бинарник. Node 22/24 исключить, иначе Nano отпадает без перепрошивки.
4. **Архитектурные приёмы, которые стоит взять со ссылкой на источник:**
   - узел-периферия с MCP-серверами (OpenClaw nodes, ESP-Claw `mcp_server_point`, xiaozhi `AddTool`/`AddUserOnlyTool`);
   - декларативное описание платы (ESP-Claw YAML, ZeroClaw VID/PID-реестр);
   - подтверждение опасных аппаратных действий (ZeroClaw `pico_flash`, PicoClaw `confirm`);
   - script gates и «LLM компилирует правило» (NanoClaw, WireClaw) — как механизм экономии токенов, который ВКР может измерить количественно.
5. **Эксперимент, который прямо следует из найденного:** зависимость RSS и токенов/час от числа подключённых MCP-инструментов и размера их схем. Мотивация — ZeroClaw #8642 и G3 (размер MCP-схем). Агенты: PicoClaw, nanobot, агент ВКР. Длительность — часы, статистика — по E-заметке.
6. **Метрики энергии:** монитор tegrastats/INA для Jetson в OpenJarvis не реализован («Design Needed»). Вклад ВКР может быть сделан в совместимом формате: Дж на запрос, Дж на час простоя.

---

## 9. Оговорки и непроверенное
- RAM ни одного агента я сам не измерял. Все вердикты «запустится на Jetson Nano» — вывод из версий glibc, ядра и рантайма, а не запуск.
- У NullClaw не выяснял статическую линковку релизного бинарника и причину разницы 678 КБ против 4.2 MiB. У Moltis не проверял требования gnu-бинарника к glibc. Не проверял aarch64-колёса всех зависимостей nanobot для manylinux2014.
- Не проверял поддержку Docker CE ≥ 23 на Ubuntu 18.04, а также работу Node 24 на ядре 4.9.
- Не верифицировал коммерческие облачные «claw» китайских вендоров, которые встречаются в блогах. В заметку они не вошли.
- Сравнение OpenClaw и Hermes в OpenJarvis и детали PinchBench (цены по моделям) смотрел только на уровне abstract и главной страницы.
- Число коммитов за 30 дней у OpenClaw и Hermes Agent (≈17k) — артефакт автоматизации, мерой зрелости это не является.
- Звёзды и даты — на 2026-10-06, проект может быть переименован. Подтверждённые переименования: `qwibitai/gavrielc/nanoclaw` → `nanocoai/nanoclaw`, `block/goose` → `aaif-goose/goose`, `open-interpreter` → `openinterpreter`.

---

## 10. Источники (проверены 2026-10-06)

Репозитории (GitHub API: метаданные, README, деревья файлов, релизы, issues):
- openclaw/openclaw (+ `docs/install/raspberry-pi.md`, `docs/nodes/*`, `docs/reference/token-use.md`, `docs/concepts/usage-tracking.md`, `docs/start/lore.md`, `docs/platforms/linux.md`); issue #61690
- npm: https://registry.npmjs.org/openclaw ; https://api.npmjs.org/downloads/point/last-month/openclaw
- NousResearch/hermes-agent; https://hermes-agent.nousresearch.com/docs/getting-started/installation ; PyPI hermes-agent; pypistats
- NVIDIA/NemoClaw; NVIDIA/OpenShell; https://docs.nvidia.com/nemoclaw/latest/get-started/prerequisites.html ; jetsonhacks/NemoClaw-Orin
- ID-Robots/clawbox
- HKUDS/nanobot (README на коммите ec4340d и текущий); PyPI nanobot-ai; pypistats
- nanocoai/nanoclaw
- sipeed/picoclaw (+ `docs/guides/hardware-compatibility.md`, `pkg/tools/hardware/*`, `pkg/devices/*`, `.goreleaser.yaml`); issue #346
- zeroclaw-labs/zeroclaw (+ `docs/book/src/hardware/*`, `docs/book/src/foundations/fnd-001-intentional-architecture.md`, `crates/zeroclaw-hardware/src/registry.rs`); issues #8642, #11421
- nullclaw/nullclaw (+ `src/cost.zig`); releases v2026.5.29
- nearai/ironclaw; moltis-org/moltis; microclaw/microclaw; warengonzaga/tinyclaw; RightNow-AI/openfang
- memovai/mimiclaw (+ `docs/ARCHITECTURE.md`, `main/tools/*`); tnm/zclaw; espressif/esp-claw (+ `application/mcp_server_point/README.md`, `boards/.../board_peripherals.yaml`); M64GitHub/WireClaw; samiul000/femtoclaw; wireless-tag-com/EmbedClaw; device-context-protocol/dcp
- 78/xiaozhi-esp32 (+ `docs/mcp-usage.md`); xinnan-tech/xiaozhi-esp32-server; huangjunsen0406/py-xiaozhi
- home-assistant/core (`pyproject.toml`, `components/mcp_server/manifest.json`); https://www.home-assistant.io/integrations/mcp_server/ ; https://www.home-assistant.io/integrations/mcp/ ; https://www.home-assistant.io/installation/linux
- esphome/home-assistant-voice-pe; OHF-Voice/wyoming; OHF-Voice/linux-voice-assistant; rhasspy/rhasspy; rhasspy/wyoming-satellite; acon96/home-llm
- HeyWillow/willow; https://heywillow.io/
- openinterpreter/openinterpreter; openinterpreter/01; PyPI open-interpreter
- aaif-goose/goose
- open-jarvis/OpenJarvis (+ `src/openjarvis/telemetry/*`, `docs/development/roadmap.md`); PyPI openjarvis
- dusty-nv/jetson-containers (`packages/agents/{openclaw,zeroclaw,openfang}`); dusty-nv/NanoLLM
- cloudflare/moltworker; eliranwong/NvidiaJetsonOpenClaw; Qengineering/Jetson-Nano-Ubuntu-20-image
- pinchbench/skill; https://pinchbench.com

Статьи:
- Salman A., Halgamuge M., Susnjak T. Resource Constraints and Performance in Agentic AI Systems. arXiv:2608.27886, 2026. https://arxiv.org/abs/2608.27886 (HTML-версия — для таблицы чисел)
- OpenJarvis: Personal AI, On Personal Devices. arXiv:2605.17172, 2026. https://arxiv.org/abs/2605.17172
- ClawEnvKit: Automatic Environment Generation for Claw-Like Agents. arXiv:2604.18543, 2026. https://arxiv.org/abs/2604.18543

Железо и платформа:
- https://www.nvidia.com/en-us/autonomous-machines/embedded-systems/jetson-nano/product-development/
- https://developer.nvidia.com/embedded/learn/get-started-jetson-nano-2gb-devkit
- https://developer.nvidia.com/embedded/linux-tegra-r3276
- https://launchpad.net/ubuntu/bionic/arm64/libc6 ; .../python3 ; .../docker.io
- https://raw.githubusercontent.com/nodejs/node/v24.x/BUILDING.md ; .../v22.x/BUILDING.md
- https://raw.githubusercontent.com/astral-sh/python-build-standalone/main/docs/running.rst
