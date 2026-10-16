#!/usr/bin/env bash
# Smoke доступа агента к роботу через MCP: ros-mcp-server (R-generic) и прототип R-compact, оба на «плате».
#   scripts/sim/rosmcp_smoke.sh [OUT] [--where sbc|mac] [--only generic,compact,cbor,camera,tokens]
#   (camera = camera_generic + camera_compact)
# OUT по умолчанию experiments/results/robot_smoke_<дата>. Перед каждым сценарием с движением симуляция
# перезапускается (робот на dock, поза AMCL = спавн), чтобы прогоны были сопоставимы.
# --where sbc (по умолчанию): MCP-сервер и MCP-клиент (rosmcp_probe.py) — в VM "sbc" (~/.venv-rosmcp), rosbridge —
#   ws://host.lima.internal:9090, результаты копируются в OUT/rosmcp_*_sbc. --where mac: всё на Mac (.venv-rosmcp).
# Требует: scripts/sim/install_rosmcp.sh sbc|mac; VM "sim" с установленным стеком.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
OUT="$PROJ/experiments/results/robot_smoke_$(date +%Y%m%d)"
WHERE=sbc ONLY="generic,compact,camera,cbor,tokens"
while [ $# -gt 0 ]; do
  case "$1" in
    --where) WHERE="$2"; shift 2;;  --only) ONLY="$2"; shift 2;;
    -h|--help) sed -n 2,9p "$0"; exit 0;;
    *) OUT="$1"; shift;;
  esac
done
mkdir -p "$OUT"
[ "$WHERE" = sbc ] && SBC_HOME="$(to 20 limactl shell --workdir / sbc -- printenv HOME)"
has() { [[ ",$ONLY," == *",$1,"* ]]; }
fresh() { "$SIM_DIR/stop_sim.sh" >/dev/null 2>&1 || true; "$SIM_DIR/start_sim.sh" --nav2 | tail -1; sleep 5; }

# probe <имя> <аргументы rosmcp_probe.py…>: запуск клиента там, где живёт MCP-сервер; результат → $OUT/rosmcp_<имя>_<где>
probe() {
  local name="$1"; shift
  local dst="$OUT/rosmcp_${name}_$WHERE"
  rm -rf "$dst"; mkdir -p "$dst"
  if [ "$WHERE" = sbc ]; then
    local args="" x
    for x in "$@"; do args+=" $(printf '%q' "$x")"; done
    to 900 limactl shell --workdir / sbc -- bash -c "rm -rf ~/rosmcp_probe/$name; mkdir -p ~/rosmcp_probe; \
      ~/.venv-rosmcp/bin/python $SIM_DIR/rosmcp_probe.py --rosbridge host.lima.internal:9090 \
      --out ~/rosmcp_probe/$name $args > ~/rosmcp_probe/$name.stdout 2> ~/rosmcp_probe/$name.log; rc=\$?; \
      grep '^\[probe\]' ~/rosmcp_probe/$name.log; exit \$rc" || { echo "[rosmcp_smoke] $name: ошибка" >&2; }
    to 120 limactl copy -r "sbc:$SBC_HOME/rosmcp_probe/$name/." "$dst/"
    to 30 limactl copy "sbc:$SBC_HOME/rosmcp_probe/$name.log" "$dst.log"
  else
    to 900 "$PROJ/.venv/bin/python" "$SIM_DIR/rosmcp_probe.py" --rosbridge 127.0.0.1:9090 --out "$dst" "$@" \
      2> "$dst.log" >/dev/null || echo "[rosmcp_smoke] $name: ошибка" >&2
    grep '^\[probe\]' "$dst.log" || true
  fi
}

if [ "$WHERE" = sbc ]; then PY="$SBC_HOME/.venv-rosmcp/bin/python"; ROSMCP="$SBC_HOME/.venv-rosmcp/bin/ros-mcp"
else PY="$PROJ/.venv-rosmcp/bin/python"; ROSMCP="$PROJ/.venv-rosmcp/bin/ros-mcp"; fi
COMPACT="$PY $SIM_DIR/robot_mcp_compact.py --rosbridge ws://{proxy}"

if has generic; then echo "== R-generic (ros-mcp-server)"; fresh; probe generic --scenario generic --server-cmd "$ROSMCP"; fi
if has compact; then echo "== R-compact"; fresh; probe compact --scenario compact --server-cmd "$COMPACT"; fi
if has cbor; then
  echo "== R-compact, /scan в JSON (без CBOR)"
  probe compact_json --scenario compact --only 'status$|look_summary' --no-reset --server-cmd "$COMPACT --compression none"
fi
if has camera || has camera_generic; then
  echo "== кадр по запросу (ros-mcp subscribe_once), ленивая камера: 10 попыток, пауза 6 с"
  probe camera_generic --scenario camera-generic --reps 10 --gap 6 --no-reset --server-cmd "$ROSMCP"
fi
if has camera || has camera_compact; then
  echo "== кадр по запросу (R-compact look(image=true)), ленивая камера: 10 попыток, пауза 6 с"
  probe camera_compact --scenario camera-compact --reps 10 --gap 6 --no-reset --server-cmd "$COMPACT"
fi
if has tokens; then
  echo "== токены одного сообщения каждой темы"
  to 300 "$PROJ/.venv/bin/python" "$SIM_DIR/topic_tokens.py" --out "$OUT/topic_tokens" 2>&1 | grep '^\[tokens\]' || true
fi
if [ -d "$OUT/rosmcp_generic_$WHERE" ] && [ -d "$OUT/rosmcp_compact_$WHERE" ]; then
  cam=()
  [ -d "$OUT/rosmcp_camera_generic_$WHERE" ] && cam+=(--camera-generic "$OUT/rosmcp_camera_generic_$WHERE")
  [ -d "$OUT/rosmcp_camera_compact_$WHERE" ] && cam+=(--camera-compact "$OUT/rosmcp_camera_compact_$WHERE")
  "$PROJ/.venv/bin/python" "$SIM_DIR/rosmcp_analyze.py" --generic "$OUT/rosmcp_generic_$WHERE" \
    --compact "$OUT/rosmcp_compact_$WHERE" "${cam[@]+"${cam[@]}"}" --out "$OUT/rosmcp_analysis_$WHERE" >/dev/null
  echo "[rosmcp_smoke] сводка: $OUT/rosmcp_analysis_$WHERE/summary.md"
fi
