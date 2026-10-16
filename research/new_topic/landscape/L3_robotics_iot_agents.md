# L3. Ландшафт: агенты для робототехники и IoT (ROS/ROS 2, стек NVIDIA Jetson, промышленный IoT) на октябрь 2026

Тема ВКР: «Разработка ИИ-агента для приложений граничного искусственного интеллекта» (ИТМО, 09.03.01).
Вопрос студента: «какие агенты сейчас вообще есть и используются в этой сфере». Эта заметка даёт карту **систем и фреймворков**: что существует, где у каждой работает LLM, являются ли инструменты датчиками и приводами, есть ли MCP, на каком железе это реально применяют, насколько система зрелая и **запустится ли она на кафедральном Jetson Nano**.

Дата проверки: **2026-10-06**. Звёзды, даты и версии указаны на эту дату.

**Методика.**
- Метаданные репозиториев брались из GitHub API (`gh api repos/...`): stargazers_count, created_at, pushed_at, language, license, releases. Тексты — из README через raw.githubusercontent.com.
- Пакеты проверялись через PyPI JSON API (версия, requires_python, зависимости), npm registry (engines.node) и pypistats.org (загрузки за месяц).
- Документацию читал через WebFetch: NVIDIA, Home Assistant, Microsoft Learn, пресс-релизы Siemens, блог AWS. Статьи — через arXiv API и Crossref.
- Для чтения кода сделал `git clone --depth 1` у ros-mcp-server, ROSA, ros2_mcp, ros2ai, EmbodiedAgents, ROS-LLM (Auromix), nav2_mcp_server, ros2_nanollm и sparse-клон jetson-containers.
- **Собственные измерения [M]:** скачал linux-arm64 бинарники OM1 (nightly), PicoClaw v0.3.1 и ZeroClaw v0.8.5 (gnu и musl) и проверил их **только статически** (`file`, `strings` по версиям символов GLIBC). Ничего не запускал и не устанавливал.

**Пометки:**
- [V] — проверено по первоисточнику;
- [M] — измерено мной;
- [A] — число заявлено авторами проекта, независимо не проверено;
- [U] — не проверено или вывод по косвенным данным.

**Связь с соседними заметками (не дублирую, только ссылаюсь):**
- научные работы SayCan, Inner Monologue, Code as Policies, ProgPrompt, ChatGPT for Robotics, ROSGPT, ROS-LLM (Huawei), ROSA (статья), Sasha, SAGE, HomeBench, SimuHome, IoT-MCP, DCP разобраны в `../C_llm_agents_robotics_iot.md`;
- ros-mcp-server (31 инструмент, 6 ресурсов), gazebo-mcp, Home Assistant MCP, ha-mcp, xiaozhi-esp32, MCP over MQTT — в `../B_mcp_hardware.md`;
- Claw-семейство (OpenClaw, NanoClaw, PicoClaw, ZeroClaw…) и статья ROSClaw (Cardenas et al.) — в `../A_claw_analogs.md`;
- ReMEmbR как память робота — в `../D_context_memory.md` [F4];
- стек RouterAI, Raspberry Pi 5, эмуляция на Mac, ROS 2 Jazzy + Gazebo Harmonic — в `../F_practical_stack.md`;
- локальное восприятие на борту — в `../G_edge_ai_onboard.md`.

Здесь — то, чего в них нет: экосистема ROS-агентов и агентных рантаймов роботов, стек NVIDIA Jetson AI Lab, промышленные edge-агенты и **матрица совместимости с Jetson Nano**.

---

## 0. Коротко

1. **«Jetson Nano на 1 ГБ» не существует.** Бывают Jetson Nano Developer Kit 4 ГБ (2019), Jetson Nano 2GB Developer Kit (2020) и модуль Jetson Nano 4 ГБ, в том числе вариант с 16 ГБ eMMC. Оба devkit сняты с производства, модуль поставляется до января 2027 [V, NVIDIA lifecycle]. **Программный потолок Nano — JetPack 4.6.6 / Jetson Linux R32.7.6. Это финальный выпуск JetPack 4:** Ubuntu 18.04, ядро Linux 4.9, CUDA 10.2 [V]. Отсюда glibc 2.27 и Python 3.6 из Ubuntu 18.04 [U: на плате не проверял]. Почти весь современный агентный софт этому не соответствует:
   - Python ≥3.10 требуют ros-mcp, RAI, ros2_mcp, официальный MCP SDK;
   - Node ≥22/24 требуют OpenClaw и ROSClaw (Tongji);
   - glibc ≥2.34 требуют бинарники OM1 и ZeroClaw-gnu [M];
   - ROS 2 Humble/Jazzy нативно не ставится.

   На Nano нативно работают ROS Melodic с `rosbridge_server` (0.11.16) [V] и **статически слинкованные бинарники**: PicoClaw v0.3.1, ZeroClaw-musl [M], движок Edge Agents (`CGO_ENABLED=0`) [V, Dockerfile]. Остальное запускается только в контейнерах с другим userspace или выносится на другой хост.
2. **Схема руководителя совпадает с основной схемой области.** Внешняя LLM рассуждает, на борту тонкий оркестратор, MCP или ROS даёт доступ к датчикам и приводам. LLM работает в облаке (или на сервере оператора) у ROSA, RAI, ros-mcp-server, ros2ai, OM1, DimOS, ROSClaw, xiaozhi-esp32, Home Assistant, AWS-образца и Siemens (вариант cloud). Локально, на борту, LLM работает только на Jetson Orin/Thor (NanoLLM, OpenClaw-on-Jetson, ReMEmbR-память), на AMD Ryzen AI (демо RAI) или на промышленных IPC с GPU (Siemens on-prem, Azure Foundry Local).
3. **MCP стал стандартным «южным» интерфейсом к железу за 2025–2026 годы.** Появились:
   - мосты к ROS: ros-mcp-server — 1 486★, 1 464 загрузки за месяц; ros2_mcp; nav2_mcp_server; unitree-go2-mcp;
   - MCP в агентных ОС роботов: DimOS (skills через MCP), ROSClaw (22 MCP-инструмента в UR5e-тесте), OM1 (MCP-клиент на каждый режим);
   - MCP на самом устройстве: xiaozhi-esp32 — 30 445★, ESP32 выступает MCP-сервером;
   - официальный MCP-сервер Isaac Sim 6.0, но он отдаёт документацию и API, а не управление роботом;
   - встроенный MCP-сервер в Azure Agentic Retrieval (Foundry Local).

   **Исключения:** зрелые ROS-агенты ROSA и RAI MCP не используют, у них свои LangChain/LangGraph-инструменты. В коде MCP не найден [V, поиск по коду и дереву].
4. **Есть две топологии MCP, и их стоит явно сравнить в ВКР:**
   - (а) робот или устройство — MCP-**сервер**, агент — внешний клиент: ros-mcp-server, ros2_mcp, xiaozhi, Home Assistant MCP Server, DimOS;
   - (б) агент на борту — MCP-**клиент** внешних инструментов, а железо подключено своими плагинами: OM1, Home Assistant MCP client.

   Для Jetson Nano естественна (а) в варианте «Nano = оборудование + лёгкий сервер».
5. **Контекст оборудования в существующих системах слабый.** У ros-mcp-server это свободный текст в YAML (`robot_specifications/*.yaml`, поле `prompts`). У ROSA — интроспекция ROS-графа через CLI. У RAI — `rai_whoami`, синтез описания из документации, изображений и URDF. У ROSClaw — манифест тел и возможностей с полномочиями. Структурированного стандарта (единицы, диапазоны, задержки, стоимость опроса, безопасные режимы) нет ни у кого, кроме протокольных попыток вроде DCP (см. C [20]).
6. **Метрики «во времени» в существующих системах фрагментарны:**
   - OM1 — Prometheus/Grafana, но только задержки: LLM, VLM, ASR, TTS, KB, HTTP. Токенов, стоимости, CPU и RAM в `internal/metrics/metrics.go` нет [M, чтение кода];
   - ROSA — `show_token_usage` на каждый вызов и только для ChatOpenAI/AzureChatOpenAI [V, код];
   - RAI — трассировка через Langfuse, это зависимость `langfuse>=3.8` и `docs/setup/tracing.md`;
   - Live VLM WebUI — живые GPU, CPU, RAM и задержка;
   - jetson_stats (jtop) — ресурсы Jetson.

   Ни одна система не сводит в один суточный профиль ресурсы, токены, стоимость и задержки. Это совпадает с выводом заметки C и подтверждает нишу ВКР.
7. **NVIDIA перестроила Jetson AI Lab.**
   - Старые NanoLLM и Agent Studio заморожены: последний push в NanoLLM — 2024-10-18, страница Agent Studio помечена «deprecated and no longer maintained» [V]. Работали они только на Orin с JetPack 5/6.
   - Новая линия — **OpenClaw и NemoClaw на Jetson**. Для Orin Nano 8 ГБ авторы туториала измерили: Ollama + qwen3.5:2b, 16K контекста, 4.6 ГБ памяти, около 20 ток/с генерации, холодный ответ около 15 с, тёплый около 3 с [A].
   - В jetson-containers появилась группа `agents` (openclaw, zeroclaw, openfang, hermes-agent, mempalace).
   - Ветка master официально поддерживает **только JetPack 6.2 и 7**, Nano остаётся на legacy-ветках `L4T-R32.x` [V].
8. **Промышленный IoT опубликовал меньше, чем анонсировал:**
   - AWS: открытый образец, крошечная ONNX-модель (около 25 тыс. параметров) на Cortex-A53 с 256 МБ, эскалация в облачный агент Bedrock AgentCore, 22 МБ пикового RSS [A, блог 2026-09-01];
   - Microsoft: превью Agentic Retrieval in Foundry Local (бывший Edge RAG) на Azure Local с двумя GPU и встроенным MCP-сервером;
   - Siemens: Industrial Copilot for Operations on-prem на Simatic IPC 1047E + NVIDIA NIM, без интернета. Это закрытый продукт без технических деталей.

   Ни один из трёх не ориентирован на одноплатник класса Nano или Raspberry Pi как на хост LLM-агента.
9. **Совпадения имён: в литературе путают.**
   - «ROS-LLM» — это (1) Huawei Noah's Ark, *Nature Machine Intelligence* 2026, ROS Noetic, и (2) Auromix, ROS 2 Humble, 830★, заброшен в 2023.
   - «ROSClaw» — это (1) статья Cardenas et al., arXiv:2603.26997, код пока не опубликован (см. A [S16]), и (2) репозиторий ros-claw/rosclaw от Tongji University / SRIAS, 215★, alpha.
   - PyPI-пакет `rosclaw` 0.0.1 — заглушка («Placeholder package to reserve the PyPI name») с URL `yourusername`.
   - PyPI-пакет `nanollm` 0.1 (2023) — не NanoLLM от dusty-nv.

---

## 1. Кафедральное железо: какой бывает Jetson Nano и что это значит для софта

### 1.1. Варианты [V]

| Плата | Память | Статус | Источник |
|---|---|---|---|
| Jetson Nano Developer Kit (2019, ревизии A02/B01) | 4 ГБ LPDDR4 64-bit, GPU 128 ядер Maxwell, 4× Cortex-A57, 472 GFLOPS | EOL: «will be discontinued over the next few months as inventory declines» (форум NVIDIA, 20.12.2023) | forums.developer.nvidia.com/t/276730; даташиты у реселлеров |
| Jetson Nano 2GB Developer Kit (2020) | 2 ГБ LPDDR4 | EOL (страница lifecycle) | developer.nvidia.com/embedded/lifecycle |
| Модуль Jetson Nano (в т.ч. 16 ГБ eMMC) | 4 ГБ | «Available through January 2027» | там же |
| Jetson Orin Nano 4GB / 8GB (другое поколение, Ampere) | 4 / 8 ГБ | «Available through January 2032» | там же |

Варианта на 1 ГБ у Jetson нет. Возможно, студент имел в виду Nano 2GB или вообще другую плату.
**Что сделать на кафедре:**
- `cat /etc/nv_tegra_release` — версия L4T;
- `free -h` — объём памяти;
- `sudo -H pip3 install jetson-stats && jtop` — модель платы.

Про jetson_stats: rbonghi/jetson_stats — 2 632★, AGPL-3.0, в описании «Orin, Xavier, Nano, TX».

### 1.2. Программный потолок [V]

- **JetPack 4.6.6 / Jetson Linux R32.7.6.** Страница NVIDIA: «marks the final release for Jetson Linux R32 and JetPack 4».
- Состав: Linux Kernel 4.9, sample rootfs на Ubuntu 18.04, CUDA 10.2, cuDNN 8.2.1, TensorRT 8.2.1.
- Из Ubuntu 18.04 следуют **glibc 2.27** и системный **Python 3.6** [U: сведения о дистрибутиве, на плате не проверял].

### 1.3. Что из этого следует (матрица требований)

| Требование агентного софта | Пример | Nano нативно? | Обход |
|---|---|---|---|
| Python ≥3.10 | ros-mcp 3.1.2, rai-core 2.12.1 (<3.13), mcp_server_ros_2, MCP SDK `mcp` 2.3.0, live-vlm-webui 0.4.0 | нет (3.6) | собрать CPython или взять контейнер с Ubuntu 22.04 arm64; либо вынести процесс на Mac |
| Python ≥3.12 | LeRobot 0.6.1, nav2-mcp-server (только 3.12) | нет | то же |
| Node.js ≥22/24 | OpenClaw 2026.9.8 (`engines.node >=24.16.0 <25 \|\| >=26.1.0`), ROSClaw (≥22.19) | нет: официальные arm64-сборки Node требуют **glibc ≥2.28 и ядро ≥4.18** (BUILDING.md, v18–v22). Сноска: «Older kernel versions may work» | контейнер даст новый glibc, но ядро останется 4.9 [U] |
| Динамически слинкованные бинарники с GLIBC_2.34 | OM1 nightly linux-arm64 (`om1` 13.1 МБ + `libzenohc.so` 16.0 МБ; максимум GLIBC_2.34) [M]; ZeroClaw v0.8.5 gnu (GLIBC_2.34) [M] | нет | контейнер Ubuntu 22.04+ [U] |
| Статические бинарники | PicoClaw v0.3.1 `picoclaw` (34.5 МБ, statically linked) [M]; ZeroClaw v0.8.5 musl (`zeroclaw` 41.2 МБ, statically linked) [M]; движок Edge Agents (Go, `CGO_ENABLED=0`, distroless/static) [V] | **вероятно да**: нет зависимости от glibc, а Go и musl работают на ядре 4.9 [U: не запускал] | — |
| ROS 1 | ROS Melodic (Ubuntu 18.04); `rosbridge_suite` 0.11.16-2 выпущен для Melodic (rosdistro) | да | — |
| ROS 2 Humble/Jazzy | RAI, ros2_mcp, nav2_mcp, llama_ros, EmbodiedAgents, ros2ai | нет (18.04) | старые образы `dustynv/ros:humble-ros-base-l4t-r32.7.1` (0.71 ГБ сжато, обновлён 2023-12-06) [V, Docker Hub]. В legacy Dockerfile ROS 2 собирается из исходников на системном python3 базового l4t-образа, то есть, вероятно, 3.6 [U] |
| CUDA 11+/12 для LLM | NanoLLM (только Orin), vLLM, современный Ollama/llama.cpp | нет (CUDA 10.2) | на Nano локальную LLM не запускать, рассуждение — во внешней LLM |

**Вывод для архитектуры на Nano.** Реалистичны два варианта:
- (1) **Nano как «оборудование»:** датчики, камера и приводы плюс тонкий сервер — rosbridge на Melodic или свой MCP-сервер в статическом бинарнике на Go, Rust или C. Агент и MCP-клиент работают на Mac или в облаке.
- (2) **Агент на Nano** в виде статического бинарника по образцу PicoClaw или ZeroClaw-musl, с внешней LLM по API.

Python-агенты (ROSA, RAI) на Nano пойдут только в контейнере с новым userspace, и при 4 ГБ (тем более 2 ГБ) общей памяти запас небольшой [U].

Отдельно: Raspberry Pi 5 на Ubuntu 24.04 arm64 — Tier 1 для ROS 2 Jazzy по REP-2000 (см. F). Для него этих ограничений нет.

---

## 2. Классы систем

| Класс | Что делает LLM | Где LLM | Инструменты | Примеры |
|---|---|---|---|---|
| K1. ROS-агенты «LLM + инструменты ROS» | ReAct, выбор вызовов ROS CLI/API | облако или сервер оператора; Ollama опционально | интроспекция графа, публикация, сервисы, параметры | ROSA, RAI, ROS-LLM, ros2ai |
| K2. MCP-мосты к ROS и симуляторам | агент снаружи (Claude Code, Codex…), сервер отдаёт примитивы | облако (клиент) | topics/services/actions как MCP tools; метаданные как resources | ros-mcp-server, ros2_mcp, nav2_mcp_server, unitree-go2-mcp, gazebo-mcp, Isaac Sim MCP |
| K3. Агентные рантаймы и «ОС» роботов | оркестрация режимов, памяти, навыков, безопасного исполнения | облако + опционально локально | плагины входов (датчики) и действий (приводы), HAL | OM1, DimOS, EmbodiedAgents, ROSClaw (Tongji), quackd |
| K4. Локальный инференс как ROS-узел | генерация, tool calling на борту | борт (CPU/GPU) | ROS-сообщения с tool calls | llama_ros, ros2_nanollm |
| K5. Стек NVIDIA Jetson | локальные LLM/VLM, контейнеры, агентные туториалы | борт Orin/Thor | плагины, функции, камера | jetson-containers, NanoLLM/Agent Studio, OpenClaw/NemoClaw on Jetson, ReMEmbR |
| K6. IoT-устройства и edge-рантаймы | голосовой агент, workflow | облако (xiaozhi), облако или локально (Edge Agents, HA) | MCP на устройстве (GPIO, servo, LED, камера); GPIO/UART/MQTT-узлы | xiaozhi-esp32, Edge Agents, Home Assistant |
| K7. Промышленный IoT | RCA, Q&A по документации и данным станка | облако (AWS), on-prem GPU (Azure, Siemens) | телеметрия, документы, MCP-источники | AWS Greengrass edge AI agent, Azure Agentic Retrieval, Siemens Industrial Copilot |
| K8. Политики VLA (контекст) | прямое управление моторами | борт Orin/Thor или GPU-сервер | — (модель и есть контроллер) | GR00T N1.7, LeRobot (Pi0/SmolVLA), Cosmos Reason 2 |

---

## 3. Карточки (25 позиций)

### K1. ROS-агенты

#### 1. ROSA — Robot Operating System Agent (NASA JPL)
- **Ссылка:** https://github.com/nasa-jpl/rosa — Python, Apache-2.0, **1 653★**, создан 2024-08-05, последний push 2026-03-17, релиз v1.0.10 (2026-03-17), 5 открытых issues, около 8 контрибьюторов [V]. PyPI `jpl-rosa` 1.0.10, `requires_python >=3.9,<4`. Зависимости: `langchain~=0.3.23`, `langchain-openai~=0.3.14`, опционально `langchain-anthropic` и `langchain-ollama` [V]. pypistats: 232 загрузки за месяц [V].
- **Статья:** IEEE Aerospace Conference 2025, DOI 10.1109/AERO63441.2025.11068426; arXiv:2410.06472 [V, Crossref]. Подробный разбор — C [9].
- **Архитектура:** LangChain-агент, модель передаётся параметром (`ROSA(ros_version=..., llm=...)`), поэтому LLM в облаке или на сервере оператора. Инструменты ROS 2 вызывают CLI `ros2` через `subprocess`: `ros2_node_list`, `ros2_topic_list`, `ros2_topic_echo`, `ros2_topic_info`, `ros2_service_list`, `ros2_service_call`, `ros2_param_get/set`, `ros2_doctor`, логи [V, `src/rosa/tools/ros2.py`]. Есть `blacklist` инструментов [V, код].
- **Инструменты = датчики и приводы?** Косвенно: через топики и сервисы ROS (echo датчика, публикация команды). Основная направленность — **диагностика и инспекция**.
- **MCP:** нет (в коде не найдено) [V].
- **Метрики:** `show_token_usage` — только для ChatOpenAI/AzureChatOpenAI и не при streaming [V, код]. Временных рядов нет.
- **Железо:** демо — NeBula-Spot в Mars Yard, TurtleSim в Docker. В README ROS 1 Noetic и ROS 2 Humble/Iron/Jazzy. Расширение для Isaac Sim — «Coming Soon» [V, README].
- **Зрелость:** стабильная библиотека, но активность снизилась: последний релиз в марте 2026.
- **Jetson Nano:** с трудом. Нужен Python ≥3.9 и `ros2` CLI в том же окружении, а ROS 1 Melodic на Nano работает на Python 2.7. Реально только в контейнере с ROS 2 и новым Python [U]. Через `langchain-openai` подключается к OpenAI-совместимым API, значит, вероятно, и к RouterAI (совместимость RouterAI см. F) [U].

#### 2. RAI (RobotecAI)
- **Ссылка:** https://github.com/RobotecAI/rai — Python, Apache-2.0, **599★**, создан 2024-06-04, push 2026-09-10, релиз 2.12.4 (2026-09-08), 71 открытый issue, около 28 контрибьюторов [V]. PyPI `rai-core` 2.12.1 (2026-06-09, PyPI отстаёт от GitHub), `requires_python <3.13,>=3.10`. Зависимости: `langgraph>=1.1.5`, `langchain>=1.0`, `langchain-openai/aws/ollama/google-genai`, `opencv-python`, `streamlit`, `langfuse>=3.8`, `numpy<2.0` [V]. pypistats: 127 за месяц [V].
- **Статья:** Rachwał et al. RAI: Flexible Agent Framework for Embodied AI. PAAMS 2025, CCIS, DOI 10.1007/978-3-032-05925-3_16; arXiv:2505.07532 [V, Crossref].
- **Архитектура:** мультиагентный фреймворк поверх ROS 2 (Humble/Jazzy, Ubuntu 22.04/24.04, Python 3.10/3.12). Пакеты [V, README]:
  - `rai core`;
  - `rai whoami` — «extract and synthesize robot embodiment information from a structured directory of documentation, images, and URDFs»;
  - `rai_asr`, `rai_tts`, `rai_sim`, `rai_bench`, `rai_perception` (open-set detection), `rai_nomad`;
  - `rai_finetune` — в планах.
- **Где LLM:** облако (OpenAI, AWS Bedrock, Gemini) или локально (Ollama). Демо `agentic-mobile-manipulator` (RB-KAIROS, ROSCon 2025) выполняется на борту на AMD Ryzen AI: LFM2-VL, llama.cpp/Vulkan на GPU, «safety VLM» на NPU через FastFlowLM [V, README демо].
- **Инструменты = датчики и приводы:** да, ROS 2-инструменты (навигация, манипуляция, восприятие).
- **MCP:** не найден ни в дереве, ни через поиск по коду [V, но поиск GitHub по коду может быть неполным].
- **Метрики:** трассировка через Langfuse/LangSmith (`docs/setup/tracing.md`), то есть токены и задержки на уровне трасс [U: страницу не читал]. Плюс `rai_bench`.
- **Зрелость:** активный проект, один из фокусных у ROS Embodied AI Community Group [V, README].
- **Jetson Nano:** нет нативно (ROS 2 + Python 3.10–3.12 + тяжёлые зависимости). На Orin — вероятно [U].
- **Польза для ВКР:** `rai_whoami` — ближайший готовый аналог «контекста оборудования» как синтезированного описания; `rai_bench` — образец бенчмарка.

#### 3. ROS-LLM — две разные системы с одним именем
- **(a) Huawei Noah's Ark:** Mower et al. *Nature Machine Intelligence* 8, 313–325 (2026); arXiv:2406.19741 [V, arXiv journal_ref]. Код — https://github.com/huawei-noah/HEBO/tree/master/ROSLLM. Пакеты `agent_comm`, `behavior_executor`, `observation_manager`, `rosllm_msgs/srvs`, внешние BehaviorTree.ROS и SMACH. Установка под **ROS Noetic** + catkin. Последний коммит в папке — 2025-11-27 [V]. LLM — удалённый сервер (в статье vLLM в Европе для робота в Азии, см. C [8]). Поведение — последовательность, BT или FSM. MCP нет.
- **(b) Auromix/ROS-LLM:** https://github.com/Auromix/ROS-LLM — Python, Apache-2.0, **830★**, создан 2023-06-15, **последний push 2023-07-10** [V]. ROS 2 Humble, OpenAI GPT-4/ChatGPT, ASR через AWS или локальный Whisper. README рекомендует облачный ASR для «low-performance edge embedded platforms» [V]. В планах значились «Agent Mechanism», обратная связь от функций и навигация — не сделаны. **Заброшен.**
- **Jetson Nano:** (a) — ROS Noetic, то есть Ubuntu 20.04; на Nano только контейнер `noetic-*-l4t-r32.7.1` [U]. (b) — Humble, только в контейнере.
- **Польза:** (a) — академическая опора, NMI 2026. (b) — пример раннего «моста ChatGPT ↔ ROS 2», как и ROSGPT (см. C [7]).

#### 4. ros2ai
- **Ссылка:** https://github.com/fujitatomoya/ros2ai — Python, Apache-2.0, **331★**, создан 2023-11-12, push 2026-08-04, релизов на GitHub нет, 7 открытых issues [V].
- **Что это:** расширение ros2cli — `ros2 ai status | query | exec "give me all nodes"` [V, README и `ros2ai/verb/`]. Использует OpenAI Python API; эндпоинт задаётся `OPENAI_ENDPOINT`, а модель `OPENAI_MODEL_NAME` по умолчанию `gpt-4o`. Поэтому подходит любой OpenAI-совместимый сервис: Ollama (`http://127.0.0.1:11434/v1`), а также, вероятно, RouterAI [U].
- **Дистрибутивы:** Humble, Jazzy, Kilted, Rolling; пакет `apt install ros-kilted-ros2ai`; Docker-образ `tomoyafujita/ros2ai` [V].
- **Инструменты:** генерирует и исполняет команды ros2 CLI, то есть «агент-оператор консоли». MCP нет.
- **Jetson Nano:** нет нативно (нужен ROS 2), только контейнер.
- **Польза:** минимальная базовая линия «LLM → команда ROS» без цикла инструментов. Удобно сравнивать с агентом по токенам и задержке.

### K2. MCP-мосты к ROS и симуляторам

#### 5. ros-mcp-server (robotmcp)
- **Ссылка:** https://github.com/robotmcp/ros-mcp-server — Python, Apache-2.0, **1 486★**, создан 2025-04-11, push 2026-10-05, релиз v3.1.2 (2026-09-23), 62 открытых issue, около 19 контрибьюторов [V]. PyPI `ros-mcp` 3.1.2, Python ≥3.10. Зависимости: `fastmcp>=2.11.3,<3`, `mcp[cli]>=1.13.0`, `opencv-python`, `pillow`, `websocket-client`, `jsonschema`. **1 464 загрузки за месяц** — максимум среди ROS-агентов в этой заметке [V].
- **Подробности** (31 инструмент, 6 ресурсов, зависимость от старых ревизий MCP) — в B [H1]. Добавлю:
  - транспорты `stdio`, `http`, `streamable-http` (`ros_mcp/main.py`) [V];
  - связь с роботом через **rosbridge WebSocket**, код робота не меняется;
  - «контекст оборудования» — YAML в `robot_specifications/` (шаблон `YOUR_ROBOT_NAME.yaml`, `unitree_go2.yaml`). Это **свободный текст в поле `prompts`**: как управлять, какие топики датчиков, правила безопасности [V].
- **Инструменты = датчики и приводы:** да (`subscribe_once`, `publish_for_durations`, `send_action_goal`, изображения с камер).
- **Железо:** промышленный робот (диагностика захвата), «Wilson» (Nav2 + MoveIt), Unitree Go2 в Isaac Sim [V, README].
- **Jetson Nano:** **да, в разнесённой схеме.** На Nano нужен только `rosbridge_server` под ROS Melodic: пакет есть в Melodic, 0.11.16-2 [V, rosdistro]. Сам MCP-сервер (Python ≥3.10) запускается на Mac. README заявляет совместимость с ROS 1 [V], сквозной тест с Melodic не проверял [U].
- **Польза:** самый массовый готовый мост «агент ↔ ROS/Gazebo». Подходит как инструмент для сценария с симулятором и как пример слабого, текстового контекста оборудования.

#### 6. ros2_mcp (WiseVision)
- **Ссылка:** https://github.com/wise-vision/ros2_mcp — Python, **MPL-2.0**, **89★**, создан 2025-04-14, push 2026-09-30, релизы по схеме «ГГММ», последний — 2610 (2026-09-29) [V]. `requires-python >=3.10`, `mcp[cli]>=1.23.0` [V, pyproject].
- **Чем отличается от ros-mcp-server:** работает **нативно в ROS 2** (Humble/Jazzy) через `rclpy`, без rosbridge [V, код `server/`]. Транспорт stdio, есть Docker-образ `mcp/ros2` [V, README]. Инструменты [V, README]:
  - multi-topic subscribe/publish;
  - вызов сервисов и actions;
  - `nav_msgs/OccupancyGrid` → PNG;
  - `PointCloud2` → PNG «вид сверху»;
  - чтение из WiseVision Data Black Box (альтернатива rosbag2 на InfluxDB);
  - MCP-prompts: «topic echo & analyze», «node health check» и др.
- **Безопасность:** **режим read-only, закрытый по умолчанию (fail closed).** Инструменты, меняющие состояние (`ros2_topic_publish`, `ros2_service_call`, `ros2_send_action_goal`…), в этом режиме **не регистрируются**. Тест падает, если новый инструмент не классифицирован в `server/tool_safety.py` [V, README]. Готовый образец политики «наблюдать, но не действовать».
- **Jetson Nano:** нет нативно (ROS 2 + Python ≥3.10).
- **Польза:** приём «преобразуй тяжёлые сенсорные данные в картинку или сводку на сервере», режим read-only и классификация инструментов по опасности.

#### 7. Специализированные MCP-серверы ROS-стека: nav2_mcp_server, unitree-go2-mcp-server, gazebo-mcp
- **nav2_mcp_server:** https://github.com/ajtudela/nav2_mcp_server — Apache-2.0, **85★**, создан 2025-08-31, push 2026-05-22, `requires-python >=3.12,<3.13` [V]. 12 инструментов уровня навыков: `navigate_to_pose`, `follow_waypoints`, `spin_robot`, `backup_robot`, `dock_robot/undock_robot`, `clear_costmaps`, `get_robot_pose`, `get_path(_from_robot)`, `cancel_navigation`, `nav2_lifecycle` [V, README].
- **unitree-go2-mcp-server:** https://github.com/lpigeon/unitree-go2-mcp-server — Apache-2.0, **87★**, создан 2025-05-09, push 2026-06-10. Ubuntu 20.04/22.04, ROS 2 Humble/Foxy, поверх `unitree_ros2` [V]. Автор `lpigeon` — тот же, что у исходного ros-mcp-server (`lpigeon/ros-mcp-server` редиректит на robotmcp) [V, GitHub API].
- **gazebo-mcp** (kvgork, 18★, mock-режим без Gazebo) — подробно в B [H2]. Ещё есть мелкие `sandraschi/gazebo-mcp` (3★) и `mergeos-bounties/gazebo-mcp` (4★); `deepak61296/mavlink-mcp` (5★) — MCP для ArduPilot-дронов [V, поиск GitHub].
- **Польза:** контраст **«навыки высокого уровня» (Nav2) против «сырых примитивов ROS»** (ros-mcp). Это ось эксперимента по токенам и надёжности (ср. C, вывод про узкий высокоуровневый интерфейс). Jetson Nano — нет (ROS 2, Python 3.12).

#### 8. Isaac Sim 6.0: MCP-сервер и Isaac Sim Skills (NVIDIA)
- **Источник:** анонс Isaac Sim 6.0 GA, 2026-06-08: https://github.com/isaac-sim/IsaacSim/discussions/655 [V].
- **Что есть:**
  - MCP-сервер, который «exposes Isaac Sim Extensions, APIs, and documentation to AI coding assistants such as Claude Code and Cursor». Это **помощник разработчика**, а не управление роботом;
  - отдельный **Isaac Sim Skill** даёт живое управление запущенным экземпляром из Claude Code: исполнение кода, изменение USD-сцены, запуск симуляции [V].
- **Сообщество:** omni-mcp/isaac-sim-mcp — MIT, 194★, последний push 2025-04-25, заброшен [V].
- **Железо:** Isaac Sim требует NVIDIA RTX GPU и не работает на macOS [U: общеизвестно, в этой сессии не перепроверял]. **Для ВКР на Mac непригоден**, лучше Gazebo в Docker (см. F).

### K4. Локальный инференс в ROS

#### 9. llama_ros
- **Ссылка:** https://github.com/mgonzs13/llama_ros — C++, MIT, **266★**, создан 2023-04-01, push 2026-10-05, релиз 6.2.0 (2026-10-05), 0 открытых issues [V].
- **Что это:** llama.cpp и llava.cpp как ROS 2-узлы (Humble, Iron, Jazzy, Kilted, Lyrical). Поддерживает:
  - GBNF-грамматики, LoRA «на лету», speculative decoding;
  - **tool calling в ROS-сообщениях**: `ChatToolCall`, `ChatReqTool`, `tool_choice` auto/required/none, `parallel_tool_calls`;
  - action `GenerateChatCompletions` с OpenAI-совместимым интерфейсом;
  - интеграцию с LangChain (`llama_ros_langchain`) [V, README].
- **Демо:** `chatbot_ros` (whisper_ros + llama_ros + YASMIN), `explainable_ros` (RAG по логам робота).
- **CUDA** опционально (`-DGGML_CUDA=ON`).
- **MCP:** нет.
- **Jetson Nano:** нет нативно (ROS 2). Совместимость современного llama.cpp с CUDA 10.2 и GCC 7 не проверял [U]. Объём памяти ограничивает модели примерно до 1–1.5B (см. G/F).
- **Польза:** если в ВКР понадобится локальный fallback или классификатор интентов на Orin или Pi, это готовый ROS-узел с tool calling.

(ros2_nanollm, тоже класс K4, — в карточке 15.)

### K3. Агентные рантаймы и «ОС» роботов

#### 10. OM1 (OpenMind)
- **Ссылка:** https://github.com/OpenMind/OM1 — **Go**, MIT, **2 944★**, создан 2025-01-08, push 2026-09-30 [V]. Последний релиз — v1.0.2-beta.2 (2026-04-29), есть nightly (2026-06-04; файлы в архиве от 2026-09-30) [V].
- **Статья:** OpenMind et al. «A Paragraph is All It Takes: Rich Robot Behaviors from Interacting, Trusted LLMs», arXiv:2412.18588. Четыре LLM общаются через «шину на естественном языке» (WebSocket + ROS 2), цикл слияния данных 1 Гц, правила поведения записаны в Ethereum [V, аннотация].
- **Архитектура:** конфиг `json5` задаёт:
  - **inputs** — датчики: камера, микрофон и др.;
  - **actions** — приводы: «backflip», `move(0.37, 0, 0)`, речь;
  - LLM-провайдеров: OpenAI, xAI, DeepSeek, Anthropic, Meta, Gemini, NearAI, Ollama.

  HAL подключается через USB, serial, ROS 2, CycloneDDS, **Zenoh** (рекомендуется) или WebSocket [V, README]. Python-рантайм объявлен deprecated, основной — Go: «smaller memory footprint for edge devices… single Go binary» [A]. В quickstart нужен `OM_API_KEY` от OpenMind Portal, то есть по умолчанию LLM ходит через их облачный прокси [V, README].
- **MCP:** **MCP-клиент**, отдельный набор MCP-серверов на каждый режим (stdio/sse/http; `internal/mcp/orchestrator.go`). Внешние инструменты — Slack, Notion, погода; железо подключается не через MCP, а через свои плагины [V, `docs/developing/mcp-integration.md`].
- **Метрики [M, чтение `internal/metrics/metrics.go`]:** Prometheus-метрики `om1_llm_latency_seconds`, `om1_vlm_latency_seconds`, `om1_asr_*`, `om1_tts_latency_seconds`, `om1_kb_query/embed_latency_seconds`, `om1_http_upstream_ttfb_seconds`, `om1_quality_live_*`, и **никаких метрик токенов, стоимости, CPU или RAM**. В комплекте Grafana-дашборды.
- **Железо:** «full support» — Nvidia Thor (JetPack 7.0); «limited» — Jetson AGX Orin 64GB (JetPack 6.1); разработка ведётся на Mac Studio M2 Ultra и Mac mini M4 Pro; Raspberry Pi 5 16GB — «should run». Полная автономия заявлена для Unitree Go2, G1, Deep Robotics M20 Pro, LimX Tron с BrainPack. Есть симуляторы Gazebo (Go2) и Isaac Sim (Go2, G1) [V, README].
- **Footprint [M]:** linux-arm64 nightly — `om1` 13.1 МБ (dynamically linked, максимум GLIBC_2.34) + `libzenohc.so` 16.0 МБ (GLIBC до 2.34).
- **Jetson Nano:** **нет нативно** (нужна glibc ≥2.34, а в JetPack 4 — 2.27) [M]. В контейнере Ubuntu 22.04 запуск возможен [U].
- **Польза:** самый близкий по духу к ВКР «агентный рантайм робота»: входы — датчики, действия — приводы, внешняя LLM, единственный бинарник, метрики задержек. Отсутствие токенов, стоимости и ресурсов в метриках — конкретный зазор.

#### 11. DimOS (Dimensional)
- **Ссылка:** https://github.com/dimensionalOS/dimos — Python, **4 619★**, создан 2024-10-19, push 2026-10-06, 699 открытых issues, 47+ контрибьюторов [V]. Файл LICENSE — Apache-2.0 (Dimensional Inc., 2025), API GitHub показывает NOASSERTION [V]. Последний релиз — v0.0.14 (2026-09-19); на PyPI `dimos` 0.0.14 с `requires_python <3.13,>=3.10` [V]. Зависимости: `eclipse-zenoh`, `dimos-lcm`, `open3d-unofficial-arm` (aarch64), `pin` (pinocchio), `opencv-contrib-python`, `reactivex` [V].
- **Что это:** «agentive operating system for physical space», **без ROS**. Модули на Python общаются потоками In/Out. Есть агенты, подписанные на потоки восприятия (лидар, камера), пространственная память (spatio-temporal RAG), навигация и SLAM [V, README]. Статус — **Pre-Release Beta** [V].
- **MCP:** `dimos mcp list-tools` / `dimos mcp call move_to --arg x=0.5`. **Навыки робота отдаются через MCP** [V, README].
- **Железо:** квадропеды (Unitree Go2 — основной), гуманоиды, манипуляторы, дроны. Удалённая телеоперация dimTELE через облачный брокер и API-ключ [V].
- **Jetson Nano:** нет (Python 3.10–3.12, тяжёлые нативные зависимости). Установщик рассчитан на Ubuntu 22.04/24.04 и macOS [V].
- **Польза:** показывает, что «агентная ОС робота» может вообще не опираться на ROS; навыки через MCP; пространственная память.

#### 12. EmbodiedAgents (Automatika Robotics, экосистема EMOS)
- **Ссылка:** https://github.com/automatika-robotics/embodied-agents — Python, MIT, **69★**, создан 2023-09-05, push 2026-10-06, релиз 0.5.1 (2026-02-16), около 4 контрибьюторов [V].
- **Что это:** ROS 2-фреймворк (Humble+), где компоненты LLM, VLM, STT и TTS — ROS-узлы, а их входы и выходы — топики. Например, VLM подписан на `image_raw` и `text0` и публикует в `text1`. Компоненты переключаются между облачной и локальной моделью «на лету» по событиям. Есть иерархическая пространственно-временная память и `enable_local_model=True` для встроенных локальных моделей [V, README]. Заявлено «Optimized for edge devices and NVIDIA Jetson» [A].
- **MCP:** не найден [V, дерево].
- **Имя:** PyPI-пакет `embodied-agents` 0.0.1 (2024) — **другой проект**, с зависимостями xarm-python-sdk, gradio, torch [V].
- **Jetson Nano:** нет нативно (ROS 2 Humble+).
- **Польза:** образец «агент как граф ROS-компонентов с переключением облако/локально» и событийной реконфигурации.

#### 13. ROSClaw (Tongji University / SRIAS)
- **Ссылка:** https://github.com/ros-claw/rosclaw — Python, MIT, **215★**, создан 2026-03-16, push 2026-10-06, релиз v1.3.0 (2026-09-17) [V]. Разработчики, по README: Tongji University и Shanghai Research Institute for Intelligent Autonomous Systems [V].
- **Что это:** «Physical AI Runtime for Embodied Agents», схема «Any Agent → ROSClaw → Any Body».
  - **Сверху (northbound)** подключаются Codex, Claude Code, Hermes, OpenClaw, VLA и собственный Native Agent; **снизу (southbound)** — ROS 2, MCP, симуляторы, SDK вендоров.
  - Каноничный путь действия: intent → body → capability → authority → action, с выдачей `ExecutionReceipt`.
  - Механизмы безопасности: лизинги действий, E-Stop, deadman, политики [V, README].
- **Зрелость:** самоотчёт о статусе прямо в README — **alpha** [V]:
  - «Simulation verified» — UR5e в MuJoCo («Codex CLI black-box runs discovered all 22 MCP tools»), ROS 2 guarded motion в Turtlesim + Gazebo через rosbridge, deadman мобильной базы;
  - «Experimental» — ROS-коннекторы, LeRobot, «hardware MCP»;
  - на реальных роботах — «Revalidation pending».
- **Требования:** pip-форма требует **Node.js ≥22.19** на хосте; ядро на Python 3.11–3.13; есть офлайн-tar со встроенным Node [V].
- **Не путать:** со статьёй Cardenas et al. «ROSClaw: An OpenClaw ROS 2 Framework…», arXiv:2603.26997. Её авторы другие (Cardenas, Arnett, Yeo, Sah, Kim) [V, arXiv API]; разбор в A [S16]. PyPI `rosclaw` 0.0.1 — заглушка 2026-02-24 [V].
- **Рядом:** **quackd** — https://github.com/rokbenko/quackd, Apache-2.0, 250★, создан 2026-08-28. Это CLI «для всех ваших роботов» с адаптерами-манифестами и бейджем «MCP ready». В README описано реальное управление SO-101 через LeRobot под LLM-пилотом. Python 3.11+, для LeRobot — 3.12 [V, README; результаты — A].
- **Jetson Nano:** нет (Node ≥22.19, Python ≥3.11).
- **Польза:** самая проработанная из открытых **модель безопасного исполнения** (полномочия, лизинги, квитанции) и честная таблица зрелости. Отличный образец для раздела «безопасность исполнения».

### K5. Стек NVIDIA Jetson

#### 14. jetson-containers (dusty-nv / NVIDIA)
- **Ссылка:** https://github.com/dusty-nv/jetson-containers — **4 877★**, создан 2020-04-29, push 2026-08-10. GitHub API показывает лицензию NOASSERTION, а `LICENSE.md` — MIT-подобный текст с копирайтом NVIDIA [V].
- **Что это:** модульная сборка CUDA-контейнеров для Jetson. Группы пакетов [V, README]:
  - ML, LLM (vLLM, SGLang, MLC, Ollama, llama.cpp), VLM, ViT (NanoOWL, NanoSAM);
  - Robotics (ROS, LeRobot, OpenVLA, openpi), Simulation (Isaac Sim, Genesis, MuJoCo);
  - Home/IoT (homeassistant-core, wyoming-whisper/piper/openwakeword);
  - **Agents: OpenClaw, OpenFang, ZeroClaw**, а в дереве ещё `hermes-agent` и `mempalace`.
- **Пакет openclaw:** `npm install -g openclaw@latest` поверх `cudastack:standard`, `python`, `nodejs`. ZeroClaw и OpenFang собираются через `cargo build --release` [V, `packages/agents/*`].
- **Поддержка:** «Only Tested and supported Jetpack 6.2 (Cuda 12.6) and JetPack 7 (CUDA 13.x)». Старые L4T — в ветках `L4T-R32.4.2 … L4T-R32.7.1` и `legacy` [V, список веток]. Для Nano есть готовые ROS-образы `dustynv/ros:{melodic,noetic,foxy,galactic,humble,iron}-*-l4t-r32.7.1`; последнее обновление — 2023-12-06 [V, Docker Hub API].
- **Jetson Nano:** только legacy-образы 2023 года. Агентные пакеты (OpenClaw и др.) рассчитаны на JetPack 6/7.
- **Польза:** показывает, что NVIDIA **официально упаковывает claw-агентов как edge-нагрузку**. Для Nano — источник готовых ROS-контейнеров.

#### 15. NanoLLM, Agent Studio, ros2_nanollm, NanoOWL/NanoSAM (Jetson AI Lab, линия 2024 года)
- **NanoLLM:** https://github.com/dusty-nv/NanoLLM — MIT, **381★**, создан 2024-03-31, **последний push 2024-10-18**. Последний релиз — 24.7 (контейнеры `dustynv/nano_llm:24.7-r35.4.1` / `-r36.2.0`) [V].
  - Документация: «Currently supported on Jetson Orin and JetPack 5/6».
  - Есть function calling (`bot_function`, `BotFunctions`), Plugin API, агенты ChatAgent, VoiceChat, WebChat, VideoStream, VideoQuery [V, dusty-nv.github.io/NanoLLM].
  - PyPI `nanollm` 0.1 (2023) — **чужой пакет**.
- **Agent Studio** (на базе NanoLLM): «sandbox for connecting multimodal LLMs, speech and vision transformers, vector databases, prompt templating, and **function calling to live sensors and I/O**» [V].
  - Требования: Jetson AGX Orin 64/32 ГБ, Orin NX 16 ГБ или Orin Nano 8 ГБ; JetPack 6 (L4T r36.x); **22 ГБ под контейнер** и больше 5 ГБ под модели [V].
  - Плагины — классы с `process()`. Docstring и аннотации типов автоматически становятся описанием инструмента для LLM [V].
  - Страница в архиве: «Archive Notice: This documentation is deprecated and no longer maintained» [V].
- **ros2_nanollm:** https://github.com/NVIDIA-AI-IOT/ros2_nanollm — MIT, **198★**, последний push 2024-08-16. ROS 2-узлы для описания сцены; по умолчанию модель `Efficient-Large-Model/Llama-3-VILA1.5-8B`, бэкенд MLC, `q4f16_ft` [V].
- **NanoOWL** (525★, Apache-2.0, последний push 2025-02-06) и **NanoSAM** (919★, 2023-11-20) — открытословарная детекция и сегментация на TensorRT для **Orin** [V]. Пригодны как «инструмент-камера» на борту Orin.
- **Live VLM WebUI:** https://github.com/NVIDIA-AI-IOT/live-vlm-webui — Apache-2.0, 464★, создан 2025-11-04. PyPI `live-vlm-webui` 0.4.0, Python ≥3.10 [V]. WebRTC-поток с камеры в любой VLM (Ollama, vLLM, облако) с живыми метриками: задержка инференса, GPU и VRAM (на Jetson через jtop), CPU, RAM [V, README]. Это не агент, но **готовый образец онлайн-мониторинга ресурсов и задержки**.
- **Jetson Nano:** нет — вся линейка рассчитана на Orin (JetPack 5/6).
- **Польза:** дизайн «плагин = инструмент с автоописанием из docstring». И исторический факт: NVIDIA переключилась с собственного агентного фреймворка на OpenClaw/NemoClaw (карточка 16).

#### 16. OpenClaw и NemoClaw на Jetson (туториалы Jetson AI Lab, 2026)
- **Источник:** репозиторий сайта https://github.com/NVIDIA-AI-IOT/jetson-ai-lab — 209★, push 2026-10-05. Файлы `src/content/tutorials/applications/openclaw.md` и `nemoclaw.md` [V].
- **OpenClaw, путь A** — Jetson Orin Nano 8 ГБ / Orin Nano Super:
  - стек Ollama + **qwen3.5:2b**, контекст 16 384, `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_FLASH_ATTENTION=1`, своп 16 ГБ «recommended»;
  - OpenClaw с `tools.profile: "minimal"` — «to keep prompt overhead and attack surface lower»;
  - Node.js 22+.
- **Измерения авторов туториала [A]:**

  | Метрика | Значение |
  |---|---|
  | Модель | Qwen 3.5 2B Q8_0 |
  | Память | 4.6 ГБ, 100 % GPU |
  | Генерация | около 20 ток/с |
  | Обработка промпта | около 580 ток/с |
  | Первый ответ, холодный старт | около 15 с |
  | Первый ответ, тёплый | около 3 с |
  | Tool calling | работает (структурированные `tool_calls`) |

- **OpenClaw, путь B** — AGX Orin / AGX Thor: vLLM + Nemotron 3 Nano 30B-A3B [V].
- **NemoClaw:** https://github.com/NVIDIA/NemoClaw — TypeScript, Apache-2.0, **22 666★**, создан 2026-03-15 [V].
  - Это «reference stack for running supported AI agents more safely inside NVIDIA OpenShell sandboxes»: OpenClaw, Hermes, LangChain Deep Agents, плюс managed inference и сетевые политики [V].
  - Туториал на Jetson Thor/Orin: модель Nemotron3 Nano 30B «needs at least 32 GB»; для Orin Nano советуют модели около 4B [V].
- **Jetson Nano:** нет. Node ≥22, а сейчас ≥24.16; CUDA 12+; минимальная плата в туториале — Orin Nano 8 ГБ.
- **Польза:** единственные найденные **измеренные числа claw-агента с локальной LLM на Jetson**. Хорошая точка сравнения «локальная 2B против внешней mini-модели» по задержке и памяти. Разбор OpenClaw как продукта — в A.

#### 17. ReMEmbR (NVIDIA-AI-IOT)
- **Ссылка:** https://github.com/NVIDIA-AI-IOT/remembr — Python, **360★**, создан 2024-08-28, push 2026-08-24 [V]. **Лицензия NVIDIA License, только некоммерческое использование** («research or evaluation purposes only») [V, LICENSE.md]. arXiv:2409.13682 [V]; ICRA 2025 и детали развёртывания (Jetson Orin 32GB + GPT-4o) — в D [F4].
- **Архитектура:** VILA делает подписи к видео, они вместе с временем и позой хранятся в векторной БД Milvus. LLM-агент (Ollama или API) отвечает на пространственно-временные вопросы и ведёт робота к цели. Демо — ROS 2 bag в Gradio и живой Nova Carter на Isaac ROS [V, README].
- **Инструменты:** память (поиск по тексту, времени, позиции) и навигация.
- **MCP:** нет.
- **Jetson Nano:** нет (VILA + Milvus на Orin).
- **Польза:** «журнал оборудования» как долговременный контекст (см. D).

#### 18. Модели физического ИИ: Isaac GR00T N1.7, Cosmos Reason 2, LeRobot (контекст; это не агенты с tool calling)
- **Isaac GR00T:** https://github.com/NVIDIA/Isaac-GR00T — **8 161★**, Apache-2.0, создан 2025-03-11, push 2026-08-20 [V].
  - **N1.7 GA:** открытая VLA для гуманоидов; бэкбон **Cosmos-Reason2-2B** (архитектура Qwen3-VL) и diffusion transformer, который денойзит непрерывные действия; «fully commercially licensable under Apache 2.0»; экспорт в ONNX/TensorRT [V, README].
  - На Jetson AI Lab есть туториал «GR00T N1.7 on Thor» [V, дерево сайта].
- **Cosmos Reason 2:** https://github.com/nvidia-cosmos/cosmos-reason2 — 455★ [V].
  - Модели 2B и 8B (2025-12-19), 32B (2026-04-29).
  - Таблица памяти в README: 2B — 24 ГБ, 8B — 32 ГБ; для Jetson AGX и DGX Spark нужна CUDA 13.0 [V].
  - Позиционируется как reasoning-VLM «for physical AI and robotics… robots and vision AI agents».
- **LeRobot:** https://github.com/huggingface/lerobot — **27 963★**, Apache-2.0 [V].
  - PyPI `lerobot` 0.6.1, Python ≥3.12, `torch>=2.7,<2.12`; **237 322 загрузки за месяц** [V].
  - Единый класс `Robot`: SO100/101, LeKiwi, Koch, HopeJR, Reachy2, Unitree G1 и др.
  - Политики Pi0/Pi0.5, GR00T N1.7, SmolVLA [V, README].
- **Роль для ВКР:** VLA работает **ниже** агента. Агент на внешней LLM может вызывать навык «выполни политику X», как в quackd с SO-101 через LeRobot. Сама VLA к классу «LLM-агент с инструментами» не относится. Jetson Nano — нет (PyTorch ≥2.7 и CUDA 12/13 недоступны на JetPack 4).

### K6. IoT-устройства и edge-рантаймы

#### 19. xiaozhi-esp32 (MCP на устройстве)
- **Ссылка:** https://github.com/78/xiaozhi-esp32 — C++, MIT, **30 445★**, создан 2024-08-31, push 2026-10-02 [V]. Протокол разобран в B [H5].
- **Добавлю [V, README]:**
  - ESP-IDF ≥6.0.1, 138 каталогов плат, 171 вариант сборки;
  - «Device-side MCP for device control (Speaker, LED, Servo, GPIO, etc.)» и «Cloud-side MCP to extend large model capabilities»;
  - по умолчанию прошивка подключена к облаку xiaozhi.me (бесплатная модель Qwen realtime);
  - есть самостоятельно разворачиваемые бэкенды, например `xinnan-tech/xiaozhi-esp32-server` (10 737★, создан 2025-02-02).
- Документ `docs/mcp-protocol.md` сам предупреждает: «This document was AI-assisted; when implementing a backend, always cross-check the details against the code» [V].
- **Где LLM:** облако. **Инструменты = датчики и приводы:** да, прямо на МК.
- **Jetson Nano:** напрямую неприменимо (это прошивка МК), но Nano мог бы держать бэкенд-сервер [U].
- **Польза:** самый массовый пример топологии «устройство = MCP-сервер, агент = облачный клиент».

#### 20. Edge Agents (ForestHub)
- **Ссылка:** https://github.com/ForestHubAI/edge-agents — TypeScript (CLI и визуальный редактор) + Go (движок), **AGPL-3.0**, **105★**, создан 2026-05-19, push 2026-08-24, релизов нет [V].
- **Что это:** «The 30 MB open-source edge AI agent runtime. Run AI agents offline, on Linux» [A].
  - Workflow описывается в JSON, редактор визуальный (`npm i -g @foresthubai/workflow-cli`).
  - **GPIO, UART, MQTT — узлы первого класса**; OPC-UA и Modbus — в планах [V, README].
  - LLM: Anthropic, OpenAI, Gemini, Mistral, а также локальный SLM-провайдер для llama.cpp, vLLM, Ollama или любого OpenAI-совместимого эндпоинта [V].
- **Footprint [V, Dockerfile]:** движок собирается с `CGO_ENABLED=0` в образ `gcr.io/distroless/static-debian12:nonroot`, то есть статический бинарник без glibc.
- **Платформы:** «Raspberry Pi 5 · Jetson Orin Nano · STM32MP25 · Bosch Rexroth ctrlX CORE» [A].
- **MCP:** в README не упоминается [V].
- **Jetson Nano:** **вероятно да.** Статический Go-бинарник в Docker на JetPack 4.6 [U: не запускал]; GPIO из контейнера требует проброса устройств.
- **Польза:** ближайший по классу «тонкий edge-рантайм с GPIO и внешней LLM» после PicoClaw (A). Это workflow-движок, а не ReAct-агент. AGPL важна, если брать код.

#### 21. Home Assistant (MCP-сервер и MCP-клиент)
- **Ссылки:** https://github.com/home-assistant/core — Python, Apache-2.0, **91 275★** [V]. Подробно — в C [15] и B [H3].
- **Добавлю [V, документация]:** кроме MCP Server (HA 2025.2, Streamable HTTP, Assist API и exposed entities; Tools, Prompts, Resources; без Sampling и Notifications) есть отдельная интеграция **MCP client** (тоже 2025.2, SSE). Она отдаёт инструменты внешних MCP-серверов разговорным агентам HA (OpenAI, Ollama и др.). То есть HA реализует **обе топологии сразу**.
- В jetson-containers есть пакет `homeassistant-core` [V].
- **Jetson Nano:** не проверял [U].
- **Польза:** промышленная базовая линия «умный дом + внешняя LLM + MCP» (см. C).

### K7. Промышленный IoT

#### 22. AWS IoT Greengrass: «edge AI agent» на Rust + Bedrock AgentCore (образец)
- **Источники:**
  - блог AWS IoT «Build edge AI agents with the AWS IoT Greengrass Component SDK for Rust», **2026-09-01**, авторы Karanjkar, Gawande, Chaudhari [V];
  - код https://github.com/aws-samples/sample-greengrass-rust-edge-ai-agent — Rust, MIT-0, 1★, создан 2026-07-23 [V].
- **Архитектура [V, блог]:**
  - **на устройстве** — шлюз насосной станции: компонент Greengrass на Rust с квантованной ONNX-моделью (1D-CNN с Squeeze-and-Excitation, около 25 тыс. параметров, около 23 КБ) для обнаружения аномалий во временных рядах датчиков и офлайн-очередью FIFO на 1 000 сообщений;
  - **в облаке** — IoT Core (MQTT) → Lambda → **Amazon Bedrock AgentCore со Strands Agents**. Агент делает анализ первопричин (RCA) по истории в DynamoDB и отправляет ответ обратно на устройство через MQTT.
  - **На edge нет LLM и нет MCP.**
- **Ресурсы [A]:**
  - целевое железо — ARM Cortex-A53, 256 МБ RAM, лимит компонента 64 МБ;
  - SDK-рантайм — меньше 0.5 МБ;
  - **пиковый RSS 22 МБ** (до 35 МБ с продуктовой моделью на 12 МБ);
  - инференс — меньше 50 мс.
- **Также у AWS** (не разбирал подробно): «Architecting offline-first generative AI applications for edge deployments» (Architecture Blog) и «Emerging Architecture Patterns for Integrating IoT and generative AI on AWS» (IoT Blog) [U, только поиск].
- **Jetson Nano:** вероятно да. Cortex-A57 мощнее A53, Rust-бинарник. Но требования Greengrass nucleus к ОС не проверял [U].
- **Польза:** чистый пример паттерна **«дешёвый локальный фильтр → эскалация в облачного агента»**. Это прямой рычаг экономии токенов за сутки (ср. G, «не звать LLM на каждый тик»).

#### 23. Microsoft: Agentic Retrieval in Foundry Local (бывший Edge RAG) и Azure IoT Operations
- **Источник:** Microsoft Learn «Agentic Retrieval and Agents and Tools with Foundry Local Overview», обновлено 2026-07-23 [V].
- **Что это [V]:**
  - статус **PREVIEW**;
  - расширение Azure Arc для Kubernetes, «supported and validated on Azure Arc-enabled Kubernetes on Azure Local»; возможны отключённые от сети (disconnected) операции;
  - агентный слой со **встроенным MCP-сервером на 6 поисковых инструментов** и подключением внешних MCP-серверов. Пример из документации: «connect agents to … SCADA systems, maintenance logs… via MCP servers»;
  - языковая модель — эндпоинт Foundry Local на том же кластере или BYOM с OpenAI-совместимым API;
  - эмбеддинги BGE-M3 и CLIP ViT-L/14 «running locally on two GPUs».
- **Azure IoT Operations 2603** (апрель 2026) — платформа данных на edge (Arc, Kubernetes). Агентная часть описана только во вторичных источниках (redmondmag вернул 403) [U].
- **Jetson Nano:** нет (кластер Azure Local с GPU).
- **Польза:** показывает, что крупные вендоры решают «контекст оборудования и данных» через **MCP-источники знаний**, причём on-prem и офлайн. Но это класс серверных стоек, а не одноплатников.

#### 24. Siemens Industrial Copilot for Operations
- **Источники:** пресс-релизы Siemens [V]:
  - 2024-11-12 (thyssenkrupp): **«on-premises hardware-software bundle»** на **Simatic IPC 1047E**, ПО на **NVIDIA NIM / NVIDIA AI Enterprise**; конфигурация «doesn't require an Internet connection and stores data on local hardware devices»;
  - 2025-05-12: экосистема ИИ-агентов с «sophisticated orchestrator», Operations Copilot для цеха — «by end of 2025»;
  - CES 2025: интеграция с Industrial Edge, «AI tasks run as close as possible to machines».
- **Что делает:** объясняет коды ошибок станка, предлагает решения по документации, руководствам и спискам запчастей, использует данные процесса и датчиков через IIoT и edge (по вторичным источникам) [U].
- **Технические детали** — модель, MCP, метрики — **не опубликованы**. Текущий статус GA на октябрь 2026 не проверял [U].
- **Jetson Nano:** нет (IPC с GPU, закрытый продукт).
- **Польза:** индустриальный аргумент для главы 1: спрос на офлайн и on-prem агентов у станка есть. Но индустрия решает это «тяжёлым» IPC с GPU, а не одноплатником + внешней LLM.

### K8. Научный фон

#### 25. SayCan, Code as Policies, ChatGPT for Robotics (кратко; подробно в C)
- **SayCan:** arXiv:2204.01691 (CoRL 2022, PMLR 205) [V, arXiv]. Ноутбук `SayCan-Robot-Pick-Place.ipynb` лежит в google-research/google-research/saycan [V].
- **Code as Policies:** arXiv:2209.07753; ICRA 2023, DOI 10.1109/ICRA48891.2023.10160591 [V, Crossref]. Ноутбуки — в google-research/google-research/code_as_policies [V].
- **ChatGPT for Robotics:** Vemprala et al., arXiv:2306.17582; *IEEE Access* 2024, DOI 10.1109/ACCESS.2024.3387941 [V, Crossref]. Репозиторий microsoft/PromptCraft-Robotics: MIT, 2 117★, последний push 2024-01-20 [V].
- **Роль:** три исторических прообраза. SayCan — библиотека навыков с проверкой выполнимости (affordance). Code as Policies — код как промежуточное представление. ChatGPT for Robotics — высокоуровневая библиотека функций. Все современные системы из карточек 1–24 — инженерные наследники этих схем, а MCP стандартизовал «библиотеку функций».

---

## 4. Сводная таблица

Обозначения: «обл.» — облачная LLM по API; «борт» — LLM на устройстве; «опер.» — LLM на машине оператора. Колонка Nano: «да» — нативно; «разнес.» — на Nano только часть системы; «конт.» — только в контейнере с новым userspace [U]; «нет» — практически нет.

| # | Система | Класс | Где LLM | Инструменты = датчики/приводы | MCP | Железо в README/доках | Зрелость (★, последний push) | Jetson Nano |
|---|---|---|---|---|---|---|---|---|
| 1 | ROSA | K1 | обл./опер. (Ollama опц.) | косвенно, через ROS CLI | нет | Spot, TurtleSim | стабильна, 1 653★, 2026-03 | конт. |
| 2 | RAI | K1 | обл./борт (Ollama; AMD NPU в демо) | да (ROS 2) | нет | ROSbot XL, Franka (сим.), RB-KAIROS | активна, 599★, 2026-09 | нет |
| 3a | ROS-LLM (Huawei) | K1 | удалённый сервер | да (actions/services) | нет | UR5 | статья NMI 2026, код ROS 1 | конт. (Noetic) |
| 3b | ROS-LLM (Auromix) | K1 | обл. | да | нет | TurtleSim | заброшен, 830★, 2023-07 | конт. |
| 4 | ros2ai | K1 | обл./Ollama | косвенно (CLI) | нет | любой ROS 2 | поддерживается, 331★, 2026-08 | конт. |
| 5 | ros-mcp-server | K2 | клиент (обл.) | да | **сервер** | Go2 (Isaac), промышленный робот, Nav2/MoveIt | активен, 1 486★, 2026-10 | **разнес.** (rosbridge на Melodic) |
| 6 | ros2_mcp (WiseVision) | K2 | клиент (обл.) | да; read-only режим | **сервер** | ROS 2 Humble/Jazzy | активен, 89★, 2026-09 | нет |
| 7 | nav2_mcp / go2-mcp / gazebo-mcp | K2 | клиент (обл.) | да (навыки Nav2, Go2) | **сервер** | Nav2-роботы, Unitree Go2 | малые проекты, 85/87/18★ | нет |
| 8 | Isaac Sim 6.0 MCP + Skills | K2 | клиент (обл.) | MCP — документация; Skill — управление симулятором | **сервер** (документация) | RTX GPU | GA 2026-06-08 | нет |
| 9 | llama_ros | K4 | борт | — (это LLM-узел) | нет | любой ROS 2 + CUDA опц. | активен, 266★, 2026-10 | нет/[U] |
| 10 | OM1 | K3 | обл. (прокси OpenMind) / Ollama | да (inputs/actions) | **клиент** | Thor, AGX Orin, Go2, G1 | beta, 2 944★, 2026-09 | нет (GLIBC 2.34 [M]) |
| 11 | DimOS | K3 | обл./лок. | да (потоки) | **сервер** навыков | Go2, гуманоиды, дроны | beta, 4 619★, 2026-10 | нет |
| 12 | EmbodiedAgents | K3 | обл./борт | да (ROS-топики) | нет | Jetson (заявл.) | 0.5.1, 69★, 2026-10 | нет |
| 13 | ROSClaw (Tongji) | K3 | клиент-агенты сверху | да (тела/драйверы) | **сервер** (22 инстр. в тесте) | UR5e (MuJoCo), RH56, RealSense | alpha, 215★, 2026-10 | нет (Node ≥22) |
| 14 | jetson-containers | K5 | борт | — | — | Jetson (JP 6.2/7) | активен, 4 877★, 2026-08 | legacy-образы 2023 |
| 15 | NanoLLM / Agent Studio / ros2_nanollm | K5 | борт | да (плагины, функции, I/O) | нет | Orin | заморожены, 381/198★, 2024 | нет |
| 16 | OpenClaw/NemoClaw on Jetson | K5 | борт (Ollama/vLLM) | общие инструменты ОС; железо — через skills [U] | через OpenClaw | Orin Nano 8 ГБ, AGX Orin, Thor | туториалы 2026; NemoClaw 22 666★ | нет |
| 17 | ReMEmbR | K5 | борт (VILA) + LLM | память + навигация | нет | Nova Carter, Orin | исслед., 360★, некоммерч. | нет |
| 18 | GR00T / Cosmos Reason 2 / LeRobot | K8 | борт/GPU | VLA = контроллер | нет | Thor, GPU; SO-101 и др. | активны, 8 161 / 455 / 27 963★ | нет |
| 19 | xiaozhi-esp32 | K6 | обл. | да (GPIO, servo, LED, камера) | **сервер на МК** | 171 вариант ESP32 | массовый, 30 445★ | н/п (МК) |
| 20 | Edge Agents (ForestHub) | K6 | обл./лок. SLM | да (GPIO, UART, MQTT) | нет | Pi 5, Orin Nano, STM32MP25, ctrlX | молодой, 105★, 2026-08 | вероятно да [U] |
| 21 | Home Assistant | K6 | обл./лок. | да (exposed entities) | **сервер + клиент** | SBC, ПК | промышленный, 91 275★ | [U] |
| 22 | AWS Greengrass edge AI agent | K7 | обл. (AgentCore); борт — ONNX | телеметрия → RCA | нет | Cortex-A53, 256 МБ | образец, 2026-09 | вероятно да [U] |
| 23 | Azure Agentic Retrieval (Foundry Local) | K7 | on-prem GPU | документы/данные; MCP-источники | **сервер + клиент** | Azure Local, 2 GPU | preview | нет |
| 24 | Siemens Industrial Copilot for Ops | K7 | on-prem IPC (NIM) или обл. | данные станка, документы | не опубликовано | Simatic IPC 1047E | коммерческий, закрытый | нет |
| 25 | SayCan / CaP / ChatGPT for Robotics | фон | обл. | навыки / API / функции | — | роботы Google, AirSim | 2022–2024 | н/п |

---

## 5. Архитектурные паттерны и куда ложится ВКР

| Паттерн | Суть | Системы | Плюсы | Минусы для одноплатника |
|---|---|---|---|---|
| P1. Интроспектор ROS | LLM вызывает ROS CLI/API: список, echo, вызов | ROSA, ros2ai | универсален, без настройки под робота | много токенов на обнаружение, нужен полный ROS на хосте агента |
| P2. MCP-мост к ROS (сервер на роботе или рядом) | агент снаружи, сервер отдаёт примитивы и метаданные | ros-mcp-server, ros2_mcp | любой MCP-клиент, разнесение по хостам (rosbridge) | сырые примитивы, текстовый контекст железа, тяжёлые зависимости (opencv) |
| P3. Навыки высокого уровня через MCP | инструменты — навыки (`navigate_to_pose`, `move_to`) | nav2_mcp, DimOS, ROSClaw | короче промпт, безопаснее | нужен готовый стек навыков (Nav2 и т.п.) |
| P4. Рантайм с плагинами входов и действий + MCP-клиент | конфиг связывает датчики, LLM и приводы; MCP — для внешних сервисов | OM1, EmbodiedAgents, Agent Studio | событийность, режимы, метрики задержки | тяжёлые зависимости и новые glibc |
| P5. Устройство — MCP-сервер | МК или плата объявляет свои инструменты | xiaozhi-esp32, HA MCP Server | минимальная логика на устройстве | агент и LLM нужны снаружи |
| P6. Локальный фильтр → эскалация к облачному агенту | маленькая модель на edge решает, когда звать агента | AWS-образец; ср. G (NoScope, Reducto) | огромная экономия токенов во времени | нужна обученная локальная модель |
| P7. Локальная LLM на борту | LLM на GPU или NPU борта | OpenClaw/NemoClaw on Jetson, llama_ros, RAI-AMD, Siemens on-prem | офлайн, приватность | нужна плата класса Orin, 8 ГБ+ (для Nano неприменимо) |
| P8. Безопасное исполнение | полномочия, лизинги, квитанции, read-only, blacklist | ROSClaw, ros2_mcp, ROSA, NemoClaw/OpenShell | контроль действий LLM | сложность |

**Позиция ВКР.** Постановка руководителя («внешняя LLM, контекст пользователя и оборудования, MCP-серверы оборудования, метрики во времени») — это **P5 + P2/P3 с элементами P6 и P8**: Nano или Pi как MCP-сервер оборудования плюс тонкий агент, внешняя mini-модель через RouterAI, локальная фильтрация событий, безопасность вызовов приводов. Полного аналога среди 25 позиций нет:
- OM1 ближе всего по рантайму, но не держит токены, стоимость и ресурсы;
- ros-mcp-server ближе всего по MCP, но у него нет агента и метрик;
- AWS-образец ближе всего по паттерну экономии, но в нём нет MCP и диалога с пользователем.

---

## 6. Что существующие системы измеряют «во времени»

| Система | Токены | Стоимость | Задержка | CPU/RAM/GPU | Энергия | Временной ряд |
|---|---|---|---|---|---|---|
| ROSA | на вызов (только OpenAI/Azure) [V] | нет | нет | нет | нет | нет |
| RAI | через Langfuse-трассы [U] | через Langfuse [U] | трассы | нет | нет | трассы, не суточный профиль |
| OM1 | **нет** [M] | **нет** [M] | Prometheus: LLM, VLM, ASR, TTS, KB, HTTP TTFB [M] | **нет** [M] | нет | да (Prometheus/Grafana) |
| Live VLM WebUI | нет [U] | нет | задержка инференса [V] | GPU, VRAM, CPU, RAM (jtop) [V] | нет | живой дашборд |
| AWS-образец | — (на edge нет LLM) | нет | инференс <50 мс [A] | RSS 22 МБ [A] | нет | нет |
| OpenClaw-on-Jetson (туториал) | нет | нет | TTFT холодный/тёплый, ток/с [A] | память 4.6 ГБ [A] | нет | нет |
| jetson_stats (jtop) | — | — | — | CPU, GPU, RAM, температура, мощность (INA3221) | мощность платы | да (утилита) |

Итог: **ни одна агентная система не даёт суточного профиля «токены + стоимость + задержка + ресурсы хоста + энергия»**. Ближе всех OM1 (временные ряды задержек) и RAI (трассы через Langfuse). Монитор, который сводит это вместе (ср. E и `monitor/` в проекте), — самостоятельный вклад ВКР.

---

## 7. Выводы

1. **Карта заполнена плотно, но фрагментами.** Для ROS есть зрелые агенты (ROSA, RAI) и MCP-мосты (ros-mcp-server); есть агентные ОС роботов (OM1, DimOS, ROSClaw); у NVIDIA — свой бортовой стек; для IoT — MCP на устройствах (xiaozhi, HA). Ни одна система не закрывает одновременно (а) тонкий агент на слабом одноплатнике, (б) структурированный контекст оборудования через MCP, (в) суточные метрики ресурсов и стоимости.
2. **Jetson Nano — проблемная платформа для агента, но нормальная для роли «оборудования».** JetPack 4 закончился на 4.6.6 (Ubuntu 18.04, ядро 4.9, CUDA 10.2). Современные Python- и Node-агенты нативно не встанут. Работают ROS Melodic + rosbridge и статические бинарники (PicoClaw, ZeroClaw-musl, Go с `CGO_ENABLED=0`). Если на кафедре Nano, практичная схема такая: **Nano = датчики, приводы и лёгкий MCP- или rosbridge-сервер; агент — статический бинарник на Nano или процесс на Mac; LLM — внешняя через RouterAI.** Для полного Python- и ROS 2-стека лучше Raspberry Pi 5 или Orin Nano (см. F).
3. **MCP — де-факто стандарт интерфейса к железу 2025–2026 годов.** Это подтверждают ros-mcp-server, ros2_mcp, nav2_mcp, xiaozhi, HA, DimOS, ROSClaw, Isaac Sim 6.0 и Azure. Для ВКР это аргумент, что выбор MCP соответствует отрасли. Новизна — не в факте использования MCP, а в **содержании контекста оборудования** и **измерении его стоимости во времени**.
4. **Безопасность исполнения уже имеет готовые образцы:** read-only режим, закрытый по умолчанию (ros2_mcp), лизинги, квитанции и E-Stop (ROSClaw), blacklist (ROSA), минимальный профиль инструментов и песочница (OpenClaw-on-Jetson, NemoClaw/OpenShell). Их стоит заимствовать, а не изобретать.
5. **Индустрия (Siemens, Azure) решает офлайн-агентов «тяжёлыми» on-prem GPU.** AWS показывает альтернативу: маленькая модель на edge плюс эскалация к облачному агенту. Для одноплатника и внешней LLM паттерн AWS ближе. Его стоит взять как базовую стратегию экономии вызовов LLM.
6. **Кандидаты в базовые линии для сравнения в ВКР** (в дополнение к PicoClaw и NanoClaw из A):
   - ros-mcp-server + внешний клиент — эталон моста «MCP → ROS» для сценария с Gazebo;
   - ROSA — эталон ROS-агента на LangChain, с подсчётом токенов;
   - OM1 — эталон рантайма с метриками задержек. На Nano он не запускается [M], поэтому только на Mac или в Docker.

## 8. Открытые вопросы и что не проверено

- **Какой именно Jetson на кафедре** (Nano 4GB, Nano 2GB или вообще Orin Nano), какая версия L4T и сколько памяти — проверить на месте (раздел 1.1).
- Запуск статических бинарников (PicoClaw, ZeroClaw-musl, Edge Agents) на ядре 4.9 L4T R32 — вывод сделан по типу линковки [M] без запуска на плате [U].
- Работоспособность официальных Node 22/24 в контейнере на ядре 4.9 (требование ≥4.18 с оговоркой «may work») [U].
- Python в образах `dustynv/ros:humble-*-l4t-r32.7.1` (вероятно 3.6) [U].
- Есть ли в RAI или ROSA MCP в ветках или PR: поиск по коду GitHub неполон [U].
- Содержимое `docs/setup/tracing.md` RAI (какие метрики токенов и стоимости реально пишутся) — не читал [U].
- Siemens Operations Copilot: GA-статус на октябрь 2026, модели, наличие MCP — не опубликовано или не найдено [U].
- Azure IoT Operations 2603 — агентные функции подтверждены только вторичными источниками [U].
- Совместимость RouterAI с OpenAI-клиентами ROSA, ros2ai и OM1 — вывод по тому, что они используют OpenAI-совместимые клиенты [U]; см. F.
- pypistats для `dimos` и `live-vlm-webui` не получен (429 rate limit).

## 9. Источники (все открыты 2026-10-06)

**Репозитории (GitHub API + README):**
- nasa-jpl/rosa; RobotecAI/rai; RobotecAI/agentic-mobile-manipulator; huawei-noah/HEBO (ROSLLM); Auromix/ROS-LLM; fujitatomoya/ros2ai;
- robotmcp/ros-mcp-server; wise-vision/ros2_mcp; ajtudela/nav2_mcp_server; lpigeon/unitree-go2-mcp-server; kvgork/gazebo-mcp; omni-mcp/isaac-sim-mcp;
- mgonzs13/llama_ros; NVIDIA-AI-IOT/ros2_nanollm;
- OpenMind/OM1; dimensionalOS/dimos; automatika-robotics/embodied-agents; ros-claw/rosclaw; rokbenko/quackd;
- dusty-nv/jetson-containers; dusty-nv/NanoLLM; NVIDIA-AI-IOT/nanoowl; NVIDIA-AI-IOT/nanosam; NVIDIA-AI-IOT/live-vlm-webui; NVIDIA-AI-IOT/jetson-ai-lab; NVIDIA/NemoClaw; NVIDIA-AI-IOT/remembr; NVIDIA/Isaac-GR00T; nvidia-cosmos/cosmos-reason2; huggingface/lerobot; rbonghi/jetson_stats;
- 78/xiaozhi-esp32; xinnan-tech/xiaozhi-esp32-server; ForestHubAI/edge-agents; home-assistant/core;
- aws-samples/sample-greengrass-rust-edge-ai-agent; microsoft/PromptCraft-Robotics; google-research/google-research (saycan, code_as_policies).

**Документация и страницы:**
- NVIDIA: https://developer.nvidia.com/jetpack-sdk-466 ; https://developer.nvidia.com/embedded/linux-tegra-r3276 ; https://developer.nvidia.com/embedded/lifecycle ; https://forums.developer.nvidia.com/t/jetson-nano-developer-kit-eol/276730 ; https://dusty-nv.github.io/NanoLLM/ ; архив Agent Studio (`public/archive/site/agent_studio.html` в NVIDIA-AI-IOT/jetson-ai-lab); туториалы `openclaw.md`, `nemoclaw.md`, `jetson-agent-skills.mdx` там же.
- Isaac Sim 6.0 GA: https://github.com/isaac-sim/IsaacSim/discussions/655
- Node.js BUILDING.md (v18.x, v22.x): требования glibc и ядра.
- rosdistro `melodic/distribution.yaml` (rosbridge_suite 0.11.16-2); REP-2000 (Jazzy: Ubuntu Noble arm64 Tier 1, macOS Tier 3).
- Docker Hub API: `dustynv/ros` tags `*-l4t-r32.7.1`.
- Home Assistant: https://www.home-assistant.io/integrations/mcp_server/ ; https://www.home-assistant.io/integrations/mcp/
- AWS: https://aws.amazon.com/blogs/iot/build-edge-ai-agents-with-the-aws-iot-greengrass-component-sdk-for-rust/
- Microsoft Learn: https://learn.microsoft.com/en-us/azure/azure-arc/agents-tools-foundry-local/overview (редирект с edge-rag/overview)
- Siemens: https://press.siemens.com/global/en/pressrelease/siemens-industrial-copilot-expanded-adopted-thyssenkrupp ; https://press.siemens.com/global/en/pressrelease/siemens-introduces-ai-agents-industrial-automation ; https://press.siemens.com/global/en/pressrelease/siemens-unveils-breakthrough-innovations-industrial-ai-and-digital-twin-technology-ces

**Пакеты:** PyPI JSON — ros-mcp, jpl-rosa, rai-core, lerobot, dimos, live-vlm-webui, rosclaw, nanollm, embodied-agents, mcp, fastmcp; npm — openclaw; pypistats — ros-mcp, jpl-rosa, rai-core, lerobot.

**Статьи (arXiv API, Crossref):**
- arXiv:2410.06472 (ROSA; IEEE Aerospace 2025, DOI 10.1109/AERO63441.2025.11068426);
- arXiv:2505.07532 (RAI; PAAMS 2025, DOI 10.1007/978-3-032-05925-3_16);
- arXiv:2406.19741 (ROS-LLM; Nature MI 8:313–325, 2026);
- arXiv:2412.18588 (OM1);
- arXiv:2409.13682 (ReMEmbR);
- arXiv:2603.26997 (ROSClaw, Cardenas et al.);
- arXiv:2204.01691 (SayCan);
- arXiv:2209.07753 (Code as Policies; DOI 10.1109/ICRA48891.2023.10160591);
- arXiv:2306.17582 (ChatGPT for Robotics; DOI 10.1109/ACCESS.2024.3387941).

**Собственные проверки [M]** (артефакты в scratchpad сессии, не в проекте): `file` и `strings | grep GLIBC_` для `om1` и `libzenohc.so` (OM1 nightly linux-arm64), `picoclaw` и `picoclaw-launcher` (v0.3.1 Linux_arm64), `zeroclaw`, `zerocode`, `zerorelay` (v0.8.5, aarch64 gnu и musl); чтение `internal/metrics/metrics.go` (OM1), `src/rosa/rosa.py` и `src/rosa/tools/ros2.py` (ROSA), `ros_mcp/main.py` и `robot_specifications/*.yaml` (ros-mcp-server), `server/tool_safety.py` и импорты `rclpy` в `server/` (ros2_mcp), `go/Dockerfile.engine` (Edge Agents), `packages/agents/*` (jetson-containers).
