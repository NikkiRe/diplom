#!/usr/bin/env bash
# Смоук аналогов: короткий сжатый день (2 ч дня при k = 60 → 2 мин) для PicoClaw (best, shipped), nanobot и,
# для паритета, EdgeCtx — на Mac и (если есть ВМ sbc) в Lima VM «sbc». Последовательно (порты и CPU не делятся).
#   bench/baselines/smoke.sh [каталог_результатов] [--no-vm]
# Числа при параллельной работе других процессов — ориентиры, не результаты.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="${1:-$HERE/smoke/$(date +%Y%m%d-%H%M)}"
NOVM="${2:-}"
PY="$ROOT/.venv/bin/python"
DAY="$HERE/days/smoke_2h_H05_s1.jsonl"
export PYTHONDONTWRITEBYTECODE=1
cd "$ROOT"
[[ -f "$DAY" ]] || "$PY" -m bench.daygen gen --seed 1 --profile H --split tune --window 07:00-09:00 --rate-scale 0.5 --out "$DAY"
mkdir -p "$OUT"
run() { echo "=== $*"; "$PY" -m "$@" --day "$DAY" --k 60 --out "$OUT" --force > "$OUT/$(echo "$*" | tr ' /' '__').log" 2>&1; echo "rc=$?"; }
run bench.baselines.drive_picoclaw --profile best --run-id picoclaw-best-mac
run bench.baselines.drive_picoclaw --profile shipped --run-id picoclaw-shipped-mac
run bench.baselines.drive_nanobot --run-id nanobot-mac
run bench.baselines.drive_edgectx --run-id edgectx-parity-mac
if [[ "$NOVM" != "--no-vm" ]] && limactl list 2>/dev/null | grep -q "^sbc .*Running"; then
  run bench.baselines.drive_picoclaw --profile best --host sbc --run-id picoclaw-best-sbc
  run bench.baselines.drive_nanobot --host sbc --run-id nanobot-sbc
fi
"$PY" -m bench.baselines.report "$OUT" --out "$OUT/table.md"
