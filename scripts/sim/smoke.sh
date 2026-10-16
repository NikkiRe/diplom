#!/usr/bin/env bash
# Сквозная проверка стенда «робот»: старт с Nav2 → темы/частоты → движение по cmd_vel → кадр по запросу →
# navigate_to_pose (с feedback и без) → доступность rosbridge через host.lima.internal → стоп.
#   scripts/sim/smoke.sh [out_dir]   (по умолчанию experiments/results/robot_smoke_<YYYYMMDD>/smoke)
set -euo pipefail
source "$(dirname "$0")/lib.sh"
OUT="${1:-$PROJ/experiments/results/robot_smoke_$(date +%Y%m%d)/smoke}"
mkdir -p "$OUT"
PY="$PROJ/.venv/bin/python"; PROBE="$SIM_DIR/rosbridge_probe.py"
step() { echo "--- $*"; }
"$SIM_DIR/stop_sim.sh" >/dev/null 2>&1 || true
step "старт: turtlebot3_world + Nav2 (camera lazy)"
"$SIM_DIR/start_sim.sh" --nav2 --ready-timeout 150 | tee "$OUT/start.txt"
sleep 5
step "темы через rosbridge (с хоста)"
to 30 "$PY" "$PROBE" --out "$OUT/topics.json" topics >/dev/null
step "частоты тем и RTF (в VM, подписчик и на JPEG камеры)"
vm 90 "source $SIM_DIR/vm_env.sh; python3 $SIM_DIR/topic_rates.py --seconds 15 --topics /scan /odom /imu /camera/image_raw/compressed /camera/camera_info /tf /joint_states /cmd_vel" > "$OUT/rates_vm.json"
step "cmd_vel → /odom: вперёд 0,2 м/с 3 с; поворот 0,5 рад/с 3 с"
to 60 "$PY" "$PROBE" --out "$OUT/drive_forward.json" drive --vx 0.2 --seconds 3 >/dev/null
to 60 "$PY" "$PROBE" --out "$OUT/drive_turn.json" drive --vx 0 --wz 0.5 --seconds 3 >/dev/null
step "кадр по запросу (JPEG, ленивая камера)"
to 120 "$PY" "$PROBE" --out "$OUT/snapshot_jpeg.json" snapshot --n 10 --gap 3 >/dev/null
step "navigate_to_pose: (1.8, 0.5) без feedback, затем (-2.0, -0.5) с feedback"
to 200 "$PY" "$PROBE" --out "$OUT/nav1_nofeedback.json" nav --x 1.8 --y 0.5 --yaw 0 --no-feedback --nav-timeout 180 >/dev/null
to 200 "$PY" "$PROBE" --out "$OUT/nav2_feedback.json" nav --x -2.0 --y -0.5 --yaw 0 --nav-timeout 180 >/dev/null
step "rosbridge через host.lima.internal:9090 (тот же путь, что у VM sbc: шлюз usernet → loopback хоста → проброс)"
vm 30 "cd /tmp && python3 $PROBE --url ws://host.lima.internal:9090 topics" > "$OUT/reach_host_lima_internal.json"
step "стоп"
"$SIM_DIR/stop_sim.sh"
"$PY" - "$OUT" <<'PY'
import json, pathlib, sys
d = pathlib.Path(sys.argv[1]); j = lambda n: json.loads((d / n).read_text())
r = j("rates_vm.json"); t = r["topics"]
s = {
 "rtf": r.get("rtf"),
 "hz_sim": {k: v.get("hz_sim") for k, v in t.items()},
 "drive_forward_m": j("drive_forward.json")["moved_m"], "drive_turn_rad": j("drive_turn.json")["turned_rad"],
 "snapshot_jpeg": {"p50_s": j("snapshot_jpeg.json")["latency_s"]["p50"], "bytes": j("snapshot_jpeg.json")["bytes_per_frame"]},
 "nav": {n: {k: j(n).get(k) for k in ("succeeded", "wall_s", "ground_truth_err_m", "feedback_msgs", "rx_bytes_until_result")}
         for n in ("nav1_nofeedback.json", "nav2_feedback.json")},
 "reach_host_lima_internal_topics": len(j("reach_host_lima_internal.json")["topics"]),
}
(d / "summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=1) + "\n")
print(json.dumps(s, ensure_ascii=False, indent=1))
PY
