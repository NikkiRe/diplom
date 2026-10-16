#!/usr/bin/env bash
# Дымовой прогон sysmon в Lima VM: нагрузка (bash → python, растущий на 2 МиБ/с, + sleep) в transient scope
# с memory.max = 256 МиБ; sysmon снаружи группы меряет дерево и cgroup раз в секунду. Печатает сводку.
# Это проверка работоспособности, а не измерение: числа не являются результатами.
set -euo pipefail
cd "$(dirname "$0")/.."
VM=${VM:-sbc}
BIN=$PWD/bin/sysmon-linux-arm64
[[ -x $BIN ]] || { echo "нет $BIN: make build-linux-arm64"; exit 1; }
limactl shell "$VM" -- bash -s "$BIN" <<'INNER'
set -euo pipefail
BIN=$1
U=sysmon-smoke-$RANDOM
OUT=/tmp/$U.jsonl
systemd-run --user --scope --quiet --unit="$U" -p MemoryMax=256M bash -c '
  python3 -c "
import time
buf = []
for _ in range(12):
    buf.append(bytearray(2 * 1024 * 1024))   # +2 МиБ анонимной памяти в секунду
    time.sleep(1)
" &
  sleep 30 &
  wait' &
sleep 1
CG=$(find /sys/fs/cgroup/user.slice -type d -name "$U.scope" | head -1)
ROOT=$(head -1 "$CG/cgroup.procs")
"$BIN" --pid "$ROOT" --cgroup auto --interval 1s --count 8 --system --out "$OUT"
kill $(cat "$CG/cgroup.procs") 2>/dev/null || true
python3 - "$OUT" <<'PY'
import json, sys
recs = [json.loads(l) for l in open(sys.argv[1])]
h, samples, end = recs[0], recs[1:-1], recs[-1]
print(f"backend={h['backend']} kernel={h['kernel']} page={h['page_size']} clk_tck={h['clk_tck']} cgroup={h['target']['cgroup']}")
for s in samples:
    t, cg = s["tree"], s.get("cgroup", {})
    print(f"t={s['t_s']:5.1f}s procs={t['procs']} pss={t.get('pss_kib',0)/1024:6.1f}MiB rss={t['rss_kib']/1024:6.1f}MiB "
          f"cg.cur={cg.get('memory_current_kib',0)/1024:6.1f}MiB cg.max={cg.get('memory_max_kib',0)/1024:.0f}MiB "
          f"cpu={t['cpu_s']:.2f}s ctxsw={t.get('ctxsw',{}).get('vol')} collect={s['self']['collect_ms']:.2f}ms errors={s.get('errors')}")
print("end:", json.dumps(end, ensure_ascii=False))
pss = [s["tree"]["pss_kib"] for s in samples]
assert all(b >= a for a, b in zip(pss, pss[1:])), "PSS растущей нагрузки должна расти"
assert all(not s.get("errors") for s in samples), "ошибки чтения"
print("OK")
PY
rm -f "$OUT"
INNER
