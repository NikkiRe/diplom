# L4. Jetson Nano как целевая платформа агента

Дата проверки: 2026-10-06. Звёзды и даты репозиториев взяты из GitHub API на эту дату. Версии пакетов взяты из PyPI JSON API, npm registry, Launchpad API, Docker Hub и NGC registry.

Метки достоверности:
- **[V]** — проверено по первоисточнику в этой сессии: документация NVIDIA, README или код, API реестров.
- **[P]** — вторичный источник: блог, форум, сообщество. Первоисточник не найден или не читался.
- **[D]** — мой расчёт из проверенных чисел.
- **[НП]** — не проверено. Это гипотеза или вывод по аналогии.

На устройстве ничего не запускалось: Jetson у меня нет. Всё, что сказано о поведении на Nano, — это вывод из требований и чужих замеров.

Связь с другими заметками:
- `A_claw_analogs.md` — OpenClaw, NanoClaw, PicoClaw и их RAM;
- `F_practical_stack.md` §2.4 — Lima и Docker для эмуляции;
- `E_time_cost_metrics.md` E8 — tegrastats;
- `research/literature/G6_gap.md` B3 — Shalavi'23, калибровка датчиков Jetson.

Здесь они не пересказываются, только используются.

---

## 0. Главное

1. **Jetson Nano на 1 ГБ не существует** [V].
   - Бывают 4 ГБ (Developer Kit ревизий A02 и B01, промышленный модуль с eMMC) и 2 ГБ (Jetson Nano 2GB Developer Kit).
   - Младший Jetson Orin Nano — 4 ГБ, это другое поколение.
   - 1 ГБ бывает у Raspberry Pi 5 и Pi 3/4. Возможно, путаница отсюда.
   - Что именно стоит на кафедре, проверяется командой `free -h` (§7).
2. **Программная платформа заморожена** [V].
   - Последний JetPack 4.6.6 / L4T R32.7.6 — финальный выпуск. EOL JetPack 4 объявлен 22.11.2024: «will no longer receive any critical updates».
   - Внутри: Ubuntu 18.04 (стандартная поддержка кончилась 31.05.2023), ядро 4.9-tegra, glibc 2.27, Python 3.6.9, CUDA 10.2 (compute capability 5.3), TensorRT 8.2.1.
   - Модуль продаётся до января 2027. Developer Kit 4 ГБ и 2 ГБ уже EOL.
3. **Node-агенты нативно не запускаются** [V].
   - Официальные сборки Node.js 18–26 для Linux arm64 требуют glibc ≥ 2.28 и ядро ≥ 4.18. В ветке `main` (будущая 27) требования уже glibc ≥ 2.34 и ядро ≥ 5.14.
   - OpenClaw 2026.9.8 требует Node ≥ 24.16 или ≥ 26.1. NanoClaw требует Node ≥ 22 и Docker.
   - Реальный случай [P]: Node 22 собирали из исходников прямо на Nano 4 ГБ. Ушло ~27 ч 15 мин, до 3,6 ГБ swap и патч V8. Для текущего OpenClaw нужен уже Node 24, сборку пришлось бы повторить.
4. **Go-бинарники со статической линковкой должны работать** [V по требованиям; НП на устройстве].
   - PicoClaw собирается с `CGO_ENABLED=0`, минимум — Linux kernel 3.x и 10 МБ свободной RAM.
   - Официальный MCP Go SDK позволяет писать MCP-серверы для Nano без Python 3.10.
5. **Python для MCP — только ≥ 3.10** [V].
   - Это `requires_python` у `mcp` с первой версии 0.9.1 (11.2024), у `fastmcp` и у `openai`.
   - На 18.04 deadsnakes не помогает: PPA больше не поддерживает bionic. apt даёт максимум 3.8.0.
   - Рабочие пути: **uv + python-build-standalone** (glibc ≥ 2.17), **Miniforge** (glibc ≥ 2.17), сборка из исходников (OpenSSL 1.1.1 в bionic есть) или контейнер.
   - Бинарные колёса зависимостей MCP (pydantic-core, rpds-py, cryptography) есть как `manylinux_2_17_aarch64`, компилировать ничего не придётся.
   - Python 3.10 сам стал EOL 01.10.2026, поэтому цель — 3.12.
6. **Прецедент агента именно на Nano есть** [V]: EdgeSentinel VisionOps (2026-08).
   - Связка: Jetson Nano, USB-камера, Docker и systemd, онлайн-LLM DeepSeek с детерминированным офлайн-режимом.
   - MCP stdio-сервер они написали сами на Python 3.6, без официального SDK.
7. **ROS 2 только в контейнере** [V].
   - Humble рассчитан на Ubuntu 22.04. Для Nano есть `dustynv/ros:humble-ros-base-l4t-r32.7.1`: собран из исходников, декабрь 2023, 712 МБ в сжатом виде.
   - Основная ветка jetson-containers сейчас поддерживает только JetPack 6.2 и 7.
   - Gazebo запускать на Mac или ПК, не на Nano.
8. **Энергия** [V/P].
   - В модуле (не в 2 ГБ!) стоит трёхканальный INA3221: VDD_IN, VDD_GPU, VDD_CPU. В tegrastats это `POM_5V_IN/GPU/CPU`.
   - Он видит только **модуль**, без периферии на несущей плате.
   - По Shalavi'23 занижает до 50 %, после линейной калибровки ошибка ±3 %.
   - Опрос раз в 100 мс стоит < 0,5 % энергии, раз в 10 или 1 мс — > 2 % (Dutt'23).
   - Для «Вт·ч в день» нужен внешний ваттметр на входе 5 В.
9. **Локальные LLM на Nano слабые, руководитель прав** [P+D].
   - Модели ~1B (TinyLlama, Gemma3-1B) генерируют **4–7 ток/с**, Gemma3-4B — ~1,5 ток/с.
   - Ollama не поддерживает GPU на JetPack 4: требования GCC и CUDA 10.2 несовместимы, так сказал мейнтейнер. llama.cpp с CUDA собирается только с патчами.
   - Prefill агентского промпта на 1 500 токенов займёт ~13 с на GPU и ~200 с на CPU. Это до первого токена ответа.
10. **Локальное зрение работает хорошо** [V]: detectNet SSD-Mobilenet-v2 300×300 — 39 кадр/с (FP16, TensorRT, данные NVIDIA 2019). Если LLM внешняя, GPU Nano свободен под зрение. Это архитектурный довод в пользу постановки руководителя.
11. **Эмуляция на Mac.**
    - Docker-образ `nvcr.io/nvidia/l4t-base:r32.7.1` (arm64, Ubuntu 18.04) с `--cpus` и `--memory` точно воспроизводит пользовательское окружение: glibc 2.27, apt-пакеты, Python 3.6.
    - Не воспроизводит ядро 4.9, CUDA, датчики и частоту.
    - Чтобы поймать недопустимые инструкции (A57 — это ARMv8.0-A, без LSE и dotprod), нужен QEMU TCG с `-cpu cortex-a57`.
    - Производительность пересчитывается калибровочными бенчмарками на обеих машинах (§6).

---

## 1. Какие бывают «Jetson Nano»

| Вариант | Появился | Состав (P-номера) | RAM | Хранилище | CSI-камеры | Питание | Датчик мощности | Статус на 2026-10 |
|---|---|---|---|---|---|---|---|---|
| Jetson Nano Developer Kit, ревизия A02 | 18.03.2019, $99 [V: блог NVIDIA] | P3450 = модуль P3448-0000 + несущая плата P3449-0000 [V: L4T docs] | 4 ГБ LPDDR4, 64 бит | microSD на модуле | 1 | micro-USB 5 В⎓2 А [V] или DC-разъём (перемычка J48; 5 В⎓4 А [P: JetsonHacks]) | INA3221 [V] | Dev Kit — EOL [V] |
| Jetson Nano Developer Kit, ревизия B01 | январь 2020 [P: CNX] | тот же P3450, новая ревизия несущей платы | 4 ГБ | microSD | **2** [P: CNX, Arducam] | то же; J48 перенесена к краю платы | INA3221 | EOL |
| Jetson Nano 2GB Developer Kit | октябрь 2020, $54 / $59 с Wi-Fi [P: CNX] | P3541 = модуль P3448-0003 + несущая плата P3542-0000 [V] | **2 ГБ** | microSD | 1 | USB-C 5 В⎓3 А или micro-USB 5 В⎓2 А (перемычка) [P: CNX] | **нет INA3221** [V: L4T docs] | EOL [V] |
| Jetson Nano (промышленный модуль) | — | P3448-0002 [V] | 4 ГБ | 16 ГБ eMMC 5.1 [V] | зависит от несущей платы (до 4 камер, 12 линий MIPI CSI-2) | от несущей платы | INA3221 | «Available through January 2027» [V: Jetson Product Lifecycle] |
| *Не путать:* Jetson Orin Nano 4 ГБ / 8 ГБ, Orin Nano Super Dev Kit 8 ГБ | другое поколение (даты выпуска не проверял) | — | 4 / 8 ГБ LPDDR5 (Super: 128 бит, 102 ГБ/с) | SD + NVMe | — | 7–25 Вт | свои датчики | модули до января 2032 [V] |

Дополнительно:
- На 2 ГБ версии, чтобы сэкономить память, вместо GNOME стоит LXDE с Openbox [P: CNX]. На ней также нет глубокого сна SC7 [V: L4T docs].
- Среди модулей Jetson на сайте NVIDIA вариантов на 1 ГБ нет. Самый маленький из перечисленных — Orin Nano 4GB [V: developer.nvidia.com/embedded/jetson-modules].

---

## 2. Характеристики (официальные) [V]

- **SoC:** Tegra X1 (T210). Так он назван в документации tegrastats, а CUDA показывает «NVIDIA Tegra X1, compute capability 5.3».
- **CPU:** 4 × Arm Cortex-A57 MPCore, архитектура ARMv8.0-A.
  - Частота заявлена как 1,43 ГГц. В таблице nvpmodel максимум 1479 МГц.
  - Расширений ARMv8.1+ нет: ни атомарных LSE, ни dotprod. Отсюда SIGILL у бинарников, собранных под armv8.2-a [P: обсуждения proton-arm64-a57, Ollama PR #17654].
- **GPU:** Maxwell, 128 CUDA-ядер, 921 МГц, 472 GFLOPS (FP16). Максимальная CUDA — 10.2. CUDA 11+ на Nano не ставится [V: Qengineering README; так же по EOL JetPack 4].
- **Память:** 4 ГБ, 64 бит, LPDDR4-1600, теоретически 25,6 ГБ/с. **Общая для CPU и GPU.** В примере tegrastats для T210 всего доступно 3955 МБ.
  - Замер сообщества: CPU через `sysbench memory` получает ~6,9 ГБ/с (27 % теоретического), GPU в `bandwidthTest` device-to-device — 16,5 ГБ/с [P: kreier/llama.cpp-jetson].
- **Режимы питания** (nvpmodel; Jetson Linux R32.7.6, Power Management for Jetson Nano):

  | Режим | ID | Бюджет | Ядер CPU онлайн | CPU max, МГц | GPU max, МГц | EMC max, МГц |
  |---|---|---|---|---|---|---|
  | MAXN (по умолчанию) | 0 | 10 Вт | 4 | 1479 | 921,6 | 1600 |
  | 5W | 1 | 5 Вт | 2 | 918 | 640 | 1600 |

  Команды: `sudo nvpmodel -q` (текущий режим), `sudo nvpmodel -m 1` (переключить). Режим сохраняется между перезагрузками.
- **Интерфейсы модуля:** GbE, PCIe Gen2 x4, 1 × USB 3.0 и 3 × USB 2.0, I2C/SPI/UART/I2S/GPIO, HDMI 2.0 и eDP 1.4. Видео: кодирование 4K@30 HEVC (250 Мп/с), декодирование 4K@60 (500 Мп/с).
- **Камеры:** 12 линий MIPI CSI-2 (до 4 камер). Raspberry Pi Camera v2 (IMX219) поддерживается на Nano [V: jetson-inference docs]. На Dev Kit B01 два разъёма CSI, на A02 и 2 ГБ — один.

---

## 3. Программный стек JetPack 4.6.x и что из него следует [V]

| Компонент | Версия на Nano | Источник |
|---|---|---|
| JetPack / Jetson Linux | **4.6.6 / R32.7.6** — финальный; «the same as JetPack 4.6.5 but includes Jetson Linux 32.7.6» (исправления безопасности, новая DRAM) | developer.nvidia.com/jetpack-sdk-466; форум NVIDIA, 22.11.2024 |
| Ядро | 4.9.x-tegra (на JetPack 4.6.1 — 4.9.253, после обновления до 4.6.6 — 4.9.337 [P: kreier]) | L4T docs, kreier |
| Ubuntu | 18.04 (bionic); стандартная поддержка до 31.05.2023, ESM до 26.04.2028 | endoflife.date |
| glibc | **2.27**-3ubuntu1.6 | Launchpad API |
| Python | **3.6.9** (`python3.6` 3.6.9-1~18.04ubuntu1.12); в apt также `python3.7` 3.7.5 и `python3.8` **3.8.0** | Launchpad API |
| OpenSSL | 1.1.1 (bionic-updates) — хватает для сборки Python 3.10+ | Launchpad API |
| GCC / CMake | gcc 7.5 по умолчанию, `gcc-8` 8.4.0 в apt; cmake 3.10.2 (llama.cpp требует ≥ 3.14) | Launchpad API; CMakeLists llama.cpp |
| Docker | `docker.io` 20.10.21 в bionic-updates | Launchpad API |
| CUDA / cuDNN / TensorRT / VPI / OpenCV | 10.2 / 8.2.1 / 8.2.1 / 1.2 / 4.1.1 | страница JetPack 4.6.6 (компоненты как в 4.6.1) |
| Vulkan | 1.2 [P: README Z841973620 со ссылкой на NVIDIA] | — |

### 3.1. Node.js: требования платформы [V: `BUILDING.md` в ветках nodejs/node]

| Линия Node | Статус (endoflife.date) | Linux arm64 Tier 1 | На Nano нативно |
|---|---|---|---|
| 18 | EOL 30.04.2025 | kernel ≥ 4.18, **glibc ≥ 2.28** | нет |
| 20 | EOL 30.04.2026 | то же | нет |
| 22 | LTS до 30.04.2027 | то же | нет |
| 24 | LTS до 30.04.2028 | то же | нет |
| 26 | Current, LTS с 28.10.2026 | то же | нет |
| `main` (будущая 27) | — | **kernel ≥ 5.14, glibc ≥ 2.34** | нет (и в контейнере ядро 4.9 < 5.14) |

Комментарии к таблице:
- Сноска в `BUILDING.md`: «Older kernel versions may work. However, official Node.js release binaries are built on RHEL 8 systems with kernel 4.18».
- Неофициальные сборки (unofficial-builds.nodejs.org) для arm64 есть **только `linux-arm64-musl`**. Вариант на старой glibc (`glibc-217`) собирают только для x64 [V: index.json, v26.10.0].
- Итог: готового бинарника Node ≥ 18 под glibc 2.27 на aarch64 нет.

Что это значит для конкретных агентов:
- **OpenClaw 2026.9.8** [V: npm]:
  - `engines.node` = `>=24.16.0 <25 || >=26.1.0`, 66 прямых зависимостей, распакованный пакет 389 МБ;
  - в документации: «Node 24.16+ or Node 26.1+», «glibc-based Linux»;
  - по независимому замеру (Salman et al. 2026, см. A_claw_analogs) медиана пиковой памяти — 2926,6 МиБ. На Nano 4 ГБ с общей памятью CPU/GPU это почти вся RAM.
  - Учебник Jetson AI Lab по OpenClaw рассчитан только на Orin Nano 8 ГБ и AGX [V].
- **NanoClaw** (репозиторий `qwibitai/nanoclaw` переехал в `nanocoai/nanoclaw`) [V: package.json, README]:
  - `engines.node >=22`, pnpm 10+, Docker;
  - агент выполняется в контейнере, внутри Bun и Claude Agent SDK.
- **Реальный опыт OpenClaw на Nano 4 ГБ** [P: блог brtkwr.com, 08.02 и 02.03.2026]:
  - сначала запуск через Bun вместо Node, потому что «NodeSource doesn't publish Node 22 binaries…»;
  - потом Bun сломался на проверке манифестов плагинов в OpenClaw 2026.2.26+ (gateway ушёл в цикл из 408 перезапусков);
  - затем Node 22.22.0 собрали из исходников: **~27 ч 15 мин**, `cc1plus` занимал до 3,6 ГБ swap, понадобился патч `deps/v8/src/heap/base/memory-tagging.cc` (ассемблер не знает расширение `memtag`);
  - установка: `npm install -g openclaw@latest --ignore-scripts`.
- **Обходные пути:**
  - (а) **Контейнер** Ubuntu 22.04/24.04 arm64 с Node 24. glibc в контейнере подходит, но ядро 4.9 ниже заявленного минимума 4.18 [НП: «may work»]. Для образов на glibc ≥ 2.34 нужен Docker ≥ 20.10.10 из-за `clone3`: старый seccomp возвращал EPERM вместо ENOSYS [V: заметки к релизу Docker 20.10; moby PR #42836]. В bionic-updates есть 20.10.21.
  - (б) **Bun** нативно: «Bun's glibc binaries require glibc 2.17 or newer», «runs on kernels as old as 3.10» [V: bun.com/docs/installation]. Совместимость с OpenClaw неустойчива, см. выше.
  - (в) **Образ Ubuntu 20.04 от Qengineering** (glibc 2.31 при JetPack 4.6.1) [V: README]. Образ неофициальный, весит 8,7 ГБ в xz, последнее обновление 17.09.2023, ядро остаётся 4.9.
  - (г) Своя glibc 2.28 параллельно системной [P: статья на Medium, текст не прочитан (403)].

### 3.2. Go и Rust

- **PicoClaw** [V: `.goreleaser.yaml`, `go.mod`, docs/guides/hardware-compatibility.md, релизы]:
  - сборка с `CGO_ENABLED=0`, то есть статический бинарник; модуль на go 1.25.13;
  - v0.3.1 от 03.07.2026, архив `picoclaw_Linux_arm64.tar.gz` весит 21,9 МБ;
  - минимум: «Linux (kernel 3.x+)», RAM 10 МБ свободно (рекомендовано 32 МБ+);
  - Go ≥ 1.24 требует ядро ≥ 3.2 [V: go.dev/wiki/MinimumRequirements];
  - **Jetson в списке совместимого железа нет**, отчётов о запуске на Nano я не нашёл [НП];
  - оговорка: в примерах MCP-серверы запускаются через `npx` (`picoclaw mcp add filesystem -- npx -y @modelcontextprotocol/server-filesystem`). На Nano Node нет, поэтому MCP-серверы для PicoClaw придётся писать на Go или Python 3.12.
- **Официальный MCP Go SDK** `modelcontextprotocol/go-sdk` [V]: 5188★, v1.8.0 от 14.09.2026, go 1.25. С `CGO_ENABLED=0` даёт статический бинарник, то есть естественный язык для MCP-серверов «контекста оборудования» на Nano. Альтернатива — `mark3labs/mcp-go` (9153★, MIT).
- **bubbaloop** (kornia; Rust, Apache-2.0, 28★) [V: README, release.yml]:
  - «Hardware AI agent», один бинарник ~13 МБ, MCP-сервер на 47 инструментов, в описании «Runs on Jetson»;
  - но arm64-релиз собран на `ubuntu-24.04-arm` под `aarch64-unknown-linux-gnu`. Скорее всего, ему нужна glibc новее 2.27 [НП: символы бинарника не проверял].

### 3.3. Python ≥ 3.10 на Ubuntu 18.04

Требования ключевых пакетов [V: PyPI JSON]:

| Пакет | Последняя версия | requires_python | Последняя версия под Python 3.6 |
|---|---|---|---|
| `mcp` (официальный SDK) | 2.3.0 (02.10.2026) | ≥ 3.10 (и у 0.9.1 от 20.11.2024) | нет |
| `fastmcp` | 4.0.11 | ≥ 3.10 | нет |
| `openai` | 3.24.0 | ≥ 3.10 | 0.10.5 (01.10.2021, старый API) |
| `requests` | 2.34.2 | ≥ 3.10 | 2.27.1 |
| `httpx` | 0.28.1 | ≥ 3.8 | 0.22.0 |
| `jetson-stats` (jtop) | 7.2.2 (01.10.2026) | ≥ 3.7 | **4.3.2** (19.03.2025, `>=2.7`) |

Сроки поддержки Python (endoflife.date): 3.6 — EOL 23.12.2021, 3.8 — 07.10.2024, **3.10 — 01.10.2026**, 3.12 — 31.10.2028. Цель для агента — **3.12**.

Варианты установки:

| Способ | Требование к хосту | Вывод для Nano |
|---|---|---|
| **uv + python-build-standalone** | Python-сборки: «minimum glibc version required for most targets is 2.17» [V]. У uv есть «fully statically linked» musl-бинарники [V: docs.astral.sh/uv/reference/policies/platforms]; glibc-сборка uv для aarch64 требует manylinux_2_28 | **Лучший вариант** [НП на устройстве]. `uv-aarch64-unknown-linux-musl` (21 МБ), затем `uv python install 3.12` (cpython-3.12.15 aarch64-gnu, 52 МБ) |
| **Miniforge (conda-forge)** | `Miniforge3-Linux-aarch64.sh`: glibc ≥ 2.17 [V: README]. Базовая glibc conda-forge пока 2.17, issue о переходе на 2.28 открыт, переход «likely still years away» [V] | Работает. Тяжелее uv |
| pyenv / сборка из исходников | OpenSSL ≥ 1.1.1 есть [V] | Работает. Время сборки на A57 не измерено [НП] |
| deadsnakes PPA | поддерживает только 22.04, 24.04 и 26.04 [V: Launchpad] | **Нет** |
| apt bionic | максимум `python3.8` 3.8.0 [V] | Мало для MCP |
| Контейнер `python:3.12-slim` arm64 | Docker ≥ 20.10.10 (из-за `clone3`) | Работает [НП на ядре 4.9] |

- **Бинарные колёса** для aarch64 [V: PyPI]: `pydantic-core` 2.49.0, `rpds-py`, `cryptography` 50.0.2, `uvloop`, `httptools` публикуются как `manylinux_2_17_aarch64` / `manylinux2014`. На glibc 2.27 `pip install mcp` под Python 3.12 пройдёт без компиляции Rust и C [НП на устройстве].
- **Прецедент Python 3.6 без SDK** [V: код репозитория]: в EdgeSentinel VisionOps `packages/mcp/server.py` написан как «Minimal UTF-8 MCP stdio server», `PREFERRED_PROTOCOL_VERSION = "2025-11-25"`. Зависимости закреплены в `requirements-api-py36.txt`: fastapi 0.83.0, pydantic 1.9.2 и бэкпорты `dataclasses` и `contextvars`.
- **Мелочь про HTTPS** [рекомендация, НП]: Python 3.6 с OpenSSL 1.1.1 умеет TLS 1.3. Но системный набор корневых сертификатов у EOL-дистрибутива может устареть. Для запросов к API LLM лучше брать `certifi`.

### 3.4. Контейнеры на Nano

- **jetson-containers** (dusty-nv, 4877★) [V: README]:
  - основная ветка: «Only Tested and supported Jetpack 6.2 (Cuda 12.6) and JetPack 7»;
  - старая версия лежит в ветке `legacy`.
- **Готовые образы dustynv для r32** [V: Docker Hub API]:
  - `dustynv/ros` даёт 51 тег, среди них `humble-ros-base-l4t-r32.7.1`, `humble-desktop-…`, `iron-…`, `foxy-…`, `noetic-…`, `melodic-…`;
  - **`dustynv/llama_cpp`, `dustynv/ollama` и `dustynv/nano_llm` тегов r32 не имеют**, только r35.4.1 и r36.x.

  Иначе говоря, генеративный стек NVIDIA (NanoLLM, учебники Jetson AI Lab) на оригинальный Nano не рассчитан.
- **`nvcr.io/nvidia/l4t-base:r32.7.1`** [V: NGC registry]:
  - arm64, 245,5 МБ в сжатом виде, создан 15.12.2021;
  - нижний слой — Ubuntu 18.04 (тот же digest слоя, что и в `dustynv/ros`);
  - внутри частичная CUDA 10.2: bin, nvvm, include, заглушки. Остальное на Jetson монтирует nvidia-container-runtime с хоста [P: по слоям образа и устройству JetPack 4];
  - последний тег r32 — `r32.7.1`, образа r32.7.6 нет.

### 3.5. ROS 2

- Сроки поддержки (endoflife.date) [V]: Humble — 23.05.2022…31.05.2027; Jazzy — до 31.05.2029; Kilted — до 31.12.2026; Lyrical — 22.05.2026…31.05.2031.
- **На Nano** [V: Docker Hub и registry API, конфиг образа]:
  - образ `dustynv/ros:humble-ros-base-l4t-r32.7.1` обновлён 06.12.2023, 711,5 МБ в сжатом виде;
  - база — l4t-base r32.7.1 (Ubuntu 18.04), ROS собран из исходников (`ros2_build.sh`), `RMW_IMPLEMENTATION=rmw_cyclonedds_cpp`;
  - python3 поставлен из apt bionic, значит, 3.6 [НП: вывод по истории сборки, версию явно не ставили].
- **На Mac:** официальные `ros:humble-ros-base` (arm64v8, 259,5 МБ) и `ros:jazzy-ros-base` (arm64, 287,4 МБ) обновлены 02.10.2026 [V].
- **jetson-inference** даёт ROS-узлы через `dusty-nv/ros_deep_learning` (983★, последний push 13.07.2024) [V].
- Gazebo на Nano нецелесообразен: GPU Maxwell и 4 ГБ общей памяти [НП: моя оценка]. Схема как в F_practical_stack: симулятор на Mac или ПК, на плате ROS-узлы или rosbridge.

### 3.6. Локальные LLM на Nano: числа сообщества

Основной источник [P]: `kreier/llama.cpp-jetson` (MIT, 32★, создан 05.04.2025, последний push 23.02.2026). Условия: Jetson Nano 4 ГБ 2019 года без разгона, llama.cpp b5043–b5058 (апрель 2025), Ollama 0.6.4.

| Модель (GGUF) | Размер | Режим | Prefill, ток/с | Генерация, ток/с |
|---|---|---|---|---|
| TinyLlama-1.1B-Chat Q4_K_M | 636 МиБ | `llama-bench`, CUDA, ngl 99 | 80,57 (pp512) | 6,69 (tg128) |
| TinyLlama-1.1B | — | CPU-сборка, `llama-cli` (среднее) | — | ≈5,25 |
| Gemma3-1B Q4_K_M | 762 МиБ | `llama-bench`, CUDA, ngl 27 | 115,75 (pp512) | 5,84 (tg128) |
| Gemma3-1B, 11 промптов по 12–76 токенов | — | Ollama 0.6.4, CPU | 9,56 ± 0,3 | 4,69 ± 0,23 |
| — | — | llama.cpp CPU b5058 | 7,5 ± 0,3 | 4,22 ± 0,14 |
| — | — | llama.cpp GPU b5050 | 23,29 ± 9,6 | 5,26 ± 0,37 |
| Gemma3-4B Q4_K_M (unsloth) | 3,3 ГБ файл | Ollama CPU | 2,32 | 1,61 |
| Gemma3-4B Q2_K | 1,7 ГБ | llama.cpp, все слои на GPU (влезла только Q2) | 1,77 | 1,52 |

Детали из того же источника:
- Gemma3:1b в Ollama на Jetson занимает 1,9 ГБ RAM, 4B Q4_K_M — 2,8 ГБ.
- При генерации на GPU: загрузка 100 %, 1,5 ГБ памяти GPU, ~4 Вт на GPU и ~0,6 Вт на CPU.
- Сборка с CUDA требует gcc 8.5 (собирается 3 ч), cmake ≥ 3.14 (38 мин), затем 85 мин компиляции llama.cpp.
- Патчи: убрать `constexpr`, закомментировать `__builtin_assume`, заменить `bfloat16` на `half`.
- Первая загрузка модели — ~6,5 мин, повторные — 12 с.

Другие источники и оговорки:
- Форк `Z841973620/llama.cpp-jetpack4.6` (10★, push 20.09.2026) [P]: основан на llama.cpp b8056, bf16 вырезан. Автор **прекращает поддержку CUDA** и советует бэкенд Vulkan (L4T r32.7 поддерживает Vulkan 1.2). Производительность Vulkan на Nano никто не мерил [НП].
- **Ollama** [V: issue ollama/ollama#4140]: мейнтейнер (dhiltgen, 25.07.2024) — «the GCC version requirements between CUDA v10 and llama.cpp seem to be mutually exclusive… JP5 looks like it's going to be the oldest version we'll be able to support». В режиме CPU Ollama 0.6.4 на Nano работал [P: kreier]. Запустится ли текущий arm64-бинарник Ollama на glibc 2.27, неизвестно [НП].
- Отдельно есть рецензируемые замеры SLM на Nano, Pi 5 и Orin Nano с внешним Otii Ace Pro на 100 Гц: Islam et al., IEEE MASS 2025. Они разобраны в F_practical_stack [F10] и G_edge_ai_onboard [C2].

**Расчёт для агента [D].** Возьмём статическую часть промпта агента с каталогом инструментов ~1 500 токенов (оценка из `research/ENVIRONMENT.md`):
- prefill на GPU Nano (Gemma3-1B, pp512 ≈ 116 ток/с): ≈13 с;
- на CPU (≈7,5 ток/с): ≈200 с;
- генерация вызова инструмента на 40 токенов при ~5,3 ток/с: ≈7,5 с;
- **итого шаг агента ≥ 20 с на GPU и минуты на CPU**, причём для 1B-модели, у которой надёжность вызова инструментов низкая (см. C и G заметки).

Для сравнения: на M4 Pro (Metal) 8B-модель проходит prefill тех же 1 500 токенов за ≈4,3 с. Вывод: на Nano локальная LLM годится разве что как аварийная деградация («офлайн-правила», как в EdgeSentinel). Основной разум должен быть внешним.

### 3.7. Локальное зрение: jetson-inference [V]

- **Репозиторий:** `dusty-nv/jetson-inference`, 9010★, MIT, C++ и Python, последний push 16.10.2025.
- **Поддержка:** «Jetson Nano Developer Kit with JetPack 4.2 or newer», «Jetson Nano 2GB… JetPack 4.4.1 or newer».
- **Возможности:** detectNet (по умолчанию SSD-Mobilenet-v2, 91 класс COCO), imageNet, segNet, poseNet, actionNet.
- **Числа для Nano:**
  - классификация GoogleNet и ResNet-18 — «up to around ~75 FPS»;
  - сегментация FCN-ResNet18 (FP16, JetPack 4.2.1, MAXN): Cityscapes 512×256 — 48 кадр/с, Pascal VOC 320×320 — 45 кадр/с, DeepScene 576×320 — 26 кадр/с.
- **Таблица NVIDIA из блога 18.03.2019** (FP16, batch 1, TensorRT):

  | Модель | Кадр/с |
  |---|---|
  | SSD Mobilenet-V2 300×300 | **39** |
  | SSD Mobilenet-V2 480×272 | 27 |
  | Tiny YOLO V3 416×416 | 25 |
  | ResNet-50 224×224 | 36 |
  | OpenPose 256×256 | 14 |

  Страница «Jetson Nano DL inference benchmarks» теперь показывает только MLPerf для Orin и Thor.
- **Источники видео (videoSource):** `csi://0`, `v4l2:///dev/video0`, `rtsp://…`, `webrtc://…`, `file://…`. Последние два позволяют подавать записанное видео — это удобно для повторяемых экспериментов и для mock-режима.

---

## 4. Измерение ресурсов и энергии на Nano

- **Датчик** [V: Jetson Linux R32.7.6, Power Management for Jetson Nano]:
  - «The original Jetson Nano and Jetson TX1 modules have a three-channel INA3221 power monitor at I2C address 0x40»;
  - sysfs: `/sys/bus/i2c/drivers/ina3221x/6-0040/iio:device0/`, файлы `rail_name_<N>`, `in_current<N>_input` (мА), `in_voltage<N>_input` (мВ), `in_power<N>_input` (мВт);
  - каналы: 0 — `VDD_IN` (вход модуля), 1 — `VDD_GPU`, 2 — `VDD_CPU`;
  - «Do not modify any INA sysfs node value… can result in damage»;
  - **«Jetson Nano 2GB does not have an INA3221 power monitor»**.
- **tegrastats** [V: L4T docs, Appendix tegrastats; P: форумы]:
  - выводит RAM (used/total и largest free block), SWAP, загрузку и частоту каждого ядра, `EMC_FREQ`, `GR3D_FREQ`, температуры и шины питания в формате «мгновенная/средняя, мВт»;
  - на Nano шины называются `POM_5V_IN`, `POM_5V_GPU`, `POM_5V_CPU` [P: форумы NVIDIA];
  - интервал по умолчанию 1000 мс, задаётся через `--interval`.
- **Что видит датчик** [P]:
  - модератор NVIDIA: `POM_5V_IN` — «5v of module only»;
  - пользователи на форуме сообщают, что `POM_5V_IN` ≤ показаний лабораторного БП;
  - USB-периферию (камеру, Wi-Fi-донгл) датчик не учитывает.
- **Точность:**
  - сам чип INA3221 по даташиту: смещение ±80 мкВ (max), ошибка усиления 0,25 % (max), 13-битный АЦП [V: ti.com/product/INA3221]; погрешность системы определяется шунтом и усреднением;
  - **Shalavi et al., IEEE EDGE 2023** (Nano включён): занижение до 50 %, после линейной калибровки против осциллографа ±3 % [V: разобрано в G6_gap B3];
  - **Dutt et al., IGSC 2023** (Nano и Xavier NX): опрос датчиков раз в 100 мс стоит < 0,5 % накладных; 10 мс и 1 мс — > 2 % по энергии. Всю периферию при замерах отключали [V: PDF прочитан];
  - Holly et al., IGSC 2020: из-за ограниченного временного разрешения INA3221 энергия по слоям расходится с энергией всей сети [P: аннотация].
- **jtop / jetson-stats** [V: PyPI, README]:
  - 2632★, AGPL-3.0, 37 тыс. загрузок в месяц (pypistats);
  - версия 7.2.2 требует Python ≥ 3.7 (зависимости `smbus2`, `distro`, `nvidia-ml-py`). На стоковом Python 3.6 pip поставит **4.3.2**;
  - служба jtop отдаёт данные через сокет `/run/jtop.sock`, его можно прокинуть в контейнер (`-v /run/jtop.sock:/run/jtop.sock`). MCP-сервер на Python 3.12 в контейнере сможет читать телеметрию через `from jtop import jtop`;
  - `jetson_release` печатает модель, L4T и JetPack.
- **Рекомендация для ВКР:**
  - (1) внешний ваттметр на входе 5 В (DC-разъём или USB-C у 2 ГБ) — это граница «вся плата», для «Вт·ч в день»;
  - (2) INA3221 — для разложения на CPU и GPU и для коротких интервалов, с калибровкой по классам нагрузки (G6);
  - (3) опрос не чаще 10 Гц;
  - (4) на 2 ГБ-версии остаётся только внешний прибор.

---

## 5. Существующие проекты «агент / MCP + Jetson» (что уже сделано)

| Проект | ★ / даты | Что это | Работает на оригинальном Nano? |
|---|---|---|---|
| **zja0011/edgesentinel-visionops** [V] | 22★; создан 10.08.2026, push 14.08.2026; Python; Apache-2.0 | «Jetson Nano edge AI vision Agent Harness». Детекция и трекинг, зоны, инвентаризация, журнал событий с защитой от подделки, 33 инструмента и 25 MCP-инструментов, DeepSeek онлайн с детерминированным офлайн-режимом, RBAC и «гейты подтверждения». Docker и systemd | **Да** (заявлено и показано на видео): контейнер JetPack 4 / Python 3.6.9, свой MCP stdio-сервер |
| Zalmotek/jetson-mcp [V] | 11★; 29.04.2025, разовый | MCP-сервер на FastMCP (SSE): `get_jetson_sw_info` (`/etc/nv_tegra_release`, `/proc/version`) и др. | Не на стоковом Python 3.6: `fastmcp` требует ≥ 3.10 |
| lhzn-io/jetson-telemetry-mcp [V] | 1★; 06.05–02.10.2026 | Read-only MCP по Streamable HTTP: шины питания, режимы, температуры, L4T. MCP Python SDK 2.x (ревизия 2026-07-28), Docker. Пример — AGX Orin | Только в контейнере с Python ≥ 3.10 [НП] |
| kornia/bubbaloop [V] | 28★; 20.12.2025–26.06.2026; Rust | «Hardware AI agent» (камеры, датчики, флоты), MCP-сервер на 47 инструментов | Вероятно, нет из-за glibc (сборка на Ubuntu 24.04) [НП] |
| GmausDev/nanobot-jetson [P] | 0★; push 21.02.2026 | Полностью локальный голосовой робот на Nano 4 ГБ: whisper.cpp, llama.cpp TinyLlama, Piper. Бюджет памяти ~2,1 ГБ из 4, отдельно «~900MB» на llama.cpp с TinyLlama Q4 | Да (заявлено) |
| OpenClaw на Nano (блог brtkwr) [P] | 02–03.2026 | Сборка Node из исходников, gateway через systemd | Да, но ценой ~27 ч сборки Node; текущий OpenClaw требует Node 24 |

Вывод:
- Ниша «лёгкий агент + внешняя LLM + MCP-контекст оборудования **на оригинальном Nano** с измерением ресурсов во времени» почти пуста.
- Единственный близкий прецедент — EdgeSentinel. Он доказывает реализуемость, но замеров энергии и стоимости токенов во времени в нём нет [НП: README прочитан не целиком].

---

## 6. Эмуляция Jetson Nano на MacBook (M4 Pro, 24 ГБ)

Цель — заранее поймать три класса проблем: несовместимость userland (glibc 2.27 и старые пакеты), недопустимые инструкции и нехватку RAM. Производительность при этом пересчитывается калибровкой.

| Вариант | Что воспроизводит | Чего не воспроизводит | Как |
|---|---|---|---|
| **A. Docker, образ `nvcr.io/nvidia/l4t-base:r32.7.1`** (или `ubuntu:18.04` arm64) | glibc 2.27, Python 3.6.9, версии apt-пакетов, структуру L4T-контейнера, лимиты RAM и CPU через cgroup | ядро 4.9-tegra (в Docker VM ядро 6.x), CUDA, TensorRT, VPI, датчики INA3221 и tegrastats, CSI, частоту и полосу памяти, набор инструкций (M4 исполняет ARMv8.6+) | `docker run --rm -it --platform linux/arm64 --cpus=4 --memory=3.5g --memory-swap=5.5g nvcr.io/nvidia/l4t-base:r32.7.1 bash`; внутри проверить `ldd --version`, `python3 --version` [команда не запускалась] |
| **B. Lima VM с облачным образом Ubuntu 18.04 arm64** | полноценная ВМ с systemd, ядро 4.15 (ближе к 4.9, чем 6.x) | то же, кроме части про ядро | Шаблона 18.04 в Lima 2.2.1 нет (шаблоны начинаются с 20.04) [V]. Образ `ubuntu-18.04-server-cloudimg-arm64.img` (377 МБ, 07.06.2023) есть на cloud-images.ubuntu.com [V]. `cpus: 4`, `memory: 4GiB`. Загрузится ли 18.04 под `vz`, не проверено; запасной вариант — `vmType: qemu` [НП] |
| **C. QEMU TCG, `-cpu cortex-a57`** | **набор инструкций ARMv8.0-A**: ловит SIGILL от LSE, dotprod и fp16-арифметики в бинарниках, собранных на Mac | время: эмуляция медленнее в разы и не отражает A57 | Плата QEMU `virt` поддерживает `cortex-a57` [V: документация QEMU]. В Lima — `vmOpts.qemu.cpuType` [V: default.yaml]. Только для дымовых тестов бинарников |

Как компенсировать то, что частоту ограничить нельзя:
1. **Квота CPU** (`--cpus`, CFS) режет долю времени, а не частоту. Короткая однопоточная операция на ядре M4 всё равно выполняется быстро (см. F_practical_stack [F12]). Поэтому квоту стоит применять только для «фоновой» нагрузки.
2. **Калибровка коэффициентов.** Одинаковые бенчмарки прогоняются на Mac в контейнере (`--cpus=4`) и на Nano, отдельно на CPU и на память:
   - `llama-bench -m TinyLlama-1.1B-Q4_K_M -t 4` (для Nano уже есть опорные ≈5,25 ток/с генерации на CPU);
   - `sysbench memory` (на Nano ≈6,9 ГБ/с);
   - микробенчмарк шага агента: разбор и валидация JSON, сборка промпта, запись в SQLite.

   Из этого получаются k_cpu и k_mem. Задержка шага агента в эмуляции считается как T = T_сеть (меряется как есть) + k·T_CPU [D: методика]. Для ориентира: теоретическая полоса памяти отличается в 273 / 25,6 ≈ 10,7 раза (M4 Pro против Nano), а по CPU-замеру Nano (6,9 ГБ/с) разрыв ещё больше [D].
3. **RAM.** Для 4 ГБ — `--memory=3.5g` (headless, без рабочего стола), для 2 ГБ — `--memory=1.5g`. zram и общий пул памяти с GPU не эмулируются.
4. **Сборка под цель.** Флаги `-march=armv8-a -mtune=cortex-a57`. Для llama.cpp: `-DGGML_CPU_ARM_ARCH=armv8-a -DGGML_NATIVE=off` (так собирал kreier) [P]. Для Go по умолчанию `GOARM64=v8.0` [НП: проверить `go env`].
5. **Железо.** MCP-сервер «контекста оборудования» делается с переключаемым бэкендом:
   - `jetson`: tegrastats, jtop, sysfs INA3221, nvpmodel, v4l2, CSI;
   - `mock`: записанные логи tegrastats, видеофайлы или RTSP через тот же `videoSource`.

   Агент и эксперименты на Mac и на плате тогда одинаковые, меняется только источник данных.
6. **Энергию в эмуляторе не мерить.** Модель E = P_idle·T + Σ ΔP·t параметризуется по [F10], Dutt'23, Shalavi'23 и проверяется на плате (как в F_practical_stack §4).

---

## 7. Что проверить на кафедре (чек-лист)

Команды (все безопасные, только чтение):

```bash
cat /proc/device-tree/model            # модель платы (Developer Kit / 2GB Developer Kit)
cat /etc/nv_tegra_release              # "# R32 (release), REVISION: 7.x" -> L4T 32.7.x
dpkg-query --show nvidia-l4t-core      # 32.7.6-... = JetPack 4.6.6 (как у kreier)
apt-cache show nvidia-jetpack | grep Version
free -h                                # ~3.9Gi -> 4 ГБ; ~1.9Gi -> 2 ГБ (1 ГБ не бывает)
uname -r                               # 4.9.xxx-tegra
ldd --version | head -1                # glibc 2.27
python3 --version                      # 3.6.9
docker --version                       # >= 20.10.10 нужен для образов с glibc >= 2.34
sudo nvpmodel -q                       # MAXN (10 Вт) или 5W
ls /sys/bus/i2c/drivers/ina3221x/      # есть ли датчик мощности (на 2 ГБ нет)
sudo tegrastats --interval 1000        # POM_5V_IN / POM_5V_GPU / POM_5V_CPU, RAM, GR3D_FREQ
ls /dev/video*; v4l2-ctl --list-devices   # камеры (нужен пакет v4l-utils)
/usr/local/cuda/bin/nvcc --version     # CUDA 10.2
df -h /                                # размер и заполненность microSD
jetson_release                         # если стоит jetson-stats: модуль p3448-0000/0002/0003, L4T, JetPack
```

Что посмотреть физически и что спросить:
- **Ревизия.** Наклейка на несущей плате. Два разъёма CSI — B01, один — A02 или 2 ГБ. USB-C питание — 2 ГБ.
- **Питание.** DC 5 В⎓4 А с перемычкой J48 или micro-USB 5 В⎓2 А. С камерой и Wi-Fi в MAXN micro-USB рискованно [P: JetsonHacks]. Есть ли вентилятор или радиатор (для длительной работы в MAXN).
- **microSD.** Объём и класс: NVIDIA рекомендует от 32 ГБ UHS-1 [V]. Для контейнеров и моделей нужно 64 ГБ+.
- **Периферия.** Какая камера (IMX219 / RPi v2?). Wi-Fi: карта M.2 Key E или USB-донгл. Есть ли на кафедре USB- или DC-ваттметр с логированием.
- **Доступ и организация.** Можно ли забрать плату домой или работать по SSH удалённо. Есть ли sudo и можно ли перепрошить на 4.6.6. Пропускает ли сеть университета API RouterAI.
- **К руководителю:**
  - (1) обязателен ли **оригинальный** Nano, или допустим Raspberry Pi 5 / Orin Nano (JetPack 6/7, Ubuntu 22.04/24.04, поставки до 2032);
  - (2) можно ли запускать агент в контейнере, или он должен работать «нативно» на L4T 18.04.

---

## 8. Каталог позиций (сводная таблица)

| # | Позиция | Категория | Ключевой факт | На Nano |
|---|---|---|---|---|
| 1 | Jetson Nano Developer Kit 4 ГБ (A02/B01) | железо | A57×4, Maxwell 128, 4 ГБ общей памяти, 5/10 Вт, INA3221 | цель |
| 2 | Jetson Nano 2GB Developer Kit | железо | 2 ГБ, без INA3221, USB-C 5 В⎓3 А | цель (хуже) |
| 3 | Модуль Jetson Nano P3448-0002 | железо | 4 ГБ + 16 ГБ eMMC, до 01.2027 | — |
| 4 | Jetson Orin Nano / Orin Nano Super | железо (альтернатива) | 4/8 ГБ LPDDR5, 67 TOPS (Super), JetPack 6/7, до 01.2032 | — |
| 5 | JetPack 4.6.6 / L4T R32.7.6 | платформа | финальный, EOL 22.11.2024 | — |
| 6 | Node.js 18–26 (официальные бинарники) | рантайм | glibc ≥ 2.28, ядро ≥ 4.18 | нет |
| 7 | OpenClaw | агент | Node ≥ 24.16 / 26.1, 389 МБ пакет, ~2,9 ГиБ пик RAM | только со сборкой Node из исходников |
| 8 | NanoClaw | агент | Node ≥ 22, Docker, агенты в контейнерах (Bun + Claude Agent SDK) | нет нативно; в контейнерах [НП] |
| 9 | PicoClaw | агент | Go, `CGO_ENABLED=0`, ядро 3.x+, ≥ 10 МБ RAM, клиент MCP | да (ожидается) [НП] |
| 10 | MCP Python SDK (`mcp`) | SDK | 2.3.0, Python ≥ 3.10, 235 млн загрузок в месяц | через uv / conda / контейнер |
| 11 | MCP Go SDK | SDK | v1.8.0, go 1.25, статический бинарник | да |
| 12 | uv + python-build-standalone | установка Python | musl-static uv; CPython glibc ≥ 2.17 | да [НП] |
| 13 | Miniforge | установка Python | aarch64, glibc ≥ 2.17 | да [НП] |
| 14 | EdgeSentinel VisionOps | агент на Nano | свой MCP stdio на Py 3.6, DeepSeek + офлайн-режим | да (заявлено) |
| 15 | Zalmotek/jetson-mcp | MCP-сервер | FastMCP, SSE | нужен Python ≥ 3.10 |
| 16 | bubbaloop | агент + MCP-сервер | Rust 13 МБ, 47 инструментов | вероятно, нет (glibc) [НП] |
| 17 | jetson-stats / jtop | мониторинг | 7.2.2 (Py ≥ 3.7) / 4.3.2 (Py 3.6), сокет для контейнеров | да |
| 18 | tegrastats + INA3221 | измерение | VDD_IN/GPU/CPU, только модуль, занижение до 50 % | да (кроме 2 ГБ) |
| 19 | jetson-inference | зрение | SSD-Mobilenet-v2 — 39 кадр/с, csi/v4l2/rtsp/file | да |
| 20 | jetson-containers / dustynv/ros r32.7.1 | ROS 2 / контейнеры | Humble из исходников на 18.04; основная ветка только JP 6.2/7 | да (образ 2023) |
| 21 | llama.cpp на Nano (kreier и др.) | локальная LLM | 1B: 4–7 ток/с; патчи под CUDA 10.2 | да, с трудом |
| 22 | Ollama | локальная LLM | JP4: GPU нет; CPU 0.6.4 работал | только CPU |
| 23 | Qengineering Ubuntu 20.04 image | ОС | glibc 2.31 при JetPack 4.6.1, неофициальный | да |
| 24 | Эмуляция: l4t-base r32.7.1 / Lima / QEMU cortex-a57 | эмуляция | userland, ВМ, набор инструкций | — |

---

## 9. Что это значит для архитектуры ВКР

1. **Оркестратор на плате:** Go (по образцу PicoClaw) или Python 3.12 в пользовательском окружении через uv. Без Node.js на хосте.
2. **MCP-серверы оборудования:** Python 3.12 (официальный SDK) или Go (официальный Go SDK). Транспорт — stdio или Streamable HTTP. Источники данных: tegrastats, jtop (`/run/jtop.sock`), sysfs INA3221, `nvpmodel -q`, `/etc/nv_tegra_release`, v4l2 и CSI, детекции jetson-inference.
3. **Тяжёлое — вне платы:** LLM через RouterAI, Gazebo на Mac. Локальная 1B-модель — только как аварийный режим, по образцу офлайн-правил EdgeSentinel.
4. **Энергия:** tegrastats на 1–10 Гц плюс внешний ваттметр, калибровка по Shalavi'23. На 2 ГБ — только внешний прибор.
5. **Окружение разработки на Mac** должно совпадать с L4T по userland: образ l4t-base r32.7.1 или эквивалент. Каждый бинарник проходит дымовой тест под `-cpu cortex-a57`, чтобы на плате не было сюрпризов.
6. **Довод для раздела «выбор платформы».** Оригинальный Nano — честный «наихудший случай» по программному стеку (EOL, glibc 2.27). Решение, которое работает на нём, переносится на Pi 5 и Orin почти без изменений, а в обратную сторону это неверно.

---

## 10. Что осталось непроверенным

- Запуск PicoClaw, uv/python-build-standalone, Miniforge и `pip install mcp` на реальном Nano (ожидается, что работает: требования совпадают).
- Node 24 в контейнере Ubuntu 22.04 на ядре 4.9: документация говорит лишь «may work».
- Минимальная glibc у бинарника bubbaloop и у текущего arm64-бинарника Ollama.
- Загрузится ли Ubuntu 18.04 arm64 в Lima с `vz`.
- Производительность Vulkan-бэкенда llama.cpp на Nano.
- Ток DC-разъёма (5 В⎓4 А) взят из JetsonHacks, не из документа NVIDIA (страница «Getting Started» его не указывает).
- Статья на Medium про glibc 2.28 + Node 25 на Nano B01 не прочитана (403).
- Фактическая модель на кафедре: «1 ГБ» по словам студента физически невозможен для Nano. Это либо 2 ГБ, либо 4 ГБ, либо вообще не Jetson.

---

## 11. Источники (доступ 2026-10-06)

**NVIDIA (первоисточники)**
1. Jetson Nano — технические характеристики модуля: https://developer.nvidia.com/embedded/jetson-nano ; страница продукта: https://www.nvidia.com/en-us/autonomous-machines/embedded-systems/jetson-nano/product-development/
2. Jetson Product Lifecycle: https://developer.nvidia.com/embedded/lifecycle
3. Jetson Modules: https://developer.nvidia.com/embedded/jetson-modules
4. JetPack SDK 4.6.6: https://developer.nvidia.com/jetpack-sdk-466 ; анонс EOL JetPack 4 (22.11.2024): https://forums.developer.nvidia.com/t/announcing-end-of-life-for-nvidia-jetpack-4-with-the-release-of-jetpack-4-6-6/314300
5. Jetson Linux R32.7.6 Developer Guide — Power Management for Jetson Nano and TX1: https://docs.nvidia.com/jetson/archives/l4t-archived/l4t-3276/Tegra%20Linux%20Driver%20Package%20Development%20Guide/power_management_nano.html
6. Там же — tegrastats Utility: …/AppendixTegraStats.html ; Jetson Module Adaptation and Bring-Up (P-номера): …/adaptation_and_bringup_nano.html
7. NVIDIA Developer Blog, «Jetson Nano Brings AI Computing to Everyone», 18.03.2019: https://developer.nvidia.com/blog/jetson-nano-ai-computing/
8. Getting Started with Jetson Nano Developer Kit: https://developer.nvidia.com/embedded/learn/get-started-jetson-nano-devkit
9. NGC `nvcr.io/nvidia/l4t-base` (registry API, тег r32.7.1).
10. Orin Nano Super Developer Kit: https://www.nvidia.com/en-us/autonomous-machines/embedded-systems/jetson-orin/nano-super-developer-kit/ ; JetPack 7: https://developer.nvidia.com/embedded/jetpack
11. Jetson AI Lab — OpenClaw on Jetson: https://www.jetson-ai-lab.com/tutorials/openclaw/

**Репозитории и реестры**
12. dusty-nv/jetson-inference (README, docs/aux-streaming.md, docs/imagenet-camera-2.md), dusty-nv/jetson-containers (README), Docker Hub `dustynv/ros`, `dustynv/nano_llm`, `dustynv/llama_cpp`, `dustynv/ollama`.
13. rbonghi/jetson_stats (README); PyPI `jetson-stats`; pypistats.
14. sipeed/picoclaw (README, `.goreleaser.yaml`, `go.mod`, docs/guides/hardware-compatibility.md, releases).
15. openclaw/openclaw; npm `openclaw` (engines); https://docs.openclaw.ai/install/node
16. nanocoai/nanoclaw (ранее qwibitai/nanoclaw): package.json, README.
17. nodejs/node `BUILDING.md` (ветки v18.x…v26.x, main); https://unofficial-builds.nodejs.org/download/release/index.json
18. PyPI: `mcp`, `fastmcp`, `openai`, `requests`, `httpx`, `pydantic-core`, `cryptography`, `rpds-py`, `uvloop`, `httptools`, `uv`.
19. modelcontextprotocol/python-sdk, modelcontextprotocol/go-sdk, mark3labs/mcp-go.
20. astral-sh/uv, документация платформ: https://docs.astral.sh/uv/reference/policies/platforms/ ; python-build-standalone: https://gregoryszorc.com/docs/python-build-standalone/main/running.html
21. conda-forge/miniforge (README); conda-forge news 2024-11-22; issue conda-forge.github.io#2383.
22. deadsnakes PPA: https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa ; Launchpad API (bionic: glibc, python3.x, openssl, docker.io, gcc-8, cmake).
23. kreier/llama.cpp-jetson (README, бенчмарки); Z841973620/llama.cpp-jetpack4.6; GmausDev/nanobot-jetson.
24. ollama/ollama issue #4140.
25. Qengineering/Jetson-Nano-Ubuntu-20-image (README).
26. zja0011/edgesentinel-visionops (README, `packages/mcp/server.py`, `requirements-api-py36.txt`); Zalmotek/jetson-mcp; lhzn-io/jetson-telemetry-mcp; kornia/bubbaloop (README, release.yml).
27. lima-vm/lima (templates, default.yaml); QEMU `virt`: https://www.qemu.org/docs/master/system/arm/virt.html ; cloud-images.ubuntu.com (bionic arm64).
28. Bun installation: https://bun.com/docs/installation ; Go Minimum Requirements: https://go.dev/wiki/MinimumRequirements
29. Docker Engine 20.10 release notes; moby PR #42836 (clone3).
30. endoflife.date API: ros-2, nodejs, ubuntu, python.
31. TI INA3221: https://www.ti.com/product/INA3221
32. Raspberry Pi 5: https://www.raspberrypi.com/products/raspberry-pi-5/ ; Apple M4 Pro, 273 ГБ/с: https://www.apple.com/newsroom/2024/10/apple-introduces-m4-pro-and-m4-max/

**Статьи**
33. Dutt A., Rachuri S. P., Lobo A., Shaik N., Gandhi A., Liu Z. Evaluating the energy impact of device parameters for DNN inference on edge // IGSC 2023. PDF: https://www3.cs.stonybrook.edu/~anshul/igsc23-dnnedge.pdf
34. Shalavi N. et al. Accurate Calibration of Power Measurements from Internal Power Sensors on NVIDIA Jetson Devices // IEEE EDGE 2023. DOI 10.1109/EDGE60047.2023.00034 (разбор в G6_gap B3).
35. Holly S. et al. Profiling Energy Consumption of Deep Neural Networks on NVIDIA Jetson Nano // IGSC 2020 (только аннотация).

**Вторичные (форумы, блоги)**
36. CNX Software: Jetson Nano 2GB (06.10.2020), B01 (17.01.2020); Arducam: B01 vs A02; JetsonHacks: «Jetson Nano — Use More Power!» (10.04.2019).
37. Форум NVIDIA: «Hardware used for Tegrastats?», «Jetson Nano eMMC POM_5V_IN/POM_5V_GPU mismatch».
38. brtkwr.com: «Installing OpenClaw on a Jetson Nano» (08.02.2026), «Upgrading OpenClaw to Latest on Jetson Nano with Node 22» (02.03.2026).
