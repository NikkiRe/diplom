#!/usr/bin/env bash
# Создать и запустить Lima VM "sim" (Ubuntu 24.04 arm64, vz, 6 vCPU, 8 ГБ, 40 ГБ) по шаблону sim.lima.yaml.
# Идемпотентно: если VM уже есть — только запускает. VM "sbc" не трогает.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
if limactl list -q 2>/dev/null | grep -qx "$SIM_VM"; then
  echo "[create_vm] VM $SIM_VM уже существует"
else
  to 600 limactl create --name "$SIM_VM" --tty=false "$SIM_DIR/sim.lima.yaml"
fi
if [ "$(limactl list --format '{{.Status}}' "$SIM_VM")" != "Running" ]; then
  to 900 limactl start --tty=false "$SIM_VM"
fi
limactl list "$SIM_VM"
