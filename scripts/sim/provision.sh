#!/usr/bin/env bash
# Запустить install_ros.sh в VM "sim" в фоне с логом (долгая установка, ~2–3 ГБ). Прогресс: tail -f "$LOG".
set -euo pipefail
source "$(dirname "$0")/lib.sh"
LOG="${1:-$PROJ/experiments/results/robot_smoke_install/install.log}"
mkdir -p "$(dirname "$LOG")"
nohup perl -e 'alarm shift; exec @ARGV' 5400 \
  limactl shell --workdir / "$SIM_VM" -- bash "$SIM_DIR/install_ros.sh" > "$LOG" 2>&1 &
echo "$!" > "$(dirname "$LOG")/install.pid"
echo "[provision] PID $! лог $LOG"
