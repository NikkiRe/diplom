# F. Практический стек ВКР: RouterAI и модели GPT mini, одноплатник, эмуляция на MacBook, ROS 2 / Gazebo

Тема (согласована 06.10.2026): «Разработка ИИ-агента для приложений граничного искусственного интеллекта».
Дата исследования: 2026-10-06. Каждый факт ниже взят из первоисточника (официальная страница, документация, API, репозиторий, arXiv/DOI).
Откуда взято число, указано рядом. Что проверено командами на этой машине, помечено **[локально]**.
Где первоисточник найти не удалось, стоит пометка **[не проверено]**.

Сырые данные и скрипт лежат в `research/new_topic/F_artifacts/`:
- `routerai_models_2026-10-06.json`: снимок каталога RouterAI (`GET https://routerai.ru/api/v1/models`, без ключа), 548 моделей;
- `routerai_endpoints_*_2026-10-06.json`: провайдеры моделей gpt-4o-mini, gpt-4.1-nano, gpt-5-mini, gpt-5-nano;
- `price_table.py` и `price_table_output_2026-10-06.txt`: таблица ₽/$ и иллюстративная модель стоимости «работы во времени»;
- `ros_apt_arm64_check_2026-10-06.txt`: наличие arm64-пакетов ROS 2 и Gazebo в apt-индексе packages.ros.org.

---

## 0. Коротко (рекомендация стека)

| Слой | Рекомендация | Почему (источник) |
|---|---|---|
| Рассуждение | Внешняя LLM через RouterAI (`https://routerai.ru/api/v1`, OpenAI-совместимый), модели `openai/gpt-4.1-nano`, `gpt-4o-mini`, `gpt-4.1-mini`, `gpt-5-nano`, `gpt-5-mini`, `gpt-5.4-nano/mini`, `gpt-5.6-luna`. Провайдер закрепить: `model@provider=openai&allow_fallbacks=false` | В каталоге есть все «mini/nano», tools и structured outputs поддерживаются [F1]. Цена = официальная цена OpenAI × 110,41 ₽/$ (кроме gpt-5.4-nano: 115,93) [F1, F2]. Без закрепления запрос может уйти к Azure, а у Azure для gpt-4o-mini параметр `tools` не заявлен [F1] |
| Оплата | Командный аккаунт RouterAI на факультет: договор, счёт, ЭДО; студенту выдаётся роль `member` с лимитом расходов на день, неделю или месяц | Team API и лимиты описаны в документации, юрлицо ООО «Интер» (Томск) [F1] |
| Одноплатник | **Raspberry Pi 5, 4 ГБ** (основной). 8 ГБ — если нужен локальный fallback на SLM ≤1,5B. Дешёвая нижняя граница для демонстрации: LicheeRV Nano (RISC-V, 256 МБ) | Ubuntu 26.04 LTS сертифицирована для Pi 5, ROS 2 Lyrical (arm64) — Tier 1 [F5, F17]. Локальные LLM на SBC реалистичны до ~1,5B при 5–15 ток/с [F9] |
| Эмуляция до покупки | Lima, `vmType: vz`, Ubuntu arm64, `--cpus 4 --memory 4` и сверху cgroup-квота CPU (`docker --cpus` или `cpu.max`), `tc netem` для сети. Квоту калибровать по реальной плате | Lima, Colima и Docker уже стоят **[локально]**. VZ задаёт только число vCPU и объём RAM, частоту не задаёт [F13]. Квота CFS режет долю процессорного времени [F12]. Формула «ядра × частота / частота хоста» (Fogify) не учитывает IPC [F15] |
| Симулятор | Отдельная Linux-ВМ на ноутбуке: Ubuntu 24.04 + ROS 2 Jazzy + Gazebo Harmonic + TurtleBot3/4, либо Ubuntu 26.04 + Lyrical + Jetty + TurtleBot3. Наружу открыт rosbridge (WebSocket, порт 9090) | Для arm64 все нужные deb-пакеты есть **[локально, apt-индекс]** [F17]. Kilted (EOL ноябрь 2026) и Ionic (EOL декабрь 2026) не брать [F17, F18] |
| Инструменты агента (MCP) | MCP-сервер оборудования на одноплатнике (stdio). Для робота — `ros-mcp-server`: через rosbridge, без установленного ROS. Для реального железа — свои инструменты GPIO/I²C/SPI/камера (как в PicoClaw) | ros-mcp-server: Python, зависимости только fastmcp, mcp и websocket-client [F20]. PicoClaw уже имеет инструменты i2c, spi, serial [F3] |
| Метрика | «Работа во времени», а не надёжность: ₽, токены (вход, кэш, выход, reasoning), число вызовов LLM, трафик, CPU-секунды, пиковый RSS, энергия (Вт·ч) за час и за сутки при заданном профиле нагрузки (часы в день), плюс p50/p95 задержки ответа пользователю | См. §5. В найденных источниках таких измерений для «агента с внешней LLM на SBC» нет (§7, открытые проблемы) |

---

## 1. Уточнение интерпретации заметок руководителя

Записи со встречи сопоставлены с найденными фактами:

1. **«Работа с одноплатником… Вопрос в одноплатнике».** Агент-оркестратор работает на SBC. Какую плату брать, пока не решено, поэтому эмуляция до покупки входит в работу (§2.4, §4).
2. **«Внешняя ЛЛМ, которая знает контекст оборудования. Контекст работы с пользователем и контекст оборудования».** Агент держит два контекста: пользовательский (диалог, предпочтения, история) и аппаратный (что подключено, состояние). Оба уходят во внешнюю LLM как часть промпта. Отсюда главный «расход во времени»: каждый вызов несёт этот контекст, и его стоимость зависит от размера контекста, кэширования префикса и частоты вызовов.
3. **«Локальные тупые ллм… нужна нормальная оперативка».** Это подтверждается измерениями: на Pi 4/5 и Orange Pi 5 Pro устойчиво работают модели до 1,5B, 5–15 ток/с; модели 3B+ выдают 1,5–5 ток/с только на Orange Pi 5 Pro (6 ГБ) [F9]. Рассуждение поэтому выносится во внешнюю LLM, а локально остаются оркестрация, фильтрация событий и, возможно, крошечный fallback.
4. **«РоутерАи — централизованно оплачивать доступ от факультета… Гпт Мини различных версий».** RouterAI — российский агрегатор (ООО «Интер»), принимает оплату по счёту и ЭДО, есть командные аккаунты с лимитами на участника [F1]. Все GPT mini/nano доступны [F1].
5. **«Сужаем — внешняя ллм на одноплатнике… мсп сервер, чтобы получать информацию о подключенном оборудовании. Инструменты — датчики и исполнительные устройства».** Архитектура: MCP-сервер(ы) оборудования, инструменты вида «прочитать датчик» и «подать команду». Сделать это можно как с реальными шинами (I²C/SPI/GPIO, камера), так и с симулятором через rosbridge.
6. **«Другая метрика, не надёжность — работа системы во времени. Несколько часов в день, считать это всё».** Оценка строится по профилю эксплуатации: система включена N часов в день, пользователь пишет λ_u раз в час, датчики дают λ_s событий в час. Считаются накопленные ресурсы и стоимость (§5).
7. **«НаноКлав, ПикоКлов — как примеры».** NanoClaw и PicoClaw — открытые лёгкие персональные агенты из семейства OpenClaw [F3, F4]. PicoClaw ориентирован именно на дешёвые SBC (RISC-V/ARM), умеет MCP и шины I²C/SPI. Ближе к теме он.
8. **«ROS, Gazebo».** Это источник «гипотетических» датчиков и приводов: камера, лидар, одометрия, `cmd_vel` у TurtleBot [F21].

---

## 2. Источники

### 2.1. Доступ к LLM: RouterAI и официальные цены OpenAI

**[F1] RouterAI — документация, публичный каталог моделей API, оферта.** ООО «Интер» (ОГРН 1147017006950, ИНН 7017351722, Томск), 2026.
URL: https://routerai.ru, https://routerai.ru/docs/guides, https://routerai.ru/llms-full.txt, https://routerai.ru/api/v1/models, https://routerai.ru/terms (редакция от 11.07.2026). Проверено 2026-10-06.
- **Что это.** Российский агрегатор LLM API: единый ключ к моделям OpenAI, Anthropic, Google, DeepSeek, Qwen, Yandex и др. Оплата в рублях картой или СБП; для юрлиц и ИП — договор, счёт, закрывающие документы, ЭДО (главная страница). Работает без VPN. На главной также заявлено, что инфраструктура обработки запросов RouterAI находится в дата-центрах РФ.
- **API.** Base URL `https://routerai.ru/api/v1`, формат OpenAI-совместимый (`/chat/completions`). Поддерживаются `/v1/responses` и Anthropic-совместимый `/v1/messages`. Есть параметры `tools`, `tool_choice` (none/auto/required/конкретная функция), `parallel_tool_calls` (по умолчанию true), `response_format`, `structured_outputs`, `seed`, `verbosity`, `reasoning`. Tool calling «следует формату OpenAI» и для не-OpenAI провайдеров конвертируется.
- **Каталог (снимок 2026-10-06).** 548 моделей, у 306 заявлен `tools`. Цены лежат в поле `pricing` в рублях за 1 токен. Для моделей OpenAI есть отдельная цена кэшированного входа `input_cache_read`, а для gpt-5.6-* ещё и `input_cache_write`. Фактическая стоимость запроса возвращается в `usage.cost`; так написано в разделах про изображения и речь, для чата **[не проверено]**.
- **Модели GPT mini/nano в каталоге**: gpt-4o-mini (контекст 128K), gpt-4.1-nano и gpt-4.1-mini (1M), gpt-5-nano и gpt-5-mini (400K, у провайдера OpenAI `max_prompt_tokens` = 272 000), gpt-5.4-nano и gpt-5.4-mini (400K), gpt-5.6-luna (1,05M), o4-mini и o3-mini (200K), gpt-5.1-codex-mini. Цены в §3.1.
- **Маршрутизация.** По умолчанию балансировка между провайдерами: сначала стабильность (не было сбоев последние 30 с), затем низкая цена, остальные провайдеры — fallback. Управлять этим можно объектом `provider` (`order`, `only`, `ignore`, `allow_fallbacks`, `country`) или через `@`-синтаксис в имени модели (`<model>@provider=openai&allow_fallbacks=false`). Список провайдеров модели с ценами и `supported_parameters` отдаёт `GET /api/v1/models/{author}/{model}/endpoints` без авторизации. **Важно:** у gpt-4o-mini два провайдера, OpenAI и Azure. У Azure в `supported_parameters` **нет `tools`** (снимок 2026-10-06). Значит, без закрепления провайдера вызов инструментов может не сработать или сработать иначе.
- **Режимы обработки (`service_tier`).** `flex` — «обычно −50 %» и медленнее; если мощностей нет, возвращается ошибка без списания. `priority` — быстрее и дороже. Фактический режим приходит в поле ответа `service_tier`. Для gpt-5-mini и gpt-5-nano есть endpoint `openai/flex` ровно за половину цены (снимок endpoints).
- **Команды (Team API).** Командный аккаунт, роли `admin`/`member`. Обычный участник не видит баланс команды. Лимит расходов на участника задаётся на day/week/month, например «1000 ₽ в неделю». Управление — мастер-ключом администратора. Это прямой механизм для фразы «централизованно оплачивать доступ от факультета».
- **Лимиты.** Для MCP-сервера RouterAI — 120 запросов в минуту на ключ, при превышении ответ 429. Для видео — 3 параллельные генерации. Rate limit для chat completions в документации **не найден**.
- **Данные в РФ.** `provider.country: "ru"` направляет запрос к провайдерам в России, если они есть у модели. У всех GPT mini/nano провайдеры только с `countries: ["us"]` (OpenAI, Azure) по снимку endpoints.
- **Польза для ВКР.** Единственная точка доступа к внешним LLM, годная для оплаты организацией. Цены и параметры машиночитаемы, поэтому стоимость в экспериментах можно считать автоматически, а цены — фиксировать снимком на дату. Методический риск: агрегатор добавляет неопределённость провайдера. Провайдер нужно закреплять и журналировать (`service_tier`, `model`, `id` ответа).

**[F2] OpenAI API — Pricing и Prompt caching (официальная документация).** OpenAI, 2026.
URL: https://developers.openai.com/api/docs/pricing ; https://developers.openai.com/api/docs/guides/prompt-caching (с platform.openai.com/docs/pricing стоит 301-редирект). Проверено 2026-10-06.
- **Цены Standard, $ за 1M токенов (вход / кэшированный вход / выход):** gpt-4o-mini 0,15 / 0,075 / 0,60; gpt-4.1-nano 0,10 / 0,025 / 0,40; gpt-4.1-mini 0,40 / 0,10 / 1,60; gpt-5-nano 0,05 / 0,005 / 0,40; gpt-5-mini 0,25 / 0,025 / 2,00; gpt-5.4-nano 0,20 / 0,02 / 1,25; gpt-5.4-mini 0,75 / 0,075 / 4,50; gpt-5.6-luna 0,20 / 0,02 / 1,20; o4-mini 1,10 / 0,275 / 4,40. Batch и Flex — примерно −50 %.
- **Кэширование префикса.** Работает автоматически, начиная с минимальной длины префикса: 1024 токена для GPT-5.6 и новее, для более ранних моделей порог зависит от настроек. У ранних моделей `cached_tokens` округляется вниз до кратного 128. Время жизни кэша: in-memory — 5–10 мин неактивности (до часа) или расширенное `24h`; для GPT-5.6+ — 30 мин после последней записи или использования. Ключ `prompt_cache_key` нужен для стабильной маршрутизации к кэшу. Кэшированный вход стоит 0,1× обычного для новых моделей и 0,5× для gpt-4o-mini (по таблице цен).
- **Польза для ВКР.** Эталон для сравнения с ценами RouterAI (§3.1). Кэширование префикса — главный рычаг снижения стоимости «во времени», если стабильный системный промпт и каталог MCP-инструментов стоят в начале промпта. Через агрегатор этот рычаг нужно проверить экспериментально (§7).

### 2.2. Примеры-аналоги от руководителя: PicoClaw и NanoClaw

**[F3] PicoClaw.** Sipeed, 2026. https://github.com/sipeed/picoclaw — MIT, Go, ★30 012 (GitHub API, 2026-10-06), создан 2026-02-04, последний push 2026-09-24. Версии v0.2.8–v0.2.9 вышли в мае 2026.
- **Что это.** Сверхлёгкий персональный ИИ-ассистент, переписанный на Go по мотивам NanoBot. Рассуждение выполняет внешняя LLM: заявлено 30+ провайдеров, включая OpenRouter, Azure, LiteLLM и локальный Ollama. Каналы — Telegram, Discord, Matrix, WeChat и др.
- **Числа из README.** «$10 hardware, <10 MB RAM», с оговоркой: последние сборки занимают 10–20 МБ. Загрузка <1 с на одноядерном 0,6 ГГц. Сравнение по RAM: OpenClaw >1 ГБ, NanoBot >100 МБ. Сравнение по старту на ядре 0,8 ГГц: OpenClaw >500 с, NanoBot >30 с. Минимальные требования: 10 МБ свободной RAM (рекомендуется 32 МБ+), 20 МБ на диске.
- **Железо (Hardware Compatibility List).** RISC-V SG2002 (C906 @ 1 ГГц, 256 МБ DDR3 на кристалле: LicheeRV-Nano за $9,9, NanoKVM, MaixCAM), K230, SpacemiT K1/K3. ARM: Pi Zero…Pi 5, Orange Pi Zero 3. Также MIPS-роутеры (OpenWrt, нужно >32 МБ RAM) и Android через Termux. С мая 2026 продаётся набор LicheeRV-Claw.
- **MCP и оборудование.** Нативный MCP (stdio, SSE, HTTP), CLI `picoclaw mcp add/list/test`. В `pkg/tools/hardware/` есть встроенные инструменты **`i2c`** (detect/scan/read/write; параметры bus, 7-битный address, register, data, length; только Linux), **`spi`** и **`serial`**.
- **Польза для ВКР.** Ближайший аналог постановки: агент на дешёвом SBC, внешняя LLM, MCP, инструменты-датчики. Для сравнения есть заявленные числа (RAM, время старта). Их стоит **перепроверить измерением**: в README это маркетинговые цифры, методика не описана. Каркас реализации на Go/Rust оправдан для ресурса «RAM во времени». На машине **нет Go** **[локально]**, поэтому сборка форка PicoClaw потребует `brew install go` (отложено вместе с другими загрузками).

**[F4] NanoClaw.** nanocoai, 2026. https://github.com/nanocoai/nanoclaw — MIT, TypeScript, ★30 882, создан 2026-01-31, push 2026-10-06.
- **Что это.** Лёгкая альтернатива OpenClaw с изоляцией агентов в Linux-контейнерах (Docker). Хост-процесс на Node маршрутизирует сообщения через SQLite-очереди `inbound.db`/`outbound.db` в контейнер агента (Bun + Claude Agent SDK). Другие провайдеры подключаются навыками: Codex, OpenCode→OpenRouter, Ollama. Для доступа к API используется «credential gateway», ключи в контейнер не попадают.
- **Требования.** macOS/Linux, Docker, Node, pnpm, Claude Code. Мотивация из README: у OpenClaw «почти полмиллиона строк кода», 53 конфигурационных файла и 70+ зависимостей.
- **Польза для ВКР.** Противоположная точка дизайна: безопасность через изоляцию при заметной цене в ресурсах (Docker и Node на SBC). Пара NanoClaw/PicoClaw даёт естественную ось сравнения «изоляция ↔ ресурсы на одноплатнике» для обзорной главы. Модель исполнения NanoClaw (хост-маршрутизатор + опрос очередей) полезна как пример архитектуры событий.

### 2.3. Одноплатники и измерения LLM на них

**[F5] Raspberry Pi 5: страница продукта, анонсы цен, документация по питанию.** Raspberry Pi Ltd, 2023–2026.
URL: https://www.raspberrypi.com/products/raspberry-pi-5/ ; новости 01.12.2025 https://www.raspberrypi.com/news/1gb-raspberry-pi-5-now-available-at-45-and-memory-driven-price-rises/ , 02.02.2026 https://www.raspberrypi.com/news/more-memory-driven-price-rises/ , 01.04.2026 https://www.raspberrypi.com/news/a-new-3gb-raspberry-pi-4-for-83-75-and-more-memory-driven-price-increases/ ; документация (исходник) https://github.com/raspberrypi/documentation — `computers/raspberry-pi/power-supplies.adoc`, `config_txt/overclocking.adoc`; Ubuntu для Pi https://ubuntu.com/download/raspberry-pi .
- **Характеристики.** BCM2712, 4× Cortex-A76 @ 2,4 ГГц, L2 512 КБ на ядро, L3 2 МБ. RAM 1/2/4/8/16 ГБ LPDDR4X-4267. VideoCore VII. PCIe 2.0 x1. 2× 4-lane MIPI для камеры или дисплея. 40-pin GPIO. Питание 5 В / 5 А по USB-C PD (рекомендован блок 27 Вт). Gigabit Ethernet, Wi-Fi 802.11ac, BT 5.0.
- **Цены в USD (официальные, растут из-за LPDDR4).** Декабрь 2025: 1 ГБ $45, 2 ГБ $55, 4 ГБ $70, 8 ГБ $95, 16 ГБ $145. Февраль 2026: +$10 / +$15 / +$30 / +$60 за 2/4/8/16 ГБ. Апрель 2026: +$25 (4 ГБ), +$50 (8 ГБ), +$100 (16 ГБ). Отсюда цены после 01.04.2026: **2 ГБ $65, 4 ГБ $110, 8 ГБ $175, 16 ГБ $305**. Это расчёт по объявленным приращениям; цена $305 за 16 ГБ совпадает со страницей продукта, цены 1 ГБ и 2 ГБ в апреле не менялись. На 2026-10-06 страница продукта показывает $305 за 16 ГБ, то есть действует апрельский уровень. Вторичные сайты (поисковая выдача) называют другие «текущие» цены; им не доверяю.
- **Цены в РФ (2026-10-06).** Амперка: Pi 5 8 ГБ — 32 990 ₽ (в наличии >10 шт.), Pi 4 4 ГБ — 19 990 ₽, Pi 4 8 ГБ — 21 990 ₽ (страница amperka.ru/product/raspberry-pi-5-8gb). По поисковой выдаче [не проверено на складе]: ChipDip 41 130 ₽ и robotclass 29 000 ₽ за Pi 5 8 ГБ.
- **Питание (официальная таблица).** Типичный ток платы без периферии под активной нагрузкой: **Pi 5 — 800 мА** (около 4 Вт при 5,1 В), **Pi 4 — 600 мА**. Измерения Pi 4 (июнь 2019): простой 0,6 А, стресс в среднем 1,2 А, максимум 1,25 А, то есть примерно 3,1 и 6,1 Вт при 5,1 В. Выключенный Pi 5 по умолчанию потребляет 1–1,4 Вт; с `POWER_OFF_ON_HALT=1` — около 0,01 Вт. У PMIC есть АЦП, которые читает `vcgencmd pmic_read_adc`. Ток USB и всего, что висит на 5 В напрямую, они не видят, и сумма не равна мощности блока питания. Частота по умолчанию: `arm_freq` 2400 МГц у Pi 5, 1500 МГц у Pi 4 (1800 при `arm_boost`).
- **ОС.** Ubuntu 26.04.1 LTS Desktop и Server для Pi 5 и Pi 4B, статус «certified».
- **Польза для ВКР.** Основной кандидат. Есть референсные значения мощности для модели энергии до покупки внешнего измерителя. Есть встроенный, но неполный канал телеметрии питания (PMIC ADC) — тема для валидации, как в G6. ОС совпадает с Tier 1 платформой ROS 2 Lyrical.

**[F6] NVIDIA Jetson Orin Nano Super Developer Kit.** NVIDIA, 2024–2026. https://www.nvidia.com/en-us/autonomous-machines/embedded-systems/jetson-orin/nano-super-developer-kit/ ; повышение цен: CNX Software, 22.07.2026, https://www.cnx-software.com/2026/07/22/nvidia-increases-the-price-of-jetson-modules-and-devkits-by-up-to-101/
- **Характеристики.** 67 INT8 TOPS. GPU Ampere: 1024 CUDA-ядра, 32 тензорных ядра. CPU 6× Cortex-A78AE (L2 1,5 МБ + L3 4 МБ). 8 ГБ LPDDR5 128-бит, 102 ГБ/с. Мощность 7–25 Вт. microSD и NVMe.
- **Цена.** $249 при запуске (декабрь 2024) → **$399** с июля 2026 (+60 %, CNX). В РФ по поисковой выдаче [не проверено]: 76 990 ₽ (onpad.ru), 96 891 ₽ с НДС (empc.ru).
- **Польза для ВКР.** Нужен, только если локально обрабатывается зрение (детектор на GPU). Для постановки «рассуждение во внешней LLM» он избыточен по цене и по мощности (7–25 Вт против ~4 Вт у Pi 5). Годится как верхняя граница класса устройств в обзоре.

**[F7] Orange Pi 5 (RK3588S) и аналоги RK3588 (Radxa ROCK 5C).** Shenzhen Xunlong Software / Radxa. http://www.orangepi.org/html/hardWare/computerAndMicrocontrollers/details/Orange-Pi-5.html ; https://radxa.com/products/rock5/5c/
- **Orange Pi 5.** RK3588S, 8 нм: 4× Cortex-A76 @ 2,4 ГГц + 4× Cortex-A55 @ 1,8 ГГц. Mali-G610. NPU 6 TOPS (INT4/INT8/INT16). RAM 4/8/16 ГБ LPDDR4/4X. Питание USB-C 5 В @ 4 А. MIPI CSI 4-lane, гигабитный Ethernet, масса 46 г.
- **ROCK 5C.** Четыре или два ядра A76 на выбор, RAM 4/8/16/32 ГБ (LPDDR4X в v1.1, LPDDR5 в v2.1), MIPI CSI. Модель SoC на странице не указана [не проверено].
- **Цена РФ** по поисковой выдаче [не проверено]: Orange Pi 5 16 ГБ — 29 490 ₽ (onpad.ru).
- **Польза для ВКР.** Больше RAM и ядер на рубль, чем у Pi 5, плюс NPU. Минусы: ROS 2 на вендорских образах — только Tier 3 (Debian), сообщество меньше. Разумная альтернатива, если нужен локальный SLM-fallback: в [F9] Orange Pi 5 Pro — единственная плата, где шли модели 3B+.

**[F8] Sipeed LicheeRV Nano.** Sipeed, 2024. https://wiki.sipeed.com/hardware/en/lichee/RV_Nano/1_intro.html
- **Характеристики.** SOPHGO SG2002: основное ядро RISC-V C906 @ 1 ГГц или ARM A53 (выбирается), дополнительное C906 @ 700 МГц. 256 МБ DDR3 на кристалле. NPU 1 TOPS INT8. MIPI CSI 4-lane. Ethernet 100 Мбит или Wi-Fi 6 + BLE 5.4. Размер 22,86 × 35,56 мм. Цена $9,9 на AliExpress (из README PicoClaw [F3]; на вики цены нет).
- **Польза для ВКР.** Платформа «нижней границы»: на ней демонстрируют PicoClaw. Если агент укладывается в ~256 МБ RAM вместе с ОС, это сильный аргумент. Агент на Python здесь проблематичен, нужен Go/Rust/C.

**[F9] Nguyen T., Nguyen T. «An Evaluation of LLMs Inference on Popular Single-board Computers».** arXiv:2511.07425, 2025 (препринт, 9 с.). https://arxiv.org/abs/2511.07425
- **Что сделано.** 25 квантованных (q4_k_m) открытых LLM от 135M до 7B на Raspberry Pi 4 (4 ГБ), Raspberry Pi 5 (8 ГБ) и Orange Pi 5 Pro (6 ГБ LPDDR5). Рантаймы — Ollama и Llamafile. Метрики: пропускная способность, память, мощность.
- **Результаты.** Модели ~1B: на Pi 4 <5 ток/с, на Pi 5 и Orange Pi 5 Pro 5–15 ток/с. Модели ~1,5B: 5–15 ток/с на Pi 5 и Orange Pi 5 Pro. Модели 3B+: 1,5–5 ток/с, только на Orange Pi 5 Pro. Пиковая мощность при инференсе в Ollama: Pi 4 — 8 Вт, Pi 5 — 10 Вт, Orange Pi 5 Pro — 14 Вт (9 Вт на 4 ядрах). Llamafile даёт до 4× больше пропускной способности и на 30–40 % меньше мощности, чем Ollama. Вывод авторов: SBC «надёжно» тянут модели до 1,5B.
- **Польза для ВКР.** Количественно обосновывает тезис руководителя «локальные LLM слабые, нужна RAM». Это аргумент за внешнюю LLM. Числа годятся для сравнения в экспериментах: стоимость в ₽ и задержка внешнего вызова против энергии и задержки локального 1B-fallback.

**[F10] Islam M. R., Deng B., Dhar N., Nguyen T. N., He S., Shi Y., Suo K. «Characterizing and Understanding Energy Footprint and Efficiency of Small Language Model on Edges».** IEEE MASS 2025 (представлена, публикация ожидается), arXiv:2511.11624. https://arxiv.org/abs/2511.11624
- **Что сделано.** Llama 3.2, Phi-3 Mini, TinyLlama, Gemma 2 на Raspberry Pi 5 (4 ГБ, 17 ГБ/с), Jetson Nano (4 ГБ, 25,6 ГБ/с) и Jetson Orin Nano (8 ГБ, 102 ГБ/с), на CPU и GPU. Мощность снималась внешним Otii Ace Pro с частотой 100 Гц.
- **Результаты.** Лучшее отношение энергии к производительности у Orin Nano с GPU. Пример: Llama 3.2 на MMLU — 1,71 Вт·ч при задержке 0,57 с на Orin Nano (GPU). Phi-3 Mini на Pi 5 — 65,64 Вт·ч при 34,36 с (Table III). Ключевые факторы: GPU, пропускная способность памяти и архитектура модели.
- **Польза для ВКР.** Готовая методика измерения энергии на плате (внешний измеритель, 100 Гц, E = ∫P dt). Даёт порядок величин, почему локальный инференс на Pi 5 энергетически дорог по сравнению с HTTP-вызовом внешней модели, при котором плата почти простаивает.

### 2.4. Эмуляция одноплатника на MacBook M4 Pro

**[F11] Lima (и Colima поверх Lima): типы ВМ, ресурсы, сеть.** Проект Lima (CNCF), документация https://lima-vm.io/docs/config/vmtype/ и шаблон `/opt/homebrew/share/lima/templates/default.yaml` (Lima 2.1.1) **[локально]**.
- **Типы ВМ.** На macOS ≥13.5 по умолчанию VZ (Apple Virtualization.framework). QEMU нужен для эмуляции чужих архитектур. Тип ВМ задаётся только при создании. По умолчанию `cpus = min(4, ядра хоста)`, `memory = min(4 GiB, половина RAM)`. Вложенная виртуализация есть только на M3 и новее с `vz` или `krunkit`. Rosetta внутри ВМ — только с `vz`.
- **CLI.** `limactl create --cpus=2 --memory=2`. Поддерживаются `--arch` x86_64/aarch64/riscv64/armv7l (не-host архитектуры идут через QEMU). Colima 0.10.1: `colima start --cpu 4 --memory 8`, `--vm-type qemu|vz|krunkit` (по умолчанию vz), по умолчанию 2 CPU и 2 GiB **[локально]**.
- **Шаблоны** **[локально]**: `ubuntu-24.04`, `ubuntu-25.10`, `experimental/ubuntu-26.04`, `debian-12`, `debian-13`, `docker`, `k3s` и др.
- **Сеть.** `portForwards` с `hostIP: "0.0.0.0"` выставляет порт гостя на внешний интерфейс Mac. Так одноплатник в LAN достучится до rosbridge в ВМ. `vzNAT` даёт IP, доступный с хоста, но не из других гостей. Для мостовой сети нужен `socket_vmnet`.
- **Польза для ВКР.** Главный инструмент эмуляции до покупки. Ubuntu arm64 в VZ-ВМ — та же ISA (AArch64) и та же ОС, что на Pi 5 с Ubuntu 26.04. Совпадают бинарники, пакеты и cgroups. **Не совпадают** микроархитектура, частота, кэш и пропускная способность памяти (§4).

**[F12] Docker — Resource constraints (официальная документация).** Docker Inc. https://docs.docker.com/engine/containers/resource_constraints/
- **Что задаётся.** `--cpus` — доля CPU (например, 1,5 из 2). `--cpu-period` (по умолчанию 100 000 мкс) и `--cpu-quota` — квота планировщика CFS через cgroup. `--cpuset-cpus` — привязка к ядрам. `--memory` (минимум 6 МБ) и `--memory-swap`.
- **Польза для ВКР.** Внутри Linux-ВМ контейнер агента ограничивается по RAM (жёсткий OOM-предел, как 2/4 ГБ платы) и по доле CPU. Ограничение по CPU — это квота времени, а не частота: задержка одиночной короткой операции у ядра M4 не растягивается так, как на A76. Отсюда требование калибровки (§4).

**[F13] Apple Virtualization framework — `VZVirtualMachineConfiguration`.** Apple, документация (https://developer.apple.com/documentation/virtualization/vzvirtualmachineconfiguration) и заголовок `Virtualization.framework/Headers/VZVirtualMachineConfiguration.h` из MacOSX26.sdk **[локально]**.
- **Что задаётся.** `CPUCount` (в пределах `minimumAllowedCPUCount`..`maximumAllowedCPUCount`) и `memorySize`. Параметров частоты или типа ядра нет.
- **Польза для ВКР.** Формальное основание тезиса «VZ-ВМ эмулирует число ядер и объём RAM, но не скорость ядра». Отдельно стоит измерить, на какие ядра M4 Pro (8 производительных + 4 энергоэффективных) macOS ставит vCPU и насколько от этого скачет время. Документально это не описано [не проверено].

**[F14] QEMU — Raspberry Pi boards (`raspi*`).** QEMU Project. https://www.qemu.org/docs/master/system/arm/raspi.html
- **Что эмулируется.** Машины `raspi0/1ap/2b/3ap/3b/4b`; `raspi4b` — 4× Cortex-A72 и 2 ГБ RAM. Эмулированы GPIO, SPI, I²C, UART, USB-хост, SD, термодатчик, mailbox. Не реализованы PWM, PCIe Root Port и Ethernet GENET (у raspi4b). **Машины для Raspberry Pi 5 нет.** `qemu-system-aarch64` на машине **не установлен** **[локально]**.
- **Польза для ВКР.** Функциональная эмуляция периферии (I²C/SPI) для отладки драйверного слоя без платы возможна, но только для Pi 4 и без сети и PCIe. Скорость TCG-эмуляции не связана со скоростью реальной платы, поэтому для метрик ресурсов вариант непригоден.

**[F15] Symeonides M., Georgiou Z., Trihinas D., Pallis G., Dikaiakos M. D. «Fogify: A Fog Computing Emulation Framework».** IEEE/ACM SEC 2020. DOI 10.1109/SEC50012.2020.00011. Код: https://github.com/UCY-LINC-LAB/fogify (Apache-2.0, ★32).
- **Что это.** Эмулятор fog/edge-топологий на Docker Swarm. Узлы описываются через `capabilities.processor.{cores, clock_speed}` и `memory`, сеть эмулируется, метрики собираются.
- **Ключевой факт из кода.** В `fogify/connectors/materialized_connectors/DockerBasedConnectors.py:135` CPU-лимит считается как `cores × clock_speed / частота_хоста` и передаётся в NanoCPUs, то есть в квоту CFS. Пропорция частот здесь подменяет производительность.
- **Польза для ВКР.** Стандартный в литературе приём эмуляции edge-узла. Его нужно процитировать и **критиковать**: при разной микроархитектуре (Apple M4 и Cortex-A76) отношение частот не равно отношению производительности. Нужна калибровка по эталонной нагрузке, а не по ГГц. Это аккуратный методический вклад раздела «стенд».

**[F16] Hasenburg J., Grambow M., Bermbach D. «MockFog 2.0: Automated Execution of Fog Application Experiments in the Cloud».** IEEE Transactions on Cloud Computing, 2021. DOI 10.1109/TCC.2021.3074988, arXiv:2009.10579.
- **Что это.** Эмулированный стенд fog-инфраструктуры в облаке: задаются характеристики узлов и сети, эксперименты идут по заранее заданному расписанию изменений инфраструктуры и нагрузки. Авторы показывают хорошую воспроизводимость даже в публичном облаке.
- **Польза для ВКР.** Методическая опора для экспериментов «во времени»: сценарий из фаз (ночь, день, пиковая активность, деградация сети) задаётся заранее и прогоняется автоматически и повторяемо.

### 2.5. ROS 2, Gazebo, связь агента с симулятором

**[F17] REP-2000 (ROS 2 Releases and Target Platforms), анонс ROS 2 Lyrical Luth и проверка apt-индекса.** Open Robotics.
https://github.com/ros-infrastructure/rep/blob/master/rep-2000.rst ; https://discourse.openrobotics.org/t/ros-2-lyrical-luth-released/55021 ; http://packages.ros.org/ros2/ubuntu/dists/{noble,resolute}/main/binary-arm64/Packages.gz **[локально, 2026-10-06]**.
- **Jazzy Jalisco** (май 2024 – май 2029): Tier 1 — Ubuntu 24.04 amd64 **и arm64** (deb, архив, исходники) и Windows 10. macOS — только Tier 3, amd64, сборка из исходников. Пара — Gazebo Harmonic. RMW по умолчанию — Fast DDS.
- **Kilted Kaiju** (май 2025 – **ноябрь 2026**) — истекает во время ВКР, не брать.
- **Lyrical Luth** (вышел 22.05.2026, LTS до мая 2031): Tier 1 — Ubuntu 26.04 amd64/arm64 и Windows 11; Tier 3 — Ubuntu 24.04, macOS, Debian Trixie. Пара — Gazebo Jetty (`ros-lyrical-ros-gz`). Заявлено: Events Executor тратит на 10–15 % меньше CPU, чем Single/MultiThreaded, AsyncNode в rclpy — «значительно» меньше.
- **Наличие arm64-пакетов (проверено по индексу).** Ubuntu noble/arm64 (8992 пакета): `ros-jazzy-rosbridge-server/-suite`, `ros-jazzy-ros-gz-sim/-bridge`, `ros-jazzy-gz-sim-vendor`, `ros-jazzy-turtlebot3-gazebo/-simulations`, `ros-jazzy-turtlebot4-gz-bringup`, `ros-jazzy-nav2-bringup`. Ubuntu resolute/arm64 (2759 пакетов): те же для `lyrical`, кроме `turtlebot4-gz-bringup` (его нет).
- **Польза для ВКР.** Обоснование выбора дистрибутива. Вариант A: Jazzy + Harmonic + Ubuntu 24.04, максимум готовых миров, включая TurtleBot4. Вариант B: Lyrical + Jetty + Ubuntu 26.04, та же ОС, что сертифицирована на Pi 5. Оба устанавливаются бинарными пакетами в arm64-ВМ на Mac.

**[F18] Gazebo — документация: релизы, платформы, установка на macOS, troubleshooting.** Open Robotics. https://github.com/gazebosim/docs (`common/releases.md`, `common/ros_installation.md`, `jetty/install.md`, `jetty/install_osx.md`, `jetty/troubleshooting.md`, `harmonic/install.md`).
- **Релизы.** Harmonic (сентябрь 2023 – май 2029, LTS), Ionic (сентябрь 2024 – **декабрь 2026**), Jetty (сентябрь 2025 – май 2031, LTS), следующий — «M» (март 2027). Пары с ROS: Jazzy ↔ Harmonic, Kilted ↔ Ionic, Lyrical/Rolling ↔ Jetty.
- **Платформы.** Официально Jetty поддерживается только на Ubuntu Noble amd64, Harmonic — на Jammy и Noble amd64. arm, Windows и macOS — **best-effort**. На macOS Jetty ставится через `brew tap osrf/simulation; brew install gz-jetty`; в документации названы только Ventura и Sonoma, macOS 27 не упомянута [не проверено]. Для серверов без GUI есть пакет «server-only» (Ubuntu).
- **ВМ и рендеринг.** Если OpenGL/Ogre2 не работает «внутри виртуальной машины», документация предлагает `LIBGL_DRI3_DISABLE=1` или программный рендеринг `LIBGL_ALWAYS_SOFTWARE=1`. Камеры в Gazebo рендерятся (ogre2), поэтому в ВМ без GPU-ускорения кадры камеры считаются на CPU (Mesa) и FPS будет низким.
- **Польза для ВКР.** Ограничения стенда (камера в ВМ) и выбор LTS-пары. Отсюда же требование: агент запрашивает кадр по событию, а не держит видеопоток.

**[F19] rosbridge v2.1.0 Protocol Specification и rosbridge_suite.** RobotWebTools. https://github.com/RobotWebTools/rosbridge_suite (BSD-3-Clause, ★1249, ветка `ros2` для Jazzy и новее), спецификация `ROSBRIDGE_PROTOCOL.md`; клиент roslibpy https://github.com/RobotWebTools/roslibpy (MIT, ★335).
- **Протокол.** JSON-конверт `{"op": ...}` поверх WebSocket или TCP. Операции: `advertise/publish/subscribe/unsubscribe` для топиков, `call_service/service_response`, а для действий `send_action_goal/cancel_action_goal/action_feedback/action_result`. Поддерживается QoS-профиль.
- **Управление трафиком.** У `subscribe` есть параметры `throttle_rate` (минимальный интервал между сообщениями, мс), `queue_length`, `fragment_size` и `compression` (`none`, `png`, `cbor`, `cbor-raw`). Массивы `uint8[]` кодируются в base64, что «до 60 %» компактнее списка чисел. Ограничение: одна подписка на топик на всех клиентов, QoS берётся от первой.
- **Польза для ВКР.** Агенту на одноплатнике не нужен установленный ROS: хватает WebSocket-клиента (roslibpy или свой). Симулятор работает на ноутбуке, связь идёт по LAN через один TCP-порт, без настройки DDS-discovery между сетями и через NAT ВМ (это архитектурное соображение, а не утверждение источника). `throttle_rate` и `compression` — прямые ручки для метрики «трафик во времени».

**[F20] ros-mcp-server.** robotmcp, 2025–2026. https://github.com/robotmcp/ros-mcp-server — Apache-2.0, Python ≥3.10, ★1486, версия 3.1.2 (pyproject), push 2026-10-05.
- **Что это.** MCP-сервер, связывающий LLM-клиенты с роботами через rosbridge. Код робота не меняется, достаточно запустить узел rosbridge. Работает с ROS 1 и ROS 2 (Jazzy, Humble и др.).
- **Инструменты (по исходникам `ros_mcp/tools/*.py`).** `connect_to_robot`, `ping_robots`, `get_topics`, `get_topic_type`, `get_topic_details`, `get_message_details`, `subscribe_once`, `subscribe_for_duration`, `publish_once`, `publish_for_durations`, `get_services`, `call_service`, `get_actions`, `get_action_status`, `cancel_action_goal`, `get_nodes`, `get_parameter(s)`, `set_parameter`, `view_saved_image`, `detect_ros_version`, `get_verified_robot_spec`. Зависимости — только `fastmcp`, `mcp[cli]`, `websocket-client`. Есть пример `examples/9_turtlebot3` с портом rosbridge 9090.
- **Польза для ВКР.** Готовый «MCP-сервер оборудования» для симулятора, который запускается на одноплатнике без ROS. Около 30 функций-инструментов (по определениям в исходниках; какие именно регистрируются, проверить при запуске) — реалистичный каталог для измерения стоимости промпта: каталог инструментов в каждом вызове. С этим связан G3 (сжатие и отбор каталога). Годится как baseline, с которым сравнивается собственный компактный MCP-сервер с агрегированными инструментами.

**[F21] TurtleBot3 Simulations (ветка `jazzy`).** ROBOTIS, https://github.com/ROBOTIS-GIT/turtlebot3_simulations — Apache-2.0, ★531, ветки humble/jazzy/main/noetic. TurtleBot4 Simulator (ветки humble/jazzy): https://github.com/turtlebot/turtlebot4_simulator (Apache-2.0, ★111).
- **Мост ROS↔Gazebo для Waffle Pi** (`turtlebot3_gazebo/params/turtlebot3_waffle_pi_bridge.yaml`). Из Gazebo в ROS: `clock`, `joint_states`, `odom` (nav_msgs/Odometry), `tf`, `imu`, `scan` (sensor_msgs/LaserScan), `camera/camera_info`. Из ROS в Gazebo: `cmd_vel` типа **geometry_msgs/TwistStamped**. Изображение камеры идёт через `ros_gz_image` (зависимость пакета). Пакет `turtlebot3_gazebo` 2.3.7 зависит от `ros_gz_bridge`, `ros_gz_image`, `ros_gz_sim`.
- **Польза для ВКР.** Готовый набор «датчиков и приводов»: камера, лидар, IMU, одометрия, скорость. Из него получаются MCP-инструменты агента. Важная деталь для инструментов движения: в Jazzy `cmd_vel` — TwistStamped, а не Twist.

**[F22] Fischer T., Vollprecht W., Traversaro S., Yen S., Herrero C., Milford M. «A RoboStack Tutorial: Using the Robot Operating System Alongside the Conda and Jupyter Data Science Ecosystems».** IEEE Robotics & Automation Magazine, 2021. DOI 10.1109/MRA.2021.3128367, arXiv:2104.12910. Репозитории https://github.com/RoboStack/ros-jazzy (MIT; сборки linux, win, osx, osx-arm64, linux-aarch64) и ros-humble.
- **Что это.** ROS в виде conda-пакетов, без системных библиотек и root, в том числе для Apple Silicon и ARM (Raspberry Pi).
- **Польза для ВКР.** Запасной путь запустить ROS 2 Jazzy прямо на macOS arm64, без ВМ (например, rosbridge или утилиты). Официально macOS — Tier 3 [F17], поэтому основной путь — Linux-ВМ.

### 2.6. Научные работы: LLM-агенты поверх ROS и MCP (для главы 1 и сравнения)

**[F23] Royce R., Kaufmann M., Becktor J., Moon S., Carpenter K., Pak K., Towler A., Thakker R., Khattak S. «Enabling Novel Mission Operations and Interactions with ROSA: The Robot Operating System Agent».** IEEE Aerospace Conference 2025 (принята), arXiv:2410.06472. Код: https://github.com/nasa-jpl/rosa (Apache-2.0, ★1653; построен на LangChain).
- **Что это.** Агент NASA JPL, который переводит команды на естественном языке в действия ROS1/ROS2 через набор инструментов. Есть проверка параметров и ограничения как механизмы безопасности. Показаны демонстрации в Mars Yard, в лаборатории и в симуляции с тремя роботами, плюс демо на TurtleSim.
- **Польза для ВКР.** Ближайший академический аналог «агент + ROS-инструменты». В отличие от постановки ВКР, оценки ресурсов и стоимости во времени нет, а агент работает рядом с ROS, а не на ограниченном SBC через мост.

**[F24] Mower C. E., Wan Y., Yu H., …, Wang J., Bou-Ammar H. «ROS-LLM: A ROS framework for embodied AI with task feedback and structured reasoning».** Nature Machine Intelligence 8, 313–325 (2026). DOI 10.1038/s42256-026-01186-z, arXiv:2406.19741. Код: https://github.com/huawei-noah/HEBO/tree/master/ROSLLM
- **Что это.** Фреймворк, где LLM (открытые и коммерческие) через чат программирует робота. Поведение извлекается из ответа LLM и исполняется как последовательность, дерево поведения или конечный автомат на действиях и сервисах ROS. Есть имитационное обучение новых действий и рефлексия по обратной связи от человека и среды.
- **Польза для ВКР.** Свежий журнальный источник уровня Nature MI, подтверждающий актуальность «LLM + ROS-контекст». Полезна идея: LLM генерирует структурированное поведение (BT/FSM) один раз, а исполняет его локальный движок, без LLM на каждом шаге. Это прямой механизм снижения числа вызовов LLM во времени.

**[F25] Fu L., Salimpour S., Militano L., Edelman H., Peña Queralta J., Toffetti G. «ROSBag MCP Server: Analyzing Robot Data with LLMs for Agentic Embodied AI Applications».** arXiv:2511.03497, 2025 (препринт).
- **Что это.** MCP-сервер для анализа ROS/ROS 2 bag-файлов: траектории, лазерные сканы, TF, временные ряды. Есть UI для бенчмаркинга вызова инструментов разными LLM. Сравнены 8 моделей; по выводу авторов, разрыв в качестве tool calling большой, лучшие — Kimi K2 и Claude Sonnet 4.
- **Польза для ВКР.** Прецедент методики «одни и те же MCP-инструменты робота — разные LLM» и подтверждение, что выбор модели заметно влияет на успех вызовов. Для ВКР та же схема переносится на GPT mini/nano через RouterAI, с добавлением стоимости и ресурсов. Близкие работы для обзора [не разбирались подробно]: RoboNeuron (arXiv:2512.10394), где MCP-инструменты выводятся из ROS-схем, и навигация LLM через MCP (arXiv:2609.27340, сентябрь 2026).

Перекрёстные ссылки на уже разобранное в проекте (не дублирую): спецификация MCP (ревизия 2026-07-28; в ней два стандартных транспорта — stdio и Streamable HTTP, а сервер не инициирует JSON-RPC-запросы) — G3_gap; «AI Agents That Matter» (Kapoor et al., TMLR 2025) и HAL — R7, G5, G7; методика валидации энергосчётчиков (включая Jetson) — G6; маршрутизация и каскады малых и больших моделей — R6.

---

## 3. Сводные таблицы

### 3.1. Цены GPT mini/nano: RouterAI (₽ за 1M токенов) против OpenAI ($ за 1M токенов)

Снимок 2026-10-06, `F_artifacts/price_table_output_2026-10-06.txt`. Цена кэшированного входа RouterAI — поле `input_cache_read`.

| Модель | Контекст | RouterAI вход | RouterAI кэш | RouterAI выход | OpenAI вход | OpenAI выход | ₽/$ |
|---|---:|---:|---:|---:|---:|---:|---:|
| gpt-4o-mini | 128K | 16,56 | 8,28 | 66,25 | 0,15 | 0,60 | 110,41 |
| gpt-4.1-nano | 1M | 11,04 | 2,76 | 44,16 | 0,10 | 0,40 | 110,41 |
| gpt-4.1-mini | 1M | 44,16 | 11,04 | 176,66 | 0,40 | 1,60 | 110,41 |
| gpt-5-nano | 400K | 5,52 | 0,55 | 44,16 | 0,05 | 0,40 | 110,41 |
| gpt-5-mini | 400K | 27,60 | 2,76 | 220,82 | 0,25 | 2,00 | 110,41 |
| gpt-5.4-nano | 400K | 23,19 | 2,32 | 144,91 | 0,20 | 1,25 | **115,93** |
| gpt-5.4-mini | 400K | 82,81 | 8,28 | 496,85 | 0,75 | 4,50 | 110,41 |
| gpt-5.6-luna | 1,05M | 22,08 | 2,21 | 132,49 | 0,20 | 1,20 | 110,41 |
| o4-mini | 200K | 121,45 | 30,36 | 485,80 | 1,10 | 4,40 | 110,41 |

Наблюдения:
- RouterAI пересчитывает официальную цену OpenAI по постоянному коэффициенту **110,41 ₽/$**; исключение — gpt-5.4-nano с 115,93. Наценку относительно курса ЦБ нужно считать на дату закупки.
- Кэш-скидки повторяют OpenAI: ×0,5 у gpt-4o-mini, ×0,25 у gpt-4.1-*, ×0,1 у gpt-5-*.
- У gpt-5-* и o4-mini скрытые reasoning-токены тарифицируются как выход (общее правило OpenAI для reasoning-моделей). Для агента надо задавать `reasoning_effort` и журналировать `completion_tokens_details.reasoning_tokens`, если он доходит через RouterAI [не проверено].
- Для сравнения, дешёвые модели с tools у других провайдеров в том же каталоге: `google/gemini-2.5-flash-lite` 11,04 / 44,16; `qwen/qwen3-30b-a3b-instruct-2507` 5,32 / 21,31; `mistralai/mistral-small-3.2-24b-instruct` 8,28 / 22,08; `meta-llama/llama-3.1-8b-instruct` 2,21 / 4,42 ₽ за 1M (вход / выход). Цена модели в каталоге — минимум по провайдерам: у `openai/gpt-oss-120b` разброс 3,31–38,64 ₽ за вход в зависимости от провайдера.

**Иллюстрация стоимости во времени (не измерение, параметры заданы вручную в `price_table.py`).** Один вызов: 3000 входных токенов, из них 2000 — стабильный префикс (системный промпт и каталог MCP) из кэша; 150 видимых выходных токенов, у reasoning-моделей ещё +300 скрытых. На одну реплику пользователя 3 вызова, 20 реплик в час, 8 часов в день, итого 480 вызовов в сутки. Итог за 30 дней с кэшем: gpt-4.1-nano ≈ 334 ₽, gpt-5-nano ≈ 382 ₽, gpt-4o-mini ≈ 620 ₽, gpt-4.1-mini ≈ 1336 ₽, gpt-5-mini ≈ 1908 ₽, o4-mini ≈ 5771 ₽. Кэш снижает стоимость на 27–42 % в зависимости от модели. Для gpt-5.6-* иллюстрация не учитывает плату за запись в кэш (`input_cache_write`). Вывод: при таком профиле **стоимость API для факультета — сотни рублей в месяц на стенд**. Главная переменная — число вызовов в час, а оно зависит от архитектуры агента: фильтрации событий, структурированного поведения, каскада моделей.

### 3.2. Одноплатники: сравнение для ВКР

| Плата | CPU | RAM | Ускоритель | Мощность (источник) | Цена | ROS 2 / ОС | Вердикт |
|---|---|---|---|---|---|---|---|
| Raspberry Pi 5 | 4× A76 @ 2,4 ГГц | 1–16 ГБ LPDDR4X | — | типично ~0,8 А × 5,1 В ≈ 4 Вт; пик в Ollama 10 Вт [F5, F9] | 4 ГБ $110, 8 ГБ $175 (апрель 2026); 8 ГБ — 32 990 ₽ (Амперка) | Ubuntu 26.04 certified → Lyrical Tier 1 arm64 | **Основной выбор** |
| Raspberry Pi 4 | 4× A72 @ 1,5 ГГц | 1–8 ГБ | — | простой 0,6 А, стресс 1,2 А (≈3,1 / 6,1 Вт) [F5]; пик в Ollama 8 Вт [F9] | 4 ГБ — 19 990 ₽ (Амперка) | как у Pi 5 | Если Pi 5 недоступен |
| Orange Pi 5 | 4× A76 + 4× A55 | 4/8/16 ГБ | NPU 6 TOPS | блок 5 В / 4 А; Pro: 9–14 Вт в Ollama [F9] | 16 ГБ ≈ 29 490 ₽ [не проверено] | вендорский Debian/Ubuntu, ROS — Tier 3 | Если нужен локальный SLM-fallback |
| Jetson Orin Nano Super | 6× A78AE | 8 ГБ LPDDR5, 102 ГБ/с | GPU 67 TOPS | 7–25 Вт [F6] | $399 (июль 2026); 77–97 тыс. ₽ [не проверено] | JetPack (Ubuntu) | Только для локального зрения |
| LicheeRV Nano | C906 @ 1 ГГц (RISC-V) | 256 МБ | NPU 1 TOPS | [нет данных] | $9,9 [F3] | Linux (образ Sipeed) [не проверено]; без ROS | Демонстрация «нижней границы» (PicoClaw) |

---

## 4. Эмуляция одноплатника на MacBook M4 Pro: что реалистично

Состояние машины **[локально, 2026-10-06]**: Apple M4 Pro, 12 ядер (8P + 4E), 24 ГиБ, macOS 27.0.1.
- Установлено: `limactl` 2.1.1 (инстансов нет), `colima` 0.10.1 (не запущена), Docker CLI 29.3.1, `rustc`/`cargo` (~/.cargo/bin), Python 3.14 (Homebrew), Node.
- **Нет**: `go`, `qemu-system-aarch64`, `utmctl`, `ros2`, `gz`.
- SDK: MacOSX26.sdk с Virtualization.framework.

| Подход | Что эмулирует правдоподобно | Чего не эмулирует | Оценка |
|---|---|---|---|
| Lima/Colima, VZ, Ubuntu arm64, `cpus=4`, `memory=2..8 GiB` [F11, F13] | ISA AArch64, ОС и пакеты (Ubuntu 26.04 как на Pi 5), объём RAM (OOM, swap), число ядер, cgroups v2 | Частоту и IPC A76, кэш (L3 2 МБ), пропускную способность памяти (Pi 5 ~17 ГБ/с [F10]), I/O microSD, энергию | **Основной вариант** |
| + Docker `--cpus/--memory` или `cpu.max` внутри ВМ [F12] | Долю CPU-времени и жёсткий лимит RAM для процесса агента | Задержку одиночной операции (квота режет время пачками по 100 мс, а не скорость ядра) | Нужен с калибровкой |
| Fogify-подобная формула `cores × MHz / MHz_хоста` [F15] | — | Разницу IPC и памяти M4 ↔ A76 | Не использовать без калибровки (критиковать в тексте) |
| QEMU `raspi4b` (TCG) [F14] | Периферию Pi 4 (GPIO/I²C/SPI) функционально | Pi 5, сеть GENET, PCIe; производительность | Только для отладки драйверного слоя; QEMU не установлен |
| Lima `--arch riscv64` (QEMU TCG) [F11] | Бинарную совместимость с RISC-V (для PicoClaw-подобного агента) | Производительность | Опционально; нужен QEMU |
| `tc netem` в ВМ | Задержку, джиттер и потери Wi-Fi/LTE до RouterAI | — | Нужен для экспериментов с деградацией сети |

**Методика калибровки (предложение).**
1. Выбрать эталонную нагрузку, похожую на работу агента: разбор JSON-ответа LLM, сериализация контекста, вызов MCP по stdio, TLS-рукопожатие, Python-импорт и старт процесса.
2. До покупки взять «якорь» из литературы: на Pi 5 модели 1–1,5B идут со скоростью 5–15 ток/с [F9], а на M4 Pro CPU (8 потоков) Qwen3-8B даёт 24 ток/с (research/ENVIRONMENT.md). Это даёт грубый порядок отношения, но не для нагрузки агента.
3. После покупки прогнать тот же набор на Pi 5. Подобрать квоту `cpu.max` в ВМ так, чтобы медианы совпали. Указать остаточную ошибку по каждому микробенчмарку как ограничение валидности эмулятора.
4. Энергию в эмуляторе не мерить. Использовать модель E = P_idle · T + Σ ΔP_k · t_k с параметрами из [F5, F9, F10], а на реальной плате проверить её USB-C-измерителем (как Otii в [F10]) и сравнить с `vcgencmd pmic_read_adc` (G6).

**Связь агента с симулятором.** Нужны две ВМ на Mac или одна ВМ плюс позже реальная плата:
```
[Pi 5 / ВМ "pi-sim" (4 vCPU, 4 GiB, cpu.max)]
   агент-оркестратор ─HTTPS─► RouterAI ─► OpenAI gpt-*-mini/nano
        │ MCP stdio
        ├─ hw-mcp (реальные GPIO/I²C/SPI/камера на плате)
        └─ ros-mcp-server ─WebSocket JSON :9090─┐
                                                 ▼
[Mac: ВМ "sim" Ubuntu arm64 + ROS 2 Jazzy/Lyrical + Gazebo Harmonic/Jetty (headless, LIBGL_ALWAYS_SOFTWARE) +
      rosbridge_server + TurtleBot3/4 (camera, scan, odom, imu, cmd_vel)]   portForwards: 9090 → hostIP 0.0.0.0
```

---

## 5. Метрика «работы во времени»: что считать

Предложение для главы «Методика», согласованное с фразой руководителя:
- **Профиль эксплуатации** (входной параметр эксперимента): T_on часов в день, интенсивность реплик пользователя λ_u в час, интенсивность событий датчиков λ_s в час, доля событий, требующих рассуждения. Фазы сценария задаются заранее, как в MockFog [F16].
- **Накопительные метрики за час и за сутки:**
  - ₽ (по `usage` и снимку цен);
  - токены вход / кэш / выход / reasoning;
  - число вызовов LLM;
  - байты в сеть и из сети (LLM и rosbridge отдельно);
  - CPU-секунды и пиковый, а также средний RSS процесса агента на SBC;
  - Вт·ч платы на реальном Pi;
  - число MCP-вызовов инструментов.
- **Метрики отклика:** p50/p95 времени от реплики или события до действия, с разбиением на сеть, LLM (TTFT и генерация) и локальную часть.
- **Нормированные:** ₽ и Вт·ч на одну обработанную реплику или событие; ₽ и Вт·ч на час работы при данном профиле. Важно: при λ → 0 остаётся «стоимость простоя» — энергия P_idle платы и фоновые опросы.
- **Качество** (чтобы экономия не была бесплатной): доля корректных вызовов инструментов (BFCL-подмножество через `harness/` и сценарии робота). Парные сравнения — через `harness/stats.py` (bootstrap, McNemar, TOST).

---

## 6. Что переиспользовать из уже сделанного в проекте

- `harness/server_runner.py` уже ходит в OpenAI-совместимый `POST /v1/chat/completions` через `http.client.HTTPConnection`. Чтобы получить раннер RouterAI, нужны HTTPS, заголовок `Authorization: Bearer`, префикс `/api/v1`, параметр `provider` и журнал `usage` и `service_tier`. Интерфейс `Runner`/`Session` (`harness/runner_base.py`) подходит без изменений.
- `harness/stats.py` (парный bootstrap, McNemar, TOST, Холм) и `harness/bfcl.py` — для сравнения GPT mini/nano по точности tool calling.
- `monitor/check_trace.py` — независимая проверка трасс агента (схемы аргументов, бюджеты, времена). Пригоден для трасс агента на SBC.
- `harness/envinfo.py` — фиксация окружения; нужно добавить снимок цен RouterAI и версии провайдера.
- Литература: G3 (сжатие и отбор каталога MCP-инструментов — прямое снижение ₽ во времени), G6 (валидация энергосчётчиков — для PMIC ADC на Pi 5), R6 (каскады nano→mini), R7 (методика измерений).

---

## 7. Выводы, открытые проблемы, следствия

### Выводы
1. Стек реализуем без покупки и без ключа уже сейчас. Каталог моделей и цены RouterAI публичны и машиночитаемы. Lima, Colima и Docker стоят. arm64-пакеты ROS 2 и Gazebo, включая rosbridge, ros_gz и TurtleBot3, есть в apt для Ubuntu 24.04 и 26.04.
2. RouterAI подходит для факультетской оплаты: юрлицо в РФ, счёт и ЭДО, командные лимиты. Все нужные GPT mini/nano есть, с tools и structured outputs. Цена = OpenAI × 110,41 ₽/$. Методический риск — неявная маршрутизация по провайдерам: провайдер нужно закреплять.
3. Raspberry Pi 5 (4 ГБ) — рациональный выбор. Ubuntu 26.04 certified даёт ROS 2 Lyrical Tier 1, есть официальная таблица мощности и PMIC-телеметрия. С декабря 2025 по апрель 2026 цены выросли в 1,6–2,1 раза из-за DRAM ($70 → $110 за 4 ГБ, $145 → $305 за 16 ГБ).
4. Локальные LLM на SBC годятся только как fallback до ~1,5B с 5–15 ток/с при 8–10 Вт пика [F9]. Это количественное подтверждение «тупых локальных LLM».
5. Эмуляция в VZ-ВМ правдоподобна по ОС, RAM и числу ядер, но не по скорости ядра и не по энергии. Стандартный приём «квота = отношение частот» (Fogify) методически слаб, нужна калибровка.
6. Связь «агент на SBC → симулятор на ноутбуке» лучше строить через rosbridge и MCP (ros-mcp-server), чем через DDS. Агенту не нужен ROS, а трафиком управляют `throttle_rate` и `compression`.
7. Стоимость API при разумном профиле — сотни рублей в месяц на стенд. Поэтому интереснее оптимизировать не цену модели, а число вызовов и размер контекста во времени: это задача архитектуры агента.

### Открытые проблемы
- Не найдено опубликованных измерений «агент с внешней LLM на одноплатнике, несколько часов работы»: ₽, трафик, энергия и RSS во времени. Это пробел, который ВКР закрывает экспериментально. Поиск шёл по arXiv, OpenAlex и GitHub; Semantic Scholar недоступен (429).
- Наблюдаемость кэша через агрегатор: возвращает ли RouterAI `prompt_tokens_details.cached_tokens` и `reasoning_tokens`, «прилипают» ли запросы к одному провайдеру и кэшу, пробрасывается ли `prompt_cache_key`. Проверяется первым же ключом.
- Rate limits RouterAI для chat completions не документированы.
- Точность эмуляции Pi 5 на M4 Pro: размещение vCPU на P- и E-ядрах, разница IPC и пропускной способности памяти. Нужна калибровка на реальной плате.
- Камера в Gazebo внутри ВМ без GPU рендерится программно: низкий FPS и CPU-нагрузка на ноутбуке. Кадры с камеры во внешнюю LLM стоят денег (vision-токены) и упираются в приватность (152-ФЗ; у GPT провайдеры только в США).
- Gazebo Jetty и Harmonic на macOS 27 через Homebrew официально не заявлены; arm64 Linux — best-effort.
- Цены нестабильны: RouterAI и OpenAI меняют тарифы, Pi подорожал трижды за 4 месяца, Jetson на +60 %. Нужны снимки цен на дату эксперимента. Цены в РФ отличаются от цен в USD (Pi 5 8 ГБ: $175 ≈ 32 990 ₽ у Амперки).
- Заявления PicoClaw (<10 МБ RAM, загрузка <1 с) не подкреплены методикой, их нужно перемерить.

### Следствия для постановки, архитектуры и экспериментов
- **Постановка.** «Разработка ИИ-агента для граничного устройства с внешней LLM, который поддерживает контекст пользователя и оборудования (MCP), и исследование ресурсоёмкости и стоимости его работы во времени при ограничениях одноплатника». Объект — система «SBC + внешняя LLM + MCP-оборудование». Предмет — механизмы сокращения вызовов и контекста и их влияние на ₽, Вт·ч, трафик, RSS и задержку при сохранении точности.
- **Архитектура.**
  - Оркестратор на Go или Rust. Rust уже стоит; Go для форка PicoClaw придётся поставить. Python — как быстрый прототип для сравнения ресурсов.
  - Стабильный кэшируемый префикс: системный промпт и каталог MCP.
  - Компактный контекст оборудования: сводка состояния, а не сырые топики.
  - Локальный фильтр событий датчиков: правила и пороги, LLM вызывается только по значимым событиям.
  - Структурированное поведение от LLM (BT/FSM, как в ROS-LLM [F24]) исполняется локально.
  - Каскад nano → mini (R6), flex tier для фоновых задач.
  - MCP-серверы по stdio на плате: `hw-mcp` для реальных шин и `ros-mcp-server` или собственный компактный сервер для симулятора.
- **Эксперименты.**
  - E1 — выбор модели: точность tool calling на BFCL-подмножестве и на сценариях TurtleBot против ₽ и задержки для 8–9 GPT mini/nano с закреплённым провайдером.
  - E2 — «сутки работы»: сценарий из фаз длительностью несколько часов на эмуляторе, затем на Pi 5; накопительные метрики §5.
  - E3 — абляции механизмов: кэш префикса, сжатие или отбор каталога (G3), фильтр событий, BT/FSM, каскад, flex; ₽ и вызовы в час против точности.
  - E4 — ресурсы оркестратора: Python, Go и Rust, плюс замер PicoClaw и NanoClaw (RSS, CPU, время старта) на эмуляторе и на плате.
  - E5 — деградация сети (`netem`): задержка, потери, повторы и стоимость.
  - Статистика — парные тесты из `harness/stats.py`. Цены и версии моделей фиксируются снимком.
- **Закупка (предложение руководителю).** Raspberry Pi 5 4 ГБ (или 8 ГБ), блок 27 Вт, microSD или NVMe, Camera Module 3 — опционально, USB-C-измеритель мощности с логированием, датчик I²C для демонстрации реального инструмента (например, температуры). Факультетский командный аккаунт RouterAI с лимитом на студента.
