# A_claw_analogs: «Claw-семейство» агентов и агенты для слабого железа

Область исследования: промышленные и открытые аналоги, которые назвал руководитель (NanoClaw, PicoClaw), их «родитель» OpenClaw, родственные проекты (nanobot, ZeroClaw, NullClaw, IronClaw, Moltis, ветка для микроконтроллеров) и агенты для умного дома и IoT (Home Assistant + LLM/MCP, xiaozhi-esp32, ESP-Claw). Сюда же входят научные статьи 2026 года, где эти системы измеряли или использовали как основу.

- Дата сбора: 2026-10-06. Звёзды, даты и лицензии сняты через GitHub REST API (`api.github.com/repos/...`) в этот же день.
- Проверка: каждый проект проверен по README или документации в репозитории (raw.githubusercontent.com / docs-сайт проекта), каждая статья — по arXiv API (аннотация). Ключевые числа статей перепроверены поиском по HTML-версии (`arxiv.org/html/...`). Snapshot'ы README и фрагменты кода лежат в `research/new_topic/A_artifacts/` (readmes/, code_snippets/, arxiv_abstracts_*.xml).
- Пометки: **[заявлено]** — цифра от авторов проекта без независимой проверки; **[измерено авт.]** — авторы приводят методику замера; **[измерено незав.]** — независимая статья; **[расчёт]** — моя арифметика на основе указанных источников; **[не проверено]** — встретилось только во вторичных источниках.

---

## 0. Коротко (TL;DR)

1. **NanoClaw** и **PicoClaw** существуют, это два разных проекта, и оба — «облегчённые OpenClaw». NanoClaw (Gavriel Cohen, TypeScript, MIT, ~30,9 тыс. звёзд) берёт безопасностью: каждый агент работает в своём Linux-контейнере, внутри — Claude Agent SDK. PicoClaw (компания Sipeed, Go, MIT, ~30,0 тыс. звёзд) берёт размером: один статический бинарник, заявлено <10 МБ RAM, работает на RISC-V/ARM/MIPS-платах от $10. Есть родные инструменты I2C/SPI/serial и клиент MCP.
2. **Родитель** — **OpenClaw**: Peter Steinberger, TypeScript, ~391 тыс. звёзд. Цепочка имён: Warelay (24.11.2025) → CLAWDIS → Clawdbot → Moltbot (27.01.2026) → OpenClaw (30.01.2026). Из OpenClaw за февраль 2026 выросло целое «claw-семейство»: nanobot (HKU), ZeroClaw (Rust), NullClaw (Zig), IronClaw (NEAR AI), Moltis, MicroClaw, TinyClaw. Отдельная ветка ушла на микроконтроллеры: MimiClaw, zclaw, WireClaw, ESP-Claw от Espressif, femtoclaw.
3. Архитектура у всех почти одинаковая, и она **точно совпадает с темой ВКР**. Лёгкий процесс-оркестратор на устройстве, рассуждение — во внешней LLM через API. Каналы — Telegram и прочие мессенджеры. Контекст пользователя хранится в файлах (`MEMORY.md`, `USER.md`, дневные заметки, JSONL-сессии) с суммаризацией. Есть heartbeat и cron для работы во времени, клиент MCP, у части проектов — инструменты для «железа» (GPIO/I2C/SPI/serial).
4. **Цифры по RAM — почти всегда маркетинг.** ZeroClaw и NullClaw мерили `--help` и `status` через `/usr/bin/time -l` на macOS, а не работающий цикл агента. Время старта «нормировано на 0,8 ГГц» без описания методики. Единственное независимое измерение — Salman et al., 2026, OpenClaw против NanoBot, gpt-4o-mini. Медиана пикового потребления памяти: **2926,6 МиБ у OpenClaw и 136,1 МиБ у NanoBot**, то есть в 19,4 раза больше. Время выполнения — в 2,98 раза дольше. Полнота выполнения задач статистически не различается. Замеров PicoClaw, ZeroClaw или NullClaw **на одноплатнике под нагрузкой агента не найдено**. Это ниша для ВКР.
5. Во «времени» при внешней LLM доминирует не RAM оркестратора, а **токены, трафик и задержки**. Один запрос OpenClaw — это ~28–30 тыс. входных токенов и ~500 выходных (Hua et al., 2026). Один heartbeat OpenClaw без изоляции — ~100 тыс. токенов, с `isolatedSession` — ~2–5 тыс. (документация OpenClaw). Ход LLM в ROSClaw — 1,3–2,3 с (P95 ≤ 3,8 с). Задача на 6–15 ходов занимает десятки секунд. Метрика руководителя «работа системы во времени» этим и определяется.
6. Baseline для замеров на одноплатнике:
   - основной — **PicoClaw**: Go-бинарник, MCP, I2C/SPI/serial, события USB hotplug, heartbeat, маршрутизация между лёгкой и тяжёлой моделью, OpenAI-совместимый `api_base`, то есть потенциально RouterAI;
   - второй — **nanobot**: Python, независимо измерен, удобен для исследования;
   - тяжёлая точка отсчёта — **OpenClaw**: есть официальный гайд для Raspberry Pi, минимум 1 ГБ RAM, только 64-бит;
   - по желанию — **ZeroClaw**: Rust, трейт `Peripheral`, GPIO Raspberry Pi, SOP-движок с MQTT-триггерами.

   NanoClaw для одноплатника неудобен: нужны Docker и Claude Agent SDK. Его берём как источник архитектурных идей: изоляция и «script gates».

---

## 1. Расшифровка заметок руководителя

| Фраза из заметок | Что это на самом деле (проверено) |
|---|---|
| «НаноКлав» | **NanoClaw**, `github.com/nanocoai/nanoclaw` (ранее `qwibitai/nanoclaw`, API отдаёт 301-редирект). Автор — Gavriel Cohen (`gavrielc`, 1417 коммитов, первый среди контрибьюторов). Создан 2026-01-31 |
| «ПикоКлов» | **PicoClaw**, `github.com/sipeed/picoclaw`. Инициатор — Sipeed, китайский производитель RISC-V-плат LicheeRV, MaixCAM, NanoKVM. Репозиторий создан 2026-02-04, релиз 2026-02-09 |
| «внешняя ллм которая знает контекст оборудования» | Это узор всего семейства: модель в облаке, «знание об оборудовании» попадает в промпт. Варианты: skill-файл (PicoClaw `workspace/skills/hardware/SKILL.md`), манифест возможностей (ROSClaw), инструменты MCP-сервера на устройстве (xiaozhi-esp32, ESP-Claw), снимок состояния `GetLiveContext` (Home Assistant) |
| «контекст работы с пользователем» | Память в файлах: `MEMORY.md`, `USER.md`, `SOUL.md`, дневные заметки, JSONL-история по чатам, суммаризация старых ходов (PicoClaw, MimiClaw, nanobot, OpenClaw) |
| «мсп сервер чтобы получать информацию о подключенном оборудовании» | MCP-клиент есть у всех крупных проектов (PicoClaw ≥ v0.2.1, nanobot, OpenClaw, ZeroClaw, NullClaw, IronClaw, Moltis). MCP-сервер на самом устройстве — у xiaozhi-esp32 и ESP-Claw (сервер и клиент). Home Assistant 2025.2+ — MCP Server |
| «работа системы во времени, несколько часов в день» | В семействе это heartbeat (OpenClaw, PicoClaw, MimiClaw — по умолчанию 30 мин), cron и SOP/routines. Именно они создают «фоновую» стоимость в токенах (раздел 6) |
| «локальные тупые ллм… нужна нормальная оперативка» | Подтверждено. Документация OpenClaw для Pi прямо пишет «do not run local LLMs on a Pi». Статья Kalušev et al.: на Pi 4 8 ГБ отчёт от llama3.2:1b идёт десятки секунд, есть таймауты. PR nanobot #610: на Pi 5 с моделью 1–3B и определениями инструментов ответ ~6 мин |
| «гпт мини различных версий» | OpenClaw в гайде для Pi как пример называет «GPT-5.4-mini». Salman et al. сравнивали агентов на `gpt-4o-mini` |

---

## 2. Генеалогия (кто от кого)

```
Clawd (личный бот Steinberger'а, обёртка над Claude Code, WhatsApp)
 └─ OpenClaw  (Warelay 24.11.2025 → CLAWDIS 03.12.2025 → Clawdbot 02.01.2026 → Moltbot 27.01.2026 → OpenClaw 30.01.2026)
     ├─ nanobot (HKUDS, Python, 01–02.02.2026)  «~4000 строк, на 99% меньше Clawdbot (430k+)»
     │    ├─ PicoClaw (Sipeed, Go, 09.02.2026)  «inspired by NanoBot», переписан с нуля; в pkg/agent/memory.go: «Inspired by and based on nanobot»
     │    │    └─ femtoclaw (порт PicoClaw на MCU ESP32/Pico W), luci-app-picoclaw (OpenWrt)
     │    └─ MimiClaw (ESP32-S3, C, 04.02.2026)  в docs/ARCHITECTURE.md — таблица «Nanobot Reference Mapping»: agent/loop.py → agent_loop.c …
     │         └─ ESP-Claw (Espressif, C, 17.04.2026)  «Inspired by OpenClaw… draws on MimiClaw»
     ├─ NanoClaw (Gavriel Cohen, TS, 31.01.2026)  контейнеры + Claude Agent SDK
     │    └─ MicroClaw (Rust, «inspired by nanoclaw»)
     ├─ ZeroClaw (Rust, 13.02.2026; Harvard/MIT/Sundai Club)  есть `zeroclaw migrate openclaw`
     ├─ NullClaw (Zig, 16.02.2026)
     ├─ IronClaw (NEAR AI, Rust, 03.02.2026)  WASM-песочница
     ├─ Moltis (Rust, 29.01.2026)
     ├─ zclaw (ESP32, C), WireClaw (ESP32, C++), EmbedClaw (ESP32)
     ├─ NemoClaw (NVIDIA, TS, 15.03.2026)  обёртка-песочница для OpenClaw/Hermes
     └─ «воплощённые» надстройки (статьи): ROSClaw (ROS 2), OpenGo (Unitree Go2), ABot-Claw, PHILIA (Astribot S1), HROS/Argos, VetClaw, VisionClaw
```
Источники дат и имён: Wikipedia «OpenClaw» и даты создания репозиториев из GitHub API. Связь «PicoClaw ← nanobot» — из README PicoClaw и заголовка `pkg/agent/memory.go`. Связь «MimiClaw ← nanobot» — из `docs/ARCHITECTURE.md` MimiClaw.

---

## 3. Карточки источников

### 3.1. Claw-семейство для Linux (одноплатники, ПК)

#### S1. OpenClaw — родитель семейства
- **Ссылки:** https://github.com/openclaw/openclaw · https://en.wikipedia.org/wiki/OpenClaw · https://docs.openclaw.ai/platforms/raspberry-pi · https://docs.openclaw.ai/nodes · https://docs.openclaw.ai/gateway/heartbeat · https://docs.openclaw.ai/tools/mcp
- **Что это:** личный ИИ-ассистент, который работает на своём компьютере и общается через мессенджеры. Автор — Peter Steinberger. Лицензия MIT, TypeScript (плюс Swift/Kotlin для приложений-компаньонов). Сейчас проектом управляет OpenClaw Foundation, некоммерческая организация 501(c)(3). По Wikipedia, 14.02.2026 Steinberger объявил о переходе в OpenAI.
- **Факты:**
  - 391 497 звёзд, 82 299 форков (API, 2026-10-06). Репозиторий создан 2025-11-24. Wikipedia на 02.03.2026: 247 000 звёзд и 47 700 форков.
  - Архитектура: **Gateway** — «локальная плоскость управления» сессиями, инструментами, событиями и каналами. К нему подключаются Control UI, CLI и TUI. **Channels** — WhatsApp, Telegram, Slack, Discord, Signal, iMessage и «20+ more». Node 24.16+ или 26.1+ (README).
  - **Nodes** — устройства-«периферия» (macOS/iOS/watchOS/Android/headless). Подключаются к Gateway по WebSocket с `role: "node"` и отдают пространства команд `camera.*`, `screen.*`, `device.*`, `notifications.*`, `system.*`, `location.*` через `node.invoke`. Это готовая модель «контекста оборудования»: устройство регистрирует свои возможности у шлюза.
  - **MCP:** поддержан нативно. Транспорты stdio, Streamable HTTP, SSE, ключи `mcp.servers`, `toolFilter`. MCP-инструменты проходят через те же tool-policy, что и встроенные.
  - **Raspberry Pi (официальная документация):** минимум «1 GB RAM, 1 core, 500 MB free disk, 64-bit OS»; рекомендуется «2 GB+ RAM, 16 GB+ SD card (or USB SSD), Ethernet». Pi 5 4/8 ГБ — «Best», Pi 4 4 ГБ — «Good», Pi 4 2 ГБ — «OK with swap», Pi 3B+ — «Slow but functional», Pi Zero 2 W — «Not recommended». 32-бит не поддерживается. При RAM ≤ 2 ГБ — swap-файл 2 ГБ и `vm.swappiness=10`. Совет: «do not run local LLMs on a Pi». В примерах моделей названы Claude Sonnet 4.6 и GPT-5.4-mini.
  - **Heartbeat:** периодический ход агента, по умолчанию `every: "30m"` (1 ч при авторизации через Anthropic OAuth). Документация предупреждает: «Heartbeats run full agent turns. Shorter intervals burn more tokens». С `isolatedSession: true` токенов на запуск становится «~100K → ~2–5K». Есть `lightContext`, `activeHours` и отдельная, более дешёвая `model`.
- **Чем полезно для ВКР:** тяжёлая точка отсчёта (верхняя граница по ресурсам). Даёт официальные требования к Pi и готовые абстракции: node + capability, heartbeat с ценовыми ручками. Документированные цифры heartbeat напрямую задают метрику «токены в сутки» (раздел 6).

#### S2. NanoClaw — «безопасный и маленький» OpenClaw
- **Ссылки:** https://github.com/nanocoai/nanoclaw (ранее qwibitai/nanoclaw) · https://nanoclaw.dev
- **Что это:** TypeScript, MIT, 30 882 звезды и 12 777 форков. Создан 2026-01-31, основной автор `gavrielc` (Gavriel Cohen). Мотивация в README: в OpenClaw «nearly half a million lines of code, 53 config files, and 70+ dependencies», а безопасность — на уровне приложения, всё в одном Node-процессе **[заявлено]**.
- **Архитектура (README v2):** `messaging apps → host process (router) → inbound.db → container (Bun, Claude Agent SDK) → outbound.db → host process (delivery)`. Каждая сессия держит два SQLite-файла, у каждого ровно один писатель. Хост раз в 60 с выполняет sweep. У каждой группы агентов свой `CLAUDE.md`, своя память, свой контейнер и только разрешённые монтирования. Учётные данные не попадают в контейнер: запросы идут через credential gateway (OneCLI Agent Vault).
- **LLM:** «из коробки» — Claude через Claude Agent SDK. Другие модели ставятся skill-ами: `/add-codex`, `/add-opencode` (OpenRouter, Google, DeepSeek и др.), `/add-ollama-provider`. Подходит и любой Anthropic-совместимый endpoint через `ANTHROPIC_BASE_URL`.
- **Каналы:** WhatsApp, Telegram, Discord, Slack, Teams, iMessage, Matrix и другие. Ставятся по требованию skill-ами `/add-*`.
- **Работа во времени:** запланированные задачи с «script gates», которые не будят агента, если работы нет. Это прямой приём экономии токенов.
- **Требования:** macOS или Linux (Windows через WSL2), Node.js 22+, pnpm 10+, Docker. Заявленных цифр RAM нет.
- **Моё наблюдение (GitHub tree API, 2026-10-06):** в trunk около 472 файлов `.ts` без тестов, ≈3,3 МБ исходников: `.claude/skills` ≈582 КБ, `container/agent-runner` ≈351 КБ, `src/modules` ≈258 КБ и т.д. Тезис README «one process and a handful of files» к текущей v2 буквально не относится.
- **Чем полезно:** архитектурные приёмы — изоляция агента в контейнере, «один писатель на файл» вместо IPC, script gates для фоновых задач. Как baseline для одноплатника неудобен: Docker-демон, Bun-контейнер, привязка к Claude Agent SDK.

#### S3. PicoClaw (Sipeed) — главный кандидат в baseline
- **Ссылки:** https://github.com/sipeed/picoclaw · https://picoclaw.io · `docs/guides/hardware-compatibility.md` · issue #346 · `pkg/tools/hardware/*` · `pkg/heartbeat/service.go` · `docs/architecture/routing-system.md` · `docs/architecture/agent-refactor/context.md`
- **Что это:** «Ultra-Efficient AI Assistant in Go». Go, MIT, 30 012 звёзд и 4 454 форка, создан 2026-02-04. Последний релиз v0.3.1 от 2026-07-03, последний push — 2026-09-24. README: «independent… written entirely in Go from scratch — not a fork», «inspired by NanoBot», «95% of core code was generated by an Agent».
- **Заявленные ресурсы [заявлено]:** «<10MB RAM», «Boots in <1s even on a 0.6GHz single-core». Оговорка в самом README: «Recent builds may use 10-20MB RAM». В таблице сравнения: OpenClaw >1 ГБ / >500 с, NanoBot >100 МБ / >30 с, PicoClaw <10 МБ / <1 с — при «0.8GHz core», без методики.
- **Issue #346 «Prevent memory bloat» (2026-02-17, открыт):** текущая база ~10 МБ, целевое железо — платы с DDR от 64 МБ. Предложена «20MB Hard Red Line» и профилирование памяти в CI. Сами разработчики признают, что потребление памяти растёт.
- **Минимальные требования (hardware-compatibility.md):** RAM — 10 МБ свободно минимум, 32 МБ+ рекомендуется. Диск — 20 МБ под бинарник, 50 МБ+ с рабочим каталогом. CPU — любой одноядерный от 0,6 ГГц. Linux kernel 3.x+. Сеть обязательна для вызовов LLM API.
  - Проверенные платы: Raspberry Pi 1/2/3/4/5/Zero, LicheePi Zero (64 МБ), LicheeRV-Nano (SG2002, 256 МБ), Milk-V Duo 64M (CV1800B, **64 МБ**), CanMV-K230, MaixCAM/MaixCAM2, NanoKVM.
  - Устройства: роутер Xiaomi 3G (MT7620 MIPS, 256 МБ), Nokia N900, Android через Termux или APK.
  - Сборки: `linux-amd64/arm64/arm/riscv64/loong64/mipsle`. Есть цель `make build-pi-zero` (Pi Zero 2 W, 32 и 64 бит).
  - Архив релиза v0.3.1 `picoclaw_Linux_arm64.tar.gz` весит 21 933 591 байт (API релизов). Внутри, судя по README, ядро плюс launcher с веб-интерфейсом. Отдельный размер бинарника ядра не проверял.
- **Цикл агента и контекст (context.md):** окно контекста = system prompt + summary + история сессии + определения инструментов. Бюджет истории = `ContextWindow − system − summary − tool_defs − MaxTokens`, по умолчанию `ContextWindow = MaxTokens × 4`. Три пути сжатия:
  1. асинхронная суммаризация после хода;
  2. проактивная проверка бюджета перед вызовом LLM («prevents wasted (and billed) LLM calls»);
  3. аварийное сжатие — отбросить ~50% старых ходов.

  Оценка токенов — эвристика ~2,5 символа на токен. «Ход» (Turn) — атомарная единица сжатия.
- **Память:** `memory/MEMORY.md` (долговременная) и `memory/YYYYMM/YYYYMMDD.md` (дневные заметки). В промпт кладутся долговременная память и заметки за последние 3 дня (`GetMemoryContext`). Запись атомарная, с fsync, «for flash storage reliability». С v0.2.1 есть JSONL-хранилище памяти.
- **Маршрутизация моделей (routing-system.md, `pkg/routing/features.go`):** выбор между основной и «light» моделью по структурным признакам — оценка токенов, число блоков кода, число недавних вызовов инструментов (окно 6), глубина диалога, наличие вложений. Без анализа ключевых слов. Это готовый механизм экономии на «GPT mini разных версий».
- **Heartbeat (`pkg/heartbeat/service.go`):** `defaultIntervalMinutes = 30`, `minIntervalMinutes = 5`. Промпт берётся из `HEARTBEAT.md`.
- **Оборудование:**
  - `pkg/tools/hardware/i2c.go` — действия `detect/scan/read/write`, только Linux (`/dev/i2c-*`). Адрес проверяется на 7-битный диапазон 0x03–0x77. Для записи нужно `confirm: true`. До 256 байт за транзакцию.
  - `spi` — `list/transfer/read`, до 4096 байт.
  - `serial` — `list/read/write`, до 4096 байт, белый список имён портов. На Linux и macOS скорость до 230400.
  - `pkg/devices` — сервис событий **USB hotplug** (Linux) на шину сообщений агента.
  - Skill `hardware/SKILL.md` с распиновкой и картами регистров AHT20, BME280, SSD1306, MPU6050, DS3231, INA219, PCA9685. Дизайн-документ (`docs/design/current-hardware-support-and-serial.zh.md`) прямо называет недостаток: нет единого слоя описания аппаратных возможностей («统一硬件 capability 描述层»).
- **MCP:** нативный клиент с v0.2.1 (2026-03-09). Транспорты stdio, SSE, HTTP. CLI `picoclaw mcp add/list/test/edit/remove`. Есть флаг `--deferred` — отложенная загрузка инструментов.
- **LLM:** 30+ провайдеров через `model_list` (`protocol/model`). У каждой модели можно задать `api_base`, `rpm`, `fallbacks`, `request_timeout`, `custom_headers`. Значит, любой OpenAI-совместимый агрегатор подключается сменой `api_base`. Совместимость RouterAI надо проверить (это другая область).
- **Каналы:** 19+, среди них Telegram (long polling), Discord, Matrix, VK, **MQTT**, MaixCam (TCP).
- **Чем полезно:** лучший baseline. Работает на Pi Zero-классе и ниже, умеет I2C/SPI/serial, USB-события и MCP, имеет heartbeat и маршрутизацию моделей, лицензия MIT, код на Go, удобный для профилирования (pprof, runtime.MemStats). Слабое место — заявленные цифры RAM не подтверждены независимо, а сами разработчики фиксируют рост потребления (#346). Измерить это на плате под реальной нагрузкой — готовый вклад ВКР.

#### S4. nanobot (HKUDS, Гонконгский университет)
- **Ссылки:** https://github.com/HKUDS/nanobot · README на коммите `ec4340d` (2026-02-09) · PR #610
- **Что это:** «ultra-lightweight, open-source, self-hosted personal AI agent framework in Python». MIT, 48 821 звезда и 8 611 форков, создан 2026-02-01.
- **Факты:**
  - Ранний README: «~4,000 lines of code — 99% smaller than Clawdbot's 430k+ lines», «Real-time line count: 3,510 lines» (скрипт `core_agent_lines.sh`) **[заявлено]**.
  - Сейчас: WebUI, TUI, Python SDK, OpenAI-совместимый API, MCP, cron, субагенты, долговременная память «Dream». Python ≥ 3.11. Колёса для Linux ARM64 есть.
  - Провайдеры — через реестр (`providers/registry.py`). Поддержаны vLLM и любой OpenAI-совместимый сервер.
- **PR #610 «minimalist mode for low-resource hardware (Pi 5)», 2026-02-13, закрыт:** на Pi 5 с локальной моделью 1–3B через Ollama определения инструментов в промпте дают ответ «exceeding 5–6 minutes» и галлюцинации вызовов. Без инструментов — «< 2 seconds» **[заявлено автором PR]**. Это иллюстрация цены префилла схем инструментов на слабом железе.
- **Независимые замеры:** см. S16, медиана пиковой памяти 136,1 МиБ.
- **Чем полезно:** второй baseline. Есть независимо измеренная точка, читаемый код, архитектура — эталон для MimiClaw и PicoClaw.

#### S5. ZeroClaw (Rust)
- **Ссылки:** https://github.com/zeroclaw-labs/zeroclaw · README на коммите `835d51d` (2026-02-19) · `docs/book/src/hardware/{subsystem,hardware-peripherals-design,raspberry-pi-setup}.md`
- **Что это:** «agent runtime — a single Rust binary». API показывает Apache-2.0, README — двойную MIT OR Apache-2.0. 32 934 звезды, создан 2026-02-13. Автор `theonlyhennygod`, руководитель `JordanTheJet`. «Built by students and members of the Harvard, MIT, and Sundai.Club communities».
- **Заявленные и «измеренные» ресурсы (README от 19.02.2026):** RAM «< 5MB», старт «< 10ms» (0,8 ГГц), бинарник 3,4 МБ. Строкой ниже — пример замера: «macOS arm64, measured on February 18, 2026: Release binary size: 8.8M; `zeroclaw --help`: about 0.02s real time, ~3.9MB peak memory footprint; `zeroclaw status`: ~4.1MB» **[измерено авт.: CLI-команда, не цикл агента]**. Там же оценка, что Node.js у OpenClaw добавляет «~390MB additional memory overhead».
- **Оборудование:** feature-флаги `hardware`, `peripheral-rpi` (нативный GPIO Pi), `probe` (probe-rs SWD).
  - Трейт `Peripheral`: GPIO, I2C, SPI, USB на Raspberry Pi, STM32 Nucleo, Arduino Uno Q, ESP32.
  - Платы опознаются по USB VID/PID.
  - Два режима: «Edge-Native» (агент на ESP32 или Pi) и «Host-Mediated» (агент на хосте, плата по USB/J-Link).
  - Аппаратные инструменты можно скрыть от немониторных каналов через `autonomy.non_cli_excluded_tools`.
  - systemd-юнит ставит `SupplementaryGroups=gpio spi i2c`.
  - Гайд для Pi: у рантайма «no meaningful memory floor». Ограничение — сборка на устройстве: fat LTO может вызвать OOM. Совет — Podman вместо Docker, потому что у Podman нет резидентного демона.
- **SOP engine:** процедуры, которые запускаются по MQTT, webhook, cron или событию периферии, с шлюзами подтверждения. Это механизм «работы во времени» по событиям, а не по опросу.
- **Чем полезно:** ближе всех к формулировке «инструменты — датчики и исполнительные устройства». Полезен как источник идей по абстракции периферии. Как baseline — по желанию: экосистема Rust, сборка на Pi тяжёлая, но есть готовые бинарники.

#### S6. NullClaw (Zig)
- **Ссылка:** https://github.com/nullclaw/nullclaw (MIT, 8 102 звезды, создан 2026-02-16)
- **Заявлено:** «678 KB binary · <2 ms startup · 5,300+ tests · 50+ providers · 19 channels», «~1 MB peak RSS», «<8 ms on a 0.8 GHz edge core». MCP, субагенты, «hardware peripherals», 10 движков памяти. Методика: «Measured with `/usr/bin/time -l` on ReleaseSmall builds», команды `nullclaw --help` и `nullclaw status` **[измерено авт.: CLI, не цикл]**.
- **Чем полезно:** нижняя граница «стоимости оркестратора». Хороший пример для критики методики: пиковая RSS команды `--help` не отражает работу агента с TLS, JSON и историей.

#### S7. IronClaw (NEAR AI, Rust) и S8. Moltis (Rust) — кратко
- **IronClaw:** https://github.com/nearai/ironclaw, Apache-2.0, 12 639 звёзд, создан 2026-02-03. Упор на безопасность: недоверенные инструменты в WASM-песочнице с правами по capability; секреты подставляются на границе хоста; allowlist эндпоинтов. MCP, heartbeat, routines (cron, события, webhook), гибридный поиск в памяти (полнотекстовый + векторный, RRF).
- **Moltis:** https://github.com/moltis-org/moltis, MIT, 2 884 звезды. «Runs on a Mac Mini, a Raspberry Pi…». Для слабых устройств — флаги `--no-default-features --features lightweight`. Заявлено: агент и интерфейс модели — «~7.5K lines», вся кодовая база — «~270K Rust LoC across 59 crates». Подсчёт строк (tokei) для OpenClaw даёт «~1.1M app LoC». MCP через stdio и HTTP/SSE.
- **Чем полезно:** для полноты обзора аналогов. IronClaw/WASM — пример изоляции инструментов без Docker, что важно на одноплатнике.

#### Прочие родственники (только существование, без глубокой проверки)
- `microclaw/microclaw` (Rust, 746★, «inspired by nanoclaw»).
- `warengonzaga/tinyclaw` (TS, GPL-3.0, 292★). Под именем TinyClaw есть ещё несколько мелких проектов.
- `NVIDIA/NemoClaw` (TS, Apache-2.0, 22 666★): «Run agents like Hermes, LangChain Deep Agents, and OpenClaw more securely inside NVIDIA OpenShell».
- `ID-Robots/clawbox` (OpenClaw на NVIDIA Jetson, 30★).
- `NousResearch/hermes-agent` (Python, MIT, 251 593★) — крупнейший «не-claw» конкурент.

Карточки по ним не делались.

### 3.2. Ветка микроконтроллеров (ESP32): нижняя граница железа

#### S9. MimiClaw (ESP32-S3, C)
- **Ссылки:** https://github.com/memovai/mimiclaw (MIT, 5 783★, создан 2026-02-04) · `docs/ARCHITECTURE.md`
- **Что это:** «AI assistant (OpenClaw) on a $5 chip. No Linux. No Node.js. Just pure C». Нужна ESP32-S3 с 16 МБ flash и 8 МБ PSRAM. Telegram через long polling, LLM — Anthropic или OpenAI (переключается на ходу). «USB power, 0.5 W, runs 24/7» **[заявлено]**.
- **Цикл агента:** ReAct, не более 10 итераций. Запросы к Claude по HTTPS без стриминга, с массивом tools. Системный промпт собирается из `SOUL.md`, `USER.md`, `MEMORY.md`, недавних заметок и подсказок по инструментам. История — JSONL на чат с кольцевым буфером, хранится на SPIFFS.
- **Бюджет памяти (ARCHITECTURE.md) [заявлено]:**
  - во внутренней SRAM: стеки FreeRTOS ~40 КБ, буферы WiFi ~30 КБ;
  - в PSRAM: два TLS-соединения (Telegram и Claude) ~120 КБ, буферы разбора JSON ~32 КБ, кэш истории ~32 КБ, системный промпт ~16 КБ, поток ответа LLM ~32 КБ; остаётся ~7,7 МБ.

  Задачи: `tg_poll` (12 КБ стека), `agent_loop` (12 КБ), `outbound` (8 КБ). `max_tokens: 4096`.
- **Работа во времени:** heartbeat читает `HEARTBEAT.md` раз в 30 мин. Cron-задачи, которые LLM создаёт сама, хранятся в `cron.json` и переживают перезагрузку.
- **Чем полезно:** показывает, что оркестратору с внешней LLM хватает **сотен килобайт**. Основные расходы — TLS и буферы JSON. Это довод, что на одноплатнике узкое место — не RAM агента, а сеть, токены и энергия. Готовая раскладка бюджета памяти — образец для раздела ПЗ.

#### S10. zclaw (ESP32, C)
- **Ссылка:** https://github.com/tnm/zclaw (MIT, 2 233★, создан 2026-02-16)
- **Факты [заявлено]:** прошивка целиком «<= 888 KiB», включая ESP-IDF/FreeRTOS, Wi-Fi, TLS и бандл сертификатов. Кода приложения — «~35KB» (по описанию репозитория).
  - Цели: ESP32, C3, S3, C6.
  - Инструменты: `gpio_read_all`, `i2c_scan`, `i2c_read`/`i2c_write`, `dht_read`, `get_diagnostics` (memory, rates, time).
  - Провайдеры: Anthropic, OpenAI, OpenRouter, Ollama.
  - Лимиты вызовов LLM по умолчанию «100/hour and 1000/day» (`RATELIMIT_*`).
- **Чем полезно:** **ограничение частоты вызовов LLM как часть конфигурации**. Это прямая аналогия метрики «работа во времени».

#### S11. WireClaw (ESP32, C++)
- **Ссылка:** https://github.com/M64GitHub/WireClaw (193★, лицензия не указана, создан 2026-02-09)
- **Идея:** LLM создаёт правило, например `rule_create(sensor_name="chip_temp", condition="gt", threshold=28, ...)`, а дальше правило крутится в основном цикле прошивки «No cloud, no server, no LLM calls» и опрашивает датчик раз в 5 с.
- **Чем полезно:** ключевой архитектурный приём для ВКР — **LLM как «компилятор» правил вместо LLM в цикле опроса**. Регулярный мониторинг датчиков переносится из платного цикла LLM в локальный детерминированный движок правил. Это гипотеза для эксперимента: насколько это снижает токены в сутки по сравнению с heartbeat.

#### S12. ESP-Claw (Espressif) и xiaozhi-esp32 — MCP на стороне устройства
- **ESP-Claw:** https://github.com/espressif/esp-claw (Apache-2.0, 2 200★, создан 2026-04-17). Официальный фреймворк производителя чипов: «Chat Coding AI agent framework for IoT devices».
  - Событийный цикл агента и динамическая загрузка Lua: поведение устройства задаётся в чате.
  - Структурированная память.
  - **MCP: «Works as both Server and Client»**.
  - LLM в стиле OpenAI и Anthropic плюс свои endpoint'ы. Каналы: Telegram, QQ, Feishu, WeChat.
  - Чипы: ESP32-S3, P4, C5, S31.
- **xiaozhi-esp32:** https://github.com/78/xiaozhi-esp32 (MIT, 30 445★, создан 2024-08-31). «An MCP-based chatbot».
  - Голосовой терминал: офлайн-распознавание слова активации (ESP-SR), Opus-поток, ASR + LLM + TTS.
  - **Device-side MCP:** устройство регистрирует инструменты через `McpServer::AddTool(name, description, properties, callback)`, например `self.dog.forward` или `self.light.set_rgb(r,g,b)` с диапазонами 0–255. Бэкенд подключается по WebSocket или MQTT+UDP и вызывает `initialize`, `tools/list`, `tools/call` (JSON-RPC 2.0).
  - `AddUserOnlyTool` — скрытые инструменты (перезагрузка, OTA), которые модель не может вызвать сама.
  - 138 каталогов плат и 171 вариант сборки.
- **Чем полезно:** готовый образец «MCP-сервер = устройство с датчиками и приводами». Транспорт MCP — WebSocket или MQTT, а не stdio. Узор «user-only tools» нужен для безопасных исполнительных устройств. Для ВКР: одноплатник как MCP-хост или клиент, а периферийные узлы (ESP32 или симулятор) как MCP-серверы.

### 3.3. Умный дом и протокол

#### S13. Home Assistant: MCP Server, MCP client, LLM API
- **Ссылки:** https://www.home-assistant.io/integrations/mcp_server/ · https://www.home-assistant.io/integrations/mcp/ · https://developers.home-assistant.io/docs/core/llm/ · https://github.com/home-assistant/core (Apache-2.0, 91 275★)
- **Факты:**
  - **MCP Server** (с HA 2025.2) отдаёт LLM-клиентам контекст и действия умного дома. Транспорт — Streamable HTTP. Есть Tools, Prompts и Resources. Ресурс-снимок совпадает с выводом инструмента `GetLiveContext`. Доступ ограничен «exposed entities». Авторизация — OAuth (IndieAuth) или long-lived token. Не поддержаны «Sampling» и «Notifications».
  - **MCP client** (с 2025.2): инструменты MCP-серверов становятся доступны агентам разговора HA. Транспорт SSE. Поддержаны только Tools и Authorization.
  - **LLM API:** классы `llm.API`/`APIInstance`, инструменты через `llm.Tool` и `async_call`. Интеграции добавляют инструменты через `<integration>/llm.py` → `async_get_tools`.
  - Неофициальный `homeassistant-ai/ha-mcp` — 4 956★.
- **Вторичное [не проверено]:** в обзорах встречается совет держать для малых локальных моделей меньше 25 «exposed entities» (страница devhandbook.io, из поиска). Первоисточник не нашёл.
- **Чем полезно:** промышленный образец ответа на вопрос «как отдать LLM контекст оборудования». Это фильтр видимых сущностей плюс плоский текстовый снимок состояния (`GetLiveContext`) плюс инструменты-намерения. Ограничение «нет Notifications» значит, что события устройств модель не видит сама. Это открытый вопрос и для ВКР.

#### S14. Спецификация Model Context Protocol (текущая версия 2026-07-28)
- **Ссылка:** https://modelcontextprotocol.io/specification/latest (ссылается на `schema/2026-07-28/schema.ts`)
- **Факты:** JSON-RPC 2.0. В разделе Key Details сказано «Stateless, self-contained requests», согласование возможностей — на каждый запрос. Возможности сервера: Resources, Prompts, Tools. Возможности клиента: **Elicitation** (Sampling в ключевых возможностях клиента больше не упомянут). Расширения: **Tasks** (асинхронные долгие операции с опросом и устойчивыми дескрипторами), «Skills over MCP», MCP Apps. В разделе безопасности: «Hosts must obtain explicit user consent before invoking any tool».
- **Чем полезно:** для ВКР это нормативная база MCP-сервера оборудования. Расширение Tasks подходит для долгих действий приводов. Версию спецификации нужно зафиксировать в ПЗ. Подробно MCP разбирается в другой области; здесь — только связь с аналогами.

### 3.4. Научные статьи (2026)

#### S15. Salman, Halgamuge, Susnjak. Resource Constraints and Performance in Agentic AI Systems. arXiv:2608.27886, 28.08.2026
- **Ссылка:** https://arxiv.org/abs/2608.27886 (числа проверены в HTML: https://arxiv.org/html/2608.27886v1)
- **Что сделали:** сравнили OpenClaw 2026.2.26-beta.1 и NanoBot v0.1.5 как целые системы, модель OpenAI `gpt-4o-mini`. Основной бенчмарк — 100 промптов (30 коротких, 35 средних, 35 длинных). Детальный инструментированный слой — 23 промпта.
- **Числа [измерено незав.]:**
  - полное выполнение: 31% у OpenClaw против 25% у NanoBot; разница 6 п.п., 95% bootstrap-интервал от −3 до 15 п.п., то есть **значимого различия нет**;
  - в детальном слое: обе по 26%; хотя бы частичное выполнение — 43% у NanoBot против 26% у OpenClaw;
  - **медиана пиковой памяти — 2926,6 МиБ (OpenClaw) против 136,1 МиБ (NanoBot)**, геометрическое отношение 19,44 (95% ДИ 17,61–21,32);
  - медиана времени — 34,063 с (IQR 13,040–70,754) против 10,446 с (IQR 6,528–17,212), отношение 2,98 (1,78–5,06);
  - OpenClaw медленнее на 19 из 23 промптов и тяжелее по памяти на всех;
  - из 18 случаев доминирования NanoBot 10 — «cheaper joint failures», то есть оба провалились, но NanoBot дешевле.
- **Методические дыры (сами авторы перечисляют, что не записано):** хост — «Docker Desktop/WSL2», CPU, RAM, лимиты контейнеров и версии ОС **не записаны**. Сборщик памяти и частота опроса не указаны. Токены и стоимость не приводятся. Одна попытка на пару «система–промпт». Параметры инференса у систем разные: температура 0 против 0,1.
- **Чем полезно:** главный близкий аналог экспериментальной части ВКР и прямой «заявлено против измерено». OpenClaw заявлен как «>1 ГБ», измерено ~2,9 ГиБ в контейнере. NanoBot заявлен как «>100 МБ», измерено 136 МиБ. Статья подсказывает, что сделать лучше: реальный одноплатник, записанное окружение (это уже делает `harness/envinfo.py`), токены и стоимость, несколько повторов, ряды RSS во времени, одинаковые параметры инференса. Их тезис «связывать итог с конкретным исполнением» совпадает с идеей нашего монитора трасс (`monitor/check_trace.py`).

#### S16. Cardenas et al. ROSClaw: An OpenClaw ROS 2 Framework for Agentic Robot Control and Interaction. arXiv:2603.26997, 27.03.2026
- **Ссылка:** https://arxiv.org/abs/2603.26997 (HTML проверен)
- **Что это:** исполнительный слой между OpenClaw и ROS 2.
  - Динамически обнаруживает возможности робота и внедряет их в промпт: манифест «all available topics, services, and actions with their message types» кладётся в системный промпт. Для TurtleBot3 это `/cmd_vel` (Twist), `/odom`, `/scan`, камеры.
  - Нормализует наблюдения.
  - Проверяет действие **до** исполнения в «safety envelope» через хук `before_tool_call`.
  - Ведёт аудиторский журнал.
- **Числа [измерено авт.]:** транспорт — локальный DDS < 1 мс, rosbridge WebSocket 5–10 мс, WebRTC P2P 20–100 мс. **Средняя задержка одного хода LLM — 1,3–2,3 с (P95 ≤ 3,8 с)**, многошаговые задачи — 6–15 ходов и «tens of seconds total». Вывод авторов: такой агент годится для «deliberative planning but not reactive control». Доля предложений действий вне политики различается до 4,8× между моделями (3,4× среди фронтирных). Платформы: TurtleBot3 Waffle Pi (полная оценка), Unitree Go2 и G1. Бэкенды: Claude Opus 4.6, GPT-5.2, Gemini 3.1 Pro, Llama 4 Maverick (vLLM).
- **Чем полезно:** прямой ответ на «ROS / Gazebo» из заметок. Показывает, как «контекст оборудования» превращается в манифест возможностей. Даёт числа задержек, на которые опирается аргумент «LLM — для планирования, рефлексы — локально». Код пока в анонимном артефакте (anonymous.4open.science), публичный выпуск обещан после принятия статьи.

#### S17. Kalušev, Brkljač, Brkljač. Multi-Agent Object Detection Framework Based on Raspberry Pi YOLO Detector and Slack-Ollama Natural Language Interface. arXiv:2604.13345, 14.04.2026
- **Ссылка:** https://arxiv.org/abs/2604.13345 (HTML проверен)
- **Что сделали:** на одном Raspberry Pi 4 Model B (BCM2711, 4×Cortex-A72 @ 1,8 ГГц, **8 ГБ LPDDR4**, камера IMX219) запустили YOLOv8n-агента зрения, Slack-бота и LLM-агента отчётов через Ollama. Модели: tinyllama (637 МБ), llama3.2:1b (1,3 ГБ), gemma3 (3,3 ГБ) и облачная kimi-k2.5:cloud. Оркестрация — своя событийная, как противопоставление OpenClaw.
- **Числа [измерено авт.]:** «less then 3 fps at 640x480». Генерация отчёта — «at the order of tens of seconds» даже с консервативными промптами. Отсюда задержки и потерянные уведомления по таймаутам. Нагрузка CPU очень высокая уже от одного агента зрения. Лучшее соотношение качества и скорости среди локальных — llama3.2:1b, но и она «not performing at expected speed». Лучшие результаты — с облачной kimi.
- **Чем полезно:** эмпирическое подтверждение фразы руководителя «локальные тупые LLM… нужна нормальная оперативка» и аргумент за внешнюю LLM. Близкий сценарий «камера на одноплатнике». Можно сослаться в обосновании постановки.

#### S18. Hua, Wang, Yang, Wang, Lian. GLM-5 Serving Parameter Tuning for OpenClaw. arXiv:2607.02518 (техотчёт)
- **Ссылка:** https://arxiv.org/abs/2607.02518
- **Числа [измерено авт.]:** типичный запрос OpenClaw — «about 28k-30k input tokens and 500 output tokens per request». Запросы «dominated by long, tool-augmented prefixes»: системный промпт, история, вывод инструментов. Средний TTFT на стороне сервиса — 8,98 → 6,69 с после настройки, P90 задержки — 40,23 → 32,64 с.
- **Чем полезно:** даёт порядок размера контекста claw-агента. Входные токены превышают выходные в ~60 раз. Отсюда следует, что **стоимость определяется префиксом**, а значит важны кэширование промптов и минимизация контекста оборудования. Это прямая гипотеза для экспериментов.

#### S19. Dong, Feng, Wang. Clawdrain: Exploiting Tool-Calling Chains for Stealthy Token Exhaustion in OpenClaw Agents. arXiv:2603.00902, 01.03.2026
- **Ссылка:** https://arxiv.org/abs/2603.00902
- **Числа [измерено авт.]:** троянский skill увеличивает расход токенов в 6–7 раз относительно безобидного варианта, в неудачной конфигурации — примерно в 9 раз. Модель Gemini 2.5 Pro, реальный биллинг. Векторы атаки: раздувание промпта через `SKILL.md`, накопление вывода инструментов в контексте, **«cron/heartbeat frequency amplification»**.
- **Чем полезно:** не ради безопасности (этот ракурс отклонён), а как доказательство, что **heartbeat, cron и рост контекста — главные статьи токенов во времени**. Показывает и то, что расход токенов можно измерить и надо отслеживать.

#### S20. She. AgentRM: An OS-Inspired Resource Manager for LLM Agent Systems. arXiv:2603.13110, 13.03.2026
- **Ссылка:** https://arxiv.org/abs/2603.13110
- **Факты [измерено авт.]:**
  - разобрано более 40 000 GitHub issues шести фреймворков, включая OpenClaw;
  - выделены два класса проблем: (1) отказы планирования — блокировки, зомби-процессы, каскады rate limit; (2) деградация контекста («amnesia» от неограниченного роста памяти);
  - MLFQ-планировщик: P95 задержки −86%, «lane waste» −96%, пропускная способность +168%, зомби 0 против 29;
  - менеджер жизненного цикла контекста: 100% сохранения ключевой информации против 65,1%, но 34 330 токенов на компакцию против 17 212.
- **Чем полезно:** даёт для ВКР «системный» взгляд, близкий профилю студента: агент — это набор ресурсов, которыми нужно управлять как в ОС (планирование, rate limit, «спячка» контекста). Компромисс «качество памяти против токенов на компакцию» — готовая ось эксперимента.

#### S21. Yan, Li, Liu. AgentDecarbonizer: Carbon-Aware Execution for AI Agents. arXiv:2608.20566, 20.08.2026
- **Ссылка:** https://arxiv.org/abs/2608.20566
- **Факты [измерено авт.]:** углеродный след нагрузок OpenClaw (WildClawBench) зависит от расхода токенов, переиспользования кэша контекста и углеродной интенсивности энергосети. Сдвиг выполнения во времени и между сетями при заданном дедлайне снижает выбросы до 57,9% относительно базовой линии без учёта углерода. 60 задач, 4 энергосети.
- **Чем полезно:** обоснование, что «работа во времени» — законная ось оценки агентов. **Гибкость по дедлайну** (фоновые задачи можно отложить или сгруппировать) применима и к деньгам и токенам: пакетная обработка heartbeat, окна `activeHours`.

#### S22. Oliveira. Throughput per Megabyte: A Pilot Benchmark of Language-Stack Efficiency for Self-Hosted HTTP Services on a Raspberry Pi 5. arXiv:2609.11932, 2026
- **Ссылка:** https://arxiv.org/abs/2609.11932
- **Факты [измерено авт.]:**
  - одна плата Pi 5, CRUD-сервис на SQLite на Go 1.26, Rust 1.95/Axum, Python 3.13/FastAPI, Node.js 24/Fastify и .NET 10 AOT;
  - N = 50 рандомизированных прогонов, отдельный ряд замеров памяти во времени;
  - Rust: взвешенная пиковая RSS 7,36 МБ, 310,25 req/s/МБ (2,8× Go);
  - **временной ряд памяти: Rust держит ровные 7,8 МБ RSS, Node.js вырос на 117 МБ** без заметных сборок мусора.
- **Чем полезно:** методический образец замера на одноплатнике: idle RSS, энергия на запрос, временные ряды памяти, рандомизация, bootstrap-ДИ. Поведение рантаймов — тот же вопрос «Go (PicoClaw) / Rust (ZeroClaw) / Python (nanobot) / Node (OpenClaw)» под длительной работой.

#### S23. Воплощённые агенты на базе OpenClaw (группа)
- **Liu et al. VisionClaw: Always-On AI Agents through Smart Glasses.** arXiv:2604.03486. Постоянно включённый агент на Meta Ray-Ban, задачи делегируются в OpenClaw, в том числе управление IoT. Лабораторное исследование N = 12 и **лонгитюдное развёртывание N = 5**. Ценность: методика «длительной эксплуатации» с пользователями.
- **Jin et al. PHILIA (A Glimpse into Long-term Physical Coexistence with Intelligent Robots).** arXiv:2607.11377. Абстракция «robot gateway» над OpenClaw: «decouples low-frequency, high-semantic agent reasoning from high-frequency, low-level robot execution». Единый интерфейс возможностей: наблюдение, выполнение задачи, навигация, речь, статус, отмена. Роботы Astribot S1.
- **Yan et al. Harness Robotic OS (HROS/Argos).** arXiv:2609.11225. Общий контекст «связывает физическое состояние с рассуждением агента». Память трёх уровней: рабочая, эпизодическая, семантическая. Квадропед, оркестрация Qwen3-VL через OpenClaw. 100% достижимости точек маршрута, реакция на препятствие < 200 мс (локально), 99% доставки тревог.
- **Hasan et al. VetClaw.** arXiv:2607.26042. Граница и облако: камера и OpenClaw на краевом устройстве (планирование, инструменты, уведомления), VLM на сервере. Близкий к ВКР сценарий «камера → внешняя модель».
- **OpenGo** (arXiv:2604.01708, Unitree Go2) и **ABot-Claw** (arXiv:2604.10096) — дополнительно.
- **Чем полезны:** общий вывод группы — **разделение частот**. Рассуждение LLM идёт редко, секунды на ход; исполнение и рефлексы локальны, миллисекунды. Это аргумент для архитектуры ВКР: одноплатник держит быстрый локальный контур (правила, проверка безопасности, драйверы), внешняя LLM — медленный семантический.

#### S24. Barbaste et al. Harness Engineering: Anatomy, Architecture, and Evolution of Coding Agents — A Source-Code Study of Eleven Systems. arXiv:2609.00006
- **Ссылка:** https://arxiv.org/abs/2609.00006
- **Факты:** разобраны исходники 11 харнессов, включая OpenClaw. Семь канонических подсистем, 29 паттернов. «SKILL.md skills lead MCP in adoption (9/11 vs. 8/11)». «No agent runtime imports a general-purpose agentic framework» — циклы везде написаны вручную. В конце — 90-строчный каркас минимального харнесса.
- **Чем полезно:** обоснование решения «пишем собственный тонкий оркестратор, а не берём LangChain». Даёт словарь подсистем для архитектурной главы ПЗ.

#### Дополнительно (метаданные проверены, глубоко не разбирались)
- `nasa-jpl/rosa` — ROS-агент от NASA JPL, Apache-2.0, 1 653★; с ним ROSClaw сравнивает себя по протоколу паритета.
- `robotmcp/ros-mcp-server` — MCP-мост к ROS, Apache-2.0, 1 486★.

Обе темы — для области про ROS и Gazebo.

---

## 4. Сводная таблица аналогов

Звёзды — GitHub API на 2026-10-06. «RAM заявлено» — из README. «RAM измерено» — только с указанной методикой.

| Проект | Язык / лицензия | ★ | Где работает (заявлено) | RAM заявлено | RAM измерено | MCP | Оборудование | LLM API | Каналы |
|---|---|---|---|---|---|---|---|---|---|
| OpenClaw | TS / MIT | 391 497 | Pi 4/5 64-бит, ≥1 ГБ (рек. 2 ГБ+) | >1 ГБ (в чужих таблицах) | **2926,6 МиБ медиана пика** (Docker/WSL2, gpt-4o-mini) [S15] | клиент (stdio/HTTP/SSE) | nodes: camera/screen/location/system | много провайдеров, плагины | 20+ |
| NanoClaw | TS / MIT | 30 882 | macOS/Linux + Docker | нет | нет | MCP-инструменты в шаблонах агентов | нет (Docker-изоляция) | Claude Agent SDK; OpenCode/Ollama через skills | WhatsApp, Telegram, Slack и др. |
| nanobot | Python / MIT | 48 821 | Linux ARM64 колёса; «Linux SBC ~$50» (в таблице PicoClaw) | >100 МБ (в таблице PicoClaw) | **136,1 МиБ медиана пика** [S15] | клиент | нет встроенного | реестр провайдеров, OpenAI-совм., vLLM | Telegram, Discord, Slack, WeChat и др. |
| **PicoClaw** | Go / MIT | 30 012 | RISC-V/ARM/MIPS/x86/LoongArch, от 64 МБ (Milk-V Duo), Pi Zero…Pi 5, OpenWrt | <10 МБ («recent builds 10–20 МБ»); мин. 10 МБ свободно | **нет независимых** (issue #346: база ~10 МБ) | клиент (stdio/SSE/HTTP), CLI `mcp` | **i2c, spi, serial; USB hotplug** | 30+, `api_base` на модель, light/heavy routing | 19+, вкл. Telegram, VK, MQTT |
| ZeroClaw | Rust / MIT или Apache-2.0 | 32 934 | любой Pi (готовые бинарники aarch64/armv7) | <5 МБ | авт.: 3,9 МБ (`--help`), 4,1 МБ (`status`), macOS | клиент | **GPIO/I2C/SPI/USB, Pi/STM32/Arduino/ESP32** | ~20 провайдеров, OpenAI-совм. | 30+ |
| NullClaw | Zig / MIT | 8 102 | «any $5 board» | ~1 МБ | авт.: `/usr/bin/time -l` на `--help`/`status` | есть | «hardware peripherals» | 50+ | 19 |
| IronClaw | Rust / Apache-2.0 | 12 639 | macOS/Linux/Windows | нет | нет | клиент | нет (WASM-инструменты) | несколько провайдеров | REPL, webhooks, Telegram, Slack (WASM) |
| Moltis | Rust / MIT | 2 884 | Mac mini, Raspberry Pi (`lightweight`) | нет | нет | stdio + HTTP/SSE | нет | несколько провайдеров | Telegram, Signal, Discord и др. |
| MimiClaw | C / MIT | 5 783 | ESP32-S3, 8 МБ PSRAM | бюджет ≈ 300 КБ + ~7,7 МБ свободно | нет (`heap_info` в CLI) | нет | (инструменты: поиск, время, cron) | Anthropic, OpenAI | Telegram, WebSocket |
| zclaw | C / MIT | 2 233 | ESP32/C3/S3/C6 | прошивка ≤ 888 КиБ | нет | нет | GPIO, I2C, DHT | Anthropic, OpenAI, OpenRouter, Ollama | Telegram, web relay |
| ESP-Claw | C / Apache-2.0 | 2 200 | ESP32-S3/P4/C5/S31 | нет | нет | **сервер и клиент** | периферия платы + Lua | OpenAI- и Anthropic-стиль | Telegram, QQ, Feishu, WeChat |
| xiaozhi-esp32 | C++ / MIT | 30 445 | ESP32 (138 плат) | нет | нет | **сервер на устройстве** (WS/MQTT) | динамик, LED, серво, GPIO, камера | через сервер xiaozhi (Qwen, DeepSeek) | голос |
| Home Assistant | Python / Apache-2.0 | 91 275 | Pi, HA Green и др. | — | — | **сервер** (Streamable HTTP) и клиент (SSE) | вся экосистема HA | агенты разговора HA | Assist, голос |

---

## 5. Заявленное против измеренного: что не так с цифрами

1. **Что мерят.** ZeroClaw и NullClaw мерят пиковую RSS процессов `--help` и `status` через `/usr/bin/time -l` на macOS arm64. Это не цикл агента с TLS-сессиями к LLM, разбором JSON, историей диалога и MCP-подпроцессами. PicoClaw метод не указывает вовсе.
2. **«Normalized for 0.8 GHz»** — таблицы ZeroClaw и NullClaw сняты на «Local machine quick benchmark (macOS arm64, Feb 2026) normalized for 0.8GHz edge hardware». Процедура нормировки не описана, на реальной плате 0,8 ГГц ничего не измерено.
3. **Чужие цифры копируются из таблицы в таблицу.** «OpenClaw >1GB / >500 s, NanoBot >100MB / >30 s» впервые появляются у PicoClaw и затем повторяются у ZeroClaw и NullClaw без новой методики.
4. **Независимые данные есть только у Salman et al. [S15].** Для OpenClaw они **хуже** заявленного (~2,9 ГиБ медиана пика против «>1 ГБ»; правда, это Docker с неизвестными лимитами). Для NanoBot близки к заявленному (136 МиБ против «>100 МБ»). Для Go, Rust и Zig независимых замеров нет.
5. **Дрейф версий.** PicoClaw сам пишет «Recent builds may use 10–20MB» и открыл issue про раздувание памяти (#346). Проекты возрастом 8 месяцев выходят раз в 1–3 недели (PicoClaw: v0.2.4 25.03 → v0.3.1 03.07.2026). Для ВКР нужно фиксировать коммит или версию, как уже сделано для llama.cpp и BFCL (docs/DECISIONS.md).
6. **Вывод для ВКР:** честное и воспроизводимое измерение claw-агентов на реальном одноплатнике во времени — **незакрытая ниша**. Нужны RSS(t), CPU-время, пробуждения, сетевой трафик, токены и их стоимость, энергия, задержки, повторы и записанное окружение. В открытой литературе на 2026-10-06 такого не найдено (поиск по arXiv: `abs:OpenClaw AND abs:Raspberry`, `all:PicoClaw`, `all:ZeroClaw`, `abs:nanobot AND abs:OpenClaw`).

---

## 6. «Работа системы во времени»: что известно о стоимости

**Факты из источников:**
- запрос OpenClaw: ~28–30 тыс. входных и ~500 выходных токенов [S18];
- heartbeat OpenClaw: по умолчанию раз в 30 мин; ~100 тыс. токенов за запуск без изоляции, ~2–5 тыс. с `isolatedSession` [S1];
- heartbeat PicoClaw: 30 мин по умолчанию, минимум 5 мин [S3];
- heartbeat MimiClaw: 30 мин [S9];
- лимит zclaw: 100 вызовов LLM в час и 1000 в сутки [S10];
- ход LLM: 1,3–2,3 с, P95 ≤ 3,8 с, задача — 6–15 ходов [S16].

**[расчёт] Фоновая нагрузка heartbeat OpenClaw** при интервале 30 мин:
- 8 часов активности в день (`activeHours`): 16 запусков × ~100 тыс. = **~1,6 млн входных токенов в день** без изоляции против 16 × 2–5 тыс. = **32–80 тыс.** с `isolatedSession`. Разница 20–50×;
- круглые сутки: 48 запусков → ~4,8 млн против 96–240 тыс.

Стоимость = токены × тариф выбранной «mini»-модели у RouterAI. Тарифы в этой области не проверял: цены — в области про RouterAI и модели. Кэширование префикса может сильно снизить реальную цену; это видно и по AgentDecarbonizer [S21], где учитывается переиспользование кэша контекста.

**Механизмы снижения стоимости, найденные в аналогах** (кандидаты для абляций в ВКР):

| Механизм | Где | Что делает |
|---|---|---|
| Изоляция или облегчение контекста heartbeat | OpenClaw `isolatedSession`, `lightContext` | урезает префикс фонового хода |
| Отдельная дешёвая модель для фона | OpenClaw heartbeat `model`; PicoClaw light/heavy routing | «mini» для рутины, большая модель — по признакам сложности |
| Окна активности | OpenClaw `activeHours` | «несколько часов в день» из заметок руководителя |
| Script gates | NanoClaw scheduled tasks | не будить LLM, если скрипт не нашёл работы |
| Компиляция правил | WireClaw `rule_create` | LLM пишет правило один раз, правило работает без LLM |
| Rate limits | zclaw `RATELIMIT_*`, PicoClaw `rpm` | жёсткий потолок вызовов |
| Проактивная проверка бюджета контекста | PicoClaw `isOverContextBudget` | не тратить платные вызовы, которые упадут по переполнению окна |
| Отложенная загрузка инструментов MCP | PicoClaw `mcp add --deferred` | меньше схем инструментов в префиксе |
| Событийные триггеры вместо опроса | ZeroClaw SOP (MQTT/peripheral), ESP-Claw event-driven, PicoClaw USB hotplug | LLM вызывается только по событию |

---

## 7. Baseline для замеров на одноплатнике и как начать без платы

**Предлагаемый набор:**
- **B1 — PicoClaw** (основной). Работает на всём, от Pi Zero 2 W до Pi 5, есть MCP-клиент и аппаратные инструменты. Конфигурация через JSON, OpenAI-совместимый `api_base`. Go даёт точные внутренние счётчики памяти (runtime/pprof).
- **B2 — nanobot.** Python-эталон с независимой точкой 136 МиБ. На нём удобно проверить, воспроизводится ли S15 на плате.
- **B3 — OpenClaw** (тяжёлая точка отсчёта). Только Pi 4/5, 64-бит, ≥2 ГБ рекомендуется. Есть официальный гайд и heartbeat с документированной стоимостью.
- **B4 (по желанию) — ZeroClaw.** Rust, GPIO и SOP. Если в ВКР будет акцент на исполнительных устройствах.
- **Свой агент ВКР** сравнивается с B1–B3 на одинаковых сценариях, одинаковой модели (GPT «mini» через RouterAI) и одинаковом MCP-сервере оборудования.

**Что брать как плату** (по совместимости аналогов): Raspberry Pi 4/5 с 2–8 ГБ — запускает всё, от B1 до B3. Pi Zero 2 W (512 МБ) — только B1/B4: OpenClaw его прямо не рекомендует. Выбор железа с ценами и наличием — отдельная область.

**Эмуляция до покупки (проверено, что есть на машине: `/opt/homebrew/bin/docker`, `colima`, `limactl`; QEMU не установлен):**
- Linux arm64 в VM colima/lima на Apple Silicon работает нативно. Docker-контейнеры с `--memory` и `--cpus` (cgroup) задают «потолок Pi».
- RSS бинарников arm64 переносится на плату приблизительно (разные ядро и размер страницы — уточнить). Задержки и CPU-время **не переносятся**: Cortex-A53/A72/A76 против M4 Pro.
- Оборудование можно имитировать:
  - MCP-сервером с синтетическими датчиками;
  - модулем ядра Linux `i2c-stub` для проверки I2C-инструментов PicoClaw — работу внутри VM colima надо проверить;
  - симулятором ROS 2/Gazebo — другая область.
- Всё это **не требует загрузок сверх обычных образов**, но образ VM и контейнеры всё равно скачиваются, поэтому нужно согласие пользователя.

---

## 8. Выводы

1. Темы руководителя «НаноКлав» и «ПикоКлов» — это NanoClaw (nanocoai, изоляция в контейнерах, Claude Agent SDK) и PicoClaw (Sipeed, Go, <10 МБ заявлено, $10-платы). Оба — облегчённые потомки OpenClaw, появились в начале февраля 2026.
2. Архитектура семейства совпадает с согласованной постановкой ВКР: тонкий оркестратор на устройстве, внешняя LLM, файловая память пользователя, heartbeat и cron, MCP-клиент, мессенджер-каналы. Значит, ВКР вписывается в живое направление, и новизну нужно искать не в «агенте на одноплатнике вообще», а в **измерении и оптимизации работы во времени** и в **контексте оборудования через MCP**.
3. Заявления о ресурсах в README методически слабы: CLI-команды на macOS и «нормировка на 0,8 ГГц». Единственная независимая работа (S15) не записала окружение и не мерила токены.
4. При внешней LLM потребление RAM оркестратора — мегабайты-десятки мегабайт (Go, Rust, C) или сотни (Python). Это нижний порядок. Доминируют префикс (~28–30 тыс. токенов на запрос у OpenClaw), фоновые ходы (heartbeat ~100 тыс. токенов на запуск без изоляции) и задержки LLM (секунды на ход).
5. Образцы «контекста оборудования» уже есть:
   - манифест возможностей в промпте (ROSClaw);
   - фильтр видимых сущностей плюс снимок `GetLiveContext` (Home Assistant);
   - устройство как MCP-сервер с user-only инструментами (xiaozhi, ESP-Claw);
   - узлы с пространствами команд (OpenClaw nodes);
   - skill-файл с картами регистров (PicoClaw).

   Их сравнения по стоимости и точности не найдено.
6. В воплощённых системах повторяется принцип разделения частот: LLM — для медленного планирования, локальный контур — для быстрых реакций и безопасности (ROSClaw, PHILIA, HROS, WireClaw).

## 9. Открытые проблемы

1. Нет независимых замеров PicoClaw, ZeroClaw, NullClaw и nanobot **на реальном одноплатнике под нагрузкой агента и во времени** (часы и сутки): RSS(t), рост памяти, CPU, пробуждения, трафик, энергия.
2. Нет данных о **токенах и деньгах в сутки** у claw-агентов при типовом профиле «несколько часов в день». Salman et al. токены не записали. В документации OpenClaw — только порядок величины для heartbeat.
3. Не сравнивались **способы подачи контекста оборудования**: всё в промпте, MCP-ресурс-снимок, инструменты по запросу, отложенная загрузка инструментов. Нет оценки их влияния на токены, задержку и точность выбора инструмента.
4. **События устройств к LLM:** HA MCP Server не поддерживает Notifications. Heartbeat — это опрос, а он дорогой. Как доставлять события датчиков агенту дёшево (MCP Tasks, правила, script gates) — открытый вопрос.
5. **Безопасность исполнительных устройств** нужна в минимальном объёме, без инфобез-ракурса: `confirm: true` (PicoClaw), user-only tools (xiaozhi), исключение аппаратных инструментов из удалённых каналов (ZeroClaw), проверка до исполнения (ROSClaw).
6. **Российские условия:** доступность Telegram, OpenAI-совместимость RouterAI с tool calling у «mini»-моделей, задержка до агрегатора — проверить в соответствующих областях.
7. Быстрый дрейф версий: результаты устаревают за недели. Нужна фиксация коммитов.

## 10. Следствия для ВКР

**Постановка.** Тема: «Разработка ИИ-агента для приложений граничного ИИ». Объект — агент-оркестратор на одноплатнике с внешней LLM и MCP-доступом к оборудованию. Предмет — **ресурсная эффективность и качество работы агента во времени**. Метрики:
- RSS(t) и CPU оркестратора;
- токены, запросы и стоимость в сутки;
- сетевой трафик в сутки;
- задержка ответа пользователю p50 и p95;
- энергия платы в сутки;
- доля корректно выполненных сценариев.

Аналоги (S1–S12) идут в обзорную главу, S15 — как ближайшая работа, недостатки которой ВКР исправляет.

**Архитектура** (берём из аналогов то, что подтверждено):
- цикл в стиле PicoClaw и nanobot, бюджет контекста и суммаризация по ходам — по `context.md` PicoClaw;
- память пользователя в файлах (`USER.md`/`MEMORY.md`/дневные заметки/JSONL);
- **MCP-сервер(ы) оборудования**: инструменты-датчики и приводы с диапазонами, как у xiaozhi `AddTool`; ресурс-снимок состояния, как `GetLiveContext`; «user-only» и `confirm` для опасных действий; события hotplug;
- локальный быстрый контур: правила, как в WireClaw, и проверка до исполнения, как в ROSClaw;
- маршрутизация «mini или большая модель» по структурным признакам, как в PicoClaw `routing/features.go`;
- фоновые задачи по событиям и script gates вместо частого heartbeat.

**Эксперименты:**
- **E-A «Простой и фон»:** B1–B3 и свой агент без пользовательской нагрузки N часов: RSS(t), CPU, пробуждения, трафик, токены heartbeat. Сравнить с заявленными цифрами (раздел 5).
- **E-B «Рабочий день»:** скриптованный профиль на несколько часов в день — сообщения пользователя, события датчиков, команды приводам (MCP-сервер реального или симулированного оборудования). Метрики раздела «Постановка», несколько повторов, записанное окружение (`harness/envinfo.py`).
- **E-C «Абляции стоимости»:**
  - heartbeat: выкл., 30 мин, 30 мин + изоляция;
  - контекст оборудования: в промпте, через MCP-ресурс или через инструменты по запросу;
  - правила локально или LLM в цикле;
  - одна модель или маршрутизация между GPT «mini»-версиями.
- **Статистика и контроль:** парный bootstrap, McNemar, TOST и Холм из `harness/stats.py`; проверка трасс через `monitor/check_trace.py` (адаптировать контракт трассы под новый цикл). Это уже готовые наработки, которые стоит сохранить.
- **Сравнение с S15:** повторить на плате схему «OpenClaw против nanobot» на той же модели и одинаковых параметрах инференса, но с токенами и стоимостью. Это напрямую проверяет, переносятся ли их выводы на граничное железо.

---

## Приложение A. Список источников (кратко)

| # | Источник | Тип | Год | URL |
|---|---|---|---|---|
| S1 | OpenClaw (репозиторий, Wikipedia, docs: Pi, nodes, heartbeat, MCP) | проект/документация | 2025–2026 | https://github.com/openclaw/openclaw |
| S2 | NanoClaw | проект | 2026 | https://github.com/nanocoai/nanoclaw |
| S3 | PicoClaw (+ hardware-compatibility, issue #346, context.md, routing, hardware tools) | проект | 2026 | https://github.com/sipeed/picoclaw |
| S4 | nanobot (+ PR #610) | проект | 2026 | https://github.com/HKUDS/nanobot |
| S5 | ZeroClaw (+ hardware docs) | проект | 2026 | https://github.com/zeroclaw-labs/zeroclaw |
| S6 | NullClaw | проект | 2026 | https://github.com/nullclaw/nullclaw |
| S7 | IronClaw | проект | 2026 | https://github.com/nearai/ironclaw |
| S8 | Moltis | проект | 2026 | https://github.com/moltis-org/moltis |
| S9 | MimiClaw (+ ARCHITECTURE.md) | проект | 2026 | https://github.com/memovai/mimiclaw |
| S10 | zclaw | проект | 2026 | https://github.com/tnm/zclaw |
| S11 | WireClaw | проект | 2026 | https://github.com/M64GitHub/WireClaw |
| S12 | ESP-Claw; xiaozhi-esp32 (docs/mcp-usage.md) | проекты | 2024–2026 | https://github.com/espressif/esp-claw · https://github.com/78/xiaozhi-esp32 |
| S13 | Home Assistant MCP Server / MCP / LLM API | документация | 2025–2026 | https://www.home-assistant.io/integrations/mcp_server/ |
| S14 | MCP Specification 2026-07-28 | спецификация | 2026 | https://modelcontextprotocol.io/specification/latest |
| S15 | Salman et al., Resource Constraints and Performance in Agentic AI Systems | препринт | 2026 | https://arxiv.org/abs/2608.27886 |
| S16 | Cardenas et al., ROSClaw | препринт | 2026 | https://arxiv.org/abs/2603.26997 |
| S17 | Kalušev et al., Raspberry Pi YOLO + Slack-Ollama | препринт | 2026 | https://arxiv.org/abs/2604.13345 |
| S18 | Hua et al., GLM-5 Serving Parameter Tuning for OpenClaw | препринт | 2026 | https://arxiv.org/abs/2607.02518 |
| S19 | Dong et al., Clawdrain | препринт | 2026 | https://arxiv.org/abs/2603.00902 |
| S20 | She, AgentRM | препринт | 2026 | https://arxiv.org/abs/2603.13110 |
| S21 | Yan et al., AgentDecarbonizer | препринт | 2026 | https://arxiv.org/abs/2608.20566 |
| S22 | Oliveira, Throughput per Megabyte (Pi 5) | препринт | 2026 | https://arxiv.org/abs/2609.11932 |
| S23 | VisionClaw / PHILIA / HROS / VetClaw / OpenGo / ABot-Claw | препринты | 2026 | arXiv:2604.03486, 2607.11377, 2609.11225, 2607.26042, 2604.01708, 2604.10096 |
| S24 | Barbaste et al., Harness Engineering (11 систем) | препринт | 2026 | https://arxiv.org/abs/2609.00006 |
