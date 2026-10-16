#!/usr/bin/env bash
# Проверка профиля neurobot в ВМ sim: подъёмы стенда в разных режимах, замеры, сводка с критериями (README, §6).
#   neurobot/sim/stand/check.sh [out_dir] [--seconds 30] [--starts 0]
# По умолчанию out_dir = experiments/results/neurobot_<ГГГГММДД>_stand; существующий каталог не перезаписывается.
# Чужой стенд не трогает: если в ВМ уже поднят стенд — отказ (код 2); гасит только свои прогоны.
# Прогоны (каталоги в out_dir):
#   hold          TB3, stm32 hold: RTF и нагрузка за --seconds, частоты, /scan, /cmd_vel; проба без сторожа;
#                 0,5 м/с и тишина 4 с — база упирается в стену (S1 на стенде, стена твёрдая);
#   timeout300    сторож 300 мс (просьба PLAN);
#   stamped500    /cmd_vel TwistStamped, сторож 500 мс (вероятный реальный: diff_drive_controller Foxy);
#   odom20_limits --odom-hz 20 (исправленный параметр DiffDrive), пределы stm32; затем kill -9 stm32_sim — стенд
#                 гаснет целиком, gz sim в ВМ не остаётся;
#   neurochip     следующий подъём — база NeuroChip, окклюдер и коды URG в /scan; RTF, частоты, въезд в стену;
#   neg_box, neg_shelf, neg_table — мир lab_low: лидар NeuroChip не видит препятствие и база в него въезжает (RS1);
#                 после neg_table — сервер gz «завис» (SIGSTOP) и kill -9 stm32_sim: стенд всё равно гаснет целиком;
#   starts/NNN    --starts N холодных подъёмов up → ready → down (TB3).
# Итог — summary.json и manifest.json, даже если прогон сорвался; код 0 — все критерии выполнены.
set -uo pipefail
STAND="$(cd "$(dirname "$0")" && pwd)"
source "$STAND/../../../scripts/sim/lib.sh"
OUT="$PROJ/experiments/results/neurobot_$(date +%Y%m%d)_stand" SEC=30 STARTS=0 ARGV="$*"
while [ $# -gt 0 ]; do
  case "$1" in --seconds) SEC="$2"; shift 2;; --starts) STARTS="$2"; shift 2;; *) OUT="$1"; shift;; esac
done
[ -e "$OUT" ] && { echo "[check] $OUT уже существует — не перезаписываю" >&2; exit 2; }
if vm 20 '[ -f ~/robot_sim/current/pid ] && pgrep -g "$(cat ~/robot_sim/current/pid)" >/dev/null' 2>/dev/null; then
  echo "[check] в ВМ уже поднят стенд ($(vm 10 'readlink -f ~/robot_sim/current')) — чужой прогон не трогаю;" \
    "остановить явно: neurobot/sim/stand/stand.sh down" >&2
  exit 2
fi
mkdir -p "$OUT"
echo "neurobot/sim/stand/check.sh $ARGV" > "$OUT/command.txt"
date -Iseconds > "$OUT/run_started.txt"
RUN="" D=""
invm() { local s="$1"; shift; vm "$s" "source $SIM_DIR/vm_env.sh; cd /tmp; $*"; }
down_own() {  # погасить свой стенд (stand.sh down --run): чужой, поднятый после нашего, не трогаем
  [ -n "$RUN" ] && "$STAND/stand.sh" down --run "$RUN" >> "$OUT/stops.log" 2>&1
  RUN=""
}
finish() {
  down_own
  date -Iseconds > "$OUT/run_finished.txt"
  vm 30 "dpkg-query -W ros-jazzy-gz-sim-vendor ros-jazzy-ros-gz-bridge ros-jazzy-turtlebot3-gazebo \
    ros-jazzy-rosbridge-server ros-jazzy-rmw-cyclonedds-cpp ros-jazzy-rclpy; echo kernel \$(uname -r); \
    echo nproc \$(nproc); sha256sum /opt/ros/jazzy/share/turtlebot3_gazebo/models/turtlebot3_waffle_pi/model.sdf" \
    > "$OUT/versions_vm.txt" 2>&1
  { sw_vers; sysctl -n machdep.cpu.brand_string hw.ncpu hw.memsize; limactl --version; } > "$OUT/versions_host.txt"
  "$PROJ/.venv/bin/python" "$STAND/summarize_check.py" "$OUT"
}
trap 'finish; exit $?' EXIT
trap 'exit 130' INT TERM
up() {  # up <прогон> [флаги stand.sh up] — подъём своего стенда, готовность и сгенерированные файлы в $OUT/<прогон>
  down_own
  D="$OUT/$1"; shift; mkdir -p "$D"
  echo "--- $(basename "$D"): stand.sh up $*" >&2
  "$STAND/stand.sh" up "$@" > "$D/start.txt" 2>&1
  local rc=$?
  # Каталог прогона печатает vm_start.sh ещё до готовности — по нему гасим и неготовый стенд.
  RUN="$(sed -n 's/^\[vm_start\] PID [0-9]* run_dir \(.*\)$/\1/p' "$D/start.txt")"
  [ "$rc" = 0 ] || { echo "[check] $(basename "$D"): стенд не поднялся — $D/start.txt" >&2; return 1; }
  invm 60 python3 "$STAND/stand_ready.py" > "$D/ready.json"
  for f in args.txt launch_file bridge.yaml models/robot.sdf world.sdf; do
    vm 10 "cat $RUN/$f" > "$D/$(basename "$f")"
  done
}
probe() { local name="$1"; shift; invm 60 python3 "$STAND/stm32_probe.py" "$@" > "$D/$name.json"; }
host_load() { uptime; limactl list; top -l 2 -n 0 -s 1 | grep -E '^(Load Avg|CPU usage|PhysMem)' | tail -3; }

if up hold --stm32-timeout-ms 0; then
  host_load > "$D/host_load.txt"
  "$SIM_DIR/measure_load.sh" "$D/load.json" "$SEC" /clock /scan /odom /ground_truth/pose /imu /joint_states /tf \
    /stand/base_cmd_vel
  probe probe
  probe probe_wall --vx 0.5 --drive-s 1 --silence-s 4
fi
up timeout300 --stm32-timeout-ms 300 && probe probe
up stamped500 --cmd-vel-type TwistStamped --stm32-timeout-ms 500 && probe probe --type TwistStamped
if up odom20_limits --odom-hz 20 --stm32-max-lin 0.15 --stm32-max-lin-acc 0.5; then
  invm 60 python3 "$SIM_DIR/topic_rates.py" --seconds 10 --topics /odom /scan > "$D/rates.json"
  probe probe
  invm 60 python3 "$STAND/stand_procs.py" crash --run-dir "$RUN" > "$D/crash.json" && RUN=""    # стенд погас сам
fi
if up neurochip --base neurochip --scan-occluder --scan-codes; then
  "$SIM_DIR/measure_load.sh" "$D/load.json" "$SEC" /clock /scan /stand/scan_raw /odom /ground_truth/pose
  probe probe_wall --vx 0.5 --drive-s 1 --silence-s 4
fi
for lane in box:0 shelf:1.0 table:-1.0; do
  up "neg_${lane%%:*}" --base neurochip --world "$STAND/worlds/lab_low.sdf" --y "${lane#*:}" &&
    probe probe_obstacle --vx 0.2 --drive-s 5 --silence-s 1
done
[ "$(basename "$D")" = neg_table ] && [ -n "$RUN" ] &&
  invm 60 python3 "$STAND/stand_procs.py" crash --run-dir "$RUN" --hang-gz > "$D/crash.json" && RUN=""
for ((i = 1; i <= STARTS; i++)); do
  up "starts/$(printf %03d "$i")"
done
