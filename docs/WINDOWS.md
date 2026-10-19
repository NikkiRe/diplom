# Запуск на Windows (WSL2)

Проект разрабатывался на Mac (Apple M4, arm64) с двумя ВМ Lima:
- `sbc` — «слабая плата» под лимитами;
- `sim` — ROS 2 Jazzy + Gazebo Harmonic.

На Windows то же делается в **WSL2** — настоящий Linux, для стенда этого достаточно. Ниже — как поднять окружение и
что в проекте придётся адаптировать. Это инструкция и план. На Windows проект **ещё не запускался**, первому, кто
его поднимет, нужно дописать сюда фактические результаты.

## 1. Окружение

1. **WSL2 + Ubuntu 24.04:** `wsl --install -d Ubuntu-24.04` (PowerShell от администратора), затем перезагрузка.
2. **Ресурсы WSL** — файл `%UserProfile%\.wslconfig`:

   ```ini
   [wsl2]
   memory=16GB
   processors=8
   kernelCommandLine = cgroup_no_v1=all
   ```

   `cgroup_no_v1=all` нужен для чистой иерархии cgroup v2: на ней построены лимиты «платы». После правки —
   `wsl --shutdown`.
3. **systemd внутри WSL** — `/etc/wsl.conf`:

   ```ini
   [boot]
   systemd=true
   ```

   Юниты робота, `systemd-run` и лимиты cgroup работают только с ним. Проверка: `systemctl is-system-running`.
4. **Клонировать в файловую систему Linux**, а не в `/mnt/c`: там нет прав на исполнение, симлинков, и всё медленно.

   ```bash
   git config --global core.autocrlf input
   git clone https://github.com/NikkiRe/diplom.git ~/diplom && cd ~/diplom
   ```

   В репозитории `.gitattributes` задаёт LF для всего текста. CRLF ломает bash-скрипты.
5. **Go 1.27:** архив с go.dev в `/usr/local/go`, `PATH=$PATH:/usr/local/go/bin`.
6. **Python 3.12 через uv:**

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   uv venv .venv --python 3.12
   uv pip install --python .venv/bin/python -r requirements-dev.txt
   ```

7. **ROS 2 Jazzy + Gazebo Harmonic.** Скрипт `scripts/sim/install_ros.sh` — обычный apt для Ubuntu 24.04, внутри
   WSL он подходит; запускать в самом WSL. Для цифрового двойника робота нужны ещё:

   ```bash
   sudo apt install ros-jazzy-gz-ros2-control ros-jazzy-ros2-controllers ros-jazzy-twist-mux \
     ros-jazzy-imu-tools ros-jazzy-xacro ros-jazzy-robot-state-publisher jq shellcheck
   ```

8. **Gazebo в WSL.** Работает через WSLg (GPU через D3D12) или программную отрисовку (`LIBGL_ALWAYS_SOFTWARE=1`).
   Двойник запускается без окна. RTF на Windows-ПК надо измерить заново (на Mac было ≈ 0,96–1,0).

## 2. Что адаптировать в проекте

### 2.1. Вызовы ВМ (`limactl`)

Около 35 скриптов зовут `limactl shell sbc|sim -- …` или `limactl copy`:
- `scripts/sim/*`, `scripts/board/board.sh`, `bench/board/vm.py`, `bench/sysfsim/vm_check.sh`;
- `neurobot/sim/*/`, `hwmcp/tools/vm_*.sh`, Makefile'ы модулей.

Не переписывать каждый по-своему — ввести **одну точку запуска**, например `scripts/vmexec <роль> -- команда`:
- бэкенд `lima` — как сейчас (Mac);
- бэкенд `native` — команда выполняется прямо в текущем Linux (WSL), а `copy` — обычный `cp`.
- Бэкенд выбирается переменной окружения (`NB_VM_BACKEND=native`) или автоматически: нет `limactl` — значит, native.

Роли `sbc` и `sim` на Windows — один и тот же дистрибутив WSL. «Плата» изолируется своей cgroup/slice,
как в `scripts/board/board.sh` и `neurobot/sim/budget`. Отдельная ВМ не нужна. Если нужна полная изоляция —
второй дистрибутив через `wsl --import`.

Переводить скрипты по одному, каждый — с проверкой: тест или прогон.

### 2.2. Архитектура процессора

На Mac всё arm64, на обычном Windows-ПК — **x86_64**.
- Бинарники для стенда собирать под `linux/amd64`. Для Jetson Nano остаётся `linux/arm64`.
- Суффиксы `-linux-arm64` в скриптах и Makefile заменить на `$(go env GOARCH)` или параметр.
- PicoClaw для стенда: linux-amd64 из GitHub releases (v0.2.8 — как на роботе, v0.3.1 — для сравнения).
  `bench/baselines/install_picoclaw.sh` сейчас знает только arm64 — добавить amd64 с проверкой sha256.
- torch CPU для perception на стенде — обычный x86_64-пакет.

### 2.3. Возможности ядра WSL — проверить, а не предполагать

| Что | Где нужно | Как проверить | Если нет |
|---|---|---|---|
| cgroup v2: memory, cpu, io, pids | лимиты «платы», бюджет Jetson 4 ГБ | `cat /sys/fs/cgroup/cgroup.controllers` | без `io` — пропускать профили диска; без `memory`/`cpu` лимитов нет |
| `tc netem`, `ifb` | профили сети (3g, edge, обрывы) | `sudo modprobe sch_netem ifb && sudo tc qdisc add dev lo root netem delay 1ms && sudo tc qdisc del dev lo root` | своё ядро WSL или пропуск сетевой лестницы |
| `gpio-sim` (configfs) | датчики через настоящий GPIO uAPI | `sudo modprobe gpio-sim` | своё ядро WSL (CONFIG_GPIO_SIM=m, CONFIG_GPIO_CDEV_V1=y) или бэкенд `files` у `bench/sysfsim` |
| systemd | юниты робота, `systemd-run` | `systemctl is-system-running` | включить в `/etc/wsl.conf` |

Своё ядро WSL собирается по официальной инструкции Microsoft (репозиторий WSL2-Linux-Kernel) и подключается
строкой `kernel=` в `.wslconfig`. Чего нет — честно отключать соответствующие проверки, с пометкой в отчёте.
Подменять результат нельзя.

### 2.4. Маковское, что на Windows не нужно или заменяется

- `pmset`, `caffeinate`, `powermetrics` — только Mac (питание, сон); на Windows не нужны.
- Сон ноутбука сдвигает часы ВМ: на Mac это была ловушка. В WSL проверить отдельно.
- `tools/html2docx.py` запускался встроенным Python Codex, в Windows его нет. Ставить `python-docx` в `.venv`. PDF — headless Chrome или Edge.
- Абсолютные пути `/Users/chosenundead/...` в `HANDOFF.md` и отчётах — исторические, не пути проекта.

## 3. Материалы лаборатории NeuroChip

В публичный репозиторий **не входят** — `.gitignore`:
- код, веса, журналы и презентация лаборатории (`third_party/neurochip/`);
- наши копии и патчи их кода: `neurobot/perception/predict_live.py`, `neurobot/perception/predict_live_v2.patch`, `neurobot/deploy/mcp/*.patch`.

Их передают отдельно — архив `neurochip_private_*.tar.gz` у студента. Распаковать в корень репозитория:
структура совпадёт. Без них часть тестов `neurobot/` пропустится или упадёт, а прогон «демо как есть» (A0)
невозможен.

Правила:
- `*.pt`/`*.pth` — непроверенный pickle: не загружать. Исключение — `snn_emnist_small.pth`, с `weights_only=True`;
- секреты в архиве лаборатории уже вычищены. Если придёт новый архив — сначала вычистить (`neurobot/lab/redact.py`), потом работать;
- ничего из этого не коммитить.

## 4. Порядок первого запуска на Windows

1. Окружение — §1. Проверки ядра — §2.3: записать результаты сюда.
2. `go test ./...` во всех Go-модулях (`agent`, `hwmcp`, `cammcp`, `robotmcp`, `sysmon`, `bench/board/cpubench`).
3. `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q bench/tests monitor/tests harness/tests`, затем
   `bench/baselines/tests` и `neurobot/tests`. Тесты, которым нужна ВМ, на этом шаге пропускаются.
4. Смоук датчиков: `.venv/bin/python -m bench.sensors --out /tmp/sensor-smoke`.
5. Слой `vmexec` (§2.1) и архитектура (§2.2), потом двойник: `neurobot/sim/twin/twin.sh up` → `check.sh`.
6. Дальше — по `docs/ROADMAP.md` (для робота шаг W3 на двойнике сделан; следующее — §2–3: решения студента, поставка, HIL).
