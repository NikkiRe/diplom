#!/bin/bash
# Сквозная проверка cam-mcp в ВМ Lima "sbc" (Ubuntu 24.04 arm64, общая для нескольких агентов):
# статический бинарник linux/arm64 в transient scope systemd с CPUQuota=25 % и MemoryMax=64M — грубая
# модель слабой платы; сеанс JSON-RPC по stdio: initialize → подписка на cam://state/cam1 → ожидание
# сцены → camera_status, motion_state, snapshot. Печатает уведомления, эпизоды, итог run_end и память:
# anon (память процесса), VmHWM (пик RSS, в нём и текст бинарника) и memory.peak scope (в нём ещё и кэш
# страниц бинарника — зависит от того, читался ли файл раньше). Все числа — ориентир, не результат: ВМ на
# ноутбуке, параллельная нагрузка.
#   synthetic — синтетика configs/scene.smoke.json (≈40 с реального времени);
#   v4l2      — виртуальная камера ядра vivid (YUYV): тест источника, затем ≈12 с работы cam-mcp;
#               модуль загружается и выгружается скриптом (если его уже кто-то загрузил — отказ).
# Запуск с Mac: make vm-smoke | make vm-v4l2. OUT=<новый каталог на Mac> — сохранить журнал, ответы
# сервера, память и sha256 бинарника (существующий каталог не перезаписывается).
set -euo pipefail
MODE=${1:-synthetic}
VM=${VM:-sbc}
HERE=$(cd "$(dirname "$0")/.." && pwd)
VMOUT=/tmp/cammcp-smoke-$$
case $MODE in synthetic | v4l2) ;; *) echo "usage: $0 [synthetic|v4l2]" >&2; exit 2 ;; esac
if [ -n "${OUT:-}" ] && [ -e "$OUT" ]; then echo "OUT exists, not overwriting: $OUT" >&2; exit 1; fi
trap 'limactl shell --workdir / "$VM" -- sudo rm -rf "$VMOUT"' EXIT

limactl shell --workdir / "$VM" -- sudo bash -s "$HERE" "$MODE" "$VMOUT" <<'INVM'
set -euo pipefail
HERE=$1 MODE=$2 OUT=$3
B=$HERE/.build/cam-mcp-linux-arm64
UNIT=cammcp-smoke-$$
LOADED=
cleanup() { if [ -n "$LOADED" ]; then modprobe -r vivid; fi; }
trap cleanup EXIT
mkdir -p "$OUT"
sha256sum "$B" | cut -d' ' -f1 > "$OUT/cam-mcp.sha256"
if [ "$MODE" = synthetic ]; then
  ARGS=(-scene "$HERE/configs/scene.smoke.json")
  WAIT=38
else
  if lsmod | grep -q '^vivid'; then echo "vivid is already loaded by someone else" >&2; exit 1; fi
  modprobe vivid n_devs=1 node_types=0x1 num_inputs=1 input_types=0x0
  LOADED=1
  sleep 1
  DEV=/dev/$(basename "$(dirname "$(grep -l vivid /sys/class/video4linux/video*/name | head -1)")")
  echo "--- source.test on $DEV"
  (cd "$HERE/internal/source" && CAMMCP_V4L2_DEVICE=$DEV "$HERE/.build/vm/source.test" -test.run V4L2Device -test.v)
  ARGS=(-source v4l2 -v4l2-device "$DEV")
  WAIT=12
fi
{
  echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"sh","version":"1"}}}'
  sleep 0.3
  echo '{"jsonrpc":"2.0","method":"notifications/initialized"}'
  echo '{"jsonrpc":"2.0","id":2,"method":"resources/subscribe","params":{"uri":"cam://state/cam1"}}'
  sleep "$WAIT"
  echo '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"camera_status","arguments":{}}}'
  echo '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"motion_state","arguments":{}}}'
  echo '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"snapshot","arguments":{}}}'
  sleep 0.5
  S=/sys/fs/cgroup/system.slice/$UNIT.scope
  { echo "memory.peak $(cat "$S/memory.peak")"; grep -E '^(anon|file) ' "$S/memory.stat"
    grep VmHWM "/proc/$(head -1 "$S/cgroup.procs")/status"; } > "$OUT/memory.txt"
} | systemd-run --scope --quiet --unit "$UNIT" -p CPUQuota=25% -p MemoryMax=64M \
    "$B" "${ARGS[@]}" -trace "$OUT/trace.jsonl" -run-id "vm-smoke-$MODE" > "$OUT/out.jsonl"

echo "--- notifications (время от старта, с)"
jq -s -c '(.[0].ts_clock) as $t0 | .[] | select(.type == "notify") | {event, t: ((.ts_clock - $t0) * 10 | round / 10)}' "$OUT/trace.jsonl"
jq -c 'select(.method == "notifications/resources/updated") | .params._meta | {event: ."cam/event", state}' "$OUT/out.jsonl"
echo "--- episodes"
jq -s -c '(.[0].ts_clock) as $t0 | .[] | select(.type == "episode_end") | .episode
  | {start: ((.start - $t0) * 10 | round / 10), end: ((.end - $t0) * 10 | round / 10), peak_area_pct, zone}' "$OUT/trace.jsonl"
echo "--- camera_status / motion_state"
jq -r 'select(.id == 3 or .id == 4) | .result.content[0].text' "$OUT/out.jsonl"
echo "--- snapshot"
jq -c 'select(.id == 5) | .result | {meta: .content[0].text, image: .content[1].mimeType, base64_chars: (.content[1].data | length)}' "$OUT/out.jsonl"
echo "--- run_end"
jq -c 'select(.type == "run_end") | .stats | {frames, skipped, errors, episodes, fps, grab_us, detect_us, detect_us_max, cpu_s, cpu_ms_per_frame}' "$OUT/trace.jsonl"
echo "--- memory (bytes; VmHWM in kB), cam-mcp $(cut -c1-12 "$OUT/cam-mcp.sha256")"
cat "$OUT/memory.txt"
INVM

if [ -n "${OUT:-}" ]; then
  mkdir -p "$OUT"
  for f in trace.jsonl out.jsonl memory.txt cam-mcp.sha256; do limactl copy "$VM:$VMOUT/$f" "$OUT/$f"; done
  echo "saved to $OUT"
fi
