#!/usr/bin/env bash
# Проверка двойника в ВМ sim: подъёмы в мирах, пробы, сводка с критериями (README двойника §7).
#   neurobot/sim/twin/check.sh [out_dir] [--seconds 20] [--domain 31] [--port 9091]
# По умолчанию out_dir = experiments/results/neurobot_<ГГГГММДД>_twin; существующий каталог не перезаписывается.
# Свой домен DDS и порт rosbridge — чтобы не пересекаться со стендом и узлами других компонентов в той же ВМ. Если
# двойник уже поднят — отказ (код 2); гасит только свои прогоны.
# Прогоны (каталоги в out_dir):
#   xacro  xacro → check_urdf (кинематика), xacro → gz sdf -k (модель Gazebo целиком);
#   lab    cmd_vel_timeout 0,5: готовность; нагрузка группы (vm_load.py); sensors — типы и частоты тем, RTF без
#          камеры и с ней, TF, слепой сектор лидара, IMU в покое, глубина и низкий ящик; mux; silence; odom; truth —
#          истина при set_pose, create и remove (зазор, касания колёс и роликов, RTF);
#   hold   cmd_vel_timeout 0: готовность с /odom до первой команды; silence 0,3 м/с — тишина не останавливает, база
#          въезжает в low_box (касание);
#          параллельно наблюдатель жестов (neurobot/sim/gestures/observer.py) с препятствиями мира lab;
#   ramp   sensors (кратко), ramp — въезд с пола, стоянка на склоне, наклон по IMU;
#   step   sensors — робот на площадке, глубина видит обрыв; затем kill -9 sensor_model — двойник гаснет целиком.
# Итог — summary.json и manifest.json (twin_summary.py), даже если прогон сорвался; код 0 — все критерии выполнены.
set -uo pipefail
TWIN="$(cd "$(dirname "$0")" && pwd)"
source "$TWIN/../../../scripts/sim/lib.sh"
OUT="$PROJ/experiments/results/neurobot_$(date +%Y%m%d)_twin" SEC=20 DOM=31 PORT=9091 ARGV="$*"
while [ $# -gt 0 ]; do
  case "$1" in
    --seconds) SEC="$2"; shift 2;; --domain) DOM="$2"; shift 2;; --port) PORT="$2"; shift 2;;
    *) OUT="$1"; shift;;
  esac
done
[ -e "$OUT" ] && { echo "[check] $OUT уже существует — не перезаписываю" >&2; exit 2; }
if vm 20 '[ -f ~/twin_runs/current/pid ] && pgrep -g "$(cat ~/twin_runs/current/pid)" >/dev/null' 2>/dev/null; then
  echo "[check] двойник уже поднят ($(vm 10 'readlink -f ~/twin_runs/current')) — остановить явно: twin.sh down" >&2
  exit 2
fi
mkdir -p "$OUT"
echo "neurobot/sim/twin/check.sh $ARGV" > "$OUT/command.txt"
date -Iseconds > "$OUT/run_started.txt"
RUN="" D=""
invm() { local s="$1"; shift; vm "$s" "source $SIM_DIR/vm_env.sh; export ROS_DOMAIN_ID=$DOM; cd /tmp; $*"; }
down_own() {
  [ -n "$RUN" ] && "$TWIN/twin.sh" down --run "$RUN" >> "$OUT/stops.log" 2>&1
  RUN=""
}
finish() {
  down_own
  date -Iseconds > "$OUT/run_finished.txt"
  vm 30 "dpkg-query -W ros-jazzy-gz-sim-vendor ros-jazzy-ros-gz-bridge ros-jazzy-ros-gz-sim ros-jazzy-gz-ros2-control \
    ros-jazzy-controller-manager ros-jazzy-diff-drive-controller ros-jazzy-joint-state-broadcaster ros-jazzy-twist-mux \
    ros-jazzy-imu-filter-madgwick ros-jazzy-robot-state-publisher ros-jazzy-xacro ros-jazzy-rosbridge-server \
    ros-jazzy-rmw-cyclonedds-cpp ros-jazzy-rclpy; python3 -c 'import numpy; print(\"numpy\", numpy.__version__)'; \
    echo kernel \$(uname -r); echo nproc \$(nproc)" > "$OUT/versions_vm.txt" 2>&1
  { sw_vers; sysctl -n machdep.cpu.brand_string hw.ncpu hw.memsize; limactl --version; } > "$OUT/versions_host.txt"
  "$PROJ/.venv/bin/python" "$TWIN/twin_summary.py" "$OUT"
}
trap 'finish; exit $?' EXIT
trap 'exit 130' INT TERM
host_load() { uptime; vm 10 'uptime; ps -eo pcpu,args --sort=-pcpu | head -12 | cut -c1-140'; }
up() {  # up <прогон> [флаги twin.sh up] — свой двойник, готовность и файлы прогона в $OUT/<прогон>
  down_own
  D="$OUT/$1"; shift; mkdir -p "$D"
  echo "--- $(basename "$D"): twin.sh up $*" >&2
  host_load > "$D/host_load_before.txt" 2>&1
  "$TWIN/twin.sh" up --domain "$DOM" --rosbridge-port "$PORT" "$@" > "$D/start.txt" 2>&1
  local rc=$?
  RUN="$(sed -n 's/^\[twin\] PGID [0-9]* домен [0-9]* run_dir \(.*\)$/\1/p' "$D/start.txt")"
  for f in args.txt domain gz_partition controllers.yaml bridge.yaml robot.urdf; do
    [ -n "$RUN" ] && vm 10 "cat $RUN/$f" > "$D/$f" 2>/dev/null
  done
  [ "$rc" = 0 ] || { echo "[check] $(basename "$D"): двойник не поднялся — $D/start.txt" >&2; return 1; }
  invm 90 python3 "$TWIN/twin_ready.py" --timeout 30 --run-dir "$RUN" > "$D/ready.json"
}
probe() { local name="$1"; shift; invm 900 python3 "$TWIN/twin_probe.py" "$@" > "$D/$name.json" 2> "$D/$name.log"; }
keep_log() {
  [ -n "$RUN" ] && vm 20 "cat $RUN/launch.log" > "$D/launch.log" 2>/dev/null
  host_load > "$D/host_load_after.txt" 2>&1
}

D="$OUT/xacro"; mkdir -p "$D"
invm 60 "xacro $TWIN/urdf/neurochip.urdf.xacro sim:=false > kin.urdf && check_urdf kin.urdf" > "$D/check_urdf.txt" 2>&1
echo "rc=$?" >> "$D/check_urdf.txt"
invm 60 "xacro $TWIN/urdf/neurochip.urdf.xacro controllers:=/dev/null > sim.urdf && gz sdf -k sim.urdf" \
  > "$D/gz_sdf_check.txt" 2>&1
echo "rc=$?" >> "$D/gz_sdf_check.txt"

if up lab; then
  invm $((SEC + 120)) python3 "$SIM_DIR/vm_load.py" --seconds "$SEC" --pid-file "$RUN/pid" --topics /clock /scan /odom \
    > "$D/load.json" 2> "$D/load.log"
  probe sensors sensors --world lab --seconds "$SEC"
  probe mux mux --world lab
  probe silence silence --world lab
  probe odom odom --world lab
  probe truth truth --world lab
  keep_log
fi
if up hold --hold; then
  OBS="$RUN/observer.jsonl"
  # Простая команда в фоне (не список через &&): иначе фоновая подоболочка держит вывод limactl, и вызов не вернётся.
  invm 20 "cd $PROJ; PYTHONDONTWRITEBYTECODE=1 setsid nohup python3 -m neurobot.sim.gestures.observer --out $OBS \
    --node-name twin_check_observer --extra-cmd /diff_cont/cmd_vel_unstamped --world-sdf $TWIN/worlds/lab.sdf \
    --wall-regex 'wall|table|chair|person|box' --footprint-radius 0.195 > $RUN/observer.log 2>&1 < /dev/null & \
    echo \$! > $RUN/observer.pid"
  probe silence silence --world lab --v 0.3 --drive-s 1 --wait-s 6
  vm 30 "kill -INT \$(cat $RUN/observer.pid); for i in \$(seq 50); do kill -0 \$(cat $RUN/observer.pid) 2>/dev/null \
    || break; sleep 0.2; done; cat $OBS" > "$D/observer.jsonl" 2>/dev/null
  vm 10 "cat $RUN/observer.log" > "$D/observer.log" 2>/dev/null
  keep_log
fi
if up ramp --world ramp; then
  probe sensors sensors --world ramp --seconds 5
  probe ramp ramp --world ramp
  keep_log
fi
if up step --world step; then
  probe sensors sensors --world step --seconds 5
  keep_log
  invm 60 python3 "$TWIN/../stand/stand_procs.py" crash --run-dir "$RUN" --victim sensor_model.py \
    > "$D/crash.json" && RUN=""                                    # двойник погас сам
fi
