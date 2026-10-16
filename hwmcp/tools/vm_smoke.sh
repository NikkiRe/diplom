#!/bin/bash
# Сквозная проверка hw-mcp на настоящем sysfs Linux (ВМ Lima "sbc", Ubuntu 24.04 arm64):
# статический бинарник linux/arm64, настоящий /dev/gpiochip0 (PL061 QEMU, линии 0 и 1 свободны),
# сеанс JSON-RPC по stdio: initialize -> write_property (allow) -> повтор раньше min_interval (rate_limit)
# -> read_property входа -> board_health -> list_devices; журнал печатается в конце.
# Запуск с Mac:  make build-linux-arm64 && tools/vm_smoke.sh
# Меняет уровень линии 1 на gpiochip0 ВМ (нужен sudo в ВМ); на плате не запускать.
set -euo pipefail
VM=${VM:-sbc}
HERE=$(cd "$(dirname "$0")/.." && pwd)
limactl shell "$VM" -- sudo bash -s "$HERE" <<'INVM'
set -euo pipefail
cd "$1"
B=.build/hw-mcp-linux-arm64
TRACE=$(mktemp /tmp/hwmcp-smoke.XXXXXX.jsonl)
send() { echo "$1"; sleep 0.3; }
{
send '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"sh","version":"1"}}}'
send '{"jsonrpc":"2.0","method":"notifications/initialized"}'
send '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"write_property","arguments":{"device":"vm_relay","property":"state","value":true}}}'
send '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"write_property","arguments":{"device":"vm_relay","property":"state","value":false}}}'
send '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"read_property","arguments":{"device":"vm_button","property":"state"}}}'
send '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"board_health","arguments":{}}}'
send '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"list_devices","arguments":{"all":true}}}'
} | $B -root / -declared configs/declared.vm.json -trace "$TRACE" -run-id vm-smoke
echo "--- trace"
cat "$TRACE"
rm -f "$TRACE"
INVM
