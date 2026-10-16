#!/usr/bin/env bash
# Замер нагрузки текущей симуляции: в VM — vm_load.py (CPU/PSS процессов, RTF, частоты тем),
# на хосте — CPU и RSS процесса Virtualization.framework этой VM (по времени старта рядом с hostagent).
#   scripts/sim/measure_load.sh <out.json> [секунды=20] [темы...]
set -euo pipefail
source "$(dirname "$0")/lib.sh"
OUT="$1"; SEC="${2:-20}"; shift || true; shift || true
TOPICS="${*:-}"
HA_PID="$(cat "$HOME/.lima/$SIM_VM/ha.pid")"
epoch() { date -j -f "%a %b %d %T %Y" "$(ps -o lstart= -p "$1" | sed 's/  */ /g; s/ *$//')" +%s; }
HA_T="$(epoch "$HA_PID")"
VZ_PID=""; best=999999
for p in $(pgrep -f 'com.apple.Virtualization.VirtualMachine'); do
  d=$(( $(epoch "$p") - HA_T )); d=${d#-}
  if [ "$d" -lt "$best" ]; then best=$d; VZ_PID=$p; fi
done
cpu_s() { ps -o time= -p "$1" | awk -F'[:.]' '{ if (NF==4) print $1*3600+$2*60+$3+$4/100; else print $1*60+$2+$3/100 }'; }
H0=$(cpu_s "$VZ_PID"); T0=$(perl -MTime::HiRes=time -e 'printf "%.3f", time')
VMJSON="$(vm $((SEC + 90)) "source $SIM_DIR/vm_env.sh; python3 $SIM_DIR/vm_load.py --seconds $SEC ${TOPICS:+--topics $TOPICS}")"
H1=$(cpu_s "$VZ_PID"); T1=$(perl -MTime::HiRes=time -e 'printf "%.3f", time')
RSS_KB=$(ps -o rss= -p "$VZ_PID" | tr -d ' ')
ARGS="$(vm 10 'cat ~/robot_sim/current/args.txt' | tr '\n' ' ')"
mkdir -p "$(dirname "$OUT")"
TMPJ="$(mktemp)"; printf '%s' "$VMJSON" > "$TMPJ"
python3 - "$OUT" "$H0" "$H1" "$T0" "$T1" "$RSS_KB" "$VZ_PID" "$ARGS" "$TMPJ" <<'PY'
import json, sys
out, h0, h1, t0, t1, rss, pid, args, vmj = sys.argv[1:]
vm = json.load(open(vmj))
res = {"launch_args": args.split(), "host": {"vz_pid": int(pid), "vz_cpu_cores": round((float(h1) - float(h0)) / (float(t1) - float(t0)), 3),
       "vz_rss_mib": round(int(rss) / 1024), "note": "CPU процесса VM на хосте (12 ядер M4 Pro), включает гостевое ядро и virtio"},
       "vm": vm}
json.dump(res, open(out, "w"), ensure_ascii=False, indent=1)
r = vm.get("rates", {})
print(f"[measure_load] {out}: RTF={r.get('rtf')} VM CPU={vm['vm_cpu_cores_busy']} ядер, sim={vm['sim_cpu_cores']} ядер/{vm['sim_pss_mib']} МиБ PSS, хост VZ={res['host']['vz_cpu_cores']} ядер/{res['host']['vz_rss_mib']} МиБ")
PY
rm -f "$TMPJ"
