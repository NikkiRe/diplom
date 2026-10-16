# L1. Универсальные фреймворки и SDK для ИИ-агентов: что реально используется и что из этого влезет на одноплатник

Дата проверки: **2026-10-06**. Все звёзды, даты, версии и загрузки сняты в этот день из первоисточников (GitHub API, PyPI JSON API, pypistats.org, npm registry, crates.io). Сырые данные и скрипты лежат в `L1_artifacts/` рядом с заметкой.

Область: универсальные (не узкоспециализированные под edge) фреймворки для агентов. Это LangChain/LangGraph, LlamaIndex, AutoGen/AG2, Microsoft Agent Framework/Semantic Kernel, CrewAI, smolagents, OpenAI Agents SDK, Google ADK, PydanticAI, Agno, Mastra, Letta, Strands, DSPy и смежные. Семейство «*Claw» (NanoClaw, PicoClaw, ZeroClaw, nanobot) относится к области A и здесь упоминается только для сравнения.

---

## 0. Главное (TL;DR)

1. **Лидеры по использованию** (загрузки PyPI за последний месяц): LangChain (170 млн; цифра раздута транзитивными зависимостями), LangGraph (44,8 млн), Strands Agents (37,1 млн), Claude Agent SDK (29,8 млн), PydanticAI (23,9 млн у `pydantic-ai-slim`), OpenAI Agents SDK (12,4 млн), Google ADK (10,1 млн), LlamaIndex (6,6 млн), DSPy (5,3 млн), CrewAI (2,4 млн), Agno (1,8 млн), Microsoft Agent Framework (1,0 млн). В JS/TS лидеры такие: Vercel AI SDK (`ai`, 110 млн в месяц на npm), LangChain.js, Mastra (7,3 млн).
2. **За 2025–2026 рынок консолидировался.** AutoGen переведён в режим поддержки, Semantic Kernel объявлен предшественником **Microsoft Agent Framework** (MAF 1.0 вышел 2026-04-02). Python-сервер Letta выведен из эксплуатации, проект переехал в TypeScript-репозиторий `letta-code`. LlamaIndex сместил фокус на LlamaParse, а LlamaIndexTS заархивирован. AG2 1.0 (2026-07-27) — это новый фреймворк, классический API вынесен в `ag2-classic`. Мажорные версии 2026 года: PydanticAI 2.0 (06-23), Google ADK 2.0 (05-19), Agno 3.0 (08-24), Haystack 3.0 (07-20).
3. **MCP-клиент есть у всех основных фреймворков** (это подтверждено по исходникам). Исключение: в langchaingo нативного MCP нет (issue #1209 открыт), есть только сторонний адаптер. Tool calling поддерживают все. Значит, MCP-сервер оборудования из постановки ВКР подключается к любому кандидату, и выбор фреймворка от MCP не зависит.
4. **На одноплатнике с внешней LLM узкое место — не RAM, а установка.** Я сделал разрешение зависимостей (pip `--dry-run`, без установки) под aarch64 и Python 3.12 для двух порогов glibc:
   - **2.27** — оригинальный Jetson Nano с JetPack 4.6;
   - **2.35** — Jetson Orin / JetPack 6, а также Raspberry Pi OS (2.36/2.41) как надмножество.

   Результат на glibc 2.27:
   - **OpenAI Agents SDK, MAF (core+openai+mcp), AG2, Agno, Strands, Google ADK (без LiteLLM), LangChain/LangGraph** ставятся из готовых колёс последних версий. У LangChain/LangGraph единственный откат — `tiktoken` 0.11 вместо 0.14.
   - **PydanticAI** последней версии не ставится: `pydantic-ai-slim[openai]` 2.x требует `tiktoken>=0.12`, а у него для aarch64 есть только колёса manylinux_2_28. Разрешается лишь 1.38.0, у полного `pydantic-ai` — 0.4.2.
   - **LiveKit Agents** не ставится вовсе: нативный `livekit` выпускается только под manylinux_2_28.
   - **smolagents, DSPy, CrewAI** ставятся только с откатом `huggingface-hub` до 0.31.4, потому что `hf-xet` собирается только под 2_28.
   - **Node.js 20/22/24** официально требует glibc ≥ 2.28 и ядро ≥ 4.18. Поэтому Mastra, Vercel AI SDK, LangChain.js, Strands TS и Letta Code на хостовой JetPack 4 не запускаются без контейнера с новой userland. Даже в контейнере ядро 4.9 официально не поддерживается.
5. **Вес (минимальный профиль «агент + OpenAI-совместимый клиент + MCP-клиент», распакованный объём):**
   - эталон «голый» `openai`+`mcp`: 38 МБ, 31 пакет;
   - OpenAI Agents SDK: 46 МБ;
   - AG2: 45 МБ;
   - MAF core: 48 МБ;
   - Google ADK: 61 МБ;
   - Strands: 62–69 МБ;
   - LangGraph: 65 МБ;
   - AutoGen: 73 МБ;
   - Agno: 74 МБ;
   - PydanticAI-slim: 75 МБ;
   - LangChain: 85–109 МБ;
   - smolagents: 90 МБ;
   - LlamaIndex: 206 МБ;
   - DSPy: 203 МБ;
   - Claude Agent SDK: 270 МБ (вшит бинарник Claude Code);
   - Semantic Kernel: 371 МБ;
   - Pipecat: 409 МБ;
   - CrewAI: 698 МБ.

   Единственное найденное независимое измерение RSS на одноплатнике — препринт (Zenodo, 2026-06): **smolagents ≈ 90 МБ** пикового RSS на Raspberry Pi 4 (2 ГБ) с облачной моделью. Для сравнения там же PicoClaw (Go) ≈ 31 МБ, ZeroClaw (Rust) ≈ 20 МБ, nanobot (Python) ≈ 130 МБ.
6. **Популярные «личные» edge-агенты общих фреймворков не используют.** nanobot (48,8 тыс. ★), PicoClaw (30 тыс. ★) и ForestHub edge-agents написаны прямо на SDK провайдеров (`openai`/`openai-go`, `anthropic`) плюс официальный MCP SDK. Это проверено по `pyproject.toml`/`go.mod`.

   Из общих фреймворков в робототехнике и на одноплатниках реально засветились:
   - **LangChain/LangGraph**: ROSA от NASA JPL, RAI от Robotec.ai для ROS 2, gpt-home (LangGraph на Raspberry Pi Zero 2 W/4/5);
   - **Strands Agents**: вендорская линия «physical AI» — демонстрации на Jetson, `strands-robots`, Boston Dynamics Spot, участие в превью Model Hardware Standard;
   - **smolagents**: упомянутый препринт про Raspberry Pi 4.
7. **Вывод для ВКР.** На Jetson Nano / Raspberry Pi 5 при внешней LLM (RouterAI, OpenAI-совместимый Chat Completions API) по весу, установке на JetPack 4 и родному MCP разумно рассматривать три варианта:
   - (а) собственный тонкий цикл на `openai`+`mcp`, как у *Claw: полный контроль над метриками токенов, стоимости и задержек;
   - (б) OpenAI Agents SDK;
   - (в) MAF core или Strands.

   LangGraph тоже приемлем, если нужны графы состояний и есть желание опереться на ROSA/RAI. CrewAI, LlamaIndex, Semantic Kernel, Claude Agent SDK и LiveKit для оригинального Jetson Nano нецелесообразны. Подробно — в §7.

---

## 1. Метод (как получены числа) и ограничения

| Что | Источник / способ |
|---|---|
| Звёзды, форки, даты создания и последнего push, язык, лицензия, флаг архивации | `gh api repos/OWNER/REPO` (2026-10-06) → `L1_artifacts/gh_repos.tsv` |
| Версия, дата релиза, `requires_python`, зависимости ядра и extras | `https://pypi.org/pypi/PKG/json` → `pypi_meta.jsonl` |
| Даты первого релиза каждой мажорной версии | история релизов в PyPI JSON |
| Загрузки за последний месяц | `https://pypistats.org/api/packages/PKG/recent` → `pypistats.txt` (включают CI и зеркала, поэтому это индикатор популярности, а не число пользователей) |
| npm: версия, `engines.node`, загрузки | `registry.npmjs.org/PKG/latest`, `api.npmjs.org/downloads/point/last-month/PKG` → `npm_meta.txt` |
| **Замыкание зависимостей (Python)** | `pip install --dry-run --report --only-binary=:all: --python-version 3.12 --platform manylinux_2_{17..N}_aarch64 --platform manylinux2014_aarch64`. Ничего не устанавливается, pip скачивает только метаданные. N=27 моделирует оригинальный Jetson Nano (JetPack 4.6.x, Ubuntu 18.04, glibc 2.27), N=35 — Jetson Orin (JetPack 6, Ubuntu 22.04) и Raspberry Pi OS Bookworm/Trixie (glibc 2.36/2.41: колёс выше manylinux_2_35 практически нет). Скрипт: `resolve.py` |
| Сжатый размер колёс | сумма `size` файлов из PyPI JSON (`recompute.py`, с повторами при сетевых сбоях) |
| **Распакованный размер** (≈ место на диске после установки, без `.pyc`) | сумма `file_size` из центрального каталога каждого колеса, прочитанного HTTP Range-запросами без скачивания целиком (`unzipsize.py`) → `unzip_results_final.tsv` |
| npm-замыкание | `npm install --package-lock-only --ignore-scripts` в песочнице (без `node_modules`) плюс сумма `dist.unpackedSize` для linux-arm64 (`npm/sizes.mjs`). Это нижняя оценка: для части scoped-пакетов размер не получен |
| Поддержка MCP | наличие MCP-модулей в дереве исходников (`gh api .../git/trees?recursive=1`) и имена классов в исходниках |
| Использование на железе | README и деревья репозиториев, поиск по GitHub (issues и репозитории), jetson-containers, блоги вендоров, препринт на Zenodo |

**Ограничения.**
- RSS и время импорта на этой машине **не измерялись**: по правилам задачи ничего не устанавливалось. Числа RSS взяты только из внешнего препринта (smolagents) или помечены как заявления вендора.
- В разрешение зависимостей попадали только готовые колёса. У Semantic Kernel есть чисто-Python зависимости, которые публикуются только в виде sdist (`pybars4`, `PyMeta3`). Я собрал из них локальные чисто-Python колёса (`pip wheel --no-deps`) и подложил через `--find-links`; в систему ничего не ставилось. Langroid не разрешился по той же причине (`halo` и другие), поэтому его вес не измерен.
- Режим glibc 2.27 показывает, что ставится **из бинарных колёс**. Пакет без подходящего колеса можно собрать из исходников (Rust/C-тулчейн на Cortex-A57), но это долго и в оценку не входит.
- Пакеты на PyPI с `+`/extras отражают **мой** минимальный профиль под сценарий ВКР (OpenAI-совместимая модель плюс MCP), а не «полную» установку.

---

## 2. Целевые платформы: что из этого следует для фреймворков

| Платформа | ОС / userland | glibc | Python в системе | Node.js (офиц. бинарники) | Значение для фреймворков |
|---|---|---|---|---|---|
| **Jetson Nano Developer Kit** (2019; варианты **4 ГБ** B01/A02 и **2 ГБ** 2020 г.; варианта на 1 ГБ у Jetson Nano нет) | JetPack 4.6.x (последний — 4.6.6, L4T R32.7.6), Ubuntu 18.04, **ядро 4.9** | **2.27** | 3.6 (все фреймворки требуют ≥ 3.9–3.12) | Node 20/22/24 требуют glibc ≥ 2.28 и ядро ≥ 4.18, то есть **не запускаются** | Python 3.12 ставится через `uv python install` / python-build-standalone (минимум glibc 2.17). Колёса проверены под 2.27 (§4) |
| Jetson Orin Nano (4/8 ГБ) | JetPack 6, Ubuntu 22.04 | 2.35 | 3.10 | ок | ограничений нет |
| Raspberry Pi 5 (4/8/16 ГБ) | Raspberry Pi OS Bookworm (glibc 2.36) / Trixie (2.41) | ≥ 2.36 | 3.11 / 3.13 | ок | ограничений нет. На 3.13 действует PEP 668, поэтому нужен venv или uv |

Источники: NVIDIA JetPack 4.6.6 release page (L4T 32.7.6, «Linux kernel 4.9 … Ubuntu 18.04»); Node.js `BUILDING.md` веток v20.x/v22.x/v24.x (GNU/Linux arm64: kernel ≥ 4.18, glibc ≥ 2.28; на `main`, будущий Node 26: kernel ≥ 5.14, glibc ≥ 2.34); документация python-build-standalone («minimum glibc … 2.17»). Подробнее о железе — в другой области исследования.

**Модель через RouterAI.** Сервис OpenAI-совместим (`base_url = https://routerai.ru/api/v1`, Chat Completions; источник — routerai.ru/docs). Поэтому годится любой фреймворк с настраиваемым OpenAI-клиентом. У OpenAI Agents SDK по умолчанию используется Responses API: для RouterAI нужно `OpenAIChatCompletionsModel` или `set_default_openai_api("chat_completions")`, а также отключить трассировку в OpenAI (`set_tracing_disabled(True)`). Это прямо рекомендовано в `docs/models/index.md` SDK для ключей не от platform.openai.com.

---

## 3. Популярность и зрелость (на 2026-10-06)

| # | Фреймворк | Репозиторий | ★ | Создан | Последний push | Лицензия | Последняя версия (дата) | Загрузки/мес. | Статус |
|---|---|---|---:|---|---|---|---|---:|---|
| 1 | LangChain | langchain-ai/langchain | 147 497 | 2022-10-17 | 2026-10-06 | MIT | `langchain` 1.4.3 (2026-09-28); 1.0 — 2025-10-17 | 170,5 млн (`langchain-core` 144,8 млн) | активен |
| 2 | LangGraph | langchain-ai/langgraph | 42 777 | 2023-08-09 | 2026-10-05 | MIT | 1.2.13 (2026-10-05); 1.0 — 2025-10-17 | 44,8 млн | активен |
| 3 | LlamaIndex | run-llama/llama_index | 52 422 | 2022-11-02 | 2026-10-05 | MIT | `llama-index-core` 0.14.25 (2026-09-21) | 6,6 млн | фокус компании смещён на LlamaParse; TS-версия заархивирована |
| 4 | Microsoft Agent Framework | microsoft/agent-framework | 13 963 | 2025-04-28 | 2026-10-06 | MIT | 1.20.0 (2026-10-02); 1.0 — 2026-04-02 | 1,0 млн (`agent-framework-core`) | активен, преемник AutoGen и SK |
| 5 | AutoGen | microsoft/autogen | 61 275 | 2023-08-18 | 2026-04-15 | CC-BY-4.0 (репо) / MIT (пакеты) | `autogen-agentchat` 0.7.5 (2025-09-30) | 0,38 млн | **режим поддержки** |
| 6 | AG2 | ag2ai/ag2 | 4 978 | 2024-11-11 | 2026-10-06 | Apache-2.0 | 1.1.2 (2026-10-03); 1.0 — 2026-07-27 | 0,16 млн | активен (форк AutoGen 0.2, новая архитектура в 1.0) |
| 7 | Semantic Kernel | microsoft/semantic-kernel | 28 630 | 2023-02-27 | 2026-10-06 | MIT | 1.45.0 (2026-10-06) | 0,30 млн | README: «SK is now Microsoft Agent Framework» |
| 8 | CrewAI | crewAIInc/crewAI | 59 396 | 2023-10-27 | 2026-10-06 | MIT | 1.15.23 (2026-09-28); 1.0 — 2025-10-20 | 2,4 млн | активен |
| 9 | smolagents | huggingface/smolagents | 29 701 | 2024-12-05 | 2026-10-06 | Apache-2.0 | 1.26.0 (2026-05-29) | 0,43 млн | активен, релизы реже (последний в мае) |
| 10 | OpenAI Agents SDK | openai/openai-agents-python (+ -js 3 890 ★) | 29 857 | 2025-03-11 | 2026-10-06 | MIT | 0.23.1 (2026-10-02), всё ещё 0.x | 12,4 млн; npm `@openai/agents` 7,9 млн | активен |
| 11 | Google ADK | google/adk-python (+ adk-go 8 848 ★, adk-java 1 746 ★) | 21 724 | 2025-04-01 | 2026-10-06 | Apache-2.0 | 2.11.0 (2026-10-02); 2.0 — 2026-05-19 | 10,1 млн | активен |
| 12 | PydanticAI | pydantic/pydantic-ai | 20 435 | 2024-06-21 | 2026-10-06 | MIT | 2.54.0 (2026-10-03); 2.0 — 2026-06-23 | 23,9 млн (slim) / 5,4 млн (полный) | активен |
| 13 | Agno (бывш. phidata) | agno-agi/agno | 42 575 | 2022-05-04 | 2026-10-06 | Apache-2.0 | 3.1.1 (2026-10-02); 3.0 — 2026-08-24 | 1,8 млн | активен |
| 14 | Strands Agents (AWS) | strands-agents/harness-sdk (бывш. sdk-python) | 8 686 | 2025-05-14 | 2026-10-06 | Apache-2.0 | 1.58.0 (2026-10-06); 1.0 — 2025-07-15 | 37,1 млн | активен; отдельный репозиторий sdk-typescript заархивирован (код в монорепо) |
| 15 | DSPy | stanfordnlp/dspy | 38 516 | 2023-01-09 | 2026-10-05 | MIT | 3.4.0 (2026-09-25) | 5,3 млн | активен |
| 16 | Haystack | deepset-ai/haystack | 26 684 | 2019-11-14 | 2026-10-06 | Apache-2.0 | 3.3.0 (2026-10-01); 3.0 — 2026-07-20 | 0,57 млн | активен |
| 17 | Claude Agent SDK | anthropics/claude-agent-sdk-python (+ TS 1 783 ★) | 8 218 | 2025-06-11 | 2026-10-06 | MIT (Python) | 0.2.163 (2026-09-30) | 29,8 млн; npm 47,8 млн | активен |
| 18 | Letta (бывш. MemGPT) | letta-ai/letta → **letta-ai/letta-code** (3 528 ★, TS) | 25 045 | 2023-10-11 | 2026-09-10 | Apache-2.0 | npm `@letta-ai/letta-code` 0.34.4 | npm 0,55 млн; PyPI `letta` 15 тыс. | Python-сервер V1 выведен из эксплуатации (ветка `archive`) |
| 19 | Mastra | mastra-ai/mastra | 28 593 | 2024-08-06 | 2026-10-06 | NOASSERTION (в основном Apache-2.0) | `@mastra/core` 1.74.0 | 7,3 млн (npm) | активен |
| 20 | Vercel AI SDK | vercel/ai | 27 144 | 2023-05-23 | 2026-10-06 | NOASSERTION (пакет `ai`: Apache-2.0) | `ai` 7.0.128 | 110,3 млн (npm) | активен |
| 21 | Eino (ByteDance/CloudWeGo, Go) | cloudwego/eino | 13 250 | 2024-12-04 | 2026-09-29 | Apache-2.0 | v0.9.21 (2026-09-23) | — | активен |
| 22 | LangChainGo | tmc/langchaingo | 9 711 | 2023-02-18 | 2026-10-05 | MIT | v0.1.15 (2026-10-05) | — | активен |
| 23 | Rig (Rust) | 0xPlaygrounds/rig | 8 820 | 2024-06-05 | 2026-10-06 | MIT | `rig-core` 0.43.0 (2026-09-30) | crates.io: 3,3 млн всего / 1,8 млн «recent» | активен, README предупреждает о breaking changes |
| 24 | Pipecat (голосовые агенты) | pipecat-ai/pipecat | 16 220 | 2023-12-27 | 2026-10-06 | BSD-2-Clause | 1.12.0 (2026-09-26) | 1,0 млн | активен |
| 25 | LiveKit Agents (голосовые агенты) | livekit/agents | 14 618 | 2023-10-19 | 2026-10-06 | Apache-2.0 | 1.8.5 (2026-10-06) | 3,6 млн | активен |

**Второй эшелон** (проверено, но в основную таблицу не включено):

| Проект | ★ | Последний push | Версия | Загрузки/мес. | Комментарий |
|---|---:|---|---|---:|---|
| FoundationAgents/MetaGPT | 70 762 | 2026-01-21 | 0.8.2 (2025-03-09), Python < 3.12 | 3,4 тыс. | исследовательский, фактически не развивается, 66 закреплённых зависимостей |
| camel-ai/camel | 17 814 | 2026-10-05 | 0.2.90 (2026-03-22) | 43 тыс. | исследовательский мультиагентный |
| lastmile-ai/mcp-agent | 8 568 | 2026-01-25 | 0.2.6 | 26 тыс. | «MCP-нативный», тянет scipy и scikit-learn (297 МБ) |
| genkit-ai/genkit (Google; JS/Go/Dart/Python) | 6 474 | 2026-10-06 | `genkit` 1.42.0 | npm 1,0 млн | npm-замыкание 511 пакетов / ≈ 304 МБ |
| Eigenwise/atomic-agents | 6 269 | 2026-10-04 | 2.10.3 (Python ≥ 3.12) | 12 тыс. | на основе instructor и LiteLLM |
| langroid/langroid | 4 111 | 2026-10-06 | 0.68.6 | 11 тыс. | 50 зависимостей ядра, включая onnxruntime и pandas |
| evalstate/fast-agent | 3 929 | 2026-10-04 | 0.10.42 (Python ≥ 3.12) | 108 тыс. | MCP/ACP/A2A; на glibc 2.27 разрешается только 0.2.49 |
| i-am-bee/beeai-framework (IBM / Linux Foundation) | 3 426 | 2026-09-28 | 0.1.85 | 28 тыс. | через LiteLLM |
| microsoft/agent-framework-go | 638 | 2026-10-06 | go 1.26.0 | — | Go-SDK MAF |
| dusty-nv/NanoLLM (NVIDIA, Jetson-специфичный) | 381 | **2024-10-18** | 24.7 (контейнеры r35/r36, то есть JetPack 5/6) | — | заброшен; на Jetson Nano (r32) не рассчитан |

---

## 4. Вес и устанавливаемость на aarch64 (Python 3.12)

Профиль — «то, что нужно агенту ВКР»: фреймворк, OpenAI-совместимый клиент и MCP-клиент. Сжатый размер — сумма колёс, распакованный — по центральным каталогам ZIP. 1 МБ = 10⁶ байт.

| Профиль (что ставилось) | Пакетов | Колёса, МБ | Распаковано, МБ | `requires_python` | glibc 2.27 (Jetson Nano, JP 4.6) | Крупнейшие зависимости |
|---|---:|---:|---:|---|---|---|
| **Эталон: `openai` + `mcp`** (как у nanobot) | 31 | 11,9 | 38 | ≥ 3.10 (mcp) | ок, версии те же | cryptography, pydantic-core |
| `openai-agents` (MCP уже в ядре) | 38 | 14,0 | 46 | ≥ 3.10 | **ок, те же версии** | cryptography, openai |
| `ag2[openai,mcp]` | 37 | 14,1 | 45 | ≥ 3.10 | **ок** | cryptography, openai |
| `agent-framework-core` + `agent-framework-openai` + `mcp` (MAF) | 37 | 14,6 | 48 | ≥ 3.10 | **ок** | cryptography, openai, regex |
| `google-adk` (только Gemini) | 48 | 18,2 | 61 | ≥ 3.10 | **ок** | fastapi, uvicorn, google-genai, OTel |
| `google-adk` + `litellm` (нужен для OpenAI-совместимых моделей) | 81 | 87,1 | 242 | ≥ 3.10 | ставится с откатами: litellm 1.104 → 1.91.5, tiktoken 0.14 → 0.11, huggingface-hub → 0.31.4 (159 МБ) | litellm (36 МБ колесо), botocore |
| `strands-agents` (голый) / `[openai]` | 48 / 54 | 28,8 / 30,9 | 62 / 69 | ≥ 3.10 | **ок** | **boto3/botocore всегда в ядре** (16 МБ колесо) |
| `langgraph` + `langchain-openai` | 43 | 17,4 | 65 | ≥ 3.10 | ок, кроме tiktoken 0.14 → 0.11 | zstandard, tiktoken, langsmith |
| `langchain` + `langchain-openai` + `langchain-mcp-adapters` | 63 | 23,9 | 85 | ≥ 3.10 | ок, кроме tiktoken → 0.11 | zstandard, cryptography |
| `langchain[mcp]` (новый `langchain.mcp.MCPAdapter` на FastMCP) + `langchain-openai` | 96 | 30,7 | 109 | ≥ 3.10 | ок | FastMCP и его зависимости |
| `autogen-agentchat` + `autogen-ext[openai,mcp]` | 44 | 21,6 | 73 | ≥ 3.10 | ок, кроме tiktoken → 0.11 | pillow, protobuf |
| `agno` + `openai` + `mcp` | 54 | 20,6 | 74 | ≥ 3.9 | **ок** | agno (4,2 МБ колесо), rich, pygments |
| `pydantic-ai-slim[openai,mcp]` | 65 | 22,3 | 75 | ≥ 3.10 | **последняя версия не ставится**: разрешается только 1.38.0 (42 пакета, 43 МБ), так как extra `openai` в 2.x требует `tiktoken>=0.12`, а это только manylinux_2_28_aarch64 | fastmcp-slim, beartype, tiktoken |
| `pydantic-ai` (полный) | 97 | 30,3 | 106 | ≥ 3.10 | разрешается только **0.4.2** | logfire, anthropic, OTel |
| `smolagents[openai,mcp]` | 53 | 27,2 | 90 | ≥ 3.10 | ставится с `huggingface-hub` 2.1.1 → 0.31.4 (`hf-xet` только 2_28): 52 пакета, 77 МБ | pillow, hf-xet, huggingface-hub |
| `haystack-ai` + `mcp-haystack` | 56 | 33,5 | 113 | ≥ 3.10 | ок | numpy, networkx, posthog (телеметрия) |
| `dspy` | 66 | 75,7 | 203 | 3.10–3.14 | откаты litellm/tiktoken/hf-hub (55 пакетов, 119 МБ) | **litellm**, botocore |
| `llama-index-core` + `llama-index-llms-openai-like` + `llama-index-tools-mcp` | 87 | 63,7 | 206 | ≥ 3.10 | ок, кроме tiktoken → 0.11 | numpy, nltk, SQLAlchemy, pillow, networkx |
| `claude-agent-sdk` | 30 | 112,2 | 270 | ≥ 3.10 | ок (колесо помечено manylinux_2_17) | **колесо 103 МБ: вшит бинарник Claude Code CLI** |
| `semantic-kernel` | 94 | 111,2 | 371 | ≥ 3.10 | ставится с `av` 17.1 → 14.2 (383 МБ) | scipy, av (PyAV), numpy, azure-*, aiortc |
| `pipecat-ai[openai]` | 38 | 125,9 | 409 | ≥ 3.11 | ок | llvmlite/numba, scipy, numpy, onnxruntime |
| `livekit-agents[openai,mcp]` | 84 | 129,8 | 322 | 3.10–3.14 | **не ставится**: `livekit==1.1.20` только manylinux_2_28_aarch64 | livekit-local-inference, av, numpy |
| `crewai` | 136 | 237,8 | **698** | 3.10–3.13 | ставится с откатами onnxruntime 1.30 → 1.28, pyarrow 25 → 20, hf-hub → 0.31.4 (665 МБ) | pyarrow, lancedb, chromadb, onnxruntime, **uv** |
| `letta-client` (только клиент к серверу Letta) | 14 | 3,5 | 10 | ≥ 3.9 | ок | — |
| `letta` (PyPI, теперь это Letta Code) | 1 | 78 (одно колесо) | — | ≥ 3.9 | **нет**: колесо `manylinux_2_28_aarch64` | бинарник |

**Наблюдения по таблице.**
- Без тяжёлых добавок (LiteLLM, векторные БД, аудио/видео) всё ядро современных фреймворков — это **pydantic + httpx + openai + mcp + OpenTelemetry**: 40–110 МБ на диске. Для Jetson Nano с eMMC/SD на 16+ ГБ это несущественно.
- На стыке с glibc 2.27 ломаются **конкретные нативные колёса**: `tiktoken` ≥ 0.12, `hf-xet`, `pyarrow` ≥ 21, `onnxruntime` ≥ 1.29, `av` ≥ 15, `livekit`. Все они в 2025–2026 перешли на manylinux_2_28. Фреймворк считается непригодным для Nano только тогда, когда такой пакет требуется в **ядре** (PydanticAI 2.x через extra `openai`, LiveKit). В остальных случаях pip выбирает более старую совместимую версию.
- LiteLLM как универсальный адаптер моделей утяжеляет профиль в 2–4 раза: ADK с LiteLLM — 242 МБ против 61 МБ, DSPy — 203 МБ. Для RouterAI он не нужен: хватает OpenAI-совместимого клиента. Исключение — Google ADK, где не-Gemini модели штатно подключаются через `LiteLlm`.
- Strands всегда тянет `boto3`/`botocore`, даже с провайдером OpenAI: +16 МБ колесо, +≈ 25 МБ на диске.

### 4.1. JS/TS (npm, замыкание для linux-arm64, нижняя оценка)

| Профиль | `engines.node` | Пакетов | Распаковано, МБ | Jetson Nano (JP 4.6) |
|---|---|---:|---:|---|
| `ai` + `@ai-sdk/openai` + `@ai-sdk/mcp` (Vercel AI SDK) | ≥ 22 | 20 | ≈ 23 | нет (Node ≥ 22 → glibc 2.28) |
| `@openai/agents` | не указан в package.json (README: «Node.js 22 or later», Deno, Bun) | 25 | ≈ 65 | нет без контейнера |
| `langchain` + `@langchain/openai` + `@langchain/mcp-adapters` | ≥ 20 | 38 | ≈ 86 | нет |
| `@strands-agents/sdk` | ≥ 22 | 127 | ≈ 29 | нет |
| `@mastra/core` + `@ai-sdk/openai` + `@mastra/mcp` | **≥ 22.13** | 167 | ≈ 130 (сам `@mastra/core` — 71,8) | нет |
| `@google/adk` | не указан | 177 | ≈ 109 | нет |
| `genkit` + `@genkit-ai/compat-oai` | не указан | 511 | ≈ 304 (`googleapis` 110,6) | нет |
| `@anthropic-ai/claude-agent-sdk` | ≥ 18 | 103 | ≈ 284 (бинарник linux-arm64 249) | нет |
| `@letta-ai/letta-code` | ≥ 22.19 | 355 | ≈ 447 (`node-pty` 64) | нет |
| `@modelcontextprotocol/sdk` (для сравнения) | ≥ 18 | 94 | ≈ 17 | — |

### 4.2. Go / Rust (компилируемые)

| Проект | Минимальная версия тулчейна | Зависимости | Jetson Nano |
|---|---|---|---|
| Eino (+ eino-ext) | `go 1.18` в `go.mod` | 38 require в ядре | да: `GOOS=linux GOARCH=arm64 CGO_ENABLED=0`, статический бинарник от glibc не зависит (кросс-компиляция с Mac) |
| LangChainGo | `go 1.24.4` (toolchain 1.24.6) | 286 require в монорепо (в бинарник попадает только используемое) | да, аналогично |
| ADK-Go | `go 1.26.6` | 98 require | да, аналогично |
| Rig (`rig-core`) | Rust MSRV **1.95** | crate 1,0 МБ | да: таргет `aarch64-unknown-linux-gnu` / `-musl` |

Размер бинарников и RSS для Go/Rust-фреймворков **не измерены**. Ориентир — PicoClaw (Go) ≈ 31 МБ и ZeroClaw (Rust) ≈ 20 МБ RSS на Raspberry Pi 4 по препринту (§6), но это не фреймворки из этой области.

---

## 5. Карточки фреймворков

Формат карточки: что это и архитектура → tool calling и MCP (подтверждено по исходникам) → вес → железо/edge → вердикт для Jetson Nano (JP 4.6, 2–4 ГБ) и Raspberry Pi 5.

### 5.1. LangChain (Python; есть LangChain.js)
- **Что это.** Фреймворк компонентов и интеграций, с версии 1.0 (2025-10-17) построенный вокруг `create_agent` поверх LangGraph. Над ним надстроен Deep Agents (планирование, субагенты, файловая система). Трассировка через LangSmith (`langsmith` в ядре `langchain-core`).
- **MCP.** Два пути: (1) пакет `langchain-mcp-adapters` 0.3.2 (`MultiServerMCPClient`); (2) в `langchain` v1 появился модуль `langchain/mcp/` (`MCPAdapter` поверх FastMCP, extra `langchain[mcp]`). Tool calling нативный.
- **Вес.** 63 пакета / 85 МБ; через `langchain[mcp]` — 96 / 109 МБ.
- **Железо.** ROSA (NASA JPL, 1 653 ★) — агент для ROS1/ROS2 на `langchain~=0.3` (pyproject). jetson-containers содержит пакет `langchain`, но с `requires: '>=34.1.0'` (L4T R34+, то есть JetPack 5+, не оригинальный Nano). Поиск GitHub «langchain raspberry pi» даёт 12 репозиториев, крупнейший — gpt-home (652 ★, LangGraph).
- **Jetson Nano.** Да, с оговорками: нужен Python ≥ 3.10 (uv); все колёса есть под glibc 2.27, кроме `tiktoken`, который откатывается на 0.11 без потери функций. Готового контейнера NVIDIA для R32 нет. **Raspberry Pi 5:** да.

### 5.2. LangGraph
- **Что это.** Низкоуровневый оркестратор агентов в виде графа состояний: durable execution через чекпоинтеры, human-in-the-loop, память. Используется в Klarna, Replit, Elastic (по README).
- **MCP** — через те же адаптеры LangChain.
- **Вес.** 43 пакета / 65 МБ вместе с `langchain-openai`.
- **Железо.**
  - RAI (RobotecAI/rai, 599 ★): «vendor agnostic agentic framework for Physical AI robotics» на ROS 2, зависит от `langgraph>=1.1.5`, `langchain>=1.0`.
  - gpt-home (judahpaul16/gpt-home, 652 ★): домашний ассистент на LangGraph + LiteLLM. README заявляет поддержку Raspberry Pi Zero 2 W (512 МБ) и Pi 4B/5. Это заявление проекта, я его не проверял.
- **Jetson Nano.** Да, это наиболее «проверенный сообществом» вариант на слабых платах. **Raspberry Pi 5:** да.

### 5.3. LlamaIndex
- **Что это.** Изначально RAG-фреймворк, затем агенты на event-driven Workflows (`llama-index-workflows`). README прямо говорит, что основной фокус компании смещён на LlamaParse и парсинг документов, а OSS-фреймворк остаётся доступным как открытый инструментарий. LlamaIndexTS заархивирован (флаг archived, последний push 2026-03-11).
- **MCP.** `llama-index-tools-mcp` 0.6.0 (`McpToolSpec`/`BasicMCPClient`) и `llama-index-tools-mcp-discovery`.
- **Вес.** 87 пакетов / 206 МБ: в ядре numpy, nltk, SQLAlchemy, networkx, pillow, tiktoken.
- **Железо.** Пакет `llama-index` в jetson-containers (L4T ≥ 34.1) и туториалы Jetson AI Lab для Orin. На Nano ничего не найдено.
- **Jetson Nano.** Технически ставится (glibc 2.27 ок, tiktoken 0.11), но тяжело и не по профилю задачи: агентская часть вторична по отношению к RAG. **Raspberry Pi 5:** да.

### 5.4. Microsoft Agent Framework (MAF)
- **Что это.** Объединённый преемник AutoGen и Semantic Kernel (оба README отсылают к нему). Python, .NET и отдельный Go-SDK (`microsoft/agent-framework-go`). Включает агентов, граф-воркфлоу (последовательные, параллельные, handoff, group chat), middleware, durability, OpenTelemetry. Версия 1.0 GA вышла 2026-04-02, текущая 1.20.0. Модульные пакеты: `openai`, `anthropic`, `ollama`, `foundry_local`, `hosting-mcp`, `hosting-telegram`, `devui` и другие.
- **MCP.** В ядре `agent_framework/_mcp.py`: `MCPStdioTool`, `MCPStreamableHTTPTool`, `MCPWebsocketTool`, плюс одобрение вызовов. Публикация агента как MCP-сервера — пакет `hosting-mcp`.
- **Вес.** `agent-framework-core` + `-openai` + `mcp`: 37 пакетов / 48 МБ. Метапакет `agent-framework` тянет `core[all]` (Azure, Redis, Cosmos и т. д.), поэтому ставить нужно только core и провайдера.
- **Железо.** Примеров на одноплатниках не найдено: issues с «raspberry pi»/«jetson» — 0.
- **Jetson Nano.** Да: всё из колёс под glibc 2.27, версии последние. **Raspberry Pi 5:** да.

### 5.5. AutoGen (Microsoft)
- **Что это.** Акторная мультиагентная система (`autogen-core`) и AgentChat поверх неё. Сейчас **в режиме поддержки**: «will not receive new features», новым пользователям рекомендован MAF. Последний релиз 0.7.5 вышел 2025-09-30.
- **MCP.** `autogen-ext[mcp]` (`McpWorkbench`, `tools/mcp/`).
- **Вес.** 44 пакета / 73 МБ.
- **Железо.** Только запрос фичи «EmbodiedAgent … control a robot» (#1239, 2024).
- **Jetson Nano.** Ставится, но брать для новой работы не стоит из-за статуса. **Raspberry Pi 5:** то же.

### 5.6. AG2 (бывш. AutoGen 0.2, сообщество)
- **Что это.** С версии 1.0 (2026-07-27) — новый «protocol-driven» фреймворк, импорт `ag2`. Классический `ConversableAgent`/`GroupChat` вынесен в `ag2ai/ag2-classic`.
- **MCP.** Модуль `ag2/mcp` (+ extras `mcp`, `mcp-ui`, `a2a`, `ag-ui`).
- **Вес.** 37 пакетов / 45 МБ, один из самых лёгких.
- **Железо.** Нет данных.
- **Jetson Nano.** Да, колёса под 2.27 ок. **Raspberry Pi 5:** да.

### 5.7. Semantic Kernel (Microsoft; C#, Python, Java)
- **Что это.** «Kernel» с плагинами и планировщиками, исторически ориентирован на .NET и Azure. README: «SK is now Microsoft Agent Framework».
- **MCP.** `semantic_kernel/connectors/mcp.py`, `mcp>=1.26` в ядре.
- **Вес.** 94 пакета / 371 МБ. В ядре scipy, numpy, PyAV/aiortc (realtime), azure-identity, azure-ai-projects, openapi-core. Часть зависимостей публикуется только как sdist (`pybars4`, `PyMeta3`).
- **Железо.** Issue #12712 (2025): .NET-коннектор ONNX не запускается на Raspberry Pi OS Lite.
- **Jetson Nano.** Нецелесообразно: тяжело, нужен откат PyAV, проект фактически замещён MAF. **Raspberry Pi 5:** ставится, но смысла нет.

### 5.8. CrewAI
- **Что это.** Мультиагентность в «ролевом» стиле (Crews) плюс событийные Flows. Коммерческий контур AMP.
- **MCP.** Нативный модуль `lib/crewai/src/crewai/mcp`, `mcp~=1.28.1` в ядре, адаптер `crewai-tools`.
- **Вес.** **Самый тяжёлый**: 136 пакетов / 698 МБ (chromadb, lancedb, pyarrow, onnxruntime, tokenizers и даже бинарник `uv` 21 МБ — всё в ядре).
- **Железо.** Не найдено: «crewai raspberry pi» — 0 репозиториев, issues — 0.
- **Jetson Nano.** Ставится с тремя откатами нативных пакетов, но весит ~0,7 ГБ на диске, а импорт chromadb/onnxruntime дорог по RAM (оценка, не измерено). **Не рекомендуется.** **Raspberry Pi 5:** возможно, но неоправданно.

### 5.9. smolagents (Hugging Face)
- **Что это.** Минималистичная библиотека. Главная идея — **CodeAgent**: модель пишет действия кодом на Python, а не JSON-вызовами. Есть и `ToolCallingAgent`. README: логика «fits in ~1,000 lines», фактически `agents.py` сейчас 1 813 строк. Песочницы: E2B, Modal, Docker, Blaxel; по умолчанию локальный интерпретатор.
- **MCP.** `smolagents/mcp_client.py` (`MCPClient`), `ToolCollection.from_mcp`. Есть импорт инструментов LangChain и Hub Spaces.
- **Вес.** 53 пакета / 90 МБ (`[openai,mcp]`), ядро — 6 зависимостей.
- **Железо.** **Единственный общий фреймворк с независимым замером на одноплатнике.** Препринт Dehigaspitiyage Don (Zenodo 10.5281/zenodo.20593332, 2026-06-08, не рецензирован):
  - стенд: Raspberry Pi 4B 2 ГБ, Debian 13, Python 3.13.5, облачная модель Gemini 3.1 Pro preview;
  - smolagents 1.26.0: пиковый RSS **≈ 89,6 МБ**, успех 25/25 задач;
  - медианная задержка выше, чем у Go/Rust-агентов (T2 — 21,9 с против 10,8 с у PicoClaw);
  - памяти «из коробки» нет: агент сам «изобрёл» память через файл;
  - на 2 ГБ одновременно работали 8 экземпляров каждого агента;
  - офлайн (Qwen2.5-1.5B / SmolLM2-360M) **ни один** агент не справился.
- **Jetson Nano.** Да, с откатом `huggingface-hub` до 0.31.4. Исполнение кода, сгенерированного LLM, на устройстве с исполнительными механизмами — отдельный риск: нужна песочница или жёсткий список импортов. **Raspberry Pi 5:** да.

### 5.10. OpenAI Agents SDK (Python и JS)
- **Что это.** «Lightweight» SDK. Примитивы: Agent (инструкции, инструменты, guardrails, handoffs), Runner, sessions, tracing, realtime- и voice-агенты, sandbox-агенты. По README поддерживает OpenAI Responses и Chat Completions и «100+ других LLM» (extras `litellm`, `any-llm`).
- **MCP.** `mcp` в **ядре**. Классы `MCPServerStdio`, `MCPServerSse`, `MCPServerStreamableHttp` (`src/agents/mcp/server.py`) и hosted MCP tool.
- **Вес.** 38 пакетов / 46 МБ.
- **Железо.** Не найдено.
- **Jetson Nano.** **Да, без откатов:** все последние версии есть в колёсах под glibc 2.27. Для RouterAI нужны Chat Completions и отключённая трассировка (§2). **Raspberry Pi 5:** да.

### 5.11. Google ADK (Python, Go, Java, TS, Kotlin)
- **Что это.** ADK 2.0 (2026-05-19). Workflow Runtime — граф-движок с маршрутизацией, fan-out/fan-in, циклами и HITL. Есть Task API для делегирования между агентами, иерархии агентов, встроенная оценка (eval) и веб-UI. «Optimized for Gemini, model-agnostic».
- **MCP.** `tools/mcp_tool/` (`MCPToolset`, `StdioConnectionParams`/`SseConnectionParams`/`StreamableHTTPConnectionParams`) и `_agent_to_mcp.py` для публикации агента. В Go — `tool/mcptoolset`.
- **Вес.**
  - Python: 48 пакетов / 61 МБ, но в ядре FastAPI, uvicorn, watchdog, graphviz, google-auth.
  - OpenAI-совместимые модели подключаются через LiteLLM: 81 пакет / 242 МБ.
  - **ADK-Go** собирается в статический бинарник (`go 1.26.6`).
- **Железо.** Не найдено.
- **Jetson Nano.** Python-ядро ставится из колёс; с LiteLLM — с откатами. Под RouterAI лучше Go-версия либо Python с собственной обёрткой модели. **Raspberry Pi 5:** да.

### 5.12. PydanticAI
- **Что это.** Типизированный агентский цикл: модели «a string swap away», структурированный вывод, Pydantic Graph, Evals, durable execution (Temporal/DBOS/Prefect). Новая надстройка «Harness» добавляет память, guardrails, субагентов и компакцию. Отдельно поддерживается пакет `genai-prices` с актуальными ценами моделей — полезно для метрики стоимости в ВКР.
- **MCP.** `pydantic_ai/mcp.py` (`MCPToolset`), MCP-клиент на `fastmcp-slim` (extra `mcp`).
- **Вес.** `pydantic-ai-slim[openai,mcp]`: 65 пакетов / 75 МБ. Полный `pydantic-ai`: 97 / 106 МБ.
- **Железо.** Не найдено.
- **Jetson Nano.** **Последняя версия из колёс не ставится:** `tiktoken>=0.12` требует manylinux_2_28. Варианты: (1) PydanticAI 1.38 — устаревшая мажорная ветка; (2) собрать `tiktoken` из исходников (нужен Rust); (3) контейнер. **Raspberry Pi 5 и Orin:** да.

### 5.13. Agno (бывш. phidata)
- **Что это.** SDK агентов, команд и воркфлоу плюс рантайм **AgentOS**: FastAPI-сервис с 50+ эндпоинтами, хранилищем в Postgres, RBAC и UI. В 3.x упор на «agent platform».
- **MCP.** `MCPTools`/`MultiMCPTools`, контекст-провайдеры MCP; AgentOS сам выставляет MCP-сервер.
- **Вес.** 54 пакета / 74 МБ. Ядро лёгкое (10 зависимостей), но extras огромные — 145 штук.
- **Заявленная производительность.** Вендор публиковал «инстанцирование агента ≈ 3 мкс и ≈ 6,6 КиБ на агента» (M4 MacBook Pro, 10.2025) и сравнения с LangGraph, PydanticAI и CrewAI. Это **не RSS процесса**, а стоимость создания объекта. На текущей странице docs.agno.com/get-started/performance чисел я не увидел, а журнал `cookbook/09_evals/performance/comparison/TEST_LOG.md` содержит статус «PENDING». **Не подтверждено.**
- **Железо.** Несколько любительских репозиториев (0 ★).
- **Jetson Nano.** Да, колёса под 2.27 ок, ставится с Python ≥ 3.9. **Raspberry Pi 5:** да.

### 5.14. Strands Agents (AWS)
- **Что это.** «Model-driven» агентский цикл: модель сама планирует, SDK даёт цикл с лимитами (ходы, бюджет токенов, отмена), инструменты, структурированный вывод, MCP, мультиагентные паттерны, память и сессии, guardrails и steering, трассировку (OpenTelemetry), evals и двунаправленный голос (`BidiAgent`). Перечень — по README. Python и TS (монорепо `harness-sdk`), поверх — пакет `strands-harness`.
- **MCP.** `strands/tools/mcp/mcp_client.py` (`MCPClient`), `mcp` в ядре.
- **Вес.** 48 пакетов / 62 МБ. Всегда тянет boto3/botocore.
- **Железо — самая сильная вендорская edge-история среди общих фреймворков.**
  - Блог «Building intelligent physical AI: From edge to cloud…» (2025-12-12): демонстрации с рукой SO-101 (NVIDIA GR00T) и Boston Dynamics Spot, «both … run on NVIDIA Jetson». Модель Jetson не названа, по контексту это не оригинальный Nano.
  - Локальные модели через Ollama или llama.cpp.
  - Образец `samples/python/08-edge/strands-spot-agent`.
  - Пакет `strands-robots` 0.5.2 (PyPI, 2026-09-17, Python ≥ 3.12; репозиторий strands-labs/robots, 176 ★): MuJoCo / Isaac Sim / Newton, режим `mode="real"`; транспорт Zenoh и AWS IoT Core.
  - Блог 2026-08-27 о **Model Hardware Standard (MHS)** — стандарте Anthropic и HHMI Janelia для безопасного управления физическим оборудованием агентами (примитивы `read`/`write`, объявление границ, блокировок и аварийных остановов). Сейчас это limited research preview, Strands Robots в нём участвует. Это прямой аналог «MCP для оборудования» из постановки ВКР; надо сверить с областью B.
  - Сообщество: billai-bass (84 ★) — Strands `BidiAgent` на Raspberry Pi 5.
- **Jetson Nano.** Да, колёса под 2.27 ок. **Raspberry Pi 5:** да, есть публичный пример.

### 5.15. DSPy (Stanford)
- **Что это.** «Программирование, а не промптинг»: сигнатуры, модули (включая ReAct-агента) и оптимизаторы промптов и весов (GEPA и др.). Это скорее инструмент оптимизации поведения LLM, чем рантайм агента.
- **MCP.** `dspy/utils/mcp.py` (`convert_mcp_tool` → `dspy.Tool`), extra `mcp`.
- **Вес.** 66 пакетов / 203 МБ, из них LiteLLM — 36 МБ колесо.
- **Jetson Nano.** Ставится с откатами; на устройстве смысла мало. Оптимизацию промптов можно делать на Mac, а на устройство выносить готовые промпты. **Raspberry Pi 5:** да.

### 5.16. Haystack (deepset)
- **Что это.** Модульные пайплайны (компоненты + граф) для RAG и агентов. Haystack 3.0 вышел 2026-07-20.
- **MCP.** Интеграция `mcp-haystack` (`MCPTool`/`MCPToolset`).
- **Вес.** 56 пакетов / 113 МБ: numpy, networkx и `posthog` для телеметрии (её надо отключать).
- **Jetson Nano.** Ставится. Для агента управления железом избыточен. **Raspberry Pi 5:** да.

### 5.17. Claude Agent SDK (Anthropic)
- **Что это.** SDK поверх рантайма Claude Code: цикл агента, инструменты, файловая система, субагенты, hooks. В Python-колесо **вшит бинарник Claude Code CLI**: колесо linux-aarch64 весит 103 МБ, npm-бинарник linux-arm64 — 249 МБ в распакованном виде.
- **MCP.** Внешние MCP-серверы и in-process «SDK MCP servers» (`sdk_mcp_bridge.py`).
- **Вес.** 30 пакетов / 270 МБ.
- **Модели.** Ориентирован на модели Claude. С RouterAI GPT-mini из постановки ВКР не стыкуется; совместимость RouterAI с Anthropic-форматом не проверял.
- **Jetson Nano.** Колесо помечено manylinux_2_17, но фактическую работу бинарника на JP 4.6 и потребление RAM я не проверял. По модели не подходит. **Raspberry Pi 5:** вероятно да, но не по профилю ВКР.

### 5.18. Letta (бывш. MemGPT)
- **Что это.** Stateful-агенты с управляемой памятью (core/archival memory), идея из статьи MemGPT. На 2026-10 README `letta-ai/letta` сообщает: «The current source code lives in letta-ai/letta-code». Python-сервер V1 ушёл в ветку `archive`. Новый продукт — TypeScript: CLI/TUI, App Server, каналы (Telegram, Slack, Discord), desktop-приложение. Пакет `letta` на PyPI теперь — бинарное колесо Letta Code (78 МБ, manylinux_2_28).
- **MCP.** `src/backend/api/mcp-servers.ts`, `src/cli/commands/mcp.ts` в letta-code.
- **Вес.** npm-замыкание ≈ 447 МБ / 355 пакетов, Node ≥ 22.19. Python-клиент `letta-client` — 10 МБ.
- **Jetson Nano.** Нет: ни Node 22, ни колесо 2_28. Может работать сервер Letta на другой машине и тонкий клиент на устройстве. **Raspberry Pi 5:** возможно (Node 22 есть), не проверено.

### 5.19. Mastra (TypeScript)
- **Что это.** Full-stack TS-фреймворк. По README: model routing (40+ провайдеров), агенты, workflows, human-in-the-loop, управление контекстом и памятью, MCP-серверы, встроенные evals. В зависимостях ядра — Hono (HTTP-сервер). Лицензия NOASSERTION: ядро под Apache-2.0, каталоги `ee/` под Mastra Enterprise License.
- **MCP.** `@mastra/mcp`: и `MCPClient`, и `MCPServer` (`packages/mcp/src/{client,server}`).
- **Вес.** `@mastra/core` 71,8 МБ распакованным; с OpenAI и MCP — ≈ 130 МБ, 167 пакетов. Node **≥ 22.13**.
- **Железо.** Не найдено (0 репозиториев «mastra raspberry pi»).
- **Jetson Nano.** Нет (Node 22). **Raspberry Pi 5:** да.

### 5.20. Vercel AI SDK (TypeScript)
- **Что это.** Провайдер-агностичный тулкит: `generateText`/`streamText` с инструментами, многошаговые циклы, Agent-абстракция, UI-хуки. Самый скачиваемый JS-пакет в этой области: 110 млн в месяц. Внимание: «edge» в мире AI SDK и LangChain.js означает **web edge runtime** (CDN-функции), а не edge-устройства.
- **MCP.** `@ai-sdk/mcp` 2.0.67 (17,6 млн загрузок в месяц).
- **Вес.** ≈ 23 МБ / 20 пакетов — самый лёгкий JS-вариант. Node ≥ 22 (README: «Node.js 22+»).
- **Jetson Nano.** Нет без контейнера. **Raspberry Pi 5:** да.

### 5.21. Eino (CloudWeGo / ByteDance, Go)
- **Что это.** Фреймворк LLM-приложений на Go «по мотивам LangChain и Google ADK». Компоненты (ChatModel, Tool, Retriever, ChatTemplate), граф-композиция (Graph/Chain/Workflow), свой ADK: агенты, мультиагентность, interrupt/resume.
- **MCP.** В eino-ext: `components/tool/mcp` (включая `officialmcp` на официальном Go SDK) и `components/prompt/mcp`.
- **Вес.** Ядро — 38 require, минимальная версия в `go.mod` — 1.18.
- **Jetson Nano.** **Да:** статический arm64-бинарник, кросс-компиляция с Mac. RSS не измерен, ориентир — Go-агент PicoClaw ≈ 31 МБ. **Raspberry Pi 5:** да.

### 5.22. LangChainGo (Go)
- **Что это.** Неофициальный порт LangChain на Go: LLM-клиенты, цепочки, агенты, векторные хранилища.
- **MCP.** **Нативной поддержки нет**: issue #1209 «Is there a plan to support the mcp tool?» открыт. Есть сторонний `i2y/langchaingo-mcp-adapter` (48 ★, последний push 2025-08).
- **Jetson Nano.** Да, как и любой Go. Но отсутствие MCP — минус для ВКР. **Raspberry Pi 5:** да.

### 5.23. Rig (Rust)
- **Что это.** Модульная библиотека LLM-приложений на Rust: агенты, инструменты, RAG, 20+ провайдеров. Поддерживает WASM: `rig-http/wasm_compat`, пример `candle_wasm_chat`. README предупреждает: «future updates will contain breaking changes».
- **MCP.** Crate `rig-rmcp` (интеграция с официальным Rust SDK `rmcp`).
- **Вес.** `rig-core` 0.43.0 весит 1,0 МБ; MSRV 1.95.
- **Чужой бенчмарк.** Вендорский бенчмарк AutoAgents (liquidos-ai) показал для Rig холодный старт 4 мс против 54–63 мс у Python-фреймворков. Но нагрузка там включала разбор parquet, поэтому пиковые RSS ≈ 1–5,7 ГБ отражают данные, а не фреймворк; автор аффилирован с AutoAgents. Для оценки на одноплатнике **не годится**.
- **Jetson Nano.** Да (`aarch64-unknown-linux-gnu`/musl). **Raspberry Pi 5:** да.

### 5.24. Pipecat (Daily, голос и мультимодальность)
- **Что это.** Фреймворк конвейеров реального времени: транспорт → STT → LLM → TTS, мультиагентность через общую шину. Есть **ESP32 Client SDK** (`pipecat-ai/pipecat-esp32`) и C++ client SDK. Типовая архитектура: тонкое устройство-клиент, агент на сервере.
- **MCP.** `services/mcp_service.py` (`MCPClient`).
- **Вес.** 38 пакетов / 409 МБ: numba, llvmlite, scipy, onnxruntime (VAD).
- **Jetson Nano.** Ставится под 2.27 (версии те же), но тяжело. Если в ВКР нужен голосовой интерфейс, на Nano разумнее держать только клиентскую часть. **Raspberry Pi 5:** да, есть любительские проекты.

### 5.25. LiveKit Agents (голос)
- **Что это.** «Realtime, programmable participants that run on servers» на WebRTC (LiveKit), с планировщиком задач и телефонией. Есть **ESP32 client SDK** (`livekit/client-sdk-esp32`).
- **MCP.** `livekit/agents/llm/mcp.py`: `MCPServerHTTP`, `MCPServerStdio`, `MCPToolset`.
- **Вес.** 84 пакета / 322 МБ.
- **Jetson Nano.** **Нет:** нативный `livekit` 1.1.20 только manylinux_2_28_aarch64. Только как клиент к серверу. **Raspberry Pi 5:** да, есть любительские проекты (Jarvis-ассистент на Pi, «nyx-agent» на Pi 4).

---

## 6. Кто на самом деле работает на одноплатниках (сводка доказательств)

| Свидетельство | Что показывает | Фреймворк | Источник |
|---|---|---|---|
| Препринт «Benchmarking Lightweight AI-Agent Frameworks on a 2 GB Raspberry Pi 4» (2026-06-08, не рецензирован) | пиковый RSS: ZeroClaw ≈ 20,5 МБ, PicoClaw ≈ 31,1 МБ, smolagents ≈ 89,6 МБ, NanoBot ≈ 130,5 МБ; 8 экземпляров каждого на 2 ГБ; офлайн-модели не справились | smolagents (+ *Claw) | zenodo.org/records/20593332 |
| nanobot (HKUDS, 48 822 ★) | общий фреймворк не используется: `openai`, `anthropic`, `mcp` напрямую | — | pyproject.toml |
| PicoClaw (sipeed, 30 012 ★, Go) | `openai-go`, `anthropic-sdk-go`, `modelcontextprotocol/go-sdk` напрямую | — | go.mod |
| ForestHubAI/edge-agents (105 ★) | «30 MB … edge AI agent runtime» для Raspberry Pi 5 / Jetson Orin Nano: GPIO, UART, MQTT; Go-движок на `openai-go`/`anthropic-sdk-go` | — | README, go.mod |
| ROSA (NASA JPL) | агент для ROS1/ROS2 | LangChain 0.3 | pyproject.toml |
| RAI (Robotec.ai) | агентный фреймворк для ROS 2 | LangChain 1.x / LangGraph | pyproject.toml |
| gpt-home (652 ★) | домашний ассистент на RPi Zero 2 W / 4B / 5 | LangGraph + LiteLLM | README (заявление) |
| Strands physical AI / Strands Robots / MHS | Jetson, Spot, SO-101, MuJoCo → real, Zenoh / AWS IoT | Strands | блог strandsagents.com, strands-labs/robots |
| billai-bass (84 ★) | голосовой агент на RPi 5 | Strands BidiAgent | README |
| jetson-containers (4 877 ★) | контейнеры `langchain`, `llama-index` для L4T ≥ 34.1 (JetPack 5+); `openai` — для L4T ≥ 32.6; новые пакеты `agents/openclaw`, `zeroclaw`, `hermes-agent` | LangChain, LlamaIndex | packages/*/Dockerfile |
| Поиск GitHub по связке «фреймворк + raspberry pi» (репозитории) | LangChain 12, LangGraph 13, Strands 11, Agno 4, AutoGen 2, smolagents 1, OpenAI Agents 1, ADK 1; CrewAI, LlamaIndex, PydanticAI и Mastra — 0 | — | GitHub search API |

Вывод: **массовых «эталонных» развёртываний общих фреймворков на одноплатниках нет.** Есть робототехника на ROS (LangChain/LangGraph), вендорский physical AI (Strands) и хобби-проекты. Самые популярные агенты для личного железа пишут собственный тонкий цикл.

---

## 7. Что это значит для ВКР (рекомендация к обсуждению с руководителем)

1. **Выбор фреймворка не влияет на MCP**: клиент есть везде (кроме LangChainGo). Поэтому выбирать стоит по трём осям: (а) установка на JetPack 4.6 / glibc 2.27, (б) вес и прозрачность цикла для метрик, (в) научная интерпретируемость — что именно «наше».
2. **Короткий список под Jetson Nano 4 ГБ (или 2 ГБ) с внешней LLM:**
   - **Собственный тонкий цикл на `openai` + `mcp`** (38 МБ; так сделаны nanobot и PicoClaw). Плюсы: полный контроль над учётом токенов, стоимости, задержек и ресурсов «по часам», минимальный вес, ставится на glibc 2.27 без откатов. Минус: оркестрацию, повторы и память придётся писать самим, но это же становится вкладом ВКР.
   - **OpenAI Agents SDK** (46 МБ; MCP в ядре; последние версии ставятся на 2.27 без откатов). Нужны Chat Completions и отключённая трассировка.
   - **MAF core** (48 МБ, GA 1.0, MCP stdio/HTTP/WebSocket, OpenTelemetry) или **Strands** (62 МБ, лучшая edge/robotics-экосистема, Model Hardware Standard; но boto3 в ядре).
   - **LangGraph** (65 МБ). Плюс — связь с ROS-экосистемой (ROSA, RAI) и durable state. Минус — больше абстракций между циклом и метриками.
   - **Альтернатива на Go — Eino**: статический бинарник, MCP через eino-ext, без проблем с glibc и Python на JetPack 4. Сопоставимо с PicoClaw по профилю, но с готовой граф-оркестрацией.
3. **Не брать для оригинального Jetson Nano:** CrewAI (0,7 ГБ, векторные БД в ядре), LlamaIndex, Semantic Kernel (замещён MAF), AutoGen (режим поддержки), LiveKit (не ставится), Claude Agent SDK (бинарник Claude Code и привязка к Claude), Letta/Mastra/AI SDK (нужен Node 22). PydanticAI 2.x — только после сборки `tiktoken` или в контейнере.
4. **Для метрик «часы в день» удобна нативная OpenTelemetry.** По `requires_dist` зависимость opentelemetry-api/sdk есть в ядре MAF, Strands (api+sdk+instrumentation), ADK (api+sdk), autogen-core, pydantic-ai-slim, CrewAI и в самом `mcp`. У OpenAI Agents SDK своя трассировка с подключаемыми процессорами, у LangChain — LangSmith. Стоимость считается по `genai-prices` (Pydantic) или по прайсу RouterAI.
5. **Python на JetPack 4.6:** `uv python install 3.12` (python-build-standalone, glibc ≥ 2.17) плюс `uv venv`. Нужно проверить на реальной плате: колёса aarch64 должны встать. Колесо `uv` 0.11.33 для aarch64 помечено `manylinux_2_17` + `musllinux_1_1` (видно в замыкании CrewAI на 2.27).

---

## 8. Не проверено / открытые вопросы

- RSS, время импорта и холодный старт каждого Python-фреймворка на aarch64. Нужен прогон на Raspberry Pi 5 или Jetson с `/usr/bin/time -f "%M"`, как в препринте. На Mac без установки этого не сделать.
- Фактическая работа на ядре 4.9 (JetPack 4.6):
  - бинарника Claude Code из `claude-agent-sdk`;
  - Python из python-build-standalone;
  - Node 22 в Docker-контейнере.

  Теги manylinux этого не гарантируют.
- Поддерживает ли RouterAI Responses API и параллельные tool calls у моделей GPT mini (влияет на OpenAI Agents SDK).
- Model Hardware Standard: спецификация в limited preview; содержание взято только из блога Strands (2026-08-27) со ссылкой на anthropic.com/news/model-hardware-standard-research-preview. Это нужно проверить в области B (MCP для оборудования).
- Числа Agno (3 мкс / 6,6 КиБ) — только заявление вендора, на текущей странице документации их нет.
- Загрузки PyPI и npm включают CI и зеркала. Высокие числа у Strands (37 млн) и Claude Agent SDK (29,8 млн) вероятно обусловлены транзитивными или облачными установками (AgentCore, Claude Code); это не равно числу разработчиков.

---

## 9. Источники

**Первоисточники-API (2026-10-06):** `api.github.com/repos/*`, `pypi.org/pypi/*/json`, `pypistats.org/api/packages/*/recent`, `registry.npmjs.org/*`, `api.npmjs.org/downloads/*`, `crates.io/api/v1/crates/rig-core`. Сырые ответы — в `L1_artifacts/`.

**README и исходники (GitHub):**
- langchain-ai/langchain (libs/langchain_v1/langchain/mcp/adapter.py), langchain-ai/langgraph
- run-llama/llama_index (README, заметка о LlamaParse)
- microsoft/autogen (баннер «Maintenance Mode»), microsoft/semantic-kernel («SK is now Microsoft Agent Framework»), microsoft/agent-framework (python/packages/core/agent_framework/_mcp.py)
- ag2ai/ag2 (заметка об AG2 Classic), crewAIInc/crewAI
- huggingface/smolagents (src/smolagents/agents.py, mcp_client.py)
- openai/openai-agents-python (src/agents/mcp/server.py, docs/models/index.md, docs/tracing.md)
- google/adk-python (src/google/adk/tools/mcp_tool/*), google/adk-go (tool/mcptoolset)
- pydantic/pydantic-ai (pydantic_ai_slim/pydantic_ai/mcp.py), agno-agi/agno (cookbook/09_evals/performance/comparison)
- strands-agents/harness-sdk (strands-py/src/strands/tools/mcp/mcp_client.py; site/src/content/blog/strands-physical-ai.mdx; robots-working-together-model-hardware-standard-strands-robots.mdx), strands-agents/samples (python/08-edge/strands-spot-agent), strands-labs/robots
- stanfordnlp/dspy (dspy/utils/mcp.py), deepset-ai/haystack
- anthropics/claude-agent-sdk-python, letta-ai/letta, letta-ai/letta-code
- mastra-ai/mastra (packages/mcp), vercel/ai (packages/mcp)
- cloudwego/eino, cloudwego/eino-ext (components/tool/mcp), tmc/langchaingo (issue #1209), 0xPlaygrounds/rig (crates/rig-rmcp)
- pipecat-ai/pipecat (services/mcp_service.py), pipecat-ai/pipecat-esp32, livekit/agents (llm/mcp.py), livekit/client-sdk-esp32
- dusty-nv/jetson-containers (packages/rag/langchain, packages/rag/llama-index, packages/llm/openai, packages/agents/*), dusty-nv/NanoLLM
- nasa-jpl/rosa, RobotecAI/rai, judahpaul16/gpt-home, morganwilliscloud/billai-bass, ForestHubAI/edge-agents, HKUDS/nanobot, sipeed/picoclaw

**Статьи и документация:**
- Dehigaspitiyage Don S. Y. Benchmarking Lightweight AI-Agent Frameworks on a 2 GB Raspberry Pi 4: Framework Overhead, Offline Feasibility, and Fleet Density. Zenodo, 2026-06-08. DOI 10.5281/zenodo.20593332 (препринт, CC-BY-4.0).
- Strands Agents blog: «Building intelligent physical AI: From edge to cloud with Strands Agents…» (2025-12-12); «Robots working together: … Strands Robots and the Model Hardware Standard» (2026-08-27).
- NVIDIA JetPack SDK 4.6.6 release page (developer.nvidia.com/jetpack-sdk-466).
- Node.js BUILDING.md (ветки v20.x, v22.x, v24.x, main) — Supported platforms.
- python-build-standalone docs, «Running Distributions» (минимум glibc 2.17).
- RouterAI docs: routerai.ru/docs/guides/overview/quickstart (base_url `https://routerai.ru/api/v1`).
- dev.to: S. Vishwak, «Benchmarking AI Agent Frameworks in 2026: AutoAgents (Rust) vs LangChain…» — вендорский, используется только как пример непригодной методики.
- Seeed Studio / CNX Software: сравнение Jetson Nano 2 ГБ и 4 ГБ (варианты RAM).
