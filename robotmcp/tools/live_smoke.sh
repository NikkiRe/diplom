#!/usr/bin/env bash
# Живой смоук robot-mcp на стенде (VM sim) с Mac и с «платы» (VM sbc).
#   robotmcp/tools/live_smoke.sh [OUT] [--only mac,sbc,camera,idle,raw,eager,soak,sbc2,paused] [--no-sim]
# Требует: make -C robotmcp build vm-copy; VM sim с установленным стеком. Симулятор запускается и
# останавливается своими скриптами (scripts/sim/start_sim.sh --nav2 --camera lazy / stop_sim.sh), если не --no-sim.
# Результаты: OUT/<имя>/{probe.json,steps/,tools.json,trace.jsonl,server_stderr.log}, OUT/<имя>.log.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PROJ="$(cd "$HERE/.." && pwd)"
source "$PROJ/scripts/sim/lib.sh"
OUT="$PROJ/experiments/results/robotmcp_smoke_$(date +%Y%m%d)"
ONLY="mac,sbc,camera,idle,raw,eager,soak" SIM=true
while [ $# -gt 0 ]; do
  case "$1" in
    --only) ONLY="$2"; shift 2;; --no-sim) SIM=false; shift;;
    -h|--help) sed -n 2,7p "$0"; exit 0;;
    *) OUT="$1"; shift;;
  esac
done
mkdir -p "$OUT"
has() { [[ ",$ONLY," == *",$1,"* ]]; }
SBC_HOME="$(to 20 limactl shell --workdir / sbc -- printenv HOME)"
if $SIM; then "$PROJ/scripts/sim/start_sim.sh" --nav2 --camera lazy | tail -1; sleep 5; fi
trap '$SIM && "$PROJ/scripts/sim/stop_sim.sh" >/dev/null 2>&1 || true' EXIT

# sbc <имя> <флаги robot-mcp> -- <флаги robot-probe>
sbc() {
  local name="$1"; shift
  local srv="" ; while [ $# -gt 0 ] && [ "$1" != "--" ]; do srv+=" $1"; shift; done; shift || true
  local d="$SBC_HOME/robotmcp_probe/$name" pargs="" x
  for x in "$@"; do pargs+=" $(printf '%q' "$x")"; done
  to 900 limactl shell --workdir / sbc -- bash -c "rm -rf $d; mkdir -p $d; ~/robotmcp/robot-probe \
    -server '$SBC_HOME/robotmcp/robot-mcp -rosbridge ws://{proxy} -trace $d/trace.jsonl -run-id $name$srv' \
    -rosbridge host.lima.internal:9090 -out $d $pargs 2> $d.log; rc=\$?; grep '^\[probe\]' $d.log; exit \$rc" \
    || echo "[live_smoke] $name: ошибка" >&2
  rm -rf "$OUT/$name"; mkdir -p "$OUT/$name"
  to 120 limactl copy -r "sbc:$d/." "$OUT/$name/"
  to 30 limactl copy "sbc:$d.log" "$OUT/$name.log"
}

if has mac; then
  echo "== Mac: compact"
  "$HERE/.build/robot-probe" -server "$HERE/.build/robot-mcp -rosbridge ws://{proxy} -trace $OUT/compact_mac/trace.jsonl -run-id compact_mac" \
    -rosbridge 127.0.0.1:9090 -out "$OUT/compact_mac" -scenario compact 2> "$OUT/compact_mac.log" || true
  grep '^\[probe\]' "$OUT/compact_mac.log"
fi
if has sbc; then echo "== sbc: compact"; sbc compact_sbc -- -scenario compact; fi
if has camera; then echo "== sbc: 10 кадров, пауза 6 с"; sbc camera_sbc -- -scenario camera -reps 10 -gap 6s -reset=false; fi
if has idle; then echo "== sbc: простой 120 с"; sbc idle_sbc -- -scenario idle -gap 120s -reset=false; fi
if has raw; then echo "== sbc: R-raw (сырые /amcl_pose и /scan)"; sbc raw_sbc -payload raw -- -scenario compact -only '^(status|look_summary)$' -reset=false; fi
if has eager; then
  echo "== sbc: «жадный» вариант (скан в движении 2 с, каждая поза AMCL)"
  sbc eager_sbc -motion-scan-ms 2000 -pose-throttle-ms 0 -- -scenario compact -only '^(go_door|return_dock)$'
fi
if has soak; then echo "== sbc: цикл поездок и кадров (PSS во времени)"; sbc soak_sbc -- -scenario soak -reps 6; fi
if has sbc2; then echo "== sbc: compact, повтор"; sbc compact_sbc_r2 -- -scenario compact; fi
if has paused; then   # чтения на паузе мира: без шагов и с -sim-step-iters (стенд перезапускается с --world-control)
  "$PROJ/scripts/sim/stop_sim.sh" >/dev/null 2>&1 || true
  "$PROJ/scripts/sim/start_sim.sh" --nav2 --camera lazy --rtf 0 --world-control | tail -1; sleep 3
  "$PROJ/.venv/bin/python" "$HERE/tools/world_pause.py" pause
  for v in nostep: "simstep:-sim-step-iters 100"; do n=${v%%:*}; f=${v#*:}
    "$HERE/.build/robot-probe" -server "$HERE/.build/robot-mcp -rosbridge ws://{proxy} -trace $OUT/paused_$n/trace.jsonl $f" \
      -rosbridge 127.0.0.1:9090 -out "$OUT/paused_$n" -scenario compact -only '^(status|look_image)$' -reset=false \
      2> "$OUT/paused_$n.log" || true
    grep '^\[probe\]' "$OUT/paused_$n.log"
  done
  "$PROJ/.venv/bin/python" "$HERE/tools/world_pause.py" run
fi
echo "[live_smoke] $OUT"
