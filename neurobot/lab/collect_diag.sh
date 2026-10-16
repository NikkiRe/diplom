#!/usr/bin/env bash
# Сбор диагностики с робота NeuroChip: версии, процессы и порты стека, способ запуска, юниты и их журналы,
# журналы PicoClaw, MCP-серверов, воркеров и демо, ядро, tegrastats, граф ROS 2 и параметры цепочки движения,
# сводка /scan по секторам, память и диск. Результат — каталог neurobot_diag_<хост>_<время>/ и архив .tar.gz.
#
# Только чтение: ничего не останавливает, не перезапускает и не меняет. Исключения:
#   - служебный ros2 daemon: если его не было, он поднимается на время сбора и останавливается в конце;
#   - из программ лаборатории запускаются только «picoclaw version» (сам picoclaw; лаунчер — никогда) и найденные
#     интерпретаторы Python (venv, pyenv, процессы) — версия и список пакетов: -V, pip freeze или importlib.metadata
#     (Python при этом читает .pth из site-packages); у pyenv — versions и global;
#   - адреса статуса воркеров и шлюза PicoClaw (8765 /health, 8766 /state, 18790 /health и /ready) — запрос GET;
#   - камера открывается только с флагом --camera и только если её не держит другой процесс.
# Вывод команд и копии файлов проходят через redact.py до записи на диск; служебные строки и весь каталог
# перед упаковкой проверяются повторно.
#
#   bash collect_diag.sh [флаги]
#     --sudo              команды, которым нужен root, — через sudo (пароль один раз в начале); по умолчанию выкл.
#     --out DIR           где создать каталог и архив (по умолчанию — текущий каталог)
#     --tegrastats-sec N  сколько секунд писать tegrastats (60; 0 — не писать)
#     --days N            глубина журналов юнитов, ROS 2 и логов рядом с кодом, сут (7)
#     --max-log-mb N      сколько последних МБ брать из каждого файла журнала (5)
#     --ros-setup FILE    setup-файл ROS 2; повторяется: база, затем рабочее пространство
#                         (по умолчанию /opt/ros/$ROS_DISTRO/setup.bash или первый /opt/ros/*/setup.bash)
#     --search DIR        где искать код лаборатории, venv, скрипты запуска, PicoClaw; повторяется ($HOME)
#     --camera            снять параметры камеры: rs-enumerate-devices -c (по умолчанию выкл.)
#
# Рядом со скриптом лежат redact.py и scan_sectors.py. В stdout печатается только путь к архиву, ход сбора —
# в stderr. Код выхода: 0 — сбор завершён (отдельные пункты могут быть «нет»); 2 — неверные аргументы, нет
# python3, redact.py или timeout, sudo не подтверждён.

# Этап Э0 ВКР: docs/neurobot/PLAN.md §7, neurobot/lab/README.md.
# shellcheck disable=SC2016  # $… в одинарных кавычках — для дочерней оболочки, dpkg-query и python3 -c
set -uo pipefail  # без -e: отказ отдельной команды — обычный исход сбора, он попадает в REPORT.txt

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# sha256 версий, присланных лабораторией (third_party/neurochip, срез 14.10.2026): та ли версия на роботе.
declare -A REFERENCE=(
  [predict_live.py]=d186195dc0d3abb7d6b45730baa84c04ecaa7ddcbc5ea726b47ed541a7153a54
  [ros_mcp_server.py]=64766fa8c19fac9fc21db9a592d2c02e4a0d069319a5b1bad6252309ede22039
  [robot_vision_mcp_proxy.py]=16fa227a65df9eb8c95d6c10a18227d6e59a39a4886e2bea900835d66223963b
  [yolo_mcp_server.py]=99ccf03cab0f6b5049ff53a77220886d4f1f58812869c4e430d945de56769aec
  [yolo_hand_worker.py]=1f28c87fc818c20a1341323a4b662c8c90ddf6e13b675ddb19b175d781bf2156
  [run_yolo_mcp_stack.sh]=57f02fd4b0f47a6ac699c7acdaeb7206688377d01e9c61cfc44056e8480e7dfe
)
PROC_RE='predict_live|mcp|picoclaw|yolo|vision|worker\.py|rosbridge|rosapi|urg_node|realsense|ros2|twist_mux'
PROC_RE+='|stm32|serial|uart|cmd_vel'
LAB_RE='predict_live|mcp|yolo|vision|worker\.py'  # процессы лаборатории: их рабочий каталог — к поиску кода
CAMERA_RE='predict_live|realsense|vision|yolo|worker\.py'  # кто может держать камеру: при них --camera её не трогает
# Порты стека. У воркеров и шлюза — адреса статуса, которые опрашивают сами инструменты агента (только GET).
PORTS=(9090 8765 8766 18790 18800 18110)
declare -A PORT_ROLE=([9090]=rosbridge [8765]=YOLO-воркер [8766]="воркер robot_vision" [18790]="шлюз PicoClaw"
  [18800]="лаунчер PicoClaw" [18110]="MCP по HTTP, пакет 1")
declare -A PORT_STATUS=([8765]=/health [8766]=/state [18790]="/health /ready")
LAB_LOG_DIRS=(/tmp/yolo_hand_mcp_logs)  # журналы воркеров, которые run_yolo_mcp_stack.sh запускает через nohup
UNIT_RE='ros|claw|mcp|predict|neuro|demo|realsense|urg|stm32|robot|gesture|camera|yolo|vision|worker'
KERN_RE='usb|uvc|realsense|oom|out of memory|killed process|thermal|throttl|soctherm|over-current|oc alarm'
KERN_RE+='|under-?volt|hung_task|segfault|nvgpu|tegra-xusb'
RC_RE='ros|source|export|python|pyenv|picoclaw|mcp|alias|predict'
ROS_VARS='ROS_DOMAIN_ID|ROS_LOCALHOST_ONLY|ROS_AUTOMATIC_DISCOVERY_RANGE|ROS_STATIC_PEERS|RMW_IMPLEMENTATION'
ROS_VARS+='|CYCLONEDDS_URI|FASTRTPS_DEFAULT_PROFILES_FILE'
PERM_RE='permission denied|operation not permitted|insufficient permissions|must be root|access denied'
PERM_RE+='|a password is required|not seeing messages from other users'
NOTFOUND_RE='no such file|not found|no packages found|no crontab|no module named|no server running'
NOTFOUND_RE+='|could not find|does not exist|invalid choice'
# Окружение ROS 2 для одной команды: $1 — setup-файлы через «:», дальше — команда. Позиционные параметры
# очищаются до source, чтобы setup.bash их не увидел.
ROS_SHELL='set +u; files=$1; shift; cmd=("$@"); set --
IFS=: read -r -a fs <<< "$files"; for f in "${fs[@]}"; do . "$f"; done > /dev/null 2>&1; exec "${cmd[@]}"'
# GET на локальный адрес без прокси из окружения, таймаут 3 с: код ответа и первые 64 КБ тела.
HTTP_GET='import sys, urllib.error, urllib.request
try:
    r = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(sys.argv[1], timeout=3)
except urllib.error.HTTPError as e:
    r = e
print("HTTP", r.getcode())
print(r.read(65536).decode("utf-8", "replace"))'
# Пакеты интерпретатора без pip (в venv, созданных uv, pip нет).
PY_DISTS='from importlib import metadata
names = {d.metadata["Name"] + "==" + d.version for d in metadata.distributions() if d.metadata["Name"]}
print("\n".join(sorted(names)))'

die() { echo "collect_diag: $*" >&2; exit 2; }
usage() { awk 'NR > 1 { if (!/^#/) exit; sub(/^# ?/, ""); print }' "$0"; }

use_sudo=0 camera=0 out_parent=$PWD tegra_sec=60 days=7 max_log_mb=5
ros_setups=() search_dirs=()
args_line="$*"
while (($#)); do
  case $1 in
    --sudo) use_sudo=1 ;;
    --camera) camera=1 ;;
    --out) out_parent=${2:?--out DIR}; shift ;;
    --tegrastats-sec) tegra_sec=${2:?--tegrastats-sec N}; shift ;;
    --days) days=${2:?--days N}; shift ;;
    --max-log-mb) max_log_mb=${2:?--max-log-mb N}; shift ;;
    --ros-setup) ros_setups+=("${2:?--ros-setup FILE}"); shift ;;
    --search) search_dirs+=("${2:?--search DIR}"); shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "неизвестный аргумент: $1 (см. --help)" ;;
  esac
  shift
done
for n in "$tegra_sec" "$days" "$max_log_mb"; do [[ $n =~ ^[0-9]+$ ]] || die "ожидалось целое число: $n"; done
((days > 0 && max_log_mb > 0)) || die "--days и --max-log-mb должны быть больше 0"
for f in "${ros_setups[@]}"; do [[ -f $f ]] || die "нет файла --ros-setup: $f"; done
((${#search_dirs[@]})) || search_dirs=("$HOME")
for d in "${search_dirs[@]}"; do [[ -d $d ]] || die "нет каталога --search: $d"; done
command -v python3 > /dev/null || die "нужен python3: без него секреты не вырезать, сбор не выполняется"
[[ -f $here/redact.py ]] || die "рядом со скриптом нет redact.py"
command -v timeout > /dev/null || die "нужен timeout (coreutils)"

P=()  # префикс для команд, которым нужны права root
hint_sudo="повторите с --sudo"
if ((use_sudo)); then
  sudo -v || die "sudo не подтверждён; запустите без --sudo"
  P=(sudo -n)
  hint_sudo="нет прав и с sudo"
fi

export LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 NO_COLOR=1  # NO_COLOR: без цветовых кодов в выводе (picoclaw)
max_bytes=$((max_log_mb * 1024 * 1024))
host=$(hostname -s 2>/dev/null || echo host)
name=neurobot_diag_${host}_$(date +%Y%m%d-%H%M%S)
out_parent=$(cd "$out_parent" && pwd) || die "нет каталога --out: $out_parent"
out=$out_parent/$name
umask 077
mkdir -p "$out" || die "не создать $out"
work=$(mktemp -d) || die "не создать временный каталог"
cd "$work" || die "нет $work"
report=$out/REPORT.txt
daemon_started=0 tegra_pid=
ros_env=() pythons=() pico_homes=() pico_logs=() pico_configs=() pico_bins=() mcp_configs=() robot_pids=()
declare -A port_up=() port_pids=()

# redact [--json|--check DIR] — фильтр секретов; при сбое в вывод попадает метка, её ищет finish.
redact() { python3 -I "$here/redact.py" "$@" || echo "collect_diag: сбой redact.py (код $?), дальше вывод не записан"; }

# note <статус> <что> [подробности] — строка отчёта (и в stderr). Статусы: ок, нет, права, таймаут, ошибка.
note() {
  local line="[$1] $2${3:+ — $3}"
  echo "$line" >> "$report"
  echo "$line" >&2
}

# verdict <что> <файл> <код> <вывод> — статус команды по коду выхода и тексту ошибки.
verdict() {
  local what=$1 rel=$2 rc=$3 seg=$4
  if ((rc == 124 || rc == 137)); then
    note таймаут "$what" "$rel"
  elif grep -qiE "$PERM_RE" "$seg" && { ((rc != 0)) || grep -q 'not seeing messages' "$seg"; }; then
    note права "$what" "$hint_sudo"
  elif ((rc == 126 || rc == 127)) || { ((rc != 0)) && { grep -qiE "$NOTFOUND_RE" "$seg" || [[ ! -s $seg ]]; }; }; then
    note нет "$what" "см. $rel"
  elif ((rc != 0)); then
    note ошибка "$what" "код $rc, см. $rel"
  else
    note ок "$what" "$rel"
  fi
}

# cap <что> <файл> <таймаут, с> <команда…> — выполнить команду с таймаутом; stdout и stderr через redact
# дописываются в $out/<файл> под строкой «$ команда» (или CAP_SHOW); исход — в отчёт. Возвращает код команды.
cap() {
  local what=$1 rel=$2 t=$3 rc
  shift 3
  mkdir -p "$(dirname "$out/$rel")"
  timeout -k 2 "$t" "$@" < /dev/null 2>&1 | redact > "$work/seg"
  rc=${PIPESTATUS[0]}
  { printf '$ %s\n' "${CAP_SHOW:-$*}"; cat "$work/seg"; echo; } >> "$out/$rel"
  verdict "$what" "$rel" "$rc" "$work/seg"
  return "$rc"
}

# take <что> <источник> <файл> — последние --max-log-mb МБ файла через redact (*.json — с разбором) в $out/<файл>.
take() {
  local what=$1 src=$2 rel=$3 mode=() rc
  mkdir -p "$(dirname "$out/$rel")"
  [[ $src == *.json ]] && mode=(--json)
  timeout -k 2 30 "${P[@]}" tail -c "$max_bytes" "$src" < /dev/null 2> "$work/err" | redact "${mode[@]}" > "$out/$rel"
  rc=${PIPESTATUS[0]}
  verdict "$what" "$rel" "$rc" "$work/err"
}

exists() { timeout -k 1 5 "${P[@]}" test -e "$1" 2> /dev/null; }

# Чтение /proc с таймаутом: у процесса в состоянии D (например, зависание nvgpu) оно блокируется.
proc_read() { timeout -k 1 5 "${P[@]}" cat "/proc/$1/$2" 2> /dev/null; }
proc_link() { timeout -k 1 5 "${P[@]}" readlink "/proc/$1/$2" 2> /dev/null; }

# penv <pid> <имена через |> — только перечисленные переменные окружения процесса, NAME=value по строке.
penv() { proc_read "$1" environ | tr '\0' '\n' | grep -E "^($2)="; }

# args_of <pid> — аргументы процесса из снятого (и очищенного от секретов) списка ps.
args_of() { awk -v p="$1" '$1 == p {$1 = $2 = ""; sub(/^ +/, ""); print; exit}' "$work/ps"; }

is_elf() { [[ $(timeout 5 head -c 4 "$1" 2> /dev/null | od -An -tx1 | tr -d ' \n') == 7f454c46 ]]; }

# find_in <глубина> <условия find…> — поиск в каталогах --search мимо служебных и тяжёлых подкаталогов, каталога
# результата и каталога самого скрипта.
find_in() {
  local depth=$1
  shift
  timeout 60 find "${search_dirs[@]}" -maxdepth "$depth" \( -path "$out" -o -path "$here" -o -name .git \
    -o -name node_modules -o -name site-packages -o -name dist-packages -o -name __pycache__ -o -name .cache \
    -o -name '.Trash*' -o -name snap \) -prune -o "$@" -print 2> /dev/null
}

# add_search <каталог> — добавить рабочий каталог процесса к --search (системные и уже заданные — нет).
add_search() {
  local d
  case $1 in
    / | /usr | /usr/* | /opt/ros | /opt/ros/* | /proc* | /sys* | /etc* | /var* | /run* | /tmp | /snap*) return ;;
  esac
  for d in "${search_dirs[@]}"; do [[ $1/ == "$d"/* ]] && return; done
  search_dirs+=("$1")
  note ок "поиск кода и скриптов расширен" "$1 (рабочий каталог процесса)"
}

# flat <путь> — имя файла в архиве из пути: /home/a/b.log → home_a_b.log
flat() { local p=${1#/}; echo "${p//\//_}"; }

ros_files() { local IFS=:; echo "${ros_setups[*]}"; }

# ros_cmd <таймаут, с> <команда…> — команда в окружении ROS 2 робота (журналы ros2 CLI — во временный каталог).
ros_cmd() {
  local t=$1
  shift
  timeout -k 2 "$t" env "${ros_env[@]}" ROS_LOG_DIR="$work/roslog" PYTHONUNBUFFERED=1 bash -c "$ROS_SHELL" ros2env \
    "$(ros_files)" "$@" < /dev/null 2>&1
}

# rosrun <что> <файл> <таймаут, с> <аргументы ros2…> — cap для ros2 в окружении ROS 2 робота.
rosrun() {
  local what=$1 rel=$2 t=$3
  shift 3
  local CAP_SHOW="ros2 $*"
  cap "$what" "$rel" "$t" env "${ros_env[@]}" ROS_LOG_DIR="$work/roslog" PYTHONUNBUFFERED=1 \
    bash -c "$ROS_SHELL" ros2env "$(ros_files)" ros2 "$@"
}

# ros_node <узел> — есть ли узел в снятом списке; если нет — строка «нет» в отчёте.
ros_node() {
  grep -qx -- "$1" "$out/ros/nodes.txt" && return
  note нет "ROS 2: узел $1" "нет в графе"
  return 1
}

cleanup() {
  if ((daemon_started)); then
    rosrun "ROS 2: служебный ros2 daemon остановлен" ros/daemon.txt 20 daemon stop
    daemon_started=0
  fi
  [[ -n $tegra_pid ]] && kill "$tegra_pid" 2> /dev/null
  rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

start_tegrastats() {
  if ((tegra_sec == 0)); then
    note нет "tegrastats" "отключено (--tegrastats-sec 0)"
  elif ! command -v tegrastats > /dev/null; then
    note нет "tegrastats" "не найден (не Jetson?)"
  else
    mkdir -p "$out/system"
    timeout -s INT -k 2 "$tegra_sec" "${P[@]}" tegrastats --interval 1000 < /dev/null \
      > "$out/system/tegrastats.txt" 2>&1 &
    tegra_pid=$!
  fi
}

finish_tegrastats() {
  [[ -n $tegra_pid ]] || return
  wait "$tegra_pid"
  local rc=$? lines
  tegra_pid=
  lines=$(wc -l < "$out/system/tegrastats.txt")
  if ((rc == 124 && lines > 0)); then
    note ок "tegrastats, $tegra_sec с" "$lines строк, system/tegrastats.txt"
  else
    verdict "tegrastats" system/tegrastats.txt "$rc" "$out/system/tegrastats.txt"
  fi
}

section_system() {
  local f
  if [[ -r /proc/device-tree/model ]]; then
    tr -d '\0' < /proc/device-tree/model > "$out/system/jetson_model.txt"
    note ок "модель платы" "$(cat "$out/system/jetson_model.txt")"
  else
    note нет "модель Jetson (/proc/device-tree/model)" "не Jetson?"
  fi
  for f in /etc/nv_tegra_release /usr/local/cuda/version.txt /usr/local/cuda/version.json /etc/os-release; do
    if [[ -r $f ]]; then take "$f" "$f" "system/$(flat "$f")"; else note нет "$f"; fi
  done
  cap "ядро" system/versions.txt 5 uname -a
  cap "systemd" system/versions.txt 5 systemctl --version
  cap "python3" system/versions.txt 5 python3 -V
  if command -v dpkg-query > /dev/null; then
    for f in 'ros-*' 'nvidia-l4t-*' 'nvidia-jetpack' 'librealsense2*' 'python3-rosbridge*'; do
      cap "пакеты $f" system/packages.txt 30 dpkg-query -W -f '${db:Status-Abbrev} ${Package} ${Version}\n' "$f"
    done
  else
    note нет "dpkg-query" "не Debian/Ubuntu?"
  fi
  cap "загрузки системы" system/boots.txt 15 "${P[@]}" journalctl --list-boots --no-pager
  cap "перезагрузки и выключения (last)" system/boots.txt 10 last -n 50 -x -F reboot shutdown
  cap "память" system/memory.txt 5 free -m
  cap "подкачка" system/memory.txt 5 swapon --show
  cap "время работы и нагрузка" system/memory.txt 5 uptime
  cap "диски" system/disk.txt 10 df -hT
  for f in /tmp/ros_mcp_frames /tmp/ros_mcp_lidar "$HOME/.ros/log"; do
    [[ -d $f ]] && cap "размер $f" system/disk.txt 30 du -sh "$f"
  done
}

section_power() {
  local z f
  if command -v nvpmodel > /dev/null; then
    cap "режим питания (nvpmodel -q)" system/power.txt 10 "${P[@]}" nvpmodel -q
  else
    note нет "nvpmodel" "не Jetson?"
  fi
  if command -v jetson_clocks > /dev/null; then
    if ((use_sudo)); then
      cap "частоты (jetson_clocks --show)" system/power.txt 10 "${P[@]}" jetson_clocks --show
    else
      note права "jetson_clocks --show" "$hint_sudo"
    fi
  fi
  for z in /sys/class/thermal/thermal_zone*; do
    [[ -r $z/temp ]] && echo "$(timeout 2 cat "$z/type" 2> /dev/null) $(timeout 2 cat "$z/temp" 2> /dev/null)"
  done > "$out/system/thermal.txt"
  for f in /sys/devices/pwm-fan/target_pwm /sys/devices/pwm-fan/cur_pwm; do
    [[ -r $f ]] && echo "$f $(timeout 2 cat "$f")"
  done >> "$out/system/thermal.txt"
  if [[ -s $out/system/thermal.txt ]]; then
    note ок "температуры и вентилятор" system/thermal.txt
  else
    note нет "температуры и вентилятор" "нет /sys/class/thermal и pwm-fan"
  fi
}

section_devices() {
  local g found=()
  if command -v lsusb > /dev/null; then
    cap "USB" system/usb.txt 10 lsusb
    cap "USB, дерево и скорости" system/usb.txt 10 lsusb -t
  else
    note нет "lsusb" "пакет usbutils"
  fi
  for g in '/dev/video*' '/dev/ttyUSB*' '/dev/ttyACM*' '/dev/ttyTHS*'; do
    mapfile -t -O "${#found[@]}" found < <(compgen -G "$g")
  done
  if ((${#found[@]})); then
    cap "камеры и последовательные порты" system/devices.txt 5 ls -l "${found[@]}"
  else
    note нет "камеры и последовательные порты" "нет /dev/video*, /dev/ttyUSB*, /dev/ttyACM*, /dev/ttyTHS*"
  fi
}

section_kernel() {
  local f logs=()
  cap "dmesg" kernel/dmesg.txt 15 "${P[@]}" dmesg -T ||
    cap "журнал ядра (journalctl -k)" kernel/dmesg.txt 30 "${P[@]}" journalctl -k -b --no-pager -o short-iso
  grep -aiE "$KERN_RE" "$out/kernel/dmesg.txt" > "$out/kernel/dmesg_filtered.txt"
  # syslog и kern.log хранят прошлые загрузки, даже если журнал systemd не постоянный
  for f in /var/log/syslog /var/log/syslog.1 /var/log/kern.log /var/log/kern.log.1; do
    exists "$f" && logs+=("$f")
  done
  if ((${#logs[@]} == 0)); then
    note нет "syslog, kern.log" "нет /var/log/syslog и /var/log/kern.log"
  fi
  for f in "${logs[@]}"; do
    CAP_SHOW="grep -aiE <ядро и процессы робота> $f | tail -n 5000" cap "отбор из $f" kernel/syslog_filtered.txt 60 \
      "${P[@]}" bash -c 'set -o pipefail; grep -aiE "$1" "$2" | tail -n 5000' grep "$KERN_RE|$PROC_RE" "$f"
  done
}

# Процессы робота — по именам (PROC_RE) и по портам стека (PORTS): аргументы, куда идёт их вывод (файл —
# копируется), рабочий каталог (добавляется к поиску кода), интерпретатор Python, окружение ROS 2 и PicoClaw
# (только перечисленные переменные, остальное не читается), скрипт процесса на порту стека.
section_processes() {
  local pid fd tgt line venv name have var d f port script args=() pids=() cfg_re='--config[= ]([^ ]+)'
  cap "процессы" processes/ps.txt 15 ps -eww -o pid,ppid,user,etimes,pcpu,pmem,rss,stat,args --sort=-rss
  # pid, ppid и аргументы — из уже снятого списка: повторный вызов ps тоже мог бы повиснуть
  awk '$1 ~ /^[0-9]+$/ {p = $1; q = $2; $1 = $2 = $3 = $4 = $5 = $6 = $7 = $8 = ""; sub(/^ +/, ""); print p, q, $0}' \
    "$out/processes/ps.txt" > "$work/ps"
  if command -v ss > /dev/null; then
    cap "слушающие порты" network/listen.txt 10 "${P[@]}" ss -ltnup
    for port in "${PORTS[@]}"; do
      awk -v p=":$port" '$1 == "tcp" && $2 == "LISTEN" && substr($5, length($5) - length(p) + 1) == p' \
        "$out/network/listen.txt" > "$work/port"
      [[ -s $work/port ]] && port_up[$port]=$(awk '{print $5}' "$work/port" | sort -u | tr '\n' ' ')
      port_pids[$port]=$(grep -oE 'pid=[0-9]+' "$work/port" | cut -d= -f2 | sort -un | tr '\n' ' ')
    done
  else
    note нет "ss" "пакет iproute2"
  fi
  read -r -a pids <<< "${port_pids[*]}"
  mapfile -t robot_pids < <({
    grep -E "$PROC_RE" "$work/ps" | awk -v self=$$ '$1 != self && $2 != self && $3 !~ /^\[/ {print $1}'
    printf '%s\n' "${pids[@]}"
  } | grep . | sort -un)
  if ((${#robot_pids[@]} == 0)); then
    note нет "процессы робота" "не запущено ничего похожего на демо, MCP, воркеры, PicoClaw, rosbridge"
    return
  fi
  note ок "процессы робота" "${#robot_pids[@]} шт., processes/ps.txt"
  for pid in "${robot_pids[@]}"; do
    for fd in fd/1 fd/2 cwd; do
      tgt=$(proc_link "$pid" "$fd") || tgt="(нет доступа)"
      echo "$pid $fd $tgt"
    done
  done > "$work/fds"
  redact < "$work/fds" > "$out/processes/fds.txt"
  while read -r pid fd tgt; do
    [[ $fd == fd/* && $tgt == /* && $tgt != /dev/* && $tgt != *' (deleted)' ]] || continue
    take "вывод процесса $pid (${fd/\//})" "$tgt" "processes/console_${pid}_$(flat "$tgt").txt"
  done < <(sort -u -k3,3 "$work/fds")
  grep -q ' /dev/pts/' "$work/fds" &&
    note нет "консоль процессов в терминале" "вывод не сохраняется; при сбое скопируйте его вручную (processes/fds.txt)"

  for pid in "${robot_pids[@]}"; do
    line=$(args_of "$pid")
    mapfile -d '' -t args < <(proc_read "$pid" cmdline)
    if [[ ${args[0]:-} == *python* || $line == *python* ]]; then
      venv=$(penv "$pid" VIRTUAL_ENV | cut -d= -f2-)
      if [[ -n $venv ]]; then pythons+=("$venv/bin/python")
      elif [[ ${args[0]:-} == /*python* ]]; then pythons+=("${args[0]}")
      fi
    fi
    while IFS= read -r var; do
      name=${var%%=*}
      have=0
      [[ -n ${!name+x} ]] && have=1
      [[ " ${ros_env[*]} " == *" $name="* ]] && have=1
      ((have)) || { ros_env+=("$var"); echo "$var  (из процесса $pid)" >> "$work/rosenv"; }
    done < <(penv "$pid" "$ROS_VARS")
    d=$(proc_link "$pid" cwd)
    if [[ $line =~ $LAB_RE || " ${port_pids[*]} " == *" $pid "* ]] && [[ -n $d ]]; then
      add_search "$d"
      if [[ $line =~ $cfg_re ]]; then
        tgt=${BASH_REMATCH[1]}
        [[ $tgt == /* ]] || tgt=$d/$tgt
        mcp_configs+=("$(readlink -f "$tgt" || echo "$tgt")")
      fi
    fi
    if [[ $line == *picoclaw* ]]; then
      tgt=$(proc_link "$pid" exe)
      [[ ${tgt##*/} == picoclaw* ]] && pico_bins+=("$tgt")
      while IFS= read -r var; do
        case ${var%%=*} in
          PICOCLAW_HOME) pico_homes+=("${var#*=}") ;;
          HOME) pico_homes+=("${var#*=}/.picoclaw") ;;
          PICOCLAW_CONFIG) pico_configs+=("${var#*=}") ;;
          PICOCLAW_LOG_FILE) pico_logs+=("${var#*=}") ;;
        esac
      done < <(penv "$pid" 'PICOCLAW_HOME|PICOCLAW_CONFIG|PICOCLAW_LOG_FILE|HOME')
    fi
  done

  # Скрипт процесса на порту стека: так приходит код воркера 8766, которого нет в присланных архивах.
  for port in "${PORTS[@]}"; do
    read -r -a pids <<< "${port_pids[$port]:-}"
    for pid in "${pids[@]}"; do
      mapfile -d '' -t args < <(proc_read "$pid" cmdline)
      script=
      for f in "${args[@]:1}"; do
        [[ $f == *.py || $f == *.sh ]] && { script=$f; break; }
      done
      [[ -n $script ]] || continue
      [[ $script == /* ]] || script=$(proc_link "$pid" cwd)/$script
      take "скрипт процесса на порту $port (pid $pid)" "$script" "processes/port${port}_${script##*/}"
    done
  done

  if command -v tmux > /dev/null && timeout 10 tmux list-panes -a \
    -F '#{session_name}:#{window_index}.#{pane_index}' > "$work/panes" 2> /dev/null; then
    while read -r line; do
      cap "консоль tmux $line" "processes/tmux_$(flat "${line//[:.]/_}").txt" 10 \
        tmux capture-pane -p -J -S -5000 -t "$line"
    done < "$work/panes"
  else
    note нет "сессии tmux" "консоль процесса, запущенного в обычном терминале, скрипт не видит"
  fi
}

# Порты стека: кто слушает и на каком адресе, ответ адресов статуса (GET), соединения с rosbridge.
section_network() {
  local port pid who path paths=() pids=()
  [[ -f $out/network/listen.txt ]] || return
  for port in "${PORTS[@]}"; do
    if [[ -z ${port_up[$port]:-} ]]; then
      note нет "порт $port (${PORT_ROLE[$port]})" "не слушается"
      continue
    fi
    who=""
    read -r -a pids <<< "${port_pids[$port]:-}"
    for pid in "${pids[@]}"; do who+="; pid $pid: $(args_of "$pid" | cut -c1-120)"; done
    note ок "порт $port (${PORT_ROLE[$port]})" "слушает ${port_up[$port]% }${who:-; процесс виден только с --sudo}"
    read -r -a paths <<< "${PORT_STATUS[$port]:-}"
    for path in "${paths[@]}"; do
      CAP_SHOW="GET http://127.0.0.1:$port$path" cap "порт $port: GET $path" "network/http_$port.txt" 6 \
        python3 -I -c "$HTTP_GET" "http://127.0.0.1:$port$path"
    done
  done
  if [[ -n ${port_up[9090]:-} ]] &&
    cap "соединения с портом 9090" network/port9090.txt 10 ss -tan '( sport = :9090 or dport = :9090 )'; then
    note ок "rosbridge :9090, соединения" "$(grep -c '^ESTAB' "$out/network/port9090.txt") ESTAB, \
$(grep -c '^TIME-WAIT' "$out/network/port9090.txt") в TIME-WAIT"
  fi
}

# Как запускают систему: юниты systemd с журналами, cron, rc.local, автозапуск рабочего стола, строки .bashrc,
# скрипты в каталогах --search, где упоминаются демо, MCP, воркеры, PicoClaw или драйверы.
section_startup() {
  local scope sc u f n=0 units=() files=() uid
  uid=$(id -u)
  for scope in system user; do
    sc=()
    [[ $scope == user ]] && sc=(--user)
    if ! timeout 10 systemctl "${sc[@]}" list-units --all --no-pager --no-legend --plain > "$work/units" 2>&1 ||
      ! timeout 10 systemctl "${sc[@]}" list-unit-files --no-pager --no-legend >> "$work/units" 2>&1; then
      note нет "юниты systemd ($scope)" "systemctl ${sc[*]} недоступен"
      continue
    fi
    mapfile -t units < <(awk '{print $1}' "$work/units" | grep -E '\.(service|timer|socket|target)$' |
      grep -iE "$UNIT_RE" | sort -u)
    if ((${#units[@]} == 0)); then
      note нет "юниты робота ($scope)" "в systemd нет ничего похожего — запуск вручную?"
      continue
    fi
    for u in "${units[@]}"; do
      if [[ $u == *@.* ]]; then  # шаблон: у него есть только файл, состояние и журнал — у экземпляров
        cap "юнит $u: файл шаблона" "units/${scope}_$u.txt" 10 systemctl "${sc[@]}" cat "$u"
        continue
      fi
      cap "юнит $u: состояние" "units/${scope}_$u.txt" 10 systemctl "${sc[@]}" show --no-pager -p Id,Description \
        -p LoadState,ActiveState,SubState,UnitFileState,FragmentPath,Type,User,WorkingDirectory,ExecStart \
        -p Restart,RestartUSec,NRestarts,Result,ExecMainStatus,WatchdogUSec,After,Requires,Wants,WantedBy \
        -p Environment,EnvironmentFiles,ActiveEnterTimestamp,InactiveEnterTimestamp "$u"
      cap "юнит $u: файл" "units/${scope}_$u.txt" 10 systemctl "${sc[@]}" cat "$u"
      if [[ $scope == system ]]; then
        cap "юнит $u: журнал" "units/${scope}_$u.txt" 30 "${P[@]}" journalctl -u "$u" --no-pager -o short-iso \
          --since "-${days}d" -n 5000
      elif ((use_sudo)); then
        # журнал пользователя часто виден только root: те же записи, что у journalctl --user -u
        cap "юнит $u: журнал" "units/${scope}_$u.txt" 30 "${P[@]}" journalctl _SYSTEMD_USER_UNIT="$u" _UID="$uid" \
          + USER_UNIT="$u" _UID="$uid" --no-pager -o short-iso --since "-${days}d" -n 5000
      else
        cap "юнит $u: журнал" "units/${scope}_$u.txt" 30 journalctl --user -u "$u" --no-pager -o short-iso \
          --since "-${days}d" -n 5000
      fi
    done
  done

  cap "crontab пользователя" startup/crontab.txt 5 crontab -l
  for f in /etc/crontab /etc/cron.d/* /etc/rc.local "$HOME"/.config/autostart/*.desktop; do
    [[ -r $f ]] || continue
    [[ $f == /etc/cron* ]] && ! grep -qE "$PROC_RE" "$f" && continue
    take "$f" "$f" "startup/$(flat "$f")"
  done
  for f in "$HOME/.bashrc" "$HOME/.profile" "$HOME/.bash_profile" "$HOME/.bash_aliases"; do
    [[ -r $f ]] && files+=("$f")
  done
  ((${#files[@]})) &&
    cap "строки shell-профиля про ROS и Python" startup/shellrc.txt 5 grep -HnE "^[^#]*($RC_RE)" "${files[@]}"

  while IFS= read -r f; do
    grep -qE "$PROC_RE" "$f" 2> /dev/null || continue
    ((++n > 40)) && break
    take "скрипт запуска $f" "$f" "startup/scripts/$(flat "$f")"
  done < <(find_in 5 -type f -size -256k \( -name '*.sh' -o -name '*.launch.py' -o -name '*.launch.xml' \
    -o -name '*.launch' -o -name '*.service' -o -name '*.desktop' -o -name Makefile \))
  ((n)) || note нет "скрипты запуска" "в ${search_dirs[*]} нет скриптов, где упоминаются демо, MCP или PicoClaw"
}

# Интерпретаторы: pyenv, python3 в PATH, файлы .python-version; для системного Python, найденных venv, версий
# pyenv и интерпретаторов процессов — версия и пакеты (pip freeze, без pip — importlib.metadata).
section_python() {
  local cfg py v n=0 seen=" " files=() pyenv_root=${PYENV_ROOT:-$HOME/.pyenv} pyenv
  pyenv=$(command -v pyenv || echo "$pyenv_root/bin/pyenv")
  if [[ -x $pyenv ]]; then
    cap "pyenv: версии" python/pyenv.txt 10 "$pyenv" versions
    cap "pyenv: global" python/pyenv.txt 10 "$pyenv" global
    while IFS= read -r v; do
      pythons+=("$pyenv_root/versions/$v/bin/python")
    done < <(timeout 10 "$pyenv" versions --bare 2> /dev/null)
  else
    note нет "pyenv" "нет в PATH и в $pyenv_root"
  fi
  cap "python3 в PATH (which -a)" python/pyenv.txt 5 which -a python3
  mapfile -t files < <(find_in 5 -type f -name .python-version)
  ((${#files[@]})) &&
    cap "файлы .python-version (pyenv local)" python/python_version_files.txt 10 grep -H . "${files[@]}"
  while IFS= read -r cfg; do
    pythons+=("$(dirname "$cfg")/bin/python")
  done < <(find_in 5 -type f -name pyvenv.cfg)
  pythons+=("$(command -v python3)")
  for py in "${pythons[@]}"; do
    [[ $seen == *" $py "* ]] && continue
    seen+="$py "
    n=$((n + 1))
    cap "Python $py" "python/pip_$n.txt" 10 "$py" -V || continue
    cap "pip freeze $py" "python/pip_$n.txt" 90 "$py" -m pip freeze --all ||
      cap "пакеты $py (importlib.metadata, без pip)" "python/pip_$n.txt" 30 "$py" -I -c "$PY_DISTS"
  done
}

# Код лаборатории: sha256 и сравнение с присланными версиями (копия, если отличается), git, журналы рядом с кодом,
# конфиги MCP, журналы воркеров из /tmp, веса моделей (только sha256: файлы как модели не открываются).
section_lab_code() {
  local f sum ref top k d n=0 seen=" " names=() found=()
  for k in "${!REFERENCE[@]}"; do names+=(-o -name "$k"); done
  mapfile -t found < <(find_in 7 -type f \( "${names[@]:1}" \) | sort)
  if ((${#found[@]} == 0)); then
    note нет "код лаборатории (${!REFERENCE[*]})" "не найден в ${search_dirs[*]} (укажите --search)"
  fi
  mkdir -p "$out/lab_code"
  for f in "${found[@]}"; do
    n=$((n + 1))
    sum=$(timeout 30 sha256sum < "$f" | cut -d' ' -f1)
    ref=${REFERENCE[${f##*/}]}
    echo "$sum  $f  $(stat -c '%s байт, изменён %y' "$f")" >> "$out/lab_code/files.txt"
    if [[ $sum == "$ref" ]]; then
      note ок "$f" "совпадает с версией, присланной лабораторией"
    else
      note ок "$f" "ОТЛИЧАЕТСЯ от присланной версии — копия в lab_code/"
      take "копия $f" "$f" "lab_code/${n}_${f##*/}"
    fi
    [[ ${f##*/} == ros_mcp_server.py ]] && mcp_configs+=("$(dirname "$f")"/*.json)
    # корень репозитория: по git, иначе для демо — на два уровня выше src/camera/
    top=$(timeout 10 git -C "$(dirname "$f")" rev-parse --show-toplevel 2> /dev/null) || top=$(dirname "$f")
    [[ $top == */src/camera ]] && top=${top%/src/camera}
    [[ $seen == *" $top "* ]] && continue
    seen+="$top "
    if [[ -d $top/.git ]]; then
      cap "git $top: коммит" "lab_code/git_$n.txt" 10 git -C "$top" --no-optional-locks log -1 --format='%H %ci %d %s'
      cap "git $top: локальные правки" "lab_code/git_$n.txt" 10 git -C "$top" --no-optional-locks status --porcelain -b
      cap "git $top: объём правок" "lab_code/git_$n.txt" 10 git -C "$top" --no-optional-locks diff --stat
      cap "git $top: откуда" "lab_code/git_$n.txt" 10 git -C "$top" remote -v
    fi
    while IFS= read -r f; do
      take "журнал $f" "$f" "lab_code/logs/$(flat "$f")"
    done < <(timeout 30 find "$top" -maxdepth 3 -type f \( -name nohup.out -o -name '*.log' \) -mtime -"$days" \
      -printf '%T@ %p\n' 2> /dev/null | sort -rn | head -n 20 | cut -d' ' -f2-)
  done
  while IFS= read -r f; do
    [[ -f $f ]] && take "конфиг MCP $f" "$f" "lab_code/mcp_config_$(flat "$f")"
  done < <(printf '%s\n' "${mcp_configs[@]}" | sort -u)

  for d in "${LAB_LOG_DIRS[@]}"; do
    if ! exists "$d"; then
      note нет "журналы воркеров $d" "каталога нет"
      continue
    fi
    while IFS= read -r f; do
      take "журнал воркера $f" "$f" "lab_code/logs/$(flat "$f")"
    done < <(timeout 10 "${P[@]}" find "$d" -maxdepth 1 -type f -name '*.log' 2> /dev/null)
  done

  find_in 7 -type f \( -name '*.pt' -o -name '*.pth' -o -name '*.onnx' -o -name '*.engine' \) -size -500M |
    head -n 30 > "$work/weights"
  if [[ -s $work/weights ]]; then
    while IFS= read -r f; do
      echo "$(timeout 60 sha256sum < "$f" | cut -d' ' -f1)  $(stat -c '%s' "$f") байт  $f"
    done < "$work/weights" | redact > "$out/lab_code/weights.txt"
    note ок "веса моделей: sha256 (как модели не открываются)" "$(wc -l < "$work/weights") шт., lab_code/weights.txt"
  else
    note нет "веса моделей (*.pt, *.pth, *.onnx, *.engine)" "не найдены в ${search_dirs[*]}"
  fi
}

# PicoClaw: версия (запускается только сам picoclaw — ELF с этим именем; picoclaw-launcher принимает «version»
# за путь к конфигу и запускает onboard, который перезаписывает workspace), sha256 бинарников, версия по
# build_info в config.json, каталог (состав, конфиг без ключей, журналы шлюза и MCP-серверов, число сессий).
section_picoclaw() {
  local b src h f n=0 seen=" " homes=() logs=() ver sess
  pico_bins+=("$(command -v picoclaw)" "$HOME/.local/bin/picoclaw" /usr/local/bin/picoclaw)
  while IFS= read -r f; do pico_bins+=("$f"); done < <(find_in 4 -type f -name 'picoclaw*' -perm -u+x)
  for src in "${pico_bins[@]}"; do
    [[ -n $src && -f $src && -x $src ]] || continue
    b=$(readlink -f "$src")
    [[ $seen == *" $b "* ]] && continue
    seen+="$b "
    if [[ ${src##*/} == picoclaw && ${b##*/} != *launcher* ]] && is_elf "$b"; then
      cap "PicoClaw: версия $b" picoclaw/version.txt 10 "$b" version
    fi
    cap "PicoClaw: sha256 $b" picoclaw/version.txt 10 sha256sum "$b"
  done
  [[ $seen == " " ]] &&
    note нет "PicoClaw: бинарник" "не найден в PATH, ~/.local/bin, /usr/local/bin, ${search_dirs[*]}"

  homes=("${PICOCLAW_HOME:-}" "$HOME/.picoclaw" "${pico_homes[@]}")
  seen=" "
  for h in "${homes[@]}"; do
    [[ -z $h || $seen == *" $h "* ]] && continue
    seen+="$h "
    exists "$h" || continue
    n=$((n + 1))
    note ок "PicoClaw: каталог $h" "picoclaw/home_$n/"
    cap "PicoClaw: состав $h" "picoclaw/home_$n/ls.txt" 10 "${P[@]}" find "$h" -maxdepth 3 \
      -path "$h/workspace/skills" -prune -o -printf '%M %10s %TY-%Tm-%Td %TH:%TM  %P\n'
    if exists "$h/config.json"; then
      take "PicoClaw: конфиг $h/config.json" "$h/config.json" "picoclaw/home_$n/config.json"
      ver=$(python3 -I -c 'import json, sys
b = json.load(open(sys.argv[1])).get("build_info")
if b:
    print("{} (git {})".format(b.get("version", "?"), b.get("git_commit", "?")))' \
        "$out/picoclaw/home_$n/config.json" 2> /dev/null)
      [[ -n $ver ]] && note ок "PicoClaw: версия по build_info в $h/config.json" "$ver"
    fi
    exists "$h/.security.yml" &&
      note ок "PicoClaw: $h/.security.yml" "есть; не копируется — в нём ключи"
    mapfile -t logs < <({
      timeout 10 "${P[@]}" find "$h/logs" -maxdepth 1 -type f
      timeout 10 "${P[@]}" find "$h/workspace/mcp" -maxdepth 1 -type f -name '*.log'
      timeout 10 "${P[@]}" find "$h/workspace" -maxdepth 1 -type f -name heartbeat.log
    } 2> /dev/null)
    for f in "${logs[@]}"; do take "PicoClaw: журнал $f" "$f" "picoclaw/home_$n/${f#"$h"/}"; done
    ((${#logs[@]})) || note нет "PicoClaw: журналы $h/logs" "пусто — шлюз пишет в консоль или в журнал юнита"
    if exists "$h/workspace/sessions"; then
      sess=$(timeout 10 "${P[@]}" find "$h/workspace/sessions" -maxdepth 1 -type f -printf '%s\n' 2> /dev/null |
        awk '{n++; s += $1} END {printf "файлов: %d, %.0f КБ", n, s / 1024}')
      note ок "PicoClaw: сессии $h/workspace/sessions" "$sess; содержимое не копируется"
    fi
  done
  seen=${seen# }
  ((n)) || note нет "PicoClaw: каталог" "нет ни одного из: ${seen% }"
  for f in "${pico_configs[@]}" "${pico_logs[@]}"; do
    exists "$f" && take "PicoClaw: $f" "$f" "picoclaw/$(flat "$f")"
  done
}

# Граф ROS 2: темы, узлы, издатели и QoS тем движения и скана, параметры цепочки /cmd_vel → twist_mux →
# diff_drive_controller, контроллеры ros2_control, URDF, сводка /scan по секторам, одно сообщение /odom.
section_ros() {
  local f t distro="" dump=() files=() pubs=""
  if ((${#ros_setups[@]} == 0)); then
    if [[ -n ${ROS_DISTRO:-} && -f /opt/ros/$ROS_DISTRO/setup.bash ]]; then
      ros_setups=("/opt/ros/$ROS_DISTRO/setup.bash")
    else
      mapfile -t files < <(compgen -G '/opt/ros/*/setup.bash')
      ((${#files[@]})) && ros_setups=("${files[0]}")
    fi
  fi
  if ((${#ros_setups[@]} == 0)) && ! command -v ros2 > /dev/null; then
    note нет "ROS 2" "нет /opt/ros/*/setup.bash и ros2 в PATH (укажите --ros-setup)"
    return
  fi
  for f in "${ros_setups[@]}"; do [[ $f =~ ^/opt/ros/([^/]+)/ ]] && distro=${BASH_REMATCH[1]}; done
  [[ -n $distro ]] || distro=${ROS_DISTRO:-}
  # до Humble включительно param dump пишет в файл, а в консоль — только с --print; с Iron флага нет
  [[ $distro =~ ^(foxy|galactic|humble)$ ]] && dump=(--print)
  mkdir -p "$out/ros"
  {
    echo "setup: ${ros_setups[*]}"
    env | grep -E "^($ROS_VARS)=" | sed 's/$/  (из окружения сбора)/'
    cat "$work/rosenv" 2> /dev/null
  } | redact > "$out/ros/env.txt"
  mapfile -t files < <(compgen -G '/opt/ros/*/share/rosbridge_server/package.xml'
    find_in 6 -type f -path '*rosbridge_server/package.xml')
  if ((${#files[@]})); then
    cap "rosbridge_server: версия" ros/rosbridge_version.txt 5 grep -H -m 1 '<version>' "${files[@]}"
  else
    note нет "rosbridge_server" "package.xml не найден"
  fi

  rosrun "ROS 2: ros2 daemon до сбора" ros/daemon.txt 20 daemon status
  if ! grep -q 'daemon is running' "$work/seg"; then
    rosrun "ROS 2: служебный ros2 daemon поднят на время сбора" ros/daemon.txt 20 daemon start && daemon_started=1
    sleep 3
  fi
  rosrun "ROS 2: темы" ros/topics.txt 30 topic list -t
  rosrun "ROS 2: узлы" ros/nodes.txt 30 node list
  for t in /scan /cmd_vel /odom /cmd_vel_joy /cmd_vel_tracker /diff_cont/cmd_vel_unstamped; do
    if ! grep -qE "^$t( |$)" "$out/ros/topics.txt"; then
      note нет "ROS 2: тема $t" "нет в графе"
      continue
    fi
    f=ros/topic_$(flat "$t").txt
    rosrun "ROS 2: $t, издатели, подписчики, QoS" "$f" 30 topic info -v "$t"
    pubs+="$t $(sed -n 's/^Publisher count: //p' "$out/$f" | head -n 1), "
  done
  [[ -n $pubs ]] && note ок "ROS 2: число издателей" "${pubs%, }"
  # цепочка движения на роботе (R7): /cmd_vel → twist_mux → /diff_cont/cmd_vel_unstamped → diff_drive_controller
  if ros_node /diff_cont; then
    rosrun "ROS 2: параметры /diff_cont" ros/diff_cont.txt 20 param dump /diff_cont "${dump[@]}"
    f=$(grep -oE '(cmd_vel_timeout|use_stamped_vel|wheel_separation|wheel_radius): [^ ]+' "$out/ros/diff_cont.txt" |
      paste -sd ';' -)
    note "$([[ -n $f ]] && echo ок || echo нет)" "ROS 2: /diff_cont, остановка при тишине /cmd_vel" \
      "${f:-нет cmd_vel_timeout в ros/diff_cont.txt}"
  fi
  ros_node /twist_mux && rosrun "ROS 2: параметры /twist_mux" ros/twist_mux.txt 20 param dump /twist_mux "${dump[@]}"
  ros_node /topic_watchdog && rosrun "ROS 2: узел /topic_watchdog" ros/topic_watchdog.txt 20 node info /topic_watchdog
  ros_node /controller_manager &&
    rosrun "ROS 2: контроллеры ros2_control" ros/controllers.txt 20 control list_controllers
  ros_node /robot_state_publisher && rosrun "ROS 2: URDF (robot_description)" ros/robot_description.txt 20 \
    param get /robot_state_publisher robot_description
  if grep -qE '^/scan( |$)' "$out/ros/topics.txt"; then
    scan_sectors
  fi
  if grep -qE '^/odom( |$)' "$out/ros/topics.txt"; then
    ros_cmd 8 ros2 topic echo --no-arr /odom | redact | awk 'done {next} {print} /^---$/ {done = 1}' \
      > "$out/ros/echo_odom.txt"
    if grep -q '^---$' "$out/ros/echo_odom.txt"; then
      note ок "ROS 2: /odom, одно сообщение без массивов" ros/echo_odom.txt
    else
      note нет "ROS 2: /odom, одно сообщение без массивов" "за 8 с не пришло, см. ros/echo_odom.txt"
    fi
  fi
  if ((daemon_started)); then
    rosrun "ROS 2: служебный ros2 daemon остановлен" ros/daemon.txt 20 daemon stop
    daemon_started=0
  fi
  while IFS= read -r f; do
    take "журнал ROS 2 $f" "$f" "ros/logs/$(flat "$f")"
  done < <(timeout 30 find "$HOME/.ros/log" -type f -mtime -"$days" -printf '%T@ %p\n' 2> /dev/null |
    sort -rn | head -n 30 | cut -d' ' -f2-)
}

# Сводка одного сообщения /scan по секторам 10° без массива (scan_sectors.py): виден ли блок NaN прямо по курсу.
scan_sectors() {
  local rc what="ROS 2: /scan по секторам 10°"
  if [[ ! -f $here/scan_sectors.py ]]; then
    note нет "$what" "рядом со скриптом нет scan_sectors.py"
    return
  fi
  ros_cmd 30 python3 "$here/scan_sectors.py" /scan 10 | redact > "$out/ros/scan_sectors.txt"
  rc=${PIPESTATUS[0]}
  case $rc in
    0) note ок "$what" "$(tail -n 1 "$out/ros/scan_sectors.txt"); ros/scan_sectors.txt" ;;
    2 | 3) note нет "$what" "$(tail -n 1 "$out/ros/scan_sectors.txt")" ;;
    124 | 137) note таймаут "$what" ros/scan_sectors.txt ;;
    *) note ошибка "$what" "код $rc, см. ros/scan_sectors.txt" ;;
  esac
}

# Параметры камеры (только с --camera): rs-enumerate-devices открывает устройство, поэтому не запускается, пока
# камеру может держать другой процесс.
section_camera() {
  local holders what="камера: параметры (rs-enumerate-devices -c)"
  if ((!camera)); then
    note нет "$what" "не снимались (флаг --camera)"
    return
  fi
  if ! command -v rs-enumerate-devices > /dev/null; then
    note нет "$what" "rs-enumerate-devices не найден (пакет librealsense2-utils)"
    return
  fi
  holders=$(for pid in "${robot_pids[@]}"; do args_of "$pid" | grep -qE "$CAMERA_RE" && echo "$pid"; done |
    paste -sd ',' -)
  if [[ -n $holders ]]; then
    note нет "$what" "не запускался: камерой может владеть процесс $holders — остановите его и повторите с --camera"
    return
  fi
  cap "$what" system/camera.txt 30 rs-enumerate-devices -c
}

finish() {
  local fixed broken masked s counts=""
  broken=$(grep -rlE '^collect_diag: сбой redact.py \(код [0-9]+\)' "$out" | sed "s|^$out/||")
  [[ -n $broken ]] && note ошибка "сбой redact.py" "файлы неполные: ${broken//$'\n'/, }"
  fixed=$(redact --check "$out")
  if [[ -n $fixed ]]; then
    note ошибка "повторная проверка секретов" "дополнительно вырезано в: ${fixed//$'\n'/, }"
  else
    note ок "повторная проверка секретов" "остатков нет"
  fi
  masked=$(grep -rao '<redacted>' "$out" | wc -l)
  for s in ок нет права таймаут ошибка; do
    counts+="$s $(grep -c "^\[$s\]" "$report"), "
  done
  echo "итого: ${counts%, }; вырезано значений: $masked" | tee -a "$report" >&2
  tar -czf "$out.tar.gz" -C "$out_parent" "$name" || die "не удалось упаковать $out"
  echo "$out.tar.gz"
}

{
  echo "neurobot collect_diag: $(date -Is)"
  echo "хост $host, пользователь $(id -un) (uid $(id -u)), sudo: $( ((use_sudo)) && echo да || echo нет)"
  echo "аргументы: ${args_line:-нет}; поиск: ${search_dirs[*]}"
  echo "sha256: collect_diag.sh $(sha256sum < "$here/collect_diag.sh" | cut -c1-16)…," \
    "redact.py $(sha256sum < "$here/redact.py" | cut -c1-16)…"
  echo "статусы: ок — собрано; нет — не найдено; права — нужен --sudo; таймаут; ошибка — см. файл"
  echo
} > "$report"
mkdir -p "$out/system"
start_tegrastats
section_system
section_power
section_devices
section_kernel
section_processes
section_network
section_startup
section_python
section_lab_code
section_picoclaw
section_ros
section_camera
finish_tegrastats
finish
