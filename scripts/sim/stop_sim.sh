#!/usr/bin/env bash
# Остановить симуляцию в VM "sim" (группа процессов ros2 launch, затем демон ros2). --vm — ещё и остановить VM.
set -uo pipefail
source "$(dirname "$0")/lib.sh"
[ "$(limactl list --format '{{.Status}}' "$SIM_VM" 2>/dev/null)" = "Running" ] || { echo "[stop_sim] VM $SIM_VM не запущена"; exit 0; }
vm 90 bash "$SIM_DIR/vm_stop.sh"
vm 20 "source $SIM_DIR/vm_env.sh; ros2 daemon stop >/dev/null 2>&1 || true"
if [ "${1:-}" = "--vm" ]; then to 300 limactl stop "$SIM_VM"; fi
