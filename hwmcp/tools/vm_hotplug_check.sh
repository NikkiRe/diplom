#!/bin/bash
# Горячее отключение датчика на настоящем sysfs (ВМ Lima "sbc", Ubuntu 24.04 arm64): устройства модуля ядра
# iio_dummy создаются и удаляются через configfs. hw-mcp (-root /, stdio) видит пропажу (null, absent,
# уведомление, sample_error), а вернувшееся под новым номером iio:deviceN устройство находит повторной
# инвентаризацией (sensor_rebound, sample_recovered).
# Запуск с Mac: tools/vm_hotplug_check.sh   (собирает статические бинарники linux/arm64 в .build)
# Устройства получают имена hwmcp-hp-<pid>-* и удаляются при выходе (trap); нужен sudo в ВМ. Проверка не
# начинается на ядре с ненулевым tainted, после неё tainted проверяется снова.
set -euo pipefail
VM=${VM:-sbc}
HERE=$(cd "$(dirname "$0")/.." && pwd)
cd "$HERE"
mkdir -p .build/vm
make -s build-linux-arm64 >/dev/null
GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go test -c -o .build/vm/hw-mcp.test ./cmd/hw-mcp
limactl shell "$VM" -- sudo bash -s "$HERE" <<'INVM'
set -euo pipefail
cd "$1"
P=hwmcp-hp-$$
DUMMY=/sys/kernel/config/iio/devices/dummy
untainted() {
	local t
	t=$(cat /proc/sys/kernel/tainted)
	echo "kernel tainted=$t"
	[ "$t" = 0 ]
}
untainted || { echo "kernel is already tainted: check not started" >&2; exit 1; }
cleanup() {
	trap - EXIT
	set +e # убрать все устройства, даже если какое-то не удаляется
	for d in "$DUMMY/$P"-*; do
		[ -d "$d" ] && rmdir "$d"
	done
	untainted || exit 1
}
trap cleanup EXIT
modprobe iio_dummy
echo "kernel $(uname -r); iio_dummy devices with prefix $P"
HWMCP_IIO_DUMMY=$P HWMCP_BIN=$PWD/.build/hw-mcp-linux-arm64 \
	./.build/vm/hw-mcp.test -test.v -test.count=1 -test.run IIODummyHotplug
cleanup
echo "hot-plug check: OK"
INVM
