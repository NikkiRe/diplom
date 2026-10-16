#!/usr/bin/env bash
# Матрица конфигураций симулятора → нагрузка (CPU/память VM и хоста, RTF, частоты тем).
#   scripts/sim/load_matrix.sh <out_dir> [секунды замера=20] [прогрев=10]
# Каждая конфигурация: start_sim.sh … → прогрев → measure_load.sh → stop_sim.sh. Итог — <out_dir>/summary.md.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
OUT="$1"; SEC="${2:-20}"; WARM="${3:-10}"
mkdir -p "$OUT"
# Без /camera/*: в режиме camera=lazy любая подписка на камеру (даже на camera_info) включает рендер.
TOPICS="/scan /odom /imu /joint_states /tf"
STREAMT="$TOPICS /camera/camera_info"            # stream: камера рендерится всегда, частоту видно по camera_info
WATCH="$TOPICS /camera/image_raw/compressed"   # «агент смотрит в камеру» — подписчик на JPEG весь замер
# метка|аргументы start_sim.sh|темы замера (пусто — $TOPICS)
CONFIGS=(
  "A_stream30|--camera stream --camera-hz 30|$STREAMT"
  "B_stream5|--camera stream --camera-hz 5|$STREAMT"
  "C_lazy30_idle|--camera lazy --camera-hz 30|"
  "D_lazy30_watch|--camera lazy --camera-hz 30|$WATCH"
  "E_lazy5_watch|--camera lazy --camera-hz 5|$WATCH"
  "F_cam_off|--camera off|"
  "G_cam_off_lidar_off|--camera off --lidar-hz 0|"
  "H_lazy_idle_rtf0|--camera lazy --rtf 0|"
  "I_cam_off_lidar_off_rtf0|--camera off --lidar-hz 0 --rtf 0|"
  "J_lazy_idle_rtf0_step4ms|--camera lazy --rtf 0 --step 0.004|"
  "K_stream30_nav2|--camera stream --camera-hz 30 --nav2|$STREAMT"
  "L_lazy_idle_nav2|--camera lazy --nav2|"
  "M_house_lazy_idle|--world turtlebot3_house --camera lazy|"
  "N_house_lazy5_watch|--world turtlebot3_house --camera lazy --camera-hz 5|$WATCH"
  "O_empty_lazy_idle|--world empty_world --camera lazy|"
)
"$SIM_DIR/stop_sim.sh" >/dev/null 2>&1 || true
for c in "${CONFIGS[@]}"; do
  IFS='|' read -r label args topics <<<"$c"
  topics="${topics:-$TOPICS}"
  # ONLY=<регулярка> — прогнать только подходящие конфигурации.
  if [ -n "${ONLY:-}" ] && ! [[ "$label" =~ $ONLY ]]; then continue; fi
  echo "=== $label: $args"
  # shellcheck disable=SC2086
  if ! "$SIM_DIR/start_sim.sh" $args --ready-timeout 150; then echo "[$label] старт не удался"; "$SIM_DIR/stop_sim.sh" || true; continue; fi
  sleep "$WARM"
  # shellcheck disable=SC2086
  "$SIM_DIR/measure_load.sh" "$OUT/$label.json" "$SEC" $topics || echo "[$label] замер не удался"
  "$SIM_DIR/stop_sim.sh" >/dev/null || true
done
"$PROJ/.venv/bin/python" "$SIM_DIR/summarize_load.py" "$OUT" > "$OUT/summary.md"
cat "$OUT/summary.md"
