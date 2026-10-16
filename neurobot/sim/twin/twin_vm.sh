#!/usr/bin/env bash
# (в ВМ sim) Запуск и остановка двойника: ros2 launch twin.launch.py в своей группе процессов; каталог прогона —
# $TWIN_RUN_ROOT/<ГГГГММДД_ЧЧММСС> (по умолчанию ~/twin_runs), ссылка current — на последний.
#   twin_vm.sh start [ключ:=значение …]   PID лидера (= PGID) → run_dir/pid, домен DDS → run_dir/domain
#   twin_vm.sh stop [run_dir]             SIGINT группе, затем TERM и KILL (stand_procs.py down); без run_dir — current
# Домен DDS — TWIN_DOMAIN, иначе ROS_DOMAIN_ID из vm_env.sh (30, как у стенда и компонентов пакета 1).
set -euo pipefail
TWIN="$(cd "$(dirname "$0")" && pwd)"
source "$TWIN/../../../scripts/sim/vm_env.sh"
ROOT="${TWIN_RUN_ROOT:-$HOME/twin_runs}"
CMD="${1:-}"
[ $# -gt 0 ] && shift
case "$CMD" in
  start)
    if [ -f "$ROOT/current/pid" ] && pgrep -g "$(cat "$ROOT/current/pid")" >/dev/null; then
      echo "[twin] уже запущен: PGID $(cat "$ROOT/current/pid") ($(readlink -f "$ROOT/current")) — twin.sh down" >&2
      exit 2
    fi
    RUN="$ROOT/$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$RUN"
    ln -sfn "$RUN" "$ROOT/current"
    export ROS_DOMAIN_ID="${TWIN_DOMAIN:-$ROS_DOMAIN_ID}" ROS_LOG_DIR="$RUN/ros_log"
    printf '%s\n' "$@" > "$RUN/args.txt"
    echo "$ROS_DOMAIN_ID" > "$RUN/domain"
    setsid nohup ros2 launch "$TWIN/launch/twin.launch.py" run_dir:="$RUN" "$@" > "$RUN/launch.log" 2>&1 < /dev/null &
    echo "$!" > "$RUN/pid"
    date +%s.%N > "$RUN/started_at"
    echo "[twin] PGID $! домен $ROS_DOMAIN_ID run_dir $RUN";;
  stop)
    RUN="$(readlink -f "${1:-$ROOT/current}")"
    [ -f "$RUN/pid" ] || { echo "[twin] нет $RUN/pid — нечего останавливать"; exit 0; }
    python3 "$TWIN/../stand/stand_procs.py" down --pgid "$(cat "$RUN/pid")" && echo "[twin] остановлен: $RUN";;
  *)
    sed -n 2,6p "$0"; exit 2;;
esac
