#!/usr/bin/env bash
# (в VM) Запуск launch/robot_sim.launch.py в фоне, своя группа процессов; PID → $SIM_RUN_ROOT/current/pid.
# Аргументы — пары ключ:=значение для ros2 launch (world:=…, rtf:=…, nav2:=true, camera_hz:=…).
# SIM_LAUNCH — другой launch-файл (профиль стенда, start_sim.sh --profile); путь пишется в run_dir/launch_file.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/vm_env.sh"
LAUNCH="${SIM_LAUNCH:-$HERE/launch/robot_sim.launch.py}"
RUN="$SIM_RUN_ROOT/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RUN"
# Живой считается вся группа процессов (PGID = PID лидера): лидер ros2 launch мог выйти, а его gz sim — остаться.
if [ -f "$SIM_RUN_ROOT/current/pid" ] && pgrep -g "$(cat "$SIM_RUN_ROOT/current/pid")" >/dev/null; then
  PID="$(cat "$SIM_RUN_ROOT/current/pid")"
  if kill -0 "$PID" 2>/dev/null; then
    echo "[vm_start] уже запущено: PID $PID ($(readlink -f "$SIM_RUN_ROOT/current"))" >&2
  else
    echo "[vm_start] остались процессы прошлого запуска (PGID $PID, $(readlink -f "$SIM_RUN_ROOT/current")):" \
      "$(pgrep -d ' ' -g "$PID") — остановите: scripts/sim/stop_sim.sh" >&2
  fi
  exit 2
fi
ln -sfn "$RUN" "$SIM_RUN_ROOT/current"
printf '%s\n' "$@" > "$RUN/args.txt"
echo "$LAUNCH" > "$RUN/launch_file"
export ROS_LOG_DIR="$RUN/ros_log"
setsid nohup ros2 launch "$LAUNCH" run_dir:="$RUN" "$@" > "$RUN/launch.log" 2>&1 < /dev/null &
echo "$!" > "$RUN/pid"
date +%s.%N > "$RUN/started_at"
echo "[vm_start] PID $! run_dir $RUN"
