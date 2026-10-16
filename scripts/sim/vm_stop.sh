#!/usr/bin/env bash
# (в VM) Остановить симуляцию, запущенную vm_start.sh: SIGINT группе процессов, затем TERM, затем KILL.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/vm_env.sh" >/dev/null 2>&1 || true
PIDF="$SIM_RUN_ROOT/current/pid"
[ -f "$PIDF" ] || { echo "[vm_stop] нет $PIDF"; exit 0; }
PID="$(cat "$PIDF")"
# vm_start запускает ros2 launch через setsid: PID лидера = PGID; дети остаются в группе и после выхода лидера.
PGID="$PID"
if ! pgrep -g "$PGID" >/dev/null; then echo "[vm_stop] процессов группы $PGID нет"; rm -f "$PIDF"; exit 0; fi
# INT — штатное завершение ros2 launch (Nav2 под нагрузкой гасит lifecycle-узлы до ~10–20 с).
for sig in INT TERM KILL; do
  kill -"$sig" -- "-$PGID" 2>/dev/null || true
  case "$sig" in INT) n=60;; TERM) n=20;; *) n=10;; esac
  for _ in $(seq 1 "$n"); do pgrep -g "$PGID" >/dev/null || break; sleep 0.5; done
  [ -n "${SIM_STOP_DEBUG:-}" ] && { echo "после $sig:"; ps -o pid,stat,comm -g "$PGID"; }
  pgrep -g "$PGID" >/dev/null || { echo "[vm_stop] остановлено ($sig), PGID $PGID"; rm -f "$PIDF"; exit 0; }
done
echo "[vm_stop] не удалось остановить PGID $PGID:"; pgrep -a -g "$PGID"; exit 1
