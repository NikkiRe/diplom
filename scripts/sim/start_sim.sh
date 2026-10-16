#!/usr/bin/env bash
# Запуск headless-симуляции TurtleBot3 в VM "sim" с хоста.
#   scripts/sim/start_sim.sh [--world turtlebot3_world|turtlebot3_house|empty_world|/путь/в/VM.sdf]
#       [--rtf 1.0] [--step 0.001] [--nav2] [--no-rosbridge] [--port 9090]
#       [--camera lazy|stream|off] [--camera-hz 30] [--jpeg-quality 80] [--lidar-hz 10] [--imu-hz 200] [--odom-hz 30]
#       [--x X --y Y --yaw R] [--world-control] [--ready-timeout 120] [--profile default|neurobot]
#       [-- <доп. аргументы ros2 launch ключ:=значение>]
# --profile — умолчания и launch-файл стенда из profiles/<имя>.sh (явные флаги их переопределяют, порядок не важен);
#   neurobot — стенд робота NeuroChip (neurobot/sim/stand/README.md); default — как без флага.
# --world-control — сервис /world/<мир>/control (ros_gz_interfaces/srv/ControlWorld) для пошагового режима (lockstep_probe.py).
# --rtf 0 — без ограничения скорости (настолько быстро, насколько позволяет CPU); --camera-hz 0 — камера выключена.
# --camera lazy (по умолчанию) — кадры рендерятся, только пока на /camera/* есть подписчик; stream — как в TB3 (всегда).
# Готовность: /clock идёт; при rosbridge — WebSocket отвечает с хоста; при --nav2 — lifecycle Nav2 активен.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
WORLD=turtlebot3_world RTF=1.0 STEP="" NAV2=false WCTL=false RB=true PORT=9090 CAMMODE=lazy CAM=30 JPEGQ=80 LID=10 IMU=200 ODOM=30 X="" Y="" YAW=0.0 READY=120
EXTRA=() PROFILE=default LAUNCH="" PROFILE_ARGS=() READY_CHECK="" prev=""
for a in "$@"; do
  [ "$a" = -- ] && break
  [ "$prev" = --profile ] && PROFILE="$a"
  prev="$a"
done
[ "$prev" = --profile ] && PROFILE=""
if [ "$PROFILE" != default ]; then
  [ -f "$SIM_DIR/profiles/$PROFILE.sh" ] || { echo "неизвестный профиль: $PROFILE" >&2; exit 2; }
  source "$SIM_DIR/profiles/$PROFILE.sh"
fi
while [ $# -gt 0 ]; do
  case "$1" in
    --world) WORLD="$2"; shift 2;;  --rtf) RTF="$2"; shift 2;;  --step) STEP="$2"; shift 2;;  --nav2) NAV2=true; shift;;
    --no-rosbridge) RB=false; shift;;  --port) PORT="$2"; shift 2;;
    --camera) CAMMODE="$2"; shift 2;;  --jpeg-quality) JPEGQ="$2"; shift 2;;  --camera-hz) CAM="$2"; shift 2;;  --lidar-hz) LID="$2"; shift 2;;  --imu-hz) IMU="$2"; shift 2;;
    --odom-hz) ODOM="$2"; shift 2;;  --x) X="$2"; shift 2;;  --y) Y="$2"; shift 2;;  --yaw) YAW="$2"; shift 2;;
    --ready-timeout) READY="$2"; shift 2;;  --world-control) WCTL=true; shift;;
    --) shift; EXTRA=("$@"); break;;
    --profile) shift 2;;
    -h|--help) sed -n 2,12p "$0"; exit 0;;
    *) echo "неизвестный аргумент: $1" >&2; exit 2;;
  esac
done
if [ "$(limactl list --format '{{.Status}}' "$SIM_VM" 2>/dev/null)" != "Running" ]; then
  to 600 limactl start --tty=false "$SIM_VM" >/dev/null
fi
T0=$(date +%s)
LARGS=(world:="$WORLD" rtf:="$RTF" nav2:="$NAV2" rosbridge:="$RB" rosbridge_port:="$PORT"
  camera:="$CAMMODE" camera_hz:="$CAM" jpeg_quality:="$JPEGQ" lidar_hz:="$LID" imu_hz:="$IMU" odom_hz:="$ODOM" yaw:="$YAW"
  world_control:="$WCTL" "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}")
# ros2 launch не принимает пустые значения (ключ:=) — необязательные передаём, только если заданы.
[ -n "$STEP" ] && LARGS+=(step:="$STEP")
[ -n "$X" ] && LARGS+=(x_pose:="$X")
[ -n "$Y" ] && LARGS+=(y_pose:="$Y")
vm 60 ${LAUNCH:+SIM_LAUNCH="$LAUNCH"} bash "$SIM_DIR/vm_start.sh" "${LARGS[@]}" "${EXTRA[@]+"${EXTRA[@]}"}"
RUN="$(vm 20 readlink -f '~/robot_sim/current')"
ready() {
  vm 20 "source $SIM_DIR/vm_env.sh; timeout 5 ros2 topic echo --once /clock --qos-reliability best_effort >/dev/null 2>&1" || return 1
  if [ "$NAV2" = true ]; then
    vm 10 "grep -q 'lifecycle_manager_navigation.*Managed nodes are active' ~/robot_sim/current/launch.log" || return 1
  fi
  if [ "$RB" = true ]; then
    to 15 "$PROJ/.venv/bin/python" "$SIM_DIR/rosbridge_probe.py" --url "ws://127.0.0.1:$PORT" --timeout 5 topics >/dev/null 2>&1 || return 1
  fi
  if [ -n "$READY_CHECK" ]; then
    vm 60 "source $SIM_DIR/vm_env.sh; $READY_CHECK" >/dev/null || return 1
  fi
}
while ! ready; do
  if [ $(( $(date +%s) - T0 )) -ge "$READY" ]; then
    echo "[start_sim] не готово за ${READY} с; лог: limactl shell $SIM_VM -- tail -50 $RUN/launch.log" >&2
    [ -n "$READY_CHECK" ] && vm 90 "source $SIM_DIR/vm_env.sh; $READY_CHECK" >&2
    exit 1
  fi
  if ! vm 10 "kill -0 \$(cat ~/robot_sim/current/pid)" 2>/dev/null; then
    # Дети лидера (gz sim) могут его пережить — гасим всю группу, чтобы следующий запуск не подхватил старый мир.
    echo "[start_sim] ros2 launch завершился; лог: $RUN/launch.log" >&2; vm 10 "tail -30 $RUN/launch.log" >&2
    echo "[start_sim] первые ошибки в журнале:" >&2; vm 10 "grep -m 6 -E 'ERROR|Error' $RUN/launch.log" >&2
    vm 90 bash "$SIM_DIR/vm_stop.sh" >&2; exit 1
  fi
  sleep 2
done
echo "[start_sim] готово за $(( $(date +%s) - T0 )) с: profile=$PROFILE world=$WORLD rtf=$RTF step=${STEP:-world} nav2=$NAV2 rosbridge=$RB(ws://127.0.0.1:$PORT) camera=$CAMMODE@${CAM}Hz run_dir=$RUN"
